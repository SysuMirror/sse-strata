#!/usr/bin/env python3
"""端到端 A/B：结构优先分块 vs 旧的按长度硬切（同一份语料、同一个窗口、真嵌入+真重排）。

跑法：/home/cloud/Strata/.venv/bin/python test_structure_e2e.py [--turns 30]

为什么这样测：1M 精度报告的结论是"精度损失全部发生在检索环节，模型侧没有短板"，所以这里用
mock_upstream.py 当引擎——它只在**事实真的进了窗口**时才答对（oracle），于是召回率纯粹反映
网关的检索/组装质量：可复现、不烧 GPU、不碰生产代理、也不碰真引擎。

语料刻意做成报告里最难的那两件事的叠加：
  * **agent 形态**：每轮 = 用户输入 → 工具调用 → 工具返回 → agent 回答，6 条事实埋在**工具返回**里；
  * **填充雷同**：非事实轮的工具返回逐字相同（报告 §2.6 的"块不可区分 → 张冠李戴"）。
提问时问的是 "the <label> code"，oracle 只有在窗口里找到 `the <label> code is <CODE>` 才答得对——
检索带回相似的另一条事实同样算失败（就是报告里的张冠李戴）。

断言：新分块的召回不低于旧分块，且组装请求里系统提示词不丢、检索块带回合结构标签与锚点、
没有行中间劈开的碎片、提示词不超预算。
"""
import argparse
import json
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = "/home/cloud/Strata/.venv/bin/python"
KEY = Path("/home/cloud/logs/strata-api-key.txt").read_text().strip()
MOCK_PORT = 8099

TOPICS = ["城市地铁的调度原理", "咖啡豆的烘焙曲线", "候鸟迁徙的导航机制", "桥梁的抗震设计", "茶多酚的氧化路径",
          "射电望远镜的干涉测量", "疫苗的冷链运输", "珊瑚礁的生态修复", "锂电池的热失控边界", "榫卯的受力模型"]
FACTS = [(1, "vault", "ZEBRA-7734"), (3, "gate", "FALCON-1122"), (8, "pod", "ORCHID-5566"),
         (15, "harbor", "TUNDRA-8899"), (22, "lab", "COBALT-3141"), (28, "radio", "MEADOW-2718")]
ABSENT = ("submarine", "What is the submarine code? Answer with just the code, or say UNKNOWN if it was never given.")
SYSTEM = "You are a deployment agent. Use tools to look things up; never invent values."

# 填充工具返回：形状完全一样、内容逐轮略不同（真实工具输出的样子）。
# 报告 §2.6 的"块雷同"由**事实句式完全一样**这一层提供（6 条事实都是 `the X code is Y`）。
def log_lines(turn: int) -> str:
    return "\n".join(f"line {j}: seq={turn} status=ok stage={j // 20} retry={j % 3} checked by deploy-agent"
                     for j in range(120))


KNOWN_LINES = set(log_lines(1).split("\n"))


def build_turns(n: int) -> list:
    """agent 形态的会话：system 在 messages 里（OpenAI 形态）——旧组装会把它整条丢掉。"""
    msgs = [{"role": "system", "content": SYSTEM}]
    facts = {t: (lab, code) for t, lab, code in FACTS if t <= n}
    for i in range(1, n + 1):
        if i in facts:
            label, code = facts[i]
            user = f"请用工具查一下 {label} 的访问码并记下来。"
            tool = f'vault record for "{label}": the {label} code is {code}.\n' + log_lines(i)
        else:
            topic = TOPICS[i % len(TOPICS)]
            user = f"请用工具查一下{topic}的归档状态并摘要。"
            tool = log_lines(i)
        msgs.append({"role": "user", "content": user})
        msgs.append({"role": "assistant", "content": "我用工具查一下。", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"q": i})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": tool})
        msgs.append({"role": "assistant", "content": "已记录，继续。"})   # 不复述密码：让召回只反映检索质量
    return msgs


def _call(req, timeout, tries=6):
    """本机 HTTP：偶发的 getaddrinfo/连接抖动不该让整个测试挂掉，重试几次。"""
    for k in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(0.5 * (k + 1))


def post(url, payload, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {KEY}")
    return _call(req, timeout)


def get(url, timeout=20):
    return _call(urllib.request.Request(url), timeout, tries=10)


def chat(base, messages, max_tokens=48):
    r = post(f"{base}/v1/chat/completions",
             {"model": "strata", "messages": messages, "max_tokens": max_tokens, "temperature": 0})
    return r["choices"][0]["message"].get("content") or ""


def start_mock(record: Path):
    record.write_text("")
    p = subprocess.Popen([PY, str(HERE / "mock_upstream.py"), "--port", str(MOCK_PORT),
                          "--record", str(record)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            get(f"http://127.0.0.1:{MOCK_PORT}/health", timeout=2)
            return p
        except Exception:
            time.sleep(0.2)
    raise SystemExit("mock upstream did not start")


def start_proxy(port: int, overrides: dict, name: str, script: str = "rag_proxy.py"):
    cfg = json.loads((HERE / "config.json").read_text())
    cfg.update(overrides)
    cfg["listen_port"] = port
    cfg["upstream"] = f"http://127.0.0.1:{MOCK_PORT}"
    cfg["upstreams"] = [f"http://127.0.0.1:{MOCK_PORT}"]
    cfg["model_routes"] = {"strata": f"http://127.0.0.1:{MOCK_PORT}"}
    cfg["default_upstream"] = f"http://127.0.0.1:{MOCK_PORT}"
    # 推理记录照开，但**落点搬到临时目录**：不往线上的索引/分段目录里写测试数据
    cfg.setdefault("infer_store", {}).update({"dir": f"/tmp/rag-proxy-{name}-segs",
                                              "db": f"/tmp/rag-proxy-{name}.db",
                                              "minio_prefix": "infer-test-struct"})
    p = HERE / f"config-test-{name}.json"
    p.write_text(json.dumps(cfg, indent=1))
    log = open(f"/tmp/rag_proxy_{name}.log", "w")
    proc = subprocess.Popen([PY, str(HERE / script), "--config", str(p)], stdout=log, stderr=log)
    for _ in range(50):
        try:
            get(f"http://127.0.0.1:{port}/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"proxy {name} did not start - see /tmp/rag_proxy_{name}.log")


def drain(port: int, secs=600):
    """等嵌入真正排空——否则会话里一半块没有向量，检索候选直接少一半，测出来的差异是假的。"""
    for _ in range(int(secs / 0.4)):
        h = get(f"http://127.0.0.1:{port}/health")
        c = h["counters"]
        if h["embed_queue"] == 0 and c["embeds_queued"] <= c["embeds_done"] + c["embed_errors"]:
            return h
        time.sleep(0.4)
    return get(f"http://127.0.0.1:{port}/health")


# ---- 组装请求的结构体检 -------------------------------------------------------------

def prompt_text(body: dict) -> str:
    out = []
    for m in body.get("messages") or []:
        c = m.get("content")
        out.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    return "\n".join(out)


def system_kept(body: dict) -> bool:
    """系统提示词必须作为**一条 role=system 的消息**留在最前——不是"文本碰巧出现在某处"
    （旧版正是靠把 system 当普通内容检索回来才"看起来在"，位置、角色、完整性都没有保证）。"""
    ms = body.get("messages") or []
    if not ms:
        return False
    first = ms[0]
    if first.get("role") != "system":
        return False
    c = first.get("content")
    return SYSTEM in (c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))


def mid_line_cuts(body: dict) -> int:
    """数一数提示词里有多少行是某条日志行的**后半截**（= 块边界落在行中间，语序失真的直接证据）。"""
    lines = prompt_text(body).split("\n")
    full = [l for l in lines if l.startswith("line ")]
    n = 0
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("line ", "---", "[", "{", "vault record")):
            continue
        if len(s) > 8 and any(k.endswith(s) and len(s) < len(k) for k in full):
            n += 1
    return n


def run_mode(name: str, port: int, script: str, turns: int, budget: int, recent: int, ttl: int = 0,
             drift: bool = False, debug: bool = False):
    """script 决定跑的是**哪一份网关实现**——新代码 rag_proxy.py，或改动前的备份
    rag_proxy.py.bak-*。整份实现一起对照才有意义：系统提示词保住在组装里，不在分块开关里。"""
    print(f"\n===== {name}（{script}）=====")
    rec = Path(f"/tmp/mock_reqs_{name}.jsonl")
    mock = start_mock(rec)
    try:
        proc = start_proxy(port, {"max_context": budget, "recent_window_tokens": recent,
                                  "gen_reserve_default": 1024, "gen_reserve_max": 2048,
                                  "memory_min_tokens": 512, "chunk_tokens": 768,
                                  "retrieve_candidates": 200, "rerank_candidates": 200,
                                  "embed_wait_s": 90, "assembly_ttl_s": ttl,
                                  "rerank_cache_ttl_s": max(ttl, 10800),
                                  "block_drift_rebuild": drift, "debug": debug}, name, script)
        base = f"http://127.0.0.1:{port}"
        try:
            msgs = build_turns(1)
            t0 = time.time()
            for n in range(1, turns + 1):        # 逐轮到达（真实 agent 流量）
                msgs = build_turns(n)
                chat(base, msgs + [{"role": "user", "content": "继续。"}], max_tokens=8)
                drain(port, 120)
            h = drain(port)
            sess = max(h["sessions"], key=lambda x: x["chunks"]) if h["sessions"] else {}
            print(f"  灌入 {turns} 轮 {time.time()-t0:.0f}s: chunks={sess.get('chunks')} "
                  f"vectors={sess.get('vectors')} 嵌入调用={h['counters']['embeds_queued']} "
                  f"嵌入失败={h['counters']['embed_errors']} "
                  f"assembled={h['counters']['assembled']} reused={h['counters']['assembly_reused']}")

            # 提问阶段（只问这份语料里真的埋过的事实）
            facts = [(t, l, c) for t, l, c in FACTS if t <= turns]
            got = {}
            for _, label, code in facts:
                out = chat(base, msgs + [{"role": "user",
                                          "content": f"What is the {label} code? Answer with just the code."}], 32)
                got[label] = (code in out, out)
            out_absent = chat(base, msgs + [{"role": "user", "content": ABSENT[1]}], 32)
            refused = "UNKNOWN" in out_absent.upper() or not re.search(r"[A-Z]+-\d{4}", out_absent)

            # 判据的另一半：块**已经覆盖**当前问题时必须照旧复用（否则等于每问都重建，白付前缀缓存）。
            before = get(f"{base}/health")["counters"]
            t_rep = time.time()
            out_rep = chat(base, msgs + [{"role": "user",
                                          "content": f"What is the {facts[-1][1]} code? Answer with just the code."}], 32)
            dt_rep = time.time() - t_rep
            after = get(f"{base}/health")["counters"]
            rebuilt_again = after.get("block_rebuilds", 0) - before.get("block_rebuilds", 0)
            print(f"  同问题追问（{facts[-1][1]}，块已覆盖）: {out_rep[:24]!r} {dt_rep:.2f}s "
                  f"→ 额外重建 {rebuilt_again} 次（期望 0）")
            metrics_extra = {"repeat_rebuilt": rebuilt_again, "repeat_ok": facts[-1][2] in out_rep,
                             "repeat_s": dt_rep}

            # 最后一批请求 = 提问阶段的组装请求
            reqs = [json.loads(l) for l in rec.read_text().splitlines() if l.strip()]
            asks = [r for r in reqs if "Answer with just the code" in prompt_text(r["body"])]
            last = asks[-1] if asks else (reqs[-1] if reqs else None)

            hits = sum(1 for v, _ in got.values() if v)
            print(f"  召回 {hits}/{len(facts)}  " + " ".join(
                f"{l}:{'OK' if v else 'MISS'}" for l, (v, _) in got.items()))
            print(f"  无中生有对照: {'PASS（未编造）' if refused else 'FAIL（编造了 ' + out_absent[:40] + '）'}")
            for l, (v, o) in got.items():
                if not v:
                    print(f"    miss {l}: 答成 {o[:60]!r}")

            metrics = {"recall": hits, "refused": refused, "queued": h["counters"]["embeds_queued"],
                       "chunks": sess.get("chunks"), "assembled": h["counters"]["assembled"],
                       "degraded": h["counters"]["degraded"],
                       "block_rebuilds": after.get("block_rebuilds", 0), **metrics_extra}
            if last:
                body = last["body"]
                pt = prompt_text(body)
                metrics.update({
                    "prompt_tokens": last["prompt_tokens"],
                    "within_budget": last["prompt_tokens"] <= budget,
                    "system_kept": system_kept(body),
                    "turn_labels": pt.count("--- turn "),
                    "mid_line_cuts": mid_line_cuts(body),
                })
                print(f"  组装请求: {last['prompt_tokens']} tok（窗口 {budget}，"
                      f"{'在窗内' if metrics['within_budget'] else '⚠ 超窗'}）"
                      f" | 系统提示词 {'在' if metrics['system_kept'] else '⚠ 丢了'}"
                      f" | 回合结构标签 {metrics['turn_labels']} 处"
                      f" | 行中间劈开 {metrics['mid_line_cuts']} 处")
                # 检索带回的事实块有没有出处（回合标签 + 那条用户问题）
                for _, label, code in facts:
                    if f"the {label} code is {code}" in pt:
                        i = pt.index(f"the {label} code is {code}")
                        head = pt[:i]
                        lab = re.findall(r"--- turn (\d+) · ([^-\n]+) ---", head)
                        anchor = "访问码" in head[head.rfind("--- turn"):] if lab else False
                        print(f"    {label}: 在窗内，归属 turn {lab[-1][0] if lab else '?'}"
                              f"（{lab[-1][1].strip() if lab else '无标签'}）"
                              f"{'，同回合的用户问题也带回来了' if anchor else '，⚠ 没带回锚点'}")
                        break
            return metrics
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
    finally:
        mock.send_signal(signal.SIGTERM)
        mock.wait(timeout=10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=30)
    ap.add_argument("--budget", type=int, default=16384)
    ap.add_argument("--recent", type=int, default=4096)
    ap.add_argument("--ttl", type=int, default=0, help="组装块复用 TTL（秒）；0=每问都重建（纯测检索）")
    ap.add_argument("--drift", action="store_true", help="开启覆盖度判据（block_drift_rebuild）")
    ap.add_argument("--debug", action="store_true", help="打开网关 debug 日志（看判据为什么触发/不触发）")
    ap.add_argument("--old-script", default="rag_proxy.py.bak-20261008",
                    help="对照用的旧实现（默认＝本次改动前的备份）")
    a = ap.parse_args()

    # ttl=0：每个请求都重建检索块 → 测的是**检索/组装质量本身**。
    # ttl>0：生产语义（TTL 内复用组装块，前缀缓存友好）→ 顺带量出"复用"对召回的代价。
    new = run_mode("new", 8086, "rag_proxy.py", a.turns, a.budget, a.recent, a.ttl, a.drift, a.debug)
    old = run_mode("old", 8087, a.old_script, a.turns, a.budget, a.recent, a.ttl)

    print("\n===== 对照 =====")
    rows = [("召回（事实进窗）", f"{new['recall']}/{len(FACTS)}", f"{old['recall']}/{len(FACTS)}"),
            ("嵌入调用次数（越少=复用越好）", new["queued"], old["queued"]),
            ("会话块数", new["chunks"], old["chunks"]),
            ("组装请求 token", new.get("prompt_tokens"), old.get("prompt_tokens")),
            ("提示词在窗内", new.get("within_budget"), old.get("within_budget")),
            ("系统提示词保留", new.get("system_kept"), old.get("system_kept")),
            ("行中间劈开的碎片数", new.get("mid_line_cuts"), old.get("mid_line_cuts")),
            ("判据触发的重建次数", new.get("block_rebuilds", 0), old.get("block_rebuilds", 0)),
            ("已覆盖的追问仍复用（额外重建）", new.get("repeat_rebuilt"), old.get("repeat_rebuilt")),
            ("降级次数", new["degraded"], old["degraded"])]
    print(f"  {'指标':34s} {'新（结构优先）':>16s} {'旧（按长度）':>14s}")
    for k, x, y in rows:
        print(f"  {k:34s} {str(x):>16s} {str(y):>14s}")

    fails = []
    if a.ttl == 0:
        if new["recall"] < old["recall"]:
            fails.append(f"召回退化: 新 {new['recall']} < 旧 {old['recall']}")
    else:
        # TTL 复用是**两份实现共有**的既有行为（TTL 判定不看问题变没变），不是本次改动引入的回归，
        # 所以这里只报数不判失败——见 README §5e 末尾"顺带查出的一个真问题"。
        print(f"\n  注：TTL={a.ttl}s 时召回由**组装块跨问题复用**主导（新旧都掉），"
              f"这是既有行为；`block_drift_rebuild` 可以关掉它，代价是前缀缓存失效。")
    if a.drift and new.get("repeat_rebuilt", 0) > 0:
        fails.append(f"判据过度重建：块已覆盖的追问仍重建 {new['repeat_rebuilt']} 次（前缀缓存白丢）")
    if a.drift and not new.get("repeat_ok"):
        fails.append("块已覆盖的追问答错了")
    if not new.get("system_kept"):
        fails.append("系统提示词在组装后丢失")
    if not new.get("within_budget"):
        fails.append("组装后的提示词超窗")
    if new.get("mid_line_cuts", 0) > 0:
        fails.append(f"新分块仍有 {new['mid_line_cuts']} 处行中间劈开")
    if new.get("turn_labels", 0) == 0:
        fails.append("检索块没有回合结构标签")
    if not new["refused"]:
        fails.append("无中生有对照被编造")
    if new["degraded"]:
        fails.append(f"出现降级 degraded={new['degraded']}")

    print("=" * 62)
    if fails:
        print("FAIL: " + "; ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
