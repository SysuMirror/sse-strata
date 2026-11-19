#!/usr/bin/env python3
"""并发监控（GET /concurrency）的离线测试（2026-10-15）。

守的一句话：**代理侧看到的"在途请求"必须与真实并发一致，且分得清是哪条通道**。
代理是唯一能同时看到两条通道与全部在途请求的地方（引擎自己的 /slots 永远只报 1 个槽、
/v1/status 的 in_flight 是个聚合数），所以前端并发视图完全依赖这里的登记表与通道轮询。

不联网、不碰真引擎、不碰生产代理：起**两个假引擎**当两条通道（各自 SSE 流式回答 +
/v1/status + /metrics），进程内跑 rag_proxy，断言：
  T1 鉴权        → /concurrency 无 key 401、错 key 401、Bearer/x-api-key 都对 200
  T2 在途可见    → 4 个请求并发在飞时，in_flight 有 4 条、按通道分成 2+2、
                   状态 streaming、TTFT/估算 token/实时速度都有值
  T3 通道状态    → lanes 两条、名称取自 lane_labels、serving_slots/GPU 来自引擎 /v1/status + /metrics
  T4 结束归档    → 请求结束后 in_flight 清空、条目进入 recent 且状态 completed
  T5 通道宕机    → 关掉一条通道后它被标记离线（另一条仍在线），mode 仍为 multi

用法：/home/cloud/Strata/.venv/bin/python test_concurrency.py
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LANE_A, LANE_B = 8096, 8097       # 两条假通道（与既有测试 8096/8097/8085/8086/8087 不冲突）
PROXY_PORT = 8085
CHUNKS, CHUNK_S = 12, 0.15        # 流式回答 12 块 × 0.15s ≈ 1.8s：够观察在途状态

import rag_proxy as R                                             # noqa: E402

OUT = sys.stdout
FAILS = []


def ck(name: str, ok: bool, detail: str = ""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {detail}" if detail else ""), file=OUT, flush=True)
    if not ok:
        FAILS.append(name)


class LaneMock(BaseHTTPRequestHandler):
    """假引擎的一条通道：SSE 流式回答 + /v1/status + /metrics（供代理的通道轮询取数）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        label = getattr(self.server, "label", "lane")
        if path in ("/health", "/v1/health"):
            self._json({"status": "ok"})
        elif path == "/v1/status":
            self._json({"service": "strata", "model": f"mock-{label}", "loaded": True, "uptime_s": 123,
                        "concurrency": {"serving": 4, "requested": 4},
                        "activity": {"requests": 7, "in_flight": 0},
                        # 一条道一张卡，卡号由各通道自己上报（a=GPU 0、b=GPU 1）—— 监控页要说「这条道在哪张卡上」，
                        # 只能靠这里；两张同型号卡的数值本身分不出彼此。
                        "machine": {"gpu": {"name": "MockGPU", "used_mib": 1024, "total_mib": 24576,
                                            "util_pct": 55, "temp_c": 61, "power_w": 210, "power_limit_w": 300},
                                    "gpus": [{"index": 0 if label == "a" else 1, "name": "MockGPU", "used_mib": 1024,
                                              "total_mib": 24576, "util_pct": 55, "temp_c": 61, "power_w": 210,
                                              "power_limit_w": 300}],
                                    "ram": {"used_gib": 10.0, "total_gib": 216.0}}})
        elif path == "/metrics":
            self._json({"live": {"state": "generating", "tok_s": 42.0}, "totals": {"requests": 7},
                        "requests": [], "history": {"gpu_util": [50, 55], "tok_s": [40, 42]},
                        # 「上下文占用」要按道显示，就得拿到**每条道自己的**窗口大小与专家驻留
                        "engine": {"model": f"mock-{label}", "max_context": 262144,
                                   "expert_slots": 6226, "expert_cache_mib": 10341}})
        else:
            self._json({}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            json.loads(raw.decode("utf-8"))
        except Exception:
            self._json({"error": {"message": "bad json"}}, 400)
            return
        if self.path.split("?")[0].endswith("/count_tokens"):
            self._json({"input_tokens": 10})
            return
        # SSE：第一块立刻发（TTFT 可测），之后每 CHUNK_S 秒一块
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for i in range(CHUNKS):
            ev = {"choices": [{"delta": {"content": f"第{i}块内容。"}}]}
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(CHUNK_S)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def serve_lane(port: int, label: str) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), LaneMock)
    srv.label = label
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(model: str, session: str, out: dict, timeout: float = 60.0):
    body = {"model": model, "max_tokens": 32, "stream": True,
            "messages": [{"role": "user", "content": "说一句问候"}]}
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Strata-Session", session)      # 不同会话键 → 不被会话锁串行化（那是有意为之）
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while r.read1(4096):
                pass
            out["r"] = (r.status, round(time.perf_counter() - t0, 2))
    except urllib.error.HTTPError as e:
        e.read()
        out["r"] = (e.code, round(time.perf_counter() - t0, 2))
    except Exception as e:
        out["r"] = (-1, repr(e))


def conc(headers: dict | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/concurrency")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, None


def wait_lanes(n: int = 2, expect_up: int | None = None, timeout: float = 8.0) -> dict:
    """等通道轮询把状态填好（后台线程每 lane_poll_s 一次）。"""
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        code, data = conc({"x-api-key": "test"})
        if code == 200 and data and len(data.get("lanes") or []) >= n:
            last = data
            ups = sum(1 for l in last["lanes"] if l["up"])
            if expect_up is None or ups == expect_up:
                return last
        time.sleep(0.25)
    return last


def main() -> int:
    lane_a = serve_lane(LANE_A, "a")
    lane_b = serve_lane(LANE_B, "b")

    cfg = dict(R.DEFAULTS)
    cfg.update({
        "listen_port": PROXY_PORT,
        "upstream": f"http://127.0.0.1:{LANE_A}",
        "upstreams": [f"http://127.0.0.1:{LANE_A}", f"http://127.0.0.1:{LANE_B}"],
        "default_upstream": f"http://127.0.0.1:{LANE_A}",
        "model_routes": {"mock-a": f"http://127.0.0.1:{LANE_A}", "mock-b": f"http://127.0.0.1:{LANE_B}"},
        "lane_labels": {f"http://127.0.0.1:{LANE_A}": "快道", f"http://127.0.0.1:{LANE_B}": "普道"},
        "lane_poll_s": 1,
        "max_context": 32768, "recent_window_tokens": 4096, "chunk_tokens": 256,
        "count_timeout_s": 5, "embed_url": "http://127.0.0.1:9/", "rerank_url": "http://127.0.0.1:9/",
        "debug": False,
    })
    R.CFG = cfg
    R.API_KEY = "test"

    proxy = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), R.Handler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    threading.Thread(target=R.lane_poller, daemon=True).start()
    time.sleep(0.5)

    try:
        print("== T1 鉴权 ==", file=OUT, flush=True)
        # 这两条原来断言 401，是**旧行为**：那时 /concurrency 还有一层只认 key 的检查。
        # 后来那层检查被**有意去掉**了 —— 它会把登录用户挡在外面（登录用户本来就不带 key，
        # 由代理代持），而「监控页不登录也能看」是当时定的需求。见 rag_proxy.do_GET 里
        # /concurrency 分支上的说明。所以现在的正确期望是：匿名可读（与 /metrics 同档）。
        ck("匿名可读（监控页不登录也能看，代理代持 key）", conc()[0] == 200, f"{conc()[0]}")
        ck("错 key 也不影响这条只读接口", conc({"Authorization": "Bearer nope"})[0] == 200, "")
        code_b, data_b = conc({"Authorization": "Bearer test"})
        ck("Bearer 正确 key → 200", code_b == 200, f"{code_b}")
        ck("x-api-key 正确 key → 200", conc({"x-api-key": "test"})[0] == 200, "")

        print("== T3 通道状态（引擎状态经轮询进入 lanes）==", file=OUT, flush=True)
        d = wait_lanes(2, expect_up=2)
        lanes = {l["label"]: l for l in (d.get("lanes") or [])}
        ck("两条通道都在", len(lanes) == 2, f"{list(lanes)}")
        ck("mode = multi（配了两个上游）", d.get("mode") == "multi", f"{d.get('mode')}")
        la = lanes.get("快道") or {}
        ck("通道名取自 lane_labels", "快道" in lanes and "普道" in lanes, f"{list(lanes)}")
        ck("通道模型名来自引擎 /v1/status", la.get("model") == "mock-a", f"{la.get('model')}")
        ck("serving_slots 来自 /v1/status 的 concurrency", la.get("serving_slots") == 4, f"{la.get('serving_slots')}")
        ck("GPU 数据来自 /v1/status 的 machine", (la.get("gpu") or {}).get("util_pct") == 55, f"{la.get('gpu')}")
        lb = lanes.get("普道") or {}
        ck("逐卡读数带卡号，各通道自己的那张卡", [(la.get("gpus") or [{}])[0].get("index"),
                                            (lb.get("gpus") or [{}])[0].get("index")] == [0, 1],
           f"{la.get('gpus')} / {lb.get('gpus')}")
        ck("live 来自 /metrics", (la.get("live") or {}).get("tok_s") == 42.0, f"{la.get('live')}")
        ck("每条道自己的引擎事实（窗口 / 专家驻留）也带上，页面才能一条道一行显示上下文占用",
           (la.get("engine") or {}).get("max_context") == 262144
           and (la.get("engine") or {}).get("expert_cache_mib") == 10341, f"{la.get('engine')}")

        print("== T2 在途请求可见（4 路并发）==", file=OUT, flush=True)
        outs = [{} for _ in range(4)]
        models = ["mock-a", "mock-a", "mock-b", "mock-b"]
        threads = [threading.Thread(target=post, args=(models[i], f"cc-{i}", outs[i])) for i in range(4)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        time.sleep(0.6)                                   # 全部进入流式
        code, d = conc({"x-api-key": "test"})
        mid = time.perf_counter() - t0
        live = (d or {}).get("in_flight") or []
        ck("取到并发数据", code == 200 and d is not None, f"HTTP {code}")
        ck("在途 4 条", len(live) == 4, f"{len(live)} 条 @{mid:.1f}s")
        lanes_of = {e["lane"] for e in live}
        ck("分属两条通道", len(lanes_of) == 2, f"{sorted(lanes_of)}")
        per = {u: sum(1 for e in live if e["lane"] == u) for u in lanes_of}
        ck("按模型分流：每条通道 2 条", sorted(per.values()) == [2, 2], f"{per}")
        ck("状态均为 streaming", all(e["state"] == "streaming" for e in live),
           f"{sorted({e['state'] for e in live})}")
        ck("每条都有 TTFT 与估算 token/速度",
           all(e["ttft_s"] is not None and e["est_tokens"] > 0 and e["tok_s"] is not None for e in live),
           f"{[(e['ttft_s'], e['est_tokens']) for e in live]}")
        ck("识别为流式且带模型名", all(e["stream"] and e["model"] in ("mock-a", "mock-b") for e in live), "")
        agg = (d or {}).get("aggregate") or {}
        ck("聚合条：在途 4 / 生成中 4 / 速度 > 0",
           agg.get("in_flight") == 4 and agg.get("generating") == 4 and (agg.get("tok_s") or 0) > 0, f"{agg}")
        ck("通道卡片里的在途数与登记表一致",
           all(l["in_flight"] == per.get(l["url"], 0) for l in d["lanes"]), f"{[(l['label'], l['in_flight']) for l in d['lanes']]}")

        for t in threads:
            t.join(timeout=60)
        ck("4 个请求都拿到了 200", all(o.get("r", (0,))[0] == 200 for o in outs), f"{[o.get('r') for o in outs]}")

        print("== T4 结束后归档 ==", file=OUT, flush=True)
        time.sleep(0.3)
        code, d = conc({"x-api-key": "test"})
        ck("在途清空", not (d or {}).get("in_flight"), f"{(d or {}).get('in_flight')}")
        recent = (d or {}).get("recent") or []
        done = [e for e in recent if e["state"] == "completed"]
        ck("4 条已归档为 completed", len(done) >= 4, f"completed {len(done)} / recent {len(recent)}")
        ck("归档条目带时长与估算 token",
           all(e.get("elapsed_s") and e.get("est_tokens", 0) > 0 for e in done[:4]), "")

        print("== T5 通道宕机 ==", file=OUT, flush=True)
        lane_b.shutdown()
        d = wait_lanes(2, expect_up=1, timeout=8.0)
        st = {l["label"]: l for l in (d.get("lanes") or [])}
        ck("宕掉的通道被标记离线", st.get("普道", {}).get("up") is False, f"{st.get('普道', {}).get('up')}")
        ck("另一条仍在线", st.get("快道", {}).get("up") is True, f"{st.get('快道', {}).get('up')}")
        ck("宕机通道带错误信息", bool(st.get("普道", {}).get("error")), f"{st.get('普道', {}).get('error')}")
        ck("mode 仍为 multi（配置未变）", d.get("mode") == "multi", f"{d.get('mode')}")
    finally:
        lane_a.shutdown()
        try:
            lane_b.shutdown()
        except Exception:
            pass

    print("=" * 62, file=OUT, flush=True)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 项未通过 -> {FAILS}", file=OUT, flush=True)
        return 1
    print("ALL PASS", file=OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
