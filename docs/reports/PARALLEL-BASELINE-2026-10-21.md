# 并行化改造 P0 基线（prefill 与 decode 并存）

> 测于 **2026-10-21 20:19–20:21（北京时间）**，lane A（`127.0.0.1:8091`，GPU0），
> 线上二进制 `ssebed-0.1.38-b5`（sha256 `4e5ce24d…`，带 `--mtp-varlen`）。
> lane B（8092）同期有真实流量（GPU1 99%），A 道本身空闲；`/shared/pressure` 为 cpu low / gpu low / mem med。
> 本文是后续所有"混合窗口有没有用"的对照组。**P0 期间没有改任何代码、没有重启任何进程。**

---

## 1. 负载定义（可复现）

脚本：`~/bench/deepinfer/parallel_baseline.py`（新增，只打 8091）。
原始数据：`~/bench/runs/parallel-baseline/p0-*.json`（含每条流的逐 chunk 时间戳汇总 + 本次运行的
全部 `step-stats` 窗口）。

| 项 | 取值 |
|---|---|
| 背景解码流 | 1 / 2 / 3 条并发，短提示词，`max_tokens=1800`，`temperature=0`（贪心） |
| 冷 prefill 流 | 随机 nonce 前缀 + 语料，目标 8k / 16k / 32k token，`max_tokens=2`，`temperature=0` |
| 顺序 | 先起背景流 → 稳定出字后再等 3.5 s → 发 prefill → prefill 结束后再跑 3 s |
| 并发上限 | `--batch 4`：3 条 decode + 1 条 prefill = 4，正好占满，不排队 |
| 计时 | 客户端流式到达时刻（`time.time()`），ITL = 相邻 content/reasoning chunk 间隔 |

> **口径提醒**：这是压测台口径（用户实际感受到的流式间隔），不是逐窗口口径。
> 背景流用贪心，避免"采样关掉 MTP 整组退化"的干扰（P0 的背景流确实走了 chain 或 batch 的正常路径，
> 见 §3 的 `drafts` 字段）。

---

## 2. 结果（主表）

### 2.1 1 路冷 prefill + N 路持续 decode

| prefill 目标 | 解码流 | prefill TTFT | prefill 实际 token / 墙钟 | 解码冻结（最长 ITL） | prefill 前聚合 | **prefill 期间聚合** | 保留比例 |
|---|---|---|---|---|---|---|---|
| 8k | 1 | **2.905 s** | 8 019 / 2.57 s（3 126 tok/s） | **2.846 s** | 91.9 tok/s | 3.79 tok/s | 4.1% |
| 8k | 2 | **2.433 s** | 8 015 / 2.30 s（3 491 tok/s） | **2.521 s** | 123.7 tok/s | 5.75 tok/s | 4.6% |
| 8k | 3 | **2.451 s** | 8 016 / 2.30 s（3 481 tok/s） | **2.509 s** | 133.8 tok/s | 9.79 tok/s | **7.3%** |
| 16k | 3 | **4.506 s** | 16 087 / 4.31 s（3 731 tok/s） | **4.545 s** | 141.4 tok/s | 6.44 tok/s | 4.6% |
| 32k | 3 | **9.317 s** | 32 281 / 8.98 s（3 597 tok/s） | **4.748 s** | 109.4 tok/s* | 8.48 tok/s | 7.8% |

\* 32k 的"prefill 前"窗口长 9.3 s，包含了背景流刚起步的一小段，所以偏低；8k/16k 两行才是稳态值
（≈ 134–141 tok/s，3 路 batch 窗口 ~20–21 ms）。

**一句话**：prefill 一开始，解码吞吐从 ~134 tok/s 掉到 ~4–10 tok/s（**只剩 4–7%**），
每次冻结 2.5 s（8k）到 4.5 s（16k/32k 的单段）。

### 2.2 纯解码对照（无 prefill，同一脚本 `--length 0`）

| 解码流 | 聚合吞吐 | 每流 ITL p50 / p95 | 路径 |
|---|---|---|---|
| 1 | 94.3 tok/s | 0.1 ms / 29.2 ms（MTP 成对到达，p50 被 0 间隔拉低） | chain（`drafts≈2.8`） |
| 2 | 101.5 tok/s | 18.2 / 24.5 ms | chain（`drafts≈2.0`） |
| 3 | 127.3 tok/s | 32.1 / 44.5 ms | batch（`drafts=0`） |

这与既有认知一致，也暴露一个必须写进 P2 判据的事实：**3 路并发时 serve 把请求放进批槽，
批槽路径不做 MTP**（`step-stats` 里 `kind=batch, drafts=0`），所以 P0 的 3 路基线
本来就是"逐 token 窗口"，不是投机窗口。

---

## 3. 引擎侧证据（`step-stats-a.jsonl`，本次运行新增 400–440 个窗口）

以 **8k + 3 decode** 为例（`p0-8000-d3-20261021T122014Z.json`）：

```
12:20:03–08   batch 窗口 44–47 个/秒，每窗 3 行，中位 21.3 ms   ← 正常解码（≈140 tok/s 聚合）
12:20:08      客户端发出 prefill
12:20:08–11   ！零个 decode 窗口（只有一条 prefill 记录）
12:20:11      prefill 完成：tokens=8016, step_ms=2302.7, tok_s=3481
12:20:11      恢复：第一个窗口 85.5 ms（缓存/管道重建），随后回到 ~22 ms
```

- **8k 只有一个 chunk**（`chunk=8192`），而 yield 段是 `2 × chunk = 16384`，
  所以 8k 预填充**根本没有让出点**：解码被冻结整个 2.3 s + 固定开销 ≈ 2.5 s。
- **16k 也只有一个 chunk 段**（16 087 ≤ 16 384），冻结 4.5 s。
- **32k 才出现一次让出**：`12:18:37` 有 12 个 decode 窗口（≈0.25 s 出字），
  然后继续冻结 4.7 s。也就是说默认 `STRATA_PREFILL_YIELD_CHUNKS=2` / `YIELD_STEPS=12`
  在 32k 上只给出 **0.25 s 的喘息 / 每 4.7 s**，与"解码最长停顿 4.9 s"的旧结论吻合。

同时注意 prefill 的**固定开销**：3 条背景流的短提示词各自有 205–309 ms 的 prefill
（`tokens=28, reused=21, read=7`），其中真正读的只有 7 个 token —— 这是每请求 ~0.2–0.3 s 的固定项，
P2 的 TTFT 判据要把它和长 prompt 的线性项分开看。

### 引擎自报分项（`strata batch:`，热缓存、3 路 batch）

最近几次汇总的窗口均值（把累计值除以窗口数）：

| 窗口数 | 每窗 ms | GPU-reach wait | CPU experts | commit |
|---|---|---|---|---|
| 436 | 21.73 | 1.23 ms | 0.88 ms | 1.27 ms |
| 446 | 20.36 | 1.15 ms | 0.83 ms | 1.26 ms |

**这与历史重载样本（wait 21 ms + CPU 70 ms）不是一个量级**：P0 的负载是热缓存 + 3 行，
GPU 等待和 CPU 专家都只有 ~1 ms/窗。所以：

> 本基线里"可被混合窗口填掉的空隙"主要不是稳态的 CPU 专家段，而是 **prefill 独占造成的 2.5–4.7 s 全冻结**。
> P2 的收益应主要来自"prefill 期间 decode 仍能出字"，而不是稳态窗口内部。

---

## 4. P2 判据的基线值（固定下来，后续不再改负载）

| 判据 | 基线值（8k + 3 decode） | P2 目标 |
|---|---|---|
| 解码最长停顿 | **2.509 s** | ≤ 50% → **≤ 1.25 s**（更严格地看"prefill 期间窗口数 > 0"） |
| prefill 期间解码聚合 | 9.79 tok/s（对照 133.8） | ≥ 50% 对照 ≈ 67 tok/s（或至少 ≥ 2× 基线） |
| prefill TTFT | **2.451 s** | 不劣化 > 10% → ≤ 2.70 s |
| 1 prefill + 3 decode 聚合 | 基线约 (133.8×2.45 + 9.79×2.45)/4.9 ≈ 71.8 tok/s 区间均值 | ≥ +15% → ≥ 82.6 tok/s |

单流与双流基线同样保留（TTFT 2.905 / 2.433 s，冻结 2.846 / 2.521 s），用于检查策略在不同并发下的形状。

> **p95 的坑**：prefill 期间每条流通常只有 1 个大间隔 + 若干正常间隔，样本数 n≈9，
> 这时"p95"会把最大值排除掉（8k+d1 的 p95=0.19 s，max=2.85 s）。
> 因此本任务用 **`freeze_max`（prefill 期间最长 ITL）** 作为主判据，p95 只作参考；
> P2 若把长间隔切成很多小间隔（n 变大），p95 才有统计意义。

---

## 5. 本次**没有**测的（需要停服，等用户批准）

P0 原计划还包含 `STRATA_PREFILL_YIELD_CHUNKS` / `STRATA_PREFILL_YIELD_STEPS` 的取值扫描。
这两个变量在 `generate.cpp:6774–6778` 通过 `std::getenv` 读取，**改值必须重启引擎**；
按红线没有擅自重启。等用户批准后，建议的扫描（每次重启 lane A 一次）：

| 变量 | 现网 | 建议试验值 | 预期 |
|---|---|---|---|
| `STRATA_PREFILL_YIELD_CHUNKS` | 2（段 = 16 384 token） | 1（段 = 8 192） | 32k 的冻结从 4.7 s 降到 ~2.4 s；8k 仍然整段冻结 |
| `STRATA_PREFILL_YIELD_STEPS` | 12 | 24 / 48 | 让出期间解码多跑几个窗口，但 prefill 墙钟变长 |

注意：即使把 `YIELD_CHUNKS` 调到 1，**8k 仍是一个 chunk、仍然整段冻结** ——
这正说明 P2 的"层窗口交错"不是调参能替代的。

---

## 6. 复现命令

```bash
cd ~/bench
# 纯解码对照
./venv/bin/python deepinfer/parallel_baseline.py --decode 3 --length 0 --decode-tokens 1800 --warmup 3.5 --tag pure-d3
# 主实验：8k prefill + 3 decode
./venv/bin/python deepinfer/parallel_baseline.py --decode 3 --length 8000 --decode-tokens 1800 --warmup 3.5 --tag p0-8000-d3
# 16k / 32k
./venv/bin/python deepinfer/parallel_baseline.py --decode 3 --length 16000 --decode-tokens 1800 --warmup 3.5 --tag p0-16000-d3
./venv/bin/python deepinfer/parallel_baseline.py --decode 3 --length 32000 --decode-tokens 1800 --warmup 3.5 --tag p0-32000-d3
```

---

## 7. 结论

1. **基线成立**：8k 冷 prefill 的 TTFT 2.45 s，期间 3 路解码从 133.8 掉到 9.8 tok/s，
   最长停顿 2.51 s；每次 prefill 都是"整段独占"，因为 8k/16k 都装不满一个默认 yield 段（16384）。
2. **P2 的收益空间明确**：不是稳态窗口里的 1 ms CPU 段，而是这 2.5–4.7 s 的**全冻结**。
   只要混合窗口能让 decode 在每个层窗口里保留 1 行，就能把"零窗口"变成"每 ~20 ms 一个窗口"。
3. **判据固定**：见 §4；`freeze_max` 为主，p95 仅参考。
4. **待批准**：yield 两个环境变量的扫描需要重启 lane A（每次 6–13 分钟），未执行。