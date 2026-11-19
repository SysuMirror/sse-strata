# 当前版本快照（2026-10-21 13:55 北京时间）

> 一页纸回答「现在线上到底跑的是什么」。所有值都是当场从进程/配置/`/metrics` 读出来的，
> 不是计划值。密钥一律不抄（配置文件里是明文 key，权限 0600）。
> 相关文档：`docs/reports/DEEPINFER-VARLEN-2026-10-21.md`（改造与实验）、
> `archive/patches/mtp-varlen-2026-10-21/`（源码存档）。

---

## 1. 进程拓扑

| 角色 | 监听 | 状态 | 进程 / 入口 |
|---|---|---|---|
| 网关 `rag-proxy` | `0.0.0.0:8080`（唯一公网面，ai.ssemarket.cn） | RUNNING | `Strata/.venv/bin/python rag_proxy.py --config rag-proxy/config.json` |
| 快道 A `strata-a` | `127.0.0.1:8091`（GPU0） | RUNNING | `Strata/serve/server.py --config deploy/engines/profiles/ssebed/strata-a.json --port 8091` |
| 普道 B `strata-b` | `127.0.0.1:8092`（GPU1） | RUNNING | 同上，`…/strata-b.json --port 8092`；由 `active-b.env` 分发（10-21 起不再走 legacy 回退） |

模型：`qwen3.8-flash-next-iq3_xxs`（IQ3_XXS，3 × RTX 4090D）。

## 2. 引擎版本（两条 lane **同一份二进制**）

| 项 | 值 |
|---|---|
| 路径 | `/home/cloud/deploy/engines/bin/mtpvarlen-20261021d/strata` |
| 身份 | `ssebed 0.1.38 build 5`（`/metrics.engine` 与 `/health` 同源） |
| sha256 | `4e5ce24dac34a8dcc9c5adb34eda03c87d46ae75d7654a8794a7f3710bfbdea6` |
| 大小 / 构建时间 | 44 702 760 B / 2026-10-21 04:39:15 |
| BUILD.json | 二进制同目录；`source: local + --mtp-varlen + STEP_STATS + draft-depth ramp` |
| 存档 | `archive/binaries/strata-ssebed-0.1.38-b5-mtpvarlen-20261021/`、`archive/patches/mtp-varlen-2026-10-21/` |
| 回滚目标 | `~/SSEStraBed/build/strata` = `ssebed 0.1.38 build 1`，sha `3d3f3c9d…` |

构建谱系：build 1（10-18 生产基线）→ build 3（L 自由变化，**失败**，c=2 −10%）→
build 4（按 L_max 造草稿，**失败**，p0 0.14）→ **build 5（深度斜坡，现网）**。

## 3. 两条 lane 的引擎参数（`args` 完全相同）

```
--pack ~/Strata-data/packs/iq3_xxs
--native …/IQ3_XXS/…-00001-of-00002.gguf   --ple-gguf …-00002-of-00002.gguf
--expert-profile ~/Strata/data/expert-profile.bin   --expert-cache auto   --prefill auto
--spec 4  --spec-min-p 0.5  --mtp ~/Strata-data/mtp/rt
--max-context 262144  --kv int8  --kv-resident 32768  --vision
--conversation-cache-slots 20  --conversation-cache-mib 8192
--vram-reserve-mib 1600  --ple-io ram  --pool-workers 13
--batch 4  --batch-mtp  --batch-groups 1
--mtp-varlen                       ← 10-21 新增（自适应验证窗口）
```

运行时快照（`/metrics.engine`，两 lane 一致）：`max_context 262144`、`expert_slots 6427`
（`expert_cache_mib 10651`）、`arena_mib 40925`、`vram_free_mib 1183`、`kv int8 / resident 32768`、
`pool_workers 13`、停车槽 `20 / 8192 MiB`。

环境变量：A `STRATA_STEP_STATS=/home/cloud/logs/step-stats-a.jsonl`，
B `…/step-stats-b.jsonl`（每窗口/每次 prefill 一行 JSONL）。

## 4. 这一版新增了什么（只在 `--mtp-varlen` 后面）

1. **自适应验证窗口**：`plan_uniform(rates, S, L_max, cost)` 按逐位置接受率与在线实测
   `(rows → step_ms)` 曲线选 L；硬约束 `L ≤ 上轮草稿深度 + 1`（`mtp.cpp` 每轮只造 L 个草稿，其余清零）。
2. **每窗口统计** `StepStatsWriter`（`STRATA_STEP_STATS`），字段
   `slots/rows/drafts/accepted/step_ms/keeps/ctx` 与 `prefill(tokens/reused/read/step_ms/tok_s)`。
3. **网关上游 failover**（`rag-proxy`，与引擎无关）：被路由到的 lane 连不上时，
   自动改投 `upstreams` 里另一条活着的 lane 重试一次（`upstream_failover` 计数）。
   10-21 切 B 期间实测 19 次改投、`upstream_errors = 0`，网站没有出现 502。

**没有变的部分**（避免误读版本差异）：prefill 路径、KV/专家缓存策略、RAG 仍是关闭
（`rag_enabled=false`，有效窗口 262,144）、窗口守卫、前端。

## 5. 实测水平（同日、同协议）

| 指标 | 改造前 build 1 | 现网 build 5 |
|---|---|---|
| decode c=1 / c=2 / c=4（tok/s/请求） | 178.0 / 53.7 / 28.6 | 180.2 / 55.4 / 30.6（持平，±5% 内） |
| 冷 prefill TTFT 8k / 32k / 128k | 2.81 / 9.21 / 37.47 s | 2.55 / 9.15 / 36.94 s（持平） |
| 每窗口统计 | 无 | 有（A 10 040 行、B 1 390 行，10-21 13:53） |

**普道切完后的冒烟**（13:55–14:10，32 token 探针）：空闲单流 **115 tok/s**、TTFT 0.30 s；
若与真实用户会话并发，每请求掉到 **15–19 tok/s**、SSE 间隔 ~55 ms —— 原因是引擎转到了
**batch 路径（不做 MTP，`step-stats kind:"batch", drafts:0`，一步 ~36 ms）**，
这是改造前就有的「批槽无 MTP」，不是 build 5 的回退。快道同时段空闲单流 78 tok/s、间隔 0.2 ms。

## 6. 已知问题 / 下一次重启前要记住

- **冷启动 8–10 分钟**（专家 arena 40 GiB，实测 0.07–0.14 GiB/s，取决于宿主页缓存）。
  重启前先确认网关 failover 正常（`/health` 两条 upstream 都是 ok）。
- 代价曲线仍是 **一维 rows**：`rows=3` 中位 38 ms > `rows=4` 19 ms，非单调；
  下一步应按 `(S, rows)` 分桶。
- `plan_varlen`（每个槽不同 L）没有启用：需要 ragged verify kernel。
- 旧缺陷仍在：并发下 `timings` 偶然缺失/错挂、批槽惩罚参数不生效（问题 #3）。
- `Strata/serve/frontend.py:385` 对 `tools[*]["name"]` 无保护，缺 `name` 的请求会 KeyError
  （历史日志 6785 次，均早于 10-19；当前未复发）。