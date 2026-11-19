#!/usr/bin/env python3
"""测试用的假引擎（只在测试里用，绝不替代真引擎）。

为什么要它：要验证"网关到底把什么送进了窗口"，必须看到组装后的请求体；真引擎看不到这点。
它做三件事：
  1. `POST /v1/chat/completions`：把收到的请求体逐条记到 JSONL（测试据此断言组装结构）；
  2. `POST /v1/messages/count_tokens`：给一个近似 token 数（CJK 1 字/token，其余 4 字/token）；
  3. 回答用**oracle**：1M 精度报告实测"模型侧没有短板——事实在窗口里就答得对，不在就答不出"，
     所以 oracle 只在提示词里找得到那条事实时才回答它，否则回"不知道"。
     这样"召回率"就纯粹反映网关的检索/组装质量，可复现、不烧 GPU。

用法：python mock_upstream.py --port 8099 --record /tmp/reqs.jsonl
"""
import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REC: str = "/tmp/mock_reqs.jsonl"
LOCK = threading.Lock()
# 窗口守卫测试用：--window 打开后，mock 像真引擎一样对 prompt + max_tokens + 8 > 窗口 直接 400。
WINDOW: int = 0
IMAGE_TOKENS: int = 800          # 真引擎把一张图展开成几百 token，而 count_tokens 只数 1 个
LIE_MARKER: str = ""             # 请求里含这个串时，第一次故意报一次超窗 400（测网关的重试网）
LIE_ROOM: int = 0
LIED = {"done": False}


def est_tokens(s: str) -> int:
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ")
    return cjk + max(0, (len(s) - cjk)) // 4


def n_images(body: dict) -> int:
    n = 0
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") in ("image", "image_url"):
                    n += 1
    return n


def body_tokens(body: dict, images_as: int = 1) -> int:
    """文本按 CJK 1.0 / 其余 0.25 计（与真引擎同一量级）；图片按 images_as 计——
    count_tokens 传 1，生成路径传 IMAGE_TOKENS，这个差额正是真引擎与计数接口不一致的地方。"""
    t = 0
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            t += est_tokens(c) + 8
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    t += est_tokens(str(b.get("text", ""))) + est_tokens(str(b.get("thinking", ""))) + 8
        if isinstance(m.get("tool_calls"), list):
            for tc in m["tool_calls"]:
                t += est_tokens(json.dumps(tc, ensure_ascii=False)) + 8
    t += est_tokens(str(body.get("system") or ""))
    t += est_tokens(json.dumps(body.get("tools") or [], ensure_ascii=False))
    t += n_images(body) * images_as
    return t


def overfit_400(prompt: int, max_new: int, room: int | None = None) -> str:
    """照抄真引擎 #545 的措辞——网关的重试网就是按这句里的 "at most N here" 工作的。"""
    room = max(0, WINDOW - 8 - prompt) if room is None else room
    return (f"prompt ({prompt} tokens) + max tokens ({max_new}) exceeds the context ({WINDOW}); "
            f"requests are never truncated. Send a smaller max_tokens (at most {room} here), or add "
            f"\"fit_max_tokens\": true to the model's strata-<model>.json to shorten it to the room left (#545)")


def oracle(body: dict) -> str:
    """只在提示词里找得到答案时才回答——模拟真引擎"事实进窗即答对"的行为。"""
    prompt = json.dumps(body.get("messages") or [], ensure_ascii=False)
    prompt += str(body.get("system") or "")
    q = ""
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            q = m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"), ensure_ascii=False)
            break
    mlab = re.search(r"the (\w+) code", q or "")
    if mlab:
        label = mlab.group(1)
        hit = re.search(rf"the {label} code is ([A-Z]+-\d{{4}})", prompt)
        if hit:
            return hit.group(1)
        return "I don't have that information."
    return "noted"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/v1/health"):
            self._json({"status": "ok", "mock": True})
        else:
            self._json({"object": "list", "data": []})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        path = self.path.split("?")[0]
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json({"error": {"message": "bad json"}})
            return
        if path.endswith("/count_tokens"):
            self._json({"input_tokens": body_tokens(body)})     # 图片只数 1 个 pad：真引擎就这样
            return
        if path.endswith("/chat/completions"):
            prompt = body_tokens(body, images_as=IMAGE_TOKENS)  # 生成路径：图片按展开后的 token 数
            max_new = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
            raw = json.dumps(body, ensure_ascii=False)
            if LIE_MARKER and LIE_MARKER in raw and not LIED["done"]:
                # 假装"我数到的比你报的多，而且余量比你要的 max_tokens 小"——网关必须照这句话重发
                LIED["done"] = True
                self._json({"error": {"type": "invalid_request_error",
                                      "message": overfit_400(prompt, max_new,
                                                             room=max(1, (max_new or LIE_ROOM) - 1500))}}, 400)
                return
            if WINDOW and max_new and prompt + max_new + 8 > WINDOW:
                self._json({"error": {"type": "invalid_request_error",
                                      "message": overfit_400(prompt, max_new)}}, 400)
                return
            with LOCK:
                with open(REC, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"prompt_tokens": prompt, "max_tokens": max_new, "body": body},
                                       ensure_ascii=False) + "\n")
            self._json({"id": "mock", "object": "chat.completion", "model": body.get("model", "mock"),
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": oracle(body)}}]})
            return
        self._json({})


def main():
    global REC, WINDOW, IMAGE_TOKENS, LIE_MARKER, LIE_ROOM
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--record", default=REC)
    ap.add_argument("--window", type=int, default=0, help=">0 时像真引擎一样拒绝超窗请求（400，从不截断）")
    ap.add_argument("--image-tokens", type=int, default=800)
    ap.add_argument("--lie-marker", default="", help="含此串的第一个请求故意报一次超窗 400")
    ap.add_argument("--lie-room", type=int, default=12000)
    a = ap.parse_args()
    REC = a.record
    WINDOW, IMAGE_TOKENS, LIE_MARKER, LIE_ROOM = a.window, a.image_tokens, a.lie_marker, a.lie_room
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"mock upstream on :{a.port} -> recording {a.record}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
