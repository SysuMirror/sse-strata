#!/usr/bin/env python3
"""RAG 代理测试台
T1 透传一致性：短请求经代理 :8080 与直连引擎回环 :8090，贪心输出逐 token 相同；
T2 摄取/嵌入：请求后 embeds_queued/done 增长、embed_cache 有向量；
T3 长上下文组装+召回：用一个小窗口实例（max_context=32768），发一个 ~40K token 的会话，
   早期埋唯一事实，末端提问——断言 200、assembled 计数 +1、回答里出现该事实；
T4 降级：嵌入/重排地址不可达时，长请求仍返回 200（退化到最近窗口），计数 degraded/assembled 合理。

用法：/home/cloud/Strata/.venv/bin/python test_rag_proxy.py
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = "/home/cloud/Strata/.venv/bin/python"
KEY = Path("/home/cloud/logs/strata-api-key.txt").read_text().strip()
UP = "http://127.0.0.1:8092"              # 引擎回环直连（普道 strata-b；:8090 在端口翻转后已停用）
PROXY = "http://127.0.0.1:8080"           # 生产代理（外部唯一入口）


def post(url, payload, timeout=900):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def chat(base, messages, max_tokens=32, effort="none"):
    r = post(f"{base}/v1/chat/completions",
             {"model": "strata", "messages": messages, "max_tokens": max_tokens,
              "reasoning_effort": effort, "temperature": 0})
    return r["choices"][0]["message"].get("content") or ""


def start_proxy(port: int, overrides: dict, name: str):
    cfg = json.loads((HERE / "config.json").read_text())
    cfg.update(overrides)
    cfg["listen_port"] = port
    p = HERE / f"config-test-{name}.json"
    # 推理记录照开，但落点搬到临时目录：不往线上的索引/分段目录里写测试数据
    cfg.setdefault("infer_store", {}).update({"dir": f"/tmp/rag-proxy-{name}-segs",
                                              "db": f"/tmp/rag-proxy-{name}.db",
                                              "minio_prefix": "infer-test-ragproxy"})
    p.write_text(json.dumps(cfg, indent=1))
    log = open(f"/tmp/rag_proxy_{name}.log", "w")
    proc = subprocess.Popen([PY, str(HERE / "rag_proxy.py"), "--config", str(p)], stdout=log, stderr=log)
    for _ in range(50):
        try:
            get(f"http://127.0.0.1:{port}/health", timeout=2)
            return proc
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"proxy {name} did not start - see /tmp/rag_proxy_{name}.log")


FILLER = ("The archived design notes describe the subsystem in detail. " * 36 +
          "It covers naming, layering, and the migration plan for the storage tier. " * 36)


def make_history(turns: int, fact_everywhere: bool):
    msgs = [{"role": "system", "content": "You are a careful engineering assistant."}]
    for i in range(turns):
        if i == 1:
            msgs.append({"role": "user", "content":
                         "Please remember this access code for later: the vault access code is ZEBRA-7734."})
            msgs.append({"role": "assistant", "content": "Noted. The vault access code is ZEBRA-7734."})
        else:
            msgs.append({"role": "user", "content": f"Note {i}: {FILLER}"})
            msgs.append({"role": "assistant", "content": f"Acknowledged note {i}. {FILLER[:400]}"})
    msgs.append({"role": "user", "content": "What is the vault access code? Answer with just the code."})
    return msgs


def main():
    fails = []
    print("== T1/T2 透传 + 嵌入（代理 :8080，默认配置）==")
    short = [{"role": "user", "content": "Say hello in one short sentence."}]
    a = chat(UP, short)
    b = chat(PROXY, short)
    ok = a.strip() == b.strip() and len(a) > 0
    print(f"  direct: {a[:60]!r}\n  proxy : {b[:60]!r}\n  -> {'OK' if ok else 'MISMATCH'}")
    if not ok:
        fails.append("T1 透传输出不一致")
    time.sleep(3)
    h = get(PROXY + "/health")
    c = h["counters"]
    ok = c["passthrough"] >= 1 and c["embeds_queued"] >= 1 and h["embed_cache"] >= 1
    print(f"  counters: passthrough={c['passthrough']} queued={c['embeds_queued']} done={c['embeds_done']} "
          f"cache={h['embed_cache']} dim={h['embed_dim']} -> {'OK' if ok else 'FAIL'}")
    if not ok:
        fails.append("T2 嵌入管线未工作")

    print("== T3 长上下文组装 + 召回（小窗口实例 :8082, max_context=32768）==")
    proc = start_proxy(8082, {"max_context": 32768, "recent_window_tokens": 8192,
                              "gen_reserve_default": 2048, "gen_reserve_max": 2048}, "small")

    def drain(port):
        """等嵌入队列消化完（真实会话里每轮之间早就消化了）。"""
        for _ in range(300):
            h = get(f"http://127.0.0.1:{port}/health")
            if h["embed_queue"] == 0 and h["counters"]["embeds_queued"] == h["counters"]["embeds_done"] + h["counters"]["embed_errors"]:
                return h
            time.sleep(0.5)
        return get(f"http://127.0.0.1:{port}/health")

    try:
        # 渐进式会话：每 10 轮发一问（这期间是透传 + 后台嵌入），最后再问事实——
        # 与真实 agent 流量一致（每轮到达时，更早的内容早已嵌入就绪）
        msgs = make_history(10, True)
        for i in range(10, 60, 10):
            chat("http://127.0.0.1:8082", msgs, max_tokens=8)
            drain(8082)
            msgs = make_history(i + 10, True)
        h = drain(8082)
        print(f"  渐进摄入完成: chunks={h['sessions'][0]['chunks'] if h['sessions'] else '?'} "
              f"vectors={h['sessions'][0]['vectors'] if h['sessions'] else '?'} queue={h['embed_queue']}")
        t0 = time.time()
        out = chat("http://127.0.0.1:8082", msgs, max_tokens=48)
        dt = time.time() - t0
        h = get("http://127.0.0.1:8082/health")
        c = h["counters"]
        recall = "ZEBRA-7734" in out
        print(f"  answer: {out[:80]!r}  ({dt:.1f}s)")
        print(f"  counters: assembled={c['assembled']} reused={c['assembly_reused']} "
              f"rebuilt={c['assembly_rebuilt']} degraded={c['degraded']}")
        ok = c["assembled"] >= 1 and recall
        print(f"  -> {'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append(f"T3 组装/召回失败 (assembled={c['assembled']}, recall={recall})")
        # 第二问：检索块应复用（前缀缓存友好）
        msgs2 = msgs + [{"role": "assistant", "content": out},
                        {"role": "user", "content": "And what did note 23 say about the subsystem?"}]
        out2 = chat("http://127.0.0.1:8082", msgs2, max_tokens=48)
        h = get("http://127.0.0.1:8082/health")
        c = h["counters"]
        ok = c["assembly_reused"] >= 1
        print(f"  第二问: reused={c['assembly_reused']} -> {'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append("T3b 检索块未复用")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

    print("== T4 降级（嵌入/重排不可达 :8083）==")
    proc = start_proxy(8083, {"max_context": 32768, "recent_window_tokens": 8192,
                              "gen_reserve_default": 2048, "gen_reserve_max": 2048,
                              "embed_url": "http://127.0.0.1:1/v1/embeddings",
                              "rerank_url": "http://127.0.0.1:1/v1/rerank"}, "degraded")
    try:
        msgs = make_history(60, True)
        out = chat("http://127.0.0.1:8083", msgs, max_tokens=32)
        h = get("http://127.0.0.1:8083/health")
        c = h["counters"]
        ok = len(out) > 0 and c["assembled"] == 1
        print(f"  answer len={len(out)} assembled={c['assembled']} embed_errors={c['embed_errors']} "
              f"-> {'OK（退化但成功）' if ok else 'FAIL'}")
        if not ok:
            fails.append("T4 降级路径失败")
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)

    if "--full" in sys.argv:
        print("== T5 全窗口验收（生产代理 :8080，真 256K 窗口）==")
        # 渐进灌入到 >256K：前三轮在窗口内（透传），第四轮触发组装；最后提问早埋的事实
        base = PROXY
        msgs = make_history(20, True)
        for n in (60, 160, 280):
            chat(base, msgs, max_tokens=1)
            drain(8080)
            msgs = make_history(n, True)
            h = get(base + "/health")
            s = max(h["sessions"], key=lambda x: x["chunks"]) if h["sessions"] else {}
            print(f"  灌到 {n} 轮: chunks={s.get('chunks')} vectors={s.get('vectors')} "
                  f"assembled={h['counters']['assembled']}")
        t0 = time.time()
        out = chat(base, msgs, max_tokens=48)
        dt = time.time() - t0
        h = get(base + "/health")
        recall = "ZEBRA-7734" in out
        ok = recall and h["counters"]["assembled"] >= 1
        print(f"  answer: {out[:60]!r} ({dt:.0f}s)  assembled={h['counters']['assembled']} "
              f"reused={h['counters']['assembly_reused']} -> {'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append("T5 全窗口验收失败")

    print("=" * 60)
    if fails:
        print("FAIL:", "; ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
