#!/usr/bin/env python3
"""死锁现场取证器（只读，不动任何服务）。

背景：b7 引擎出现过的挂起 = 服务循环停在 batched prefill layer-1，GPU 空闲、CPU 池闲置，
重启即愈但**事后无线索**。本脚本盯三件事：
  (a) serve 日志最后一行的进度数字（"N of max M tokens" / "reading the prompt: N tokens, Ts"）
      —— 有在途请求却 100 秒不动 ⇒ 判挂起；
  (b) 挂起瞬间抓现场：nvidia-smi、每线程状态/wchan/内核栈（sudo）、/v1/status、日志尾部
      → 写 ~/logs/deadlock-forensics-<道>-<时间>.txt；
  (c) [2026-11-01 新增] 引擎进程缺失检测（lane A SIGSEGV 后 90 分钟没人踢的教训）：
      /health loaded=false 连续 N 轮 ⇒ 报警；DEADLOCK_KICK=1 时另发一发最小请求触发
      server.py 的「下个请求自动重拉」。**默认关**（重拉=几分钟占内存，属服务动作，人工确认后再开）。
教训钉正：v1 在引擎消失时拿 pid "0" 去读 /proc/0/task，FileNotFoundError 把看门狗自己打死
（2026-11-01 19:02 lane A 挂 90 分钟无人报警的放大器）。本版原则 = **看门狗绝不自杀**：
每轮 try/except、pid 缺失走安全分支、取证失败只记日志。
用法：nohup python3 -u tools/deadlock_watch.py >> ~/logs/deadlock-watch.log 2>&1 &
停止：kill 掉即可（它对服务零影响）。"""
import datetime
import os
import re
import subprocess
import time

TARGETS = {
    "a": {"out": "/home/cloud/deploy/logs/strata-a.out.log",
          "port": 8091, "prof": "/home/cloud/deploy/engines/profiles/ssebed/strata-a.json"},
    "b": {"out": "/home/cloud/deploy/logs/strata-b.out.log",
          "port": 8092, "prof": "/home/cloud/deploy/engines/profiles/ssebed/strata-b.json"},
}
STALL_S = 100
POLL = 15
OUT_DIR = os.path.expanduser("~/logs")
PROG = re.compile(r"(\d+) of max \d+ tokens|reading the prompt: (\d+) tokens")
KICK = os.environ.get("DEADLOCK_KICK") == "1"      # 默认关：自动踢只发请求，但仍算服务动作
MISS_ROUNDS = 4                                    # loaded=false 连续几轮才认定「引擎缺失」(4*15s)


def _proc_cmd(pid: int) -> str:
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace").replace("\0", " ")
    except OSError:
        return ""


def _children(pid: int):
    try:
        return [int(x) for x in open(f"/proc/{pid}/task/{pid}/children").read().split()]
    except OSError:
        return []


def engine_pids() -> dict:
    """lane -> 引擎 pid。走可信父链：server.py(--port N) 的子进程里找 `strata --serve`。
    （v1 用 environ 里的 step-stats 标记认道，重启方式一变就认不出 ⇒ 回退 "0" 打死自己。）"""
    out = {}
    for lane, cfg in TARGETS.items():
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            cmd = _proc_cmd(int(d))
            if "serve/server.py" in cmd and f"--port {cfg['port']}" in cmd:
                for ch in _children(int(d)):
                    ec = _proc_cmd(ch)
                    if "build/strata" in ec and "--serve" in ec:
                        out[lane] = ch
                        break
    return out


def health_loaded(lane: str):
    """True/False=已知加载状态，None=问不到（server.py 本身没起）。"""
    port = TARGETS[lane]["port"]
    p = subprocess.run(["curl", "-s", "--max-time", "5", f"http://127.0.0.1:{port}/health"],
                       capture_output=True, text=True)
    if not p.stdout.strip():
        return None
    return '"loaded": true' in p.stdout


def kick(lane: str):
    """发一发最小请求触发 server.py 的「下个请求自动重拉引擎」。KICK=1 才会走到这里。"""
    import json as _json
    prof = TARGETS[lane]["prof"]
    try:
        key = _json.load(open(prof))["api_key"]
    except Exception as e:  # noqa: BLE001
        print(f"[deadlock_watch] lane {lane}: 取 profile key 失败，不踢：{e!r}", flush=True)
        return
    body = '{"messages":[{"role":"user","content":"ping"}],"max_tokens":1}'
    subprocess.run(["curl", "-s", "--max-time", "5", "-o", "/dev/null",
                    "-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json",
                    "-d", body, f"http://127.0.0.1:{TARGETS[lane]['port']}/v1/chat/completions"])
    print(f"[deadlock_watch] lane {lane}: 已发最小请求（触发引擎自动重拉）", flush=True)


def tail_line(path):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 4096))
            lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def dump(lane, line, pid):
    """抓现场。pid 可为 None（引擎已消失）——此时只留外围证据，**绝不碰 /proc/0**。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    fp = os.path.join(OUT_DIR, f"deadlock-forensics-{lane}-{ts}.txt")
    def sh(cmd):
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15).stdout
        except Exception as e:  # noqa: BLE001
            return f"<err {e}>\n"
    txt = [f"# lane {lane} 疑似挂起 @ {ts}（判据：{STALL_S}s 无进度；最后日志行）\n{line}\n",
           "\n## nvidia-smi\n", sh("nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv"),
           sh("nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader"),
           "\n## /v1/status\n", sh(f"curl -s --max-time 5 http://127.0.0.1:{TARGETS[lane]['port']}/v1/status | head -c 800")]
    if pid:
        txt.append(f"\n## engine pid={pid} 线程状态/wchan/内核栈\n")
        try:
            for t in sorted(os.listdir(f"/proc/{pid}/task")):
                try:
                    st = open(f"/proc/{pid}/task/{t}/stat").read().split()
                    w = open(f"/proc/{pid}/task/{t}/wchan").read().strip()
                    txt.append(f"tid={t} state={st[2]} wchan={w}\n")
                except OSError:
                    pass
            txt.append(sh(f"sudo timeout 10 cat /proc/{pid}/task/*/stack 2>/dev/null | head -120"))
        except OSError as e:
            txt.append(f"\n<读 /proc/{pid} 失败：{e!r}>\n")
    else:
        txt.append("\n## 引擎进程不存在（崩溃后未重拉），无线程栈可取；查 serve 日志与 dmesg\n")
        txt.append(sh("dmesg 2>/dev/null | tail -15"))
    txt.append("\n## 引擎 stderr 尾部\n")
    txt.append(sh(f"tail -30 /home/cloud/SSEStraBed/logs/strata-iq3_xxs-{lane}.log | cut -c1-160"))
    try:
        with open(fp, "w") as f:
            f.write("".join(txt))
        print(f"[deadlock_watch] lane {lane} 挂起取证 → {fp}", flush=True)
    except OSError as e:
        print(f"[deadlock_watch] lane {lane} 取证写文件失败：{e!r}", flush=True)


def main():
    last = {}    # lane -> (进度签名, 最后变化时刻)
    armed = {}   # lane -> 已取证冷却
    miss = {}    # lane -> loaded=false 连续轮数
    print(f"[deadlock_watch] started（kick={'开' if KICK else '关'}）", flush=True)
    while True:
        try:
            pids = engine_pids()
        except Exception as e:  # noqa: BLE001
            print(f"[deadlock_watch] engine_pids 失败（跳过本轮）：{e!r}", flush=True)
            pids = {}
        for lane, cfg in TARGETS.items():
            try:
                # (c) 引擎缺失检测：server.py 在、引擎不在（loaded=false）——连续多轮则报警/可自动踢
                if lane not in pids:
                    ld = health_loaded(lane)
                    if ld is False:
                        miss[lane] = miss.get(lane, 0) + 1
                        if miss[lane] == MISS_ROUNDS:
                            print(f"[deadlock_watch] ⚠ lane {lane}: /health loaded=false 持续 "
                                  f"{MISS_ROUNDS * POLL}s，引擎缺失（崩溃后没人踢）——"
                                  f"server.py 会在下一个真实请求时自动重拉"
                                  f"{'（已发最小请求触发）' if KICK else '；要自动踢：DEADLOCK_KICK=1 重启本脚本'}",
                                  flush=True)
                            if KICK:
                                kick(lane)
                    else:
                        miss[lane] = 0
                else:
                    miss[lane] = 0
                # (a)(b) 挂起取证
                line = tail_line(cfg["out"])
                m = PROG.search(line)
                if not m:
                    continue
                sig = m.group(0)
                now = time.time()
                prev = last.get(lane)
                if prev and prev[0] == sig and now - prev[1] > STALL_S and now - armed.get(lane, 0) > 600:
                    dump(lane, line, pids.get(lane))
                    armed[lane] = now
                    last[lane] = (sig, now)   # 重置计时，防刷屏
                elif not prev or prev[0] != sig:
                    last[lane] = (sig, now)
            except Exception as e:  # noqa: BLE001  看门狗绝不自杀：单道出错只记日志
                print(f"[deadlock_watch] lane {lane} 本轮异常（继续跑）：{e!r}", flush=True)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
