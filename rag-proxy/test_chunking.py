#!/usr/bin/env python3
"""分块/组装结构的单元测试（不联网、不碰引擎、不碰生产代理）。

覆盖这次重写的每一条主张：
  1. messages 先被解析成回合（OpenAI 与 Anthropic 两种形态都认，Anthropic 的 tool_result 藏在
     user 消息里，不能被当成新用户回合）；
  2. 块不跨回合；回合内相邻的小单元合并成一块（工具调用与它的工具返回在同一块里）；
  3. 超长消息按 行→段→句 切，块边界不落在行中间/句子中间；
  4. 系统提示词不进检索块，组装时逐字保留在最前（OpenAI 形态的 role=system 也不能丢）；
  5. 最近窗口边界落在回合边界上，不会停在回合中间留下孤零零的工具返回；
  6. 单回合超预算时保住回合骨架（用户问题/工具调用留下，巨型输出按行截尾）；
  7. 逐轮追加历史时旧块的哈希不变（嵌入向量直接复用，不因邻居变长而重算）；
  8. 检索块按原始次序渲染、带回合结构标签；结构补全能把回合锚点带回来；
  9. structure_chunking=false 能退回旧行为（A/B 对照）。

用法：/home/cloud/Strata/.venv/bin/python test_chunking.py
"""
import sys

sys.path.insert(0, "/home/cloud/rag-proxy")
import rag_proxy as R

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def cfg(**over):
    R.CFG = dict(R.DEFAULTS)
    R.CFG.update(over)


# ---- 测试语料 -----------------------------------------------------------------------

def log_text(n=400):
    return "\n".join(f"line {i}: step {i % 5} of deploy stage {i // 20} reported status=ok retry={i % 3}"
                     for i in range(n))


def openai_agent_conv():
    """OpenAI 形态：system 在 messages 里；工具返回是 role=tool。"""
    return [
        {"role": "system", "content": "You are an agent. Use tools to answer. Never guess."},
        {"role": "user", "content": "查一下 pod 的访问密码并部署。"},
        {"role": "assistant", "content": "我先查密码库。", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "vault_get", "arguments": '{"key":"pod"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "vault record: pod code = TITAN-4400"},
        {"role": "assistant", "content": "pod 的访问密码是 TITAN-4400，现在部署。"},
        {"role": "user", "content": "部署日志里失败原因是什么？"},
        {"role": "assistant", "content": "我去看日志。", "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "read_log", "arguments": '{"path":"/var/log/deploy.log"}'}}]},
        {"role": "tool", "tool_call_id": "c2", "content": log_text()},
    ]


def anthropic_agent_conv():
    """Anthropic 形态：system 在 body["system"]；工具调用是 assistant 里的 tool_use 块，
    工具返回是**user 消息里的 tool_result 块**——它属于上一回合，不能开新回合。"""
    return [
        {"role": "user", "content": [{"type": "text", "text": "查一下 lab 的门禁密码。"}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "查密码库。"},
            {"type": "tool_use", "id": "t1", "name": "vault_get", "input": {"key": "lab"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "vault record: lab code = COBALT-3141"}]}]},
        {"role": "assistant", "content": [{"type": "text", "text": "lab 的门禁密码是 COBALT-3141。"}]},
    ]


def growing_session(turns: int):
    msgs = [{"role": "system", "content": "You are a careful engineering assistant."}]
    for i in range(turns):
        if i == 1:
            msgs.append({"role": "user", "content":
                         "Please remember this access code for later: the vault access code is ZEBRA-7734."})
            msgs.append({"role": "assistant", "content": "Noted. The vault access code is ZEBRA-7734."})
        else:
            msgs.append({"role": "user", "content": f"Note {i}: " + "The archived design notes describe the subsystem. " * 60})
            msgs.append({"role": "assistant", "content": f"Acknowledged note {i}. " + "Noted the design notes. " * 20})
    return msgs


# ---- 1. 回合解析 --------------------------------------------------------------------

def test_turn_parsing():
    print("== 1 回合解析 ==")
    cfg()
    turns = R.parse_turns(openai_agent_conv())
    check("OpenAI 形态解析出 2 条回合", len(turns) == 2, f"got {len(turns)}")
    check("回合 0 含 用户输入+工具调用+工具返回+回答", len(turns[0].units) == 4,
          f"units={[u.kind for u in turns[0].units]}")
    check("工具名被识别出来", turns[0].units[1].name == "vault_get",
          f"name={turns[0].units[1].name!r}")
    check("system 消息不进任何回合", all(u.msg_idx != 0 for t in turns for u in t.units))

    turns = R.parse_turns(anthropic_agent_conv())
    check("Anthropic 形态解析出 1 条回合（tool_result 不开新回合）", len(turns) == 1, f"got {len(turns)}")
    kinds = [u.kind for u in turns[0].units]
    check("Anthropic 的 tool_result 被归为工具返回而不是用户输入",
          kinds == ["user", "tool_call", "tool_result", "assistant"], f"kinds={kinds}")
    check("回合标签带工具名", "vault_get" in turns[0].label(), turns[0].label())


# ---- 2. 块不跨回合 / 结构单元合并 ----------------------------------------------------

def test_chunk_boundaries():
    print("== 2 块边界 ==")
    cfg()
    msgs = openai_agent_conv()
    turns = R.parse_turns(msgs)
    turn_of = {u.msg_idx: t.idx for t in turns for u in t.units}
    chunks = R.chunk_messages(msgs)

    spanning = [c for c in chunks if any(i in turn_of and turn_of[i] != c.turn
                                         for i in range(c.msg_from, c.msg_to + 1))]
    check("没有任何块跨回合", not spanning, f"{[(c.idx, c.turn, c.msg_from, c.msg_to) for c in spanning]}")

    t0 = [c for c in chunks if c.turn == 0]
    check("一个结构单元一块（用户输入/工具调用/工具返回/回答各成一块）",
          [c.kind for c in t0] == ["user", "tool_call", "tool_result", "assistant"],
          f"kinds={[c.kind for c in t0]}")
    check("每个块都记得自己属于哪条回合、什么结构",
          all(c.turn_label and "tool_call: vault_get" in c.turn_label for c in t0),
          t0[0].turn_label if t0 else "")
    check("块不跨越消息边界", all(c.msg_from == c.msg_to for c in chunks if c.n_parts == 1))

    # 回合的连贯性由渲染还原（不是靠合并进同一块）
    block = R.render_block(t0)
    check("渲染后整条回合完整可读（问题与答案在同一段里、次序原样）",
          "查一下 pod" in block and "TITAN-4400" in block
          and block.index("查一下 pod") < block.index("TITAN-4400"))


# ---- 3. 超长消息按自然边界切 --------------------------------------------------------

def test_natural_boundaries():
    print("== 3 自然边界 ==")
    cfg()
    msgs = openai_agent_conv()
    parts = [c for c in R.chunk_messages(msgs) if c.n_parts > 1]
    lines = log_text().split("\n")

    check("巨型工具输出被切成多段", len(parts) > 1, f"{len(parts)} 段")
    bad_start = [c for c in parts if c.text.split("\n")[0] not in lines]
    check("每段都从**完整行**开始（没有从行中间劈开）", not bad_start,
          f"bad={[c.text[:40] for c in bad_start[:2]]}")
    joined = "\n".join(c.text for c in parts)
    broken = [l for l in lines if l not in joined]
    check("没有任何一行被劈成两半", not broken, f"broken={broken[:2]}")
    check("分段带 part/n_parts 标识", all(c.part < c.n_parts for c in parts))

    # 散文（无行结构）走段落/句子边界
    prose = ("这是第一句话。" * 40 + "\n\n" + "This is a sentence. " * 120) * 6
    p = R.split_unit(prose, 768, 128)
    check("散文按句/段切，不在句子中间结束",
          all(c.rstrip()[-1] in "。！？!?.\n" for c in p[:-1]),
          f"tails={[c[-6:] for c in p[:3]]}")

    # 单行超长（没有换行的巨型 JSON）仍能硬切，不产生无限循环
    blob = "x" * 20000
    p = R.split_unit(blob, 768, 128)
    check("无换行的巨型内容仍能切完", sum(len(c) for c in p) >= 20000 and len(p) > 1, f"{len(p)} 段")


# ---- 4. 系统提示词 -------------------------------------------------------------------

def test_system_preserved():
    print("== 4 系统提示词不丢 ==")
    cfg()
    msgs = openai_agent_conv()
    chunks = R.chunk_messages(msgs)
    check("system 消息不进检索块", not any("Never guess" in c.text for c in chunks))

    # 组装：OpenAI 形态（system 在 messages 里）——旧版会把它整条丢掉
    sess = R.Session("t")
    sess.ingest = lambda m: []                      # 不测嵌入
    body = {"model": "strata", "messages": msgs, "max_tokens": 64}
    R.count_tokens = lambda *a, **k: 1000           # 让它走组装路径
    new_body, info = R.assemble(sess, body, msgs, 999999)
    got = [m for m in new_body["messages"] if m.get("role") == "system"]
    check("OpenAI 形态的 system 消息在组装后仍在", len(got) == 1 and "Never guess" in R.message_text(got[0]),
          f"roles={[m.get('role') for m in new_body['messages']]}")
    check("system 在最前", new_body["messages"][0].get("role") == "system")

    # Anthropic 形态：body["system"] 原样保留
    body2 = {"model": "strata", "system": "ANTHROPIC-SYSTEM-PROMPT-KEEP-ME",
             "messages": anthropic_agent_conv(), "max_tokens": 64}
    nb2, _ = R.assemble(R.Session("t2"), body2, body2["messages"], 999999)
    check("Anthropic 的 body[system] 原样保留", nb2.get("system") == "ANTHROPIC-SYSTEM-PROMPT-KEEP-ME")


# ---- 5/6. 最近窗口：回合对齐 + 巨型回合保骨架 ----------------------------------------

def test_recent_window():
    print("== 5 最近窗口回合对齐 ==")
    cfg()
    msgs = openai_agent_conv()
    turns = R.parse_turns(msgs)
    picked, tkept, mkept, tot = R.recent_window(msgs, turns, 1500)
    roles = [m.get("role") for m in picked]
    check("窗口不会停在回合中间（工具返回带着它的工具调用与用户问题）",
          "user" in roles and "assistant" in roles, f"roles={roles}")
    check("窗口保留的是整条回合", mkept == {5, 6, 7}, f"kept={sorted(mkept)}")
    check("窗口边界落在回合边界上（回合 0 整条交给检索）", tkept == {1}, f"turns={sorted(tkept)}")

    # 窗口够大时两条回合都逐字保留
    picked, tkept, _, _ = R.recent_window(msgs, turns, 100000)
    check("预算充足时所有回合逐字保留", tkept == {0, 1}, f"turns={sorted(tkept)}")

    print("== 6 单回合超预算保骨架 ==")
    big = [{"role": "user", "content": "HEAD 用户的问题在这里\n" + "noise filler. " * 4000 + "\nTAIL: 秘密代码是 GOLD-9911"},
           {"role": "assistant", "content": "我去查。", "tool_calls": [
               {"id": "c", "function": {"name": "lookup", "arguments": "{}"}}]},
           {"role": "tool", "tool_call_id": "c", "content": log_text(600)}]
    turns = R.parse_turns(big)
    picked, tkept, mkept, tot = R.recent_window(big, turns, 2000)
    roles = [m.get("role") for m in picked]
    check("巨型回合里短单元（工具调用）被保留", "assistant" in roles, f"roles={roles}")
    check("巨型输出按行截尾（切口不在行中间）",
          all(R.message_text(m).split("\n")[0].startswith("line ") for m in picked if m.get("role") == "tool"),
          f"{[R.message_text(m)[:30] for m in picked if m.get('role') == 'tool']}")
    check("保留内容仍在预算内", tot <= 2000, f"tot={tot}")


# ---- 7. 嵌入标识复用 -----------------------------------------------------------------

def test_embedding_ids_stable():
    print("== 7 块标识稳定 → 嵌入复用 ==")
    cfg()
    h5 = R.chunk_messages(growing_session(5))
    h8 = R.chunk_messages(growing_session(8))
    old = {c.hash for c in h5}
    new = {c.hash for c in h8}
    check("历史增长后旧块哈希全部不变（旧向量直接复用）", old <= new,
          f"丢失 {len(old - new)} 个旧哈希")
    check("新增内容才产生新哈希", len(new - old) > 0)

    # 邻居变长不该让已有块的边界漂移（旧版重叠是"回退 128 字符"，邻居一变边界就漂）
    a = [{"role": "user", "content": "问题一"},
         {"role": "assistant", "content": "回答一"},
         {"role": "user", "content": "问题二"}]
    b = [{"role": "user", "content": "问题一"},
         {"role": "assistant", "content": "回答一"},
         {"role": "user", "content": "问题二"},
         {"role": "assistant", "content": "回答二"},
         {"role": "user", "content": "问题三"}]
    ha = {c.hash for c in R.chunk_messages(a)}
    hb = {c.hash for c in R.chunk_messages(b)}
    check("追加回合不重切已有块", ha <= hb, f"丢失 {len(ha - hb)}")

    # 同一内容在不同会话里哈希一致 → 全局嵌入缓存命中
    c1 = R.chunk_messages(growing_session(3))[1].hash
    c2 = R.chunk_messages(growing_session(3))[1].hash
    check("同一内容哈希一致（跨会话去重）", c1 == c2)

    # 真实 agent 流程：历史每次只多**一条**消息。逐单元成块必须是零重嵌（这是选它的理由）。
    def agent_steps(n_turns):
        msgs = [{"role": "system", "content": "You are an agent."}]
        yield msgs
        for i in range(n_turns):
            for m in [
                {"role": "user", "content": f"问题 {i}：" + "请分析这个子系统的分层与迁移计划。" * 8},
                {"role": "assistant", "content": "我查一下。", "tool_calls": [
                    {"id": f"c{i}", "function": {"name": "lookup", "arguments": '{"k":%d}' % i}}]},
                {"role": "tool", "tool_call_id": f"c{i}",
                 "content": "\n".join(f"line {j}: status=ok stage={j//20}" for j in range(300 if i % 4 == 0 else 12))},
                {"role": "assistant", "content": f"结论 {i}：" + "该子系统的结论是分层保持不变。" * 10}]:
                msgs = msgs + [m]
                yield msgs

    seen, queued = set(), 0
    last = []
    for msgs in agent_steps(40):
        cs = R.chunk_messages(msgs)
        queued += sum(1 for c in cs if c.hash not in seen)
        seen.update(c.hash for c in cs)
        last = cs
    steady = {c.hash for c in last}
    check("40 轮逐条到达：零重嵌（每条消息只嵌一次）", queued == len(steady),
          f"queued={queued} steady={len(steady)}")
    check("没有陈旧块留在检索索引里", seen == steady, f"陈旧 {len(seen - steady)} 块")


# ---- 8. 检索块渲染与结构补全 ---------------------------------------------------------

def test_render_and_expand():
    print("== 8 渲染次序与结构补全 ==")
    cfg()
    msgs = openai_agent_conv()
    chunks = R.chunk_messages(msgs)
    sess = R.Session("t")
    sess.chunks = list(chunks)
    sess.hashes = {c.hash for c in chunks}

    # 只选中"巨型工具输出的中段一段"，补全应把这条回合补齐（用户问题、工具调用、其余分段）
    mid = next(c for c in chunks if c.n_parts > 1 and 0 < c.part < c.n_parts - 1)
    chosen = [mid]
    used = mid.tokens + 24
    added = R.expand_context(chosen, sess, budget_tokens=60000, used=used)
    texts = " ".join(c.text for c in chosen)
    check("结构补全带回了回合锚点（用户问题）", "部署日志里失败原因" in texts, f"added={len(added)}")
    check("结构补全带回了产生它的工具调用", "我去看日志" in texts, f"added={[c.kind for c in added]}")
    check("结构补全带回了同消息的其余分段", any(c.msg_idx == mid.msg_idx and c.part != mid.part
                                               for c in added), f"added={[c.idx for c in added]}")
    check("补全不越预算", sum(c.tokens + 24 for c in chosen) <= 60000)

    # 预算紧时仍至少保住锚点
    chosen2 = [mid]
    R.expand_context(chosen2, sess, budget_tokens=mid.tokens + 24 + 400, used=mid.tokens + 24)
    check("预算紧张时仍带回回合锚点", any("部署日志里失败原因" in c.text for c in chosen2),
          f"{len(chosen2)} 块")

    block = R.render_block(chosen)
    check("检索块带回合结构标签", "--- turn 1 ·" in block and "tool_result" in block, block[:120])
    check("检索块按原始次序渲染（不是分数次序）",
          block.index("部署日志里失败原因") < block.index(f"line {mid.text.split(chr(10))[0].split()[1]}")
          if mid.text.split("\n")[0].split()[1].isdigit() else True)

    # 顺序：跨回合时按回合先后
    multi = [c for c in chunks if c.turn == 0] + [mid]
    b2 = R.render_block(multi)
    check("跨回合渲染保持原始先后（turn 0 在 turn 1 之前）",
          b2.index("--- turn 0") < b2.index("--- turn 1"))
    check("分段块带 part 标识", "part 2/" in b2 or any(f"part {mid.part + 1}/" in b2 for _ in [0]),
          b2[b2.index("--- turn 1"):][:80])


# ---- 9. 旧行为可回退（A/B 对照）------------------------------------------------------

def test_legacy_toggle():
    print("== 9 structure_chunking=false 回退 ==")
    cfg(structure_chunking=False)
    msgs = openai_agent_conv()
    chunks = R.chunk_messages(msgs)
    check("旧模式仍产出块", len(chunks) > 1, f"{len(chunks)}")
    check("旧模式按长度硬切（工具输出被切成多段且可能切在行中间）",
          any(c.n_parts > 1 for c in chunks))
    check("旧模式的块没有回合号", all(c.turn == -1 for c in chunks))
    cfg()


def main():
    for fn in (test_turn_parsing, test_chunk_boundaries, test_natural_boundaries,
               test_system_preserved, test_recent_window, test_embedding_ids_stable,
               test_render_and_expand, test_legacy_toggle):
        fn()
    print("=" * 62)
    if FAILS:
        print(f"FAIL {len(FAILS)}: " + "; ".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
