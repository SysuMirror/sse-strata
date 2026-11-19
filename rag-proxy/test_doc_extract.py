#!/usr/bin/env python3
"""Unit tests for the gateway's document extraction (no GPU, no proxy needed).

Run: /home/cloud/Strata/.venv/bin/python test_doc_extract.py
"""
from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doc_extract  # noqa: E402


def _cfg() -> dict:
    c = json.loads((Path(__file__).with_name("config.json")).read_text())
    c.setdefault("documents", {})
    c["documents"]["allow_urls"] = False
    return c


def _text_pdf(text: str) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


def _scanned_pdf() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.draw_rect(pymupdf.Rect(0, 0, 400, 200), color=(1, 0, 0), fill=(1, 0, 0))
    out = doc.tobytes()
    doc.close()
    return out


def _docx(text: str) -> bytes:
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["item", "value"])
    ws.append(["code", "XLSX-9"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _anthropic_doc(data: bytes, mime: str, title: str) -> dict:
    return {"model": "x", "messages": [{"role": "user", "content": [
        {"type": "document", "title": title,
         "source": {"type": "base64", "media_type": mime, "data": _b64(data)}},
        {"type": "text", "text": "question"}]}]}


def _openai_file(data: bytes, mime: str, name: str) -> dict:
    return {"model": "x", "messages": [{"role": "user", "content": [
        {"type": "file", "file": {"filename": name,
                                  "file_data": f"data:{mime};base64,{_b64(data)}"}},
        {"type": "text", "text": "question"}]}]}


def _texts(req: dict) -> str:
    return "\n".join(p.get("text", "") for m in req["messages"]
                     for p in (m.get("content") or []) if isinstance(p, dict) and p.get("type") == "text")


class Detect(unittest.TestCase):
    def test_formats(self):
        c = _cfg()["documents"]
        self.assertEqual(doc_extract.detect_format(_text_pdf("hello"), "a.pdf", "application/pdf"), "pdf")
        self.assertEqual(doc_extract.detect_format(_docx("hello"), "a.docx", None), "docx")
        self.assertEqual(doc_extract.detect_format(_xlsx(), "a.xlsx", None), "xlsx")
        self.assertEqual(doc_extract.detect_format(b"a,b\n1,2\n", "a.csv", "text/csv"), "csv")
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg==")
        self.assertIsNone(doc_extract.detect_format(png, "a.png", "image/png"))


class Extract(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()

    def _run(self, req, path="/v1/chat/completions", budget=20000):
        parts = doc_extract.find_documents(req, path, self.cfg)
        self.assertTrue(parts)
        new, info = doc_extract.transform(req, path, parts, budget, self.cfg)
        return new, info

    def test_anthropic_pdf(self):
        new, info = self._run(_anthropic_doc(_text_pdf("CODE ZEBRA-42"), "application/pdf",
                                             "report.pdf"), "/v1/messages")
        self.assertEqual(info["mode"], "doc")
        self.assertIn("ZEBRA-42", _texts(new))
        self.assertIn("report.pdf", _texts(new))

    def test_openai_docx(self):
        new, info = self._run(_openai_file(_docx("LAUNCH DOCX-777"),
                                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                           "plan.docx"))
        self.assertEqual(info["mode"], "doc")
        self.assertIn("DOCX-777", _texts(new))

    def test_openai_xlsx(self):
        new, info = self._run(_openai_file(_xlsx(),
                                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                           "budget.xlsx"))
        self.assertEqual(info["mode"], "doc")
        self.assertIn("XLSX-9", _texts(new))

    def test_pdf_sent_as_image_url(self):
        req = {"model": "x", "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:application/pdf;base64," + _b64(_text_pdf("CODE PDF-1"))}},
            {"type": "text", "text": "question"}]}]}
        new, info = self._run(req)
        self.assertEqual(info["mode"], "doc")
        self.assertIn("PDF-1", _texts(new))

    def test_scanned_pdf_renders_images(self):
        new, info = self._run(_anthropic_doc(_scanned_pdf(), "application/pdf", "scan.pdf"),
                              "/v1/messages")
        self.assertEqual(info["mode"], "doc_images")
        self.assertGreaterEqual(info["images"], 1)
        types = [p.get("type") for p in new["messages"][0]["content"]]
        self.assertIn("image", types)

    def test_truncation_keeps_head_and_tail(self):
        text = "HEAD-111\n" + ("填充文本。" * 3000) + "\nTAIL-999"
        req = _anthropic_doc(text.encode(), "text/plain", "big.txt")
        new, info = self._run(req, "/v1/messages", budget=500)
        self.assertEqual(info["mode"], "doc_truncated")
        out = _texts(new)
        self.assertIn("HEAD-111", out)
        self.assertIn("TAIL-999", out)
        self.assertIn("truncated", out)
        self.assertLessEqual(doc_extract.estimate_text_tokens(out, self.cfg), 500)

    def test_multiple_documents(self):
        req = {"model": "x", "messages": [{"role": "user", "content": [
            {"type": "file", "file": {"filename": "a.txt", "file_data": "data:text/plain;base64," + _b64(b"AAA-1")}},
            {"type": "file", "file": {"filename": "b.txt", "file_data": "data:text/plain;base64," + _b64(b"BBB-2")}},
            {"type": "text", "text": "question"}]}]}
        new, info = self._run(req, budget=2000)
        self.assertEqual(info["docs"], 2)
        out = _texts(new)
        self.assertIn("AAA-1", out)
        self.assertIn("BBB-2", out)

    def test_legacy_doc_errors(self):
        req = _anthropic_doc(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 100,
                             "application/msword", "old.doc")
        parts = doc_extract.find_documents(req, "/v1/messages", self.cfg)
        with self.assertRaises(doc_extract.DocumentError):
            doc_extract.transform(req, "/v1/messages", parts, 2000, self.cfg)

    def test_budget_too_small(self):
        req = _anthropic_doc(_text_pdf("x"), "application/pdf", "a.pdf")
        parts = doc_extract.find_documents(req, "/v1/messages", self.cfg)
        with self.assertRaises(doc_extract.DocumentError):
            doc_extract.transform(req, "/v1/messages", parts, 0, self.cfg)


class Security(unittest.TestCase):
    def test_local_path_and_url_rejected(self):
        c = _cfg()["documents"]
        for src in ("/etc/passwd", "file:///etc/passwd", "http://127.0.0.1:8080/health"):
            with self.assertRaises(doc_extract.DocumentError, msg=src):
                doc_extract.load_source(src, c)

    def test_real_image_is_left_alone(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lK3Q6wAAAABJRU5ErkJggg==")
        req = {"model": "x", "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(png)}}]}]}
        self.assertEqual(doc_extract.find_documents(req, "/v1/chat/completions", _cfg()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
