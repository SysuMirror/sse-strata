#!/usr/bin/env python3
"""Long-context test set through the RAG proxy (port 8080) - the ~1M-token production shape.

The engine's window is 262144; the proxy (which uses the intranet bge-m3 / bge-reranker-v2-m3 documented in
~/rag.md) assembles anything longer down to the window.  This test drives a client-style conversation of
~1M tokens - the whole history re-sent every turn - and then asks a question set:

  * 6 unique facts planted at widely different depths (turn 1 .. near the end),
  * 1 question whose answer was never in the history (the model must not invent one).

Every fact must come back verbatim; the absent one must be refused.  One line a turn; ends with the verdicts,
timings, and the proxy's own counters (assembled / assembly_reused).

  /home/cloud/Strata/.venv/bin/python rag_1m_test.py [--turns 300] [--tok-per-turn 3300] [--base URL]
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

KEY = Path("/home/cloud/logs/strata-api-key.txt").read_text().strip()
MODEL = "strata-b"          # 显式点名普道（8092）；--model 可换
TOPICS = ["城市地铁的调度原理", "咖啡豆的烘焙曲线", "候鸟迁徙的导航机制", "桥梁的抗震设计", "茶多酚的氧化路径",
          "射电望远镜的干涉测量", "疫苗的冷链运输", "珊瑚礁的生态修复", "锂电池的热失控边界", "榫卯的受力模型",
          "数值天气预报的初值问题", "深海耐压壳的失稳判据", "路面排水的径流系数", "蜂群的信息素决策", "胶片扫描的色彩还原"]
# (turn, label, code, question) - the turns are spread across the whole history
FACTS = [(1, "vault", "ZEBRA-7734", "What is the vault access code? Answer with just the code."),
         (2, "gate", "FALCON-1122", "What is the north gate passcode? Answer with just the code."),
         (40, "pod", "ORCHID-5566", "What is the pod maintenance code? Answer with just the code."),
         (150, "harbor", "TUNDRA-8899", "What is the harbor berth code? Answer with just the code."),
         (260, "lab", "COBALT-3141", "What is the lab door code? Answer with just the code."),
         (299, "radio", "MEADOW-2718", "What is the radio channel code? Answer with just the code.")]
ABSENT = ("What is the submarine hatch code? Answer with just the code, or say UNKNOWN if it was never given.")


def post(path, payload, timeout=1800):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode())


def chat(messages, max_tokens, tag="", quiet=False):
    t0 = time.time()
    r = post("/v1/chat/completions", {"model": MODEL, "messages": messages, "max_tokens": max_tokens,
                                      "temperature": 0, "reasoning_effort": "none"})
    dt = time.time() - t0
    out = r["choices"][0]["message"].get("content") or ""
    if not quiet:
        print(f"    {tag:10s} {dt:6.1f}s  -> {out[:70]!r}", flush=True)
    return out, dt


def turn_text(i, chars):
    return ((f"【第 {i} 篇归档】主题：{TOPICS[i % len(TOPICS)]}。"
             + "本节描述该子系统的分层结构、命名约定、迁移计划与验收口径；") * 40)[:chars]


def main():
    global BASE, MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=300)
    ap.add_argument("--tok-per-turn", type=int, default=3300)
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="strata-b", help="模型 ID：strata-b=普道(8092) strata-a=快道(8091)")
    a = ap.parse_args()
    BASE, MODEL = a.base, a.model
    chars = int(a.tok_per_turn * 1.5)
    facts = {t: (label, code) for t, label, code, _ in FACTS if t <= a.turns}
    print(f"test set: {a.turns} turns x ~{a.tok_per_turn} tok (~{a.turns * a.tok_per_turn} tok), "
          f"{len(facts)} facts at turns {sorted(facts)}, 1 absent question; proxy {BASE}", flush=True)

    msgs = []
    t_start = time.time()
    for i in range(1, a.turns + 1):
        if i in facts:
            label, code = facts[i]
            msgs.append({"role": "user", "content":
                         f"Please remember this {label} code for later: the {label} code is {code}."})
            msgs.append({"role": "assistant", "content": f"Noted - the {label} code {code}."})
        msgs.append({"role": "user", "content": turn_text(i, chars) + f"\n第 {i} 篇归档的要点是什么？一句话。"})
        try:
            out, _ = chat(msgs, 16, quiet=True)
            msgs.append({"role": "assistant", "content": out or "(empty)"})
        except Exception as e:
            print(f"  turn {i}: failed {e!r}", flush=True)
            msgs.pop()
            time.sleep(3)
            continue
        if i % 25 == 0 or i == a.turns:
            h = get("/health")
            c = h["counters"]
            s = max(h["sessions"], key=lambda x: x["chunks"]) if h["sessions"] else {}
            print(f"  turn {i:4d}: {time.time()-t_start:6.0f}s  chunks={s.get('chunks')} vectors={s.get('vectors')} "
                  f"queue={h['embed_queue']} assembled={c['assembled']} reused={c['assembly_reused']}", flush=True)

    print(f"\ningest done in {time.time()-t_start:.0f}s -> question set", flush=True)
    fails = []
    for t, label, code, q in FACTS:
        if t > a.turns:
            continue
        out, dt = chat(msgs + [{"role": "user", "content": q}], 24, f"{label}@{t}")
        if code in out:
            print(f"      recall {label}: PASS ({dt:.1f}s)")
        else:
            print(f"      recall {label}: FAIL - wanted {code}")
            fails.append(label)
    out, dt = chat(msgs + [{"role": "user", "content": ABSENT}], 24, "absent")
    refuse = ("UNKNOWN" in out.upper()) or (not any(ch.isdigit() for ch in out))
    print(f"      absent-question: {'PASS (no invented code)' if refuse else 'FAIL - invented: ' + out[:60]}")
    if not refuse:
        fails.append("absent")

    h = get("/health")
    c = h["counters"]
    print(f"\nRESULT: {'ALL PASS' if not fails else 'FAIL ' + str(fails)}  (fails={len(fails)})  "
          f"assembled={c['assembled']} reused={c['assembly_reused']} degraded={c['degraded']} "
          f"total wall {time.time()-t_start:.0f}s")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
