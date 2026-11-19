#!/usr/bin/env python3
"""推理记录模块的回归测试（全是本地假件，不碰线上的桶、不碰线上实例）。

覆盖的都是**错了会很难受**的点：
  · 流式 SSE 解析（OpenAI 与 Anthropic 两种体裁）——内容、思考、工具调用、usage、timings；
  · 元信息字段（读/缓存/写的 token 数、TTFT、tok/s、组装信息）；
  · 分段封口 → 上传 → 本机缓存清理；
  · 内容读取：本机分段在 / 不在（不在就去对象存储取回）两条路；
  · **热路径绝不能被拖住**：队列满时 finish() 要立刻返回并计数，capture 超限要截断标记。

用法：/home/cloud/Strata/.venv/bin/python test_infer_store.py
"""
import gzip
import json
import os
import pathlib
import queue
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import infer_store as S                                     # noqa: E402

FAILS = []


def ck(name, expect, got, extra=""):
    if expect == got:
        print(f"  PASS  {name:<52} {got} {extra}")
    else:
        print(f"  FAIL  {name:<52} 期望 {expect}，实际 {got} {extra}")
        FAILS.append(name)


def ck_true(name, cond, extra=""):
    ck(name, True, bool(cond), extra)


class FakeMinio:
    """假的 S3：对象落在本地目录里，够用就行（只要 fput_object / fget_object）。"""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.put = []

    def fput_object(self, bucket, key, path, content_type=None):
        p = self.root / bucket / key
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, p)
        self.put.append(key)

    def fget_object(self, bucket, key, path):
        p = self.root / bucket / key
        if not p.exists():
            raise FileNotFoundError(key)
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, path)


SSE = "\n".join([
    'data: {"id":"c1","choices":[{"index":0,"delta":{"reasoning_content":"先想一下"},"finish_reason":null}]}',
    "",
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}',
    "",
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"，世界"},"finish_reason":null}]}',
    "",
    'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1200,"completion_tokens":8,"total_tokens":1208,'
    '"prompt_tokens_details":{"cached_tokens":1100}},'
    '"timings":{"cache_n":1100,"prompt_n":100,"prompt_per_second":500.0,'
    '"predicted_n":8,"predicted_per_second":42.5}}',
    "",
    "data: [DONE]",
    "",
]).encode("utf-8")

# 同一段流式，但**不带** timings：用来验"引擎没给速度时，我们自己按首帧后时长算"
SSE_NO_TIMINGS = "\n".join([
    'data: {"id":"c1","choices":[{"index":0,"delta":{"reasoning_content":"先想一下"},"finish_reason":null}]}',
    "",
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}',
    "",
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"，世界"},"finish_reason":null}]}',
    "",
    'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1200,"completion_tokens":8}}',
    "",
    "data: [DONE]",
    "",
]).encode("utf-8")

ANTHROPIC_SSE = "\n".join([
    'event: content_block_delta',
    'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"嗯"}}',
    "",
    'event: content_block_delta',
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"答案"}}',
    "",
    'event: message_delta',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"input_tokens":300,"output_tokens":2,"cache_read_input_tokens":250}}',
    "",
]).encode("utf-8")


def make_rec(stream=True, body='{"messages":[{"role":"user","content":"你好"}]}'.encode("utf-8"), raw=SSE):
    rec = S.begin("/v1/chat/completions", body, {"User-Agent": "test-agent/1.0"})
    S.note_route(rec, model="strata-b", lane="http://127.0.0.1:8092", lane_label="普道",
                 session="abcdef012345", mode="assembled", est=1500, recent_tokens=1000,
                 memory_tokens=400, block_reused=True, retrieve_ms=12.5, stream=stream, http_status=200)
    S.note_request(rec, {"messages": [{"role": "user", "content": "你好"}]},
                   {"messages": [{"role": "user", "content": "你好"}], "max_tokens": 64},
                   client_bytes=len(body), sent_bytes=80)
    rec["first_chunk_at"] = rec["ts"] + 0.25
    S.capture(rec, raw)
    rec["ended_at"] = rec["ts"] + 1.25
    S.finish(rec, "completed")
    return rec


def wait_rows(n, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with S.DB_LOCK:
            c = S._conn().execute("SELECT COUNT(*) c FROM infer").fetchone()["c"]
        if c >= n:
            return c
        time.sleep(0.05)
    return c


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="infer-store-test-"))
    fake_root = tmp / "fake-minio"
    fake = FakeMinio(fake_root)
    cfg = {"infer_store": {
        "enabled": True, "dir": str(tmp / "segs"), "db": str(tmp / "infer.db"),
        "minio_prefix": "infer-test", "seg_max_mb": 0.00002, "seg_max_s": 3600,
        "queue_max": 5000, "capture_max_mb": 16, "local_cache_gb": 1.0,
        "local_cache_days": 7, "hot_rows": 10}}
    S.init(cfg)
    S.set_minio_factory(lambda: (fake, "test-bucket"))

    print("推理记录模块（临时目录 %s）" % tmp)

    # ---- 一条流式记录：字段与解析
    rec = make_rec()
    wait_rows(1)
    row = S.detail(rec["id"])
    ck_true("记录已入库", row is not None)
    ck("prompt_tokens（读了多少）", 1200, row["prompt_tokens"])
    ck("cached_tokens（缓存里已有的）", 1100, row["cached_tokens"])
    ck("completion_tokens（写了多少）", 8, row["completion_tokens"])
    ck("decode_tok_s（引擎自报优先）", 42.5, row["decode_tok_s"])
    ck("prompt_tok_s（prefill 速度）", 500.0, row["prompt_tok_s"])
    ck("ttft_ms", 250.0, row["ttft_ms"])
    ck("total_ms", 1250.0, row["total_ms"])
    ck("ctx 组装模式", "assembled", row["mode"])
    ck("组装块复用", 1, row["block_reused"])
    ck("记忆块 token", 400, row["memory_tokens"])
    ck("检索耗时", 12.5, row["retrieve_ms"])
    ck("正文长度（你好，世界=5 字）", 5, row["content_chars"])
    ck("思考长度（先想一下=4 字）", 4, row["reasoning_chars"])
    ck("结束原因", "stop", row["finish_reason"])
    ck("htt p状态", 200, row["http_status"])
    ck("UA 已记录", "test-agent/1.0", row["ua"])
    ck("L1 内存里有它", 1, sum(1 for r in S.hot() if r["id"] == rec["id"]))

    # ---- 内容：本机分段里能读回完整记录
    full = S.raw(rec["id"])
    ck("完整记录的正文", "你好，世界", (full.get("response") or {}).get("content"))
    ck("完整记录的思考", "先想一下", (full.get("response") or {}).get("reasoning"))
    ck("完整记录里带客户端原始请求",
       "你好", ((full.get("request") or {}).get("client") or {}).get("messages", [{}])[0].get("content"))
    ck("完整记录里带真正发出去的请求",
       "你好", ((full.get("request") or {}).get("sent") or {}).get("messages", [{}])[0].get("content"))
    ck("引擎 usage 原样留着", 1200, ((full.get("engine") or {}).get("usage") or {}).get("prompt_tokens"))

    # ---- Anthropic 体裁
    rec2 = S.begin("/v1/messages", b'{"messages":[]}', {})
    S.note_route(rec2, model="qwen3.8-flash", lane="http://127.0.0.1:8092", mode="est", est=300,
                 stream=True)
    S.capture(rec2, ANTHROPIC_SSE)
    rec2["first_chunk_at"] = rec2["ts"] + 0.1
    rec2["ended_at"] = rec2["ts"] + 0.6
    S.finish(rec2, "completed")
    wait_rows(2)
    r2 = S.detail(rec2["id"])
    ck("Anthropic：正文", 2, r2["content_chars"])
    ck("Anthropic：思考", 1, r2["reasoning_chars"])
    ck("Anthropic：input_tokens", 300, r2["prompt_tokens"])
    ck("Anthropic：cache_read", 250, r2["cached_tokens"])
    ck("Anthropic：output_tokens", 2, r2["completion_tokens"])
    ck("Anthropic：stop_reason", "end_turn", r2["finish_reason"])

    # ---- 分段封口 + 上传（seg_max_mb 故意设得极小 → 每条都封口）
    t0 = time.time()
    while time.time() - t0 < 8 and len(fake.put) < 2:
        time.sleep(0.05)
    ck_true("分段已上传到（假）对象存储", len(fake.put) >= 2, f"{len(fake.put)} 个对象")
    ck_true("对象落在 infer-test/ 前缀下", all(k.startswith("infer-test/") for k in fake.put),
            fake.put[0] if fake.put else "")
    with S.DB_LOCK:
        nup = S._conn().execute("SELECT COUNT(*) c FROM segments WHERE uploaded=1").fetchone()["c"]
    ck_true("segments 表标记已归档", nup >= 2, f"{nup} 个")

    # ---- 本机分段没了 → 从对象存储取回（L4 兜底）
    seg = S.detail(rec["id"])["seg"]
    date = S.detail(rec["id"])["iso"][:10]
    local = tmp / "segs" / date / seg
    ck_true("本机分段文件在", local.exists(), str(local))
    local.unlink()
    again = S.raw(rec["id"])
    ck("删掉本机副本后从对象存储取回", "你好，世界", (again.get("response") or {}).get("content"))
    ck_true("取回后会重新缓存在本机", local.exists())

    # ---- 列表 / 统计
    lst = S.query(limit=10)
    ck("列表总数", 2, lst["total"])
    ck("列表按时间倒序（最新在前）", rec2["id"], lst["rows"][0]["id"])
    ck("按模型过滤", 1, S.query(model="qwen3.8-flash")["total"])
    ck("按状态过滤", 0, S.query(state="error")["total"])
    ck("按组装的 raw 标记过滤", 0, S.query(mode="assembled", model="nope")["total"])
    st = S.stats(7)
    ck("统计：条数", 2, st["total"])
    ck("统计：输出 token 合计", 10, st["out_tokens"])
    ck("统计：状态分布有 completed", True, any(x["state"] == "completed" for x in st["by_state"]))
    ck_true("统计：带存储状态", "minio_ok" in st["store"], str(st["store"].get("minio_ok")))

    # ---- 抓取上限：超了要标记截断，且不无限吃内存
    S._CFG["capture_max_mb"] = 0.0005                          # ~512 B
    big = S.begin("/v1/chat/completions", b"{}", {})
    S.capture(big, b"x" * 5000)
    ck("超限后标记截断", 1, 1 if big["trunc"] else 0)
    ck("缓冲不会超过上限", True, len(big["buf"]) <= 600)
    ck("字节计数仍按真实流量算", 5000, big["bytes_out"])

    # ---- 非流式且引擎没给 timings：**不许编**速度（曾经算出过 40 万 tok/s）
    S._CFG["capture_max_mb"] = 16                              # 上面测过截断，这里要恢复正常上限
    S._CFG["seg_max_mb"] = 64                                  # 后面这几条别再把分段切碎
    S._CFG["seg_max_s"] = 3600
    rec4 = S.begin("/v1/chat/completions", b'{"messages":[]}', {})
    S.note_route(rec4, model="strata-b", mode="est", stream=False)
    S.capture(rec4, json.dumps({"choices": [{"message": {"role": "assistant", "content": "答案"},
                                             "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 40, "completion_tokens": 20}}).encode())
    rec4["first_chunk_at"] = rec4["ts"] + 1.2                  # 非流式：这就是整段到达的时刻
    rec4["ended_at"] = rec4["ts"] + 1.2
    S.finish(rec4, "completed")
    wait_rows(3)
    r4 = S.detail(rec4["id"])
    ck("非流式且无 timings → 速度留空（不编）", None, r4["decode_tok_s"])
    ck("非流式的 token 数照旧有", 20, r4["completion_tokens"])

    # ---- 流式且无 timings：用首帧之后的时长算，是**合法**的
    rec5 = S.begin("/v1/chat/completions", b'{"messages":[]}', {})
    S.note_route(rec5, model="strata-b", mode="est", stream=True)
    S.capture(rec5, SSE_NO_TIMINGS)
    rec5["first_chunk_at"] = rec5["ts"] + 0.2
    rec5["ended_at"] = rec5["ts"] + 2.2                        # 解码 2 秒
    S.finish(rec5, "completed")
    wait_rows(4)
    r5 = S.detail(rec5["id"])
    ck_true("流式且无 timings → 按首帧后时长算速度", r5["decode_tok_s"] and abs(r5["decode_tok_s"] - 4.0) < 0.5,
            f"{r5['decode_tok_s']} tok/s（8 token / 2 s）")

    # ---- 分段**还没封口**时也要读得到内容（gzip 默认缓冲到封口 → 进程一挂就是 0 字节文件）
    time.sleep(2.0)                                            # 等写线程提交 + flush 一轮
    seg = S.detail(rec5["id"])["seg"]
    date = S.detail(rec5["id"])["iso"][:10]
    sp = tmp / "segs" / date / seg
    ck_true("未封口的分段也有内容落盘（不是 0 字节）", sp.exists() and sp.stat().st_size > 0,
            f"{sp.stat().st_size if sp.exists() else -1} 字节")
    mid = S.raw(rec5["id"])
    ck("未封口也能读回这条记录", "你好，世界", (mid.get("response") or {}).get("content"))

    # ---- RAG 前后对照：原输入 / 实际输入 / 检索结果（元信息，正文不在这里）
    S._CFG["capture_max_mb"] = 16
    rec6 = S.begin("/v1/chat/completions", b'{"messages":[]}', {})
    client_req = {"messages": [{"role": "user", "content": "x" * 100},
                               {"role": "user", "content": "y" * 50}]}
    sent_req = {"messages": [{"role": "system", "content": "you are helpful"},
                             {"role": "user", "content": "MEMORY BLOCK" * 10},
                             {"role": "assistant", "content": "(noted)"},
                             {"role": "user", "content": "y" * 50}]}
    S.note_request(rec6, client_req, sent_req, client_bytes=180, sent_bytes=260)
    S.note_route(rec6, mode="assembled", recent_tokens=600, memory_tokens=900, block_reused=False)
    S.note_rag(rec6, {"query_chars": 42, "query_sha": "abc123", "budget": 200000,
                      "pool": 73, "candidates": 73, "reranked": True, "block_chars": 3210,
                      "need_compact": False, "block_cov": 0.81,
                      "chosen_detail": {"chunks": [
                          {"turn": 3, "turn_label": "第 3 轮", "role": "tool", "kind": "text",
                           "chars": 1200, "tokens": 700, "score": 0.83, "cos": 0.61, "rerank": 0.9},
                          {"turn": 9, "turn_label": "第 9 轮", "role": "user", "kind": "text",
                           "chars": 800, "tokens": 500, "score": 0.55, "cos": 0.5, "rerank": 0.6},
                          {"turn": 9, "turn_label": "第 9 轮(补齐)", "role": "assistant", "kind": "text",
                           "chars": 300, "tokens": 180, "score": None, "expanded": True}]}})
    S.capture(rec6, json.dumps({"choices": [{"message": {"content": "答"}, "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 1500, "completion_tokens": 4}}).encode())
    rec6["ended_at"] = rec6["ts"] + 2.0
    S.finish(rec6, "completed")
    wait_rows(5)
    r6 = S.detail(rec6["id"])
    ck("原输入消息数", 2, r6["client_msgs"])
    ck("实际输入消息数（组装后多了 system+检索块+回执）", 4, r6["sent_msgs"])
    ck("标记前后不同", 1, r6["differs"])
    ck("查询字符数", 42, r6["rag_query_chars"])
    ck("候选池大小", 73, r6["rag_pool"])
    ck("选中块数（含结构补全那块）", 3, r6["rag_chunks"])
    ck("最高分", 0.83, r6["rag_score_max"])
    ck("最低分", 0.55, r6["rag_score_min"])
    ck("检索块字符数", 3210, r6["rag_block_chars"])
    ck_true("走了重排", r6["rag_rerank"] == 1)
    ck("这条是重建的（不是复用）", 1, r6["rag_rebuild"])
    # 完整记录里能看到检索明细，但**函数不吐正文**：rag_meta 只挑 rag 段
    meta = S.rag_meta(rec6["id"])
    ck("rag 元信息：查询指纹", "abc123", ((meta.get("rag") or {}).get("query_sha")))
    ck("rag 元信息：第一条块的回合标签", "第 3 轮",
       (((meta.get("rag") or {}).get("chosen_detail") or {}).get("chunks") or [{}])[0].get("turn_label"))
    blob = json.dumps(meta, ensure_ascii=False)
    ck_true("rag 元信息不带任何正文", "MEMORY BLOCK" not in blob and "答" not in blob.replace('"rag"', ""),
            blob[:80])
    full = S.raw(rec6["id"])
    ck("完整记录里留着原输入", 100, len((full["request"]["client"]["messages"][0]["content"])))
    ck("完整记录里留着实际输入（含检索块）", 120, len(full["request"]["sent"]["messages"][1]["content"]))

    # ---- 进程重启留下的未封口分段：启动时要回收并补传（否则永远归档不到对象存储）
    S._seg_close()                                             # 先把当前段正常收尾，避免干扰
    with S.DB_LOCK:
        # 两条"上次进程留下的"未封口段：一条磁盘上有内容（要补传），一条只有空文件（没内容可传）。
        # 注意 rows 故意都写 0 —— 真实情况就是这样（rows 只在封口时回写），第一版按 rows>0 筛，
        # 线上一条都筛不出来。
        S._conn().execute("INSERT OR REPLACE INTO segments(name,date,created,bytes,rows,closed,uploaded)"
                          " VALUES('orphan-x.jsonl.gz','2026-10-16',?,0,0,0,0)", (time.time(),))
        S._conn().execute("INSERT OR REPLACE INTO segments(name,date,created,bytes,rows,closed,uploaded)"
                          " VALUES('orphan-empty.jsonl.gz','2026-10-16',?,0,0,0,0)", (time.time(),))
        S._conn().commit()
    d = tmp / "segs" / "2026-10-16"
    (d / "orphan-x.jsonl.gz").write_bytes(gzip.compress(b'{"id":"x"}\n'))
    (d / "orphan-empty.jsonl.gz").write_bytes(b"")             # 开了口、一个字没写
    before = len(fake.put)
    S.init(cfg)                                                # 再 init 一次 = 模拟重启
    time.sleep(0.5)
    with S.DB_LOCK:
        rows = {r["name"]: dict(r) for r in S._conn().execute(
            "SELECT name, closed, uploaded FROM segments WHERE name LIKE 'orphan-%'")}
    ck("重启时把遗留的未封口分段收尾", 1, rows["orphan-x.jsonl.gz"]["closed"])
    ck("空段直接标归档（不占上传）", (1, 1), (rows["orphan-empty.jsonl.gz"]["closed"],
                                             rows["orphan-empty.jsonl.gz"]["uploaded"]))
    t0 = time.time()
    while time.time() - t0 < 5 and len(fake.put) <= before:
        time.sleep(0.1)
    uploaded = fake.put[before:]
    ck_true("有内容的遗留分段被补传", any("orphan-x" in k for k in uploaded), f"{uploaded}")
    ck_true("空段没有被传上去", not any("orphan-empty" in k for k in uploaded), f"{uploaded}")

    # ---- 热路径不被拖住：队列满 → 立刻丢并计数，不抛异常、不等待
    real_q, S._Q = S._Q, _FullStub()

    def flood():
        for _ in range(2000):
            S.finish(S.begin("/v1/chat/completions", b"{}", {}), "completed")
    t0 = time.perf_counter()
    flood()
    dt = (time.perf_counter() - t0) * 1000
    S._Q = real_q
    ck_true("队列满时不阻塞（2000 次 < 200ms）", dt < 200, f"{dt:.1f} ms")
    ck("丢弃被计数", 2000, S.STATE["dropped"])

    ok = not FAILS
    print(("全部通过" if ok else f"失败 {len(FAILS)} 项：{FAILS}") + f"（临时目录 {tmp}）")
    return 0 if ok else 1


class _FullStub:
    """永远满的队列：验证 finish() 在极端情况下也只是丢弃 + 计数。"""

    def put_nowait(self, item):
        raise queue.Full

    def qsize(self):
        return 1


if __name__ == "__main__":
    code = main()
    sys.exit(code)
