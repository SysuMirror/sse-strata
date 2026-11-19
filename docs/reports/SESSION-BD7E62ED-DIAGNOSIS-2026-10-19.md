# 会话 bd7e62ed 诊断：不是早停，是路径幻觉 + 误报目录被删

> 会话：`bd7e62ed-79cb-467e-a917-99c595ab27fb`  
> 时间：2026-10-18 23:14 – 10-19 00:04（北京时间）  
> 模型：`qwen-3.8-flash-fast` → `strata-a`（快道 A，已修复早停）  
> 结论：**不是早停/只 thinking；没有目录被删；正确文件完好；是模型把不存在的路径当成“曾经存在过”。**

---

## 1. 一句话

用户让模型给 **`/home/cloud/SSEStraBed/src/program/generate.cpp`** 加中文注释。
模型前半段确实在读这个文件（7862 行），后半段开始读一系列**不存在的路径**
（`/home/cloud/worker/tools/strata-engine/...`、`/home/cloud/dev/strata/...` 等），
然后把“读不到”误判成“目录在 15:44 被删除”，向用户报了警。
**这些路径在工具结果里从未成功过；`worker` 目录从未存在，谈不上被删。**

---

## 2. 证据

### 2.1 早停维度：正常

- 84 个 assistant 响应，`stop_reason`：`tool_use` 83、`end_turn` 1；
- `thinking-only` **0**；`[Your previous response had no visible output...]` 自动续写 **0**；
- 没有 `InputValidationError` 式的工具 JSON 截断（14 个 tool error 都是路径不存在/Edit 匹配不到/脚本不存在）；
- 同一窗口 infer 记录里，A 的 `prompt_escapes` / `stops_while_reasoning` 正常，早停修复未触发异常。

### 2.2 目标路径：先对后错

| 时间（北京） | 动作 | 结果 |
|---|---|---|
| 23:14:04 | 用户：`/home/cloud/SSEStraBed/src/program/generate.cpp 给这个文件添加中文注释...` | 正确目标 |
| 23:14–23:40 | 多次 Read 正确文件；`apply_cn.py --dry-run` 对正确文件 | **56 个锚点命中**（23:32:26） |
| 23:40:31 | Read `/home/cloud/dev/strata/engine/src/strata_generate.cpp` | **File does not exist** |
| 23:42:15 | Read `/home/cloud/worker/StrataEngine/src/tools/strata_generate.cpp` | **File does not exist** |
| 23:42:38 | Read `/home/cloud/work/engines/...` | **File does not exist** |
| 23:43:09 | Read `/home/cloud/engine/repo/...` | **File does not exist** |
| 23:43:37 | Read `/home/cloud/dev/strata/engine/...` | **File does not exist** |
| 23:44:47 | `apply_cn.py --src /home/cloud/worker/tools/strata-engine/...` | **FileNotFoundError** |
| 23:49:39 / 23:50:36 | 再 Read `/home/cloud/worker/tools/strata-engine/...` | **File does not exist** |
| 23:58:02 | 模型报告“`/home/cloud/worker` 整个不见了，15:44 被删除” | **误报** |
| 23:58:21 | 用户选择“你先确认目录情况” | 模型继续只读排查 |
| 00:01–00:04 | 模型做最后确认，会话结束 | 没有对源码写入 |

> 关键：模型说“15:31–15:40 我多次成功读取该文件（worker 路径）”，但工具结果里
> **worker 路径的每一次 Read 都是 `File does not exist`**。成功的是
> `SSEStraBed/src/program/generate.cpp`，不是 worker 路径。模型把两者混淆了。

### 2.3 正确文件现状：完好

```text
/home/cloud/SSEStraBed/src/program/generate.cpp
7862 行，514,308 字节，mtime 2026-10-18 06:13
中文行数：0
```

会话没有修改它；模型只写了草稿：
`~/tools_dev/comment_check/gen1/`（s01…s06、s06a、s06b，约 91 KB）。
还写了一条 memory：`no-subagent-for-edits.md`，并编辑了 `MEMORY.md`（这是真实改动，内容有效）。

### 2.4 草稿的可复用性：部分可救

- 归一化后 380 条 spec（after/before/append）；
- 对正确文件 dry-run：**254 个锚点命中**，**125 个报错**（未命中/命中多次），
  主要集中在 `s04_tiers.json`（23）、`s06_serve.json`（66）、`s06b.json`（24）；
- 模型自己的 `apply_cn.py` 还有一个 schema bug：部分 `after` 条目用了
  `{"match":..., "append":...}`，脚本却要求 `"lines"`，会 `KeyError: 'lines'`；
  归一化后可以 dry-run，但 125 条仍需人工/模型复核。

### 2.5 重复工具调用 + 无总结（用户补充的问题）

会话后半段共 **54 次 Bash**，大量是同一类只读排查的变体：

| 关键词 | 出现次数 |
|---|---:|
| `strata_generate` | 11 |
| `find /` | 9 |
| `/home/cloud/worker` | 7 |
| `ls /home/cloud` | 6 |
| `mount` | 4 |
| `git -C /home/cloud` | 4 |
| `/proc/mounts` | 2 |

最后两轮的关键证据（两个**不同**的 message id，说明是模型真的生成了两遍，不是转录重复）：

```text
16:00:42  msg_mve0i14q  stop=tool_use
  thinking: "Confirmed: the directory is gone, no copy, no mount, no tool record..."
  text:     "Confirmed gone. Checking two real recovery avenues before I report."
  tool:     Bash（查编辑器本地历史 / 远端 ls-remote）
16:01:06  tool_result 返回
16:01:19  msg_mve0iuao  stop=end_turn
  thinking: 与上一条**逐字相同**
  text:     "Confirmed gone. Checking two real recovery avenues before I report."（逐字相同）
  tool:     无
```

- 第二轮把上一轮的 thinking + 计划句**原样重复**了一遍；
- 然后 `stop_reason=end_turn`，**没有执行承诺的检查，也没有给出总结**；
- 上下文 87.9k / 200k（44%），没有 `max_tokens`、没有 API error、没有 thinking-only；
- 所以不是引擎/网关截断，是模型自己的**计划-执行断裂 + 退化重复**：说要“检查两条恢复途径再报告”，
  结果既没调工具也没报告，直接结束了这一轮。

> 这与之前归一化里看到的“Let me run… 然后 EOS”属于同一类行为（计划后停止），
> 只是这次有可见正文，所以不是 thinking-only；但同样没有交付总结。

### 2.6 原始推理留档核对：不是客户端重试/截断

`~/logs/infer.db` 里找到了这两轮的真实请求/响应：

| id（北京时间） | messages | prompt | completion | content | reasoning | tool_calls | finish |
|---|---:|---:|---:|---:|---:|---:|---|
| `20261019-000029-de091a`（00:00:29） | 98 | 86,472 | 1,236 | 67 字符 | 2,855 字符 | **1** | `tool_calls` |
| `20261019-000107-5c6c32`（00:01:07） | **101** | **87,890** | 740 | **67 字符** | 2,818 字符 | **0** | `stop` |

第二轮的 101 条 messages 里**确实包含了**：

- 上一条 Bash 的 tool result（编辑器历史、远端 ls-remote、repo 无 `src/tools/` 等）；
- 上一条 assistant 文本 “Confirmed gone. Checking two real recovery avenues before I report.”；
- 以及客户端注入的 `<total_tokens>` 提醒（还出现了两条重复的）。

也就是说：

1. **不是客户端把同一请求重试了一遍**：第二轮 prompt 从 86,472 涨到 87,890，
   多了 tool result + 上一条 assistant 文本，是一个新的 turn。
2. **不是 serve/网关截断**：原始 `response.content` 就是 67 字符的计划句，
   `reasoning` 2,818 字符，`finish=stop`，`max_tokens=32000`，completion 只有 740；
   没有隐藏的总结被丢掉。
3. **模型拿到了新工具结果，却没有使用它**：它把第一轮的 thinking 和计划句又生成了一遍，
   然后正常 EOS。这是模型侧的重复/计划-执行断裂。

> 需要说明的边界：**能 100% 确认的是“不是技术截断/不是客户端重试”**；
> “模型内部为什么重复”只能推断（长上下文跟踪、错误前提、上一轮 assistant 文本的复读倾向），
> 不能从黑盒里直接证明。但现有证据已经足以把引擎/serve 排除。

---

## 3. 根因判断

1. **不是推理引擎/早停问题**：A 的 serve 是修复后的代码，本次会话 0 thinking-only、0 自动续写。
2. **是模型路径幻觉**：模型在 `cd` 到草稿目录、上下文变长后，开始“猜”源文件路径，
   把 `dev/strata`、`worker/StrataEngine`、`work/engines`、`engine/repo` 等不存在的路径
   当成候选；每次 `File does not exist` 都没有纠正它的假设，反而被解释成“文件被删”。
3. **误报目录被删**：`worker` 路径从未成功读取过；`/home/cloud` 的 mtime 变化不足以证明删除。
   没有 `rm`、没有源码写入、没有数据丢失。
4. **草稿工具 schema 不匹配**：模型把 `append` 条目放进了 `after` 列表，导致脚本 KeyError；
   s04/s06 的锚点大量不匹配，说明这些 spec 不是基于正确文件写的（或基于错误的文件状态）。
5. **重复工具调用 + 计划-执行断裂**：错误前提（目录被删）没有停止条件，模型连续做 54 次
   只读 Bash（同类关键词重复）；最后一轮把上一轮的 thinking/计划句**逐字重复**，然后
   `end_turn`，既没执行承诺的检查也没总结。上下文 44%、无 `max_tokens`/API error，
   所以不是截断，是模型自己的重复/终止行为。
6. **原始留档进一步排除技术原因**：第二轮请求 101 条 messages、prompt 87,890，
   已包含上一轮 Bash 的 tool result；原始 response 的 content 就是 67 字符计划句、
   reasoning 2,818 字符、`finish=stop`、无 tool_calls。不是客户端重试，也不是 serve 截断。
   客户端的 `<total_tokens>` 提醒出现了两条重复、上一轮 assistant 计划句也在上下文里，
   可能会加剧模型的“复读”倾向，但这是 harness/模型侧，不是引擎。

---

## 4. 建议

1. **不要再按“worker 目录被删”去恢复**：不存在这个目录，恢复是空操作。
2. 如果要继续给 `generate.cpp` 加注释：
   - 目标固定为 `/home/cloud/SSEStraBed/src/program/generate.cpp`；
   - 先 `ls -la`/`realpath` 验证绝对路径，再 Read/Edit；
   - 用归一化后的 spec 先 dry-run，只保留 254 个命中锚点，125 个错误逐条复核；
   - 或直接重做一轮小批量注释，不沿用 s04/s06 的草稿。
3. **防复发**：建议给 agent 加一条规则：
   “使用绝对路径前先 `ls`/`realpath` 验证；不要把从未成功读取过的路径当成‘曾经存在’；
   报告目录被删前，必须给出该路径此前成功读取的工具结果。”
4. **模型侧**：这是模型行为（路径幻觉/上下文跟踪/重复终止）问题，serve 的特殊 token 修复不覆盖；
   如果频繁出现，考虑缩短会话、在关键节点重新给出绝对路径、或换更强模型做长文件任务。
5. **加“停止条件 + 总结”规则**：告诉 agent：
   - 同一类只读检查连续两次没有新信息 → 停止排查，先给总结；
   - 说“我接下来要检查 X”时必须真的调用工具；如果这一轮不调用，就直接交付已知结论；
   - 每轮结束前给一句“当前结论/下一步”，不要用计划句代替总结。
6. **harness 侧可做的两件小事**：
   - 去掉重复的 `<total_tokens>` 提醒（本次请求里出现了两条一样的）；
   - 检测“连续同类 tool 调用 / 计划句后无 tool call”，注入一条“先总结”的提醒；
     不改引擎，只在代理或客户端做（需要重启代理时再走停服确认）。

---

## 5. 复现/核对命令

```bash
# 正确文件完好
ls -la /home/cloud/SSEStraBed/src/program/generate.cpp
wc -l /home/cloud/SSEStraBed/src/program/generate.cpp
grep -cP '[\x{4e00}-\x{9fff}]' /home/cloud/SSEStraBed/src/program/generate.cpp   # 0

# worker 路径不存在（会话里每次 Read 也是这个结果）
ls -ld /home/cloud/worker 2>&1

# 草稿与 dry-run（只读）
ls -la /home/cloud/tools_dev/comment_check/gen1/
python3 /home/cloud/tools_dev/comment_check/apply_cn.py \
  /tmp/gen1_norm/s01_head.json /tmp/gen1_norm/s02_args.json /tmp/gen1_norm/s03_setup.json \
  /tmp/gen1_norm/s04_tiers.json /tmp/gen1_norm/s05_tokenpath.json \
  /tmp/gen1_norm/s06_serve.json /tmp/gen1_norm/s06b.json \
  /home/cloud/SSEStraBed/src/program/generate.cpp --dry-run
```

> `/tmp/gen1_norm/` 是对原 spec 做 `append` 归一化后的临时副本（未改原文件）。
> 原 spec 在 `~/tools_dev/comment_check/gen1/`，未改动。
