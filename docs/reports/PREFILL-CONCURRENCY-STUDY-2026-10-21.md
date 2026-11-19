# 并发阶段的 prefill 串行：现状定位、参考方案与落地路线

> 提问（2026-10-21）：「我们的瓶颈是在并发阶段，prefill 仍然是串行的，这是一个瓶颈。请你在这方面去研究，
> 看看有没有更好的解决方案，能够获得并发方案的参考。」
> 本文 = 本机实测与源码定位（事实层）+ 主流推理系统怎么做（参考层）+ 我们能落地的排序（决策层）。
> 所有改动都需要重启引擎 = 需要用户同意；本文不含任何已执行变更。

---

## 0. 结论先行

1. **串行不在一个地方，而在三处叠加**：serve 层一把「一次只准一个请求读提示词」的锁、引擎里单条 prefill 流水线、
   以及 prefill 与 decode 共享的同一条 GPU 时间线。只解其中一处，TTFT 都不会质变。
2. **参考方案已经收敛成两种范式**：
   - **调度范式（Sarathi-Serve / vLLM / SGLang / DeepInfer）**：把 prefill 切成块，按**全局 token 预算**把
     「若干 prefill token + 所有在途 decode」放进同一个迭代，decode 永不停顿；同一 lane 内多请求公平轮转。
     论文自报容量 +2.6×～+5.6×（相对 vLLM，SLO 口径）。
   - **架构范式（DistServe / Mooncake）**：prefill 与 decode 拆到不同 GPU，KV 通过高速互联搬运，
     按阶段各自扩缩容；论文自报 7.4× 请求数（或 12.6× 更紧的 SLO）。
3. **我们的形状决定了先做哪种**：单机 2 lane、无 NVLink、KV 不可廉价搬运、解码已经受专家搬运带宽限制。
   → **调度范式可搬，架构范式暂不可搬**。但调度范式的收益在我们这里不来自「算得更快」，而来自
   **① 解码不再被长 prefill 冻结 ② 短提示词不再排在大提示词后面 ③ 多请求 prefill 共享权重/专家读取**。
4. **最优先的动作不是改引擎，而是把队列与阶段变成可观测的**（现在连「谁在排队、排了多久」都没有），
   再配合**网关侧准入/分流**（秒级生效、不动引擎）先止住「8 路长文一起打进来」的最坏情况。
5. 引擎侧的推荐形态：**「prefill 是每 N 个 decode 步插一次的步，而不是一段独占的时间」**——
   DeepInfer 的 `ALL/ANY + 水位 + sum_seqlen_k 预算` 已经是开源可抄的完整实现（`scheduler.hpp`），
   比我们自己 `#prefill-yield` 的「读 2 块、让 12 个窗口」更接近标准答案。

---

## 1. 事实层：串行到底在哪里、代价多大

### 1.1 三处串行点

| # | 层 | 位置（2026-10-21 在 `SSEStraBed` 上复核） | 语义 | 后果 |
|---|---|---|---|---|
| ① | serve（Python） | `Strata/serve/server.py:657-660` 注释 + `:670` 取 `self.ctl`、`:735-747` 释放 | **控制线一次只取一个请求**：`ctl` 从 `BGEN` 持有到 `BADM`，即 prompt 读取（prefill）严格一次一个；`BADM` 之后才把 decode 交给 `slot_q`/`BT` 并发推进 | 新请求的 prefill 只能排在当前 prefill 之后；真实准入队列 `engine.waiting`（`:411/668`）**未导出**，`/metrics.live.queued` 在 `--batch` 模式下恒为 0 |
| ①' | serve（对照） | `Service.fifo`（`:1268`、`:1825`） | 只有 `--batch 0` 时生效 | **生产 `--batch 4`，此路径不生效**——书稿 09 章把它当主串行点，需更正 |
| ② | 引擎主循环 | `generate.cpp:6075` 的单线程 `for (;;)`；`6831/6837` 同步调用 `sp.run(...)` | prefill 在**服务线程里同步读完**，返回前循环不会回到 6076 跑 decode 窗口 | 多请求的 prompt 只能一个接一个读；`in_lines` 是无界 FIFO（`:5418/5453/5460`），没有 prefill 队列或并发池 |
| ③ | 缓存槽互斥（**仅多 stage**） | `generate.cpp:6666` `lend()` / `6640` `refill()`；借出上限 90%/85%（`4415-4420`） | 多 stage（`--layer-split`）时 prefill 把最多 90% 的专家槽借给 prompt 缓冲，`host_res` 清成 `kNotResident`（`6697-6703`），`refill` 约 300 ms | **生产两条 lane 是单 stage**（profile 无 `--layer-split`，`pf_parts[0].first=-1`，`4579`），`lend()` 对 `first<0` 跳过 → **不借槽、无 refill 成本**；单卡 prefill 用自己的 scratch |
| ④ | 让出粒度 | `generate.cpp:6781-6801` `prompt_slot_steps`，由 `6834` 在段边界调用；段 = `yield_chunks × prefill_chunk`（默认 2×8192=**16384** token），每段后跑 `yield_steps`=12 个窗口 | prefill 与 decode 靠**段边界让出**，不是每步交错 | 段内不可抢占；最长断流 3.86 s；段内 decode 完全冻结 |

> 引擎事实来自本轮源码复核（`src/program/generate.cpp` 8177 行、`src/prefill/prefill.cpp`），
> 与书稿 09 章的行号存在漂移，以本节为准。**prefill 不使用 CPU 专家池**：它走 `prefill.cpp:1971-2034`
> 的 `fused::experts_native/experts`，驻留专家直接 `cache->device_slot(...)`，未驻留的经 staging ring DMA
> （`1520-1526`、`1571/1580` 统计 `experts_dma/experts_streamed`）；`drive_pool` 只挂在 decode/verify（`generate.cpp:687-769`）。

### 1.2 代价的量化

- 单卡 prefill 吞吐：**~3,350 tok/s**（10-14 口径）；10-21 复测小提示词表现为
  **固定开销 ~0.9 s + tokens/3.6k**（B 道 298 token 读 1.04 s；7,979 token 读 2.23 s = 3,580 tok/s）。
- 于是 TTFT ≈ `0.4–0.9 s + tokens/3350 + 排队`。258k ≈ **82 s**，1M ≈ **5 分钟**（单请求）。
- 并发排队是乘法的：8 路 128k **165 s**、8 路 262k **220 s**、8 路 1M **4.7 分钟**（10-13 口径）。
- prefill 进行时，在途解码被饿到 **0.3–1.4 tok/s**（实验报告 §7.2；分段让出后最长断流 19.45 s → 3.86 s）。
- 复用是唯一的「把 82 s 变成 0.79 s」机制，但被停车缓存容量打穿：单条 254k 快照 **4.7 GB**，
  缓存 8 GiB / 20 槽，10-07 全天 **驱逐 2,508 次**；被驱逐的会话下一轮重读 ~238k。

### 1.3 为什么「再调大 batch」不解决

prefill 的计算是**权重/专家带宽**问题：42.9 GB 专家、3 张卡共享 PCIe、解码期 94% 时间在等专家（见
`WHY-NO-GAIN-2026-10-21.md`）。prefill 同样要吃专家，但它走的是 `prefill.cpp:1971-2034` 的 fused 专家路径 +
staging ring DMA，**不经过 CPU 专家池**（`drive_pool` 只在 decode/verify，`generate.cpp:687-769`）。
因此：
- prefill 与 decode 争的是 **PCIe/DMA 与专家缓存槽**，不是 CPU worker；
- `kVerifyMaxT=8` / MMVQ `ncols<=8` 只约束 **decode 的验证行数**，不是 prefill 批的物理上限
  （书稿 09 章 §9.5 的「prefill 撞同一组天花板」说法本轮未找到证据，待实验澄清）；
- **单 stage 不借槽**：`lend()` 只对 `first >= 0` 的 participant 生效（`6666/6687`），生产 profile 没有 `--layer-split`，
  `pf_parts[0].first = -1`（`4579`）→ prefill 用 `Prefill::init` 自己的 scratch。**之前「prefill 借 90% 槽」的结论只在多 stage 成立，已更正。**
- 那么单卡上 prefill 与 decode 的真正耦合是 **专家缓存的驱逐竞争**：prefill 每个 chunk 要流式载入大量专家，
  会把 decode 的 93% 热命中挤掉（expert cache 是全局 LRU）。这是并发化后 decode 掉速的主要风险，而不是槽失效。

### 1.4 服务层其余事实（决定方案边界）

- **没有并发上限**：`ThreadingHTTPServer` 每连接一线程（`server.py:46/2916/3101-3106`，proxy 同），
  没有 Semaphore / 线程池 / 有界队列；真正的并发上限是引擎 `--batch 4`。
- **路由按模型 ID 钉死**：`rag-proxy/config.json:68-84` —— `qwen3.8-flash`→8092（普道）、
  `qwen3.8-flash-fast`→8091（快道）、`default_upstream`=8092；只有都没命中才用 `sha1(key)` 会话粘性
  （`rag_proxy.py:189-213`）。**所以「长文自然落在普道」已经是现状，但普道自己的并发长文仍互相排队。**
- **超时**：serve 的引擎静默预算 300 s + `prompt_len/50`，每个 `PP` chunk 重置（`:74-81/819-822/850-858`），
  超时**直接杀引擎**；proxy 上游超时固定 600 s（`rag_proxy.py:1741`）。
- **会放大 prefill 的两条重试**：① 引擎 400 `overfit` → 压 `max_tokens` 后**整包重发一次**（`:1802-1817`），
  等于再 prefill 一遍；② `URLError` 且未写客户端字节 → 换 lane 重发一次（`:1834-1845`），
  **600 s 读超时也会落到这里**，最坏情况是把一个巨型 prefill 复制到另一条 lane。
- **图片编码与请求同 FIFO**（`server.py:1722-1729`），长图会额外占控制线。
- **观测缺口**：真实队列 `engine.waiting` 未导出；`/metrics.live.queued` 在 batch 模式下恒 0；
  停车缓存的 hit/miss/evict **完全没有计数**（只有 `reused` 与专家 `hits/lookups`）。

---

## 2. 参考层：主流系统怎么处理「prefill 与 decode 抢 GPU」

### 2.1 调度范式（同一 GPU 内交错，单卡即可受益）

| 系统 | 机制 | 自报收益 | 对我们的可搬性 |
|---|---|---|---|
| **Orca**（OSDI'22） | iteration-level / continuous batching | 相对静态批处理大幅提升 | 已具备（批槽） |
| **Sarathi-Serve**（OSDI'24） | chunked prefill + **stall-free batching**：每次迭代在固定 **token 预算**内混入 prefill 块，decode 从不停顿；uniform batch 减少 PP 气泡 | Mistral-7B 单 A100 **2.6×** 容量；Yi-34B 双 A100 **3.7×**；Falcon-180B PP **5.6×**（SLO 口径，相对 vLLM） | ★★★ 最贴我们的引擎结构；我们已有 chunk，缺「预算 + 交错」 |
| **vLLM V1** | 统一调度器：`max_num_batched_tokens` 预算内 chunked prefill 与 decode 同批；decode 优先；抢占用 recompute | 工程事实标准 | ★★★ 概念可直接照抄 |
| **SGLang** | RadixAttention 前缀树缓存 + **overlap scheduler**（CPU 调度与 GPU 计算重叠） | 前缀复用 6.4×（论文口径） | ★★ 前缀缓存我们已有，树形组织是升级方向 |
| **DeepSeek DeepInfer**（2026-10 开源） | prefill 是**每 N 个 decode 步插一次的事件**：`ALL` 点每 70 步（全员有活才做，防陪跑）、`ANY` 点每 100 步兜底；`GPU_CACHE_PREFILL_WATERMARK=0.9`；`prefill_max_sum_seqlen_k` 控制一条 prefill 步能吃多长；两轮 allreduce 防空步 | 内部生产系统，无公开端到端倍数 | ★★★ 开源、C++、且我们已在读它的代码 |

**DeepInfer 这一段的源码要点**（本地快照 `docs/references/deepinfer-2026-10-20/code/scheduler.hpp`）：
- `:299-320` 注释把两类点写得很清楚：ALL 点「全员都有 prefill 活才做」、ANY 点「给孤立 rank 兜底」；
  两类重合按 ANY 算；`prompt step 会把 decode_step 清零`，所以刚做完 prefill 的下一步仍在 ALL 点上。
- `:313` 触发条件同时看 **KV 水位** 与队列非空。
- `:351-448` `_schedule_prefill` 在一个 `prefill_sum_seqlen_k` 预算内**循环取多个 waiting 序列**
  （`prefill_sum_seqlen_k_remain -= seq->get_len()`），即**一次 prefill 步可以带多个请求**。
- `:534-537` chunked prefill 用 `chunked_rest_len` 记剩余，`tokens_chunked_prefill_remaining` 汇总预算。

### 2.2 架构范式（跨 GPU 分离，需要 KV 高速通道）

| 系统 | 机制 | 自报收益 | 我们的障碍 |
|---|---|---|---|
| **DistServe**（OSDI'24） | prefill / decode 放不同 GPU，按「goodput」分别分配资源；KV 走 NVLink/IB | **7.4×** 请求数或 **12.6×** 更紧 SLO | 两台 4090D 无 NVLink；238k 会话快照 4.7 GB，PCIe 上单次搬运 ~0.5–1 s 且与专家搬运抢带宽 |
| **Mooncake**（FAST'25） | KVCache 为中心的分离式架构 + 全局 KV 池（3FS/DRAM/SSD） | 模拟负载下 **525%** 更多请求（真实负载 +75%） | 没有 RDMA/3FS；我们的停车缓存只有 8 GiB，池化前提不存在 |

**判断**：PD 分离的前提是「KV 搬运远便宜于重算」。我们的重算是 3.35k tok/s（238k ≈ 71 s），
4.7 GB 走 PCIe 理论 0.5 s —— 数字上其实成立，但代价是：
① 每条 lane 都要跑完整模型与专家分页，拆开后 decode 侧显存/专家命中率不变、prefill 侧却闲一半；
② 引擎的 KV/GDN 状态、停车快照、检查点协议都绑在进程内，跨进程交接是**源码级重构**；
③ 两条 lane 现在就是两个独立实例，分离等于把 A 变成「纯 prefill 机」，**解码容量直接减半**。
→ 不作为本轮方案，列为「若将来上第三张卡/有 NVLink 再评估」。

---

## 3. 我们能做的方案（按代价/收益排序）

### 方案 0（零引擎改动，秒级生效）：网关侧准入与分流
- **现状**：`qwen3.8-flash` 已钉在 8092、`-fast` 钉在 8091（`config.json:68-84`），所以「长文落在普道」
  已经成立；但普道内部多个长文仍然**互相排队**，而且 A 道只在客户端显式选 fast 模型时才被用到。
- 可做：① 在 proxy 加**按提示词长度的准入**（同一 lane 最多 1 个 >100k 的 prefill 在跑，
  其余返回 429/排队并给出 `Retry-After`），避免「8 路长文一起打进来」把 TTFT 推到分钟级；
  ② 让 600 s 超时的 failover **不要复制巨型 prefill**（对 `prompt_tokens > 阈值` 的请求不跨 lane 重试）；
  ③ 把 `engine.waiting` 暴露出来（见方案 3）。
- 依据：DistServe 的 goodput 思路——**不是让所有人更快，而是别让一个长请求拖垮所有 SLO**。
- 代价：只改 `rag-proxy`（`supervisorctl restart rag-proxy`，秒级，无需动引擎）。

### 方案 1（配置级，先量化收益）：把让出粒度从 16384 降到 8192
- 现成旋钮：`STRATA_PREFILL_YIELD_CHUNKS`（`generate.cpp:6773-6776`，默认 2；段 = 2×8192 = 16384 token）。
  设为 **1** 就是每 8192 token 让出一次——**不动代码，只改 env 后重启引擎**。
- 账：chunk=8192 在 3.35k tok/s 下 ≈ **2.4 s**；单 stage 不借槽，所以让出没有 refill 成本，只是每段跑 12 个
  decode 窗口（~0.3 s，本来就是该发生的解码工作）→ **prefill 墙钟约 +12%，系统总工作量不变**。
  换来**最长断流从 ~4.9 s（实测 3.86 s）降到 ~2.4 s**。这是「prefill 延迟换 decode 流畅度」的权衡，
  适合先做一次 A/B 拿真实数（注意不要把墙钟变长误读成吞吐下降）。
- 局限：粒度下限就是 chunk（8192）。再往下就要在 chunk **内部**让出，而 chunk 内部是一次完整的层循环
  （`prefill.cpp:1651`），中途让出没有现成状态机——这正是方案 1b 的改造点。

### 方案 1b（小改代码）：把 prefill 放到工作线程，服务线程在等待时跑 decode
- 天然钩子已经存在：`sp.on_chunk`（`generate.cpp:5218`，每 chunk 回调）与 `sp.should_stop`
  （`prefill.cpp:1370-1371`，每 chunk 检查一次）。
- 形态：`sp.run` 在 worker 线程跑，服务线程在等待期间执行 `batch_step/pump`；每个 chunk 边界同步一次。
- **硬约束**（全部来自本轮源码复核）：
  ① prefill 与 decode 共用同一组 CUDA stream / `ss` 会话 / verify 状态，当前**完全依赖单线程串行**保证正确性
  （`generate.cpp:6075/6916`、`prefill.cpp:1651`），改并发必须显式加同步；
  ② `sp.run` 绑定的是**一个会话**的 KV/检查点状态，不是无状态任务；
  ③ 单 stage 虽然不借槽，但 prefill 流式载入大量专家会**驱逐 decode 的热专家**（全局 LRU），
  并发后 decode 命中率会掉——这是最可能吃掉收益的地方，必须有分区/保护策略或量化测量。
- 现实收益：把「4.9 s 冻结」变成「2.4 s 冻结 + 更平滑」，但不会变成 100 ms 级——**100 ms 级需要方案 2 的混合批**。

### 方案 2（中改，收益上限最高）：同一前向里混 prefill token 与 decode 行
- Sarathi/DeepInfer 的做法：一个 step 的输入里既有 prefill chunk 的 token，也有在途 decode 的行，
  用 attention 掩码隔离；decode 的 TBT 由「本步 prefill token 预算」兜住。
- 对我们的额外好处：prefill 的大头是权重/专家读取，混批能把这份读取**同时喂给 decode**，
  等同于把 decode 的专家等待「免费」摊进 prefill。
- 代价：内核层面要支持 prefill+decode 混合批（现在 `prefill.cpp` 与 `verify/chain` 是两套独立路径）；
  KV/GDN 状态的按序列隔离；借出槽策略要重写。**这是源码级重构，不是调参。**
- 收益形态：并发时**总时间**下降（不是只把 TTFT 摊平），因为权重读取次数减少。

### 方案 3（配套，必须先做）：观测
现状的缺口是具体的（不是泛泛而谈）：
- **真实准入队列不可见**：`engine.waiting` 存在（`server.py:411/668`），但 `/metrics.live.queued` 在 `--batch` 模式下恒为 0；
  `/v1/status` 的 `in_flight = busy + queued` 是聚合数。
- **停车缓存无计数**：`--conversation-cache-slots 20` 的 hit/miss/evict 完全没有导出（`reused` 只是前缀复用 token 数）。
- **prefill 进度只有 `PP` 行**（`generate.cpp:5229-5231`），serve 用它做心跳（`server.py:819-822`），
  但没有结构化的「本请求 prefill 到第几块/总块数/预计剩余」。
应补：
1. serve `/metrics`：`engine.waiting`、每请求阶段（queued/prefill/decode）、`prefill_chunks_done/total`、409/429 计数；
2. 引擎：每 chunk 一行结构化日志（`tokens_this_chunk`、`chunk_index`、`segment_index`、`lent_slots`、`ms`）；
3. 停车缓存：`hits/misses/evictions/bytes` 四个计数。
没有这层，方案 1/1b/2 都无法验证，也无法回答「卡在哪一段」。

### 方案 4（不改代码但最省时间）：把「重读」消灭掉
- `--conversation-cache-mib 8192 → 20000+`（RAM 有余量需核查）、`--prompt-cache-every 16384 → 4096`、
  对**全新巨型提示词跳过停车**（park 是纯浪费且驱逐他人）。
- 这是唯一能把 82 s 变 0.79 s 的杠杆；优先级不低于调度改造，且可以和方案 0 一起做。

---

## 4. 待验证 / 风险

1. **混合批的真实边际成本**：用两个 8k 提示词同批 vs 串行各一次，比 `read in` 时间。
   若边际 < 0.5×，方案 2 的收益是数量级的；若接近 1×，方案 2 只改善公平性。
2. **chunk 边界与检查点/停车的一致性**：chunked prefill 已有实现（`--prefill auto`=8192），
   但「每段可恢复」是否成立要读代码确认；`prompt-cache-every=16384` 恰好是 2 个 chunk，密度与让出粒度耦合。
3. **decode 在交错下的抖动**：每步插入 prefill 会让 decode 窗口变长，需测 TBT p95；
   现在的 12 个窗口让出后解码会「补跑」，交错后应更平滑，但要防饥饿。
4. **共享的是 PCIe/DMA 与专家缓存，不是 CPU 池**：prefill 不走 `drive_pool`；单 stage 不借槽，
   但 prefill 的专家流会驱逐 decode 的热专家（全局 LRU）——方案 1b/2 的核心难点是缓存分区，不是槽失效。
5. **单线程假设**：引擎当前没有任何 mutex/flag 保护 prefill 与 decode 的共享状态，正确性完全依赖
   「同一线程串行」（`generate.cpp:6075`）；任何并发化都必须显式引入同步与 `host_res`/`d.conf` 的一致性检查。
6. **重试放大**：proxy 的 600 s 超时 failover 会把慢 prefill 复制到另一条 lane（`rag_proxy.py:1834-1845`），
   准入改造时要同时给巨型请求关掉它。

---

## 5. 建议的落地顺序

| 阶段 | 动作 | 停机 | 预期 |
|---|---|---|---|
| P0 | 网关准入（同 lane 限 1 个 >100k prefill）+ 长请求禁用跨 lane failover | `restart rag-proxy`，秒级 | 止住 8 路长文互踩与重复 prefill |
| P1 | 观测：导出 `engine.waiting`、prefill chunk 进度、停车 hit/evict | 引擎重启（日志/指标） | 先看得见，后面才能验证 |
| P2 | 方案 4 缓存参数 + `STRATA_PREFILL_YIELD_CHUNKS=1` A/B | 同一次重启窗口 | 重读大幅减少；断流 3.86→~2.4 s，代价 prefill +~12% |
| P3 | 方案 1b：prefill 工作线程 + chunk 边界让出 | 重启（代码） | 断流稳定在 chunk 级，decode 不再整段冻结 |
| P4 | 方案 2：prefill/decode 混合批（Sarathi 形状） | 重启（代码） | 并发总时间下降（权重读取共享） |
| P5 | PD 分离评估（需第三张卡或 NVLink） | — | 本轮不做 |

---

## 6. 参考来源

- Sarathi-Serve（OSDI'24，arXiv 2403.02310）：chunked prefill + stall-free batching；容量 2.6×/3.7×/5.6×。
- DistServe（OSDI'24，arXiv 2401.09670）：PD 分离 + goodput；7.4× 请求数 / 12.6× SLO。
- Mooncake（FAST'25）：KVCache-centric 分离架构；模拟负载 +525% 请求。
- vLLM V1 scheduler：`max_num_batched_tokens`、chunked prefill、decode 优先与 recompute 抢占。
- SGLang：RadixAttention 前缀树 + overlap scheduler。
- DeepSeek DeepInfer：本地快照 `docs/references/deepinfer-2026-10-20/code/scheduler.hpp`（行号见 §2.1）。
- 本机：书稿 09 章、`docs/reports/WHY-NO-GAIN-2026-10-21.md`、memory `strata-ttft-prefill-queue` / `strata-concurrency-limits`。