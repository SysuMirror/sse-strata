#!/usr/bin/env python3
"""推理记录（inference log）——每一次推理的元信息与**全部内容**都留档。

为什么要它：出了问题要能倒带——谁、什么时候、用什么参数、问了什么，引擎怎么答的、慢在哪一段。
界面上**只看元信息**（不展示输入 / 思考 / 回复），但内容照存，便于事后做错误分析。

分层（对象存储是主副本，本机全部是缓存 / 加速）：
    L1 内存      最近 hot_rows 条元信息（页面「实时」那一段，零 IO）
    L2 SQLite    <db>                                全量元信息索引，分页 / 过滤，重启不丢
    L3 本机分段   <dir>/<日期>/seg-*.jsonl.gz         完整记录（内容在这里），近期热读
    L4 MinIO     s3://<bucket>/<prefix>/<日期>/...    主副本，跨 pod 重建仍在

**不影响推理**：请求线程只做 dict 赋值、计字节、以及**一次非阻塞入队**（队列满就丢弃并计数，
绝不阻塞、绝不等待 IO）；解析、压缩、落盘、上传全在后台线程里。写线程与上传线程分开，
上传慢也不会堵住写线程。

MinIO 不可用时降级：分段留在本机（标 uploaded=0），后台反复重试，**数据不丢**，只是还没归档。
"""
from __future__ import annotations

import datetime
import gzip
import json
import os
import pathlib
import queue
import re
import sqlite3
import threading
import time
import uuid

# --------------------------------------------------------------------------- 配置
DEFAULTS = {
    "enabled": True,
    "dir": "/home/cloud/logs/infer-segs",     # 本机分段目录（缓存）
    "db": "/home/cloud/logs/infer.db",        # 本机元信息索引
    "minio_prefix": "infer",                  # 桶里的前缀
    "seg_max_mb": 8,                          # 单个分段到多大就封口
    "seg_max_s": 300,                         # 或封口的最长间隔
    "queue_max": 5000,                        # 入队上限；满了就丢弃并计数（绝不阻塞请求线程）
    "capture_max_mb": 16,                     # 单次推理最多抓多少响应字节（超了截断并标记）
    "local_cache_gb": 4.0,                    # 本机分段缓存上限
    "local_cache_days": 7,                    # 或保留天数（保留期内不删）
    "hot_rows": 50,                           # L1 内存里留多少条
    "upload_retry_s": 300,                    # 上传失败后的重试间隔
}

_CFG: dict | None = None
_CFG_LOCK = threading.Lock()

# --------------------------------------------------------------------------- 状态
_Q: "queue.Queue[dict]" = queue.Queue(maxsize=1)
_UPQ: "queue.Queue[str]" = queue.Queue()
_HOT: "list[dict]" = []
DB_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
STATE = {"queued": 0, "stored": 0, "dropped": 0, "rows": 0, "segments": 0,
         "uploaded": 0, "upload_fail": 0, "pruned": 0, "last_error": None, "last_ts": None}

_MINIO_FACTORY = None                          # 测试可注入；None 用默认实现
_MINIO_CACHE: dict = {"cli": None, "bucket": None, "at": 0.0, "err": None}


def log(*a):
    print("[infer-store]", *a, flush=True)


def bj(ts: float) -> str:
    """北京时间 ISO 串（Pod 是 UTC，界面上按 +8 显示才符合习惯）。"""
    return (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- MinIO
def credentials() -> tuple[str, str, str, str] | tuple[None, None, None, None]:
    """取对象存储凭证：先环境变量，再退回 ~/.minio.env。

    为什么要退回文件：平台注入的环境变量只给 supervisor 拉起的进程，而这个代理是被
    `su -l` 拉起来的登录 shell 链条，注入的变量到不了它这儿（实测：日志里"minio 不可用"
    一次都没出现，因为压根没走到 import）。文件里那份是用户给的，稳定。
    """
    keys = ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET", "MINIO_BUCKET")
    vals = {k: (os.environ.get(k) or "").strip() for k in keys}
    if not all(vals.values()):
        f = pathlib.Path(os.path.expanduser("~/.minio.env"))
        try:
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r"^(?:export\s+)?([A-Z_]+)\s*=\s*(.*)$", line)
                    if not m:
                        continue
                    k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
                    if k in vals and not vals[k]:
                        vals[k] = v
        except OSError:
            pass
    if all(vals.values()):
        return vals["MINIO_ENDPOINT"], vals["MINIO_ACCESS_KEY"], vals["MINIO_SECRET"], vals["MINIO_BUCKET"]
    return None, None, None, None


def minio_client():
    """返回 (client, bucket)；没凭证 / 没装 minio / 连不上都返回 (None, None)。

    失败后 60 秒内不再重试（避免每次上传都卡在连接超时上）。"""
    if _MINIO_FACTORY is not None:
        return _MINIO_FACTORY()
    now = time.time()
    if _MINIO_CACHE["cli"] is not None:
        return _MINIO_CACHE["cli"], _MINIO_CACHE["bucket"]
    if _MINIO_CACHE["err"] and now - _MINIO_CACHE["at"] < 60:
        return None, None
    ep, ak, sk, bucket = credentials()
    if not (ep and ak and sk and bucket):
        _MINIO_CACHE.update(at=now, err="no-credentials")
        return None, None
    try:
        from minio import Minio                      # 只有真要用了才 import
        cli = Minio(ep.replace("http://", "").replace("https://", ""),
                    access_key=ak, secret_key=sk, secure=ep.startswith("https"))
        _MINIO_CACHE.update(cli=cli, bucket=bucket, at=now, err=None)
        return cli, bucket
    except Exception as e:                            # 没装包 / 端点不对
        _MINIO_CACHE.update(at=now, cli=None, err=repr(e))
        STATE["last_error"] = f"minio: {e!r}"
        return None, None


def set_minio_factory(fn):
    """测试注入：fn() -> (client, bucket)。client 只要有 fput_object/get_object/stat_object。"""
    global _MINIO_FACTORY
    _MINIO_FACTORY = fn
    _MINIO_CACHE.update(cli=None, bucket=None, err=None, at=0.0)


# --------------------------------------------------------------------------- 建库
_SCHEMA = """
CREATE TABLE IF NOT EXISTS infer(
  id TEXT PRIMARY KEY, ts REAL, iso TEXT, path TEXT, model TEXT, lane TEXT, lane_label TEXT,
  session TEXT, stream INTEGER, state TEXT, http_status INTEGER, error TEXT, mode TEXT,
  prompt_tokens INTEGER, cached_tokens INTEGER, completion_tokens INTEGER,
  est_prompt_tokens INTEGER, recent_tokens INTEGER, memory_tokens INTEGER, block_reused INTEGER,
  ttft_ms REAL, total_ms REAL, decode_tok_s REAL, prompt_tok_s REAL, retrieve_ms REAL,
  events INTEGER, bytes_out INTEGER, client_bytes INTEGER, sent_bytes INTEGER, resp_bytes INTEGER,
  finish_reason TEXT, content_chars INTEGER, reasoning_chars INTEGER, tool_calls INTEGER,
  truncated INTEGER, retries INTEGER, ua TEXT, seg TEXT, line INTEGER, uploaded INTEGER,
  rag_query_chars INTEGER, rag_pool INTEGER, rag_candidates INTEGER, rag_chunks INTEGER,
  rag_score_max REAL, rag_score_min REAL, rag_block_chars INTEGER, rag_rerank INTEGER,
  rag_rebuild INTEGER, client_msgs INTEGER, sent_msgs INTEGER, client_chars INTEGER,
  sent_chars INTEGER, differs INTEGER);
CREATE INDEX IF NOT EXISTS infer_ts ON infer(ts DESC);
CREATE INDEX IF NOT EXISTS infer_model ON infer(model);
CREATE INDEX IF NOT EXISTS infer_state ON infer(state);
CREATE TABLE IF NOT EXISTS segments(
  name TEXT PRIMARY KEY, date TEXT, created REAL, bytes INTEGER, rows INTEGER,
  closed INTEGER DEFAULT 0, uploaded INTEGER DEFAULT 0);
"""

_COLS = ("id", "ts", "iso", "path", "model", "lane", "lane_label", "session", "stream", "state",
         "http_status", "error", "mode", "prompt_tokens", "cached_tokens", "completion_tokens",
         "est_prompt_tokens", "recent_tokens", "memory_tokens", "block_reused", "ttft_ms",
         "total_ms", "decode_tok_s", "prompt_tok_s", "retrieve_ms", "events", "bytes_out",
         "client_bytes", "sent_bytes", "resp_bytes", "finish_reason", "content_chars",
         "reasoning_chars", "tool_calls", "truncated", "retries", "ua", "seg", "line", "uploaded",
         # RAG 前后对照（2026-10-16 加）：组装是否触发、把哪些历史喂进去了、块有多少条
         "rag_query_chars", "rag_pool", "rag_candidates", "rag_chunks", "rag_score_max",
         "rag_score_min", "rag_block_chars", "rag_rerank", "rag_rebuild",
         "client_msgs", "sent_msgs", "client_chars", "sent_chars", "differs")


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        p = pathlib.Path(_CFG["db"])
        p.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(p), check_same_thread=False, timeout=10)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA synchronous=NORMAL")
        _CONN.executescript(_SCHEMA)
        # 增量迁移：老库（跑过线上流量）缺的是新加的列，CREATE TABLE IF NOT EXISTS 不会补。
        # 加列只影响新写入的行，老行为 NULL —— 够用，也就不折腾重写表。
        have = {r["name"] for r in _CONN.execute("PRAGMA table_info(infer)")}
        for c in _COLS:
            if c not in have:
                kind = "REAL" if c.endswith(("_ms", "_s")) or c.startswith("rag_score") else "INTEGER"
                _CONN.execute(f"ALTER TABLE infer ADD COLUMN {c} {kind}")
        _CONN.commit()
    return _CONN


def init(cfg: dict) -> bool:
    """由 rag_proxy.main() 调用。cfg 是整份配置，这里只取 cfg["infer_store"]。"""
    global _CFG, _Q
    with _CFG_LOCK:
        _CFG = {**DEFAULTS, **((cfg or {}).get("infer_store") or {})}
        if not _CFG["enabled"]:
            log("已禁用（配置 infer_store.enabled=false）")
            return False
        _Q = queue.Queue(maxsize=int(_CFG["queue_max"]))
        pathlib.Path(_CFG["dir"]).mkdir(parents=True, exist_ok=True)
        try:
            _conn()
        except Exception as e:
            log("元信息库打不开，记录功能关闭:", repr(e))
            _CFG["enabled"] = False
            return False
        # 上一次进程留下的**未封口**分段：写它的进程已经没了，永远不会再封口 —— 不处理的话
        # 这些段永远归档不到对象存储（内容只在本机，本机缓存一清就真没了）。启动时按"已封口"
        # 收尾并交给上传线程补传；文件是按秒 flush 过的，读到最后会有 EOFError，但内容都在。
        # 判据必须看**磁盘**：segments 表里的 rows/bytes 是封口时才回写的，未封口段的这两列恒为 0，
        # 拿它们筛会一个都筛不出来（第一版就是这么写的，线上一次都没触发）。
        with DB_LOCK:
            rows_open = [dict(r) for r in _conn().execute("SELECT name, date FROM segments WHERE closed=0")]
        orphans, empty = [], []
        for r in rows_open:
            p = pathlib.Path(_CFG["dir"]) / (r["date"] or "") / r["name"]
            (orphans if p.exists() and p.stat().st_size > 0 else empty).append(r["name"])
        with DB_LOCK:
            if empty:                 # 开了口没写进东西的空段：没内容可传，直接标归档
                _conn().executemany("UPDATE segments SET closed=1, uploaded=1 WHERE name=?",
                                    [(n,) for n in empty])
            if orphans:
                _conn().executemany("UPDATE segments SET closed=1 WHERE name=?", [(n,) for n in orphans])
            _conn().commit()
        for n in orphans:
            _UPQ.put_nowait(n)        # 队列无界，这里不会阻塞
        if orphans or empty:
            log(f"上次遗留的未封口分段 {len(orphans)} 个有内容 → 已收尾待补传；{len(empty)} 个是空段 → 直接归档")
    threading.Thread(target=_worker, name="infer-store", daemon=True).start()
    threading.Thread(target=_uploader, name="infer-upload", daemon=True).start()
    log(f"启用：分段 {_CFG['dir']}，索引 {_CFG['db']}，桶前缀 {_CFG['minio_prefix']}/")
    return True


# --------------------------------------------------------------------------- 热路径 API
def begin(path: str, body: bytes | None, headers) -> dict:
    """请求进来时开一条记录。返回的 dict 由请求线程独占改，最后交给 finish()。"""
    if _CFG is None or not _CFG["enabled"]:
        return {}
    now = time.time()
    return {
        "id": time.strftime("%Y%m%d-%H%M%S", time.gmtime(now + 8 * 3600)) + "-" + uuid.uuid4().hex[:6],
        "ts": now, "path": path, "t0": time.perf_counter(),
        "model": None, "lane": None, "lane_label": None, "session": None,
        "stream": None, "state": None, "http_status": None, "error": None, "mode": None,
        "est": None, "recent_tokens": None, "memory_tokens": None, "block_reused": None,
        "retrieve_ms": None, "retries": 0,
        "client_bytes": len(body or b""), "sent_bytes": None, "client": None, "sent": None,
        "buf": bytearray(), "trunc": False, "bytes_out": 0, "events": 0,
        "first_chunk_at": None, "ended_at": None,
        "ua": ((headers.get("User-Agent") or "")[:160] if headers else ""),
    }


def note_route(rec: dict, **kw) -> None:
    """路由 / 组装阶段的信息（都是小字段，赋值即返回）。"""
    if rec:
        for k, v in kw.items():
            if v is not None and k in rec:
                rec[k] = v


def note_request(rec: dict, client: dict | None, sent: dict | None, client_bytes=None, sent_bytes=None):
    """记下客户端原始请求与**真正发往引擎**的请求（超窗组装后两者会不同，错误分析要看这个差）。

    除了两份正文，还顺手记一组便宜的结构指纹（消息条数 / 正文字符数）—— 页面上"前后对照"要展示的
    就是这几个数，而且不用把 4MB 的请求序列化一遍去比。"""
    if not rec:
        return
    rec["client"] = client
    rec["sent"] = sent
    if client_bytes is not None:
        rec["client_bytes"] = client_bytes
    if sent_bytes is not None:
        rec["sent_bytes"] = sent_bytes
    for tag, obj in (("client", client), ("sent", sent)):
        msgs = (obj or {}).get("messages") if isinstance(obj, dict) else None
        rec[f"{tag}_msgs"] = len(msgs) if isinstance(msgs, list) else None
        rec[f"{tag}_chars"] = _msg_chars(msgs)
    if rec.get("client_msgs") is not None and rec.get("sent_msgs") is not None:
        rec["differs"] = int(rec["client_msgs"] != rec["sent_msgs"]
                             or rec["client_chars"] != rec["sent_chars"])


def _msg_chars(msgs) -> int | None:
    """消息体的字符总量（只数，不序列化：1M 上下文那种请求序列化一遍要几十毫秒）。"""
    if not isinstance(msgs, list):
        return None
    n = 0
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    n += len(p["text"])
    return n


def note_rag(rec: dict, info: dict) -> None:
    """记下这一次 RAG 组装的"前后依据"：窗口预算、最近窗口/检索块 token、查询指纹、
    每一条被检索回来的块（哪一段历史、多长、三阶段分数）。

    **只留元信息与结构**：块的正文不单独存（它就在发给引擎的请求里，`request.sent.messages`
    的系统提示词之后那一条），所以不会把同一份内容存两遍。"""
    if rec and info:
        rec["rag"] = info


def note_retry(rec: dict):
    if rec:
        rec["retries"] = rec.get("retries", 0) + 1


def capture(rec: dict, chunk: bytes) -> None:
    """把响应字节抓一份（有上限）。热路径上就是一次 memcpy，解析留到后台。"""
    if not rec or not chunk:
        return
    rec["bytes_out"] += len(chunk)
    rec["events"] += chunk.count(b"data:")
    cap = int(_CFG["capture_max_mb"] * 1024 * 1024)
    buf = rec["buf"]
    if len(buf) < cap:
        take = chunk[: cap - len(buf)]
        buf.extend(take)
        if len(take) < len(chunk):
            rec["trunc"] = True
    elif not rec["trunc"]:
        rec["trunc"] = True


def finish(rec: dict, state: str, http_status: int | None = None, error: str | None = None) -> None:
    """收尾：只入队，不做任何 IO。队列满了就丢弃并计数——**绝不阻塞推理**。"""
    if not rec or _CFG is None or not _CFG["enabled"]:
        return
    rec["ended_at"] = rec["ended_at"] or time.time()
    rec["state"] = state
    if http_status is not None:
        rec["http_status"] = http_status
    if error:
        rec["error"] = str(error)[:400]
    try:
        _Q.put_nowait(rec)
    except queue.Full:
        STATE["dropped"] += 1
        if STATE["dropped"] % 50 == 1:
            log(f"入队已满（{_CFG['queue_max']}），丢弃 {STATE['dropped']} 条（写盘跟不上，检查磁盘/对象存储）")


# --------------------------------------------------------------------------- 后台：写盘
_SEG = {"fh": None, "name": None, "date": None, "bytes": 0, "rows": 0, "opened": 0.0}


def _seg_open() -> None:
    now = time.time()
    date = time.strftime("%Y-%m-%d", time.gmtime(now + 8 * 3600))
    d = pathlib.Path(_CFG["dir"]) / date
    d.mkdir(parents=True, exist_ok=True)
    name = f"seg-{time.strftime('%H%M%S', time.gmtime(now + 8 * 3600))}-{uuid.uuid4().hex[:5]}.jsonl.gz"
    _SEG.update(fh=gzip.open(d / name, "wb", compresslevel=6), name=name, date=date,
                bytes=0, rows=0, opened=now)
    with DB_LOCK:
        _conn().execute("INSERT OR REPLACE INTO segments(name,date,created,bytes,rows,closed,uploaded)"
                        " VALUES(?,?,?,?,?,0,0)", (name, date, now, 0, 0))
        _conn().commit()


def _seg_close() -> str | None:
    if _SEG["fh"] is None:
        return None
    try:
        _SEG["fh"].close()
    except OSError as e:
        log("分段收尾失败:", repr(e))
    name, date, nbytes, rows = _SEG["name"], _SEG["date"], _SEG["bytes"], _SEG["rows"]
    _SEG.update(fh=None, name=None, date=None, bytes=0, rows=0)
    with DB_LOCK:
        _conn().execute("UPDATE segments SET closed=1, bytes=?, rows=? WHERE name=?",
                        (nbytes, rows, name))
        _conn().commit()
    STATE["segments"] += 1
    try:
        _UPQ.put_nowait(name)                     # 交给上传线程，写线程立刻能干别的
    except queue.Full:
        pass
    log(f"分段封口 {date}/{name}：{rows} 条 / {nbytes / 2**20:.2f} MiB（未压缩计）")
    return name


def _worker() -> None:
    pending = 0
    last_commit = time.time()
    while True:
        try:
            rec = _Q.get(timeout=0.5)         # 队列长度直接看 _Q.qsize()（status() 里报 queue）
        except queue.Empty:
            rec = None
        if rec is not None:
            try:
                if _SEG["fh"] is None:
                    _seg_open()
                row, obj = _build(rec)
                line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
                raw = line.encode("utf-8")
                _SEG["fh"].write(raw)
                _SEG["bytes"] += len(raw)
                row["seg"], row["line"] = _SEG["name"], _SEG["rows"]
                _SEG["rows"] += 1
                with DB_LOCK:
                    _conn().execute("INSERT OR REPLACE INTO infer(%s) VALUES(%s)"
                                    % (",".join(_COLS), ",".join("?" * len(_COLS))),
                                    tuple(row.get(c) for c in _COLS))
                pending += 1
                STATE["stored"] += 1
                STATE["rows"] += 1
                STATE["last_ts"] = rec["ts"]
                _hot_push(row)
            except Exception as e:
                STATE["last_error"] = f"write: {e!r}"
                log("写入失败:", repr(e))
        now = time.time()
        if pending and (pending >= 200 or now - last_commit > 1.0):
            with DB_LOCK:
                try:
                    _conn().commit()
                except sqlite3.Error as e:
                    log("提交失败:", repr(e))
            pending = 0
            last_commit = now
            # 索引里已经有这条了，内容就**必须**落到盘上 —— gzip 默认一直缓冲到封口（最多 300 秒），
            # 那期间进程一挂，索引会指着一份空文件（实测踩到：分段 0 字节，raw 读不到）。
            # 每秒 sync-flush 一次：崩溃最多损失最近一秒，代价可以忽略。
            if _SEG["fh"] is not None:
                try:
                    _SEG["fh"].flush()
                except (OSError, ValueError) as e:
                    log("分段 flush 失败:", repr(e))
        if _SEG["fh"] is not None and (_SEG["bytes"] >= int(_CFG["seg_max_mb"] * 2**20)
                                       or now - _SEG["opened"] >= int(_CFG["seg_max_s"])):
            _seg_close()


def _hot_push(row: dict) -> None:
    _HOT.append(row)
    while len(_HOT) > int(_CFG["hot_rows"]):
        _HOT.pop(0)


# --------------------------------------------------------------------------- 组装一条记录
def _usage_of(j: dict) -> tuple[dict, dict]:
    """取 usage 与 timings，**统一成 OpenAI 的字段名**（两种体裁上游都可能有）。"""
    if not isinstance(j, dict):
        return {}, {}
    src = j.get("message") if isinstance(j.get("message"), dict) else j
    u = j.get("usage") or src.get("usage") or {}
    if not isinstance(u, dict):
        u = {}
    if "input_tokens" in u or "output_tokens" in u:      # Anthropic 体裁 → 换名
        u = {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens"),
             "prompt_tokens_details": {"cached_tokens": u.get("cache_read_input_tokens")}}
    return u, (j.get("timings") or {})


def _parse_response(raw: bytes, stream: bool) -> dict:
    """把响应体拆成 content / reasoning / tool_calls / usage / timings / finish_reason。

    流式：SSE 逐事件（OpenAI 的 `data:` 与 Anthropic 的 `event:`+`data:` 都认）。
    非流式：整份 JSON（两种体裁都认）。认不出来的原样留着，别丢。
    """
    out = {"content": "", "reasoning": "", "tool_calls": [], "usage": {}, "timings": {},
           "finish_reason": None, "events": 0}
    text = raw.decode("utf-8", "replace")
    # 上游没给 Content-Type（或走到别的分支）时，按内容自己认一下：SSE 一定以 data:/event: 起头
    if not stream and text.lstrip().startswith(("data:", "event:")):
        stream = True
    if not stream:
        try:
            j = json.loads(text)
        except ValueError:
            out["raw"] = text[:200_000]
            return out
        cho = (j.get("choices") or [{}])[0] if isinstance(j, dict) else {}
        msg = cho.get("message") or j.get("message") or {}
        out["content"] = _text_of(msg.get("content")) or _text_of(j.get("content"))
        out["reasoning"] = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
        out["tool_calls"] = _calls_of(msg.get("tool_calls"))
        out["finish_reason"] = cho.get("finish_reason") or j.get("stop_reason")
        out["usage"], out["timings"] = _usage_of(j)
        out["events"] = 1
        return out

    parts, reason, calls, last_usage, last_timings, finish = [], [], [], {}, {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        out["events"] += 1
        try:
            j = json.loads(payload)
        except ValueError:
            continue
        u, tm = _usage_of(j)
        if u:
            last_usage = u
        if tm:
            last_timings = tm
        cho = (j.get("choices") or [None])[0] if isinstance(j, dict) else None
        if cho:
            d = cho.get("delta") or {}
            parts.append(_text_of(d.get("content")))
            if d.get("reasoning_content"):
                reason.append(str(d["reasoning_content"]))
            calls.extend(_calls_of(d.get("tool_calls")))
            if cho.get("finish_reason"):
                finish = cho["finish_reason"]
            continue
        d = j.get("delta") or {}                     # Anthropic 体裁
        if d.get("type") == "text_delta" or d.get("text"):
            parts.append(str(d.get("text") or ""))
        elif d.get("type") == "thinking_delta" or d.get("thinking"):
            reason.append(str(d.get("thinking") or ""))
        elif d.get("type") == "input_json_delta":
            calls.append({"name": None, "arguments": str(d.get("partial_json") or "")})
        finish = d.get("stop_reason") or j.get("stop_reason") or finish
    out["content"] = _join(parts)
    out["reasoning"] = _join(reason)
    out["tool_calls"] = _merge_calls(calls)
    out["usage"], out["timings"] = last_usage, last_timings
    out["finish_reason"] = finish
    return out


def _text_of(x) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, list):                          # Anthropic 的 content:[{type:text,...}]
        return "".join(str(p.get("text") or "") for p in x
                       if isinstance(p, dict) and p.get("type") in ("text", None))
    return str(x)


def _join(parts: list[str]) -> str:
    """流式 delta 是**逐块**到达的（每个 token 一块），拼起来才是正文；空块要丢掉。"""
    return "".join(p for p in parts if p)


def _calls_of(tcs) -> list[dict]:
    out = []
    for t in tcs or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        out.append({"index": t.get("index", 0), "name": fn.get("name") or t.get("name"),
                    "arguments": fn.get("arguments") or t.get("arguments") or ""})
    return out


def _merge_calls(calls: list[dict]) -> list[dict]:
    """流式 tool_call 的参数是分片到达的，按 index 拼回去。"""
    by = {}
    for c in calls:
        cur = by.setdefault(c.get("index") or 0, {"name": None, "arguments": ""})
        cur["name"] = c.get("name") or cur["name"]
        cur["arguments"] += c.get("arguments") or ""
    return [by[i] for i in sorted(by)]


def _build(rec: dict) -> tuple[dict, dict]:
    """热路径的临时记录 → (SQLite 行, 落盘的完整对象)。"""
    u = {}
    resp = _parse_response(bytes(rec["buf"]), bool(rec["stream"]))
    u = resp.get("usage") or {}
    tm = resp.get("timings") or {}
    rag = rec.get("rag") or {}
    chunks = ((rag.get("chosen_detail") or {}).get("chunks") or [])
    scores = [c["score"] for c in chunks if isinstance(c.get("score"), (int, float))]
    details = u.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = tm.get("cache_n")
    prompt = u.get("prompt_tokens") or rec.get("est")
    completion = u.get("completion_tokens")
    if completion is None and rec["stream"]:
        completion = rec["events"]                      # 拿不到 usage 时用事件数估（≈ 1 token/事件）
    first, started, ended = rec["first_chunk_at"], rec["ts"], rec["ended_at"] or time.time()
    ttft_ms = (first - started) * 1000 if first else None
    total_ms = (ended - started) * 1000
    tok_s = tm.get("predicted_per_second")
    if not tok_s and completion and rec["stream"] and first:
        # 只有**流式**才能这样算：首帧之后的时间就是解码时间。
        # 非流式不行 —— 那时 first_chunk_at 就是"整段响应到达"，相减约等于 0，
        # 算出来的速度会是 40 万 tok/s 这种荒唐数（实测踩到）。那种情况宁可留空。
        decode_s = ended - first
        if decode_s > 0:
            tok_s = round(completion / decode_s, 1)
    row = {
        "id": rec["id"], "ts": rec["ts"], "iso": bj(rec["ts"]), "path": rec["path"],
        "model": rec.get("model"), "lane": rec.get("lane"), "lane_label": rec.get("lane_label"),
        "session": rec.get("session"), "stream": 1 if rec["stream"] else (0 if rec["stream"] is not None else None),
        "state": rec.get("state"), "http_status": rec.get("http_status"), "error": rec.get("error"),
        "mode": rec.get("mode"), "prompt_tokens": prompt, "cached_tokens": cached,
        "completion_tokens": completion, "est_prompt_tokens": rec.get("est"),
        "recent_tokens": rec.get("recent_tokens"), "memory_tokens": rec.get("memory_tokens"),
        "block_reused": None if rec.get("block_reused") is None else int(bool(rec["block_reused"])),
        "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
        "total_ms": round(total_ms, 1),
        "decode_tok_s": round(float(tok_s), 1) if tok_s else None,
        "prompt_tok_s": tm.get("prompt_per_second"), "retrieve_ms": rec.get("retrieve_ms"),
        "events": rec["events"], "bytes_out": rec["bytes_out"], "client_bytes": rec["client_bytes"],
        "sent_bytes": rec.get("sent_bytes"), "resp_bytes": len(rec["buf"]),
        "finish_reason": resp.get("finish_reason"), "content_chars": len(resp.get("content") or ""),
        "reasoning_chars": len(resp.get("reasoning") or ""), "tool_calls": len(resp.get("tool_calls") or []),
        "truncated": 1 if rec["trunc"] else 0, "retries": rec.get("retries") or 0,
        "ua": rec.get("ua"), "uploaded": 0,
        # ---- RAG 前后对照
        "rag_query_chars": rag.get("query_chars"), "rag_pool": rag.get("pool"),
        "rag_candidates": rag.get("candidates"), "rag_chunks": len(chunks) or None,
        "rag_score_max": max(scores) if scores else None, "rag_score_min": min(scores) if scores else None,
        "rag_block_chars": rag.get("block_chars"),
        "rag_rerank": None if rag.get("reranked") is None else int(bool(rag["reranked"])),
        "rag_rebuild": None if rec.get("mode") != "assembled" else int(not rec.get("block_reused")),
        "client_msgs": rec.get("client_msgs"), "sent_msgs": rec.get("sent_msgs"),
        "client_chars": rec.get("client_chars"), "sent_chars": rec.get("sent_chars"),
        "differs": rec.get("differs"),
    }
    obj = {
        "id": rec["id"], "ts": rec["ts"], "iso": bj(rec["ts"]), "meta": row,
        "request": {"client": rec.get("client"), "sent": rec.get("sent")},
        "rag": rag or None,          # RAG 触发时的"前后依据"（查询指纹/窗口预算/每块明细）
        "response": {k: v for k, v in resp.items() if k not in ("usage", "timings")},
        "engine": {"usage": u, "timings": tm},
    }
    return row, obj


# --------------------------------------------------------------------------- 后台：上传 + 清理
def _uploader() -> None:
    last_sweep = 0.0
    while True:
        try:
            name = _UPQ.get(timeout=1.0)
        except queue.Empty:
            name = None
        if name:
            _upload(name)
        now = time.time()
        if now - last_sweep > 60:                    # 每分钟兜一次：补传失败的分段 + 清理缓存
            last_sweep = now
            with DB_LOCK:
                rows = _conn().execute("SELECT name FROM segments WHERE uploaded=0 AND closed=1"
                                       " ORDER BY created LIMIT 20").fetchall()
            for r in rows:
                _upload(r["name"], quiet=True)
            _prune()


def _upload(name: str, quiet: bool = False) -> bool:
    with DB_LOCK:
        row = _conn().execute("SELECT date, bytes, rows, uploaded FROM segments WHERE name=?",
                              (name,)).fetchone()
    if not row or row["uploaded"]:
        return True
    path = pathlib.Path(_CFG["dir"]) / row["date"] / name
    if not path.exists():
        STATE["upload_fail"] += 1
        STATE["last_error"] = f"分段文件不见了: {path}"
        return False
    cli, bucket = minio_client()
    if cli is None:
        STATE["last_error"] = _MINIO_CACHE.get("err") or "minio 不可用"
        return False
    key = f"{_CFG['minio_prefix']}/{row['date']}/{name}"
    try:
        cli.fput_object(bucket, key, str(path), content_type="application/gzip")
    except Exception as e:
        STATE["upload_fail"] += 1
        STATE["last_error"] = f"upload: {e!r}"
        if not quiet:
            log(f"上传失败（数据仍在 {path}，后台会重试）:", repr(e))
        return False
    with DB_LOCK:
        _conn().execute("UPDATE segments SET uploaded=1 WHERE name=?", (name,))
        _conn().execute("UPDATE infer SET uploaded=1 WHERE seg=?", (name,))
        _conn().commit()
    STATE["uploaded"] += 1
    if not quiet:
        log(f"已归档 {key}（{row['bytes'] / 2**20:.2f} MiB, {row['rows']} 条）")
    return True


def _prune() -> None:
    """清理**本机**分段缓存：只删已归档的，保留期内不删。对象存储那份是主副本，不动。"""
    limit = int(_CFG["local_cache_gb"] * 2**30)
    keep_days = int(_CFG["local_cache_days"])
    with DB_LOCK:
        rows = _conn().execute("SELECT name,date,created,bytes FROM segments WHERE uploaded=1"
                               " ORDER BY created DESC").fetchall()
    total, old = 0, []
    for r in rows:
        total += r["bytes"] or 0
        if time.time() - (r["created"] or 0) > keep_days * 86400:
            old.append(r)
    drop = list(old)
    seen = {r["name"] for r in drop}                 # 用名字比，别拿 sqlite3.Row 比相等（语义不直观）
    for r in reversed(rows):                         # 从最老的开始删，直到总量进了限额
        if total <= limit:
            break
        if r["name"] in seen:
            continue
        drop.append(r)
        seen.add(r["name"])
        total -= r["bytes"] or 0
    for r in drop:
        p = pathlib.Path(_CFG["dir"]) / r["date"] / r["name"]
        try:
            if p.exists():
                p.unlink()
            STATE["pruned"] += 1
        except OSError as e:
            log("清理失败:", repr(e))
    if drop:
        log(f"本机缓存清理 {len(drop)} 个已归档分段（阈值 {_CFG['local_cache_gb']} GiB / {keep_days} 天）")


# --------------------------------------------------------------------------- 读（HTTP 线程用）
def _where(f: dict) -> tuple[str, list]:
    sql, args = [], []
    if f.get("model"):
        sql.append("model = ?"); args.append(f["model"])
    if f.get("state"):
        sql.append("state = ?"); args.append(f["state"])
    if f.get("lane"):
        sql.append("lane = ?"); args.append(f["lane"])
    if f.get("mode"):
        sql.append("mode = ?"); args.append(f["mode"])
    if f.get("stream") is not None:
        sql.append("stream = ?"); args.append(int(f["stream"]))
    if f.get("since"):
        sql.append("ts >= ?"); args.append(float(f["since"]))
    if f.get("until"):
        sql.append("ts <= ?"); args.append(float(f["until"]))
    if f.get("min_ms"):
        sql.append("total_ms >= ?"); args.append(float(f["min_ms"]))
    if f.get("q"):
        sql.append("(id LIKE ? OR model LIKE ? OR session LIKE ?)");
        args += [f"%{f['q']}%"] * 3
    return (" WHERE " + " AND ".join(sql) if sql else ""), args


def query(**f) -> dict:
    limit = max(1, min(int(f.get("limit") or 50), 500))
    offset = max(0, int(f.get("offset") or 0))
    w, args = _where(f)
    with DB_LOCK:
        total = _conn().execute("SELECT COUNT(*) c FROM infer" + w, args).fetchone()["c"]
        rows = _conn().execute(
            "SELECT * FROM infer" + w + " ORDER BY ts DESC LIMIT ? OFFSET ?", args + [limit, offset]
        ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset,
            "source": "sqlite"}


def hot() -> list[dict]:
    """L1：内存里最近若干条（页面「实时」那一段，零 IO）。"""
    return list(_HOT)


def stats(days: int = 7) -> dict:
    since = time.time() - max(1, days) * 86400
    with DB_LOCK:
        c = _conn()
        tot = dict(c.execute("SELECT COUNT(*) n, SUM(completion_tokens) out_tok,"
                             " SUM(prompt_tokens) in_tok, SUM(cached_tokens) cached,"
                             " AVG(decode_tok_s) avg_tok_s, AVG(ttft_ms) avg_ttft,"
                             " SUM(total_ms)/1000.0 busy_s FROM infer WHERE ts >= ?", (since,)).fetchone())
        today0 = time.time() - (time.time() % 86400)
        today = dict(c.execute("SELECT COUNT(*) n, SUM(completion_tokens) out_tok FROM infer"
                               " WHERE ts >= ?", (today0,)).fetchone())
        bymodel = [dict(r) for r in c.execute(
            "SELECT model, COUNT(*) n, AVG(decode_tok_s) tok_s, SUM(completion_tokens) out_tok"
            " FROM infer WHERE ts >= ? GROUP BY model ORDER BY n DESC", (since,)).fetchall()]
        bystate = [dict(r) for r in c.execute(
            "SELECT state, COUNT(*) n FROM infer WHERE ts >= ? GROUP BY state ORDER BY n DESC",
            (since,)).fetchall()]
        hourly = [dict(r) for r in c.execute(
            "SELECT CAST(ts/3600 AS INT) h, COUNT(*) n, AVG(decode_tok_s) tok_s FROM infer"
            " WHERE ts >= ? GROUP BY h ORDER BY h", (since,)).fetchall()]
    return {"days": days, "total": tot["n"] or 0, "today": today["n"] or 0,
            "today_out_tokens": today["out_tok"] or 0,
            "out_tokens": tot["out_tok"] or 0, "in_tokens": tot["in_tok"] or 0,
            "cached_tokens": tot["cached"] or 0, "avg_tok_s": tot["avg_tok_s"],
            "avg_ttft_ms": tot["avg_ttft"], "busy_s": tot["busy_s"] or 0,
            "by_model": bymodel, "by_state": bystate, "hourly": hourly[-72:],
            "store": status()}


def detail(rid: str) -> dict | None:
    with DB_LOCK:
        r = _conn().execute("SELECT * FROM infer WHERE id = ?", (rid,)).fetchone()
    return dict(r) if r else None


def raw(rid: str) -> dict | None:
    """完整记录（含内容）。本机分段没有就去对象存储取回并缓存——界面上不展示，仅供错误分析。"""
    d = detail(rid)
    if not d or not d.get("seg"):
        return None
    date, name, line = (d.get("iso") or "")[:10].replace("-", "-"), d["seg"], int(d.get("line") or 0)
    date = d["iso"][:10] if d.get("iso") else ""
    local = pathlib.Path(_CFG["dir"]) / date / name
    if not local.exists():
        cli, bucket = minio_client()
        if cli is None:
            return {"_error": "本机没有该分段，对象存储也不可用", "meta": d}
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            cli.fget_object(bucket, f"{_CFG['minio_prefix']}/{date}/{name}", str(local))
        except Exception as e:
            return {"_error": f"取回分段失败: {e!r}", "meta": d}
    try:
        with gzip.open(local, "rt", encoding="utf-8") as fh:
            i = -1
            try:
                for i, ln in enumerate(fh):
                    if i == line:
                        return json.loads(ln)
            except EOFError:
                pass          # 分段还在写（sync-flush 过、但没封口）：读到哪算哪，不是错误
    except (OSError, ValueError) as e:
        return {"_error": f"读分段失败: {e!r}", "meta": d}
    return {"_error": f"分段里没有这一行（读到第 {i + 1} 行；可能还在写或被清理过）", "meta": d}


def rag_meta(rid: str) -> dict | None:
    """只取这一条的 **RAG 元信息**（查询指纹、窗口预算、每块明细/分数）—— 不含任何正文。

    页面要画"检索回来的都是哪几段历史"，而那些信息存在完整记录里（分段/对象存储）。
    这里从完整记录里**只挑 rag 这一段**返回：正文（原输入、检索块、回复）一律不出这个函数的门。"""
    full = raw(rid)
    if not full:
        return None
    if full.get("_error") and not full.get("rag"):
        return {"id": rid, "_error": full["_error"]}
    d = detail(rid) or {}
    return {"id": rid, "ts": d.get("ts"), "iso": d.get("iso"), "mode": d.get("mode"),
            "rag": full.get("rag"), "meta": {k: d.get(k) for k in (
                "client_msgs", "sent_msgs", "client_chars", "sent_chars", "differs",
                "recent_tokens", "memory_tokens", "block_reused", "retrieve_ms",
                "rag_pool", "rag_candidates", "rag_chunks", "rag_score_min", "rag_score_max",
                "rag_block_chars", "rag_rerank", "rag_rebuild", "rag_query_chars")}}


def status() -> dict:
    cli, bucket = minio_client()
    with DB_LOCK:
        try:
            segs = dict(_conn().execute("SELECT COUNT(*) n, SUM(uploaded) up, SUM(bytes) bytes"
                                        " FROM segments").fetchone())
            pending = _conn().execute("SELECT COUNT(*) c FROM segments WHERE uploaded=0 AND closed=1"
                                      ).fetchone()["c"]
        except sqlite3.Error:
            segs, pending = {}, 0
    s = dict(STATE)
    s.pop("queued", None)                          # 老版本留下的计数（曾经是减出来的，会变负数）
    s.update({"enabled": bool(_CFG and _CFG["enabled"]), "queue": _Q.qsize(),
              "hot": len(_HOT), "segments_total": segs.get("n") or 0,
              "segments_uploaded": segs.get("up") or 0, "pending_upload": pending,
              "minio_ok": cli is not None, "bucket": bucket,
              "dir": (_CFG or {}).get("dir"), "db": (_CFG or {}).get("db")})
    return s
