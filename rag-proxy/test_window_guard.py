#!/usr/bin/env python3
"""窗口守卫回归测试。

要守的东西一句话：**引擎对 prompt + max_tokens + 8 > 窗口 直接 400、从不截断**，所以网关绝不能把
这样一个请求放出去。2026-10-08 生产翻车现场：网关记"精确计数 219,021"、放行 max_tokens 32000，
引擎自己数到 231,775 → 客户端看见 400（差 5.8%）。三条真实通路都在这里补：
  ① 旧估算 len/1.5 不是上界（数字串与单字重复的 CJK 实测 1.00 tok/char，被低估 1.5 倍），
     而快通道判据 `est*2 < budget` 其实是拿 ×2 替估算兜底 —— 真实 token 超过估算 2 倍就漏；
  ② /v1/messages/count_tokens 把一张图只数 1 个 pad，引擎 prepare() 展开成几百个（实测 6 张
     1024x768 = +4602），谁都没把这块差额算进预算；
  ③ 计数与引擎渲染之间的残差没有留余量，砍无可砍时组装还会"算了，就这样发出去"。

A 组是离线单元测试（不叫引擎）；B 组把 mock_upstream.py 开成"严格窗口"模式（超窗就照真引擎
#545 的措辞报 400），跑真 rag_proxy.py，断言客户端一次 400 都看不见、且每条进引擎的请求都装得下。
B 组用小窗口（max_context 24000）跑，判据与生产同一套代码，只是快；生产口径的数字在 A 组里核。
"""
import json
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PY = "/home/cloud/Strata/.venv/bin/python"
KEY = Path("/home/cloud/logs/strata-api-key.txt").read_text().strip()

import rag_proxy as R                                            # noqa: E402

R.CFG = dict(R.DEFAULTS)                                        # 单元测试用默认口径（窗口 262144）

MOCK_PORT = 8097
PROXY_PORT = 8087
WIN = 24000                                                     # B 组的小窗口
MAXTOK = 4000
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


# ============================================================= A. 离线单元测试
# 对真引擎 count_tokens 标定过的实测 tok/char（标定脚本与数据见 README §5f）
MEASURED = [
    ("英文散文", "The archived design notes describe the subsystem in detail, covering naming, layering, "
                 "and the migration plan for the storage tier. ", 0.188),
    ("中文散文", "归档的设计说明详细描述了该子系统的命名、分层与存储层的迁移计划。这是一个漫长而复杂的过程。", 0.506),
    ("代码", "def solve(n: int) -> list[int]:\n    out = []\n    for i in range(n):\n"
             "        if i % 3 == 0 and i % 5 != 0:\n            out.append(i * i - 3 * i + 17)\n", 0.389),
    ("工具JSON", json.dumps({"type": "function", "function": {"name": "read_file", "description":
                    "Read a file from the workspace and return its contents.", "parameters":
                    {"type": "object", "properties": {"path": {"type": "string"}}}}}, ensure_ascii=False), 0.266),
    ("中英混排", "根据 log 输出 `ctx[组装]` 判断是否触发组装；max_tokens=32000 时余量 30361。", 0.625),
    ("数字串", " ".join(f"{i:07d}" for i in range(120)), 1.000),
    ("随机词", " ".join("".join(chr(97 + (i * 7919 + j * 31) % 26) for j in range(9)) for i in range(120)), 0.474),
    ("单字重复CJK", "汉" * 2000, 1.000),
]


def group_a():
    print("\n=== A1 估算必须是上界（对真引擎实测 tok/char） ===")
    for name, sample, ratio in MEASURED:
        text = sample * 10
        real = int(len(text) * ratio)
        upper = R.text_tokens_upper(text)
        old = int(len(text) / 1.5)
        ck(f"上界 >= 实测: {name}", upper >= real, f"上界 {upper} / 实测 {real} / 旧估算 {old}")
        if ratio > 0.67:      # 旧公式在这两类上确实低估（这就是 231775 vs 111k 的那类内容）
            ck(f"旧估算确实低估（回归点）: {name}", old < real, f"旧 {old} < 实测 {real}")

    print("\n=== A2 图片要按引擎的展开方式数 ===")
    msgs = [{"role": "user", "content": [{"type": "image", "source": {"type": "base64",
                "media_type": "image/png", "data": "AAAA" * 30}}, {"type": "text", "text": "这是什么"}]}]
    ck("count_image_blocks 认出 Anthropic 形态图片块", R.count_image_blocks(msgs) == 1)
    msgs2 = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]}]
    ck("count_image_blocks 认出 OpenAI 形态图片块", R.count_image_blocks(msgs2) == 1)
    est_with = R.est_prompt_tokens(msgs)
    est_text_only = R.text_tokens_upper(R.message_text(msgs[0])) + R.CFG["msg_overhead_tokens"]
    ck("估算含图片展开 allowance", est_with >= est_text_only + R.CFG["image_tokens"],
       f"含图 {est_with} / 只算文本 {est_text_only}")

    print("\n=== A3 clamp：生产翻车的那组数字 ===")
    # 引擎当时报的原话：prompt 231775，"at most 30361 here"
    req = {"model": "strata", "messages": [{"role": "user", "content": "x"}], "max_tokens": 32000}
    req2, clamped = R.clamp_max_tokens(req, 231775)
    room_engine = 262144 - 8 - 231775
    ck("超余量必须压", clamped and req2["max_tokens"] < 32000, f"压到 {req2['max_tokens']}")
    ck("压完 + 提示词 + 边界 + 残差 <= 窗口",
       231775 + req2["max_tokens"] + 8 + R.count_margin(231775) <= 262144,
       f"max_tokens {req2['max_tokens']} + 残差 {R.count_margin(231775)}")
    ck("比引擎报的余量更保守（留残差）", req2["max_tokens"] <= room_engine,
       f"{req2['max_tokens']} <= 引擎余量 {room_engine}")
    r3, c3 = R.clamp_max_tokens({"max_tokens": 1000}, 1000)
    ck("装得下就不动", (not c3) and r3["max_tokens"] == 1000)
    r4, c4 = R.clamp_max_tokens({"max_completion_tokens": 40000}, 240000)
    ck("max_completion_tokens 也走同一道闸", c4 and "max_completion_tokens" not in r4,
       f"-> max_tokens {r4.get('max_tokens')}")
    r5, c5 = R.clamp_max_tokens({}, 1000)
    ck("没写 max_tokens 不算超", not c5)

    print("\n=== A4 引擎 400 的余量能读出来（重试网的输入） ===")
    msg = ("prompt (231775 tokens) + max tokens (32000) exceeds the context (262144); requests are never "
           "truncated. Send a smaller max_tokens (at most 30361 here), or add \"fit_max_tokens\": true "
           "to the model's strata-<model>.json to shorten it to the room left (#545)")
    payload = json.dumps({"error": {"type": "invalid_request_error", "message": msg}}).encode()
    ck("overfit_room 读出引擎报的余量", R.overfit_room(payload) == 30361, f"-> {R.overfit_room(payload)}")
    ck("别的 400 不误判", R.overfit_room(b'{"error":{"message":"bad image"}}') is None)
    patched = R.patch_max_tokens(json.dumps({"messages": [], "max_tokens": 32000}).encode(), 30361)
    ck("patch 压到引擎余量", json.loads(patched)["max_tokens"] == 30361)
    ck("当前值已 <= 余量时不重试（不是输出长度的锅）",
       R.patch_max_tokens(json.dumps({"max_tokens": 100}).encode(), 30361) is None)

    print("\n=== A5 降级透传也不放弃窗口守卫（RAG 自己出错时的那条路） ===")
    big = [{"role": "user", "content": "说明文字 " * 90000}]        # 上界估算远超窗口
    body = json.dumps({"model": "strata", "messages": big, "max_tokens": 32000}).encode()
    patched = R.Handler._guard_body(None, body)
    ck("降级时压 max_tokens 而不是原样放行", patched is not None)
    if patched:
        p = json.loads(patched)
        # 提示词本身比窗口还大时，压 max_tokens 救不了（引擎的 "leaves no room" 那一档）：
        # 守卫能做的是压到最小 + 记日志，剩下的失败由 overfit_failures 计数器暴露。
        ck("压到最小输出（1）", p["max_tokens"] == 1, f"max_tokens {p['max_tokens']}")
    # 提示词本身装得下窗口、但 + 客户端要的 32000 就出界：应该压输出而不是放行
    fits = json.dumps({"model": "strata", "messages": [{"role": "user", "content": "说明文字 " * 50000}],
                       "max_tokens": 32000}).encode()
    p2 = R.Handler._guard_body(None, fits)
    e2 = R.est_prompt_tokens(json.loads(fits)["messages"], "")
    ck("提示词装得下窗口（但加 32000 就出界）", e2 + 8 <= 262144 < e2 + 8 + 32000, f"上界估算 {e2}")
    ck("压输出而不是放行", p2 is not None and json.loads(p2)["max_tokens"] < 32000,
       str(json.loads(p2)["max_tokens"] if p2 else None))
    if p2:
        m2 = json.loads(p2)["max_tokens"]
        ck("压完 + 上界 + 边界 + 残差 <= 窗口", e2 + m2 + 8 + R.count_margin(e2) <= 262144,
           f"max_tokens {m2} + 残差 {R.count_margin(e2)}")
    small = json.dumps({"model": "strata", "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 100}).encode()
    ck("装得下就原样转发（不无谓改动）", R.Handler._guard_body(None, small) is None)
    ck("坏 body 不炸", R.Handler._guard_body(None, b"not json") is None)

    print("\n=== A6 组装必须收敛到装得下（模拟引擎比网关的上界还多数 30%） ===")
    # 为什么要有这一组：上界估算（A1）只是"我们没见过的那类内容也不会低估"，而生产翻车那次
    # 网关记 219,021、引擎数到 231,775 —— 计数本身比估算还大。所以这里把 count_tokens 换成
    # "比上界多算 30% 且随砍随降"的假引擎，逼组装走完收敛的两条手段（砍最长消息头部 / 丢最旧消息），
    # 而不是"砍不动就照发"。检索被换成空块（不叫嵌入/重排服务），只剩系统 + 最近窗口，最干净。
    real_count, real_block = R.count_tokens, R.build_memory_block
    R.build_memory_block = lambda sess, messages, q, budget_tokens, rt, ri: ("", 0, 0.0, [], {})

    def fake_count(messages, extra=None, up=None):        # 假引擎：比上界还多 30%，随内容减少
        return int(1.3 * R.est_prompt_tokens(messages, str((extra or {}).get("system") or "")))

    R.count_tokens = fake_count
    budget = R.CFG["max_context"] - R.CFG["ctx_slack"] - 32000
    old_window_cfg = R.CFG["recent_window_tokens"]
    R.CFG["recent_window_tokens"] = 300000                # 最近窗口先全留：把压力全交给收敛循环
    try:
        def run(msgs, label, want):
            sess = R.Session(f"guard-{label}")
            body = {"model": "strata", "messages": msgs, "max_tokens": 32000}
            new_req, info = R.assemble(sess, body, msgs, fake_count(msgs), None)
            final = info["final_tokens"]
            ck(f"{label}：组装后 + max_tokens + 边界 + 残差 <= 窗口",
               final + 32000 + R.CFG["ctx_slack"] + R.count_margin(final) <= R.CFG["max_context"],
               f"提示词 {final} / 预算 {budget} / 精确={info['final_exact']}")
            ck(f"{label}：收敛手段用对了（{want}）", bool(info.get(want)),
               f"shrunk={info.get('shrunk')} recent_dropped={info.get('recent_dropped')}")
            ck(f"{label}：系统提示词没丢", new_req["messages"][0]["role"] == "system")
            return info

        def grow(msgs, make, stop_at):
            """把语料长到"假引擎报的数超过预算"为止——不靠我手算字符数猜，判据本身说了算。"""
            i = 0
            while fake_count(msgs) <= stop_at and i < 4000:
                msgs.extend(make(i)); i += 1
            return msgs, fake_count(msgs)

        # ① 有巨型消息：应该靠"从头部砍最长消息"收敛（砍掉的头部仍在会话分块里，检索能带回）
        big = [{"role": "system", "content": "SYS " * 400}]
        big, fake1 = grow(big, lambda i: [{"role": "user",
                                           "content": f"第 {i} 段粘贴：" + "记录内容说明 " * 2400}], budget)   # 每条 15k 字符
        ck("巨消息：假引擎报的数确实比上界估算还超预算（这才算考验收敛）",
           fake1 > budget > R.est_prompt_tokens(big), f"假引擎 {fake1} / 上界 {R.est_prompt_tokens(big)} / 预算 {budget}")
        run(big, "巨消息", "shrunk")

        # ② 全是小消息（砍无可砍）：必须靠"丢最近窗口里最旧的整条消息"收敛，绝不能照发
        small = [{"role": "system", "content": "SYS " * 400}]
        small, fake2 = grow(small, lambda i: [{"role": "user", "content": f"第 {i} 轮：" + "说明文字 " * 100},
                                              {"role": "assistant", "content": f"收到 {i}。" + "回答内容 " * 100}], budget)
        ck("小消息：没有 >=4096 字符的消息可砍（只能靠丢整条）",
           all(len(R.message_text(m)) < 4096 for m in small), f"最长 {max(len(R.message_text(m)) for m in small)} 字符")
        info2 = run(small, "小消息", "recent_dropped")
        ck("小消息案：确实丢了整条而不是截断", (info2.get("recent_dropped") or 0) > 0,
           f"丢了 {info2.get('recent_dropped')} 条")
    finally:
        R.count_tokens, R.build_memory_block = real_count, real_block
        R.CFG["recent_window_tokens"] = old_window_cfg


# ============================================================= B. 端到端（严格窗口 mock）
def get(url, timeout=15):
    for _ in range(8):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as f:
                return json.load(f)
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"GET {url} 失败")


def post(url, body, timeout=180):
    """返回 (status, parsed)；400 也要拿到 body，测试就是在看有没有 400。"""
    data = json.dumps(body, ensure_ascii=False).encode()
    for _ in range(6):
        try:
            r = urllib.request.Request(url, data=data, method="POST")
            r.add_header("Content-Type", "application/json")
            r.add_header("Authorization", f"Bearer {KEY}")
            with urllib.request.urlopen(r, timeout=timeout) as f:
                return f.status, json.loads(f.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"POST {url} 连不上")


def start_mock(window: int, lie_marker: str = ""):
    args = [PY, str(HERE / "mock_upstream.py"), "--port", str(MOCK_PORT),
            "--record", "/tmp/window_guard_reqs.jsonl", "--window", str(window), "--image-tokens", "800"]
    if lie_marker:
        args += ["--lie-marker", lie_marker, "--lie-room", str(window - 8 - 12000)]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            get(f"http://127.0.0.1:{MOCK_PORT}/health", timeout=2)
            return p
        except Exception:
            time.sleep(0.2)
    raise SystemExit("mock 没起来")


def start_proxy(name: str, overrides: dict):
    cfg = json.loads((HERE / "config.json").read_text())
    # 本套用例测的是"检索组装/降级/重试"这条 RAG 路径——rag_enabled 必须开。
    # 生产 config.json 自 2026-10-19 起是关的（纯透传），照抄它会让 B/C 组实际跑进
    # no_rag 透传分支、被严格窗口 mock 的 400 打回（2026-10-19 计数口径排查时撞上）。
    if "rag_enabled" not in overrides:
        cfg["rag_enabled"] = True
    cfg.update(overrides)
    cfg["listen_port"] = PROXY_PORT
    cfg["upstream"] = cfg["default_upstream"] = cfg["upstreams"][0] = f"http://127.0.0.1:{MOCK_PORT}"
    cfg["model_routes"] = {"strata": f"http://127.0.0.1:{MOCK_PORT}"}
    cfg["embed_batch"], cfg["embed_wait_s"] = 32, 60
    pth = HERE / f"config-test-{name}.json"
    pth.write_text(json.dumps(cfg, indent=1))
    log = open(f"/tmp/rag_proxy_{name}.log", "w")
    proc = subprocess.Popen([PY, str(HERE / "rag_proxy.py"), "--config", str(pth)], stdout=log, stderr=log)
    for _ in range(50):
        try:
            get(f"http://127.0.0.1:{PROXY_PORT}/health", timeout=2)
            return proc, Path(f"/tmp/rag_proxy_{name}.log")
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"proxy 没起来 - 看 /tmp/rag_proxy_{name}.log")


def corpus(n_turns: int, chars: int) -> list:
    """agent 形态长会话：足够长以触发组装（小窗口 24000）。"""
    msgs = [{"role": "system", "content": "You are a deployment agent. 查工具、别编。"}]
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"第 {i} 步：检查部署状态并摘要。"})
        msgs.append({"role": "assistant", "content": "我查一下。", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "lookup",
             "arguments": json.dumps({"q": i})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": "\n".join(f"line {j}: seq={i} status=ok stage={j//20} retry={j%3}"
                                          for j in range(chars // 45))})
        msgs.append({"role": "assistant", "content": f"第 {i} 步完成。"})
    msgs.append({"role": "user", "content": "最后总结：有没有失败？"})
    return msgs


def recorded() -> list:
    p = Path("/tmp/window_guard_reqs.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def group_b():
    print("\n=== B1 严格窗口引擎：客户端一次 400 都不该看见 ===")
    mock = start_mock(WIN)
    proxy, logpath = start_proxy("guard", {
        "max_context": WIN, "recent_window_tokens": 6000, "chunk_tokens": 512,
        "gen_reserve_default": 2048, "gen_reserve_max": MAXTOK, "assembly_ttl_s": 0,
        "block_drift_rebuild": False, "memory_min_tokens": 512, "debug": False})
    base = f"http://127.0.0.1:{PROXY_PORT}"
    try:
        Path("/tmp/window_guard_reqs.jsonl").write_text("")
        codes = []
        # 从小到大的扫描：每档都问 max_tokens=4000，窗口只有 24000
        for turns, chars in [(2, 400), (6, 1500), (14, 4000), (30, 6000), (60, 8000), (90, 9000)]:
            st, out = post(f"{base}/v1/chat/completions",
                           {"model": "strata", "messages": corpus(turns, chars), "max_tokens": MAXTOK})
            codes.append((turns, chars, st, (out.get("error") or {}).get("message", "")[:60]))
        bad = [c for c in codes if c[2] != 200]
        ck("扫描全 200（没有一个 400 交回客户端）", not bad, str(bad[:3]))

        recs = recorded()
        over = [r for r in recs if r["prompt_tokens"] + (r["max_tokens"] or 0) + 8 > WIN]
        ck("每条进引擎的请求都装得下（引擎口径）", not over,
           f"{len(recs)} 条记录，超窗 {len(over)}")
        ck("确实触发了组装（不是全透传）", any("ctx[组装]" in logpath.read_text() for _ in [0]))
        txt = logpath.read_text()
        ck("日志带计数残差", "计数残差" in txt)
        ck("max_tokens 被压时日志明说", "已压到" in txt or "余量" in txt)

        print("\n=== B2 图片：count_tokens 数 1、引擎数 800 的差额必须算进预算 ===")
        Path("/tmp/window_guard_reqs.jsonl").write_text("")
        png = "AAAA" * 900                       # mock 只数块数，不看真尺寸
        imgs = [{"role": "user", "content": [{"type": "image", "source": {"type": "base64",
                  "media_type": "image/png", "data": png}}, {"type": "text", "text": f"图 {i} 里有什么"}]}
                for i in range(10)]
        msgs = corpus(20, 3000) + imgs
        st, out = post(f"{base}/v1/chat/completions", {"model": "strata", "messages": msgs, "max_tokens": MAXTOK})
        ck("带 10 张图的请求不超窗", st == 200, f"status {st} {(out.get('error') or {}).get('message','')[:80]}")
        recs = recorded()
        over = [r for r in recs if r["prompt_tokens"] + (r["max_tokens"] or 0) + 8 > WIN]
        ck("带图请求也装得下", not over, f"{len(recs)} 条，超窗 {len(over)}")
        n_img = max((R.count_image_blocks(r["body"].get("messages") or []) for r in recs), default=0)
        ck("图片确实进了引擎", n_img > 0, f"最多一条含 {n_img} 张")

    finally:
        for p in (proxy, mock):
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=8)
            except Exception:
                p.kill()


def group_b3():
    """B3：网关算对了、引擎却仍然报超窗时（残差、旧计数、图片展开的近似），照引擎报的余量压一次重发。
    单独起一个 mock：它的"我这次偏 1500 token"的谎话是一次性的（进程内记 LIED），必须新进程。"""
    print("\n=== B3 重试网：引擎仍报超窗时，照它报的余量压一次重发 ===")
    mock = start_mock(WIN, lie_marker="TRIGGER400")
    proxy, logpath = start_proxy("retry", {
        "max_context": WIN, "recent_window_tokens": 6000, "chunk_tokens": 512,
        "gen_reserve_default": 2048, "gen_reserve_max": MAXTOK, "assembly_ttl_s": 0,
        "block_drift_rebuild": False, "memory_min_tokens": 512, "debug": False})
    base = f"http://127.0.0.1:{PROXY_PORT}"
    try:
        Path("/tmp/window_guard_reqs.jsonl").write_text("")
        st, out = post(f"{base}/v1/chat/completions",
                       {"model": "strata", "messages": corpus(3, 800) + [
                           {"role": "user", "content": "TRIGGER400 请总结"}], "max_tokens": MAXTOK})
        ck("客户端拿到 200（400 被重试网吃掉）", st == 200, f"status {st} {(out.get('error') or {}).get('message','')[:90]}")
        txt = logpath.read_text()
        ck("日志记录了按引擎余量重试", "按引擎报的余量" in txt)
        recs = recorded()
        ck("重试真的把 max_tokens 压小了", any(r["max_tokens"] < MAXTOK for r in recs),
           str([r["max_tokens"] for r in recs]))
        ck("重试后装得下", all(r["prompt_tokens"] + (r["max_tokens"] or 0) + 8 <= WIN for r in recs),
           str([(r["prompt_tokens"], r["max_tokens"]) for r in recs]))
        h = get(f"{base}/health")
        ck("计数器 overfit_retries 记上了", h["counters"].get("overfit_retries", 0) >= 1,
           str(h["counters"].get("overfit_retries")))
        ck("重试成功就不算失败（overfit_failures 不动）", h["counters"].get("overfit_failures", 0) == 0,
           str(h["counters"].get("overfit_failures")))
    finally:
        for p in (proxy, mock):
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=8)
            except Exception:
                p.kill()


def group_c():
    """C 组：嵌入/重排全挂（RAG 帮不上忙）时，窗口守卫仍然不许把必然 400 的请求放出去。"""
    print("\n=== C RAG 服务不可达：降级路径也必须装得下 ===")
    mock = start_mock(WIN)
    proxy, logpath = start_proxy("deg", {
        "max_context": WIN, "recent_window_tokens": 6000, "chunk_tokens": 512,
        "gen_reserve_default": 2048, "gen_reserve_max": MAXTOK, "assembly_ttl_s": 0,
        "block_drift_rebuild": False, "memory_min_tokens": 512, "debug": False,
        "embed_url": "http://127.0.0.1:9/v1/embeddings",       # 立刻拒绝，不占时间
        "rerank_url": "http://127.0.0.1:9/v1/rerank",
        "embed_wait_s": 2, "embed_timeout_s": 2, "rerank_timeout_s": 2, "count_timeout_s": 3})
    base = f"http://127.0.0.1:{PROXY_PORT}"
    try:
        Path("/tmp/window_guard_reqs.jsonl").write_text("")
        st, out = post(f"{base}/v1/chat/completions",
                       {"model": "strata", "messages": corpus(60, 8000), "max_tokens": MAXTOK})
        ck("RAG 全挂也拿到 200", st == 200, f"status {st} {(out.get('error') or {}).get('message','')[:80]}")
        recs = recorded()
        over = [r for r in recs if r["prompt_tokens"] + (r["max_tokens"] or 0) + 8 > WIN]
        ck("RAG 全挂时进引擎的请求也装得下", not over, f"{len(recs)} 条，超窗 {len(over)}")
        h = get(f"{base}/health")
        c = h["counters"]
        ck("走了组装或降级（不是原样透传一个超窗请求）",
           (c.get("assembled", 0) + c.get("degraded", 0)) >= 1, str(c))
        ck("没有请求被原样放行（passthrough 只该属于短请求）", c.get("passthrough", 0) == 0, str(c))
    finally:
        for p in (proxy, mock):
            try:
                p.send_signal(signal.SIGTERM)
                p.wait(timeout=8)
            except Exception:
                p.kill()


def main():
    t0 = time.time()
    group_a()
    group_b()
    group_b3()
    group_c()
    print(f"\n{'=' * 70}")
    if FAILS:
        print(f"FAILED {len(FAILS)}: " + "; ".join(FAILS))
        return 1
    print(f"ALL PASS  ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
