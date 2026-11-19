# DeepInfer 仓库精读 + 官方部署 Q&A：可移植与不可移植（对照我们引擎）

- 落款：2026-10-20 17:45（北京时间）
- 取材：`deepseek-ai/DeepInfer` 仓库（README、`examples/README.md`、`examples/yoco/README.md`、
  `deepinfer/engine/arg_utils.py`、`deepinfer/engine/stats_logger.py`、`deepinfer/model_executor/eplb/policy.py`、
  `deepinfer/distributed/shm_broadcast.py`）+ 仓库内 **`doc/DeepSeek V4.1 Flash 推理部署 Q&A.pdf`（10 页）**。
  原文已留档到 `~/docs/references/deepinfer-2026-10-20/`（qa.txt / README.md / yoco.md / PDF）。
- 对照对象：本 Pod 的 `SSEStraBed` 引擎与 `Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS` 模型（事实取自 `~/book/` 已核实章节）。
- 全程只读：未改配置、未重启服务、未打压测流量。

---

## 1. 官方 Q&A 里的硬数字（可直接引用的参照点）

**规模与成本**
- 单一集群服务吞吐 **155 万亿 Tokens/天**；整体收入 **每卡每小时 $38**（按谷价）。
- 设计目标三条：兼容多厂商芯片；**用常见 8 卡机器做 Prefill、常见 64~128 卡超节点做 Decode**；**KVCache 保留 72 小时以上**。
- 跨机房 P/D 可行（V4.1 Flash 大幅压缩了 KVCache），**约每 8 张卡需 400 Gbps 互联**。
- 最小部署单元 1P+1D 也能跑，P:D 卡数比实践上约 1:1；**效率最高的最小单元是 64 卡 P + 64 卡 D**。

**并行形态**
- Prefill：EP2~8 / DP2~8，**显存够用时用尽可能小的 EP/DP**；Decode：**EP64~128 / DP64~128（Wide-EP）**，卡可用时尽量大。
- VE（Vision Encoder）单卡部署即可，建议用便宜的卡。**生产上暂未用 TP、CP**（代码里有 CP，但生产未启用）。
- Wide-EP 的收益来源：显著减少 per-step MoE 专家部分访存量，配合 EPLB 提升 TPS（文中给了 EP8 vs EP64 对比）。
- 做了负载均衡后，**实际达到「完全均衡离线 benchmark」的约 80%**。

**EPLB（专家并行负载均衡）实测口径**
- 做法：每卡在自有专家之外多备若干**冗余专家**放热门副本；统计最近 N 步各专家负载；每 M 步重排，专家权重经 NVLink `symm_mem` 在组内 shuffle。
- Prefill：每卡 **4 个冗余专家起步**（向上填充至总专家数为 ep_size 倍数），统计窗 **100 步**，每 **1000 步**重排；实测 `moe_lb`（最慢 rank / 均值）= **1.074**（EP4DP4）/ **1.045**（EP2DP2）。
- Decode：每卡 **1 个冗余专家起步**，EP64 取整后每卡共 7 个专家（不含共享专家），统计窗 **1000 步**，每 **10000 步**重排；实测 `moe_lb` = **1.090**。
- 代码侧（`eplb/policy.py`）：logical expert（模型定义）vs physical expert（卡上实际副本，数量 = logical + redundant）；
  贪心分配冗余副本（每轮复制「加副本后最大每副本负载」最小的那个），再用 `_batch_greedy_indices` 把副本排到各 rank，
  目标是最小化 `sum_step max_rank(load)`。

**KVCache 与存储**
- Block KVCache（Main KV + Indexer K）放**独立 3FS 存储集群**，不用本机内存 → 所有 prefill 机器能命中任何前缀，**因此不需要前缀亲和路由**（也避免与负载均衡策略打架）。
- 存储服务端推荐 **20 × 30.73 TB SSD**，单服务端至少 **40 GB/s 读 / 5 GB/s 写**，保留 24 小时数据；集群与线上卡的配比按逻辑表约 **14.4 TB/卡**。
- 3FS：纠删码 **10+2 EC**（每 EC 组容忍 2 节点同时故障），读取时向组内 11 台发起请求、任意 10 个响应即可解码；GC Worker 按 LRU 周期性淘汰。
- TTL：agent 流量 **3 天**、chatbot 流量 **7 天**；经验值——3 天 TTL 会让 chatbot 的 KVCache 命中率下降 **0.5%~1%**。
- prefill 侧同样是 **paged KV cache**；decode 阶段**不开 KV offload**（decode 不是 cache 容量 bound），但 decode 产生的 KV 也落盘以提升命中率。
- 换入路径：3FS → 宿主机 shm（独立进程异步）→ HBM（engine 进程内线程，逐层 H2D 与前向重叠）；
  因为 **CUDA copy engine 对 KV block 粒度拷贝性能不佳，改用 GDR/RDMA 打满 PCIe 且不占 SM**。

**低精度**
- Main KV：per-16，e4m3（相比 NVFP4 无 global scale）；Indexer KV：per-32，ue8m0，MXFP4；MoE 路由专家权重：per-32，ue8m0，MXFP4。

---

## 2. 调度：prefill 用「时间预算反解 chunk」，decode 用「双维度均衡」

**Prefill Load Balancer**
- 按 context 总长**分 4 个 tiers**：`<16K / 16-64K / 64-256K / ≥256K`；每档分到的引擎台数按该档上一轮的 compute-quota 需求占比动态重分，每档有自己的单批 compute-quota 上限。
- DP 组间：按剩余 compute-quota 拉取；**不做前缀亲和路由**（因为任何引擎都能命中任何前缀 —— 前提是独立 KV 存储）。
- DP 组内：一批请求给到 engine，每收到一个就派给当前 quota 负载最低的 DP rank。
- **单卡内（关键）**：请求 FIFO 出队，**chunk size 不固定，由预算反解** ——
  每步定一个时间预算 `compute_quota_limit`（单位就是纳秒），用**拟合的二次代价模型 `cost(q, c)`** 预测这一步要花多久
  （`q` = 序列 token 数，`c` = 这条序列已算完的前缀长度），调度时对 q 解二次方程，取
  「在 32,768 token 预算之内、又正好把剩余时间预算吃满」的最大 q；预算本身还会被 DP 组内的 allreduce-MIN 进一步压低。

**Decode Load Balancer**
- 对所有 decode 实例的所有卡统一路由，**同时均衡两个维度：请求数（决定每步批大小）与 KVCache 总长度（决定注意力部分代价）**。
- 启发式：**短请求发给「并发较小且 KVCache 总长偏大」的卡，长请求发给「KVCache 总长较小」的卡**（文中称 DualPath 组内调度算法）。

**Vision Encoder Load Balancer**
- 以单台机器为单位路由（随机选一台部署了卡数个 VE 的机器），机器内选**当前处理图片 patch 数最少**的卡
  （patch 数由宽高直接算：`(width/patch_size) × (height/patch_size)`）。

**Asynchronous Scheduling（Zero-Overhead Batch Scheduler）**
- 问题定义：decode 第 N 步是 `schedule(N) → forward(N) → sample(N) → post-process(N)`，其中 schedule 与 post-process 是纯 CPU 逻辑，
  且 post-process(N) 前必须 GPU-CPU 同步拿到 sample(N) 结果 → **这段 CPU 时间里 GPU 空闲**。
- 做法（参考 SGLang / Nanoflow）：GPU 执行 `forward(N) → sample(N)` 时，CPU 同时执行
  `schedule(N+1) → launch_forward(N+1) → post-process(N-1) → launch_sample(N+1)`，GPU 全程无空闲。
- 代码侧：`deepinfer/distributed/shm_broadcast.py` 提供共享内存环形队列（`ShmRingBuffer` / `MessageQueue`，
  自旋等待 + 超时抛错 + 可跨进程 export handle），就是这条 CPU↔GPU 流水线的载体。

**扩缩容与容错**
- prefill 通过统计**未命中 token 数**判断所需机器数；decode 通过在跑请求数调整，调整有限速与上下界。
- 大 EP 部署下长尾请求浪费资源：CE 收到下线通知后进**宽限期 1 min**，仍未完成的请求及其 KVCache 一次性转发给其他 CE 继续 decode。
- 冗余配额：池里留未占用的 ready 机器作扩容库存；实例数永远比算出来的需求多；**配额占用率 > 0.8 时禁止缩容**。
- 重启是分钟量级；**容错靠 PE/CE 配额冗余 + 请求级重新路由，而不是靠缩短单引擎重启时间**。
- EP failover：大 EP 下实例全部卡构成单一通信域，任一张卡故障即整个实例不可用；重启时**按当前健康卡数重新确定 EP 规模**。

---

## 3. 推测解码：DSpark 的「变长验证」与它的实测收益

- 机制：draft 一次生成 **5 个 token**；**Confidence Head 预测每个 draft token 的接受率**；
  调度时把各 draft token 按预期接受率排序，**选取「有效吞吐最大」的前缀子集去验证** —— 因为不同请求、不同位置的接受概率不同，
  固定长度验证会把验证算力浪费在低概率 token 上。
- **实测收益（EP64/DP64 benchmark）**：dynamic length verification 把每步验证的 draft token 数**砍掉约 30%（5.00 → 3.2~4.4）**，
  同时**平均接受长度基本不变（3.29 vs 3.20）**；在生产运行点（**每 rank ~60 并发**）折合 **+4~7% 吞吐**。
- 与异步调度的配合（这条很关键）：`schedule(N+1)` 执行时第 N 步的 confidence **尚未回传**，
  所以 **CPU 调度时只确定「总预算」，逐请求的验证长度在 GPU 上由一个 tilelang kernel 重算**
  （累乘、全局排序取 top-k，并融合 remap scatter），结果回写 pinned buffer。
- 开关：`DEEPINFER_ENABLE_MTP_VARLEN=1`（变长 MTP）、`DEEPINFER_USE_NEXTN_DECODE`（最大 draft 长度，默认 5；设 0 关闭投机）。
- Draft head 叫 **DFlash**；confidence 附着在每轮发出的最后一个 token 上、**滞后一轮**（`examples/yoco/README.md`：第一轮为 None）。
- 内部 draft model 比开源版本新一些，但**性能差别不大**（原文口径）。

## 4. SWA Bounded Replay / YOCO / CED：把重算代价变成常数

- **Bounded Replay 本体**：只重算最近 **128** 个 token，并把注意力窗口截断在重算段内 ——
  从位置 s 起回放，位置 i 的 query 只与 `max(s, i−127) … i` 区间的 key 计算 → **重算代价降为与上下文长度和层数无关的常数**。
- 用在两处：
  1. **Encoder SWA Bounded Replay**：前缀缓存命中长度从此**只依赖 Block KVCache**；SWA KVCache 缺失时只重算命中前缀的最后 128 token，
     与未命中后缀一同前向；重算段只重建 SWA KVCache，**Block KVCache 原样复用、不重算不覆写**。
  2. **Decoder SWA Bounded Replay**：**prefill 计算仅在 encoder 层进行**；CED 架构下 decoder 层的 Block KVCache 由 encoder 末层 hidden state 投影得到，
     只有 decoder 的 SWA KV 需各层自算 —— 精确重建要 **20 个 decoder 层跑最后 2560 个 token**；
     改为每次 prefill 在 decoder 层**只重算 prompt 末尾 128 个 token**（产出仅供 decode、不进前缀缓存）→ **总 prefill 计算量近乎减半**。
- **命中策略**：命中长度由 Block KVCache 决定，最后一个 block 是否需要重算由 SWA 决定；
  对输入长度 L，**最大命中长度 ≤ L − 134**（窗口 128 + 投机回退 6）。先查 Block KV 得命中长度 L1（128 的整数倍），
  再查 SWA：若 `L1−128 … L1` 区间内有 SWA KV 则命中 L1，否则退一块命中 `L1 − 128`。
- **SWA KV 的存储形态**（与 Block KV 完全不同，值得注意）：不进分页 KV block，而是放 **state cache**，每序列一个**定长 slot**（`StateBlock`），
  按「是否需要 store」切成 `[stored region: SWA] [dropped region: YOCO consumer SWA]`；
  dropped region 含 YOCO consumer SWA 层（**恢复依赖 prefill 重算**）与 C2 压缩的边界累加器 `ca_state` / `ica_state`；
  **V41 需要 store 的层是前 20 层**。
- SWA KV **只在 128 tokens 整数倍位置存储**；prefill 结束后按「序列末尾留一个 token 后向下对齐到 128」落点，不足 128 则本轮不存；
  **chunked prefill 中途不存储，只有最后一个 chunk 才存**。decode 阶段在显存另开副本区，每跨过 128 边界就把当前窗口拷入副本区，
  请求结束时从最近一次完成拷贝的位置存储（拷贝位置最多越过边界 5 个 token，由 ring 比窗口多出的 6 个位置吸收）。
- **ring 长度**：推理视角 `ring = 窗口 128 + 最大回退 6 = 134`（投机解码取 6）；
  PE 侧为支持任意位置 store，ring 加长到 **396 = 134 + `VLLM_PREFILL_STORE_EXTRA_SIZE`(128) + `VLLM_YOCO_PREFILL_KEEP_TOKENS`(134)**；CE 不开 extra，ring 就是 134。
- **落盘体量**：`state_size_to_disk = 20 层 × 134 token × 528 B = 1,415,040 B/seq`（528 B/token 是 MXFP8 的 SWA KV 格式）。
  SWA KVCache **不进持久化缓存**，留在产生它的那台机器的内存里，由按 key 分散存放在各 Entrypoint 上的内存目录记录位置，本机没有就跨机取回（400G VLAN）。
- **YoCo 的架构前提**（`examples/yoco/README.md`）：模型必须是兼容的 V4.1 YoCo checkpoint，判据是 **`n_prefill_layer < n_layer`**，脚本会在运行时校验；
  等价性验证分 L1（单卡、秒级、不加载权重，比较 SWA pivots / visible keys / store slots / compressed key ranges）
  与 L2（8 卡 + 权重，固定 teacher-forcing 轨迹逐元素比 logprob 与 top-K）。
- 其它算子口径：**Mega MoE** 是 NVL domain 的通用方案，无论 EP 大小、batch 大小都显著快于 unfused MoE；
  开源 FlashMLA 少了一个硬改 SASS 的优化，decoding 算子慢 5%~10%，端到端最多差 5%。

---

## 5. 我们这边的既有事实（本次当场核对，作为对照基准）

**模型 `Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS`**（GGUF 头部 `general.architecture = "qwen4exp"`，当场 hexdump 可读）
- 48 层、`n_embd = 2560`、`qsa_interval = 4` → **层号 3,7,…,47 是全注意力**（派生而非第二份列表）。
- **GDN 门控线性注意力 36 层**：16 个 key 头 ×128、48 个 value 头 ×128、conv1d 宽度 4、conv 通道 10240、value 维 6144、`ssm_state_size = 128`。
- **QSA 稀疏全注意力 12 层**：24 头 ×256、**2 个 KV 头**、索引器 4 个 query 头 + 1 个共享 key 头 128。
- 门控残差每层 `hc = 4` 流 ×2560、低秩 320；MoE 每层 **512 专家 top-10 + 1 共享**、`n_ff = 640`。
  出处：`book/01-这套系统是什么.md`（n_layers 48 / n_expert 512）、`book/06-引擎启动与内存计划.md:91-101`、`book/08-前向通路与内核.md` §8.2。

**引擎 `SSEStraBed`**
- **没有任何 SWA / sliding-window / local-attention 支持**（当场 `grep -rniE "swa|sliding_window|local_attn|window_size" src include` 零命中）。
- prefill 用**固定 chunk**：`prefill_chunk`（默认 2048；`prefill_auto` / `prefill_auto_max` / `prefill_until` / `short_read`），
  见 `src/program/generate.cpp:180, 368, 1414, 1689, 1756`。没有任何「时间预算」形式的调度量。
- 推测解码侧**已经有一套代价感知的草稿预算选择器**：`include/strata/spec/controller.hpp` 的 `K_MAX = 8`、
  `dense_ratio[]`（验证 n=k+1 个 token 的稠密部分耗时比例）、`distinct_ratio[]`（窗口 n 与单 token 的**不同专家数之比** U(n)/U(1)）、
  `mtp_p_[]`（MTP 各位置接受率估计）；`src/spec/draft_policy.cpp` 用衰减计数（`kDecay = 0.97`）统计 lookup/MTP 的接受情况。
- 默认值仍是保守的：`spec=0`、`batch_mtp=false`、`batch_groups=1`、`pool_workers=0`（`generate.cpp:284-447`）。
- 观测面：`GET /slots` 只返回一个合成槽（`Strata/serve/server.py:2594-2601`），引擎的 4 个批槽对外不可见。

---

## 6. 逐条对照：可移植 / 不可移植

总判断先给：**我们的 GDN（36 层，常数大小递归状态）+ QSA（12 层，带索引器的稀疏全注意力）与 DeepSeek 的 CED/YOCO（有界窗 SWA + Block KV + Indexer）属于同一家族**
—— 都是「多数层用有界或常数状态、少数层用带索引器的全注意力」。所以**能搬的是调度与缓存策略，不是它的算子**（算子绑 SM100 / 昇腾 950）。

| 机制 | DeepInfer 的做法 | 我们现状 | 判断 |
|---|---|---|---|
| **变长推测解码** | Confidence Head 预测每个 draft token 接受率，按预期接受率排序取「有效吞吐最大」的前缀验证；每步验证 token 数 −30%（5.00→3.2~4.4），接受长度不变（3.29 vs 3.20），生产点 +4~7% 吞吐 | 已有 `Controller` 用 `dense_ratio`/`distinct_ratio` 成本表 + `mtp_p_` 逐位置接受率来选 k | **高**。我们缺的不是「代价感知选 k」（已有），而是**批内逐请求分配**：一个 k 服务整批 vs 每条序列各自预算 |
| **异步调度（zero-overhead batch scheduler）** | GPU 跑 `forward(N)+sample(N)` 时 CPU 跑 `schedule(N+1)+launch_forward(N+1)+post-process(N-1)+launch_sample(N+1)`；载体是 shm 环形队列 | 已知瓶颈就是 **CPU 池串行 + prefill 独占**；`pool_workers=0` | **中-高**，方向明确但改动最大（要双缓冲 + 把采样/后处理拆成可延迟一轮） |
| **prefill 用时间预算反解 chunk** | 每步定 `compute_quota_limit`（纳秒），用拟合二次代价模型 `cost(q, c)` 反解「正好吃满时间预算」的最大 q（上限 32768 token）；tier 分桶 + DP 组内派给 quota 负载最低的 rank | **固定 `prefill_chunk`（默认 2048）**，无任何时间预算形式的调度量 | **最高**。纯调度层改动，不依赖模型/硬件；直接对着我们「并发下 TTFT 两分钟」的痛点 |
| **EPLB 冗余专家 + 周期重排** | 每卡若干冗余专家放热门副本，统计最近 N 步负载、每 M 步重排；prefill 4 冗余/卡、窗 100 步、1000 步重排（moe_lb 1.045~1.074）；decode 1 冗余/卡、窗 1000 步、10000 步重排（moe_lb 1.090） | 512 专家/层、top-10+1 共享；**专家三级存储 + 命中率**是既有主线（book 07） | **中**。我们只有 3 卡，EP 收益小；但「按最近 N 步负载周期性重排常驻副本」这个**循环本身**可移植到 CPU→GPU 专家分层上 |
| **SWA Bounded Replay / YoCo** | 只重算最近 128 token 且把注意力窗截断在重算段内 → 重算代价变成与上下文长度无关的常数；decoder 层只重算 prompt 末尾 128 token → **总 prefill 计算量近乎减半** | 引擎里**没有任何 SWA 概念**；GDN 36 层是常数状态（比 SWA 更省，但状态依赖整段历史，不能用「有界重放」近似）；QSA 12 层的 KV 必须覆盖整段前缀 | **低（照搬不可行）**。可移植的是它的**问题分解方式**：把「必须整段重算」与「可以只重算边界」分开记账，并给后者定长 slot。我们的 GDN state 检查点正是「必须整段」那一侧，值得单独计量 |
| **独立 KV 存储集群 + 72h TTL** | Block KV 放独立 3FS（约 100 PiB SSD、10+2 EC、LRU GC、TTL 3~7 天）→ 任何引擎都能命中任何前缀 → **不需要前缀亲和路由** | 双车道 + 前缀亲和（快道/普道分工）；prompt-cache 检查点 6 个 / 每 16384 token / root 2048（约 118 MB/检查点），停车快照被驱逐 → 每轮重读 ~238k | **不可移植（体量差 10^6）**。但它点出我们的真病灶：**缓存命中率被「缓存放在计算节点里」这件事绑架**。我们做不了 3FS，但能做「检查点不被驱逐」与「驱逐后代价可计量」 |
| **KV 换入用 GDR/RDMA，不用 copy engine** | 3FS → 宿主机 shm（独立进程异步）→ HBM（engine 进程内线程，**逐层 H2D 与前向重叠**）；因 CUDA copy engine 对 KV block 粒度拷贝性能不佳，改用 GDR 打满 PCIe 且不占 SM | 我们的 H2D 走普通拷贝路径，且与 CPU 池串行耦合；专家权重换入同理 | **中**。单卡内没有 RDMA 可用，但「**逐层换入与前向重叠**」这个形状在单机内就能实现（prefetch 下一层权重/KV 到 pinned buffer），是我们 CPU→GPU 分层最直接的收益点 |
| **低精度 KV / 权重格式** | Main KV per-16 e4m3（相比 NVFP4 无 global scale）；Indexer KV per-32 ue8m0 MXFP4；MoE 路由专家权重 per-32 ue8m0 MXFP4 | 我们走 GGUF 量化路线（iq3_xxs 等），KV 精度与权重精度由 GGUF 元数据决定 | **中**。方向一致：**KV 比权重更激进地压**才划算。可直接对照的是「KV 用 e4m3/MXFP4 而非 fp16」这一档，能显著推迟 prompt-cache 被驱逐 |
| **Batch-invariant / 确定性采样** | `DEEPINFER_BATCH_INVARIANT=1`（仅 NVIDIA）+ `MTP_VARLEN=0` + 固定 `seq_seed` → 结果不随 batch 组成变化 | 无对应开关；A/B 复现时「同输入不同 batch 组成结果漂移」会污染结论（`claude-local-early-stop` 排查吃过这个亏） | **中，但优先级被低估**。我们做引擎改造要靠 A/B 判定收益，**没有可复现基线就没有可信测量**。这是改造工作的前置设施，不是性能特性 |
| **deepseek-recipe：special token 防注入 + 分段并行 tokenize** | 构建 prompt 时记录显式 special token 的位置，编码时直接映射 token ID；用户文本里的控制标记字面量被编码成**普通 token 序列**；输出解析**按 token ID** 识别思考结束标记与 DSML 标记，模型用普通 token 拼出的相同文本不会获得触发能力；special token 之间的普通文本可**独立并行编码**再按序拼接，降低超长上下文分词耗时 | 我们已在网关侧做过 special token 转义（`archive/patches/serve-special-token-escape-2026-10-18/`），但走的是「文本层面转义」，不是「按 token ID 解析」 | **高，且成本最低**。它给的判据很硬：**任何用文本拼接判定控制语义的地方都是攻击面**。我们的思考/工具解析目前在 Python 文本层，值得按这个判据重审一遍 |
| **每步自描述日志（观测面）** | `stats_logger.py` 每步产出 StepStats（`sum_seqlen_qxk` / `max_seqlen_k` / `attn_tflops` / `total_tflops` / `num_cached_tokens_gpu` / `num_verified_tokens` / `batch_confidence_error` / `moe_load_balance_ratio`），窗口聚合出 MFU、avg_verified_len、moe_lb、TTFT max/mean、running/pending/chunked/preempted/aborted、prefill/decode 时间占比 | `GET /slots` 只暴露一个合成槽，引擎 4 个批槽对外不可见；批槽 metrics 缺失是已知坑（memory `strata-bench-harness`） | **最高性价比的一条**。它证明「把批内组成、缓存命中、注意力代价、被抢占数打进每步日志」是**可实现的常规做法**。我们改造前就该先有这一层，否则改完无法归因 |
| **扩缩容与容错的取向** | 重启是**分钟量级**，容错靠 PE/CE 配额冗余 + 请求级重新路由，**而不是靠缩短单引擎重启时间**；配额占用率 > 0.8 时禁止缩容；大 EP 长尾请求进 1 min 宽限期后整体转发给其它 CE | 我们只有 3 卡、单引擎、无冗余实例；重启 = 停服（红线：必须先经用户同意） | **不照搬，但接受它的判断**：「不要优化重启速度，要优化故障后的请求迁移」。我们若拆出第二辆车道，价值主要就在**请求可迁移**上，而不是启动快慢 |

---

## 7. 源码级补充：他们到底怎么实现的（第二轮精读，只看开源部分）

第二轮把引擎侧关键源码拉到本地留档（`~/docs/references/deepinfer-2026-10-20/code/`，15 个文件）：
`scheduler.hpp`(66 KB)、`mtp_varlen.hpp`、`flops_estimator.hpp`、`block_manager.hpp`、`zos.cpp/hpp`、
`sequence.hpp`、`llm_engine.py`、`config.py`、`environ.py`、`worker/model_runner.py`、`worker/decode_perf_cache.py`、
`tilelang_ops/schedule_mtp_varlen.py`、`eplb/shuffle.py`。

### 7.1 NORMAL 引擎（= 我们的形态）里，prefill 是「按节拍插进 decode 流」的事件

`scheduler.hpp:296-341`。单引擎同时干 prefill 和 decode 时，**不是「有 prompt 就先做 prompt」**，而是：

- **ALL 点**：每 `prefill_attempt_interval` 个 decode step（默认 **70**）尝试一次，**全员都有 prefill 活才做**
  —— 防止一个 rank 干活、其余 rank 陪跑空 prompt step；
- **ANY 点**：每 `prefill_any_attempt_interval` 个 decode step（默认 **100**）兜底，**任一 rank 有活就集体做**
  —— 注释原话：「给孤立 rank 兜底——它的 prefill 最多等这么多个 decode step」；
- 触发前还要过 KV 水位门：`get_gpu_cache_usage() <= GPU_CACHE_PREFILL_WATERMARK`（常量 **0.9**）；
- **第二轮投票**：`want` 只看了 waiting 非空，但 `_schedule_prefill` 仍可能空手（slot 满、KV 块不够）。
  全组都空手时必须转 decode —— 注释写得很直白：硬做空 prompt step 会把 `decode_step` 清零，
  下一步接着投票、接着空手，「running 满 + waiting 非空就死循环了」。

**对我们的意义**：我们「prefill 独占 → 并发腰斩 / TTFT 两分钟」的形态，在他们那里是被**节拍 + 水位 + 防死循环投票**
三重约束住的。这套东西**不依赖任何硬件、不依赖模型结构、不依赖集群**，是纯调度层策略，
是本次精读里对我们最直接可用的一条。

### 7.2 开源部分里 prefill chunk 的真实约束（时间预算反解不在开源里）

`_schedule_prefill`（`scheduler.hpp:351-560`）对 chunk 大小的约束是**token 预算 + 强制切点**，不是时间预算：

- `max_num_batched_tokens`：默认 `max(max_model_len, 4096, max_num_seqs × (1 + nextn))`（`config.py:219-227`）；
  超过就把这条 prompt 截到剩余额度（`new_workload = min(seq_workload, 剩余额度对齐 cp_align)`）；
- `seq->schedule.chunk_checkpoints`：**外部传入的强制切点队列**（`llm_engine.py:483, 615-616`），
  chunk 不能越过下一个 checkpoint —— 这就是 Q&A 里「SWA KV 只在 128 整数倍处存储」在代码里的落点；
- `_adjust_chunk_size`（`scheduler.hpp:235-260`）：多模态保护，切点落在 `IMAGE_PAD` 区间内时**向前收缩或向后扩展**到图片边界；
- `prefill_max_sum_seqlen_k`：对一步内 `sum(seqlen_k)` 的封顶（`DEEPINFER_PREFILL_MAX_SUM_SEQLEN_K` 未设即不限制）；
- `lookahead_tokens`：`seqlen_q < max(lookahead_tokens, 1)` 就不调度；`chunked_prefill=False` 时一旦需要截断直接 break（不切）。

**结论修正**：Q&A 里最吸引人的「纳秒时间预算 + 拟合二次代价模型 `cost(q,c)` 反解 chunk」**在未开源的 Prefill LB 里**，
仓库里搜不到 `quota` / `cost(q,c)`。开源的是「token 预算 + 强制切点」这一层。
→ 我们想抄时间预算，**代价模型得自己拟合**，抄不到现成实现；但 §7.1 的节拍/水位是可以直接抄的。

### 7.3 DSpark 变长验证的完整算法（`csrc/engine/mtp_varlen.hpp`，纯 CPU C++，可照搬）

这是本次精读**最有价值的一块**：整套「按置信度分配验证预算」是**一个 100 行的 CPU 函数**，不依赖集群、不依赖 SM100。

输入三样：
1. `_decode_perf_curve`：**profile 出来的 `(batch_size → forward_ms)` 曲线**，batch_size 严格递增（见 §7.4）；
2. 每条序列的 `mtp.accept_rates`：MTP confidence head 预测的**逐位置接受率**（长度 = nextn）；
3. 两个运行时估计器（见下）。

`schedule()` 的做法（`mtp_varlen.hpp:150-267`）：

- 把所有 (序列, 位置) 的接受率摊平成两个队列，**都用「接受率降序、同率按索引升序」排序**：
  - `acc_queue_for_bsz` 用 **N−2 轮**的接受率 → 用来选**总预算 `best_bsz`**；
  - `acc_queue_for_seqs_q` 用 **N−1 轮**的接受率 → 用来**逐请求分配 `seqlen_q`**；
  - 注释原话：「假设此时在第 N 轮，我们期望使用 N-2 轮的 accept_rate 去计算 best_bsz；
    并使用 N-1 轮的 accept_rate 去分配每个请求的 seqlen_q（non-ZOS 在这里做，ZOS 在 GPU 上修正）」。
    新序列 / 尚未产生 accept_rate 的：有 draft 则 pad `1.0`，否则 pad `0.0`。
- **选总预算 = 扫描边际吞吐**：`cur_expected` 从 `num_seqs`（每序列保底 1 个 golden token）起，
  按接受率降序逐个加：`cur_expected += rate; cur_bsz += 1;`
  每加一个就算 `time_ms = baseline_ms(graph) + slope_ms × graph_size + sampler_ms`，
  `throughput = cur_expected / time_ms`，**取最大者作为 `best_bsz`**。
  → 「有效吞吐最大的前缀子集」在代码里就是这条扫描，不是公式求解。
- **分配**：`seqlens_q.assign(num_seqs, 1)`，再把 `best_bsz − num_seqs` 个名额按第二个队列顺序逐个 +1。

两个估计器（都是**滑窗中位数**，不是均值，抗抖动）：
- `ForwardRuntimeEstimator`：启动曲线没考虑上下文长度影响，于是动态拟合一个 `slope_ms`，
  给耗时估计加 `slope_ms × graph_size`。输入是 `(forward_ms − baseline_ms) / graph_size`；
  滑窗 **5000** 步，**两层中位数**（先按 graph size 聚合取中位，再对各 graph 的中位取中位），
  只计入样本数 ≥10 的 graph，各 graph 等权；用两个 multiset 维护动态中位数，更新 O(log n)。
  取用时 `slope_ms = max(0, …)`，注释：「以启动曲线为下界，避免负斜率外推到更大的 graph 后产生非正耗时」。
- `SamplerRuntimeEstimator`：滑窗 **9** 样本、≥5 才出值、取中位数；按 `num_seqs` 分桶，样本不够就回退全局那份。

**GPU 修正版与 CPU 版有一处实质差别**（值得注意）：`schedule_mtp_varlen.py` 的参考实现先做
`cum_rates = cumprod(rates, dim=1)`，再全局 top-k，`seqlens_q = 1 + wins`。
即 **GPU 上按「前缀累乘的接受概率」排序**（验证第 k 个 draft 的前提是前 k−1 个都被接受，累乘才是边际价值）；
而 CPU 版排的是**逐位置原始接受率**。Q&A 说的「累乘、全局排序取 top-k、融合 remap scatter」就是这一个 kernel，
`total_q` 作为预算传进去，`remap` 把上一批的 confidence 行散射到当前批（新序列 → 0）。

### 7.4 `(batch_size → step_ms)` 曲线怎么测出来的（`model_runner.profile_decode_perf`）

这段是**可以直接搬进我们压测台的测量协议**（`model_runner.py:561-631`）：

- 启动时 profile 一次；**沿 CUDA graph 的 batch size 列表逆序**（从大到小）跑，
  因为各 batch size 共享最大 batch buffer 的切片视图，`valid_token_mask` / `gate_force_random` 填一次即可；
- 每个点：**1 次 warmup + 5 次 CUDA Event 计时**，每次 replay 后跟一次 `all_reduce` 同步；
- **截尾均值**：排序后丢掉最高和最低各 1 个（`times[1:4]`），再在 EP 组内 `all_reduce(SUM)/size` 取均值；
- 结果落盘缓存：文件名 `sha256(model, parallel_config, batch_sizes, time_unit="ms").<uuid>.<timestamp>`，
  TTL 默认 **3600 s**，写用 `tmp + os.replace` 原子替换，只有 rank 0 写、之后 `broadcast_object_list` 发给全组。

### 7.5 可直接引用的默认常量（`deepinfer/environ.py`）

| 常量 | 默认值 | 备注 |
|---|---|---|
| `DEEPINFER_USE_NEXTN_DECODE` | 5 | 设 0 关闭投机 |
| `DEEPINFER_MAX_BACKTRACKING_TOKENS` | `max(v, nextn+1)` = **6** | Q&A 里 `ring = 128 + 6 = 134` 的 6 就是它 |
| `DEEPINFER_PREFILL_ATTEMPT_INTERVAL` | **70** | ALL 投票点（每 70 个 decode step） |
| `DEEPINFER_PREFILL_ANY_ATTEMPT_INTERVAL` | **100** | ANY 兜底点 |
| `GPU_CACHE_PREFILL_WATERMARK` | **0.9** | `scheduler.hpp:80` 硬编码常量 |
| `DEEPINFER_MOE_LB_STAT_INTERVAL` | 128 | benchmark 里临时设成 10 |
| `DEEPINFER_EXPERT_COUNT_WINDOW_SIZE` | 100 | 与 Q&A 的「统计窗 100 步」一致 |
| `DEEPINFER_LOGGING_INTERVAL_STEP` | 100 | prefill 再除以 5、decode 用 20（benchmark 口径） |
| `DEEPINFER_CUDAGRAPH_BATCH_SIZE_INTERVAL` | 8 | graph 桶间距 |
| `DEEPINFER_INDEX_ATTN_MEMORY_LIMIT` | 10 GiB | 稀疏注意力索引内存上限 |
| `DEEPINFER_NUM_STATE_SLOTS_RATIO` | 1.2 | state slot 过量分配比 |
| `DEEPINFER_YOCO_PREFILL_TRUNCATION_MODE` | **off** | `mask` 只收窄可见域，`compact` 真删 query |

**一处要诚实标注的反差**：`DEEPINFER_ENABLE_MTP_VARLEN` 与 `DEEPINFER_ZERO_OVERHEAD_SCHEDULING` 在 `environ.py` 里
**默认都是 False**；后者由 `arg_utils.py:51-54` 按 `engine_type.default_zero_overhead_scheduling` 写入环境变量。
README 说「异步调度默认开启」要按引擎类型理解，不是全局无条件默认。

---

## 8. 结论：如果只挑三件事动手，挑这三件（按顺序）

**第 1 件：先把「每步自描述」做出来（观测面），再谈改造。**
DeepInfer 一个 decode 步的日志里同时带着 `num_batched_tokens`、`sum_seqlen_qxk`、`max_seqlen_k`、
`num_cached_tokens_gpu`、`avg_verified_len`、`preempted`、`prefill/decode 时间占比`。
我们「并发腰斩 / TTFT 两分钟」之所以难归因，正是因为批槽内部这些量看不见（`/slots` 只有一个合成槽）。
**第一步（不停服）**：在批循环里按步追加一条结构化记录，字段照抄它的命名习惯，先只统计、不改任何行为。
这一步做完，后两条才有判据。

**第 2 件：prefill 从「固定 chunk」改成「时间预算反解 chunk」。**
它每步定一个纳秒级 `compute_quota_limit`，用拟合的二次代价模型 `cost(q, c)` 反解「正好吃满预算」的最大 q。
我们已有 `dense_ratio[]` / `distinct_ratio[]` 这类耗时比例表（`include/strata/spec/controller.hpp`），
说明「用拟合代价预测耗时」这条路在我们代码里已经走通过一次，只是目前只用于选投机宽度。
**第一步（不停服）**：先在压测台（普道 8092）离线量我们自己的 `cost(q, c)` 曲线，把固定 2048 换成
「预算内最大 q」，对比 TTFT 分布；默认仍走旧路径，用开关切换。

**第 3 件：批内逐请求的投机验证预算。**
我们已有代价感知选 k，但一个 k 服务整批；它的实测是**验证 token 数 −30%、接受长度不变、+4~7% 吞吐**。
**第一步（不停服）**：先只做「按请求分别记录接受率与验证长度」，把 `avg_verified_len` 拿到手，
再决定是否做 GPU 侧 top-k 分配。注意它的经验：CPU 调度时只定**总预算**、逐请求长度在 GPU 上重算 ——
那是因为它有异步调度；我们没有异步调度时逐请求分配可留在 CPU，代价不同但结论可先验证。

**明确不做**：Wide-EP（3 卡无收益）、独立 KV 存储集群、YoCo/SWA 重放（模型无 SWA 层）、
换 DeepInfer 二进制（SM89 + GGUF 路线不匹配）。

### 8.1 源码精读后的修订（同一天的第二轮）

看完源码，上面三条**不变，但顺序和形态要改**：

- **新增第 0 件（比原第 2 件更便宜、更对症）**：把 prefill 从「有活就插队」改成
  **「按 decode step 节拍插入 + KV 水位门 + 防空转投票」**（§7.1）。
  它不需要代价模型、不需要 profile 曲线、不需要改模型，只改「什么时候允许插 prefill」。
  他们的默认节拍是 ALL 点 70 步 / ANY 兜底点 100 步 / 水位 0.9，
  这三个数**不能照抄**（他们一步比我们快一到两个数量级），但**形状可以直接用**：
  我们的等价量应该是「每 N 个 decode step 或每 T 毫秒允许一次 prefill 窗口，且 KV/内存水位低于阈值」。
- **原第 2 件（时间预算反解 chunk）降级为「自己拟合」**：`cost(q, c)` 在未开源的 LB 里，仓库里没有实现（§7.2）。
  我们能抄的是它的**测量协议**（§7.4：1 warmup + 5 次 CUDA Event + 截尾均值 + 落盘缓存 + TTL），
  以及「用实测曲线做**边际吞吐扫描**选预算」这个决策形状（§7.3），而不是它的二次公式。
- **原第 3 件升级**：DSpark 的预算分配**整套是 CPU 代码**（§7.3），我们完全可以在自己的
  `Controller` / `draft_policy` 里复刻：我们已有逐位置接受率（`mtp_p_[]` + `kDecay=0.97` 的衰减计数），
  缺的是 ①`(batch → step_ms)` 曲线，②滑窗中位数估计器，③「按 `expected/time` 扫描选总预算」这一步。
  注意抄 **cumprod 版本**（GPU 修正版）而不是 CPU 的原始率排序版本——累乘才是「验证第 k 个 draft」的真实边际价值。

---

## 9. 出处

- 公告原文（微信「DeepSeek」，2026-10-20 16:54 CST）：https://mp.weixin.qq.com/s/5MEu2eeqNfBWXlkCKTd6sg
- 仓库：https://github.com/deepseek-ai/DeepInfer （Apache-2.0，created 2026-10-16，无 tag）
  与 https://github.com/deepseek-ai/deepseek-recipe （MIT，Rust）
- 本次留档（原文快照，只读）：`~/docs/references/deepinfer-2026-10-20/`
  - `DeepSeek V4.1 Flash 推理部署 Q&A.pdf`（10 页）与 `qa.txt`（其文本抽取）
  - `README.md`（`examples/README.md`，含 benchmark / sweep / YoCo 口径）
  - 代码摘录：`arg_utils.py`（Derived from vLLM; modified by DeepSeek）、`stats_logger.py`、
    `model_executor/eplb/policy.py`、`distributed/shm_broadcast.py`
- 知乎技术报告（KVCache 与负载均衡两节的出处，本次未读）：https://zhuanlan.zhihu.com/p/2095560295930508953
- 我们侧事实：`book/01-这套系统是什么.md`、`book/06-引擎启动与内存计划.md:91-101`、`book/08-前向通路与内核.md` §8.2、
  `src/program/generate.cpp`、`include/strata/spec/controller.hpp`、`src/spec/draft_policy.cpp`、
  `Strata/serve/server.py:2594-2601`；memory `strata-concurrency-limits`、`strata-ttft-prefill-queue`、
  `strata-bench-harness`、`context-count-billing-layers`
- 相关既有计划：`docs/plans/ENGINE-LEARNING-ROADMAP.md`、`docs/plans/GENERIC-MOE-ENGINE-PLAN.md`

> 说明：文中 DeepInfer 的机制、开关名与数字均取自其 README / Q&A 原文，**未在本机编译或运行验证**；
> 我们侧的 grep / 行号是本次当场核对所得。全程只读：未改配置、未重启服务、未打压测流量。
