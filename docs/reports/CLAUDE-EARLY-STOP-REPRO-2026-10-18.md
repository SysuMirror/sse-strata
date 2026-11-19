# 复现“Claude 本地模型只 thinking / 早停”（2026-10-18）

> 目标：用**一条命令**复现当时的原始请求，并在已修复的快道 A 上验证不再早停。
> 脚本：`~/tools/repro_claude_early_stop.py`（只读本地留档；唯一副作用是发一次真实模型请求）。
> 背景/证据：`docs/reports/CLAUDE-EARLY-STOP-2026-10-18.md`。

## 0. 前置条件

- 快道 A 已部署修复（8091）；普道 B 仍是旧代码（8092）。
- API key：`~/logs/strata-api-key.txt`（脚本自动读取，**不会打印**）。
- 推理留档：`~/logs/infer.db` + `~/logs/infer-segs/`。
- 不要在 B 上反复跑大请求：B 是外部用户常用通道；控制组用下面的**最小提示**即可。

## 1. 最小 A/B（几秒钟，最推荐）

### A（已修复）：应返回正文

```bash
python3 -I ~/tools/repro_claude_early_stop.py minimal \
  --url http://127.0.0.1:8091/v1/chat/completions
```

实测关键行：

```text
HTTP 200 in ~4 s
finish_reason: stop
content: ~188 chars
content has marker: False | has <|endoftext|>: False | has <|im_end|>: False
server prompt_escapes: {'calls': 5, 'literals': 11} | stops_while_reasoning: 0
```

### B（旧代码控制组）：应复现 `content=0`

```bash
python3 -I ~/tools/repro_claude_early_stop.py minimal \
  --url http://127.0.0.1:8092/v1/chat/completions
```

实测关键行：

```text
HTTP 200
finish_reason: stop
content: 0 chars
reasoning: ... /// Stop when the model emits an end-of-turn token (
server prompt_escapes: None | stops_while_reasoning: None   ← 旧代码没有这两个字段
```

> 判读：**`finish=stop` + content 0 + reasoning 截断在 `end-of-turn token (` = 早停复现**；
> A 上 content 非空 = 已修复。

## 2. 原始 incident prompt 重放（最忠实，约 40 秒）

### 2.1 先 dry-run，不发请求

```bash
python3 -I ~/tools/repro_claude_early_stop.py replay \
  --id 20261017-235221-665100 --dry-run
```

输出类似：

```text
id=20261017-235221-665100 model=strata-a messages=18 tools=54 bytes=171755
max_tokens=32000 reasoning_budget=27904
special-token literals in messages: [(16, 'tool', 2)]
dry-run: not sending
```

这条记录就是 2026-10-17 23:52:21 那次 48-token 早停的原始请求；message 16 的 tool result
里含 `<|endoftext|>` 与 `<|im_end|>`。

### 2.2 在 A 上重放（收紧输出上限，避免跑太久）

```bash
python3 -I ~/tools/repro_claude_early_stop.py replay \
  --id 20261017-235221-665100 \
  --max-tokens 4000 --reasoning-budget 2000
```

实测关键行（2026-10-18 22:2x）：

```text
HTTP 200 in ~42 s
finish_reason: tool_calls
usage: prompt_tokens 50,237 (cached 50,232), completion_tokens 2,146
content: 0 chars; reasoning: ~6,003 chars; tool_calls: 1
--- tool 0: Bash ---
args head: {"command":"cd /home/cloud/SSEStraBed/src/program && grep -n ..."}
server prompt_escapes: {'calls': 4, 'literals': 9} | stops_while_reasoning: 0
```

> `content=0` 在这里是**正常**的：模型选择了工具调用（`finish=tool_calls`），不是早停。
> 早停的判别是 `finish=stop` + content 0 + **没有** tool_calls。

### 2.3 想看下一轮？

把工具调用的 `arguments` 解析成 `{"command": ...}` 执行，把结果作为 `role=tool` 消息追加，
再调用一次同样的 `/v1/chat/completions` 即可。脚本只发一轮，不自动执行工具。

## 3. 找更多可复现的现场

```bash
# 列出某天的 thinking-only / 工具 JSON 截断样本（只读）
python3 -I ~/tools/scan_claude_early_stop.py --date 2026-10-17

# 或直接查 infer 表
python3 -I - <<'PY'
import sqlite3
con=sqlite3.connect('/home/cloud/logs/infer.db'); con.row_factory=sqlite3.Row
for r in con.execute("""select id,iso,model,lane_label,prompt_tokens,completion_tokens,
                               content_chars,reasoning_chars,finish_reason
                        from infer
                        where finish_reason='stop' and content_chars=0 and reasoning_chars>0
                        order by ts desc limit 20"""):
    print(dict(r))
PY
```

拿到 `id` 后直接：

```bash
python3 -I ~/tools/repro_claude_early_stop.py replay --id <id> --dry-run
```

## 4. 判读表

| 现象 | 含义 |
|---|---|
| `finish=stop`、content 0、reasoning 截断在 `end-of-turn token (` | 早停复现（旧代码 / 未转义） |
| `finish=tool_calls`，有合法 `tool_calls` | 正常继续任务（content 0 是工具调用的正常形态） |
| `finish=stop`，content 非空 | 正常回答 |
| A 的 `prompt_escapes.literals` 增长 | 转义在真实请求中生效 |
| A 的 `stops_while_reasoning` 增长 | 仍有“thinking 中停止”的残余（B 模式/模型行为） |

## 5. 安全与注意

- 这些是**真实模型请求**，会占用 A/B 的 GPU；不要循环压测。
- B 是旧代码、外部用户常用通道；控制组只跑一次最小提示，不要重放大 prompt。
- 不要把 `~/logs/strata-api-key.txt` 的内容贴到命令回显/文档里；脚本只读文件。
- 若要复现“修复前”的行为，也可以临时 `STRATA_ESCAPE_SPECIAL=0` 重启 A，但这会停服，
  必须经用户同意；一般用 B 做控制组就够了。
- 复现所需的原始请求已留档在 `~/logs/infer-segs/`；不要在 B 上重复发送 50k prompt。
