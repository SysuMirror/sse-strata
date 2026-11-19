#!/usr/bin/env python3
"""并发 A/B 小压测台：客户端按 SSE 到达时间算 TTFT / ITL / 总时长。

每条请求的 prompt 前缀唯一（带 salt），避免命中前缀缓存。
用法: bench.py --base http://127.0.0.1:8097 --key-file key.txt --conc 1,2,4
"""
import argparse
import json
import statistics
import threading
import time
import urllib.request

FILLER = (
    "第{n}节：混合专家模型的解码性能取决于若干相互耦合的因素。权重与专家参数的内存带宽，"
    "注意力阶段的 KV 缓存规模，以及多个请求共享同一张卡时的调度方式，都会影响最终的吞吐。"
)


def make_prompt(tokens, salt):
    parts = [f"请求编号 {salt}。请阅读下面的材料并回答问题。\n"]
    i = 0
    while len("".join(parts)) < tokens:
        parts.append(FILLER.format(n=i))
        parts.append(f"\n（材料 {salt}-{i}）\n")
        i += 1
    parts.append("\n问题：请用一句话总结上面材料讨论的核心因素。")
    return "".join(parts)


def one(base, key, model, tokens_in, max_tokens, salt):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": make_prompt(tokens_in, salt)}],
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    t0 = time.time()
    ttft = None
    ntok = 0
    last = t0
    gaps = []
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                ch = obj.get("choices") or [{}]
                delta = (ch[0].get("delta") or {})
                txt = delta.get("content") or delta.get("reasoning_content") or ""
                if txt:
                    now = time.time()
                    if ttft is None:
                        ttft = now - t0
                    else:
                        gaps.append(now - last)
                    last = now
                    ntok += 1
    except Exception as e:
        return {"salt": salt, "error": repr(e)}
    total = last - t0
    return {
        "salt": salt,
        "ttft": ttft,
        "total": total,
        "tokens": ntok,
        "itl_ms": (statistics.mean(gaps) * 1000) if gaps else None,
        "tok_s": (ntok / total) if total > 0 else None,
    }


def run_case(base, key, model, conc, rounds, tokens_in, max_tokens):
    res = []
    t0 = time.time()
    for r in range(rounds):
        out = [None] * conc
        ths = []
        for i in range(conc):
            def work(i=i, r=r):
                out[i] = one(base, key, model, tokens_in, max_tokens, salt=10000 * (r + 1) + i)
            t = threading.Thread(target=work)
            t.start()
            ths.append(t)
        for t in ths:
            t.join()
        res.extend([o for o in out if o])
    wall = time.time() - t0
    ok = [x for x in res if "error" not in x and x.get("tokens")]
    errs = [x for x in res if "error" in x]
    ttfts = [x["ttft"] for x in ok if x.get("ttft") is not None]
    totals = [x["total"] for x in ok]
    toks = sum(x["tokens"] for x in ok)
    itls = [x["itl_ms"] for x in ok if x.get("itl_ms")]
    return {
        "conc": conc, "rounds": rounds, "requests": len(res), "errors": errs[:3],
        "ttft_ms_mean": round(statistics.mean(ttfts) * 1000, 1) if ttfts else None,
        "ttft_ms_p50": round(statistics.median(ttfts) * 1000, 1) if ttfts else None,
        "ttft_ms_max": round(max(ttfts) * 1000, 1) if ttfts else None,
        "total_s_mean": round(statistics.mean(totals), 2) if totals else None,
        "itl_ms_mean": round(statistics.mean(itls), 2) if itls else None,
        "tok_s_per_req": round(statistics.mean([x["tok_s"] for x in ok]), 2) if ok else None,
        "tok_s_aggregate": round(toks / wall, 2) if wall > 0 else None,
        "wall_s": round(wall, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8097")
    ap.add_argument("--key-file", default="/home/cloud/engines/test/key.txt")
    ap.add_argument("--model", default="qwen3.8-flash-next-iq3_xxs")
    ap.add_argument("--conc", default="1,2,4")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--prompt-tokens", type=int, default=1000)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    key = open(a.key_file).read().strip()
    results = []
    for n in [int(x) for x in a.conc.split(",") if x.strip()]:
        r = run_case(a.base, key, a.model, n, a.rounds, a.prompt_tokens, a.max_tokens)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        results.append(r)
    if a.out:
        open(a.out, "w").write(json.dumps(results, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()