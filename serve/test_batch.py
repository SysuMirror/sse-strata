"""serve/test_batch.py - [SSE 2026-10-27] /v1/chat/completions/batch 批量端点单测（mock 引擎，无 GPU）。

    cd ~/Strata && .venv/bin/python -m unittest serve.test_batch -v
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve.frontend import ChatTemplate  # noqa: E402
from serve.server import ByteTokenizer, MockEngine, Service, serve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CTX = 8192


class Batch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.engine = MockEngine(tok, ["</think>\n\nalpha one", "</think>\n\nbeta two"], max_context=CTX)
        cls.svc = Service(cls.engine, tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, path, body):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def msg(self, text):
        return {"model": "test-model", "messages": [{"role": "user", "content": text}], "max_tokens": 32}

    def test_two_items_ordered(self):
        code, d = self.post("/v1/chat/completions/batch", {"model": "test-model", "requests": [self.msg("一"), self.msg("二")]})
        self.assertEqual(code, 200)
        self.assertEqual([x["index"] for x in d["data"]], [0, 1])
        self.assertTrue(all(x["status"] == 200 for x in d["data"]), d)
        texts = [x["body"]["choices"][0]["message"]["content"] for x in d["data"]]
        self.assertTrue(all(t for t in texts), texts)         # 每条都拿到内容
        self.assertGreater(d["usage"]["completion_tokens"], 0)

    def test_item_errors_are_per_item(self):
        code, d = self.post("/v1/chat/completions/batch", {"requests": [{"messages": "坏形状"}, self.msg("好")]})
        self.assertEqual(code, 200)                            # 批次整体成功，逐条报状态
        self.assertEqual(d["data"][0]["status"], 400)
        self.assertEqual(d["data"][1]["status"], 200)

    def test_count_bounds(self):
        code, d = self.post("/v1/chat/completions/batch", {"requests": []})
        self.assertEqual(code, 400)
        code, d = self.post("/v1/chat/completions/batch", {"requests": [self.msg("x")] * 17})
        self.assertEqual(code, 400)

    def test_stream_flag_is_aggregated_not_streamed(self):
        item = dict(self.msg("流"), stream=True)
        code, d = self.post("/v1/chat/completions/batch", {"requests": [item]})
        self.assertEqual(code, 200)
        self.assertEqual(d["data"][0]["status"], 200)          # 不 400：批内强制关流

    def test_single_endpoint_untouched(self):
        code, d = self.post("/v1/chat/completions", self.msg("旧路径"))
        self.assertEqual(code, 200)
        self.assertIn("choices", d)


if __name__ == "__main__":
    unittest.main()
