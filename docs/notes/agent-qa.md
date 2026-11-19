# Agent 问答汇总

> 规则见 `~/.claude/CLAUDE.md`「铁律：用户问过的信息要汇总记录」。
> 只记可复用的结论与出处；不记密钥、不粘贴整段对话。时间均为北京时间。

---

## 2026-10-17 20:11 — 切换引擎与普道/快道的关系

**问题**：切换引擎和普道/快道是什么关系？

**结论**：
- 普道/快道是两条固定 lane：A=快道 8091/GPU0/`strata-a`，B=普道 8092/GPU1/`strata-b`。
- 切换引擎不改变 lane 身份，只换 lane 背后的实现（legacy ↔ ssebed）。
- 有四种组合：A/B 都 legacy、A 新 B 旧、A 旧 B 新、A/B 都新。
- 相关：`deploy/switch_engine.py`、`deploy/README-switch.md`、`切换引擎用户手册.md`。

---

## 2026-10-17 20:11 — 改引擎后的编译、测试与可选切 lane

**问题**：改完推理引擎代码后，怎么方便地编译、测试，并可选把新引擎切到 A 或 B 跑？

**结论**：
- `switch-engine dev build`：只编译 `~/SSEStraBed/build/strata`，不影响线上。
- `switch-engine dev test`：只跑 C++ CPU 回归（ctest），不影响线上。
- `switch-engine dev run --lane b|a|both --yes`：只切指定 lane，另一条继续服务。
- `switch-engine dev rollback --lane b|a --yes`：回滚指定 lane。
- `switch-engine dev cycle --lane b --yes`：build + CPU 测试 + 切 B。
- `switch-engine dev status`：看构建 hash/mtime/stale 与 lane 当前引擎。
- 实测：`dev build` 成功；`dev test` 10/10 CTest 通过；未执行真实切换。

---

## 2026-10-17 20:11 — SSEStraBed 只保留引擎

**问题**：SSEStraBed 里有空目录和重复文档，只想要 Strata 引擎。

**结论**：
- 现在只保留：`src/`、`include/`、`tests/`、`third_party/`、`cmake/`、`CMakeLists.txt`、`LICENSE`、
  `tools/` 的 3 个测试 fixture 脚本、`deploy/`、`build/`、根目录说明与手册。
- 已删除：`serve/`、`docs/`、`bench/`、`ref/`、`data/`、`sse-deploy/`、`setup.py`、Dockerfile、脚本等。
- `ssebed` profile 只替换 C++ 二进制；Python 服务层仍用 `~/Strata/serve/server.py`，
  数据仍用 `~/Strata/data` 与 `~/Strata-data`。

---

## 2026-10-17 20:11 — 引擎入口与阅读路线

**问题**：引擎入口在哪里，怎么读？

**结论**：
- 唯一入口：`SSEStraBed/src/program/generate.cpp:1076` `main()`。
- 不要从第一行读；先读 `--tokens` 一次性路径，再读 `--serve` 路径。
- 一次性路径：`main` → `WeightTable::load`（`src/core/weights.cpp:144`）→ `session_init`
  （`src/core/session.cpp:75`）→ `session_loop`（`session.cpp:568`）/ `session_run_token`（`:903`）
  → `block_layer_pre`（`layer.cpp:1142`）/ `block_layer_post`（`:1291`）→ `lm_head` / `sample_tokens`。
- 生产路径：`if (o.serve)`（`generate.cpp:4179`）→ `READY`（`:5213`）→ serve loop（`:5640`）
  → `GEN` 解析（`:5734`）→ `Prefill::run`（`prefill.cpp:1304`）→ decode（`:6480`）
  → `Verifier::run`（`verify.cpp:1139`）→ `T`（`:6548`）→ `DONE`（`:6733`）。
- 用户打开的 `include/strata/plan/plan.hpp` 是内存规划器，不在 token 前向路径上。

---

## 2026-10-17 20:11 — Python server 与 C++ engine 的分工

**问题**：为什么是 Python server + C++ 引擎？前者如何使用后者？

**结论**：
- Python 负责 HTTP/SSE、OpenAI/Anthropic 协议、tokenizer、chat template、工具调用、
  鉴权、排队、监控、Web UI。
- C++ 负责 CUDA kernel、量化、专家三级存储、prefill/decode、KV、CUDA Graph、MTP、采样。
- 两者通过 `Popen(stdin/stdout=PIPE)` 连接，协议是一行一条命令：
  `GEN/BGEN/BSTOP/STOP/QUIT` → `INFO/READY/PP/T/DONE/ERR`（batch 另有 `BADM/BT/BDONE`）。
- 传输的是 **token ids**，不是文本；Python 侧编码/解码，C++ 只做数学。
- stdout 只跑协议，日志全部走 stderr。
- 代码：`~/Strata/serve/server.py:307`（`StrataEngine`）、`:350`（`Popen`）、`:549`（`_control`）、
  `:636`（`generate_batched`）；引擎侧 `generate.cpp:4179/5213/5640/5734/6480/6548/6733`。

---

## 2026-10-17 20:11 — C++ 引擎是否常驻

**问题**：C++ 引擎是常驻运行的吗？

**结论**：
- **是。** 默认 eager：Python server 启动时 spawn 一个 C++ 进程（`--serve`），
  加载模型/专家后常驻，循环读 stdin；每个请求只发一行命令，不重新加载模型。
- 常驻状态包括：dense 权重、专家 arena/VRAM cache、KV/state、MTP、prompt cache/停车快照。
- 退出时机：`QUIT`（正常关闭/`POST /unload`）、崩溃、OOM、Python server 死亡。
- `--lazy` 可选：server 先不启引擎，首个生成请求再加载。
- 引擎是 Python server 的子进程；supervisor 管的是 Python server，不是引擎本身。
- 两条 lane = 两个常驻引擎，各占一张 GPU。

---

## 2026-10-17 20:20 — 重启/切换引擎的具体机制

**问题**：重启切换引擎是怎么弄的？

**结论**：
- 不是热切换，是**冷重启**：停掉 supervisor 的 `strata-{a,b}`（Python server + 其子进程 C++ 引擎），
  改写 `~/deploy/engines/active-{a,b}.env`，再 `supervisorctl start`。
- 分发器 `~/Strata/run-iq3_xxs-{a,b}.sh` 读 env，决定用哪个 `server.py` + config；
  config 里的 `exe` 决定用哪个 C++ 二进制。
- 停止走 TERM + `stopasgroup/killasgroup`；server 关闭时给引擎发 `QUIT`；supervisor `stopwaitsecs=30`。
- 启动后脚本等 `/health` + `/v1/status.loaded` + 1-token 冒烟；失败自动恢复 `.prev` 并重启旧引擎。
- 停机时间 = 停进程 + 模型/专家重载（30s–3min）；一次只切一条 lane。
- 不修改 supervisor.conf；不修改代理 upstreams。

---

## 2026-10-17 20:18 — dev 命令改为显式指定引擎名

**问题**：`switch-engine dev run --lane b` 没写引擎名；已经给两个引擎命名了，应该能指定。

**结论**：
- `dev` 子命令现在接受显式引擎名（`legacy` / `ssebed`）：
  - `switch-engine dev build ssebed`
  - `switch-engine dev test ssebed`
  - `switch-engine dev status ssebed`
  - `switch-engine dev run ssebed --lane b --yes`
  - `switch-engine dev run legacy --lane b --yes`（显式切回原引擎）
  - `switch-engine dev cycle ssebed --lane b --yes`
- `dev rollback --lane b --yes` 不需要引擎名：它恢复 `active-b.env.prev`。
- 省略引擎名时默认仍是 `ssebed`（兼容旧命令），但文档统一写成显式形式。
- 代码：`~/SSEStraBed/deploy/switch_engine.py` 的 `dev_profile(name)` 与 `dev` 子命令参数。

---

## 2026-10-17 20:25 — 切换成功看什么？为什么会一直出日志？

**问题**：显示什么才算运行成功？为什么日志一直刷？

**结论**：
- `dev build` 成功：最后一行 `build ok: /home/cloud/SSEStraBed/build/strata`，退出码 0。
- `dev test` 成功：`100% tests passed out of N`，退出码 0。
- `dev run` 成功：最后出现 `lane B switched to ssebed`，随后 `proxy health: ok ...`，退出码 0。
- `dev rollback` 成功：`lane B rolled back`。
- `status` 正常：`RUNNING` + `health: loaded=True`。
- `dev status` 正常：`stale: False`，binary sha/mtime 是新的。
- 一直出日志的两种正常情况：
  1. `dev run` 等待引擎加载时，每 15 秒打印 `waiting lane 8092: ...`（最多 360 秒）；
  2. 引擎是常驻服务，`~/SSEStraBed/logs/strata-iq3_xxs-b.log` 会按请求/prefill chunk 持续写日志；
     如果 `tail -f`，就会一直刷。这不是切换脚本在刷。
- 判断成功不要只看日志滚动，要看**最后一行 + 退出码 + `status`/`dev status`**。

---

## 2026-10-17 20:27 — 加载日志：打印权重去了哪张卡/CPU

**问题**：能否简单修改加载 log，通用打印什么权重加载到哪张卡或 CPU？

**结论**：
- `SSEStraBed/src/program/generate.cpp` 新增 `log_weight_placement()`，在专家缓存/远端/peer 设置完成后打印
  与模型家族无关的摘要：
  - dense 权重按类别（attention / GDN / router / shared expert / hc / PLE / output / embedding）汇总
    tensors、MiB、VRAM arena vs native/host；
  - routed experts：层数 × 专家数、总 GiB、来源（pinned RAM arena 或 mmap 文件）；
  - VRAM expert cache：设备、槽数、GiB；
  - CPU expert pool workers；
  - PLE 表行数与 IO 模式；
  - MTP draft layer 路径与设备。
- 开关：`STRATA_LOG_LOAD=0` 关闭，默认 1 摘要，2 每张量，3 每层专家 blob；输出走 stderr（协议不受影响）。
- 已用 `switch-engine dev build ssebed` 编译通过；只在 SSEStraBed 引擎里，legacy 没有。
- 文档：`切换引擎用户手册.md` §7、`README-SSEStraBed.md` §5.5。

---

## 2026-10-17 20:35 — lane A 切 ssebed 一直 Connection refused 的原因

**问题**：执行 `switch-engine dev run ssebed --lane a --yes` 后，脚本一直
`waiting lane 8091: Connection refused`，是不是失败了？

**结论**：
- **不是失败，是冷启动加载中。** Python server 要等引擎 `READY` 后才监听 8091；
  加载期间 `curl 8091` 必然是 connection refused。
- 实测：引擎进程状态 `D`，`wchan=folio_wait_bit_common`，`read_bytes` 从 7.7GB
  涨到 24.8GB（约 120MB/s），从 `/shared` 读 IQ3_XXS 专家到 RAM；总耗时约 **6 分钟**。
- 20:34:33 `lane 8091 ready`，20:34:34 冒烟通过，`lane A switched to ssebed`，
  proxy health 两条 lane 都 ok。**切换最终成功。**
- 脚本默认健康等待原来是 360s，几乎不够；已改为 **900s**（可用 `--timeout 1200` 更长）。
- lane B 仍是 legacy；只有 A 切到了 ssebed。
- 引擎日志里已经能看到 `strata load placement` 摘要；native（GGUF 直供）张量之前显示 0.0 MiB，
  已改为从 `src_bytes` 取大小/标注“size logged separately”，下次重启生效。
- PLE `mlock failed` 是本 Pod 的 8MB memlock 限制，正常回退到页缓存，不是失败原因。

---

## 2026-10-17 20:36 — 首次切换后的 rollback 与超时修复

**结论**：
- `dev rollback --lane a` 在**第一次切换**（没有 `active-a.env.prev`）时，现在会删除 `active-a.env`，
  让 dispatcher 回到 legacy fallback，而不是直接跳过。
- 切换脚本默认健康等待时间由 360s 提高到 **900s**；冷启动实测约 6 分钟，必要时用 `--timeout 1200`。
- 文档已同步：`切换引擎用户手册.md` §5.4、Q8。

---

## 2026-10-17 20:41 — 加载过程没有逐段打印

**问题**：虽然加了加载位置摘要，但加载过程中没有“一点一点”的进度，只有 `still starting (N s)`。

**结论**：
- 已在 `ArenaExpertSource` 的专家 arena 读取路径加进度：
  - 开始前：`expert arena load: 48 layers x 512 experts, 39.97 GiB from ...`
  - 读取中：每 2 层一行 `expert arena 2/48 layers, 1.7/40.0 GiB, 14.2 s (0.12 GiB/s)`
  - `load_experts_gguf`（native GGUF）和 `load_experts_ranges`（experts.bin）都加了。
- 该进度只影响 stderr 日志，不影响协议/性能。
- 已编译进新的 `SSEStraBed/build/strata`；**当前 lane A 跑的还是加进度前的二进制**，
  下次重启/切换才生效。
- 文档：`切换引擎用户手册.md` §7、`README-SSEStraBed.md` §5.5。

---

## 2026-10-17 20:49 — 重启后仍看不到加载进度日志

**问题**：用 20:44 新编译的二进制重启 A 后，终端还是只有 `waiting lane`，没有逐层日志。

**结论**：
- 逐层进度**已经打印了**，但在**引擎日志文件**里，不在切换脚本终端：
  `~/SSEStraBed/logs/strata-iq3_xxs-a.log`；`grep 'expert arena'` 能看到 1/48…48/48。
- 切换脚本只打印 `[engine]` 心跳之外的自己的日志；引擎 stderr 被 server.py 重定向到配置文件
  的 `log` 路径，不会自动出现在终端。
- 已修：切换脚本的 `wait_health` 现在会**跟随引擎日志**，把新行以 `[engine] ...` 打到终端；
  下次切换就能直接看到。
- 另修了一个计数 bug：`lay.bytes[l]` 是**单个专家 blob**，不是该层总字节，之前进度条一直是
  `0.0/40.0 GiB`；已乘 `n_expert`，下次重启显示正确的 GiB 与 GiB/s。
- 本次 A 实际加载 262 秒（页缓存已热），日志完整；0.00 GiB/s 只是计数 bug，不是没加载。

---

## 2026-10-17 21:15 — 前端对话框能否选择引擎

**问题**：ai.ssemarket.cn 当前的前端对话框，能不能选“用哪个引擎”？

**结论**：
- 原先不能：`ChatView` 固定用 `app.health.model`；而代理的 `/health` 没有 `model` 字段，
  所以请求落到 `default_upstream` = 8092（普道 B / legacy）。
- 已实现一个前端选择器（源码已改，构建到 `/tmp/strata-web-build-engine-selector`，**尚未发布**）：
  - 普道 B：model=`qwen3.8-flash` → 网关 8092
  - 快道 A：model=`qwen3.8-flash-fast` → 网关 8091
  - 选择存 localStorage；对话、建议问题、可视化里的真实问答都跟着选中的 model 走。
- 网关 `model_routes` 已登记这两个名字，**不需要改代理/重启引擎**；只需发布静态前端。
- 发布命令：`cd ~/Strata/serve/webapp && bash tools/deploy.sh`（index.html 最后写，无引擎重启）。
- 当前 A=ssebed、B=legacy；所以选“快道 A”就是用新引擎。
- 局限：选择器显示的是 lane（快道 A / 普道 B），不是引擎名 legacy/ssebed；
  网关不暴露 lane 背后的引擎名，需要额外小接口才能显示。

---

## 2026-10-17 21:15 — 前端引擎选择器已发布

**结论**：
- 已执行 `bash ~/Strata/serve/webapp/tools/deploy.sh` 发布到 `~/Strata/serve/web`；
  公网 `https://ai.ssemarket.cn/` 返回新 `index.html` 与 `app-DQXRpWK_.js`（均 200）。
- 选择器已进入构建产物：`qwen3.8-flash-fast`、`engine-select` 均在线上 JS 里。
- 实测通过网关请求：
  - `model=qwen3.8-flash` → `/concurrency` 记录 lane `http://127.0.0.1:8092`（普道 B / legacy）
  - `model=qwen3.8-flash-fast` → lane `http://127.0.0.1:8091`（快道 A / ssebed）
- 发布只改静态文件，**没有重启引擎**；8091/8092 仍 healthy。
- 浏览器需要强刷一次加载新 hash 资源。

---

## 2026-10-17 21:30 — 前端支持并行对话

**问题**：对话框请支持并行。

**结论**：
- 已把 `busy` 从 ChatView 全局状态改为**每个会话自己的 `chat.busy`**：
  - 多个会话可以同时流式生成，切换会话不会中断另一个；
  - 每个请求仍使用发送时选中的引擎（A/B）；
  - 侧栏给正在生成的会话加了一个脉冲小圆点；
  - `newChat` 不再因为旧会话在生成而拒绝；“新对话”可以在旧回答继续跑时创建；
  - 离开页面时 abort 所有会话的流，不留孤儿请求；
  - 持久化时剔除 `busy`（含 AbortController），避免写进 localStorage。
- 构建到 `/tmp/strata-web-build-parallel`；`tools/smoke.mjs` 全部功能检查通过
  （无 JS 报错、流式/工具/登录/监控等 PASS），仅 1 条既有的手机端视口断言
  `app=390 vs viewport=480` 失败，与本次改动无关。
- **尚未发布到线上**，等待确认。
- 注意：并行是“多个会话”并行；同一条会话里仍然一次只跑一个回答。
  后端每 lane `--batch 4`，两条 lane 合计最多约 8 路并发；prefill 仍按引擎现有调度串行。

---

## 2026-10-17 21:32 — 并行对话前端已发布

**结论**：
- 执行 `bash ~/Strata/serve/webapp/tools/deploy.sh`，线上 `index.html` 引用
  `app-CXUPqADB.js`；公网 `https://ai.ssemarket.cn/web/app-CXUPqADB.js` 返回 200。
- 产物包含并行标记 `side__busy` / `chat.streaming`，以及引擎选择 `qwen3.8-flash-fast`。
- 8091/8092 的 `/` 都返回新页面；两条 lane healthy；**未重启引擎**。
- 用法：多个会话可以各自生成；侧栏生成中的会话有脉冲点；同一条会话仍一次只跑一个回答。
- 浏览器需强刷一次加载新 hash 资源。

---

## 2026-10-18 11:47 — Claude 早停 / 只 thinking 的初步定位

**问题**：昨天到今天用本机 Claude Code 调本地模型（经 `api.ssemarket.cn` 中转）时，模型只输出 thinking、
不输出正文；这是不是之前就存在的问题？原因是什么？

**结论**：
- 是之前就存在的问题（旧文档问题 #4/#5），但 2026-10-17 晚的集中爆发有一个明确的**新根因**：
  **特殊 token 碰撞**。prompt / tool result 里含字面量 `<|endoftext|>` / `<|im_end|>`
  （来自 `generate.cpp:394/514` 的注释），模型在 thinking 或工具参数里复述时真的生成了
  EOS token（248044/248046）；引擎按 `eos_ids` 当轮次结束，于是 thinking 截断、正文为空；
  若正在写工具 JSON，则调用被截断，客户端报 `InputValidationError`。
- 统计（2026-10-17 快道 strata-a，同日对照）：含特殊 token 的 58 次请求中，thinking-only 16 次、
  工具 JSON 坏 5 次；不含特殊 token 的 295 次中，thinking-only **0** 次、坏 JSON 1 次。
  16/16 thinking-only 全部落在含特殊 token 的组。
- 最小复现（已实测，只打普道 8092）：保留 `<|endoftext|>`/`<|im_end|>` → `content=None`、
  reasoning 截断在 `... end-of-turn token (`；删掉这两个字面量 → 正常 176 tokens 正文。
- 受影响会话（北京时间）：`f04d9927`（23:21–23:47，10 次 thinking-only、7 次自动续写、3 次 Edit
  JSON 错误）、`2f0111be`（23:50–23:52，2 次）、`fc0c219f` 的子代理（23:00–23:13，3 次大 thinking）、
  `19877945` 的子代理（22:10，1 次）。10-18 的 Claude 会话走 DeepSeek，没有新的本地模型现场。
- 另有一类少数残余模式：prompt 无特殊 token，模型“计划完（Let me run…）就 EOS”；
  普道 2/1383，历史 10-07/08/14 出现过；尚未根因。
- 证据与复现索引：`docs/reports/CLAUDE-EARLY-STOP-2026-10-18.md`；
  逐请求表 `docs/reports/artifacts/claude-stop-evidence-2026-10-18.csv`；
  原始留档 `~/logs/infer.db` + `~/logs/infer-segs/2026-10-17/`。
- 代码出处：`Strata/src/program/generate.cpp:394-396`（eos_ids）、
  `Strata/serve/server.py:60,1277-1279,1630-1634`（stop_ids / parse_special）、
  `Strata/serve/frontend.py:560-575`（不完整工具调用被丢弃，#211）、
  `rag-proxy/rag_proxy.py:264-314`（thinking_guard）。
- 已更新 `Claude_Session_Inspector`：新增 `thinking_only_end_turn` 检测与
  `assistant_thinking_only_count`，并补上 `--output` / 子代理转录。
- 与 10-17 20:34 的引擎切换（A=ssebed）**无关**：最小复现在普道 B/8092（legacy）上同样复现；
  `Strata` 与 `SSEStraBed` 的 `generate.cpp` 非注释行无差异；根因在共享 serve 层 + 模型词表。
- **未做任何停服动作**；修复方向里需要重启网关或 serve 的，必须先经用户同意。

---

## 2026-10-18 14:13 — 根治早停：serve 层转义已实现；generate.cpp 中文注释已清理

**问题**：早停有没有可能是推理引擎的问题？如果是，可以改 `~/SSEStraBed`。
另外 `generate.cpp` 的中文注释没写完、混合状态不好，先清理掉。

**结论**：
- **不是引擎 bug**：引擎的 `eos_ids`（`generate.cpp:394-396`）和 serve 的 `stop_ids`
  （`server.py:1277-1279`）语义正确；模型确实生成了 EOS token。最小复现在普道 B/8092
  （legacy 引擎）上同样成立，所以也与 10-17 切到 ssebed 无关。**不需要改引擎 C++**。
- **根治在 serve 层**（已实现，未部署）：
  - `Strata/serve/frontend.py` 新增控制 token 提取/转义：在用户内容的 `<|im_end|>` 等
    type-3 CONTROL 字面量第一个字符后插入 `U+2063`，tokenizer 不再把它当 248044/248046；
  - `Strata/serve/server.py`：`prepare` + `count_tokens` 前转义；`Detokenizer` 输出剥离
    `U+2063`，客户端仍看到原始字面量；`/metrics`/`/v1/status` 加
    `prompt_escapes` / `stops_while_reasoning`；`STRATA_ESCAPE_SPECIAL=0` 可关闭。
  - 覆盖 message content / reasoning_content / tool_calls arguments / tool description+parameters；
    不碰图片 source、名字/ID/dict key。
- 验证：新增 7 个 `SpecialTokenEscape` 测试全过；全量 `serve.test_server` **124/125**
  （唯一失败是既有的 `WebApp.test_page_and_files`，线上 index 已换 `app-CXUPqADB.js`，
  与本次无关）；真实 pack tokenizer：未转义 248044×1/248046×3 → 转义后 0/2（模板 marker 保留）；
  真实模型 A/B：未转义 `content=None`（复现），转义后正常 206 tokens 正文。
- **尚未部署**：serve 进程是引擎父进程，重启 `strata-a`/`strata-b` 会连带重启引擎，
  属停服操作，等用户确认窗口。备份/补丁/步骤：
  `~/archive/patches/serve-special-token-escape-2026-10-18/`（含 README、before 文件和 patch）。
- **generate.cpp 中文注释已清理**：`~/SSEStraBed/src/program/generate.cpp` 移除
  121 行未完成中文注释块 + 65 处行内注释；恢复 `stop_eos` 的英文原注释；保留 ssebed
  load-logging 代码。`strip_comments` 等价检查：清理前后 code-only 行完全一致。
  备份/补丁：`~/archive/patches/generate-cpp-cn-cleanup-2026-10-18/`。纯注释改动，
  **不需要重编/重启引擎**。
- 残余 B 模式（无特殊 token、计划完即 EOS）仍未根因；本次只加 `stops_while_reasoning`
  观测，另立问题。

---

## 2026-10-18 14:24 — 快道 A 已部署根治补丁并验证

**结论**：
- 14:15:36 重启 `strata-a`；14:22:51 ready（约 7 分 15 秒冷启动）；**普道 B 未动、期间保持在线**。
- 直接打 8091，原始 incident prompt（含 `<|endoftext|>` / `<|im_end|>` 字面量）：
  HTTP 200、4.3 s、completion 219、`finish=stop`、**content 非空**；
  `prompt_escapes={"calls":1,"literals":2}`、`stops_while_reasoning=0`。
- 走完整代理路径（`model=qwen3.8-flash-fast` → 8091，等同 Claude Code 通道）：
  HTTP 200、2.7 s、completion 225、**content 非空**、`cached_tokens=108`；
  `prompt_escapes={"calls":2,"literals":4}`、`stops_while_reasoning=0`。
- 原样复述场景：HTTP 200、4.9 s、completion 369；`content` 与原始行完全一致
  （含 `<|endoftext|>` / `<|im_end|>` 文本），无 U+2063 残留；
  `prompt_escapes={"calls":3,"literals":6}`、`stops_while_reasoning=0`。
- 两条 lane `/health` 均 ok；A 日志无 error/traceback。
- **普道 B 仍是旧代码**，部署需另约窗口（B 是外部用户常用通道）。
  14:12:34（A 重启前）B 出现 1 次 A 类早停 `20261018-141234-591df7`：
  prompt 4493、completion 469、content 0、reasoning 1202、finish stop；B 也需要部署。
- 更新后的报告：`docs/reports/CLAUDE-EARLY-STOP-2026-10-18.md` §8.3；
  部署/回滚：`~/archive/patches/serve-special-token-escape-2026-10-18/README.md`。

---

## 2026-10-18 22:26 — 用原始 incident prompt 在 A 上重放

**结论**：
- 从 `~/logs/infer-segs/2026-10-17/seg-235211-e3420.jsonl.gz` 第 4 行取出当时的原始请求
  （18 messages / 54 tools / prompt 50,237 tokens），原样组装后直打快道 A（8091）。
- 为控制测试时长只把 `max_tokens` 收到 4000、`reasoning_budget_tokens` 收到 2000；
  messages/tools 未改。
- 结果：HTTP 200、42.5 s、completion 2,146、**`finish=tool_calls`**、1 个 Bash 工具调用
  （`grep` 查 Options 字段用法），reasoning 6,003 字符；content 为空是工具调用的正常形态。
- 工具参数里没有 U+2063，也没有误生成的 EOS；`prompt_escapes={"calls":4,"literals":9}`、
  `stops_while_reasoning=0`。
- **模型不再停在 `end-of-turn token (` 中途，而是继续执行任务并发出合法工具调用。**
  原始 incident prompt 的 `prompt_tokens_details.cached_tokens=50,232`，前缀缓存也正常复用。

---

## 2026-10-18 22:30 — 早停的可复现方法

**问题**：给我可复现的方法。

**结论**：
- 脚本：`~/tools/repro_claude_early_stop.py`（只读本地留档；唯一副作用是发一次真实模型请求）。
- 最小 A/B：
  ```bash
  # A（已修复）：应 content 非空
  python3 -I ~/tools/repro_claude_early_stop.py minimal --url http://127.0.0.1:8091/v1/chat/completions
  # B（旧代码控制）：应 content 0，reasoning 截断在 end-of-turn token (
  python3 -I ~/tools/repro_claude_early_stop.py minimal --url http://127.0.0.1:8092/v1/chat/completions
  ```
- 原始 incident prompt 重放（最忠实）：
  ```bash
  python3 -I ~/tools/repro_claude_early_stop.py replay --id 20261017-235221-665100 --dry-run
  python3 -I ~/tools/repro_claude_early_stop.py replay --id 20261017-235221-665100 \
      --max-tokens 4000 --reasoning-budget 2000
  ```
  实测 HTTP 200/42.5s、`finish=tool_calls`、1 个 Bash 工具调用、completion 2146、
  `prompt_escapes={"calls":4,"literals":9}`、`stops_while_reasoning=0`。
- 判读：`finish=stop` + content 0 + 无 tool_calls = 早停；`finish=tool_calls` + 合法调用 = 正常。
- 找更多样本：`python3 -I ~/tools/scan_claude_early_stop.py --date 2026-10-17`；
  拿到 `id` 后 `replay --id <id> --dry-run`。
- 完整命令/实测输出/判读表/安全注意：`docs/reports/CLAUDE-EARLY-STOP-REPRO-2026-10-18.md`。
- 注意：B 是旧代码、外部常用通道；控制组只跑一次最小提示，不要重放大 prompt。

---

## 2026-10-18 22:42 — 引擎版本字段 engine_version / engine_build

**问题**：回复里多一个字段表达当前引擎版本；版本号自动维护（每重新 build 一次 +1），
让开发用户知道当前引擎切换/重编是否成功。

**结论**：
- 新增字段：
  - `engine_version`：如 `ssebed-0.1.38-b17`（引擎名-语义版本-构建号）；
  - `engine_build`：`{name,version,build,built_at,sha256}`。
  - 出现在 `/health`、`/v1/status`、OpenAI `/v1/chat/completions`（非流式；流式首/末 chunk）、
    Anthropic `/v1/messages`（非流式；流式 `message_start.message`）。
  - 旧 `/v1/status.engine`（`0.1.38`）保留兼容；判断新 build 看 `engine_version` / `engine_build.build`。
- 构建号自动维护：
  - `switch-engine dev build ssebed` 成功后 `build/build-number.txt` +1，写 `build/BUILD.json`；
  - `Strata/setup.py` 的 legacy 本地编译同样 +1 并写 `~/Strata/build/BUILD.json`；
  - `StrataEngine` 从二进制同目录的 `BUILD.json` 读取；没有 manifest 的旧二进制回退为 SHA-256 前 8 位。
- 当前初始 manifest：`ssebed-0.1.38-b0`、`legacy-0.1.38-b0`；下一次成功 build → b1。
- 测试：`serve.test_server.EngineVersion` 6/6 通过；全量 130/131（唯一失败为既有
  `WebApp.test_page_and_files`）；构建计数器 helper 连续调用得到 1/2/3，manifest 字段正确。
- **尚未重启 serve**：线上 A/B 仍跑旧代码，响应暂时还没有这两个字段；重启 lane 后生效
  （属停服，需用户同意）。
- 文档：`docs/reports/ENGINE-VERSION-FIELD-2026-10-18.md`；
  操作说明：`~/SSEStraBed/切换引擎用户手册.md` §6.1。
- 改动文件：`SSEStraBed/deploy/switch_engine.py`、`Strata/setup.py`、`Strata/serve/server.py`、
  `Strata/serve/test_server.py`、`SSEStraBed/切换引擎用户手册.md`。

---

## 2026-10-18 22:52 — 引擎版本字段已部署到快道 A

**结论**：
- 22:45:11 重启 `strata-a`；22:51:47 ready（约 6m36s）；普道 B 未动、保持在线。
- A 的 `/health` 与 `/v1/status` 现在返回：
  `engine_version="ssebed-0.1.38-b0"`、`engine_build={"name":"ssebed","version":"0.1.38",
  "build":0,"built_at":"2026-10-17 23:17:15","sha256":"3d3f3c9d..."}`。
- 对话响应验证：
  - OpenAI 非流式：顶层字段存在，clean 请求 `content="OK"`、`finish=stop`；
  - 代理 `model=qwen3.8-flash-fast` → 8091：字段存在；
  - OpenAI 流式：首个和最后一个 chunk 都带字段；
  - Anthropic 非流式：字段在 message 顶层。
- **B（8092）仍是旧代码**：`/v1/status` 没有 `engine_version`/`engine_build`；B 的部署需另约窗口。
- 下一次 `switch-engine dev build ssebed` 成功 → `b1`；重启 A 后应显示 `ssebed-0.1.38-b1`，
  用它确认新 build 生效。
- 文档：`docs/reports/ENGINE-VERSION-FIELD-2026-10-18.md` §8；手册 §6.1 已更新。

---

## 2026-10-18 23:11 — 当前状态：早停 A 已解决、B 未解决；测试指南已写

**问题**：报告当前状态；这个问题解决了吗？解决的话写个文档，等会去使用测试。

**结论**：
- **早停**：快道 A（8091）已修复并验证——最小 prompt 返回正文、原始 incident prompt 重放
  `finish=tool_calls`、逐字复述无 U+2063/EOS；普道 B（8092）仍是旧代码，仍会复现
  （14:12 现场 `20261018-141234-591df7`）。Claude Code 的 `*-fast` 路径走 A，所以用户路径已修。
- **引擎版本字段**：A 已上线，`/v1/status` / `/health` / OpenAI/Anthropic 响应显示
  `engine_version=ssebed-0.1.38-b0` + `engine_build`；B 没有这两个字段（旧代码）。
- **服务健康**：`strata-a` / `strata-b` / `rag-proxy` 均 RUNNING；proxy `/health` ok；
  infer_store 18,060 条、pending_upload=0、minio_ok=true；A `stops_while_reasoning=0`。
- **测试文档**：`docs/reports/CURRENT-STATUS-AND-TEST-2026-10-18.md`
  （状态表、A/B 最小命令、原始 prompt 重放、engine_version 验证、b0→b1 步骤、判读表、限制）。
- **限制**：B 部署需另约低峰窗口；当前 b0 是基线，下一次 `dev build` → b1；
  外部中转站可能不透传未知字段，以 Pod 本地 `/v1/status` 为准；不要循环压测。

---

## 2026-10-19 00:14 — 会话 bd7e62ed：不是早停，是路径幻觉 + 误报目录被删

**问题**：`bd7e62ed-79cb-467e-a917-99c595ab27fb` 这个也有问题。

**结论**：
- **不是早停/只 thinking**：84 个 assistant 响应，`tool_use` 83 + `end_turn` 1；
  thinking-only 0、自动续写 0、没有工具 JSON 截断。模型是 `strata-a`（A，已修复）。
- **没有目录被删**：模型声称 15:44 起 `/home/cloud/worker` 不见了，但工具结果里
  `/home/cloud/worker/...`、`/home/cloud/dev/strata/...`、`/home/cloud/work/...`、
  `/home/cloud/engine/repo/...` 的**每一次 Read 都是 `File does not exist`**；
  worker 路径从未成功读取过。模型把正确文件 `SSEStraBed/src/program/generate.cpp`
  与幻觉路径混淆，进而误报“目录被删”。
- **正确文件完好**：`/home/cloud/SSEStraBed/src/program/generate.cpp` 7862 行、
  514,308 字节、mtime 10-18 06:13、中文 0 行。会话没有修改源码，只写了草稿
  `~/tools_dev/comment_check/gen1/` 和一条 memory `no-subagent-for-edits.md`。
- **草稿部分可救**：归一化后 380 条 spec，对正确文件 dry-run 命中 254 条、
  报错 125 条（主要是 s04/s06）；模型自己的 `apply_cn.py` 还有
  `after` 里放 `append` 的 schema bug（KeyError: 'lines'）。
- 根因是**模型路径幻觉 / 上下文跟踪失败**，不是引擎/早停；serve 的特殊 token 修复覆盖不到。
- **重复工具调用 + 无总结**（用户补充）：54 次 Bash，`strata_generate`×11、`find /`×9、
  `/home/cloud/worker`×7、`ls /home/cloud`×6、`mount`×4；最后两轮：
  - 16:00:42 `msg_mve0i14q`：thinking + “Confirmed gone. Checking two real recovery avenues
    before I report.” + Bash；
  - 16:01:19 `msg_mve0iuao`：**同一 thinking + 同一句话逐字重复**，`stop_reason=end_turn`，
    **无 tool call、无总结**；
  - 上下文 87.9k/200k（44%），无 max_tokens/API error/thinking-only → 不是截断，是模型的
    计划-执行断裂 + 退化重复。
- **原始留档核对**：
  - `20261019-000029-de091a`：98 messages、prompt 86,472、completion 1,236、
    content 67 字符、tool_calls 1、finish `tool_calls`；
  - `20261019-000107-5c6c32`：**101 messages**、prompt 87,890、completion 740、
    content **67 字符**、tool_calls **0**、finish `stop`；请求里已包含上一轮 Bash 的
    tool result 和上一轮 assistant 计划句。
  - 所以不是客户端重试（prompt 增加、tool result 在上下文里），也不是 serve 截断
    （原始 response content 就是 67 字符计划句，reasoning 2,818 字符）。
  - 能 100% 确认的是“不是技术截断/不是客户端重试”；模型内部为什么重复只能推断
    （长上下文跟踪、错误前提、上一轮 assistant 文本的复读倾向）。
- 诊断报告：`docs/reports/SESSION-BD7E62ED-DIAGNOSIS-2026-10-19.md`。
- 建议：不要再按“worker 目录被删”去恢复；继续注释时固定正确绝对路径，先 `ls`/`realpath`
  验证；草稿只保留 dry-run 命中的 254 条，125 条逐条复核；给 agent 加“同类检查两次无新信息
  就停止并总结、说要做就必须真做”的规则。

---

## 2026-10-19 01:2x — generate.cpp 中文注释：255 条已安全落地，剩余 106 条不能自动重新锚定

**问题**：接着上一次的注释草稿（`~/tools_dev/comment_check/gen1/` 8 个 JSON、381 条）继续做。

**结论**：
- **草稿审计**：381 条 = 255 可用 + 111 schema 错（`after`/`before` 条目里放了 `append` 而不是
  `lines`，就是 `apply_cn.py` 崩在 `KeyError: 'lines'` 的原因，内容没丢、已机械修复）
  + 53 锚点未命中但标识符存在 + 44 结构编造 + 13 空注释 + 16 锚点多重命中。
- **已落地 255 条**（用户选「先落真文件，再补 53 条」）：
  - 备份 `~/archive/snapshots/generate.cpp.precomment-20261018-170728`，sha256
    `49c11dc6eb214a6dfc2269c3b473bcbd49c7cb5ba113053bbff073786ecfb44b`（**这个文件被 git 忽略，
    `.gitignore:99 /SSEStraBed/`，没有 git 回滚，必须靠文件备份**）。
  - `/home/cloud/SSEStraBed/src/program/generate.cpp` 7862 → **8091 行，299 行中文**。
  - 严格 diff 核验（difflib opcode 级，不是 grep 计数）：**229 行纯插入注释 + 71 行行尾追加
    `// 注释`、被整行删除的源行 0、非注释新增行 0、不合规改写 0**。
  - 用项目自己的参数编译（`-O3 -DNDEBUG -std=c++20 -Wall -Wextra` + 三个 `STRATA_*` 宏）：
    **exit 0，17.5 s，警告 3 条与未注释基线完全相同**（2× unused-variable、1× unused-function）
    → 没引入新警告。
- **「53 条可重新锚定」这个判断是错的**，两个层次都不成立：
  - *标识符存在 ≠ 结构存在*：`if (o.dump_routing != nullptr)`（`dump_routing` 是 `std::string`）、
    `if (o.prefill_chunk > 0 && o.serve)`、`for (int64_t i = 1; i < n_prompt; ++i)`、
    `if (produced.empty())`、`if (dump != nullptr && …)` 全文都不存在。
  - *相似度重新锚定不可信*：严格阈值 0.85 只有 **24/106** 对上真实行，且多条对到**语义不同的行**
    —— `for (int64_t i = 1; i < n_prompt; ++i)` 对到 `for (int64_t i = 0; i < want; ++i)`（行 3436，
    另一个循环）、`} else if (!line.empty())` 对到 `} else if (!why.empty())`（行 1712，与 serve
    协议无关）、`if (line.rfind("GEN ", 0) == 0)` 对到 `if (line.rfind("BCHAIN ", 0) == 0)`（行 6011）。
- **草稿对 serve 协议的描述本身有错**：`IMAGES` / `CONV` / `CONVDROP` / `CONVSAVE` / `CONVSTAT`
  在 generate.cpp **和全仓库都没有分派代码**（只有两处无关内核注释里的英文单词 IMAGES）。
  引擎里真实的命令分派不是 `if/else if` 阶梯，而是各自独立的 `if`：BSTOP 6003、BCHAIN 6011、
  BGEN/BGENI 6057、QUIT 6076、GENI 6084、GEN 6085。所以那 5 条协议注释在本文件里没有落点。
- **真正机械可重锚的只有约 12–15 条**（同一主题、只差写法）：`--expert-cache`→1395、
  `int32_t d_mrope`→2316（漏了 `*`）、`o.dump_routing != nullptr`→3774 `!o.dump_routing.empty()`、
  `suffix_draft && spec>=2 && !mtp.empty()`→1810（尾部条件不同）、`int32_t pf_need =
  Prefill::bytes_needed`→4664 `const uint64_t need = …`、`stp->sp.reset(); cudaGetLastError()`→4790、
  `pf_parts.size() > 1 / alone = pick(&pf_parts[0])`→4609、`kStepChunks / c < o.prefill_chunk`→4783、
  `split_small`→4830–4836、`o.logits_stride > 1`→1861、`conversation_cache` 容量检查→1564、
  `expert_profile_save` 周期保存→6073。其余需逐条读代码判断，不能批处理。
- **服务未受影响**：只改了源码，`build/strata` 未重链、supervisord 未重启，线上仍跑
  10-18 15:59 的二进制。要让注释进线上需要 `dev build` + 重启 = 停服，**必须先经用户同意**。
- 工具与产物：`~/tools_dev/comment_check/apply_cn.py`（已修 schema 兼容）、
  `gen1/usable.json`（255 条）、`gen1/discard_report.md`（126 条丢弃明细）、
  核验脚本 `…/scratchpad/verify2.py` + `reanchor_strict.py`。

**教训（已写 memory）**：注释锚点审计必须按**整行文本**核验，不能按标识符存在或相似度判断；
「标识符都在文件里」不等于「这段代码存在」。

---

## 2026-10-19 01:2x — generate.cpp 中文注释：255 条已落地并验证，剩余 106 条不能自动重新锚定

**问题**：上一轮会话（bd7e62ed）留下的注释草稿 `~/tools_dev/comment_check/gen1/` 到底有多少能用？怎么安全落地？

**结论（全部实测）**：
- **源文件**：`/home/cloud/SSEStraBed/src/program/generate.cpp` 7862 行、514,308 字节、mtime 10-18 06:13、
  落地前 **0 行中文**。它被 `.gitignore:99 /SSEStraBed/` 忽略，`git ls-files src/` = 0 个文件 →
  **没有 git 回滚**，所以先备份：`~/archive/snapshots/generate.cpp.precomment-20261018-170728`
  （sha256 `49c11dc6…`，与源文件一致）。
- **草稿 381 条审计**：可用 **255**（合并进 `gen1/usable.json`）；schema 错 **111**（`after`/`before` 条目里
  放了 `append` 而不是 `lines` —— 这是 `apply_cn.py` 崩在 `KeyError: 'lines'` 的原因，内容没丢，已机械修复）；
  锚点未命中 **106**；多重命中 **16**；空注释 **13**。
- **已落地**：7862 → **8091 行**，**299 行中文**。严格 diff 核验（difflib opcode 级）：
  **229 行纯插入注释 + 71 行行尾追加 `// 注释`、被整行删除的源行 0、非注释新增行 0、不合规改写 0**。
- **编译核验**：用项目自己的参数（`-O3 -DNDEBUG -std=c++20 -Wall -Wextra` + 三个 `STRATA_*` 宏 + 项目 include）
  编译真文件 **exit 0，17.5 s**；警告 **3 条，与未注释基线完全相同**（2× unused-variable、1× unused-function）
  → 没有引入新警告。
- **剩余 106 条不能自动重新锚定**（这是本轮最重要的否定结论）：
  - 严格相似度（difflib ≥0.85）只有 **24** 条能对上真实行，而且其中多条匹配到**语义不同的行**：
    `for (int64_t i = 1; i < n_prompt; ++i) {` 对到 `for (int64_t i = 0; i < want; ++i) {`（行 3436，另一个循环）；
    `} else if (!line.empty()) {` 对到 `} else if (!why.empty()) {`（行 1712，完全不同的上下文）；
    `if (line.rfind("GEN ", 0) == 0) {` 对到 `if (line.rfind("BCHAIN ", 0) == 0) {`（行 6011）。
  - **标识符存在 ≠ 结构存在**：上一轮的「可重新锚定 53 条」判据只查标识符，太宽松。
  - **草稿对 serve 协议的描述本身有错**：`IMAGES` / `CONVDROP` / `CONVSAVE` / `CONVSTAT` 在 generate.cpp
    与全仓库都**没有分派代码**（全仓库只有两处无关的 "IMAGES" 出现在 kernel 注释里）。引擎里真实的命令分派是
    行 6003 BSTOP / 6011 BCHAIN / 6057 BGEN+BGENI / 6076 QUIT / 6084-6085 GENI+GEN，**不是** `if/else if` 阶梯。
  - 也不存在的结构：`pf_need`/`pf_room`/`pf_slots` 那一大段、`batch_admit`/`batch_finish`/`batch_reset`/
    `batch_stats`/`batch_drain`/`run_request`/`decode_greedy`/`decode_spec`/`window_*` 这些 lambda。
- **服务未受影响**：只编译到 scratch 的 `.o`，`build/strata`（mtime 10-18 15:59）**未重链**，
  strata-a/strata-b 未重启 → 对外服务无停服。注释要生效到线上需要 `dev build` + 重启，那必须另约窗口。

**下一步（未做）**：剩余 106 条只能逐条人工读代码决定 —— 约 12–15 条是纯笔误级（`int32_t d_mrope` →
`int32_t* d_mrope` 行 2316；`o.dump_routing != nullptr` → `!o.dump_routing.empty()` 行 3774；
`else if (a == "--expert-cache")` → `if (…)` 行 1395），其余应直接弃用或改放到正确的文件。

**出处**：`~/tools_dev/comment_check/gen1/usable.json`、审计脚本
`/tmp/claude-1000/-home-cloud/84557757-…/scratchpad/{audit_final.py,normalize.py,verify2.py,reanchor_strict.py}`、
`~/bin/note` 2026-10-19 01:0x 条目。

---

## 2026-10-19 01:20 — generate.cpp 中文注释：255 条已落地，剩余 106 条不能自动重新锚定

**问题**：草稿 381 条注释里哪些能安全写进 `src/program/generate.cpp`？落地后怎么证明只加了注释？

**结论**：
- **已落地 255 条**（合并到 `~/tools_dev/comment_check/gen1/usable.json`）：
  `src/program/generate.cpp` 7862 → **8091 行**，中文注释 **299 行**；
  备份 `~/archive/snapshots/generate.cpp.precomment-20261018-170728`（sha256 `49c11dc6…`，
  与写入前逐字节一致）。**该文件被 git 忽略**（`.gitignore:99` `/SSEStraBed/`，
  `git ls-files src/` = 0），所以没有 git 回滚，只有这份文件备份。
- **只加注释的硬核验**（difflib 逐 opcode，不是 grep 计数）：
  纯插入注释行 **229**、行尾追加 `// …` 的源行 **71**、被整行删除的源行 **0**、
  非注释新增行 **0**；71 条行尾追加里每条源行都是新行的**逐字前缀**。
  ⚠️ 我一开始用 `grep -c '^< '` 报成「被修改源行 0」，实际是 **71** —— 那 71 是 `append`
  语义的行尾追加，代码文本没变，但**必须按「前缀 + 行尾注释」逐条核验**，不能只看计数。
- **编译核验**：用项目自己的参数
  `g++ -DSTRATA_NATIVE_EXPERTS=1 -DSTRATA_PREFILL_FUSED=1 -DSTRATA_PREFILL_MMQ=1 -DSTRATA_VERSION="0.1.38" -O3 -DNDEBUG -std=c++20 -Wall -Wextra`
  → **exit 0**，17.5 s；警告 **3 条与未注释原文件完全相同**（2× unused-variable、1× unused-function）
  → 没引入新警告。`build/strata` **未重链**、服务**未重启**（源码注释不影响线上，重启需另约）。
- **`apply_cn.py` 的崩溃根因**：111 条把注释文本放在 `after`/`before` 条目的 `append` 字段里
  （应为 `lines`）→ `KeyError: 'lines'`。已修：`append` 字段按行尾注释处理。内容没丢。
- **剩余 106 条不能自动重新锚定**（这是本轮最重要的否定结论）：
  - 「标识符都在文件里」的宽松判据给出 53 条可重新锚定 —— **这个判据本身不可信**。
    例：草稿写 `} else if (line.rfind("GENI ", 0) == 0) {`，真实代码是
    `const bool geni = line.rfind("GENI ", 0) == 0;`（6084）+ `if (!geni && line.rfind("GEN ", 0) != 0) {`（6085），
    **根本没有 else-if 命令梯**；`pf_need/pf_room/pf_slots`、`batch_admit/batch_finish/run_request/decode_spec/window_step`
    这些标识符全文不存在。
  - 换成严格判据（整行 difflib 相似度 ≥ 0.85）只有 **24 条**能对上真实行，而且其中多条匹配到
    **语义不同的行**：`for (int64_t i = 1; i < n_prompt; ++i) {` 对到 `for (int64_t i = 0; i < want; ++i) {`（3436）、
    `} else if (!line.empty()) {` 对到 `} else if (!why.empty()) {`（1712）、
    `if (line.rfind("GEN ", 0) == 0) {` 对到 `if (line.rfind("BCHAIN ", 0) == 0) {`（6011）。
    → **相似度不是语义证据，自动重新锚定会把注释挂到错误的代码上。**
  - `IMAGES` / `CONVDROP` / `CONVSAVE` / `CONVSTAT` 在 generate.cpp 与全仓库都**没有分派代码**
    → 草稿对 serve 协议的描述本身有错，这几条注释没有可挂的地方。
- **真实 serve 命令集**（generate.cpp 实测）：`BSTOP`(6003) / `BCHAIN`(6011) / `BGEN|BGENI`(6057) /
  `QUIT`(6076) / `GENI`(6084) / `GEN`(6085)，`RESUME` 只在 6450 回显、4355 与 4498 有说明。
- 下一步若继续：剩余条目只能**逐条读真实代码**再定锚点，不能靠脚本匹配；
  已落地的 255 条**位置**是硬验证的，**内容语义**仍未逐条核对。

---

## 2026-10-19 12:53 — 写书会话 848a12ed 早停诊断

**问题**：这次早停的原因是什么？就是最近让 agent 去写书的那个对话。

**结论**：
- **不是特殊 token A 模式**：4 次 early-stop 的请求里 `<|endoftext|>`/`<|im_end|>`/
  `<|im_start|>` 计数都是 0；A 的 `prompt_escapes={"calls":365,"literals":1603}` 正常。
- **是 B 模式 plan-then-EOS**：
  - 主会话 `20261019-123936-abb483`（12:39:36）：147 msgs / prompt 146,433 /
    completion 3,240 / content 42 字符 / reasoning 6,995 / **tool_calls 0** / `stop`；
    content “第 3 部完成。现在写引擎内核部分…”，reasoning 末尾 “Let me write now.”。
  - 主会话 `20261019-124548-da1258`（12:45:48）：152 msgs / prompt 156,568 /
    completion 267 / content 26 / reasoning 772 / **tool_calls 0** / `stop`；
    content “第 09 章 prefill。先读它自己的设计注释。”，reasoning 末尾 “Let me grep…”。
  - 子 agent `20261019-120419-5896b3`（12:04:19）：197 msgs / prompt 79,465 /
    completion 204 / content “Now the frontend JS.” / **tool_calls 0** / `stop`。
  - 子 agent `20261019-120918-151e77`（12:09:18）：202 msgs / prompt 79,792 /
    completion 504 / **content 0** / reasoning 1,571 / **tool_calls 0** / `stop`
    （thinking-only；上下文里有 “Wasted call — file unchanged since your last Read”
    和 “handback-send-enforce”）。
- **贡献因素**：长上下文（146k/156k/79k）、大 Write 负载、子 agent 的 Read dedup 提示
  没告诉已读 range、主会话违反“不要开子 agent”开了 3 个并触发 429（并发上限 2）。
- **书当前状态**：`/home/cloud/book` 有 01/02/04/05/06/07/08 + README；
  缺 03、09–19、附录 A/B/C；模型自己说 04 写错事实、需要重写。
- **建议**：按章写，强制“读源码 → Write 到 `book/N-*.md` → 下一章”；长会话到 100k+
  就分段/新会话；agent 规则“说 Let me write/read 就必须真的发 tool call”；
  代理层可选加 guard（stop + 0 tool call + 计划句 → 注入 continue），需重启代理。
- 报告：`docs/reports/SESSION-848A12ED-BOOK-EARLY-STOP-2026-10-19.md`。

**补充核对（tool schema / max_tokens / 前缀）**：
- **tool schema 没丢**：两次 early-stop 的 `request.sent.tools` 都有 55 个工具，`Write` 在列表里；
  system message（6,019 字符）也在。
- **max_tokens 不是原因**：`max_tokens=32000`；completion 3,240 / 267；引擎日志
  `thinking: 3194 of max 32000` → `done: 3240 (stop)`、`thinking: 232` → `done: 267 (stop)`；
  两次 `done` 前都**没有 `writing a tool call` 行** → 模型根本没进入 tool-call 模式，不是工具调用被截断。
- **前缀/任务锚点确实被丢**：`mode=assembled`、`client_msgs=315` → `sent_msgs=147`；
  `sent` 的 147 条 messages 里找不到原始任务“写一本教学书 / 持续工作 / 不要开子agent”；
  system + tools 还在，但任务/约束被组装挤掉。
- **回放实验**：原样回放 `20261019-123936-abb483`（146,433 prompt，max_tokens 4000，
  reasoning_budget 2000）→ HTTP 200/70s，finish `stop`，completion 2,690，
  content 2,867 字符（代码注释/事实摘录），**tool_calls 0**；工具 schema 在，但模型仍不调 Write。
- **结论**：不是 tool schema 丢失，也不是 max_tokens；是
  **RAG 组装丢任务锚点 + 模型 plan-then-EOS** 共同导致“说要写、但没写”。
  建议代理组装固定保留首条用户任务/当前章节目标（改代理需重启），并按章写、强制 Write 落盘。
- **任务卡实验（直接验证）**：在同一条 146,433-token 请求末尾追加
  “TASK CARD：当前目标是用 Write 把第 04 章写到 book/04-*.md；现在调用 Write”，
  max_tokens 8000/reasoning 2000 → **HTTP 200/10.1s，finish=tool_calls，1 个 Bash tool call**
  （模型先检查 04 章是否已存在，引擎日志 `writing a tool call: Bash: 322 of max 8000`）。
  不加任务卡时是 0 tool calls、`stop`。补回任务锚点后模型立刻从“分析/计划”切回“调工具”。

---

## 2026-10-19 13:08 — 关闭 RAG，回调引擎原生 256K

**问题**：先去掉现在的 RAG，回调成 256K 上下文；RAG 看起来有很多问题，外面会同步调整。

**结论**：
- `rag-proxy/config.json` 增加 `"rag_enabled": false`；`rag_proxy.py` 在 `thinking_guard` 后短路：
  不建 RAG 会话、不 ingest、不计数、不组装，按模型 ID 选 lane 后**纯透传**。
- 有效上下文 = 引擎原生 **262144 token（256K）**；超窗由引擎 400 / 代理 `retry_on_overfit`
  按引擎报的余量压一次 max_tokens 重发；如果 prompt 本身超窗，客户端会收到 400。
- 2026-10-19 13:07:00 重启 `rag-proxy`；13:07:06 恢复；**引擎未重启**，两条 lane 全程在线；
  重启前 in-flight=0。
- 验证：`/health config.rag_enabled=false`、`counters.no_rag=1/assembled=0/sessions=0`；
  经 8080 请求 `qwen3.8-flash-fast` → `finish=stop, content="OK", engine_version=ssebed-0.1.38-b0`；
  infer.db `20261019-130717-98e9e2`：lane 快道、**mode=no_rag**、prompt 56、completion 30。
- `thinking_budget_guard` 保留（不是 RAG）；要完全原样透传可把 `thinking_budget_guard` 设为 false。
- 回滚：`config.json` 的 `rag_enabled` 改回 `true` + 重启代理；或恢复
  `~/archive/patches/rag-off-256k-2026-10-19/` 的 before 文件。
- 文档：`docs/reports/RAG-DISABLED-256K-2026-10-19.md`；`rag-proxy/README.md` 顶部、
  `docs/项目背景与问题总览.md` §3.4 已加现状说明。

---

## 2026-10-19 13:09 — 图片处理机制与用户用法

**问题**：现在图片处理的机制是什么？用户侧怎么用图片？

**结论**：
- 两条 lane 都开着 vision（`/health images:true`）；编码器是 serve 的 `strata-vision`
  （llama.cpp mtmd + `mmproj-Qwen3.8-Flash-Next-BF16.gguf`），config `--vision`、GPU、
  `max_tokens:1024`。单图最多 **1024 image token**；编码 0.1–0.5 s/张，按图片 SHA-256 缓存。
- 链路：客户端图片 → `rag-proxy`（现在 `rag_enabled=false`，纯透传，不做图片估算/组装）→
  `Strata/serve` 的 `_image_source`/`images_of` → `Vision.encode` 归一化+编码 →
  把模板里的一个 `<|image_pad|>` 替换成 N 个 pad → C++ 引擎消费。
- 用户侧：
  - OpenAI `/v1/chat/completions`：`content` 里放 `{"type":"image_url","image_url":{"url":
    "data:image/...;base64,..."}}`；`url` 也可以是 http(s) 或 serve 能读到的本地路径。
  - Anthropic `/v1/messages`：`{"type":"image","source":{"type":"base64","media_type":
    "image/png","data":"..."}}`；`source` 也可用 `{"type":"url","url":"..."}`。
  - 终端：`/image <路径>` 再问问题。
- 格式：JPEG/PNG/BMP/GIF 直接读；WebP/TIFF/AVIF 等经 Pillow 转 PNG（透明填白）。
- 实测（A/8091，经 8080）：
  - 96×96 红方块 + 一句问题：prompt **33 tokens**，content ` red`，0.9 s；
  - 同图第二次（Anthropic）：input 7 + cache_read 26 = **33**，0.2 s；
  - 640×480 蓝底 + 一句问题：prompt **324 tokens**（约 300 是图片），content ` Blue`，1.3 s；
  - B/8092 `qwen3.8-flash` 同图：prompt 33，content ` red`，0.8 s。
- 限制：图片 token 计入 262144（256K）；RAG 关闭后不组装，长文本+图片超窗就是引擎 400
  （代理 overfit 只能压 max_tokens，压不掉图片 token）；单图上限 1024；本地路径只对 serve 有效；
  外部中转站 `api.ssemarket.cn` 是否透传图片取决于其转译层，建议走一次外部出口验证。
- 报告：`docs/reports/IMAGE-VISION-MECHANISM-2026-10-19.md`；项目问题 #6 已更新。

**单图压缩/限制（补充）**：
- **有 token 级自动缩放**：`strata-vision --max-tokens 1024` 设置 `cp.image_max_tokens=1024`
  （`Strata/tools/vision/strata_vision.cpp:105`），mtmd 会把大图缩放到这个 token 上限。
  实测：96×96 总 prompt 33；640×480 324；**4000×3000 总 prompt 996**（2.1 s），
  不是 12MP 的巨量 token。
- **没有文件级自动压缩/重采样**：`Vision.normalize()` 只做格式归一化
  （JPEG/PNG/BMP/GIF 直接过；WebP/TIFF/AVIF→PNG；透明填白），不按文件大小/像素做 JPEG 质量压缩、
  不重采样；也没有按上下文余量动态降级。原始文件仍完整读取/解码，只是 mtmd 按 token 上限缩放。
- **代理层**：`image_tokens=1024` 只是 RAG 打开时的预算估算；现在 `rag_enabled=false` 不估算/不压缩。
- 可调：把 `vision.max_tokens` 调低（如 512）可让单图更小，需重启 lane；若要“按像素/文件大小
  自动压缩”，需在 serve 层加 `max_side`/`max_pixels`/`max_bytes` 预处理（目前没有，可按需实现）。

---

## 2026-10-19 13:21 — PDF / DOC / Excel 文档格式怎么处理

**问题**：上游传 PDF、DOC、Excel 文档时模型怎么处理？是不是容易直接超上下文？

**结论**：**Pod 没有文档解析器。**
- Anthropic `{"type":"document", ...pdf...}`：**静默丢弃**（`anthropic_to_messages` 只处理
  text/thinking/tool_use/tool_result）。实测 HTTP 200、prompt 21 tokens，模型回复
  “Please provide the text content of the PDF...”。
- OpenAI `{"type":"file", ...}`：**静默丢弃**（`_parts_of`→`_text_of` 只保留 text/input_text）。
  实测 HTTP 200、prompt 21、模型回复“Please provide the content of the file...”。
- PDF/DOC/XLSX 原始字节塞进 `image_url`/`image`：**HTTP 400**
  `the image could not be read (cannot identify image file ...)`。
- 上游把文档抽成文本：**当普通文本**处理；没有文档感知的分块/摘要。实测 340,000 汉字 =
  180,012 tokens，HTTP 200 但 55.8 s prefill；595,000 汉字 = 315,012 tokens，
  **HTTP 400**（`prompt ... exceeds the context (262144); requests are never truncated`）。
  RAG 关闭后代理不组装；overfit 只能压 max_tokens，压不掉 prompt。
- PDF 逐页渲染成图片再发：走 vision，每页≤1024 image token；页数多会累积超窗。
- DOCX/XLSX 没有原生 parser；建议上游用 python-docx/openpyxl/pandas 抽文本/CSV。

**建议**：上游做文档抽取/分块/摘要；扫描件渲染成页面图片；不要把原始字节当 image；
若希望 Pod 原生支持文档，需要在 `rag-proxy`/serve 加文档预处理层（改代码 + 重启），
或先做“不支持的 document/file part 返回明确 400”的最小改进。

报告：`docs/reports/DOCUMENT-FORMATS-HANDLING-2026-10-19.md`；项目问题 #6 已更新。

---

## 2026-10-19 13:56 — 网关自动识别并解析文档（已上线）

**问题/需求**：上游直接传 PDF/DOC/Excel 时，网关要自动检测格式、解析成可读内容，
避免整份文档一下子把 256K 上下文顶爆。

**实现**：`rag-proxy/doc_extract.py` + `rag_proxy.py` 接线（RAG 关闭时也生效）：
- 识别：`document` / `file` / `input_file` / `image_url` / `image` 里的 PDF、DOCX、XLSX、
  XLS、CSV、TXT/MD、HTML、RTF；真实图片（PNG/JPEG/...）原样走 vision，不碰。
- 抽取：PDF→`pypdf`；DOCX→`python-docx`；XLSX→`openpyxl`；XLS→`xlrd`；扫描 PDF→
  `pymupdf` 渲染页面 PNG（最多 8 页，每页按 1024 token）。
- 预算：`room = 262144 - 8 - other - max_tokens - 4096`，`doc_budget=min(120000, room)`；
  文本按头 70% + 尾 30% 截断并加 `[... truncated: N chars omitted ...]`；`<256` 明确 400。
- 安全：默认不读客户端本地路径、不抓 URL（`documents.allow_urls=false`，防 SSRF/文件泄露）。
- 旧 `.doc`、PPTX 明确 400；`.doc` 建议转 `.docx`/PDF/文本。

**验证**：
- 单测 12/12（`test_doc_extract.py`）。
- 生产 8080（13:56 重启，引擎未重启）：PDF 当 image_url → 0.7s/prompt 103/`ZEBRA-42`；
  DOCX 当 file → 1.0s/prompt 113/`DOCX-777`；扫描 PDF → 2.6s/prompt 1103/`SCAN-CODE-888`；
  普通红图回归 → 0.7s/prompt 87/`Red`，文档计数不增加。
- 临时实例把 `documents.max_tokens` 降到 4000：264,029 字符大文本 → prompt 2226，
  头尾两个 code 都答对，HTTP 200（no 400）。

**限制**：不做 OCR（扫描件靠 vision 读页面）；PPTX/旧 `.doc` 未支持；只扫顶层
`messages[].content`，`tool_result.content` 嵌套 document 暂不处理；表格简化为 `a | b`，
500×30 截断；截断是头尾保留不是摘要。

代码/配置：`~/rag-proxy/doc_extract.py`、`rag_proxy.py`、`test_doc_extract.py`、
`config.json → documents`；报告：`docs/reports/DOCUMENT-GATEWAY-EXTRACT-2026-10-19.md`；
补丁：`archive/patches/gateway-document-extract-2026-10-19/`。

---

## 2026-10-19 14:43 — Claude Code 上下文显示 vs Pod 引擎实测 vs 中继估算：三方口径差距根因

**问题**：书稿会话里 `/context` 显示 157.9k/200k（79%），但同一个请求被中继 400（“输入超限：本次约 398,890 tokens，超 256,000”）、
被本 Pod 引擎 400（“prompt (372066) + max tokens (1) exceeds the context (262144) (#545)”）。三者差这么大，是哪里的计费问题？

**结论**：
- **计费没错，错在估算层。** Pod 账务口径 = 引擎 tokenizer 实测（infer_store 的 `prompt_tokens`）；400 请求在 serve 层
  tokenize 后即拒，不占 GPU、不计费。三个数分别是：CC 客户端估算 157.9k、中继上界估算 398.9k（真值 +7.2%）、引擎实测 372,066（真超窗）。
- **CC 低估 ~2.4× 的原因**：①中文约 0.55–0.75 token/字，CC 按 ≈4 字符/token 估 → 中文内容低估 2–3 倍；
  ②该会话 12:49 后切去了另一渠道（token-plan/aliyun，Pod 侧 12:49–14:14 无任何该会话记录），快道渠道的校准冻结在
  12:49 的 154,796（那一轮 CC≈157.9k 与真值基本一致），14:14 “继续”一次性把 ~92 万字符书稿历史（增补大量中文）发给 Pod。
- **两个 400 不同层**：“#545”文案出自本 Pod `Strata/serve/server.py:1760-1763`，rag-proxy 压 max_tokens=1 重试仍败后
  原样透传（`rag_proxy.py:1715-1720`，Pod 日志 06:15:00–13 UTC 三次）；“输入超限…256,000”文案**不在本 Pod**（全库+git -S 无），
  来自上层中继（api.ssemarket.cn/token-plan），且其窗口用十进制 256,000，与引擎 262,144 不对齐。
- **Pod 代理上界估算器虚高 3.3×**：12:49 同一请求 est=516,561 vs 引擎 154,796（`rag_proxy.py:229-230` 按
  tok_per_char_other=0.70，而英文/代码实测 0.19–0.47）。不坑计费，但 `clamp_max_tokens`(:265)/思考预算(:372) 会被虚高值压狠。
  serve 已有精确计数 `/v1/messages/count_tokens`（server.py:2668），守卫没用。
- **经验法则**：中文会话把 CC 显示 ×2.4 当引擎真值；CC 到 ~100k 就该 /compact。渠道切换后 /context 不可信。
- **待办**：中继窗口对齐 262,144 并回传引擎 usage；本 Pod（需重启、等同意）守卫改精确计数、超窗不盲重试改 413+实测数、
  strata-a/b.json 加 `"fit_max_tokens": true`。
- 详版：`docs/reports/CONTEXT-COUNT-BILLING-2026-10-19.md`；证据：`~/logs/infer-segs/2026-10-19/`、`~/deploy/logs/rag-proxy.out.log`。

---

## 2026-10-19 15:50 — 上下文计数口径差：Pod 侧三项修复落地

**问题**：用户批准“本 pod 可改的请你改好”——把报告 C 组三项（守卫精确计数、超窗不盲重试+不透传 #545、fit_max_tokens）落地。

**结论/进展**：
- 已改：`rag-proxy/rag_proxy.py`（no_rag 窗口预检 + `overfit_info/overfit_error` 改写 + 真超窗不重试 +
  `window_guard_rejects` 计数；infer 留档仍存引擎原始错误正文）、`Strata/serve/server.py`
  （`_count_tokens` 支持 openai 方言，新路由 `/v1/chat/completions/count_tokens`）、三条 lane 配置加
  `"fit_max_tokens": true`（profiles/ssebed/strata-a.json + strata-iq3_xxs-a/b.json）。
- **rag-proxy 已重启生效**（15:33，小请求 200、上游 ok、对旧 serve 的计数 404 按设计降级透传）。
  serve 侧两项**待 strata-a/strata-b 重启**（模型重载 1–3 分钟停服窗口）——分类器与红线都要求用户
  **点名批准**这两条引擎重启，未绕过，已等批复。
- 回归：`test_window_guard.py` ALL PASS。顺带修了两处与现状脱节的测试脚手架：A6 stub 4/5 元组不符
  （工作树里 `build_memory_block` 早返回 5 元组）；B/C 组 `start_proxy` 派生生产 config.json，
  生产 `rag_enabled=false` 后这些“组装”用例其实跑的是透传分支——测试内强制开 RAG。
- 兼容设计：先重启的代理对新计数返回 404 时自动降级原样透传，旧 serve 在线期间无风险。
- 出处：`docs/reports/CONTEXT-COUNT-BILLING-2026-10-19.md` 落地记录；代码 `rag_proxy.py`
  （count_tokens/overfit_info/_proxy 400 分支/no_rag 预检）、`server.py`（_count_tokens、路由分发）。

---

## 2026-10-19 19:16 — 计数口径修复：strata-a/b 重启生效与全项验证

**问题**：用户点名批准“这两个都重启一下”——strata-a/strata-b 重启后按 TODO 验证。

**结论**：
- 两通道 18:52–18:55 重启完成并 loaded：A=ssebed-0.1.38-b1（b1 是 10-18 23:59 内容未变重建的构建号，
  重启才显示出来），B=legacy-0.1.38-b0。
- OpenAI 方言精确计数在 8091/8092 均通（与 anthropic 方言同数：同一句 56 token）；
- 30 万字探针（实测 600052 token）现在被代理**本地拒绝、0 次上游来回**，客户端拿到结构化计数错误；
- :8080 正常小请求 200 无回归；`serve/test_server.py` 131 条过 130（唯一失败为 Vue 迁移遗留的前端资源断言，与本次无关）；
- `fit_max_tokens` 生效证据=配置有效+加载路径+进程重启；行为级验证要窗口边缘大 prefill，未在现网跑，
  留待观察 `overfit_retries` 趋零。
- 剩两项在 Pod 外/时间维度：中继窗口对齐与 usage 真值（找 api.ssemarket.cn 维护人）；新日志分布观察 1–2 天。
- 出处：`docs/reports/CONTEXT-COUNT-BILLING-2026-10-19.md` 落地记录+TODO 状态。

---

## 2026-10-20 02:15 — 《SSE-Strata 系统全书》补完：旋钮表与名词表取证中的六条可复用事实

**问题**：把 `~/book/` 补成完整书（新增 11–19 章 + 附录 A/B/C），写附录 B（旋钮表）与附录 C（名词表）时
需要逐项核实"引擎默认值 / 生产值 / 网关配置全键"，过程中确认了六条此前没有成文的事实。

**结论 / 关键事实**：
1. **两条 lane 的引擎 `args` 逐项完全相同**（`Strata/strata-iq3_xxs-b.json` 与
   `deploy/engines/profiles/ssebed/strata-a.json`）。差异只在二进制、CWD、日志路径、`gpu`、`port`。
   → "快道更快"不是参数不同，而是车道分工 + 钉核 + 流量少。
2. **`deploy/engines/profiles/ssebed/strata-b.json` 缺 `fit_max_tokens` 键**（legacy B 与 ssebed A 都有）。
   → 把普道 B 切到 ssebed profile 会顺手改掉"超窗自动截断输出"的行为，切换时必须一起核对。
3. **两份 lane 配置都没有设 `--prompt-cache` / `--prompt-cache-every` / `--prompt-cache-root`**，
   因此吃引擎默认 **6 / 16384 / 2048**（每个检查点约 118 MB 主机 RAM）。
   → 12 章 §12.7 的"TTFT 四旋钮"至今未落地，属配置级改动但需停服窗口。
4. **引擎默认值集中在一个结构体里**：`SSEStraBed/src/program/generate.cpp:284–447`
   （`pool_workers=0` :284、`vram_reserve_mib=700` :365、`spec=0` :389、`batch=0` :393、
   `batch_groups=1` :399、`batch_mtp=false` :400、`prompt_cache=6` :439、`conversation_cache_mib=0` :440、
   `conversation_cache_min_free_mib=2560` :442、`prompt_cache_every=16384` :444、`prompt_cache_root=2048` :447）。
   两个硬编码常量：`kVerifyMaxT=8`（`include/strata/kernels/verify_kernels.hpp:28`）、
   `GMAX=8`（`src/kernels/cuda/s2_expert_grouped.cu:1359–1365`，`static_assert(GMAX >= kVerifyMaxT)`，超出静默丢条目）。
5. **`GET /slots` 永远只返回一个合成槽**（`Strata/serve/server.py:2594–2601`：
   `slot = {"id": 0, "n_ctx": …, "is_processing": busy}` → `[slot]`），与引擎的 4 个批槽无关。
   → 这是"引擎状态模型是单路的"的直接代码证据，也是 19 章盲区清单第一条的依据。
6. **日志时区口径不统一（跨日志对齐前必须换算）**：`~/deploy/logs/supervisord.log` 与
   rag-proxy 的 `[HH:MM:SS]` 前缀 = **UTC**（`rag_proxy.py:168-169` 用裸 `time.strftime`）；
   `switch-engine.log` 与 `infer_store` 归档名 = **北京时间**。差 8 小时。

**顺带核实的行号漂移（旧文档不可直接引用）**：`rag_proxy.py` 2206 → **2382 行**（mtime 10-19 15:28 北京）；
`clamp_max_tokens` :243 → **:265**、`thinking_guard` :264 → **:286**、`overfit_room/patch_max_tokens`
:320/:333 → **:342/:355**、盲重试调用点 :1715-1720 → **:1768**、`assemble` :1301 → **:1255**、
`chunk_messages` :708 → **:728**（:704 是 `chunk_turn`、:739 是 `chunk_messages_legacy`）。
`generate.cpp` 两个仓库行数不同：legacy `Strata` **7741**、`SSEStraBed` **8091**。

**产出**：`~/book/` 23 个文件（正文 01–19 + 附录 A/B/C + README），5371 行 / 342,858 字节；
04 章增补 §4.10「2026-10-19 窗口预检与 400 改写」；README 写作时间与取材清单行数口径已修正。
已备份到 MinIO 桶 `qwen-3.8-flash` 前缀 `book/`（23 对象，上传日志 `~/logs/minio-book-backup-20261020-0214.log`）。
**全程只读 + 只写 `~/book/`，未重启任何服务、未打压测流量。**

**出处**：`~/book/appendix-b-旋钮表.md`、`~/book/appendix-c-名词表.md`、
`~/book/appendix-a-源码入口地图.md`（锚点均于 2026-10-20 02:00 北京当场 `grep -n` 核实）；
配置文件 `Strata/strata-iq3_xxs-b.json`、`deploy/engines/profiles/ssebed/strata-{a,b}.json`（脱敏读取）；
`docs/reports/CONTEXT-COUNT-BILLING-2026-10-19.md` §落地记录（04 章 §4.10 的素材源）。

---

## 2026-10-20 17:05 — DeepSeek 开源 DeepInfer：内容要点、能否在我们这套硬件上跑

**问题**：用户发来微信文章链接（DeepSeek 开源推理框架公告），需要读正文并判断对本项目「改造推理引擎」主线的价值。

**结论 / 关键事实**：
1. **公告内容**（公众号「DeepSeek」，2026-10-20 16:54 北京）：把内部推理框架 **DeepInfer 全面开源**，
   同时开源 **deepseek-recipe**（生产级 Token 编解码组件，声称与官方 API 处理逻辑完整对齐）。公告正文 1305 字，
   **没有任何性能数字**，能力细节全在仓库 README。
2. **仓库实测元数据**（当场 `api.github.com`）：`DeepInfer` = Apache-2.0、Python 2.35 MB + C++ 278 KB、
   created **2026-10-16**、last push **2026-10-20 04:09 UTC**、254 stars、**无任何 tag/release**（成熟度低）；
   `deepseek-recipe` = MIT、**Rust**、created 2026-09-10、460 stars。
3. **DeepInfer 只支持 DeepSeek-V4.1-Flash**，硬件只支持 **NVIDIA SM100（Blackwell）或昇腾 950**；
   门槛 GCC ≥ 13 / **Python 3.12** / **CUDA 13**（或 CANN 9.2）。基准口径：prefill 4 张 SM100、decode 64 张 SM100。
4. **跑不了**：本 Pod 是 3× **RTX 4090 D（compute_cap 8.9 / SM89，24 GB）**，且我们服务的是本地量化 GGUF 路线。
   → DeepInfer 对我们是**设计参考**，不是可替换的二进制；换引擎的冲动先按住。
5. **README 明说没开源的三件事**：KV cache 系统、VE/Prefill/Decode 分离、prefill 与 decode 的 DP 副本负载均衡。
   → 开源的是「单实例引擎内核 + 集群并行骨架」，不是可对外营业的服务栈。
6. **四条与我们已记录痛点直接对应**：
   - async / zero-overhead batch scheduler（CPU 调度与设备执行重叠，默认开）↔ 我们的「CPU 池串行 + prefill 独占」；
   - **DSpark + confidence-scheduled verification**（按置信度分配验证预算，`DEEPINFER_ENABLE_MTP_VARLEN=1`、
     `DEEPINFER_USE_NEXTN_DECODE` 默认 5）↔ 我们「批槽无 MTP」与 `kVerifyMaxT=8`/`GMAX=8` 硬编码宽度；
   - **SWA bounded replay / YOCO prefill truncation**（只重放有界后缀，`..._KEEP_TOKENS=140`，
     后缀须覆盖 SWA 窗口 + 最大回溯长度）↔ 我们 TTFT 两分钟（停车快照被驱逐 → 每轮重读 ~238k）的**另一类解法**：
     不靠 prompt-cache 检查点，而是从模型结构砍 prefill 工作量；前提是模型本身带 SWA/局部注意力层；
   - **batch-invariant / 确定性采样**（`DEEPINFER_BATCH_INVARIANT=1`，仅 NVIDIA）↔ 我们 A/B 复现与压测的
     「同输入不同 batch 组成结果漂移」污染问题。
   另 **MoE EP + EPLB**（expert placement load balancer）对应 `docs/plans/GENERIC-MOE-ENGINE-PLAN.md` 的热点专家问题。

**踩坑（可复用的抓取经验）**：`mp.weixin.qq.com` 对 WebFetch 与 iwiki `fetch_url` 都返回「环境异常」验证码页
（final_url 跳到 `wappoc_appmsgcaptcha`），搜索引擎也不索引 `/s/<sn>` 短链；
**但本 Pod 本机 `curl` 带 iPhone UA 能直接拿到 3.4 MB 完整 HTML**，本地解析即可。
另：本 Pod **`raw.githubusercontent.com` 超时不可用**，取仓库文件要走 `api.github.com/repos/.../contents/<path>`（base64）。

**出处**：原文 https://mp.weixin.qq.com/s/5MEu2eeqNfBWXlkCKTd6sg ；
https://github.com/deepseek-ai/DeepInfer （README / git tree / languages / metadata 当场核实）；
`nvidia-smi` 当场查询硬件；知乎技术报告 https://zhuanlan.zhihu.com/p/2095560295930508953（未读）。
**全文整理见 `docs/reports/DEEPSEEK-DEEPINFER-2026-10-20.md`**（已备份 MinIO）。
**全程只读，未改服务配置、未重启服务、未打压测流量。**

## 2026-10-20 19:27 · DeepInfer 仓库精读 + 官方部署 Q&A：逐条可移植性判断

**问题**：继续深挖 DeepInfer（含仓库内 10 页《DeepSeek V4.1 Flash 推理部署 Q&A.pdf》），逐条对照我们的引擎与模型，判断哪些能搬。

**结论 / 关键事实**
1. **DeepInfer 派生自 vLLM**：`deepinfer/engine/arg_utils.py` 里写明 "Derived from vLLM; modified by DeepSeek"。
2. **prefill chunk 不固定，由时间预算反解**：每步定 `compute_quota_limit`（单位纳秒），用拟合二次代价模型
   `cost(q, c)`（q=序列 token 数，c=已算完前缀长）反解「32768 token 内正好吃满预算」的最大 q；预算还被 DP 组内 allreduce-MIN 压低。
   我们侧是**固定 `prefill_chunk`（默认 2048）**，无任何时间预算形式的调度量 → **这是可移植性最高的一条**（纯调度层，不依赖硬件）。
3. **prefill LB 按 context 分 4 tiers**（<16K / 16-64K / 64-256K / ≥256K），每档引擎台数按上轮 compute-quota 占比动态重分；
   **不做前缀亲和路由**（前提：KV 在独立 3FS，任何引擎能命中任何前缀）。
4. **decode LB 同时均衡两个维度**：请求数（决定批大小）与 KVCache 总长（决定注意力代价）；
   短请求 → 并发小且 KV 总长偏大的卡，长请求 → KV 总长小的卡。
5. **DSpark 变长验证实测**：每步验证 draft token 数 −30%（5.00→3.2~4.4），平均接受长度基本不变（3.29 vs 3.20），
   生产运行点（每 rank ~60 并发）折合 **+4~7% 吞吐**。逐请求长度在 **GPU 上由 tilelang kernel 重算**（CPU 只定总预算），
   因为异步调度下 `schedule(N+1)` 时第 N 步 confidence 尚未回传。
6. **SWA Bounded Replay**：只重算最近 128 token 且把注意力窗截断在重算段内 → 重算代价变成常数；
   decoder 层只重算 prompt 末尾 128 token → **总 prefill 计算量近乎减半**。
   前提 `n_prefill_layer < n_layer`（YoCo checkpoint 判据）。**我们引擎 grep 零命中 SWA/sliding_window，模型也无 SWA 层 → 照搬不可行**。
7. **EPLB 参数**：prefill 每卡 4 冗余专家、窗 100 步、1000 步重排（moe_lb 1.045~1.074）；
   decode 每卡 1 冗余专家（EP64 取整后每卡 7 专家）、窗 1000 步、10000 步重排（moe_lb 1.090）。
8. **KV 存储**：独立 3FS 约 100 PiB SSD、10+2 EC、LRU GC、TTL agent 3 天 / chatbot 7 天；
   3 天 TTL 会让 chatbot 命中率降 0.5~1%；换入用 **GDR/RDMA 而非 CUDA copy engine**（后者对 KV block 粒度拷贝性能差）。
9. **观测面可直接抄**：`stats_logger.py` 每步带 `sum_seqlen_qxk` / `max_seqlen_k` / `num_cached_tokens_gpu` /
   `num_verified_tokens` / `avg_verified_len` / `preempted` / prefill-decode 时间占比 / MFU。
10. **容错取向**：重启是分钟量级，**容错靠配额冗余 + 请求级重新路由，而不是靠缩短重启时间**；配额占用率 >0.8 禁止缩容。

**判断（挑三件事动手，都不停服）**：① 先做「每步自描述」观测面；② prefill 改「时间预算反解 chunk」；
③ 批内逐请求投机验证预算（先只记录 `avg_verified_len`）。
**明确不做**：Wide-EP（3 卡无收益）、独立 KV 集群、YoCo/SWA 重放、换 DeepInfer 二进制。

**出处**：`~/docs/reports/DEEPINFER-DEPLOY-QA-NOTES-2026-10-20.md`（全文 219 行，已备份 MinIO）；
原文快照 `~/docs/references/deepinfer-2026-10-20/`（PDF / qa.txt / README.md / yoco.md，已备份 MinIO）；
我们侧 `src/program/generate.cpp:180,368,1414,1689,1756`、`include/strata/spec/controller.hpp`、
`src/spec/draft_policy.cpp`、`Strata/serve/server.py:2594-2601`。**全程只读，未改配置、未重启服务、未打压测流量。**

## 2026-10-20 22:43 · 第二轮：DeepInfer 源码级实现（prefill 节拍 / MTP 变长预算 / perf 曲线测量）

**问题**：「目标就是探索一下他们的方案」——不停在公告与 Q&A 摘要，落到开源代码看他们具体怎么实现。

1. **NORMAL 引擎（单实例、prefill+decode 混跑，= 我们的形态）里 prefill 不是「有活就插」，而是按 decode step 节拍插入的事件**。`csrc/engine/scheduler.hpp`：`prefill_attempt_interval=70`（ALL 点：全员都有 prefill 活才集体做，防止一个 rank 干活、其余 rank 陪跑空 prompt step）、`prefill_any_attempt_interval=100`（ANY 兜底点：任一 rank 有活就做，孤立 rank 最多等 100 步）、`GPU_CACHE_PREFILL_WATERMARK = 0.9f`（KV 显存水位高于 0.9 就不插 prefill）；插完还有一轮 `_dp_allreduce(..., MIN)` 投票，防空 prompt step 死循环（硬做空 prompt step 会把 decode_step 清零 → running 满 + waiting 非空就死循环）。出处：scheduler.hpp:80, 296-341；environ.py 默认 70/100。**判断：本次对我们最直接可用的一条**——纯调度层策略，不依赖硬件、模型结构、集群。
2. **Q&A 里最吸引人的「纳秒时间预算 + 拟合二次代价模型 cost(q,c) 反解 chunk」不在开源里**：对 scheduler.hpp grep `quota|cost|quad|sqrt|discriminant` 零命中，它在未开源的 Prefill LB 里。开源侧只有 token 预算：`max_num_batched_tokens = max(max_model_len, 4096, max_num_seqs*(1+nextn))`、`seq->schedule.chunk_checkpoints` 强制切点、`cp_align`、`lookahead_tokens`、`prefill_max_sum_seqlen_k`（默认 2^30 即不限制）。→ 代价模型必须我们自己拟合。
3. **DSpark 变长验证整套是 CPU C++，可照搬**（`csrc/engine/mtp_varlen.hpp`）：两个扁平队列——用 N−2 轮接受率扫「边际吞吐最大」的总预算 `best_bsz`（`time = baseline + slope*graph_size + sampler_ms`，`throughput = expected/time`），再用 N−1 轮接受率把预算逐请求分配成 `seqlens_q`；GPU 版（`schedule_mtp_varlen.py`）用 **cumprod** 排序取 top-k，`seqlens_q = 1 + wins`。两个运行时估计器都是滑窗中位数：forward 窗 5000 / 最少 10 样本（按 graph size 分桶再取中位数），sampler 窗 9 / 最少 5。
4. **perf 曲线测量协议**（`model_runner.profile_decode_perf`）：对每个 CUDA graph batch size 倒序跑，1 warmup + 5 次 replay 计时（CUDA Event + `dist.all_reduce` 求组内均值），去掉首尾各 1 取截尾均值；结果按 `sha256(model + parallel_config + batch_sizes + time_unit)` 落盘缓存，文件名 `<hash>.<uuid>.<ts>`、TTL 3600 s、rank0 写后 `broadcast_object_list` 给其余 rank。→ 我们要做「batch → step_ms」曲线可以直接抄这套协议（含缓存与 TTL 处理）。
5. **默认值反差**：`DEEPINFER_ENABLE_MTP_VARLEN` 与 `DEEPINFER_ZERO_OVERHEAD_SCHEDULING` 默认都是 False（由 engine_type 显式打开），而 `DEEPINFER_USE_NEXTN_DECODE=5` 默认开——即「投机默认开、变长验证默认关」。`DEEPINFER_MAX_BACKTRACKING_TOKENS = max(v, nextn+1) = 6`，这就是 SWA 环 128+6=134 的来源。
6. **取源码的路子**（本机 raw.githubusercontent.com 不可达）：`api.github.com/repos/deepseek-ai/DeepInfer/git/trees/main?recursive=1` 拿全树（342 blobs，未截断），再 `/contents/<path>` 取 base64。快照在 `docs/references/deepinfer-2026-10-20/code/`（21 个文件，读它们的 python 一律 `-I`）。

**全程只读**：未改引擎配置、未重启服务、未打压测流量。

## 2026-10-20 23:44 · 第三轮：把 DeepInfer 的三个决策形状落成代码并实测

**问题**：借 DeepInfer 的做法优化我们的架构，写清文档与测试效果。

1. **实现了什么**（全部默认关闭，未重启、未上线）：
   - `SSEStraBed/include|src/strata/spec/varlen_budget.*`：cumprod 期望 token、`plan_uniform`（整组 L）、`plan_varlen`（逐请求前缀分配）、
     `StepTimeEstimator` + **`StepCurve`（按 rows 分桶中位数 + 插值/外推）**、`AcceptanceTracker`。
   - 引擎：`--mtp-varlen`（generate.cpp:405/1432/5768-5785/5846-5849）在批链窗口按实测接受率与代价曲线选 L；
     `STRATA_STEP_STATS`（include/strata/program/step_stats.hpp）每窗口一行 JSONL（rows/drafts/accepted/step_ms/ctx）+ 每 prompt 一行 prefill。
   - 工具：`tools/varlen_plan.cpp`（喂接受率/曲线，比较 uniform vs varlen）、`bench/deepinfer/profile_decode.py`（外部 profile 协议）、
     `bench/deepinfer/analyze_steps.py`（StepStats → 曲线 + 逐位置接受率 + 上下文分桶）。
2. **实测数字**：8091 `/metrics` 真实边际接受率 **0.738**（5143/3796），单请求中位 0.762–0.801（区间 0.641–0.890）；
   8092 外部测量并发 1/2/4：每请求 **152 / 72 / 44 tok/s**，墙钟聚合 **84 / 87 / 97 tok/s**（共享 GPU，逐次 ±40%）；
   TTFT 0.16/0.26/0.54 s；另一次 c=4 的 9.88 s 与日志中 5.3k token prefill 同期，属排队不是稳态。
3. **决定性发现**：**代价曲线的形状决定投机是否划算**。同样 p=0.8、S=2，线性代价 `0+5×rows` 时最优是 L=1（不投机），
   次线性曲线 `1:7,2:8,4:10,8:14` 时最优是 L=4。DeepInfer 用 profile 表而非拟合直线，原因就在这里。
   → `--mtp-varlen` 的先验改用兼容先验 `{12ms, 2ms}`（均匀接受率下复现今天的 L=4/L=4/L=2），真实曲线等引擎内 StepStats。
4. **收益口径**：8 行预算、4 并发，真实接受率（0.890/0.839/0.727/0.646）下逐请求分配比统一 L **+2.1%** 期望 token
   （中位段 +0.8%）；异质分布（0.92/0.88/0.55/0.45）**+9.2%**。不是固定提速，是分布自适应。
5. **产物**：新二进制 `archive/binaries/strata-ssebed-0.1.38-b1-mtpvarlen-20261020-r2`（sha 2be4dd32…）；
   `build/strata` 已还原现网 sha `3d3f3c9d…`；单测 `varlen_budget_test` 11 组全过。
6. **上线前提**：两条 lane 的 config 指向同一个 exe，只换 B 必须先给新二进制独立路径，再改 B 的 config/args 并重启；
   换引擎=重启，**按红线待用户同意**。方案与回滚见 `docs/reports/DEEPINFER-ADOPTION-2026-10-20.md` §6。

## 2026-10-21 13:05 · 第四轮：只换快道，做前后对比（含两次失败实现）

**问题**：「可以重启，写清改了啥/参考了啥/为啥这么改、别处为啥不适配；做好实验对比前后区别；先只换快道。」

1. **现网状态**：快道 8091 = `ssebed 0.1.38 build 5`（`sha 4e5ce24d…`，`--mtp-varlen` + `STRATA_STEP_STATS`）；
   普道 8092 全程未动（`legacy build 0`）。回滚 = 改 `strata-a.json` 的 exe/args + `supervisorctl restart strata-a`（冷启动 6–8 min）。
2. **实验结果（同二进制、只差 `--mtp-varlen`，warm）**：
   - 改造前：c=1 **178.0/177.5**、c=2 **53.7/55.6**、c=4 **28.6/28.7** tok/s/请求；
   - build 3（L 自由变化）：c=2 55.1/48.8/49.7(n=48) → **−10%**；
   - build 4（按 L_max 造草稿）：c=2 **37.1/40.1**/50.8(n=48) → 大幅回退；
   - **build 5（深度斜坡）：c=1 180.2/177.6、c=2 51.2/55.4/55.2(n=48)、c=4 30.6/29.4 → 与改造前持平**。
   - 对照 `--mtp-max-t 2`（强制 L=2，B 道空闲）：c=1 138.5（**证明 S=1 必须 L=4**）、c=2 58.5/54.6、c=4 30.8。
3. **根因（引擎内 `keeps` 统计，S=2 的 L=4 窗口逐位置接受率）**：改造前 0.83/0.54/0.49；build 3 **0.79/0.21/0.18**；
   build 4 **p0 0.14**（S=4 p0 0.05）；build 5 0.88/0.67/0.47。
   - build 3：`mtp.cpp:1185-1190` 的 `draft_batch` 只按本轮 L 生成草稿、其余**清零**；窗口加宽后校验 0 → 必然拒绝 →
     规划器自锁在 L=2。**修复：`L ≤ 上轮草稿深度 + 1`（缩短不受限），不改 `draft_batch`。**
   - build 4：按 L_max 造草稿时，`mtp.cpp:1163-1177` 的追赶行会写「接受位置之后」的 cell，草稿链被污染 → p0 崩塌。此路不通。
4. **收益口径**：窗口层面 S=2 的 L=2 是 0.1915 tok/ms，L=4 是 0.1804（**+6.2%**），但端到端只兑现 +2~5% ——
   差额是**窗口外固定开销**（草稿图启动/row_stage 拷贝/BT 输出+fflush），我们的口径只能量墙钟，DeepInfer 用 CUDA Event 量窗口内。
5. **结构发现**：代价不是 `rows` 的一维函数 —— 同一时段 `S=2, rows=8` 31.2–37.2ms，而 `S=4, rows=8` 29.8–31.7ms。
   DeepInfer 的 profile 表本来就是 `(num_seqs, num_batched_tokens)` 二维；我们的 `StepCurve` 只有 rows，需升级。
6. **踩坑（测量）**：快道重启后第一轮必然偏慢（专家 arena 冷，c=1 只有 127 tok/s），必须 warmup 后再测；
   两卡共用宿主 CPU 池，普道 8092 的真实请求会污染快道，B 空闲时同一配置 c=4 就高 7.7% → 每次测量都记录 B 的请求/token 作协变量
   （`bench/deepinfer/ab_lane.py`）。
7. **文档**：`docs/reports/DEEPINFER-VARLEN-2026-10-21.md`（改了啥/参考了啥/为啥/别处为啥不适配/实验全表/回滚）。

## 2026-10-21 14:20 · 第五轮：TTFT 对比、普道同步与存档、对外 502 修复

**问题**：「TTFT 对比呢？现在可以同步到普道并做一次存档版本（只在你确定没问题时）；文档写清当前版本；book 也要。」
随后报障：对外 `{"error":{"message":"rag proxy: upstream failed: URLError(ConnectionRefusedError(111))"}}`
并说明 `ai.ssemarket.cn` 就是现在跑的网页。

1. **TTFT 结果（A 道，同二进制只换 build，随机 nonce 强制缓存 miss，`ttft_probe.py`）**：
   | 长度 | build 1（无 varlen） | build 5（--mtp-varlen） |
   |---|---|---|
   | 8k 冷 prefill | 2.81 s | 2.55 s |
   | 32k | 9.21 s | 9.15 s |
   | 128k | 37.47 s | 36.94 s |
   结论：**持平**；128k/37 s 反算 ≈3.4k tok/s，与历史 3350 tok/s 公式吻合；8k 的差在噪声内。
   短提示 TTFT（c=1/2/4）：0.356/0.541/0.945 → 0.277/0.469/0.893 s（基线时 B 道更忙，慎作结论）。
2. **同步普道**：`switch_engine.py switch ssebed --lanes b --yes`（drain 30 s → stop → start → `/health` → 冒烟），
   13:52 完成。B 道 profile 补了原先缺的 `fit_max_tokens: true` + `--mtp-varlen` + `STRATA_STEP_STATS`。
   现在两条 lane 同一份二进制 `ssebed 0.1.38 build 5`、sha `4e5ce24d…`、参数完全一致。
3. **归档**：`archive/binaries/strata-ssebed-0.1.38-b5-mtpvarlen-20261021/`（二进制 + BUILD.json）、
   `archive/patches/mtp-varlen-2026-10-21/`（源码 + sha256 + 逐 hunk 说明 + 回滚）——因为 `SSEStraBed/` 在 .gitignore 里。
4. **对外 502 的根因与我引入的问题**：重启快道期间，`model_routes` 把 `qwen3.8-flash` **钉**在 8092 上，
   而 8092 也在冷启动/或请求命中正在重启的 lane → 网关直接 502。修复两层：
   (a) 立即把 lane 拉起来；(b) `rag_proxy._proxy()` 加**上游 failover**：`except URLError` 且未写客户端任何字节时，
   改投另一条 live sibling 重试一次（`upstream_alive()` TCP 探测 2 s 缓存，`failover=False` 防循环，计数器 `upstream_failover`）。
   验证：隔离实例（8099，死端口）自动改投；切 B 期间实测 19 次改投、`upstream_errors 0`、网站没断。
5. **普道冒烟（重要口径）**：空闲单流 115 tok/s、TTFT 0.30 s；一旦有真实用户会话并发，掉到 15–19 tok/s、
   SSE 间隔 ~55 ms。引擎统计给出证据：`{"kind":"batch","slots":2,"rows":2,"drafts":0}` —— 两槽以上走 batch 路径、
   **不做 MTP**，一步 ~36 ms。这是已知问题「批槽无 MTP」，改造前后都存在，**不是 build 5 的回退**。
6. **当前版本一页纸**：`docs/reports/CURRENT-VERSION-2026-10-21.md`（拓扑/版本/sha/参数/新增项/实测/已知问题）；
   book 同步：10 章 §10.3.1、14 章 14.4/14.5、16 章 §16.4/§16.6、19 章、附录 B、README 取材清单。

## 2026-10-21 14:54 · 第六轮：为什么 `--mtp-varlen` 没有差别，DeepInfer 架构对我们有没有优化

**问题**：「为啥没有差别呢，DeepSeek 开源的架构对当前的没有优化吗，请你研究一下。」

**结论：不是实现错，也不是 DeepInfer 没用；它的收益上限本来就是个位数百分比，而且落点不在我们的瓶颈上。**
四层原因（全文 `docs/reports/WHY-NO-GAIN-2026-10-21.md`）：

1. **可撬动空间只有 ±5%**。快道 A 按 `(S, rows)` 分桶（`step-stats-a.jsonl`，warm）：
   S=1 的 L=1/2/3/4 → 140/179/**201** tok/s（最优点就在 L=4，GMAX=8 与 `chain_rows_per_slot=min(--mtp-max-t,--spec)=4` 的边界）；
   S=2 → 174/182/173（差 5%）；S=3/4 → 只有 L=1/L=2 可选。边际成本每行 +2.2~2.7 ms 换 +0.8~0.9 token，
   性价比 1.4~1.6 几乎恒定 → 决策面是平台不是尖峰，改造前的固定 L=4 已在最优点附近。
2. **真实网页流量根本不走投机**。前端默认 `temperature:0.6`（`Strata/serve/webapp/src/store.js:9`，`ChatView.vue:143-146`）；
   引擎 `sl.sampled = !req_sp.greedy`（`generate.cpp:7192`），`chain_step` 首行
   `if (sl.active && sl.sampled) return one_token();`（`generate.cpp:5821-5823`）→ **一个采样槽就把整组退回逐 token 窗口**。
   现场计数：B 道 29,117 窗口里 batch 29,058 / chain 59（那 59 个是 `temperature=0` 探针）。
3. **瓶颈是专家搬运，不是验证窗口**。B 道 `strata batch: 850 windows, avg 2.00 rows, 22.26 ms` =
   run 21.39（`GPU-reach wait 13.53` + `CPU experts 7.17`）+ commit 0.84 + emit 0.02；每层窗口 VRAM 命中 15.34 / CPU 2.51 / PCIe 2.09。
   即 ~94% 时间是等专家到位 → 解码是 **PCIe/主机内存带宽 bound**，所有「少算一点」的优化（投机、varlen、FP8、kernel）都没杠杆。
4. **DeepInfer 的「快」来自整套软硬件共设计，逐条搬不动**（源码 `docs/references/DeepInfer-src/`）：
   仅 SM100/昇腾 950（`README.md:33-36`）；权重硬校验 FP8+MXFP4 block32/ue8m0（`hugging_face/config.py:254-264`），Ada 无 FP8；
   参数必须 `p.is_cuda`、**无 CPU offload/分页**（`model_executor/model_loader.py:76-79`）；纯 EP 无 PCIe 回退（`v4_modules/moe.py:94`）；
   FlashMLA/DeepGEMM/DeepEP 为空子模块；PD 分离未开源（`README.md:38-42`）。**它自己的 varlen 收益就是 +4~7%**
   （验证长度 5.00→3.2-4.4，接受长度 3.29 vs 3.20，`doc/inference_deployment_qa.md:245-258`，EP64/DP64、每 rank ~60 序列）。

**顺带量出的两个数**（A 道，128 token，交替顺序，warm）：
- 4 路并发：贪心（链式）**111.3 tok/s** vs 采样（逐 token）**92.8 tok/s** → **+19%**；单路串行 87.4 vs 85.0（+3%）。
- 同时段同态直接对照：chain S=4 rows=8 → 174.0 tok/s（38.47 ms / 7.04 tok），batch S=4 rows=4 → 168.7 tok/s（22.95 ms / 4 tok）——
  **多槽时「开投机」与「纯批」每 token 成本几乎相等（5.47 vs 5.74 ms）**，这就是「没有差别」的画像。
- 单路贪心引擎日志：128 token / 1192 ms（107 tok/s）、drafts accepted 76/116（65%）、解码期专家命中 92.4–94.0%。

**下一步排序（都需重启=需用户同意）**：① 专家取数（94% 时间，先按层量 multi_misses/pcie/ms_wait）；② 让采样请求也能走链式投机
（verify.cpp:1822 目前只有 argmax；`STRATA_SPEC_COUPLED` 已有雏形，实测上限 +19%）；③ 量「窗口外开销」（单路 87 tok/s vs 窗口内 174–240）；
④ 代价曲线升级二维 `(S, rows)`；⑤ ragged verify 排最后。**不建议**换 FP8/稀疏注意力路线（等于换模型换硬件）。

## 2026-10-21 15:30 · 第七轮：并发阶段 prefill 串行——源码定位、参考方案、论文清单

**问题**：「我们的瓶颈是在并发阶段，prefill 仍然是串行的……研究有没有更好的解决方案，能够获得并发方案的参考。」
随后补充：「你要啥论文我可以给你下载，你要列个清单文档出来。」

**产出**：`docs/reports/PREFILL-CONCURRENCY-STUDY-2026-10-21.md`（事实/参考/决策三层）、
`docs/references/PAPER-REQUEST-2026-10-21.md`（12 篇，P0 4 篇）、book 09 章 §9.8 增补。

1. **串行点的更正**：生产是 `--batch 4`，`Service.fifo` 不生效；真正的串行是 serve 控制线锁 `self.ctl`
   （`server.py:657-660` 注释、`:670` 取、`:735-747` 释放，覆盖 BGEN→BADM=读 prompt）。
   引擎侧没有 prefill 队列：`generate.cpp:6075` 单线程 `for(;;)`，`6831/6837` 同步 `sp.run`，
   返回前不跑 decode 窗口。
2. **粒度**：`--prefill auto` 实际 8192（日志 `prompt chunk auto: 8192 tokens`）；让出段 = 2×8192=**16384**，
   段间跑 12 窗口（`6773-6791`），一次 refill ~300 ms（`6770-6772`）。
   → 配置级杠杆 `STRATA_PREFILL_YIELD_CHUNKS=1`：断流 ~4.9s→~2.4s，prefill +~12%。
3. **prefill 不走 CPU 专家池**：`prefill.cpp:1971-2034` fused 专家 + staging ring DMA；`drive_pool` 只在 decode。
   与 decode 争的是 PCIe/DMA 与**专家缓存槽**；prefill 借走 90%/85% 槽（`4415-4420`、`6666`），
   借出期间 decode 不能碰（`host_res=kNotResident`，`6697-6703`）——这是「交错」难做的根因。
4. **没有 batched prefill**：`sp.run` 绑定单个 `ss` 会话，`in_lines` 无界 FIFO；BGEN 无空槽直接报错。
   `kVerifyMaxT=8` 只约束 decode 验证行数，prefill 不撞这个天花板（书稿 09 §9.5 该说法待澄清）。
5. **服务层**：无信号量/线程池/有界队列，`ThreadingHTTPServer` 每连接一线程，并发上限=引擎 `--batch 4`；
   路由按模型钉死（flash→8092、fast→8091，`config.json:68-84`）；真实队列 `engine.waiting` 未导出，
   `/metrics.live.queued` 在 batch 模式下恒 0；停车缓存 hit/miss/evict 无计数。
6. **重试放大**：400 overfit 会整包重发（再 prefill）；600 s 超时 failover 会把巨型 prefill 复制到另一条 lane
   （`rag_proxy.py:1802-1817/1834-1845`）——准入改造时要给巨型请求关掉跨 lane failover。
7. **参考方案**：调度派（Sarathi-Serve 2.6/3.7/5.6×、vLLM V1、SGLang、DeepInfer 的 ALL/ANY+水位+预算）
   可搬；架构派（DistServe 7.4×、Mooncake）需要 KV 高速通道，2 lane 无 NVLink 暂不做。
8. **落地排序**：P0 网关准入（秒级）→ P1 观测 → P2 缓存参数+yield=1 A/B → P3 prefill 工作线程
   → P4 prefill/decode 混合批（源码级）→ P5 PD 评估。

## 2026-10-21 15:55 · 第八轮：prefill 并发实施方案（设计稿）

**问题**：「你先等等，我先弄文章，你出一个文档写好方案。」

**产出**：`docs/plans/PREFILL-CONCURRENCY-PLAN-2026-10-21.md`（v0.1 设计稿，未实施）。

1. **一处事实更正（重要）**：「prefill 借走 90%/85% 专家缓存槽」只在**多 stage（`--layer-split`）**成立；
   生产两条 lane 是单 stage，`pf_parts[0].first=-1`（`generate.cpp:4579`），`lend()` 跳过 `first<0`（`6666`）
   → **单卡 lane 不借槽、无 refill 成本**，prefill 用自己的 scratch。单卡真正的耦合是**专家缓存驱逐竞争**
   （prefill 流式专家挤掉 decode 92–94% 的热命中）。报告 §1/§3/§4、书稿 §9.8、memory 均已更正。
2. **方案分层**：A 网关准入+缓存经济（秒级/一次重启）→ B `YIELD_CHUNKS=1` 粒度 A/B → C 多请求 prefill 公平队列
   （2 路 chunk 轮转）→ D 流级并发（退路）→ E 混合批（主线：E1 只合并同层 MoE、attention 仍分开）→ F PD（不做）。
3. **quantum 固定 8192 的理由**：prefill 每块有 ~0.9 s 固定开销（298 token 1.04s vs 7979 token 2.23s），
   块越小越亏；Sarathi 式 512-token 预算要等 E2 统一批之后才有意义。
4. **E1 的必要改动**：decode 路径从「一次跑完所有层」拆成可按层推进；行元数据扩展 `(job_id,pos,kv_page)`；
   MoE 输出按行区间回写；greedy 逐 token 对照（不接受数值漂移）；`--mixed-batch=off|moe` 开关默认 off。
5. **成败关键**：专家缓存分区/弱插入策略；验收线 = 4 路解码 + 1 路 128k 时聚合 ≥+15%、命中率降 ≤5pp，
   否则停止 E1（决策点在 M4 原型）。
6. **工作量**：M0 观测 2–3 人日、M1 准入+缓存 1–2、M2 yield A/B 0.5、M3 公平队列 5–8、
   M4 E1 原型 15–20、M5 生产 A/B 5。所有引擎改动需重启，落地前单独确认窗口。

## 第九轮 · 2026-10-21 16:05 · 网页"平均首字"与压测口径、并发聚合为何上不去

**Q1：网页端的"平均首字"和压测数字对不上。**
A：这是两个不同的样本/口径，不是数据错。
- 网页：`/infer/stats` 的 `AVG(ttft_ms)`（`infer_store.py:785`），统计**全部流量**的算术平均。
  7 天 41,393 条：均值 **319 ms**、中位 **12 ms**、p90 96 ms、p99 **6.5 s**、最大 **165 s**。
  82% 是 ≤2k token 的短回合（前缀/停车缓存命中，12 ms 级）；64k–128k 段均值 ~2.4 s。
- 压测：冷提示 / 有排队 / 指定并发，测的是**最坏路径**。README 口径的 8 路 128k TTFT 165 s 属于这一类。
- 同一个量在 DB 里能差 4 个数量级（12 ms ↔ 165 s），所以**页面的均值只适合看趋势**；
  定位要用分位数与按 prompt 长度分桶。出处：`infer_store.py:596-616`、`rag_proxy.py:1775-1778`。

**Q2：并行到底怎么并行，为什么并行时 token 生成这么慢、聚合还不如单路？**
A：并行是"时间片轮转"，不是并行计算。
- serve 每请求一个线程，`self.ctl` 锁让 `BGEN→BADM` 一次只过一个（`server.py:670/735-747`）；
- 引擎单线程（`generate.cpp:6075`）：一次 decode 窗口 = 全 48 层一遍；prefill 是同步 `sp.run`
  （`6831/6837`），期间只有段边界 yield 12 个窗口（`6773-6801`）。
- 窗口内部是**逐层 GPU↔CPU 握手**：GPU 跑到门铃 → host 自旋（`wait_flag_ge`，单核）→
  CPU 池算 miss 专家（`verify.cpp:1797`）→ 放行下一层。**CPU 段在关键路径上。**
- 实测（`step-stats-b.jsonl`，9.3 万窗口）：1/2/3/4 槽窗口 13.8/22.0/28.2/46.5 ms，
  聚合 72/91/106/**86** tok/s，单请求 72/45/35/**22** tok/s。窗口 ≈ 11 ms 固定 + 7.05 ms×行数
  → 聚合上限 ≈ 142 tok/s，与开多少路无关。
- 三个原因：① 每行路由不同 → miss 专家随行数线性增长，且全在 CPU 关键路径（占窗口 33%→40%）；
  ② 前端默认 temp=0.6 → `sl.sampled` → `chain_step` 直接退回一次一行（`generate.cpp:5821-5823`），
  MTP 链窗整条关闭（贪心 S=4 时 174 tok/s）；③ prefill 独占 GPU 数秒并驱逐 decode 热专家。
- 对案：混合窗口（SARATHI decode-maximal：融合线性/MoE、attention 分开）把 prefill 行塞进 decode 的空隙；
  采样链窗（最便宜）；miss 专家改走 PCIe+GPU。方案：`docs/plans/PREFILL-CONCURRENCY-PLAN-2026-10-21.md` §2/§4/§5。

## 第十轮 · 2026-10-21 · 通读引擎源码建立代码知识库：解码到底卡在哪一行

**Q1：glm 多并发时单请求变慢，是不是引擎架构/代码有问题？**
A：是代码问题，但**不是"GPU 没算满"那种**——恰恰相反：**GPU 在解码时基本是闲的**。
- 引擎自报的分项账（`strata batch:` 行）：`92.50 ms/窗口 = run 90.86（GPU 等主机抬门铃 21.14 + CPU 专家 70.00）+ commit 1.63`。
  即 **76% 的时间是主机 AVX2 线程池在算未命中专家，23% 是 GPU 空转等人**。
- 机制：`verify.cpp:1755-1827` 每层一次门铃握手——GPU 到第 l 层停下、主机自旋、
  **同步**把该层所有 miss 专家算完（`verify.cpp:1797`），再抬 flag 放行（`:1811`）。48 层 = 48 次。
- 所以"提高 GPU 利用率"在这里没有意义；要砍的是**主机侧的专家量**与**串行握手**。
- 完整代码库+方案：`docs/plans/ENGINE-CODEBASE-AND-OPTIMIZATION-PLAN-2026-10-21.md`。

**Q2：专家三级是怎么分的，天花板在哪？**
A：每层每窗口在 `expert_source.cpp:3330-3450` 做三分决策，三个硬上限：
① `--pcie-frac`（生产 0.55）；② **`kStagingBlobs = 16`**（`include/strata/core/verify.hpp:357`）——
一个窗口最多 16 个 miss 专家能走 PCIe 快档；③ 代码里 `fetches < 64` 的数组上限。
越过 16 的部分**全部落到 CPU 档**（约 95 µs/对），这是并发一高就慢的直接原因。
显存代价：16 槽 ≈ 30 MB（`Verifier::init` 按 `max_t` 只分配一次），扩到 32/64 分别 +30/+90 MB，启动还剩 1182 MiB。

**Q3：prefill 为什么插不进解码？**
A：两个原因叠加：
① `sp.run` 是同步调用（`generate.cpp:6831/6837`），只在段边界让出（`6773-6804`，默认 2 chunk=16384 token ≈ 4.9 s）；
② **生产走的是 MMQ 专家路径，每层一次 `cudaStreamSynchronize`**（`prefill.cpp:2055`）。
本包所有层的 gu/d 类型都被融合 kernel 覆盖（`moe_fused_iq.cu:1089/1095`），
`fused::available()` 要求 cc≥80（`moe_fused.cu:415`，4090D=8.9 满足），
**只差 `STRATA_PF_FUSED=1` 这个 opt-in 开关**（`moe_fused.cu:428`）——生产 profile 没设，日志里也没有
`prompt experts on the fused int8 kernels`。
另：`read_windows`（`generate.cpp:6554`）**已经实现"用解码窗口读 prompt"**，只是被 `--short-read 64` 限死；
M3（prefill 与 decode 同窗）就是从这条既有路径推广。

**Q4：并发退化具体是哪几行？**
A：四个：
① `L_max = min(4, kVerifyMaxT/S)`（`generate.cpp:5781`）——4 路时投机额度 4→2；
② `if (sl.active && sl.sampled) return one_token()`（`:5823`）——一个非贪心槽让整组退化为逐 token
（网页默认 temp 0.6 ⇒ 真实流量基本没吃到投机；实测 chain 194 tok/s vs batch 82 tok/s）；
③ 槽间完全同步，没有"这轮这个槽先不算"的机制；
④ 外加 prefill 独占 GPU（`sp.run`）与控制线锁（`server.py:412/670/747`）。
实测（lane A）：chain 1/2/3/4 槽每请求 **194/121/82/67 tok/s**；物理上 4 路应保留 60%。

**Q5：`--pool-workers 13` 合理吗？（先答错后翻案，以此为准）**
A：**合理，保持 13**。我最初按 cgroup 配额 32 CPU（`cpu.max = 3200000/100000`）推断"13 太少、应加到 24"，
**读 `~/Strata/run-iq3_xxs-a.sh` 后翻案**：两条 lane 都用 `taskset` 钉核——A=`TASKSET=0-15`、B=`38-53`，
每 lane 只有 **16 核**；13 个 worker + 主线程 ≈ 14 线程已接近打满 16 核，再加 worker 只会互相抢核
（2026-10-07 就有过 24 worker 抢核把 solo 打到 9.3 tok/s 的实证）。
所以 CPU 专家那 76% 只能"少做"（提命中率）或"换档"（PCIe/GPU），**不能靠加线程**。
→ 结论变化：M0 第一项不再是调 pool-workers，改为 greedy 默认（零成本）+ `kStagingBlobs 16→32`。

## 第十一轮 · 2026-10-21 19:10 · 完善《挣出来的服务质量》：两个读数陷阱与全书口径复核

**Q1：`strata batch:` 汇总行的 `per layer-window` 三项（CPU experts / VRAM hits / PCIe）能直接和 `avg N rows` 对账吗？**
A：**要看打这行的是哪条路径，两条路径的 `avg rows` 不是一个量。**
- `batch_step`：`bt_rows += S`（`generate.cpp:5683`）→ `avg rows` = **每窗口行数**；
  `CPU+VRAM+PCIe ≈ rows × 10` 成立（10 = 每行每层路由的专家数，48 层 / 48 已约掉）。
  样本 `~/Strata/strata-iq3_xxs-a.log:200`：`avg 2.84 rows`，`5.38+18.36+5.41=29.15 ≈ 2.84×10`。
- `chain_step`：每提交一个 token 就 `++bt_rows`（`:5894`）→ `avg rows` = **每窗口提交的 token 数**，
  分母实际是 `S × L`。拿 `×10` 去对账会得到 >100% 的命中率（我曾按此读出"命中 220%"的假象）。
- 读法：先看这行是不是 chain 打出来的（窗口毫秒在 90 ms 量级、`avg rows` 常 >4），再用对了的分母。
  写进书里：20 章 §20.4 字段表 + §20.9 小结、07 章 §7.12、引擎方案文档 §12.3。

**Q2：写书/复核时怎么确认一个历史性能数字还有效？**
A：**数字要落到"可复现的锚"**——文件名 + 行号 + 一条能重跑的命令。
`92.50 ms/窗口` 的分解来自 `~/Strata/strata-iq3_xxs-a.log:200`（2026-10-17 的日志，
`grep -n "strata batch:"` 命中 2 行，`:223` 是另一条 32.69 ms 样本）。
该日志里的引擎是当时的上游构建，**不代表现在的 b5**，但"76% 主机算专家 / 23% GPU 等门铃"的结构性结论
在 b5 上仍成立（step-stats 复算相同比例）。→ 结论：**结构性结论可以跨版本引用，绝对毫秒数不行**。

**Q3：这一轮核正了哪些过期信息？**
A：四条（都按书里写作约定 2 留在原处加注，不静默改）：
1. 两条 lane 都跑 `SSEStraBed`（`ssebed-0.1.38-b5`，`--mtp-varlen`），源码行号从此对两条 lane 一致；
2. `--batch-groups` **两条 lane 都是 1**（曾是 A=1 / B=2）；
3. ssebed 的 `strata-b.json` **已有 `fit_max_tokens`**；
4. `--pool-workers 13` 是对的（`taskset` 16 核已饱和），此前的"13→24"是我按 cgroup 32 核推错的。
出处：`book/README.md` "完成状态" 表；源码锚点本轮全部 `sed -n` 逐条复核过。

**Q4：书现在的规模与入口？**
A：`~/docs` 外的独立文档树 `~/book/`：**25 个文件、6385 行 / 410,104 字节**（`wc` 实测 2026-10-21）。
只想看"慢在哪、先动哪一行"：`20 → 21 → 19.8`（约 40 分钟）。

## 第十二轮 · 2026-10-21 19:20 · 方案落在哪、谁是"给人看"的版本

**Q：改造方案到底在哪？**
A：三份，分工不同（`docs/plans/` 下）：
- **`ENGINE-OPTIMIZATION-PLAN-M0-M4-2026-10-21.md`** ← **给人看、可直接排期的决定版**（509 行）：
  现状与红线、三条诊断、量化目标、M0→M4 每步的改动点/成本/风险/验收/回滚、排期、两套口径、待拍板事项。
- `ENGINE-CODEBASE-AND-OPTIMIZATION-PLAN-2026-10-21.md` — 证据底稿（带行号，1004 行）。
- `PREFILL-CONCURRENCY-PLAN-2026-10-21.md` — M3 的设计稿。
可读版是同目录外的 `book/20`（慢在哪）+ `book/21`（先动哪一行）。

**顺带核实的两个实施细节（写进方案前验证过）：**
1. **网页默认贪心是一项"一行前端 + 无停机"的改动**：`Strata/serve/webapp/src/store.js:9`
   的 `DEFAULTS.temperature = 0.6` → `0`，`bash tools/deploy.sh` 发布即可，引擎不用重启
   （server.py 每请求重读静态文件）。而 **API 客户端省略 temperature 时本来就是贪心**
   （serve 配置 `strata-iq3_xxs-{a,b}.json` 里没有 `sampling` 块，引擎默认贪心），
   所以 M0-1 只影响网页用户；真正的取舍是产品层（输出多样性 vs 速度）。
2. **`switch_engine.py dev run/rollback` 的 `--lane` 默认是 `b`**（`deploy/switch_engine.py:1022`），
   不写 `--lane a` 就会打到对外普道——这条必须写在每个操作步骤旁边。

## 第十三轮 · 2026-10-21 19:45 · 通用化改造与"自动最优放置"的真相

**Q1：现在的引擎有多"专有"？换成通用框架难在哪？**
A：不是"为 Qwen 优化过"，是**只认 Qwen**，三条独立证据：
1. 架构门禁 `gguf_reader.hpp:610` 只放行 `arch == "qwen4exp"`（调用点 `gguf_reader.cpp:65`、
   `native_head.cpp:38,126`、`native_dense.cpp:108`）；
2. **只从 GGUF 读 2 个几何量**（`expert_count`/`expert_used_count`，`generate.cpp:2087-2115`），
   层数/隐维/QSA/GDN/中间维全来自 `layout.hpp:40-72` 的默认值；通用键（`block_count` 等）
   只校验不消费（`gguf_reader.hpp:596-621`）；
3. **同一组几何有六份字面量副本**：`layout.hpp`、CPU 专家 `expert.hpp:41-52,56-59`（`BLOB=1,382,400`）、
   prefill `prefill.cpp:102`、CUDA `s2_expert_grouped.cu:54-60`、router `layer.cpp:581-584`、
   规划器 `plan.hpp`。→ **改几何 = 逐 kernel 改契约**，不是改结构体默认值。
另外：PLE 私有 sidecar（`ngram.hpp:40-56`，表 320,001,536 行）、MTP 特供草稿层（`mtp.hpp`、
`generate.cpp:3003-3010` 用 `static ModelGeometry draft_geometry{}`）、
serve 层的 Qwen 方言（EOS 248044/248046、turn 248045、图像 pad 248056、XML tool-call）。

**Q2：现在的权重放置是"计算+测试特化的最优"吗？**
A：**不是最优，是启发式**，准确说法分三层：
- **打包/量化是特化的**（IQ3_XXS + 自造 4-bin pack + 19 字段 index，`weights.cpp:40-52`）；
- **放置是数槽**：`slots = (free − reserve − mtp_bind) / max_blob`（`generate.cpp:3196-3218`），
  再用离线热度表 `expert-profile.bin` 截断；有 profile 时 `open_sized()` 按真实 blob 开槽，
  同显存约多装 **30%**（`:3290-3306`）——**但按层配额默认关**（`--expert-cache-per-layer` 默认 0），
  不开它按到达顺序填，实测命中率 **2.97%**；8 槽/层 21.4%、64 槽/层 70.4%（书 07 §7.5）；
- **有规划器但没接线**：`strata-plan`（`src/plan/plan_main.cpp` + `plan.hpp`）几何也写死 Qwen、
  池大小钉 5.943 GB，主引擎从不调用；没有 dry-run / plan-only。
→ 结论：**"能不能跑"目前靠 `cudaMalloc` 报错回答，"放得最优"目前靠一个固定的除法和一份离线表。**

**Q3：方案是什么？**
A：`docs/plans/GENERIC-ENGINE-AND-AUTO-PLACEMENT-PLAN-2026-10-21.md`（V2，606 行）。
- 通用化：**模型是数据不是代码**——ModelSpec（字段表）+ 架构注册表 + 声明式张量角色 + 层类型数组；
  新 target `strata-next`，旧 `qwen4exp` 原样保留（生产零回归门禁）。
- 自适应放置：`strata plan --model … --gpu-mem 22G --cpu-mem 180G --ctx …` → 可行性 + 槽方案 + 预测；
  目标函数 `T_window ≈ Σ_layer max(T_gpu, T_fetch) + T_cpu + barrier`，
  决策变量是每个 `(layer,expert)` 落 VRAM/PCIE/CPU/DISK；贪心按 `收益/字节` + 拉格朗日 λ 拉平每层边际收益；
  运行期沿用现有 `adapt_every=4/adapt_swaps=96/衰减 0.7/收益差≥1.5` 当执行器。
- **可复用零件比想象多**：真实 blob 尺寸（`native_experts.txt`）、热度（`--expert-profile-save`）、
  `open_sized()` 槽表、`--resident-*` 主机档、以及一套已有的切分成本模型
  （`--layer-split auto`：0.33 ms/层 @5080、未命中 ~190 ms、第 r 名质量 `(r+1)^-1.2`，`generate.cpp:2710-2863`）。
- 排期：G0 离线勘察 → G1 ModelSpec → **G2 求解器（离线、不碰 GPU，可与 M0/M1 并行）** → G3 通用 decode → G4 接通。
- 验收用现模型当 oracle：先证明求解器能**预测已知答案**——真实 blob + 生产热度下，命中率预测与 step-stats 偏差 ≤3pp，
  否则不进入 G3。

**Q4：为什么不用 vLLM / llama.cpp？**
A：vLLM/SGLang 假设权重放得进显存（专家 42.9 GB vs 单卡 24 GB）；llama.cpp 的 offload 粒度是整层/整张量，
没有"每层每专家按热度驻留"——而这正是 24 GB 跑 125B 级模型的全部价值。

**出处**：`docs/plans/GENERIC-ENGINE-AND-AUTO-PLACEMENT-PLAN-2026-10-21.md`；
`book/06` §6.6（dense 4.522 GiB）、`book/07` §7.1/§7.5（槽数与命中率）；
`CURRENT-VERSION-2026-10-21.md:49-50`（slots 6,427 / cache 10,651 MiB / free 1,183 MiB，多卡配置）。

## 第十四轮 · 2026-10-21 20:10 · "并行推理"的正解：层优先全局调度，以及它在这台机器上的带宽墙

**Q1：用户要的并行到底是什么？**
A：不是"prefill 和 decode 挤进一个窗口"，而是**层优先（layer-major）**：
第 0 层把所有活跃请求的第 0 层一起算，第 1 层再一起算……一层权重只上岗一次，
同一层专家取用做并集、所有请求共用。现有解码窗口其实是它的缩小版（S 个槽一起过 48 层），
差别在 S 被硬常量卡在 8，且 prefill 走独立路径。

**Q2：那直接把 S 开到 48 不就行了？**
A：不行，会撞带宽墙。每层 512 个专家，N 个请求各选 10 个，并集 `U ≈ min(512, 10N)`：
- N=4 → U≈40，命中率实测 88.6%，每层只取几个；
- N=48 → U≈480，缓存约 130/层，命中率掉到 ~25%，每层要搬 ~0.68 GB，48 层 ~31 GB/步；
  按 PCIe 22.7 GB/s，一步 ~1.4 s，每请求 <1 tok/s。
估算曲线（S=4/8/16/48）：每请求 ~55/13/3.7/0.7 tok/s。**并集随 S 线性涨、命中率跟着掉，
搬运量涨得更快**；所以深度必须现场扫，最优大概率在 4–16 而不是 48。
若模型能整个塞进显存，48 路当然赢——问题是 40 GB 塞不进 24 GB。

**Q3：那"根本的时间占用方式"怎么改？**
A：四件，按根本性排序（详见文档 §4）：
1. **拆行数上限**：`kVerifyMaxT=8` / CUDA `GMAX=8`（超了静默丢行）/ CPU `MAXT=8` / `--batch` 卡 8。
   不拆，S 连实验都做不了。改法：同一专家超 8 行拆多组，CPU 按 token 分块，verify 图按 T 动态建。
2. **层优先全局窗口 + 调度器接线**：`sched.hpp` 骨架已有、全仓库未使用；一次窗口装所有活跃序列、
   一次 router / 专家并集 / 门铃；prefill 改成"占几行的普通请求"。
3. **专家计算 GPU/CPU 分流**：GPU 空转 23%，PCIe 22.7 GB/s 与 13 线程 CPU 专家吞吐同量级，
   把部分 miss 专家 stage 到显存用 GPU 算，预期多出 30%–50% 专家产能。
4. **提高命中率**（放置）：同显存可多装 ~30%，按层配额默认关；这是减少分子的唯一办法。
流水（跨层）只能藏延迟（最多赚回 21 ms），赚不回 CPU 专家那 70 ms。

**Q4：文档与交接 prompt 在哪？**
A：- 给人看：`~/docs/plans/引擎为什么慢-与改进方案-2026-10-21.md`（修订版，含 §3.2 的 S 曲线表）。
- 给下一个 agent：`~/docs/plans/HANDOFF-PROMPT-ENGINE-PARALLEL-2026-10-21.md`（P0 建模→P1 解锁行预算→
  P2 层优先窗口→P3 S 扫描；明确"最优 S 可能就是 4–8，如实写"）。
- 调度骨架现状：`include/strata/sched/sched.hpp` 有 `Policy/Window/WorkItem`，**无 `src/sched/` 实现、
  无 `tests/sched/`、`generate.cpp` 无任何引用**。

## 第十五轮 · 2026-10-21 20:20 · 现在到底是"搬数据找专家"还是"搬专家"？

**Q：引擎的默认路径是数据下去找 CPU 专家，还是把专家搬上 GPU？我上一版的说法是不是自相矛盾？**
A：用户的记忆是对的，默认路径是**数据去找专家**。三级路径（`src/core/expert_source.cpp:3376-3430`）：
1. **VRAM 常驻**：GPU 直接算，不搬；4 路时命中率实测 88.6%。
2. **主机内存 + CPU 池**：未命中的**绝大部分**走这里——只把行/激活下去（每行几 KB），
   专家权重留在主机内存不动，13 个 AVX2 线程算完把结果传回。
3. **PCIe 暂存**：未命中里按 `pcie_num/256` 分出一小份，把专家 blob（最多 `kStagingBlobs=16` 块）
   搬进显存用 GPU 算。**（更正，见第十六轮：VRAM 驻留集是加载时按热度表一次定好的静态集合，
   运行期不换入换出，`host_res` 只读。）**

所以"搬运专家"只发生在第 3 条和缓存换入，主流是第 2 条。
**上一版方案表里"每步要搬 31 GB"的说法不准确**：那是假设"把并集专家搬到 GPU 算"的路径；
在真实的 CPU 路径里，等价量是 **CPU 池要从主机内存读的不同专家字节**（每层并集 × 1.8 MB × 48），
瓶颈是主机内存带宽/CPU 专家吞吐（~25–30 GB/s），与 PCIe 22.7 GB/s 同量级，所以数量级结论不变。

**关键推论**："数据去找专家"的优势是"一行激活几 KB vs 一个专家 1.8 MB"，代价随**不同专家数**涨、
不随行数涨。并发增大 → 并集逼近整层 512 → 退化成"把整层从主机内存读一遍"，省搬运的优势消失。
这就是 S=48 不划算的根本原因；也是为什么提高命中率/减少 miss 比加深批更根本。

已修正文档 §2 与新增 §3.2（`docs/plans/引擎为什么慢-与改进方案-2026-10-21.md`）。

## 第十六轮 · 2026-10-21 20:55 · 命中率是固定的吗？并发会不会占满 CPU↔GPU 带宽？KV 在哪？

**Q1：专家权重又不动，命中率应该是固定的吧？推理时每层选的专家是一样的吗？**
A：权重和 **VRAM 驻留表都是静态的**——`host_res[layer][expert]` 在加载时按 `--expert-profile`（热度排序）
一次算好，运行期**只读不写**（写：`src/core/expert_source.cpp:1951,1988`；读：`2013,3140`）。
所以**没有运行期换入换出/抖动**，用户的直觉对。
但**命中率不是常数**：48 层每层各有自己的 router 和 512 个专家，**层与层之间专家权重完全不共享**；
每个 token 在每层独立选 top-10，不同 token 选的也不一样。
所以 命中率 = f(固定驻留集, 当前流量的路由分布, 并发度)。对固定负载它是可复现的常数——
这恰好是好事：**可以离线精确预测**，不用猜。并发升高 → 并集变大 → 命中率下降。

**Q2：多个请求同时下去找专家，会不会把 CPU↔GPU 的 PCIe 带宽占满？**
A：**激活流量很小**。下行 `h_x_` 是 R×2560×4 B/层（`verify.cpp:281`）；上行 `h_ymiss_` 按
(R, miss 专家) × 2560×4 B 计（`:288`）。R=20、假设全 miss、48 层 ≈ **108 MB/步 ≈ 4.8 ms @22.7 GB/s**，
只有 92.5 ms 一步的 ~5%。**真正吃 PCIe 的是另外三样**：
1. **长上下文 KV 流式**（200k 时 2.1 GB/token/请求，见 Q3）；
2. `pcie_num/256` 份额的专家 staging（每层最多 64 个 × 1.8 MB，`expert_source.cpp:3376-3430`）；
3. prefill 的 `refill/lend`（一次约 300 ms）。
结论：**并发本身不会占满 PCIe，KV 长度才会。**

**Q3：KV cache 现在也放不下吧，都放在内存里？**
A：是。生产 `--kv int8 --kv-resident 32768`（已从 `/proc/<pid>/cmdline` 核实）：
每个 QSA 层只有 32k cell 在显存，其余在**锁页主机内存**，由 `kv_stream_resolve` 按
**4 cell = 4,224 B 一页**、CLOCK 二次机会淘汰经 PCIe 换入（`include/strata/kernels/kv_stream.hpp:13-28`）。
- int8 = 1,056 B/cell；12 个 QSA 层（`n_layers/qsa_interval = 48/4`）→ **12.4 KB/token**；
- 200k ≈ **2.5 GB/请求**；
- ~~**但代价是每步重扫**：非驻留块每步都要再过一遍 → 177 MB/层/请求/步 ×12 ≈ 2.1 GB/token~~
  **这条算错了（按稠密注意力算的）。更正见第十七轮**：QSA 是稀疏选择，decode 每步只换入选中的
  ≤2,051 个 cell（`qsa.hpp:110-122`），约 2.2 MB/层、26 MB/token/请求 ≈ 1 ms。
- 另外 36 个 GDN 层是常数状态，不增长，不参与这部分。

**Q4：20 个 200k 请求一起同层找专家，可行吗？**
A：**不可行，但墙是 KV 容量与 prefill，不是 decode 带宽。** 20×200k 的 KV 要 ~50 GB，主机放不下；
prefill 的索引器要对全量历史打分。**decode 本身不随上下文变慢**（实测 32k–128k 仍 58.8 tok/s）。
prefill 侧：它走 GPU 融合专家、一个 8k chunk 就几乎路由整层 512 个专家，
多请求同层几乎不增加专家搬运，卡的是 KV 容量与 GPU 计算。

**Q5：层优先调度可行吗？能保证吗？**
A：**机制可行，收益来源可证**：驻留集静态 → 每层并集只需取一次 → **专家权重读取与请求数解耦**（每层一次）；
随请求数线性增长的只有激活和 KV。可行域 = **中等上下文 × 4–16 路**，不是 200k × 20。
上线前用 `native_experts.txt` + `expert-profile.bin` 离线复算并集/命中率/字节（P0），
预测不成立就不改代码。

出处：`src/core/expert_source.cpp:1951,1988,2013,3140,3376-3430`；`src/core/verify.cpp:281,288,1724,1797`；
`include/strata/kernels/kv_stream.hpp:13-28`；`include/strata/core/layout.hpp:40-78`；
`docs/plans/引擎为什么慢-与改进方案-2026-10-21.md` §3.4。

## 第十七轮 · 2026-10-21 21:10 · 现在的瓶颈是 KV cache 吗？改进方案总结

**Q1：现在瓶颈在 KV cache 吗？**
A：**decode 不是。** 实测全量 **15,712 条请求**（`~/logs/infer-segs/2026-10-2*`，512 个分段）：
prompt p50 = 298、p90 = 27,372、p99 = 106,349、max = 198,067；
<4k 占 86.9%，4k–32k 占 3.4%，32k–128k 占 9.5%，≥128k 占 0.2%。
按桶看 decode 中位数：**<4k 61.0 / 4k–32k 63.7 / 32k–128k 65.2 / ≥128k 78.0 tok/s**——
不随上下文掉（长的反而更快，因为那批多是开 MTP 的 API 客户端）。
TTFT 中位数：11.4 / 91.6 / 472.7 / 774.0 ms——**随长度涨的是 TTFT，不是 decode**。
原因：QSA 是**稀疏注意力**，`idx_top_k=2048`、`idx_block=4`，每步只把选中的
≤2,051 个 cell 换入（`include/strata/kernels/qsa.hpp:110-122`），约 2.2 MB/层、26 MB/token/请求 ≈ 1 ms。
**KV 的真实代价只有两处**：① 容量（12.4 KB/token，200k ≈ 2.5 GB/请求）；② prefill 的索引器
仍要对全量历史打分（长 prompt 的 TTFT 近似线性）。

**Q2：那现在到底卡在哪？（按实测排序）**
1. **MTP 被采样关掉**：830 条有 predicted_n 的请求里，**571 条 `draft_n=0`（69%）**，decode 中位数
   **62.1 tok/s**；有 MTP 的 259 条中位数 **98.0 tok/s**。差 **+58%**。网页默认 `temperature=0.6`
   （`Strata/serve/webapp/src/store.js:9`）→ 整组退回逐 token 窗口 → 投机不生效。
2. **并发时 CPU 专家**：4 路 92.5 ms 窗口里 70 ms 是 CPU 专家（76%），21 ms 是 GPU 等门铃（23%）。
   13 个 worker 线程（`--pool-workers 13`）是固定产能。
3. **长 prompt 并发 TTFT**：prefill 串行 + 停车快照被驱逐后重读（`strata-ttft-prefill-queue` 记录）。

**Q3：改进方案（按收益/成本排序）**
| 序 | 做什么 | 打哪个瓶颈 | 预期 | 成本/前提 |
|---|---|---|---|---|
| P0 | 网页默认改贪心（或允许采样下走 MTP） | MTP 被关 | 62 → **98 tok/s** | 配置/前端；重启需批准 |
| P1 | 放置求解：按"收益/字节"用真实热度表求驻留集 | 命中率 | 17% → **40%**，直接砍 70 ms | 离线算 + 改参数 |
| P2 | 层优先：**prefill 先批**（几乎免费）+ decode 浅批 4–16；先拆 `kVerifyMaxT/GMAX/MAXT` | 并发/TTFT | 长 prompt 并发显著改善 | 改引擎内核 |
| P3 | CPU/GPU 专家分流（用掉 23% 空转） | 专家总产能 | **+30%–50%** | 改引擎 |
| P4 | KV 只做容量：`k8v4`（-23%）、更大 `--kv-resident`、会话缓存 | 长上下文并发数 | 200k 并发从 0 到少量 | 吃显存/改参数 |

**明确不做**：decode 深批到 48（并集撞带宽墙）；把 KV 当 decode 瓶颈去优化（它不是）。

出处：`include/strata/kernels/qsa.hpp:102-122,145-160`、`include/strata/kernels/kv_stream.hpp:10-28`、
`src/core/expert_source.cpp:1951,1988`、`Strata/serve/webapp/src/store.js:9`、
`logs/infer-segs/2026-10-2*`（1,168 条 meta + 830 条 engine.timings）。

## 2026-10-22 P2 实测：宽校验窗口（16 行）值不值？「上限 8」到底有几处？

**Q1：把 `kVerifyMaxT` 从 8 放大到 32、窗口开到 16 行，能提升并发吞吐吗？**
**不能。** 真实路由下「同一专家在一个窗口里的行数」**最多 8 行**（`STRATA_PLAN_CHECK=1` 统计 26,624 个窗口 /
466,119 次专家执行，`max 8`，0 丢行）。加宽窗口只是让一个窗口里同时活着更多**不同**专家，
不产生更宽的 GEMV；实测 S=8 时 ITL 从 109 ms 恶化到 170 ms（+19%），TTFT +3.6%。
默认保持 `--max-rows 8`。

**Q2：引擎里「8 行」的假设一共有几处？**
六处，分属不同层次：① CPU 专家 `cpu::MAXT`（栈数组）② `s2_expert_grouped.cu` 的 `GMAX`（`min(...,GMAX)`
静默丢行）③ `native_mmvq` 的 `MAX_NCOLS=8`（多列 kernel 编译期特化）④ `native_q8_1_bytes` 复用列数校验
（只是算缓冲大小）⑤ `fused_gr_read_multi` 的 `kFusedGrMaxT=8`（参数数组 + shared memory，超了直接 `std::exit(1)`）
⑥ `shared_expert_multi` 的 1..8 token。逐个修法：①②拆组/分块，③8 列切块递归，④放宽为「列数 ≥1」，
⑤⑥在调用处按 8 行分块。**没有统一的常量可改**，只能打开宽窗口让它自己报错。

**Q3：改完怎么证明「没丢行 / 没算错」？**
- kernel 级：`verify_wide_t_test`（T=8/16/24/32 与 8 行块逐位相同、窗口外不被写）、
  `mmvq_multi_parity` 扩到列宽 12/16/24（7 种量化格式 0 bit 差异，负向对照有功效）。
- 计划级：`STRATA_PLAN_CHECK=1` 在真实流量上校验「每个条目恰好一次、每组 ≤ 容量」，0 违规。

**Q4：为什么「换二进制后输出逐 token 一致」这条门禁根本过不了？**
引擎本来就不逐次可复现：同二进制 + 贪心，两次运行只有 6–12/20 条输出逐字节相同。
所以门禁要用 kernel 逐位证明 + 统计时延，不能用文本一致性。

**Q5：压测数字为什么差一倍？**
重启后第一次运行是冷的（专家缓存/会话缓存/GPU 时钟），TTFT 比暖机后高约 2 倍（509 → 255 ms）。
所有 A/B 必须丢弃第一次运行。

出处：`~/docs/reports/P2-ROW-CAPACITY-2026-10-22.md`、`~/docs/reports/P0-GREEDY-DEFAULT-2026-10-22.md`、
`~/docs/reports/PLACEMENT-2026-10-22.md`、`~/archive/patches/p2-row-capacity-2026-10-22/`、
`src/kernels/cuda/{native_mmvq.cu,fused_gr.cu,shared_expert.cu,s2_expert_grouped.cu}`、
`src/core/{verify.cpp,expert_source.cpp}`、`src/program/generate.cpp`。

---

## P3 / P4 / P5（2026-10-22，lane A 实测）

**Q1：并发时服务层返回的 `engine.timings` 能用来算 TTFT 吗？**
不能。`Strata/serve/server.py:2003 request_timings()` 从一份共享的 `last` 快照取时间戳，32 路并发实测只有约 1/8
的响应带 `timings`，其余是 `None`；拿它算会得到「中位 0.3 ms」这种假数。**必须用流式 + 客户端 chunk 到达时间**
（TTFT = 首 chunk − 发起，ITL = 相邻 chunk 间隔）。同一个竞态也出现在 `usage.prompt_tokens_details.cached_tokens`：
两条 117k 并发里有一条报「全量复用」，但它自己算对了答案——并发下这两个字段都不可信。

**Q2：`--batch` 开到多少最好？**
4。S=4 每请求 15–18 tok/s、ITL 30–31 ms；S=8 掉到 5.6–6.2 tok/s、ITL 55–58 ms（每槽只剩 1 行，`chain_step`
退化成逐 token 窗口，投机归零）；S=16 引擎起不来（`16 slot sessions (0.95 GiB each); 2.08 GiB free` 之后
`prefill: device buffers for a chunk of 512 tokens do not fit`）。48 路要 45.6 GiB 显存，本卡 24 GiB，方向性不可行。

**Q3：还有第 7 处隐藏的「8 行」上限吗？**
有，而且是唯一一处会用异常杀整条优化路径的：`src/kernels/cuda/shared_expert.cu:206` 用窗口行数调
`bf16_gemv_fp32_mmvf_multi`，`src/kernels/cuda/native_bf16.cu:254` 在 `n_tok > 8` 时
`throw std::invalid_argument`，链式路径捕获后置 `chain_off`，**此后整组永远走逐 token 窗口**。
所以 `--max-rows 16` 不是「更宽的窗口」，是「永久关掉投机」。

**Q4：`--pcie-frac`（CPU/GPU 专家分流比例）值得调吗？**
不值得。0.35 / 0.65 / 0.75 三臂（各 6 轮、丢前 3 轮暖机）都没有超过引擎默认的 0.55（启动实测 22.8 GB/s 自动取），
平台期聚合 42–47 tok/s vs 48–52。机制：调小份额把「等 GPU」从 24.5 降到 12.3 ms，但「池 + 规划」从 34.1 涨到
37.5 ms（`STRATA_SPLIT_TIMING=1` 边际值），两条腿已经跨在交点上。每层窗口只有 **8.86 个** CPU 专家却花 34–41 ms
（≈0.33 GB/s），说明这条腿是**同步/固定开销受限，不是带宽受限**。`kStagingBlobs=16` 的 PCIe 上限在 S=4 下也没顶到
（未命中 ~18、0.75 只请求 ~13）。→ 不做自适应控制器。

**Q5：KV 容量怎么加？（P5）**
只有 `--max-context` 有用。`src/core/layer.cpp:1067` 的主机 K/V 池按**全部页**（= max_context）分配，与
`--kv-resident` 无关；后者只是「多少页额外镜像进显存」，实测从 32768 提到 65536：主机池仍 3.09 GiB、
显存仍 1172 MiB、吞吐反而略降 → 纯无效旋钮。`--kv k8v4`（816 B/cell）不支持流式、要求全量进显存 +2.2 GiB，
本卡只剩 1.17 GiB，必须从专家缓存抠 → 净亏。
**落地**：`--max-context 262144 → 524288`，代价 +3.10 GiB 锁页内存、显存 −2 MiB；实测单条 **353,443 token**
提示词正确作答（122.5 s），**2 × 199,550 token 并发**都成功且各答对，标准 32 路负载无退化
（agg 51.7 vs 49.0、每请求 16.8 vs 15.4、TTFT 不变，ITL 中位 36.4 vs 30.7 ms）。

**Q6：长上下文并发是硬失败吗？**
不是。旧配置 2 × 140,450（合计 280,900 > 262,144）两条都成功，但第二条 TTFT 97 s（被 prefill 串行 + 停车换出拖住）。
**瓶颈是 prefill 串行，不是 KV**；而且并发下答案不会串味（3k / 117k / 148k 三组不同秘密编号探针全部答对）。

出处：`~/docs/reports/{P3-LAYER-SCHED,P4-EXPERT-SPLIT,P5-KV-CAPACITY}-2026-10-22.md`、
`~/archive/patches/{p3-layer-sched,p4-expert-split,p5-kv-capacity}-2026-10-22/`、
`src/core/{expert_source.cpp,layer.cpp,verify.cpp}`、`src/kernels/cuda/{shared_expert.cu,native_bf16.cu}`、
`src/prefill/prefill.cpp`、`src/program/generate.cpp`、`Strata/serve/server.py`。

## 2026-10-24 18:00 · Pod 全扫描：当前服务、运行原理、文档缺口（用户要求扫描并补文档）

**Q：当前 pod 上跑着什么、原理是什么、文档缺什么、下一步干什么？**

**进程拓扑（supervisorctl + ps 实测）**：supervisord 托管 `rag-proxy`（0.0.0.0:8080，唯一公网面，
uptime 3d4h）、`strata-a`（8091，GPU0 核 0-15，10-22 晨起）、`strata-b`（8092，GPU1 核 38-53，3d4h）；
单实例程序 `strata` STOPPED（双 lane 布局生效）；每 lane 一个 `strata-vision` 子进程。
serve 层（`Strata/serve/server.py`）拉起并守护引擎子进程（B 道引擎 10-23 自动重启过一次）。

**两条 lane 已分叉（10-22 实验结果，A/B 对照在跑）**：
A（实验道）= `deploy/engines/bin/p2-rowcap-b7-20261022/strata`（sha da5e3bba…，P2 行容量版，
`--max-context 524288`、**无** `--mtp-varlen`，profile `ab-novarlen`）——目录缺 `BUILD.json`，
`/v1/status` 回退显示 `legacy-0.1.38-sha.da5e3bba`，名字误导；
B（对外主力）= `mtpvarlen-20261021d` = `ssebed-0.1.38-b5`（`--mtp-varlen`，256K）。
**网关窗口守卫仍 262144 → 对外有效窗口还是 256K**，512K 只有直连 8091 能用。

**网关现状**：`rag_enabled=false` 纯透传（计数 requests 26,254 = no_rag 26,254）；模型分流
（qwen3.8-flash→8092 默认 / *-fast→8091）；failover；文档抽取；窗口守卫；OAuth；
推理留档 26,254 行已上传 MinIO 2,179 段（minio_ok=true）。

**运行时实况**：GPU0/GPU1 均 100% util、~23.2-23.7/24.5 GiB；容器内存 184/216 GiB（剩 31）；
lane B 停车 parked=20/5.37GB、累计 evictions 3,875、专家命中率 78.7–86%、KV 流式 VRAM 命中 99.4%、
MTP 草稿在工作；lane A 重启以来 118 请求、前缀复用 86.8%。

**文档缺口与本次补齐**：`项目定位与服务全景.md`（10-12 版还在讲 RAG 已上线）、根 `README.md`（10-15 版，
还写 ssemarket.cn:23034 与「1M 永远被接受」）、`CURRENT-VERSION-2026-10-21.md`（两 lane 同二进制已不成立）、
`docs/README.md` 索引缺 10-19~10-22 全部报告。→ 新增 `docs/reports/CURRENT-VERSION-2026-10-24.md`（逐值快照），
重写 `项目定位与服务全景.md` 与根 `README.md`，补全 `docs/README.md`，`项目路线图与待解问题.md` 增补
10-24 当前路线（观测→M2 拆门铃→M3 prefill 同窗→lane 收敛→1M 内存账→对外回归）。

**下一步（权威版）**：见 `docs/项目定位与服务全景.md` §10 与 `docs/周报/周报-2026-W43.md` §八。

出处：`supervisorctl status`、`ps aux`、`/home/cloud/deploy/engines/{active-*.env,profiles/*,bin/*}`、
`rag-proxy/config.json`、网关 `/health` `/concurrency`、引擎 `/v1/status`、
`SSEStraBed/logs/strata-iq3_xxs-b.log`、`free -g`、`nvidia-smi`。

## 2026-10-26 02:25 · 新增人脸录入/识别服务（/face，多租户 + MinIO 备份）

**Q：用开源人脸库做一套可用于生产的人脸录入/识别，经 Pod 8080 网关以 `/face` 对外，支持并发、
数据备份到对象存储、API key 系统且人脸按 key 隔离，并写一个 skill 指导其他应用接入。**

**选型**：没有 OpenAI 人脸库；采用 **InsightFace buffalo_l**（SCRFD-10GF 检测 + ArcFace ResNet-50
WebFace600K 识别，512 维），ONNX Runtime CPU。注意权重是「非商业研究」授权，商业上线前需授权/换权重。

**部署**：`/home/cloud/face-service`，FastAPI/Uvicorn 单进程，`127.0.0.1:8095`（只绑回环）；
supervisor `[program:face-service]` 已加（reread/update 只新增该组，rag-proxy/lane uptime 未变）。
公网出口 `https://ai.ssemarket.cn/face/*` 由网关 `rag-proxy` 反向代理（`face_upstream`，
64MB body，支持 GET/POST/DELETE/PUT/PATCH/OPTIONS 与 CORS 预检）。
**截至本条记录，生产网关 8080 尚未重启，/face 还未对外；重启需用户明确同意。**

**多租户与 key 流程**：申请 `POST /face/v1/keys/apply`（返回 request_id + 一次性 request_secret）
→ 管理员 `facectl approve` → 申请人 `POST /face/v1/keys/apply/{id}/retrieve` 领取（只能一次，第二次 410）。
key 只存 SHA-256；admin key 在 `secrets/admin.key`（600）。所有 persons/faces 查询按 `key_id` 过滤，
对象前缀 `face/keys/<key_id>/...`，跨 key 访问 404（已实测）。

**接口**：人员 CRUD + 加脸/取图；`/recognize`（1:N，批量≤8 图，top_k/min_score）、
`/verify`（与已注册人员或两图 1:1）、`/me`、`/health`、`/metrics`、`/docs`。默认阈值 0.5，
限流 10 rps/key，配额 1000 人/key，单图≤10MB。

**备份/恢复**：MinIO 桶 `qwen-3.8-flash` 前缀 `face/`；person.json + 人脸 jpg 为 source of truth，
`db/face.db` 每 5 分钟快照，审计 JSONL 按天。实测：注册→停服→`rm -rf data`→重启，
`restored_files=2, reindexed=1`，同人识别分数 1.0。**修复了恢复顺序**：启动先同步执行
`pending_deletes` 再 restore，避免「本地已删、S3 未删」时人员从备份复活（临时前缀实测通过）。

**性能（CPU，单图 640 左右）**：4 workers × 4 ORT 线程：单请求 0.22–0.31s，12 并发 p95≈1.15s；
8×2：单请求 0.36–0.42s，8/12/24 并发 p95=0.94/1.09/1.78s。当前选 4×4。
（未显式限制 ORT 线程时 12 并发 p95 曾达 5.4s，属线程超订。）

**测试**：`test_face_service.py` 33 项全过（申请→审批→领取、注册、识别、比对、幂等、跨 key 隔离、
删除、限流可选）。测试 key/人员已从生产库清理（active_keys/persons/faces=0）。

**产物位置**：`face-service/{README.md,app.py,face_store.py,face_engine.py,facectl,run.sh,requirements.txt,test_face_service.py,skill/SKILL.md}`；
skill 安装于 `~/.claude/skills/face-api/SKILL.md`；代码归档已备份
`s3://qwen-3.8-flash/face-service/code/20261026-022336/`。

出处：`/home/cloud/face-service/`、`/home/cloud/rag-proxy/{rag_proxy.py,config.json}`、
`/home/cloud/deploy/supervisord.conf`、`~/.claude/skills/face-api/SKILL.md`。

**上线结果（2026-10-26 02:30，已获用户同意）**：`supervisorctl restart rag-proxy` 后
`https://ai.ssemarket.cn/face/health` = ok、`/face/docs` 200、引擎 `/v1/models` 200；
公网完成一次真实全链路（申请→审批→领取→multipart 注册→识别 score 0.9619→删除→吊销），
事后 `active_keys/persons/faces = 0/0/0`，MinIO `face/keys/` 为空，`face/db/`、`face/audit/`
与 `face-service/code/` 归档正常。CORS 预检 200；未认证识别 401。

## 2026-10-26 03:14 人脸服务升级：动态活体、不存原图、GPU 与 UniSSO key

**问题**：UniSSO 要求接入人脸登录（一物理人=一个 person，多账号共用一张脸），并要求动态活体
（照片不能过）、不保存用户照片、用 GPU、权重备份到对象存储，同时回答活体方案 9 个问题。

**结论/实现**（已上线，http://localhost:9001 refused connection，未动网关）：

1. **动态活体**：服务端随机下发 challenge（turn_left/right、look_up/down），
   `POST /v1/liveness/session` 建 120s 一次性会话 → `POST /v1/recognize/live` 回传 3–8 帧；
   同时校验头部姿态幅度（yaw 0.10 / pitch 0.04）、帧间同一人（cos≥0.45）、被动 PAD（MiniFASNetV2 threshold 0.3）。
   注册对应 `POST /v1/persons/live`。静态照片实测 400 `challenge_not_completed`。
2. **不存原图**：`storage.store_face_images=false`，只存 512 维特征；取图接口 404 `image_not_stored`；
   临时库实测 0 个 jpg，MinIO/日志无图。
3. **GPU**：ORT GPU 1.23.2 + CUDA12 wheels 装在 `.ort-gpu/`（宿主无 cuDNN），
   `CUDA_VISIBLE_DEVICES=2`、显存上限 2 GiB（实测 ~0.9 GB）；单帧 p50 从 ~197ms 降到 **~8ms**，CPU 自动兜底。
4. **权重备份**：MinIO `qwen-3.8-flash/face-service/models/`：det_10g(16.9MB)、w600k_r50(174MB)、
   minifasnet_v2(1.7MB) + MANIFEST.json（SHA-256）。工具 `tools/backup_models.py`。
5. **UniSSO key**：`key_c2fd966e495170548271`，read+write，30 rps，20000 persons，365 天；
   明文写 `/home/cloud/deploy/unisso-v2/backend/secrets/face-api.key`（700/600，未出现在聊天/文档/git）。
   `/v1/me` 经公网验证 200。
6. **安全建议**：B 方案（人脸+密码/验证码）；管理员禁止纯人脸；多账号命中→脱敏账号选择器；
   0 命中→密码回退；登录阈值 0.6、绑定 0.5；一图多脸直接拒绝（不自动取最大脸）。
7. **活体 9 问**：纯 HTTP（帧输入，不收视频）；challenge 服务端下发；输出 pass+分项+best_frame 索引，
   同调用完成 1:N；同一 X-API-Key；浏览器 HTTPS+getUserMedia，8–12fps/2–3s/JPEG≤10MB，支持前摄；
   会话 120s 一次性、429 带 Retry-After；帧不落盘；失败区分动作/PAD/同一人/多脸/无脸。
   完整答复：`face-service/docs/unisso-integration.md`。

**风险**：buffalo_l 权重仅非商业研究许可，商业上线前需授权/换权重；PAD 阈值需真机样本标定；
纯 HTTP 动态动作不能防高级实时换脸，管理员仍需第二因子。

**测试**：`test_liveness.py` 13/13（临时实例，GPU）；`test_face_service.py` 原 33 项未回归。
出处：`/home/cloud/face-service/{app.py,face_store.py,face_engine.py,config.json,run.sh,README.md,docs/unisso-integration.md,test_liveness.py,tools/}`。

## 2026-10-26 10:18 真人活体被误拒：原因排查与放宽（face-service）

**问题**：用户本人按提示做了转头动作，UniSSO 活体仍无法通过，问是不是检测太严格；
要求上游 Face 服务不能误判。

**日志事实**：01:34–02:02（UTC）共 4 次 `POST /face/v1/recognize/live` 全部 400（177–235 ms，
IP 58.249.112.36），对应会话 challenge 分别为 turn_right / look_down / look_up / turn_left；
耗时说明已通过 session 校验和帧数校验、进入逐帧推理，不是 `missing_session`/`too_few_frames`/`wrong_purpose`。
旧代码没有记录失败码，`audit` 只有 `liveness.session`，所以无法从日志直接读到是哪一项挂了。

**代码级根因（三个都会误伤真人）**：
1. **PAD 强制拦截且未标定**：`liveness.pad.enforce=true`、阈值 0.3，但 MiniFASNetV2 在
   `testdata/crop*.jpg` 上无论 raw/255、ImageNet 归一化、BGR/RGB 都输出 replay≈0.99、
   live≈0.0003–0.008。也就是说这个 PAD 模型对现有样本系统性判“翻拍”，真机帧一旦同分布
   就必然 `spoof_suspected`。这是最可能的直接原因。
2. **单帧失败整单拒绝**：`analyze_images(require_single=True, choose_largest=False)` 对每帧
   要求“恰好一张脸”；任何一帧模糊/过曝/侧脸/背景多一张脸都会让整个请求 400
   `no_face`/`multiple_faces`。浏览器连续帧里出现这种帧很常见。
3. **同一人判定用全帧最小值**：`identity_min = min(各帧与均值余弦)`，一帧侧脸就能把 min 拉到 0.45 以下。

**改动（已上线，仅重启 face-service，未动网关/引擎）**：
- `evaluate_liveness` 返回逐帧 `frames_detail`（yaw/pitch/pad/det/quality）+ `identity_median`/`identity_outliers`；
  失败/成功都写一条 `liveness.rejected|passed` WARNING/INFO 日志（只有数字，无图无特征）。
- 同一人判定改为 medoid 参考 + 中位数≥0.45，且**最多容忍 1 帧不一致**（不是全帧最小）。
- 逐帧容错：无脸/多脸/太小只**跳过该帧**，记 `skipped_frames`；可用帧≥`min_usable_frames=2`
  就继续判定，只有不足才 400 `too_few_usable_frames`（附可用数与跳过原因）。
- PAD 改 **advisory**：`liveness.pad.enforce=false`，多帧取中位数（`aggregate=median`），
  `pad` 只随响应/日志返回，不参与拒绝。静态照片仍由动作幅度拦截（实测 motion_range=0 →
  `challenge_not_completed`）。
- 动作阈值 yaw 0.10→0.08、pitch 0.04→0.03（方向校验仍默认关闭）。
- 失败文案分码给出可操作提示（动作幅度不够 / 疑似翻拍 / 换人 / 可用帧不足）。

**验证**：`test_liveness.py` 13/13（GPU 实例）；合成帧单测：正确动作轴通过、错误轴拒绝、
1 帧坏 PAD 不影响、1 帧不一致不影响；`crop0 + 灰图 + crop0` 实测灰帧被跳过、仍进入动作判定。
**残余风险**：PAD 不拦截后，拿打印照片绕轴转动可能伪造 yaw 变化；UniSSO 采用 B 方案
（人脸+密码/验证码）可接受，严格防翻拍需先采真机样本标定后再开 `pad.enforce=true`。
**下一步**：请用户重新取会话、按提示把动作做大一点再试一次；若仍失败，日志
`liveness.rejected` 的 `failure`/`motion_range`/`identity_median`/`frames_detail` 会直接指出卡在哪一项。
出处：`/home/cloud/face-service/app.py`、`config.json`、`README.md`、`docs/unisso-integration.md`、
`logs/face-service.log`。

### 追加：02:27 真实一次失败的关键证据（identity 误判）

用户截图报“画面中的人脸前后不一致”（即 `identity_mismatch`）。日志（02:27:11 UTC）：
8 帧全部可用、`motion_range=0.8501`（look_up 阈值 0.03，动作完全够）、`pad=0.0003`（已不拦截），
但 `identity_min=0.1498`。逐帧 pitch：0.5625→0.602→0.6762→0.2859→-0.1114→-0.1739→0.168→0.3735，
说明用户做了大幅度抬头/低头；ArcFace 在同一人大幅俯仰时相似度会掉到 0.15，旧规则（中位数≥0.45
且不一致帧≤1）仍会拒绝。**结论：身份一致性不适合做硬门槛，尤其在动作挑战场景。**

改动（已上线）：
- `liveness.identity_enforce=false`：身份一致性只观测不拦截，响应仍给 `identity_min/median/outliers`；
  需要严格时可开 `true`（先用真机标定）。
- `liveness.pad.enforce=false`（上一轮已改）：PAD 只观测。
- 1:N 改为**全部可用帧分别检索、取最高分**，响应新增 `match_frame`（命中的上传帧序号），
  避免“最清晰帧不是最佳 probe”造成的近失。
- 失败 precedence 改为：仅当显式开启对应 enforce 时才报 `identity_mismatch`/`spoof_suspected`；
  否则只剩动作幅度会拒绝。

验证：临时实例（CPU、动作阈值置 0）跑的正面端到端——注册→会话→3 帧→`passed=true`、
1:N `score=1.0`、`match_frame=0`；生产实例 `test_liveness.py` 13/13，静态照片仍 400
`challenge_not_completed`。

**为什么这是正确的取舍**：活体的核心证据是“随机动作幅度 + 每帧恰好一张脸 + 连续采集”；
身份一致性是辅助风险信号。把它当硬门槛会在真人做大动作时系统性误拒，而这正是用户遇到的情况。
照片/屏幕重放仍过不了动作；残余风险是“打印照片绕轴转动”可伪造 yaw，需靠已标定的 PAD 或第二因子。

## 2026-10-26 11:13 解绑人脸（按应用/账号）怎么做

**问题**：有没有提供解绑登录 / 解绑人脸的功能？对于一个应用（UniSSO）要有解绑能力。

**结论**：
- 原有 `DELETE /face/v1/persons/{person_id}` 只能按 Face 自己的 `person_id` 删整人；应用只知道自己的账号 id，不够用。
- 新增两个接口（都要 `write` scope，按 key 隔离）：
  - `DELETE /face/v1/persons/{person_id}/faces/{face_id}` — 删单张模板；删掉最后一张时自动删除该 person（响应 `person_deleted: true`、`face_count: 0`），不留空壳。
  - `DELETE /face/v1/persons?external_id=<账号id>` — 按业务方自己的账号 id 解绑，默认 `ignore_missing=true` **幂等**（未注册/已解绑返回 200 `{"deleted": false, "reason": "not_found"}`）；`ignore_missing=false` 时找不到返回 404。
- 归属划分：`user_faces`（账号↔人脸多对多）在 UniSSO 侧维护；Face 只认 person/face，不理解账号。
  解绑单个账号 = 删自己的绑定行；**仅当该 person 不再被任何账号绑定时**才调 Face 删除。
  （一个物理人 = 一个 person，多账号可绑定同一 person。）
- 实现：`face-service/face_store.py:delete_face()`（删行→重写 manifest→MinIO 删除队列→审计 `person.face.delete`→失效索引；最后一张转调 `delete_person`）、
  `face-service/app.py` 两个 DELETE 路由。
- 验证：新增 `face-service/test_unbind.py`，13/13 通过；生产 `/face` 实测同一套流程通过，
  `test_liveness.py` 13/13 无回归；删模板后识别仍命中剩余模板（0.96），全删后不再命中。
- 文档：`face-service/README.md` §3.4、`face-service/docs/unisso-integration.md`、`docs/人脸服务-UnisSO接入答复.md`、skill `face-api`。

## 2026-10-26 15:50（北京时间）DeepSeek-V4.1-Flash 适配：项目定位、合并与权重盘点
- **问**：找到 strata 对 deepseek-v4.1-flash 的适配版项目，下载合并到我们的引擎，并检查 /shared 权重完整性。
- **答**：
  - 项目 = GitHub **Niko1221/Strata**（我们引擎的上游），v0.2.6（2026-10-25）含 DeepSeek-V4.1-Flash 引擎
    `strata-dsv41`（源码 `src/dsv41`，移植自 Project Maya 的 maya-ds4，MIT）；适配首发于 0.2.4（10-17）。
  - 合并落地：`/home/cloud/SSEStraBed`（主副本）+ `/home/cloud/SSEStraBed-engine`（工作副本）；
    dsv41 源码/CMake 块原样移植（tarball sha256 `c650d7e8…`），CPU 内核层整体升 0.1.51（旧文件已验证为
    注释级差异、备份 `vendor-backup/`），补齐新依赖闭包 geometry/model_family/aux_cpus/thread_name 四头。
  - **构建+测试**：两棵树 `build-dsv41` 出 `strata-dsv41`（48.2MB，CUDA arch89，NATIVE_EXPERTS=ON）；
    ds4 slot/experts_rule/pick_rule 单测 3/3 绿；生产 Qwen 通道未动。
  - 权重：`/shared/weights/deepseek-v4.1-flash-1017` = ISTA IQ2_XXS(2.00bpw) 4 分片 138.2GB，
    **sha256 4/4 对上上游 setup.py 钉值（10-24 rev）→ 主体完整可用**；缺 Engram(110.6G)+DSpark(8G)+
    Unsloth 元数据分片(5.8M)，/shared 只读故补到 `~/models/deepseek-v4.1-flash-1017-shared`
    （脚本 `~/tools/ds41_download_shared.sh`，sha 校验，进行中）。
  - 上游访问：Pod 直连 github 不通；`https://gh-proxy.com/https://…` 可用（API/raw/archive 都行）；
    跟踪脚本 `~/bin/upstream-watch`（记录到 `~/docs/notes/upstream-strata.md`）。
  - 关键架构差异与引擎优化全量见 **`~/docs/模型架构与推理对比-Qwen3.8-Flash与DeepSeek-V4.1-Flash.md`**。
  - 部署下一步（**需停服同意**）：打包 pack → 冒烟 → serve 层接 dsv41；RAM 空余仅 ~28GiB，
    DeepSeek CPU 腿需 ~100GiB 量级，上线前必须做通道让位测算。

## 2026-10-26 19:05（北京时间）serve 层如何接 DeepSeek 通道（不停 Qwen）
- **问**：部署时停 Qwen 换 DS；serve 层先写好。怎么接、怎么保证不破坏现网？
- **答**：
  - dsv41 与 strata **同一条 stdin 行协议**（`GEN <max_new> [k=v] ids` → `T/DONE`，见 src/dsv41/ds4_main.cpp:7-10），
    所以服务层不需要新 RPC，只需要**换 exe + 换 tokenizer + 解析/停止符/默认采样按家族切换**。
  - 家族开关 = lane 配置的 `"arch": "deepseek41"`（serve/server.py 全部门控，缺键即 Qwen 现行为）；
    接入点：tokenizer(tcfg: pre/special_ids) → dsml.DsmlOutputParser（DSML→Qwen 调用形式，serve/dsml.py）→
    effort 词 low50/medium62/high75/max100、none 关思考（ds_kwargs）→ 模板 pick_template 防旧拼写 →
    stop_ids={1}、engine.eos_ids={1} → ds4-slots 对话槽目录、model-card 采样默认 1.0/1.0。
  - 上游 0.1.51 基类支持"思考内写调用"，我们 0.1.38 基类不支持 → DsmlOutputParser.feed/finish 补守卫
    （translated 里出现 <tool_call> 且仍在 reasoning 态 = 该轮没有 thinking，直接切 content）。
  - 回归证据：test_ds41_serve 7/7 绿；test_server 131 项仅 1 项**预存在**失败（WebApp 断言旧 web/app.js
    文件名，Vue 重写遗留，非本次引入）；Qwen 词表升级前后 A/B 五组用例逐 id 一致。
  - 示例配置 ~/Strata/strata-ds41.example.json（不激活）；**部署动作（停 Qwen、打包、重启切流）等用户窗口指令**。

## 2026-10-26 22:45（北京时间）DS 通道首夜实测数字
- **问**：切完速度怎么样？
- **答**（8092 直连、api_key 生效、RAM 165G/216、卡0+1 各 23.5G）：
  - 单流：思考关 4.7–4.9 tok/s（TTFT 2.2s）；思考 high 5.2 tok/s（TTFT 7.6s，301 tokens）——**与上游 12G 单卡基准 5–7 tok/s 同量级**。
  - 引擎自报 `CPU lane 46.8% 的专家`；Engram/DSpark 行为正常（DSpark 按 auto 未载，专家 132G ≫ 48G 显存）。
  - 并发 2：13.4s/18.6s，墙钟 18.6s —— **串行排队实锤**；并发 3 全部通过无错误。
  - 加载：首次 READY≈19.5 分钟（21:53→22:12），重启≈22 分钟（94.3G 专家拷 RAM + VRAM 放置）；重启后首查发现 400 bug（high→xhigh 规范化进 DS 模板被拒），ds_kwargs 加 xhigh→75 修复并复测通过。
  - **网关 rag-proxy 仍停**，恢复对外等用户指令；DS 期上下文 8K、无图、串行。

## 2026-10-26 23:05（北京时间）DS 试验窗口回滚
- 决定：5 tok/s 带宽受限（数据找权重，pull 模式每 token ~2GB 权重搬运、GPU util 均值 11%）不达上线预期 → 回滚 Qwen。
- 结果：22:55 停 strata-ds41 → 起 strata-a/b → 23:0x 双 200 → rag-proxy 自动恢复，对外闭环验证过。
  **对外中断时长 ≈ 1 小时 01 分（21:53:08 停网关 → ~22:54 恢复，其间 8092 于 22:5x 先回）**。
- DS 资产保留：引擎 build-dsv41、pack ~/models/deepseek-v4.1-flash-1017/pack、权重（/shared 主体 + ~/models 配套）、
  serve 门控代码（arch=deepseek41）、示例配置与切换/回滚脚本。再启用 = supervisorctl start strata-ds41（加载约 19 分钟）。
- 经验固化：大 MoE 在 48G 显存 + 216G RAM 的 offload 形态 = 带宽受限（约 5 tok/s @2.00bpw）；
  提速在算法侧已到顶（--experts 5 +10% 量级），真解在我们引擎改造线（CPU 腿带宽调度/层优先/投机形态）。

## 2026-10-27 16:20 Qwen 提速：批链采样投机上线 A 道，实测 +18.8%
- **问**：Qwen 还有什么没提速？并行时有 MTP 吗？→ 授权部署快道并测试。
- **答**：
  - 并行投机早有（--batch-mtp），但**带采样的槽整组踢回逐 token**（generate.cpp v1 注释行）；
    真实 API 流量多带采样 ⇒ 大头没吃到。实现 --batch-mtp-sampled：链窗口对采样槽按 one_token 同款
    Philox(seed,位置) 逐行抽样（verify::sample_rows_chain），接受/提交/打印零改动。
  - A 道 4 并发 temp0.6 同热度实测 **48.5 → 57.6 tok/s（+18.8%）**；flag 已钉 A 道，回滚=摘 flag 重启。
  - **纠偏**：现系统批输出**从不逐位可复现**（同状态贪心重放 16 仅 6 稳；one_token 自对照互翻 27/32）——
    批形状浮点末位×采样放大所致；质量验收改为架构论证+统计等价。详 docs/plans/批链窗口采样投机-实现方案.md。
  - 附带修复：dsv41 合并遗留的主引擎编译断点（kernels 0.1.51 与 core 0.1.38 ModelGeometry 撞名）→
    双轨 vendor/kernels-cpu-0151 专供 dsv41；strata + strata-dsv41 全绿，ctest CPU 11/11。

## 2026-10-28 12:36（北京时间）llm-chat-train 训更久 + GPU2 显存从 TTS 邻居腾出来
- **问**：怎么重新跑更长 steps / 多 epoch 的训练？要不要指定显卡？GPU2 的大头是谁、能停吗？
- **答**：
  - 选卡只由 `CUDA_VISIBLE_DEVICES` 决定（`activate.sh` 里 export =2）；`main.py --device cuda` 解析成 `cuda:0`
    ＝第一个可见卡（`minillm/common/config.py:150`），脱离 `source activate.sh` 的新 shell 会打到 GPU0 → OOM，命令里钉死。
  - steps 口径：batch32×block256 → 1 epoch ≈ 4220 步；基线 8000 步≈1.9 epoch（val 3.2543）。
    拉长到 **16000 步**是同 lr 3e-3 的干净一档；**40000 步实测 NO-GO**（cosine 中段仍 93% 峰值 lr，loss 只震荡），
    更长要分轮：`--init-from <上轮 best.pt>` + lr 减半（重置优化器；加 `--resume` 才连优化器/步数一起恢复）。
    `--out-dir` 别复用 `checkpoints/chat`，会覆盖基线 best.pt。
  - **GPU2 占用大户 = 邻居 pod 的 GPT-SoVITS TTS**（本 pod GPU2 == 那台 GPU0，同物理卡）：
    `ssh -p 22026 cloud@ssemarket.cn`（密码存 `~/.ssh/tts_host.env` chmod600，不进文档/git），
    远端 supervisord 管 `gpt-sovits-infer`/`-2`/`-3`（api_v2.py 端口 9880/9881/9882，各 ~2120 MiB）+ `voice-studio`（不停）。
  - **经用户同意已停三个 infer 实例**：本 pod GPU2 used 17149 → **10787 MiB**（空闲 13.8 GB，batch 64 可上）。
    恢复 = `supervisorctl -c ~/deploy/supervisord.conf start gpt-sovits-infer gpt-sovits-infer-2 gpt-sovits-infer-3`
    （别 kill，supervisor 会拉起）。非交互密码走 `sshpass`（本 pod 2026-10-28 apt 装好）。
- **追加（同日 13:05，北京时间）**：训练曲线要上「那个网页」且刷新不丢。
  - **问**：8080 上有没有现成网页能挂训练 log、实时渲染 loss、且刷新不丢、全量存储？
  - **答**：有（SSEStraBed 前端），但**不能加接口** —— 加路由要重启 rag-proxy/引擎 = 打断对外服务（红线）。
    可行路子：`serve/server.py` 每请求读盘，静态服务只认 `/web/<扁平名>.css|.js|.svg` → 让训练进程把
    **全量指标原子重写成 `train-data.js`**（`window.__TRAIN_METRICS__ = {...}`），前端 `<script>` 注入轮询，
    同源、无 CORS、零重启。刷新不丢是因为数据在文件里而不是内存里。
  - **实现**：训练侧 `minillm/train/metrics.py`（`--web-metrics`）→ `metrics.jsonl`（每步全量、即时 flush）
    + `metrics.csv` + `loss-curve.png`（每次 eval 重画）+ `train-data.js`（≤2s 一次）。
    前端新增 `src/train/TrainView.vue`（标签页「训练」，5 张单 y 轴图：train loss / val loss / lr / gnorm / tok·s⁻¹
    + 评估台账 + 本次配置），zh/en 词表已加；`bash tools/deploy.sh` 发布（index.html 最后写，原子切换）。
  - **验证**：`node tools/verify-train.mjs` 10 项全过（含「自动刷新 step 推进」「刷新后 step 不回零、曲线仍完整」）；
    回归 `verify-live.mjs` ALL PASS（标签页断言与 `smoke.mjs` 已同步改成 6 个标签）。
  - **两个坑**：① Vue `whitespace: condense` 吃掉换行分隔元素间的空格 → 卡片数字用 CSS 分行；
    ② eval 那一格的 tok/s 是评估时间摊出来的假低值 → 记缺测 null，不拿 0 顶。
  - **备份**：新增代码 + 当轮 metrics 已打 tar 存 MinIO `qwen-3.8-flash/llm-chat-train/2026-10-28/train-web-dashboard.tgz`。

## 2026-10-28 14:40（北京时间）多 epoch 分轮训练结果：val 3.2543 → 2.7864
- **问**：进度和效果怎样？
- **答**（两轮各 30000 步 batch64×block256，墙钟 12:48→14:36 = **1h48m**，共 9.83 亿 token ≈ 28.5 epoch）：
  | 轮 | 配置 | 最佳 val | ppl | 对照 |
  | --- | --- | --- | --- | --- |
  | 基线 | 8000 步 b32，1.9 epoch | 3.2543 | 25.90 | — |
  | r1 | 30000 步 b64，lr 5e-3，14.2 epoch | 2.8289 | 16.93 | −13.1% |
  | r2 | r1 best 热启动，lr 2.5e-3 | **2.7864** | **16.22** | −14.4%（r1→r2 只 −1.5%） |
  - 两轮跑到最后 val 仍在降（没到「回升 = 过拟合」的点）；train-val 差从 0.72 缓升到 0.94，28.5 epoch 还算温和。
  - **热启动会先反弹**：r2 开头 lr 从近 0 抬回 2.5e-3，val 先 2.89→3.18（step 3500）再退火到 2.79 —— 分轮有效但收益递减。
  - 定性 A/B（同采样 temp0.7/top-k40/rp1.15，`scripts/chat_demo.py`）：基线会编造词（"Readstroput"）与跑题（天空→北极熊）；
    r2 答得短且贴题（鼓励语通顺），故事有完整小弧线但「小猫」重复偏多。事实性仍差（15.7M 参数的天花板）。
  - 产物：`checkpoints/chat-b64-r2/best.pt`（含优化器，可继续热启动）、`exports/chat-b64-r2/model-fp32.pt`；
    曲线 `metrics.{jsonl,csv}` + `loss-curve.png`，网页 `ai.ssemarket.cn/#train` 显示终态。
  - 备份：`s3://qwen-3.8-flash/llm-chat-train/2026-10-28/run-b64-two-rounds.tgz`（指标 + 导出权重；优化器态未传）。
  - **GPU2 已还**：邻居 GPT-SoVITS 三实例 supervisorctl start 回 RUNNING（显存回到 ~17 GB），我们没占卡。

## 2026-10-29 10:5x（北京时间）「loss 先涨、没收敛是不是轮数不够？」→ 查出一个指标口径问题
> **⚠ 本条第一段是错的**（2026-10-29 11:40 更正，见下一条）：网页喂的是 `chat-b64-r2`（热启动那轮），
> 它的 train loss **确实从 2.005 涨到 2.484**、val 涨到 3.261。我当时拿 `chat-b64`（第一轮）的数据回答
> "没涨、你看错面板了" —— 读错了 run，用户是对的。后半条（v4 验证集没有 QA）仍然成立。
- **问**：网页曲线 loss 先上涨、且没收敛，是不是训练轮数不够？
- **答**：
  - ~~**没有上涨**~~（错，见上方更正）：`chat-b64` 第一个记录点 loss 8.9459（随机初始化理论值 ln8192=9.0109），其后单调下降；
    用户看到上升的那条是**学习率面板**（warmup 1500 步 0→5.5e-3，设计如此）与梯度范数面板开头 3.43 的尖峰。
    截图是跑到 step 8250（28%）的快照，该任务已于 14:36 结束（两轮共 1h48m）。
  - **不是轮数不够**：train loss 每 4000 步均值 2.65→2.55→2.45→2.34→2.20→2.06→1.99（还在降），
    但 val 末段 6 个点 2.8519/2.8390/2.8482/2.8376/2.8289/2.8444 —— **在 ±0.01 里抖动，已进平台期**；
    r2 热启动一整轮只换来 −0.04。继续加 epoch 是"训练集更熟、验证集不动"，即记忆而非能力。
  - **真正的坑（已修）**：v4 基准的 `val.bin` 1,067,800 token 里 `<|user|>`/`<|sep|>` **出现 0 次** ——
    验证集全是故事流，QA 全在 train（152,339 条）。所以此前所有 "val loss/ppl" 量的是通用中文 LM，
    不是问答能力。修法：`scripts/make_v5_holdout.py` 从 train.bin 按 `<|user|>…<|sep|>` 边界抽出
    **400 条 QA（87,052 token）搬进 val**，不重新分词、故事段原地不动；产出 `~/lab-data/base/tokenized/chat-v5`
    + 明文 `holdout.jsonl`（测评题集）。旧模型评这个留出仍是开卷（v4 训过），只有用 v5 训的才可信。
  - **分开口径重测（scripts/eval_sft.py，与训练值对得上：story loss 2.8467 ≈ 训练 val 2.79）**：
    | 模型 | loss_qa / ppl_qa | loss_story / ppl_story | 生成 f1 |
    | --- | --- | --- | --- |
    | 基线 1.9ep | 2.1975 / 9.00 | 3.2907 / 26.86 | 0.204 |
    | 两轮 28.5ep | **1.6866 / 5.40** | **2.8467 / 17.23** | 0.264 |
  - 另修两处：ppl 扫描原来从头顺序取 200 块只覆盖 51,200 token，**整段落在留出区**（把背过的题当故事算出 1.67）
    → 改成全 val 均匀铺开并单列 holdout 档；`if mark_t` 张量真值歧义 → `is not None`。

## 2026-10-29 12:2x（北京时间）「两次重训 val 都不动 = 过拟合，是不是流程/数据有问题？」→ 是，查出四处
- **问**：r3/r4 的 val 不变，泛化没学到，看训练流程和数据准备有没有问题；修好给我训练命令。
- **过拟合的量化证据**（同一模型、同一 val）：
  - r3（chat-v6，热启动 + 身份×60）：val 从 2.8174（起点=r2 权重）→ best **2.7752**（step 34,500）→ 结束 2.8283；
    train 1.84 → 1.70。**train-val gap 1.10**（健康 <0.5）。
  - r4（chat-v7，身份×80=0.6%）：val 不降反升到 2.86，best.pt 都没再产生（val 从未低于恢复来的 2.7752）。
  - 根因是**唯一数据太少**：34.0M unique token ÷ 15.7M 参数 = **2.2 token/参数**（经验最优 ~20），
    却跑了 22-28 epoch。加 epoch 只会更记，不会更会。
- **数据准备里查出四个真问题**（前三个已修）：
  1. **val 92.5% 是故事**：v5 的 val = 87k 留出 QA + 1,067.8k 故事 → 页面上那条 val 曲线主要反映讲故事，
     对话的进步被稀释到"看起来不动"。分档实测：QA 基线 2.1975 → r3 **1.8439**（ppl 9.0 → 6.3）其实一直在进步。
     → 修：新基准 val 提到 **3,000 条真留出 QA（615,542 token，占 37%）**。
  2. **block=256 腰斩长答案**：答案 token 长度 p50 69 / p90 **258** / p99 532 → 约四分之一样本装不下，
     随机窗口把"答案没说完 + 下一条 `<|user|>`"当正常序列学（坏格式 + 长答案无监督）。
     → 修：训练 `--block-size 512`（模型 `max_seq_len` 本就是 512，免费）；`prepare_sft.py` 加
     `--block-hint/--drop-overlong` 在切数据时就告警/丢弃。
  3. **train/val 按 token 流尾部切**（`prepare_sft.py` 老逻辑）→ 边界样本腰斩成近重复泄漏，且语料
     "QA 在前、故事在后"时尾部全是故事（就是 v4 的 val `<|user|>` 0 次的由来）。
     → 修：改成**按样本随机切**（`--split-seed`，meta 记 `n_val_qa`/`split_by:sample`），并自检打印
     val 里 QA 条数；实测 116 条样本 97/19 切分、val 含 QA 19 条、mask 63.1%。
  4. **热启动姿势**（r2 的 loss 抬头）：`--init-from` 不带 `--resume` → Adam 矩清零 + 重新 warmup，
     实测 train 2.005→2.484、val 2.893→3.261 才回头。r3/r4 已改用 `--resume`（无暂态 ✓）。
  - 去重不是问题：148,690 条里 (问,答) 对重复 **0**，答案重复仅 1.0%。
  - 人设占比：r4 提到 0.6% 后 val 变差 → 定在 **0.30%**（83 条 ×40 = 101,760 token）。
    身份题现状：seen 8/8 命中，**unseen 只有 5/12**（"你贵姓/我该喊你啥"这类换皮问法不认）→
    下一步靠**增加问法多样性**（已补 35 条同义问法）而不是无脑加重复。
- **交付给用户的训练命令**：新基准 `~/lab-data/build/chat-v8`（train 34,011,772 / val 1,683,342），
  `--block-size 512 --batch-size 32 --lr 3e-3 --steps 14500`（≈7 epoch ≈26-30 分钟），**从头训**
  （老权重已把 v4 背过，热启动只会加重记忆）；测评看 `loss_qa` 与 `identity.unseen_hit_rate`。
  ⚠ val 口径变了（QA 占 37%），**2.3x 的新 val 与 2.78 的旧 val 不可直接比**，只能同基准内看曲线与分档。

## 2026-10-29 12:3x（北京时间）「ai.ssemarket.cn 怎么跳到 platform 了？」→ 域名只代理 API，网页在端口地址上
- **问**：给的训练页链接 `ai.ssemarket.cn/#train` 不对，点进去跳 platform.ssemarket.cn。
- **实测**：
  - `https://ai.ssemarket.cn/` → 200，`<title>SSE_Market API 开发平台</title>`（455 B）= **平台页面**，不是我们的前端；
  - `https://ai.ssemarket.cn/v1/models` → `401 缺少有效的 API Key` = 我们 rag-proxy 的回复 → **该域名只把 API 前缀转到本 pod**；
  - `https://ai.ssemarket.cn/face/health` → 返回平台 SPA 的 HTML（`/face` 没经这个域名）；
  - `https://ssemarket.cn:23034/` → 200，`<title>SSEStraBed · 层磐</title>` + `app-B8C-Jczx.js`，与本机 `127.0.0.1:8080` 一致 → **网页的正确地址**。
- **结论/口径**：**网页 = `https://ssemarket.cn:23034/`**（含 `#train`、`#monitor` 等 hash 路由）；
  **API = `https://ai.ssemarket.cn/v1/...`**（带 API Key）。以后给链接前先
  `curl -sk https://<候选>/ | grep -o "<title>[^<]*"` 对一下标题，别凭记忆写域名。
  公网端口来自平台（`~/deploy/context.json` 在本次 pod 重建后不存在了，别依赖它）。

## 2026-10-29 14:2x（北京时间）「MiMo 那个数据集能不能用？去洗点新数据」→ MiMo 不能用，但摸清了取数与换算口径
- **问**：小米 MiMo 前阵子开源的数据集能不能当我们的小模型语料？顺便去洗一些新数据。
- **MiMo 判定（不能用）**：`XiaomiMiMo/MiMo-V2.6-RL-oss` 实测是 **agentic RL 环境包**（41,292 个文件：
  `general/envs/<任务>/instruction.md + manifest.json + mcp_bridge.py + state.db + schema.sql`，另有
  `code.parquet`/`cyber.parquet`），HF 标注 `1K<n<10K`、任务名带 `_en_` → 不是 SFT 语料、量太小、英文为主。
  MiMo 名下 HF 一共只有 3 个数据集（另两个是 `SpeechMMLU`、`MiMo-Audio-Evalset`，都是评测集）。
- **取数通道实测**：`huggingface.co` 直连**不通**；**`hf-mirror.com` 可用，2.9–6 MB/s**，但它 302 跳到
  `cas-bridge.xethub.hf.co` —— **curl 必须加 `-L`**，否则只拿到 1 KB 跳转页；`modelscope.cn`、`github.com` 也通。
  `BAAI/Infinity-Instruct`（1M–10M 行中文指令，最对症）是 **gated**：README 可读、数据文件 `HTTP 403`，要用得申请。
- **改用无需授权的**：`m-a-p/COIG-CQIA`(149 MB，我们原先只吃了 16,350 条 human_verified)、
  `BelleGroup/train_1M_CN`(458 MB / 100 万行，**GPL-3.0**)、`train_3.5M_CN`(4.85 GB 多轮，备用)。
- **换算口径纠正（重要）**：本仓 8192 词表对中文实测 **1.06 字符/token（≈一字一 token）**，
  每条 QA 样本平均 **148.1 token**（答案 p50 46 / p90 246 字符）。
  我先前用"文件字节 ÷ 2.46 字符/token"估出"Belle 1M ≈ 1.9 亿 token"**高估约 4 倍** ——
  真实是 **≈92M token**。结论：**别按 GB 估数据量，按 `行数 × 通过率 × 148 token` 算。**
- **清洗产出率实测**（`scripts/import_external_qa.py`，读入 344,694）：保留 **214,184（62.1%）**；
  丢弃里 格式不合格 21.9%（Markdown/列表/JSON）、与现有池重复 **6.8%**（Belle 里确实含我们抽过的 12 万条 →
  去重必须做）、答案过长 4.8%（>900 字符配不上 block 512）、过短 4.1%、内部重复 0.3%。
  这批 ≈ **31.7M token**，正好把 v8 的 34.0M 翻倍；全取 Belle ≈ **3.7× 唯一数据**（2.16 → 7.6 token/参数）。
- **两个自己踩的坑**（写进了脚本注释）：① 手写括号计数当流式 JSON 数组扫描器 → Belle **一条都没解析出来**，
  改用 `json.JSONDecoder.raw_decode`（C 实现，2 万条 6 秒）；② 内部去重在每条上重算 2000 行集合 → 复杂度爆，
  改增量 set。
- **新管线**：`import_external_qa.py`（洗）→ `grow_base.py`（并进基准：自动抽 3,000 条新 QA 进 val 保持闭卷、
  其余均匀插 train、自检"旧数据抽查 6 段仍连续"、打印新 `steps_per_epoch`）→ chat-v9。
  训练预算的换法：**时长不变、epoch 从 9.6 降到 ~2、唯一数据 ×3.7** —— 这才是对症的药。
- **许可**：Belle GPL-3.0 / COIG 未标注 → 内部教学自用可以，**不外发数据与权重**，要标出处。
  全文见 `~/llm-chat-train/DATASETS.md` 第十节。

## 2026-10-29 14:46（北京时间）「当前引擎是否支持生图」→ 不支持；但上游 v0.2.6 已有该能力，是可选模块

**问**：看看当前引擎是否支持生图。

**结论：不支持。** 逐条证据（都是实测，不是推断）：

1. **生产 serve 没有这条路**：`Strata/serve/` 里没有 `images.py`；路由清单里只有
   `/health` `/api/health` `/models` `/v1/chat/completions` `/v1/chat/completions/batch`
   `/v1/chat/completions/count_tokens` `/v1/messages` `/v1/messages/count_tokens` `/v1/models`
   `/v1/load` `/v1/unload` `/v1/status` —— **没有 `/v1/images/generations`**。
2. **开关没打开**：上游以 run config 里的 `"images"` 段作为启用条件；我们两条 lane 的
   `deploy/engines/profiles/ssebed/strata-a.json` / `strata-b.json` 的键只有
   `api_key,args,cwd,env,exe,fit_max_tokens,gpu,gpus_asked,host,lib_dirs,log,model_name,port,tokenizer,vision`，**无 `images`**。
3. **生图后端不存在**：没有 `Strata/engine-image/`，全盘找不到 `sd-cli` / `diffusion-cli` 二进制。
   `SSEStraBed/third_party/llama.cpp/examples/diffusion/` 只是随 vendored llama.cpp 带进来的**源码**，
   `SSEStraBed/build/` 里没有编译出任何 diffusion 可执行文件。
4. **别和 vision 混**：现在跑的 `strata-vision` + `mmproj-Qwen3.8-Flash-Next-BF16.gguf`（以及引擎的
   `--vision`）是**读图**（image encoder，输入侧），与生图无关。
5. 8091/8092 直接 POST 该路径返回 **401**，是鉴权先于路由，不能当作"路由存在"的证据 —— 要看代码和路由表。

**上游能力（本地归档就有，可直接抄）**：`archive/incoming/strata-upstream/v026/Strata-0.2.6/`
（= GitHub Niko1221/Strata **v0.2.6**，`release_latest.json` 也指这个 tag）
- `serve/images.py`（468 行）：每个请求**起一个 `sd-cli`（stable-diffusion.cpp）子进程**，
  输出与裸 sd-cli 同参数逐字节一致；对外是 OpenAI 形状 `POST /v1/images/generations`
  （`prompt` / `size`（边长 256~2048、须 32 的倍数，默认 1024x1024）/ `n` / `seed` / `steps` /
  `response_format: b64_json|url`）。
- 模型 **Qwen-Image-2.1-Turbo**（8 步、CFG 1），用 Unsloth GGUF 量化，按显存档三档：
  denoiser `Q4_K_M` 4.2 GB / `Q6_K` 6.3 GB / `Q8_0` 7.6 GB + 文本编码器
  `Qwen3-VL-8B-Instruct-UD-Q4_K_XL.gguf` 5.1 GB + VAE bf16 0.7 GB ⇒ **合计约 10~13.4 GB 显存**。
  **许可是 Qwen Research License，不是 Apache 2.0**，商用前要先读它的 license。
- 装法：`./setup.sh --setup --images qwen-image`（要源码 checkout + 编译工具，非普通安装路径），
  或已装好的实例上 `python tools/image/install.py --config strata-<model>.json`。
- 显存共存策略（`vram` 模式）：`keep`（装得下就并存）/ `shrink`（引擎须以 `--vram-elastic` 启动，
  从 expert cache 让出显存再收回，会话不丢）/ `unload`。每张图跑完自己的进程，卡上不残留。
- 实测参考：RTX A4000 16 GB / CUDA 首图 27 s、后续 8.8 s。

**另外一条没接线的路**：`/shared/weights/microsoft-Mage-Flow-Turbo`（老师放到共享盘的）**确实是文生图模型**
（`model_index.json` 的 `_class_name: MageFlowPipeline`、README `pipeline_tag: text-to-image`、**MIT 许可**），
但它是 **diffusers 格式**（`transformer` 7.7 G + `text_encoder` 8.3 G（Qwen3VL）+ `vae` 330 M ≈ **16.4 GiB**），
需要微软 `mage_flow` 自定义 pipeline 包 —— **与上面 sd.cpp/GGUF 路线不兼容**，且本机 python3 **没装 diffusers**。

**卡现状（决定可行性）**：卡0/卡1 = 我们两条 lane，util 100%、显存占用 0.945/0.951（各只剩 857/714 MiB）；
卡2 util 0%、显存 0.428（~10.5 GB 被邻居 GPT-SoVITS 占着，**空闲 13,572 MiB**）。
`pressure.level` 里 **gpu=high**。改 config / 换二进制 = **重启 = 停服，必须先经用户同意**（红线）。

## 2026-10-29 15:1x（北京时间）「按上游参数部署 Qwen-Image-2.1，怎么做」+「能不能上越狱版」
→ 方案文档 `~/docs/生图部署方案-Qwen-Image-2.1.md`；脚本 `~/tools/fetch_qwen_image.sh`。

- **先纠正上面那条**：`shrink`/`--vram-elastic` 在我们树上**根本不可用**（grep 无 `--vram-elastic`、无 `POST /v1/vram`、
  无 `images.py` 依赖的 `svc._turn`）。**但卡2 上没有我们的文本模型**（lane 在卡0/卡1）⇒ 用 `gpu_policy: keep`
  就够，显存腾挪机制整段跳过。这是本机相对上游"单卡"假设的结构性优势，也是唯一可行形状。
- **上游 v0.2.6 参数核对**：`--image-quant`（setup.py:8569）、`--image-gpu-policy`（:8575）**真实存在**；
  **`--image-ssd-offload` 不存在**（全树搜 `ssd` 只命中 README/setup.py 讲**文本引擎**专家流式 `--resident-budget-gib`）。
  用户描述的行为（"坚持指定量化档、放不下的分层调入、不自动降级"）**真身是 sd.cpp 自己的开关组**：
  `--params-backend disk|cpu|模块=…`、`--auto-fit on|off`（默认 on，按可用内存自动放参数）、
  `--max-vram 卡=GiB`（受管权重+runner 缓冲预算，非硬物理上限）、`--offload-to-cpu`、
  `--split-mode layer|row`、`--disable-prefetch`（异步预取默认开）。经 config `images.args` 原样追加生效。
  `--clip-on-cpu`/`--vae-on-cpu`/`--control-net-cpu` 已 deprecated → 统一 `--backend <模块>=cpu`。
- **上游只 pin Turbo**：三个量化文件全在 `unsloth/Qwen-Image-2.1-Turbo-GGUF`（Q4_K_M 4,199,565,120 /
  Q6_K 6,271,551,296 / Q8_0 7,640,860,480），逐文件 SHA256。**完整版要改一行**：`sd_cli_args()` 把
  `--cfg-scale` **硬编码 1.0**（Turbo 蒸馏配方），而 sd.cpp 官方 `docs/qwen_image_2.1.md` 给完整版是
  **CFG 6.0** ⇒ 直接换权重会出废图。完整版与 Turbo **体积几乎相同**（差百来字节元数据），
  差别是步数 20~50 vs 8 ⇒ 约 3~6 倍耗时。
- **`--max-vram` 的卡名陷阱**：sd-cli 自己从 0 编号（`CUDA0/CUDA1/CUDA2`）；用 `CUDA_VISIBLE_DEVICES=2`
  只暴露物理卡2 时，它里面叫 **`CUDA0`**。设错会把预算落到别人的卡上。
- **P0 已完成**：`sd-cli` 在本机 CUDA 编译通过（nvcc 12.4 + `-DSD_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89`，
  `make -j16`，exit 0，242 MB），`--list-devices` 认到 3 张 4090 D（capability 8.9、VMM yes）。
  坑：没传 `-G Ninja` 时 CMake 默认 **Unix Makefiles**，`build/` 里是 `Makefile` 不是 `build.ninja`
  → 直接跑 `ninja` 会报 "loading 'build.ninja': No such file"。
- **通路实测**：`git clone https://github.com/…` **直连超时**；**`https://gh-proxy.com/https://github.com/…` 可用**
  （已浅克隆 v0.2.6，`serve/images.py` 与本地归档逐字节一致；sd.cpp 源码包 124 MB / 22 s / 5.6 MB/s）。
  `huggingface.co` 直连不通、**hf-mirror 可用**。cmake/ninja 在 `Strata/.venv/bin`（不在 PATH，
  所以裸 shell 看着像"缺工具链"）。磁盘：home 是 `/dev/sdb` 15 TB（剩 12 TB），不是小盘。
- **移植成本评估**：我们 `serve/server.py` **3580 行** vs 上游 **8718 行** —— 是分叉不是落后。
  `images.py` 本身只用标准库 + `self.lock`（对 `svc` 的依赖仅在可选的 `gpu_turn()`），
  ⇒ **推荐路线 A：卡2 旁挂薄 HTTP 服务，生产 lane 完全不重启、零停服**；路线 B（并进生产）需重启、需批准。
- **越狱版答复（不做）**：技术上换权重无门槛（sd-cli 只按路径加载、不校验），但这是**对外多用户**服务，
  去掉图像模型安全过滤 = 真人换脸/涉未成年人内容不可控且**服务提供方担责**，与国内面向公众的生成式 AI
  服务的内容安全/备案义务直接冲突；叠加 Qwen **Research License 限制商用**。
  **可做的替代**：不对外、有访问控制与日志的隔离端点上做审核链路红队测试，且需事先取得老师/管理员授权。

## 2026-10-29 16:12 — Strata 能不能部署 ≤30B 轻量 MoE 供更高并发？
- **问题**：现引擎能否适配轻量级 MoE（总参 30B 上下）作为高并发快车道？该选哪个模型？
- **引擎谱系现状**（`vendor/kernels-cpu-0151/include/strata/artifact/model_family.hpp`）：只有
  `qwen4exp`（现生产，27B 级激活、512 选 10、48 层）真跑；`dsv41` 已并入未上线；
  **`qwen35moe`（Qwen3.6-35B-A3B）已被识别且 geometry 表已就位**（`geometry.hpp:57-60`，40 层 + blk.40 MTP），
  但我们树里没有执行路径 `src/qwen35`。**外来谱系（gpt-oss、Gemma、老 Qwen3-30B-A3B）直接
  `model_family_not_supported` 拒绝** → 适配它们 = 落地 G1–G5 通用化计划（周级工作）。
- **关键发现**：上游 Strata（本地克隆 `/tmp/qimg-probe/strata-clone` @4b9e05a，2026-10-25）**已有完整
  `src/qwen35/q36_engine.cu` 引擎路径**：batch 并行 ≤8（`--parallel N`→`--batch N`）、`--batch-mtp`、
  视觉（mmproj 0.9GB）、conversation cache，**serve 协议与 Flash-Next 相同** → 我们 serve 层只需新增 lane
  配置。文档 `docs/QWEN36.md`/`MODELS.md §Qwen3.6`。CMake：`add_library(strata_qwen35 ...)`，`strata` 主程序按
  GGUF architecture 自动切换。
- **实测口径**：UD-Q4_K_XL 单流 decode R9700 117.8 tok/s（llama.cpp 81.9）、RTX 3060 12GB hybrid 59.7 tok/s；
  Unsloth MTP GGUF 再 ×1.5–2。量化：Q4_K_XL 22.9GB / Q6 32.6GB / Q8 37.8GB；社区 2-bit 约 13GB。
  评分两极：LLMCheck 本地 116 模型榜第 1（81 分）、"编码超 Gemma 4 稠密"；BenchAlign 130/230。
- **并发账**：激活 3B vs 现 ~27B → 每 token 读字节量降 ~一个数量级，正面绕开我们的解码瓶颈
  （94% 解码时间等专家，见 [[decode-expert-bandwidth-bound]]）。槽位规则：专家全进 VRAM→3–4 槽@32K；
  hybrid→会话 ≤35% 专家空间；KV fp32 45KB/token，32K 槽 ≈1.5GB。
- **本机现实**：3×4090D 24G；GPU0/1 被 A/B lane 占满（各余 ~1.2GB），GPU2 邻居 TTS 占 10.8GB
  （可 ssh 协调腾 ~6.4GB，见 [[gpu2-tts-neighbor]]）；RAM 余 63GB、76C。**Q4 单卡装不下全部专家 → 只能
  hybrid 或先腾卡**；新 lane 不动现有服务 = 无需停服批准，但建 lane 前要按红线确认 GPU2 显存来源。
- **建议路线**：照 [[dsv41-merge-state]] 的做法并入 src/qwen35+tools/qwen35+CMake 块 → 编 `strata-q36ref`
  跑 `tools/qwen35/parity.py` 对 llama.cpp 校验 → hf-mirror 拉 unsloth/Qwen3.6-35B-A3B-MTP-GGUF →
  GPU2 起独立 lane 冒烟压测 → 数字达标再谈网关/前端挂模型名。

## 2026-10-29 16:30 — 「卡0 被停了」排查：实为 lane A 引擎段错误自动重启
- **问题**：用户发现 GPU0 显存从 23.1GB 掉到 11.5GB，怀疑 agent 停了服务。
- **结论（非人为）**：lane A（8091，supervisor strata-a → server.py pid169 → 引擎子进程）的引擎
  **SIGSEGV（exit -11）崩溃**，server.py 看门狗自动重生；重载 40GiB expert arena（PCIe ~23GB/s，约 5 分钟）
  期间 GPU0 显存被释放再逐步涨回，看起来像"停了"。~16:27 恢复：GPU0 回到 23100MB，/health loaded:true。
- **崩溃现场**：引擎日志最后一条固定为 `strata serve: mtp prefill: invalid argument` 后进程即死
  （`~/SSEStraBed/logs/strata-iq3_xxs-a.log` 出现 15 次、`-b.log` 3 次，累计）。这是 MTP prefill 路径的
  引擎缺陷，两 lane 都可能在触发条件下崩。
- **对外影响**：lane B（8092 主力）未中断（GPU1 23.4GB 不变）；rag-proxy failover 顶住
  ai.ssemarket.cn，16:06–16:11 有少量 broken pipe（`~/deploy/logs/rag-proxy.out.log`）。
- **排查口径（可复用）**：显存掉了先分三层看——supervisorctl uptime（受管进程没重启）→
  `ps -o etime` 引擎子进程（重生=崩溃）→ `~/deploy/logs/strata-{a,b}.err.log` 的 `EngineDied: exit code`
  （-11=段错误，-9 才像 OOM/驱逐）→ 引擎日志崩溃行前的最后一条 serve 记录定根因。
- **待办**：定位 `mtp prefill: invalid argument` 代码路径；若复发频繁，选项=关 `--batch-mtp`/回退稳定构建
  （属停服操作，先经用户同意）。

## 2026-10-29 16:50 — MTP prefill 崩溃链修复完成（b7 已编译，待批准部署）
- **根因确认（代码级）**：① `src/core/mtp.cpp` 全部 8 处 CUDA 错误分支用 `cudaGetLastError()` 取错误串，
  **消费了线程粘滞错误**，使 `generate.cpp:7030` 的 #224 护栏（毒化上下文应 `_Exit(1)`）peek 到 cudaSuccess、
  改走析构 → SIGSEGV；② `finish_capture` 把 `cudaGraphInstantiate` 的输出直接写进 `exec`——失败时输出为
  未定义值，可能把半截句柄发布给 `if (exec) return true` 缓存复用，`cudaGraphLaunch` 随即报
  "invalid argument"（与本 bug 的报错形态吻合）；③ 上传/同步返回值无人认领；④ E-4 realloc 前未排空
  `cs_`，`cudaFree` 可能在飞拷贝读旧缓冲时发生。
- **修复内容**：新增文件内辅助 `mtp_cuda_err()`（用**返回码**报错+带 `op=` 失败调用名+现场数值/指针，
  不消费粘滞错误→护栏恢复效力）；`finish_capture` 改为局部 `new_exec` 成功才发布、upload/sync 报错并回收；
  realloc 前 `cudaStreamSynchronize(cs_)`；prefill E-4/旧循环、draft round/step/batch、copy_state、
  coupled setup 共 8 站点全部改造。**触发侧（conversation cache 回收借用缓冲 vs E-4 在飞）未根治**，
  下次触发将打印精确 op 与指针，据此定位再修。
- **验证**：`switch-engine dev build` → **b7**（sha b8e34082…）编译通过；`dev test` 11/11 全绿；
  mtp.cpp 无新警告（旧警告在 weights.cpp）。原 b6 二进制（sha 39ec860a…）已从 /proc 进程存证到
  `~/archive/binaries/strata-ssebed-0.1.38-b6-20261029`；补丁前源码在 `~/archive/patches/mtp-prefill-fix-2026-10-29/`。
- **注意**：两 lane 的引擎 exe 路径就是 `~/SSEStraBed/build/strata`，现盘上已是 b7——**lane 若自然崩溃重生会
  直接拉起 b7**（含修复，可接受）；若要维持 b6 需把存证拷回。正式换道运行仍按红线等用户批准重启窗口。

## 2026-10-29 17:00 — qwen36 道并发提升估算（推算，待实测）
- **依据**：上游单流实测（R9700 117.8 / RTX3060 hybrid 59.7 tok/s）、QWEN36.md 槽位规则
  （全 VRAM→3–4 槽@32K；hybrid→会话 ≤35% 专家空间；窗口上限 8 行）、现 lane 今天日志（~120–128 tok/s 单流、
  --batch 4 最优）。
- **估算**：① 单张 4090D hybrid：2–4 路@32K / 4–8 路@8K ≈ 用 1 卡换现 2 卡的并发与聚合吞吐（单位 GPU 效率 ~2×）；
  ② 双卡专家全 VRAM：6–8 路@32K、8K 短会话可到 16 路（对现"4 路顶"约 3–4×），prefill 便宜 → TTFT 排队消失；
  ③ batch-8 聚合吞吐上游无实测数，外推偏差 ±30%；32K 上下文定位=快车道，长文流量仍走现 lane。
- **验证路径**：并 src/qwen35 → 拉 UD-Q4_K_XL → GPU2 测试道 → bench 台 8 路并发实测替换估算表。

## 2026-10-29 17:22 — b7 正式部署完成（双 lane），并发现 switch-engine start 竞态
- **部署结果**：A(8091) 与 B(8092) 均 `loaded:true`、`engine_version=ssebed-0.1.38-b7`；网关双上游 ok；
  B 道端到端冒烟通过（temperature 0，content「好」，finish stop）。A 道自 16:59 起独扛过全量流量一次，无事故。
- **过程发现（部署工具缺陷，未修）**：`switch_engine.py` 在 stop 后立刻 start，旧引擎显存/进程未完全释放时
  **strata-vision 起不来 → server.py RuntimeError("the vision encoder did not start") → 首次 start 以
  supervisorctl exit 7 失败 → 脚本 traceback 中止**；实际 supervisor 会在 ~1 分钟后自动重试成功（A、B 都如此）。
  影响：脚本没跑完"健康等待+冒烟"就退出，需人工接管确认。修法建议：stop 后轮询等旧 pid 消失/显存回落，
  或对 exit 7 重试 start，或失败后继续走健康轮询。改动在部署路径，先请示再动。
- **b7 内容回顾**：MTP prefill SIGSEGV 链修复（8 处错误分支改返回码报错、#224 护栏恢复、finish_capture
  防半截句柄、realloc 前排空流、全站点带 op/指针现场诊断）。**触发侧未根治**：下次再报 invalid argument 时，
  日志将带 `op=` 与指针，可据此定位 conversation cache 与 E-4 在飞拷贝的生命周期冲突点。
- **回滚物**：b6 二进制 `~/archive/binaries/strata-ssebed-0.1.38-b6-20261029`（sha 39ec860a… 对上）；
  `dev rollback --lane a/b` 或拷回 build/strata 即可回退。

## 2026-10-31 09:38 — 现在 MTP 是几，一次并行预测几个 token
- **问**：当前引擎 MTP 配置是几，一步并行预测几个 token？
- **结论**：
  - A(8091)/B(8092) 两条 lane 参数完全一致：`--spec 4 --spec-min-p 0.5 --mtp Strata-data/mtp/rt
    --batch-mtp --batch-groups 1 --mtp-varlen --batch-mtp-sampled`
    （`deploy/engines/profiles/ssebed/strata-a.json` / `strata-b.json`，进程 cmdline 实测核对）。
  - `--spec 4` = 投机解码窗口 T=4：MTP 草稿层一次出 **4 个候选 token**，目标模型一次前向并行校验，
    最多 4 接受 + 1 bonus = **一步最多吐 5 个 token**（`src/program/generate.cpp:1437`、`:1827-1835`
    solo 窗口 = `--spec` 行，超 `--max-rows` 会 clamp）。
  - `--mtp-varlen` + `--spec-min-p 0.5`：窗口变长——草稿概率跌破 0.5 提前截断，实际常少于 4 行。
  - prompt-lookup 后缀草稿默认开（`suffix_draft=3`），lookup 窗口最多比 `--spec` 长 2（=6 行、一步最多 7 token）；
    编译上限 `kVerifyMaxT=8`（`src/kernels/cuda/iq_kernels.cu:1804`）。
  - `--batch-mtp-sampled` 开：网页默认 temperature 0.6 的 sampled 流量也走 MTP 窗口
    （此前整组退回逐 token，投机对真实流量 0 生效的问题已解）。

## 2026-10-31 22:3x（北京）prefill/decode 并发改造：合并多请求+mask 可行吗？业界 PD 分离怎么落
**问题**：①prefill 只算 kvcache 的话，可否把多个请求拼接、用 mask 隔开、一起算各自的 KV cache？②学长给的业界方案「GPU 干成两份：prefill 与 decode 并行，decode 多请求合 batch，prefill 多请求合并+mask」当前引擎有没有？怎么做？

**结论**：
1. **当前不支持**。引擎单服务线程（`generate.cpp` for(;;)），prefill 同步 `sp.run` 一次一个 prompt（serve 控制锁 `server.py` ctl 串行）；prefill 主循环四层状态全绑单会话：`ss.gdn_state`、`ss.qsa_states[primary]`、`ss.ple.hist`、单份 RoPE/页表（P3 报告 §3 有逐行表）。
2. **「mask 隔离」在这台模型上要改成「按行状态隔离」**：模型不是 softmax attention 为主——GDN 是线性注意力（递归状态，token 拼接后状态互染，mask 不适用）；QSA 是每序列 top-k 块选择、PLE 是跨相邻 token 卷积。这三者的多序列隔离都要 per-row 状态指针（= 内核签名改动）。softmax 部分才谈得上 mask。
3. **好消息：多会话逐行的地基已经在**——decode 批窗口本来就是 S 个槽各过各的状态（`verify.cpp` `slot_ss(t)` 逐行；GDN 链行=同会话连续行在寄存器内推进；`read_windows` 已用这套机制读 ≤`--short-read`(64) token 的提示词）。「合并多请求 prefill」等价于把这套机制从每槽 1-2 行扩到每槽整 chunk 行 = 设计稿 `docs/plans/PREFILL-CONCURRENCY-PLAN-2026-10-21.md` §4 主线 **E1 混合窗口**（C1-C6 改动表：C1 prefill chunk 可重入、C2 超级步调度、C3 MoE 行合流【大】、C4 混合窗跳过 decode CPU 池、C5 一致性、C6 开关）。
4. **「GPU 干成两份」（PD 分离，DistServe 式）本机不适用**：无 NVLink、两卡已被 A/B 两 lane 各占、跨卡搬 KV 走 PCIe 22.7GB/s（一条 250k 提示词 KV 数十 GB 不可接受）；分离后每 lane 解码容量减半。业界同卡形态 = Sarathi 式混合批（一个 fused mega-batch 里 prefill 行+decode 行），不是两张 CUDA 分片。
5. **收益实测口径**：合并 prefill 的价值在摊每请求固定项（~1s：单请求 TTFT 1k→2.02s）与共享专家流。P3 §3 估：8×1k 合并 ≈6×；8×3.8k ≈1.8×（长 chunk 专家并集已接近整层，撞计算上限）。E1 的另一半价值：prefill 行效率 ~0.3ms/行 vs decode 行 ~10.5ms/行——decode 行「顺路」跑掉关键路径 CPU 段。
6. **今晚新实测（普道 8092 现网 b7）**：1/2/4 流 batch 窗 21.6/30.3/42.2ms 中位（≈10.5ms/行恒定→聚合封顶 ~85-92 tok/s）；探针贪心双流 250 窗全 kind=batch rows=slots accepted=0——`--mtp-varlen` 的在线成本拟合把多行组规划成 L=1，链在全 batch 时代被**规划器锁死**；CPU 专家池线程在窗内满负荷 12.8/13 核。已改：step-stats 每窗腿分解（wait/pool/drain/plan/actq/jobs/run+misses/entries/hits/pcie+why 回退原因）+ `--mtp-chain-floor N` 实验开关（默认关），待重启验证。
**出处**：`docs/plans/PREFILL-CONCURRENCY-PLAN-2026-10-21.md`、`docs/reports/P3-LAYER-SCHED-2026-10-22.md` §2/§3、`~/logs/step-stats-b.jsonl`（今晚分析）、源码 `src/prefill/prefill.cpp`/`src/core/verify.cpp`/`src/core/expert_source.cpp`。

## 2026-10-31 12:23（北京）gpu-unlock / gpu-unlock-ada 两个开源项目我们能用吗
**问**：Zagitta/gpu-unlock 与 JustAResearcher/gpu-unlock-ada，看看这两个开源项目，我们能用吗？

**结论**：**用不上，两个层面都不行。**
1. **它们是什么**：不是早年「破解 GRID vGPU 授权」的同名项目。它们是 **GeForce Tensor Core 算力解锁**——NVIDIA 把 GeForce 的 FP16/BF16/TF32（FP32 累加）tensor 路径限到半速（市场分层），这两个项目用一个 **initramfs 阶段、在 nvidia 驱动绑定显卡之前** 加载的内核模块，经签名 GPU firmware 写速度选择器（SS0 `0x88888888`/SS1 `0x8`）+ FLR，把峰值算力翻倍；驱动本体不打补丁、输出逐位一致。ada fork 加了实验性 Ada 目标，**RTX 4090 D（PCI ID 2685）在其列**（需 `rtx4090d_experimental=1`，证据=单 host 八卡的一次 PR 测试）。
2. **架构上装不了**：要改宿主机内核 cmdline（GRUB）+ 重建 initramfs + DKMS 编内核模块 + **整机断电冷启动**。我们 Pod `CapEff=0`、bounding set 里连 `cap_sys_module/cap_sys_admin/cap_sys_boot` 都没有；宿主机驱动 580.178.04 早已绑定三张卡，卡上还共置 ~41 个租户（GPU2 还有邻居的 TTS）。冷重启宿主机 = 全平台停服，踩「服务对外在线、停服须用户同意」红线；这事只有物理机管理员能做、也不该做。
3. **就算装上，对我们的负载收益≈0**：两条 README 都写明——量化模型（INT4/IQ 系）**无收益**、**decode（显存带宽 bound）无收益**；ada fork 自己的实测证据恰是「quantized MoE serving workload 无提升（8K prefill 0.983×）」。我们在产精度是 **IQ3_XXS 量化 GGUF + 专家 CPU 卸载 + int8 KV**，而本机源码级实测解码 94% 时间在等专家（PCIe/主机内存带宽 bound）——解锁只提 GPU tensor 发射率，碰不到 PCIe 带宽。4090 本身「先撞功率墙再撞算力墙」，大 GEMM 远吃不到 2×。唯一沾边的受益面是 **bf16 稠密训练**（llm-chat-train 的 minillm 跑在 GPU 上，cuBLAS 实测按 shape +12~34%）和无量化的 FP16 prefill——都不是当前并发/TTFT 瓶颈。
4. **风险**：4090 D 路径标注 experimental；firmware 序列失败会保留 DMA buffer、**卡必须断电才能恢复**——在共享宿主机上等于帮倒忙弄挂别人的卡。规避 NVIDIA 算力分层在 EULA 层面也是灰色地带。
**出处**：两仓库 README（经 gh-proxy 拉取）、`deploy/engines/profiles/ssebed/strata-b.json`（IQ3_XXS 实态）、`/proc/self/status` CapBnd、memory [[decode-expert-bandwidth-bound]] [[strata-concurrency-limits]] [[gpu2-tts-neighbor]]。

## 2026-10-31 13:3x（北京）MTP 深度扫描第一轮炸出第 7 处「8 行上限」的完整体
**问题**：按用户要求扫 MTP 深度 2/3/4/5（lane B，`--mtp-chain-floor`+`--max-rows 20`），第一轮 A2-A4 全部窗口 `why=chain_off`，链被永久关闭——为什么？
**结论**：
- 真凶 = `verify.cpp:784` 批量路由 GEMV `bf16_gemv_fp32_mmvf_multi` **整窗直传行数**；该内核只按 NT=4/8 模板特化，>8 行抛 invalid_argument（即 P2 报告说的「第 7 处隐藏 8 行上限」`native_bf16.cu:254`）。P2 当年给 shared_expert_multi 和 native_mmvq 都做了 8 行分块（verify.cpp:819 `kSharedMaxT`），**唯独漏了路由这处**。行预算 ≤8（现网默认）永远踩不到；一旦抬 `--max-rows>8` 想做深投机，第一个宽链窗就炸，且异常处理置 `chain_off=true` **进程终身**——此后连窄窗的链也不跑（step-stats 里连 S1 窗都是 chain_off）。
- 修法 = 同处方：路由 GEMV+top10 按 `kRouterMaxT=8` 行分块（每行点积独立、顺序不变 ⇒ 逐位一致）。已修+重编（`cd158f23`）+归档 `deploy/engines/bin/mixed-e1-20261031/`，gate/iq/mmvq parity 4/4 绿。
- A0 基线腿分解首轮读数（S=4 组）：规划器 100% 选 `varlen_l1`（4011/4011 窗无投机）；被历史保留的 chain S4L2 窗 tok/ms≈0.094 > batch S4L1 的 0.0775 → varlen 在 S=4 锁 L=1 **判错了**，深度扫描（二/三轮）定量裁决。
- 工具教训：重启等待不能把 vision 瞬崩（supervisor autorestart 会在 <1min 自愈）当失败重踢——第二轮因此自杀；等待逻辑应「只认 health、STOPPED/FATAL 且 3min 无响应才补 start」。引擎重载实测 ~470s（arena 冷读 0.09 GiB/s）。
**出处**：`logs/mtp-sweep-20261031-round1-broken.json`（chain_off 证据）、`SSEStraBed/logs/strata-iq3_xxs-b.log`（"chain window failed (verify router: bf16_gemv…)"）、源码 `src/core/verify.cpp:784`、`src/kernels/cuda/native_bf16.cu:254`。

## 2026-10-31 17:5x（北京）宽窗实验的三个次级发现（引擎挂起点诊断过程）
**问题**：深度扫描 v4/v5 臂"引擎死亡一小时不恢复"，宽窗到底崩在哪？
**结论**：
1. **不是宽窗崩**：stall 报告点名 `reading the prompt (batched): layer 1 of the prompt chunk`（批量 prefill 的 PLE 批路径挂 60s），且同类挂起在扫描之前的 b7 生产日志里已有（行 103197/217547，早于扫描区间）——**b7 既有的稀有 prefill 挂起点**，看门狗（issue #29）主动杀引擎自保；剩余嫌疑=ple_gather 的 std::async SSD 读在内存压力下不返回（当时 RAM avail 67GiB、major faults 4001，未定案，待复现）。
2. **serve 重启路径被一个空指针打死**（已修，`Strata/serve/server.py:454`）：引擎死后 `_say_died → death_note → self.proc.poll()` 在 `proc is None` 的竞态里抛 AttributeError——报告死因的路径自己崩掉，放大成"一小时不恢复"。
3. **`CUDA_LAUNCH_BLOCKING=1` 对本引擎不可用**：与每层门铃协议天然死锁（LB 让 launch 同步等内容完成，而完成要主机抬 flag，主机却被 LB 卡在 launch 里）；诊断该引擎的异步 CUDA 错误要用别的手段（launch 后逐调用查 cudaGetLastError 的调试开关，或分半二分）。
**出处**：`SSEStraBed/logs/strata-iq3_xxs-b.log`（stall report 全文）、`~/logs/wide-diag.log`、修复见 server.py:454 注释。

## 2026-10-31 晚（北京）双卡互联真相 + MTP 深度定版实测
**问题①**：两张卡是不是"数据线连着"、数据共享？"GPU 干成两份 PD 并行"行不行？
**结论**：4090D 物理无 NVLink；`nvidia-smi topo -m` GPU0↔GPU1=**SYS**（跨 socket PCIe+UPI）；`cudaDeviceCanAccessPeer` 全 0（GeForce 驱动禁 P2P）——**一切卡间数据走主机内存双跳**。引擎已按此设计（peer_experts pinned 中转、KV 权威池在主机）。空间 PD 分离在双卡各占一 lane 的现状下不成立（权重不可复制、无 SM 硬配额、缓存互踩）；"自动调节"的正确形态是 token 预算的时间切分（Sarathi 混合批），本引擎已有粗版 prefill-yield，细版=已实现默认关的 `--mixed-batch=interleave`（E1-C1/C2，层粒度暂停门 + 借用区准入钳制 `set_reserve_tail`）。
**问题②**：多并发 MTP 深度 2/3/4/5 哪个好？（受控五臂，lane B，floor 强拉+逐臂钉行预算）
**结论**：**深度 2 定版**（8 行预算内）：贪心 4 流 23.5→**51.1 tok/s (+117%)**、采样 4 流 +80%、2 流 +30%；已 `--mtp-chain-floor 2` 灰度普道。≥3 需宽窗（S=4 L=3=12 行），宽窗在修完 mmvf 三处（路由/索引K/索引Q，P2 漏改）后**仍有静默挂死点**（无异常、看门狗杀）——深度收益本就被接受率衰减吃掉（S1: L2 0.090 > L3 0.085 ≈ L4 0.086；S2: L2 0.100 > L4 0.082），宽窗挂死列为独立课题再攻。
**问题③**：为何 B 重载时公网 chat 会挂 25s+？
**结论**：serve 先监听、hold 请求到引擎 loaded，TCP 探活失明；rag-proxy 已加 `lane_ready()` 就绪门（model_routes/粘性/least-load/failover 四处，`lane_ready_gate:false` 回退）。附带修了 serve 重启路径 death_note 空指针（引擎死后"自动重启"曾被它打死）与记录了 serve close() BrokenPipe 崩溃循环（未修，仅噪音）、b7 既有批量 prefill layer-1 稀有挂起（未修，课题）。
**出处**：`logs/mtp-sweep-20261031*.json`、`archive/patches/mtp-depth-20261031/`（+MinIO `engine-mtp-depth/2026-10-31/`）、`docs/plans/PREFILL-CONCURRENCY-PLAN-2026-10-21.md` 追加两节、nvidia-smi topo/cuda 探针。

## 2026-11-01 14:4x（北京）「先了解网页端的情况」——前端全景摸底 + 实测出的 4 个问题
**问题**：用户说网页端有问题、后续要改，先摸清现状（架构/数据流/发布链/测试台/已知坑）。
**结论**：
- **三层链路（谁发什么）**：浏览器只连 `rag-proxy :8080`（唯一公网面）→ 它**不产生任何静态字节**，`/`、`/web/*`、`/fonts/*` 全透传给某条 lane 引擎；真正发文件的是 `Strata/serve/server.py`（`/web/` 白名单只有 `.css/.js/.svg` 且必须扁平名，`/fonts/` 只收 `.woff2`，**`/web/index.html` 必 404**、首页只能走 `/`）。SPA 用 hash 路由，`fetch("metrics")` 这类相对路径都解析成 `/metrics`。
- **页面结构**：6 个标签页（对话/监控/记录/推理可视化/训练/关于）+ 应用内登录页；`src` 45 个文件 8,316 行。数据来源分工：`/metrics` 1s（引擎八张卡+请求表，经代理按路径哈希只落到一条道）、`/concurrency` 2s（代理侧两条道聚合+在途台账，前端自己留 90 点≈3min 折线历史）、`/mcp` 10s、`/health` 一次性、训练页 `/web/train-data.js` 用 `<script>` 注入轮询（5s）。
- **线上产物=当前源码**：`md5 app-Dal4wrM6.js` 与 `npx vite build` 到暂存目录的结果逐位一致（`6cd4400c…`）；构建 52s，产物 gzip 后 app 96.7KB + chunk 25.1KB + css 8.76KB。**但整个 Oct 28/29 的前端改动（训练页、通道选择器、并行流、temp 默认 0）在工作区未提交**，git 里前端最新提交只到 `11dee9a0`。
- **词表完整**：zh/en 各 **562** 条，模板实际引用 446 个键，**零缺失、零单边**（脚本比对 src 全量 `t("…")`）。
- **实测出的 4 个问题**（都有复现命令，见出处）：
  1. **网页端图片附件被永久禁用**：网关 `health()` 的返回里**没有 `images` 键**（实测 8080 `/health` 顶层键 = status/uptime_s/upstream/upstreams/model_routes/infer_store/sessions/embed_*/counters/config），而 `store.js:53` 出厂默认 `images:false` → `ChatView.vue:256/292` 判定「本模型不支持图片」，贴图/粘贴直接被 toast 挡掉。**但引擎明明支持**：8092 `/health` 实测 `images:true`，且 `8080/metrics` 的 `engine.images=true`（纯前端改这一处即可放开，不用重启代理）。
  2. **登录页填 token 后的「校验」是假的**：`LoginView.vue:32` 拿 `GET /health` 的 200 当 token 有效。实测错 token：`/health` `/metrics` `/concurrency` `/v1/models` **全 200**（这些是设计上"不登录也能用"的开放/代持路径），只有 `GET /v1/chat/completions` 回 **401**（正确 token 时是 404，因为引擎没有 GET handler）—— 所以改用 GET 对话端点探测，`status!==401` 即有效，**零 GPU 成本**。
  3. **文档附件白名单落后于网关能力**：网关 `doc_extract.py` 早就支持 PDF/DOCX/XLSX/XLS/CSV（2026-10-19 上线，base64/data URL，256K 预算头尾截断），但 `ChatView.vue:247` 的 `TEXT_EXT` 白名单不含这些扩展名 → 网页上传 PDF 会被「只能附文本文件」拒绝，用户只能去贴纯文本。
  4. **训练页时间单位会在跑完后变难看**：`TrainView.vue` 的 `train.updated` 用**秒**报「{s} 秒前」，而 chat-v8 那场训练 10-29 13:14（北京）就结束了 → 现在 `age≈267,000 秒`；又因 `finished:true` 分支排在 stale 判定之前，页面显示「已结束」+ 一个巨大的秒数，不说「几天前」。
- **测试台用法坑（这次踩到）**：`tools/smoke.mjs` 的第 1 个参数是**已构建的产物目录**（`ROOT=argv[2]`，脚本自己起 8123 端口的 mock 后端读那个目录），不是截图目录 —— `/tmp` 被清过之后直接跑必然「找不到 `.st-header__brand`」。正确姿势：`npx vite build --outDir /tmp/xxx --emptyOutDir && node tools/smoke.mjs /tmp/xxx`。另外 puppeteer 的 Chromium 与系统库都在（`ldd` 报 0 missing），但**冷启动在宿主机高负载下会撞 30s 的 "WS endpoint" 超时**（当时 load 52→58），重试即过。
**出处**：`Strata/serve/webapp/{README.md,src/*}`、`rag-proxy/rag_proxy.py`（`health()` 2597-2621、`_ALWAYS_OPEN`/`_READ_OPEN`/`_gate` 1663-1700、`concurrency()` 2558-2594、`route_upstream` 246-266）、`Strata/serve/server.py`（`/web` 2551-2568、`/fonts` 2536-2550、首页 2592-2598、`svc.metrics` 1646-1683）；实测：8080 `/health` `/metrics` `/concurrency` 与 8091/8092 `/health`（curl + 真 key），错/对 token 的 401/404 矩阵，`web/train-data.js`（finished 于 1793250857），`docs/reports/DOCUMENT-GATEWAY-EXTRACT-2026-10-19.md`。

## 2026-11-01 15:3x（北京）手机端显示：整页被撑到 752px 的根因与修法（已发布）
**问题**：用户要求「先优化手机端显示」。390px 下对话页整页被缩小、右边一片空白。
**根因（四处，全部实测定位，不是猜）**：
1. `.view`（`main` 的 flex 子项）**缺 `min-width:0`** —— 默认 `min-width:auto` 拒绝收缩到内容 min-content 以下，内容里任何一个不换行的长串都会决定整页宽度。`.app`/`main`/`.records`/`.viz` 早就各自补过，**`.view` 这一层漏了**（同一个坑第四次踩）。
2. **助手气泡没有 `overflow-wrap`**（只有用户气泡有）→ 模型吐一段 300 字符不间断串 / 长 URL / base64，`min-content` 直接顶到 **752px**（页头仍按 390 布局，所以右边是空白）。
3. **输入条写死 `nowrap`**：4×33 图标 + 通道选择器 120（2026-10-28 新加）+ 键盘提示 93 + 发送 38 + 间距 48 = **433px** 的 min-content → 即使没有脏内容，390px 也放不下（整页 479px）。
4. 回答里的**宽 markdown 表格**没有滚动容器，同样撑页。
**修法**：`.view{min-width:0}`；助手气泡加 `overflow-wrap:anywhere`；≤720px 时 `.st-composer__bar{flex-wrap:wrap}` 且**收掉「Shift+Enter：换行」提示**（触屏没有 Shift，它白占 93px）；`.st-bubble table{display:block;max-width:100%;overflow-x:auto}` 让表格在气泡里滚。
**踩到的两个反直觉点**：
- **`overflow-wrap:anywhere` 会参与内在宽度计算**：给气泡加它之后，宽表格不再横向滚、而是被压成「一列一个词」咬碎。单元格必须退回 `break-word`（不影响内在宽度）—— 表格该宽还是宽，交给外层滚。
- **测试台两处「空过」**：① isMobile 模拟下 `window.innerWidth` 会被溢出内容顶大（390 的屏报 480），拿它当越界阈值 = 对溢出本身是瞎的 → 必须用 `documentElement.clientWidth` + `body.scrollWidth`；② 重载时停在 `#monitor`，对话页被 `v-show` 藏掉 → 气泡宽度全是 0，断言照样 PASS。所以守卫里加了「必须出现有宽度的气泡」。
**实测前后**（真实 8080，脏内容会话）：390px `body.scrollWidth` **752 → 390**，320px 同样归位；宽表格 `w=565 不滚` → **`w=324 横向滚=true`**；6 个标签页 × 两档视口全部 `body.scrollWidth == clientWidth`，无 JS 报错。回归：`smoke.mjs` **206/206**（新增 4 条手机端守卫），`verify-live.mjs` 对真实服务 ALL PASS。
**发布**：`bash tools/deploy.sh`（引擎按请求读盘，**没重启任何进程**）→ 线上产物 `app-Ajaat9Yr.js` / `style-gtonip8P.css` / `chunk-Dn6He-A8.js` + `chunk-QBqT8ZKz.js`；`train-data.js` 未被清理逻辑误删。
**留给下一轮**：只带 API key、无登录会话的访客在记录页看到的全是「–」（stats 空）—— 待查是按会话过滤还是接口没回数据。
**出处**：`Strata/serve/webapp/public/app.css`（`.view` / `.st-bubble` / ≤720 媒体查询）、`tools/smoke.mjs`（手机端段 + 脏内容守卫）、`src/App.vue`（标签加 `title`，窄屏只剩图标时长按可见名字）；探针脚本为一次性、已删。

## 2026-11-01 19:0x（北京）「现在如何访问前端面板」——两个公网入口都可用，且域名根路径已改成我们的前端
**问题**：用户问现在从哪儿进前端面板。
**结论（实测，不是背旧笔记）**：
- **两个地址都能进、而且是同一份产物**：`https://ai.ssemarket.cn/` 与 `https://ssemarket.cn:23034/` 的 `index.html` 与本机 `127.0.0.1:8080/` **逐字节相同**（各 1485B，都引 `app-Ajaat9Yr.js`，即 15:3x 刚发布的那版手机端修复）；`/web/app-Ajaat9Yr.js` 在两域名都 200 / 276619B。
- **⚠️ 过期认知纠正**：旧结论（memory `strata-rag-proxy`）说 `ai.ssemarket.cn` 根路径返回**平台的**「SSE_Market API 开发平台」页、只有 `/v1` 转给我们。**现在根路径已经是我们的前端** —— 发网页地址可以用 `https://ai.ssemarket.cn/` 了（不用再带 23034 端口）。
- **进页面 ≠ 能对话**：面板是**应用内一页**（`#login`，`auth.js:17`），六个标签（对话/监控/记录/推理可视化/训练/关于，`store.js:34`）不登录也能浏览；要对话/看内容得有身份，两条路：
  1. **软工集市账号 OAuth**（普通用户，服务端代持引擎 key）：`GET /auth/login` 实测 **302 → `https://ssemarket.cn/new/connect?appid=ssestrabed&redirect_uri=https://ai.ssemarket.cn/oauth/callback`**，`/auth/config` 回 `{"configured": true, "app_name": "软工集市"}` ⇒ 应用已登记，这条路是通的；
  2. **填 API token**（开发者/压测）：登录页下方输入框，等价原来的 Bearer/x-api-key 用法。
- **链路照旧**：浏览器 → `rag-proxy :8080`（唯一公网面，透传静态文件+代持 key）→ lane A `127.0.0.1:8091`（快道）/ lane B `127.0.0.1:8092`（普道），两条 `/health` 实测 `upstreams` 全 ok，`model_routes` 另有别名 `qwen3.8-flash`→8092、`qwen3.8-flash-fast`→8091。
- **别依赖 `~/deploy/context.json`**：本次 pod 重建后仍然不存在（`ls` 报 No such file），公网端口只能实测出来的 23034 或走域名。
**出处**：实测 `curl` 三处 index.html diff 相同、`/web/*.js` 与 `/v1/models` `/concurrency` 状态码、`/auth/config` + `/auth/login` 302；代码 `webapp/src/auth.js:67`（`location.href="/auth/login"`）、`webapp/src/views/LoginView.vue:5-7/59/69-74`、`rag-proxy/rag_proxy.py:1670-1747`（`_is_auth_path`、`/auth/login`→`OAUTH.authorize_url`）、`webapp/src/store.js:34`。

## 2026-11-01 19:2x（北京）「两张卡是不是只能监控一张？」——监控页 GPU 数据的三层真相
**问题**：后台两张 GPU，当前监控功能是否只看得到一张卡？
**结论**：**「指标卡」这一层确实只有一张卡，但另一张的数据已经在报文里、只是前端没画。**
- **① 引擎侧天然一进程一卡**：lane A profile `gpu: 0`（快道）、lane B profile `gpu: 1`（普道，`deploy/engines/profiles/ssebed/strata-*.json`）；`/metrics` 的 `hardware.gpu_*` 是**标量**（util/mem_used/temp/power/pcie…），`hardware_static.gpu_count = 1`。⇒ 想让单条 `/metrics` 报全机卡是**做不到**的，必须代理聚合。
- **② 监控页那八张指标卡（含 GPU 利用率/显存/温度/功耗折线）只反映一条道**：数据源 `/metrics`，而代理把 `"/metrics"` 当**会话键**丢进 `pick_upstream()` 的粘性最少负载路由（`rag_proxy.py:216-238`，TTL `sticky_ttl_s=1800`，可 `least_load_routing:false` 回纯哈希）→ 当前 `proxy.metrics_lane = 127.0.0.1:8092`（普道/GPU1）。**坑**：切道后 `history` 是**引擎侧**的采样环，前端 `h.value.gpu_util` 直接画 → **折线会在两张卡之间无声换卡**；且通道标签 `metricsLaneLabel` 只挂在「模型状态」卡标题上（`MonitorView.vue:356`），**八张指标卡本身不标自己是谁家的卡**。
- **③ 但两条卡的完整数据已经到手了**：代理每 2s 轮询各通道 `/v1/status`+`/metrics`，`/concurrency` 回 `lanes[].gpu = {name, used_mib, total_mib, util_pct, temp_c, power_w}`（实测 快道 `1961/24564 MiB, util 0, 27℃, 14.8W`、普道 `23763/24564 MiB, 28℃, 28.1W`）。前端只用了 `used/total` 做「显存」文本（`MonitorView.vue:47 vramText`），**`util/temp/power` 全丢**；`store.js:169-177` 的 `concHistory` 采样只留 `tok_s / in_flight / state`，**没留 gpu** → 所以画不出逐卡通电历史。⇒ **补逐卡曲线 = 纯前端改动（采样加 gpu 字段 + 每 lane 一组卡），零代理/零引擎 = 不用重启、不碰对外服务。**
- **宿主机其实是三张卡，前端一张都没读全**：`nvidia-smi` 与 `/shared/pressure/pressure.json` 的 `gpu_cards` 都给 3 张（index 0/1/2；GPU2 mem 0.438 = 邻居 pod 的 GPT-SoVITS TTS，见 memory `gpu2-tts-neighbor`）。`src/` 里 grep `pressure` **零命中** → 第三张卡完全不在面板上；要显示它得**加代理接口（=重启）**，或走训练页那套「`serve/web/*.js` 静态喂数据」的零重启路子。
- **顺带澄清**：「推理可视化」页里的 `lanes` 是 **MoE 专家/注意力头的并行支路**（`IsoView.vue:364-370`），与 GPU 物理卡无关，别当成多卡视图。
- 另注：直连引擎 `8091/8092` 的 `/metrics` 会 **401**（`missing or wrong API key`），只有经代理（代持 key）才拿得到。
**出处**：实测 `127.0.0.1:8080/metrics`（`hardware_static.gpu_count=1`）、`/concurrency`（两 lane 的 `gpu` + `proxy.metrics_lane`）、`nvidia-smi` 三卡、`/shared/pressure/pressure.json` `gpu_cards`；代码 `rag_proxy.py:216-238/2594`、`serve/webapp/src/views/MonitorView.vue:18-19/27-36/47/171-178/356`、`src/store.js:169-177`、`src/viz/components/IsoView.vue:364-370`、`deploy/engines/profiles/ssebed/strata-{a,b}.json`。

## 2026-11-01 20:2x（北京）逐卡 GPU 监控已上线（纯前端、零重启）——落地记录与三条会再踩的约束
**做了什么**：监控页并发区块下新增「逐卡 GPU」：**一条通道一张卡**（负载 % / 显存 GB / 温度 °C / 功耗 W 四格 + 卡型 + 状态徽章 + 与折线同色的色块），下面 **四张 `LineChart`（2×2）**，一条线一张卡、一轴一图不叠双轴。GPU2（邻居 TTS）按用户要求**不接**。
**改动面（4 个文件，全在 `Strata/serve/webapp`）**：`src/store.js`（`concHistory` 采样加 `gpu`）、`src/views/MonitorView.vue`（`hasGpu/gpuLaneList/gpuLanes` + 四条 series + 模板 + scoped CSS + 手机端两列）、`src/i18n.js`（`mon.gpu.{title,sub,note,none,chart.max}`，zh/en 各加 5 条 → **567/567 零单边**）、`tools/{smoke.mjs,verify-live.mjs}`。**代理与引擎零改动 ⇒ 没重启任何进程**，`bash tools/deploy.sh` 发布（产物 `app-ZkIqrfxP.js` / `style-BoG1o0Ap.css` / `chunk-fR3zQe8I.js` / `chunk-BGgeUF5o.js`；`train-data.js` 未被清理逻辑误删）。发布前把当时的 `serve/web/` 整份快照到 **`~/archive/web-before-percard-gpu-2026-11-01/`**（回滚 = 拷回去，引擎不重启）。
**三条以后再动这块必踩的约束（都写成断言钉住了）**：
1. **别复用 `.metric-card` 类名** —— 监控页有断言「八张指标卡 === 8」，逐卡四格用自有的 `.gpu-metric`；
2. **新图必须放 `.gpu-charts`，不能塞进 `.conc-charts`** —— 后者钉死 `.lc svg === 2`（速度 / 在途两张）；
3. **四张图用固定两列**（`repeat(2, minmax(0,1fr))`，≤900px 一列）。`auto-fit minmax(320px,1fr)` 在 1440 下排成 **3+1 孤儿行**（截图实测），smoke 里加了 `cols===2 && rows===2 && widths===1` 的断言把这次改版钉住。
**一个必须讲清口径的地方：掉线时不许冒充现值。** 代理的 `LANE_CACHE` 在通道出错时**只加 `error`、不清 `status`**（`rag_proxy.py` `concurrency()`：`up = bool(st) and not c.get("error")`），所以 `up:false` 那条通道的 `gpu` 会**停在最后一次成功**的读数。`gpuSample()` 对 `!l.up` 直接记 `null` ⇒ 曲线**断线**而不是画一条假的平线（与 `LineChart` 的既有纪律一致：缺测抬笔，0 是个真实的值、会撒谎）。
**验证（全部真跑，不是「应该没问题」）**：`smoke.mjs` 多路 **222/222 ALL PASS**、单路（`SINGLE=1`，只有一条通道 + 无 `/concurrency` 的降级路径）**221/221 ALL PASS**；`verify-live.mjs` 对真实 8080 **ALL PASS**（新增 9 条逐卡断言，**页面无 JS 报错**）。真实读数对得上报文：快道 `0% / 1.9 GB / 27°C / 15W`（1961 MiB）、普道 `0% / 23.2 GB / 27°C / 26W`（23763 MiB）；32 秒采样攒出 17 个点/线。断言口径刻意用「**DOM 读数 == `/concurrency` 报文读数**」而不是「读数该是多少」—— 机器负载会变，但页面不许在中间偷换数字（含 MiB→GB 换算 ±0.1）。
**顺带发现（未处理，可查）**：`/concurrency` 现报 普道 `in_flight 323–336`（`服务槽位 336/4`），聚合「在途 323 / 生成中 314」，而两条引擎的 GPU 负载都是 0%、`engine_in_flight` 远小于此 —— 代理侧在途登记表疑似**有未回收的条目**（连接没走 finish 分支？）。与本功能无关，但数字很扎眼。
**出处**：`Strata/serve/webapp/{src/store.js,src/views/MonitorView.vue,src/i18n.js,tools/smoke.mjs,tools/verify-live.mjs}`；`/tmp/smoke-multi.log`、`/tmp/smoke-single.log`（本次会话内有效）；`~/archive/web-before-percard-gpu-2026-11-01/`；线上 `https://ai.ssemarket.cn/web/app-ZkIqrfxP.js` 200。
**未做**：没提交 git（用户没要求；这块与 Oct 28 起的前端改动都还在工作区）；GPU2 与 `pressure.json` 未接入。

## 2026-11-01 20:2x–20:5x（北京）「网页 502 Bad Gateway (nginx/1.31.4)」——中继瞬断 + 两条 lane 引擎先后挂掉的完整复盘
**问题**：用户报 `https://ai.ssemarket.cn/` 显示 502（nginx/1.31.4 报错页）。
**结论**：502 页面本身是**中继侧 nginx 生成的**（我们的代理是 Python，不产 nginx 页）⇒ 断点在平台链路某一跳；但查下去发现**真正让服务不可用的是容器里两条 lane 引擎先后挂了**，502 是表象窗口期的症状之一：
- **时间线（北京时间）**：17:16 lane B 引擎由 server.py 重启（原因未留痕，`strata-b.out.log` 无 exit 记录）并正常服务到 ~20:18；**19:02 lane A 引擎在 prefill 中 SIGSEGV（exit -11）**（`deploy/logs/strata-a.out.log` 末段：8,192/8,222 tokens 读完即崩），server.py 只在「下一个请求」才重拉引擎 ⇒ A 挂 90 分钟无人触发；**~20:18–20:19 lane B 卡死**：一条 ~3,780-token 请求进入 prefill 死循环，`strata-b.out.log` 以 ~200 行/秒刷 `reading the prompt: 3,780 tokens, 39 s so far`，210% CPU、GPU util 0，随后 8092 对新连接直接 RemoteDisconnected（20:23–20:30 留档多条 disconnected）⇒ **两条 lane 全挂，网页可打开（静态由代理本机供）但对话全挂**。
- **每 2 分钟的 TimeoutError 之谜**：`undici`（Node HTTP 客户端 = 软工集市平台后端，走公网进来）一直按 ~2 分钟节奏 POST chat completions、5s 超时即断（`rag-proxy.out.log` 成对的 TimeoutError+BrokenPipe、`infer.db` 里 ua=undici 每 ~1m50s 一条）。B 卡死后这些请求全超时 ⇒ 之前看到的「可疑定时流量」其实就是**集市平台的正常业务调用在撞墙**。恢复后它们重新 200。
- **恢复动作（经用户批准「立即 kill B」）**：kill -9 卡死引擎(2537826)后 server.py 自己没醒（其计时器还在给已死引擎打「reading the prompt 80→131s」）⇒ 改走 `supervisorctl -c /home/cloud/deploy/supervisord.conf restart strata-b`（首次 supervisorctl 要带 -c，裸跑连不上 9001）；lane A 由小请求踢了「下个请求自动重拉」（引擎加载 40 GiB 专家 arena ~5 分钟，0.12–0.18 GiB/s）。两 lane `/health loaded:true` 后公网 `/v1/chat/completions`（qwen3.8-flash 与 -fast 各一发）实测通。公网 GET / 早在 20:26 就自行恢复 200（连续 8 次）——即中继瞬断与 B 卡死几乎同时结束。
- **代理侧隐患（本窗实测）**：8080 积了 **334 条 ESTABLISHED**（keep-alive 无 idle 回收，`ThreadingHTTPServer`+`protocol_version=HTTP/1.1`），411 线程、FD 695/1024；与前一 session 在 /concurrency 看到的「在途 323–336/服务槽 4」同源 = **代理在途登记表未回收**（连接异常断掉没走 finish 分支）。再积会撞 FD 上限造成真 502。
- **两个未解问题**：① lane A 引擎 **SIGSEGV 是真 bug**（build 7，prefill 末段崩，此前 17:16 B 的无声重启疑似同类）；② lane B 的 3,780-token prefill 死循环触发输入未定位（该请求在 seg 归档里，request.sent 字段为空壳，需要看引擎侧日志或复放）。**deadlock_watch 看门狗反而先死**：lane A 引擎消失时它拿 pid "0" 去读 `/proc/0/task` 抛 FileNotFoundError 整个退出（`tools/deadlock_watch.py:67`），没起到兜底作用。
**出处**：`deploy/logs/rag-proxy.out.log`、`strata-{a,b}.out.log`、`~/logs/infer.db`（ua/状态分组）、`SSEStraBed/logs/strata-iq3_xxs-{a,b}.log`（arena 加载进度、SIGSEGV 后重启）、`/shared/pressure/pressure.json`（GPU util 0、psi.io 0.25）、实测 ss/curl。MinIO 对象：`infer/2026-11-01/seg-204012-c64aa.jsonl.gz`（卡死窗口原始留档）。

## 2026-11-01 21:5x（北京）「1 和 2 都修」——看门狗防自杀重写 + 代理连接超时上线（含部署实测）
**问题**：复盘留下的两个隐患要修：① deadlock_watch 引擎缺失时自己崩；② 代理 334 条连接/411 线程/FD 逼近上限。
**① 看门狗（`tools/deadlock_watch.py` 重写核心，取证逻辑不变）**：
- **绝不自杀**：主循环每道 `try/except` 包住，`dump()` 对 pid=None 走「引擎不存在」分支（写 dmesg/日志尾部而非 `/proc/0/task`）。v1 死因 = `engine_pids()` 拿 environ 里的 `step-stats-a/b` 标记认道，引擎重启方式一变就认不出 → 回退 `"0"` → `os.listdir('/proc/0/task')` FileNotFoundError 打穿 main()（`deadlock_watch.py:67` 当年不在 try 里）。
- **pid 归属改走可信父链**：扫 /proc 找 `serve/server.py --port 809x`，再取其 `/proc/<pid>/task/<pid>/children` 里的 `build/strata --serve`，不再依赖环境变量标记。
- **新增引擎缺失检测**：`/health loaded=false` 连续 4 轮(60s) ⇒ 报警。**自动踢=默认关**（`DEADLOCK_KICK=1` 才发最小请求触发 server.py 自动重拉——重拉仍是服务动作，遵守红线，人工确认后再开）。
- 已拉起：`nohup python3 -u tools/deadlock_watch.py >> ~/logs/deadlock-watch.log 2>&1 &`。**上线 20 分钟即抓到真案例**：A 引擎第三次崩溃（见下），看门狗存活并按预期报警。
**② 代理连接超时（`rag-proxy/rag_proxy.py` 三处小改）**：
- `Handler.timeout=180`（main() 里按配置 `client_timeout_s` 覆盖，0/负=关闭回滚）：StreamRequestHandler.setup 对每个连接 `settimeout` ⇒ 空闲 keep-alive 在 `handle_one_request` 里被标准库转成优雅关闭；**吊死的 wfile.write（死客户端不回 FIN）也会抛 TimeoutError 让线程退出**——这是 334 条 ESTABLISHED/411 线程/FD 695 的根（清道夫只结台账、叫不醒阻塞的线程）。
- `Handler.handle_error` 静音 Timeout/BrokenPipe/ConnectionReset 类 traceback（此前 err.log 9MB 大半是它们）。
- 启动日志带 `client_timeout=` 便于核对。
**验证（全真跑）**：隔离实例(:18085, timeout=4s)实测启动日志 `client_timeout=4.0`；`test_window_guard.py` 对改动后代码 **ALL PASS (5s)**（B 组真起 rag_proxy+mock）；生产 supervisorctl restart rag-proxy（经用户批准，闪断数秒）后：**ESTABLISHED 334→5**、公网 `GET /` 200、`client_timeout=180.0` 在案。**顺带更正一个此前的误判**：代理对每个代理响应都发 `Connection: close`（`rag_proxy.py:1830`），所以积压不是"空闲复用"而是**未完结的在途流线程**；窗口期 323–336 条在途会靠 urlopen 600s 超时自解，但卡死的线程/FD 只有 socket 超时才能救——本次改动正对着它。
**遗留（未修，属引擎）**：A 通道引擎今晚反复崩（19:02 SIGSEGV → 20:4x 再崩 → 21:4x 第三次，每次活 <60 分钟），B 引擎 20:37 起重载一次后稳定；`dmesg` 无权限看 OOM 记录。**建议下一步**：拿 `SSEStraBed/logs/strata-iq3_xxs-a.log` 三次崩溃尾部对时间轴找共性，或考虑 `DEADLOCK_KICK=1` 让缺失自动恢复（代价：崩溃后 5 分钟不可用变成"自动重载 5 分钟"，无感但吃 RAM/IO）。
**出处**：`tools/deadlock_watch.py`（重写段 (c)/`engine_pids`/`dump` 守卫）、`rag-proxy/rag_proxy.py`（`client_timeout_s` 默认 138 行区、`Handler.timeout/handle_error`、`main()` 覆盖）、`~/logs/deadlock-watch.log`（21:5x lane a 报警实录）、`test_window_guard.py` ALL PASS、实测 ss/curl/proc。

## 2026-11-02 10:5x（北京）「监控页底部只有一张卡的 GPU 数据、还不知道是哪张卡」——数据源链路、卡号来源与按卡拆开
**问题**：前端监控页底部显示 GPU 详细数据那块只有一张卡的数，而且说不出是哪张卡，要求能显示两张卡。
**结论（为什么原本只有一张卡）**：不是 bug，是**数据源**决定的。底部那五张指标卡（GPU 负载/显存/温度/功耗/PCIe）读的是引擎 `/metrics` 的 `hardware.gpu_*` —— 那是**标量**、只覆盖「这个进程所在的那张卡」；而代理把 `"/metrics"` 当**会话键**做粘性最少负载路由（`rag-proxy/rag_proxy.py:216-238`，TTL 1800s），所以整个页面永远只能看到**一条通道那一张卡**，粘性过期换道时历史还会无声串到另一张卡上。
**卡号（GPU 0 / GPU 1）从哪来 —— 整条 HTTP 链路上原本没有任何地方带它**：
- 引擎侧 `machine.gpu`（`serve/server.py` 的 `v1_status()`）只有 name/used_mib/total_mib/util_pct/temp_c/power_w；`hardware_static.gpu_count` 恒为 1。
- 唯一知道卡号的是**坐在卡上的那个进程**：lane profile 的 `gpu`（`~/deploy/engines/profiles/ssebed/strata-a.json` = 0、`strata-b.json` = 1）→ `server.py` 写进 `CUDA_VISIBLE_DEVICES`，同时 `svc.gpu_index/gpu_indices` 传给 `Telemetry`，NVML 按 nvidia-smi 的序号读那张卡。
- **不能按数值反推配对**：实测两条道各自那张卡 used_mib 23773 vs 23759（差 14 MiB）、两张同型号同容量（RTX 4090 D / 24564 MiB），配对就是猜；容器里还有第三张卡（GPU 2，邻居 GPT-SoVITS TTS 在用，显存 10.7 GB）。
**改法（三条路的取舍）**：① 侧信道探针写 `/web/gpu-data.js`（零停服，训练页那套）；② 改引擎上报 + 择机重启双 lane；③ 只改代理 + 重启 rag-proxy。**用户选 ②**（干净、且本就是「改造推理引擎」主线的一部分），所以：
- `serve/telemetry.py`：`Telemetry.sample()` 现在**一张卡也发** `gpus[]`，每项带 `index`/`name` 与全部原始读数（util/mem/temp/power/power_limit/pcie_*）；`static` 加 `gpu_indices`。聚合口径不变（多卡仍是 合计/均值/最热），老读者不受影响。
- `serve/server.py`：`v1_status()` 的 `machine` 加 `gpus[]`（新 `card()` 帮助函数换算成 MiB/%/°C/W），并给 `machine.gpu` 补 `power_limit_w`。
- `rag-proxy/rag_proxy.py`：`/concurrency` 的 lane 加 `"gpus": machine.get("gpus")`（老引擎 → None，前端自己降级）。
- 前端：`store.js` 的 `gpuSample()` 换成 `laneCards()`（一张卡一项、`up:false` 仍返回空，不许拿陈旧缓存冒充现值）；`MonitorView.vue` 底部新增「GPU 详细数据」= **一卡一组**（负载/显存/温度/功耗/PCIe 五行，每行一根该卡自己的曲线），指标卡只剩 速度(+预填)/CPU/磁盘 三张；上面「逐卡 GPU」区块标题改成**卡号**、旁边署通道名；「上下文占用」的温度行按卡列（没卡号时用通道名，不重复三遍「卡号未上报」）。
**两个实现上的坑（都留了注释）**：
1. **卡的身份键用「通道 + 第几张」，不用卡号**：卡号有时报有时不报（引擎升级前后），拿卡号当键会让同一张物理卡变成两张新卡、折线与配色重来。卡号只是**显示用的数据**，每帧从最新一份上报里取。
2. **卡号以「这一次上报」为准**：通道在线时它没报就是「卡号未上报」，**不沿用三分钟前的旧账** —— 换引擎/改 profile 会把同一条道挪到另一张卡上；通道掉线时才保留最后一次报到的号。
**生效边界（重要）**：前端是静态产物、**已发布上线**（`webapp/tools/deploy.sh`，引擎按请求读盘、不重启）⇒ 底部现在就是**两张卡各自一组**，只是标题为「卡号未上报」。卡号与逐卡 PCIe 要等**重启窗口**：`supervisorctl -c ~/deploy/supervisord.conf restart strata-a strata-b`（Python 侧改动 + 模型重载，分钟级不可用）与 `restart rag-proxy`（秒级）。**未重启，红线未动。**
**验证**：`webapp/tools/smoke.mjs` 多路 236 / 单路 235 全绿（新增夹具开关 `/__oldgpu?1` 专门演老引擎不上报卡号的样子）；`webapp/tools/verify-live.mjs` 对真实 8080 全绿（口径仍是「DOM 读数 == 报文读数」，且卡号断言对「已上报/未上报」两种状态都成立，重启后不用改脚本）。
**顺带发现（未修，属既有问题）**：`serve/test_server.py:1315` 断言 `web/index.html` 里要有 `"web/app.js"` —— Vue 重写（2026-10-15）后产物是 `/web/app-<hash>.js` **绝对路径**，该用例在本次改动前就已失败（HEAD 版 index.html 即引用 `app-Dio92p-D.js`）。它同时说明 #82 的「资源路径要相对、以便挂在带路径前缀的反代下」这个意图在重写时丢了，需要单独定夺，不在这次里顺手改掉。
**补一条口径（改的时候踩到过）**：显存的**单位在两条路上不一样** —— 代理 `/concurrency` 的 `lanes[].gpus[].used_mib` 已是 **MiB**，而引擎 `/metrics` 的 `hardware.gpus[].mem_used` 与 `hardware.gpu_mem_used` 是**字节**；前端统一收成「内部 MiB、显示 GB」。直连引擎（没有 `/concurrency`，代理不在或太旧）这条路 smoke 的夹具走不到（SINGLE 也只是「一条通道」），所以单独留了 `webapp/tools/check-direct-gpus.mjs` 钉住它 —— 夹具刻意用一张层切分（GPU 0 + GPU 2）的 `/metrics`，断言「一卡一组、各带曲线、指标卡只剩 3 张」。**别用 JS 的 `<<` 造字节数**：`20 << 30 == 0`、`18 << 30 == -2147483648`（32 位有符号移位），这次就是它把显存显示成 0.0 GB、看着像换算写错。

**追问：「上下文占用那块也不知道是哪张卡」**（同一轮，用户接着指出的）。同一个根：这块的 token 仪表与「常驻显存专家」都来自 `/metrics`，而 `/metrics` 被网关按**会话粘性**钉在一条通道上（`proxy.metrics_lane` 给出是哪条）⇒ 「6,226 · 10.1 GB」其实是**那一条道那一张卡**上的专家驻留，看着却像全机的。改法（纯前端）：① 标题署名 `上下文占用 · 普道 · GPU 1`（报文里 `metrics_lane` 是谁就写谁；直连引擎没有代理时写「本引擎」）；② 专家条的分母从 `hardware.gpu_mem_total`（粘性那条道的标量）换成**那张卡自己的容量**（层切分时 = 它那几张卡的合计，例：`10.1 GB / 48 GB`）；③ 温度行同样按卡列。实测：多路夹具 `上下文占用 · 普道 · GPU 1`，直连引擎夹具（层切分 GPU 0+GPU 2）`上下文占用 · 本引擎 · GPU 0 + GPU 2`。

**顺带修掉一个既有小毛病（用户点头后）：折线图 x 轴时间戳压叠**。症状：刚打开页面、折线只攒出两三个采样点时，轴上显示成 `03:583:53:52` 这种乱码。根因是 `LineChart.vue` 把刻度写死成「两端 + 中间」三个下标，而中间那个 = `floor((n-1)/2)` —— **n=2 时算出 0**，与左端同一个点，于是同一个 x 上画了两遍时间戳（窄屏/短窗口时也会因间距不够而糊在一起）。
**判据抽成纯函数**：`src/chart-axis.js` 的 `xTicks(n, iw)` —— 只回答「给定 n 个点、这么多像素，哪些下标配得上一个时间戳」。关键是**按标签实际占的区间判重叠**而不是「两个 x 坐标差多少」：贴左的往右长 62px、居中的两边各 31px，`start×middle` 这种撞法只有按区间才看得见（第一版按中心距写，宽 140px 时放过了 9px 的压叠）。挤不下就**少画一个**（先丢中点；连两端都摆不下就只留最新那一刻）。
**三处测试**：① `tools/test-axis.mjs`（无浏览器、秒出）扫 **n=1..120 × 宽度 20..1400 每 10px 一档 = 16,680 种组合**，断言下标不重复、标签互不压叠、不越出绘图区；② `smoke.mjs` 与 `verify-live.mjs` 各加一条 **DOM 级** `getBoundingClientRect` 重叠检查（桌面 6 张图、手机 6 张图、真实页 6 张图全 0 命中）—— 真实页那条专挑「刚加载完只有两三个点」的时刻。修完线上 x 轴恢复 `04:08:51 … 04:08:53`。

**再扫一遍全站，把同一类问题清干净**：关于页「本机」那节也只有一行 `GPU = RTX 4090 D, 24 GB`，来源同样是粘性的 `/metrics` ⇒ 双卡部署下既像「只有一张卡」又说不清是哪张。改成**一张卡一行**：`GPU 0（快道）` / `GPU 1（普道）`（数据源换成 `/concurrency` 的逐卡，直连引擎时退回一行并尽量带 `hardware_static.gpu_indices` 的卡号）。线上现在（引擎还没上报卡号）显示 `GPU（快道）` `GPU（普道）`，重启窗口后自动补上号。断言口径按报文算期望行数（`(l.gpus || l.gpu)` 的张数），不写死「等于通道数」—— 一条通道可能报 0 张或几张。

**出处**：`serve/telemetry.py`（`Telemetry.sample`/`static`）、`serve/server.py`（`v1_status()` 的 `machine`、`gpu_list()`/`CUDA_VISIBLE_DEVICES` 在 1077/1170 行区）、`rag-proxy/rag_proxy.py`（`concurrency()`）、`webapp/src/{store.js,views/MonitorView.vue,i18n.js}`、`serve/test_server.py::AmdTelemetry::test_card_numbers_reach_the_monitor`、`rag-proxy/test_concurrency.py` T3、实测 `curl :8080/concurrency` 与 `nvidia-smi --query-gpu=`。

## 2026-11-02 18:3x（北京）「项目对外的 web 在哪 / 本机有哪些 AI 服务」——入口链路与在跑服务清单

**对外 web（唯一公网面）**：`https://ai.ssemarket.cn` → frps 中继（TLS 在此终结）→ Pod **:8080** → 网关 `rag-proxy/rag_proxy.py`。
- 根路径 `/` 直接返回前端 SPA `Strata/serve/web/index.html`（1485 B，标题「SSEStraBed · 层磐」）；静态资源全走 `/web/<name>`，且 `serve/server.py:2565` **只服务扁平的 .css/.js/.svg**——所以 `/web/index.html` 是 404（`index.html` 注释里也写明了，图标只能用 svg）。
- 前端源码 `Strata/serve/webapp`（Vue3 + Vite，outDir=`../web` 平铺），`bash webapp/tools/deploy.sh` 发布；引擎按请求读盘，**改前端不用重启引擎**。页面有 chat / monitor / records / about 四个标签 + 登录页。
- 网关暴露的路径：`/v1/chat/completions`、`/v1/messages`（含 count_tokens）、`/v1/models`、`/health`、`/concurrency`、`/infer/*`、`/web/*`、`/face/*`（转 127.0.0.1:8095）。`/v1/models` 现有 4 个模型名：strata-a / strata-b / qwen3.8-flash（→8092）/ qwen3.8-flash-fast（→8091）。

**本机在跑的 AI 服务（supervisor 托管，`/home/cloud/deploy/supervisord.conf`）**：

| 服务 | 端口 | 是什么 | 现状 |
|---|---|---|---|
| rag-proxy | 0.0.0.0:8080 | 网关：分流 / 窗口守卫 / 文档抽取 / failover / 推理留档 | RUNNING，upstream 两条道都 ok |
| strata-a | 127.0.0.1:8091 | Qwen3.8-Flash-Next IQ3_XXS 引擎，GPU0，模型名 *-fast（实验道） | RUNNING，ssebed-0.1.38-b7 |
| strata-b | 127.0.0.1:8092 | 同款引擎，GPU1，**默认主力**；压测只打这条道 | RUNNING，ssebed-0.1.38-b7 |
| strata-vision | 随 lane 各一个进程 | 视觉编码侧车（mmproj BF16，单图 ≤1024 token） | 随 A/B 运行 |
| face-service | 127.0.0.1:8095 | 人脸服务：录入/1:N 识别/1:1 比对/活体，buffalo_l、512 维、API key 隔离 | RUNNING（4 天+），公网走 `/face/*` |
| strata-ds41 | — | DeepSeek-V4.1-Flash 试验通道 | 已定义但 **STOPPED**（autostart=false） |
| strata（单实例） | — | 旧单通道布局 | STOPPED（现为双 lane 布局） |

- 两条 lane 的 `/health` 现在都报 **max_context=262144**、`engine_version=ssebed-0.1.38-b7`；文档里「A=524288」是 10-22 实验期的旧值，**已不是当前状态**。
- GPU 实况（nvidia-smi）：0 号 23.3 GiB/100%（lane A）、1 号 23.3 GiB（lane B）、**2 号 11.9 GiB = 邻居 pod 的 GPT-SoVITS TTS**，不是我们的。
- **不是本机服务**（别混淆）：`embed_url`/`rerank_url`（config.json 现指 10.42.0.67 / 10.42.0.91）是集群里别的 pod 的 bge-m3 / reranker，且 `rag_enabled=false` 不生效；`:11435` 是 CodeGeeX IDE agent，不是对外 AI 服务。
- `~/deploy/context.json` **当前不存在**（CLAUDE.md §0 提到的平台上下文文件在这个 Pod 上没有落地），端口/端点只能从 supervisord.conf + config.json + 实测取。

**出处**：`deploy/supervisord.conf`、`rag-proxy/config.json`、`rag-proxy/rag_proxy.py`（路由 1676–1690、`/face` 1975）、`Strata/serve/server.py:2565`（`/web/` 静态规则）、`Strata/serve/web/index.html`、实测 `curl :8080/{,/health,/v1/models,/face/v1/meta}`、`nvidia-smi`、`docs/项目定位与服务全景.md` §3–4。

## 2026-11-02 18:5x（北京）免密登录 DGX Spark（sse-dgx-spark）并扫描其上 AI 服务

**接入方式**：新生成专用密钥 `~/.ssh/id_ed25519_peer`（ed25519，无口令，公钥 `cloud@qwen-flash-pod-20261102`），用户已把公钥装到对端；本机 `~/.ssh/config` 加别名 `sse-dgx-spark` = `sse@ssemarket.cn:2223`，`ssh sse-dgx-spark` 免密可用（BatchMode 验证通过）。

**对端机器**：`spark-b973`，DGX Spark —— NVIDIA **GB10**（单卡、**统一内存** 119 GiB，nvidia-smi 的 memory total/used 报 N/A）、aarch64、20 核、3.7 TB NVMe（55% 用）、Ubuntu 24.04、驱动 580.82.09 / CUDA 13.0，已开机 54 天，load 0.3。

**公网入口**（frpc `ssemarket.cn:7000`，两条隧道）：`ssh-spark` 2223→22；`rag-gateway` **18888→本机 nginx 127.0.0.1:8888**。另有 tailnet `100.67.13.77:18888`。

**模型网关 = nginx `/etc/nginx/conf.d/rag-gw.conf`（唯一的门）**：
| 路由 | 上游 | 模型 | 现状 |
|---|---|---|---|
| `/image/` | :8890 | qwen-image-2.1-turbo | docker `qwen-image-21` Up 28h |
| `/embed/` | :8891 | wemm-embedding-9b（dim 4096） | docker Up 45h |
| `/rerank/` | :8892 | qwen3-vl-reranker-8b | docker Up 2d |
| `/llm/` | :8893 | qwen3.8-flash-next（vLLM） | 容器 **Exited(0) 2 天前**（10-31 05:44 正常停机）→ `/llm/` 现返回 503 |

- 鉴权：按 key，`Authorization: Bearer` **或** `X-API-Key`；限速 6 req/s、burst 20（超了 429）；每请求在 `/var/log/nginx/rag-gw.access.log` 里带 **key 名** 记录。key 名单现有 `owner`、`demo`。
- 管理脚本：`~/bin/rag-keys.sh list|show|add|rm|rotate|gen`（真源 `~/.rag-gw-keys`，生成的 `/etc/nginx/conf.d/rag-gw-keys.conf` 含明文 key，640）。
- 模型生命周期：`~/all-models.sh status|start [image|wemm|rerank|qwen38]|stop`；**qwen38 与另外三个互斥**（qwen38 要 GPU_UTIL 0.80），裸 `start` 不含它。

**同机其它服务**：litellm `:4000`（`~/qwen38-deploy/litellm-config.yaml`，全部模型 → `localhost:8888/v1`，但 `api_key: none`，被网关 401 ⇒ health 报 unhealthy）；litellm `:4100`（`/tmp/ltenv`，passthrough 配置，内部 500）；`maas-proxy :8123`（对外 MaaS，模型 glm-5.1 / glm-5.2）；DeepSeek V4 Flash 聊天页 `:8889`（`~/deepseek-v4-flash-0731-spark-sparkinfer/chat_web.py`，**绑 0.0.0.0**）；`dgx-dashboard :11000`（NVIDIA 自带面板）；`healthbot-pg :24321`（postgres 容器）；apache2 `:80` 默认页；cloudflared、tailscaled、trustedgpt-evaluator。

**GPU 占用（统一内存，只能按进程看）**：image 31.2 GiB + rerank 16.9 GiB + embed 26.3 GiB ≈ 74/119 GiB；三容器都 `Count:-1` 抢整卡。

**做监控时的抓手与坑**：
1. 没有 Prometheus / node-exporter / DCGM exporter —— 现成的只有 `:11000` 面板；要监控得自建（或复用 nginx access log + `~/all-models.sh status`）。
2. **GB10 的 `nvidia-smi` 显存 total/used 是 N/A**，采集必须走 `nvidia-smi --query-compute-apps=pid,used_memory` 或 `/proc/<pid>`，不能读 `memory.total`。
3. 网关 `/`、`/v1/models` 也要 key（实测 401），健康探测要带 key；`/llm/` 后端停着时返回 **503 JSON**（nginx 自定义 error_page），可据此区分「网关挂」与「模型停」。
4. `:8890/:8891/:8892/:8889` 都绑 **0.0.0.0**，绕过 nginx 就等于绕过鉴权与限速 —— 内网监听得从这些端口判断真实来源。
5. 服务器名 `sse@`，很多文件 root 所有；`sudo -n` 可用（免密）。

**出处**：`~/.ssh/config`、`/etc/nginx/conf.d/rag-gw.conf`、`/opt/frp/frpc.toml`、`~/all-models.sh`、`~/bin/rag-keys.sh`、实测 `curl :8888../health`、`nvidia-smi`、`docker ps -a`。

## 2026-11-02 19:0x（北京）两台机器的资源与 AI 服务汇总（监控基线）

**本机 Pod（SSE-Strata，group-qwen-3-8-flash-next）**
- 资源：cgroup **32 核**（cpu.max 3200000/100000；nproc 显示宿主 76 核）、**内存上限 216 GiB**（当前已用 163 GiB，压力档 mem=med）、`/home/cloud` 15 TB 用 2.0 TB、GPU **2× RTX 4090 D 24 GB**（0 号 23.3G/100%、1 号 23.3G/99%），**GPU2 11.9 GB 是邻居 pod 的 GPT-SoVITS TTS**。压力档 `gpu=high`（gpu_max=1.0）。
- 服务：`rag-proxy :8080`（唯一公网面 → ai.ssemarket.cn，upstream 两条都 ok）、`strata-a :8091` / `strata-b :8092`（Qwen3.8-Flash-Next IQ3_XXS，262144 ctx，`ssebed-0.1.38-b7`，各带一个 `strata-vision` 侧车）、`face-service :8095`（健康路径是 `/face/health`，`/health` 是 404）、`strata-ds41` 已定义未启动。
- 对外：`https://ai.ssemarket.cn` → frps → :8080；`:8080/v1/models` 四个名字 strata-a / strata-b / qwen3.8-flash(→8092) / qwen3.8-flash-fast(→8091)。

**DGX Spark（sse-dgx-spark，spark-b973）**
- 资源：**20 核**、**统一内存 119 GiB（已用 81 G，avail 37 G）**、3.7 TB NVMe 55%、**单卡 GB10**（显存只能按进程读：image 31.2 + embed 26.3 + rerank 16.9 ≈ 74 GiB）、load 0.04。
- 服务：nginx 网关 `127.0.0.1:8888` → 公网 `ssemarket.cn:18888`，四路由实测（带 owner key）：`/image/ 200`、`/embed/ 200`、`/rerank/ 200`、`/llm/ 503`（qwen38-flash-next 容器 10-31 正常停机未起）；LiteLLM `:4000`（子进程健康 unhealthy，配置里 api_key=none 会被网关 401）、LiteLLM passthrough `:4100`、`maas-proxy :8123`（glm-5.1/5.2）、DeepSeek V4 Flash 聊天页 `:8889`、DGX Dashboard `:11000`、Postgres `:24321`、apache2 `:80`。
- 两台机器的共同点：都是「一个网关 + N 个本地模型」的形状；都没有 Prometheus 类监控栈；Spark 的 GPU 指标口径完全不同（统一内存，`memory.total` = N/A）。

**出处**：`/sys/fs/cgroup/{cpu.max,memory.max,memory.current}`、`nvidia-smi`、`supervisorctl status`、实测 curl；Spark 侧见上一条记录的出处。

## 2026-11-02 19:2x（北京）三台 GPU2 邻居 Pod 免密接入与摸底（embedding / reranker / gpt-sovits）

**接入**（公钥 `id_ed25519_peer` 已用 sshpass + ssh-copy-id 装到三台，`~/.ssh/config` 别名如下；三台都是 YatTerra `clouds` 命名空间的组卡 Pod，给的密码没有落盘）：
| 别名 | 公网端口 | 主机名 | 集群 IP | cgroup |
|---|---|---|---|---|
| `pod-embedding` | 22035 | group-**embeding**-c6449fcbd-d64t8（组名拼写就是 embeding） | 10.42.0.166 | 4 核 / 16 GiB |
| `pod-reranker` | 22021 | group-reranker-6d4854c846-c8qmc | 10.42.0.138 | 4 核 / 16 GiB |
| `pod-tts` | 22026 | group-gpt-sovits-b8b55897f-f8pfx | 10.42.0.137 | 8 核 / 24 GiB |

三者都跑在同一台宿主上（`nproc` 76、263 GB、`/dev/sdb` 15 TB 用 2 TB），**都只看得到同一张 GPU2**：`GPU-27a2f80c-84f9-eef0-ea28-08da6588be7e`，11895/24564 MiB。**关键**：这三台从我们 Pod 可以直接 curl 通（7 ms 内），监控**不必走 SSH**。

**各台在干什么**
- `pod-embedding`：`/home/cloud/embed_server.py`（FastAPI，`0.0.0.0:8080`，supervisor `embed-server` RUNNING 4d16h）→ **bge-m3**（`/shared/weights/bge-m3`，fp16、normalize）。路由 `/health`、`/embed`、`/v1/embeddings`。实测一条 embedding 正常返回 1024+ 维向量。
- `pod-reranker`：`/home/cloud/reranker_server.py`（`0.0.0.0:8080`，`reranker-server` RUNNING 4d16h）→ **bge-reranker-v2-m3**（`/shared/weights/bge-reranker-v2-m3`，fp16）。路由 `/health`、`/v1/models`、`/v1/rerank`。实测「RAG 是什么」→ 0.998 / 0.000016，区分度正常。
- `pod-tts`：**Voice Studio**（GPT-SoVITS v2pro 之上自研的训练+合成+RVC 翻唱平台，`/home/cloud/voice-studio`，uvicorn `app.main:app` 绑 `0.0.0.0:8080`，RUNNING 4d16h）。对外开放 API 在 `/api/v1/*`（`tts`、`tts/stream`、`tts/session` 流式、`separate` 人声分离、`asr`、`covers` 翻唱、`voices`、`tasks`），用 `X-API-Key` 或 `Authorization: Bearer`。数据 **104 GB**（runs 96 G、models 4.6 G、songs 2.2 G、covers 779 M）。**三个 gpt-sovits-infer 推理 worker（9880/9881/9882）已于 10-31 14:53 全部 STOPPED**，现在只有 voice-studio 后端在跑。

**GPU2 的 11.9 GiB 归属（推断，容器内 `nvidia-smi` 进程名一律 `[Not Found]`，宿主才能精确归属）**：bge-m3 ≈ 2200 MiB、bge-reranker ≈ 2394 MiB、Voice Studio 后端（内含预热模型）≈ 5286 MiB；另 1388 / 580 / 34 MiB 属其它组的 pod。我方两张 4090 与 GPU2 无交集。

**两条会踩的坑**
1. 我们 `rag-proxy/config.json` 里的 `embed_url=10.42.0.67` / `rerank_url=10.42.0.91` **已经不通了**（实测超时），这两台现在的 IP 是 **.166 / .138**。RAG 现在是关的所以没影响，但要重新打开 RAG 必须先改这两行。
2. 三台 pod 的 8080 是**公网 8080 映射**（平台 NodePort + frps），不是内部端口；它们没有 `context.json`（和我们一样，平台上下文文件没落地），公网地址只能从平台/中继侧确认。

**出处**：`~/.ssh/config`、三台 `deploy/supervisord.conf` 与 `output of supervisorctl status`、`embed_server.py:9`/`reranker_server.py:12`、`voice-studio/app/main.py`（`/api/v1` 段 2909+）、实测 curl 与 `nvidia-smi --query-compute-apps`。

## 2026-11-02 19:3x–20:0x（北京）监控页补全 + 模型广场 + Agent 模式（一次交付）

**后端（rag-proxy，重启两次，均经用户同意）**
- `concurrency()` 新增三块：`gpus`（本机 3 张卡，nvidia-smi 一次全读，含邻居在用的 GPU2）、`spark`（DGX Spark 的 GB10，网关每 10s SSH 采一次、按进程求和——统一内存没有 total/used）、`services`（8 个服务的健康/调用数/错误/在途/延迟）。每条 lane 另加 `max_context`（**取自 /health 的静态值**：引擎空闲时 /v1/status 的 context 与 /metrics 的 engine.max_context 都报 0，前端因此只画得出一条上下文）。
- 新增 `/spark/*`（→ https://spark.ssemarket.cn，网关代持 key，key 在 `/home/cloud/.secrets/spark.key` 600）、`/tts/*`（→ 10.42.0.137:8080，`X-Voice-Key` 原样转发）、`/tool/status`、`/tool/search`（Bing RSS，`cn.bing.com` 是唯一可达的搜索出口，duckduckgo 不通）。**这些路由一律要求登录会话或 API token**（实测未登录 401），否则公网任何人都能烧生图 GPU。
- 邻居三台加 `/metrics`（`deploy/patches/add_metrics.py`，纯插入 + py_compile 校验 + 各自 supervisor 重启）：bge-m3 / bge-reranker / Voice Studio。副作用：重启 voice-studio 时**三个 gpt-sovits-infer worker（9880-9882）被一起拉起来了**（此前 10-31 起一直 STOPPED）。

**前端（静态发布，零重启）**
- 监控页：上下文**一条道一行**（快道 14/256K、普道 7.7k/256K 实测）；新增「全部显卡」4 张（GPU0/1 标注本项目、GPU2 标注邻居共用、Spark GB10）；新增「全部 AI 服务」8 张卡（调用/错误/在途/探活/显存）。
- 新页「模型广场」（`views/HubView.vue` + `hub.js`）：8 张能力卡，点开即体验——生图（多参考图拼一张底图直接喂回去，**不是看图转述**）、Agent 优化提示词、向量化、精排、TTS（自带 key）。
- 对话页新增「对话 / Agent」模式切换：原生 tool calling（实测引擎 `finish_reason=tool_calls` 可用），工具 search_web / service_status / generate_image / embed_text，自动续轮最多 4 轮；结果复用 ToolCall 卡片渲染。端到端实测：问「现在有哪些 AI 服务在线」→ 调 service_status → 基于结果作答。
- 手机端按小屏**重新排版**（不是压缩）：模式切换整行分段控件、输入框 16px + 64px 最小高（iOS 不再聚焦放大）、面板从底部整屏升起 + 标题吸顶 + 操作区 sticky、触控目标 ≥44px、safe-area 内边距；smoke 的 390px 断言全过（无横向溢出）。

**出处**：`rag-proxy/rag_proxy.py`（`svc_poller`/`gpu_snapshot`/`_passthrough`/`_tool_get`）、`deploy/patches/{add_metrics.py,patch_proxy_services.py,patch_proxy_routes.py}`、`Strata/serve/webapp/src/{hub.js,views/HubView.vue,views/ChatView.vue,views/MonitorView.vue,store.js,i18n.js}`、`webapp/tools/{deploy.sh,smoke.mjs}`。

## 2026-11-02 20:1x–20:4x（北京）补齐：工作空间 / skill / MCP / fetch_url / 搜图 / 生图占位 / 全站字体

**每用户独立工作空间**（`/home/cloud/workspaces/<uid>/`，uid = OAuth sub 或 API token 的 sha256 前 16 位）
- 目录：`skills/*.md`、`mcp.json`、`images/*.png`；所有文件名过 `_ws_safe`，逃不出自己的目录。
- 端点：`GET /workspace`、`POST/DELETE /workspace/skill`、`POST/DELETE /workspace/mcp`、`POST /workspace/image`、`GET /workspace/image?uid&name创&exp&sig`。
- **图片直链必须签名**：`<img>` 带不了 Authorization，用「uid+文件名+过期时间」的 HMAC（7 天）代替登录态；签名覆盖 uid，跨目录拼不出来。这是这一轮最容易踩的坑（第一版没带 uid，匿名访问会落到 anon 目录）。

**工具（7 个内置 + MCP）**：`search_web`（Bing RSS，给标题/链接/摘要）、**`fetch_url`**（抓正文，SSRF 闸门拒绝内网/回环/保留地址）、**`image_search`**（Bing 图片 HTML 里的 murl 直链）、`generate_image`（可吃用户附图 + 搜图直链，多图在 canvas 拼成一张底图再喂，**不是让模型看图转述**）、`service_status`、`embed_text`，以及用户配置的 MCP 工具（`mcp__<server>__<tool>`）。
- `fetch_image`：服务端抓远程图片转 dataURL，绕开浏览器 CORS —— 否则搜图结果既读不到像素也画不进 canvas。

**MCP**（`rag-proxy/mcp_client.py`）：JSON-RPC over HTTP（兼容 SSE 响应与 Mcp-Session-Id），initialize → tools/list → tools/call。**只支持远程 HTTP 型**；stdio 型要在共享网关里 spawn 用户进程，不给。实测接 `https://mcp.deepwiki.com/mcp`，列工具与调用都通，agent 端到端成功调用 `mcp__deepwiki__read_wiki_structure`。

**skill**：上传 .md（≤64KB），最多取 8 个、每个前 4000 字进系统提示。

**生图占位**：`m.imgPending` → 消息里渲染转圈占位 + 提示词预览，出图后替换；生成的图存进工作空间，页面用短链接显示（dataURL 会让会话变重）。

**全站字体（风格一致性的真凶）**：`<button>/<input>/<select>` **默认不继承 font-family**，用的是浏览器 UA 字体 —— 卡片里的中文和标题根本不是同一套字（无 CJK 字体的环境直接显示豆腐块）。修法是一条全局规则 `button,input,select,textarea{font-family:inherit}`。顺手核对了 `tencent-sans-w7.woff2`：cmap 29002 条、这些字都有 outline，字体本身没问题。

**出处**：`rag-proxy/{rag_proxy.py,mcp_client.py}`、`deploy/patches/patch_workspace.py`、`Strata/serve/webapp/src/{hub.js,views/HubView.vue,views/ChatView.vue,components/MessageItem.vue}`、`webapp/public/app.css`。

## 2026-11-02 21:0x–21:4x（北京）首页重做 / 监控排版修整 / 导流与「假死锁」复盘

**首页 = 选择页（新）**
- 默认标签变成 `home`（地址就是 `/`）；「对话」是独立页 `#chat`。`setTab` 里 home 写路径、其余写 hash → 刷新/收藏/分享都回到同一页（原来 chat 特判成 pathname，导致 #chat 读不回来）。
- 首页内容：hero「先选一个开始」+「开始对话」+ 三组服务卡（对话/生成/检索）+ 我的工作空间；卡片点击即体验，对话卡直接跳 `#chat`。
- 原来的独立「模型广场」标签撤销（用户要求并进主页），HubView 改名为 HomeView。

**监控页**
- **上下文占用改成一条道一张卡**（快道/普道各带自己的仪表 + 专家驻留），系统内存/温度单独一张 —— 之前是一张卡里塞两条 + 一个只指一条的仪表，看起来像「只有一条」。
- GPU 区块合并去重：原来的「逐卡 GPU（两条道）」与新增的「全部显卡」重复，现在合成一块：**本机 3 张 + Spark 1 张**，四格改两列、标签缩短（负载/显存/温度/功耗），不再截断。
- 容器宽度 1240 → `min(1760px, 100%)`（2K 屏左右各空出几百像素）；`.metrics` 自动列数。
- Spark 上**没跑的服务不再显示**（qwen3.8-flash-next 的 `/llm/` 已下线，从服务清单里删掉）。

**导流（真 bug，已修）**
- 症状：选「快道」还是落在普道；普道被压满、快道空转。
- 根因：引擎空闲会自动卸载（`/v1/status` 的 `loaded=false` + `auto_load=true`），而 `lane_ready()` 把这种**正常空闲**当成「正在重载」，于是把钉在快道的请求改投到当时正忙的普道。
- 修法：① 显式选道只在该道**真连不上**时才改投；② `lane_ready()` 遇到 `auto_load=true` 一律视为就绪。
- 新策略（用户要求）：**未指定模型的请求优先走慢道 8092，慢道批槽满了才去快道 8091**，两条都满再退回最少负载。实测未登记模型 → 普道。

**「快道有请求没推理」的真相**
- **不是死锁**：实测该道空闲时 TTFT 5.3 s、日志里 4 条序列同时在跑（80–120 tok/s）。
- 是**批槽被长输出占满**：单请求 `max_tokens` 走引擎默认 6k–8k，4 路各 ~15 tok/s ⇒ 一条占用 1–7 分钟，第 5 个请求只能排队，而排队期间界面空白，看起来像卡死。
- 处置：前端默认 `max_tokens=2048`（用户可调）；**不再向用户显示「批槽已满/排队中」**（用户明确要求）。

**测试**：`tools/smoke.mjs` 跟随改版更新（首页断言、上下文按道断言、GPU 四卡断言、mock 补 `/workspace` `/tool/mcp/list` `/tool/status` 与 gpus/spark/services、导航改到 `#chat`）。**ALL PASS**。

**出处**：`Strata/serve/webapp/src/{store.js,views/HomeView.vue,views/ChatView.vue,views/MonitorView.vue,components/MessageItem.vue,i18n.js}`、`webapp/public/app.css`、`rag-proxy/rag_proxy.py`（`preferred_upstream`/`lane_has_room`/`route_upstream`/`lane_ready`）、`webapp/tools/smoke.mjs`。

## 2026-11-02 22:0x（北京）工作空间重构为三栏工作台（文件树 + 编辑器 + agent）

**后端**（rag_proxy，重启一次）：新增通用文件 API —— `GET /workspace/tree`（扁平清单）、`GET /workspace/file?path=`（文本或 base64 图片）、`POST /workspace/file`（写/覆盖，自动建目录）、`POST /workspace/mkdir`、`DELETE /workspace/path`。路径由 `ws_path()` 收敛在本人目录内，`..` 与隐藏文件一律拒绝。

**前端**
- 新 `views/WorkspaceView.vue`：`grid-template-columns: 240px 1fr 380px`（文件树 / 编辑器 / agent）；≤860px 改成三行（树在上、编辑器中、agent 在下）。
- 文件树：扁平清单 → 嵌套树（`workspace.js` 的 `toTree`），目录可折叠，行内删除，顶部 `+`（文件/文件夹）与刷新。
- 编辑器：按扩展名分文本/图片；文本用 textarea + 脏标记 + 保存（Ctrl/Cmd+S）+ 删除；图片直接预览。
- 新 `components/AgentPanel.vue`：顶部切换 **agent / 生图 / 向量 / 精排 / TTS** —— 就是「不同体验在 agent 侧边栏换」。agent 模式带原生 tool calling，工具包括 `list_files` / `read_file` / `write_file` / `search_web` / `fetch_url` / `service_status` / `generate_image`，**能直接读写这个工作空间**（写文件后自动刷新文件树）。
- 导航栏**去掉「对话」入口**：`VIEWS` 里删掉 chat，但 `TABS` 仍保留 `#chat` 深链（老书签/老页面还能到），首页就是工作台。

**踩到的坑（两个）**
1. `workspace.js` 的 `req()` 一开始用 `headers(...)` 时拿到空头 → `/workspace/tree` 401。改成自己拼 `Authorization: Bearer` 就好了（`hdr()`）。
2. `toTree()` 给文件节点漏了 `name` 字段 → `a.name.localeCompare` 抛 TypeError → 整棵树渲染成空。补 `name: parts[parts.length-1]` 后恢复。

**验证**：浏览器实测 —— 打开 notes.md → 编辑 → Ctrl+S → 回读接口确认内容已落盘；agent 回话并调用 `list_files`。`tools/smoke.mjs` 跟随改版：导航断言去掉「对话」、首页断言改成「文件树 + agent 面板」、其余用到 chat 的地方改走 `#chat` 深链、mock 补 `/workspace/tree`。**ALL PASS**。

**出处**：`rag-proxy/rag_proxy.py`（`ws_path`/`ws_tree`/文件路由）、`Strata/serve/webapp/src/{workspace.js,views/WorkspaceView.vue,components/AgentPanel.vue,store.js,App.vue,i18n.js}`、`webapp/tools/smoke.mjs`。

## 2026-11-02 23:0x（北京）工作台体验迭代：工具卡 / 模型选择 / 速度 / 生图两模式 / 编辑器

**工具调用卡片重做**（`components/ToolCard.vue`）
- 一行摘要（图标 + 中文名 + 关键参数 + 耗时 + 展开箭头），展开才看参数/结果；状态用颜色和图标表达（执行中旋转 / 完成绿勾 / 失败红）。
- 中文名映射：list_files→列目录、read_file→读文件、write_file→写文件、search_web→联网搜索、fetch_url→读网页。

**模型选择 + 速度（补回）**
- Agent 面板右上角可选 **普道（qwen3.8-flash）/ 快道（qwen3.8-flash-fast）**，选择持久化。
- 速度两段式：流式期间显示实时 tok/s；结束后显示「N tokens · X tok/s · 首字 Ys」（口径：首字之后到结束，用引擎回报的 completion_tokens）。

**生图重做成两种模式**（`components/ImageStudio.vue`）
- **直接生成**：提示词 + 最多 4 张参考图（上传/联网找），尺寸/步数/seed/透明底。
- **Agent 模式**：自己联网找参考图 → 读公开资料 → **两轮迭代提示词**（先写，再按「主体/构图/光线/材质/镜头」收紧）→ 生成；每一步都落成工具卡给用户看。
- 结果图带 seed、「用作参考」、「下载」，并自动存进工作空间；生成期间有骨架屏。

**编辑器升级**
- 行号栏（与 textarea 滚动同步）、字数/行数统计、未保存状态点；
- Markdown 文件支持 **编辑 / 分栏 / 预览** 三态；预览走 `markdown(text, {doc:true})`（标题保留真实层级，`#`→h1 而不是聊天里的 h3）；
- Tab 缩进、中文等宽回退字体、⌘/Ctrl+S 保存。
- 修掉一个关键坑：**scoped 样式对 `v-html` 不生效**，预览/气泡里的标题、表格、代码块全都没样式 —— 改用 `:deep()` 才生效。

**agent 与文档联动**
- 面板拿到「当前打开的文件路径 + 内容」，直接可以「把这段改短并写回文件」；实测：读文件→写文件→磁盘内容确实更新。
- 每条回答底部有 **复制 / 存为文件**（存成 `<当前文档>-MMDDHHmm.md`）。

**修掉的 bug**：工具轮之后回答会重复显示两条 —— 续轮的消息并回同一条并从列表移除。

**验证**：浏览器实测（工具卡、速度、模型选择、生图 512 出图 naturalWidth=512、分栏预览 h1/h2/表格/代码块、agent 改文件落盘）；smoke **ALL PASS**。

**出处**：`Strata/serve/webapp/src/{components/ToolCard.vue,components/ImageStudio.vue,components/AgentPanel.vue,views/WorkspaceView.vue,markdown.js}`。

## 2026-11-03 00:4x（北京）工作台再迭代：自动保存 / Markdown 工具栏 / 上传 / 来源

- **自动保存**：停止输入 1.2s 后静默写盘；状态点从「● 未保存」改成「● 编辑中… / 已保存」，⌘/Ctrl+S 仍可立即保存。
- **Markdown 工具栏**：标题 / 粗体 / 斜体 / 列表 / 引用 / 行内代码 / 链接，包裹选区或插入标记并把光标放回原位（实测选区 3 字 → `**第一段**文字`）。
- **折行开关与行号**：散文默认折行（不显示行号），代码类文件自动关折行并显示行号 —— 原来折行时行号会与视觉行错位。
- **上传文件**：文件树新增上传，图片走 b64、其余走文本，落在 `uploads/`；做调研时直接把素材丢进工作空间。
- **参考来源**：agent 这轮如果调了 `search_web` / `fetch_url`，回答下方自动列出域名小标签（最多 6 个，去重、跳过图片直链）。
- 实测：新建 draft2.md → 输入 → 工具栏加粗 → 2.5s 后自动保存 → 回读接口内容一致；smoke **ALL PASS**。

**环境观察（非前端问题）**：验证时机器 load ≈ 30–40（推理流量 + 邻居），浏览器导航会超时；`/concurrency` 里普道有一条 **4108s 仍在流式**的条目（长 prefill 的 keep-alive 一直在刷新活动时间，所以清道夫不会回收），它占着批槽导致新请求排队 —— 这是引擎 prefill 串行的老问题，不是这一次改动引入的。

## 2026-11-03 00:5x（北京）写代码体验：零依赖语法高亮

- 新增 `src/highlight.js`：一条主正则切出「注释 / 字符串 / 数字 / 关键字」四类，其余纯文本转义后拼回 HTML；支持 js/ts/py/sh/json/css/html，未知语言原样转义。刻意不做完整语法分析（编辑器里读代码，四类颜色足够，出错也不会破坏文本）。
- `WorkspaceView`：**代码文件也出现 编辑 / 分栏 / 预览** 三态；预览是可滚动的只读高亮视图（`.ws__preview--code`），明暗两套配色。
- 实测 `demo.py`：关键字 7 / 字符串 9 / 数字 5 / 注释 1，关键字色 `rgb(124,58,237)` 生效。
- 顺手修：文件树头部四个按钮把标题挤成两行（收紧按钮尺寸 + 标题 nowrap）。
- smoke **ALL PASS**。

**当前工作台能力盘点（用下来）**：写文章有工具栏/自动保存/分栏预览/字数/下载；做调研有联网搜索→读正文→来源标签→一键存为文件；写代码有行号、折行开关、语法高亮预览、重命名与下载。仍缺：多文件标签页、拖拽排序。

## 2026-11-03 01:0x（北京）多文件标签页 + 收尾

- 编辑器改成**多标签**：`tabs[]` + `activeIdx`，`open` 变成指向当前标签的 computed；已打开的文件再点直接切过去（不再弹「有未保存修改」）。
- 切换标签**保留各自的正文与未保存状态**（脏标记在标签上显示为 ●），关闭有未保存内容的标签才提示；删文件会自动关掉引用它的标签。
- 实测：依次打开 article.md / demo.py / mcp.json → 三个标签；切回 article.md 内容仍在；关闭中间标签后剩两个；零 JS 报错。
- smoke **ALL PASS**。

**至此工作台的能力**：写文章（工具栏/自动保存/分栏预览/字数/重命名/下载）、做调研（联网搜索→读正文→来源标签→存为文件→编辑成稿）、写代码（行号/折行开关/语法高亮预览/多标签对照）。仍缺：文件树拖拽移动、工作空间全文搜索、编辑态的叠加高亮。

## 2026-11-03 08:3x（北京）工作空间全文搜索；Pod 已重建（重要）

- **工作空间全文搜索**：`GET /workspace/search?q=`（服务端遍历文本类文件，单文件 >1MB 跳过，最多 60 条命中，返回 path/line/文本）。前端文件树顶部一个搜索框，输入停 250ms 查询，结果点一下即打开并把光标跳到那一行。实测搜「推理」→ `article.md:1`，点击打开正确。
- **Pod 在 2026-11-02 22:39 前后被重建过**（`uptime` 只有 2 小时，宿主/镜像换了）。表现：supervisor 自动把 strata-a / strata-b / rag-proxy / face-service 全拉起来了（这是预期行为），工作空间文件都在；但**容器里装过的系统包没了**，导致 headless Chrome 起不来（`libglib-2.0.so.0: cannot open shared object file`）。
- **重装命令（下次重建后照抄）**：
  ```
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends libglib2.0-0 libnss3 libnspr4 libatk1.0-0 \
    libatk-bridge2.0-0 libdbus-1-3 libcups2t64 libxkbcommon0 libasound2t64 libgbm1 libcairo2 \
    libpango-1.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxrender1 libxext6 libxi6
  ```
  装完 `ldd <chrome> | grep "not found"` 应为 0。
- smoke **ALL PASS**（重装依赖后重跑）。

## 2026-11-03 09:0x（北京）工作台加「终端」：共享镜像 + 三个代码环境

**共享镜像**：`/home/cloud/sandbox/img/venv` 一个 venv（numpy 2.5.4 / pandas 3.1.0 / matplotlib 3.11.2），
所有 Python 环境共用它 —— 这就是「每个环境共用一个镜像」的落地方式（Pod 里没有 docker，用 venv 当镜像层）。

**三个环境**（`rag_proxy.SANDBOX["envs"]`，可扩）：
| id | 解释器 | 说明 |
|---|---|---|
| `python` | `/usr/bin/python3 -I` | 系统 3.12，只有标准库 |
| `ds` | `/home/cloud/sandbox/img/venv/bin/python -I` | numpy / pandas / matplotlib |
| `node` | `/usr/bin/node` | node 18 |

**接口**：`GET /workspace/envs`、`POST /workspace/run {env, code, timeout}` —— 每次跑都在临时目录（跑完删），
限制：CPU ≤ timeout+1s、地址空间 6 GiB、写盘 20 MiB、打开文件 256、超时 120s 上限。
实测：python `print(1+1)`→2；ds `numpy+pandas`→6/10；node→2,4,6；死循环 3s 被终止。

**两个踩到的坑（都记在代码注释里）**：
1. **不要设 `RLIMIT_NPROC`** —— 它按**用户**统计，本 Pod 里推理服务已占成百上千线程，设 64 会让子进程连
   OpenBLAS/node 自己的线程都建不出来（`pthread_create failed`），还会波及我们自己。
2. 共享机上要给 `OPENBLAS_NUM_THREADS` / `OMP_NUM_THREADS` 等设成 2 —— 否则按 76 核开线程，既抢 CPU，
   又各自预留缓冲区把 `RLIMIT_AS` 顶爆（第一版就是 `Memory allocation still failed`）。

**前端**：编辑器底部「终端」栏（可收起），环境下拉 + 运行 + **跑当前文件**（按扩展名/内容自动挑环境）+ 输出区
（stderr 红色）；Ctrl/⌘+Enter 运行。smoke **ALL PASS**（mock 补了 `/workspace/envs`、`/workspace/search`）。

**安全边界（必须说清）**：这不是安全沙箱 —— 和网关同用户、同 Pod，只有资源/时间限制与临时工作目录。
挡住的是死循环、内存爆炸、写满磁盘；挡不住蓄意恶意代码。只应对可信账号开放。

## 2026-11-03 09:2x（北京）agent 能真正跑代码；代码产出落回工作区

- Agent 新增工具 **`run_code`**（env: python / ds / node），系统提示里明确要求「需要验证代码就真跑一遍，按报错改到能跑为止」。
  端到端实测：问「用 python 算 1 到 100 有多少个质数，跑一遍」→ agent 调 `run_code` → 返回 25 → 正确作答。
- **代码产出自动落盘**：运行结束前把临时目录里的生成文件（如图表）拷到工作空间 `runs/<时间戳>/`，返回 `files[]`；
  前端终端会显示「产出 N 个文件」并刷新文件树。跳过 `.cache/` 这类库自己写的隐藏文件（第一版把 matplotlib 的字体缓存也拷进来了）。
- 实测 ds 环境跑 matplotlib → `runs/20261103-005238/chart.png` 出现在文件树里。

**安全边界不变**：同用户同 Pod 的受限子进程，不是安全沙箱。

## 2026-11-03 09:4x（北京）手机端改成「三选一」单栏工作台

- ≤860px 时不再把三栏硬挤成三行：顶部一条 **文件 / 编辑 / agent** 分段切换，一次只显示一栏，每栏都吃满屏高。
- 390px 实测：默认落在「编辑」；切「文件」看到完整文件树（含 `runs/` 下代码产出的图）；切「agent」看到对话面板；`body.scrollWidth == 390` 无横向溢出。
- smoke **ALL PASS**。

**工作台最终形态**
- 宽屏：文件树 240px | 编辑器 | agent 420px
- 窄屏：一条切换栏 + 单栏满屏
- 终端：编辑器底部可收起；agent 也有 run_code 工具

## 2026-11-03 10:0x（北京）写作闭环：回答「插入到文档」+ 快捷键

- Agent 每条回答新增 **插入到文档**（有光标插光标处、没有就接文末，自动补空行并把光标停在插入内容末尾）；配合已有的复制/存为文件。
  实测：article.md 243 字 → 插入后 334 字、状态变「编辑中…」。
- 快捷键：`⌘/Ctrl+S` 保存、`⌘/Ctrl+J` 收起/展开终端、`⌘/Ctrl+K` 聚焦工作空间搜索（窄屏会自动切到「文件」栏）。
- 「跑当前文件」只对 `.py/.js/.ts/.sh` 可点。
- smoke **ALL PASS**。

**写作闭环现在是**：问 agent → 复制/插入到文档 → 在编辑器里改 → 自动保存 → 下载。

## 2026-11-03 14:27（北京）首页/Agent 改造：能力全部收进 coding agent + 每用户 LLM Lab/沙箱 + 知识库 + 胡桃 TTS

**问题**：首页上的生图 / Embedding / rerank / TTS 不要再单独出现，全部集成到 agent；agent 要变成 coding agent；每个用户要有能跑 C++/Python 的简单环境和一个默认干净、可改可训练可调试的 `llm-study-lab`；还要有新建会话。

**交付与结论**
- **信息架构**：`首页`改为入口页（对话 / LLM Lab / 知识库 / 最近文件），新增 `对话` 与 `LLM Lab` 两个导航页；生图、向量检索、精排、TTS 不再有独立入口。`Strata/serve/webapp/src/{store.js,App.vue,views/HomeView.vue}`。
- **Coding agent**：对话页默认 Agent 模式，工具含 `list_files/read_file/write_file/edit_file/search_files`、`run_code`（python/ds/node/cpp/lab）、`start_job/job_status/job_stop`、`kb_add/kb_search/kb_list`、联网/读网页/搜图/服务状态/生图；右侧工作台 agent 同样一套。`views/ChatView.vue`、`components/AgentPanel.vue`、`components/ToolCard.vue`。
- **新建会话**：对话页模式条新增「新会话」按钮；工作台 agent 头部也有「新会话」；左侧栏原有新建保留。
- **LLM Lab**：首次访问工作空间时把 `/home/cloud/llm-study-lab` 拷一份到 `workspaces/<uid>/llm-study-lab`（保留 `.git`，`git show solution:...` 仍可用；跳过 `__pycache__/.pytest_cache/.venv`）。工作台默认打开 `START_HERE.md`，支持重置（旧副本改名 `llm-study-lab.bak-<时间>` 备份）。`rag_proxy.py:ensure_lab/lab_cfg`。
- **每用户沙箱**：新增 `cpp`（g++ -O2 -std=c++17 编译运行）与 `lab`（`/home/cloud/llm-lab/.venv`，torch 2.6+cu124，`CUDA_VISIBLE_DEVICES=2`）环境；`run_code` 支持多文件 `files`、`in_place`（在 lab 目录里跑）、`cwd`、GPU 压力检查；后台任务用独立进程组 + 日志落盘 + 增量轮询，训练不会被 HTTP 超时掐死。`rag_proxy.py:SANDBOX/run_code/JOBS`。
- **个人知识库**：`<uid>/.kb/*.json`，支持文本/工作空间文件（PDF/DOCX/XLSX 走 `doc_extract`）/URL；bge-m3 嵌入 + bge-reranker-v2-m3 精排；对话发送前自动检索（可关），agent 也可显式 `kb_search`。嵌入/精排改用稳定集群 DNS：`group-embeding` / `group-reranker`。
- **胡桃 TTS**：Voice Studio 邻居 pod 的 9882 worker 已常驻 run 122（「胡桃」）权重；新增 supervisor `tts-tunnel` 把 `127.0.0.1:9882` 隧道到本 Pod，网关 `POST /tts/say` 直接合成并缓存（`/home/cloud/logs/tts-cache`）。前端回答默认自动朗读，可关；每条回答也有「朗读（胡桃）」「存入知识库」按钮。不需要用户自己的 Voice Studio key，密钥不进浏览器。
- **实测**：`python main.py doctor` 在 lab 环境跑通（torch 2.6.0+cu124, cuda=True, RTX 4090 D）；C++ hello 编译运行 exit 0；KB 增/检索（rerank 0.85+）/删通；胡桃 TTS 返回 WAV（~110KB/句）；`tools/smoke.mjs` ALL PASS；线上 8080 浏览器实测首页/LLM Lab/知识库弹层/新会话/自动朗读均正常，无 pageerror。

**出处**：`Strata/serve/webapp/src/{store.js,App.vue,views/HomeView.vue,views/ChatView.vue,views/WorkspaceView.vue,components/AgentPanel.vue,components/MessageItem.vue,components/ToolCard.vue,workspace.js,hub.js,i18n.js}`；`rag-proxy/rag_proxy.py`、`rag-proxy/config.json`；`deploy/supervisord.conf`（新增 tts-tunnel）。备份：`rag_proxy.py.bak-20261103-lab`、`config.json.bak-20261103-lab`、`deploy/supervisord.conf.bak-20261103-lab`。

## 2026-11-03 14:3x（北京）「快道/普道导流」排查：全量改投快道是 11-02 上午的事，今天坏的是快道引擎

**问**：今天早上快道/慢道的导流出问题 —— 为什么请求都被导到快道了？

**结论（先核对事实）**
1. **今天（11-03）的记录里没有「全导快道」**。08:00–12:00 共 1544 条：普道 1535 条（1530 成功、2 失败）、快道 45 条（37 失败，全部是客户端显式指定 `strata-a` 的流量）。快道全天 8 成功 / 98 失败。
2. **「全导快道」确实发生过，但不在今天**：11-02 10:56:31–12:16:33（289 条钉普道的请求 100% 落快道）、11-01 09:06–10:23（170/171）、10-31 22:58–11-01 00:12（241/241）。
3. 根因是 **`lane_ready()` 就绪门 + `route_upstream()` 的「未就绪即改投」**：引擎在**加载/启动**期间 `loaded=false`（`server.py` 的 `alive()` 在 READY 之前返回 False），旧代码把这种状态当成不可用，把钉在普道的请求整批改投快道；而改投之后普道**收不到请求就不会被唤醒**（server.py 的引擎是按需启动，issue #27），于是形成自锁 —— 越改投越不会恢复，直到有请求绕过门或直打 8092 才结束（11-02 就是 12:16 恢复的）。11-02 21:51 已修：只有 TCP 不可达才改投、`auto_load` 视为就绪、未登记模型优先慢道。

**今天真正坏的是快道引擎本身**
- `strata-a.err.log`：`EngineDied: the engine stopped unexpectedly (exit code -11)`（SIGSEGV）、`RuntimeError("the vision encoder did not start")`，以及自愈路径自己崩掉：`AttributeError: 'NoneType' object has no attribute 'poll'`（`death_note()` 没判空）。
- 时间线：10:52 起出现 503；11:00 之后每 ~2 分钟一条 `strata-a`（UA `undici`）每条挂满 20 分钟超时（`total_ms`≈1,200,000）；引擎进程 11:13 被重新拉起，但仍卡在 prefill（日志反复 `reading the prompt: 6,987 tokens, 60 s so far`）。
- 现场实测（14:3x）：对 8091 发 8 token 的「1+1=?」30 秒无响应；`nvidia-smi` GPU0 util 0% / 显存 23.1 GB，同时普道 GPU1 100%；`/v1/status` 快道 `serving 4 / requested 4`。

**为什么今天「可达但卡死」不会被改投**：11-02 的修法只看 TCP 探活，引擎卡死但端口仍在监听 → 不触发 failover，钉在快道的请求只能等 20 分钟超时。要兜住这类事故需加「hold 超时熔断」（改代理 → 需重启代理进程，得先经同意）。

**出处**：`rag-proxy/rag_proxy.py`（`route_upstream` / `lane_ready` / `preferred_upstream` / `lane_has_room`）、`Strata/serve/server.py`（`loaded()` / `alive()` / `death_note()`）、`~/logs/infer.db`、`~/deploy/logs/{rag-proxy.out.log,strata-a.err.log,strata-a.out.log,supervisord.log}`。

## 2026-11-03 14:58（北京）UI 迭代：导航收口、输入条统一、首页继续对话卡片、图片查看/下载、流式朗读

**问题（用户连续反馈）**：输入框上方堆一排模式胶囊太乱，要统一放到底部、模式选择做成「快道」那种下拉；导航太多，LLM Lab/对话不要直接入口，统一从首页进；首页所有元素都要可点；刚提交的对话点首页要能原样回到；图片要支持点击查看和下载；朗读没生效，要流式按块合成。

**结论/实现**
- **导航收口**：`VIEWS` 只留 `首页/监控/记录/推理可视化/训练/关于` 6 项；`#chat`、`#lab` 仍是可用深链，`TABS` 保留。对话/Lab 页面高亮「首页」。`store.js`、`App.vue`。
- **输入条统一**：删掉输入框上方的 `mode-switch`/`mode-hint`；模式选择改成与「快道」同款 `<select>`（`Coding Agent` / `对话`），与「新会话 / 上传 / 导出 / 采样 / 朗读 / 知识库 / 通道 / 发送」全放在输入框下方一行。手机端图标化并收紧宽度（实测 390px 单行不溢出）。`views/ChatView.vue`。
- **首页全部可点**：四张能力卡是按钮（Coding→对话、知识库→Lab 知识库面板、生图/语音→对话并预填提示词）；Lab 行、知识库资料、Skill、MCP、最近文件、生成图都可点。`views/HomeView.vue`。
- **继续上次对话**：首页顶部有「继续对话 · 标题」主按钮 + 一张最近对话卡（标题/最后一句/消息数/时间/状态已保存）；点卡片 `openChat(id)` 回到原会话，消息/工具/图片原样。`HomeView.vue` 的 `lastChat/resumeChat`。
- **图片查看/下载**：新增 `components/ImageViewer.vue`（全屏大图 + 下载 + Esc 关闭）；聊天用户图/生成图、工作台图片预览、AgentPanel 图、首页画廊全部接入；下载走 fetch→blob（签名直链/dataURL 都行）。`MessageItem.vue`、`AgentPanel.vue`、`WorkspaceView.vue`、`HomeView.vue`。
- **流式朗读（胡桃）**：回答还在流式生成时按句切块（跳过代码块/行内代码），每块单独 `POST /tts/say`，播放队列边播边预取下一块；首句出声早，长回答不再等整段。首次点击解锁 AudioContext；自动播放被拦时保留队列并显示「继续朗读」。`ChatView.vue` 的 `ttsFeed/ttsDrain/playNextTts`；工作台 agent 也改成分句队列。
- **实测**：三句话回答触发 3 次 `/tts/say`，音频 6.26s 正常播放，开关显示「朗读中」；图片查看器在首页画廊和聊天消息里都能打开/关闭/下载；`tools/smoke.mjs` 251 项 ALL PASS；线上 8080 导航 6 项、无 `.mode-switch`、底部条 11 个控件、无 pageerror。

**出处**：`Strata/serve/webapp/src/{store.js,App.vue,views/HomeView.vue,views/ChatView.vue,views/WorkspaceView.vue,components/AgentPanel.vue,components/MessageItem.vue,components/ImageViewer.vue,i18n.js}`、`tools/smoke.mjs`。

## 2026-11-03 15:0x（北京）快道「有请求不推理、一直卡着」：引擎进程卡死 + 三处防护补齐

**问**：监控上 5 个在途、快道「读取 5/4」、GPU 0%、普道空闲，请求一直卡着不推理，为什么？

**现场（14:42 抓的 forensics，存 `~/logs/forensics/fastlane-hang-20261103-1445.txt`）**
- 快道引擎进程 `126014`（11:13 启动）**日志从 11:23 起完全冻结**，之后再没有一行输出；CPU ≈0、GPU0 util 0%、19 个线程全在 `futex_do_wait`、`anon_pipe_read` —— 是**引擎内部卡死**，不是排队慢、也不是显存/带宽打满。
- 上游：11:00 那次是 `EngineDied: the engine stopped unexpectedly (exit code -11)`（SIGSEGV），server 用 issue #27 的"下一个请求再拉起"在 11:13 重启成功，跑 10 分钟又卡死。
- serve 侧：请求在 `generate_batched` 的 `ctl` 锁/slot 队列里等（等 ctl 只有 10 s 轮询、没有总时限），一轮轮被客户端 20 分钟超时收掉；`strata-a.out.log` 里一直刷 `reading the prompt: 9,554 tokens, N s so far`（`progress` 不前进就是这个形状）。
- 代理侧：只做 TCP 探活 + `/v1/status.loaded`，两者对"进程卡死但端口还在听"都是绿的 → 不会 failover，请求只能挂到客户端自己超时。
- 监控侧：`server.py` 只用**一个布尔 `busy`** 表示"有请求在跑"，并发时任何一条请求结束都会把它清掉 → `/v1/status` 的 `activity.in_flight` 读成 0（截图里"在途 5 / 生成中 1 / 速度 0.0 / 普道空闲"就是它）。

**处置**
1. 14:42 `supervisorctl restart strata-a`（只动 8091；普道 8092 未动）。15:0x 引擎加载完成，实测 8091 直连 2.0 s 出字、`/concurrency` 快道 `generating 90 tok/s`。
2. `rag-proxy` 加**首字节看门狗**：`first_byte_timeout_s`（默认 300 s，config.json 已加）。等不到上游第一个字节就按不可达处理、改投兄弟道；首块到达后读超时放宽回 600 s（SSE 安静期不算失败）。
3. `rag-proxy` 修三个真 bug：
   - `except URLError` 抓不到 py3.12 的 `TimeoutError` → 看门狗掐断了也不会改投（已加）；
   - `lane_poller` 里 `hc` 在 `/v1/status` 抛异常时未初始化 → 轮询线程当场死掉、之后 LANE_CACHE 永不更新（已先置 `{}`）；
   - `lane_slots` 把 `concurrency`（serve 侧是**容量** 4/4）当成实时占用 → `lane_has_room()` 永远 False，"优先慢道、慢了再溢出快道"从未生效（改用 `activity.in_flight`）。
4. `Strata/serve/server.py`：`status` 加 `running` 计数与 `owner`；`activity.in_flight = running + queued`，只有 owner 能清 `busy`（并发时不互相踩）。
5. 新测试 `rag-proxy/test_first_byte_timeout.py`：假上游只 accept 不回包 + 健康兄弟道，实测 **3.1 s 改投并拿到 200**（PASS）。

**待办（要停服窗口，等用户点头）**
- 代理重启：当前在跑的代理是 14:52 那版（有看门狗，但缺 TimeoutError 捕获 / `hc` / `running` 修复）—— 要在 8080 无在途时换。
- 普道 8092 重启后才会带上 `server.py` 的 `in_flight`/`busy` 修复（它是公网主流量，必须约窗口）。
- 引擎卡死本身是上游已知的"静默挂死"类问题（`docs` 里 `--mtp-varlen` 的宽窗挂死同源），要另立课题抓 core/堆栈。

**出处**：`rag-proxy/rag_proxy.py`（`_proxy` 看门狗、`lane_poller`、`lane_slots`）、`rag-proxy/config.json`（`first_byte_timeout_s`）、`Strata/serve/server.py`（`run()` 的 `running/owner`、`v1_status` 的 `activity`）、`~/logs/forensics/fastlane-hang-20261103-1445.txt`。

## 2026-11-03 19:02（北京）去 AI 味：首页改控制台、首页动作开新页面、思考卡片修复

**问题（用户反馈）**：页面太像 AI 做的，要减少 AI 感；首页点「新建对话」等不要本页跳转，要开新页面；展开思考卡片看不到思考内容。

**实现**
- **首页从「能力卡广场」改成控制台**：去掉四张并排能力卡、大标题营销文案、装饰性图标网格；改成「工作台」标题 + 一行主操作 + 最近对话/文件（左）+ LLM Lab/知识库/最近生成/Skill·MCP（右）+ 一行工具链接。层级用 hairline / 间距 / surface，不靠渐变和大阴影。`views/HomeView.vue`。
- **全站背景去装饰**：删掉 28px 网格 + 三层点阵「星星」的 `body::before` 纹理，回到纯色底。`public/app.css`。
- **对话空态**：去掉居中的大标题 + 两列胶囊建议卡；改成左上「新对话」+ 一行示例任务列表（人工挑选、和 LLM Lab/知识库/代码相关），并关掉入场浮起动画；空态从竖向居中改为内容在上、输入框沉底。`views/ChatView.vue`、`suggestions.js`（删掉空闲时让模型生成建议的循环，不再占 GPU/生成通用问题）。
- **首页动作开新页面**：`window.open` 新标签打开 `#chat` / `#lab`，当前工作台不被顶掉。要打开的文件/预填提示词/知识库面板用 URL query（`?open=` / `?prefill=` / `?tab=kb`）传给新页面，避免多个 tab 抢同一个 localStorage 键；新页面读取后立即 `history.replaceState` 清掉 query。`HomeView.openNew/startChat/openLab/openKB`、`ChatView.onMounted`、`WorkspaceView.openInitial`。
- **思考卡片修复**：`<details>` 用户点击时浏览器会改 `open`，但 Vue 的 `thinkOpen` 没同步，而正文是 `v-if="thinkOpen || thinkingNow"` → 展开后空白。现在 `onThinkToggle` 把 `thinkOpen` 同步成 `details.open`，并且思考正文始终渲染，由 `<details>` 自己控制显隐。`components/MessageItem.vue`。
- **Toast 不再压输入框**：`.st-toasts` 从右下 20px 移到右上 `top:74px`，并给容器 `pointer-events:none`（toast 本身 auto），避免遮住发送按钮。`public/components.css`。

**实测**：轻量 Puppeteer 线上验证——首页加载、点「生图」开新页面 `#chat`、输入框预填「画一张赛博朋克…」；此前完整 `smoke.mjs` 251 项 ALL PASS（含新页面跳转、思考正文、继续对话）。最新一次完整 smoke 因宿主机 load 40+、Chrome 30s 导航超时未能跑完（环境资源问题，非页面报错）。

**出处**：`Strata/serve/webapp/src/{views/HomeView.vue,views/ChatView.vue,views/WorkspaceView.vue,components/MessageItem.vue,suggestions.js}`、`public/{app.css,components.css}`、`tools/smoke.mjs`。

## 2026-11-04 21:55（北京）监控页 TTS / Embedding 显示「挂掉」但服务正常

**问题（用户反馈）**：监控页里 TTS 与 Embedding 显示挂了，但服务应该还活着。

**结论（已修复并上线）**
- 根因：`rag_proxy.py` 的 `service_defs()` 把三个邻居服务的地址写死成 **旧 Pod IP**（embed `10.42.0.166` / rerank `10.42.0.138` / tts `10.42.0.137`）。Pod 重建后 IP 漂移，健康探测全部 3 s 超时；服务本身没事。
- 实测：旧 IP 三个地址 `curl` 全部超时；集群 DNS `group-embeding` / `group-reranker` / `group-gpt-sovits` 的 `/health`、`/metrics` 全部 200（bge-m3 / bge-reranker-v2-m3 / voice-studio 的 uptime、calls 都在更新）。
- 修复：新增 `_svc_root()`，从 `config.json` 的 `embed_url` / `rerank_url` / `tts.base_url` 推导服务根地址（去掉 `/v1/embeddings` 等接口路径），**监控一律走集群 DNS，不再写 Pod IP**；TTS 反代兜底地址同步改掉。
- 重启 `rag-proxy` 后 `/concurrency` 七项全部 `up=true`（embed 8.5 ms / rerank 6.6 ms / tts 8.3 ms）。
- 教训：邻居服务的稳定入口是 `group-<组名>.clouds.svc.cluster.local`，Pod IP 只能用于一次性排查，不能进代码。

**出处**：`rag-proxy/rag_proxy.py`（`service_defs` / `_svc_root` / TTS 反代兜底）、`rag-proxy/config.json`。

## 2026-11-04 21:55（北京）去掉首页 + ChatGPT 式侧边栏 + 对话页/工具卡重做

**需求**：首页没必要，打开就是对话页；侧边栏做成 ChatGPT 那样，带图库、知识库；对话页的示例提示与工具卡样式要更好看；生图要提示「可以先联网找参考图、搜集信息」。

**实现**
- **去掉首页**：`store.js` 的 `TABS`/`VIEWS` 移除 `home`，默认标签 = `chat`；`App.vue` 不再挂 `HomeView`，品牌点击回对话页；`ChatView` 空态第二个入口改成「个人知识库」。
- **侧边栏（ChatGPT 布局）**：新建对话（主按钮）+ 折叠、会话搜索、图库（最近 4 张缩略图，点开工作空间原图）、知识库（资料名 + 块数，点开 KB 面板）、按日期分组的历史会话；数据来自 `getWorkspace()` 与 `kbList()`，未登录静默为空。`components/SideBar.vue`。
- **示例任务卡**：示例从纯字符串改成 `{tag, icon, tone, text}`，三列卡片（窄屏两列/手机一列），图标底色按类别区分，hover 有轻抬升；生图那条改成「先联网找几张参考图，再画…」。`suggestions.js`、`ChatView.vue`。
- **工具卡**：图标按工具类别着色（文件/代码/任务/知识库/联网/生图/MCP）、状态用文字胶囊（执行中/完成/失败）、执行中有底部脉冲进度线、结果区加复制按钮。`components/ToolCard.vue`。
- **生图先找参考**：`generate_image` 的 schema 增加 `refs`（`image_search` 直链，最多 4 张），系统提示改为「主体/风格不确定先 image_search 找参考、需要真实信息先 search_web，再生成」；生成中占位显示参考图张数，没参考图时提示下次可以让它先联网找；首页入口提示词同步改成「先联网找参考图再画」。`ChatView.vue`、`AgentPanel.vue`、`MessageItem.vue`、`i18n.js`、`HomeView.vue`。

**发布**：`bash tools/deploy.sh` 纯静态发布（不重启引擎/代理），线上 `/` 已指向新产物 `app-BWOMxq9w.js`。Puppeteer 因容器缺 `libglib-2.0.so.0` 无法启动，`smoke.mjs` 本轮未跑；其中若干断言仍按旧「首页」行为写，后续需同步。

## 2026-11-04 22:00（北京）LLM Lab 并入训练页：代码模式 + 监控模式

**需求**：把 LLM Lab 移到训练页面，做成代码编辑器那样；训练时可以点一个按钮切到监控模式（放大成现在训练页的样子）。核心功能，要求完全重新设计。

**实现**
- **路由**：`#train` 现在是训练工作台；`#lab` 保留为旧深链，`store.js` 的 `normTab()` 统一折算到 `train`。App 不再单独挂 `WorkspaceView`，改由 `TrainView` 内嵌。
- **页面结构**：`train/TrainView.vue` 重写为上下两段：固定顶栏（LLM Lab 品牌 / 实时训练摘要：状态徽章、step、loss、best val、进度条 / 「启动训练」/「代码 | 监控」分段切换）+ 占满剩余高度的模式区。
  - **代码模式**：完整 `WorkspaceView`（文件树、编辑器、终端、agent、后台任务/知识库弹层），`v-show` 保持挂载，切到监控再回来不丢未保存内容。
  - **监控模式**：现有训练曲线页原样放大为一整页（进度卡 + train/val loss 两张 + lr/gnorm/tok 三张 + 评估台账 + 配置），独立滚动；没有数据时给空态（命令示例 + 启动训练 + 回代码）。
- **模式记忆**：`train.mode` 存 localStorage；侧边栏图库/知识库、对话页「打开 LLM Lab / 个人知识库」都会先把模式设回 `code` 再跳转。
- **启动训练**：顶栏按钮切到代码模式并让 Lab 弹出后台任务面板（`WorkspaceView` 新增 `openJobs` 计数 prop）。
- **数据源不变**：仍读 `/web/train-data.js`（`window.__TRAIN_METRICS__`），5 秒轮询、90 秒判中断；不改代理/引擎、不重启。

**发布**：纯静态 `bash tools/deploy.sh`，线上 `app-B1x7mxSD.js`。

**出处**：`Strata/serve/webapp/src/train/TrainView.vue`、`views/WorkspaceView.vue`（openJobs prop）、`store.js`（normTab）、`App.vue`、`components/SideBar.vue`、`views/ChatView.vue`、`i18n.js`。

## 2026-11-04 22:14（北京）LLM Lab 换成 Monaco（VS Code 内核）+ 文件树清理

**问题（用户反馈）**：文件树满屏 `__pycache__` 和 `.pyc`（截图里 `__init__.cpy`、`attention.cp…`），
自制的 textarea 编辑器「谁看得懂、谁会用」，要求直接上成熟的 VS Code 前端框架。

**结论**
- **编辑器换成 Monaco Editor 0.57**（VS Code 同一个编辑器内核）：语法高亮、行号、括号匹配/着色、
  代码折叠、多光标、查找替换、Ctrl/⌘+S 保存，`vs` / `vs-dark` 跟随主题；底部加状态栏
  （语言 / Ln,Col / 行数字数 / 保存态 / Spaces:4 / UTF-8）。
- **为什么不是整包 VS Code / code-server**：那是需要常驻服务端进程的完整 IDE，本服务的网页只能由引擎静态目录
  发布（`/web/<扁平>.js|css|svg`，不重启引擎/代理是硬约束）；Monaco 是其中可纯前端落地的编辑器内核，
  正好满足「成熟框架 + 不能停服」。
- **打包约束**：Monaco 走训练页的异步 chunk（首屏对话不下载）；worker 用 Vite `?worker` 打成扁平
  `*.worker-<hash>.js`；`assetsInlineLimit` 调到 1MB，把 codicon 字体以 data URI 内联进 CSS
  （引擎只发 .css/.js/.svg，独立 .ttf 会 404）。`tools/deploy.sh` 的清理规则补上 `*.worker-*.js`。
- **文件树清理**：前端 `reload()` 过滤 `__pycache__` / `node_modules` / `.venv` / `.pytest_cache` 等目录和
  `.pyc/.pyo`；文件用彩色类型徽标（PY/JS/C++/MD…）替代统一图标。后端 `ws_tree()` 同步加了同样的过滤
  （`WS_HIDE_PARTS` / `WS_HIDE_SUFFIX`），**要等下一次 rag-proxy 重启生效**，前端过滤已立即生效。

**发布**：纯静态 `bash tools/deploy.sh`，线上 `app-BlhkwD4I.js`；Monaco 主 chunk 4.1MB（gzip 1.08MB，仅进训练页时下载）。

**出处**：`src/components/CodeEditor.vue`（新增）、`src/views/WorkspaceView.vue`、`vite.config.js`、
`tools/deploy.sh`、`rag-proxy/rag_proxy.py`（`ws_tree`）。

## 2026-11-04 22:48（北京）生图结果挪进工具卡 + 假进度条 + 润色文案

**问题（用户反馈）**：agent 调 `generate_image` 后，图片显示在消息**顶部**、不在对应的 agent-tool 卡片里；
生成期间没有进度条，也没有「正在润色…」这类状态文案。

**修复**
- **图片归位**：`ChatView.agentFollowUp` 不再把结果塞进 `m.images`（那个渲染在气泡上方），改成挂在
  `card.image` 上；`ToolCard.vue` 在卡片下方直接渲染缩略图（点击开大图，复用 `ImageViewer`）。
  工具卡会随会话落盘（`chats.js` 的 `slim()` 展开整个 tool 对象），刷新后图片还在。
  `AgentPanel.vue`（训练页右侧 agent）同步改成 `card.image = r.image`。
- **假进度条**：`generate_image` 的卡片在执行中显示 5%→94% 的渐进条（指数衰减，前快后慢，最多 94%，
  真图回来才消失），每 400ms 刷新；下方渐变高光扫光，配合 `tool__pulse` 底部脉冲。
- **状态文案**：每 3.5 秒轮换一句：理解画面 → 拆解构图/光线/材质 → 铺底色 → 刻画主体 → 精修边缘 →
  润色光影 → 最后检查；参考图数量作为胶囊显示。中英文词条 `chat.image.step.0..6`。
- 移除了旧的 `m.imgPending` 占位骨架（`MessageItem.vue` 与 `ChatView.vue` 的相关逻辑），避免重复占位。

**发布**：纯静态 `bash tools/deploy.sh`，线上 `app-lhj86md9.js`。

**出处**：`src/components/ToolCard.vue`、`src/views/ChatView.vue`、`src/components/MessageItem.vue`、
`src/components/AgentPanel.vue`、`src/i18n.js`。

## 2026-11-04 23:03（北京）空态回退、逐会话草稿、整体耗时、agent 轮数/max_tokens、生图 502 排查

**1）生图 502：不是我们走内网 IP，是 Spark 网关自己挂了**
- 链路：浏览器 → 本站 `/spark/image/v1/images/generations`（同源，key 由网关代持）→
  `spark.base_url = https://spark.ssemarket.cn`（**公网域名**，写死在 `config.json`）。
- 22:53 实测：`https://spark.ssemarket.cn/image/health`、`/embed/health`、`/rerank/health`、`/llm/health`
  **全部 502（nginx/1.31.4）**；`http://ssemarket.cn:18888` 拒绝；tailnet `100.67.13.77:18888` 超时；
  `ssh ssemarket.cn:2223` 也 Connection refused。21:56 时四个 health 还是 200 → 对端在 21:56–22:50 之间整体掉线。
- 结论：与内网/公网选路无关，Spark 机器上的网关/上游挂了，等恢复。前端已把上游 5xx 的 HTML
  翻译成「上游服务暂时不可用（502）」而不是裸 `HTTP 502`。
- 另：日志里的 `/tool/fetch_image` 404 是搜图直链本身失效（源站 404），不会让生图 502。

**2）空态回到最初的样子**：用户要「最开始的样子」。恢复 `public/app.css` 原有的居中大标题（40px）+
两列建议卡（48px 高、描边/阴影）+ 第四格虚线「换一批」+ 入场浮起动画；删掉 11-03 加的左对齐三卡布局
与其 scoped 覆盖。保留「打开 LLM Lab / 个人知识库」两个小入口。

**3）逐会话草稿（真 bug）**：输入框原来是组件级单例，切会话时文字会跟过去。
改为 `drafts`/`attachDrafts` 两个 Map（key = chat id），切走时保存、切回时恢复；首次绑定保留预填，
发送后清空；附件（dataURL）只在内存 Map 里，不落盘。

**4）去掉分段耗时，改整体汇总**：agent 每轮都写一行 meta（`6m41s · tool calls 7`）很乱。
现在 `streamTurn` 只把 token 数累计进 `run`，`finishRun()` 在整轮（含所有工具往返）结束后写一条
`chat.lastRun = {secs, tokens, tools, limit, stopped}`，渲染在**输入框下面**一行：
「上次回答 · 用时 6m41s · 12,345 tokens · 7 次工具调用」。消息流里不再出现分段 meta。

**5）agent 轮数与 max_tokens**：
- `AGENT_MAX_ROUNDS` 8 → **40**（改一个文件+跑测试+修错就超过 8 轮，用户要求尽量别打断）。
- 默认 `max_tokens` agent **16384** / 普通 **8192**（原来是 4096/2048，长回答被截断）。
- `finish_reason=length` 时自动续写最多 **3 次**：把半截回答作为 assistant、追加一条「从断点接着写、
  不要重复/重新开头」的 user 指令，续写内容拼回同一条消息 —— 用户看不到截断。

**发布**：纯静态 `bash tools/deploy.sh`，线上 `app-BfIg6W2k.js`。

**出处**：`src/views/ChatView.vue`、`public/app.css`（回退使用）、`src/hub.js`、`src/i18n.js`、
`rag-proxy/rag_proxy.py`（`ws_tree` 过滤待重启生效）、`rag-proxy/config.json`（spark.base_url）。

## 2026-11-04 23:25（北京）尝试登录 Spark 排查：所有入口都不可达

用户要求「上 spark 看看」。逐个入口实测（23:25）：

| 入口 | 结果 | 说明 |
|---|---|---|
| `ssh sse-dgx-spark`（ssemarket.cn:2223 → Spark:22） | **Connection refused** | frpc 隧道 `ssh-spark` 断了 |
| `ssemarket.cn:18888`（frpc → Spark nginx:8888） | 拒绝 | 同一条 frpc 的网关隧道也断了 |
| `100.67.13.77`（tailnet）:22/443/8888/18888 | 全部超时 | 本 pod / 邻居 pod 都不在 tailnet |
| `175.178.90.215:22`（公网 VM 本体 SSH） | Permission denied | 手上只有 Spark 的 key，没有 VM 账户 |
| `ssemarket.cn:7500`（frps dashboard） | **401** | 需要凭据，我们没有 |
| `https://spark.ssemarket.cn/*/health` | **502（0.1s）** | nginx 活着，后端没有 |
| `https://ssemarket.cn/` | 200 | 公网 VM 本身正常 |
| `pod-embedding`（22035）等邻居隧道 | 可 SSH | 但同样到不了 Spark / tailnet |

**结论**：Spark 那台 `spark-b973` 或它的 frpc 客户端离线（机器关机/断网/frpc 挂了都可能），
公网只有 nginx 在前面空转。没有第二个入口能进去，**需要 Spark 现场/管理员重启那台机器或 frpc**；
或者提供 frps dashboard 凭据 / VM SSH / tailnet 授权，我们才能远程排查。

---

## 2026-11-04 23:49 — 下载 Qwen3.5-4B 作蒸馏学生；停 TTS 后 GPU2 能否训练

**问题**：千问有没有 3B 左右的小模型，下载下来用于后续蒸馏部署 qwen-3.8-flash；如果把 TTS 停了，卡二的余量能不能支持训练？

**结论**：
- Qwen3.5 家族是 2B/4B/9B/27B，**没有 3B**；选了 `Qwen/Qwen3.5-4B`：4.66B
  （文本 4.21B + MTP 0.12B + 视觉 0.33B），bf16 9.32GB，Apache-2.0，原生 262K；
  架构为 GDN+全注意力 3:1、head_dim 256、vocab 248,320、带 MTP/视觉塔 —— 与 Flash-Next 同谱系，
  适合做蒸馏学生。下载在 `/home/cloud/models/Qwen3.5-4B`，两个分片 + index 的 SHA256 与官方一致。
  更小的同架构备选是 `Qwen/Qwen3.5-2B`（4.57GB）。
- GPU2 实测：总量 24,564 MiB，当前 used 19,279、free ~5,285（torch 口径 4,456）。
  邻居 TTS 的 3 个 `gpt-sovits-infer` 共 **8,848 MiB**；`voice-studio` 不占大显存、不用停。
  停 TTS 后 free ≈ **13.3GB**；卡上还有 ~10.4GB 属别的 pod，不可回收、会波动。
- 训练可行性（学生 4B）：QLoRA(NF4) 约 8–10GB ✅ 推荐；bf16 LoRA 约 11–13GB ⚠️ 临界；
  全参 bf16+AdamW 约 49GB ❌；全参+8bit Adam 约 25GB+ ❌（24GB 卡本身就不够）。
- vocab 248,320 → logits 必须分块算 loss；文本蒸馏可冻结/不加载视觉塔，省 ~1.2GB。
- 当前 `Strata/serve/server.py` 没有 logprobs/top_logprobs → 做不了 logit KD，只能
  **序列级蒸馏**（teacher 批量生成 + 学生 SFT）；瓶颈在 teacher 生成（单 lane 70–140 tok/s）。
- 部署回本引擎还需并入上游 qwen35 路径（本地 `SSEStraBed` 源码与二进制都没有 qwen35）；
  llama.cpp 侧已有 `LLM_ARCH_QWEN35` 与 `Qwen3_5ForConditionalGeneration` 转换映射。

**出处**：`docs/reports/QWEN35-4B-DISTILL-FEASIBILITY-2026-11-04.md`；
`Strata/serve/server.py`（无 logprobs）；`SSEStraBed/third_party/llama.cpp/src/llama-arch.cpp:41`、
`third_party/llama.cpp/conversion/__init__.py:240,342`；2026-11-04 15:20 UTC 的 `nvidia-smi` /
`torch.cuda.mem_get_info` 实测。

---

## 2026-11-05 01:21 — 换 2B、停 TTS、蒸馏小秋、训练接网页

**问题**：不能换小一点的模型吗？停 TTS 后卡二能不能训练？把蒸馏过程接到网页训练页给 oauthid=561 看；
数据量要扩、别过拟合；走卡0模型流量训练。

**结论**：
- Qwen3.5 同架构家族 0.8B/2B/4B/9B/27B；选了 **Qwen3.5-2B**（2.274B，
  文本 1.88B + MTP 0.06B + 视觉 0.33B，bf16 4.55GB，`/home/cloud/models/Qwen3.5-2B`，SHA256 已校验）。
- 停 TTS 三个 `gpt-sovits-infer` 后 GPU2 从 19,273 → 10,429 MiB，torch 可用 **13.0 GiB**。
  2B 用 bf16 LoRA 很宽裕；4B 只能 QLoRA / 紧的 bf16 LoRA；全参（4B ~49GB）仍不可能。
- 引擎 `/v1/chat/completions` **不支持 logprobs**（实测传了也只回普通响应）→ 只能做
  **token 级序列蒸馏**：teacher 生成回答 + 学生 SFT，不是 logit KD。
- 训练页机制：训练进程原子重写 `/home/cloud/Strata/serve/web/train-data.js`
  （`window.__TRAIN_METRICS__`，前端 5s 轮询）；原始 log 写
  `/home/cloud/workspaces/u-561/runs/jobs/<jid>.log` + 同名 `.json`（uid=u-561），
  561 的后台任务面板可直接读。不用改接口/不重启引擎。
- 踩坑：① loss 未右移（hidden[t] 对 labels[t]）→ 模型抄输入、loss 掉到 0.0002；
  ② 修复后 step 100/200 val 2.56/2.54 而 train ~0.3 → 过拟合，已停一阶段、降 lr/dropout；
  ③ teacher 160 token 截断 380/413 条，已改为只收 `finish_reason=stop`；
  ④ fla 在 import 时探测 CUDA，必须先 `torch.cuda.init()`，否则 GDN 回退 CPU。
- 小秋身份：117 条训练 / 51 条留出；身份强化后 **51/51 = 100%**（“通义/Qwen”只出现在否定句）。
- 二阶段 teacher-heavy（teacher + 4k 对齐 replay + 身份×4，lr 5e-5）在 teacher≥300 时自动启动。

**出处**：`~/distill-qwen35/`（scripts/prepare_data.py、gen_teacher_v2.py、train_sft.py、
eval_identity.py、stage2_auto.sh、launch_train.sh）；报告
`docs/reports/QWEN35-2B-DISTILL-2026-11-04.md`；`Strata/serve/web/train-data.js`；
`rag-proxy/rag_proxy.py` 的 workspace/job 逻辑（line 3599+）。

## 2026-11-05 13:00（北京）图片闭环与早停修复：生图占位框、train 图片 502、561 只有 thinking

**问题**：① 生图工具调用时只显示通用 `generate_image` 工具卡，没有图片占位框/占位文字；② `#train` 里打开图片全部提示失败；③ 561 的一段对话只输出 thinking 就停，没有 content。

**根因与修复**
- **生图占位框**：`generate_image` 走的是通用 ToolCall 卡片，且没有内联占位。现在 `MessageItem` 的 segments 对 `generate_image` 特殊处理：运行中在**工具调用所在位置**渲染占位框（spinner + 「正在生成图片…」+ prompt + 提示），成功后在同一位置换成图片（可点开/下载）；失败才显示错误工具卡。`AgentPanel` 同样处理。`ChatView` 给工具卡挂 `image`，不再往消息顶部塞图。
- **#train 图片 502**：`_hub_route` 里 POST `/workspace/image` 分支有一句局部 `import base64`，Python 把 `base64` 当成整个函数的局部变量，导致 GET `/workspace/file` 分支执行 `base64.b64encode` 时抛 `UnboundLocalError`，所有图片读取返回 502。删掉局部 import（模块顶部已有），重启 rag-proxy；实测 `GET /workspace/file?path=images/...png` 返回 200、b64 342496 字节。
- **561 只有 thinking**：infer 原始留档 `20261105-124035-8c9359` 显示 `finish_reason=stop / content_chars=0 / reasoning_chars=266 / tool_calls=0`，reasoning 末尾是两段 `<tool_call><function=list_files>…`。起因是 `generate_image` 工具结果只返回 `{ok:true,seed}`，模型以为没生成，想 list_files 找文件，并把工具调用写进 thinking 后 EOS。
  - 工具结果补 `image_url` / `path` / `note`（ChatView + AgentPanel）；
  - 旧历史回喂模型时，若工具卡有 image 但 result 没有 image_url，自动补上（`toolResultForModel`）；
  - 若流结束只有 thinking、没有 content/tool_calls，先解析 reasoning 里的 `<tool_call>` XML 当正常 tool_calls 执行；解析不到就自动补一句「直接回答」重试一次（`retryIfEmpty`）；
  - 已存在的空回答显示明确提示「这次只产生了思考…」，不再留空白气泡。
- **顺带**：toast 移到右上，避免挡发送按钮（此前 smoke 登录后点发送被 toast 挡住）。

**实测**：图片 API 200；构建产物包含 `imgSkeleton` / `image_url` / 空回答提示；smoke 在图片修复前 253 项 ALL PASS（本机 Chrome 后来缺 libglib，无法再跑浏览器渲染测试，但 build/编译与 API 实测通过）。

**出处**：`Strata/serve/webapp/src/{components/MessageItem.vue,components/AgentPanel.vue,views/ChatView.vue,i18n.js}`、`rag-proxy/rag_proxy.py`（删局部 `import base64`）、infer 原始记录 `logs/infer-segs/2026-11-05/seg-123555-62a7d.jsonl.gz`。

## 2026-11-05 13:45（北京）流式工具体验：参考 iwiki，改成单条消息 + 运行中计时

**问题**：agent 执行工具时，一个模型生成完（进入工具执行/下一轮）界面像停了：发送按钮回来、消息动作按钮出现、工具卡只有静止的「运行中」。用户要求参考 `https://iwiki.ssemarket.cn/ai`（仓库 `gitee.com/xindonhub/iwiki`）的流式体验。

**参考实现**：iwiki 的 `apps/iwiki-web/src/components/ai/ToolCallCard.vue` 与 `services/agent-message.ts`：工具是消息里的 step，运行中显示 spinner + 实时秒数 + 语义提示（正在联网搜索…）；只有整轮 done 才结束 streaming。

**改动**
- **同一条 assistant 消息贯穿所有工具轮次**：`ChatView.agentFollowUp` 不再每轮新建 m2 气泡，续写直接追加到原消息；工具卡/旁白/正文按 `round` + `at` 排在同一条消息里。历史改为从 `m.text + m.tools` 由 `assistantMessages()` 重建，删掉会重复喂给模型的 `m.agentMsgs`。
- **整轮保持 busy**：`streamTurn` 不再清 `c.busy`；`agentFollowUp` 在工具执行期间把 `c.busy` 指向当前消息，只有 `finishRun`（全部轮次结束）才清。这样工具执行时发送按钮仍是「停止」，消息动作按钮不提前出现。
- **工具卡运行态**：`ToolCall.vue` 增加 spinner、实时耗时 `x.xs`、按工具名的提示（正在联网搜索/正在生成图片/正在运行代码…）；完成后显示实际耗时。
- **轮次间隙**：所有工具完成、下一轮还在路上时，气泡尾部显示「正在继续…」+ 小 spinner。
- **生图**：运行中在原位置显示占位框，完成后原位换图（上一轮已交付，保持）。
- **构建工具**：`tools/deploy.sh` 改为优先用 `./node_modules/.bin/vite`（Node 18 下 `npx` 解析到 rolldown 版 Vite 会报 `node:util.styleText` 不存在）。

**实测**：本地 Vite 构建通过；线上已发布 `app-CngxWAUU.js`。本机 Chrome 缺 libglib/nss，无法跑浏览器 smoke；API/构建与代码路径已核对。

**出处**：`Strata/serve/webapp/src/views/ChatView.vue`、`components/ToolCall.vue`、`components/MessageItem.vue`、`tools/deploy.sh`；参考 `/tmp/iwiki-ref/apps/iwiki-web/src/components/ai/ToolCallCard.vue`。

## 2026-11-05 14:04（北京）刷新续跑、去分段、输入框回缩

**问题**：① 刷新页面后 agent 运行态消失、直接停；② 对话中间一段一段（旧数据每条 assistant 一条消息，思考是独立灰卡）；③ 发送后输入框保持很高不回缩；④ 对话卡片白色背景想透明一点。

**实现**
- **刷新续跑**：chat 级 `agentRun = {active, startedAt, round}` 在 send 开始、工具执行前落盘（工具卡 push 后也落盘）；`finishRun`/`stop` 置 inactive。ChatView 挂载和切换会话时 `resumeIfNeeded()`：
  - 重跑状态为 running/writing 的工具（`start_job/job_stop` 不自动重跑，标错误让用户先确认）；
  - 设 `continueHint=RESUME_HINT`（“上次生成被页面刷新中断，请从中断处继续…”），复用 `streamTurn → continueIfLength → retryIfEmpty → collectCalls/agentFollowUp` 完成整轮；
  - 刷新时正在传输的 token 无法恢复，但整轮任务会继续，而不是卡在运行中。
- **去分段**：`chats.js: mergeAssistantRuns()` 在载入/云合并时把相邻 assistant 合并成一条（文本 `\n\n` 接、工具卡 `at` 平移、`round` 重新编号）；新运行本来就写同一条消息。思考改为透明、无边框的一行「思考了 X 秒」，展开时左侧细线缩进；assistant 气泡和工具卡背景都改透明（保留细线），不再是一块块白/灰卡。
- **输入框回缩**：`watch(input, () => nextTick(autosize))` —— 程序清空（发送/预填/新会话）后在 DOM 更新完再量高度；之前发送后立刻 autosize 读到旧 scrollHeight，所以一直很高。
- **构建**：继续用本地 `./node_modules/.bin/vite`。

**实测**：构建通过，线上 `app-py5oOwiQ.js`；bundle 含刷新续跑提示/agentRun/透明思考卡。容器 Chrome 缺 libglib，浏览器 smoke 仍跑不了。

**出处**：`Strata/serve/webapp/src/views/ChatView.vue`、`chats.js`、`components/MessageItem.vue`、`components/ToolCall.vue`、`public/components.css`。

## 2026-11-05 14:41（北京）Agent 改为服务端后台运行；生图占位缩小；输入框回缩

**问题**：① 用户要求 agent 真正后台运行，切页面/刷新都不能中断；之前 agent 循环在浏览器里，刷新=fetch 中断。② 生图占位卡 420px 太大，占满屏。③ 发送后输入框不缩回。

**实现**
- **服务端 Agent Runner**（`rag_proxy.py`）：
  - `POST /agent/run`：接收 chat_id/model/messages/max_rounds，开后台线程（daemon），模型调用、工具执行、续轮全在网关里跑；客户端断开不影响。
  - 工具执行器 `agent_execute_tool`：文件读写/编辑/搜索、run_code、job 启动/查/停、kb、联网/抓网页/搜图、service_status、generate_image（Spark，含参考图底图）。
  - 事件流 `GET /agent/stream?run_id&cursor`：SSE，事件 `reasoning/content/tool_call/tool_result/usage/done/error/cancelled`，按 seq 重放；持久化到 `/home/cloud/logs/agent-runs/<id>.jsonl`，代理重启也能读历史。
  - 服务端自己处理「只有 thinking」重试和 length 续写，不再依赖客户端。
- **前端**（`ChatView.vue` + `hub.js`）：agent 模式改为 `startAgentRun` + `openAgentStream`，客户端只应用事件；`chat.agentRun={active,runId,cursor}` 落盘，刷新/切会话时 `resumeIfNeeded` 从 cursor=0 重放并继续跟流。切页面/刷新都不会停（实测刷新时 image_search 正在跑，回来完整重建工具卡和最终回答）。
- **生图占位**：`.img-skeleton` 420→240px（手机 200px），生成后的内联图片 420→320px，不再占满屏。
- **输入框回缩**：`watch(input, () => nextTick(autosize))`，发送后 204px→60px 实测。
- **注意**：并行会话把信息架构改成「打开即对话、LLM Lab 并入训练页、无首页」，本次没有回退；smoke 断言已同步。

**测试**：线上 live——agent 调 service_status 后正常回答；刷新中途（image_search）后自动重放并完成；输入框回缩；服务端 `generate_image` 512²/2步 2.9s 返回图片。完整 smoke 223 项通过，剩余失败是并行 IA 改动后的手机端侧栏断言（与本次功能无关）。

**出处**：`rag-proxy/rag_proxy.py`（`AGENT_RUNS/agent_worker/agent_execute_tool/_agent_*`）、`Strata/serve/webapp/src/{views/ChatView.vue,hub.js,chats.js,components/MessageItem.vue,public/app.css}`。

## 2026-11-05 14:52（北京）生图 400 与占位卡尺寸修复（561）

**问题**：561 用户的生图多次 400；占位卡仍然很大。

**根因（从 561 的 chats + Spark 文档核对）**
- Spark 生图服务是 8 步 turbo：`steps` 必须在 **1..8**，传 40/50 → `400 steps must be 1..8`。
- `size` 必须是 `宽x高` 且两边都是 **16 的倍数**；模型经常传 `"16:9"`（比例不是像素）或 `"1664x936"`（936 不是 16 倍数）→ `400`。
- 之前服务端 `_agent_generate_image` 原样透传 `steps/size`，且 `HTTPError` 的响应体被丢掉，只显示 `HTTP Error 400`，所以不容易定位。
- 另外早期若干 502 是 Spark 服务瞬时不可用，不是参数问题。

**修复**
- `_agent_image_size()`：比例串映射到官方档位（16:9→1280x720、9:16→720x1280、1:1→1024x1024、4:3→1024x768、3:4→768x1024、21:9→2048x864）；像素尺寸四舍五入到 16 的倍数并限制面积；非法/过大按比例找最接近档位。
- `steps = clamp(1..8)`；去掉未文档化的 `n`。
- `HTTPError` 捕获并回传 Spark 的响应体（`生图服务 400: ...`），下次能直接看到原因。
- `hub.js generateImage` 同步做尺寸/步数归一，AgentPanel/旧客户端路径也不会再传 40/50。
- 占位卡从 420×420 大块改成 **84×84 缩略框 + 右侧文字**（手机 64×64），总高 84px；生成后的内联图从 320 降到 260。

**实测**：`16:9 + steps 50` 归一为 `1280x720 + 8`，10.9 秒成功出图；占位实测 420×84（框 84×84）。代理已重启，线上 `app-CShTwnVY.js`。

**出处**：`rag-proxy/rag_proxy.py:_agent_image_size/_agent_generate_image`、`Strata/serve/webapp/src/hub.js`、`public/app.css`、`components/MessageItem.vue`；Spark 文档 `~/.claude/skills/ssemarket-sparklab-multimodal/references/{image-guide,api}.md`。

### 2026-11-05 补充：占位卡改内联样式，彻底防止横向拉条

- 之前按 CSS 类控制的紧凑占位，在用户旧缓存/其它样式覆盖下仍可能变成全宽方块并出现横向滚动条。
- 现在占位结构直接用**内联样式**：`display:flex; max-width:380px; width:fit-content`，缩略框固定 `64×64`，文字区 `min-width:0` + 省略号；生成图内联 `max-width/height:240px`。这样即使外部 CSS 旧/被覆盖，也不会撑宽。
- 实测：长 prompt 占位 380×64、框 64×64；561 历史对话图片 240×240；`chat-scroll` 的 `scrollWidth == clientWidth`，无横向滚动条。
- 线上 `app-Cd39q_jt.js`。

---

## 2026-11-05 17:56 — thinking/多模态/同质 val 与最终服务

**问题**：微调没训 thinking/多模态，原生支持是否就够？val 为什么不降？服务怎么走网关？

**结论**：
- 原生 Qwen3.5-2B 支持 thinking（思考过程）与视觉；但早期 `merged/xiaoqiu` 剥了视觉塔、
  SFT 只用 `enable_thinking=false`，所以不支持。重建 `merged/xiaoqiu-vl3`（完整 VL 底座 +
  文本 adapter）后：视觉正常、thinking 可开（中文推理）。
- 旧 val 是通用对齐集，不是蒸馏目标分布，所以不降；改用同质 teacher 留出后：
  thinking loss 1.6336→1.3718，general 0.9898→1.0650（学到思考、通用略漂移）。
- thinking 数据必须让 teacher “用中文思考”，否则默认英文推理；并过滤英文占比高的样本。
- 训练验证：身份 58/58=100%（0 次自称 Qwen）、工具 5/5、视觉正确、thinking 算术正确，
  复杂逻辑仍弱（2B 容量）。
- 服务：vLLM 0.33（独立 `.vllm-venv`，torch 2.14+cu130），`--enforce-eager` 稳定；
  flashinfer JIT 在 CUDA13/sm89 编译失败，已禁用采样器。128K/16 并发 182 tok/s。
- 网关：`rag-proxy` 加 `model_routes.xiaoqiu → 127.0.0.1:8093`，非 Strata 模型直接转发；
  公网 `model="xiaoqiu"`，Bearer token 在 `~/logs/strata-api-key.txt`。

**出处**：`docs/reports/QWEN35-2B-DISTILL-2026-11-04.md` §9–10；
`scripts/gen_teacher_think2.py`、`merge_vl.py`、`eval_teacher_val.py`；
`rag-proxy/config.json` 与 `rag_proxy.py` 的 `route_upstream`。

## 2026-11-11 11:53（北京）新增公网反代 `xiaoqiurobot.ssemarket.cn → https://ssemarket.cn:23008/`

- **需求**：给四足机器人公网控制台加一个免端口子域名，且不能影响现有反代。
- **公网入口的真实归属**：`*.ssemarket.cn` 都解析到 `175.178.90.215`；80/443 由 Docker 容器
  `sse_market_server-nginx_proxy-1` 收口，配置文件 `/root/SSE_Market/nginx_proxy/nginx.conf`
  （bind mount 成容器 `/etc/nginx/custom.conf`）。`23008` 是宿主机 nginx 1.18.0 上的 https vhost
  （`server_name ssemarket.cn`，后端 `127.0.0.1:13008`，YatTerra 组 `sheentan-xiaoqiu-robot`）。
- **怎么进去**：本 Pod 没有这台 VM 的 SSH；走 `ssh sse-dgx-spark`（Spark）上的 `Host ssemarket`
  （`sse@ssemarket.cn:22`，key `~/.ssh/id_ed25519`）两跳。VM 上 `sse` 属于 `sudo`/`docker` 组。
- **改法**：完全按配置里 `ai.ssemarket.cn` 的模版追加两个 server 块（80 跳 443 + 443 反代），
  证书复用 `*.ssemarket.cn` 通配 SAN；上游必须 **HTTPS + SNI/Host 都设 `ssemarket.cn`**（http 访问 23008 会 400，
  未知 SNI 会落 444 默认块）；带 WebSocket Upgrade 与长超时（机器人遥测）。
- **安全步骤**：先 `cp -a` 备份（`nginx.conf.bak-xiaoqiurobot-20261111-115141`）→ 容器内
  `nginx -t -c /etc/nginx/custom.conf` 通过 → `nginx -s reload -c ...` 平滑重载（`restart` 会断所有子域，禁用）。
  只有一条既有告警 `conflicting server name "derp.ssemarket.cn"`（与本次无关）。
- **实测**：新站 `/` 200（`Quad 公网控制台`）、`/style.css` 200、http 301 到 https；
  `ssemarket.cn`/`ai`/`iwiki`/`api`/`platform`/`ces` 全部仍 200。
- **出处**：VM `/root/SSE_Market/nginx_proxy/nginx.conf`、宿主机 `/etc/nginx/sites-available/https-web.conf` 的 23008 块；
  留档 `docs/services/public-nginx-xiaoqiurobot-2026-11-11.md`（已备份 MinIO `docs/public-nginx/`）。

## 2026-11-13 10:08（北京）能不能在 Pod 里部署 `Zagitta/gpu-unlock`？

**问题**：用户要找 Zagitta/gpu-* 这个仓库部署并给使用说明，先判断适配性。

**结论**：
- 仓库名是 **`Zagitta/gpu-unlock`**（不是 gpu_poor）；C + 内核模块，18.9k star，2026-10-18 建，
  解锁 GeForce 的 BF16/FP16/TF32 **FP32 累加**半速限制（4090 165→329 TFLOPS 峰值）。
- **本 Pod 不适配，无法部署**：它靠内核模块在 `nvidia` 绑定前（initramfs 阶段）改 SS0/SS1 并做 FLR。
  容器里 `capsh` 当前/Bounding 都**没有 `cap_sys_module`、`cap_sys_admin`、`cap_sys_rawio`**，
  `/lib/modules` 不存在（无主机内核头），`lspci` 无权限；三张卡早已被 580.178.04 绑定，
  README 明确「boot 后手工加载对已绑定卡无效」。
- 唯一可行路径 = **宿主机级**：`make install-deb`（装 initramfs drop-in + DKMS）→ 内核 cmdline 加
  `gpu_unlock.targets=<PCI>` → **重启**。本机宿主机是 53 租户共享、跑着对外推理服务，重启与驱动态改动
  超出本组权限，必须走平台管理员 + 用户同意（红线：停服先经同意）。
- 收益面：引擎里**只有 `src/prefill/gemm.cu` 用 cuBLAS**（`CUDA_R_16BF × CUDA_R_16BF → CUDA_R_32F`,
  `CUBLAS_COMPUTE_32F`，正是被限速的那条路径），解码走自研内核且是专家带宽 bound → **解码 0 收益**；
  prefill 还卡在每层流同步，实际 TTFT 收益 < README 的 25–30%，需解锁后实测。

**出处**：github.com/Zagitta/gpu-unlock README「How it works / Boot flow / FAQ」；
本机 `capsh --print`、`ls /lib/modules`、`nvidia-smi`(580.178.04)、`/shared/pressure/pressure.json`
（3 张 4090D、53 租户、GPU1 util 1.0）；`Strata/src/prefill/gemm.cu:388-411`。

## 2026-11-13 22:15（北京）上游 Strata 有没有「支持并发」的 PR？

**问题**：看看远端（Niko1221/Strata）的 PR，有没有支持并发的。

**结论**：
- **有，而且是一整条线，但截至今天全部未合并**。上游历史上只合并过 30 个 PR（全部 2026-09-28/29），
  9 月底之后社区 PR 一律挂着 open —— 想用只能从作者 fork 的 head 分支拿（base 都是 `main`≈引擎 0.1.5x）。
- 与我们（0.1.38 fork、DeepSeek-V4.1-Flash、单卡/单 lane）相关的：
  - **#5662** 固定物理槽 `--unified-slots`：请求从 prefill 到完成绑定同一物理槽 + drafter 会话，
    邻居进出只改计算批、不迁移会话状态（+1829/-158，31 文件，mergeable=unstable）。
  - **#5726** 并发槽下的多步 MTP `--batch-mtp-multistep`：并发时每槽可验到 `--spec`-1 个候选
    （stack 在 #5662 上，+2027/-176，35 文件，clean）。
  - **#5623** 跨流式会话共享不可变 host KV 前缀 `--kv-share-prefix`（COW 写、含 #1614 pinned pool，
    +3626/-233，35 文件，clean）→ 直接对应我们的 TTFT/停车快照重读。
  - **#5500** L2 session cache（回访长会话 prefill 降 ~95%，+7120/-268，mergeable=dirty）。
  - **#5877** DeepSeek learned starts（新会话复用共享开头，3×L40S 17.8→12.3 s）。
  - **#5712** NUMA 感知的空闲 CPU 专家预取（opt-in 投机读，不改变专家同步结构）。
  - **#5820** 按 API key 的并发上限与保留容量（多租户公平，不提速）；**#5657/#5305/#5306** 槽位
    claim 转移、logit_bias 泄漏、监控口径小修。
  - **不适用**：`#5860/#5862/#5864/#5866/#5868` 双卡层切分批槽（我们是两卡各跑一条 lane）、
    `#5837` helper GPU 3→N、以及 Metal/SYCL/Windows 那些。
- **移植成本**：#5662 的 31 个文件里 20 个在我们树里存在、#5623 是 23/35，但它们 base=main、
  我们改过 `src/program/generate.cpp`、`serve/server.py`、前端，直接 apply 会大面积冲突；
  真要用得按它们的做法在我们树上重做（或先把 fork rebase 到某个上游 tag）。

**出处**：GitHub API（经 `gh-proxy.com`）——`/repos/Niko1221/Strata/pulls`、`/search/issues?q=...is:pr is:merged`
（total_count=30）、各 PR 的 `files` 列表与本机 `~/Strata` 路径比对（`CMakeLists.txt: project(strata VERSION 0.1.38)`）。

## 2026-11-13 22:21（北京）逐块看 #5662 / #5726 / #5623 到底改了什么、在我们树上能不能落

**问题**：按上一问的结论去看上游并发 PR 的实际改动面。

**结论（内容）**：
- **#5662 `--unified-slots`**（+1829/-158，31 文件，无冲突但 CI unstable）：
  - server.py：引擎 INFO 输出 `unified_slots=1` 后，服务端**不再走 GEN 单独路径**（`if alone and left > 1 and info.unified_slots != 1`），
    一律 BGEN 从起始就进槽；返回 `_may_go_solo` 也直接 False；离开就回不到 solo。
  - 引擎：请求级 owner 指针 `request_state/request_draft/request_df2` 在 GEN/BGEN 入口切换；槽位会话拿到自己的
    PLE 历史指针；`Verifier::bind_session()` 把空闲单级验证器重绑到同几何的槽会话（销毁 solo 图、保留显式槽批图）；
    新增 `Verifier::run_slot()` 让「只剩一个活跃槽」用**单序列 kernel** 在该槽里跑，而不是走批窗口。
  - 每槽私有：MTP drafter 新增自有 bias 拷贝 `cbias_`（`set_draft_sampling(sp, err)` 同步等空闲）、
    `prepare_draft()` 预捕获 coupled 图、`chain_launch(..., coupled)`；DFlash2 每槽一个 drafter（`fixed_df2`）；
    视觉 mrope 变成会话级表（`session_mrope_init/upload/reset` + `MropeScope`），不再全局一张表。
  - 调度：窗口按 `active_count` 选 `window_mtp/window_df2/multistep_window`，每槽 `proposal_budget`，
    cvec 按窗口归组，预填 yield 后状态留在该槽（`owner.partial/partial_plan`），`BADM` 新增 continuation 语义。
  - 顺手修：`set_logit_bias` 后 `cudaStreamSynchronize`（页锁 H2D 可能未完成）、`session_mrope_upload` 同样补同步；
    新增 `mtp_sampling_test` / `session_mrope_test`，`test_parallel_yield.py` 加两条 unified 用例。
- **#5726 `--batch-mtp-multistep`**（stack 在 #5662 上，+2027/-176）：把「并发槽每槽最多验 1 个 MTP 候选」
  放开到 `--spec-1`，深度受 `slot_draft_limit()`（剩余输出/上下文）、置信度、`--mtp-max-t` 约束；默认关；
  指标按 `bt_tokens`（押出的候选数）而非行数统计。增量很小（约 15 处生成逻辑分支）。
- **#5623 `--kv-share-prefix`**（+3626/-233，35 文件）：新增 pinned host KV 池
  （`kv_pool.hpp/.cpp`、`kv_chunks.hpp/.cpp`、`kv_pool_policy.hpp`），按 `--kv-chunk-tokens`（默认 4096，4×2^n）
  分块、引用计数 + COW；`--kv-pool-tokens` 定池预算（≥1 个 `--max-context`），会话按需取块；
  完成的前缀块可被其它会话/RAM 快照共享（partial tail、indexer、GDN、PLE、drafter 状态仍私有）；
  池满时先让空闲缓存会话让路。base 依赖 #1614 的 pinned-KV pool。

**结论（落地测算）**：在 `~/Strata`（0.1.38）上 `patch --dry-run`：
- #5662：147 hunk，25 过、138 失败、9 个文件不存在；
- #5726：153 / 25 / 144 / 13；
- #5623：160 / 53 / 125 / 5。
根因是我们落后上游 21 个小版本（main 已 0.1.59.1）：缺 DFlash2、缺异步 chain 草稿（`chain_launch/prepare_chain`）、
缺稀疏槽位批调度（我们仍是「S=最高活跃槽+1、空槽补 pad 行」的 0.1.38 写法，没有 `rows[]/first[]/nrow[]/next_slot` 轮转）、
缺 `run_slot_rows/set_slot_spec_q/bind_session`、缺 #1614 的 pinned KV 池。**不是逐个 apply 能解决的**。

**出处**：PR diff 落在 `/tmp/upstream/{5662,5726,5623}.diff`；本机比对 `~/Strata/src/program/generate.cpp`
（`batch_step()` 的 `S = b+1` 补 pad 行、无 `next_slot`）、`~/Strata/serve/server.py:657 generate_batched()`、
`~/Strata/include/strata/core/verify.hpp`（只有 `run_slots/run_slots_chain`）。

## 2026-11-13 23:40（北京）单路到底多少 tok/s？前端后台库里的历史数据

**问题**：实验里 1 路只有 ~39 tok/s，用户指出历史数据约 120，要求查前端后台数据库。

**结论**：
- 前端后台库 = `~/logs/infer.db`（rag-proxy 的 `infer_store.py` 写 SQLite；元信息 L2，分段/MinIO 存全文）。
  关键列 `decode_tok_s = completion_tokens /（结束 − 首帧）`，**纯解码口径，不含 TTFT**。
- 按「同一时刻是否还有别的请求在跑」分独占/并发（14 天）：
  **快道 8091 独占 median 122 tok/s（p90 128）**、并发 118；
  **普道 8092 独占 median 96（p90 103）**、并发 36；近 2 天普道独占 94（快道几乎无流量）。
- 实验里的 39.5 tok/s 是**端到端**（256 token / 含 TTFT 总时长），换算解码口径 ITL 13.3 ms ≈ 75 tok/s；
  仍低于线上 94–122，原因是：实验时 lane B 在接真实流量（解码 76% 时间等主机 CPU 专家，两引擎抢 CPU/PCIe）、
  测试实例专家缓存冷（命中 70–90% vs 线上 90%+）、合成提示词 MTP 接受率不同。
- 口径教训：跨引擎/跨实验比较必须用**解码口径**（首帧之后），不能用总时长；且必须在同类负载下比。

**出处**：`rag-proxy/infer_store.py:600-620`（tok_s 算法）、`/home/cloud/logs/infer.db` 的 `infer` 表；
实验脚本 `~/engines/test/bench.py`（ITL 分位）。

## 2026-11-14 01:10（北京）两卡分别部署 vs 两卡一起部署，并发会不会更好？

**问题**：把两张 4090D 从「各跑一条 lane」改成「两卡一起跑一个实例」，并发效果会不会更好？

**结论（截至目前的证据）**：
- **理论**：我们解码 94% 时间在等主机专家（PCIe/内存带宽 bound）。合并后单实例可用 48 GB 显存，
  专家缓存从约 10 GB（10/47）抬到约 40 GB，每 token 的主机专家搬运显著减少——这是合并最大的潜在收益。
  代价：层切分每层边界走 PCIe、单请求延迟变差；失去两条 lane 的独立性与滚动升级；单点故障。
  总吞吐天花板不升，因为主机内存/PCIe 带宽原本就是共享的。
- **实测尚不成立**：双 lane 全停后用 GPU0+1 试了两种接法，均未启动成功：
  ① `--layer-split 24` → 引擎按卡加载权重后 `cannot create the main stream`（建 CUDA stream 失败，
     代码未打印 CUDA 错误码；单独 CUDA 程序验证三卡建流正常，非容器问题）；
  ② `--expert-cache-device1 auto` → 被现网 server.py 自动补的 `--layer-split auto` 冲突而拒绝；
     上游的正确表达是 `--peer-device 1`（server 会因此不再加 split）。
- 要下结论需再约一个双 lane 停机窗口：补 CUDA 错误码诊断 + `--peer-device` 重试。
- 参考：上游 2×3090 的 PR（#5860/#5864/#5868）在「模型单卡装不下、部分专家在 CPU」的形状下测到
  「两个对话同时跑 +53%」，与本机形状一致，说明合并有值得验证的空间。

**出处**：引擎源码 `src/program/generate.cpp:5431`（建流处）、`serve/server.py:1102`（自动补 layer-split）、
上游 `serve/server.py:3881 layer_split_of()`（--peer-device 例外）；实验配置 `~/engines/test/two-*.json`，
日志 `~/engines/test/two-layersplit.log`、`two-expertcache.log`。

## 2026-11-14 01:35（北京）两卡一起部署 vs 分别部署：8 路实测结论

**问题**：把两卡分别部署（两条 lane）改成两卡一起部署（层切分单实例），并发会不会更好？

**结论：不会，分别跑更好。**
- 条件：两条 lane 均为新引擎 0.1.59.1 + `--unified-slots`（每道 batch 4）；两卡单实例为 `--layer-split 24` + `--batch 8`；
  对外流量关闭、唯一前缀 1200 token 提示、256 输出、每档 2 轮。
- 4 路：双 lane 107.8 vs 合并 110.6（基本持平）。
- **8 路：双 lane 141.6 vs 合并 107.7 → 合并低 31%**；TTFT 1.69/2.63 s vs 2.28/5.85 s；单请求 20.1 vs 14.5 tok/s。
- 合并唯一领先的是**单请求解码**：1 路 ITL 8.0 ms ≈ 125 tok/s（双 lane 单独约 60–70），来自 48 GB 显存把专家缓存
  从 ~10 GB 抬到 ~30 GB。
- 机制解释：合并多一倍缓存，但少一套独立调度/prefill 通道，并引入每层跨卡 handoff；解码受主机专家带宽限制，
  缓存收益抵不过调度与通信开销。结论：**并发场景保留两条独立 lane**。

**出处**：`~/engines/test/results/{two-lane-clean,two-ls8,two-layersplit,two-peer}.json`，
报告 `docs/reports/engine-upgrade-unified-2026-11-13.md` 第 10–11 节。

## 2026-11-14 01:50（北京）网关双道调度器：规则、数据依据与为什么能优化并发

**问题**：前端/网关要有调度——快道请求去人少的道；普道先看卡1（8092）是否满，满了且卡0（8091）空闲才去卡0；
按历史数据在快道高峰时段给卡0留位置；没人用时两张卡都用起来。

**实现**（`rag-proxy/rag_proxy.py` + `config.json` 的 `lane_scheduler`）：
- 快道模型（`qwen3.8-flash-fast`/`strata-a`）→ 两lane中实时 `in_flight` 更少者（持平留快道）；
- 普道模型 → 普道有空槽走普道；普道满且快道 `load < 4 - reserve` 才溢出快道；否则留普道排队；
- 分时预留（`reserve_by_hour`）：09–12 点留 3 槽、23–01 与 06–07 点留 2 槽，其余不预留；
- 空闲分摊：普道有人、快道全空 → 新请求分到快道，两张卡都用起来；
- `pinned_models`（strata-a/strata-b）保持死钉，不受调度影响；取不到实时负载按"完全空闲"保守处理。

**数据依据**（infer.db 近 14 天）：快道并发 ≥4（4 槽全满）的时长占比在 09–12 点为 61–66%、06–07 点 32–44%、
23–01 点 10–23%；其余时段 <20%。普道约 5.4–6.4k 请求/小时，快道 100–550/小时。

**为什么能优化并发**：把两条 lane 的 4+4 槽合成一个 8 槽池（实测双 lane 8 路 141.6 tok/s，单卡 4 路约 70–75）；
消除队头阻塞（引擎 prefill 串行，长 prompt 只堵它自己那条道）；高峰给快道留槽，快请求不再排在普道批量任务后；
低负载时分摊到两张卡。

**验证状态**：静默窗口下 8 路混合 A/B 仍待做（当前内部流量持续，测试被真实请求污染）。

**出处**：`rag-proxy/rag_proxy.py`（`schedule_lane()`、`route_upstream()`）、`rag-proxy/config.json` 的 `lane_scheduler`、
报告 `docs/reports/gateway-lane-scheduler-2026-11-14.md`。

## 2026-11-14 22:30（北京）微信文章：腾讯开源 DSpine（Qwen3.8-27B 上吞吐 2.7–3.8 倍）

**问题**：这篇公众号文章讲了什么？

**结论（原文核心）**：
- 腾讯优图 + 上交 + 厦大开源 **DSpine**（论文《Draft in Parallel, Condition Through Depth》，arXiv v1 2026-09-28），
  先支持 Qwen3-4B/8B，10-16 放出 8B 草稿权重，近期补上 **Qwen3.8-27B** 草稿权重与推理支持。
- 做法：小模型（27B 配套草稿器约 2B、5 层、8K 滑窗）**并行起草一块 16 个位置**（1 个已知起始 + 15 候选），
  大模型整块验证。核心创新 **Adjacent Causal Injection（相邻因果注入）**：草稿网络每算完一层就把前一个位置的
  预测特征门控注入到后一个位置，越深越"往后传"，解决并行草稿彼此不连贯、验证只能接住从头连续那段的问题。
- 细节：token 表示表由目标模型 embedding 经归一化 + PCA 白化降维得到（已有 token 查表、未定用网络预测特征）；
  每层加余弦监督对齐正确 token；训练早期按概率换入真 token 再退火；目标模型/输入 embedding/LM head 冻结。
- 选词：**last-write refinement**（前驱选定后回写最后一层修正后继分数）+ 每对相邻位置预计算 **top-K(K=16) 转移表**
  （transition cache），顺序选词时只查表不再做矩阵乘；小算子融合 kernel，五层注入额外开销 ≤0.15ms/轮。
- 实测（SGLang 0.5.22 / BF16 / FA3、8 路并发回放、开前缀缓存、tokens/s 合计）：WorkBuddy Code/Web/Office
  低推理档 253→686、247→773、210→806（2.71/3.13/3.84×）；xhigh 3.18/3.33/3.55×；SWE-bench Pro 242→713、τ²-bench 301→964。
  对比 DFlash2（b8）同一回放 590 vs 686（DSpine 用 b16 + 动态验证 + ReplaySSM）。
- 代价面：验证更宽要更多计算；27B 版用**动态验证宽度** + 8 路间按草稿置信度分配验证位置 + **SPS 调优工具**（配置与 GPU 绑定）；
  混合注意力的递归状态用 **ReplaySSM** 在验证后对齐缓存，只从真正接受的前缀续算。
- 社区：AJ 的 llama.cpp 分支 + GGUF（3090 24G / Q4_K_M：代码 128.7、数学 122.8 tok/s，混合贪心 28.2→90.1；
  8K/32K 深度生成 35.6/31.1 tok/s）；MLX（M4 Pro 短提示 2.70×、32K 1.96×；验证 16 行成本是 8 行的 1.65 倍，
  平均接受长度约 7.2 但额外验证吃掉部分收益）。

**对本项目的可用结论**：
1. 与我们在跑的 MTP/varlen 投机同属"草稿-验证"，但 DSpine 是**外挂独立草稿模型**，不占目标模型容量；
   我们的瓶颈是解码 94% 时间等专家（PCIe/主机带宽 bound），外挂草稿器的算力不抢专家带宽，方向上不冲突。
2. 最值得抄的是**验证宽度的动态化 + 按置信度分配**：我们已有 `--mtp-varlen` 的草稿深度约束，可把"验证多宽"做成运行时按负载/置信度调整。
3. 反面教训与之呼应：Mac 上 16 行验证成本 1.65×8 行、接受长度 7.2 仍不划算 → 验证宽度必须与"多接受多少"一起算，
   不要盲目加宽（与已有 P2「窗口加宽 NO-GO」结论一致）。
4. 前置条件：需目标模型同架构的草稿权重；我们服务的是 MoE Flash-Next，官方暂无对应草稿器，短期不能直接上，只能借设计。

**出处**：https://mp.weixin.qq.com/s/uQ_DdqcIwAa9QIs9NaCzFA （AINLP）；
项目 https://github.com/Tencent/DSpine ；权重 https://huggingface.co/tencent/Qwen3.8-27B-DSpine-b16 ；论文 arXiv 2609.36173。

## 2026-11-14 22:33（北京）DSpine 草稿模型能否直接用到我们的引擎上

**问题**：腾讯 DSpine 的小模型权重在哪？下载到本机做点改动；它是「把 MTP 架构升级了」而不是训了个小模型吧？能否用到我们的模型上？

**结论：用不上，已停止下载（权重 0 字节，只留了 27KB 的 config/README 作参考）。**

1. **它不是 MTP 升级，确实是独立训练的小模型**：`tencent/Qwen3.8-27B-DSpine-b16` 是独立 checkpoint
   （`architectures=DSpineDraftModel`，5 层、hidden 5120、32 头/8 KV、dense MLP 17408、block_size 16、
   滑窗 8192、vocab 248320，BF16 4.05GB ≈ 2B），训练时目标模型/输入 embedding/LM head 全冻结，
   推理走 SGLang 0.5.22。思想上是「多 token 预测」的升级（串行链 → 并行块 + 深度条件），但形态是外挂草稿模型。
2. **和我们目标的硬不匹配**（本机 `strata-iq3_xxs.json` + `Strata-data/packs/iq3_xxs`）：
   | 项 | DSpine 草稿 | 我们的 Flash-Next |
   |---|---|---|
   | 目标模型 | Qwen3.8-27B dense（64 层） | Qwen3.8-Flash-Next MoE（QSA/indexer 稀疏注意力 + hyper-connection） |
   | hidden | 5120 | 2560（`index.txt`：token_embd 2560×248320） |
   | 草稿结构 | 5 层 dense，跨层相邻注入 | 官方 MTP **仅 1 层**（`mtp.*` 31 个张量：fc_embedding/fc_hidden、hyper_connection_mixer、experts、indexer） |
   | 词表 | 248320 | 248320（**只说明同 tokenizer**，embedding 空间仍绑 27B） |
   - 草稿的 token 表示表来自 27B 的 embedding（归一化+PCA 白化），换目标就失效；
   - 我们引擎没有 `DSpineDraftModel` 加载路径，MTP 走 `src/core/mtp.cpp` 的 dense.txt/dense.bin/experts 格式；
   - DSpine 的核心「相邻因果注入」靠**深度**传递，1 层 MTP 没有地方放。
3. **可以借的（按性价比）**：
   - **按请求分配验证宽度**：DSpine 的 dynamic verification + 置信度分配。我们的 `draft_batch(S,L,...)` /
     `batch_chain_` 目前所有槽共用同一个 L（键 `S*16+L`）；改成按槽分桶 L 是**不训练、纯推理侧**的改动，
     也是我们 `--mtp-varlen` 最自然的下一步。
   - **验证宽度的代价必须和边际接受一起算**：与已有 P2「窗口加宽 NO-GO」一致；DSpine 自己也承认
     Mac 上 16 行验证成本是 8 行的 1.65 倍。
   - **块并行草稿**要落地只能自训一个多位置草稿头：本机 `Qwen3.5-4B`（hidden 2560、vocab 248320，与目标同宽同词表）
     是合适的底座，但那是训练项目，不是「简单改动」。
4. 若只想本地玩这个模型：还需要 Qwen3.8-27B 目标权重 + SGLang，本机没有、且与线上服务无关。

**出处**：`tencent/Qwen3.8-27B-DSpine-b16` 的 `config.json`（本机 `~/models/Qwen3.8-27B-DSpine-b16/config.json`）；
`Strata-data/mtp/mtp-inventory.json`（MTP 仅 layers.0）；`src/core/mtp.cpp` `capture_step(j)`（逐 step 串行）；
`src/spec/draft_policy.cpp`、`src/spec/controller.cpp`；`~/models/Qwen3.5-4B/config.json`。

## 2026-11-14 22:42（北京）DSpine 源码拆解与改造我们 MTP 的可行性

**问题**：按 DSpine 的思想改造我们的 MTP（我们有原生 MTP，不需要它的权重）——仓库怎么做、能不能移植？

**结论**：机制可移植，但缺的是**训练范式**，不是推理开关；内核能力已具备。完整方案见
`docs/reports/mtp-dspine-port-plan-2026-11-14.md`。

**DSpine 源码要点**（repo @ d211869，本机 `~/archive/upstream/DSpine`）：
- 块并行：一块 16 行 = 1 个已知 anchor + 15 个 mask 位置，5 层 DFlash decoder，块内因果注意力，滑窗 8192
  （`sglang_dspine/models/dspine.py:388/474`）。
- 相邻因果注入 `DSpineCodeBridge`（`models/dspine.py:258-352`）：每层后读码（RMSNorm→W_read→anchor 查 codebook /
  其余归一化预测码）→ 门控 `sigmoid(query_self + sender_prev + bias)` → 写回 `out = raw + rms·(message·W_write)`；
  anchor 行不写；随后 RMSNorm 进下一层。rank 512、code_size 1024、5 层全激活。
- 选词：top-16 候选 + last-write refinement（用已选前驱的精确码重做最后一层写），16×16 转移表用 FP64
  因子分解**预计算**（`sampling/factorized/kernels.py:179`），选词只查表。
- 验证：DSpark 动态验证宽度 + SPS 调参；混合注意力用 ReplaySSM 对齐只接受前缀后的状态。

**我们 MTP 的硬事实**：仅 **1 层**（`mtp.layers.0.*`，hidden 2560、512 experts、HC=4、indexer）；
`draft()` 是**逐 token 图启动 + host 同步**的串行链（`src/core/mtp.cpp:1086`，`capture_step(j)`）；
`record_forward(T)` 支持多行但 full layer 目前只允许 batched chain 多行；`qsa_decode_attn_batch` 按 step 的
`n_kv` 做因果注意力（块内因果天然支持）；`kv_restore`、`kVerifyMaxT=32` 都在；`--mtp-varlen` 是**统一 L**。

**判断**：M0 块并行管线（不训练）可先做，测"草稿阶段摊销专家读取"的收益与 mask token OOD 掉多少接受长度；
真正的 2-3× 来自 M1/M2 的桥接与多层训练（目标冻结，只训草稿），需要 GPU 训练窗口。
mask id 别用 248070（我们词表里是 `<|audio_start|>`），用 `[PAD248077]` 这类占位符。

**出处**：`~/archive/upstream/DSpine`；`docs/reports/mtp-dspine-port-plan-2026-11-14.md`；`src/core/mtp.cpp`；
`Strata-data/mtp/mtp-inventory.json`。

## 2026-11-14 22:49（北京）能不能直接用腾讯为 Qwen3.8 训练的 DSpine 草稿权重

**问题**：直接用腾讯基于 Qwen3.8（27B）训练的 DSpine 草稿模型行不行？

**结论：不行，三重硬约束，且不是"改改维度"能绕过的。**

| | 我们目标 Flash-Next | DSpine 草稿（27B 版） | DSpine 草稿（8B 版） |
|---|---|---|---|
| 目标 | MoE，hidden **2560** | dense 27B，hidden **5120** | dense 8B，hidden **4096** |
| vocab | 248320 | 248320（同 tokenizer） | 151936 |
| 草稿结构 | 原生 MTP 1 层 | 5 层 dense + bridge | 5 层 dense + bridge |

1. **输入是目标模型自己的 embedding**：`DSpineDraftModel.forward` 强制要求 `input_embeds`（`models/dspine.py:473`），
   第一层投影就是 5120 维。我们的 hidden 是 2560，矩阵乘都做不了；硬加投影只会把分布弄坏。
2. **分布绑定**：草稿是逼近 27B 的条件分布训出来的；换目标后接受长度会崩，即使维度强行对上。
3. **引擎不支持**：DSpine 是 SGLang 0.5.22 的 DFlash 插件；我们引擎只有 `mtp.*` 单层权重格式与串行 step 图。

**能复用的是训练配方**（已写进方案）：block 16、code_size 1024、rank 512、5 层、feature layers [1..5]、
GT token 注入退火（1/3 零、峰值 0.5、保持 1/6）、top-16 选词表、SPS 调参方法。
若真要用它，唯一前提是把线上目标换成 Qwen3.8-27B——那是换模型项目，不是加草稿。

**出处**：`tencent/Qwen3.8-27B-DSpine-b16/config.json`、`tencent/Qwen3-8B-DSpine-b16/config.json`（hf-mirror）、
`sglang_dspine/models/dspine.py:473`、`Strata-data/packs/iq3_xxs/index.txt`。

## 2026-11-14 23:10（北京）基于现有 MTP 层训练草稿：深度是什么、能不能训

**问题**：能不能基于现在的预测层训练一个合理的 MTP 小模型？为什么原版小模型不支持"多层推理"？

**结论**：
- **"深度"= 草稿网络的层数**。串行 MTP 用**时间**传递前驱信息（每步把采样出的 token 喂回下一步）；
  DSpine 把 16 个位置**同时**算，位置之间的依赖只能靠**更深的下一层**注入（论文 "Condition Through Depth"）。
  1 层网络没有第二次机会，所以块并行的后几行会崩——这就是"不支持多层推理"的含义：
  我们的 checkpoint 只有 `mtp.layers.0.*`，引擎的权重表/step 图也只按单层实现。
- 现有 MTP 层**不能直接**当块并行草稿：它吃**目标 hidden + 上一 token**，块内第 2 行起没有目标 hidden；
  DSpine 草稿只吃 token embedding（anchor 用真 token、其余 mask）。所以"混合路线"要成立，第 0 层之后必须是
  DSpine 式（embedding + 块内因果）。
- 可以训：已实现 5 层 dense 草稿（`~/draft-train`），DSpine bridge、PCA codebook、GT 注入退火都在；
  共享冻结的目标 embedding/LM head（与腾讯 checkpoint 一致）。
  实测吞吐 2.4k supervised tok/s（4090，batch 8/ctx 256/4 blocks），40k 步 ≈ 2 小时。

**出处**：`~/draft-train/`（model.py / train.py / README.md）、`src/core/mtp.cpp`、
`models/dspine.py:388-511`、`docs/reports/mtp-dspine-port-plan-2026-11-14.md`。


## 2026-11-18 · 引擎并发改造技术文档与 iwiki 发布

**问题**：把 #5662（固定物理槽）+ #5726（并发多步 MTP）的代码改动、前后机制对比、实测效果、decode 速度随并发曲线写成完整技术文档，发到软工集市「技术报告」。

**结论 / 关键事实**
- 线上文档：iwiki `space-_jtMARIW`（技术报告）→ doc `page-kb5lZ4-H`《Strata 推理引擎并发改造技术报告：固定物理槽 + 并发多步 MTP》；本地源 `~/docs/reports/engine-concurrency-unified-slots-2026-11-18.md`，图 `figs/decode-curve-2026-11-18.png`；MinIO 备份 `docs/reports/2026-11-18/`。
- decode 口径（1000/ITL，不含 prefill）：受控 1/2/4 路 —— 旧 0.1.38 = 75.0/43.5/22.8，新 0.1.59+unified = 64.1/46.6/21.9；两卡合并 8 槽 = 125.5/68.9/38.8/18.0（1/2/4/8）；两条 lane 4/8 路 = 40.6/25.6；生产 14 天普道中位 = 95.2/41.2/28.4/21.3/13.4（并发 1/2/3/4/≥5），快道 = 79.6/30.8/24.0/17.2/3.6。
- 聚合口径（tok/s，全部请求）：旧 36.3/51.7/61.1 vs 新 38.6/70.8/70.6（1/2/4 路）；8 路两条 lane 141.6 vs 两卡合并 107.7。
- 发布方式：`upsert_markdown_doc`（space_id=space-_jtMARIW）；图片必须走 `upload_attachment`（base64 参数，会进上下文，先压到 ~15 KB 再传，返回 `absolute_url` 嵌进 Markdown）。
- 事故：2026-11-18 14:16（北京）普道引擎 `futex_do_wait` 卡死、GPU1 利用率 0、日志停在 06:16 UTC；13/14/15 点普道平均耗时 16 s → 230 s → 616 s；网关 `slow_load=318/4`（在途计数泄漏）后全量 traffic spill 到快道，单 lane 运行约 4 小时；18:20 经用户同意 `supervisorctl restart strata-b`，权重重载约 30 分钟（/shared 冷读 ~7–20 MB/s），恢复后需确认 `in_flight` 归零。

**出处**：`~/docs/reports/engine-concurrency-unified-slots-2026-11-18.md`、`~/engines/test/results/*.json`、`~/logs/infer.db`、`~/deploy/logs/strata-b.out.log`、iwiki `page-kb5lZ4-H`。


## 2026-11-18（补）· 技术报告重写：decode 速度 vs 并发实测曲线

**问题**：原报告太啰嗦、夹带“合并了什么分支”的过程叙述；缺一张多路并发实测 decode 速度折线图。要求按技术文档重写（改了什么 / 原理 / 效果），用真实观测数据。

**结论 / 做法**
- 定稿 iwiki：`page-qXlENFqJ`（技术报告）；本地 `~/docs/reports/engine-concurrency-report-final-2026-11-18.md`；图 `docs/reports/figs/decode-vs-concurrency-2026-11-18.png`（iwiki 附件 `4nag8S-…png`，可访问 URL 形如 `https://ssemarket.cn/iwiki-api/uploads/files/<id>-<name>.png`，**工具返回的 absolute_url 多一段 /api，会 404**）。
- 曲线数据（真实现网，`~/logs/infer.db` 14 天、输出 ≥50 token、按解码区间中点同 lane 活跃数分组取中位）：普道 1/2/3/4/5 路 = 95.2/41.1/28.4/21.3/15.8 tok/s（样本 3.38 万/3.80 万/3.59 万/2.18 万/5 024）；快道 = 79.2/30.8/24.4/16.3/10.6（样本 382/125/77/27/6）。受控新旧对照（唯一前缀、1200 token、每档 2 轮）：旧 75.0/43.5/22.8，新 64.1/46.6/21.9。
- 结论口径：单请求解码上限 79–95 tok/s 是等专家带宽的硬件上限；并发翻倍近似减半；改造收益在聚合吞吐（2 路 +37%）与 TTFT，而不在单请求解码曲线。
- 同日普道挂死事故：18:20 重启后权重重载约 45 分钟（/shared 冷读 7–70 MB/s）；期间用 `ionice -c2 -n7 dd` 顺序预热 47 GB 专家分片，引擎读取从 9 MB/s 提升到 ~96 MB/s；19:00 左右恢复，15 分钟内 131 条请求正常完成。

**出处**：`~/docs/reports/engine-concurrency-report-final-2026-11-18.md`、`~/engines/test/results/prod-decode-vs-conc-20261118.json`、`~/deploy/logs/strata-b.out.log`。
