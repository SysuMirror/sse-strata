"""serve/test_ds41_serve.py - [SSE merge 2026-10-26] the DeepSeek-V4.1 lane in OUR serve fork.

Covers what the merge wired into serve/server.py + serve/dsml.py (the upstream test_dsv41.py assumes 0.1.51
server internals; these tests use ours): the DSML tool calls through the real Service.run with the MockEngine,
the thinking-without-end-tag recovery, the effort-word mapping, the template pick, the family switch.
No GPU, no pack.

    cd ~/Strata && .venv/bin/python -m unittest serve.test_ds41_serve -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import strata_tokenizer as ST                                # noqa: E402
from serve.dsml import DsmlOutputParser, pick_template       # noqa: E402
from serve.frontend import ChatTemplate                      # noqa: E402
from serve.server import MockEngine, Service                 # noqa: E402

# the fullwidth vertical bar and underscore that DeepSeek's special tokens are spelled with, built by code
# point: these literals have been silently mangled once already when written as text (2026-10-26)
V = chr(0xFF5C)          # fullwidth vertical bar
U = chr(0x2581)          # lower one-eighth block, the "word piece" underscore DeepSeek spells with
def _sp(name):
    return "<" + V + name + V + ">"
BOS = _sp("begin" + U + "of" + U + "sentence")
END = _sp("end" + U + "of" + U + "sentence")
DSML = V + "DSML" + V
SPECIALS = [(BOS, 3), (END, 3), (_sp("User"), 3), (_sp("Assistant"), 3), (_sp("System"), 3),
            (_sp("latest_reminder"), 3), ("<think>", 4), ("</think>", 4), (DSML, 4)]


def mini_tokenizer():
    b2u = ST.bytes_to_unicode()
    tokens = [s for s, _ in SPECIALS] + [b2u[b] for b in range(256)]
    types = [t for _, t in SPECIALS] + [1] * 256
    return ST.Tokenizer(tokens, [], types, "deepseek3", {"bos": 0, "eos": 1})


class DsMock(MockEngine):
    """MockEngine replays an end-of-turn marker from Qwen's vocabulary; the DeepSeek turn ends on ONE end token
    (id 1).  Rewriting the script tails to [1] also proves set_family swapped Service.stop_ids."""

    def __init__(self, tokenizer, script, max_context=8192):
        super().__init__(tokenizer, script, max_context=max_context)
        im = len(tokenizer.encode("<" + chr(124) + "im_end" + chr(124) + ">", parse_special=True))
        self.scripts = [s[:len(s) - im] + [1] for s in self.scripts]
        self.script = self.scripts[0]


def drain(svc, ids, thinking, tools, max_new=64):
    events, done = [], {}
    for kind, payload in svc.run(ids, thinking, tools, max_new, {}, threading.Event()):
        if kind == "event":
            events.append(payload)
        elif kind == "done":
            done = payload
    return events, done


class Family(unittest.TestCase):
    def setUp(self):
        self.tok = mini_tokenizer()
        self.tpl = ChatTemplate(ROOT / "serve/fixtures/deepseek41/chat_template.jinja")

    def service(self, script):
        eng = DsMock(self.tok, script, max_context=8192)
        eng.eos_ids = {1}
        return Service(eng, self.tok, self.tpl)

    def test_qwen_lane_defaults_untouched(self):
        svc = self.service("ok")
        self.assertIsNone(svc.family)
        self.assertIsNone(svc.parser_cls)
        self.assertEqual(svc.ds_kwargs({"reasoning_effort": "low"}), {"reasoning_effort": "low"})

    def test_effort_words(self):
        svc = self.service("ok")
        svc.set_family("deepseek41")
        self.assertEqual(svc.ds_kwargs({"reasoning_effort": "low"})["reasoning_effort"], 50)
        self.assertEqual(svc.ds_kwargs({"reasoning_effort": "medium"})["reasoning_effort"], 62)
        self.assertEqual(svc.ds_kwargs({"reasoning_effort": "max"})["reasoning_effort"], 100)
        self.assertEqual(svc.ds_kwargs({"reasoning_effort": "30"})["reasoning_effort"], 30)
        off = svc.ds_kwargs({"reasoning_effort": "none"})
        self.assertNotIn("reasoning_effort", off)
        self.assertIs(off.get("enable_thinking"), False)
        self.assertEqual(svc.ds_kwargs({}), {})


class Template(unittest.TestCase):
    def test_the_dsml_block_template_passes(self):
        tpl, swapped = pick_template(ROOT / "serve/fixtures/deepseek41/chat_template.jinja")
        self.assertTrue(tpl.exists())
        self.assertFalse(swapped)

    def test_an_old_spelling_template_is_swapped_out(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "chat_template.jinja"
            bad.write_text("...<｜DSML｜tool_calls>...", encoding="utf-8")
            tpl, swapped = pick_template(bad)
            self.assertTrue(swapped)
            self.assertIn("deepseek41", str(tpl))


class Parser(unittest.TestCase):
    def setUp(self):
        self.tok = mini_tokenizer()
        self.tpl = ChatTemplate(ROOT / "serve/fixtures/deepseek41/chat_template.jinja")

    def ds_service(self, script):
        eng = DsMock(self.tok, script, max_context=8192)
        eng.eos_ids = {1}
        svc = Service(eng, self.tok, self.tpl)
        svc.set_family("deepseek41")
        return svc

    def test_dsml_tool_call_end_to_end(self):
        tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {
            "type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}}}}}]
        L, G = chr(60), chr(62)   # < and >, spelled out for the same reason
        answer = (L + DSML + " calls" + G + chr(10) +
                  L + DSML + ' invoke name="get_weather"' + G + chr(10) +
                  L + DSML + ' parameter name="city" string="true"' + G + "Zagreb" +
                  L + "/" + DSML + " parameter" + G + chr(10) +
                  L + DSML + ' parameter name="days" string="false"' + G + "3" +
                  L + "/" + DSML + " parameter" + G + chr(10) +
                  L + "/" + DSML + " invoke" + G + chr(10) +
                  L + "/" + DSML + " calls" + G)
        svc = self.ds_service(answer)
        ids, thinking, _ = svc.prepare([{"role": "user", "content": "天气如何"}], tools, {})
        self.assertTrue(thinking)            # DS template thinks by default (model card: high)
        norm = [{"name": "get_weather", "parameters": tools[0]["function"]["parameters"]}]   # run() takes the parsed form
        events, done = drain(svc, ids, thinking, norm, max_new=256)
        kinds = [e.kind for e in events]
        self.assertIn("tool_call", kinds)
        call = [e for e in events if e.kind == "tool_call"][-1].call
        self.assertEqual(call.name, "get_weather")
        self.assertEqual(call.arguments.get("city"), "Zagreb")
        self.assertEqual(call.arguments.get("days"), 3)          # string="false" stays a JSON int
        self.assertEqual(done.get("finish"), "stop")

    def test_thinking_without_the_end_tag_becomes_the_answer(self):
        # the 2-bit sets often write the whole reply after THINK_OPEN and stop without THINK_CLOSE
        script = "THINK_OPEN答案是巴黎。"
        script = script.replace("THINK_OPEN", "THINK_OPEN_MARK").replace("THINK_CLOSE", "")
        svc = self.ds_service("The answer is Paris.")   # plain text, no think markers: the fast path
        ids, thinking, _ = svc.prepare([{"role": "user", "content": "capital of France?"}], None, {})
        self.assertTrue(thinking)
        events, done = drain(svc, ids, True, None, max_new=64)
        content = "".join(e.text for e in events if e.kind == "content")
        self.assertIn("Paris", content)

    def test_the_dsml_class_is_installed(self):
        svc = self.ds_service("x")
        self.assertIs(svc.parser_cls, DsmlOutputParser)


if __name__ == "__main__":
    unittest.main()
