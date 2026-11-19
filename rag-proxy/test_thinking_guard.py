#!/usr/bin/env python3
"""思考预算守卫的离线测试（2026-10-08）。

守的一句话：**客户端没表达过思考偏好时，网关替它把"答复的那份"留出来**——把思考上限压到
`max_tokens - 预留`；客户端一旦自己说了（`thinking` / `output_config` / `reasoning_effort` /
`reasoning_budget_tokens`），一个字节都不改。

不联网、不碰真引擎、不碰生产代理：进程内起一个 rag_proxy（线程里跑 HTTP），上游指向
mock_upstream，mock 会把**收到的请求体**逐条记进 JSONL——断言就落在"引擎到底收到了什么"。
另外单测纯函数 thinking_guard 的边界。

用法：/home/cloud/Strata/.venv/bin/python test_thinking_guard.py
"""
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PY = "/home/cloud/Strata/.venv/bin/python"
MOCK_PORT = 8095
PROXY_PORT = 8084
REC = HERE / "thinking-guard-reqs.jsonl"

import rag_proxy as R                                              # noqa: E402

OUT = sys.stdout
FAILS = []


def ck(name: str, ok: bool, detail: str = ""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {detail}" if detail else ""), file=OUT, flush=True)
    if not ok:
        FAILS.append(name)


def post(body: dict, timeout: float = 30.0) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def sent() -> list[dict]:
    """mock 收到的**请求体**（按顺序；mock 的记录是 {"prompt_tokens":…, "max_tokens":…, "body":{…}}）。"""
    if not REC.exists():
        return []
    return [json.loads(l)["body"] for l in REC.read_text(encoding="utf-8").splitlines() if l.strip()]


def unit_cases():
    print("== A 纯函数 thinking_guard ==", file=OUT, flush=True)
    for req, want, why in [
        ({"max_tokens": 2112}, 1408, "实测受害最重的一档（2112→留 704 给答复）"),
        ({"max_tokens": 512}, 342, "strata-b 上最多的一档"),
        ({"max_tokens": 32000}, 27904, "大额度：只留 4096 给答复，正常流量碰不到"),
        ({"max_tokens": 64}, None, "上限太小，留不出答复空间 → 不补"),
        ({"max_tokens": 127}, None, "预算 < 64 → 不补"),
        ({}, None, "客户端没给上限 → 不动"),
        ({"max_tokens": 2048, "thinking": {"type": "enabled", "budget_tokens": 1024}}, None, "客户端自己要了思考预算"),
        ({"max_tokens": 2048, "output_config": {"effort": "high"}}, None, "客户端给了 effort"),
        ({"max_tokens": 2048, "reasoning_effort": "low"}, None, "客户端给了 reasoning_effort"),
        ({"max_tokens": 2048, "reasoning_budget_tokens": 900}, 900, "客户端自带 → 原样保留，绝不覆盖"),
        ({"max_completion_tokens": 2112}, 1408, "OpenAI 口径的字段名也认"),
    ]:
        out, inj = R.thinking_guard(json.loads(json.dumps(req)))
        got = out.get("reasoning_budget_tokens")
        ck(why, got == want, f"in={req} → budget={got} injected={inj}")
        ck("  ↳ 原请求未被就地修改", req.get("reasoning_budget_tokens") is None or got == req.get("reasoning_budget_tokens"))
    off = dict(R.CFG)
    off["thinking_budget_guard"] = False
    print("== A2 refit：max_tokens 被窗口守卫压小之后重算 ==", file=OUT, flush=True)
    for req, want, why in [
        ({"max_tokens": 26228, "reasoning_budget_tokens": 27904}, 22132, "23万提示词那档：32000→26228 后重算 22132"),
        ({"max_tokens": 300, "reasoning_budget_tokens": 1408}, 200, "小额度重算（300−100）"),
        ({"max_tokens": 64, "reasoning_budget_tokens": 1408}, None, "重算后留不出答复空间 → 干脆不带"),
        ({"max_tokens": 26228, "reasoning_budget_tokens": 100}, 100, "本来就比上限小 → 不动"),
        ({"max_tokens": 26228}, None, "没有预算字段 → 不动"),
    ]:
        out = R.refit_thinking_budget(json.loads(json.dumps(req)))
        got = out.get("reasoning_budget_tokens")
        ck("refit " + why, got == want, f"in={req} → {got}")
    R.CFG = off
    out, inj = R.thinking_guard({"max_tokens": 2112})
    ck("开关关掉后完全不碰（回滚用）", inj is False and "reasoning_budget_tokens" not in out)
    R.CFG = dict(R.CFG, thinking_budget_guard=True)


def e2e_cases():
    print("== B 端到端：引擎到底收到了什么 ==", file=OUT, flush=True)
    plain = {"model": "mock", "max_tokens": 2112, "messages": [{"role": "user", "content": "你好"}]}
    with_think = dict(plain, thinking={"type": "enabled", "budget_tokens": 1024})
    own_budget = dict(plain, reasoning_budget_tokens=900)
    for b in (plain, with_think, own_budget):
        ck(f"请求返回 200（{ 'b' + str(len(sent())) }）", post(b) == 200)
    bodies = sent()[-3:]
    ck("快通道请求：引擎收到思考预算 1408", bodies[0].get("reasoning_budget_tokens") == 1408,
       f"got={bodies[0].get('reasoning_budget_tokens')}")
    ck("快通道请求：其余字段未被改动",
       {k: v for k, v in bodies[0].items() if k != "reasoning_budget_tokens"} == plain,
       f"diff={[k for k in set(bodies[0]) | set(plain) if bodies[0].get(k) != plain.get(k) and k != 'reasoning_budget_tokens']}")
    ck("客户端带 thinking：原样转发（无注入）", "reasoning_budget_tokens" not in bodies[1],
       f"body_keys={sorted(bodies[1])}")
    ck("客户端带 thinking：body 逐字段相同", bodies[1] == with_think)
    ck("客户端自带预算：原样 900（不覆盖）", bodies[2].get("reasoning_budget_tokens") == 900,
       f"got={bodies[2].get('reasoning_budget_tokens')}")
    ck("客户端自带预算：body 逐字段相同", bodies[2] == own_budget)


def main() -> int:
    mock = subprocess.Popen([PY, str(HERE / "mock_upstream.py"), "--port", str(MOCK_PORT), "--record", str(REC)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = dict(R.DEFAULTS)
    cfg.update({"listen_port": PROXY_PORT, "upstream": f"http://127.0.0.1:{MOCK_PORT}",
                "upstreams": [f"http://127.0.0.1:{MOCK_PORT}"], "default_upstream": f"http://127.0.0.1:{MOCK_PORT}",
                "count_timeout_s": 5, "debug": False})
    R.CFG = cfg
    R.API_KEY = "test"
    old_stdout = sys.stdout
    try:
        REC.unlink(missing_ok=True)
        time.sleep(0.4)
        srv = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), R.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)
        sys.stdout = open(HERE / "thinking-guard-proxy.out.log", "w", encoding="utf-8")
        unit_cases()
        e2e_cases()
    finally:
        sys.stdout = old_stdout
        mock.terminate()

    print("=" * 62, file=OUT, flush=True)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 项未通过 -> {FAILS}", file=OUT, flush=True)
        return 1
    print("ALL PASS", file=OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
