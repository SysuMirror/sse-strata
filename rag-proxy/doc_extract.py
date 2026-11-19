#!/usr/bin/env python3
"""Document extraction for the gateway (PDF / DOCX / XLSX / XLS / CSV / text).

The engine only understands text and images.  This module detects document parts in
OpenAI/Anthropic requests, extracts readable text (or renders scanned PDF pages as
images), and keeps the result inside a caller-provided token budget so a large file
cannot push the request over the model's 262144-token window.

It is deliberately independent of rag_proxy.py (no circular import): the caller
passes the config dict and the token budget.
"""
from __future__ import annotations

import base64
import copy
import io
import os
import pathlib
import re
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser

IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
    "image/bmp", "image/tiff", "image/avif",
}
DOC_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/rtf", "text/rtf",
    "text/csv", "text/plain", "text/markdown",
}


class DocumentError(ValueError):
    """A document part could not be read/supported; the gateway answers 400."""


# ------------------------------------------------------------------ source loading
def _split_data_url(url: str) -> tuple[str, bytes] | None:
    if not url.startswith("data:"):
        return None
    header, _, payload = url.partition(",")
    mime = header[5:].split(";", 1)[0] or "application/octet-stream"
    if ";base64" in header.lower():
        try:
            return mime, base64.b64decode(payload, validate=False)
        except Exception as e:
            raise DocumentError(f"bad base64 in data URL: {e}") from None
    return mime, urllib.parse.unquote_to_bytes(payload)


def _size_limit(cfg: dict) -> int:
    return int(float(cfg.get("max_file_mb", 20)) * 1024 * 1024)


def _check_size(data: bytes, cfg: dict):
    limit = _size_limit(cfg)
    if len(data) > limit:
        raise DocumentError(f"file is larger than {limit // (1024 * 1024)} MB")


def _read_limited(stream, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise DocumentError(f"file is larger than {limit // (1024 * 1024)} MB")
    return data


def _fetch(url: str, cfg: dict) -> bytes:
    if not cfg.get("allow_urls", False):
        raise DocumentError("document URLs are disabled; send the file as base64/file_data")
    limit = _size_limit(cfg)
    req = urllib.request.Request(url, headers={"User-Agent": "strata-gateway"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return _read_limited(r, limit)


def load_source(source, cfg: dict, media_type: str | None = None,
                filename: str | None = None) -> tuple[bytes | None, str | None, str | None]:
    """Return (bytes, text, media_type).  `source` is an Anthropic source dict, an
    OpenAI file dict, a data URL, an http(s) URL, or a base64 string.

    Client-provided local paths and (by default) URLs are rejected: the gateway is
    public-facing, so reading /etc/... or fetching internal services would be an
    information leak/SSRF.  Set documents.allow_urls=true only if you really need
    remote URLs and trust the caller.
    """
    if isinstance(source, dict):
        kind = str(source.get("type") or "").lower()
        media_type = source.get("media_type") or source.get("mime_type") or media_type
        filename = source.get("filename") or source.get("name") or filename
        if kind == "text":
            return None, str(source.get("text") or source.get("data") or ""), media_type
        if kind == "content":
            blocks = source.get("content") or []
            text = "\n".join(str(b.get("text") or "") for b in blocks
                             if isinstance(b, dict) and b.get("type") == "text")
            return None, text, media_type
        if kind == "url":
            source = source.get("url")
        elif kind == "base64":
            source = source.get("data")
            if isinstance(source, str):
                data = _decode_text_or_base64(source, cfg, media_type)
                _check_size(data, cfg)
                return data, None, media_type
        elif kind in ("file", "file_data"):
            source = source.get("file_data") or source.get("data") or source.get("url")
        elif source.get("url") or source.get("path") or source.get("file_data"):
            source = source.get("file_data") or source.get("url") or source.get("path")
    if not isinstance(source, str) or not source:
        raise DocumentError("document source is empty or unsupported")
    if source.startswith("file://"):
        raise DocumentError("local document paths are not allowed; send the file as base64/file_data")
    _check_encoded_size(source, cfg)
    data_url = _split_data_url(source)
    if data_url:
        _check_size(data_url[1], cfg)
        return data_url[1], None, media_type or data_url[0]
    if source.startswith(("http://", "https://")):
        return _fetch(source, cfg), None, media_type
    if os.path.isfile(source):
        raise DocumentError("local document paths are not allowed; send the file as base64/file_data")
    # A bare base64 payload (Anthropic's source.data often arrives without a data: prefix)
    try:
        data = base64.b64decode(source, validate=False)
    except Exception:
        raise DocumentError("document source is neither a data URL nor base64") from None
    _check_size(data, cfg)
    return data, None, media_type


def _check_encoded_size(text: str, cfg: dict):
    # base64 inflates by 4/3; reject before decoding so a huge payload cannot spike memory
    if len(text) > _size_limit(cfg) * 4 // 3 + 4096:
        raise DocumentError(f"file is larger than {cfg.get('max_file_mb', 20)} MB")


def _decode_text_or_base64(text: str, cfg: dict, media_type: str | None) -> bytes:
    _check_encoded_size(text, cfg)
    data_url = _split_data_url(text)
    if data_url:
        return data_url[1]
    if text.startswith(("http://", "https://")):
        return _fetch(text, cfg)
    if os.path.isfile(text):
        raise DocumentError("local document paths are not allowed; send the file as base64/file_data")
    if text.startswith("file://"):
        raise DocumentError("local document paths are not allowed; send the file as base64/file_data")
    try:
        return base64.b64decode(text, validate=False)
    except Exception:
        # a source declared text: turn it into bytes so extract_text can decode it
        return text.encode("utf-8")


# ------------------------------------------------------------------ format detection
def _zip_kind(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
    except Exception:
        return None
    if any(n.startswith("word/") for n in names):
        return "docx"
    if any(n.startswith("xl/") for n in names):
        return "xlsx"
    if any(n.startswith("ppt/") for n in names):
        return "pptx"
    return None


def detect_format(data: bytes, filename: str | None = None,
                  media_type: str | None = None) -> str | None:
    """Return pdf/docx/doc/xlsx/xls/pptx/csv/text/rtf/html, or None for an image/unknown."""
    mime = (media_type or "").split(";", 1)[0].strip().lower()
    ext = pathlib.Path(filename or "").suffix.lower()
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return _zip_kind(data) or ("docx" if ext == ".docx" else
                                   "xlsx" if ext == ".xlsx" else
                                   "pptx" if ext == ".pptx" else None)
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if ext in (".xls",) or mime == "application/vnd.ms-excel":
            return "xls"
        return "doc"
    if data.startswith(b"{\\rtf"):
        return "rtf"
    low = data[:512].lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html"):
        return "html"
    # image magic: leave it to the vision encoder
    if data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"BM", b"GIF87a", b"GIF89a")):
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return None
    if mime in IMAGE_MIMES or ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"):
        return None
    if mime == "application/pdf" or ext == ".pdf":
        return "pdf"
    if mime.endswith("wordprocessingml.document") or ext == ".docx":
        return "docx"
    if mime == "application/msword" or ext == ".doc":
        return "doc"
    if mime.endswith("spreadsheetml.sheet") or ext == ".xlsx":
        return "xlsx"
    if mime == "application/vnd.ms-excel" or ext == ".xls":
        return "xls"
    if mime == "text/csv" or ext == ".csv":
        return "csv"
    if mime in ("text/plain", "text/markdown") or ext in (".txt", ".md", ".markdown"):
        return "text"
    if mime in ("application/rtf", "text/rtf") or ext == ".rtf":
        return "rtf"
    if mime in ("text/html", "application/xhtml+xml") or ext in (".html", ".htm"):
        return "html"
    if mime.startswith("text/"):
        return "text"
    return None


# ------------------------------------------------------------------ extraction
def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _extract_pdf(data: bytes, cfg: dict) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocumentError("PDF support is not installed on the gateway (pypdf)") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("encrypted PDF is not supported")
        pages = len(reader.pages)
        parts = []
        for i, page in enumerate(reader.pages, 1):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                parts.append(f"--- page {i} ---\n{t}")
        text = "\n\n".join(parts).strip()
    except DocumentError:
        raise
    except Exception as e:
        raise DocumentError(f"PDF could not be read: {e}") from None
    stripped = text.strip()
    # A short *text* PDF (e.g. one page, 40 chars) is not a scan.  Only treat it as scanned when
    # there is essentially no text layer, or when a multi-page PDF has almost no text per page.
    scanned = (len(stripped) < 20 or
               (pages > 1 and len(stripped) < int(cfg.get("min_text_chars", 200))
                and len(stripped) / max(1, pages) < 20))
    return {"text": text, "pages": pages, "chars": len(text), "scanned": scanned}


def _extract_docx(data: bytes, cfg: dict) -> dict:
    try:
        import docx
    except ImportError:
        raise DocumentError("DOCX support is not installed on the gateway (python-docx)") from None
    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as e:
        raise DocumentError(f"DOCX could not be read: {e}") from None
    parts = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        parts.append("")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts).strip()
    return {"text": text, "pages": None, "chars": len(text), "scanned": False}


def _extract_xlsx(data: bytes, cfg: dict) -> dict:
    try:
        import openpyxl
    except ImportError:
        raise DocumentError("XLSX support is not installed on the gateway (openpyxl)") from None
    max_rows = int(cfg.get("max_xlsx_rows", 500))
    max_cols = int(cfg.get("max_xlsx_cols", 30))
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        raise DocumentError(f"XLSX could not be read: {e}") from None
    parts = []
    try:
        for ws in wb.worksheets:
            parts.append(f"--- sheet: {ws.title} ---")
            for r, row in enumerate(ws.iter_rows(values_only=True)):
                if r >= max_rows:
                    parts.append(f"[... sheet truncated at {max_rows} rows ...]")
                    break
                vals = [str(v) for v in row[:max_cols] if v is not None and str(v).strip() != ""]
                if vals:
                    parts.append(" | ".join(vals))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    text = "\n".join(parts).strip()
    return {"text": text, "pages": None, "chars": len(text), "scanned": False}


def _extract_xls(data: bytes, cfg: dict) -> dict:
    try:
        import xlrd
    except ImportError:
        raise DocumentError("legacy XLS support is not installed on the gateway (xlrd)") from None
    max_rows = int(cfg.get("max_xlsx_rows", 500))
    max_cols = int(cfg.get("max_xlsx_cols", 30))
    try:
        wb = xlrd.open_workbook(file_contents=data)
    except Exception as e:
        raise DocumentError(f"XLS could not be read: {e}") from None
    parts = []
    for ws in wb.sheets():
        parts.append(f"--- sheet: {ws.name} ---")
        for r in range(min(ws.nrows, max_rows)):
            vals = [str(ws.cell_value(r, c)) for c in range(min(ws.ncols, max_cols))
                    if str(ws.cell_value(r, c)).strip() != ""]
            if vals:
                parts.append(" | ".join(vals))
        if ws.nrows > max_rows:
            parts.append(f"[... sheet truncated at {max_rows} rows ...]")
    text = "\n".join(parts).strip()
    return {"text": text, "pages": None, "chars": len(text), "scanned": False}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        s = data.strip()
        if s:
            self.parts.append(s)


def _extract_html(data: bytes) -> dict:
    p = _HTMLText()
    try:
        p.feed(_decode_text(data))
    except Exception:
        pass
    text = "\n".join(p.parts).strip()
    return {"text": text, "pages": None, "chars": len(text), "scanned": False}


def _extract_rtf(data: bytes) -> dict:
    raw = _decode_text(data)
    raw = re.sub(r"\\'[0-9a-fA-F]{2}", " ", raw)
    raw = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", raw)
    raw = raw.replace("{", "").replace("}", "")
    text = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return {"text": text, "pages": None, "chars": len(text), "scanned": False}


def extract_text(data: bytes, fmt: str, filename: str | None, cfg: dict) -> dict:
    if fmt == "pdf":
        return _extract_pdf(data, cfg)
    if fmt == "docx":
        return _extract_docx(data, cfg)
    if fmt == "xlsx":
        return _extract_xlsx(data, cfg)
    if fmt == "xls":
        return _extract_xls(data, cfg)
    if fmt == "html":
        return _extract_html(data)
    if fmt == "rtf":
        return _extract_rtf(data)
    if fmt in ("csv", "text"):
        text = _decode_text(data).strip()
        return {"text": text, "pages": None, "chars": len(text), "scanned": False}
    if fmt == "doc":
        raise DocumentError("legacy .doc is not supported; convert it to .docx, PDF or plain text")
    if fmt == "pptx":
        raise DocumentError("PPTX is not supported yet; convert it to PDF or extract the text")
    raise DocumentError(f"unsupported document format: {fmt}")


# ------------------------------------------------------------------ rendering (scanned PDF)
def render_pdf_pages(data: bytes, cfg: dict, max_pages: int) -> list[bytes]:
    try:
        import pymupdf as fitz      # PyMuPDF >= 1.24; the old `fitz` alias is deprecated
    except ImportError:
        try:
            import fitz
        except ImportError:
            raise DocumentError(
                "scanned PDF rendering is not installed on the gateway (pymupdf)") from None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise DocumentError(f"PDF could not be opened for rendering: {e}") from None
    dpi = int(cfg.get("pdf_dpi", 150))
    max_side = int(cfg.get("max_image_side", 2048))
    out = []
    try:
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            w, h = page.rect.width, page.rect.height
            scale = min(dpi / 72.0, max_side / max(w, h) if max(w, h) else dpi / 72.0)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            out.append(pix.tobytes("png"))
    finally:
        doc.close()
    return out


# ------------------------------------------------------------------ token budget
def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or
            0xF900 <= o <= 0xFAFF or 0x3040 <= o <= 0x30FF or
            0xAC00 <= o <= 0xD7AF)


def estimate_text_tokens(text: str, cfg: dict) -> int:
    cjk = float(cfg.get("tok_per_char_cjk", 1.05))
    digit = float(cfg.get("tok_per_char_digit", 1.05))
    other = float(cfg.get("tok_per_char_other", 0.70))
    n = 0.0
    for ch in text:
        if _is_cjk(ch):
            n += cjk
        elif ch.isdigit():
            n += digit
        else:
            n += other
    return int(n + 0.999)


def truncate_text(text: str, budget: int, cfg: dict) -> tuple[str, bool, int]:
    """Keep head+tail within `budget` estimated tokens. -> (text, truncated, omitted chars)."""
    if budget <= 0:
        return "", True, len(text)
    total = estimate_text_tokens(text, cfg)
    if total <= budget:
        return text, False, 0
    ratio = float(cfg.get("truncate_head_ratio", 0.7))
    marker_reserve = 64
    use = max(64, budget - marker_reserve)
    head_budget = max(16, int(use * ratio))
    tail_budget = max(0, use - head_budget)
    cjk = float(cfg.get("tok_per_char_cjk", 1.05))
    digit = float(cfg.get("tok_per_char_digit", 1.05))
    other = float(cfg.get("tok_per_char_other", 0.70))

    def cost(ch):
        return cjk if _is_cjk(ch) else digit if ch.isdigit() else other

    n = 0.0
    head_cut = 0
    for i, ch in enumerate(text):
        n += cost(ch)
        if n > head_budget:
            head_cut = i
            break
    n = 0.0
    tail_cut = len(text)
    for i in range(len(text) - 1, -1, -1):
        n += cost(text[i])
        if n > tail_budget:
            tail_cut = i + 1
            break
    if head_cut >= tail_cut:            # too small to split: keep the head
        tail_cut = len(text)
    omitted = max(0, tail_cut - head_cut)
    out = (text[:head_cut].rstrip() + "\n\n[... document truncated: "
           f"{omitted} characters omitted ...]\n\n" + text[tail_cut:].lstrip())
    return out, True, omitted


# ------------------------------------------------------------------ request walking
def _known_image(media_type: str | None, filename: str | None) -> bool:
    mime = (media_type or "").split(";", 1)[0].strip().lower()
    ext = pathlib.Path(filename or "").suffix.lower()
    return mime in IMAGE_MIMES or ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")


def _looks_document_url(url: str) -> bool:
    low = url.lower()
    return any(low.endswith(ext) or (ext + "?") in low for ext in
               (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt", ".md", ".rtf", ".html", ".htm"))


def _source_spec(part: dict, api: str) -> tuple[object, str | None, str | None] | None:
    """Extract (source, filename, media_type) from a document-ish part."""
    if api == "anthropic":
        if part.get("type") == "document":
            src = part.get("source") or {}
            if not isinstance(src, dict):
                return src, part.get("title"), None
            return src, part.get("title") or src.get("filename"), src.get("media_type")
        if part.get("type") == "image":
            src = part.get("source") or {}
            if not isinstance(src, dict):
                return src, None, None
            return src, src.get("filename"), src.get("media_type")
        return None
    kind = part.get("type")
    if kind in ("file", "input_file"):
        f = part.get("file") if isinstance(part.get("file"), dict) else part
        src = f.get("file_data") or f.get("file_url") or f.get("url") or f.get("data")
        return src, f.get("filename") or f.get("name"), f.get("mime_type") or f.get("media_type")
    if kind in ("image_url", "input_image"):
        iu = part.get("image_url")
        src = iu.get("url") if isinstance(iu, dict) else iu
        if not isinstance(src, str) and kind == "input_image":
            src = part.get("image_url") or part.get("url")
        return src, None, None
    return None


def find_documents(req: dict, path: str, cfg: dict) -> list[dict]:
    """Return descriptors for document parts; leaves real image parts alone.
    `cfg` may be the full proxy config (with a nested `documents` section) or that section."""
    docs_cfg = cfg.get("documents") if isinstance(cfg.get("documents"), dict) else cfg
    api = "anthropic" if path.split("?")[0].rstrip("/").endswith("/v1/messages") else "openai"
    out = []
    for mi, msg in enumerate(req.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            spec = _source_spec(part, api)
            if spec is None:
                continue
            source, filename, media_type = spec
            # a file/document part is always a document; an image part only if it is not a known image
            kind = part.get("type")
            # A data URL carries its own MIME type: a PDF sent through image_url must be treated as
            # a document (this used to be the 400 "cannot identify image file" path).
            if isinstance(source, str) and source.startswith("data:"):
                data_mime = source[5:].split(";", 1)[0].strip().lower()
                if data_mime:
                    media_type = media_type or data_mime
            if kind in ("image", "image_url", "input_image") and _known_image(media_type, filename):
                continue
            if isinstance(source, str) and kind in ("image", "image_url", "input_image") and \
                    not source.startswith("data:") and \
                    not (media_type and media_type.lower() in DOC_MIMES) and not _looks_document_url(source):
                # unknown image URL: don't fetch a normal image just to inspect it
                continue
            if not source:
                if kind in ("file", "input_file", "document"):
                    raise DocumentError("document part has no file_data/url (file_id is not supported)")
                continue
            data, text, mime = load_source(source, docs_cfg, media_type, filename)
            if text is not None:
                out.append({"msg": mi, "part": pi, "kind": kind, "fmt": "text",
                            "text": text, "data": None, "filename": filename or f"document-{len(out)+1}",
                            "media_type": media_type})
                continue
            fmt = detect_format(data or b"", filename, mime or media_type)
            if fmt is None:
                # a real image / unknown binary: leave it to the existing image path
                continue
            out.append({"msg": mi, "part": pi, "kind": kind, "fmt": fmt,
                        "text": None, "data": data, "filename": filename or f"document-{len(out)+1}",
                        "media_type": media_type})
    return out


def without_documents(req: dict, parts: list[dict]) -> dict:
    """A copy of the request with document parts replaced by empty text parts (for budgeting)."""
    out = copy.deepcopy(req)
    by_msg = {}
    for d in parts:
        by_msg.setdefault(d["msg"], {})[d["part"]] = d
    for mi, mapping in by_msg.items():
        content = out.get("messages", [])[mi].get("content")
        if not isinstance(content, list):
            continue
        new = []
        for pi, part in enumerate(content):
            new.append({"type": "text", "text": ""} if pi in mapping else part)
        out["messages"][mi]["content"] = new
    return out


def _text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def _image_part(api: str, png: bytes) -> dict:
    b64 = base64.b64encode(png).decode()
    if api == "anthropic":
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}


def transform(req: dict, path: str, parts: list[dict], budget: int, cfg: dict) -> tuple[dict, dict]:
    """Replace document parts with extracted text / rendered page images."""
    if not parts:
        return req, {}
    api = "anthropic" if path.split("?")[0].rstrip("/").endswith("/v1/messages") else "openai"
    docs_cfg = cfg.get("documents") or {}
    # Token-ratio settings live at the top level; document-specific settings override them.
    work_cfg = {**cfg, **docs_cfg}
    page_tokens = int(docs_cfg.get("page_tokens", 1024))
    max_pages = int(docs_cfg.get("max_pages", 8))
    render_scanned = bool(docs_cfg.get("pdf_render_scanned", True))
    out = copy.deepcopy(req)
    by_msg = {}
    for d in parts:
        by_msg.setdefault(d["msg"], {})[d["part"]] = d
    remaining = max(0, budget)
    left = len(parts)
    total_images = total_chars = 0
    any_truncated = False
    for d in parts:
        per_doc = max(256, remaining // max(1, left)) if remaining >= 256 else 0
        left -= 1
        if per_doc < 256:
            raise DocumentError("document + conversation leaves no room in the 256K context; shorten the conversation")
        filename = d.get("filename") or "document"
        fmt = d.get("fmt") or "text"
        if d.get("text") is not None:
            text, pages, scanned = d["text"], None, False
        else:
            info = extract_text(d.get("data") or b"", fmt, filename, work_cfg)
            text, pages, scanned = info.get("text", ""), info.get("pages"), info.get("scanned", False)
        replacement = []
        if scanned and fmt == "pdf":
            if not render_scanned:
                raise DocumentError(
                    f"scanned PDF '{filename}' has no text layer; enable documents.pdf_render_scanned "
                    "or convert it to text/images upstream")
            pages_can = max(1, per_doc // page_tokens)
            pages_can = min(max_pages, pages_can)
            # render at most the pages the budget allows
            pngs = render_pdf_pages(d.get("data") or b"", work_cfg, pages_can)
            total_images += len(pngs)
            shown = f"{len(pngs)}"
            total_pages = pages or len(pngs)
            note = (f"rendered first {shown} of {total_pages} pages as images"
                    if len(pngs) < total_pages else f"rendered {shown} pages as images")
            replacement.append(_text_part(
                f"[document: {filename} | PDF | {total_pages} pages | scanned/no text | {note}]\n"
                "The pages are attached as images; read them directly."))
            replacement.extend(_image_part(api, png) for png in pngs)
            remaining = max(0, remaining - len(pngs) * page_tokens)
        else:
            text = text or ""
            total_chars += len(text)
            # leave room for the provenance header (filename/format/page count/truncation note)
            budget_for_text = max(64, per_doc - 128)
            text, truncated, omitted = truncate_text(text, budget_for_text, work_cfg)
            any_truncated = any_truncated or truncated
            page_note = f"{pages} pages | " if pages else ""
            header = (f"[document: {filename} | {fmt.upper()} | {page_note}"
                      f"{len(text)} chars extracted")
            if truncated:
                header += f" | truncated: {omitted} chars omitted"
            header += "]\n"
            replacement.append(_text_part(header + text))
            remaining = max(0, remaining - estimate_text_tokens(header + text, work_cfg))
        d["replacement"] = replacement
    for mi, mapping in by_msg.items():
        content = out.get("messages", [])[mi].get("content")
        if not isinstance(content, list):
            continue
        new = []
        for pi, part in enumerate(content):
            d = mapping.get(pi)
            if d is not None:
                new.extend(d["replacement"])
            else:
                new.append(part)
        out["messages"][mi]["content"] = new
    mode = "doc_images" if total_images else ("doc_truncated" if any_truncated else "doc")
    return out, {"mode": mode, "docs": len(parts), "truncated": any_truncated,
                 "images": total_images, "chars": total_chars}
