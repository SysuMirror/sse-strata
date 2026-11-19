# 把 DeepInfer 的「变长验证预算」搬进 strata：改了什么、为什么、实测如何

- 落款：2026-10-21 13:00（北京时间）
- 前置文档：`docs/reports/DEEPINFER-ADOPTION-2026-10-20.md`（可迁移性评估与首版实现）、
  `docs/reports/DEEPSEEK-DEEPINFER-2026-10-20.md`（公告/README 层面）、
  `docs/reports/DEEPINFER-DEPLOY-QA-NOTES-2026-10-20.md`（部署可行性）
- 源码：`docs/references/DeepInfer-src` @ `774ce244`（2026-10-20）
- 本文回答四件事：**参考了 DeepInfer 的哪几段代码**、**我们的引擎到底改了什么（到行）**、
  **为什么这么改**、**为什么 DeepInfer 的其它部分搬不过来**；并给出**快道 8091 上的前后对比实验**（含两次失败与根因）。
- 现网状态（2026-10-21 13:00 起）：**快道 8091 与普道 8092 都是 `ssebed 0.1.38 build 5`**
  （`sha 4e5ce24d…`，带 `--mtp-varlen`；A 写 `logs/step-stats-a.jsonl`，B 写 `logs/step-stats-b.jsonl`）。
  实验阶段的对照状态是：A=build 5、B=`legacy build 0`（`sha 0f495a5f…`）；B 于 13:4x 切到 build 5。
- 源码存档：`archive/patches/mtp-varlen-2026-10-21/`（五个源文件全量 + README + 硬约束）；
  二进制存档：`archive/binaries/strata-ssebed-0.1.38-b5-mtpvarlen-20261021/`。

---

## 0. 结论摘要

1. **上线的是「自适应验证窗口」**：每个 chain 窗口按**逐位置接受率**与**在线实测的 (rows → step_ms) 曲线**，
   从 `L = 1..L_max` 里选期望吞吐最高的宽度，而不是固定 `L = min(4, 8/S)`。
   全部逻辑在 `--mtp-varlen` 后面，**不加参数时二进制与现网完全同路径**（零行为变化）。
2. **实测：快道三个并发档位与改造前持平（±5% 内）**，没有提速也没有回退。
   同协议、同二进制、只差一个参数的 A/B（§5.3）：
   `c=1 177.5–178.0 → 177.6–180.2`、`c=2 53.7–55.6 → 51.2–55.4（深采样 55.2）`、`c=4 28.6–28.7 → 29.4–30.6` tok/s。
3. **两版实现先后踩了同一个坑的两面**，这也是本文最有价值的部分：**草稿深度是「每轮」的，不是「每请求」的**。
   - 首版（build 3）让 L 自由变化：加宽窗口时，超出上一轮草稿深度的位置是**被清零的草稿**，
     校验必然失败 → 逐位置接受率从 `p1 = 0.54` 塌到 `0.21` → 规划器观察到低接受率后**退到 L=2 并锁死** → c=2 **−10%**。
   - 第二版（build 4）改成「按最大深度 L_max 造草稿」：`draft_batch` 的追赶行会写进**尚不该写的 cell**，
     把草稿链本身弄脏 → `p0` 从 0.83 塌到 **0.14**（S=4 时 0.05）→ c=2 掉到 37–40 tok/s。
   - 第三版（build 5，现网）只加一条约束：**每轮最多比上一轮深一格**（`L ≤ 上轮深度 + 1`，缩短不受限）。
     接受率恢复到正常水平（S=2 的 `p0/p1/p2 = 0.88/0.67/0.47`），性能回到持平。
4. **规划的「模型」是对的，收益被「每窗固定开销」吃掉**：同样的 S=2，
   `L=2` 的窗口效率 0.1915 tok/ms 比 `L=4` 的 0.1804 高 **+6.2%**，
   但端到端只兑现 +2~5%（§5.5，用 `--mtp-max-t 2` 强制 L=2 的对照臂验证）。
   窗口越短，分摊到每个 token 的**窗口外**开销（草稿图启动、staging 拷贝、调度决策）越高。
5. **TTFT 不受影响**：冷 prefill（随机 nonce，前缀缓存必然 miss）8k/32k/128k 两臂持平
   （2.81→2.55 s、9.21→9.15 s、37.47→36.94 s）；短 prompt 并发档位 TTFT 0.356→0.277 s（c=1）等也是同向或持平。
   原因很简单：`--mtp-varlen` 只改 decode 窗口宽度，不碰 prefill。详见 §5.7。
6. **DeepInfer 的其它部分搬不过来**，原因分三类：硬件/模型绑定（SM100 kernel、V4.1-Flash 路由器）、
   需要 EP/DP 多 rank 前提（EPLB、prefill 水位投票）、需要服务层重构（ZOS 零开销调度、集群 LB 未开源）。
   逐条见 §4。

---

## 1. 参考了 DeepInfer 的哪些代码

| DeepInfer 位置 | 它做什么 | 我们借走的「形状」 |
|---|---|---|
| `deepinfer/model_executor/layers/tilelang_ops/schedule_mtp_varlen.py:11-70`（`compute_seqlens_q_by_confidence_rates`） | 按逐位置置信度的 cumprod 排序，把总行数 `total_q` 分配给各请求 | **边际期望 token = cumprod(p[0..j])**，按边际收益分配预算 |
| `deepinfer/worker/model_runner.py:562-631`（`profile_decode_perf`） | 1 次 warmup + 5 次 CUDA Event 计时，掐头去尾取均值，按 (batch, seqlen) 缓存 | **先测再算**：代价表来自实测，不是拍的直线 |
| `deepinfer/engine/stats_logger.py:33-52`（`StepStats`） | 每步一行：`num_seqs / num_batched_tokens / cached / generated / verified / FLOPs` | **每个窗口一行自描述 JSONL**（我们的 `StepStatsWriter`） |
| `csrc/engine/scheduler.hpp:275-345` + `:80` 的 `GPU_CACHE_PREFILL_WATERMARK=0.9` | prefill 作为 decode 步里的一个「节拍事件」，ALL/ANY 两轮投票 | **没有采纳**（需要 DP/EP 组内 all-reduce，见 §4） |
| `deepinfer/worker/worker.py:143-144, 182-187`（ZOS） | 调度与执行零开销重叠 | **没有采纳**（服务层重构，见 §4） |

关键认识只有一句：**DeepInfer 的收益不来自某个魔法常数，而来自「用测量结果决定这一步花多少行」这个闭环**。
它的 profile 表是 `(num_seqs, num_batched_tokens)` 二维的；这一点后来证明非常关键（§5.6）。

---

## 2. 我们的引擎改了什么

改动集中在 `SSEStraBed/`（该目录不在 git 里，按 §6 方式留档），共 6 个文件：

### 2.1 新增：纯 CPU 的预算库（可单测、不碰 GPU）

- `include/strata/spec/varlen_budget.hpp` / `src/spec/varlen_budget.cpp`
  - `LinearCost{baseline_ms, row_ms}`：窗口代价 = 固定开销 + 每行代价。
  - `StepCurve`：用实测 `(rows → ms)` 点表 + 线性插值 + 末段外推，**代替直线拟合**。
  - `StepTimeEstimator`：200 个样本的环形窗，按 rows 分桶取中位数，再做 **PAVA 单调池化**
    （一个被污染的桶不会把曲线弄成非单调）。
  - `AcceptanceTracker`：逐位置 EMA（`p[j] += ema*(hit - p[j])`）。
  - `plan_uniform(rates, S, L_max, cost)`：**统一 L** 的暴力扫描（我们引擎的窗口是统一 T 行/槽，见 §4.2）。
  - `plan_varlen(rates, max_rows, cost)`：逐请求分配（已实现、已单测，**暂未接线**，见 §4.2）。
  - `tools/varlen_plan.cpp`：命令行计算器，把实测曲线直接代入，离线复算「该选 L 几」。
  - `src/spec/varlen_budget_test.cpp`：11 组单测（含「线性曲线选 L=1、次线性曲线选 L=4」、
    「PAVA 抗污染桶」），`g++ -O2 -std=c++20 -Iinclude src/spec/varlen_budget_test.cpp src/spec/varlen_budget.cpp` 即可跑，输出 `varlen_budget tests: OK`。

### 2.2 新增：每窗口统计（可观测性）

- `include/strata/program/step_stats.hpp`
  - `STRATA_STEP_STATS=<path>` 时，每个 chain/batch 窗口追加一行 JSONL：
    `{"kind":"chain","slots":S,"rows":S*L,"drafts":S*(L-1),"accepted":k,"step_ms":…,"keeps":[...],"ctx":[...]}`，
    每次 prompt 读完再写一行 `prefill`（`tokens/reused/read/step_ms/tok_s`）。
  - **`keeps` 是逐槽提交行数**——没有它，4 槽窗口看起来像一条长链，逐位置接受率根本还原不出来。
  - 未设置环境变量时 `enabled()==false`，热路径不付出代价。

### 2.3 修改：`src/program/generate.cpp`（chain 窗口的调度决策）

1. 新开关 `--mtp-varlen`（`bool mtp_varlen`，`generate.cpp:405` 声明、`:1432` 解析）。
2. `chain_step()` 里，原来 `int L = L_max;`（`L_max = min(chain_rows_per_slot, kVerifyMaxT/S)`），
   现在若 `--mtp-varlen` 且 `L_max >= 2`：
   ```cpp
   // 逐槽取逐位置接受率 → 用在线曲线估代价 → 扫 L=1..L_max 取 tok/ms 最大者
   strata::spec::LinearCost prior{12.0, 2.0, 0.0};          // 兼容先验
   strata::spec::StepCurve cost = chain_cost.curve(prior, L_max * S);
   strata::spec::UniformPlan plan = strata::spec::plan_uniform(rates, S, L_max, cost);
   L = std::clamp(plan.length, 1, L_max);
   L = std::min(L, std::min(L_max, std::max(1, draft_depth) + 1));   // ← build 5 的唯一新增约束
   ```
3. 窗口提交后：`chain_acc[k].observe(keeps[k]-1, L-1)`、`chain_cost.add(S*L, chain_ms)`、
   `draft_depth = L`；one-token 回退路径把 `draft_depth` 复位为 1。
4. 每窗口写 `step_stats.chain(S, S*L, L-1, accepted, chain_ms, keepv, posv)`。

### 2.4 为什么是 `draft_depth + 1` 这条约束（build 5 的核心）

`src/core/mtp.cpp:1185-1190` 的 `draft_batch`：

```cpp
const int max_n = std::min(std::min(L, max_t_ - 1), max_drafts_);
for (int k = 0; k < S; ++k) {
    ...
    for (int j = 0; j < n; ++j) drafts[k * L + j] = ...;   // 真正生成的草稿
    for (int j = n; j < L; ++j) drafts[k * L + j] = 0;     // 其余位置清零
}
```

也就是说：**某一轮被要求的深度是 L，就只保证 L 个位置有草稿**；`generate.cpp` 里也只把 `j < L` 的草稿
搬进 `sl.drafts`，其后清零。下一轮窗口用 `sl.drafts[j-1] (j=1..L-1)`：
窗口宽度从 `L_prev` 抬到 `L` 时，`j-1 >= L_prev` 的位置拿到的是 **0**。
token 0 几乎不可能被接受 → 表现为「加宽的那一格必然被拒」，逐位置接受率被系统性低估。

约束 `L ≤ 上一轮深度 + 1` 的妙处是：**它不产生额外的 GPU 工作**（不像 build 4 那样多造草稿），
却保证窗口用到的每一个草稿位置都是真实的。缩短完全不受限，加宽最多一格/轮
（L=2 → 3 → 4 两轮回到满宽，每轮 ~20ms，代价可忽略）。

### 2.5 默认零变化

`--mtp-varlen` 不加时：`L = L_max`、`draft_batch(S, L, …)` 与改造前逐字节同路径；
`draft_depth` 只在 varlen 分支里被读取，不影响行为。这也是 build 3/4/5 能拿「同一个二进制、只差一个参数」
做 A/B 的前提。

---

## 3. 为什么这么改（设计取舍）

1. **为什么用实测曲线而不是拟合直线**：单测 §10 就是这条结论的可执行版本——
   同一组接受率（p=0.8）、同一个「看起来合理」的线性代价 `0 + 5ms/row`，规划器选 **L=1（不投机）**；
   换成次线性曲线 `{1:7, 2:8, 4:10, 8:14}` 就选 **L=4**。
   我们真实的曲线（S=1：4 行 17.6ms、8 行 33.8ms）正是次线性的，直线拟合会把结论完全带反。
2. **为什么要 PAVA**：一次实测里出现过 `rows=6 → 27.1ms`、`rows=8 → 33.8ms` 但 `rows=6` 的样本只有 3 个，
   另有一次出现 `6 → 51ms` 的污染桶。分桶中位数 + 单调池化让「3 个样本的桶」不会把曲线掰弯。
3. **为什么要「兼容先验」`{12ms, 2ms}`**：冷启动（样本 < 20 或只有一种 rows）时曲线还没成型，
   先验必须**复现今天的固定 L**（S=1/2 → 4，S=4 → 2），否则一上线就是一次未经测量的行为改变。
4. **为什么统一 L 而不是逐请求 varlen**：见 §4.2，我们的 verify kernel 是「每槽 T 行」的稠密窗口，
   逐请求变长需要新 kernel；`plan_varlen` 已经写好、单测过，等 kernel 就绪即可接线。
5. **为什么默认还保留统计**：这次的三个结论（接受率塌陷、草稿深度、每窗开销）全部来自 `keeps`，
   没有它只能靠猜。统计在 `STRATA_STEP_STATS` 后面，关掉就是零成本。

---

## 4. 为什么 DeepInfer 的其它部分不适配

| DeepInfer 机制 | 前提 | 我们的现实 | 结论 |
|---|---|---|---|
| **DSpark 逐请求变长验证**（`schedule_mtp_varlen.py`） | varlen attention + 每请求不同 query 长度 | `ver.run_slots_chain(S, L, …)` 是**统一 T 行/槽**（`kVerifyMaxT=8`），ragged 需要新 kernel | 只借「按边际收益分配」的算法，逐请求分配暂不接线 |
| **prefill 作为 decode 节拍事件**（`scheduler.hpp:275-345`） | 单进程内 DP/EP 组，两轮 all-reduce 投票（ALL 每 70 步、ANY 每 100 步） | 我们一卡一进程，prefill 与 decode 在**同一进程的槽位调度**里，没有 all-reduce 组 | 不采纳；我们的等价物是槽位抢占 + 前缀缓存（`reused` 常年 90%+） |
| **ZOS 零开销调度**（`worker.py:143-187`） | 调度线程与执行线程稳态交接（baton） | 我们是单进程同步窗口；chain 路径是 `run_slots_chain` 后同步提交 | 要做等于服务层重构，收益与风险不对等 |
| **EPLB 专家并行负载均衡** | 多 rank EP 组 + 专家分片 | 单卡，专家是 **10.6 GiB / 6427 槽的 LRU 缓存**（`expert cache 68%` 命中） | 问题不同：我们优化的是命中率，不是分片均衡 |
| **SM100 tilelang / deep_gemm kernel** | SM100 / 昇腾 950 | 3×RTX 4090D（SM89），24564 MiB/卡，`vram_free 1183 MiB` | 代码级不可用 |
| **V4.1-Flash 的 MoE 路由/专家数/MTP 头** | 那个模型 | 我们跑的是 `qwen3.8-flash-next-iq3_xxs`（IQ3_XXS） | 只借方法，不借常数 |
| **集群级负载均衡 / 配额** | — | 公告里最响的部分**没有开源**（`deep_cluster/` 只有设备/NUMA 发现） | 无可借 |

一句话：**能搬的是「决策形状」，搬不了的是「多 rank 前提」和「SM100 kernel」**。

---

## 5. 实验：快道 8091 的前后对比

### 5.1 协议（可复现）

- 测量器 `bench/deepinfer/profile_decode.py`：固定 prompt（命中前缀缓存）、`temperature=0`、
  `stream` + `include_usage`，用 **`usage.completion_tokens`** 计 token；
  每档 1 次 warmup + 9/24 次计时；报 **每请求 decode tok/s**、**墙钟聚合 tok/s**、**TTFT**。
- A/B 驱动 `bench/deepinfer/ab_lane.py`：跑完一轮后回读**另一条道 8092 的 /metrics**，
  把落在测量窗口内的请求数与输出 token 记为**协变量**（两卡同宿主机 CPU 池，B 的 prefill 会污染 A）。
  原始数据在 `bench/runs/ab/*-with-load.json`。
- 引擎侧 `analyze_steps.py` 读 `STRATA_STEP_STATS`，输出 `(S,L) → n/中位 step_ms/tok per window/逐位置接受率`。
- **快道重启后的第一轮一定偏慢**（专家 arena 冷、页缓存冷）：所有表里的「baseline 第一轮」（c=1 127 tok/s）
  都是冷启动样本，已剔除，用 warmup 之后的轮次。

### 5.2 结果总表（tok/s per request；括号内为聚合 tok/s 墙钟）

| 引擎臂 | c=1 | c=2 | c=4 | B 道负载 |
|---|---|---|---|---|
| 改造前（prod build 1，L=4@S2） | **178.0 / 177.5** (59.7/58.5) | **53.7 / 55.6** (57.4/56.1) | **28.6 / 28.7** (62.9/59.1) | 3–4 请求 / 1.4k tok |
| build 3（L 自由变化，首版） | 176.8 / 175.3 (69.5/65.5) | 55.1 / 48.8 / **49.7**(n=48) (55.0/51.7/53.4) | 31.0 / 28.6 (71.2/59.0) | 3 / 1.1–1.3k tok |
| build 4（按 L_max 造草稿） | 181.2 / 177.7 (66.3/57.5) | **37.1 / 40.1 / 50.8**(n=48) (45.4/46.3/55.7) | 25.0 / 25.5 (54.4/57.3) | 7 / 2.7k，1 / 1.7k |
| **build 5（深度斜坡，现网）** | **180.2 / 177.6** (70.1/69.8) | **51.2 / 55.4 / 55.2**(n=48) (60.6/56.8/58.3) | **30.6 / 29.4** (64.0/63.1) | 2–4 请求 / 0.4–1.0k tok |
| 对照：强制 L=2（prod + `--mtp-max-t 2`） | 138.5 (63.5) | **58.5 / 54.6**(n=48) (59.2/58.7) | 30.8 (63.5) | 0 / 0（B 空闲） |

读法：

- **c=1：L=4 明显优于 L=2（178 vs 138.5，−22%）**。自适应规划器在 S=1 大量选 L=3，
  端到端与 L=4 持平（177.6–180.2），说明 L=3 在 S=1 是等价选择。
- **c=2 是唯一能区分「变 L」与「固定 L」的档位**：固定 L=2 最快（54.6–58.5），固定 L=4 为 53.7–55.6，
  自适应（build 5）为 51.2–55.4 —— **持平，最多 +2~5%**。
- **c=4：两边都是 L=2，差异在噪声 + B 道负载里**（B 空闲时 30.8，B 忙时 28.6–28.7）。
- **build 3 / build 4 的 c=2 回退是真实的**，且都能在引擎侧统计里定位（§5.4）。

### 5.3 为什么必须看 B 道的负载

两卡各跑一个进程，但 **prefill/采样线程共用宿主 CPU 池**。用 `--mtp-max-t 2` 那次 B 道**完全空闲**，
同一个引擎的 c=4 从 28.6 涨到 30.8（+7.7%）；而 c=2 的 58.5 也包含这份「环境红利」。
所以本文不把 58.5 与 53.7 直接相减当成收益；结论按「B 负载相近」的臂对比（§5.2 末列）。

### 5.4 两次失败的根因（引擎侧证据）

`(S=2, L=4)` 窗口的**逐位置接受率**（`keeps` 还原）：

| 引擎臂 | p0 | p1 | p2 | 窗口中位耗时 |
|---|---|---|---|---|
| 改造前（固定 L=4，不跨深度） | 0.83 | **0.54** | 0.49 | 31.2 ms |
| build 3（L 自由变化） | 0.79 | **0.21** | **0.18** | 31.5 ms |
| build 5（深度斜坡） | 0.88 | **0.67** | 0.47 | 40.5 ms（仅 29 窗，多在批次切换处） |

- **build 3**：`p1/p2` 塌掉 = 加宽窗口校验了**被清零的草稿**（§2.4）。
  规划器看到 p1/p2 低 → 预期 token 变少 → **选更短的 L** → 更不可能回到宽窗口 → 自锁。
  统计上 S=2 有 62% 的窗口是 L=2、只有 17% 是 L=4；客户端 c=2 因此 **−10%**。
- **build 4**：把 `draft_batch` 的深度改成 `D = L_max`，并用重复 token 填充超出窗口的行。
  `mtp.cpp:1163-1177` 的追赶行会把**接受位置之后的 cell 也写一遍**，草稿链读到被污染的 cell →
  **p0 从 0.83 塌到 0.14**（S=4 时 0.05），c=2 掉到 37–40。这个方向被数据否掉了。
- **build 5**：只加约束、不改 `draft_batch`，接受率回到正常（p0 甚至略高），c=2 回到持平。

### 5.5 「模型对、收益小」的量化

同一引擎、同一时段的窗口效率（tok per window ÷ 中位 step_ms）：

| 窗口（S=2） | 期望 token/窗 | 中位耗时 | tok/ms |
|---|---|---|---|
| L=4（改造前） | 5.62 | 31.17 ms | 0.1804 |
| L=2（build 5 实测） | 3.47 | 18.14 ms | **0.1915（+6.2%）** |
| L=3（build 5 实测） | 4.50 | 24.42 ms | 0.1844（+2.2%） |

窗口层面 L=2 确实更划算，但端到端只兑现 +2~5%：
**短窗口的「窗口外」开销更频繁**（草稿图启动、`row_stage` 拷贝、`BT` 行输出与 `fflush`、调度决策）。
这也是 DeepInfer 用 **CUDA Event 在进程内**计时、而我们只能量到墙钟的原因——口径差就在这段。

### 5.6 顺带得到的两条结构结论

1. **代价不是 `rows` 的一维函数**：同一时段 `S=2, rows=8` 中位 31.2–37.2ms，
   而 `S=4, rows=8` 只有 29.8–31.7ms —— **同样的行数，槽位多的反而更快**。
   DeepInfer 的 profile 表本来就是 `(num_seqs, num_batched_tokens)` 二维的，我们的 `StepCurve` 只用了 rows，
   这是下一步要补的维度（`StepCurve` 改成按 (S, rows) 分桶即可，接口已留函数式代价）。
   2026-10-21 13:3x 的统计（`analyze_steps.py logs/step-stats-a.jsonl`，6367 窗）给了更刺眼的一行：
   **`rows=3` 中位 38.16 ms，而 `rows=4` 只要 19.06 ms** —— 同样的"一维行数"完全无法解释，
   因为 3 行与 4 行落在不同的 (S, L) 组合上。按 rows 分桶的曲线在这种非单调处会把预算算歪。
2. **前缀缓存把 prefill 从瓶颈里拿掉了**：`prefill: 56 prompts, read=354, reused=4070, reuse=92%`，
   单次 prompt 读入中位 ~210ms（79 token 里只有 5 个是新读的）。这也解释了为什么 TTFT 主要被批次排队支配。

### 5.7 TTFT：长 prompt 的冷 prefill 前后对比

工具：`bench/deepinfer/ttft_probe.py`（本次新增）。用**原生 tokenizer** 精确构造 8k/32k/128k 的 prompt，
在**最前面**插入随机 nonce，使前缀缓存必然 miss（`usage.cached_tokens = 0` 已验证），
流式请求量首个 `content` chunk。每档 2 次（128k 为 1 次，单次约 37 s），取中位数。
两臂在同一张卡（A/GPU0）、同一天、同一份语料、同样的 `max_tokens=8 / temperature=0 / reasoning none`，
中间只换引擎二进制（均为重启后先 warmup 再测）。

| prompt 长度 | build 1（改造前） | build 5（varlen） | 差 |
|---|---|---|---|
| 8.0k | **2.81 s**（2.82 / 2.81） | **2.55 s**（2.57 / 2.52） | −9.3% |
| 32k | **9.21 s**（9.12 / 9.30） | **9.15 s**（9.07 / 9.24） | −0.7% |
| 128k | **37.47 s** | **36.94 s** | −1.4% |

短 prompt（~30 token，前缀缓存命中）下各并发档的 TTFT，取自 §5.2 同一批 A/B 运行：

| 并发 | 改造前 | build 5 |
|---|---|---|
| c=1 | 0.356 / 0.365 s | 0.277 / 0.277 s |
| c=2 | 0.541 / 0.557 s | 0.469 / 0.483 s |
| c=4 | 0.945 / 0.990 s | 0.899 / 0.893 s |

读法：

- **长 prompt 完全持平**。这与设计一致：`--mtp-varlen` 只决定 decode 窗口宽度，不进 prefill 路径；
  8k 那档 −9% 不能当收益看（单次 2.5–2.8 s 的抖动本身就 ±0.2 s，且首轮受页缓存/专家 arena 影响）。
- 短 prompt 档 build 5 低 10–20%，但 baseline 那两次跑的是 build 3（无 varlen）且当时 B 道更忙
  （协变量 3–4 个请求 vs build 5 的 2 个）。**结论只能说到「varlen 没让首 token 变慢」**，不宣称收益。
- 冷 prefill 的绝对量级本身是重要事实：**128k 新内容要 37 秒**，「长文档第一句话很慢」就来自这里；
  同一份内容第二次读（命中前缀缓存）只要 0.4 s 量级。这也再次说明瓶颈在 prefill，不在 decode 窗口。
- 复现：`bench/runs/ttft/{build1,build5}-{small,128k}.json` 与两份 `arm-*.log`。

---

## 6. 上线、版本与回滚

| 项 | 值 |
|---|---|
| 现网二进制（两条 lane 同一份） | `~/deploy/engines/bin/mtpvarlen-20261021d/strata` |
| 版本 / 构建号 | `ssebed 0.1.38`，`build 5`，`sha256 4e5ce24dac34a8dcc9c5adb34eda03c87d46ae75d7654a8794a7f3710bfbdea6` |
| BUILD.json | 同目录（`source: local + --mtp-varlen + STEP_STATS + draft-depth ramp`） |
| A 道 profile | `~/deploy/engines/profiles/ssebed/strata-a.json`：`exe → …21d/strata`、`args + --mtp-varlen`、`env STRATA_STEP_STATS=/home/cloud/logs/step-stats-a.jsonl` |
| B 道 profile | `…/strata-b.json`：同样 `exe + --mtp-varlen`，另补上原先缺的 `fit_max_tokens: true`、`env …step-stats-b.jsonl` |
| 切换方式 | A：`supervisorctl restart strata-a`；B：`switch_engine.py switch ssebed --lanes b --yes`（drain → stop → start → 等 `/health` → 1-token 冒烟，失败自动回滚） |
| 冷启动成本 | **6–10 分钟**：专家 arena ~41 GiB 从盘重建；本次实测 0.07–0.09 GiB/s（宿主内存 216 GiB 已用 187、page cache 被清空），A 道两次分别 8:24 / 9:07 |
| 停机影响 | 切换期间该 lane 的请求由网关**自动改投另一条 lane**（2026-10-21 新增，见下），网站不再 502 |

**本次顺带修的网关缺陷**（`rag-proxy/rag_proxy.py`）：`_proxy()` 以前上游连不上就
`502 rag proxy: upstream failed`。现在 `except URLError` 分支在**尚未写客户端任何字节**时
调用 `failover_upstream()` 换到另一条活着的 lane 重试一次（`failover=False` 防循环；
`upstream_alive()` 有 2 秒探测缓存）。已验证：隔离实例指向死端口 → 自动改投；切 B 期间
向 `qwen3.8-flash` 发请求 → 日志 `改投 8091`、1.16 s 返回。计数 `upstream_failover`。

**普道（8092）切到 build 5 之后的冒烟**（2026-10-21 13:55–14:10，`profile_decode.py`，32 token，同协议）：

| 时刻 / 工况 | c=1 每请求 tok/s | SSE chunk 间隔 | TTFT |
|---|---|---|---|
| 刚切完、有真实用户会话在跑 | 16.8 | 60 ms | 0.31 s |
| 与用户长上下文请求并发（c=2 档） | 18.3 | 55 ms | 10.6 s（prefill 排队） |
| 用户会话结束、空闲后重测 c=1 | 115.3 | — | 0.30 s |
| 同刻的快道 A（空闲） | 78.0 | 0.2 ms | 0.30 s |

结论：慢的那两档**不是 build 5 的回退**，是批槽路径本身的行为。引擎统计给出了直接证据：
B 道当时写出的是 `{"kind":"batch","slots":2,"rows":2,"drafts":0,...}` —— 有两个槽在跑时走
batch 路径、**不做 MTP 草稿**，一步 ~36 ms；A 道同时段写的是 `{"kind":"chain","drafts":4,...,"step_ms":30}`
（链式批槽 + 草稿）。这就是已知问题「批槽无 MTP」的现场样本，改造前后都存在，与本次变长预算无关。

回滚（一次配置改动 + 一次重启）：

```bash
python3 - <<'PY'
import json; p='/home/cloud/deploy/engines/profiles/ssebed/strata-a.json'
c=json.load(open(p)); c['exe']='/home/cloud/SSEStraBed/build/strata'
c['args']=[a for a in c['args'] if a!='--mtp-varlen']; c.pop('env',None)
json.dump(c,open(p,'w'),indent=2,ensure_ascii=False)
PY
supervisorctl -c /home/cloud/deploy/supervisord.conf restart strata-a
```

- `~/SSEStraBed/build/strata` 已恢复为**生产二进制**（`build 1`，`sha 3d3f3c9d…`），BUILD.json 同步；
  仓库 `archive/binaries/` 留有 `strata-ssebed-0.1.38-b1-20261018` 原件。
- 配置文件备份：`deploy/engines/backup/strata-a.json.before-mtpvarlen-20261021`、
  `…before-ab-novarlen-20261021`（mode 600，含明文密钥，**不要外传/提交**）。
- 历史二进制：`deploy/engines/bin/mtpvarlen-20261021`（b2）、`…b`（build 3）、`…c`（build 4）、`…d`（build 5）。

---

## 7. 观测怎么用

```bash
# 每窗口统计（快道已开）
python3 ~/bench/deepinfer/analyze_steps.py ~/logs/step-stats-a.jsonl
#   → (S,L) 分桶的 n / 中位 step_ms / tok per window / 逐位置接受率 + 可直接喂给规划器的曲线串

# 把实测曲线代入，离线复算「该选 L 几」
~/SSEStraBed/build/varlen_plan --curve '4:18.1,6:24.4,8:30.6' --rates 0.88,0.67,0.47 --max-len 4 --max-rows 8
```

- 统计体量：约 **150 B/窗口**；本次实验 2.5 小时共 ~0.9 MB。快道访问量小，长期开无压力；
  若将来压满，`truncate -s 0` 或按天切分即可（writer 以 `ab` 追加，不持有句柄语义）。
- **外部观测替代不了引擎内计时**：SSE 的 text chunk 会被合并发送（一个 chunk 可能含多个 token），
  `chunk_gap` 不是 step 时间；batch 槽的 `/metrics` 也没有逐槽窗口耗时。要做「为什么慢」的归因，只能看 `STRATA_STEP_STATS`。

---

## 8. 结论与下一步

**结论**：自适应验证窗口**与固定 L 持平**，机制正确、无回退、可回滚；
它真正的价值有两个：(a) 流量异质时（真实用户、采样、代码补全）接受率掉下来时自动收窄窗口，
而不是像现在这样一路 L=4 硬扛；(b) 每窗口统计把「慢」从日志猜测变成数据。
**按实测它没有提速**，扩到普道 8092 的依据是「无回退 + 统一版本便于观测/维护」，不是「更快」——
两条 lane 现在跑同一份二进制、同一套统计，排障时不用再记两套行为。

**下一步（按性价比排序）**：
1. **把代价曲线升级成二维 `(S, rows)`**（§5.6-1）。这是本次最硬的结构发现，改动小、可离线验证。
2. **量出「窗口外开销」**：在 `chain_step` 入口记时间戳，把 `bt_run`（窗口内）与墙钟（窗口外）分开统计，
   再决定短窗口到底该不该选——现在规划器只看见窗口内的 18ms。
3. **逐请求 varlen**（`plan_varlen` 已就绪）：需要 varlen verify kernel（每槽不同 T），
   这是把 +6% 变成 +15% 的唯一路径，但工作量大、要动 kernel。
4. **把 `keeps` 接进 `/metrics`**（逐位置接受率的滑动值），这样在线就能看出接受率是否在退化。

---

## 附录：本次新增/修改文件清单

```
SSEStraBed/include/strata/spec/varlen_budget.hpp        新增  预算库接口（LinearCost/StepCurve/Estimator/Tracker/plan_*）
SSEStraBed/src/spec/varlen_budget.cpp                   新增  实现（PAVA 单调池化、暴力扫描、逐请求分配）
SSEStraBed/src/spec/varlen_budget_test.cpp              新增  11 组 CPU 单测
SSEStraBed/tools/varlen_plan.cpp                        新增  离线计算器
SSEStraBed/include/strata/program/step_stats.hpp        新增  每窗口 JSONL writer（含 keeps）
SSEStraBed/src/program/generate.cpp                     修改  --mtp-varlen、深度斜坡、stats 接线
SSEStraBed/CMakeLists.txt                               修改  三个新 target 挂到 strata_spec / build
bench/deepinfer/profile_decode.py                       新增  外部测量（usage 计 token）
bench/deepinfer/ab_lane.py                              新增  A/B 驱动 + B 道负载协变量
bench/deepinfer/analyze_steps.py                        新增  统计解析（(S,L) 分桶 + 逐位置接受率）
deploy/engines/bin/mtpvarlen-20261021{,b,c,d}/          新增  b2 / build3 / build4 / build5 二进制 + BUILD.json
deploy/engines/profiles/ssebed/strata-a.json            修改  exe / --mtp-varlen / STRATA_STEP_STATS
```