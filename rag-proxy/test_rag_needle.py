#!/usr/bin/env python3
"""单针召回（NIAH 的最小可用版）：验证"改了 RAG 配置之后，检索到底有没有把该找的历史找回来"。

做法：先灌一段**远超最近窗口**的对话历史，中间埋一条唯一事实（暗号），末尾再问暗号是多少。
      —— 组装没生效 / 检索没召回 → 暗号不在窗口里 → 模型只能瞎猜（答案里没有暗号）。
      —— 组装生效且召回成功 → 暗号随检索块进了窗口 → 答案里出现暗号。

为什么要有它：`bench/niah_run.py` 那套全网格要几十万 token 的真实语料（还得先下 Kamradt 的
Paul Graham 文集到 /tmp/niah，pod 重建就没了），而"我这次改的配置到底有没有用"这个问题，
一条 30k token 的历史、十几秒就能回答。全网格留给配置定稿后的回归。

用法：
    /home/cloud/Strata/.venv/bin/python test_rag_needle.py                      # 打线上 :8080
    ... test_rag_needle.py --url http://127.0.0.1:8181 --model strata         # 打临时实例
    ... test_rag_needle.py --turns 60 --depth 0.3 --repeat 3                  # 调历史长度/埋针位置/重复次数
    ... test_rag_needle.py --dry-run                                          # 只打印要发多大，不发请求

退出码：全部命中 0，有漏 1 —— 可以直接进 CI / 脚本链。
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

FILLER = ("这段话是一份归档说明，用来把上下文撑到窗口之外：它讲的是命名规范、分层方式、"
          "以及存储层的迁移计划，内容彼此高度相似，所以检索必须靠语义而不是靠字面重复。")
NEEDLE_TMPL = "\n顺便记一下，{label} 的保险柜口令是 {code}，后面会问你。\n"
Q_TMPL = "{label} 的保险柜口令是什么？只回答口令本身。"

# 英文档：`mock_upstream.py` 的 oracle 只认英文句式（它按 "the <label> code is XXXX-1234" 找），
# 所以离线自检（不烧 GPU）时用 --en 这一套。
FILLER_EN = ("Archived design note: it covers naming, layering, and the migration plan for the "
             "storage tier. The paragraphs are deliberately similar to each other, so returning the "
             "right one takes semantics, not literal repetition. ")
NEEDLE_EN = "\nBy the way, the {label} code is {code} - you will be asked about it later.\n"
Q_EN = "What is the {label} code? Answer with just the code."


def tokenize(text: str) -> int:
    """按本机引擎同款口径估：CJK 一字≈1 token，其余 4 字符≈1 token。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + max(0, len(text) - cjk) // 4


def build(turns: int, depth: float, code: str, label: str, en: bool = False) -> list[dict]:
    """depth=埋针位置（0=最早，1=最后），末尾固定是提问。"""
    if en:
        msgs = [{"role": "system", "content": "You are a careful engineering assistant. Be brief."}]
        filler, tmpl, q = FILLER_EN, NEEDLE_EN, Q_EN.format(label=label)
    else:
        msgs = [{"role": "system", "content": "你是一个严谨的工程助手，回答要简短。"}]
        filler, tmpl, q = FILLER, NEEDLE_TMPL, Q_TMPL.format(label=label)
    at = max(1, int(turns * depth))
    for i in range(turns):
        body = (f"Turn {i}: {filler * 3}" if en else f"第 {i} 轮：{filler * 3}")
        msgs.append({"role": "user", "content": body})
        msgs.append({"role": "assistant", "content": f"Noted, archived turn {i}." if en else f"收到，已归档第 {i} 轮。"})
        if i == at:
            # 暗号单独成一条消息 —— 结构优先分块把它切成**独立一块**，语义不被周围的填充冲淡。
            # （塞在填充里的话，那一块的向量几乎等于填充，余弦分不开，召回就得靠重排器；
            #  没有重排器时会被"最近优先"压掉——这个坑实测踩过。）
            msgs.append({"role": "user", "content": tmpl.format(label=label, code=code).strip()})
            msgs.append({"role": "assistant", "content": "Noted, kept." if en else "记下了。"})
    msgs.append({"role": "user", "content": q})
    return msgs


def ask(url: str, model: str, msgs: list, max_tokens: int, key: str, timeout: float):
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens,
            "temperature": 0, "reasoning_effort": "low", "stream": False}
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    dt = time.time() - t0
    msg = (j.get("choices") or [{}])[0].get("message") or {}
    usage = j.get("usage") or {}
    return (msg.get("content") or ""), dt, usage.get("prompt_tokens"), usage.get("completion_tokens")


def counters(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/").replace("/v1", "") + "/health", timeout=8) as r:
            return (json.loads(r.read().decode()) or {}).get("counters") or {}
    except Exception:
        return {}


def health(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/").replace("/v1", "") + "/health", timeout=8) as r:
            return json.loads(r.read().decode()) or {}
    except Exception:
        return {}


def warm_up(url: str, model: str, msgs: list, key: str, timeout: float, wait_s: float) -> None:
    """预热：先把这段历史整段发一次，让后台把块嵌完。

    为什么必须预热：新会话第一次请求时，块刚入队、向量还没算出来 → 检索块是空的 →
    即使配置完全正确也会"召回失败"。`bench/niah_run.py` 的 --no-warm 就是拿这个当对照组的
    （它有"冷启动必然为空"的说明）。这里默认预热，并等会话的 vectors 追平 chunks。"""
    try:
        ask(url, model, msgs, 8, key, timeout)
    except Exception as e:
        print(f"   （预热请求失败：{e!r}）")
        return
    t0 = time.time()
    while time.time() - t0 < wait_s:
        sess = health(url).get("sessions") or []
        pending = [s for s in sess if (s.get("chunks") or 0) > (s.get("vectors") or 0)]
        if sess and not pending:
            print(f"   预热完成：块已全部向量化（{time.time() - t0:.1f}s）")
            return
        time.sleep(1.0)
    print(f"   ⚠ 预热等待 {wait_s:.0f}s 后仍有块没向量化（嵌入慢或不可用）——这次的召回结果不作数")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--model", default="strata-b", help="走哪条通道（strata-a 快道 / strata-b 普道）")
    ap.add_argument("--turns", type=int, default=30, help="历史轮数（每轮约 300 字符 × 2 条）")
    ap.add_argument("--depth", type=float, default=0.15, help="埋针深度 0~1（0.15 ≈ 靠前）")
    ap.add_argument("--repeat", type=int, default=1, help="重复几次（每次换一个暗号）")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--key-file", default="/home/cloud/logs/strata-api-key.txt")
    ap.add_argument("--en", action="store_true", help="英文语料（离线自检用：mock_upstream 的 oracle 只认英文）")
    ap.add_argument("--warm", dest="warm", action="store_true", default=True, help="先预热（默认开）")
    ap.add_argument("--no-warm", dest="warm", action="store_false",
                    help="不预热：看冷启动第一次（那时检索块必然是空的，用作对照）")
    ap.add_argument("--warm-wait", type=float, default=120, help="预热后最多等多少秒把块嵌完")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    key = ""
    p = Path(a.key_file)
    if p.exists():
        key = p.read_text().strip()

    c0 = counters(a.url)
    print(f"== 单针召回 @ {a.url}（模型 {a.model}，历史 {a.turns} 轮，埋针深度 {a.depth}）")
    if c0:
        print(f"   组装计数（发请求前）：assembled={c0.get('assembled')} reused={c0.get('assembly_reused')} "
              f"rebuilt={c0.get('assembly_rebuilt')} 嵌入完成={c0.get('embeds_done')} 嵌入出错={c0.get('embed_errors')}")
    hits, total_tok, fails = 0, 0, []
    if a.warm and not a.dry_run:
        print(f"   预热中（先把 {a.turns} 轮历史整段发一次，等嵌入算完）…")
        warm_up(a.url, a.model, build(a.turns, a.depth, "ZEBRA-0000", "vault" if a.en else "归档笔记", a.en),
                key, a.timeout, a.warm_wait)
    for i in range(a.repeat):
        # 口令固定 4 位数字后缀（mock_upstream 的 oracle 就是按 `[A-Z]+-\d{4}` 找的）
        code = "ZEBRA-%d%03d" % (9 - i % 9, random.randint(0, 999))
        label = "vault" if a.en else "归档笔记"
        msgs = build(a.turns, a.depth, code, label, a.en)
        est = sum(tokenize(str(m["content"])) + 8 for m in msgs)
        if a.dry_run:
            print(f"   [{i + 1}] 暗号 {code}，历史约 {est} token")
            continue
        try:
            text, dt, ptok, ctok = ask(a.url, a.model, msgs, a.max_tokens, key, a.timeout)
        except urllib.error.HTTPError as e:
            print(f"   [{i + 1}] HTTP {e.code}：{e.read()[:200]!r}")
            fails.append((code, f"HTTP {e.code}"))
            continue
        ok = code in text
        hits += ok
        total_tok += ptok or est
        print(f"   [{i + 1}] 暗号 {code} → {'✅ 召回成功' if ok else '❌ 没答出来'} "
              f"（{dt:.1f}s，上游 prompt {ptok} tok，输出 {ctok} tok）"
              + ("" if ok else f" 回答：{text[:80]!r}"))
        if not ok:
            fails.append((code, text[:120]))

    c1 = counters(a.url)
    if c1 and any(c1.get(k) is not None for k in ("assembled",)):
        d = {k: (c1.get(k, 0) - c0.get(k, 0)) for k in ("assembled", "assembly_reused", "assembly_rebuilt",
                                                        "passthrough", "degraded", "embeds_done",
                                                        "embed_errors", "block_rebuilds")}
        print("   这一轮的计数增量：", json.dumps(d, ensure_ascii=False))
        if d.get("assembled", 0) == 0 and not a.dry_run:
            print("   ⚠ 没有任何请求走组装 —— 要么这段历史没超窗（把 --turns 调大），"
                  "要么 max_context 比历史还大。这时上面即使答对，也**不是** RAG 的功劳。")
        if d.get("embeds_done", 0) == 0 and d.get("embed_errors", 0) > 0:
            print("   ⚠ 嵌入服务在报错（embeds_done 没涨）—— 检索块会是空的，"
                  "这种情况先修端点，配置调什么都没用。")
    if a.dry_run:
        return 0
    print(f"== 结果：{hits}/{a.repeat} 命中" + (f"，平均上游 prompt {total_tok // max(1, a.repeat)} tok" if total_tok else ""))
    if fails:
        print("   失败明细：", fails[:3])
    return 0 if hits == a.repeat else 1


if __name__ == "__main__":
    sys.exit(main())
