# 当前版本快照（2026-10-24 18:00 北京时间）

> 一页纸回答「现在线上到底跑的是什么」。所有值都是当场从进程 / 配置 / `/v1/status` / `/health` /
> `/concurrency` / 引擎日志读出来的，不是计划值。取代 `CURRENT-VERSION-2026-10-21.md`（那份已过期：
> 当时两条 lane 同一份二进制，现在已分叉）。密钥一律不抄（profile 里有明文 key，权限 0600）。
> 本周改造过程见 `docs/周报/周报-2026-W43.md`；P0–P5 实验报告见 `docs/reports/P{0,2,3,4,5}-*-2026-10-22.md`。

---

## 1. 进程拓扑（supervisorctl + ps 实测）

| 角色 | 监听 | 状态 | 进程 / 入口 |
|---|---|---|---|
| 网关 `rag-proxy` | `0.0.0.0:8080`（唯一公网面，ai.ssemarket.cn） | RUNNING 3d4h | `Strata/.venv/bin/python rag-proxy/rag_proxy.py --config rag-proxy/config.json` |
| 快道 A `strata-a` | `127.0.0.1:8091`（GPU0，核 0-15） | RUNNING 2d12h（10-22 晨 P5 切换后） | `Strata/serve/server.py --config deploy/engines/profiles/ab-novarlen/strata-a.json --port 8091` |
| 普道 B `strata-b` | `127.0.0.1:8092`（GPU1，核 38-53） | RUNNING 3d4h | `Strata/serve/server.py --config deploy/engines/profiles/ssebed/strata-b.json --port 8092` |
| 引擎子进程 ×2 | 回环 | A：10-22 起；B：10-23 起（serve 层自动管理重启，原因未查） | 见 §2 二进制 |
| `strata-vision` ×2 | 子进程 | RUNNING | `Strata/engine/strata-vision`（每 lane 一个，`--max-tokens 1024`） |
| supervisor 程序 `strata`（单实例旧布局） | — | STOPPED（10-10 起，双 lane 布局生效） | `Strata/run-iq3_xxs.sh` |

模型（两条 lane 相同）：`qwen3.8-flash-next-iq3_xxs`（Qwen3.8-Flash-Next-GSQ-RCO，IQ3_XXS 量化）。

## 2. ⚠️ 两条 lane 已分叉（10-22 实验的结果，收敛待定）

| 项 | 快道 A（8091，实验道） | 普道 B（8092，对外主力） |
|---|---|---|
| 二进制 | `deploy/engines/bin/p2-rowcap-b7-20261022/strata`（sha `da5e3bba…`，44,725,296 B） | `deploy/engines/bin/mtpvarlen-20261021d/strata`（sha `4e5ce24d…`） |
| `/v1/status` 身份 | `legacy-0.1.38-sha.da5e3bba`，`build=null` | `ssebed-0.1.38-b5` |
| 身份说明 | **目录里没有 `BUILD.json`** → serve 回退成「sha 前 8 位」，名字显示 legacy 是误导；实际是 **P2 行容量改造版（kVerifyMaxT 拆成容量 32 + `--max-rows`）** | DeepInfer 式变长预算版（build 5） |
| `--max-context` | **524,288**（P5，10-22 实测 +3.10 GiB 锁页内存、无退化） | 262,144 |
| `--mtp-varlen` | **无**（profile 名 `ab-novarlen` = A/B 对照：A 关 varlen 跑新行容量，B 保持 varlen） | 有 |
| profile | `deploy/engines/profiles/ab-novarlen/strata-a.json` | `deploy/engines/profiles/ssebed/strata-b.json` |
| StepStats | `~/logs/step-stats-ab-prod.jsonl` | `~/logs/step-stats-b.jsonl`（已 436 MB） |

**共同参数**（除上表两项外逐字相同）：`--spec 4 --spec-min-p 0.5 --mtp ~/Strata-data/mtp/rt`、
`--kv int8 --kv-resident 32768`、`--batch 4 --batch-mtp --batch-groups 1`、
`--conversation-cache-slots 20 --conversation-cache-mib 8192`、`--vram-reserve-mib 1600`、
`--ple-io ram`、`--pool-workers 13`、`--vision`；两份 profile 都带 `fit_max_tokens: true`。

**注意**：网关窗口守卫仍是 `max_context: 262144` —— **对外有效窗口还是 256K**，
lane A 的 512K 目前只有直连 8091（排障 / 实验）能用。要不要放宽网关守卫到 512K 是一个待拍板项。

## 3. 网关能力与计数（`/health`，10-24 18:00）

- **RAG 关闭**（`rag_enabled=false`）：纯透传。计数 `requests 26,254 = no_rag 26,254`，`assembled 0`，`sessions []`，`degraded 272`。
- **模型分流**：`qwen3.8-flash` / `strata-b` → 8092（默认）；`qwen3.8-flash-fast` / `strata-a` → 8091。
- **上游 failover**：被路由的 lane 连不上时自动改投另一条（10-21 切 B 期间实测 19 次改投、`upstream_errors=0`）。
- **窗口守卫**：超窗本地结构化拒绝（不盲重试）；思考预算守卫；`retry_on_overfit`。
- **文档抽取**（`documents.enabled=true`）：PDF/DOCX/XLSX/XLS/CSV/文本自动识别抽取，扫描 PDF 渲染页面图，按 256K 剩余预算头尾截断（单文件 ≤20MB、抽取上限 120k token）。
- **OAuth**：接 ssemarket.cn 账号体系（回调 ai.ssemarket.cn）。
- **推理留档**（`infer_store`）：`stored 26,254 行 / 832 段`，累计段 2,181、已上传 MinIO 2,179（桶 `qwen-3.8-flash` 前缀 `infer`），本地缓存 4GB/7 天（已剪枝 178,413 行），`minio_ok=true`。

## 4. 运行时实况（10-24 18:00 当场读数）

| 项 | 值 |
|---|---|
| GPU0（lane A） | 23,729 / 24,564 MiB，util **100%**，49°C，172 W |
| GPU1（lane B） | 23,204 / 24,564 MiB，util **100%** |
| GPU2 | 16.6 GB 被别的 pod 占用，util 0%（非本组） |
| 容器内存 | `free -g`：**184 / 216 GiB 已用，剩 31 GiB**（悬顶） |
| 宿主压力 | cpu 0.40 / gpu 0.67 / gpu_mem 0.86 / mem 0.76（med） |
| lane A `/concurrency` live | generating（thinking 阶段），瞬时 82.2 tok/s；重启以来 118 请求、prompt 5.63M token、**复用 4.89M（86.8%）**、输出 86.8k |
| lane B 引擎日志（近期请求） | 停车缓存 parked=20 槽 / 5.37 GB、**累计 evictions 3,875**；专家命中率 78.7–86.0%；KV 流式 VRAM 命中 99.3–99.4%；MTP 草稿在工作（如 accepted 88/136、36/47） |

## 5. 观测面（现在能看到什么）

- 网关 `GET /health`、`GET /concurrency`（每在途请求 TTFT/速度/阶段、两 lane GPU/RAM）。
- 引擎 `GET /metrics`、`GET /v1/status`（需 API key；单路状态模型，`concurrency.serving=4`）。
- **逐窗口 StepStats**（`STRATA_STEP_STATS` 打开）：`slots/rows/drafts/accepted/step_ms/keeps/ctx` + prefill 行；两 lane 各一份 JSONL。
- 引擎日志行：`prompt N = R reused + F read`、专家命中率、KV 流式命中、停车/驱逐。
- 实验工具：`STRATA_PLAN_CHECK=1`（计划不变量）、`STRATA_SPLIT_TIMING=1`（等 GPU vs CPU 池分项计时）。
- 推理留档：`~/logs/infer.db` + `~/logs/infer-segs/`（全量请求元信息+内容，MinIO 有副本）。

**仍盲区**（改造输入）：`engine.waiting` 排队队列未导出；batch 模式 `/metrics.live.queued` 恒 0；
逐槽状态；`timings`/`cached_tokens` 共享快照竞态仍在（测量侧已改用客户端流式时间戳绕开）。

## 6. 已知问题 / 下次动手前记住

1. **lane 分叉**（§2）：A 是 P2 行容量版 + 512K、B 是 build 5 varlen + 256K。收敛方案（统一到哪版、何时切、如何回滚）待拍板；**切 B 必须先经用户同意**。
2. **A 道二进制缺 `BUILD.json`** → 版本名显示 `legacy-…` 是误导；下次构建务必走 `switch_engine.py dev build`（自动写 BUILD.json + build-number）。
3. **网关守卫 256K**：512K 对外不可用（§2 注）。
4. **冷启动 8–10 分钟**（40 GiB 专家 arena，0.07–0.14 GiB/s 取决于宿主页缓存）；重启前确认 `/health` 两条 upstream 都 ok（failover 兜底）。
5. **内存悬顶**：剩 31 GiB；lane B 停车驱逐持续发生（累计 3,875）。
6. **`--max-rows 16` 会静默关投机**（第 7 处「8 行」上限在 `native_bf16`，`n_tok>8` 抛异常→`chain_off`）——只能实验用，未修前不上线。
7. **`frontend.py` 对 `tools[*]["name"]` 无保护**：缺 `name` 的 Anthropic 请求在计数路径抛 KeyError（strata-b.err.log 27 次、10-08 后未复发；strata-a.err.log 6,785 次、均早于 10-19）。潜在缺陷，未修。
8. 旧缺陷仍在：并发下 `timings` 偶发缺失/错挂、批槽惩罚参数不生效（问题 #3）。

## 7. 回滚目标

- **lane A → build 5**：`switch_engine.py` 切回 `ssebed` profile（`mtpvarlen-20261021d`），分钟级；二进制存档 `archive/binaries/strata-ssebed-0.1.38-b5-mtpvarlen-20261021/`。
- **P2 行容量补丁**：`archive/patches/p2-row-capacity-2026-10-22/`（含 `p2-row-capacity.patch`、`p2-wide-fixes.patch`、测试）。
- **RAG 回滚**：`rag_enabled=true` + 重启网关（引擎不动）。
- 引擎工作副本：`~/SSEStraBed`（权威源码 + `deploy/switch_engine.py` 工具链）；`~/SSEStraBed-engine` 是 10-21 起 P0–P5 用的干净副本（`rsync` 自 SSEStraBed，见 `docs/plans/HANDOFF-PROMPT-FULL-ENGINE-2026-10-21.md`）。
