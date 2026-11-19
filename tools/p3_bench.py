#!/usr/bin/env python3
"""P3 门禁测量：并发 prefill 排队 + 解码深度（S）扫描。

两件事必须在同一口径下量：

1. **prefill 串行**：引擎的服务循环一次只读一个 prompt（单线程 `sp.run`），
   并发 N 条请求时第 i 条的 TTFT ≈ 前面所有 prompt 的读取时间之和。
   量法：`--prompt-tokens K`（每条请求的 prompt 都接近 K token）、`--tokens` 很小（让 TTFT 主导）。
2. **解码深度 S**：`--batch S` 决定一个窗口里几条序列；S 越大每槽行数越少（行预算 8），
   投机可能整组失效。量法：固定负载（N 条并发）、只改引擎的 `--batch`，比较
   每请求 tok/s、聚合 tok/s、TTFT、ITL 分位。

只打 127.0.0.1:8091（lane A）。数据写 ~/docs/reports/data/p3-<tag>.json。
"""
import argparse
import json
import os
import statistics
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8091"
KEY = open("/home/cloud/logs/strata-api-key.txt").read().strip()
MODEL = "qwen3.8-flash-next-iq3_xxs"
STATS = "/home/cloud/logs/step-stats-ab-prod.jsonl"

# 填充段落：每个请求都从同一段文本开头拷贝，保证 prompt 内容确定、可复现（不引入随机性）。
FILLER = (
    "第{n}节：分布式推理系统的性能取决于若干相互耦合的因素。首先是权重与专家参数的内存带宽，"
    "在混合专家模型中，每个 token 只激活其中一小部分专家，因此每次前向都要把被选中的专家从主机内存"
    "搬到显存，搬运的字节数往往比算力更早成为瓶颈。其次是注意力阶段的状态规模，长上下文会让 KV 缓存"
    "持续增长，必须在显存、主机内存与磁盘之间分层安置。第三是调度：多个请求共享同一张卡时，如何把"
    "它们的行拼进同一个窗口，决定了专家读取能被复用多少次。第四是可观测性，缺少排队深度、命中率与"
    "每阶段的耗时就无法判断瓶颈究竟在读取、计算还是同步上。"
)


def make_prompt(target_tokens, salt=0):
    """按目标 token 数拼一段确定的中文文本（1 汉字≈1 token，宁多勿少由服务端报告的 prompt_n 为准）。

    salt：每条请求加一个不同的编号。**必须唯一**——否则第 2 条起会命中第 1 条的提示词缓存
    （服务端报 prompt_n 只算「新读」的 token，cache_n 才是复用的），量到的 TTFT 就不是 prefill 了。
    """
    if target_tokens <= 0:
        return f"用一句话说明什么是缓存。（请求 {salt}）"
    parts = [f"请求编号 {salt}。"]
    i = 0
    # 每节都带编号：整段前缀因此逐 token 不同，避免命中别的请求的提示词缓存（缓存命中时服务端不走引擎，
    # 响应里连 timings 都没有，TTFT 就无从谈起）。
    while i * 180 < target_tokens * 1.1:
        parts.append(FILLER.format(n=f"{salt}-{i}"))
        i += 1
    # 结尾也必须带编号：引擎的会话检查点按前缀尾部块匹配，尾段相同会让不同请求命中同一个检查点
    #（实测：只在前部改编号时，3768/3775 token 被当成「复用」），那不是 prefill 的真实成本。
    parts.append(f"（材料编号 {salt}）以上是关于推理系统的背景材料。请用一句话回答：这段材料讨论的第一个瓶颈是什么？")
    return "".join(parts)


def one(prompt, tokens):
    """一条流式请求。TTFT / ITL 全部在客户端按到达时间量。

    为什么必须流式：并发时服务层把每个请求的 timings 从一份共享的 `last` 快照里取
    （`Strata/serve/server.py:2003 request_timings`），8 路并发下只有 1 条响应带 timings，
    其余全是 None——用它做 TTFT 就会得到「中位 0.3 ms」这种假数。客户端时间戳没有这个竞态。
    """
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": tokens,
        "stream": True,
        "reasoning_effort": "none",
        "temperature": 0,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    t0 = time.time()
    arr, content, usage = [], [], {}
    with urllib.request.urlopen(req, timeout=1800) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                d = json.loads(data)
            except Exception:  # noqa: BLE001
                continue
            if d.get("usage"):
                usage = d["usage"]
            for ch in d.get("choices") or []:
                dl = ch.get("delta") or {}
                txt = dl.get("content") or ""
                if txt:
                    arr.append(time.time())
                    content.append(txt)
    t_end = time.time()
    gaps = [(arr[i] - arr[i - 1]) * 1000 for i in range(1, len(arr))]
    return {
        "cached_path": False,
        "wall_s": round(t_end - t0, 3),
        "prompt_n": usage.get("prompt_tokens"),
        "cache_n": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "prompt_ms": round((arr[0] - t0) * 1000, 1) if arr else None,   # 首 token 到达（含排队）
        "predicted_n": len(arr),
        "predicted_ms": round((arr[-1] - arr[0]) * 1000, 1) if len(arr) > 1 else None,
        "tok_s_engine": round((len(arr) - 1) / (arr[-1] - arr[0]), 2) if len(arr) > 1 and arr[-1] > arr[0] else None,
        "gaps_ms": [round(g, 1) for g in gaps],
        "draft_n": None,
        "accepted": None,
        "content": "".join(content),
    }


def parse_step_stats(off0, off1):
    """读 [off0, off1) 的 StepStats 行，统计窗口类型与草稿接受情况（引擎在这里记 prefill/chain/batch）。"""
    kinds, drafts, accepted, steps = {}, 0, 0, 0
    if not os.path.exists(STATS):
        return {"kinds": kinds, "drafts": drafts, "accepted": accepted, "steps": steps}
    with open(STATS, "rb") as f:
        f.seek(off0)
        blob = f.read(max(0, off1 - off0)).decode("utf-8", "replace")
    for line in blob.splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("kind") == "step_stats":
            continue
        k = d.get("kind")
        if k is None:
            continue
        kinds[k] = kinds.get(k, 0) + 1
        steps += 1
        drafts += d.get("drafts", 0) or 0
        accepted += d.get("accepted", 0) or 0
    return {"kinds": kinds, "drafts": drafts, "accepted": accepted, "steps": steps}


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1)))], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--n", type=int, default=32, help="总请求数")
    ap.add_argument("--concurrency", type=int, default=32, help="并发线程数")
    ap.add_argument("--prompt-tokens", type=int, default=0, help="每条 prompt 的目标 token 数（0=短提示词）")
    ap.add_argument("--tokens", type=int, default=64, help="每条生成多少 token")
    ap.add_argument("--note", default="")
    a = ap.parse_args()

    prompts = [make_prompt(a.prompt_tokens, i) for i in range(a.n)]  # 每条唯一，避免命中彼此的提示词缓存
    out = [None] * a.n
    idx = [0]
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                i = idx[0]
                idx[0] += 1
            if i >= a.n:
                return
            try:
                out[i] = one(prompts[i], a.tokens)
            except Exception as e:  # noqa: BLE001
                out[i] = {"error": repr(e)}

    off0 = os.path.getsize(STATS) if os.path.exists(STATS) else 0
    t0 = time.time()
    ths = [threading.Thread(target=worker) for _ in range(a.concurrency)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = round(time.time() - t0, 2)
    off1 = os.path.getsize(STATS) if os.path.exists(STATS) else 0

    ok = [r for r in out if r and not r.get("error")]
    ttfts = [r["prompt_ms"] for r in ok if r.get("prompt_ms")]
    per_req = [r["tok_s_engine"] for r in ok if r.get("tok_s_engine")]
    # ITL 用客户端实测的相邻首字间隔（流式），再按请求取中位、跨请求取分位
    gaps_all = [g for r in ok for g in (r.get("gaps_ms") or [])]
    per_req_itl = [statistics.median(r["gaps_ms"]) for r in ok if r.get("gaps_ms")]
    toks = sum(r.get("predicted_n") or 0 for r in ok)
    st = parse_step_stats(off0, off1)
    doc = {
        "tag": a.tag,
        "note": a.note,
        "n": a.n,
        "concurrency": a.concurrency,
        "prompt_tokens_target": a.prompt_tokens,
        # usage.prompt_tokens 是总 prompt；cached_tokens 是其中复用的部分，新读 = 总 - 复用
        "prompt_total_median": pct([r["prompt_n"] for r in ok if r.get("prompt_n")], 0.5),
        "cache_n_median": pct([r.get("cache_n") or 0 for r in ok], 0.5),
        "read_n_median": pct([(r.get("prompt_n") or 0) - (r.get("cache_n") or 0) for r in ok if r.get("prompt_n")], 0.5),
        "tokens": a.tokens,
        "steps_offset": [off0, off1],
        "ttft_ms_all": [round(x, 1) for x in ttfts],
        "ttft_ms_median": pct(ttfts, 0.5),
        "ttft_ms_p90": pct(ttfts, 0.9),
        "ttft_ms_max": pct(ttfts, 1.0),
        "per_req_tok_s_median": pct(per_req, 0.5),
        "per_req_tok_s_min": pct(per_req, 0.0),
        "itl_median_ms": pct(per_req_itl, 0.5),
        "itl_p90_ms": pct(gaps_all, 0.9),
        "itl_p99_ms": pct(gaps_all, 0.99),
        "itl_max_ms": pct(gaps_all, 1.0),
        "per_req_itl_median_ms": [round(x, 1) for x in per_req_itl],
        "wall_s": wall,
        "tok": toks,
        "agg_tok_s": round(toks / wall, 2) if wall else None,
        "windows": st["kinds"],
        "steps": st["steps"],
        "drafts": st["drafts"],
        "accepted": st["accepted"],
        "accept_rate": round(st["accepted"] / st["drafts"], 3) if st["drafts"] else None,
        "ngaps": len(gaps_all),
        "errors": [r for r in out if r and r.get("error")],
        "rows": out,
    }
    os.makedirs("/home/cloud/docs/reports/data", exist_ok=True)
    json.dump(doc, open(f"/home/cloud/docs/reports/data/p3-{a.tag}.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in doc.items() if k not in ("rows", "errors", "ttft_ms_all")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()