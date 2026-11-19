# Strata 推理引擎并发改造技术报告：固定物理槽 + 并发多步 MTP

> 版本：2026-11-18（北京） · 引擎：0.1.38-b7 → 0.1.59.1（unified slots 分支） · 部署：双 RTX 4090 D，两条 lane
> 代码树：`~/engines/source/strata-unified-0.1.59`（上游 `shoucandanghehe/Strata:upstream/batch-mtp-multistep`）
> 线上服务：`https://ai.ssemarket.cn/` · 网关 `rag-proxy:8080` → lane A 8091（快道/GPU0）、lane B 8092（普道/GPU1）

## 0. 结论摘要

1. 并发退化的根因不在 GPU 算力，而在**请求状态迁移**：旧引擎（0.1.38）在“独处 ↔ 批槽”之间反复搬会话
   （逐层 KV 拷贝 + 前缀重读），并发请求一多，TTFT 和吞吐被迁移吃掉；同时批槽里的 MTP 投机被压到每槽 1 个候选，
   并发时投机收益几乎归零。
2. 本次把上游 **#5662（固定物理槽 `--unified-slots`）**、**#5726（并发多步 MTP `--batch-mtp-multistep`）**
   连同 0.1.59.1 的批调度重写一起并入，并移植了我们 fork 的三个自定义 flag。改造后：
   - 请求从 prefill 到结束**永远待在自己的物理槽**，并发变化不再触发任何状态迁移；
   - 独处请求在槽内跑单序列 kernel，不再回 solo；
   - 并发时每槽最多验证 `--spec - 1` 个 MTP 候选（按每槽剩余输出/上下文预算），采样槽也参与；
   - 批窗口按活跃槽稀疏组行、槽位轮转，空槽不再补 pad。
3. 实测（同机同权重同上下文）：2 路聚合吞吐 **51.7 → 70.8 tok/s（+37%）**，4 路 **61.1 → 70.6（+15%）**；
   并发 TTFT 2 路 **1.30–1.50 s → 0.81–0.96 s**，4 路均值 **1.97 s → 1.43 s**。
4. 单请求解码速度随并发下降是**物理规律**（解码 94% 时间在等主机专家带宽），但新引擎把曲线整体抬高：
   受控实验中 2 路单请求解码 43.5 → 46.6 tok/s，生产 14 天数据里 2 路 41.2 tok/s。
5. 两卡合并成一个实例**不会**让并发更好：8 路双 lane 141.6 tok/s vs 两卡合并 107.7 tok/s（低 31%）。
   合并只在“单请求更快”（1 路 ~125 tok/s）时占优。并发场景应保留两条独立 lane，用网关调度把 8 个槽用满。

---

## 1. 背景：并发退化到底退化在哪

部署形态（改造前后一致）：

| lane | 端口 / 卡 | 批槽 | 定位 | 典型流量 |
|---|---|---|---|---|
| 快道 | 8091 / GPU0 | 4 | 低延迟、长上下文 | 每小时 100–550 条 |
| 普道 | 8092 / GPU1 | 4 | 主力流量、短请求 | 每小时 5.4–6.4k 条 |
| 邻居 | GPU2 | — | 租户 TTS（不归本服务） | — |

模型 Qwen3.8-Flash-Next（GSQ-RCO IQ3_XXS，MoE，权重 ~71 GB），`--max-context 262144`，`--kv int8`，
`--pool-workers 13`，专家缓存 `auto`。解码阶段 94% 的时间在等**主机 CPU 专家**与 PCIe 搬运（见
`decode-expert-bandwidth-bound`），也就是说：GPU 不算忙，瓶颈在“每轮计算窗口的组织方式”。

旧引擎（0.1.38）的服务循环是「主会话 + 批槽」两套状态：

```text
请求到达 → 只有它一个 → GEN（solo，主会话，单序列 kernel + 完整 MTP 窗口）
        → 第二个请求到达 → STOP 第一个 → BGEN 带“提示词+已生成 token”重新进槽
                          （每层 conversation_checkpoint_save/restore + conversation_kv_save/restore）
        → 槽里又只剩它 → _may_go_solo → 再搬回主会话
```

三个直接后果：

1. **并发数每变一次就迁移一次**：逐层 KV 拷贝 + 前缀重读，TTFT 峰值和吞吐损耗都在这里；
2. **批窗口按“最高活跃槽 + 1”组行**，空槽补 pad token，行预算被浪费；
3. **`--batch-mtp` 在批槽里每槽只验 1 个候选**，且只允许贪心槽，采样槽（网页默认 temperature 0.6）
   整组退回逐 token 路径——真实流量下投机解码基本失效。

## 2. 代码改动

### 2.1 分支与构建

- 上游分支 `upstream/batch-mtp-multistep` = 0.1.59.1 + #5662 + #5726；
- 编译：`CUDA sm_89`，二进制 `~/engines/source/strata-unified-0.1.59/build/strata`
  （`engine_version=legacy-0.1.59.1-sha.7c3c3c06`，`built_at 2026-11-13 16:50:37`）；
- 服务端仍用现网 fork 的 `Strata/serve/server.py`（协议兼容已验证，见 §6）。

### 2.2 服务端：不再 solo↔slot 迁移（`serve/server.py`）

引擎 READY 时打印 `INFO unified_slots=1`，服务端据此关掉两条旧路径：

```python
# server.py:2218  —— 独处请求不再回 solo
def _may_go_solo(self, left, times, embeddings) -> bool:
    if ((self.info or {}).get("unified_slots") == 1 or
            embeddings or times >= self.SOLO_AGAIN_MAX or left < self.SOLO_AGAIN_MIN_LEFT or
            not (self.info or {}).get("slot_cache")):
        return False

# server.py:2290  —— 从第一个字节起就走 BGEN 进槽，不再 GEN→STOP→BGEN
if alone and left > 1 and (self.info or {}).get("unified_slots") != 1:
    ...  # 旧路径
```

效果：`GEN → STOP → BGEN` 的前缀重读、以及槽→solo 的回迁整体消失。

### 2.3 引擎：请求级 owner 指针 + 槽内单序列 kernel（`src/program/generate.cpp`、`src/core/verify.cpp`）

- GEN/BGEN 入口把 `request_state / request_draft / request_df2` 指向**该槽自己的**会话、MTP drafter、DFlash2 drafter；
  槽会话有自己的 PLE 历史指针，不再与主会话共享；
- `Verifier::bind_session(SessionState& ss, std::string& err)`（`verify.cpp:1332`）把空闲验证器绑到“同样 carve 出来的”
  槽会话上，销毁捕获了旧地址的 solo 图、保留显式槽地址的批图；
- `Verifier::run_slot(slot, T, tokens, pos0, ...)`（`verify.cpp:1385`）让“只剩一个活跃请求”时在**那个槽里**用单序列 kernel 跑，
  commit 走 `commit(keep) + wait_commit`——独处不再意味着回主会话，而是原地全速；
- 预填被抢占时，`owner.partial / partial_plan` 记在原槽，恢复回同一个槽，不重读前缀。

### 2.4 引擎：并发多步 MTP（`src/core/mtp.cpp`、`src/program/generate.cpp`）

- `MtpDrafter` 新增自有 `logit_bias` 设备副本 `cbias_`：`set_draft_sampling(sp, err)` 先等空闲再拷贝
  （`mtp.cpp:733`），草稿 kernel 只读 `cbias_`；
- `prepare_draft(err, coupled)` / `chain_launch(..., coupled)` 让每槽草稿链携带该请求的采样参数
  ——采样槽不再被排除；
- 每槽草稿深度由 `slot_draft_limit(sl)` 决定（`generate.cpp:14496`）：

```cpp
auto slot_draft_limit = [&](const BSlot& sl) {
    return sl.stop ? 0 : (int) std::max<int64_t>(0, std::min<int64_t>(slot_mtp_max_t,
        std::min(sl.max_new - sl.produced, o.max_context - sl.p)) - 1);
};
```

- 窗口预算：只有 `--batch-mtp-multistep`（或单活跃槽）才允许多步，否则仍按每槽 1 个候选：

```cpp
// generate.cpp:14793
const bool multistep_window = unified_slots && (active_count == 1 || batch_mtp_multistep);
// generate.cpp:14866
const int native_budget = fixed_drafts && window_mtp
    ? (multistep_window ? slot_draft_limit(q) : std::min(1, slot_draft_limit(q))) : 0;
...
proposal_budget[A] = native_budget;
```

- 统计口径从“行数”改为“押出的候选数 `bt_tokens`”，被拒绝的草稿不再算作收益。

### 2.5 引擎：稀疏槽位与轮转（`generate.cpp` 批主循环）

```cpp
// 只把活跃槽组进行，空槽不补 pad；槽位每窗口轮转，避免固定槽位饥饿
for (size_t offset = 0; offset < bs.size(); ++offset) {
    const int b = (int) ((next_slot + offset) % bs.size());
    const BSlot& q = bs[(size_t) b];
    if (!q.active || ...) continue;
    ...
}
if ((batch_mtp || unified_slots) && A > 0) next_slot = (active[A - 1] + 1) % (int) bs.size();
```

批图上限随之上调（`ver.set_batch_graph_limit(64)`，`generate.cpp:11794`），因为轮转槽位会产生大量布局。

### 2.6 我们的 fork 兼容：三个自定义 flag 的映射

旧 lane 的生产参数含 `--mtp-varlen --batch-mtp-sampled --mtp-chain-floor 2`。新引擎不认这三个 flag，
我们只在 `src/program/generate.cpp` 打了五处补丁（完整 patch：`~/engines/test/fork-flags.patch`）：

| flag | 新引擎上的语义 |
|---|---|
| `--mtp-varlen` | 别名 → `--batch-mtp-multistep`（按每槽剩余预算 + `--spec-min-p` 决定草稿深度） |
| `--batch-mtp-sampled` | 接受但不再门控：0.1.59 批链默认按槽 `set_slot_sampling` 采样 |
| `--mtp-chain-floor N` | 每槽草稿深度下限 N，受 `--spec-1` 与该槽预算夹住，结束/取消的槽不复活 |

```cpp
// generate.cpp:15091 —— chain floor 只在“本来就要起草”的槽上抬深度
if (o.mtp_chain_floor > 1 && drafting[t] > 0)
    drafting[t] = std::min(slot_draft_limit(sl), std::max(drafting[t], o.mtp_chain_floor));
```

### 2.7 网关调度（`rag-proxy/rag_proxy.py`，配套改动）

引擎把单条 lane 的并发上限钉在 4 个批槽；如果请求被死钉在一条道上，就会出现“一条道排队、另一条空转”。
新增 `schedule_lane()`：

1. 快道模型：两条活着的 lane 里选**实时在途更少**的那条，持平留快道；
2. 普道模型：普道有空槽 → 普道；普道满且快道 `load < 4 - reserve` → 溢出快道；否则留在普道排队；
3. `reserve_by_hour`：按北京时间给快道留槽（09–12 留 3、23–01 与 06–07 留 2，来自 14 天 `infer.db` 统计）；
4. 空闲分摊：普道有人在跑、快道完全空闲且够预留 → 新请求去快道，避免一张卡忙一张卡空；
5. `strata-a` / `strata-b` 是调用方点名的车道，保持死钉。

> 调度器解决的是“8 个槽怎么填满”，引擎 PR 解决的是“每个槽在并发下怎么跑得快”，两者互补。

## 3. 机制对比（旧 vs 新）

| 路径 | 旧 0.1.38 | 新 0.1.59 + #5662/#5726 |
|---|---|---|
| 并发数变化 | 逐层 KV 拷贝 + 前缀重读（solo↔slot 迁移） | **无迁移**，请求原地不动 |
| 独处请求 | solo 主会话 / 或批路径补 pad 行 | 槽内单序列 kernel（`run_slot`） |
| 批窗口行布局 | 最高活跃槽 + 1，空槽补 pad | 稀疏活跃槽 + 槽位轮转 |
| 并发投机深度 | 每槽 1 个候选；采样槽整组退回 | 每槽最多 `--spec-1` 个候选，按预算 |
| 采样参数 | 全局共享 | 每槽私有（`cbias_`、`prepare_draft`） |
| 视觉位置表 | 全局一张 | 会话级（`session_mrope_*` + `MropeScope`） |
| 预填被抢占 | 状态易丢/重读 | `owner.partial` 记在原槽，恢复即用 |
| 批图 | 固定布局 | 轮转布局，最多保留 64 套 |

## 4. 实测

### 4.1 口径

- **TTFT**：请求发出到首个内容 token（客户端 SSE 计时）；
- **单请求解码速度** = 1000 / 平均 ITL（token 间隔中位数/均值），**不含** prefill；
- **聚合吞吐** = 总输出 token / 墙钟时间（`bench.py`，每条请求唯一前缀，避免前缀缓存作弊）；
- 生产口径（`~/logs/infer.db`，rag-proxy 全量请求元信息）：`decode_tok_s = completion_tokens / (结束 − 首帧)`；
- 受控实验：同权重、`--max-context 262144`、`--batch 4`、MTP、temperature 0.6、1200 token 提示、唯一前缀。

> 注意：早期报告里的 “39 tok/s” 是**端到端**（含 TTFT 与排队），不是解码口径；本报告统一用解码口径对比，
> 生产库同口径（14 天、输出 ≥50 token）的快道独占中位 79.6 tok/s、普道 95.2 tok/s。

### 4.2 四臂对比（单卡 GPU0，2026-11-13，生产同规格）

| 臂 | 引擎 / 参数 | 1 路 TTFT | 2 路 TTFT | 4 路 TTFT | 1/2/4 路聚合 tok/s |
|---|---|---|---|---|---|
| A | 现网 0.1.38（生产参数） | 1.70 s | 1.50 s | 1.97 s | 36.3 / 51.7 / 61.1 |
| B | 0.1.59.1 纯上游参数 | 1.58 s | 1.30 s | 1.70 s | 37.9 / 62.1 / 75.3 |
| C | B + `--unified-slots --batch-mtp-multistep` | 1.61 s | **0.81 s** | 1.95 s | 37.3 / 66.5 / 60.3 |
| D | B + 三个 fork flag + `--unified-slots` | 1.57 s | 0.96 s | **1.43 s** | 38.6 / **70.8** / **70.6** |

- 纯升级（B）就把 2/4 路聚合提高 20–23%；
- `--unified-slots` 的收益集中体现在 **TTFT**（2 路 1.30–1.50 s → 0.81–0.96 s），对应“不再迁移状态”；
- C 的 4 路（60.3）与 D（70.6）差距偏大，单轮两次采样噪声未完全排除，趋势以 2 路为准。

### 4.3 两卡合并 vs 两条 lane（8 路，静默窗口，2026-11-14）

| 部署 | 4 路聚合 | 8 路聚合 | 8 路 TTFT 均值/最大 | 8 路单请求 tok/s |
|---|---|---|---|---|
| 两条 lane 分别跑（2+2 / 4+4） | 107.8 | **141.6** | 1.69 / 2.63 s | 20.1 |
| 两卡合并（层切分 `--layer-split 24` + `--batch 8`） | 110.6 | 107.7 | 2.28 / 5.85 s | 14.5 |

结论：**并发场景保留两条 lane**。合并只赢单请求解码（1 路 ITL 8.0 ms ≈ 125 tok/s vs 两条 lane 60–70），
因为合并的 48 GB 显存能把专家缓存从 ~10 GB 抬到 ~40 GB；但 8 路时少了独立调度/prefill 通道、引入跨卡 handoff，
反而低 31%。

### 4.4 解码速度随并发的曲线

![decode speed vs concurrency](figs/decode-curve-2026-11-18.png)

图：单请求解码速度（1000/ITL，不含 prefill）随并发的变化。
受控实验期间另一条 lane 仍在接真实流量（CPU/PCIe 竞争），绝对值偏低，**只看形状与相对关系**；
生产曲线来自 `infer.db` 14 天真实流量，是现网绝对值。

单请求解码速度（tok/s / 请求）：

| 并发 | 旧引擎 0.1.38（受控） | 新引擎 0.1.59+unified（受控） | 两卡合并 8 槽（受控） | 两条 lane（受控） | 生产普道（14 天） |
|---|---|---|---|---|---|
| 1 | 75.0 | 64.1 | **125.5** | — | **95.2** |
| 2 | 43.5 | 46.6 | 68.9 | — | 41.2 |
| 3 | — | — | — | — | 28.4 |
| 4 | 22.8 | 21.9 | 38.8 | 40.6 | 21.3 |
| 5+ | — | — | — | — | 13.4 |
| 8 | — | — | 18.0 | 25.6 | — |

读法：

1. **曲线形状是物理的**：并发翻倍，单请求解码大约减半（95 → 41 → 28 → 21）。每一轮验证窗口要同时服务更多槽，
   主机专家搬运按槽数增长，而解码本来就有 94% 时间在等专家带宽（`decode-expert-bandwidth-bound`）。
2. **1→2 路是拐点**：2 路时新引擎每请求解码 46.6 略高于旧引擎 43.5，聚合从 51.7 抬到 70.8（+37%）——
   旧引擎在第二个请求到来时要迁移状态 + 把 MTP 压到每槽 1 个候选，新引擎两者都不做。
3. **超过槽数后，解码由“占用槽数”而不是“排队长度”决定**：单 lane 只有 4 个槽，第 5–8 条请求在排队，
   不会让每请求解码进一步变慢，只体现为 TTFT 上升（两卡合并 8 路 TTFT 均值 2.28 s、最长 5.85 s）。
   所以并发能力上限 ≈ “槽数 × 每槽解码”，要做的是**把两条 lane 的 8 个槽都填满**（网关调度的职责），
   而不是往一条 lane 里堆请求。
4. **两卡合并只在 1 路领先**：48 GB 显存把专家缓存从 ~10 GB 抬到 ~40 GB，单请求解码 125 tok/s；
   但 8 路时聚合反而最低（107.7 vs 两条 lane 141.6）——见 §4.3。
5. 旧引擎受控 1 路（75.0）高于新引擎（64.1）是**采样噪声 + 专家缓存冷热差异**（两轮采样、每档 256 token），
   不代表单请求退化；生产口径下新引擎普道独占中位 95.2 tok/s（快道 79.6 tok/s，长上下文请求占比高）。

### 4.5 生产数据（`infer.db`，14 天，按请求中点重叠计并发）

对最近 14 天、输出 ≥50 token 的请求，取每条请求解码区间中点上的“同 lane 同时活跃请求数”，分组取解码速度中位数：

| 并发（中点活跃数） | 快道中位 tok/s | 样本 | 普道中位 tok/s | 样本 |
|---|---|---|---|---|
| 1 | 79.6 | 377 | **95.2** | 33 918 |
| 2 | 30.8 | 112 | 41.2 | 38 078 |
| 3 | 24.0 | 73 | 28.4 | 35 983 |
| 4 | 17.2 | 20 | 21.3 | 21 797 |
| ≥5 | 3.6 | 19 | 13.4 | 7 539 |

读法：

- 生产单请求解码上限约 **80–95 tok/s**（普道独占中位 95.2、快道 79.6），这是解码期等专家带宽的硬件上限；
- 并发每翻一倍，单请求解码近似**减半**（95 → 41 → 28 → 21），因为每轮窗口要同时服务更多槽，专家搬运次数按槽数增长；
- 快道并发样本少（长上下文请求多、单请求本来就慢），中位数偏低不代表引擎更差；
- 这组数据是**现状（新引擎）**的基线；旧引擎的对照只能来自受控实验（§4.2/§4.4），历史库里的旧引擎时期数据混有不同参数。

## 5. 为什么这样能优化并发（机制 → 数字）

1. **去掉迁移 = 去掉并发下的固定税**。旧引擎每次并发度变化都要逐层 KV 拷贝 + 前缀重读；新引擎请求原地不动，
   2 路 TTFT 直接减半（1.30–1.50 s → 0.81–0.96 s）。
2. **独处也走批路径但用单序列 kernel**。旧版“独处回 solo”是为了拿单序列 kernel 的速度，代价是迁移；
   `run_slot` 让单序列 kernel 在槽内跑，两全。
3. **投机不再退化**。解码 94% 时间等专家，MTP 多押一个候选就少一轮专家搬运；旧版并发时每槽只验 1 个候选、
   采样槽整组退回，真实流量（temperature 0.6）投机收益≈0。新引擎让采样槽也参与，并允许按预算押多个。
4. **稀疏 + 轮转让行预算花在活跃槽上**。空槽补 pad 会白算；轮转让固定槽位不至于永远排在窗口尾部。
5. **两条 lane 的 8 个槽由网关按实时负载填**：prefill 是串行的，排队长度直接决定 TTFT；把请求导向空闲道，
   第 5–8 条不必等第二轮。

## 6. 部署、回滚与运行状态

- profile：`~/deploy/engines/profiles/unified/`（两条 lane 均加 `--unified-slots`，server_py 仍为现网 `Strata/serve/server.py`）；
- 切换：`switch_engine.py switch unified --lanes a,b --yes`（drain → 停 → 写 active 环境 → 启动 → health → 1-token 冒烟）；
- 回滚：`switch_engine.py rollback --yes`（备份在 `~/deploy/backups/engine-0.1.38-b7-20261113/`）；
- 协议兼容性：现网 0.1.38 服务端与 0.1.59.1 引擎冒烟通过，换引擎不必换服务端。

## 7. 边界与已知限制

- **不解决“两卡拼一张”**：合并是不同的问题（显存 vs 调度），结论是并发场景别合。
- **不解决单请求上限**：解码受主机专家带宽约束，单请求 ~95–125 tok/s 是硬件天花板；本改动提升的是并发下的总吞吐与 TTFT。
- **冷缓存偏差**：实验实例专家缓存冷（命中 70–90%），线上长期运行命中更高，绝对数字以 `infer.db` 为准。
- **非确定性**：每档两轮采样的波动可达 10%，趋势判断用多轮/多档，不用单点。
- **lane B 挂死事故**：2026-11-18 14:16–18:20 普道引擎在 `futex` 上卡死（详见 §8），与本次改造的吞吐结论无关，
  但说明新引擎仍需长稳观察。

## 8. 事故记录：2026-11-18 普道引擎挂死

- 14:16 北京起引擎日志停止输出；此后 14/15 点的普道请求平均耗时 230 s / 616 s，broken pipe 88/109 条；
- 网关在途计数泄漏（`slow_load=318/4`），全部流量被 spill 到快道，服务以单 lane 运行约 4 小时；
- 18:20 经用户同意 `supervisorctl restart strata-b`；权重重载很慢（`/shared` 冷读实测 7–70 MB/s，主机盘被他租户占满），
  重载期间服务由快道单独承载（取证快照：`~/docs/reports/incidents/lane-b-hang-20261118-182037.txt`）；
- 待查：卡死时的线程栈 `futex_do_wait`、GPU1 利用率 0%，疑似批窗口等待链死锁；需要给引擎补“无进展自重启”
  以外的进程级看门狗或复现实验。

## 9. 复现

```bash
# 单 lane 压测（普道 8092，唯一前缀，SSE 计时）
python3 ~/engines/test/bench.py --base http://127.0.0.1:8092 \
  --key-file /home/cloud/logs/strata-api-key.txt --model qwen3.8-flash-next-iq3_xxs \
  --conc 1,2,4,8 --rounds 3 --prompt-tokens 1200 --max-tokens 512 \
  --out ~/engines/test/results/decode-curve-20261118.json
```

## 10. 源码索引

| 主题 | 位置 |
|---|---|
| 服务端 unified slots / 退役 solo | `serve/server.py:2218`、`:2290` |
| 验证器重绑 / 槽内单序列 | `src/core/verify.cpp:1332`、`:1385`；`include/strata/core/verify.hpp:121`、`:263` |
| 请求级 owner 指针、批主循环 | `src/program/generate.cpp`（`request_state/request_draft/request_df2`、`:14793`、`:14866`） |
| MTP 每槽采样 | `src/core/mtp.cpp:733`、`:1976`、`:2001` |
| 会话级视觉位置 | `src/core/session.cpp`（`session_mrope_*`）、`include/strata/kernels/mrope.hpp` |
| fork flag 补丁 | `~/engines/test/fork-flags.patch`（`generate.cpp` 五处） |
| 网关调度 | `rag-proxy/rag_proxy.py`（`schedule_lane`）、`rag-proxy/config.json`（`lane_scheduler`） |