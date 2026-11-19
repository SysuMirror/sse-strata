"""最小 MCP 客户端：JSON-RPC 2.0 over HTTP（streamable HTTP，兼容 SSE 响应）。

只支持**远程 HTTP 型** MCP server。stdio 型要在网关进程里 spawn 子进程，多用户共享
的网关不开这个口子（一个用户配的进程会拿到所有人的运行环境）。

接口就三件事：initialize → tools/list → tools/call；会话 id 按 URL 记在内存里。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

PROTOCOL = "2024-11-05"
_LOCK = threading.Lock()
_SESSIONS: dict[str, str] = {}


def _post(url: str, payload: dict, headers: dict | None, timeout: float, session: str | None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if session:
        req.add_header("Mcp-Session-Id", session)
    for k, v in (headers or {}).items():
        req.add_header(k, str(v))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace"), r.headers.get("Content-Type", ""), r.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:240]
        raise RuntimeError(f"MCP HTTP {e.code}: {body}")


def _parse(raw: str, ctype: str, want_id) -> dict:
    """响应可能是纯 JSON，也可能是 SSE（每行 data: {...}）。取 id 对得上的那一条。"""
    if "text/event-stream" in (ctype or ""):
        found = None
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                j = json.loads(line[5:].strip())
            except Exception:
                continue
            if j.get("id") == want_id:
                found = j
                break
        if found is None:
            raise RuntimeError("MCP: 流里没有对应的 JSON-RPC 响应")
        return found
    return json.loads(raw or "{}")


def rpc(url: str, method: str, params: dict | None = None,
        headers: dict | None = None, timeout: float = 25.0, notify: bool = False) -> dict:
    with _LOCK:
        sid = _SESSIONS.get(url)
    rid = int(time.time() * 1000) % 10 ** 9
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        payload["id"] = rid
    raw, ctype, nsid = _post(url, payload, headers, timeout, sid)
    if nsid:
        with _LOCK:
            _SESSIONS[url] = nsid
    if notify:
        return {}
    obj = _parse(raw, ctype, rid)
    if obj.get("error"):
        raise RuntimeError(f"MCP error: {str(obj['error'])[:280]}")
    return obj.get("result") or {}


def initialize(url: str, headers: dict | None = None) -> dict:
    res = rpc(url, "initialize", {
        "protocolVersion": PROTOCOL,
        "capabilities": {"tools": {}},
        "clientInfo": {"name": "sse-strata-hub", "version": "1.0"},
    }, headers)
    try:
        rpc(url, "notifications/initialized", {}, headers, notify=True)
    except Exception:
        pass                       # 有的实现不认这条通知，不影响后续
    return res


def list_tools(url: str, headers: dict | None = None) -> list[dict]:
    initialize(url, headers)
    res = rpc(url, "tools/list", {}, headers)
    return res.get("tools") or []


def call_tool(url: str, name: str, arguments: dict, headers: dict | None = None, timeout: float = 60.0):
    initialize(url, headers)
    res = rpc(url, "tools/call", {"name": name, "arguments": arguments or {}}, headers, timeout)
    # MCP 的返回是 content 数组：[{type:"text", text:"..."}], 可能还有图片
    parts = []
    for c in (res.get("content") or []):
        if c.get("type") == "text":
            parts.append(c.get("text") or "")
        elif c.get("type") == "image":
            parts.append(f"[image {c.get('mimeType','')} {len(c.get('data') or '')} chars]")
        else:
            parts.append(json.dumps(c, ensure_ascii=False)[:200])
    return {"ok": not res.get("isError"), "text": "\n".join(p for p in parts if p)}