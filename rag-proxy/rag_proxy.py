#!/usr/bin/env python3
"""Strata RAG 入口代理（P0）
================================================================================
放在 Strata 服务之前（客户端 → :8080 → 引擎回环 :8090），当对话超过模型窗口时，把"更早的历史"换成
"检索出来的片段"，让上下文始终落在原生 262144 以内；短对话原样透传（零改动）。

设计要点（对应 LONG-CONTEXT-RAG-PLAN.md §1）：
  * 零侵入：只改写 messages，其余请求/响应（含 SSE 流式、工具调用、图片）原样透传；
  * **结构优先的分块**（2026-10-08 重写）：先把 messages 解析成"回合"（用户输入 → agent 工具调用
    → 工具返回 → agent 回答），块只在回合内部切、**绝不跨回合**；回合内相邻的小单元合并成一块，
    只有单条消息本身超预算才按 行→段→句 的自然边界再切（重叠也只带整行/整句）。按长度硬切会把
    工具返回和产生它的工具调用劈开、把回合从中间截断，检索带回的就是无出处的碎片——那正是
    1M 精度报告 §2.6 "张冠李戴"的根因之一。块边界只由本条消息自己的结构决定，邻居长度变化不再
    让边界漂移，所以逐轮追加历史时旧块哈希恒定、嵌入向量直接复用；
  * 增量嵌入：内容按哈希去重，后台异步嵌入（bge-m3，内网服务），请求路径永不等嵌入；
  * 检索：向量召回 top-K → bge-reranker-v2-m3 重排 → 分数 = 重排 + 时近衰减 + 结构权重
    → MMR 去冗余 → **结构补全**（选中的块带上同消息其余分段与回合锚点）→ 按 score/token 贪心填满预算；
  * 组装：**系统提示词逐字保留在最前**（Anthropic 的 body["system"] 与 OpenAI 的 role=system 消息
    都顾到）+ 检索块（更早历史，按原始次序渲染并带回合结构标签）+ **最近逐字窗口（回合对齐，
    不会停在回合中间）**；**检索块在 TTL 内保持稳定**（前缀缓存友好），只在 TTL 到期 / 窗口将满
    （compaction）时重建；
  * 缓存：嵌入（内容哈希，永久）/ 重排（查询+候选哈希，TTL）/ 组装（会话级，TTL）——全部在内存；
  * 降级：嵌入/重排不可用时退化为"只保留最近窗口"，绝不因为 RAG 失败而让请求失败。

运行：  ~/Strata/.venv/bin/python ~/rag-proxy/rag_proxy.py [--config config.json]
测试：  ~/Strata/.venv/bin/python ~/rag-proxy/test_rag_proxy.py
运行记录/交接：见同目录 README.md
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import hmac
import html
import json
import math
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import wave
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ssecho_auth                      # 集市 OAuth 登录 + 会话 Cookie（同目录，见 ssecho_auth.py）
import infer_store                      # 推理记录：元信息 + 全部内容留档（对象存储为主，本机做缓存）
import mcp_client                       # 远程 MCP 客户端（JSON-RPC over HTTP）
import doc_extract                      # 文档自动识别/抽取：PDF/DOCX/XLSX/XLS/CSV/文本 -> 可读文本或页面图

import numpy as np

# ----------------------------------------------------------------------------- config

DEFAULTS = {
    "listen_host": "0.0.0.0",
    "listen_port": 8081,
    "upstream": "http://127.0.0.1:8080",
    # 人脸服务（独立进程，只监听回环；公网只从 /face/* 进来）
    "face_upstream": "http://127.0.0.1:8095",
    "face_max_body_mb": 64,
    "api_key_file": "/home/cloud/logs/strata-api-key.txt",
    # 集群 DNS 是稳定入口；Pod IP 会随重建漂移（10.42.0.166/.138 曾是最新实测地址）。
    "embed_url": "http://group-embeding.clouds.svc.cluster.local:8080/v1/embeddings",
    "embed_model": "bge-m3",
    "rerank_url": "http://group-reranker.clouds.svc.cluster.local:8080/v1/rerank",
    # 窗口与预算（token）
    "rag_enabled": True,                    # false = 关闭检索组装，纯透传；有效上下文回到引擎原生 256K
    # 文档预处理（与 RAG 开关无关，RAG 关闭时也生效）：上游直接发 PDF/DOCX/XLSX 时在网关抽成文本，
    # 扫描件渲染成页面图，并按剩余上下文预算截断——引擎从不截断，超窗即 400。
    "documents": {
        "enabled": True,
        "allow_urls": False,                 # 公网网关默认不替客户端抓 URL（防 SSRF）；用 base64/file_data
        "max_file_mb": 20,
        "max_tokens": 120000,               # 单个请求里文档文本的总预算上限
        "reserve_tokens": 4096,             # 给对话/系统提示留的安全余量
        "truncate_head_ratio": 0.7,         # 超预算时保留头部 70% + 尾部 30%
        "min_text_chars": 200,              # 多页 PDF 文字层过少的辅助判据（单页短文本不会误判为扫描件）
        "pdf_render_scanned": True,
        "max_pages": 8,                     # 扫描件最多渲染页数（每页约 1024 image token）
        "page_tokens": 1024,
        "pdf_dpi": 150,
        "max_image_side": 2048,
        "max_xlsx_rows": 500,
        "max_xlsx_cols": 30,
    },
    "max_context": 262144,
    "ctx_slack": 8,
    "gen_reserve_default": 8192,
    "gen_reserve_max": 32768,
    "recent_window_tokens": 65536,          # 最近逐字保留的窗口
    "memory_min_tokens": 4096,              # 低于此值不检索（没必要）
    # 分块（结构优先：先解析回合，块不跨回合；只有单条消息超预算才按 行→段→句 再切）
    "chunk_tokens": 768,
    "chunk_overlap_tokens": 128,
    "structure_chunking": True,             # false = 退回旧的按长度硬切（A/B 对照用）
    "context_expand_ratio": 0.25,           # 检索块预算里留给"结构补全"（同消息分段/回合锚点）的比例
    # 组装块复用判据（2026-10-08）：TTL 内复用是前缀缓存友好的关键，但只看时间会让提问阶段拿到
    # 灌入阶段为别的查询建好的旧块。判据 = **这个块对当前问题还有没有用**，用重排器判定（见 README §5e：
    # 嵌入余弦分不开"句式相同的事实块"，实测 0.70 vs 0.48；重排器 0.80 vs 0.0004）。
    "block_drift_rebuild": True,
    "block_relevance_min": 0.30,            # 块内最好的块对当前问题的重排分低于此 → 块没覆盖 → 重建
    "block_check_docs": 128,                # 判据只重排块内优先级最高的这么多块
    "block_check_chars": 600,               # 每块只取前这么多字符（判相关性够用，且便宜）
    "chars_per_token": 2.5,                 # 估算用（精确计数走 /v1/messages/count_tokens）
    # 窗口守卫（2026-10-08）：引擎对 prompt + max_tokens + 8 > 窗口 **直接 400、从不截断**，所以
    # "估算"必须是**上界**（宁可高估），计数残差要留余量，图片要按引擎的展开方式数。
    # 每类字符的 tok/char 上界（对真引擎 count_tokens 标定，实测值见 README §5f）：
    "tok_per_char_cjk": 1.05,               # 实测最坏 1.00（单字重复/生僻字）；中文散文 0.51
    "tok_per_char_digit": 1.05,             # 实测 1.00（数字串）
    "tok_per_char_other": 0.70,             # 实测最坏 0.63（中英混排）；随机词 0.47、代码 0.39、英文 0.19
    "msg_overhead_tokens": 8,               # 实测：模板固定 ~45 + 每消息 ~6.7
    "image_tokens": 1024,                   # 引擎把一张图展开成 ~230–800 token，而 count_tokens 只数 1 个
    "clamp_margin_tokens": 512,             # 计数与引擎实际渲染的残差（固定部分）
    "clamp_margin_div": 64,                 # 再加 prompt/64（~1.6%）的比例残差
    "retry_on_overfit": True,               # 引擎仍报 400 超窗时，照引擎报的余量压 max_tokens 重试一次
    # 思考预算守卫（2026-10-08）：客户端没表达过思考偏好时，给"答复"留一份预留量，把思考上限压到
    # max_tokens - 预留。引擎在思考到达预算时会自己收尾并接着回答（serve/server.py 的 REASONING_WRAP_UP），
    # 不补的话思考会一路吃到上限、答复一个 token 都不剩——实测 strata-a 173 次 + strata-b 81 次。
    "thinking_budget_guard": True,          # false = 不碰任何请求（回到改动前行为）
    "answer_reserve_tokens": 4096,          # 保证"答复"至少有这么多 token 的空间
    "answer_reserve_div": 3,                # 但也只取 max_tokens 的 1/3（小上限的请求同样要留得出）
    # 检索
    "retrieve_candidates": 200,
    "rerank_candidates": 50,
    "mmr_lambda": 0.7,
    "score_w_rerank": 0.55,
    "score_w_recency": 0.30,
    "score_w_role": 0.15,
    # 缓存（秒）
    "assembly_ttl_s": 10800,                # 组装结果 3h
    "rerank_cache_ttl_s": 10800,
    "session_ttl_s": 86400,                 # 会话 24h
    "embed_batch": 16,
    "embed_timeout_s": 30,
    "rerank_timeout_s": 30,
    "count_timeout_s": 10,
    # 客户端连接超时（秒）：keep-alive 空闲/慢客户端读写超过它就掐掉该连接。没有它线程会永远阻塞在
    # readline()/sendall 上——2026-11-01 实测积到 334 条 ESTABLISHED、411 线程、FD 695/1024。0=关闭。
    "client_timeout_s": 180,
    "max_sessions": 64,
    "debug": False,
    # 并发监控（GET /concurrency，见本文件"并发监控"段）：通道显示名与状态轮询间隔
    "lane_poll_s": 2,
    "lane_labels": {},
    # 每个用户的 LLM Lab 模板：首次访问工作空间时拷一份到 <uid>/llm-study-lab。
    # 模板本身只读使用，用户改的是自己的副本；重置会重新拷一份。
    "lab": {
        "template": "/home/cloud/llm-study-lab",
        "dirname": "llm-study-lab",
        "python": "/home/cloud/llm-lab/.venv/bin/python",
        "gpu": "2",
        "enabled": True,
    },
    # 胡桃音色：Voice Studio 的专用 worker（9882）已经常驻加载 run 122 的权重。
    # 本 Pod 通过 SSH 隧道把 127.0.0.1:9882 映射到那台 pod（supervisor 的 tts-tunnel）。
    "tts": {
        "base_url": "http://group-gpt-sovits.clouds.svc.cluster.local:8080",
        "local_url": "http://127.0.0.1:9882",
        "cache_dir": "/home/cloud/logs/tts-cache",
        "max_chars": 1200,
        "voice": {
            "id": "hutao",
            "name": "胡桃",
            "ref_audio": "/home/cloud/voice-studio/data/runs/122/ref/reference.wav",
            "prompt_text": "明白了吗布补炉",
            "prompt_lang": "zh",
            "text_lang": "zh",
        },
    },
}


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if path and Path(path).exists():
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg


CFG: dict = {}
API_KEY = ""
OAUTH: "ssecho_auth.OAuth | None" = None      # 集市登录（main 里按配置初始化）
START_TS = time.time()

# ----------------------------------------------------------------------------- 统计与日志

COUNTERS = {
    "requests": 0, "passthrough": 0, "assembled": 0, "degraded": 0, "no_rag": 0,
    "embeds_queued": 0, "embeds_done": 0, "embed_errors": 0,
    "rerank_calls": 0, "rerank_cache_hits": 0,
    "assembly_reused": 0, "assembly_rebuilt": 0, "block_rebuilds": 0,
    "upstream_errors": 0, "upstream_failover": 0, "overfit_retries": 0, "overfit_failures": 0,
    "window_guard_rejects": 0,
    "documents_parsed": 0, "documents_chars": 0, "documents_truncated": 0,
    "documents_images": 0, "documents_errors": 0,
}


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def dbg(*a):
    if CFG.get("debug"):
        log(*a)


# ----------------------------------------------------------------------------- 工具

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()


def est_tokens(text: str) -> int:
    return max(1, int(len(text) / CFG["chars_per_token"]))


def upstreams() -> list[str]:
    """配置里的上游列表：`"upstreams": [...]` 用于"每卡一个引擎实例"的布局；没给就单个 `upstream`。"""
    us = CFG.get("upstreams") or [CFG["upstream"]]
    return [str(u).rstrip("/") for u in us]


_STICKY: dict[str, tuple[str, float]] = {}     # 会话键 → (定的道, 最后使用时间)；S1a 最少负载路由的粘性账本
_STICKY_LOCK = threading.Lock()
_STICKY_MAX = 4096


def _lane_load(u: str) -> float:
    """道上的负载估计（LANE_CACHE 的 live，2 秒一拉）：queued + 正在生成 1；数据过期/缺失 → 保守值 99。"""
    with LANE_LOCK:
        c = LANE_CACHE.get(u)
    if not c or (time.time() - c.get("ts", 0)) > 15:
        return 99.0
    st = c.get("status") or {}
    live = st.get("live") or {}
    if live.get("state") in ("generating", "reading", "busy"):
        return float(live.get("queued") or 0) + 1.0
    return float(live.get("queued") or 0)


def pick_upstream(key: str) -> str:
    """会话键粘性选实例：同一会话永远落同一台（前缀缓存/停车快照在那台，换实例=整段重读）。

    [SSE 2026-10-27 S1a] 首次见到的会话不再 sha1 撞硬币（50% 概率两会话挤同一条道互相拖慢），
    而是在活着的路里挑 live 负载最小的道，结果记进粘性表；表过期/重启后按原哈希兜底（行为向后兼容）。
    配置 `"least_load_routing": false` 一键回纯哈希粘性。"""
    us = upstreams()
    if len(us) == 1:
        return us[0]
    if not CFG.get("least_load_routing", True):
        return us[int(sha1(key), 16) % len(us)]
    now = time.time()
    ttl = float(CFG.get("sticky_ttl_s") or 1800)
    with _STICKY_LOCK:
        hit = _STICKY.get(key)
        if hit and (now - hit[1]) < ttl and hit[0] in us and upstream_ok(hit[0]):  # [SSE 10-31] 粘性道在重载则重选
            _STICKY[key] = (hit[0], now)
            return hit[0]
        chosen = min([u for u in us if upstream_ok(u)] or us, key=lambda u: (_lane_load(u), int(sha1(u), 16)))
        chosen = chosen if upstream_ok(chosen) else next((u for u in us if upstream_ok(u)), chosen)
        if len(_STICKY) >= _STICKY_MAX:   # 简单防膨胀：过期件先清，再不行清最旧一半
            for k in [k for k, (_, ts) in _STICKY.items() if now - ts > ttl]:
                _STICKY.pop(k, None)
            if len(_STICKY) >= _STICKY_MAX:
                for k, _ in sorted(_STICKY.items(), key=lambda kv: kv[1][1])[_STICKY_MAX // 2:]:
                    _STICKY.pop(k, None)
        _STICKY[key] = (chosen, now)
        return chosen


def lane_slots(u: str):
    """(serving, requested) —— 引擎自报的批槽占用；拿不到返回 None。

    [SSE 2026-11-03] `concurrency` 在 serve 侧是**容量**（serving=requested=batch，见 server.py 的 v1_status：
    "the real slot count, so clients can size their rate limits"），不是实时占用 —— 直接拿它比大小会让
    `lane_has_room()` 永远为假（4<4），「优先慢道、慢了再溢出快道」这条策略从未真的生效过。
    实时占用看 `activity.in_flight`（serve 2026-11-03 起还修好了并发时的计数）。"""
    with LANE_LOCK:
        c = LANE_CACHE.get(u)
    st = (c or {}).get("status") or {}
    cc = st.get("concurrency") or {}
    act = st.get("activity") or {}
    serving, requested = cc.get("serving"), cc.get("requested")
    live = act.get("in_flight")
    if isinstance(serving, int) and isinstance(requested, int) and requested > 0:
        return (live if isinstance(live, int) else serving), requested
    return None


def lane_has_room(u: str) -> bool:
    """这条道还有没有空批槽。拿不到数就当有 —— 宁可发过去排队，也不要凭空拒掉。"""
    s = lane_slots(u)
    return True if not s else s[0] < s[1]


# ----------------------------------------------------------------------------- 双道调度
# [SSE 2026-11-14] 按用户要求做的调度层（config.json 的 `lane_scheduler`）：
#   快道请求 → 实时人少的那条道（持平留在快道，保住它的产品语义）；
#   普道请求 → 先普道（8092），普道批槽满了再看快道（8091）是否"够闲"；
#   高峰期（从 infer.db 近 14 天统计：09–12 点、23–01 点、06–07 点快道并发最高）
#   在快道上预留 N 个槽只给快道用，普道溢出最多用其余槽；
#   两条道都空时用"慢道有人、快道空闲就分到快道"的规则把两张卡跑起来。
# 拿不到实时负载（LANE_CACHE 过期）时一律按"有余量"处理，宁可排队也不凭空拒绝。

def _sched_cfg() -> dict:
    c = CFG.get("lane_scheduler")
    return c if isinstance(c, dict) else {}


def _sched_lanes() -> tuple[str, str]:
    """(快道 URL, 普道 URL)；没配就按端口 8091/8092 认。"""
    sc = _sched_cfg()
    us = upstreams()
    fast = str(sc.get("fast_lane") or "").rstrip("/") or next((u for u in us if u.endswith(":8091")), "")
    slow = str(sc.get("slow_lane") or "").rstrip("/") or next((u for u in us if u.endswith(":8092")), "")
    return fast, slow


def _lane_state(u: str) -> tuple[int, int]:
    """(实时在途请求数, 批槽容量)。取不到就 (0, 1) —— 视为完全空闲，不拦请求。"""
    s = lane_slots(u)
    if not s:
        return 0, 1
    load, cap = s
    try:
        load = max(0, int(load))
    except (TypeError, ValueError):
        load = 0
    try:
        cap = max(1, int(cap))
    except (TypeError, ValueError):
        cap = 1
    return load, cap


def _reserve_slots_now() -> int:
    """当前时段快道预留的槽数（北京时间小时）。

    两种写法：`reserve_by_hour`（{"9": 3, ...}，按小时给不同预留）优先；否则用 `reserve_slots`
    + `reserve_hours`（列表里的小时统一预留 N）。预留只在普道溢出/空闲分摊时生效，快道自己的请求不受限。"""
    sc = _sched_cfg()
    hour = (time.gmtime().tm_hour + 8) % 24         # Pod 是 UTC，配置按北京时间写
    by_hour = sc.get("reserve_by_hour")
    if isinstance(by_hour, dict):
        for k, v in by_hour.items():
            try:
                if int(k) % 24 == hour:
                    return max(0, int(v))
            except (TypeError, ValueError):
                continue
        return 0
    try:
        n = max(0, int(sc.get("reserve_slots") or 0))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0
    hours = sc.get("reserve_hours")
    if isinstance(hours, list) and hours:
        if hour not in [int(h) for h in hours]:
            return 0
    return n


def _is_fast_model(model: str | None) -> bool:
    """这个模型 ID 是不是"快道请求"：`fast_models` 显式列出，缺省按 model_routes 里指向快道的推断。"""
    m = (model or "").strip()
    if not m:
        return False
    sc = _sched_cfg()
    fm = sc.get("fast_models")
    if isinstance(fm, list):
        return m in {str(x) for x in fm}
    routes = CFG.get("model_routes") or {}
    fast, _ = _sched_lanes()
    return bool(fast) and str(routes.get(m, "")).rstrip("/") == fast


def schedule_lane(model: str | None, key: str) -> str | None:
    """按调度策略选一条 strata 道；返回 None 表示"不接管"（单道、未启用、或两条道都不可用）。"""
    sc = _sched_cfg()
    if not sc.get("enabled"):
        return None
    fast, slow = _sched_lanes()
    us = upstreams()
    if not fast or not slow or fast == slow or fast not in us or slow not in us:
        return None
    alive = [u for u in (fast, slow) if upstream_alive(u)]
    if len(alive) == 1:
        return alive[0]
    if not alive:
        return None
    la, ca = _lane_state(fast)
    lb, cb = _lane_state(slow)
    reserve = _reserve_slots_now()
    if _is_fast_model(model):
        # 快道请求：去人少的那条；持平留在快道（前缀缓存/产品语义都在那边更合适）
        picked = fast if la <= lb else slow
        log(f"schedule: fast model={model} fast_load={la}/{ca} slow_load={lb}/{cb} -> {lane_label(picked)}")
        return picked
    # 普道请求：两条都空时把两张卡跑起来 —— 普道有人、快道完全空闲就分到快道
    if sc.get("spread_when_idle", True) and la == 0 and lb >= 1 and la < max(0, ca - reserve):
        log(f"schedule: normal spread fast_load=0 slow_load={lb}/{cb} -> {lane_label(fast)}")
        return fast
    if lb < cb:
        return slow
    if la < max(0, ca - reserve):
        log(f"schedule: normal spill fast_load={la}/{ca} slow {lb}>={cb} reserve={reserve} -> {lane_label(fast)}")
        return fast
    return slow


def preferred_upstream(key: str) -> str:
    """未指定模型时的默认落点：**先慢道（8092 普道），慢了才用快道（8091）**。
    慢道是主力（长上下文、真实流量），快道是溢出与实验道 —— 反过来的话快道会被常规流量占满。
    `lane_scheduler.enabled` 打开时交给 schedule_lane（含预留槽与空闲分摊）。"""
    picked = schedule_lane(None, key)
    if picked:
        return picked
    us = upstreams()
    if len(us) <= 1:
        return us[0]
    slow = next((u for u in us if u.rstrip("/").endswith(":8092")), None)
    fast = next((u for u in us if u.rstrip("/").endswith(":8091")), None)
    for u in (slow, fast):
        if u and upstream_alive(u) and lane_has_room(u):
            return u
    return pick_upstream(key)




def stream_xiaoqiu_reasoning(handler, r, rec=None):
    """把 xiaoqiu 的 SSE 流按  拆成 reasoning_content / content 两种 delta。"""
    END = "</" + "think>"
    handler.send_response(r.status)
    for k, v in r.headers.items():
        if k.lower() in ("transfer-encoding", "connection", "content-length"):
            continue
        handler.send_header(k, v)
    handler.send_header("Connection", "close")
    handler.end_headers()

    def emit(obj):
        handler.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")
        handler.wfile.flush()

    buf = b""
    in_think = True
    while True:
        chunk = r.read1(4096)
        if not chunk:
            break
        if rec:
            try: infer_store.capture(rec, chunk)
            except Exception: pass
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            lines = block.split(b"\n")
            data = b"\n".join(l[5:].strip() for l in lines if l.startswith(b"data:"))
            if not data:
                handler.wfile.write(block + b"\n\n"); handler.wfile.flush(); continue
            if data.strip() == b"[DONE]":
                handler.wfile.write(b"data: [DONE]\n\n"); handler.wfile.flush(); continue
            try:
                obj = json.loads(data)
            except Exception:
                handler.wfile.write(block + b"\n\n"); handler.wfile.flush(); continue
            choices = obj.get("choices") or []
            extra = None
            for ch in choices:
                d = ch.get("delta") or {}
                c = d.get("content")
                if not (isinstance(c, str) and c and in_think):
                    continue
                if END in c:
                    before, after = c.split(END, 1)
                    d.pop("content", None)
                    if before:
                        d["reasoning_content"] = before
                    if after:
                        import copy
                        obj2 = copy.deepcopy(obj)
                        d2 = obj2["choices"][0]["delta"]
                        d2.pop("reasoning_content", None)
                        d2["content"] = after
                        extra = obj2
                    in_think = False
                else:
                    d["reasoning_content"] = c
                    d.pop("content", None)
            emit(obj)
            if extra is not None:
                emit(extra)
    if buf:
        handler.wfile.write(buf); handler.wfile.flush()


def split_reasoning_content(obj):
    """把 xiaoqiu 输出里的 思考... 拆到 reasoning_content。"""
    if not isinstance(obj, dict):
        return obj
    for ch in obj.get("choices") or []:
        m = ch.get("message") if isinstance(ch, dict) else None
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        END = "</" + "think>"
        START = "<" + "think>"
        if isinstance(c, str) and END in c:
            reason, ans = c.split(END, 1)
            reason = reason.replace(START, "").strip()
            m["reasoning_content"] = reason
            m["content"] = ans.strip()
    return obj


def route_upstream(model: str | None, key: str) -> str:
    """双通道路由：模型 ID 优先（`strata-a`=快道 8091、`strata-b`=普道 8092），未登记的模型走默认
    （普道），没配默认才退回会话粘性。中转处按模型 ID 限流，网关只认 ID 不做并发限制。"""
    routes = CFG.get("model_routes") or {}
    m = (model or "").strip()
    if m in routes:                      # 显式登记的模型：strata 双道交给调度器，其它保持钉道
        u = str(routes[m]).rstrip("/")
        # [SSE 2026-11-05] 非 Strata 的本地模型（如 vLLM 的 xiaoqiu:8093）没有 /v1/status，
        # 不参与 strata 的存活探测/粘性，直接原样转发。
        if u not in upstreams():
            return u
        # [SSE 2026-11-14] 产品的快/普道模型（qwen3.8-flash-fast / qwen3.8-flash 等）交给调度器：
        # 按实时负载、预留槽与空闲分摊重选。`pinned_models`（strata-a/strata-b 这类显式指定车道的
        # 客户端）保持死钉 —— 那是调用方明确要求，不是产品语义。单道或调度关闭时保持原行为。
        pinned = {str(x) for x in (_sched_cfg().get("pinned_models") or [])}
        if u in _sched_lanes() and m not in pinned:
            picked = schedule_lane(m, key)
            if picked:
                return picked
        # [SSE 2026-11-02] 显式选择**必须**被尊重：引擎空闲时会自动卸载（/v1/status 的
        # loaded=false、auto_load=true），lane_ready 把这种正常空闲误判成「正在重载」，
        # 于是把钉在快道的请求改投到当时正忙的普道 —— 实测快道空转、普道被压满。
        # 只有这条道**真的连不上**才改投兄弟道。
        if not upstream_alive(u):
            alt = next((x for x in upstreams() if x != u and upstream_alive(x)), None)
            if alt:
                log(f"route: {m} 钉的道 {u} 不可达，改投 {alt}")
                return alt
        return u
    # [SSE 2026-11-02] 未指定模型：**优先慢道，慢道批槽满了才去快道**（产品语义见 preferred_upstream）。
    # 原来是「最少负载 + 会话粘性」，两条道会来回跳，慢道空闲时也把新会话甩到快道。
    return preferred_upstream(key)


def lane_ready(u: str) -> bool:
    """[SSE 2026-10-31] 引擎就绪门：读 LANE_CACHE 里 /v1/status.loaded。
    数据缺失/过期/非 dict 一律视为就绪——本门只管一种事故：引擎在重载时 serve **照常监听并
    hold 请求到 loaded**（换二进制/重启的 6-10 分钟），TCP 探活看不出来，粘在这条道上的请求
    会整批挂住。没配 model_routes 的新会话走 pick_upstream，同样优先就绪道。
    `"lane_ready_gate": false` 一键回到旧行为。"""
    if not CFG.get("lane_ready_gate", True):
        return True
    with LANE_LOCK:
        c = LANE_CACHE.get(u)
    if not c or (time.time() - c.get("ts", 0)) > 15:
        return True
    st = c.get("status")
    if not isinstance(st, dict):
        return True
    # auto_load=true 的引擎空闲时会主动卸载，loaded=false 是**正常状态**，不是「正在重载」。
    # 把这种空闲当成不可用，会把所有钉道请求挤到当时正好在跑的那条道上。
    if st.get("auto_load"):
        return True
    return bool(st.get("loaded", True))


def upstream_ok(u: str) -> bool:
    return upstream_alive(u) and lane_ready(u)


_up_ok_cache: dict[str, tuple[float, bool]] = {}


def upstream_alive(u: str, ttl: float = 2.0, timeout: float = 0.4) -> bool:
    """上游端口能不能连上（2 秒内复用结果，避免每个请求都探测一次）。"""
    now = time.monotonic()
    hit = _up_ok_cache.get(u)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    ok = False
    try:
        sp = urllib.parse.urlsplit(u)
        with socket.create_connection((sp.hostname, sp.port or 80), timeout=timeout):
            ok = True
    except OSError:
        ok = False
    _up_ok_cache[u] = (now, ok)
    return ok


def failover_upstream(dead: str) -> str | None:
    """给一个不可达的上游找一台活着的兄弟实例；没有就返回 None。

    2026-10-21 加的：模型按 ID 路由（`qwen3.8-flash-fast` → 8091），而 8091 换引擎要冷启动
    6~10 分钟（专家 arena 从盘重建）。以前这段时间该模型的请求全是
    `502 rag proxy: upstream failed: ConnectionRefusedError`，对外就是"服务挂了"。
    现在换到活着的 lane 继续服务——用户宁可慢一点，也不要一个红叉。
    """
    dead = dead.rstrip("/")
    cands = [u for u in upstreams() if u != dead and upstream_alive(u)]
    ready = [u for u in cands if lane_ready(u)]   # [SSE 10-31] 优先投就绪道，全在 hold 才退回活道
    return (ready or cands or [None])[0]


_CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]")
_DIGIT_RE = re.compile(r"[0-9]")
_IMAGE_TYPES = ("image", "image_url")


def text_tokens_upper(text: str) -> int:
    """一段文本的 token **上界**估算（对真引擎 count_tokens 标定过，见 README §5f）。

    旧公式 len/1.5 不是上界：数字串实测 1.00 tok/char、单字重复的 CJK 也是 1.00，都被低估 1.5 倍；
    反过来英文散文实测 0.19，旧公式又高估 3.5 倍。按字符分类取上界，才敢拿它决定"要不要问引擎"。"""
    if not text:
        return 0
    n_cjk = len(_CJK_RE.findall(text))
    n_dig = len(_DIGIT_RE.findall(text))
    n_other = len(text) - n_cjk - n_dig
    return 1 + int(n_cjk * CFG["tok_per_char_cjk"] + n_dig * CFG["tok_per_char_digit"]
                   + n_other * CFG["tok_per_char_other"])


def count_image_blocks(messages: list) -> int:
    """消息里图片块的数量。引擎 prepare() 把一张图展开成 ~230–800 个 token，
    而 /v1/messages/count_tokens 只数 1 个 pad —— 这块差额（实测 6 张 1024x768 = +4602）
    以前谁都没算，是"网关以为装得下、引擎 400"的一条真实通路。"""
    n = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") in _IMAGE_TYPES:
                    n += 1
        elif isinstance(c, dict) and c.get("type") in _IMAGE_TYPES:
            n += 1
    return n


def image_allowance(messages: list) -> int:
    return count_image_blocks(messages) * CFG["image_tokens"]


def est_prompt_tokens(messages: list, extra_text: str = "", imgs: int | None = None) -> int:
    """提示词 token 的上界估算（不叫引擎）：分类字符上界 + 每消息模板开销 + 图片展开。"""
    n_img = image_allowance(messages) if imgs is None else imgs * CFG["image_tokens"]
    t = sum(text_tokens_upper(message_text(m)) + CFG["msg_overhead_tokens"] for m in messages or [])
    return t + text_tokens_upper(extra_text) + n_img


def count_margin(prompt_tokens: int) -> int:
    """计数与引擎最终渲染之间的残差：Anthropic/OpenAI 模板不同、缓存里的旧计数、图片展开的近似。"""
    return CFG["clamp_margin_tokens"] + prompt_tokens // CFG["clamp_margin_div"]


def clamp_max_tokens(req: dict, prompt_tokens: int) -> tuple[dict, bool]:
    """保证 提示词 + max_tokens + 边界 + 计数残差 ≤ 窗口：客户端要的输出超过剩余空间时压到余量。

    引擎对超窗请求**直接 400、从不截断**，所以这里是"自动压缩"的最后一道闸。prompt_tokens 是本次
    要发出去的提示词 token 数（精确计数或组装后的估算）。返回 (可能改过 max_tokens 的 req, 是否压过)。

    2026-10-08：余量里必须再减 count_margin()。生产实测网关按"精确计数 219021"放行了
    max_tokens 32000，引擎自己数到 231775 → 400（差 5.8%）。"""
    window, slack = CFG["max_context"], CFG["ctx_slack"]
    client_max = int(req.get("max_tokens") or req.get("max_completion_tokens") or 0)
    if client_max <= 0:                       # 没写 max_tokens：用默认值，不算超
        return req, False
    room = max(1, window - slack - prompt_tokens - count_margin(prompt_tokens))
    if client_max <= room:
        return req, False
    req = dict(req)
    req["max_tokens"] = room
    req.pop("max_completion_tokens", None)    # 只留一个口径，别让另一个字段绕过闸门
    return req, True


def thinking_guard(req: dict) -> tuple[dict, bool]:
    """客户端没表达过思考偏好时，给"答复"留一份预算：把思考上限压到 `max_tokens - 预留`。

    引擎默认一直思考到答复，思考一路吃到 `max_tokens` 就是"想完了却没有答案"——引擎自己的诊断原文
    （`serve/server.py` 的 #530）：`the reply reached max tokens while still thinking, so it has no
    answer`。实测 2026-10-08：`strata-a` 173 次、`strata-b` 81 次；受害最重的是 `max_tokens` 小
    的客户端（`strata-a` 上 2112 一档占 149 次、`strata-b` 上 512 一档占 49 次）。

    引擎支持**按请求**给 `reasoning_budget_tokens`（#123）：到达预算时它把思考按模型自己的方式收尾
    （`REASONING_WRAP_UP`：一句"想够了，给答案" + `</think>`）然后接着生成——**已经读进去的前缀不重读**，
    所以代价只是"思考短一点"，不是"再来一遍 prefill"。

    只管"没说话"的请求：客户端一旦自己给了 `thinking` / `output_config` / `reasoning_effort` /
    `reasoning_budget_tokens`，一律不碰（那是它的选择，改它就是越权）。上限本身留不出答复空间的
    （预算 < 64）也不补——补了没有余量收尾，结果一样。返回 (req 或改过的新 dict, 是否改过)。"""
    if not CFG.get("thinking_budget_guard", True) or not isinstance(req, dict):
        return req, False
    for k in ("thinking", "output_config", "reasoning_effort", "reasoning_budget_tokens"):
        if req.get(k) is not None:
            return req, False
    mt = int(req.get("max_tokens") or req.get("max_completion_tokens") or 0)
    if mt <= 0:                                 # 客户端没给输出上限：引擎按自己的默认走，不动它
        return req, False
    reserve = min(int(CFG.get("answer_reserve_tokens", 4096)),
                  max(64, mt // int(CFG.get("answer_reserve_div", 3))))
    budget = mt - reserve
    if budget < 64:
        return req, False
    out = dict(req)
    out["reasoning_budget_tokens"] = budget
    return out, True


def refit_thinking_budget(req: dict) -> dict:
    """`max_tokens` 被压小之后，网关补的那个思考预算可能反而**比它、甚至比它大**（那等于没有预算：
    思考照样能吃到上限）。按同一套规则重算一次——只能重算**网关自己补的**，客户端自带的绝不碰。
    这条正好落在窗口守卫的高压区：23 万 token 的提示词会让 `max_tokens` 从 32000 被压到 ~26k，
    不重算的话思考仍可能吃掉全部额度。"""
    tb = int(req.get("reasoning_budget_tokens") or 0)
    mt = int(req.get("max_tokens") or req.get("max_completion_tokens") or 0)
    if not tb or not mt or tb < mt:
        return req
    reserve = min(int(CFG.get("answer_reserve_tokens", 4096)),
                  max(64, mt // int(CFG.get("answer_reserve_div", 3))))
    room = mt - reserve
    out = dict(req)
    if room >= 64:
        out["reasoning_budget_tokens"] = room
    else:
        out.pop("reasoning_budget_tokens", None)
    return out


_OVERFIT_RE = re.compile(r"at most (\d+) here")


def overfit_room(payload: bytes) -> int | None:
    """引擎的超窗 400 里直接写了"这里最多还能给多少 max_tokens"（#545）。照这个数压一次，
    比网关再猜一遍计数准——它是引擎自己数到的真实 prompt 长度。"""
    try:
        msg = json.loads(payload.decode("utf-8", "replace")).get("error", {}).get("message", "")
    except Exception:
        msg = payload.decode("utf-8", "replace")
    if "exceeds the context" not in msg:
        return None
    m = _OVERFIT_RE.search(msg)
    return int(m.group(1)) if m else None


def patch_max_tokens(raw: bytes, room: int) -> bytes | None:
    """把请求体里的 max_tokens 压到 room。当前值已经 ≤ room 时返回 None（说明不是输出长度的问题）。"""
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(req, dict):
        return None
    room = max(1, room)
    cur = int(req.get("max_tokens") or req.get("max_completion_tokens") or 0)
    if 0 < cur <= room:
        return None
    req["max_tokens"] = room
    req.pop("max_completion_tokens", None)
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


_PROMPT_TOK_RE = re.compile(r"prompt \((\d+) tokens\)")
_CTX_WIN_RE = re.compile(r"the context \((\d+)\)")


def overfit_info(payload: bytes) -> dict | None:
    """从引擎/serve 的超窗 400 里解析出结构化数字（实测 prompt、窗口、最多可给的输出）。
    覆盖两种措辞：`prompt (N) + max tokens (M) exceeds the context (W) … at most X here`（#545）和
    `prompt (N) leaves no room to answer in the context (W)`（fit_max_tokens 打开后、prompt 独占窗口时）。
    不是超窗错误返回 None——别的 400（坏图片之类）绝不在这被当成窗口问题。"""
    try:
        msg = json.loads(payload.decode("utf-8", "replace")).get("error", {}).get("message", "")
    except Exception:
        msg = payload.decode("utf-8", "replace")
    if "exceeds the context" not in msg and "leaves no room" not in msg:
        return None
    info: dict = {}
    m = _PROMPT_TOK_RE.search(msg)
    info["prompt"] = int(m.group(1)) if m else None
    m = _CTX_WIN_RE.search(msg)
    info["window"] = int(m.group(1)) if m else None
    m = _OVERFIT_RE.search(msg)
    info["room"] = int(m.group(1)) if m else None
    return info


def overfit_error(info: dict) -> dict:
    """把 serve 的内部文案（#545 那种写给运维看的句子）换成客户端能直接读的干净错误：
    message 照 Anthropic 原生 `prompt is too long: N tokens > M maximum` 的口径，结构化字段带上
    实测 prompt / 窗口 / 最大可用输出——中转、计费、客户端都别再解析英文句子。2026-10-19 书稿会话
    把 #545 原文透传给了终端用户，这是要修的最后一步。"""
    window = info.get("window") or CFG["max_context"]
    prompt = info.get("prompt")
    room = info.get("room")
    if room is None and prompt is not None:
        room = max(0, window - CFG["ctx_slack"] - prompt - count_margin(prompt))
    avail = max(0, window - CFG["ctx_slack"])
    if prompt is not None and prompt >= avail:
        msg = f"prompt is too long: {prompt} tokens > {avail} maximum"
    else:
        msg = f"request does not fit: prompt {prompt} tokens plus desired output exceeds the {avail}-token window"
    return {"type": "error", "error": {"type": "invalid_request_error", "message": msg,
                                       "prompt_tokens": prompt, "max_context": window,
                                       "max_output_tokens": max(0, room or 0),
                                       "hint": "提示词本身已超窗，压 max_tokens 救不了；"
                                               "请压缩对话（/compact）或开新会话后重试"}}


def log_ctx(req: dict, prompt_tokens: int, mode: str, extra: dict | None = None,
            was_total: int | None = None, clamped: bool = False):
    """每请求一行窗口算术：窗口 = 提示词 + 输出预留 + 安全边界，一眼看出有没有临窗/压缩。"""
    window, slack = CFG["max_context"], CFG["ctx_slack"]
    client_max = int(req.get("max_tokens") or req.get("max_completion_tokens") or 0)
    room = window - slack - prompt_tokens - count_margin(prompt_tokens)
    note = ""
    if clamped:
        note = f" | ⚠ max_tokens 超余量已压到 {req.get('max_tokens')}"
    elif client_max and client_max > room:
        note = f" | ⚠ 客户端 max_tokens {client_max} > 余量 {room}（未压缩，应已被压缩）"
    src = f"（原始 {was_total} tok）" if was_total and was_total != prompt_tokens else ""
    tail = ""
    if extra:
        tail = (f" 最近窗口 {extra.get('recent_tokens')} + 检索块 {extra.get('memory_tokens')}"
                + ("·块复用" if extra.get("reused_block") else ""))
    tb = int(req.get("reasoning_budget_tokens") or 0)      # 网关补的或客户端自带的思考预算，都写出来
    if tb:
        tail += f" 思考预算 {tb}"
    log(f"ctx[{mode}]: 窗口 {window} = 提示词 {prompt_tokens}{src} + max_tokens {req.get('max_tokens') or client_max or CFG['gen_reserve_default']}"
        f" + 边界 {slack} + 计数残差 {count_margin(prompt_tokens)} | 余量 {room}{note}{tail}")


def http_json(url: str, payload: dict | None, timeout: float, headers: dict | None = None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_COUNT_CACHE: dict[str, int] = {}
_COUNT_LOCK = threading.Lock()


def count_tokens(messages: list, extra: dict | None = None, up: str | None = None,
                 dialect: str = "anthropic") -> int | None:
    """精确计数（走本机 serve 的 /v1/{…}/count_tokens，不跑模型）；失败返回 None。

    `extra` 必须带上**同请求里影响渲染的字段**（`tools`、Anthropic 的 `system`）。漏掉它们会严重
    低估提示词——Claude Code 这类客户端的工具定义就有上万 token，曾导致"网关以为装得下、引擎 400 拒绝"。
    `dialect="openai"` 数 `/v1/chat/completions` 格式的请求：中转把 Claude Code 的 Anthropic 请求转成
    OpenAI 再进网关，两种方言的渲染不同，数错格式等于没数（2026-10-19 计数口径排查）。"""
    body = {"model": "strata", "messages": messages}
    for k in ("system", "tools"):
        if extra and extra.get(k):
            body[k] = extra[k]
    key = sha1(dialect + json.dumps(body, ensure_ascii=False, sort_keys=True))
    with _COUNT_LOCK:
        if key in _COUNT_CACHE:
            return _COUNT_CACHE[key]
    path = ("/v1/chat/completions/count_tokens" if dialect == "openai"
            else "/v1/messages/count_tokens")
    try:
        r = http_json(f"{up or CFG['upstream']}{path}", body,
                      CFG["count_timeout_s"], {"x-api-key": API_KEY})
        n = int(r.get("input_tokens", 0))
    except Exception as e:
        dbg("count_tokens failed:", e)
        return None
    with _COUNT_LOCK:
        if len(_COUNT_CACHE) > 4096:
            _COUNT_CACHE.clear()
        _COUNT_CACHE[key] = n
    return n


# ----------------------------------------------------------------------------- 文本抽取与分块

def message_text(msg: dict) -> str:
    """从 OpenAI / Anthropic 两种消息形态里抽出纯文本（图片用占位符，工具块带标记）。"""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    parts = []
    for b in c:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" or t == "input_text":
            parts.append(b.get("text", ""))
        elif t in ("image_url", "image"):
            parts.append("[image]")
        elif t == "tool_use":
            parts.append(f"[tool_use {b.get('name','')}] {json.dumps(b.get('input', {}), ensure_ascii=False)}")
        elif t == "tool_result":
            inner = b.get("content")
            if isinstance(inner, list):
                inner = " ".join(x.get("text", "") for x in inner if isinstance(x, dict))
            parts.append(f"[tool_result] {inner if isinstance(inner, str) else ''}")
        elif t == "thinking":
            parts.append(b.get("thinking", ""))
    if isinstance(msg.get("tool_calls"), list):     # OpenAI 助手工具调用
        for tc in msg["tool_calls"]:
            parts.append(f"[tool_call {tc.get('function', {}).get('name', '')}] "
                         f"{tc.get('function', {}).get('arguments', '')}")
    return "\n".join(p for p in parts if p)


# ---- 结构解析：先把 messages 解析成"回合"，再谈分块 ------------------------------------
#
# 为什么不能按长度切：agent 会话的最小语义单位是一条**回合**——用户输入 → agent 工具调用 →
# 工具返回 → agent 回答。按 768 token 硬切会把工具返回和产生它的工具调用劈成两块、把回合从中间
# 截断，检索带回的就是没有出处的碎片（1M 精度报告 §2.6 "张冠李戴"的根因之一：句式雷同的碎片
# 无法判断属于哪一轮，模型就把更近的相似事实搬过来）。所以顺序是：
#   解析回合 → 回合内按结构合并成块（块不跨回合）→ 只有单条消息本身超预算才按自然边界再切。

def classify(msg: dict) -> tuple[str, str]:
    """把一条消息归入对话结构，返回 (kind, 工具名)。kind ∈ system/user/assistant/tool_call/tool_result。

    注意 Anthropic 形态把工具返回放在 **user 消息**里（content 里是 tool_result 块）：这种消息是
    "工具返回"，不是新的用户输入，不能在这里开新回合——否则每个工具返回都会被当成一轮新问题。"""
    role = str(msg.get("role", "?"))
    c = msg.get("content")
    blocks = c if isinstance(c, list) else []
    names: list[str] = []
    has_tool_use = has_tool_result = False
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "tool_use":
            has_tool_use = True
            names.append(str(b.get("name", "")))
        elif t == "tool_result":
            has_tool_result = True
    if role in ("system", "developer"):
        return "system", ""
    if role in ("tool", "function"):
        return "tool_result", ""
    if role == "assistant":
        if isinstance(msg.get("tool_calls"), list):
            names += [str((tc.get("function") or {}).get("name", "")) for tc in msg["tool_calls"]
                      if isinstance(tc, dict)]
        if msg.get("tool_calls") or has_tool_use:
            return "tool_call", ",".join(n for n in names if n)
        return "assistant", ""
    if role == "user":
        return ("tool_result", "") if has_tool_result else ("user", "")
    return role, ""


class Unit:
    """一个结构单元 = 一条消息（用户输入 / agent 回复 / 工具调用 / 工具返回）。"""
    __slots__ = ("msg_idx", "turn", "kind", "role", "name", "text", "trimmed")

    def __init__(self, msg_idx: int, turn: int, kind: str, role: str, name: str,
                 text: str, trimmed: bool = False):
        self.msg_idx, self.turn, self.kind, self.role = msg_idx, turn, kind, role
        self.name, self.text, self.trimmed = name, text, trimmed


class Turn:
    """一条回合：一个用户输入 + 它引发的所有 agent 步骤（工具调用/工具返回/回答）。"""
    __slots__ = ("idx", "units", "msg_from", "msg_to")

    def __init__(self, idx: int, units: list[Unit], msg_from: int, msg_to: int):
        self.idx, self.units, self.msg_from, self.msg_to = idx, units, msg_from, msg_to

    def label(self) -> str:
        """给模型看的结构说明，例如 `user → assistant(tool_call: vault_get) → tool_result → assistant`。"""
        steps: list[str] = []
        for u in self.units:
            if u.kind == "tool_call":
                steps.append(f"assistant(tool_call: {u.name})" if u.name else "assistant(tool_call)")
            elif u.kind == "tool_result":
                steps.append("tool_result")
            else:
                steps.append(u.kind)
        out: list[str] = []
        for s in steps:                       # 压缩连续同类（一次回合里多次工具往返）
            if not out or out[-1] != s:
                out.append(s)
        return " → ".join(out)


def parse_turns(messages: list) -> list[Turn]:
    """把 messages 解析成回合；system 消息单独出去（不进检索、逐字保留在组装头部）。"""
    turns: list[Turn] = []
    cur: Turn | None = None
    for i, m in enumerate(messages):
        kind, name = classify(m)
        if kind == "system":
            continue
        text = message_text(m).strip()
        if not text:
            continue
        if kind == "user" or cur is None:            # 真正的用户输入才开新回合
            cur = Turn(len(turns), [], i, i)
            turns.append(cur)
        cur.units.append(Unit(i, cur.idx, kind, str(m.get("role", "?")), name, text))
        cur.msg_to = i
    return [t for t in turns if t.units]


# ---- 自然边界切分（只在单条消息本身超预算时用）----------------------------------------

def _pack(pieces: list[str], target: int, overlap: int) -> list[str]:
    """把**边界片段**装箱成 ≤target 字符的块。重叠只带整片段（整行/整句），不像旧版那样
    从字符位置回退 128 字符——那会把一行日志、一个 JSON 从中间劈开。"""
    out: list[str] = []
    cur, cur_pieces = "", []
    for p in pieces:
        if not p:
            continue
        if cur and len(cur) + len(p) > target:
            out.append(cur)
            carry, acc = [], 0
            for q in reversed(cur_pieces):
                if acc + len(q) > overlap:
                    break
                carry.insert(0, q)
                acc += len(q)
            cur, cur_pieces = "".join(carry), carry
        cur += p
        cur_pieces.append(p)
    if cur.strip():
        out.append(cur)
    return out


def split_unit(text: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    """按自然边界切一条长内容，优先级：**行**（日志/代码/表格类工具输出）→ 段落 → 句子 → 硬切。

    旧版是"768 token 字符窗口 + 128 字符重叠"，实测把 `…retry=2\\nline 27…` 切成下一块的
    `y stage 1 reported…`。这里块边界一定落在行/段/句上；只有单行或单句本身就超预算才硬切。"""
    target = max(400, int(target_tokens * CFG["chars_per_token"]))
    overlap = max(0, int(overlap_tokens * CFG["chars_per_token"]))
    if len(text) <= target:
        return [text]
    lines = text.split("\n")
    if len(lines) >= 8 and max(len(l) for l in lines) <= target * 0.6:
        parts = _pack([l + "\n" for l in lines], target, overlap)          # 行结构：整行装箱，绝不切行
    else:
        pieces: list[str] = []
        for p in re.split(r"(\n\s*\n)", text):                             # 段落级
            if not p:
                continue
            if len(p) > target:                                            # 段落本身超长 → 句子级
                pieces.extend(re.split(r"(?<=[。！？!?.])\s", p))
            else:
                pieces.append(p)
        parts = _pack(pieces, target, overlap)
    final: list[str] = []
    for c in parts:
        while len(c) > target * 1.5:                                       # 兜底：单行/单句超预算
            final.append(c[:target])
            c = c[max(1, target - overlap):]
        final.append(c)
    return [c for c in final if c.strip()]


def trim_at_line_tail(text: str, keep_chars: int) -> str:
    """保留尾部，并把切口往前挪到行首（最近窗口里的巨型消息也别从行中间开始）。"""
    if len(text) <= keep_chars:
        return text
    cut = len(text) - max(1, keep_chars)
    nl = text.find("\n", cut)
    if 0 <= nl < cut + 4096:
        cut = nl + 1
    return text[cut:]


class Chunk:
    """检索块：一个回合内相邻结构单元的合并，或一条超长消息按自然边界切出的一段。"""
    __slots__ = ("idx", "turn", "turn_label", "msg_idx", "msg_from", "msg_to", "role", "kind",
                 "text", "hash", "tokens", "part", "n_parts")

    def __init__(self, idx: int, turn: int, turn_label: str, msg_from: int, msg_to: int,
                 role: str, kind: str, text: str, part: int = 0, n_parts: int = 1):
        self.idx, self.turn, self.turn_label = idx, turn, turn_label
        self.msg_from, self.msg_to, self.msg_idx = msg_from, msg_to, msg_from
        self.role, self.kind, self.text = role, kind, text
        self.part, self.n_parts = part, n_parts
        # 嵌入键 = 角色 + 文本内容（不含位置）：同一内容再发一次、或在别的会话里再出现一次，
        # 哈希不变 → 直接复用已有向量。块边界只由**本条消息自己的结构**决定，不受邻居长度影响，
        # 所以逐轮追加历史时旧块哈希恒定（旧版重叠是"回退 128 字符"，邻居一变边界就漂）。
        self.hash = sha1(f"{role}\x00{text}")
        self.tokens = est_tokens(text)


def chunk_turn(turn: Turn) -> list[Chunk]:
    """把一个回合切成块：**一个结构单元 = 一块**（一条用户输入、一次工具调用、一次工具返回、
    一段回答各成一块），块绝不跨回合；只有单条消息本身超预算才按自然边界再切成 part/n_parts。

    为什么不在块里合并相邻单元（实测过，见 test_chunking.py §7 与 README §5e）：合并会让**进行中的
    回合**每来一条消息就把最后一块重新切一遍——40 轮真实 agent 会话逐条到达要白做 100 次嵌入、
    留下 100 个陈旧块（陈旧块还留在检索索引里当候选，等于自己给自己加噪声）。逐单元成块是唯一的
    **零重嵌**策略：一条消息一旦到达就不再变，它的块边界与哈希从此恒定，向量直接复用。
    回合的连贯性不靠"合并进同一块"来保证，而由 render_block 的回合标签 + expand_context 的
    结构补全在组装时还原——切分负责稳定，渲染负责语序。"""
    out: list[Chunk] = []
    target = CFG["chunk_tokens"]
    label = turn.label()
    for u in turn.units:
        if est_tokens(u.text) > target:                 # 单条超预算：按 行→段→句 再切
            pieces = split_unit(u.text, target, CFG["chunk_overlap_tokens"])
            for k, p in enumerate(pieces):
                out.append(Chunk(len(out), turn.idx, label, u.msg_idx, u.msg_idx,
                                 u.role, u.kind, p, k, len(pieces)))
            continue
        out.append(Chunk(len(out), turn.idx, label, u.msg_idx, u.msg_idx, u.role, u.kind, u.text))
    return out


def chunk_messages(messages: list) -> list[Chunk]:
    if not CFG.get("structure_chunking", True):
        return chunk_messages_legacy(messages)
    chunks: list[Chunk] = []
    for t in parse_turns(messages):
        for c in chunk_turn(t):
            c.idx = len(chunks)
            chunks.append(c)
    return chunks


def chunk_messages_legacy(messages: list) -> list[Chunk]:
    """旧的分块方式（按长度硬切），保留用于 A/B 对照与 `structure_chunking: false` 回退。"""
    chunks: list[Chunk] = []
    for i, m in enumerate(messages):
        text = message_text(m).strip()
        if not text:
            continue
        role = str(m.get("role", "?"))
        if est_tokens(text) <= CFG["chunk_tokens"]:
            c = Chunk(len(chunks), -1, "", i, i, role, role, text)
            chunks.append(c)
        else:
            pieces = split_unit(text, CFG["chunk_tokens"], CFG["chunk_overlap_tokens"])
            for k, p in enumerate(pieces):
                chunks.append(Chunk(len(chunks), -1, "", i, i, role, role, p, k, len(pieces)))
    return chunks


# ----------------------------------------------------------------------------- 嵌入（后台，增量，内容哈希缓存）

EMB_CACHE: dict[str, np.ndarray] = {}         # 内容哈希 -> 向量（“KV 形式”的预嵌入缓存）
EMB_DIM = 0
EMB_LOCK = threading.Lock()
EMB_QUEUE: "queue.Queue[str]" = queue.Queue()
EMB_SEEN: set[str] = set()                    # 已入队/已完成的哈希（防重复入队）


def embed_texts(texts: list[str]) -> list[np.ndarray] | None:
    try:
        r = http_json(CFG["embed_url"], {"input": texts, "model": CFG["embed_model"]},
                      CFG["embed_timeout_s"])
        data = sorted(r["data"], key=lambda d: d["index"])
        return [np.asarray(d["embedding"], dtype=np.float32) for d in data]
    except Exception as e:
        dbg("embed failed:", e)
        return None


def embedding_worker():
    global EMB_DIM
    backoff = 1.0
    while True:
        batch: list[tuple[str, str]] = []
        try:
            h = EMB_QUEUE.get(timeout=1.0)
            batch.append((h, EMB_TEXTS.get(h, "")))
            while len(batch) < CFG["embed_batch"]:
                try:
                    h2 = EMB_QUEUE.get_nowait()
                    batch.append((h2, EMB_TEXTS.get(h2, "")))
                except queue.Empty:
                    break
        except queue.Empty:
            continue
        batch = [(h, t) for h, t in batch if t]    # 文本已被并发请求取走的条目直接跳过，绝不拿空串去嵌入
        if not batch:
            continue
        texts = [t for _, t in batch]
        vecs = embed_texts(texts)
        if vecs is None:
            COUNTERS["embed_errors"] += 1
            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)
            for h, _ in batch:                     # 放回去重试
                EMB_QUEUE.put(h)
            continue
        backoff = 1.0
        with EMB_LOCK:
            for (h, _), v in zip(batch, vecs):
                EMB_CACHE[h] = v
                EMB_DIM = len(v)
        COUNTERS["embeds_done"] += len(batch)
        for h, _ in batch:
            EMB_TEXTS.pop(h, None)


EMB_TEXTS: dict[str, str] = {}                 # 待嵌入的哈希 -> 文本


def enqueue_embed(chunks: list[Chunk]):
    global EMB_TEXTS
    with EMB_LOCK:
        for c in chunks:
            if c.hash in EMB_CACHE or c.hash in EMB_SEEN:
                continue
            EMB_SEEN.add(c.hash)
            EMB_TEXTS[c.hash] = c.text
            EMB_QUEUE.put(c.hash)
            COUNTERS["embeds_queued"] += 1


# ----------------------------------------------------------------------------- 重排

_RERANK_CACHE: dict[str, tuple[float, list[float]]] = {}
_RERANK_LOCK = threading.Lock()


def rerank(query: str, docs: list[str]) -> list[float] | None:
    key = sha1(query + "\x00" + "\x00".join(docs))
    with _RERANK_LOCK:
        hit = _RERANK_CACHE.get(key)
        if hit and time.time() - hit[0] < CFG["rerank_cache_ttl_s"]:
            COUNTERS["rerank_cache_hits"] += 1
            return hit[1]
    try:
        r = http_json(CFG["rerank_url"], {"query": query, "documents": docs}, CFG["rerank_timeout_s"])
        scores = [0.0] * len(docs)
        for item in r.get("results", []):
            i = int(item["index"])
            if 0 <= i < len(docs):
                scores[i] = float(item["relevance_score"])
    except Exception as e:
        dbg("rerank failed:", e)
        return None
    COUNTERS["rerank_calls"] += 1
    with _RERANK_LOCK:
        if len(_RERANK_CACHE) > 512:
            _RERANK_CACHE.clear()
        _RERANK_CACHE[key] = (time.time(), scores)
    return scores


# ----------------------------------------------------------------------------- 会话

class Session:
    def __init__(self, key: str):
        self.key = key
        # 会话级串行锁（2026-10-08）：一个会话键上的请求排队执行。历史守卫（比历史）、分块入库、
        # 计数前缀缓存、组装块复用这几步共享同一份会话状态，**都不允许两个请求同时在飞**：
        # 两个内容分叉、却都"与旧历史对得上"的并发请求会各自入库，随后互相检索到对方的内容——
        # 那是唯一一条"串上下文"的路（见技术文档 §5.3 的 R1/R2/R4）。用一个会话本来就是顺序的，
        # 排队不改变正常行为；锁在转发前释放，不占上游的长尾。
        self.lock = threading.RLock()
        self.created = self.updated = time.time()
        self.chunks: list[Chunk] = []
        self.hashes: set[str] = set()
        self.vectors: dict[str, np.ndarray] = {}
        self.stable_block: str | None = None       # 稳定的“检索块”（前缀缓存友好）
        self.stable_chunks: list[Chunk] = []       # 同一批块（优先级序）：复核超预算时按整块砍
        self.stable_detail: dict | None = None     # 建这批块时的检索明细（复用路径要报同一份）
        self.stable_block_query = ""
        self.stable_block_at = 0.0
        self.assemblies = 0
        # 精确计数的前缀复用：客户端逐轮追加时，上一次请求的 messages 是新请求的前缀，
        # 只需精算新增那几轮（超长会话下整段计数是秒级开销）
        self.count_hashes: list[str] = []
        self.count_total = 0
        self.hist_hashes: list[str] = []   # 最近一次请求的消息哈希序列（会话完整性守卫用）

    def reset(self):
        """同一会话键下的历史对不上（模板提示词撞 key 等）：清空检索状态，两段对话绝不互相污染。"""
        self.chunks, self.hashes, self.vectors = [], set(), {}
        self.stable_block, self.stable_block_query, self.stable_block_at = None, "", 0.0
        self.stable_chunks = []
        self.stable_detail = None
        self.count_hashes, self.count_total = [], 0
        self.hist_hashes = []
        self.created = time.time()

    def ingest(self, messages: list) -> list[Chunk]:
        """把新出现的内容分块并排队嵌入；返回本次新增的块。"""
        fresh = []
        for c in chunk_messages(messages):
            if c.hash in self.hashes:
                continue
            self.hashes.add(c.hash)
            c.idx = len(self.chunks)
            self.chunks.append(c)
            fresh.append(c)
        if fresh:
            enqueue_embed(fresh)
        return fresh

    def refresh_vectors(self):
        with EMB_LOCK:
            for c in self.chunks:
                if c.hash not in self.vectors and c.hash in EMB_CACHE:
                    self.vectors[c.hash] = EMB_CACHE[c.hash]


SESSIONS: dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()


def get_session(key: str) -> Session:
    now = time.time()
    with SESSIONS_LOCK:
        if len(SESSIONS) >= CFG["max_sessions"] or (SESSIONS and not SESSIONS.get(key)):
            # 淘汰：TTL 过期 + 最久未用
            for k in [k for k, s in SESSIONS.items() if now - s.updated > CFG["session_ttl_s"]]:
                SESSIONS.pop(k, None)
            while len(SESSIONS) >= CFG["max_sessions"]:
                oldest = min(SESSIONS, key=lambda k: SESSIONS[k].updated)
                SESSIONS.pop(oldest, None)
        s = SESSIONS.get(key)
        if s is None:
            s = Session(key)
            SESSIONS[key] = s
        s.updated = now
        return s


def session_key(headers, body: dict) -> str:
    h = headers.get("X-Strata-Session") or headers.get("x-strata-session")
    if h:
        return "hdr:" + h.strip()[:128]
    msgs = body.get("messages") or []
    system = body.get("system") or ""
    if isinstance(system, list):
        system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
    first_user = ""
    for m in msgs:
        if m.get("role") == "user":
            first_user = message_text(m)
            break
    return "auto:" + sha1(str(system) + "\x00" + first_user)[:16]


# ----------------------------------------------------------------------------- 检索与组装

def cosine_matrix(sess: Session, query_vec: np.ndarray) -> list[tuple[Chunk, float]]:
    out = []
    qn = np.linalg.norm(query_vec) + 1e-9
    for c in sess.chunks:
        v = sess.vectors.get(c.hash)
        if v is None:
            continue
        out.append((c, float(np.dot(v, query_vec) / (np.linalg.norm(v) * qn + 1e-9))))
    return out


def query_text(messages: list) -> str:
    """查询 = 最后一条用户消息（找不到就退化为最后一条消息）。

    巨型消息（用户一次性粘贴超长文档）**不能整条当查询**：嵌入模型只吃 8192 token，
    整条送等于拿开头一截噪声做检索（还会把嵌入队列打爆→检索块为空）。超预算时取
    **头+尾**：头里常有"请阅读以下文档"之类的指令，尾里是真正的问题。"""
    for m in reversed(messages):
        if m.get("role") == "user":
            return bound_query(message_text(m))
    return bound_query(message_text(messages[-1])) if messages else ""


def bound_query(text: str) -> str:
    limit = int(CFG.get("query_max_tokens", 2048))
    if est_tokens(text) <= limit:
        return text
    keep = int(limit * CFG["chars_per_token"])
    return text[: keep // 4] + "\n...\n" + text[-(keep * 3 // 4):]   # 头 1/4 + 尾 3/4：问题通常在末尾


def recent_window(messages: list, turns: list[Turn], budget_tokens: int) -> tuple[list, set[int], set[int], int]:
    """从后往前按**整回合**收集逐字保留的消息，返回 (消息, 保留的回合号, 保留的消息序号, token 数)。

    回合对齐是关键：旧版按单条消息从后往前装，窗口可能停在回合中间——模型看到一个孤零零的工具
    返回，不知道它由哪个工具调用产生、回答的是哪个问题（语序失真的另一半来源）。这里窗口边界永远
    落在回合边界上：要么整回合在窗口里，要么整回合交给检索。

    最后一个回合本身就超预算时（一次性灌入的巨型回合）：先从头部**整单元整单元地**丢，只剩一条
    还装不下时才在那条消息内部按行截尾；被剪掉的部分仍在会话分块里，可由检索块按相关性召回
    （否则组装完全失效：检索拿不到候选，请求原样超窗被引擎 400 拒绝）。"""
    def cost_of(units: list[Unit]) -> int:
        return sum(est_tokens(u.text) + 8 for u in units)

    picked: list[Unit] = []
    turns_kept: set[int] = set()
    msgs_kept: set[int] = set()
    total = 0
    for t in reversed(turns):
        units = t.units
        cost = cost_of(units)
        if picked and total + cost > budget_tokens:
            break                                   # 窗口边界落在回合边界上
        if cost > budget_tokens:                    # 单回合就超预算（只可能是最后那条回合）
            units = list(t.units)
            # **先保住回合骨架**：按从小到大装箱，短单元（用户问题、工具调用、回答）整条留下，
            # 装不下的超长单元按行截尾、分剩余预算。旧版是从头部整条丢——结果常常把用户问题丢了、
            # 只留一坨巨型工具输出，模型完全不知道那坨输出是回答哪个问题的（语序失真）。
            room = budget_tokens
            keep: set[int] = set()
            for k in sorted(range(len(units)), key=lambda k: est_tokens(units[k].text)):
                c = est_tokens(units[k].text) + 8
                if c <= room:
                    room -= c
                    keep.add(k)
            for k in sorted(range(len(units)), key=lambda k: est_tokens(units[k].text)):
                if k in keep or room <= 512:
                    continue
                u = units[k]
                txt = trim_at_line_tail(u.text, max(1024, int(room * CFG["chars_per_token"])))
                if est_tokens(txt) + 8 <= room:
                    units[k] = Unit(u.msg_idx, u.turn, u.kind, u.role, u.name, txt, trimmed=True)
                    room -= est_tokens(txt) + 8
                    keep.add(k)
            if not keep:                            # 极端：连一条都装不下，保底留最后一条的尾巴
                u = units[-1]
                txt = trim_at_line_tail(u.text, max(512, int(budget_tokens * CFG["chars_per_token"])))
                units = [Unit(u.msg_idx, u.turn, u.kind, u.role, u.name, txt, trimmed=True)]
            else:
                units = [u for k, u in enumerate(units) if k in keep]
        turns_kept.add(t.idx)
        picked.extend(units)
        total += cost_of(units)
    picked.reverse()
    msgs_kept = {u.msg_idx for u in picked}
    out = [dict(messages[u.msg_idx], content=u.text) if u.trimmed else messages[u.msg_idx]
           for u in picked]
    return out, turns_kept, msgs_kept, total


def role_weight(role: str) -> float:
    return {"user": 1.0, "assistant": 0.8, "tool": 0.4, "system": 0.5}.get(role, 0.5)



def render_block(chosen: list[Chunk]) -> str:
    """按**原始先后顺序**渲染检索块，每换一条回合打一行结构标签。

    块顺序 = 回合顺序（不是分数顺序），模型读到的仍是事情发生的次序；结构标签
    （`user → assistant(tool_call: vault_get) → tool_result`）让每个片段带上出处——这正是
    §2.6 "张冠李戴"的对症处理：片段可归属，模型就不容易把相似事实从别的回合搬过来。"""
    if not chosen:
        return ""
    parts = ["[Earlier conversation excerpts retrieved for the current question]"]
    last_turn = None
    for c in sorted(chosen, key=lambda x: (x.turn, x.idx, x.part)):
        if c.turn != last_turn:
            parts.append(f"--- turn {c.turn} · {c.turn_label} ---" if c.turn_label
                         else f"--- turn {c.turn} ---")
            last_turn = c.turn
        tag = f" part {c.part + 1}/{c.n_parts}" if c.n_parts > 1 else ""
        parts.append(f"[{c.role}{tag}]\n{c.text}")
    return "\n\n".join(parts)


def expand_context(chosen: list[Chunk], sess: Session, budget_tokens: int, used: int) -> list[Chunk]:
    """结构补全：选中的块若属于某条回合，把它**同一消息的其余分段**、**紧邻的分段**、以及
    **回合锚点**（回合首块，通常是那条用户问题）在预算内带上——否则模型看到的仍是无头无尾的碎片
    （工具返回没有产生它的工具调用，回答没有它回答的问题）。"""
    ratio = float(CFG.get("context_expand_ratio", 0.25))
    expand_budget = min(int(budget_tokens * ratio), budget_tokens - used)
    if expand_budget <= 0:
        return []
    by_turn: dict[int, list[Chunk]] = {}
    for c in sess.chunks:
        by_turn.setdefault(c.turn, []).append(c)
    have = {c.idx for c in chosen}
    added: list[Chunk] = []
    spent = 0
    for c in list(chosen):
        siblings = by_turn.get(c.turn) or []
        if len(siblings) < 2:
            continue
        # 首选**整条回合补齐**：一条 agent 回合通常只有一两千 token，补齐它等于把"这段对话"完整
        # 带回（工具返回带上产生它的工具调用、回答带上它回答的那个问题），次序原样。
        whole = [s for s in siblings if s.idx not in have]
        cost = sum(est_tokens(s.text) + 24 for s in whole)
        if cost and spent + cost <= expand_budget:
            chosen.extend(whole)
            have.update(s.idx for s in whole)
            added.extend(whole)
            spent += cost
            continue
        # 预算不够整条补齐：退而补**同消息的其余分段**、**紧邻分段**、**回合锚点**（回合首块，
        # 通常是那条用户问题）——至少让每个片段有出处。
        anchor = siblings[0]
        for s in siblings:
            if s.idx in have:
                continue
            same_msg = s.msg_idx == c.msg_idx
            near = abs(s.idx - c.idx) <= 1
            is_anchor = s is anchor
            if not (same_msg or near or is_anchor):
                continue
            t = est_tokens(s.text) + 24
            if spent + t > expand_budget:
                continue
            chosen.append(s)
            have.add(s.idx)
            added.append(s)
            spent += t
    return added


def build_memory_block(sess: Session, messages: list, query: str, budget_tokens: int,
                       recent_turns: set[int], recent_msg_idx: set[int]) -> tuple[str, int, float, list[Chunk]]:
    """检索 + 重排 + MMR + 结构补全 + 贪心填充，产出“更早历史”的检索块。
    返回 (块文本, token 数, 耗时, 按优先级排序的块列表)——最后这项让"预算不够时砍块"
    砍的是**最低优先级的整块**，而不是像旧版那样把块文本按 0.7 比例从中间截一刀。"""
    t0 = time.time()
    sess.refresh_vectors()
    if EMB_DIM == 0:
        return "", 0, 0.0, [], {}          # 第 5 项是"检索明细"：没有就是空 dict（不是空 list）
    # 查询向量先入队：和本会话的块嵌入一起排队，等待期间它先算完
    qh = sha1("Q\x00" + query)
    # 入队必须在 EMB_LOCK 内、并与 EMB_SEEN 去重（2026-10-08）：两个并发同查询请求原本会各入队一次，
    # 第二次出队时 EMB_TEXTS 里的文本已被前一次 pop 掉 → 拿空串去嵌入，并把结果覆盖进 EMB_CACHE[qh]，
    # 之后这个查询向量就一直是错的（检索质量下降）。见技术文档 §5.3 的 R3。
    with EMB_LOCK:
        qv = EMB_CACHE.get(qh)
        if qv is None and qh not in EMB_SEEN:
            EMB_SEEN.add(qh)
            EMB_TEXTS[qh] = query
            EMB_QUEUE.put(qh)
    # 预处理等待：会话的块可能刚入队（一次性灌入整段历史），向量没算完检索就是空的。
    # 等到本会话的块**全部**嵌入完成（有界 `embed_wait_s`）——TTFT 付一次预处理代价，
    # 好过静默退化成"完全没有检索块"。嵌入服务异常（长时间没有进度）时提前放弃，行为同旧版。
    wait_s = float(CFG.get("embed_wait_s", 120))
    waited0 = time.time()
    last_missing, last_progress = None, time.time()
    while sess.chunks:
        sess.refresh_vectors()
        missing = sum(1 for c in sess.chunks if c.hash not in sess.vectors)
        with EMB_LOCK:
            qv = qv if qv is not None else EMB_CACHE.get(qh)
        if missing == 0 and qv is not None:
            break
        if missing != last_missing:
            last_missing, last_progress = missing, time.time()
        if time.time() - waited0 >= wait_s or time.time() - last_progress >= 15.0:
            break
        time.sleep(0.2)
    dt = time.time() - waited0
    if dt >= 2.0:
        log(f"embed wait: {len(sess.chunks) - (last_missing or 0)}/{len(sess.chunks)} blocks in {dt:.1f}s")
    if not sess.vectors:
        return "", 0, 0.0, [], {}          # 第 5 项是"检索明细"：没有就是空 dict（不是空 list）
    if qv is None:                              # 极端情况：块好了查询还没好（有界等待）
        deadline = time.time() + 10.0
        while qv is None and time.time() < deadline:
            time.sleep(0.1)
            with EMB_LOCK:
                qv = EMB_CACHE.get(qh)
        if qv is None:
            dbg("query embedding not ready - degrading")
            return "", 0, 0.0, [], {}          # 第 5 项是"检索明细"：没有就是空 dict（不是空 list）
    # 排除"已经在最近窗口里逐字保留"的内容：按**回合**排除（旧版按消息排除，回合被窗口从中间
    # 劈开时会把同一回合的另外半截又检索一遍——重复内容既费预算又诱导模型答两遍）。
    # 旧分块（structure_chunking=false）的块没有回合号（turn=-1），退回按消息序号排除。
    if CFG.get("structure_chunking", True):
        cands = [(c, s) for c, s in cosine_matrix(sess, qv) if c.turn not in recent_turns]
    else:                                   # 旧分块没有回合号，退回按消息序号排除
        cands = [(c, s) for c, s in cosine_matrix(sess, qv) if c.msg_idx not in recent_msg_idx]
    if not cands:
        return "", 0, 0.0, [], {}          # 第 5 项是"检索明细"：没有就是空 dict（不是空 list）
    cands.sort(key=lambda x: -x[1])
    cands = cands[: CFG["retrieve_candidates"]]
    top = cands[: CFG["rerank_candidates"]]
    rr = rerank(query, [c.text for c, _ in top])
    rerank_live = rr is not None
    if rr is None:
        # 重排器不可用 → 没有重排分（这里保持原行为：置 0 而不是拿余弦顶替）。
        # ⚠ 这会让总分退化成"最近优先"：早期事实即使余弦明显更高（实测 0.73 vs 0.43）也选不上。
        # 这是**行为选择**，不是记录功能的一部分，所以没有擅自改；现象与判定办法写在
        # ~/docs/services/rag-config-testing.md §7，记录页的「检索回来的块」能直接看出来。
        rr = [0.0] * len(top)
    n = len(sess.chunks)
    scored = []
    for (c, cos), r in zip(top, rr):
        recency = float(np.exp(-(n - c.idx) / max(32.0, n * 0.5)))
        s = CFG["score_w_rerank"] * r + CFG["score_w_recency"] * recency + CFG["score_w_role"] * role_weight(c.role)
        scored.append((c, s, cos, r))
    scored.sort(key=lambda x: -x[1])
    # MMR：贪心选，λ*相关分 - (1-λ)*与已选的最高相似度（用 chunk 自己的向量，防同一话题刷屏）
    lam = float(CFG["mmr_lambda"])
    picked: list[tuple[Chunk, float, float, float]] = []
    used_tokens = 0
    while scored and len(picked) < 512:   # 块上限：252K 窗口可容 ~460 块（768 tok/块），64 块只用到预算的 1/4
        best, best_v = None, -1e9
        for cand in scored:
            c, s, cos, r = cand
            sim_max = 0.0
            for c2, *_ in picked:
                v1, v2 = sess.vectors.get(c.hash), sess.vectors.get(c2.hash)
                if v1 is None or v2 is None:
                    continue
                sim_max = max(sim_max, float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9)))
            v = lam * s - (1 - lam) * sim_max
            if v > best_v:
                best, best_v = cand, v
        picked.append(best)
        scored.remove(best)
        used_tokens += est_tokens(best[0].text) + 24
        if used_tokens >= budget_tokens:
            break
    # 贪心填满预算（chosen 保持**优先级顺序**，渲染时才按原始次序排——砍块要砍优先级最低的）
    chosen, used = [], 0
    for c, s, cos, r in sorted([p for p in picked], key=lambda x: -x[1]):
        t = est_tokens(c.text) + 24
        if used + t > budget_tokens:
            continue
        chosen.append(c)
        used += t
    if not chosen:
        return "", 0, 0.0, [], {}          # 第 5 项是"检索明细"：没有就是空 dict（不是空 list）
    added = expand_context(chosen, sess, budget_tokens, used)
    used += sum(est_tokens(c.text) + 24 for c in added)
    if added:
        dbg(f"context expand: +{len(added)} sibling/anchor blocks ({used} tok)")
    # 每块一行的"检索结果明细"（**只给元信息**：哪一段历史、多长、各阶段分数；正文不在这里）。
    # 用途：出了事要能回答"这次到底把哪些历史喂进去了、为什么是这几块"。
    smap = {id(c): (s, cos, r) for c, s, cos, r in picked}
    chunks = []
    for c in list(chosen) + list(added):
        s, cos, r = smap.get(id(c), (None, None, None))
        chunks.append({"idx": c.idx, "turn": c.turn, "turn_label": c.turn_label, "role": c.role,
                       "kind": c.kind, "chars": len(c.text), "tokens": c.tokens,
                       "part": c.part, "n_parts": c.n_parts, "expanded": id(c) not in smap,
                       "score": round(s, 4) if s is not None else None,
                       "cos": round(cos, 4) if cos is not None else None,
                       "rerank": round(r, 4) if r is not None else None})
    detail = {"chunks": chunks, "candidates": len(top), "pool": len(sess.chunks),
              "reranked": bool(rerank_live and any(rr))}
    return render_block(chosen), used, time.time() - t0, chosen, detail


def assemble(sess: Session, body: dict, messages: list, total_tokens: int, up: str | None = None) -> tuple[dict, dict]:
    """超窗时的 RAG 组装。返回 (新的请求体, 组装信息)。"""
    gen_reserve = body.get("max_tokens") or CFG["gen_reserve_default"]
    gen_reserve = min(int(gen_reserve), CFG["gen_reserve_max"])
    budget = CFG["max_context"] - CFG["ctx_slack"] - gen_reserve

    # 1) 解析对话结构（回合）→ 最近逐字窗口（回合对齐）
    turns = parse_turns(messages)
    recent, recent_turns, recent_msg_idx, recent_tokens = recent_window(
        messages, turns, CFG["recent_window_tokens"])

    # 2) 固定头部：**系统提示词逐字保留，绝不进检索、绝不丢**——两种形态都要顾到：
    #    Anthropic 形态在 body["system"]，OpenAI 形态是 messages 里 role=system 的消息（旧版会把
    #    后者直接丢掉：recent_window 跳过 system，而组装只补 body["system"]）。
    system_msgs = [m for m in messages if classify(m)[0] == "system"]
    system = body.get("system")
    head_tokens = (est_tokens(str(system or ""))
                   + sum(est_tokens(message_text(m)) + 8 for m in system_msgs)
                   + est_tokens(json.dumps(body.get("tools") or [], ensure_ascii=False)))
    memory_budget = max(0, budget - head_tokens - recent_tokens - CFG["memory_min_tokens"])

    q = query_text(messages)
    info = {"budget": budget, "recent_tokens": recent_tokens, "memory_budget": memory_budget,
            "memory_tokens": 0, "retrieve_ms": 0.0, "reused_block": False,
            "turns_total": len(turns), "turns_recent": len(recent_turns),
            # 检索那边用的**查询串**本身：记录里只留长度 + 指纹（正文属于用户输入，不留在这儿），
            # 指纹用来判断"两次组装是不是同一个问题"。
            "query_chars": len(q or ""), "query_sha": sha1(q or "")[:12],
            "candidates": None, "pool": None, "reranked": None, "chosen_detail": None,
            "need_compact": None, "block_cov": None}

    # 3) 检索块：TTL 内且未压缩就复用（前缀缓存友好）
    fresh = (sess.stable_block and sess.stable_block_at
             and time.time() - sess.stable_block_at < CFG["assembly_ttl_s"])
    need_compact = memory_budget <= 0   # 最近窗口已吃满预算：必须重建（compaction）
    # 覆盖度判据：TTL 判定只看时间与窗口，**不看块对当前问题还有没有用**——提问阶段于是拿到灌入
    # 阶段为别的查询建好的旧块（实测同一份语料 TTL=3h 时召回从 6/6 掉到 0/6）。
    # 判据用**重排器**问一句："这个块里有没有真能回答当前问题的内容？"分数最高那块都答不上 → 重建。
    # 为什么不用嵌入余弦：句式相同的事实块它分不开（实测 0.70 vs 0.48，块里塞满填充后差距更小，
    # 于是"全历史更好 vs 块内最好"几乎永远不超阈值——实测 23 次组装只触发 1 次重建，召回 2/6）；
    # 重排器是交叉编码器，同一组文档实测 0.80 vs 0.0004。
    # 代价：每次超窗请求多一次重排调用（块内优先级最高的 ≤128 块、每块截 600 字符），
    # 换来的是"同话题追问照旧复用（前缀缓存不动），只有真漏了才付重建的钱"。
    chosen: list[Chunk] = list(sess.stable_chunks)
    if fresh and not need_compact and CFG.get("block_drift_rebuild") and chosen:
        docs = [c.text[: int(CFG.get("block_check_chars", 600))]
                for c in chosen[: int(CFG.get("block_check_docs", 128))]]
        sc = rerank(q, docs) if docs else None
        if sc:
            best = max(sc)
            info["block_cov"] = round(best, 3)
            if best < float(CFG.get("block_relevance_min", 0.30)):
                fresh = False
                COUNTERS["block_rebuilds"] += 1
                dbg(f"block rebuild: best relevance in block = {best:.4f}")
    info["need_compact"] = need_compact
    if fresh and not need_compact and est_tokens(sess.stable_block) <= memory_budget:
        block, bt = sess.stable_block, est_tokens(sess.stable_block)
        chosen = list(sess.stable_chunks)
        info["reused_block"] = True
        info["chosen_detail"] = sess.stable_detail       # 复用这一次，明细沿用建块那次的
        COUNTERS["assembly_reused"] += 1
    else:
        block, bt, ms, chosen, det = build_memory_block(sess, messages, q, memory_budget,
                                                       recent_turns, recent_msg_idx)
        info["retrieve_ms"] = ms
        info["chosen_detail"] = det
        if det:
            info["candidates"], info["pool"], info["reranked"] = det["candidates"], det["pool"], det["reranked"]
        if block:
            sess.stable_block, sess.stable_block_query, sess.stable_block_at = block, q, time.time()
            sess.stable_chunks = list(chosen)
            sess.stable_detail = det                       # 复用路径要能报出同一份明细
        COUNTERS["assembly_rebuilt"] += 1
    info["memory_tokens"] = bt
    info["memory_blocks"] = len(chosen)
    info["block_chars"] = len(block or "")
    sess.assemblies += 1

    # 4) 组装 messages：系统提示词 + 检索块 + 最近窗口（顺序即语义顺序）
    def build(blk: str) -> list:
        ms = list(system_msgs)                      # ① 系统提示词永远在最前
        if blk:
            ms.append({"role": "user", "content": blk})
            ms.append({"role": "assistant", "content": "(noted - using those earlier excerpts as reference)"})
        return ms + list(recent)

    new_messages = build(block)
    # 精确复核（估算可能有偏差）：超了就砍检索块——**按优先级砍整块**（chosen 是优先级序，砍尾部），
    # 不再像旧版那样把块文本按 0.7 比例从中间截一刀（那会把一个块、一行日志截断在半截上）。
    # 计数超时/不可用时退化为**上界估算**（见 est_prompt_tokens 的标定），宁可多砍也不让请求超窗被引擎
    # 400 拒（引擎从不截断超窗请求）。计数必须带上 tools/system（它们也进提示词）与图片展开。
    extra = {"system": body.get("system"), "tools": body.get("tools")}
    extra_text = str(body.get("system") or "") + json.dumps(body.get("tools") or [], ensure_ascii=False)

    def fit_count(msgs: list) -> tuple[int, bool]:
        """-> (这条组装结果进引擎会占多少 token, 数字是否来自引擎计数)。"""
        n = count_tokens(msgs, extra, up)
        if n is None:
            return est_prompt_tokens(msgs, extra_text), False
        return n + image_allowance(msgs), True

    for _ in range(4):
        exact, _ = fit_count(new_messages)
        if exact + count_margin(exact) <= budget:
            break
        if chosen:
            keep_n = max(1, int(len(chosen) * 0.7))
            dropped = len(chosen) - keep_n
            chosen = chosen[:keep_n]
            block = render_block(chosen)
            bt = sum(c.tokens + 24 for c in chosen)
            info["shrunk"] = f"-{dropped} blocks"
            new_messages = build(block)
        else:
            break
    else:
        block, bt, chosen = "", 0, []
        new_messages = build("")
        info["shrunk"] = "block dropped"
    # 5) 最后一道保险：丢块之后仍超预算（估算偏差 / 计数不可用 / 图片展开）时继续砍，**必须收敛到装得下**：
    #    先把最长的非 system 消息从头部砍（砍掉的头部内容仍在会话分块里，检索还能带回），
    #    没有可砍的大消息了就丢最近窗口里最旧的整条消息。引擎对超窗请求直接 400、从不截断，
    #    所以这里绝不能"砍不动就作罢"——那正是把 400 交回客户端的那条路。
    head_n = len(system_msgs) + (2 if block else 0)
    for _ in range(64):
        n, _ = fit_count(new_messages)
        if n + count_margin(n) <= budget:
            break
        i = max(range(len(new_messages)), key=lambda k: len(message_text(new_messages[k]))
                if new_messages[k].get("role") != "system" else -1, default=-1)
        if i >= 0 and len(message_text(new_messages[i])) >= 4096:
            keep = max(2048, int(len(message_text(new_messages[i])) * 0.6))
            new_messages = list(new_messages)
            new_messages[i] = dict(new_messages[i], content=trim_at_line_tail(message_text(new_messages[i]), keep))
            info["shrunk"] = True
            continue
        dropped = None
        for k in range(head_n, len(new_messages)):          # 丢最近窗口里最旧的一条
            if new_messages[k].get("role") != "system":
                dropped = k
                break
        if dropped is None:
            dbg("assemble: 砍无可砍仍超预算，交给窗口守卫压 max_tokens")
            break
        new_messages = new_messages[:dropped] + new_messages[dropped + 1:]
        info["shrunk"] = True
        info["recent_dropped"] = info.get("recent_dropped", 0) + 1
    info["memory_tokens"] = bt
    info["memory_blocks"] = len(chosen)
    # 组装结果按**引擎口径**落账：精确计数 + 图片展开；计数不可用时用**上界**估算。
    # 拿不准的数字一律往大算——它直接决定 clamp_max_tokens 给客户端留多少 max_tokens，
    # 往小算就会放行一个引擎必然 400 的请求（生产实测：网关记 219,021、引擎数到 231,775 → 400）。
    final, final_exact = fit_count(new_messages)
    info["final_tokens"] = final
    info["final_exact"] = final_exact
    new_body = dict(body)
    new_body["messages"] = new_messages
    return new_body, info


# ----------------------------------------------------------------------------- HTTP 服务


# ------------------------------------------------------------------ 对话云端同步
#
# 登录之后对话跟着**账号**走：换设备 / 换浏览器打开同一个服务，看到的是同一批会话。
# 存哪儿：优先 MinIO（跨 Pod 重建也在），没配置凭证就落到本机持久盘 /home/cloud/logs/chats/
#          —— 两者都是**服务端**，所以「多端同步」这件事本身不依赖 MinIO 是否配置好。
CHAT_STORE_DIR = os.environ.get("CHAT_STORE_DIR") or os.path.join(
    os.path.expanduser("~"), "logs", "chats")
CHAT_MAX_BYTES = 4 * 1024 * 1024          # 单账号上限：会话多了也不至于把桶塞爆


def _minio_client():
    """有 MinIO 凭证就返回客户端，否则 (None, None)。

    凭证来源统一走 infer_store.credentials()：先环境变量，再退回 ~/.minio.env —— 这个进程是被
    `su -l` 的登录 shell 链条拉起来的，平台注入的环境变量常常到不了它这儿（实测：日志里
    「minio 不可用」一次都没打过，因为压根没走到 import）。文件里那份是用户给的，稳定。
    """
    cli, bucket = infer_store.minio_client()
    if cli is None:
        return None, None
    try:
        if not cli.bucket_exists(bucket):
            cli.make_bucket(bucket)
    except Exception as e:
        log("minio 桶不可用，对话同步退回本机盘:", repr(e))
        return None, None
    return cli, bucket


def _chat_key(uid: str) -> str:
    return "chats/%s.json" % re.sub(r"[^A-Za-z0-9_.-]", "_", str(uid))


def chats_load(uid: str):
    """读这个账号的对话；没有就返回 {"chats": [], "activeId": None}。"""
    cli, bucket = _minio_client()
    if cli is not None:
        try:
            r = cli.get_object(bucket, _chat_key(uid))
            try:
                return json.loads(r.read().decode("utf-8"))
            finally:
                r.close()
                r.release_conn()
        except Exception:
            return {"chats": [], "activeId": None, "store": "minio"}
    path = os.path.join(CHAT_STORE_DIR, "%s.json" % _chat_key(uid).replace("chats/", ""))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"chats": [], "activeId": None, "store": "local"}


def chats_save(uid: str, data) -> str:
    """写回；返回实际用的存储（minio / local），界面上可以如实标出来。"""
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(raw) > CHAT_MAX_BYTES:
        raise ValueError("对话数据过大（上限 %d MB）" % (CHAT_MAX_BYTES // 1024 // 1024))
    cli, bucket = _minio_client()
    if cli is not None:
        from io import BytesIO
        cli.put_object(bucket, _chat_key(uid), BytesIO(raw), len(raw),
                       content_type="application/json")
        return "minio"
    os.makedirs(CHAT_STORE_DIR, exist_ok=True)
    tmp = os.path.join(CHAT_STORE_DIR, "%s.json.tmp" % uid)
    path = os.path.join(CHAT_STORE_DIR, "%s.json" % _chat_key(uid).replace("chats/", ""))
    with open(tmp, "wb") as f:                    # 先写临时文件再原子改名：写到一半断电不会留半截 JSON
        f.write(raw)
    os.replace(tmp, path)
    return "local"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "strata-rag-proxy/0.1"
    # StreamRequestHandler.setup 会拿它 settimeout；main() 里按配置覆盖（<=0 → None 关闭）。
    timeout = 180

    def log_message(self, fmt, *args):        # 静音默认访问日志（自己记有用的）
        dbg("http:", fmt % args)

    def handle_error(self, request, client_address):
        # 客户端断开/空闲 keep-alive 超时是常态（加了 timeout 后这类异常都从这里出）。
        # socketserver 默认会给 stderr 打整段 traceback——2026-11-01 那次 err.log 9MB 大半是它。
        exc = sys.exc_info()[1]
        if isinstance(exc, (TimeoutError, BrokenPipeError, ConnectionResetError)):
            return
        BaseHTTPRequestHandler.handle_error(self, request, client_address)

    # ---- helpers
    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send_json(self, code: int, obj: dict):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, code: int, title: str, detail: str, extra_headers: dict | None = None):
        body = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                f"<div style=\"font:16px/1.6 system-ui;max-width:520px;margin:18vh auto;text-align:center\">"
                f"<h2 style=\"margin:0 0 12px\">{title}</h2><p style=\"color:#666\">{detail}</p>"
                f"<p><a href=\"/\" style=\"color:#1a7cf8\">返回</a></p></div>").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict:
        """查询串 → 单值 dict。

        别把 parse_qs 的结果直接交给处理函数：它的值是**列表**，于是 state 成了 ['xxx']，
        进 set/dict 一查就 `TypeError: unhashable type: 'list'`，回调直接 502。
        OAuth 的参数本来就是单值，取第一个就行。（这个坑踩过。）
        """
        return {k: v[0] for k, v in urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.path).query, keep_blank_values=True).items()}

    def _send_redirect(self, location: str, cookies: list[str] | None = None):
        self.send_response(302)
        self.send_header("Location", location)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- 鉴权（**只管 AI 对话**）
    #
    # 规则由用户定：除了 AI 对话，其余功能**不登录就能用**；对话要么自带 token，要么登录。
    # 所以这里只拦真正会跑模型的那两个端点，别的路径一律放行。
    #
    # 但「放行」不等于不补 key：引擎的 /metrics 这些读接口自己也要鉴权，
    # 而监控页要求不登录可看 —— 于是读类端点由代理**代持 key**（见 _upstream_key）。
    # 这样既满足了需求，也没把 key 发到浏览器。
    _ALWAYS_OPEN = {"/", "/index.html", "/api-monitor", "/health", "/rag/health", "/rag/status",
                    "/favicon.ico", "/logo.svg", "/sprite.svg", "/tokens.css", "/components.css", "/app.css"}
    _ALWAYS_OPEN_PREFIX = ("/web/", "/fonts/")
    # 只读、不含用户输入、前端界面要用到的引擎端点：代持 key 放行
    _READ_OPEN = ("/metrics", "/props", "/slots", "/status", "/v1/status", "/mcp", "/settings",
                  "/v1/models", "/concurrency")

    # 回调路径。集市约定是 `/<产品>/oauth/callback`（iWiki、图床、sdpy 都这样），
    # 注册到 oauth2_apps 里的就是这条，所以它必须被拦下来、不能落到引擎上。
    OAUTH_CALLBACK = "/oauth/callback"

    def _is_auth_path(self, path: str) -> bool:
        return path.startswith("/auth/") or path == self.OAUTH_CALLBACK

    def _is_open(self, path: str) -> bool:
        return (path in self._ALWAYS_OPEN or path.startswith(self._ALWAYS_OPEN_PREFIX)
                or self._is_auth_path(path))

    def _needs_credential(self, path: str) -> bool:
        """唯一要凭据的就是「会跑模型」的那两个端点（和并发表登记的集合是同一个）。"""
        return path in _INFER_PATHS

    def _session(self) -> dict | None:
        """从 Cookie 里解出会话，没有/过期/签名不对都返回 None。"""
        if OAUTH is None:
            return None
        raw = ssecho_auth.session_cookie_value(self.headers.get("Cookie"))
        return OAUTH.session.verify(raw) if raw else None

    def _gate(self, path: str) -> bool:
        """放行返回 True；不放行就回 401 并返回 False，调用方必须直接 return。"""
        if OAUTH is None or self._is_open(path) or not self._needs_credential(path):
            return True
        if self._session() is not None or _authorized(self.headers):
            return True
        self._send_json(401, {"error": {"type": "not_logged_in", "code": "NOT_LOGGED_IN",
                                        "message": "对话需要先登录（或用 API token）"}})
        return False

    def _upstream_key(self, path: str) -> str:
        """转发时要补的引擎 key。返回空串表示不补、原样透传客户端的凭据。

        · 读类端点（监控页要用）→ 一律代持，这样不登录也能看监控；
        · 对话端点 → 只有**带会话**才代持 —— 这正是本次改动的目的：不再把 key 分发出去；
        · 其它路径（/load、/unload 这类）→ 不补，交给引擎自己按客户端的 key 判。
        """
        if OAUTH is not None and self._session() is not None:
            return API_KEY
        if path.split("?")[0] in self._READ_OPEN:
            return API_KEY
        return ""

    # ---- /auth/* 路由
    def _auth_get(self, path: str, query: dict):
        if OAUTH is None:
            self._send_json(503, {"error": {"message": "登录未配置"}})
            return
        if path == "/auth/config":
            if not OAUTH.cfg.configured():
                self._send_json(200, {"configured": False, "missing": OAUTH.cfg.missing()})
                return
            info, err = OAUTH.check_app()
            self._send_json(200, {"configured": True, "app_name": info.get("app_name") or "软工集市",
                                  "app_icon": info.get("app_icon") or "", "error": err or None})
            return
        if path == "/auth/me":
            claims = self._session()
            if claims is None:
                self._send_json(401, {"error": {"code": "NOT_LOGGED_IN", "message": "未登录"}})
                return
            self._send_json(200, {"user": {"id": claims.get("sub"), "name": claims.get("name"),
                                           "email": claims.get("email"), "avatar": claims.get("avatar")}})
            return
        if path == "/auth/login":
            if not OAUTH.cfg.configured():
                self._send_html(503, "登录未配置",
                                "服务端还没登记 OAuth 应用（缺 " + "、".join(OAUTH.cfg.missing()) + "）。"
                                "在管理员把应用登记进 oauth2_apps 之前，可以先在「关于 → 设置」里填 API token。")
                return
            self._send_redirect(OAUTH.authorize_url(OAUTH.create_state()))
            return
        if path in ("/auth/callback", self.OAUTH_CALLBACK):
            self._auth_callback(query)
            return
        if path == "/auth/logout":
            self._send_redirect("/", cookies=[OAUTH.clear_cookie()])
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def _auth_callback(self, query: dict):
        """IdP 跳回来：校验 state → code 换 token → 拉用户信息 → 签发会话 Cookie → 回首页。"""
        def fail(code, title, detail):
            log(f"oauth callback failed: {title} {detail}")
            self._send_html(code, title, detail)

        if query.get("error"):
            return fail(400, "授权被拒绝", str(query.get("error_description") or query["error"]))
        code, state = query.get("code"), query.get("state")
        if not code or not state:
            return fail(400, "回调参数不完整", "没拿到 code 或 state，请重新发起登录。")
        if not OAUTH.consume_state(state):          # 一次性消费 + HMAC 校验，防 CSRF
            return fail(400, "登录状态无效", "state 校验失败或已过期，请重新登录。")
        token, err = OAUTH.exchange_code(code)
        if err:
            return fail(401, "换取登录凭证失败", err)
        profile, err = OAUTH.userinfo(token)
        if err:
            return fail(401, "获取用户信息失败", err)
        claims = OAUTH.claims_of(profile)
        jwt = OAUTH.session.issue(claims)
        log(f"oauth login ok: {claims.get('name')} (id={claims.get('sub')})")
        self._send_redirect("/", cookies=[OAUTH.cookie(jwt)])


    def _proxy(self, method: str, path: str, body: bytes | None, extra_headers: dict | None = None,
               stream_back: bool = True, new_body: bytes | None = None, up: str | None = None,
               fitted: bool = False, model: str | None = None, entry: dict | None = None,
               failover: bool = True):
        """转发到上游并（默认）原样流回响应。多实例时 up 指定落哪一台（会话粘性）。

        推理请求（/v1/chat/completions、/v1/messages）在这里登记进并发表：**一处登记**，
        RAG 与直通两条路径都覆盖、超窗重试复用同一条目（entry 透传），不重复计数。"""
        url = (up or CFG["upstream"]) + path
        if entry is None and method == "POST" and path.split("?")[0] in _INFER_PATHS:
            entry = reg_open(path.split("?")[0], up or CFG["upstream"], model, self.headers)
        rec = getattr(self, "_rec", None)          # 推理记录（没有就是没开记录，全部是空操作）
        exc = None
        req = urllib.request.Request(url, data=new_body if new_body is not None else body, method=method)
        inject_key = self._upstream_key(path)
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection", "x-strata-session",
                             "cookie"):                      # Cookie 是本机会话，不往上游泄
                continue
            if inject_key and k.lower() in ("authorization", "x-api-key"):
                continue                                        # 会话优先：丢掉客户端自带的 key，用账号的
            req.add_header(k, v)
        if inject_key:
            req.add_header("x-api-key", inject_key)
        # 鉴权策略（本次改动）：
        #   · 带有效会话（集市登录）→ **由代理代持引擎 key**。这正是这个功能的目的：
        #     不再把 key 分发给所有人，普通用户登录即可用。
        #   · 没会话但自带 key → 原样透传（开发者直连、脚本压测都还能用）。
        #   · 两者都没有 → 上面的 _gate 已经拦掉了，走不到这里。
        # 代理自身的内部调用（count_tokens 等）在各自函数里显式带 key，不走这里。
        if new_body is not None:
            req.add_header("Content-Length", str(len(new_body)))
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)
        sent = False               # 是否已给客户端写过响应头：写过就不能再改投另一台上游
        # [SSE 2026-11-03] 首字节看门狗：引擎「端口还在听、进程已经卡死」时 TCP 探活和 /v1/status 都看不出来，
        # 请求会一直挂到客户端自己超时（实测 20 分钟，还把快道批槽占满）。这里给「等到上游第一个字节」
        # 设上限；超时走 URLError 分支，按不可达改投兄弟道（还没给客户端写过字节）。头 + 首个 body 块
        # 用这个上限，之后放宽回 600 s —— SSE 的正常安静期不算失败。
        ttfb = float(CFG.get("first_byte_timeout_s") or 300)
        try:
            t_fwd = time.perf_counter()
            with urllib.request.urlopen(req, timeout=ttfb) as r:
                # [SSE 2026-11-05] xiaoqiu（vLLM）非流式：按  拆分 reasoning_content
                try:
                    xq = str(CFG.get("model_routes", {}).get("xiaoqiu") or "").rstrip("/")
                    ct = r.headers.get("Content-Type") or ""
                    req_stream = bool(json.loads(body).get("stream")) if body else False
                    if xq and (up or "").rstrip("/") == xq and not req_stream and "application/json" in ct:
                        raw = r.read()
                        if rec:
                            infer_store.capture(rec, raw)
                            infer_store.note_route(rec, stream=False, http_status=r.status)
                        obj = json.loads(raw)
                        obj = split_reasoning_content(obj)
                        self._send_json(200, obj)
                        sent = True
                        return
                    if xq and (up or "").rstrip("/") == xq and req_stream and "event-stream" in ct:
                        if rec:
                            infer_store.note_route(rec, stream=True, http_status=r.status)
                        stream_xiaoqiu_reasoning(self, r, rec)
                        sent = True
                        return
                except Exception as _e:
                    log(f"xiaoqiu reasoning split skipped: {_e!r}")
                if entry is not None:
                    entry["forward_at"] = time.time()
                    entry["state"] = "forwarding"
                    entry["stream"] = "event-stream" in (r.headers.get("Content-Type") or "")
                if rec:
                    infer_store.note_route(rec, stream="event-stream" in (r.headers.get("Content-Type") or ""),
                                           http_status=r.status)
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    if k.lower() == "content-type" and "charset" not in v.lower() and (
                            v.lower().startswith("text/") or v.lower().startswith("application/json")):
                        v = v + "; charset=utf-8"     # 缺 charset 时客户端可能按 Latin-1 解码 → 中文乱码
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                sent = True
                if not stream_back:
                    return
                first = True
                while True:
                    # read1: http.client 的 read(n) 对 chunked 响应会攒满 n 字节才返回（SSE 每事件 ~150B，
                    # 8KB≈300ms 延迟！）；read1 有数据即回，逐事件转发
                    chunk = r.read1(8192)
                    if not chunk:
                        break
                    if first:
                        t0 = getattr(self, "_t0", t_fwd)
                        dbg(f"timing: forward {(t_fwd - t0) * 1000:.0f}ms, upstream headers "
                            f"{(time.perf_counter() - t_fwd) * 1000:.0f}ms, first chunk "
                            f"{(time.perf_counter() - t0) * 1000:.0f}ms after request")
                        if entry is not None:
                            entry["first_chunk_at"] = entry["first_chunk_at"] or time.time()
                            entry["state"] = "streaming"
                        if rec and not rec["first_chunk_at"]:
                            rec["first_chunk_at"] = entry["first_chunk_at"] if entry else time.time()
                        first = False
                        try:                       # [SSE 2026-11-03] 首块已到：读超时放宽回 600 s
                            r.fp.raw._sock.settimeout(600)
                        except Exception:          # noqa: BLE001  实现细节变了就退回默认（不影响转发）
                            pass
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if rec:
                        infer_store.capture(rec, chunk)   # 只是 memcpy 进缓冲；解析留到后台
                    if entry is not None:          # 并发监控：只做字节计数，不解析响应体
                        entry["bytes_out"] += len(chunk)
                        # 流式：一个 `data:` 事件 ≈ 一个 token（实测 28 块 ≈ 24 token 的回答），
                        # 比按字节估准得多（后者把 SSE 的 JSON 开销也算成内容，短回答能差百倍）；
                        # 非流式：拿不到事件数，仍按字节 / tok 估。
                        entry["events"] += chunk.count(b"data:")
                        entry["est_tokens"] = entry["events"] if entry["stream"] else \
                            int(entry["bytes_out"] / max(1.0, CFG["chars_per_token"]))
                        entry["last_activity"] = time.time()
        except urllib.error.HTTPError as e:
            exc = e
            payload = e.read()
            raw_payload = payload
            # 窗口守卫的最后一道网：网关的计数与引擎自己数到的 prompt 长度之间总有残差（图片展开、
            # 缓存里的旧计数、Anthropic/OpenAI 模板差）。残差把请求放到窗口外时，引擎报的 400 里
            # 已经写了"这里最多还能给多少 max_tokens"——照它压一次重发，客户端就不该看见这个 400。
            # 但"提示词本身就超窗"另说：压 max_tokens 救不了它，重试只是白烧一个上游来回，还会把
            # serve 写给运维看的内部文案（#545）原样漏给终端用户——2026-10-19 书稿会话的三连 400。
            info = overfit_info(payload) if e.code == 400 else None
            if info is not None:
                window, prompt, room = info.get("window"), info.get("prompt"), info.get("room")
                prompt_over = window is not None and (
                    (prompt is not None and prompt + CFG["ctx_slack"] >= window)
                    or (room is not None and room < 1))
                if not prompt_over and not fitted and CFG.get("retry_on_overfit"):
                    sent = new_body if new_body is not None else body
                    if room is not None and sent:
                        patched = patch_max_tokens(sent, room)
                        if patched is not None:
                            COUNTERS["overfit_retries"] += 1
                            infer_store.note_retry(rec)
                            log(f"引擎 400 超窗：按引擎报的余量 {room} 压 max_tokens 后重试一次")
                            return self._proxy(method, path, body, extra_headers, stream_back, patched, up, True,
                                               model=model, entry=entry)
                if fitted:
                    COUNTERS["overfit_failures"] += 1
                    log("引擎重试后仍报超窗：提示词本身就超出窗口，压 max_tokens 救不了")
                elif prompt_over:
                    COUNTERS["overfit_failures"] += 1
                    log(f"引擎 400 超窗且提示词本身出界（实测 prompt {prompt} / 窗口 {window}）：不重试")
                payload = json.dumps(overfit_error(info), ensure_ascii=False).encode("utf-8")
            if rec:                                # 引擎原文留档（错误分析要看它），发客户端的是改写后的
                infer_store.capture(rec, raw_payload)
                infer_store.note_route(rec, http_status=e.code)
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (urllib.error.URLError, TimeoutError) as e:
            # 连不上上游（拒绝/超时/DNS）。此时还没给客户端发过任何字节 → 可以换一台活着的 lane 重试一次。
            # 典型场景：换引擎重启期间，被 model_routes 钉到这条 lane 的请求；以前直接 502。
            # [SSE 2026-11-03] 读超时（首字节看门狗）在 py3.12 里是 TimeoutError，不会被 urllib 包成
            # URLError —— 只写 URLError 的话看门狗虽然掐断了连接，却走不到改投。
            cur = (up or CFG["upstream"]).rstrip("/")
            alt = failover_upstream(cur) if (failover and not sent) else None
            if alt is not None:
                COUNTERS["upstream_failover"] += 1
                log(f"upstream {cur} 不可达（{e!r}），改投 {alt}")
                if entry is not None:
                    entry["upstream"] = alt
                return self._proxy(method, path, body, extra_headers, stream_back, new_body, alt,
                                   fitted, model=model, entry=entry, failover=False)
            exc = e
            COUNTERS["upstream_errors"] += 1
            log("upstream error:", repr(e))
            self._send_json(502, {"error": {"message": f"rag proxy: upstream failed: {e!r}"}})
        except Exception as e:
            exc = e
            COUNTERS["upstream_errors"] += 1
            log("upstream error:", repr(e))
            self._send_json(502, {"error": {"message": f"rag proxy: upstream failed: {e!r}"}})
        finally:
            # 并发监控：把条目结掉（客户端断开 vs 上游出错分开记）。重试路径已结过 → 幂等跳过。
            if entry is not None:
                if exc is None:
                    reg_close(entry, "completed")
                    state = "completed"
                elif isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                    reg_close(entry, "disconnected")
                    state = "disconnected"
                else:
                    reg_close(entry, "error", repr(exc))
                    state = "error"
                # 推理记录的状态跟着并发登记走（同一个事实，不重复判定）
                infer_store.note_route(rec, state=state, error=(repr(exc) if exc else None))

    def _guard_body(self, body: bytes | None) -> bytes | None:
        """RAG 逻辑没跑成时的最低守卫：用上界估算压一次 max_tokens。压不动（装得下）就返回 None 原样转发。"""
        if not body:
            return None
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            return None
        msgs = req.get("messages") if isinstance(req, dict) else None
        if not isinstance(msgs, list) or not msgs:
            return None
        extra_text = str(req.get("system") or "") + json.dumps(req.get("tools") or [], ensure_ascii=False)
        est = est_prompt_tokens(msgs, extra_text)
        req2, clamped = clamp_max_tokens(req, est)
        if not clamped:
            return None
        log_ctx(req2, est, "透传(降级·上界估算)", clamped=True)
        return json.dumps(req2, ensure_ascii=False).encode("utf-8")

    # ---- face service (independent process on loopback, public path /face/*)
    #
    # The face API has its own API-key system and must NOT inherit the LLM auth/RAG/window
    # logic.  Route it before _gate and before any body parsing so images pass through
    # untouched.  Everything is streamed back; only the hop-by-hop headers are dropped.
    _FACE_PREFIX = "/face"

    def _is_face_path(self) -> bool:
        p = self.path.split("?")[0]
        return p == self._FACE_PREFIX or p.startswith(self._FACE_PREFIX + "/")

    def _read_body_limited(self, max_bytes: int) -> bytes | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > max_bytes:
            self._send_json(413, {"ok": False, "error": {
                "code": "payload_too_large", "message": f"body exceeds {max_bytes} bytes"}})
            return None
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._send_json(411, {"ok": False, "error": {
                "code": "length_required", "message": "Content-Length is required for /face requests"}})
            return None
        return self.rfile.read(n) if n else b""

    def _face_proxy(self, method: str):
        max_body = int(CFG.get("face_max_body_mb", 64)) * 1024 * 1024
        body = self._read_body_limited(max_body) if method in ("POST", "PUT", "PATCH", "DELETE") else b""
        if body is None:
            return
        up = str(CFG.get("face_upstream") or "http://127.0.0.1:8095").rstrip("/")
        req = urllib.request.Request(up + self.path, data=(body or None), method=method)
        allowed = ("content-type", "accept", "accept-language", "authorization", "x-api-key",
                   "x-admin-key", "x-request-secret", "idempotency-key", "x-request-id", "user-agent",
                   "origin", "access-control-request-method", "access-control-request-headers")
        for k, v in self.headers.items():
            if k.lower() in allowed:
                req.add_header(k, v)
        # The face service binds loopback and trusts this header for rate limiting/audit.
        prior = self.headers.get("X-Forwarded-For", "")
        client = self.client_address[0] if self.client_address else ""
        req.add_header("X-Forwarded-For", (prior + ", " + client).strip(", ") if prior else client)
        req.add_header("X-Forwarded-Proto", "https")
        req.add_header("X-Forwarded-Host", self.headers.get("Host", ""))
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            COUNTERS["face_requests"] = COUNTERS.get("face_requests", 0) + 1
        except urllib.error.HTTPError as e:
            # Face service errors are JSON; pass them through verbatim.
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            for k, v in e.headers.items():
                if k.lower() not in ("transfer-encoding", "connection", "content-length", "content-type"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001 - never take the LLM gateway down for /face
            log(f"face proxy error {method} {self.path.split('?')[0]}: {exc!r}")
            self._send_json(502, {"ok": False, "error": {
                "code": "face_upstream_unavailable",
                "message": "face service is unavailable; retry shortly"}})
        finally:
            dbg(f"face: {method} {self.path.split('?')[0]} "
                f"{(time.perf_counter() - t0) * 1000:.0f}ms")



    # ---- 工作空间 / 工具（都要登录会话或 API token；按用户隔离目录）
    def _ws_uid(self) -> str:
        claims = self._session()
        if claims is not None:
            sub = re.sub(r"[^A-Za-z0-9_.-]", "_", str(claims.get("sub") or "anon"))[:48]
            return "u-" + sub
        raw = (self.headers.get("X-Api-Key") or self.headers.get("Authorization") or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        return "k-" + hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else "anon"

    def _hub_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _hub_route(self, method: str):
        # 图片直链：<img> 不带 Authorization，用带过期的签名代替登录态（其余端点照常要用户）
        if method == "GET" and self.path.split("?")[0] == "/workspace/image":
            q0 = self._query()
            try:
                exp = int(q0.get("exp") or 0)
            except ValueError:
                exp = 0
            uid0, name0 = q0.get("uid") or "", q0.get("name") or ""
            if exp > time.time() and uid0 and name0 and q0.get("sig") == _ws_img_sig(uid0, name0, exp):
                try:
                    g = _ws_root() / _ws_safe(uid0) / "images" / _ws_safe(name0)
                except ValueError:
                    g = None
                payload = g.read_bytes() if (g is not None and g.exists()) else None
                if payload is None:
                    self._send_json(404, {"error": {"message": "没有这张图"}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(payload)
                return
        if not self._require_user():
            return
        uid = self._ws_uid()
        path = self.path.split("?")[0]
        q = self._query()
        try:
            if method == "GET":
                if path == "/workspace":
                    self._send_json(200, ws_meta(uid))
                    return
                if path == "/workspace/envs":
                    self._send_json(200, {"envs": [{k: e.get(k) for k in ("id", "label", "note", "ext", "compile", "lab")}
                                                   for e in SANDBOX["envs"]]})
                    return
                if path == "/workspace/kb":
                    self._send_json(200, kb_list(uid))
                    return
                if path == "/workspace/job":
                    self._send_json(200, job_status(uid, q.get("id") or "", int(q.get("offset") or 0)))
                    return
                if path == "/workspace/jobs":
                    self._send_json(200, {"jobs": job_list(uid)})
                    return
                if path == "/workspace/search":
                    self._send_json(200, {"q": q.get("q") or "", "hits": ws_search(uid, q.get("q") or "")})
                    return
                if path == "/workspace/tree":
                    self._send_json(200, {"user": uid, "entries": ws_tree(uid)})
                    return
                if path == "/workspace/file":
                    f = ws_path(uid, q.get("path") or "")
                    if not f.is_file():
                        self._send_json(404, {"error": {"message": "没有这个文件"}})
                        return
                    raw = f.read_bytes()
                    ext = f.suffix.lower()
                    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".docx", ".xlsx",
                               ".xls", ".pptx", ".zip", ".gz", ".bin"):
                        mime = {
                            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".webp": "image/webp", ".gif": "image/gif",
                            ".pdf": "application/pdf",
                            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            ".xls": "application/vnd.ms-excel",
                            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                            ".zip": "application/zip", ".gz": "application/gzip",
                        }.get(ext, "application/octet-stream")
                        self._send_json(200, {"path": q.get("path"), "mime": mime,
                                              "b64": base64.b64encode(raw).decode()})
                    else:
                        self._send_json(200, {"path": q.get("path"), "text": raw.decode("utf-8", "replace")})
                    return
                if path == "/tool/status":
                    self._tool_get(path)
                    return
                if path == "/workspace/skill":
                    f = ws_dir(uid) / "skills" / _ws_safe(q.get("name") or "")
                    if not f.exists():
                        self._send_json(404, {"error": {"message": "没有这个 skill"}})
                        return
                    self._send_json(200, {"name": f.stem, "content": f.read_text(encoding="utf-8", errors="replace")})
                    return
                if path == "/workspace/image":
                    f = ws_dir(uid) / "images" / _ws_safe(q.get("name") or "")
                    if not f.exists():
                        self._send_json(404, {"error": {"message": "没有这张图"}})
                        return
                    payload = f.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "private, max-age=86400")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self._send_json(404, {"error": {"message": "未知的工作空间端点"}})
                return

            body = self._hub_body()

            if method == "POST":
                if path == "/workspace/skill":
                    name = _ws_safe(body.get("name") or "")
                    content = str(body.get("content") or "")
                    if len(content) > _SKILL_MAX:
                        self._send_json(413, {"error": {"message": f"skill 超过 {_SKILL_MAX} 字节"}})
                        return
                    if name.endswith(".md"):
                        name = name[:-3]
                    f = ws_dir(uid) / "skills" / (name + ".md")
                    with WS_LOCK:
                        f.write_text(content, encoding="utf-8")
                    self._send_json(200, {"ok": True, "name": name})
                    return
                if path == "/workspace/run":
                    self._send_json(200, run_code(
                        uid, str(body.get("env") or "python"), str(body.get("code") or ""),
                        float(body.get("timeout") or 30), files=body.get("files"),
                        stdin=str(body.get("stdin") or ""),
                        in_place=bool(body.get("in_place")),
                        cwd_rel=body.get("cwd") or None,
                        force=bool(body.get("force"))))
                    return
                if path == "/workspace/lab":
                    d = ensure_lab(uid, force=bool(body.get("reset")),
                                   backup=bool(body.get("backup", True)))
                    self._send_json(200, {"ok": True, "dir": lab_cfg()["dirname"], "path": str(d)})
                    return
                if path == "/workspace/kb":
                    self._send_json(200, kb_add(uid, str(body.get("name") or ""),
                                                text=body.get("text"), path=body.get("path"),
                                                url=body.get("url")))
                    return
                if path == "/workspace/kb/search":
                    self._send_json(200, kb_search(uid, str(body.get("q") or ""),
                                                   int(body.get("k") or 6),
                                                   rerank=bool(body.get("rerank", True))))
                    return
                if path == "/workspace/job":
                    self._send_json(200, job_start(
                        uid, str(body.get("env") or "lab"), body.get("args") or body.get("cmd") or [],
                        files=body.get("files"), cwd_rel=body.get("cwd") or None,
                        timeout_s=float(body.get("timeout") or 1800), force=bool(body.get("force"))))
                    return
                if path == "/workspace/job/stop":
                    self._send_json(200, job_stop(uid, str(body.get("id") or "")))
                    return
                if path == "/workspace/file":
                    rel = str(body.get("path") or "")
                    f = ws_path(uid, rel)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    if body.get("b64"):
                        f.write_bytes(base64.b64decode(str(body["b64"])[:24 * 1024 * 1024]))
                    else:
                        f.write_text(str(body.get("text") or ""), encoding="utf-8")
                    self._send_json(200, {"ok": True, "path": rel})
                    return
                if path == "/workspace/mkdir":
                    ws_path(uid, str(body.get("path") or "")).mkdir(parents=True, exist_ok=True)
                    self._send_json(200, {"ok": True})
                    return
                if path == "/workspace/mcp":
                    name = _ws_safe(body.get("name") or "")
                    url = str(body.get("url") or "").strip()
                    if not url.startswith(("http://", "https://")):
                        self._send_json(400, {"error": {"message": "MCP 只支持 http/https 的远程地址"}})
                        return
                    with WS_LOCK:
                        servers = [s for s in ws_mcp(uid) if s.get("name") != name]
                        servers.append({"name": name, "url": url,
                                        "headers": body.get("headers") or {}})
                        ws_write_mcp(uid, servers)
                    self._send_json(200, {"ok": True})
                    return
                if path == "/workspace/image":
                    data = str(body.get("data") or "")
                    if "," in data and data.startswith("data:"):
                        data = data.split(",", 1)[1]
                    payload = base64.b64decode(data[:16 * 1024 * 1024])
                    name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
                    (ws_dir(uid) / "images" / name).write_bytes(payload)
                    self._send_json(200, {"ok": True, "name": name, "url": ws_image_url(uid, name)})
                    return
                if path == "/tool/search":
                    self._send_json(200, tool_search(str(body.get("q") or ""), int(body.get("n") or 5)))
                    return
                if path == "/tool/fetch":
                    self._send_json(200, tool_fetch(str(body.get("url") or ""), int(body.get("max_chars") or 8000)))
                    return
                if path == "/tool/image_search":
                    self._send_json(200, tool_image_search(str(body.get("q") or ""), int(body.get("n") or 6)))
                    return
                if path == "/tool/fetch_image":
                    self._send_json(200, tool_fetch_image(str(body.get("url") or "")))
                    return
                if path == "/tool/mcp/list":
                    self._send_json(200, tool_mcp_list(uid))
                    return
                if path == "/tool/mcp/call":
                    res = tool_mcp_call(uid, str(body.get("server") or ""), str(body.get("tool") or ""),
                                        body.get("args") or {})
                    self._send_json(200, res)
                    return

            if method == "DELETE":
                if path == "/workspace/path":
                    f = ws_path(uid, q.get("path") or "")
                    if f.is_dir():
                        import shutil as _sh
                        _sh.rmtree(f, ignore_errors=True)
                    elif f.exists():
                        f.unlink()
                    self._send_json(200, {"ok": True})
                    return
                if path == "/workspace/skill":
                    f = ws_dir(uid) / "skills" / (_ws_safe(q.get("name") or "") + ".md")
                    if f.exists():
                        f.unlink()
                    self._send_json(200, {"ok": True})
                    return
                if path == "/workspace/kb":
                    self._send_json(200, kb_delete(uid, q.get("id") or ""))
                    return
                if path == "/workspace/mcp":
                    name = _ws_safe(q.get("name") or "")
                    with WS_LOCK:
                        ws_write_mcp(uid, [s for s in ws_mcp(uid) if s.get("name") != name])
                    self._send_json(200, {"ok": True})
                    return

            self._send_json(404, {"error": {"message": "未知端点"}})
        except ValueError as e:
            self._send_json(400, {"error": {"message": str(e)}})
        except Exception as e:  # noqa: BLE001
            log(f"hub route error {method} {path}: {e!r}")
            self._send_json(502, {"error": {"message": "调用失败", "detail": repr(e)[:200]}})

    # ---- 服务端 Agent Runner：提交/断线重连/停止
    def _agent_run_post(self):
        if not self._require_user():
            return
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body 需要是对象")
            rid = agent_start(self._ws_uid(), str(body.get("chat_id") or ""),
                              str(body.get("model") or "qwen3.8-flash"),
                              body.get("messages") or [], int(body.get("max_rounds") or AGENT_MAX_ROUNDS),
                              body.get("params") or {})
            self._send_json(200, {"run_id": rid, "status": "running"})
        except ValueError as e:
            self._send_json(400, {"error": {"message": str(e)}})
        except Exception as e:  # noqa: BLE001
            log(f"agent run start error: {e!r}")
            self._send_json(502, {"error": {"message": "agent 启动失败", "detail": repr(e)[:200]}})

    def _agent_stream_get(self):
        if not self._require_user():
            return
        q = self._query()
        rid = str(q.get("run_id") or "")
        try:
            cursor = int(q.get("cursor") or 0)
        except ValueError:
            cursor = 0
        run = agent_get(rid)
        if not run:
            self._send_json(404, {"error": {"message": "没有这个 agent run"}})
            return
        if run.get("uid") and run["uid"] != self._ws_uid():
            self._send_json(403, {"error": {"message": "无权查看这个 run"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        last = max(0, cursor)
        try:
            while True:
                evs = agent_events_after(run, last)
                for ev in evs:
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last = int(ev.get("seq") or last)
                if run.get("status") != "running" and not evs:
                    break
                cond = run.get("cond")
                if cond is not None:
                    with cond:
                        cond.wait(timeout=0.5)
                else:
                    time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.close_connection = True

    def _agent_stop_post(self):
        if not self._require_user():
            return
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
            rid = str((body or {}).get("run_id") or "")
        except Exception:
            rid = ""
        run = agent_get(rid)
        if not run:
            self._send_json(404, {"error": {"message": "没有这个 agent run"}})
            return
        if run.get("uid") and run["uid"] != self._ws_uid():
            self._send_json(403, {"error": {"message": "无权停止这个 run"}})
            return
        cancel = run.get("cancel")
        if cancel is not None and run.get("status") == "running":
            cancel.set()
            run["status"] = "cancelled"
            _agent_emit(run, "cancelled")
            _agent_write_meta(run)
        self._send_json(200, {"ok": True, "run_id": rid})

    # ---- 胡桃 TTS（/tts/say = 本地 worker，/tts/* 其余仍然透传给 Voice Studio）
    def _tts_route(self, method: str):
        if not self._require_user():
            return
        path = self.path.split("?")[0]
        if method == "GET" and path == "/tts/voices":
            v = tts_cfg()["voice"]
            self._send_json(200, {"voices": [{"id": v.get("id") or "hutao",
                                              "name": v.get("name") or "胡桃",
                                              "description": "训练好的胡桃音色（Voice Studio run 122）",
                                              "default": True}]})
            return
        if method == "GET" and path == "/tts/health":
            cfg = tts_cfg()
            ok = False
            try:
                with urllib.request.urlopen(cfg["local_url"] + "/", timeout=3) as r:
                    ok = r.status < 500
            except urllib.error.HTTPError as e:
                ok = e.code < 500
            except Exception:
                ok = False
            self._send_json(200 if ok else 503, {"ok": ok, "voice": cfg["voice"].get("name"),
                                                 "url": cfg["local_url"]})
            return
        if method == "POST" and path == "/tts/say":
            body = self._hub_body()
            text = str(body.get("text") or "")
            if not text.strip():
                self._send_json(400, {"error": {"message": "没有要朗读的文字"}})
                return
            try:
                data = tts_say(text, float(body.get("speed") or 1.0))
            except ValueError as e:
                self._send_json(502, {"error": {"message": str(e)}})
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            return
        self._send_json(404, {"error": {"message": "未知的 TTS 端点"}})

    # ---- 体验页的通用反向代理（/spark/* → Spark 网关，/tts/* → Voice Studio）
    #
    # 浏览器到不了集群内网（10.42.x）也不能持有 Spark 的 key，所以由网关代持。
    # 和 /face 一样：先鉴权（登录会话或 API token），再原样流式转发。
    def _require_user(self) -> bool:
        if self._session() is not None or _authorized(self.headers):
            return True
        self._send_json(401, {"error": {"code": "NOT_LOGGED_IN",
                                        "message": "这个接口需要登录或 API token"}})
        return False

    def _passthrough(self, method: str):
        if not self._require_user():
            return
        p = self.path.split("?")[0]
        if p == "/spark" or p.startswith("/spark/"):
            kind = "spark"
            rest = self.path[len("/spark"):]
            base = str((CFG.get("spark") or {}).get("base_url") or "https://spark.ssemarket.cn").rstrip("/")
        else:
            kind = "tts"
            rest = self.path[len("/tts"):]
            base = str((CFG.get("tts") or {}).get("base_url")
                       or DEFAULTS["tts"]["base_url"]).rstrip("/")
        url = base + (rest or "/")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n and method in ("POST", "PUT", "PATCH") else b""
        req = urllib.request.Request(url, data=body or None, method=method)
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length", "connection", "cookie",
                             "authorization", "x-api-key", "x-voice-key"):
                continue
            req.add_header(k, v)
        if kind == "spark":
            key = spark_key()
            if key:
                req.add_header("Authorization", "Bearer " + key)
        else:
            # Voice Studio 的 key 属于使用者自己：由前端放 X-Voice-Key，代理不改名、不落盘
            vk = self.headers.get("X-Voice-Key") or ""
            if vk:
                req.add_header("X-API-Key", vk)
        if body:
            req.add_header("Content-Length", str(len(body)))
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            COUNTERS["hub_" + kind] = COUNTERS.get("hub_" + kind, 0) + 1
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001 - 体验页挂了不能带走对话网关
            log(f"passthrough {kind} error {p}: {exc!r}")
            self._send_json(502, {"error": {"message": f"{kind} upstream unavailable", "detail": repr(exc)[:160]}})
        finally:
            dbg(f"hub {kind}: {method} {p} {(time.perf_counter() - t0) * 1000:.0f}ms")

    # ---- agent 工具（前端 agent 模式走这里；模型拿到的结果是纯 JSON）
    def _tool_get(self, path: str):
        if not self._require_user():
            return
        q = self._query()
        if path == "/tool/status":
            lanes = []
            with LANE_LOCK:
                cache = {u: dict(v) for u, v in LANE_CACHE.items()}
            for u in upstreams():
                c = cache.get(u) or {}
                st = c.get("status") or {}
                lanes.append({"url": u, "label": lane_label(u), "up": bool(st) and not c.get("error"),
                              "model": st.get("model"), "loaded": st.get("loaded"),
                              "max_context": st.get("max_context"),
                              "in_flight": (st.get("activity") or {}).get("in_flight")})
            self._send_json(200, {"time": time.time(), "lanes": lanes,
                                  "services": services_snapshot(), "gpus": gpu_snapshot(),
                                  "spark": spark_snapshot()})
            return
        # /tool/search：GET ?q= 或 POST {"q": ...}
        text = q.get("q") or ""
        if not text:
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    text = (json.loads(self.rfile.read(n).decode("utf-8")) or {}).get("q") or ""
                except Exception:
                    text = ""
        try:
            self._send_json(200, tool_search(text, int(q.get("n") or 5)))
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": {"message": "search failed", "detail": repr(exc)[:200]}})

    # ---- routes
    def do_GET(self):
        # HTTP/1.1 keep-alive 下同一个 Handler 实例会连着服务多个请求，而 self._rec 是实例属性 ——
        # 不在这里清掉，上一条对话的记录会被后面的（比如 /metrics）继续写进去。必须按请求复位。
        self._rec = None
        if self._is_face_path():
            self._face_proxy("GET")
            return
        path0 = self.path.split("?")[0]
        if self._is_auth_path(path0):
            self._auth_get(path0, self._query())
            return
        if not self._gate(path0):
            return
        if self.path.split("?")[0] in ("/health", "/rag/health", "/rag/status"):
            self._send_json(200, health())
            return
        if self.path.split("?")[0] == "/concurrency":
            # 并发监控数据（前端 Monitor 页）：各通道引擎状态 + 代理侧在途/最近请求。
            # 必须在这里拦截、绝不落到引擎——引擎只有单路视角，而且按路径哈希会落到另一条通道。
            # 鉴权不在这里再判一次：上面 _gate 已经保证了「有会话或有 key」。
            # （原来这里还有一层只认 key 的检查，那会把登录用户挡在外面——他们本来就不带 key。）
            self._send_json(200, concurrency())
            return
        if self.path.split("?")[0] == "/agent/stream":
            self._agent_stream_get()
            return
        if self.path.split("?")[0].startswith(("/tool/", "/workspace")):
            self._hub_route("GET")
            return
        if self.path.split("?")[0] in ("/tts/say", "/tts/voices", "/tts/health"):
            self._tts_route("GET")
            return
        if self.path.split("?")[0].startswith(("/spark", "/tts")):
            self._passthrough("GET")
            return
        if self.path.split("?")[0] == "/chats":
            # 对话同步：只认**登录会话**（没有账号就谈不上「多端」），没登录就明说
            claims = self._session()
            if claims is None:
                self._send_json(401, {"error": {"code": "NOT_LOGGED_IN", "message": "对话同步需要登录"}})
                return
            data = chats_load(str(claims.get("sub") or "anon"))
            self._send_json(200, data)
            return
        if self.path.split("?")[0].startswith("/infer/"):
            # 推理记录（前端「记录」页）。必须在这里拦下——落到引擎上就是 404。
            self._infer_get(self.path.split("?")[0], self._query())
            return
        if self.path.split("?")[0] == "/v1/models":
            routes = CFG.get("model_routes") or {}
            now = int(time.time())
            self._send_json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": now,
                 "owned_by": "strata", "lane": "fast" if str(u).endswith(":8091") else "normal"}
                for m, u in routes.items()]})
            return
        self._proxy("GET", self.path, None, up=pick_upstream(self.headers.get("X-Strata-Session") or self.path))

    # ---- 推理记录（只读）
    #
    # 元信息列表/统计：登录会话**或** API token 都能看（脚本做实验要能拉数据）。
    # **内容**（/infer/raw）多一道：只认登录会话 —— 它是用户输入与模型回复的原文，
    # 界面上不展示，只为错误分析留的口子。
    def _infer_get(self, path: str, q: dict):
        def need_login():
            self._send_json(401, {"error": {"code": "NOT_LOGGED_IN",
                                            "message": "推理记录需要登录（或用 API token）"}})

        if path == "/infer/raw":
            if self._session() is None:
                need_login()
                return
            d = infer_store.raw(q.get("id") or "")
            self._send_json(200 if d else 404, d or {"error": {"message": "没有这条记录"}})
            return
        if self._session() is None and not _authorized(self.headers):
            need_login()
            return
        if path == "/infer/list":
            st = q.get("stream")
            f = {"model": q.get("model") or None, "state": q.get("state") or None,
                 "lane": q.get("lane") or None, "mode": q.get("mode") or None,
                 "q": q.get("q") or None, "limit": q.get("limit"), "offset": q.get("offset"),
                 "stream": None if st in (None, "", "all") else (1 if st in ("1", "true") else 0),
                 "since": float(q["since"]) if q.get("since") else None,
                 "until": float(q["until"]) if q.get("until") else None,
                 "min_ms": float(q["min_ms"]) if q.get("min_ms") else None}
            out = infer_store.query(**f)
            out["live"] = infer_store.hot()[-10:]     # L1 内存里的最新若干条，给页面「实时」用
            self._send_json(200, out)
            return
        if path == "/infer/stats":
            self._send_json(200, infer_store.stats(int(q.get("days") or 7)))
            return
        if path == "/infer/item":
            d = infer_store.detail(q.get("id") or "")
            self._send_json(200 if d else 404, d or {"error": {"message": "没有这条记录"}})
            return
        if path == "/infer/rag":
            # RAG 的"前后对照"元信息（哪几段历史被检索回来、多长、多少分）——**不含正文**，
            # 所以和 list/item 同档：登录或 API token 就能看。正文那条口子仍然是 /infer/raw。
            d = infer_store.rag_meta(q.get("id") or "")
            self._send_json(200 if d else 404, d or {"error": {"message": "没有这条记录"}})
            return
        self._send_json(404, {"error": {"message": "未知的记录端点"}})

    def do_POST(self):
        self._rec = None                      # 见 do_GET 里的说明：连接复用时要按请求复位
        if self._is_face_path():
            self._face_proxy("POST")
            return
        path = self.path.split("?")[0]
        if self._is_auth_path(path):
            self._auth_get(path, self._query())
            return
        if not self._gate(path):
            return
        if path in ("/tts/say", "/tts/voices", "/tts/health"):
            self._tts_route("POST")
            return
        if path == "/agent/run":
            self._agent_run_post()
            return
        if path == "/agent/stop":
            self._agent_stop_post()
            return
        if path.startswith(("/spark", "/tts")):
            self._passthrough("POST")
            return
        if path.startswith(("/tool/", "/workspace")):
            self._hub_route("POST")
            return
        body = self._read_body()
        if path == "/chats":
            claims = self._session()
            if claims is None:
                self._send_json(401, {"error": {"code": "NOT_LOGGED_IN", "message": "对话同步需要登录"}})
                return
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(data, dict) or not isinstance(data.get("chats"), list):
                    raise ValueError("格式不对：需要 {chats: [...], activeId}")
                store_used = chats_save(str(claims.get("sub") or "anon"), data)
            except Exception as e:
                self._send_json(400, {"error": {"message": str(e)}})
                return
            self._send_json(200, {"ok": True, "store": store_used,
                                  "count": len(data.get("chats") or [])})
            return
        if path in ("/v1/chat/completions", "/v1/messages"):
            try:
                _m = json.loads(body.decode("utf-8") or "{}").get("model")
            except Exception:
                _m = None
            if _m == "xiaoqiu" and "xiaoqiu" not in (CFG.get("model_routes") or {}):
                self._send_json(503, {"error": {"type": "model_offline",
                                                "message": "xiaoqiu 已下线"}})
                return
            # 推理记录：**在这里开一条**（不是在 _rag_request 里）——降级、透传、组装三条路都要留档。
            # 与转发并行：热路径只做字段赋值，收尾只是一次非阻塞入队，落盘/上传在后台线程。
            self._rec = infer_store.begin(path, body, self.headers)
            try:
                self._rag_request(path, body)
            except Exception as e:                 # RAG 自身出错 -> 透传，但**不是原样**：先按上界估算压 max_tokens
                log("rag path failed, passing through:", repr(e))
                COUNTERS["degraded"] += 1
                infer_store.note_route(self._rec, mode="degraded")
                # 降级不等于放弃窗口守卫：原样转发一个必然超窗的请求，就是把 400 交回客户端。
                self._proxy("POST", self.path, body, new_body=self._guard_body(body),
                            up=getattr(self, "_up", None))
            finally:
                _r = getattr(self, "_rec", None) or {}
                infer_store.finish(_r, _r.get("state") or "completed",
                                   http_status=_r.get("http_status"))
            return
        self._proxy("POST", self.path, body,
                    up=pick_upstream(self.headers.get("X-Strata-Session") or self.path))

    def do_DELETE(self):
        self._rec = None
        if self.path.split("?")[0].startswith(("/tool/", "/workspace")):
            self._hub_route("DELETE")
            return
        if self._is_face_path():
            self._face_proxy("DELETE")
            return
        self._send_json(405, {"error": {"code": "method_not_allowed",
                                        "message": "only /face supports DELETE"}})

    def do_PUT(self):
        self._rec = None
        if self._is_face_path():
            self._face_proxy("PUT")
            return
        self._send_json(405, {"error": {"code": "method_not_allowed",
                                        "message": "only /face supports PUT"}})

    def do_PATCH(self):
        self._rec = None
        if self._is_face_path():
            self._face_proxy("PATCH")
            return
        self._send_json(405, {"error": {"code": "method_not_allowed",
                                        "message": "only /face supports PATCH"}})

    def do_OPTIONS(self):
        self._rec = None
        if self._is_face_path():
            self._face_proxy("OPTIONS")
            return
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, PUT, PATCH, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- the RAG path
    def _rag_request(self, path: str, body: bytes):
        COUNTERS["requests"] += 1
        self._t0 = time.perf_counter()          # debug timing: request -> upstream forward
        rec = getattr(self, "_rec", None)
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            infer_store.note_route(rec, mode="direct")
            self._proxy("POST", self.path, body)
            return
        messages = req.get("messages") or []
        orig_req = req
        if not messages:
            infer_store.note_route(rec, mode="direct")
            self._proxy("POST", self.path, body)
            return
        sess_key = session_key(self.headers, req)
        up = route_upstream(req.get("model"), sess_key)   # 模型 ID 定通道；没登记的模型走默认通道
        self._up = up
        infer_store.note_route(rec, model=req.get("model"), lane=up, lane_label=lane_label(up),
                               session=sess_key[:24])
        t_parse = time.perf_counter()
        # 思考预算守卫：客户端没表达过思考偏好时，给"答复"留一份预算（见 thinking_guard 的说明）
        req, tj_injected = thinking_guard(req)
        # 文档预处理：PDF/DOCX/XLSX/XLS/CSV/文本 part 在网关抽出可读文本（扫描 PDF 渲染成页面图），
        # 并按 256K 窗口的剩余预算截断，避免整份文档一次性把 prompt 顶爆（引擎从不截断，超窗即 400）。
        doc_info = None
        body_changed = tj_injected
        if CFG.get("documents", {}).get("enabled", True):
            try:
                parts = doc_extract.find_documents(req, path, CFG)
                if parts:
                    base_req = doc_extract.without_documents(req, parts)
                    base_msgs = base_req.get("messages") or []
                    base_extra = (str(base_req.get("system") or "") +
                                  json.dumps(base_req.get("tools") or [], ensure_ascii=False))
                    other = est_prompt_tokens(base_msgs, base_extra, count_image_blocks(base_msgs))
                    # Reserve what the client actually asked to generate: in RAG-off mode the request
                    # is forwarded unchanged, so a huge max_tokens must not be hidden by gen_reserve_max.
                    try:
                        max_new = int(req.get("max_tokens") or req.get("max_completion_tokens")
                                      or CFG["gen_reserve_default"])
                    except (TypeError, ValueError):
                        max_new = int(CFG["gen_reserve_default"])
                    max_new = max(0, min(max_new, CFG["max_context"]))
                    docs_cfg = CFG.get("documents") or {}
                    room = (CFG["max_context"] - CFG["ctx_slack"] - other - max_new -
                            int(docs_cfg.get("reserve_tokens", 4096)))
                    doc_budget = min(int(docs_cfg.get("max_tokens", 120000)), room)
                    if doc_budget < 256:
                        raise doc_extract.DocumentError(
                            "document + conversation leaves no room in the 256K context; "
                            "shorten the conversation or lower max_tokens")
                    req, doc_info = doc_extract.transform(req, path, parts, doc_budget, CFG)
                    body_changed = True
                    COUNTERS["documents_parsed"] += len(parts)
                    COUNTERS["documents_chars"] += int(doc_info.get("chars") or 0)
                    if doc_info.get("truncated"):
                        COUNTERS["documents_truncated"] += 1
                    if doc_info.get("images"):
                        COUNTERS["documents_images"] += 1
                    log(f"document: {len(parts)} part(s) -> {doc_info['mode']} "
                        f"({doc_info.get('chars', 0)} chars, {doc_info.get('images', 0)} page images, "
                        f"budget {doc_budget} tok)")
            except doc_extract.DocumentError as e:
                COUNTERS["documents_errors"] += 1
                infer_store.note_route(rec, mode="doc_error", error=str(e))
                self._send_json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
                return
        messages = req.get("messages") or []
        # RAG 关闭模式：不检索、不组装、不建会话，纯透传；窗口交给引擎原生 256K + 400/overfit 重试。
        # 用户明确要求“去掉 RAG、回调成 256K”；外面（客户端/中转）会自己控制上下文。
        if not CFG.get("rag_enabled", True):
            COUNTERS["no_rag"] += 1
            infer_store.note_route(rec, mode=(doc_info or {}).get("mode", "no_rag"))
            send_body = json.dumps(req, ensure_ascii=False).encode("utf-8") if body_changed else None
            # 窗口预检（2026-10-19 书稿会话复盘）：透传模式以前完全不数 token，超窗请求要等引擎自己
            # 400 才知道，客户端看见的是一串注定失败的往返。现在：上界估算说"可能装不下"→ 向 serve
            # 要一次精确计数（同一条渲染/编码管线，只 tokenize 不跑模型）——
            #   提示词真装不下 → 就地 400，带实测数字，不进引擎、不重试；
            #   装得下但客户端要的 max_tokens 超余量 → 按实测压输出（旧逻辑连这个都没做）；
            #   计数接口不可用 → 原样透传，引擎 400 的改写网兜底。
            # 上界估算装得下的小请求仍然零开销直通。
            window, slack = CFG["max_context"], CFG["ctx_slack"]
            extra = {"system": req.get("system"), "tools": req.get("tools")}
            extra_text = str(req.get("system") or "") + json.dumps(req.get("tools") or [], ensure_ascii=False)
            est = est_prompt_tokens(messages, extra_text)
            if est + count_margin(est) > window - slack:
                dialect = "openai" if self.path.split("?")[0].startswith("/v1/chat/completions") else "anthropic"
                n = count_tokens(messages, extra, up, dialect)
                if n is not None:
                    n += image_allowance(messages)      # count_tokens 把一张图只数 1 个 pad，引擎会展开成几百
                    if n + slack + count_margin(n) >= window:
                        COUNTERS["window_guard_rejects"] += 1
                        log(f"窗口预检(no_rag): prompt 实测 {n} + 残差 ≥ 窗口 {window}，本地拒绝（不进引擎、不浪费 GPU）")
                        infer_store.note_route(rec, http_status=400, mode="window_guard",
                                               est=n, error=f"prompt {n} tokens 超出窗口 {window}（网关实测计数）")
                        self._send_json(400, overfit_error({"prompt": n, "window": window, "room": 0}))
                        return
                    req2, clamped = clamp_max_tokens(req, n)
                    if clamped:
                        if tj_injected:
                            req2 = refit_thinking_budget(req2)   # 输出被压，补过的思考预算跟着压
                        send_body = json.dumps(req2, ensure_ascii=False).encode("utf-8")
                    log_ctx(req2 if clamped else req, n, "透传(no_rag·精确计数)", clamped=clamped)
                else:
                    log("窗口预检(no_rag): 精确计数不可用，按原样透传（残差由引擎 400 改写网兜底）")
            self._proxy("POST", self.path, body, new_body=send_body, up=up, model=req.get("model"))
            return
        gen_reserve = min(int(req.get("max_tokens") or CFG["gen_reserve_default"]), CFG["gen_reserve_max"])
        budget = CFG["max_context"] - CFG["ctx_slack"] - gen_reserve

        sess = get_session(sess_key)

        # 会话级串行锁：**从这里到组装结束**都不允许同一会话键上的第二个请求插进来。守卫比历史、
        # 分块入库、计数前缀缓存、组装块复用共享同一份会话状态，交错执行会让两个分叉的对话互相
        # 检索到对方的内容。转发放在锁外（上游是几十秒的长尾，不该占着锁）。
        send_body: bytes | None = None          # None = 原样转发客户端字节
        with sess.lock:
            # 会话完整性守卫：会话键是 hash(system+首条 user)，模板化客户端可能撞键——若本次消息序列与
            # 该会话已见历史对不上（公共前缀比"允许最后一轮不同"更短），立即重置检索状态，**绝不串上下文**
            hashes = [sha1(json.dumps(m, ensure_ascii=False, sort_keys=True)) for m in messages]
            k = 0
            while k < len(hashes) and k < len(sess.hist_hashes) and hashes[k] == sess.hist_hashes[k]:
                k += 1
            if sess.hist_hashes and k < len(sess.hist_hashes) - 1:
                log(f"session reset: history diverged at message {k}/{len(sess.hist_hashes)} (key {sess.key})")
                sess.reset()
            sess.hist_hashes = hashes

            sess.ingest(messages)                  # 无论走哪条路都先把新内容排进嵌入队列

            # 快速通道（关键性能路径）：**上界估算**都装得下时纯透传，连精确计数都不调。原因是 count_tokens
            # 对引擎而言是"另一次请求"：引擎默认不驻留已停车的对话，一次计数会把本会话的前缀状态挤出去，
            # 随后同会话生成从"复用前缀"退化为"整段重读"（实测 TTFT 0.1s→0.4s+，长提示更甚）。
            # 旧写法是 `est * 2 < budget`——那个 ×2 其实是在替估算兜底，一旦真实 token 超过估算的 2 倍就漏
            # （生产实测：估算 111k 放行 max_tokens 32000，引擎数到 231,775 → 400）。现在估算本身是上界
            # （按字符分类标定 + 每消息开销 + 图片展开），所以判据直接是 `est + 残差 ≤ 预算`。
            extra = {"system": req.get("system"), "tools": req.get("tools")}
            extra_text = str(req.get("system") or "") + json.dumps(req.get("tools") or [], ensure_ascii=False)
            imgs = count_image_blocks(messages)
            est = est_prompt_tokens(messages, extra_text, imgs)
            if est + count_margin(est) <= budget:
                COUNTERS["passthrough"] += 1
                infer_store.note_route(rec, mode="est", est=est)
                log_ctx(req, est, "透传(快通道·上界估算)")
                # 只有真的改了请求（注入思考预算 / 抽出文档）才重编码；否则一个字节都不动
                if body_changed:
                    send_body = json.dumps(req, ensure_ascii=False).encode("utf-8")
            else:
                # 精确计数（前缀复用）：客户端逐轮追加时，上次请求的 messages 是本次的前缀——只精算新增轮，
                # 超长会话（>256K）下把每轮"整段重计数"（秒级）压到"只算新轮"（毫秒级）
                t_count0 = time.perf_counter()
                kc = 0
                while kc < len(hashes) and kc < len(sess.count_hashes) and hashes[kc] == sess.count_hashes[kc]:
                    kc += 1
                if kc < len(sess.count_hashes):     # 历史被改写/截断：不能复用，整段重算
                    kc = 0
                total = None
                if kc > 0:
                    tail = messages[kc:]
                    # 前缀复用计数：新轮单独计数时也要带上 tools/system（它们是每轮都重发的固定开销）
                    n_tail = count_tokens(tail, extra, up) if tail else 0
                    if n_tail is not None:
                        total = sess.count_total + n_tail + CFG["msg_overhead_tokens"] + image_allowance(tail)
                        dbg(f"count: prefix {kc} msgs reused ({sess.count_total} tok) + {len(tail)} new ({n_tail} tok)")
                if total is None:
                    total = count_tokens(messages, extra, up)
                    if total is not None:
                        total += image_allowance(messages)     # count_tokens 把一张图只数 1 个 pad，引擎会展开成几百
                if total is not None:
                    sess.count_hashes, sess.count_total = hashes, total
                else:
                    total = est                            # 计数不可用：退化为**上界**估算（宁可多组装，不可超窗）
                dbg(f"timing: parse+session {(t_parse - self._t0) * 1000:.0f}ms, count "
                    f"{(time.perf_counter() - t_count0) * 1000:.0f}ms (est {est}, exact {total}, images {imgs}, "
                    f"prefix-reuse {kc > 0})")

                # 安全余量：计数走 Anthropic 渲染，引擎最终可能按 OpenAI 渲染（市场把 Anthropic 转成 OpenAI），
                # 两者模板不同会有几 % 的差；宁可早组装，绝不让引擎 400（引擎从不截断超窗请求）
                margin = count_margin(total)
                if total + margin <= budget:
                    COUNTERS["passthrough"] += 1
                    infer_store.note_route(rec, mode="exact", est=total)
                    req2, clamped = clamp_max_tokens(req, total)
                    if clamped and tj_injected:      # max_tokens 被压小 → 补的思考预算也要跟着压
                        req2 = refit_thinking_budget(req2)
                    log_ctx(req2, total, "透传(精确计数)", clamped=clamped)
                    if clamped or body_changed:
                        send_body = json.dumps(req2, ensure_ascii=False).encode("utf-8")
                else:
                    new_req, info = assemble(sess, req, messages, total, up)
                    assembled_tokens = info.get("final_tokens") or (info["recent_tokens"] + info["memory_tokens"])
                    new_req, clamped = clamp_max_tokens(new_req, assembled_tokens)
                    if clamped and tj_injected:      # 同上：组装路径也会压 max_tokens
                        new_req = refit_thinking_budget(new_req)
                    COUNTERS["assembled"] += 1
                    infer_store.note_route(rec, mode="assembled", est=total,
                                           recent_tokens=info.get("recent_tokens"),
                                           memory_tokens=info.get("memory_tokens"),
                                           block_reused=info.get("reused_block"),
                                           retrieve_ms=round(float(info.get("retrieve_ms") or 0), 1))
                    # RAG 的"前后对照"：这一段的全部依据（窗口/预算/查询指纹/每块明细）都留档，
                    # 出问题时要能复盘"当时为什么这么组装"，而不是只看最后的 token 数。
                    infer_store.note_rag(rec, info)
                    log_ctx(new_req, assembled_tokens,
                            "组装" + ("" if info.get("final_exact") else "(计数不可用·上界估算)"),
                            extra=info, was_total=total, clamped=clamped)
                    log(f"assembled: {total} tok -> {info['recent_tokens'] + info['memory_tokens']} tok "
                        f"(recent {info['recent_tokens']}, memory {info['memory_tokens']}, "
                        f"block {'reused' if info['reused_block'] else 'built'} in {info['retrieve_ms']:.0f}ms)")
                    send_body = json.dumps(new_req, ensure_ascii=False).encode("utf-8")

        # 记下「客户端原始请求」与「真正发往引擎的请求」——超窗组装后两者不同，错误分析要看这个差。
        # send_body is None = 一个字节都没动，发出去的就是客户端那份。
        try:
            sent = json.loads(send_body.decode("utf-8")) if send_body is not None else orig_req
        except ValueError:
            sent = None
        # client = 客户端原始请求（文档抽取/思考预算注入之前的版本），sent = 真正发往引擎的请求
        infer_store.note_request(rec, orig_req, sent, client_bytes=len(body),
                                 sent_bytes=len(send_body) if send_body is not None else len(body))
        self._proxy("POST", self.path, body, new_body=send_body, up=up, model=req.get("model"))


# ----------------------------------------------------------------------------- 并发监控
# 公网 8080 是唯一入口，这里也是唯一能同时看到"两条通道 + 全部在途请求"的地方：把每个推理请求的
# 生老病死登记下来（REG/RECENT），连同各通道引擎状态（LANE_CACHE，后台线程轮询）一起交给
# GET /concurrency，前端 Monitor 页据此展示并发。热路径只做整数累加与字段赋值，不解析任何响应体。

REG: dict[str, dict] = {}                  # 请求 id -> 在途条目
RECENT: deque = deque(maxlen=200)          # 已结束的条目，新的在后
REG_LOCK = threading.Lock()
_REG_SEQ = [0]
_INFER_PATHS = ("/v1/chat/completions", "/v1/messages")


def lane_label(url: str) -> str:
    """通道显示名：配置 lane_labels 优先，否则按 model_routes 反查，最后退化到端口号。"""
    labels = CFG.get("lane_labels") or {}
    if url in labels:
        return str(labels[url])
    for m, u in (CFG.get("model_routes") or {}).items():
        if str(u).rstrip("/") == url:
            return m
    return url.rsplit(":", 1)[-1]


def _authorized(headers) -> bool:
    """/concurrency 的鉴权：与引擎一致，接受 `Authorization: Bearer <key>` 或 `x-api-key`。"""
    if not API_KEY:
        return True
    given = headers.get("Authorization", "")
    given = given[7:].strip() if given.lower().startswith("bearer ") else (headers.get("x-api-key") or "")
    return hmac.compare_digest(given.encode("utf-8"), API_KEY.encode("utf-8"))


def reg_open(path: str, lane: str, model: str | None, headers) -> dict:
    """登记一个推理请求，返回它的条目（之后由持有它的请求线程单写，不额外加锁）。"""
    with REG_LOCK:
        _REG_SEQ[0] += 1
        rid = f"r{_REG_SEQ[0]}"
        now = time.time()
        entry = {"id": rid, "path": path, "lane": lane, "lane_label": lane_label(lane),
                 "model": model, "session": (headers.get("X-Strata-Session") or "")[:24] or None,
                 "state": "queued", "started_at": now, "forward_at": None, "first_chunk_at": None,
                 "bytes_out": 0, "events": 0, "est_tokens": 0, "last_activity": now,
                 "stream": None, "error": None, "closed": False}
        REG[rid] = entry
    return entry


def reg_close(entry: dict | None, state: str, error: str | None = None):
    """结束一个条目：移出在途表、进入最近列表。可重复调用（超窗重试会走两次）。"""
    if not entry or entry.get("closed"):
        return
    entry["closed"] = True
    entry["state"] = state
    entry["ended_at"] = time.time()
    if error:
        entry["error"] = str(error)[:300]
    with REG_LOCK:
        REG.pop(entry["id"], None)
        RECENT.append(dict(entry))


def _entry_view(e: dict, now: float) -> dict:
    """条目的对外视图：补上时长、首字延迟（TTFT）与实时估算速度。"""
    v = dict(e)
    v["elapsed_s"] = round(now - e["started_at"], 1)
    first = e.get("first_chunk_at")
    v["ttft_s"] = round(first - e["started_at"], 2) if first else None
    v["tok_s"] = round(e["est_tokens"] / (now - first), 1) if first and now > first else None
    return v


LANE_CACHE: dict[str, dict] = {}           # 通道 url -> {ts, status, metrics, error, last_ok}
LANE_LOCK = threading.Lock()


def lane_poller():
    """后台轮询每台引擎的 /v1/status + /metrics，缓存给 /concurrency（引擎鉴权用 x-api-key）。"""
    while True:
        for u in upstreams():
            st = mt = None
            err = None
            hc = {}          # [SSE 2026-11-03] 先给默认值：/v1/status 一抛异常，下面引用 hc 会 UnboundLocalError，
                             # 轮询线程当场死掉 —— 之后 LANE_CACHE 永不更新（换引擎/一条道抖动后就这样）
            try:
                st = http_json(f"{u}/v1/status", None, 3, {"x-api-key": API_KEY})
                mt = http_json(f"{u}/metrics", None, 5, {"x-api-key": API_KEY})
                # [SSE 2026-11-02] /health 是**静态配置**里唯一永远可靠的窗口来源：
                # 引擎空闲/未加载时 /v1/status 的 context.max_positions 与 /metrics 的
                # engine.max_context 都会报 0，监控页于是算不出占用率、只剩粘性那一条道的数。
                try:
                    hc = http_json(f"{u}/health", None, 3)
                except Exception:
                    hc = {}
            except Exception as e:
                err = repr(e)
            with LANE_LOCK:
                prev = LANE_CACHE.get(u) or {}
                LANE_CACHE[u] = {"ts": time.time(), "status": st, "metrics": mt, "health": hc, "error": err,
                                 "last_ok": time.time() if st else prev.get("last_ok")}
        _reap_stale_entries()
        time.sleep(max(0.5, float(CFG.get("lane_poll_s") or 2)))


def _reap_stale_entries():
    """[SSE 2026-10-31] 并发台账的清道夫。引擎在流式中途死掉（换引擎/看门狗杀）时，代理这一路的
    `finally` 有时随线程一起没了执行机会，条目就永远停在 streaming/queued——监控页于是挂着一排
    "15 小时读取中"的幽灵（实测普道 19 条，bytes_out 冻死 90 分钟，而引擎 live=idle）。
    规则：未关闭且 `last_activity`（每转发一块就刷新，2486/1865）超过 stale_entry_s（默认 900s，
    0=关）→ 记 stale 结掉。真在出字的长思考流每块都刷新活动，不会被误杀。"""
    stale_s = float(CFG.get("stale_entry_s") or 900)
    if stale_s <= 0:
        return
    now = time.time()
    with REG_LOCK:
        dead = [e for e in REG.values()
                if not e.get("closed") and now - max(e.get("last_activity") or 0, e.get("started_at") or 0) > stale_s]
    for e in dead:
        reg_close(e, "stale", "watchdog: no bytes for too long")
    if dead:
        log(f"并发台账：回收 {len(dead)} 条无活动残留（>{int(stale_s)}s 未结且无字节）")




# ============================ 每用户工作空间（SSE 2026-11-02） ============================
#
# 每个登录用户（或每把 API token）一个独立目录，互不可见：
#   <root>/<uid>/skills/*.md      用户上传的 skill（会进 agent 的系统提示）
#   <root>/<uid>/mcp.json         用户自己的 MCP server 配置
#   <root>/<uid>/images/*.png     生成过的图片（留在工作空间里，刷新还在）
#
# 隔离口径：目录名来自 OAuth 的 sub 或 API token 的 sha256 前 16 位；所有路径都先过
# _ws_safe()，文件名不允许出现分隔符或 ".."，保证逃不出自己的目录。
WS_LOCK = threading.Lock()
_SKILL_MAX = 64 * 1024
_MCP_TOOL_TIMEOUT = 30.0


def _ws_root() -> Path:
    return Path(CFG.get("workspace_root") or "/home/cloud/workspaces")


def _ws_safe(name: str) -> str:
    name = str(name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("非法文件名")
    return name


_LAB_LOCK = threading.Lock()
# `.git` 要保留：START_HERE 让学员用 `git show solution:...` 对照参考答案，删了就没法看了。
# 它不会出现在文件树里（wt_tree/ws_search 跳过隐藏路径），只是占几 MB。
_LAB_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv"}
_LAB_SKIP_FILES = {".DS_Store"}


def lab_cfg() -> dict:
    lab = CFG.get("lab") or {}
    return {
        "template": Path(lab.get("template") or "/home/cloud/llm-study-lab"),
        "dirname": str(lab.get("dirname") or "llm-study-lab"),
        "python": str(lab.get("python") or "/home/cloud/llm-lab/.venv/bin/python"),
        "gpu": str(lab.get("gpu") or "2"),
        "enabled": bool(lab.get("enabled", True)),
    }


def lab_dir(uid: str) -> Path:
    return _ws_root() / uid / lab_cfg()["dirname"]


def ensure_lab(uid: str, force: bool = False, backup: bool = True) -> Path:
    """每个用户一份干净、可写的 LLM Lab 副本。

    模板是只读的教学工程（骨架分支）；用户写自己的副本，互不影响。`force=True` 时先把
    旧副本改名成 `llm-study-lab.bak-<时间>`（backup=True）再拷一份新的，绝不直接删。
    """
    cfg = lab_cfg()
    dest = lab_dir(uid)
    marker = dest / ".lab-seeded"
    with _LAB_LOCK:
        if cfg["enabled"] and force and dest.exists():
            if backup:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                dest.rename(dest.with_name(dest.name + f".bak-{stamp}"))
            else:
                shutil.rmtree(dest, ignore_errors=True)
        src = cfg["template"]
        if not cfg["enabled"]:
            return dest
        if marker.exists():
            # 早期副本没带 .git：补一份，让 `git show solution:...` 还能用
            if src.is_dir() and (src / ".git").is_dir() and not (dest / ".git").exists():
                try:
                    shutil.copytree(src / ".git", dest / ".git", dirs_exist_ok=True)
                except OSError as e:
                    log(f"lab .git repair failed for {uid}: {e!r}")
            return dest
        if not src.is_dir():
            log(f"lab template missing: {src}")
            dest.mkdir(parents=True, exist_ok=True)
            return dest
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        for p in src.rglob("*"):
            rel = p.relative_to(src)
            if any(part in _LAB_SKIP_DIRS for part in rel.parts):
                continue
            if p.name in _LAB_SKIP_FILES or p.suffix in (".pyc", ".pyo") or p.name.endswith(".tar.gz"):
                continue
            target = tmp / rel
            if p.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif p.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(p, target)
                except OSError:
                    pass
        (tmp / "AGENTS.md").write_text(
            "# LLM Lab 工作区说明（给 Agent）\n\n"
            "这是你的个人副本，模板来自 ~/llm-study-lab。\n\n"
            "- 环境：`python main.py doctor` 自检；`source activate.sh` 或直接用 lab 环境运行。\n"
            "- 测试：`pytest -m stage1 -q` … `pytest -m stage7 -q`；每完成一个阶段就跑对应测试。\n"
            "- 骨架：`minillm/` 里 27 个 `TODO(sX-yyy)`；参考答案在 git 分支 `solution`。\n"
            "- 训练产物写在本目录（checkpoints/、data/），不要写到 /tmp。\n"
            "- 改代码前先读 `START_HERE.md` 与 `docs/`；不要删测试来让它变绿。\n",
            encoding="utf-8")
        (tmp / ".lab-seeded").write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"), encoding="utf-8")
        dest.mkdir(parents=True, exist_ok=True)
        # 用目录内容替换（保留 dest 的 mtime 语义）；先清空再搬，避免旧文件残留
        for child in list(dest.iterdir()):
            if child.name == ".lab-seeded":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        for child in list(tmp.iterdir()):
            child.rename(dest / child.name)
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


def ws_dir(uid: str) -> Path:
    d = _ws_root() / uid
    (d / "skills").mkdir(parents=True, exist_ok=True)
    (d / "images").mkdir(parents=True, exist_ok=True)
    try:
        ensure_lab(uid)
    except Exception as e:  # noqa: BLE001 - 模板拷不过去不能把整个工作空间 API 拖死
        log(f"ensure_lab({uid}) failed: {e!r}")
    return d


def ws_skills(uid: str) -> list:
    d = ws_dir(uid) / "skills"
    out = []
    for f in sorted(d.glob("*.md")):
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:400]
        except Exception:
            head = ""
        title = head.splitlines()[0].lstrip("# ").strip() if head else f.stem
        out.append({"name": f.stem, "file": f.name, "title": title[:80],
                    "size": f.stat().st_size, "updated": f.stat().st_mtime})
    return out


def ws_mcp(uid: str) -> list:
    p = ws_dir(uid) / "mcp.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def ws_write_mcp(uid: str, servers: list) -> None:
    p = ws_dir(uid) / "mcp.json"
    p.write_text(json.dumps(servers, ensure_ascii=False, indent=1), encoding="utf-8")


def _ws_img_sig(uid: str, name: str, exp: int) -> str:
    return hmac.new(API_KEY.encode(), f"{uid}|{name}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def ws_image_url(uid: str, name: str, days: int = 7) -> str:
    """图片的签名直链。<img> 带不了 Authorization 头，所以用「uid+文件名+过期时间」的签名
    代替登录态：签名覆盖 uid，换个目录拼不出来；过期后自动失效。"""
    exp = int(time.time()) + days * 86400
    return (f"/workspace/image?uid={urllib.parse.quote(uid)}&name={urllib.parse.quote(name)}"
            f"&exp={exp}&sig={_ws_img_sig(uid, name, exp)}")


def ws_images(uid: str) -> list:
    d = ws_dir(uid) / "images"
    out = []
    for f in sorted(d.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True)[:60]:
        out.append({"name": f.name, "size": f.stat().st_size, "updated": f.stat().st_mtime,
                    "url": ws_image_url(uid, f.name)})
    return out


def ws_path(uid: str, rel: str) -> Path:
    """把用户给的相对路径解析到自己的工作空间内；`..`、隐藏文件、绝对路径一律拒绝。"""
    rel = (rel or "").strip().lstrip("/")
    if rel in ("", "."):
        return ws_dir(uid)
    parts = [x for x in rel.split("/") if x not in ("", ".")]
    if any(x == ".." or x.startswith(".") for x in parts):
        raise ValueError("非法路径")
    return ws_dir(uid).joinpath(*parts)


# 工作空间文件树里不展示的目录/文件：Python 缓存、依赖、VCS 元数据。
# 2026-11-04：用户截图里整棵树被 __pycache__/*.pyc 占满，根本没法找代码 —— 这些永远不是用户要编辑的东西。
WS_HIDE_PARTS = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".ipynb_checkpoints",
                 ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".tox"}
WS_HIDE_SUFFIX = {".pyc", ".pyo"}


def ws_tree(uid: str, max_entries: int = 4000) -> list:
    """工作空间的扁平文件清单（前端自己按路径拼成树）。跳过隐藏文件、缓存与依赖目录。"""
    root = ws_dir(uid)
    out = []
    for p in sorted(root.rglob("*")):
        if len(out) >= max_entries:
            break
        rel = p.relative_to(root).as_posix()
        if any(part.startswith(".") or part in WS_HIDE_PARTS for part in rel.split("/")):
            continue
        if p.suffix.lower() in WS_HIDE_SUFFIX:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({"path": rel, "dir": p.is_dir(),
                    "size": 0 if p.is_dir() else st.st_size, "mtime": st.st_mtime})
    return out


def ws_search(uid: str, q: str, limit: int = 60) -> list:
    """在工作空间里搜文件内容。只读文本类文件，单个文件超过 1MB 跳过。"""
    q = (q or "").strip()
    if not q:
        return []
    root = ws_dir(uid)
    out = []
    low = q.lower()
    TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".js", ".ts", ".py", ".css", ".html",
                ".csv", ".log", ".toml", ".sh", ".vue"}
    for f in root.rglob("*"):
        if len(out) >= limit:
            break
        if not f.is_file() or f.suffix.lower() not in TEXT_EXT:
            continue
        try:
            rel = f.relative_to(root).as_posix()
        except ValueError:
            continue
        # 隐藏目录（.git / .kb / .venv）不参与全文搜索：既慢，也容易把向量/缓存搜出来
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        try:
            if f.stat().st_size > 1024 * 1024:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if low in line.lower():
                out.append({"path": rel, "line": i, "text": line.strip()[:200]})
                if len(out) >= limit:
                    break
    return out


SANDBOX = {
    "root": "/home/cloud/sandbox/run",
    "envs": [
        {"id": "python", "label": "Python（标准库）", "argv": ["/usr/bin/python3", "-I"], "ext": "py",
         "note": "系统 Python 3.12，只有标准库"},
        {"id": "ds", "label": "Python 数据科学", "argv": ["/home/cloud/sandbox/img/venv/bin/python", "-I"], "ext": "py",
         "note": "numpy / pandas / matplotlib（共享同一个镜像 venv）"},
        {"id": "node", "label": "Node.js", "argv": ["/usr/bin/node"], "ext": "js", "note": "node 18"},
        {"id": "cpp", "label": "C++17（g++）", "argv": [], "ext": "cpp", "compile": True,
         "note": "g++ -O2 -std=c++17；编译后直接运行"},
        {"id": "lab", "label": "LLM Lab（PyTorch 2.6 / CUDA）", "argv": [], "ext": "py", "lab": True,
         "note": "llm-lab 的 venv（torch 2.6+cu124），默认用 GPU2；可跑 main.py / pytest"},
    ],
}


def _sandbox_env(env_id: str) -> dict:
    env = next((e for e in SANDBOX["envs"] if e["id"] == env_id), None)
    if env is None:
        raise ValueError(f"没有这个环境: {env_id}")
    return env


def _env_argv(env: dict) -> list[str]:
    if env.get("lab"):
        return [lab_cfg()["python"]]
    return list(env.get("argv") or [])


def _sandbox_limits(timeout_s: float, lab: bool = False):
    import resource

    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_s) + 2, int(timeout_s) + 5))
        # Lab 要装 PyTorch/数据，地址空间给大一点；普通代码仍然 6 GiB 封顶
        mem = (24 if lab else 6) << 30
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        fsize = (512 if lab else 20) << 20
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024 if lab else 256, 1024 if lab else 256))
        # 注意：**不要**设 RLIMIT_NPROC —— 它按用户统计，本 Pod 里跑着推理服务，
        # 用户已有成百上千个线程；设小会让子进程连自己的线程都建不出来
        # （OpenBLAS/node 直接 pthread_create 失败），还会波及我们自己的进程。

    return limits


def _sandbox_envvars(env: dict, work: Path) -> dict:
    e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(work if not env.get("lab") else Path("/home/cloud")),
         "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "MPLBACKEND": "Agg",
         "OPENBLAS_NUM_THREADS": "4", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4",
         "NUMEXPR_NUM_THREADS": "4", "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false"}
    if env.get("lab"):
        e["CUDA_VISIBLE_DEVICES"] = lab_cfg()["gpu"]
        e["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        e["HF_HOME"] = "/home/cloud/.cache/huggingface"
    return e


def gpu_pressure(gpu_id: str) -> tuple[bool, str]:
    """看压力文件里**这张卡**的读数（不是全机均值）。信号过期按未知处理（放行，别误伤）。"""
    try:
        p = json.loads(Path("/shared/pressure/pressure.json").read_text())
        if time.time() - float(p.get("ts") or 0) > 30:
            return True, ""
        idx = int(gpu_id)
        for c in p.get("gpu_cards") or []:
            if int(c.get("index", -1)) == idx:
                mem, util = float(c.get("mem") or 0), float(c.get("util") or 0)
                if mem >= 0.95 or util >= 0.98:
                    return False, f"GPU{idx} 忙（显存 {mem:.0%} / 利用率 {util:.0%}）"
                return True, ""
    except Exception:
        pass
    return True, ""


def _write_project_files(work: Path, files) -> list[str]:
    """把 {相对路径: 内容} 写进工作目录。路径必须留在目录内。"""
    written = []
    if not isinstance(files, dict):
        return written
    if len(files) > 64:
        raise ValueError("文件太多（上限 64 个）")
    total = 0
    for rel, content in files.items():
        rel = str(rel or "").lstrip("/")
        parts = [x for x in rel.split("/") if x not in ("", ".")]
        if not rel or any(x == ".." or x.startswith(".") for x in parts):
            raise ValueError(f"非法文件路径: {rel}")
        text = str(content or "")
        total += len(text)
        if total > 4_000_000:
            raise ValueError("文件总大小超过 4MB")
        f = work.joinpath(*parts)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
        written.append(rel)
    return written


def run_code(uid: str, env_id: str, code: str, timeout_s: float = 30.0, files=None,
             stdin: str = "", in_place: bool = False, cwd_rel: str | None = None,
             force: bool = False) -> dict:
    """在受限子进程里跑一段代码 / 一个小项目，返回 stdout/stderr/产物。

    **这不是安全沙箱**：和网关同用户、同 Pod，只做了资源与时间限制（CPU/内存/文件/进程数）
    和工作目录隔离。它挡不住恶意代码，但挡得住死循环、内存爆炸和把磁盘写满。

    `in_place=True` 时在用户工作空间的 `cwd_rel`（默认 llm-study-lab）里运行，能看到项目文件和
    真实路径；否则在临时目录里跑，跑完把产物回拷到 `runs/<时间>/`。
    """
    import tempfile

    env = _sandbox_env(env_id)
    code = str(code or "")
    if len(code) > 400_000:
        raise ValueError("代码太长（上限 400KB）")
    timeout_s = max(1.0, min(float(timeout_s or 30), 600.0 if env.get("lab") or in_place else 120.0))
    if env.get("lab") and not force:
        ok, why = gpu_pressure(lab_cfg()["gpu"])
        if not ok:
            raise ValueError(f"GPU 压力过高，暂不启动训练/推理：{why}。需要立即运行请传 force=true")

    if in_place:
        work = ws_path(uid, cwd_rel or lab_cfg()["dirname"])
        if not work.is_dir():
            raise ValueError("工作目录不存在")
        _write_project_files(work, files or {})
        run_file = work / ".agent-run.py"
        if code.strip():
            run_file.write_text(code, encoding="utf-8")
        cleanup = [run_file] if code.strip() else []
    else:
        base = Path(SANDBOX["root"]) / re.sub(r"[^A-Za-z0-9_.-]", "_", uid)
        base.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="run-", dir=str(base)))
        cleanup = []
        _write_project_files(work, files or {})
        run_file = work / f"main.{env['ext']}"
        if code.strip() or not files:
            run_file.write_text(code, encoding="utf-8")

    t0 = time.perf_counter()
    timed_out = False
    out = err = ""
    rc = -1
    try:
        if env.get("compile"):      # C++：把项目里所有 .cpp/.cc 一起编，再跑
            sources = sorted(str(p) for p in work.rglob("*") if p.suffix.lower() in (".cpp", ".cc", ".cxx", ".c"))
            if not sources:
                sources = [str(run_file)]
            exe = work / "main.bin"
            cc = subprocess.run(["/usr/bin/g++", "-O2", "-std=c++17", "-pthread", *sources, "-o", str(exe)],
                                cwd=str(work), capture_output=True, text=True, errors="replace",
                                env=_sandbox_envvars(env, work), timeout=min(timeout_s, 120))
            if cc.returncode != 0:
                out, err, rc = cc.stdout, cc.stderr, cc.returncode
                raise _CompileFailed
            argv = [str(exe)]
        else:
            argv = [*_env_argv(env)]
            target = str(run_file) if (code.strip() or not files) else None
            if target:
                argv.append(target)
            else:                    # 只给了 files：找项目入口
                for name in ("main.py", "app.py", "index.js", "main.cpp"):
                    if (work / name).exists():
                        argv.append(str(work / name))
                        break
            if not target and len(argv) == len(_env_argv(env)):
                raise ValueError("没有入口文件：请给 code，或 files 里放 main.py")
        r = subprocess.run(argv, cwd=str(work), timeout=timeout_s, input=stdin or None,
                           capture_output=True, text=True, errors="replace",
                           env=_sandbox_envvars(env, work), preexec_fn=_sandbox_limits(timeout_s, bool(env.get("lab") or in_place)))
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        rc = -1
    except _CompileFailed:
        pass
    finally:
        for f in cleanup:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    produced = []
    if not in_place:
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = ws_dir(uid) / "runs" / stamp
            for f in sorted(work.rglob("*")):
                if not f.is_file() or f.name in {run_file.name, "main.bin"}:
                    continue
                rel = f.relative_to(work).as_posix()
                if any(part.startswith(".") for part in rel.split("/")):
                    continue
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                produced.append(f"runs/{stamp}/{rel}")
        except Exception:  # noqa: BLE001 - 产物回拷失败不该把运行结果也搞丢
            produced = []
        finally:
            shutil.rmtree(work, ignore_errors=True)

    return {"env": env_id, "exit": rc, "timed_out": timed_out, "files": produced,
            "cwd": str(work), "in_place": in_place,
            "ms": round((time.perf_counter() - t0) * 1000),
            "stdout": out[-64000:], "stderr": err[-32000:]}


class _CompileFailed(Exception):
    pass


# ------------------------------------------------------------------ 后台任务（训练/长跑）
#
# 一次 HTTP 请求最多几十秒，而训练要几十分钟到几小时。后台任务把命令放进独立进程组，
# 日志落盘，前端/agent 用 job_status 轮询增量日志；代理重启后仍能从 meta 文件看到历史。
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_MAX_RUNNING = 2


def _jobs_root(uid: str) -> Path:
    d = ws_dir(uid) / "runs" / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_read_meta(uid: str, jid: str) -> dict | None:
    p = _jobs_root(uid) / f"{jid}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _job_write_meta(uid: str, jid: str, meta: dict) -> None:
    p = _jobs_root(uid) / f"{jid}.json"
    try:
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _job_argv(env: dict, args: list[str]) -> list[str]:
    if not args:
        raise ValueError("缺少命令")
    head = args[0]
    py = lab_cfg()["python"] if env.get("lab") else (_env_argv(env) or ["/usr/bin/python3"])[0]
    if head in ("python", "python3"):
        return [py, *args[1:]]
    if head in ("pytest",):
        return [py, "-m", "pytest", *args[1:]]
    if head in ("pip",):
        return [py, "-m", "pip", *args[1:]]
    if head == "node":
        return ["/usr/bin/node", *args[1:]]
    if head in ("g++", "gcc"):
        return ["/usr/bin/" + head, *args[1:]]
    if head in ("bash", "sh"):
        return ["/bin/bash", *args[1:]]
    # 其余按「脚本 + 参数」处理：python 脚本用当前环境解释器
    if head.endswith((".py", ".pyi")):
        return [py, *args]
    return args


def job_start(uid: str, env_id: str, args, files=None, cwd_rel: str | None = None,
              timeout_s: float = 1800.0, force: bool = False) -> dict:
    env = _sandbox_env(env_id)
    if isinstance(args, str):
        args = shlex.split(args)
    args = [str(x) for x in (args or [])]
    timeout_s = max(10.0, min(float(timeout_s or 1800), 21600.0))
    if env.get("lab") and not force:
        ok, why = gpu_pressure(lab_cfg()["gpu"])
        if not ok:
            raise ValueError(f"GPU 压力过高，暂不启动：{why}。需要立即运行请传 force=true")
    with JOBS_LOCK:
        running = [j for j in JOBS.values() if j.get("uid") == uid and j.get("status") == "running"]
        if len(running) >= JOB_MAX_RUNNING:
            raise ValueError(f"你已有 {len(running)} 个后台任务在跑（上限 {JOB_MAX_RUNNING}），先等它结束或停掉一个")
    cwd = ws_path(uid, cwd_rel or lab_cfg()["dirname"])
    if not cwd.is_dir():
        raise ValueError("工作目录不存在")
    _write_project_files(cwd, files or {})
    jid = "j" + uuid.uuid4().hex[:10]
    log = _jobs_root(uid) / f"{jid}.log"
    argv = _job_argv(env, args)
    meta = {"id": jid, "uid": uid, "env": env_id, "args": args, "argv": argv,
            "cwd": str(cwd), "started": time.time(), "status": "running", "exit": None,
            "log": str(log), "timeout_s": timeout_s}
    lf = open(log, "ab", buffering=0)
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=_sandbox_envvars(env, cwd),
                                preexec_fn=_sandbox_limits(timeout_s, bool(env.get("lab"))),
                                start_new_session=True)
    except Exception:
        lf.close()
        raise
    meta["pid"] = proc.pid
    with JOBS_LOCK:
        JOBS[jid] = {**meta, "proc": proc, "logfh": lf}
    _job_write_meta(uid, jid, meta)

    def watch():
        try:
            rc = proc.wait(timeout=timeout_s)
            status = "done" if rc == 0 else "failed"
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            rc = proc.wait()
            status = "timeout"
        except Exception:
            rc, status = -1, "failed"
        finally:
            try:
                lf.close()
            except Exception:
                pass
            with JOBS_LOCK:
                cur = JOBS.get(jid)
                if cur is not None:
                    cur.update({"status": status, "exit": rc, "finished": time.time(), "proc": None})
            m = _job_read_meta(uid, jid) or meta
            m.update({"status": status, "exit": rc, "finished": time.time()})
            _job_write_meta(uid, jid, m)

    threading.Thread(target=watch, daemon=True).start()
    return {"id": jid, "status": "running", "pid": proc.pid, "log": f"runs/jobs/{jid}.log"}


def job_status(uid: str, jid: str, offset: int = 0, max_bytes: int = 200_000) -> dict:
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j is not None and j.get("uid") != uid:
            j = None
    if j is None:
        m = _job_read_meta(uid, jid)
        if not m:
            raise ValueError("没有这个任务")
        # 代理重启后内存里的 proc 没了；如果进程也已经不在，别永远显示 running
        if m.get("status") == "running" and m.get("pid") and not _pid_alive(m["pid"]):
            m.update({"status": "lost", "finished": time.time()})
            _job_write_meta(uid, jid, m)
        j = m
    p = Path(j.get("log") or "")
    size = p.stat().st_size if p.exists() else 0
    offset = max(0, min(int(offset or 0), size))
    raw = b""
    try:
        with open(p, "rb") as f:
            f.seek(offset)
            raw = f.read(min(max_bytes, max(0, size - offset)))
        log = raw.decode("utf-8", "replace")
    except OSError:
        log = ""
    out = {k: j.get(k) for k in ("id", "status", "exit", "pid", "env", "args", "cwd",
                                 "started", "finished", "timeout_s")}
    out.update({"size": size, "offset": offset, "next_offset": offset + len(raw), "log": log})
    return out


def job_stop(uid: str, jid: str) -> dict:
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j is not None and j.get("uid") != uid:
            j = None
        proc = j.get("proc") if j else None
    if j is None:
        m = _job_read_meta(uid, jid)
        if not m:
            raise ValueError("没有这个任务")
        pid = m.get("pid")
        if pid and m.get("status") == "running":
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except Exception:
                pass
        return {"id": jid, "stopped": True}
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    return {"id": jid, "stopped": True}


def job_list(uid: str, limit: int = 30) -> list:
    out = []
    seen = set()
    with JOBS_LOCK:
        for j in JOBS.values():
            if j.get("uid") == uid and j.get("status") == "running":
                out.append({k: j.get(k) for k in ("id", "status", "exit", "pid", "env", "args",
                                                  "started", "finished", "timeout_s")})
                seen.add(j["id"])
    for p in sorted(_jobs_root(uid).glob("j*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if m.get("id") in seen:
            continue
        if m.get("status") == "running" and m.get("pid") and not _pid_alive(m["pid"]):
            m.update({"status": "lost", "finished": time.time()})
            _job_write_meta(uid, m["id"], m)
        out.append({k: m.get(k) for k in ("id", "status", "exit", "pid", "env", "args",
                                          "started", "finished", "timeout_s")})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 个人知识库（RAG）
#
# 每个用户一个独立的小型向量库：<uid>/.kb/<doc>.json。文档切块后用 bge-m3 嵌入，
# 检索时先余弦召回、再用 bge-reranker-v2-m3 精排；两个服务都在集群内，走稳定 DNS。
# 这不是 1M 长上下文那条主 RAG（那条是 rag_enabled），而是给 agent 用的「我的资料」。
KB_LOCK = threading.Lock()
KB_MAX_CHARS = 1_200_000
KB_CHUNK = 700
KB_OVERLAP = 120


def _kb_root(uid: str) -> Path:
    d = ws_dir(uid) / ".kb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _embed_texts(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    url = str(CFG.get("embed_url") or "")
    if not url:
        raise ValueError("嵌入服务未配置")
    for i in range(0, len(texts), 16):
        batch = texts[i:i + 16]
        body = json.dumps({"model": CFG.get("embed_model") or "bge-m3", "input": batch}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(CFG.get("embed_timeout_s") or 30)) as r:
            data = json.loads(r.read().decode())
        rows = data.get("data") or []
        rows.sort(key=lambda x: x.get("index", 0))
        out.extend([list(map(float, x.get("embedding") or [])) for x in rows])
    if len(out) != len(texts):
        raise ValueError("嵌入服务返回的数量不对")
    return out


def _chunk_text(text: str) -> list[str]:
    text = re.sub(r"\r\n?", "\n", text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= KB_CHUNK:
            cur = (cur + "\n\n" + p).strip()
            continue
        if cur:
            chunks.append(cur)
        if len(p) <= KB_CHUNK:
            cur = p
        else:  # 单段太长：滑窗切
            step = KB_CHUNK - KB_OVERLAP
            for j in range(0, len(p), step):
                part = p[j:j + KB_CHUNK].strip()
                if part:
                    chunks.append(part)
            cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def _doc_text_from_bytes(data: bytes, name: str) -> str:
    ext = (name.rsplit(".", 1)[-1] or "").lower()
    fmt = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx", "xls": "xls", "csv": "csv",
           "md": "text", "markdown": "text", "txt": "text", "json": "text", "py": "text",
           "js": "text", "ts": "text", "html": "html", "htm": "html", "rtf": "rtf"}.get(ext)
    if fmt:
        try:
            d = doc_extract.extract_text(data, fmt, name, CFG.get("documents") or {})
            return str(d.get("text") or "")
        except Exception:
            if fmt != "text":
                raise
    return data.decode("utf-8", "replace")


def kb_add(uid: str, name: str = "", text: str | None = None, path: str | None = None,
           url: str | None = None) -> dict:
    source = ""
    if path:
        f = ws_path(uid, path)
        if not f.is_file():
            raise ValueError("知识库文件不存在")
        text = _doc_text_from_bytes(f.read_bytes(), f.name)
        source = path
        name = name or f.name
    elif url:
        d = tool_fetch(url, max_chars=KB_MAX_CHARS)
        text = str(d.get("text") or "")
        source = url
        name = name or str(d.get("title") or url)
    text = str(text or "").strip()
    if not text:
        raise ValueError("知识库内容为空")
    text = text[:KB_MAX_CHARS]
    chunks = _chunk_text(text)
    if not chunks:
        raise ValueError("切块后没有内容")
    if len(chunks) > 400:
        chunks = chunks[:400]
    vecs = _embed_texts(chunks)
    name = (name or f"资料-{time.strftime('%m%d-%H%M%S')}")[:120]
    doc_id = "d" + hashlib.sha256((uid + name + source + str(time.time())).encode()).hexdigest()[:12]
    payload = {"id": doc_id, "name": name, "source": source or "(粘贴文本)", "created": time.time(),
               "chars": len(text), "chunks": [{"text": c, "vec": v} for c, v in zip(chunks, vecs)]}
    f = _kb_root(uid) / f"{doc_id}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)
    return {"id": doc_id, "name": name, "source": payload["source"], "chunks": len(chunks), "chars": len(text)}


def _kb_docs(uid: str) -> list[dict]:
    docs = []
    for p in sorted(_kb_root(uid).glob("*.json")):
        try:
            docs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return docs


def kb_list(uid: str) -> dict:
    docs = _kb_docs(uid)
    return {"count": len(docs), "chunks": sum(len(d.get("chunks") or []) for d in docs),
            "docs": [{"id": d.get("id"), "name": d.get("name"), "source": d.get("source"),
                      "chunks": len(d.get("chunks") or []), "chars": d.get("chars"),
                      "created": d.get("created")} for d in docs]}


def kb_delete(uid: str, doc_id: str) -> dict:
    doc_id = re.sub(r"[^A-Za-z0-9_.-]", "", str(doc_id or ""))
    f = _kb_root(uid) / f"{doc_id}.json"
    if f.exists():
        f.unlink()
    return {"ok": True, "id": doc_id}


def _rerank(query: str, docs: list[str]) -> list[float] | None:
    url = str(CFG.get("rerank_url") or "")
    if not url or not docs:
        return None
    try:
        body = json.dumps({"model": "bge-reranker-v2-m3", "query": query, "documents": docs}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(CFG.get("rerank_timeout_s") or 30)) as r:
            data = json.loads(r.read().decode())
        rows = sorted(data.get("results") or [], key=lambda x: x.get("index", 0))
        return [float(x.get("relevance_score") or 0) for x in rows]
    except Exception as e:  # noqa: BLE001 - 精排挂了就退回余弦
        dbg(f"kb rerank failed: {e!r}")
        return None


def kb_search(uid: str, q: str, k: int = 6, rerank: bool = True) -> dict:
    q = str(q or "").strip()
    if not q:
        raise ValueError("检索词为空")
    docs = _kb_docs(uid)
    if not docs:
        return {"query": q, "results": [], "note": "知识库还是空的，先用 kb_add 加资料"}
    qv = np.array(_embed_texts([q])[0], dtype=np.float32)
    qn = float(np.linalg.norm(qv)) or 1.0
    cand = []
    for d in docs:
        for c in d.get("chunks") or []:
            v = np.array(c.get("vec") or [], dtype=np.float32)
            if not v.size:
                continue
            cos = float(np.dot(qv, v) / (qn * (float(np.linalg.norm(v)) or 1.0)))
            cand.append({"text": c.get("text") or "", "name": d.get("name"),
                         "source": d.get("source"), "cos": cos})
    cand.sort(key=lambda x: x["cos"], reverse=True)
    top = cand[:40]
    scores = _rerank(q, [c["text"] for c in top]) if rerank else None
    if scores is not None and len(scores) == len(top):
        for c, s in zip(top, scores):
            c["rerank"] = s
        top.sort(key=lambda x: (x.get("rerank", 0), x["cos"]), reverse=True)
    for c in top:
        c["score"] = c.get("rerank", c["cos"])
        c["text"] = c["text"][:1200]
    return {"query": q, "count": len(top[:max(1, min(int(k or 6), 20))]),
            "results": top[:max(1, min(int(k or 6), 20))],
            "reranked": scores is not None}


# ------------------------------------------------------------------ 胡桃 TTS
#
# Voice Studio 的 9882 worker 已经常驻加载 run 122（胡桃）的权重；本 Pod 用 SSH 隧道
# 把 127.0.0.1:9882 映射过去（supervisor: tts-tunnel）。这里直接调 GPT-SoVITS 的 /tts，
# 不需要 Voice Studio 的 API Key，也不把任何密钥放进浏览器。
TTS_LOCK = threading.Lock()


def tts_cfg() -> dict:
    t = CFG.get("tts") or {}
    voice = t.get("voice") or {}
    return {"local_url": str(t.get("local_url") or "http://127.0.0.1:9882").rstrip("/"),
            "cache_dir": Path(t.get("cache_dir") or "/home/cloud/logs/tts-cache"),
            "max_chars": int(t.get("max_chars") or 1200),
            "voice": voice}


def tts_clean(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"```[\s\S]*?```", " ", s)            # 代码块不朗读
    s = re.sub(r"`[^`]*`", " ", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)      # 图片
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)   # 链接只读文字
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"[*_>#|]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tts_say(text: str, speed: float = 1.0) -> bytes:
    cfg = tts_cfg()
    s = tts_clean(text)
    if not s:
        raise ValueError("没有可朗读的文字")
    if len(s) > cfg["max_chars"]:
        cut = s[:cfg["max_chars"]]
        at = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("，"))
        s = cut[:at + 1] if at > cfg["max_chars"] * 0.6 else cut
    v = cfg["voice"]
    key = hashlib.sha256((v.get("id") or "hutao").encode() + b"|" + s.encode() + b"|" + str(speed).encode()).hexdigest()
    cfg["cache_dir"].mkdir(parents=True, exist_ok=True)
    cache = cfg["cache_dir"] / f"{key}.wav"
    if cache.exists() and cache.stat().st_size > 1000:
        return cache.read_bytes()
    body = {
        "text": s, "text_lang": v.get("text_lang") or "zh",
        "ref_audio_path": v.get("ref_audio") or "",
        "prompt_text": v.get("prompt_text") or "",
        "prompt_lang": v.get("prompt_lang") or "zh",
        "text_split_method": "cut5", "batch_size": 1, "media_type": "wav",
        "streaming_mode": False, "speed_factor": max(0.5, min(float(speed or 1.0), 2.0)),
    }
    req = urllib.request.Request(cfg["local_url"] + "/tts", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type") or ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError(f"TTS 合成失败: {detail}")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"TTS 服务不可用（胡桃 worker 隧道没起来？）: {e!r}")
    if not data or b"RIFF" not in data[:16]:
        raise ValueError("TTS 返回的不是 WAV 音频")
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(cache)
    return data


def ws_meta(uid: str) -> dict:
    d = ws_dir(uid)
    cfg = lab_cfg()
    return {"user": uid, "root": str(d), "skills": ws_skills(uid), "mcp": [
        {"name": s.get("name"), "url": s.get("url")} for s in ws_mcp(uid)],
        "images": ws_images(uid),
        "lab": {"dir": cfg["dirname"], "python": cfg["python"], "gpu": cfg["gpu"],
                "exists": lab_dir(uid).is_dir()},
        "kb": kb_list(uid), "jobs": job_list(uid, 10)}


def _safe_url(url: str) -> str:
    """fetch_url 的 SSRF 闸门：只许 http/https，且解析出来的地址不能是内网/回环。"""
    u = urllib.parse.urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("只支持 http/https URL")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except Exception as e:
        raise ValueError(f"域名解析失败: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("拒绝访问内网地址")
    return url


def tool_fetch(url: str, max_chars: int = 8000) -> dict:
    """抓一个网页并抽正文（去脚本/样式/标签）。拿不到就如实报错，不编内容。"""
    _safe_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; strata-hub)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(2_000_000)
        ctype = r.headers.get("Content-Type", "")
    enc = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        enc = m.group(1)
    text = raw.decode(enc, "replace")
    title = ""
    mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if mt:
        title = html.unescape(re.sub(r"\s+", " ", mt.group(1))).strip()[:200]
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(re.sub(r"\s+", " ", body)).strip()
    return {"url": url, "title": title, "chars": len(body), "truncated": len(body) > max_chars,
            "text": body[:max_chars]}


def tool_image_search(q: str, n: int = 6) -> dict:
    """搜图：Bing 图片搜索的 HTML 里带 murl（原图直链）。返回直链，交给生图当参考图。"""
    q = (q or "").strip()
    if not q:
        raise ValueError("empty query")
    url = "https://cn.bing.com/images/search?" + urllib.parse.urlencode({"q": q, "form": "HDRSC2", "first": 1})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "replace")
    urls = []
    for m in re.finditer(r'murl(?:&quot;|")\s*:\s*(?:&quot;|")([^"&]+)', raw):
        u = html.unescape(m.group(1))
        if u.startswith("http") and u not in urls:
            urls.append(u)
        if len(urls) >= n:
            break
    return {"query": q, "results": [{"image": u} for u in urls]}


def tool_fetch_image(url: str, max_bytes: int = 8 * 1024 * 1024) -> dict:
    """抓一张远程图片转成 dataURL。服务端做，绕开浏览器的 CORS：
    搜图结果里的直链在页面里既读不到像素，也画不进 canvas，但生图模型要的就是原图。"""
    _safe_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; strata-hub)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read(max_bytes)
        ctype = (r.headers.get("Content-Type") or "image/png").split(";")[0].strip()
    if not ctype.startswith("image/"):
        raise ValueError(f"不是图片（Content-Type: {ctype}）")
    return {"url": url, "mime": ctype, "bytes": len(raw),
            "data": f"data:{ctype};base64," + base64.b64encode(raw).decode()}


def tool_mcp_list(uid: str) -> dict:
    """把用户配置的 MCP server 逐个列工具。失败的服务标 ok=false，不拖垮其它服务。"""
    servers, tools = [], []
    for c in ws_mcp(uid):
        try:
            tl = mcp_client.list_tools(c["url"], c.get("headers"))
            servers.append({"name": c.get("name"), "ok": True,
                            "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:160]}
                                      for t in tl]})
            for t in tl:
                tools.append({"server": c.get("name"), "name": t.get("name"),
                              "description": (t.get("description") or "")[:300],
                              "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}}})
        except Exception as e:  # noqa: BLE001
            servers.append({"name": c.get("name"), "ok": False, "error": repr(e)[:200], "tools": []})
    return {"servers": servers, "tools": tools}


def tool_mcp_call(uid: str, server: str, name: str, args: dict) -> dict:
    for c in ws_mcp(uid):
        if c.get("name") == server:
            return mcp_client.call_tool(c["url"], name, args or {}, c.get("headers"), _MCP_TOOL_TIMEOUT)
    raise ValueError(f"没有这个 MCP server: {server}")


# ======================== 工作空间结束 ========================

# ============================ 外部服务监控（SSE 2026-11-02） ============================
#
# 监控页要「一页看全部 AI 服务」：本机两条 LLM 通道之外，邻居 pod 上还有
# bge-m3 / bge-reranker / Voice Studio，本机有 face，Spark 上有四条模型路由。
# 上报口径各不相同（JSON 计数 / Prometheus 文本 / 只有健康检查），这里统一归一成
# {up, calls, errors, in_flight, avg_ms, last_ts, ...}。
#
# 只读 poller：健康探测算「探针」，不计入调用数；每 2s 一轮，结果只放内存。
SVC_LOCK = threading.Lock()
SVC_CACHE: dict = {}
_GPU_LOCK = threading.Lock()
_GPU_CACHE = {"ts": 0.0, "cards": []}
_SPARK_GPU = {"ts": 0.0, "cards": [], "error": None}
_SPARK_KEY = {"ts": 0.0, "key": ""}


def spark_key() -> str:
    """Spark 模型网关的 key（600 文件，不进代码/仓库）。读不到就空串，监控页显 401。"""
    now = time.time()
    if _SPARK_KEY["key"] and now - _SPARK_KEY["ts"] < 60:
        return _SPARK_KEY["key"]
    f = (CFG.get("spark") or {}).get("key_file") or ""
    try:
        k = Path(f).read_text().strip()
    except Exception:
        k = ""
    _SPARK_KEY.update({"ts": now, "key": k})
    return k


def _svc_root(url: str) -> str:
    """从配置里的接口 URL 取服务根地址：去掉 /v1/embeddings 这类接口路径。

    邻居服务一律用集群 DNS（group-*.clouds.svc.cluster.local），**不要写 Pod IP**——
    Pod 重建后 10.42.x.x 会漂移，监控就会一直超时（2026-11-04 的 embed/rerank/tts 假挂机就是这么来的）。
    """
    u = str(url or "").strip().rstrip("/")
    for suf in ("/v1/embeddings", "/v1/rerank", "/v1/chat/completions"):
        if u.endswith(suf):
            return u[: -len(suf)]
    return u


def service_defs() -> list:
    """监控要看的全部 AI 服务。config.json 的 `services` 可覆盖；默认就是本机 + 邻居 + Spark。"""
    if CFG.get("services"):
        return CFG["services"]
    spark = str((CFG.get("spark") or {}).get("base_url") or "https://spark.ssemarket.cn").rstrip("/")
    auth = {"Authorization": "Bearer " + spark_key()} if spark_key() else {}
    embed = _svc_root(CFG.get("embed_url") or DEFAULTS["embed_url"])
    rerank = _svc_root(CFG.get("rerank_url") or DEFAULTS["rerank_url"])
    tts = str((CFG.get("tts") or {}).get("base_url") or DEFAULTS["tts"]["base_url"]).rstrip("/")
    return [
        {"id": "face", "label": "人脸识别", "kind": "face", "group": "本机",
         "url": str(CFG.get("face_upstream") or "http://127.0.0.1:8095").rstrip("/"),
         "health": "/face/health", "metrics": "/face/metrics"},
        {"id": "embed", "label": "bge-m3 向量", "kind": "embedding", "group": "邻居 pod",
         "url": embed, "health": "/health", "metrics": "/metrics"},
        {"id": "rerank", "label": "bge-reranker", "kind": "rerank", "group": "邻居 pod",
         "url": rerank, "health": "/health", "metrics": "/metrics"},
        {"id": "tts", "label": "Voice Studio", "kind": "tts", "group": "邻居 pod",
         "url": tts, "health": "/healthz", "metrics": "/metrics"},
        {"id": "spark-image", "label": "Qwen-Image-2.1", "kind": "image", "group": "Spark",
         "url": spark, "health": "/image/health", "metrics": "", "headers": auth},
        {"id": "spark-embed", "label": "wemm-embedding-9b", "kind": "embedding", "group": "Spark",
         "url": spark, "health": "/embed/health", "metrics": "", "headers": auth},
        {"id": "spark-rerank", "label": "qwen3-vl-reranker-8b", "kind": "rerank", "group": "Spark",
         "url": spark, "health": "/rerank/health", "metrics": "", "headers": auth},
    ]


def _svc_json(url: str, headers: dict | None, timeout: float = 3.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _parse_prom(text: str) -> dict:
    """极简 Prometheus 文本解析：只要 指标名 -> 数值，够读 face 的那些 gauge。"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def svc_poller():
    """后台轮询：每 2s 一轮，失败不清旧指标（只标 up=false + error），页面据此画断线。"""
    while True:
        for s in service_defs():
            rec = {"ts": time.time()}
            t0 = time.perf_counter()
            try:
                rec["health"] = _svc_json(s["url"] + s["health"], s.get("headers"))
                rec["up"] = True
            except urllib.error.HTTPError as e:
                rec["up"] = False
                rec["error"] = f"HTTP {e.code}"
            except Exception as e:  # noqa: BLE001 - 监控线程绝不能死
                rec["up"] = False
                rec["error"] = repr(e)[:160]
            rec["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if s.get("metrics"):
                try:
                    if s.get("kind") == "face":
                        req = urllib.request.Request(s["url"] + s["metrics"], headers=s.get("headers") or {})
                        with urllib.request.urlopen(req, timeout=3) as r:
                            rec["prom"] = _parse_prom(r.read().decode("utf-8", "replace"))
                    else:
                        rec["metrics"] = _svc_json(s["url"] + s["metrics"], s.get("headers"))
                except Exception:  # noqa: BLE001
                    rec["metrics"] = rec.get("metrics")
            with SVC_LOCK:
                SVC_CACHE[s["id"]] = rec
        _sample_spark_gpu()
        time.sleep(2)


def services_snapshot() -> list:
    """给 /concurrency 用的归一视图。calls/errors 口径：计数端点有就用它的，没有就 null（别编）。"""
    with SVC_LOCK:
        cache = {k: dict(v) for k, v in SVC_CACHE.items()}
    out = []
    for s in service_defs():
        c = cache.get(s["id"]) or {}
        m = c.get("metrics") or {}
        prom = c.get("prom") or {}
        rec = {"id": s["id"], "label": s["label"], "kind": s["kind"], "group": s.get("group") or "",
               "url": s["url"], "up": bool(c.get("up")), "latency_ms": c.get("latency_ms"),
               "health": c.get("health"), "error": c.get("error"), "last_ts": c.get("ts")}
        if s.get("kind") == "face":
            # face 只有 gauge；调用数由代理自己数（所有 /face/* 都经过这里）
            rec.update({"calls": int(COUNTERS.get("face_requests") or 0), "errors": None, "in_flight": None,
                        "avg_ms": None, "paths": {},
                        "gauges": {k: v for k, v in prom.items() if k.startswith("face_")}})
        elif m:
            rec.update({"calls": m.get("calls"), "errors": m.get("errors"),
                        "in_flight": max(0, (m.get("in_flight") or 0) - 1),
                        "gpu_alloc_mib": m.get("gpu_alloc_mib"), "uptime_s": m.get("uptime_s"),
                        "last_call_ts": m.get("last_ts"),
                        "paths": {p: {"n": v.get("n"), "err": v.get("err"), "avg_ms": v.get("avg_ms"),
                                      "last_ms": v.get("last_ms"), "last_ts": v.get("last_ts")}
                                  for p, v in (m.get("paths") or {}).items()}})
        else:
            rec.update({"calls": None, "errors": None, "in_flight": None, "avg_ms": None, "paths": {}})
        out.append(rec)
    return out


def gpu_snapshot() -> list:
    """整机三张卡的读数（本机 nvidia-smi 一次全给，含邻居在用的 GPU2）。2s 缓存。"""
    now = time.time()
    with _GPU_LOCK:
        if now - _GPU_CACHE["ts"] < 2 and _GPU_CACHE["cards"]:
            return _GPU_CACHE["cards"]
    cards = []
    try:
        q = ("index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw")
        out = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) < 7:
                continue
            def num(v):
                try:
                    return float(v)
                except ValueError:
                    return None
            cards.append({"index": int(num(f[0]) or 0), "name": f[1],
                          "used_mib": num(f[2]), "total_mib": num(f[3]), "util": num(f[4]),
                          "temp": num(f[5]), "power": num(f[6])})
    except Exception:  # noqa: BLE001
        cards = []
    with _GPU_LOCK:
        _GPU_CACHE.update({"ts": now, "cards": cards})
    return cards


def _sample_spark_gpu():
    """Spark 的 GB10：统一内存，nvidia-smi 的 total/used 报 N/A，只能给逐进程占用之和。
    10s 一采、SSH 走免密（~/.ssh/config 的 sse-dgx-spark），失败就保留上一份并记 error。"""
    now = time.time()
    if now - _SPARK_GPU["ts"] < 10:
        return
    try:
        cmd = ("nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits;"
               " echo ---; nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,power.draw "
               "--format=csv,noheader,nounits")
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "sse-dgx-spark", cmd],
                             capture_output=True, text=True, timeout=15).stdout
        procs, gpu = [], {}
        section = 0
        for line in out.splitlines():
            line = line.strip()
            if line == "---":
                section = 1
                continue
            if not line:
                continue
            if section == 0:
                pid, used = [x.strip() for x in line.split(",")][:2]
                try:
                    procs.append({"pid": int(pid), "mib": float(used)})
                except ValueError:
                    pass
            else:
                f = [x.strip() for x in line.split(",")]
                gpu = {"util": float(f[0]) if f[0] not in ("", "[N/A]") else None,
                       "temp": float(f[1]) if len(f) > 1 and f[1] not in ("", "[N/A]") else None,
                       "power": float(f[2]) if len(f) > 2 and f[2] not in ("", "[N/A]") else None}
        _SPARK_GPU.update({"ts": now, "cards": [{
            "index": 0, "name": "NVIDIA GB10 (统一内存)",
            "used_mib": round(sum(p["mib"] for p in procs), 0),
            "total_mib": int((CFG.get("spark") or {}).get("mem_total_mib") or 122880),
            "util": gpu.get("util"), "temp": gpu.get("temp"), "power": gpu.get("power"),
            "procs": procs}], "error": None})
    except Exception as e:  # noqa: BLE001
        _SPARK_GPU.update({"ts": now, "error": repr(e)[:160]})


def spark_snapshot() -> dict:
    with _GPU_LOCK:
        return {"cards": list(_SPARK_GPU["cards"]), "error": _SPARK_GPU["error"],
                "ts": _SPARK_GPU["ts"], "up": bool(_SPARK_GPU["cards"])}


def tool_search(q: str, n: int = 5) -> dict:
    """联网搜索：Bing 的 RSS 出口（cn.bing.com 可达；duckduckgo 在本容器不通）。"""
    q = (q or "").strip()
    if not q:
        return {"query": "", "results": [], "error": "empty query"}
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": q, "format": "rss"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; strata-hub)"})
    with urllib.request.urlopen(req, timeout=12) as r:
        xml = r.read().decode("utf-8", "replace")

    def clean(s: str) -> str:
        s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
        s = re.sub(r"<[^>]+>", "", s)
        return html.unescape(s).strip()

    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S)[:n]:
        def g(tag):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", it, re.S)
            return clean(m.group(1)) if m else ""
        out.append({"title": g("title"), "url": g("link"), "snippet": g("description")[:400]})
    return {"query": q, "results": [x for x in out if x["title"] or x["url"]]}


# ======================= 外部服务监控结束 =======================

def concurrency() -> dict:
    """GET /concurrency：各通道引擎状态 + 代理侧全部在途/最近请求（前端 Monitor 页的数据源）。"""
    now = time.time()
    with REG_LOCK:
        live = [_entry_view(dict(e), now) for e in REG.values()]
        recent = [_entry_view(dict(e), now) for e in list(RECENT)[-60:]]
    with LANE_LOCK:
        cache = {u: dict(v) for u, v in LANE_CACHE.items()}
    lanes = []
    for u in upstreams():
        c = cache.get(u) or {}
        st, mt = c.get("status") or {}, c.get("metrics") or {}
        machine = st.get("machine") or {}
        lanes.append({
            "url": u, "label": lane_label(u), "model": st.get("model"),
            # 窗口：/health 的静态值优先（引擎未加载时别处的 0 会把占用率算成「无」）
            "max_context": (c.get("health") or {}).get("max_context") or (st.get("context") or {}).get("max_positions"),
            "up": bool(st) and not c.get("error"), "loaded": st.get("loaded"),
            "serving_slots": (st.get("concurrency") or {}).get("serving"),
            "in_flight": sum(1 for e in live if e["lane"] == u),
            "engine_in_flight": (st.get("activity") or {}).get("in_flight"),
            "uptime_s": st.get("uptime_s"), "gpu": machine.get("gpu"), "ram": machine.get("ram"),
            # 逐卡读数（带卡号）。老引擎不下发 `gpus` → 这里是 None，前端退回用 `gpu`（那张卡没号，标「未上报」）；
            # 层切分（一条道两张卡）时 `gpu` 是合计值、`gpus` 才是一张张的，监控页必须按 `gpus` 画。
            "gpus": machine.get("gpus"),
            "live": mt.get("live"), "totals": mt.get("totals"),
            # 每条道自己的引擎事实（窗口多大、专家驻留占多少显存）。监控页的「上下文占用」要**一条道一行**，
            # 而 `/metrics` 被会话粘性钉在一条道上，页面自己拿不到另一条道的窗口 —— 只能在这里顺路带上来。
            "engine": mt.get("engine"),
            "requests": (mt.get("requests") or [])[:12], "history": mt.get("history"),
            "error": c.get("error"), "last_seen": c.get("ts"), "last_ok": c.get("last_ok")})
    with SESSIONS_LOCK:
        sessions = len(SESSIONS)
    with EMB_LOCK:
        embed_cache = len(EMB_CACHE)
    return {"time": now, "mode": "single" if len(lanes) <= 1 else "multi", "lanes": lanes,
            "aggregate": {"lanes": len(lanes), "lanes_up": sum(1 for l in lanes if l["up"]),
                          "in_flight": len(live),
                          "generating": sum(1 for e in live if e["state"] == "streaming"),
                          "est_tokens_out": sum(e["est_tokens"] for e in live),
                          "tok_s": round(sum(e["tok_s"] or 0 for e in live), 1)},
            "in_flight": live, "recent": recent,
            "services": services_snapshot(), "gpus": gpu_snapshot(), "spark": spark_snapshot(),
            "proxy": {"uptime_s": int(now - START_TS), "counters": dict(COUNTERS), "sessions": sessions,
                      "embed_queue": EMB_QUEUE.qsize(), "embed_cache": embed_cache,
                      "metrics_lane": pick_upstream("/metrics")}}


def health() -> dict:
    with SESSIONS_LOCK:
        sess_info = [{"key": s.key[:24], "chunks": len(s.chunks), "vectors": len(s.vectors),
                      "assemblies": s.assemblies, "age_s": int(time.time() - s.created)} for s in SESSIONS.values()]
    ups = {}
    for u in upstreams():
        try:
            r = http_json(f"{u}/health", None, 5)
            ups[u] = r.get("status", "?")
        except Exception as e:
            ups[u] = f"down: {e!r}"
    up = "ok" if all(v == "ok" for v in ups.values()) else ("部分不可用" if any(v == "ok" for v in ups.values()) else "down")
    with EMB_LOCK:
        emb = len(EMB_CACHE)
    return {"status": "ok", "uptime_s": int(time.time() - START_TS), "upstream": up, "upstreams": ups,
            "model_routes": CFG.get("model_routes") or {},
            "infer_store": infer_store.status(),
            "sessions": sess_info, "embed_cache": emb, "embed_dim": EMB_DIM,
            "embed_queue": EMB_QUEUE.qsize(), "counters": dict(COUNTERS),
            "config": {k: CFG[k] for k in ("rag_enabled", "max_context", "recent_window_tokens", "chunk_tokens",
                                           "assembly_ttl_s", "score_w_rerank",
                                           "structure_chunking", "context_expand_ratio",
                                           "block_drift_rebuild", "block_relevance_min",
                                           "image_tokens", "clamp_margin_tokens", "retry_on_overfit",
                                           "documents")}}


# ============================ 服务端 Agent Runner（2026-11-05） ============================
#
# 为什么放在服务端：agent 的运行不能绑在浏览器标签页上。切页面/刷新/关标签后，
# 模型流和工具执行都要继续，页面回来时按事件 seq 重放即可。客户端只负责显示。
#
# 事件存两份：内存（在线客户端实时等） + /home/cloud/logs/agent-runs/<id>.jsonl（刷新重放）。
AGENT_RUNS: dict[str, dict] = {}
AGENT_LOCK = threading.Lock()
AGENT_DIR = Path(CFG.get("agent_runs_dir") or "/home/cloud/logs/agent-runs")
AGENT_MAX_ROUNDS = 8
AGENT_KEEP_S = 86400


def _agent_log(run_id: str) -> Path:
    d = AGENT_DIR / run_id[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}.jsonl"


def _agent_meta_path(run_id: str) -> Path:
    return _agent_log(run_id).with_suffix(".json")


def _agent_write_meta(run: dict) -> None:
    try:
        p = _agent_meta_path(run["id"])
        p.write_text(json.dumps({k: run.get(k) for k in
                                 ("id", "uid", "chat_id", "status", "created", "finished",
                                  "model", "round", "error", "usage")}, ensure_ascii=False),
                     encoding="utf-8")
    except OSError:
        pass


def _agent_emit(run: dict, typ: str, **fields) -> None:
    with AGENT_LOCK:
        run["seq"] = int(run.get("seq") or 0) + 1
        ev = {"seq": run["seq"], "type": typ, "ts": time.time(), **fields}
        run["events"].append(ev)
        if len(run["events"]) > 5000:
            run["events"] = run["events"][-4000:]
        try:
            with open(run["log"], "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except OSError:
            pass
        cond = run.get("cond")
    if cond is not None:
        with cond:
            cond.notify_all()


def agent_get(run_id: str) -> dict | None:
    with AGENT_LOCK:
        r = AGENT_RUNS.get(run_id)
        if r is not None:
            return r
    # 代理重启后内存没了：从磁盘恢复事件（状态按 meta）
    p = _agent_log(run_id)
    if not p.exists():
        return None
    events = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except Exception:
                pass
    except OSError:
        return None
    meta = {}
    try:
        meta = json.loads(_agent_meta_path(run_id).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"id": run_id, "events": events, "seq": events[-1]["seq"] if events else 0,
            "status": meta.get("status") or "lost", "log": str(p), "cond": None,
            "uid": meta.get("uid"), "chat_id": meta.get("chat_id")}


def agent_events_after(run: dict, cursor: int) -> list:
    with AGENT_LOCK:
        return [e for e in run.get("events", []) if int(e.get("seq") or 0) > cursor]


def agent_cleanup() -> None:
    now = time.time()
    with AGENT_LOCK:
        for rid, r in list(AGENT_RUNS.items()):
            if r.get("status") != "running" and now - float(r.get("created") or 0) > AGENT_KEEP_S:
                AGENT_RUNS.pop(rid, None)


# --- 工具 schema：和服务端执行器一一对应
AGENT_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "list_files", "description": "列出工作空间文件（可给子目录 path）",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "read_file", "description": "读取工作空间里的一个文本文件",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "写/覆盖一个文件（自动建目录）",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}}, "required": ["path", "text"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "把文件里的 old_text 替换成 new_text（先读再改）",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {"name": "search_files", "description": "在工作空间里全文搜索",
     "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}},
    {"type": "function", "function": {"name": "run_code", "description": "运行代码/小项目；env: python/ds/node/cpp/lab；改 lab 代码后验证用 in_place=true, cwd=llm-study-lab",
     "parameters": {"type": "object", "properties": {"env": {"type": "string"}, "code": {"type": "string"},
      "timeout": {"type": "number"}, "in_place": {"type": "boolean"}, "cwd": {"type": "string"},
      "force": {"type": "boolean"}, "files": {"type": "object"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "start_job", "description": "启动后台任务（训练/长跑），立刻返回 job id",
     "parameters": {"type": "object", "properties": {"args": {"type": "array", "items": {"type": "string"}},
      "env": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "number"}, "force": {"type": "boolean"}}, "required": ["args"]}}},
    {"type": "function", "function": {"name": "job_status", "description": "看后台任务状态与增量日志",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "offset": {"type": "number"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "job_stop", "description": "停止后台任务",
     "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
    {"type": "function", "function": {"name": "kb_add", "description": "把文本/文件/网页存进个人知识库",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "text": {"type": "string"},
      "path": {"type": "string"}, "url": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "kb_search", "description": "检索个人知识库（向量召回 + 精排）",
     "parameters": {"type": "object", "properties": {"q": {"type": "string"}, "k": {"type": "number"}}, "required": ["q"]}}},
    {"type": "function", "function": {"name": "search_web", "description": "联网搜索，返回标题、链接与摘要",
     "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}},
    {"type": "function", "function": {"name": "fetch_url", "description": "打开网页读正文",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "image_search", "description": "搜图，返回图片直链",
     "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}},
    {"type": "function", "function": {"name": "service_status", "description": "查看全部 AI 服务与 GPU 的实时状态",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "generate_image", "description": "生成图片并保存到工作空间；refs 可给参考图 URL",
     "parameters": {"type": "object", "properties": {"prompt": {"type": "string"},
      "size": {"type": "string"}, "steps": {"type": "integer"}, "refs": {"type": "array", "items": {"type": "string"}}}, "required": ["prompt"]}}},
]


def _agent_tool_uid(uid: str) -> str:
    return uid or "anon"


def agent_execute_tool(uid: str, name: str, args: dict) -> tuple[dict, str | None]:
    """返回 (结果 dict, 图片 URL 或 None)。任何异常都变成 {"error": ...} 交给模型。"""
    a = args or {}
    if name == "list_files":
        entries = ws_tree(uid)
        base = str(a.get("path") or "").strip().strip("/")
        if base:
            entries = [e for e in entries if e["path"] == base or e["path"].startswith(base + "/")]
        return {"entries": [e["path"] + ("/" if e["dir"] else "") for e in entries]}, None
    if name == "read_file":
        f = ws_path(uid, str(a.get("path") or ""))
        if not f.is_file():
            return {"error": "没有这个文件"}, None
        raw = f.read_bytes()
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".docx", ".xlsx", ".xls", ".pptx"):
            return {"path": a.get("path"), "binary": True, "bytes": len(raw)}, None
        return {"path": a.get("path"), "text": raw.decode("utf-8", "replace")[:40000]}, None
    if name == "write_file":
        f = ws_path(uid, str(a.get("path") or ""))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(a.get("text") or ""), encoding="utf-8")
        return {"ok": True, "path": a.get("path"), "bytes": len(str(a.get("text") or ""))}, None
    if name == "edit_file":
        f = ws_path(uid, str(a.get("path") or ""))
        text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        old = str(a.get("old_text") or "")
        if not old:
            return {"ok": False, "error": "old_text 不能为空"}, None
        at = text.find(old)
        if at < 0:
            return {"ok": False, "error": "没找到 old_text（先 read_file 确认原文，包括缩进）"}, None
        next_text = text[:at] + str(a.get("new_text") or "") + text[at + len(old):]
        f.write_text(next_text, encoding="utf-8")
        return {"ok": True, "path": a.get("path"), "replaced_at": at}, None
    if name == "search_files":
        return {"hits": ws_search(uid, str(a.get("q") or ""))}, None
    if name == "run_code":
        r = run_code(uid, str(a.get("env") or "python"), str(a.get("code") or ""),
                     float(a.get("timeout") or 60), files=a.get("files"),
                     in_place=bool(a.get("in_place")), cwd_rel=a.get("cwd") or None,
                     force=bool(a.get("force")))
        return r, None
    if name == "start_job":
        return job_start(uid, str(a.get("env") or "lab"), a.get("args") or [],
                         cwd_rel=a.get("cwd") or None, timeout_s=float(a.get("timeout") or 1800),
                         force=bool(a.get("force"))), None
    if name == "job_status":
        return job_status(uid, str(a.get("id") or ""), int(a.get("offset") or 0)), None
    if name == "job_stop":
        return job_stop(uid, str(a.get("id") or "")), None
    if name == "kb_add":
        return kb_add(uid, str(a.get("name") or ""), text=a.get("text"), path=a.get("path"), url=a.get("url")), None
    if name == "kb_search":
        return kb_search(uid, str(a.get("q") or ""), int(a.get("k") or 6)), None
    if name == "search_web":
        return tool_search(str(a.get("q") or ""), 5), None
    if name == "fetch_url":
        return tool_fetch(str(a.get("url") or ""), 8000), None
    if name == "image_search":
        return tool_image_search(str(a.get("q") or ""), 6), None
    if name == "service_status":
        cur = concurrency()
        return {"lanes": [{"label": l.get("label"), "up": l.get("up"), "model": (l.get("engine") or {}).get("model")}
                          for l in cur.get("lanes", [])],
                "services": [{"label": s.get("label"), "up": s.get("up"), "calls": s.get("calls")}
                             for s in cur.get("services", [])],
                "gpus": [{"index": g.get("index"), "used_gib": round((g.get("used_mib") or 0) / 1024, 1),
                          "util": g.get("util")} for g in cur.get("gpus", [])]}, None
    if name == "generate_image":
        return _agent_generate_image(uid, a)
    return {"error": f"unknown tool {name}"}, None


def _agent_image_size(raw) -> str:
    """把模型给的尺寸收敛到 Spark 能接受的档位。

    Spark 的约束：宽高都必须是 16 的倍数，steps 1..8；模型经常给 "16:9"、
    "1664x936"（936 不是 16 的倍数）或 40/50 步，直接 400。这里按最接近的
    官方档位归一，宁可换一档尺寸也不让请求失败。
    """
    s = str(raw or "").strip().lower().replace("×", "x").replace(" ", "")
    w = h = 0
    is_ratio = (":" in s or "/" in s)
    m = re.match(r"^(\d+)\s*[:/]\s*(\d+)$", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r"^(\d+)\s*x\s*(\d+)$", s)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
    # 像素尺寸（不是 "16:9" 这种比例）且合法：直接用，只把边取到 16 的倍数
    if not is_ratio and w > 0 and h > 0 and w * h <= 4_600_000 and min(w, h) >= 128:
        w2 = max(256, min(2752, int(round(w / 16.0)) * 16))
        h2 = max(256, min(2048, int(round(h / 16.0)) * 16))
        if w2 % 16 == 0 and h2 % 16 == 0 and w2 * h2 <= 4_600_000:
            return f"{w2}x{h2}"
    ratio = (w / h) if (w > 0 and h > 0) else 1.0
    area = (w * h) if (w > 0 and h > 0 and not is_ratio) else 1024 * 1024
    allowed = [(512, 512), (832, 480), (1024, 1024), (1024, 768), (1024, 1536),
               (832, 1248), (768, 1024), (1280, 720), (720, 1280), (2048, 864),
               (1664, 1664), (2048, 2048), (2752, 1536)]
    best = min(allowed, key=lambda x: (abs(math.log((x[0] / x[1]) / ratio)), abs(x[0] * x[1] - area)))
    return f"{best[0]}x{best[1]}"


def _agent_generate_image(uid: str, a: dict) -> tuple[dict, str | None]:
    prompt = str(a.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt 不能为空"}, None
    base = str((CFG.get("spark") or {}).get("base_url") or "https://spark.ssemarket.cn").rstrip("/")
    key = spark_key()
    size = _agent_image_size(a.get("size"))
    try:
        steps = int(a.get("steps") or 8)
    except Exception:
        steps = 8
    steps = max(1, min(steps, 8))
    refs = [str(x) for x in (a.get("refs") or []) if str(x).startswith(("http://", "https://"))][:1]
    payload = {"prompt": prompt, "size": size, "steps": steps}
    path = "/image/v1/images/generations"
    if refs:
        try:
            with urllib.request.urlopen(urllib.request.Request(refs[0], headers={"User-Agent": "Mozilla/5.0"}),
                                        timeout=30) as rr:
                raw = rr.read(20 * 1024 * 1024)
            payload["image"] = "data:image/png;base64," + base64.b64encode(raw).decode()
            path = "/image/v1/images/edits"
        except Exception:
            pass
    def call(p: str, data_obj: dict):
        req = urllib.request.Request(base + p, data=json.dumps(data_obj).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + key} if key else {})})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        data = call(path, payload)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        # 图生图 400（参考图格式/尺寸问题）就退回纯文生图，别让整个工具失败
        if path.endswith("/edits"):
            payload.pop("image", None)
            try:
                data = call("/image/v1/images/generations", payload)
            except urllib.error.HTTPError as e2:
                body2 = e2.read().decode("utf-8", "replace")[:500]
                return {"error": f"生图服务 {e2.code}: {body2}（参考图版本也失败：{body}）"}, None
            except Exception as e2:  # noqa: BLE001
                return {"error": f"生图请求失败: {e2!r}（参考图版本也失败：{body}）"}, None
        else:
            return {"error": f"生图服务 {e.code}: {body}"}, None
    except Exception as e:  # noqa: BLE001
        return {"error": f"生图请求失败: {e!r}"}, None
    d = (data.get("data") or [{}])[0]
    b64 = d.get("b64_json")
    if not b64:
        return {"error": "生图服务没有返回图片数据"}, None
    payload_bytes = base64.b64decode(b64)
    name = f"{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
    (ws_dir(uid) / "images" / name).write_bytes(payload_bytes)
    url = ws_image_url(uid, name)
    return {"ok": True, "seed": d.get("seed"), "image_url": url, "path": f"images/{name}",
            "size": size, "steps": steps, "note": "图片已生成并显示在对话里"}, url

def _agent_upstream(model: str) -> str:
    routes = CFG.get("model_routes") or {}
    return str(routes.get(model) or CFG.get("default_upstream") or CFG["upstream"]).rstrip("/")


def _agent_engine_stream(up: str, body: dict):
    req = urllib.request.Request(up + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                yield json.loads(data)
            except Exception:
                continue


def agent_worker(run: dict) -> None:
    uid = run["uid"]
    messages = list(run["messages"])
    model = run["model"]
    up = _agent_upstream(model)
    empty_retry = 0
    length_retry = 0
    try:
        for rnd in range(1, int(run.get("max_rounds") or AGENT_MAX_ROUNDS) + 1):
            if run["cancel"].is_set():
                _agent_emit(run, "cancelled")
                run["status"] = "cancelled"
                return
            run["round"] = rnd
            body = {"model": model, "messages": messages, "stream": True,
                    "tools": AGENT_TOOL_SCHEMAS, "tool_choice": "auto",
                    "max_tokens": 16384, "temperature": 0}
            if run.get("params"):
                body.update({k: v for k, v in run["params"].items() if k in ("temperature", "top_p", "top_k", "reasoning_effort", "max_tokens")})
            content_parts, reasoning_parts = [], []
            calls: dict[int, dict] = {}
            finish = None
            usage = None
            for j in _agent_engine_stream(up, body):
                if run["cancel"].is_set():
                    break
                if j.get("usage"):
                    usage = j["usage"]
                    _agent_emit(run, "usage", usage=usage)
                ch0 = (j.get("choices") or [{}])[0]
                if ch0.get("finish_reason"):
                    finish = ch0["finish_reason"]
                d = ch0.get("delta") or {}
                if d.get("reasoning_content"):
                    piece = d["reasoning_content"]
                    reasoning_parts.append(piece)
                    _agent_emit(run, "reasoning", content=piece, round=rnd)
                if d.get("content"):
                    piece = d["content"]
                    content_parts.append(piece)
                    _agent_emit(run, "content", content=piece, round=rnd)
                for tc in d.get("tool_calls") or []:
                    idx = int(tc.get("index") or 0)
                    cur = calls.setdefault(idx, {"id": tc.get("id") or f"call_{rnd}_{idx}",
                                                 "name": "", "arguments": ""})
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        cur["name"] += fn["name"]
                    if fn.get("arguments"):
                        cur["arguments"] += fn["arguments"]
            content = "".join(content_parts)
            reasoning = "".join(reasoning_parts)
            if run["cancel"].is_set():
                run["status"] = "cancelled"
                _agent_emit(run, "cancelled")
                return
            if not calls:
                # 只有 thinking 没内容：服务端直接补一句再试一次，不把空回答交给客户端
                if not content.strip() and reasoning.strip() and empty_retry < 1:
                    empty_retry += 1
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({"role": "user", "content": EMPTY_HINT_FOR_AGENT})
                    continue
                # 长度截断：续写一次
                if finish == "length" and content.strip() and length_retry < 2:
                    length_retry += 1
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": CONTINUE_HINT_FOR_AGENT})
                    continue
                run["usage"] = usage
                _agent_emit(run, "done", finish=finish or "stop", usage=usage, round=rnd)
                run["status"] = "done"
                run["finished"] = time.time()
                return
            tool_calls = []
            for idx in sorted(calls):
                c = calls[idx]
                try:
                    args = json.loads(c["arguments"] or "{}")
                except Exception:
                    args = {}
                tool_calls.append({"id": c["id"], "type": "function",
                                   "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}})
                _agent_emit(run, "tool_call", id=c["id"], name=c["name"], arguments=args, round=rnd)
            messages.append({"role": "assistant", "content": content,
                             "tool_calls": tool_calls})
            for idx in sorted(calls):
                c = calls[idx]
                if run["cancel"].is_set():
                    break
                try:
                    args = json.loads(c["arguments"] or "{}")
                except Exception:
                    args = {}
                t0 = time.perf_counter()
                try:
                    result, image = agent_execute_tool(uid, c["name"], args)
                    ok = not (isinstance(result, dict) and result.get("error"))
                except Exception as e:  # noqa: BLE001
                    result, image, ok = {"error": f"{type(e).__name__}: {e}"}, None, False
                text = json.dumps(result, ensure_ascii=False)
                _agent_emit(run, "tool_result", id=c["id"], name=c["name"], ok=ok, text=text,
                            image=image, ms=round((time.perf_counter() - t0) * 1000), round=rnd)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
            # 本轮结束，继续下一轮
        run["status"] = "done"
        _agent_emit(run, "done", finish="limit", usage=None, round=run.get("round"))
        run["finished"] = time.time()
    except Exception as e:  # noqa: BLE001
        run["status"] = "error"
        run["error"] = f"{type(e).__name__}: {e}"
        _agent_emit(run, "error", message=run["error"])
        run["finished"] = time.time()
    finally:
        _agent_write_meta(run)


EMPTY_HINT_FOR_AGENT = "上一次回答只输出了思考就结束了，没有给用户任何内容。请直接给出最终回答；如果需要工具，请正常发起工具调用，不要把工具调用写进思考里。"
CONTINUE_HINT_FOR_AGENT = "上一条回答因长度上限被截断。请从断点自然接着写，不要重复已写内容，也不要重新开头或道歉。"


def agent_start(uid: str, chat_id: str, model: str, messages: list, max_rounds: int = AGENT_MAX_ROUNDS,
                params: dict | None = None) -> str:
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages 不能为空")
    if len(json.dumps(messages, ensure_ascii=False)) > 4 * 1024 * 1024:
        raise ValueError("上下文过大（上限 4MB）")
    rid = "ar" + uuid.uuid4().hex[:16]
    run = {"id": rid, "uid": uid, "chat_id": chat_id, "model": model,
           "messages": messages, "max_rounds": max_rounds, "params": params or {},
           "status": "running", "seq": 0, "events": [], "created": time.time(),
           "log": str(_agent_log(rid)), "cancel": threading.Event()}
    run["cond"] = threading.Condition(AGENT_LOCK)
    with AGENT_LOCK:
        AGENT_RUNS[rid] = run
    _agent_write_meta(run)
    agent_cleanup()
    threading.Thread(target=agent_worker, args=(run,), daemon=True).start()
    return rid


def main():
    global CFG, API_KEY, OAUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    a = ap.parse_args()
    CFG = load_config(a.config)
    API_KEY = Path(CFG["api_key_file"]).read_text().strip()
    OAUTH = ssecho_auth.build(CFG, Path(a.config).parent)
    if OAUTH.cfg.configured():
        log(f"oauth: app_id={OAUTH.cfg.app_id} redirect={OAUTH.cfg.redirect_uri}")
    else:
        log("oauth: 未配置（缺 " + "、".join(OAUTH.cfg.missing()) + "）—— 登录接口会返回 503，"
            "此时只能在「关于 → 设置」里填 API token")
    threading.Thread(target=embedding_worker, daemon=True).start()
    threading.Thread(target=lane_poller, daemon=True).start()
    threading.Thread(target=svc_poller, daemon=True).start()   # 外部服务监控（face/邻居/Spark）   # 并发监控：后台轮询各通道引擎状态
    infer_store.init(CFG)                                       # 推理记录：元信息 + 内容留档（后台写盘/上传）
    srv = ThreadingHTTPServer((CFG["listen_host"], CFG["listen_port"]), Handler)
    # 客户端连接超时（防 keep-alive 线程/FD 泄漏）；配 0 或负数 = 关闭（回滚旋钮）
    tmo = float(CFG.get("client_timeout_s") or 0)
    Handler.timeout = tmo if tmo > 0 else None
    log(f"rag proxy on :{CFG['listen_port']} -> {CFG['upstream']} (rag_enabled={CFG.get('rag_enabled', True)}, "
        f"embed {CFG['embed_url']}, rerank {CFG['rerank_url']}, client_timeout={Handler.timeout})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
