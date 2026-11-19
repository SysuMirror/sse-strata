# Claude 早停 / “只有 thinking、没有正文” 初步定位

> 日期：2026-10-18（北京时间）  
> 范围：本机 Claude Code 通过外接中转站调用**本 Pod 部署模型**的会话（2026-10-17 晚为主），  
> 以及同类问题的历史样本。本文只做**定位与证据索引**，没有改任何服务、没有重启引擎/网关。  
> 相关旧条目：`docs/项目背景与问题总览.md` §5 问题 #4/#5；`docs/plans/GENERIC-MOE-ENGINE-PLAN.md` §7 #4/#5。

---

## 0. 结论摘要（先看这段）

1. **昨天（2026-10-17 22:00–23:53 北京时间）Claude Code 使用本地模型时，确实出现了“只有 thinking、没有正文”的早停。**
2. **主因已定位：特殊 token 碰撞。** 提示词/工具结果里出现了**字面量** `<|endoftext|>` / `<|im_end|>`
   （来自 `generate.cpp` 第 394/514 行的注释）。模型在 thinking 或工具参数里复述这行时，真的生成了
   **EOS 特殊 token**（248044 / 248046）；引擎把 EOS 当成正常轮次结束，立即停止生成。
   于是：thinking 被截断在半句，正文为空；如果它正在写工具参数，则工具调用 JSON 被截断。
3. **不是“上下文一长就必然坏”。** 同一天快道（strata-a）里：
   - 含特殊 token 的请求 **58 次**：`thinking-only` **16 次**、工具 JSON 截断 **5 次**、有正文 20 次；
   - 不含特殊 token 的请求 **295 次**：`thinking-only` **0 次**、坏工具 JSON **1 次**、有正文 169 次。
   触发条件是**内容里出现特殊 token 字面量**，不是单纯的 prompt 长度。
4. **最小复现（引擎层，已实测）**：同一句解释任务，保留 `<|endoftext|>` / `<|im_end|>`
   → `finish=stop`、`content=None`、reasoning 截断在 `... end-of-turn token (`；
   把这两个字面量删掉 → 正常输出 176 tokens 正文。命令见 §3.4。
5. **另有一类少数残余模式**：提示词里没有特殊 token，模型“计划完（Let me run…）就直接 EOS”，
   没有正文也没有工具调用。它在 10-17 的快道里没有出现；在普道（strata-b）有 2/1383，
   在 10-07/10-08/10-14 的历史 Claude 会话里出现过。此模式尚未根因，见 §4.3。
6. **未做任何停服动作。** 修复方向里有需要重启网关或 serve 进程的，必须先经用户同意（红线）。

---

## 1. 调用链路与被测对象

```
Claude Code (本机, requestedModel=qwen-3.8-flash-fast)
   │  Anthropic /v1/messages
   ▼
https://api.ssemarket.cn          ← 外接中转站（Anthropic → OpenAI 协议转译）
   │  OpenAI /v1/chat/completions, model=strata-a
   ▼
本 Pod :8080  rag-proxy           ← 唯一公网面；thinking_guard / 窗口守卫 / 推理留档
   │  http://127.0.0.1:8091
   ▼
strata-a 快道（GPU0）             ← 引擎模型 qwen3.8-flash-next-iq3_xxs
```

- Claude Code 侧配置：`~/qwen`（`ANTHROPIC_BASE_URL=https://api.ssemarket.cn`，
  `ANTHROPIC_DEFAULT_*_MODEL=qwen-3.8-flash-fast`）。
- 当前 `~/.claude/settings.json` 是 DeepSeek 配置；**2026-10-18 的 Claude 会话没有再走本地模型**
  （今天只用了 `deepseek-flash[1m]`），所以今天没有新的同类现场。
- 中转站把 Anthropic 请求转成 OpenAI 格式；`rag-proxy` 看到的 `model` 已经是 `strata-a`。
  抓到的请求里没有 Claude 的 `thinking` 字段，网关 `thinking_guard` 因此补了
  `reasoning_budget_tokens = max_tokens - 4096 = 27904`（`rag_proxy.py:264-314`）。
  这个预算不是本次早停的原因（见 §3.3）。
- `/concurrency` 显示快道引擎：`qwen3.8-flash-next-iq3_xxs`，`serving_slots=4`。
- **与 10-17 20:34 的引擎切换无关**：快道 A 当时切到了 `ssebed`，但本次最小复现是在
  **普道 B / 8092（legacy 引擎）**上做的，同样复现；且 `Strata` 与 `SSEStraBed` 的
  `generate.cpp` 差异只有注释（`diff` 已核，非注释行无差异）。根因在**共享的 serve 层
  （`Strata/serve/server.py`）+ 模型词表**，不是新引擎二进制。

---

## 2. 数据源与查询入口（可复现）

| 数据 | 位置 | 用途 |
|---|---|---|
| Claude Code 主会话 | `~/.claude/projects/-home-cloud/<session-id>.jsonl` | 客户端看到的 message / tool_use / tool_result |
| Claude Code 子代理 | `~/.claude/projects/-home-cloud/<session-id>/subagents/agent-*.jsonl` | 子代理的 thinking-only 现场 |
| 会话清单 | `ccsinfo sessions list` | 快速列出最近会话与消息数 |
| 推理元信息 | `~/logs/infer.db`，表 `infer` | 每次请求的 model / prompt / completion / finish / 字符数 / 分段位置 |
| 推理全文 | `~/logs/infer-segs/<日期>/seg-*.jsonl.gz` | `request.client`、`request.sent`、`response.content/reasoning/tool_calls` |
| 网关日志 | `~/deploy/logs/rag-proxy.out.log` | `ctx[...]` 窗口算术、思考预算、上游错误 |
| 引擎日志 | `~/deploy/logs/strata-a.out.log` | `thinking: N of max 32000`、`done: N (...)` |

`infer.db` 没有 `sqlite3` 命令行，用系统 `python3`：

```bash
python3 -I - <<'PY'
import sqlite3
con = sqlite3.connect('/home/cloud/logs/infer.db')
print(con.execute("select count(*) from infer").fetchone())
print(con.execute("select id,iso,model,prompt_tokens,completion_tokens,finish_reason "
                  "from infer order by ts desc limit 10").fetchall())
PY
```

单条记录全文（`seg` + `line` 来自上表）：

```bash
python3 -I - <<'PY'
import gzip, json
p = '/home/cloud/logs/infer-segs/2026-10-17/seg-235211-e3420.jsonl.gz'
r = json.loads(gzip.open(p, 'rt', encoding='utf-8').read().splitlines()[4])
print(json.dumps(r['response'], ensure_ascii=False, indent=1))
PY
```

Claude Code 会话用 skill 的提取脚本（注意：当前脚本不支持 `--output`，用重定向）：

```bash
cd ~/.agent/skills/Claude_Session_Inspector
python3 scripts/extract_claude_session.py f04d9927-40be-4f68-826a-1abf1bd2c85c --compact \
  > /tmp/f04d9927.json
```

一键扫描“thinking-only / 工具 JSON 截断”（本文用的脚本，只读）：

```bash
python3 -I ~/tools/scan_claude_early_stop.py --date 2026-10-17
python3 -I ~/tools/scan_claude_early_stop.py --date 2026-10-17 --model strata-a --json
```

> ⚠️ 原 inspector 的 `early_stop_anomalies` **漏报**了本次症状：它只标记
> `stop_reason=null/max_tokens/refusal/tool_use 无后续`，而本次是
> **`end_turn` + 只有 thinking + 无 text/tool_use**。本次分析改为按 `message.id` 把同一条
> assistant 响应的多个 block 聚合后再判断（一个响应在 JSONL 里可能被拆成 thinking/text/tool_use
> 多条记录）。**该检测已补进 skill**（`thinking_only_end_turn`，并补上 `--output` 与子代理转录支持）。

---

## 3. 证据链

### 3.1 客户端层：Claude Code 自己承认“没有可见输出”

会话 `2f0111be`（2026-10-17 23:50–23:52 北京时间）的原始记录：

```json
{"type":"assistant","message":{"id":"msg_mvckrprw","content":[
 {"type":"thinking","thinking":"... The Options struct has many fields ... - Line 514: `/// When the model emits a turn-end token ("}
],"stop_reason":"end_turn"}}
{"type":"user","isMeta":true,"message":{"role":"user","content":
 "[Your previous response had no visible output. Please continue and produce a user-visible response.]"}}
{"type":"assistant","message":{"id":"msg_mvckru19","content":[
 {"type":"thinking","thinking":"... - Line 514: `/// 当模型吐出轮次结束 token（"}
],"stop_reason":"end_turn"}}
```

- 这是 **Claude Code 自动注入的续写提示**，不是用户输入。
- 两次续写都在同一位置再次截断，说明不是偶发网络问题。

会话 `f04d9927`（2026-10-17 23:21–23:47 北京时间）更典型：

- 31 个 assistant 响应中 **10 个 thinking-only**，`end_turn`；
- 触发 **7 次** `[Your previous response had no visible output...]`；
- 另有 **3 次** `InputValidationError: Edit was called with input that could not be parsed as JSON ...
  truncated output. Retry with valid JSON.`，其中最后一次的 `old_string` 正好停在：

```text
{"replace_all":false,"file_path":"/home/cloud/SSEStraBed/src/program/generate.cpp",
 "old_string":"    /// Stop when the model emits an end-of-turn token (
```

> 这正是问题 #5“明显该调用 tool 却没调”的客户端现场：工具 JSON 在特殊 token 处被截断，
> Claude Code 收到的是不完整 JSON，随后报错/丢弃。

### 3.2 网关留档层：请求里确实有特殊 token，响应确实停在那里

`infer.db` 的记录（`seg-235211-e3420.jsonl.gz` 第 4/6 行）与上面两轮完全对应：

| infer id | 开始（北京） | prompt | completion | content | reasoning | finish |
|---|---|---:|---:|---:|---:|---|
| `20261017-235221-665100` | 23:52:21 | 50,218 | 48 | 0 字符 | 185 字符 | stop |
| `20261017-235227-14cc81` | 23:52:27 | 50,241 | 69 | 0 字符 | 209 字符 | stop |

原始 `response`：

```json
{"content":"",
 "reasoning":"I see. The Options struct has many fields ... - Line 514: `/// When the model emits a turn-end token (",
 "tool_calls":[],"finish_reason":"stop"}
```

原始 `request` 的 tool result 里（`messages[16]`，role=tool）就是那一行源码：

```text
514	    /// 【当模型吐出轮次结束 token（<|endoftext|> 248044, <|im_end|> 248046, or --eos-ids).
```

- 该请求里 `<|endoftext|>` 与 `<|im_end|>` 各出现 1 次；
- 另外 `20261017-230116/230504/231024` 等 36k prompt 的大 thinking 案例，也都是同一行
  `generate.cpp:394`（英文版）被读进 tool result。

### 3.3 引擎层：EOS 是合法的停止条件，模型在 thinking 中提前吐了它

- `Strata/src/program/generate.cpp:394-396`：

```cpp
/// Stop when the model emits an end-of-turn token (<|endoftext|> 248044, <|im_end|> 248046, or --eos-ids).
bool stop_eos = false;
std::vector<int64_t> eos_ids = {248044, 248046};
```

- `Strata/serve/server.py:60,1277-1279`：

```python
IM_END = "<|im_end|>"
self.stop_ids = set(tokenizer.encode(IM_END, parse_special=True) +
                    tokenizer.encode("<|endoftext|>", parse_special=True))
```

- `Strata/serve/server.py:1630-1634`：整个渲染后的 prompt 用
  `tokenizer.encode(prompt, parse_special=True)` 编码。**用户内容里的字面量特殊 token
  因此会变成真正的特殊 token**，而不是普通文本。

- 引擎日志（快道 `~/deploy/logs/strata-a.out.log`）：
  - 48-token 案例：`thinking: 47 of max 32000 tokens` → `done: 48 tokens ... (stop, cancel=False)`；
  - 大 thinking 案例：`done: 6860 tokens ... (stop)`、`done: 10273 ... (stop)`、
    `done: 5480 ... (stop)`、`done: 7114 ... (stop)`；
  - 这些行附近**没有** `thinking budget reached (...)`，`max` 仍是 32000。
    所以不是 `reasoning_budget_tokens=27904` 用完，也不是 `max_tokens` 截断。

### 3.4 最小复现（A/B，已实测）

> 只打了**普道 8092**（测试/压测允许的通道），一次约 100–300 tokens，未动快道 8091。
> API key 从 `~/logs/strata-api-key.txt` 读取，不写进文档。

**A. 含特殊 token（复现症状）**

```bash
KEY=$(cat ~/logs/strata-api-key.txt)
curl -s http://127.0.0.1:8092/v1/chat/completions \
 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
 -d '{"model":"strata-b","messages":[{"role":"user","content":"Explain this C++ comment in one sentence:\n/// Stop when the model emits an end-of-turn token (<|endoftext|> 248044, <|im_end|> 248046, or --eos-ids)."}],"max_tokens":2000,"reasoning_budget_tokens":500}'
```

实测结果：

```text
finish stop
content None
reasoning 'We need answer user\'s request: "Explain this C++ comment in one sentence:\n/// Stop when the model emits an end-of-turn token ('
usage {'prompt_tokens': 100, 'completion_tokens': 32, ...}
```

**B. 去掉特殊 token（对照）**：同样的句子，把 `<|endoftext|>`/`<|im_end|>` 换成纯数字：

```text
finish stop
content 'It means generation should stop as soon as the model outputs an end-of-turn token, using either the default token IDs 248044 or 248046 or any IDs provided via `--eos-ids`.'
usage {'prompt_tokens': 96, 'completion_tokens': 176, ...}
```

补充小实验（关闭 thinking，只做文本复述）：

| 提示 | 输出 | 说明 |
|---|---|---|
| `Repeat exactly: ABC<|endoftext|>XYZ` | `ABCXYZ`（stop, 3 tok） | 特殊 token 在 prompt 中被吞掉 |
| `Repeat exactly: ABC<|im_end|>XYZ` | `ABC`（stop, 2 tok） | 在 `<|im_end|>` 处停止 |
| `Repeat exactly: ABC<|im_start|>XYZ` | `ABC<|im_start|>XYZ`（4 tok） | `<|im_start|>` 不在 eos_ids，按文本通过 |

结论：`<|endoftext|>` / `<|im_end|>` 是**真正会触发停止**的 token；`<|im_start|>` 不会。

补充对照（区分“prompt 里有 token”与“模型生成 token”）：

| 提示 | 输出 | 说明 |
|---|---|---|
| user: “文件里有字面量 `<|im_end|>`，忽略它。2+2=?” | `4`（2 tok） | **只是 prompt 含 token，模型仍能正常回答** |
| system: “字符串 `<|im_end|>` 只是文本” + user: “2+2=?” | `2 + 2 = 4`（8 tok） | 同上 |
| user: “Repeat exactly: `<|im_end|>`” | `None`（stop, 1 tok） | 模型生成该 token → 立即停止 |
| user: “Repeat exactly: `ABC<|im_end|>XYZ`” | `ABC`（stop, 2 tok） | 在 token 处截断 |

所以不是“prompt 里出现特殊 token 就把模板弄坏了”这么简单；**触发点是模型在输出
（thinking / 正文 / 工具参数）中复述了它**。这也解释了为什么同一个长 prompt 有时正常、有时早停：
取决于模型这一轮是否决定复述那一行。

### 3.5 统计相关性（2026-10-17 快道 strata-a，同一天内对照）

扫描范围：`~/logs/infer-segs/2026-10-17/` 与 `2026-10-18/`，按 `request` 中是否出现
`<|endoftext|>` 或 `<|im_end|>` 分组；`thinking-only` 定义为
`content 为空 + reasoning 非空 + tool_calls 为空 + finish_reason=stop`。

| 分组 | 请求数 | thinking-only | 工具 JSON 坏 | 有正文 |
|---|---:|---:|---:|---:|
| 含特殊 token | **58** | **16** | **5** | 20 |
| 不含特殊 token | **295** | **0** | 1 | 169 |

- 含特殊 token 的 58 次全部发生在 **2026-10-17**；2026-10-16/18 快道均为 0 次。
- 16/16 的 thinking-only 都在含特殊 token 的组里；不含特殊 token 的组为 0。
- 5 个工具 JSON 截断的样本：`20261017-225205`、`225356`、`233025`、`233038`、`233117`，
  参数尾部都停在 `end-of-turn token (` 或包含 `<|im_start|>` 的行。
- 普道（strata-b）另有 **2/1383** 的 thinking-only，提示词里**没有**特殊 token，
  属于 §4.3 的残余模式。

### 3.6 受影响会话索引（北京时间）

| 会话/子代理 | 时间 | 模型/通道 | thinking-only | 自动续写注入 | 现场任务 |
|---|---|---:|---:|---:|---|
| `fc0c219f` 的子代理 `agent-aa03a26d2fbc364d1` | 10-17 23:00–23:13 | qwen-3.8-flash-fast / strata-a | 3 | 1 no-visible + 2 handback | “Annotate generate.cpp 113-620” |
| `f04d9927` | 10-17 23:21–23:47 | qwen-3.8-flash-fast / strata-a | 10 | 7 no-visible | 给 generate.cpp 补中文注释 |
| `2f0111be` | 10-17 23:50–23:52 | qwen-3.8-flash-fast / strata-a | 2 | 1 no-visible | “补充完整当前文件的中文注释” |
| `19877945` 的子代理 `agent-ad511ec6eeacdd373` | 10-17 22:10 | qwen-3.8-flash-fast / strata-a | 1 | 1 no-visible | 编辑 generate.cpp includes |
| `fc0c219f` 主会话 | 10-17 22:34–23:18 | qwen-3.8-flash-fast / strata-a | 0 | 0 | 主代理本身没有 thinking-only |
| `4cb7e339` | 10-17 11:26–12:06 | qwen-3.8-flash-fast / strata-a | 0 | 0 | 当日早间，未触发 |
| `d2917afa` | 10-17 17:42–21:34 | deepseek-flash[1m] | 0 | 0 | 非本地模型 |
| `bc087195`（当前） | 10-18 11:38– | deepseek-flash[1m] | 0 | 0 | 非本地模型 |

历史同类（“之前就存在”）：

| 会话 | 时间 | thinking-only | 尾部特征 | 模式 |
|---|---|---:|---|---|
| `558cc126` | 10-07 23:34 | 1 | “Let me grep for recent_window…” | B（无特殊 token） |
| `2d15dc44` | 10-08 14:19 / 14:21 / 20:27 | 3 | “Let me update…” / “Let me remove…” / “Let me write the final answer.” | B |
| `acfed746` | 10-14 20:21 | 1 | “Let's write the scripts.” | B |
| `a67c5b95` | 10-15 22:35 | 1 | thinking 为空（另一类） | 待查 |
| `f04d9927` 等 | 10-17 晚 | 16+5 | 全部停在 `end-of-turn token (` | A（特殊 token） |

> 说明：更早的 B 模式都是“计划完就结束”，没有特殊 token；10-17 的 A 模式是这次集中爆发的直接原因。
> 两者都表现为“只有 thinking”，但机制不同，修复/验证要分开。

---

## 4. 根因分析

### 4.1 机制（A 模式，已确认）

1. 用户让 Claude Code 给 `generate.cpp` 加注释；`Read`/`Grep` 的 tool result 把第 394/514 行
   （含 `<|endoftext|>` 与 `<|im_end|>` 字面量）带进提示词。
2. `serve/server.py:1634` 用 `parse_special=True` 编码整个 prompt，这些字面量变成真正的
   特殊 token 248044 / 248046。
3. 模型在 thinking 中复述这行（或把它写进 Edit 的 `old_string`）时，生成了 EOS token。
4. `server.py:1776` 命中 `stop_ids` → `finish="stop"`，立即停止；parser 仍处于
   `reasoning`（或 `call`）状态，于是：
   - thinking 被截断在半句，正文为空（`end_turn`）；
   - 如果正在写工具调用，`frontend.py:560-575` 的 `finish()` 只在调用完整时才发
     `tool_call`；不完整调用被**静默丢弃**（注释 #211），客户端看到的就是“该调 tool 却没调”。
5. Claude Code 检测到没有可见输出，注入续写提示；但续写请求的上下文里**仍然有那行 tool result**，
   模型再次复述、再次停在同一位置，形成 7 次续写循环。

### 4.2 为什么“上下文长”是相关但不是根因

- 长上下文会话里更容易读入大文件/文档，**恰好包含**特殊 token 字面量的概率更高；
- 本次 16 次事故的 prompt 从 36k 到 90k 不等，但也有 50k 的正常请求；
- 同一天 295 次不含特殊 token 的请求（其中不少也是长上下文）**0 次** thinking-only；
- 反过来，8k prompt 的 B 模式与特殊 token 无关。
- 所以：**先看内容里有没有特殊 token 字面量，再看长度**。

### 4.3 B 模式（残余，未根因）

特征：thinking 里写“Let me run / Let me write / Let's…”这类计划，然后 `finish=stop`，
没有正文、没有工具调用；prompt 里没有 `<|endoftext|>`/`<|im_end|>`。
10-18 02:47 普道（qwen3.8-flash-next）仍有 1 例：
`20261018-024709-b4570f`，prompt 8187、completion 17，尾部
“The path seems off. Let me list deploy directory more carefully.”。
可能解释（尚未验证）：
1. 模型在 thinking 结束后直接生成了 `<|im_end|>`（正常轮次结束 token），但没写 `</think>`；
2. 模型开始写工具调用，但在 `<function=...>` 标签完成前停止，parser 在
   `state=call / scall=None` 时会把缓冲当 content 发出；若 `scall` 已存在但不完整，
   `finish()` 会静默丢弃（#211），于是看起来“没调 tool”；
3. 需要引擎 `STRATA_DEBUG` 原始 token dump 才能区分（要重启/改环境，未做）。

---

## 5. 修复 / 规避建议（按“是否需要停服”分组）

### 5.1 立即规避（不需要重启，推荐先做）

- 在需要编辑 `generate.cpp:394/514` 这类行时，**不要让模型用 Edit 的 `old_string` 复述整行**；
  改用脚本按行号/唯一前缀替换，例如：

```bash
python3 - <<'PY'
from pathlib import Path
p = Path('/home/cloud/SSEStraBed/src/program/generate.cpp')
lines = p.read_text(encoding='utf-8').splitlines(keepends=True)
# 行号定位（第 514 行 = 下标 513），避免让模型复述 <|endoftext|>
line = lines[513]
if '【' not in line:
    lines[513] = line.rstrip('\n') + '  【当模型吐出轮次结束 token 时停止】\n'
p.write_text(''.join(lines), encoding='utf-8')
PY
```

- 给 Claude Code 的项目指令/提示里加一条：
  “遇到包含 `<|...|>` 字面量的代码行，不要原样复述；用行号或 `sed`/Python 脚本修改。”
- 读文件时如果不需要那几行，用 `Read --offset/--limit` 避开，减少触发概率。

### 5.2 网关侧消毒（需要重启 `rag-proxy`，先问用户）

在 `rag-proxy` 转发前，对 `messages` / `tool` 内容里的已知特殊 token 字面量做转义，例如：

```text
<|endoftext|>  →  < |endoftext| >      （插入不可见/零宽字符，或换成全角竖线）
<|im_end|>     →  < |im_end| >
<|im_start|>   →  < |im_start| >
```

优点：**不改引擎**、对所有客户端生效；缺点：会改变模型看到的文本，可能影响“精确复制代码”类任务；
需要评估是否只对 tool result 中的这些 token 生效。
重启命令（**未经同意不要执行**）：`supervisorctl -c ~/deploy/supervisord.conf restart rag-proxy`。

### 5.3 serve 层根治（需要重启 serve/引擎，先问用户）

`serve/server.py` 的 prompt 编码从“整段 `parse_special=True`”改成“模板标记特殊编码、消息内容按普通文本编码”
（或在 `template.render` 前对用户内容做 escape）。这是**正确的长期修复**，但改的是 serve 进程，
重启会影响在线服务，必须按红线先征得用户同意。

### 5.4 可观测性（P0，和修复同等重要）

- `serve` 在 `finish="stop"` 时记录**命中的 stop token id**、停止时 parser 的 `state`
  （reasoning / content / call）、以及 `reasoning_tokens` / `content_tokens`；
- `/metrics` 增加 `stops_while_reasoning`、`tool_calls_truncated` 计数；
- `infer_store` 的 `meta` 增加 `stop_token_id`、`parser_state`、`sp_token_in_prompt` 布尔；
- `Claude_Session_Inspector` 增加“按 message.id 聚合后，thinking-only + end_turn”检测
  （**已补，2026-10-18**：新增 `thinking_only_end_turn`、`assistant_thinking_only_count`，
  并补上 `--output` 与子代理转录）。

### 5.5 验证方法

1. 用 §3.4 的 A/B 命令，在修复前后各跑一次；期望 A 组也从 `content=None` 变为有正文。
2. 用 §3.5 的扫描脚本重跑 2026-10-17 的 58 个含特殊 token 请求（或它们的脱敏回放），
   期望 `thinking-only` 从 16 降到 0，`tool_bad_json` 从 5 降到 0。
3. 对一个真实 Claude Code 会话（如 `f04d9927` 的脱敏副本）做回归：
   不再出现 `[Your previous response had no visible output...]`。

---

## 6. 待确认问题（下一轮）

1. **具体命中哪个 token**：248044 还是 248046？需要引擎原始 token id（`STRATA_DEBUG`，需重启）。
2. **B 模式的根因**：模型 EOS、还是未完成 tool call 被 parser 丢弃？需要 raw token dump / 对同一 prompt 做多次采样。
3. **中转站是否丢字段**：抓到的请求没有 `thinking`；中转站的 Anthropic→OpenAI 映射是否丢了
   `thinking.budget_tokens`？这会影响思考长度，但不是本次停止的直接原因。
4. **其他特殊 token**：`<|image_pad|>`、`<|vision_start|>`、`<|im_start|>` 是否也会造成类似问题
   （`<|im_start|>` 实测不触发 EOS，但可能影响模板结构）。
5. **`infer_store.request.client` 的口径**：它记录的是 `thinking_guard` 之后的请求，不是中转站原始字节；
   后续排查“客户端到底发了什么”时需要补一个真正的 raw client 副本。
6. **是否要更新项目文档**：把 `docs/项目背景与问题总览.md` §5 #4/#5 的状态改为
   “主因已定位（特殊 token 碰撞）；残余 B 模式待查”，并链接本文。

---

## 7. 产物与证据索引

| 产物 | 路径 | 内容 |
|---|---|---|
| 本文 | `docs/reports/CLAUDE-EARLY-STOP-2026-10-18.md` | 结论、证据链、复现、修复方向 |
| 逐请求证据表 | `docs/reports/artifacts/claude-stop-evidence-2026-10-18.csv` | 24 条：16 thinking-only + 5 工具 JSON 截断 + 2 B 模式 + 1 max_tokens |
| 会话 inspector 输出 | `docs/reports/artifacts/session-f04d9927-inspector.json` | `f04d9927` 的 meta / stop_reason / tool_errors |
| 会话 inspector 输出 | `docs/reports/artifacts/session-2f0111be-inspector.json` | `2f0111be` 同上 |
| 会话 inspector 输出 | `docs/reports/artifacts/session-fc0c219f-inspector.json` | `fc0c219f` 主会话 + 2 个子代理；子代理里 3 次 thinking-only |
| 一键扫描脚本 | `~/tools/scan_claude_early_stop.py` | 读 `infer.db` + `infer-segs`，按特殊 token 分类 thinking-only / 工具 JSON 截断（只读） |
| 一键复现脚本 | `~/tools/repro_claude_early_stop.py` | `minimal` 最小 A/B；`replay --id ...` 原样重放留档请求；用法见 `docs/reports/CLAUDE-EARLY-STOP-REPRO-2026-10-18.md` |
| 复现方法 | `docs/reports/CLAUDE-EARLY-STOP-REPRO-2026-10-18.md` | 可直接抄的命令、实测输出、判读表与安全注意 |
| 关键会话 | `~/.claude/projects/-home-cloud/f04d9927-...jsonl` | 10 次 thinking-only、7 次自动续写、3 次 Edit JSON 错误 |
| 关键子代理 | `~/.claude/projects/-home-cloud/fc0c219f-.../subagents/agent-aa03a26d2fbc364d1.jsonl` | 3 次大 thinking-only |
| 关键推理记录 | `~/logs/infer-segs/2026-10-17/seg-235211-e3420.jsonl.gz` 第 4/6 行 | 48/69 token 的 thinking-only 原始请求/响应 |
| 引擎日志 | `~/deploy/logs/strata-a.out.log`（行号约 93779/94378/94718/95514/96026） | `done: ... (stop)` 与 thinking 进度 |
| 代码 | `Strata/serve/server.py:60,1277-1279,1630-1634,1776,1787-1795` | stop_ids / parse_special / 预算 |
| 代码 | `Strata/serve/frontend.py:560-575` | 不完整工具调用被丢弃（#211） |
| 代码 | `Strata/src/program/generate.cpp:394-396` | eos_ids |
| 代码 | `rag-proxy/rag_proxy.py:264-314,1907-1909,2011-2019` | thinking_guard / 请求留档 |

---

## 8. 根治实现与验证（2026-10-18）

> 状态：**已部署到快道 A（8091，2026-10-18 14:15–14:22 北京时间）**；
> 普道 B（8092）仍运行旧代码，待另约窗口。A 的重启经用户同意，B 未动。
> 部署/回滚步骤见 `~/archive/patches/serve-special-token-escape-2026-10-18/README.md`。

### 8.1 改法

- `Strata/serve/frontend.py`：从 tokenizer 的 `special_tokens` + `token_types` 取 type 3
  （CONTROL）字面量（本 pack 27 个），在用户内容里把字面量的第一个字符后插入 `U+2063`
  （INVISIBLE SEPARATOR）：
  `<|im_end|>` → `<⁣|im_end|>`。
  tokenizer 不再把它匹配成 248046/248044，而是按普通 BPE 文本编码。
- 覆盖 message content、reasoning_content、tool_calls arguments、tool description/parameters；
  不碰图片 source、名字/ID/dict key。
- `Strata/serve/server.py`：
  - `Service.prepare` 在 render/tokenize 前转义；
  - `/v1/messages/count_tokens` 同样转义（计数与实际 prompt 一致）；
  - `Detokenizer.push` 从模型输出里剥离 `U+2063`，客户端/工具 JSON 仍看到原始字面量；
  - `/metrics` / `/v1/status` 新增 `prompt_escapes` 与 `stops_while_reasoning` 计数；
  - `STRATA_ESCAPE_SPECIAL=0` 可关闭转义（A/B 或紧急回滚，需重启）。
- **不改引擎 C++**：EOS 语义本身正确；问题在 prompt 编码层。已确认最小复现在
  普道 B/8092（legacy）上同样成立，与 10-17 切到 ssebed 无关。
- 不改 `rag-proxy`：serve 是最后一道，覆盖所有客户端。

### 8.2 验证结果（部署前）

| 验证 | 结果 |
|---|---|
| 新增 `SpecialTokenEscape` 单元测试（7 个） | 全部通过 |
| 全量 `serve.test_server` | **124/125 通过**；唯一失败是既有的 `WebApp.test_page_and_files`（线上 index 已换成 `app-CXUPqADB.js`，测试仍找 `web/app.js`，与本次无关） |
| 真实 pack tokenizer 离线检查 | 未转义：248044×1、248046×3；转义后：248044×0、248046×2（模板 turn marker 保留），`<|im_start|>` 计数不变 |
| 真实模型 A/B（普道 8092，2 次小请求） | 未转义：`content=None`、reasoning 停在 `... end-of-turn token (`（复现）；转义：正常 206 tokens 正文，无早停 |

### 8.3 部署结果（快道 A，2026-10-18）

- 14:15:36 重启 `strata-a`；14:22:51 `ready`（约 7 分 15 秒冷启动，ssebed 读 39.97 GiB 专家）；
  **普道 B 未动，期间保持在线**。
- 直接打 8091，原始 prompt（含 `<|endoftext|>` / `<|im_end|>` 字面量）：
  HTTP 200、4.3 s、completion 219、`finish=stop`、**content 非空**；
  A 的 `/v1/status`：`prompt_escapes={"calls":1,"literals":2}`、`stops_while_reasoning=0`。
- 走完整代理路径（`model=qwen3.8-flash-fast` → 8091，等同 Claude Code 的通道）：
  HTTP 200、2.7 s、completion 225、**content 非空**、`cached_tokens=108`；
  A 的计数：`prompt_escapes={"calls":2,"literals":4}`、`stops_while_reasoning=0`。
  `infer.db` 留档 `20261018-142312-5bac01`：model `qwen3.8-flash-fast`、lane `快道`、
  prompt 113、completion 225、content_chars 194、reasoning_chars 586、finish `stop`、tool_calls 0。
- **原样复述场景**（最接近事故中的 Edit `old_string`）：让模型逐字复述那一行，
  HTTP 200、4.9 s、completion 369；`content` 与原始行完全一致（含 `<|endoftext|>` /
  `<|im_end|>` 文本），**没有 U+2063 残留**；`prompt_escapes={"calls":3,"literals":6}`、
  `stops_while_reasoning=0`。说明“输入转义 + 输出剥离”对精确复制也成立。
- **原始 incident prompt 重放**（22:2x，`seg-235211-e3420` 第 4 行，18 messages / 54 tools /
  prompt 50,237 tokens；为控制时长把 `max_tokens` 收到 4000、`reasoning_budget_tokens` 收到 2000）：
  HTTP 200、42.5 s、completion 2,146、`finish=tool_calls`、**1 个 Bash 工具调用**
  （`grep` 查 Options 字段的用法），reasoning 6,003 字符；content 为空是工具调用的正常形态。
  工具参数里没有 U+2063、也没有误生成的 EOS；`prompt_escapes={"calls":4,"literals":9}`、
  `stops_while_reasoning=0`。**模型不再停在中途，而是继续执行任务。**
- 两条通道 `/health` 均为 ok；A 日志无 error/traceback。
- **B 仍是旧代码的现场**：14:12:34（A 重启前）普道 B 出现 1 次 A 类早停
  `20261018-141234-591df7`：prompt 4493、completion 469、content 0、reasoning 1202、
  finish `stop`、tool_calls 0。说明 B 也需要部署；本次没有对 B 做任何操作。
  B 的 `/v1/status` 也还没有 `prompt_escapes` / `stops_while_reasoning` 字段（旧代码）。
- **结论：A 上的早停已解决；B 仍是旧代码，部署 B 需另约窗口（B 是外部用户常用通道）。**

### 8.4 部署与回滚

- 备份/补丁：`~/archive/patches/serve-special-token-escape-2026-10-18/`
  （`frontend.py.before`、`server.py.before`、`test_server.py.before`、
  `special-token-escape.patch`、`README.md`）。
- **快道 A 已按上面的 staged 方案部署**；普道 B 仍待：
  `supervisorctl -c ~/deploy/supervisord.conf restart strata-b`（需另约窗口，B 是外部常用通道）。
- 部署后用 `~/tools/scan_claude_early_stop.py --date <部署日>` 复查，
  期望 `A_think_only_special` / `A_tool_json_truncated` 为 0；
  `/v1/status` 的 `prompt_escapes.literals` 随真实流量增长，
  `stops_while_reasoning` 不再因 A 类场景增长。

### 8.5 附：generate.cpp 中文注释清理

`~/SSEStraBed/src/program/generate.cpp` 里未完成的中文注释（121 行注释块 + 65 处行内注释）
已清理，保留英文原注释和 ssebed 的 load-logging 代码；`stop_eos` 的英文注释已从
`Strata` 基线恢复。`strip_comments` 等价检查：清理前后 **code-only 行完全一致**。
备份/补丁：`~/archive/patches/generate-cpp-cn-cleanup-2026-10-18/`。
纯注释改动，**不需要重编/重启引擎**。

---

## 9. 一句话给下一位接手的人

**先看 prompt 里有没有 `<|endoftext|>` / `<|im_end|>` 字面量；有的话，这不是“长上下文”问题，
而是模型在 thinking/工具参数里复述了 EOS token，被引擎当成轮次结束。**

