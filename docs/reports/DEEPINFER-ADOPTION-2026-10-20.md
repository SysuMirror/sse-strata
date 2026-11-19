# 从 DeepInfer 借走三件事：在线代价曲线、逐位置投机预算、每步统计

- 落款：2026-10-20 23:44（北京时间）
- **后续（2026-10-21）：本文的上线方案已执行，快道 8091 现为 build 5（深度斜坡版），实测与改造前持平。**
  改动明细、两次失败实现的根因、完整 A/B 数据见 **`docs/reports/DEEPINFER-VARLEN-2026-10-21.md`**。
- 上游调研：`docs/reports/DEEPSEEK-DEEPINFER-2026-10-20.md`（公告/README 层面）、`DEEPINFER-DEPLOY-QA-NOTES-2026-10-20.md`（部署可行性）
- 源码：`docs/references/DeepInfer-src` @ `774ce244`（2026-10-20）、`docs/references/deepseek-recipe-src` @ `935817c6`
- 本文回答三件事：**DeepInfer 到底有什么能抄**、**我们抄了什么（可运行的代码）**、**实测数据说明它值不值得上线**

---

## 0. 结论摘要

1. **DeepInfer 不能直接跑**：只支持 SM100 / 昇腾 950 + DeepSeek-V4.1-Flash；本机是 3×4090D（SM89）。它不是替换件，是设计参考。
2. **公告里最响的「集群调度/负载均衡」没有开源**：`deep_cluster/` 只有设备与 NUMA 发现，`compute_quota`/`cost(q,c)` 只存在于文档；
   能读到的调度实现都在**单进程 batch 内**（prefill/decode 步级投票、DSpark 变长验证）。
3. **可迁移的是三个「决策形状」**，已按我们引擎的约束重写并编译通过：
   - 用**实测曲线**（不是直线）估计 `(rows → step_ms)`；
   - 按**边际接受率**（cumprod）给每个并发请求分配验证行数；
   - 每个窗口写一行**自描述统计**（StepStats），把「为什么慢」从日志猜测变成数据。
4. **默认零行为变化**：新逻辑全部在 `--mtp-varlen` 后面，统计在 `STRATA_STEP_STATS` 后面；不加参数时二进制与现网完全同路径。
5. **实测结论有两面**：
   - 真实接受率中位 **0.76–0.80**，分布相当均匀（0.64–0.89），在 4 并发、8 行预算下逐请求分配只多 **+0.8%~+2.1%** 期望 token；
   - 接受率差异大时（0.92/0.88/0.55/0.45）同样的 8 行能多 **+9.2%**；所以它是**分布自适应**的收益，不是固定提速。
   - **代价曲线的形状比分配算法更重要**：同一组接受率，次线性曲线选 `L=4`，线性曲线选 `L=1`（见 §5.5）。
     这直接说明为什么 DeepInfer 用 profile 表而不是拟合直线。
6. **尚未上线**：换引擎 = 重启，按红线需要用户同意。上线方案、观测口径、回滚步骤见 §6。

---

## 1. 仓库里真实存在的东西（按可迁移性排序）

| DeepInfer 机制 | 源码位置 | 我们能用吗 | 处理 |
|---|---|---|---|
| **DSpark 变长验证**：按置信度 cumprod 分配 `total_q` 预算 | `deepinfer/model_executor/layers/tilelang_ops/schedule_mtp_varlen.py:11-70`；开关 `DEEPINFER_ENABLE_MTP_VARLEN` | 能：我们的 batch chain 已有「整组统一 L」和 8 行上限 | **已实现** `plan_uniform`/`plan_varlen` + `--mtp-varlen` |
| **profile_decode_perf**：1 warmup + 5 次 CUDA Event，去一高一低取均值，落盘缓存 | `deepinfer/worker/model_runner.py:562-631` | 部分：我们没有进程内 CUDA Event 钩子 | **已实现** 在线 `StepTimeEstimator`/`StepCurve`；外部测量脚本复刻协议 |
| **StepStats**：每步 rows/ctx/verified/generated 等自描述字段 | `deepinfer/engine/stats_logger.py:33-52`；调用方 `engine/llm_engine.py` | 能：我们只有请求结束时的聚合行 | **已实现** `StepStatsWriter`（JSONL，可选） |
| **prefill 作为节拍事件**：ALL 点每 70 步、ANY 点每 100 步，缓存水位 0.9，二次 all-reduce 防空 prompt | `csrc/engine/scheduler.hpp:275-345`（常量 `:80`） | 结构能借，机制不能：我们没有 DP rank 与 all-reduce | 只做分析，见 §7.1 |
| ZOS 异步后处理 | `deepinfer/worker/worker.py:143-144,182-187` | 需要引擎有「输出后处理线程」这一层 | 未做，列入 §8 |
| EPLB 专家重平衡 | `deepinfer/engine/...`（依赖 EP 分组） | 我们是单机专家缓存 + LRU | 不适用 |
| SWA bounded replay / YoCo | V4.1 专用，保留 140 token 后缀 | 我们没有 SWA 层 | 不适用 |
| 结构化输出（xgrammar 0.2.2） | `vendor/` | 与本次目标无关 | 不适用 |

> 一句话：**能抄的不是它的分布式，而是它「先测量、再按边际收益分配预算」的工作方式。**

---

## 2. 我们改了什么（代码地图）

### 2.1 纯 CPU 的预算库（可单测、不碰 GPU）

- `SSEStraBed/include/strata/spec/varlen_budget.hpp`
- `SSEStraBed/src/spec/varlen_budget.cpp`

```cpp
struct LinearCost { double baseline_ms, row_ms, sampler_ms; double ms(int rows) const; };
struct StepCurve { std::vector<std::pair<int,double>> pts; double ms(int rows) const; };  // 桶中位数 + 插值 + 末段外推
class StepTimeEstimator { void add(int rows,double ms); bool ready(); LinearCost fit(prior); StepCurve curve(prior,max_rows); };
class AcceptanceTracker { void observe(int accepted,int drafted); double rate(int pos); };
UniformPlan plan_uniform(rates, slots, max_length, cost);   // 整组一个 L（当前校验器可用）
VarlenPlan  plan_varlen (rates, max_rows, cost);            // 逐请求长度（等逐槽变长）
```

要点：

- 期望 token = `1 + Σ cumprod(p[0..j])`，与 DSpark 的 marginal acceptance 同形；
- `plan_varlen` 把 `(序列, 位置)` 的 cumprod 降序排列，只接受**前缀合法**的项，在行数预算内取 `期望/耗时` 最大的方案；
- `StepCurve` 是**分桶中位数表**，不是直线。`StepTimeEstimator` 每档至少 2 个样本才采用，否则退回先验直线。

### 2.2 引擎接线（默认关闭）

- `SSEStraBed/src/program/generate.cpp:405` 新增 `bool mtp_varlen`
- `:1432` 解析 `--mtp-varlen`
- `:5754` 每个 `chain_step` 持有一个 `StepTimeEstimator`
- `:5768-5785` 每个窗口：收集各槽逐位置接受率 → 估代价曲线 → `plan_uniform` → 取 `L`
- `:5846-5849` 窗口结束后用真实 `(S*L, chain_ms)` 回填估计器，并用接受结果更新 tracker
- `:5706` / `:5859` 每个 batch/chain 窗口写一行统计
- `:6575` 每次 prompt 读完写一行 prefill 统计

### 2.3 统计写入器

- `SSEStraBed/include/strata/program/step_stats.hpp`

仅在 `STRATA_STEP_STATS=<path>` 时打开，JSONL 追加，热路径无分配：

```json
{"t":1792510000123,"kind":"chain","slots":4,"rows":8,"drafts":1,"accepted":3,"step_ms":39.8,"ctx":[51200,50110,49880,53000]}
{"t":1792510000456,"kind":"prefill","tokens":5330,"reused":5000,"read":330,"step_ms":2400.0,"tok_s":137.5}
```

### 2.4 工具与压测脚本

| 文件 | 作用 |
|---|---|
| `SSEStraBed/tools/varlen_plan.cpp` | 喂真实接受率，打印 `uniform` vs `varlen` 的 `L`、期望 token、tok/ms；支持 `--curve 1:7,2:8,4:10,8:14` |
| `bench/deepinfer/profile_decode.py` | 复刻 profile 协议的外部版：每档 1 warmup + N 次、TTFT/tok-s、sha256+TTL 缓存；只打 8092 |
| `bench/deepinfer/analyze_steps.py` | 读 `STRATA_STEP_STATS` JSONL，输出 `(rows→step_ms)` 表、可直接喂 `varlen_plan` 的曲线串、逐位置接受率、上下文分桶、prefill 复用率 |
| `SSEStraBed/src/spec/varlen_budget_test.cpp` | 11 组断言，纯 CPU，不加载模型 |

### 2.5 构建产物

| 产物 | sha256 | 说明 |
|---|---|---|
| `archive/binaries/strata-ssebed-0.1.38-b1-20261018` | `3d3f3c9d…a880f` | 现网 A lane 正在跑的二进制（从 `/proc` 取回，与 `build/BUILD.json` 一致） |
| `archive/binaries/strata-ssebed-0.1.38-b1-mtpvarlen-20261020-r2` | `2be4dd32…43c7` | 含 `--mtp-varlen` + StepStats 的新二进制 |
| `SSEStraBed/build/strata` | `3d3f3c9d…a880f` | **已还原为现网二进制**，未偷偷改动 |

构建命令：`/home/cloud/Strata/.venv/bin/ninja -C build strata`；单测：`ninja -C build-tests varlen_budget_test varlen_plan`（CPU-only 配置）。

---

## 3. 测试环境与口径

- 引擎：`qwen3.8-flash-next-iq3_xxs`，`--spec 4`，`kVerifyMaxT=8`，因此
  `L_max = min(4, 8/S)`：S=1→4，S=2→4，S=4→2。
- 网络：3×RTX 4090D 共享，`pressure.json` 全程 `gpu_mem≈0.945`（medium）、GPU 利用率 0–33%。
- **只打普道 8092**；快道 8091 只读 `/metrics`（GET，不发起生成）。
- 8092 上有真实用户流量（日志里 `max 2048` 的请求），外部测量会受其影响，下面逐条标注。
- 外部测不到「窗口时间」：SSE 每个 token 一个 chunk（实测 `chunks == completion_tokens`），
  但服务端可以把一个窗口接受的多个 token 连续发出，所以 **chunk 间隔 ≠ step 时间**。
  真正的 `(rows→step_ms)` 只能来自引擎内 `STRATA_STEP_STATS`——这正是本次补上的观测盲区。

---

## 4. 实测一：生产接受率（只读 `/metrics`）

2026-10-20 15:35 UTC，快道 8091 最近 12 条带草稿计数的请求：

| 指标 | 值 |
|---|---|
| 总草稿 / 总接受 | 5143 / 3796 |
| 边际接受率 | **0.738** |
| 单请求接受率中位（含 2 条 7/7） | **0.801** |
| 剔除 2 条计数异常后中位（n=10） | **0.762**，区间 0.641–0.890 |
| 慢道 8092 的 `drafts_offered` | **0**（legacy 构建没有上报草稿计数） |

两个直接结论：

1. 真实接受率**没有 README 宣传的那么高**，也**没有低到不该投机**；0.76 左右意味着 3 个草稿的期望收益是 `1+0.76+0.58+0.44=2.78` token/窗口。
2. 两条通道的**观测口径不一致**（A 有草稿计数、B 没有），这本身就是需要统一 StepStats 的理由。

---

## 5. 实测二：吞吐、TTFT 与预算分配

### 5.1 外部吞吐（8092，32 输出 token，3 次取中位）

| 并发 | TTFT | 每请求 tok/s | 墙钟聚合 tok/s |
|---:|---:|---:|---:|
| 1 | 0.156 s | 151.9 | 84.1 |
| 2 | 0.264 s | 72.4 | 87.0 |
| 4 | 0.543 s | 44.0 | 96.8 |

读法：

- 并发翻倍，**每请求速率近似减半**，聚合只涨 3–11% → 说明一步的耗时主要由**行数**决定，而不是「一次窗口固定开销」。
- 这与 §5.3 的代价模型互为印证：`rows` 从 4 到 8，窗口时间大致翻倍。
- 另一次测量在 c=1 得到 103 tok/s、c=4 得到 36 tok/s，**逐次波动约 ±40%**，原因是 GPU 与另外两条通道/真实用户共享。所有数字都应读作区间，不是常数。

### 5.2 TTFT 的两分钟级异常（现场证据）

同一天较早一次 c=4 测量，TTFT 中位 **9.88 s**。同期 8092 日志出现：

```
[strata] reading the prompt: 5,325 of 5,330 tokens, 2 s so far
[strata] thinking: 241 of max 32 tokens, 19.5 tok/s, 13 s
```

即：**一个 5.3k token 的 prefill 正在独占引擎**，四个只有 30 token 的探针排在后面。
这不是新问题（见 `docs/reports` 中 prefill 串行/排队的历史结论），但它说明：
**外部压测的 TTFT 必须同时记录「当时是否有大 prompt 在读」**，否则会把排队当成本服务变慢。
StepStats 的 `prefill` 记录正是为了把这个变量显式化。

### 5.3 由实测反推的代价（供先验，不是定论）

用 §5.1 的每请求速率与真实接受率 0.8 估算窗口时间：

| 场景 | 行数 | 期望 token/窗口（每请求） | 每请求 tok/s | 反推窗口 |
|---|---:|---:|---:|---:|
| S=1, L=4 | 4 | 2.95 | 151.9 | 19.4 ms |
| S=2, L=4 | 8 | 2.95 | 72.4 | 40.8 ms |
| S=4, L=2 | 8 | 1.80 | 44.0 | 40.9 ms |

`rows 4→8` 对应窗口 `19.4→40.8 ms`，**近似线性（每行约 5 ms，固定开销接近 0）**。
注意：这里的接受率是按 0.8 假设的，若实际更低，窗口时间会被高估；所以它只能当量级参考。

### 5.4 分配器在真实接受率上的收益（`varlen_plan`，8 行预算）

| 输入接受率 | 统一 L | 逐请求 L | 期望 token/窗口 | 收益 |
|---|---|---|---:|---:|
| 0.890 / 0.839 / 0.727 / 0.646（真实 top4） | 2 | 3/2/2/1 | 7.102 → 7.248 | **+2.1%** |
| 0.839 / 0.806 / 0.727 / 0.646（真实中位段） | 2 | 3/2/2/1 | 7.018 → 7.076 | **+0.8%** |
| 0.92 / 0.88 / 0.55 / 0.45（异质样例） | 2 | 4/2/1/1 | 6.800 → 7.425 | **+9.2%** |

结论要诚实：**在今天的真实分布上，它是小收益；只有当并发请求的接受率拉开差距时才有明显价值。**
它不会让单请求更快，也不会改变 `L_max`；它只决定同样的 8 行怎么分。

### 5.5 关键发现：代价曲线的形状决定「要不要投机」

同样 p=0.8、S=2：

- 线性代价 `0 + 5×rows`：`L=1` 最优（每行 5 ms，一个草稿只带来 0.8 个 token，越投越亏）；
- 次线性曲线 `1:7, 2:8, 4:10, 8:14`：`L=4` 最优（第 5–8 行几乎免费，草稿的期望收益为正）。

这两条都已写进单测（`varlen_budget_test` 第 10 组）。它解释了两件事：

1. 为什么 DeepInfer 要**开机 profiler 量真实曲线**，而不是在线拟合一条直线——直线会把「行数越多每行越便宜」这个决定性的形状抹平；
2. 为什么我们的 `--mtp-varlen` **不能**用「根据 tok/s 反推的直线」当默认先验：那样会在上线第一天把投机关掉。
   因此先验选的是**兼容先验** `{baseline 12ms, row 2ms}`：在均匀接受率下复现今天的 `L=4/L=4/L=2`，只有分布不均时才偏离。

---

## 6. 上线方案（需要用户同意；当前未执行）

### 6.1 前提

- 两条通道的 config 现在指向**同一个** `exe`（`/home/cloud/Strata/build/strata`）。
  所以「只换 B lane」必须先给新二进制一个**独立路径**，不能直接覆盖共享二进制（那会在下次重启时连带换掉 A）。
- 换引擎 = 重启进程 = 该通道短暂不可用；对外服务在线期间必须先经用户同意。

### 6.2 建议步骤（B lane / 8092 先行）

```bash
# 1) 独立二进制（不影响 A）
install -m 755 archive/binaries/strata-ssebed-0.1.38-b1-mtpvarlen-20261020-r2 \
        /home/cloud/Strata/build/strata-mtpvarlen

# 2) 编辑 lane B config（strata-iq3_xxs-b.json）：
#    "exe": ".../build/strata-mtpvarlen"
#    args 追加 "--mtp-varlen"
#    （config 含密钥，已 gitignore；不要贴进文档）

# 3) 观测（可选但建议）：在 run-iq3_xxs-b.sh 里加
#    export STRATA_STEP_STATS=/home/cloud/logs/step-stats-b.jsonl

# 4) 重启 B lane（需批准）
supervisorctl -s unix:///home/cloud/deploy/supervisor.sock restart strata-b
```

### 6.3 观测与判定

```bash
# 等 10–15 分钟真实流量后
bench/venv/bin/python bench/deepinfer/analyze_steps.py /home/cloud/logs/step-stats-b.jsonl
```

判定标准（任一不满足就回滚）：

- `step_ms` 的 p90 不比同并发等级的基线高 10% 以上；
- 接受率不低于 0.70（低于说明分配把草稿浪费在了低置信槽）；
- 墙钟聚合 tok/s 不低于基线（用 `profile_decode.py` 同参数复测）；
- 无 OOM/崩溃，`/metrics` 的 `drafts_offered` 正常增长。

### 6.4 回滚

```bash
# config 的 exe 改回 build/strata、去掉 --mtp-varlen / STRATA_STEP_STATS，然后
supervisorctl -s unix:///home/cloud/deploy/supervisor.sock restart strata-b
```
二进制无需还原（旧路径没动），`build/strata` 仍是现网 sha `3d3f3c9d…`。

---

## 7. 单测与可复现结果

```
$ ./build-tests/varlen_budget_test
uniform p=.9 S=4: L=2 rows=8 expected=7.60 est=18.0ms 0.422 tok/ms
uniform p=.05: L=1 (expected, no speculation)
uniform p=0.20: L=1
uniform p=0.50: L=2
uniform p=0.70: L=3
uniform p=0.90: L=4
uniform p=0.99: L=4
varlen rows=8 lengths=6/1/1 expected=7.30 0.405 tok/ms
estimator: baseline=10.00ms row=2.00ms samples=60
tracker: p0 after full acceptance 0.700, after reset 0.700
curve: ms(3)=9.0 ms(16)=22.0
curve vs line: L=4 vs L=1 (p=0.8, S=2)
varlen_budget tests: OK
```

覆盖：暴力扫描一致性、低接受率不投机、L 随接受率单调、异质分配、预算下限、离群点鲁棒拟合、样本不足回退先验、
tracker EMA/reset、曲线插值与外推、曲线形状改变决策。

外部测量复现：

```bash
cd /home/cloud/bench
./venv/bin/python deepinfer/profile_decode.py --concurrency 1,2,4 --max-tokens 32 --repeats 3 --no-cache
```

---

## 8. 没有采用、但值得记下来的部分

1. **ZOS（zero-overhead scheduling）**：DeepInfer 把输出后处理放进独立线程/队列（`worker/worker.py:143-187`）。
   我们的 `server.py` 是同步读行 → 写 SSE；如果之后要压 decode 延迟，这是下一个该看的地方。
   本次没做：它改的是服务层并发模型，收益需要在真实流量下 A/B，且与引擎重启无关，可以单独排期。
2. **prefill 节拍**：DeepInfer 的 ALL/ANY 投票依赖 DP rank 与 all-reduce；我们是单进程多槽，没有对应物。
   但它的两个不变量值得借：**（a）prefill 必须与 decode 交错，不能等 decode 全空；（b）prefill 最多等 N 步就要兜底**。
   我们当前的 `--prefill auto` 是「谁先来谁先读」，§5.2 的 9.88 s TTFT 就是缺少 (b) 的代价。
3. **deepseek-recipe 的 token-id 安全属性**：它对用户文本用**空 added-vocabulary** 的 tokenizer 编码，
   特殊 token 按 id 处理，所以用户输入里的 `<|endoftext|>` 字面量不会变成控制 token。
   我们已在 `archive/patches/serve-special-token-escape-2026-10-18` 做过等价处理（`server.py` 的 `EOS_IDS` +
   `control_token_literals`），本次复核**无需改动**；这条只作为「对齐官方语义」的旁证。
4. **结构化输出 / EPLB / SWA**：与本次目标无关或硬件不匹配，不展开。

---

## 9. 下一步

| 优先级 | 事项 | 依赖 |
|---|---|---|
| P0 | 在 B lane 上线 `--mtp-varlen` + StepStats，跑 10–15 分钟真实流量 | **用户同意重启** |
| P0 | 用 `analyze_steps.py` 产出真实 `(rows→step_ms)` 表，替换先验曲线 | P0 上线 |
| P1 | 若曲线确认次线性：把 `plan_varlen` 接到逐槽变长校验（当前校验器只支持整组 L） | 引擎校验器改造 |
| P1 | prefill 节拍：给 `--prefill` 加「最多等 N 个 decode 窗口」的兜底 | 引擎调度器 |
| P2 | 服务层 ZOS：SSE 写出与 decode 循环解耦 | server.py |

---

## 附录：文件清单

| 路径 | 行数/大小 | 性质 |
|---|---|---|
| `SSEStraBed/include/strata/spec/varlen_budget.hpp` | ~130 行 | 新 |
| `SSEStraBed/src/spec/varlen_budget.cpp` | ~230 行 | 新 |
| `SSEStraBed/src/spec/varlen_budget_test.cpp` | ~150 行 | 新（11 组） |
| `SSEStraBed/include/strata/program/step_stats.hpp` | ~64 行 | 新 |
| `SSEStraBed/tools/varlen_plan.cpp` | ~110 行 | 新 |
| `SSEStraBed/src/program/generate.cpp` | +~45 行 | 改（默认关闭） |
| `SSEStraBed/CMakeLists.txt` | +4 行 | 改 |
| `bench/deepinfer/profile_decode.py` | ~170 行 | 新 |
| `bench/deepinfer/analyze_steps.py` | ~110 行 | 新 |