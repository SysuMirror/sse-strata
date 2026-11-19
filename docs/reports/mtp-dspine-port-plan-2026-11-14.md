# DSpine → 我们的 MTP：机制拆解、差距与分阶段改造方案

- 日期：2026-11-14（北京）
- 上游：`Tencent/DSpine` @ `d211869`（本机 `~/archive/upstream/DSpine`，3.9 MB，纯 Python + Triton）
- 本机引擎：`SSEStraBed-engine`（Strata 0.1.38），线上模型 `Qwen3.8-Flash-Next` IQ3_XXS
- 状态：**评估 + 方案，未改任何生产代码**

## 0. 结论先行

1. DSpine 不是推理开关，是**草稿模型的架构 + 训练范式**：块并行起草（16 行一块：1 个已知 anchor + 15 个 mask 位置）
   + 每层把前驱位置的预测特征注入后继位置（深度方向传递因果依赖）+ 选词侧的转移表。
2. 我们的 MTP 是**1 层、串行链、按真实 token 训练**的 DeepSeek 式 MTP；DSpine 是 **5 层、块并行、按 mask token 训练**的
   独立草稿模型。两者只共享"草稿-验证"的外壳。
3. 内核侧并非不可行：多行 forward（`record_forward(T)`）、块内因果注意力（`qsa_decode_attn_batch` 按 step 的
   `n_kv`）、KV 回滚（`kv_restore`）、批量草稿采样都已存在。**真正的缺口在训练与调度**，不是 CUDA 能力。
4. 建议按 M0→M3 分阶段：先做**不训练的块并行基线**（测出草稿阶段能省多少、接受长度掉多少），再决定是否投入训练。

## 1. DSpine 代码拆解

### 1.1 块并行前向
- 草稿 = `DFlashDraftModel` 的 5 个 decoder layer，`block_size=16`、滑窗 8192、块内因果注意力
  （`sglang_dspine/models/dspine.py:388`、`layers/attention.py`）。
- 一块 16 行：第 0 行是**已知 anchor token**，1..15 行是 mask 位置（`mask_token_id=248070`）。
  输入 embedding 直接取**目标模型**的 embedding（`input_embeds`，`models/dspine.py:474`）。
- 注意力是块内因果 + 目标上下文，位置编码沿用目标模型的 RoPE；serving 时可把 block 调小
  （`set_block_size`，2..训练值，`models/dspine.py:439`）。

### 1.2 相邻因果注入（code bridge，核心）
每层（`selected_prefix_feature_layers=[1..5]`，即全部 5 层）decode 完做一次读-门控-写（`models/dspine.py:258-352`）：

1. **读码**（`kernels/dspine.py:12-57`）
   - `raw = hidden (+residual)`，`rms = ‖raw‖/√H`
   - `projected = RMSNorm(raw) · W_read[layer]`（`code_size=1024`）
   - anchor 行（`row % 16 == 0`）直接查 **codebook[anchor_token]**（归一化后的目标 embedding 低维码）；
     其余行用 `√C · projected/‖projected‖`
2. **门控**（`feature_message`）：`gate = sigmoid(query_self + sender_prev + bias)`，
   `message = gate ⊙ value_prev`；anchor 行 message=0。sender/value 来自**前一个位置**的预测码（`prev`/`predecessor_gate` 投影）
3. **写回**（`feature_finish`）：`delta = message · W_write[layer]`（hidden×rank），
   `out = raw + rms·delta`；非块首行才写（edge 判断）；随后 RMSNorm 给下一层
- 训练细节（checkpoint config）：`rank=512`、`head_topk=16`、GT token 注入按 block-bernoulli 退火
  （`zero_fraction=1/3, peak_probability=0.5, hold_fraction=1/6`）、目标/embedding/LM head 冻结。

### 1.3 选词侧：serial last-write refinement + 转移表
- 每位置保留 top-K=16 候选；前驱选定后，用它的**精确码**重做最后一层的写，修正后继候选分数
  （`code_bridge_second_pass: {mode: serial_table, first_exact: 4}`）。
- 用 FP64 因子分解把"前驱候选 × 后继候选"的 16×16 分数表**提前算好**（`sampling/factorized/kernels.py:179 factor_table`），
  顺序选词时只查表，不再做矩阵乘。

### 1.4 验证侧
- DSpark 式**动态验证宽度**：草稿固定 16，验证宽度按 SPS 表/置信度调；8 路并发时在请求间分配验证位置
  （`runtime/worker.py:766`、`docs/sps_tuning.md`、`scripts/tune_sps.py`）。
- 混合注意力（线性层有递归状态）用 ReplaySSM 在"只接受前缀"后对齐状态（`runtime/replayssm/`）。

## 2. 我们的 MTP 现状（`SSEStraBed-engine`）

| 维度 | 现状 | 位置 |
|---|---|---|
| 权重 | 官方 Flash-Next 的 `mtp.*`，**仅 1 层**（`mtp.layers.0.*`），hidden 2560，512 routed experts + shared expert，hyper-connection(HC=4)，indexer | `Strata-data/mtp/mtp-inventory.json` |
| 草稿方式 | **串行链**：每 token 一次 `cudaGraphLaunch` + host 同步，采样出的 token 喂回下一步 | `src/core/mtp.cpp:1086 draft()`、`:758 capture_step(j)` |
| 多行能力 | `record_forward(T, ...)` 支持 T 行；但 full layer 目前**只允许在 batched chain（每行属于不同槽）**里多行 | `src/core/mtp.cpp:record_forward` |
| 注意力 | `qsa_decode_attn_batch`，每行按 `step[]` 的 `n_kv` 做因果注意力 → 同一槽内 0..j 的块内因果**天然支持** | 同上 |
| 回滚 | `kv_restore(upto)` 已有（草稿被拒后回退） | `include/strata/core/mtp.hpp:103` |
| 验证 | `run_slots_chain`，`kVerifyMaxT=32`；每槽一条 L 行链 | `include/strata/kernels/verify_kernels.hpp:32` |
| 调度 | `sched.decode_rows_per_slot(S)` + `--mtp-varlen`（统一 L，按边际接受率扫描） | `src/program/generate.cpp:5909` |
| 训练 | 无本地训练管线；MTP 权重来自上游 checkpoint | — |

## 3. 差距矩阵

| 能力 | DSpine | 我们 | 差距类型 |
|---|---|---|---|
| 块并行起草 | 16 行/块，一次前向 | 串行，逐 token 图 | **调度 + 模型（需训练）** |
| 深度条件注入 | 5 层，每层 bridge | 1 层，无 bridge | **模型（需训练）** |
| 选词转移表 | top-16，预计算表 | 每步采样 | 可选，依赖 bridge |
| 动态验证宽度 | 有（SPS） | varlen 雏形（统一 L） | 调度，可独立做 |
| 训练数据 | 目标 hidden + 真实 token | 引擎 `prefill(R_rows)` 已有目标 hidden 入口 | 需离线 dump 管线 |

## 4. 分阶段方案

### M0：块并行草稿管线（不训练，先测基线）
- 新增运行时开关 `--mtp-block T`（默认关）：每槽预分配 T 个 cell；anchor 行=当前已接受 token，
  1..T-1 行喂 mask id（用我们词表里的占位符，如 248077 `[PAD248077]`，**不要用 248070**，那是 `<|audio_start|>`）
- 一次 `record_forward(T)` 写全部行 KV，再按行做块内因果注意力；草稿采样批量出 T-1 个 token
- 验证沿用 `run_slots_chain`（L=T，`kVerifyMaxT=32` 够）；被拒后 `kv_restore`
- 验收：草稿阶段 ms/轮、接受长度、端到端 tok/s；**预期接受长度下降**（MTP 未见 mask token），
  但草稿阶段耗时应显著下降（专家读取从 T 次摊销成 1 次）

### M1：桥接模块（小训练，冻结 MTP）
- 在 MTP 层后加 DSpine 式 bridge（gate/prev/predecessor/out + codebook + per-layer read/write）
- **零初始化 delta** → 训练前行为等价于现 MTP，风险可控
- 只训练 bridge（+可选 LoRA），目标 hidden 从引擎 dump，目标模型冻结
- 前置：确认 HC=4 的 4 条残差流如何映射到 bridge 的 hidden（DSpine 是单流）

### M2：深度（1 层 → 3~5 层）
- 复制现有 MTP 层做权重初始化（weight-tied 或 untied），逐层加 bridge；或在块内多遍复用同一层（Universal-Transformer 式）
- 这是 DSpine 收益的主体；M0/M1 已把管线与 bridge 打通

### M3：选词与验证
- last-write refinement + top-K 转移表（只有 bridge 训好后才有意义）
- 动态验证宽度：`--mtp-varlen` 从"统一 L"扩展到"每槽一个 L"（`batch_chain_` 按 L 分桶）

## 5. 代码触点

- 草稿：`src/core/mtp.cpp`（`draft`/`capture_step`/`record_forward`/`capture_prefill`）、`include/strata/core/mtp.hpp`
- 调度：`src/program/generate.cpp`（`chain_step`、`--mtp-varlen`、`sched.decode_rows_per_slot`）
- 验证：`src/kernels/cuda/verify_kernels.cu`、`include/strata/kernels/verify_kernels.hpp`
- 策略：`src/spec/draft_policy.cpp`、`src/spec/controller.cpp`、`src/spec/varlen_budget.cpp`
- 训练数据入口：`MtpDrafter::prefill(const float* R_rows, ...)`（目标 hidden 已在这里）

## 6. 风险与硬约束

- **训练算力**：bridge 训练要和线上服务共享 GPU；需专门窗口（先停邻居 TTS 可腾 ~6.4 GB，见 memory）
- **停服红线**：换二进制 = 重启，必须先经用户同意
- **OOD**：MTP 未见 mask token，M0 的接受长度可能明显掉；这是预期结果，不是失败
- **QSA/indexer**：16 行块下 indexer 的选择行为未验证，需要先做单测
- **验证宽度**：L=16 时调度/带宽代价要重测（对照已有 P2 结论：窗口加宽 NO-GO，必须和边际接受一起算）

## 7. 待决策

1. 先做 M0（不训练，验证管线与收益形状），还是直接 M1（需要训练窗口）？
2. 训练窗口怎么排（夜间 / 停邻居 TTS / 单独卡）？
3. block 大小取 8 还是 16（我们 `kMaxT=8` 的策略与 `kVerifyMaxT=32` 能力之间的取舍）。