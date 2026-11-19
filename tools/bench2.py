#!/usr/bin/env python3
"""双 lane 聚合压测：把 N 条并发请求均分到两条 lane，按客户端计时算每路与聚合。

用法: bench2.py --bases http://127.0.0.1:8091,http://127.0.0.1:8092 --key-file K --conc 8
"""
import argparse
import json
import statistics
import threading
import time

import bench


def run(bases, key, model, conc, rounds, tokens_in, max_tokens):
    per = [conc // len(bases) + (1 if i < conc % len(bases) else 0) for i in range(len(bases))]
    results = []
    t0 = time.time()
    for r in range(rounds):
        out = []
        lock = threading.Lock()

        def worker(base, idx, salt):
            res = bench.one(base, key, model, tokens_in, max_tokens, salt)
            res["base"] = base
            with lock:
                out.append(res)

        ths = []
        salt = 10000 * (r + 1)
        for bi, base in enumerate(bases):
            for i in range(per[bi]):
                t = threading.Thread(target=worker, args=(base, i, salt + bi * 100 + i))
                t.start()
                ths.append(t)
                salt += 1
        for t in ths:
            t.join()
        results.extend(out)
    wall = time.time() - t0
    ok = [x for x in results if "error" not in x and x.get("tokens")]
    ttfts = [x["ttft"] for x in ok if x.get("ttft") is not None]
    itls = [x["itl_ms"] for x in ok if x.get("itl_ms")]
    toks = sum(x["tokens"] for x in ok)
    by_base = {}
    for x in ok:
        by_base.setdefault(x["base"], []).append(x)
    return {
        "conc": conc, "rounds": rounds, "per_lane": per,
        "ttft_ms_mean": round(statistics.mean(ttfts) * 1000, 1) if ttfts else None,
        "ttft_ms_max": round(max(ttfts) * 1000, 1) if ttfts else None,
        "itl_ms_mean": round(statistics.mean(itls), 2) if itls else None,
        "tok_s_per_req": round(statistics.mean([x["tok_s"] for x in ok]), 2) if ok else None,
        "tok_s_aggregate": round(toks / wall, 2) if wall > 0 else None,
        "per_lane_tok_s": {b: round(sum(y["tokens"] for y in v) / wall, 2) for b, v in by_base.items()},
        "errors": [x for x in results if "error" in x][:3],
        "wall_s": round(wall, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bases", default="http://127.0.0.1:8091,http://127.0.0.1:8092")
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--model", default="qwen3.8-flash-next-iq3_xxs")
    ap.add_argument("--conc", default="8")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--prompt-tokens", type=int, default=1200)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    key = open(a.key_file).read().strip()
    bases = [x.strip() for x in a.bases.split(",") if x.strip()]
    res = []
    for n in [int(x) for x in a.conc.split(",") if x.strip()]:
        r = run(bases, key, a.model, n, a.rounds, a.prompt_tokens, a.max_tokens)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        res.append(r)
    if a.out:
        open(a.out, "w").write(json.dumps(res, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()