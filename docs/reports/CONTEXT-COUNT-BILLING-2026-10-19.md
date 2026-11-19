# 上下文“计费/计数”三方口径差排查（2026-10-19）

**现象**（书稿会话 848a12ed，模型 qwen-3.8-flash-fast，快道 strata-a :8091）：
- Claude Code `/context` 显示 **157.9k/200k（79%）**，看起来还有余量；
- 上层中继回 400：**“输入超限：本次约 398,890 tokens，超过模型上下文长度 256,000 tokens”**；
- 点“继续”后本 Pod 引擎回 400：**“prompt (372066 tokens) + max tokens (1) exceeds the context (262144) … (#545)”**。
三个数字（157.9k / 398.9k / 372.1k）和两个窗口值（256,000 / 262,144）互相打架。

## 结论（一句话）
Pod 侧**计费没有错**：账单/守卫的权威口径是引擎 tokenizer 实测（372,066），确实超窗；
差距全部来自**估算层**——Claude Code 客户端把中文为主的长会话**低估 ~2.4 倍**（且跨渠道后校准冻结），
Pod 代理的上界估算器把英文/代码为主的请求**高估 ~3.3 倍**，中继用十进制 256,000 又与引擎 262,144 不一致。

## 实测证据

### 1. 同一条请求的三方数字（书稿会话，成功轮次）
留档：`~/logs/infer-segs/2026-10-19/seg-124459-7aa82.jsonl.gz`，会话 `auto:15eb3770f7f979bf`，12:49:00：

| 口径 | 数值 | 说明 |
|---|---|---|
| 请求体 | 1,075,447 chars（messages 997,900 / 工具 55 个 76,625 / text 617,277，CJK 仅 5.3%） | Anthropic→OpenAI 转换后的实际入 Pod 体 |
| 引擎实测 `prompt_tokens` | **154,796** | Qwen tokenizer 真值（≈4.0 text chars/token） |
| Pod 代理上界估算 `est_prompt_tokens` | **516,561** | 高估 **3.34×**（见 §3） |
| Claude Code `/context` 总量 | ≈157.9k | 与引擎真值接近（该轮） |

12:00→12:49 该会话 prompt_tokens 从 100,887 爬到 154,796，**引擎真值增长与 CC 校准一致**——此前没有问题。

### 2. 爆掉的三次 400
- Pod 记录：14:14:58 / 14:15:04 / 14:15:10，同一会话三次 400（`retries=1`，`model=strata-a`）。
- **12:49–14:14 该会话在 Pod 上没有任何请求记录** → 中间那段时间 CC 走的是另一个渠道
  （settings.json 的 `ANTHROPIC_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic`，
  10-18 也出现过“Claude 会话走 DeepSeek”）。快道渠道的校准值在 12:49 后不再更新。
- 14:14 “继续”把完整书稿历史一次性发给快道：CC 转录 `~/.claude/projects/-home-cloud/848a12ed-*.jsonl`
  内容约 92.4 万字符（含 7.1 万 CJK 与后续大量中文增补），引擎实测 **372,066 > 262,144** → 真实超窗。
- CC 的 `/context`（157.9k）之所以纹丝不动：**校准基于上一次该渠道响应的 usage + 新内容字符估算**，
  中文约 0.55–0.75 token/字，而 CC 按 ≈0.25 token/字估 → 中文重的内容**低估 2–3 倍**。

### 3. 两个 400 文案分别是谁写的
- **“#545 / prompt (372066) …”**：本 Pod `Strata/serve/server.py:1760-1763`（serve 层 tokenize 后直接拒绝，
  引擎从不截断）。rag-proxy 收到后按“余量 0”把 max_tokens 压到 1 重试一次（`rag-proxy/rag_proxy.py:1715-1720`，
  `retry_on_overfit`），仍 400，把 serve 的内部文案**原样透传**给了终端用户。
- **“输入超限：本次约 398,890 … 256,000”**：该文案**不在本 Pod**——rag-proxy/serve/引擎代码与全部 git 历史
  （`git log -S`）都没有。它来自更上层的中继（`api.ssemarket.cn` 175.178.90.215 或 token-plan 侧）。
  其估算 398,890 比引擎真值 372,066 高 7.2%（方向正确的上界）；但它用的窗口 **256,000（十进制）**
  ≠ 引擎 **262,144（=256×1024，`strata-iq3_xxs-a.json --max-context 262144`）**，两层阈值不对齐。

### 4. Pod 代理估算器的系统性失真（该修但不影响计费）
`rag-proxy/rag_proxy.py:106-108, 229-230`：上界估算按 `tok_per_char_other=0.70`、`cjk=1.05` 计。
注释自证“实测英文 0.19 / 代码 0.39”——上界取得过松，英文重的请求整体高估 3.3×。
危害不是超卖计费，而是 **`clamp_max_tokens`(:265) 与思考预算(:372) 按虚高 prompt 压缩**，
中文/长会话时回答被提前掐短；真超窗时又只能靠引擎 400 兜底。serve 已有精确计数接口
**`/v1/messages/count_tokens`（server.py:2668）**，守卫路径没有用它（config 注释 :102 自己写了“精确计数走 /v1/messages/count_tokens”）。

## 计费口径判定
- 本 Pod 账务 = `infer_store` 记录的引擎 usage（`prompt_tokens/completion_tokens/cached_tokens`），为真值；
  400 请求在 serve 层 tokenize 后即拒，**不占 GPU、不产生计费** → 三方里只有这个数是对的。
- “差距大”的锅在**估算显示层（CC）与守卫估算（中继/Pod 代理）**，不是计费层多收/少收。
- 待确认项（Pod 外，无法本地验证）：中继若按其“约 398,890”上界估算**记账**，需改为透传引擎 usage 真值。

## 后续要做

**A. 客户端习惯（马上可用，不动服务）**
1. 书稿会话先 `/compact`（或已落盘的文件直接开新会话续写——`~/book/` 里的成品不依赖会话历史）。
2. 中文为主的会话把 CC 显示值 **×2.4** 当引擎真值看；CC 到 ~100k 就应压缩（≈引擎 240k，留 20k 余量）。
3. 渠道来回切换后，`/context` 数不可信；以 infer 可视化页 / 中继 usage 回传为准。

**B. 中继层（api.ssemarket.cn，需协调维护人）**
1. 窗口阈值 256,000 → 与引擎对齐 262,144 − margin（或干脆取引擎 usage 真值回传做守卫）。
2. 400 错误体带**实测 token 数**；计费/守卫统一用引擎真值。

**C. 本 Pod（有效改动需重启进程 = 停服动作，等用户点头）**
1. rag-proxy 窗口守卫改**调 serve `/v1/messages/count_tokens` 精确计数**（结果可缓存），
   上界估算只做兜底；不再按 3.3× 虚高值压 max_tokens/思考预算。
2. 真超窗（prompt > 窗口）时**不再压 max_tokens=1 盲重试**（三次上游调用全部注定失败），
   直接回 413 + 实测数字；`#545` 内部文案不透传给终端用户。
3. `strata-iq3_xxs-a/b.json` 加 `"fit_max_tokens": true`：输出超余量时截断输出而非 400（只对“差一点”有效，救不了 prompt 超窗）。

---
出处索引：`~/logs/infer-segs/2026-10-19/`（seg-124459 / seg-141712 等）、`~/deploy/logs/rag-proxy.out.log`（06:15:00–06:15:15 UTC 三次超窗重试）、
`Strata/serve/server.py:1750-1765,2668`、`rag-proxy/rag_proxy.py:102-113,183-230,253-277,372,1715-1720`、
`Strata/strata-iq3_xxs-a.json`、`~/.claude/settings.json`、`~/.claude/projects/-home-cloud/848a12ed-*.jsonl`。

## 落地记录（同日下午，用户授权“本 pod 可改的请你改好”）

**已改并生效（rag-proxy 已重启，pid 3426352，2026-10-19 15:33 北京）**
- `rag-proxy/rag_proxy.py`：
  - `count_tokens()` 增加方言参数（OpenAI 请求走 serve 新路由 `/v1/chat/completions/count_tokens`）；
  - 新增 `overfit_info()`/`overfit_error()`：解析引擎超窗 400 的实测 prompt/窗口/余量，
    改写为 Anthropic 原生口径的干净错误（`prompt is too long: N tokens > M maximum` +
    `prompt_tokens`/`max_context`/`max_output_tokens` 结构化字段 + 中文 hint），**#545 内部文案不再透传**；
  - `_proxy` 400 分支：**提示词本身超窗（room<1 或 prompt+slack≥窗口）不再压 max_tokens=1 盲重试**，
    直接一次干净错误回客户端（省两个上游来回）；仅“输出超余量”仍按引擎余量重试一次；
  - `no_rag` 透传分支加**窗口预检**：上界估算说“可能装不下”→ 先向 serve 精确计数（只 tokenize 不跑模型）——
    真装不下就地 400（计数进 `window_guard_rejects`，不进引擎）；装得下但输出超余量按实测压 max_tokens；
    serve 未升级时计数 404 自动降级原样透传（兼容旧 serve）；
  - 留档修正：infer_store 仍存**引擎原始错误正文**（`raw_payload`），发给客户端的是改写后的。

**已改待重启生效（serve 进程加载时读配置/代码；重启=停服，须用户显式批准）**
- `Strata/serve/server.py`：`_count_tokens` 支持 `dialect="openai"`（与 `_openai` 同一渲染管线计数），
  路由分发加 `/v1/chat/completions/count_tokens`。
- `deploy/engines/profiles/ssebed/strata-a.json`（快道现行配置）、`Strata/strata-iq3_xxs-a.json`、
  `Strata/strata-iq3_xxs-b.json`：`"fit_max_tokens": true`——“输出超余量”由 400 改为自动截断输出。

**测试**
- `rag-proxy/test_window_guard.py` ALL PASS（6s）。修了两处与当前工作树脱节的测试脚手架：
  ① A6 的 `build_memory_block` stub 返回 4 元组（真实已返回 5 元组带检索明细）；
  ② B/C 组 `start_proxy` 从生产 `config.json` 派生配置，而生产自 10-19 起 `rag_enabled=false`，
  导致 RAG 组装用例实际跑进透传分支——测试内强制 `rag_enabled=True`（生产开关不动）。

**验证记录**
- 离线：`overfit_info`/`overfit_error` 对 #545、"leaves no room"、非窗口类 400 三种输入行为正确；
- 在线：rag-proxy 重启后 `/health` 正常、两条上游 ok、小请求 200（decode 60–100 tok/s），
  旧 serve 对新计数路由返回 404 → 预检按设计降级；
- 端到端超窗探针（300,000 个「龘」→ 引擎实测 600,052 token）：`POST :8080/v1/chat/completions` 返回
  `{"error":{"type":"invalid_request_error","message":"prompt is too long: 600052 tokens > 262136 maximum",
  "prompt_tokens":600052,"max_context":262144,"max_output_tokens":0,"hint":"提示词本身已超窗…"}}`；
  代理日志两行 `[09:11:17] 窗口预检(no_rag): 精确计数不可用，按原样透传` →
  `[09:11:18] 引擎 400 超窗且提示词本身出界（实测 prompt 600052 / 窗口 262144）：不重试`。
  **对比事故当时：1 次上游来回（当时 2 次）、客户端拿到结构化实测数（当时 #545 英文原文）。**
  “计数不可用”是预期的临时状态：openai 方言计数路由要等 strata-a 重启才在 serve 上出现。

**TODO（截至 2026-10-19 19:15 北京状态）**
1. ~~用户显式批准后重启 strata-a/strata-b~~ ✅ 18:52–18:55 用户点名批准，两通道已重启并 loaded
   （A=ssebed-0.1.38-b1 / B=legacy-0.1.38-b0；b1 是 10-18 23:59 一次内容未变的重建留下的构建号，与本次无关）；
2. ~~重启后验证~~ ✅ 全部通过：
   - `/v1/chat/completions/count_tokens` 在 8091/8092 均返回与 anthropic 方言一致的实测数（同一句话 56 token）；
   - 30 万字探针重放：代理日志一行“窗口预检(no_rag): prompt 实测 600052 + 残差 ≥ 窗口 262144，
     **本地拒绝（不进引擎、不浪费 GPU）**”——这次连那 1 次注定失败的上游来回都省了；
   - :8080 正常小请求 200，快道生成正常；
   - serve 单测 `test_server.py` 131 条过 130（唯一失败是 Vue 迁移遗留的前端资源断言 `web/app.js` vs 哈希文件名，与本次无关）；
   - `fit_max_tokens` 生效证据为“配置有效 + server.py:3333 加载 + 进程已重启”；行为级验证需要 ~230k token
     的窗口边缘请求（70s+ GPU prefill），未在现网跑，留待自然流量观察 `overfit_retries` 是否趋零。
3. **待办**：把“中继窗口对齐 262,144 + 按引擎 usage 真值计费/守卫”转给 api.ssemarket.cn 维护人（本 Pod 够不着）；
4. **待观察**：1–2 天 infer 记录里 `window_guard`/`透传(no_rag·精确计数)` 新日志的分布，以及 `window_guard_rejects` 计数。
