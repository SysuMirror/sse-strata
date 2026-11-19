#!/usr/bin/env python3
"""会话隔离测试：模板化客户端（同 system + 同首条 user）撞会话键时，绝不允许串上下文。

做法：起一个小窗口实例（max_context=16384，让组装尽早触发），构造两段对话：
  A：埋唯一事实 "保险箱密码是 ALPHA-111"，之后多轮无关内容；
  B：**相同的 system 与相同的首条 user**（会话键必然相同），但内容完全不同、从不提保险箱；
末端在 B 里问"保险箱密码是什么？"——正确答案是"不知道/未提供"，**出现 ALPHA-111 即串上下文失败**。
同时检查 /health 会话数与日志里的 "session reset" 记录。

用法：/home/cloud/Strata/.venv/bin/python test_session_isolation.py
"""
import json
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = "/home/cloud/Strata/.venv/bin/python"
KEY = Path("/home/cloud/logs/strata-api-key.txt").read_text().strip()
PORT = 8086
BASE = f"http://127.0.0.1:{PORT}"
FILLER = ("归档材料描述了流程、命名与迁移计划；复核周期为季度，日志保留九十天。" * 30)


def post(path, payload, timeout=600):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


def chat(msgs, max_tokens=48):
    r = post("/v1/chat/completions", {"model": "strata", "messages": msgs, "max_tokens": max_tokens,
                                      "temperature": 0, "reasoning_effort": "none"})
    return r["choices"][0]["message"].get("content") or ""


def main():
    cfg = json.loads((HERE / "config.json").read_text())
    cfg.update({"max_context": 4096, "recent_window_tokens": 2048, "gen_reserve_default": 512,
                "gen_reserve_max": 512, "listen_port": PORT})
    (HERE / "config-test-isolation.json").write_text(json.dumps(cfg, indent=1))
    log = open("/tmp/rag_isolation.log", "w")
    proc = subprocess.Popen([PY, str(HERE / "rag_proxy.py"), "--config", str(HERE / "config-test-isolation.json")],
                            stdout=log, stderr=log)
    try:
        for _ in range(50):
            try:
                get("/health")
                break
            except Exception:
                time.sleep(0.2)
        system = {"role": "system", "content": "你是一个严谨的助手。"}
        first = {"role": "user", "content": "开始分析。"}          # 两段对话**完全相同**的头
        # 对话 A：埋事实
        a = [system, first,
             {"role": "user", "content": "请记住：保险箱密码是 ALPHA-111。"},
             {"role": "assistant", "content": "已记住。"}]
        for i in range(12):
            a.append({"role": "user", "content": f"A{i}: {FILLER}一句话总结。"})
            a.append({"role": "assistant", "content": "已总结。"})
        chat(a, 8)
        # 对话 B：同样的头，内容完全不同（从不提保险箱）
        b = [system, first]
        for i in range(12):
            b.append({"role": "user", "content": f"B{i}: {FILLER}列出要点。"})
            b.append({"role": "assistant", "content": "要点已列。"})
        b.append({"role": "user", "content": "保险箱密码是什么？若从未提供请回答 UNKNOWN。"})
        out = chat(b, 48)
        h = get("/health")
        leaked = "ALPHA-111" in out
        print(f"B 段回答: {out[:120]!r}")
        print(f"泄露检查: {'❌ 串上下文！' if leaked else '✅ 未泄露（隔离正常）'}")
        print(f"代理会话数: {len(h['sessions'])}  计数器: assembled={h['counters']['assembled']} "
              f"reused={h['counters']['assembly_reused']}")
        reset_logged = "session reset" in Path("/tmp/rag_isolation.log").read_text()
        print(f"日志 reset 记录: {'✅ 有' if reset_logged else '（无需 reset 或未触发组装）'}")
        return 1 if leaked else 0
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
