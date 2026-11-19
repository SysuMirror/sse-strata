#!/usr/bin/env python3
"""首字节看门狗（first_byte_timeout_s）的回归测试。

背景（2026-11-03）：引擎可能「端口还在听、进程已经卡死」——TCP 探活和 /v1/status 都正常，
积压的请求只能挂到客户端自己超时（实测快道挂死时每条挂满 20 分钟）。代理现在给「等到上游第一个
字节」设上限，超时按不可达处理、改投兄弟道。

做法：起一个**只 accept 不回包**的假上游（8097）+ 一条真 lane（8092），临时代理（8098）把模型
钉到假上游上；断言请求在 ttfb+改投的时间内拿到 200，而不是挂住。

用法：~/.venv/bin/python test_first_byte_timeout.py   （不碰 8080/8091/8092 本身）
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SILENT_PORT, PROXY_PORT, GOOD_PORT = 8097, 8098, 8096
REAL_LANE = f"http://127.0.0.1:{GOOD_PORT}"
TTFB = 3


def good_server(sock: list):
    """兄弟道：立刻回一个最小 OpenAI JSON（改投成功就会看到它）。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):                       # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            body = json.dumps({"id": "ok", "object": "chat.completion", "created": 0,
                               "model": "ttfb-test", "choices": [
                                   {"index": 0, "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):               # 安静点
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", GOOD_PORT), H)
    sock.append(srv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def silent_server(sock: list):
    """accept 后什么都不发：模拟卡死的引擎（端口在听，但请求永远没有首字节）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", SILENT_PORT))
    srv.listen(16)
    sock.append(srv)
    conns = []
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        conns.append(c)          # 读也不读，写也不写：对端会一直等


def main() -> int:
    key = open("/home/cloud/logs/strata-api-key.txt").read().strip()
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    cfg.update(listen_host="127.0.0.1", listen_port=PROXY_PORT,
               upstreams=[f"http://127.0.0.1:{SILENT_PORT}", REAL_LANE],
               upstream=f"http://127.0.0.1:{SILENT_PORT}",
               model_routes={"ttfb-test": f"http://127.0.0.1:{SILENT_PORT}"},
               default_upstream=f"http://127.0.0.1:{SILENT_PORT}",
               first_byte_timeout_s=TTFB, rag_enabled=False)
    cfg["infer_store"] = dict(cfg.get("infer_store") or {}, enabled=False)
    tmp = f"/tmp/rag-proxy-ttfb-{os.getpid()}.json"
    json.dump(cfg, open(tmp, "w"))

    holders = []
    threading.Thread(target=silent_server, args=(holders,), daemon=True).start()
    good_server(holders)
    time.sleep(0.5)
    proc = subprocess.Popen([PY, os.path.join(HERE, "rag_proxy.py"), "--config", tmp],
                            stdout=open("/tmp/ttfb-test.log","w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(80):                      # 等代理起来
            try:
                socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=0.3).close()
                break
            except OSError:
                time.sleep(0.25)
        body = json.dumps({"model": "ttfb-test", "max_tokens": 8, "temperature": 0,
                           "messages": [{"role": "user", "content": "只回答：ok"}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key}"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                status, _ = r.status, r.read()
        except urllib.error.HTTPError as e:      # 没有兄弟道可投时是 502；有就是 200
            status = e.code
        dt = time.time() - t0
        ok = status == 200 and dt < TTFB + 10    # 3 s 卡住 → 改投健康兄弟道 → 立刻 200
        print(f"{'PASS' if ok else 'FAIL'}  首字节超时改投：status={status} 用时={dt:.1f}s "
              f"(ttfb={TTFB}s；期望 3 s 后改投并拿到 200)")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for s in holders:
            try:
                s.close()
            except Exception:
                pass
        os.unlink(tmp)


if __name__ == "__main__":
    sys.exit(main())