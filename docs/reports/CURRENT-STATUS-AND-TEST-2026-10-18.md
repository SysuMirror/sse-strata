# 当前状态与测试指南（2026-10-18 23:00 北京时间）

> 给要去实测的人：先看 §1 的状态表，再按 §2/§3 的命令测。
> 结论先说：**早停问题在快道 A 上已修复并验证；普道 B 仍是旧代码，仍会复现。**
> 引擎版本字段也只在 A 上生效。

---

## 1. 当前状态

| lane | 端口 | 早停修复 | 引擎版本字段 | 当前 `engine_version` | 说明 |
|---|---|---|---|---|---|
| **A 快道** | 8091 | ✅ 已部署并验证 | ✅ 已上线 | `ssebed-0.1.38-b0` | Claude Code 的 `*-fast` 路径；serve 新代码 |
| **B 普道** | 8092 | ❌ 仍是旧代码 | ❌ 没有字段 | `None`（无此键） | 外部用户常用；部署需另约窗口 |

服务健康（2026-10-18 23:00 检查）：

```text
strata-a      RUNNING  (新 serve 代码)
strata-b      RUNNING  (旧 serve 代码)
rag-proxy     RUNNING
/health       status=ok, upstreams {'8091':'ok','8092':'ok'}
infer_store   stored=18060, pending_upload=0, minio_ok=true
A /v1/status  loaded=true, engine_version=ssebed-0.1.38-b0,
              prompt_escapes={calls:0,literals:0}, stops_while_reasoning=0
B /v1/status  loaded=true, engine=0.1.38, engine_version=None
```

> A 在 22:45:11 重启过（部署引擎版本字段 + 早停修复），22:51:47 ready；
> 计数器从 0 重新开始，所以 `prompt_escapes` 现在是 0，不是异常。

---

## 2. 测试一：早停是否解决（A 应正常，B 应复现）

### 2.1 最小 A/B（几秒钟）

```bash
# A（已修复）：应 content 非空
python3 -I ~/tools/repro_claude_early_stop.py minimal \
  --url http://127.0.0.1:8091/v1/chat/completions

# B（旧代码控制）：应 content 0，reasoning 截断在 end-of-turn token (
python3 -I ~/tools/repro_claude_early_stop.py minimal \
  --url http://127.0.0.1:8092/v1/chat/completions
```

期望：

- A：`finish=stop`、`content` 约 188 字符；`server prompt_escapes` 的 `literals` 会增加；`stops_while_reasoning=0`。
- B：`finish=stop`、`content: 0 chars`、reasoning 尾部停在
  `... end-of-turn token (`；`prompt_escapes: None`（旧代码没有这个字段）。

### 2.2 原始 incident prompt 重放（最忠实）

```bash
# 只看组装结果，不发请求
python3 -I ~/tools/repro_claude_early_stop.py replay \
  --id 20261017-235221-665100 --dry-run

# 在 A 上重放（约 40 秒）
python3 -I ~/tools/repro_claude_early_stop.py replay \
  --id 20261017-235221-665100 \
  --max-tokens 4000 --reasoning-budget 2000
```

期望：

```text
HTTP 200（约 42 s）
finish_reason: tool_calls
usage: prompt_tokens ~50,237 (cached ~50,232), completion_tokens ~2,146
content: 0 chars; reasoning: ~6,003 chars; tool_calls: 1
--- tool 0: Bash ---（grep 查 Options 字段用法）
server prompt_escapes: ... literals 增加 | stops_while_reasoning: 0
```

判读：`finish=tool_calls` + 合法工具调用 = **正常继续任务**，不是早停。
早停的判别是 `finish=stop` + content 0 + **没有** tool_calls。

> B 是旧代码、外部常用通道；控制组只跑一次最小提示，不要重放大 prompt。

---

## 3. 测试二：引擎版本字段（只在 A 有）

### 3.1 `/v1/status`（最直接）

```bash
KEY=$(cat ~/logs/strata-api-key.txt)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8091/v1/status \
  | python3 -m json.tool | grep -A7 engine_version
```

A 期望：

```json
"engine_version": "ssebed-0.1.38-b0",
"engine_build": {
  "name": "ssebed",
  "version": "0.1.38",
  "build": 0,
  "built_at": "2026-10-17 23:17:15",
  "sha256": "3d3f3c9de7a6ca38..."
}
```

B 现在没有 `engine_version` / `engine_build`（旧代码）；看到 `None` 是正常的。

### 3.2 `/health`

```bash
curl -s http://127.0.0.1:8091/health | python3 -m json.tool | grep -A7 engine_version
```

### 3.3 对话响应（OpenAI 非流式）

```bash
KEY=$(cat ~/logs/strata-api-key.txt)
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:8091/v1/chat/completions \
  -d '{"model":"strata-a","messages":[{"role":"user","content":"Reply with the single word OK."}],
       "max_tokens":200,"reasoning_budget_tokens":100}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); \
      print("engine_version:",d.get("engine_version")); \
      print("engine_build:",d.get("engine_build")); \
      print("content:",repr(d["choices"][0]["message"].get("content")))'
```

期望：`engine_version=ssebed-0.1.38-b0`、`engine_build` 有 build=0、
`content="OK"`、`finish=stop`。

### 3.4 代理路径（Claude Code 用的那条）

```bash
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:8080/v1/chat/completions \
  -d '{"model":"qwen3.8-flash-fast","messages":[{"role":"user","content":"hi"}],"max_tokens":8}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("engine_version"), d.get("engine_build"))'
```

期望同样有字段。外部中转站 `api.ssemarket.cn` 是否会透传未知字段不确定；
以 Pod 本地的 `/v1/status` / `/v1/chat/completions` 为准。

### 3.5 流式

- OpenAI 流式：首个和最后一个 chunk 都带 `engine_version` / `engine_build`；
- Anthropic 流式：`message_start.message` 带这两个字段。

---

## 4. 测试三（可选）：构建号 b0 → b1

```bash
# 1) 看当前构建号（应为 0）
switch-engine dev status ssebed

# 2) 只编译，不停服；成功后 build-number.txt +1，写 build/BUILD.json
switch-engine dev build ssebed

# 3) 再次看构建号（应为 1）
switch-engine dev status ssebed

# 4) 在约定的停服窗口重启 A，再看 engine_version
supervisorctl -c ~/deploy/supervisord.conf restart strata-a
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8091/v1/status \
  | python3 -m json.tool | grep engine_version
# 期望：ssebed-0.1.38-b1
```

- `dev build` 本身**不碰线上**（用 CPU 16-31 编译）。
- 重启 A 会冷启动约 7 分钟，属停服操作，**必须先经用户同意**。
- 不重启的话，A 仍显示 b0；`BUILD.json` 里已经是 b1。

---

## 5. 判读表

| 现象 | 含义 |
|---|---|
| A：`finish=stop`、content 非空 | 正常回答（早停已修复） |
| A：`finish=tool_calls`、有合法 tool_calls | 正常继续任务（content 0 是工具调用形态） |
| B：`finish=stop`、content 0、reasoning 截断在 `end-of-turn token (` | B 旧代码早停复现 |
| A：`engine_version=ssebed-0.1.38-b0` | A 已加载新 serve，读到 ssebed 的初始 manifest |
| B：没有 `engine_version` | B 仍是旧 serve 代码 |
| `prompt_escapes.literals` 增长 | 特殊 token 转义在真实请求中生效 |
| `stops_while_reasoning` 增长 | 仍有“thinking 中停止”的残余（B 模式/模型行为） |

---

## 6. 当前限制与注意

1. **B 还没修复**：早停和引擎版本字段都只在 A。B 是外部用户常用通道，
   部署需要另约低峰窗口；不要在 B 上反复重放大 prompt。
2. **b0 是基线**：当前二进制建于计数器之前；下一次成功 `dev build` → b1。
3. **A 的计数器刚重置**：`prompt_escapes` 为 0 是正常的，跑一次含特殊 token 的请求就会增长。
4. **外部中转站**：`api.ssemarket.cn` 可能丢弃未知字段；判断引擎版本以 Pod 本地
   `/v1/status` 为准。
5. **测试请求是真实的**：会占用 A/B 的 GPU；不要循环压测。
6. **API key 不要外泄**：脚本只读 `~/logs/strata-api-key.txt`，不打印内容。

---

## 7. 相关文件

| 内容 | 路径 |
|---|---|
| 早停完整报告 | `docs/reports/CLAUDE-EARLY-STOP-2026-10-18.md` |
| 早停复现方法 | `docs/reports/CLAUDE-EARLY-STOP-REPRO-2026-10-18.md` |
| 引擎版本字段契约 | `docs/reports/ENGINE-VERSION-FIELD-2026-10-18.md` |
| 复现脚本 | `~/tools/repro_claude_early_stop.py` |
| 扫描脚本 | `~/tools/scan_claude_early_stop.py` |
| 切换/构建手册 | `~/SSEStraBed/切换引擎用户手册.md` §6.1 |
| 早停补丁/回滚 | `~/archive/patches/serve-special-token-escape-2026-10-18/` |
| 版本字段补丁/回滚 | `~/archive/patches/engine-version-field-2026-10-18/` |
| MinIO 备份 | `qwen-3.8-flash/fix/claude-early-stop-2026-10-18/`、`.../feature/engine-version-2026-10-18/` |
