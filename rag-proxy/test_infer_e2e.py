#!/usr/bin/env python3
"""推理记录**端到端**：一条真实的（假引擎）推理穿过代理，记录必须完整落下来。

验的是"接上了没有"，不是"字段对不对"（字段在 test_infer_store.py 里逐条断言）：
  · 流式与非流式各一次，记录都要出现，且 token 数/速度来自引擎的 usage+timings；
  · 元信息接口**不得**泄漏用户输入与模型回复（这是需求里明确要的：只给元信息）；
  · 未登录 / 无 key 时元信息接口 401；/infer/raw（有内容）**只认登录会话**；
  · 记录写盘/上传**不能**拖慢推理本身（请求的正常返回与内容要原样）。

假引擎自带的 SSE 形状照抄真引擎（最后一帧带 usage 与 timings，见 serve/server.py）。
配置里把记录目录指到临时目录、对象存储前缀指到 infer-test/，测完删掉那些测试对象 ——
不碰线上实例、不给正式的桶留垃圾。

用法：/home/cloud/Strata/.venv/bin/python test_infer_e2e.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
PY = "/home/cloud/Strata/.venv/bin/python"
MOCK_PORT = 8197
PROXY_PORT = 8198
KEY = "test-key-for-infer-e2e"
FAILS = []

ANSWER = "你好，世界"
THINK = "先想一下"


def ck(name, expect, got, extra=""):
    if expect == got:
        print(f"  PASS  {name:<52} {got} {extra}")
    else:
        print(f"  FAIL  {name:<52} 期望 {expect}，实际 {got} {extra}")
        FAILS.append(name)


def ck_true(name, cond, extra=""):
    ck(name, True, bool(cond), extra)


class Mock(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path.startswith("/v1/status"):
            return self._json(200, {"model": "mock", "loaded": True, "uptime_s": 1})
        if self.path.startswith("/metrics"):
            return self._json(200, {"live": {}, "totals": {}, "requests": []})
        if self.path.startswith("/health"):
            return self._json(200, {"status": "ok"})
        self._json(404, {})

    def do_POST(self):
        req = self._body()
        if "count_tokens" in self.path:
            # 不能返回常数：网关走"精确计数"这条快通道时以它为准，返回 10 会让任何长度都算"装得下"，
            # 组装永远不触发（第一版就是这么写的，用例红了一片）。按真引擎同款口径估：
            # CJK 一字 ≈ 1 token，其余 4 字符 ≈ 1 token。
            text = json.dumps(req.get("messages") or [], ensure_ascii=False)
            cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
            return self._json(200, {"input_tokens": cjk + max(0, len(text) - cjk) // 4})
        if req.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            frames = [
                {"choices": [{"index": 0, "delta": {"reasoning_content": THINK}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "，世界"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 42, "completion_tokens": 3, "total_tokens": 45,
                           "prompt_tokens_details": {"cached_tokens": 30}},
                 "timings": {"cache_n": 30, "prompt_n": 12, "prompt_per_second": 600.0,
                             "predicted_n": 3, "predicted_per_second": 50.0}},
            ]
            for f in frames:
                self.wfile.write(("data: " + json.dumps(f, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        self._json(200, {"id": "c1", "object": "chat.completion", "model": "mock",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": ANSWER},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                         "timings": {"predicted_per_second": 33.5}})

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)


def get(url, key=None, timeout=10):
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except ValueError:
            return e.code, {}


def chat(text, stream=True):
    payload = {"model": "strata-a", "messages": [{"role": "user", "content": text}],
               "stream": stream, "max_tokens": 32}
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode("utf-8", "replace")


def wait_record(key, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, j = get(f"http://127.0.0.1:{PROXY_PORT}/infer/list?limit=50", key)
        if st == 200 and j.get("rows"):
            return j
        time.sleep(0.2)
    return {"rows": []}


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="infer-e2e-"))
    shutil.copy(HERE / "infer_store.py", tmp / "infer_store.py")     # 代理按同目录 import
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    cfg.update({
        "listen_port": PROXY_PORT, "upstream": f"http://127.0.0.1:{MOCK_PORT}",
        "upstreams": [f"http://127.0.0.1:{MOCK_PORT}"],
        "model_routes": {"strata-a": f"http://127.0.0.1:{MOCK_PORT}"},
        "default_upstream": f"http://127.0.0.1:{MOCK_PORT}",
        "embed_url": "http://127.0.0.1:1/v1/embeddings",             # 够不到 → 组装降级，不影响短请求
        "rerank_url": "http://127.0.0.1:1/v1/rerank",
        "embed_wait_s": 1,
        # 窗口压到很小，好让下面那条"超窗"用例**真的触发组装**（嵌入不可达时检索块为空，
        # 但组装本身会跑：最近窗口 + 窗口核算 —— 这正是要验的记录路径）
        "max_context": 4096, "ctx_slack": 8, "gen_reserve_default": 256, "gen_reserve_max": 1024,
        "recent_window_tokens": 1024,
        "infer_store": {"enabled": True, "dir": str(tmp / "segs"), "db": str(tmp / "infer.db"),
                        "minio_prefix": "infer-test", "seg_max_mb": 0.00005, "seg_max_s": 2,
                        "queue_max": 5000, "capture_max_mb": 16, "local_cache_gb": 1.0,
                        "local_cache_days": 7, "hot_rows": 20},
    })
    (tmp / "config.json").write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    (tmp / "key.txt").write_text(KEY + "\n", encoding="utf-8")
    cfg["api_key_file"] = str(tmp / "key.txt")
    (tmp / "config.json").write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")

    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    log = open(tmp / "proxy.log", "w")
    proc = subprocess.Popen([PY, str(HERE / "rag_proxy.py"), "--config", str(tmp / "config.json")],
                            stdout=log, stderr=log, cwd=str(tmp))
    try:
        for _ in range(60):
            try:
                get(f"http://127.0.0.1:{PROXY_PORT}/health", timeout=2)
                break
            except Exception:
                time.sleep(0.2)
        print(f"推理记录端到端（临时目录 {tmp}）")

        # ---- 门禁：元信息接口未登录/无 key → 401；有 key → 200
        st, _ = get(f"http://127.0.0.1:{PROXY_PORT}/infer/list")
        ck("匿名看元信息列表 401", 401, st)
        st, _ = get(f"http://127.0.0.1:{PROXY_PORT}/infer/stats")
        ck("匿名看统计 401", 401, st)
        st, _ = get(f"http://127.0.0.1:{PROXY_PORT}/infer/raw?id=x", KEY)
        ck("带 key 也不能读内容（只认登录）", 401, st)

        # ---- 流式推理
        st, body = chat("请用一句话打个招呼", stream=True)
        ck("推理请求本身 200", 200, st)
        ck("推理响应原样回给用户（逐帧拼起来是整句）", True, "你好" in body and "，世界" in body)
        ck("响应里带着引擎的 usage 帧", True, '"completion_tokens": 3' in body.replace('"completion_tokens":3', '"completion_tokens": 3'))
        lst = wait_record(KEY)
        ck("流式推理产生了一条记录", 1, len(lst.get("rows") or []))
        row = (lst.get("rows") or [{}])[0]
        ck("记录：模型", "strata-a", row.get("model"))
        ck("记录：prompt（读）", 42, row.get("prompt_tokens"))
        ck("记录：缓存命中（其中已读）", 30, row.get("cached_tokens"))
        ck("记录：completion（写/输出）", 3, row.get("completion_tokens"))
        ck("记录：decode 速度来自引擎 timings", 50.0, row.get("decode_tok_s"))
        ck("记录：prefill 速度", 600.0, row.get("prompt_tok_s"))
        ck("记录：流式标记", 1, row.get("stream"))
        ck("记录：状态", "completed", row.get("state"))
        ck("记录：路径", "/v1/chat/completions", row.get("path"))
        ck("记录：正文长度（只记长度，不记正文）", len(ANSWER), row.get("content_chars"))
        ck_true("记录：TTFT 有值", (row.get("ttft_ms") or 0) > 0, f"{row.get('ttft_ms')} ms")
        ck_true("记录：总耗时 ≥ TTFT", (row.get("total_ms") or 0) >= (row.get("ttft_ms") or 0),
                f"{row.get('total_ms')} ms")

        # ---- 元信息里绝不能有用户输入 / 模型回复
        blob = json.dumps(lst, ensure_ascii=False)
        ck_true("列表不含用户输入原文", "请用一句话打个招呼" not in blob)
        ck_true("列表不含模型回复原文", ANSWER not in blob)
        ck_true("列表不含思考原文", THINK not in blob)
        ck_true("列表行里没有 content/messages 字段",
                not any(k in row for k in ("content", "messages", "reasoning", "sent", "client")))
        st, item = get(f"http://127.0.0.1:{PROXY_PORT}/infer/item?id={row['id']}", KEY)
        ck("按 id 取元信息", 200, st)
        ck_true("单条元信息同样不含正文", ANSWER not in json.dumps(item, ensure_ascii=False))

        # ---- 非流式推理（走另一条响应分支，同样要留档）
        st, body = chat("再来一句", stream=False)
        ck("非流式请求 200", 200, st)
        t0 = time.time()
        rows = []
        while time.time() - t0 < 15:
            st, j = get(f"http://127.0.0.1:{PROXY_PORT}/infer/list?limit=50", KEY)
            rows = j.get("rows") or []
            if len(rows) >= 2:
                break
            time.sleep(0.2)
        ck("两条记录都在", 2, len(rows))
        ns = [r for r in rows if r.get("stream") == 0]
        ck("非流式那条标了 stream=0", 1, len(ns))
        ck("非流式：token 数来自 usage", 5, (ns or [{}])[0].get("completion_tokens"))
        ck("非流式：速度来自 timings", 33.5, (ns or [{}])[0].get("decode_tok_s"))

        # ---- 触发 RAG 组装：这条要验的是"**前后对照**"记全了没有
        # 小窗口（4096）下灌一段远超窗口的历史 → 必走组装（嵌入不可达时检索块为空，但组装照样发生）
        long_msgs = [{"role": "user", "content": f"第 {i} 轮：这是一段用来把上下文撑过窗口的历史。" * 60}
                     for i in range(1, 13)]
        long_msgs.append({"role": "user", "content": "总结一下上面这些轮次"})
        payload = {"model": "strata-a", "messages": long_msgs, "stream": False, "max_tokens": 16}
        req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
                                     data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {KEY}")
        with urllib.request.urlopen(req, timeout=60) as r:
            ck("超窗请求仍然 200（组装后送上游）", 200, r.status)
            r.read()
        t0 = time.time()
        asm = []
        while time.time() - t0 < 20:
            st, j = get(f"http://127.0.0.1:{PROXY_PORT}/infer/list?mode=assembled", KEY)
            asm = j.get("rows") or []
            if asm:
                break
            time.sleep(0.2)
        ck("组装发生了，并且留了档", 1, len(asm))
        a = (asm or [{}])[0]
        ck("记录标了 assembled", "assembled", a.get("mode"))
        ck_true("原输入/实际输入都记了条数", (a.get("client_msgs"), a.get("sent_msgs")) != (None, None),
                f"client={a.get('client_msgs')} sent={a.get('sent_msgs')}")
        ck("前后不同（组装改写了请求）", 1, a.get("differs"))
        ck_true("记录了检索查询长度", (a.get("rag_query_chars") or 0) > 0, f"{a.get('rag_query_chars')} 字符")
        ck_true("记录了最近窗口 token", (a.get("recent_tokens") or 0) > 0, f"{a.get('recent_tokens')}")
        ck_true("标记了是重建的块", a.get("rag_rebuild") in (0, 1), f"rag_rebuild={a.get('rag_rebuild')}")
        # /infer/rag 只吐元信息（正文那条口子必须还是拦着的）
        st, rm = get(f"http://127.0.0.1:{PROXY_PORT}/infer/rag?id={a['id']}", KEY)
        ck("按 id 取 RAG 元信息", 200, st)
        blob = json.dumps(rm, ensure_ascii=False)
        ck_true("RAG 元信息里没有用户输入原文", "这是一段用来把上下文撑过窗口的历史" not in blob)
        ck_true("RAG 元信息里没有模型回复原文", ANSWER not in blob)

        # ---- 统计与筛选
        st, s = get(f"http://127.0.0.1:{PROXY_PORT}/infer/stats?days=1", KEY)
        ck("统计 200", 200, st)
        ck("统计：条数", 3, s.get("total"))          # 流式 + 非流式 + 上面那条组装
        ck("统计：输出 token 合计", 13, s.get("out_tokens"))
        st, f = get(f"http://127.0.0.1:{PROXY_PORT}/infer/list?model=nope", KEY)
        ck("按模型筛选（无匹配）", 0, len(f.get("rows") or []))
        st, live = get(f"http://127.0.0.1:{PROXY_PORT}/infer/list?limit=5", KEY)
        ck_true("列表带 L1 内存里的实时条目", len(live.get("live") or []) >= 2,
                f"{len(live.get('live') or [])} 条")

        # ---- 存储状态：本机落盘 + （可选）对象存储归档
        st, h = get(f"http://127.0.0.1:{PROXY_PORT}/health", timeout=5)
        store = (h or {}).get("infer_store") or {}
        ck_true("health 里带记录状态", bool(store), json.dumps(
            {k: store.get(k) for k in ("stored", "segments", "uploaded", "minio_ok")}, ensure_ascii=False))
        ck("本机分段已封口", True, (store.get("segments") or 0) >= 1)
        ck_true("本机分段目录里有文件",
                list((tmp / "segs").rglob("*.jsonl.gz")), str(tmp / "segs"))
        if store.get("minio_ok"):
            ck_true("已归档到对象存储", (store.get("uploaded") or 0) >= 1,
                    f"uploaded={store.get('uploaded')}")
        else:
            print(f"  SKIP  对象存储不可用，跳过归档断言（last_error={store.get('last_error')}）")

        # ---- 失败也要留档：把假引擎关掉，请求打不上去（502），记录里必须有一条 error
        mock.shutdown()
        mock.server_close()
        code = 0
        try:
            chat("上游挂了", stream=False)
        except urllib.error.HTTPError as e:
            code = e.code
            e.read()
        ck("上游不可达 → 502 干净失败（不是挂起）", 502, code)
        t0 = time.time()
        err_rows = []
        while time.time() - t0 < 15:
            st, j = get(f"http://127.0.0.1:{PROXY_PORT}/infer/list?state=error", KEY)
            err_rows = j.get("rows") or []
            if err_rows:
                break
            time.sleep(0.2)
        ck("失败的那次也留下了记录", 1, len(err_rows))
        ck("记录标了 error", "error", (err_rows or [{}])[0].get("state"))
        ck_true("记录里带上了错误原因", bool((err_rows or [{}])[0].get("error")),
                str((err_rows or [{}])[0].get("error"))[:60])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()
        log.close()
        cleanup_bucket(tmp)

    ok = not FAILS
    print(("全部通过" if ok else f"失败 {len(FAILS)} 项：{FAILS}") + f"（临时目录 {tmp}）")
    return 0 if ok else 1


def cleanup_bucket(tmp=None):
    """删掉测试往桶里写的 infer-test/ 对象（正式前缀 infer/ 不碰）。"""
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import infer_store as S
        cli, bucket = S.minio_client()
        if cli is None:
            return
        keys = [o.object_name for o in cli.list_objects(bucket, prefix="infer-test/", recursive=True)]
        for k in keys:
            cli.remove_object(bucket, k)
        if keys:
            print(f"  清理测试对象 {len(keys)} 个（{bucket}/infer-test/）")
    except Exception as e:
        print("  清理对象存储失败（不致命）:", repr(e))


if __name__ == "__main__":
    sys.exit(main())
