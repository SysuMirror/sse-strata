# 写书会话 848a12ed 早停诊断

> 会话：`848a12ed-bd2e-4964-ac46-18d2f341affd`
> 用户任务：探索当前代码，在 `book/` 目录写一本类似 c-primer-plus 的教学书，
> **持续工作直到完成，并且不要开子 agent**。
> 模型：`qwen-3.8-flash-fast` → `strata-a`（快道 A，已修复特殊 token 早停）。
> 结论：**这次不是特殊 token（A 模式）早停，是“计划完就 EOS”的 B 模式**：
> 模型 thinking 末尾写“Let me write/read…”，但响应里 **0 个 tool call**，`finish=stop`；
> 长上下文（146k/156k）+ 写书的大 Write 负载放大了这个行为。书没写完。

---

## 1. 直接证据（原始推理留档）

| 时间（北京） | infer id | messages | prompt | completion | content | reasoning | tool_calls | finish | 请求里的特殊 token |
|---|---|---:|---:|---:|---:|---:|---:|---|---|
| 12:39:36 | `20261019-123936-abb483` | 147 | 146,433 | 3,240 | 42 字符 | 6,995 字符 | **0** | `stop` | 0 |
| 12:45:48 | `20261019-124548-da1258` | 152 | 156,568 | 267 | 26 字符 | 772 字符 | **0** | `stop` | 0 |
| 12:04:19（子 agent） | `20261019-120419-5896b3` | 197 | 79,465 | 204 | 20 字符 | 398 字符 | **0** | `stop` | 0 |
| 12:09:18（子 agent） | `20261019-120918-151e77` | 202 | 79,792 | 504 | **0 字符** | 1,571 字符 | **0** | `stop` | 0 |

对应的 response：

```text
12:39:36
  content:   "第 3 部完成。现在写引擎内核部分——先补两处启动顺序的坑，这是第 06 章的核心。"
  reasoning: ... "I already wrote 04 with wrong facts. I need to rewrite it. Let me overwrite 04
             completely with verified content. Let me write 04 and 05 now." ...
  tool_calls: 0    finish: stop

12:45:48
  content:   "第 09 章 prefill。先读它自己的设计注释。"
  reasoning: ... "Let me be efficient: read the header comment of src/prefill/prefill.cpp ...
             and grep for yield/chunk." ...
  tool_calls: 0    finish: stop

12:04:19（子 agent）
  content:   "Now the frontend JS."
  reasoning: ... "Let me read 1-400, 570-1000, 1180-1600, 1600-2000 in parallel." ...
  tool_calls: 0    finish: stop

12:09:18（子 agent）
  content:   ""（thinking-only）
  reasoning: ... 最后一条 tool result 是 "Wasted call — file unchanged since your last Read"，
             随后是 "[handback-send-enforce] Your report has not been delivered"；
             thinking 末尾 "Let me try offset 1 limit 399." ...
  tool_calls: 0    finish: stop
```

> 这四次的共同点：**thinking 已经明确计划了下一步，但响应没有 tool call**，
> 正文只是一句进度/计划句，然后 `finish=stop`。这不是“没有正文”，而是“没有执行”。

---

## 2. 为什么不是特殊 token（A 模式）

- 四次 early-stop 的**请求里 `<|endoftext|>` / `<|im_end|>` / `<|im_start|>` 计数都是 0**；
- A 的 `/v1/status` 显示 `prompt_escapes={"calls":365,"literals":1603}`，
  说明这次会话里其他含特殊 token 的请求确实被转义了；
- `stops_while_reasoning=4`（thinking-only 的停止有计数），但这些有正文的 early stop
  不是特殊 token 截断；
- 没有 `max_tokens`（completion 3,240/267/204/504，max 32,000），没有 API error，没有 429
  直接挂在这些请求上。

所以：**A 修复工作正常；这次是残留的 B 模式（plan-then-EOS）**。

### 2.1 工具 schema / max_tokens / 前缀三项检查

用户怀疑“前缀丢弃到 tool schema”或“max_tokens 导致早停”。逐项核对：

| 检查 | 结果 |
|---|---|
| **tool schema 是否丢了** | **没丢**。两次 early-stop 的 `request.sent.tools` 都有 **55 个工具，`Write` 在列表里**；system message（6,019 字符）也在。模板会把 tools 渲染成 `# Tools`，模型有 Write 的定义。 |
| **max_tokens 是否截断** | **不是**。`max_tokens=32000`；completion 只有 3,240 / 267；引擎日志是 `thinking: 3194 of max 32000` → `done: 3240 tokens (stop)`、`thinking: 232 of max 32000` → `done: 267 tokens (stop)`；没有 `max_tokens` 停止。 |
| **是否开始写 tool call 后被截断** | **没有**。引擎日志在这两次 `done` 之前**没有 `writing a tool call` 行**；模型没有进入 tool-call 模式，不是“工具调用被 max_tokens/特殊 token 切断”。 |
| **前缀是否被丢** | **部分丢了，但丢的不是 tool schema，是“原始任务”**。这两次请求 `mode=assembled`，`client_msgs=315` → `sent_msgs=147`，代理组装时**丢了 168 条旧消息**；在 `sent` 的 147 条 messages 里**找不到**原始任务“请你探索…写一本教学书…持续工作…不要开子agent”。system 和 tools 还在，但用户的任务/约束被检索组装挤掉了。 |

结论：

- **tool schema 没丢、max_tokens 没截断、tool call 没开始**；
- **真正被丢的是原始任务前缀**（RAG 组装的近期窗口 + 检索块没有保留首条用户任务）。
  模型从 assistant/tool 历史里仍然知道“在写书”，但“持续写完 / 不要开子 agent / 下一章是哪一章”
  这些硬约束已经不在上下文里，容易退化成继续分析代码、然后停在计划句。

### 2.2 回放实验

用留档的 `20261019-123936-abb483` 原样回放 A（`max_tokens=4000`、`reasoning_budget=2000`）：

```text
HTTP 200 in 70 s
finish_reason: stop
prompt_tokens 146,433; completion_tokens 2,690
content: 2,867 字符（一大段代码注释/事实摘录）
tool_calls: 0
```

- 模型有完整的 55 个 tools（含 `Write`），但仍然**没有调用 Write**；
- 它输出的是“事实摘录/分析”，不是第 04/05 章；这与“原始任务被组装挤掉、只剩代码分析上下文”
  一致；
- 回放不是 max_tokens 截断（completion 2,690 < 4,000），也不是 tool schema 缺失。
- 所以：**tool schema 不是问题，max_tokens 不是问题；任务前缀被组装丢掉 + 模型的
  plan-then-EOS 行为共同导致“说要写、但没写”。**

### 2.3 任务卡实验：补回任务锚点后，模型立刻调工具

在同一条 146,433-token 的 `sent` 请求末尾追加一条任务卡：

```text
TASK CARD（来自用户，必须遵守）：你正在写 /home/cloud/book 这本书。
当前目标：用 Write 工具把第 04 章《网关：分流与守卫》写到
/home/cloud/book/04-网关分流与守卫.md。不要停在计划句；现在调用 Write，写出完整章节。
```

参数：`max_tokens=8000`、`reasoning_budget_tokens=2000`，直打 A（8091）。

结果：

```text
HTTP 200 in 10.1 s
finish_reason: tool_calls
usage: prompt 146,507; completion 434
content: 0 字符；reasoning: 1,304 字符；tool_calls: 1
tool: Bash（先检查 /home/cloud/book 里 04 章是否已经是核实版本）
引擎日志: writing a tool call: Bash: 322 of max 8000 ... 391 of max 8000
```

对比：

| 请求 | tool_calls | finish | 结果 |
|---|---:|---|---|
| 原始 assembled prompt（无任务卡） | **0** | `stop` | 输出计划句/事实摘录，5–75 s 结束 |
| 同 prompt + 任务卡 | **1** | `tool_calls` | 10 s 内开始检查 04 章文件 |

结论：**tool schema 一直在、max_tokens 也够；真正缺的是“当前该做什么”的任务锚点。**
补回任务卡后，模型立刻从“继续分析/计划”切回“调工具执行”。

---

## 3. 为什么这次 B 模式特别多

1. **上下文很长**：主会话 146k / 156k tokens，子 agent 79k；模型在长上下文里更容易
   “计划完就结束”，尤其面对“写一整章”的大任务。
2. **Write 负载很大**：书的一章约 12–21 KB；模型准备重写 04/05、写 06/09 时，
   thinking 里说“Let me write now”，但没有发出 Write 调用。
3. **子 agent 的 harness 反馈**：`Wasted call — file unchanged since your last Read`
   只告诉“文件没变”，没告诉它**已读过哪些 range**；模型反复猜 range，最后 thinking-only 结束。
   `[handback-send-enforce]` 又要求它交付报告，它也没交付。
4. **违反“不要开子 agent”**：主会话开了 3 个子 agent；`Agent` 调用 9 次。
   01:33 北京出现 `429 · 模型并发已满（上限 2）`，会话被中断/重试，增加了上下文噪声。
5. **长任务 + 一次性要求**：用户要求“持续工作直到完成”，模型没有把任务拆成
   “写一章 → 落盘 → 下一章”的硬性循环，容易在章与章之间停住。
6. **原始任务被 RAG 组装挤掉**：`mode=assembled`、315→147 messages；system 和 55 个 tools
   还在，但“写完整本书 / 不要开子 agent / 下一章是哪章”这些用户任务与约束不在 `sent` 里。
   回放里模型只剩代码/事实上下文，输出 2,867 字符 notes、**0 个 tool call**。
   这不是 tool schema 丢失，是**任务锚点丢失**。

---

## 4. 书当前状态

`/home/cloud/book/` 现有：

```text
01-这套系统是什么.md
02-硬约束如何塑造了设计.md
04-网关分流与守卫.md
05-网关检索组装与漂移重建.md
06-引擎启动与内存计划.md
07-专家三级存储与命中率.md
08-前向通路与内核.md
README.md
```

README 规划了 19 章 + 附录 A/B/C。当前**缺**：

- 03 一个请求的一生；
- 09 prefill：最大的瓶颈；
- 10–19；
- 附录 A/B/C。

另外模型自己在 12:39 的 thinking 里说 **“I already wrote 04 with wrong facts. I need to
rewrite it.”**，所以 04/05 可能需要按核实后的事实重写。

---

## 5. 建议

### 5.1 继续写书（最有效）

- **不要一次让它写整本**。按章拆：
  “读第 09 章需要的源码 → 写 `/home/cloud/book/09-prefill-最大的瓶颈.md` → 写完后立刻继续第 10 章”。
- 每章一个明确动作：**Write 到文件**；不要让它在 thinking 里计划完就结束。
- 长会话到 100k+ tokens 后，开新会话/新 part，把已完成章节和“下一章任务”作为简短上下文带过去。
- 如果它再说“Let me write…”，直接回一句：
  “现在调用 Write 把第 N 章写到 `book/N-*.md`；写完再继续下一章。”
- 需要重写 04/05 时，明确给出“以核实事实为准重写”的指令和对应源码位置。

### 5.2 防 B 模式（harness/agent 规则）

- agent 规则：**thinking 里说“Let me write/read”后，必须真的发出 tool call；
  如果这一轮不调用工具，就直接交付内容/总结，不能停在计划句。**
- 代理层可加一个 guard（需要重启代理，先经用户同意）：
  - 如果 `finish=stop`、`tool_calls=0`，且正文/推理末尾匹配 “Let me … / I'll … / 现在写…”，
    注入一条 “continue: 调用工具或直接写内容” 的提醒；
  - 连续两次同类 Read/Write 计划没有 tool call，就强制总结/提醒。
- 子 agent 的 `Wasted call` 提示建议补上“已读 range 列表”，避免模型反复猜 offset。
- 遵守“不要开子 agent”的指令；如果必须开，最多 1–2 个，并处理 429。

### 5.3 书的质量

- 04/05 是模型自己标记“事实有误”的章节，建议先审阅/重写，再继续 03、09–19。
- 每章落盘后检查文件存在、行数/字数合理，再让模型进入下一章；
  不要让“第 N 章完成”只停留在正文里。

### 5.4 代理组装要保留“任务锚点”（可选，需重启代理）

- 当前 `assemble` 的近期窗口 + 检索块会把首条用户任务挤掉（315→147）。
- 建议在组装结果里**固定保留**：
  - 首条用户任务（或一句任务摘要）；
  - 当前章节/下一步目标；
  - 用户约束（不要开子 agent、每章必须 Write 落盘）。
- 这部分可以放在 system message（它不会被组装丢掉），或作为组装块的第一块。
- **实测验证**：同一条 146,433-token 请求末尾加一条任务卡后，模型 **10.1 s 内发出
  `Bash` tool call**（`finish=tool_calls`）；不加任务卡时是 0 tool calls、`stop`。
  这说明补回任务锚点是有效的。
- 改 `rag-proxy` 需要重启代理，按红线先经用户同意。

---

## 6. 复现/核对命令

```bash
# 会话
ls -la /home/cloud/.claude/projects/-home-cloud/848a12ed-bd2e-4964-ac46-18d2f341affd.jsonl

# 原始推理记录
python3 -I - <<'PY'
import sqlite3
con=sqlite3.connect('/home/cloud/logs/infer.db'); con.row_factory=sqlite3.Row
for r in con.execute("""select id,iso,prompt_tokens,completion_tokens,content_chars,
                               reasoning_chars,tool_calls,finish_reason
                        from infer where id in
                        ('20261019-123936-abb483','20261019-124548-da1258',
                         '20261019-120419-5896b3','20261019-120918-151e77')"""):
    print(dict(r))
PY

# 书的现状
find /home/cloud/book -maxdepth 1 -type f | sort
```

> 结论一句话：**这次不是特殊 token 早停，是模型在长上下文/长写作任务里
> “计划完就 EOS、没有发出 tool call”的 B 模式。A 修复没有回退；书没写完，
> 需要按章重跑并把 Write 落盘作为硬性动作。**
