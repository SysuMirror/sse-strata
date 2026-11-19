#!/usr/bin/env python3
"""MTP 深度受控扫描（lane B 普道 8092；2026-10-31 已获用户同意，低峰外执行）。

臂（profile args 增量，其余保持现网）：
  A0 基线（现状：varlen on，无 floor，max-rows 默认 8）
  A2/A3/A4/A5 --mtp-chain-floor 2/3/4/5 + --max-rows 20（S=4 时可分别强制每槽 2/3/4/5 行）

每臂：改 profile → supervisorctl restart strata-b → 等 loaded → 3 轮暖机（弃）→
测量轮（greedy×4 流 3 轮、temp0.6×4 流 3 轮、temp0.6×2 流 3 轮，256 token，盐值提示词）→
离线切段 step-stats-b 的窗口桶（含 legs 腿分解与 why 计数）。
结束/异常都会把 profile 恢复为基线并重启验证。

用法：python3 mtp_depth_sweep.py [run|restore] [--only A2,A3]
结果：~/logs/mtp-sweep-<UTC日期>.json（每臂增量落盘）
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

CONF = "/home/cloud/deploy/supervisord.conf"
SCTL = "/home/cloud/.local/bin/supervisorctl"
PROFILE = "/home/cloud/deploy/engines/profiles/ssebed/strata-b.json"
BACKUP = PROFILE + ".pre-sweep"
STATS = "/home/cloud/logs/step-stats-b.jsonl"
PORT = 8092
TOKENS = 256
DAY = time.strftime("%Y%m%d")
RESULT = f"/home/cloud/logs/mtp-sweep-{DAY}.json"
TOPICS = ["讲讲注意力机制的来历", "介绍一下操作系统的内存管理", "为什么天空是蓝色的",
          "解释量子隧穿效应", "写一首关于秋天的短诗", "推荐三本科普书并说明理由",
          "比较 TCP 和 UDP 的适用场景", "用通俗语言解释梯度下降"]

# 方法学修正（v5 教训）：L_max = min(spec+2, max_rows/S)，floor 只抬不压——之前所有臂共用
# max_rows=20，等于每臂都开宽窗（A2 也炸），floor 形同虚设。定深度就要**逐臂钉行预算**：
# A{d} 的 max_rows = 4×d，使 S=4 的 L_max 恰为 d；floor=d 防规划器掉到 d 以下。
ARMS = {
    "A0": {"floor": None, "max_rows": None, "max_t": None},   # 现网基线（rows 8，规划器自选）
    "A2": {"floor": 2, "max_rows": 8, "max_t": None},         # S=4 → L=2（≤8 行，A0 已证安全）
    "A3": {"floor": 3, "max_rows": 12, "max_t": 3},           # S=4 → L=3（12 行宽窗）
    "A4": {"floor": 4, "max_rows": 16, "max_t": 4},           # S=4 → L=4
    "A5": {"floor": 5, "max_rows": 20, "max_t": 5},           # S=4 → L=5（草稿器深度上限也要抬）
}


def load_profile():
    with open(PROFILE) as f:
        return json.load(f)


def set_arg(args, name, value):
    if name in args:
        i = args.index(name)
        if value is None:
            del args[i]
            if i < len(args) and not str(args[i]).startswith("--"):
                del args[i]
            return
        if i + 1 < len(args) and not str(args[i + 1]).startswith("--"):
            args[i + 1] = str(value)
        else:
            args.insert(i + 1, str(value))
    elif value is not None:
        args.extend([name, str(value)])


def apply_arm(arm):
    spec = ARMS[arm]
    d = load_profile()
    set_arg(d["args"], "--mtp-chain-floor", spec["floor"])
    set_arg(d["args"], "--max-rows", spec["max_rows"])
    set_arg(d["args"], "--mtp-max-t", spec.get("max_t"))
    tmp = PROFILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROFILE)


def vram_used_b():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                              "-i", "1"], capture_output=True, text=True, timeout=8).stdout
        return float(out.split()[0]) / 1024.0
    except Exception:  # noqa: BLE001
        return 999.0


def restart_and_wait(timeout=1800):
    # 已知的 stop→start 竞态（10-27 记档）：旧进程显存未释放时新进程 vision 起不来。教训（第二轮）：
    # vision 瞬崩时 supervisor 的 autorestart 会在几十秒内自愈（引擎加载本身要 ~8 分钟），
    # 把中间态 EXITED 当失败重踢反而把好进程踢死。正确姿势：stop → 等显存落 → start 一次 →
    # 只认 health；仅当 supervisor 报 STOPPED/FATAL 且 3 分钟无任何响应时才补一次 start。
    subprocess.run([SCTL, "-c", CONF, "stop", "strata-b"], check=False)
    t_rel = time.time()
    while time.time() - t_rel < 120 and vram_used_b() > 6.0:
        time.sleep(2)
    subprocess.run([SCTL, "-c", CONF, "start", "strata-b"], check=False)
    t0 = time.time()
    last_resp = time.time()
    last_kick = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=4) as r:
                h = json.loads(r.read().decode())
            last_resp = time.time()
            if h.get("status") == "ok" and h.get("loaded"):
                return time.time() - t0
        except Exception:  # noqa: BLE001
            pass
        s = subprocess.run([SCTL, "-c", CONF, "status", "strata-b"], capture_output=True, text=True).stdout
        if ("STOPPED" in s or "FATAL" in s) and time.time() - last_resp > 180 and time.time() - last_kick > 180:
            subprocess.run([SCTL, "-c", CONF, "start", "strata-b"], check=False)
            last_kick = time.time()
        time.sleep(5)
    raise RuntimeError(f"lane B not ready in {timeout}s")


def fire(topic, seed, temp, key):
    body = json.dumps({"model": "qwen3.8-flash-next-iq3_xxs",
                       "messages": [{"role": "user",
                                      "content": f"[探测 {seed}] " + topic + f"，请在回答末尾复述编号 {seed}"}],
                       "max_tokens": TOKENS, "temperature": temp, "seed": seed}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/v1/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    wall = time.time() - t0
    return wall, d.get("usage", {}).get("completion_tokens", 0)


def run_round(streams, temp, seed0, key):
    res = []
    ths = []

    def go(i):
        try:
            res.append(fire(TOPICS[i % len(TOPICS)], seed0 + i * 7919, temp, key))
        except Exception as e:  # noqa: BLE001
            print("   round err:", repr(e), flush=True)
    for i in range(streams):
        t = threading.Thread(target=go, args=(i,))
        t.start()
        ths.append(t)
        time.sleep(0.15)
    for t in ths:
        t.join()
    toks = sum(r[1] for r in res)
    wall = max((r[0] for r in res), default=0.001)
    return {"streams": streams, "temp": temp, "agg_tps": round(toks / wall, 1),
            "per_stream": [round(r[1] / r[0], 1) for r in res]}


def stats_slice(off):
    """window buckets from STATS offset `off` (bytes) to now."""
    buckets = {}
    size = os.path.getsize(STATS)
    if off > size:
        off = 0
    with open(STATS, "rb") as f:
        f.seek(off)
        f.readline()
        for line in f:
            i = line.find(b'{"t"')
            if i < 0:
                continue
            try:
                r = json.loads(line[i:])
            except Exception:  # noqa: BLE001
                continue
            k = r.get("kind")
            if k not in ("chain", "batch"):
                continue
            L = max(1, round(r.get("rows", 1) / max(1, r.get("slots", 1))))
            key = (k, r.get("slots"), L)
            b = buckets.setdefault(key, {"n": 0, "ms": [], "acc": 0, "act": 0,
                                         "wait": 0.0, "pool": 0.0, "drain": 0.0,
                                         "miss": 0, "why": {}})
            b["n"] += 1
            b["ms"].append(r.get("step_ms", 0.0))
            b["acc"] += r.get("accepted") or 0
            ctx = r.get("ctx") or []
            b["act"] += sum(1 for c in ctx if isinstance(c, int) and c >= 0)
            lg = r.get("legs") or {}
            b["wait"] += lg.get("wait", 0.0)
            b["pool"] += lg.get("pool", 0.0)
            b["drain"] += lg.get("drain", 0.0)
            b["miss"] += lg.get("misses", 0)
            w = lg.get("why")
            if w:
                b["why"][w] = b["why"].get(w, 0) + 1
    out = {}
    for (k, s, L), b in buckets.items():
        if b["n"] < 10:
            continue
        ms = sorted(b["ms"])
        med = ms[len(ms) // 2]
        toks = (b["acc"] + b["act"]) / b["n"]     # 每窗提交 = 平均接受 + 平均活跃槽（各 1 golden）
        fkey = f"{k}|S{s}|L{L}"
        out[fkey] = {"n": b["n"], "ms_med": round(med, 1),
                     "tok_per_ms": round(toks / max(med, 0.001), 4), "toks_per_win": round(toks, 2),
                     "acc_per_win": round(b["acc"] / b["n"], 2),
                     "wait": round(b["wait"] / b["n"], 1), "pool": round(b["pool"] / b["n"], 1),
                     "drain": round(b["drain"] / b["n"], 1), "miss/win": round(b["miss"] / b["n"], 1),
                     "why": b["why"]}
    return out


def restore():
    if os.path.exists(BACKUP):
        d = json.load(open(BACKUP))
        tmp = PROFILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PROFILE)
        return restart_and_wait()   # 它自己会 stop→等显存→start，不必先 restart（早先双触发白白多等一轮加载）
    return None


def arm_run(arm, key, results, save):
    print(f"===== arm {arm}: {ARMS[arm]} =====", flush=True)
    apply_arm(arm)
    up = restart_and_wait()
    print(f"  ready in {up:.0f}s", flush=True)
    time.sleep(20)  # 引擎空转稳定
    off = None
    rounds = []
    seq = [("warm", 4, 0.0), ("warm", 4, 0.0), ("warm", 4, 0.6),
           ("greedy4", 4, 0.0), ("greedy4", 4, 0.0), ("greedy4", 4, 0.0),
           ("sampled4", 4, 0.6), ("sampled4", 4, 0.6), ("sampled4", 4, 0.6),
           ("sampled2", 2, 0.6), ("sampled2", 2, 0.6), ("sampled2", 2, 0.6)]
    for tag, st, tp in seq:
        if tag == "greedy4" and off is None:
            off = os.path.getsize(STATS)   # 统计从测量轮起算，暖机窗口不掺桶
        r = run_round(st, tp, int(time.time()) % 90000 + len(rounds) * 131, key)
        # [SSE 10-31 v4 教训] 引擎中途被打死（宽窗崩溃）时别对着尸体刷轮：立刻断臂
        if len(r["per_stream"]) < st:
            raise RuntimeError(f"probe requests failing ({r['per_stream']}/{st} ok) — engine likely dead")
        r["tag"] = tag
        rounds.append(r)
        print(f"  {tag}: agg {r['agg_tps']} tok/s per-stream {r['per_stream']}", flush=True)
        time.sleep(8)
    results[arm] = {"arm": arm, "spec": ARMS[arm], "ready_s": round(up), "rounds": rounds,
                    "measured": {"greedy4": [x["agg_tps"] for x in rounds if x["tag"] == "greedy4"],
                                 "sampled4": [x["agg_tps"] for x in rounds if x["tag"] == "sampled4"],
                                 "sampled2": [x["agg_tps"] for x in rounds if x["tag"] == "sampled2"]},
                    "windows": stats_slice(off)}
    save()
    print(f"  arm {arm} done", flush=True)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].split(",")
    if mode == "restore":
        s = restore()
        print(f"restored + ready in {s:.0f}s" if s else "restored")
        return
    if not os.path.exists(BACKUP):
        d = load_profile()
        with open(BACKUP, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print("backup written:", BACKUP)
    key = load_profile()["api_key"]
    results = {}
    if os.path.exists(RESULT):
        try:
            results = json.load(open(RESULT))
        except Exception:  # noqa: BLE001
            results = {}

    def save():
        tmp = RESULT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RESULT)

    def sig(_s, _f):
        print("\n!! interrupted: restoring baseline ...", flush=True)
        try:
            restore()
        finally:
            save()
            sys.exit(3)
    signal.signal(signal.SIGINT, sig)
    signal.signal(signal.SIGTERM, sig)

    try:
        for arm in (only or ARMS.keys()):
            try:
                arm_run(arm, key, results, save)
            except Exception as ex:  # noqa: BLE001
                results[arm] = {"arm": arm, "spec": ARMS[arm], "failed": repr(ex)}
                print(f"  arm {arm} FAILED: {ex!r} — 继续下一臂", flush=True)
                save()
    finally:
        print("===== restoring baseline =====", flush=True)
        try:
            restore()
        except Exception as e:  # noqa: BLE001
            print("RESTORE FAILED:", repr(e), "— run: python3 this restore", flush=True)
        save()
    print("sweep complete:", RESULT, flush=True)


if __name__ == "__main__":
    main()
