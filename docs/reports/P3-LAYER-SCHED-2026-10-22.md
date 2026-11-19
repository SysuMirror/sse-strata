# P3：层优先调度 / 并发 prefill —— 实测与结论

日期：2026-10-22（北京时间）
范围：**只动 lane A（:8091，GPU0，`--batch 4 --batch-mtp --batch-groups 1 --spec 4`）**。lane B / 公网 / rag-proxy 未动。
引擎：`legacy-0.1.38-sha.da5e3bba`（`~/deploy/engines/bin/p2-rowcap-b7-20261022/strata`）。
数据：`~/docs/reports/data/p3-*.json`；工具 `~/tools/p3_bench.py`、`~/tools/p3_set_batch.py`；
归档 `~/archive/patches/p3-layer-sched-2026-10-22/`。

---

## 0. 结论（先看这四条）

1. **S=4 是这块卡上的最优批槽数**，再大反而退化：S=8 每请求 5.6–6.2 tok/s / ITL 55–58 ms，
   S=4 每请求 15.0–15.7 / ITL 30–31 ms。S=16 引擎起不来（显存），48 路并发在本卡上不可能
   （48 × 0.95 GiB = 45.6 GiB > 24 GiB）。
2. **prefill 是串行的，且成本里有一大块固定项**：单请求 TTFT 曲线 1k→2.02 s、2k→2.07 s、
   7.4k→2.52 s、14.7k→4.69 s；32 路同时到达时 TTFT 中位 **7.7–13.1 s**，队列效应主导。
3. **多请求 prefill 合并是唯一的大杠杆，但不是改调度能拿到的**：`Prefill` 的每层循环整段绑定
   单会话状态（`ss.gdn_state` / `ss.qsa_states[]` / `ss.ple.hist` / 单份 RoPE 与页表），
   要按行绑不同会话必须改 kernel 签名——属内核级重构，不在 P3 范围内。收益估算见 §3。
4. **发现第 7 处隐藏的「8 行」上限**：`native_bf16.cu:254` 的 `bf16_gemv_fp32_mmvf_multi`
   在 `n_tok > 8` 时抛异常，`--max-rows 16` 会让链式验证首次尝试就失败并被**永久关闭**
   （`chain_off`），此后整组退化为逐 token 窗口。所以「加宽窗口」在本引擎里不是免费参数。

**回滚**：P3 没有改任何代码，只改了运行时参数；把 `--batch` 写回 4、删掉 `--max-rows` 即回到基线
（`~/archive/patches/p3-layer-sched-2026-10-22/config/strata-a.json.before` 是当时的原始 profile）。

---

## 1. 测量口径（这次踩到的两个坑，都会做出假数）

### 1.1 服务层的 `timings` 在并发下是假的

`Strata/serve/server.py:2003` 的 `request_timings()` 从一份共享的 `last` 快照里取时间戳。
32 路并发时实测：**只有 1/8 的响应带 `timings`，其余为 `None`**；拿它算 TTFT 会得到
「中位 0.3 ms」这种不可能的数值。

**改用流式 + 客户端时间戳**：`stream: true`，按 SSE chunk 到达时间算
`TTFT = 首 chunk − 发起`，`ITL = 相邻 chunk 间隔`。本报告所有 TTFT/ITL 都是这个口径。

同样的竞态也出现在 `usage.prompt_tokens_details.cached_tokens`：两条 117k 并发请求里，
一条报 `cached=117050`（全量复用），但它**确实各自算对了自己的答案**（见 §5），
说明「复用数」是共享快照，不是这条请求的真实值。并发下别用 `cached_tokens` 做门禁。

### 1.2 重启后要跑 4–6 轮才到平台期

同配置连跑 6 轮（32 路并发、64 token）：

| 轮次 | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| 聚合 tok/s | 31.8 | 38.5 | 49.2 | 52.2 | 48.0 | 52.2 |
| 每请求 tok/s | 10.5 | 13.2 | 16.1 | 18.4 | 16.7 | 18.0 |
| ITL 中位 ms | 56.4 | 41.9 | 35.1 | 31.5 | 30.9 | 29.8 |

第 1 轮比平台期慢 **~40%**。**协议：每次重启后丢前 3 轮，取第 4–6 轮。**

### 1.3 提示词必须头尾都唯一

引擎的会话检查点按 tail block 匹配。只在前部改编号时，实测 **3768/3775 token 被当成"复用"**，
TTFT 量到的是缓存命中而不是 prefill。`p3_bench.make_prompt()` 现在每一节和结尾都带 salt。

---

## 2. prefill 串行：证据

引擎的服务循环一次只读一个 prompt（单线程 `sp.run`）。固定 `--tokens` 很小、只改 prompt 长度：

| prompt token | TTFT 中位 ms | 说明 |
|---|---|---|
| 1,083 | 2,023 | 固定项主导 |
| 1,980 | 2,074 | +50 ms / +897 tok ≈ 18k tok/s 边际 |
| 7,380 | 2,517 | 边际降到 ~12k tok/s |
| 14,730 | 4,687 | 边际 ~3.4k tok/s（超线性） |

超线性的来源：`src/prefill/prefill.cpp` 的 `stream_all` 分支（T ≥ `stream_all_min()` = 1024）
**每个 chunk 都把每层所有非驻留专家重新流一遍**，chunk 数随 T 增长，于是固定成本按 chunk 数叠加。

并发（32 路短提示词）时 TTFT 中位 7.7–13.1 s、p90 更高——请求排在第 1..32 位，第 i 条的 TTFT
≈ 前 i−1 条 prefill 之和。**这就是「八并发 TTFT 两分钟」类问题的机制。**

---

## 3. 多请求 prefill 合并：为什么是内核级重构

合并的想法是：把不同请求的行拼进同一个层窗口，专家读取就能被复用一次（这是 MoE 段天然的行独立性质）。
读 `src/prefill/prefill.cpp:1651` 起的主层循环，**每一段都绑死单会话**：

| 位置 | 单会话假设 |
|---|---|
| `ss.gdn_state` / `ss.qsa_states[qsa_index]` | 一份状态，没有按行索引 |
| `ss.ple.hist` | 一份 PLE 历史 |
| `p0`（RoPE 位置、KV 页表） | 一份位置流 |
| indexer 的 `tail_snap_` / `sel_` | 单序列 |

要合并，`qsa_append`、GDN 递推、RoPE、页表写入、indexer 选块，**全部要带每行的会话指针**。
这不是「换个循环顺序」，是改 kernel 签名 + 重写 prefill 状态机。

**收益估算（按测得的成本曲线）**：合并 8 条 1k prompt，固定项 $2.0 s 省成 1 份
（8×2.02 → ≈2.6 s，约 **6×**）；8 条 3.8k prompt 只省线性部分，约 **1.8×**。
短 prompt 收益最大，长 prompt 会撞上每 chunk 全量专家流的天花板——所以合并要和「只流本 chunk 真正需要的专家」一起做。

**P3 的调度侧结论**：`Policy::next()`（`include/strata/sched/sched.hpp` + `src/sched/policy.cpp`）
的优先级 Control > Decode > Prefill > Idle 已经在位且已接线（`src/program/generate.cpp:5636-5760`），
本次**没有发现可以在不改内核的前提下再拿的收益**。真正的下一步是 §3 的合并重构。

---

## 4. S 扫描：4 是最优

同一负载（32 条并发、64 token、贪心），只改 `--batch S`：

| 配置 | 聚合 tok/s | 每请求 tok/s | ITL 中位 ms | 窗口构成 | 结论 |
|---|---|---|---|---|---|
| **S=4（基线）** | **46.6–52.2**（两批独立测量） | **15.0–18.0** | **29.8–31.6** | chain 165–178 / prefill 54–56 | **最优** |
| S=8 | 35.3–37.0 | 5.6–6.2 | 54.8–58.0 | chain 4–9 / **batch 119–122** | 投机整组失效 |
| S=8 + `--max-rows 16` | 34.1–38.9 | 5.5–6.3 | 53.6–61.1 | **chain 0** / batch 137–143 | 链式被永久关闭（§5） |
| S=16 | — | — | — | — | 引擎起不来 |
| 48 路（用户目标） | — | — | — | — | 不可能 |

机制：`decode_rows_per_slot(S) = min(chain_rows_per_slot, max(1, row_budget/S))`，行预算 8。
S=4 时每槽 2 行 → 链长 L=2，链式验证成立；S=8 时每槽 1 行 → `chain_step` 退化成
**逐 token 窗口**（`src/program/generate.cpp:5821-5823`），投机收益归零，而那恰是解码吞吐的主要来源。

显存账：每个 slot 会话 0.95 GiB。S=16 时日志先报
`16 slot sessions on CUDA0 (0.95 GiB each); 2.08 GiB free`，随后
`prefill: device buffers for a chunk of 512 tokens do not fit` —— 不是调度限制，是装不下。
S=48 需要 45.6 GiB，本卡 24 GiB，**方向性不可行**；要 48 路只能换「多请求合并窗口 + 更省显存的槽」的路子。

---

## 5. 第 7 处隐藏的「8 行」上限（新发现）

`--batch 8 --max-rows 16` 时日志出现：

```
chain window failed (verify router: bf16_gemv_fp32_mmvf_multi: 1..8 rows, even n_in/ldx,
aligned pointers); falling back to one-token windows
```

源码：`src/kernels/cuda/shared_expert.cu:206` 用窗口行数调
`bf16_gemv_fp32_mmvf_multi(..., n_tok, stream)`，而 `src/kernels/cuda/native_bf16.cu:254`
在 `n_tok < 1 || n_tok > 8` 时 `throw std::invalid_argument(...)`。异常被链式路径捕获后
置 `chain_off`，**此后整组永远走逐 token 窗口**（`generate.cpp:5870/5951/5975/6060`）。

这是此前记录的 6 处「8 行」假设之外的第 7 处，且是唯一一处**用异常杀整条优化路径**的。
修法：仿照 `native_bf16.cu` 的 `STRATA_MMVF_M` 分派，为 9..16 行加一个 `NT=16` 的模板实例，
或在 `shared_expert` 里按 8 行分块。

---

## 6. KV 侧与并发长上下文（P5 的前置证据）

`session_bytes(g, max_cells=262144, ...)` 只分配**一份** KV arena，所有批槽共享同一张页表池。
实测验证：

- 2 条并发 **129k**（合计 258k，正好卡在 262,144 以内）→ 两条都成功；
- 2 条并发 **140k**（合计 280.9k，**超过池容量**）→ **两条也都成功**，但第二条 TTFT 97.3 s vs 第一条 53.9 s
  （被 prefill 串行 + 停车/流式换出拖住）；
- 并发下**答案各自正确**（3k / 117k / 148k 三组不同秘密编号探针全部答对，没有串味）。

结论：**长上下文并发不是硬失败，而是退化成串行 + 停车抖动**；KV 不是当前并发上限的来源，
prefill 串行（§2）才是。这条直接决定 P5 的取舍。

---

## 7. 复现

```bash
# 并发/S 扫描（只打 lane A）
python3 ~/tools/p3_set_batch.py --batch 4 --restart            # 基线
python3 ~/tools/p3_bench.py --tag x --n 32 --concurrency 32 --tokens 64
python3 ~/tools/p3_set_batch.py --batch 8 --max-rows 16 --restart
# 回到基线（--max-rows 0 = 删掉该参数）
python3 ~/tools/p3_set_batch.py --batch 4 --max-rows 0 --restart
```

**注意**：`--max-rows 0` 会连同它的取值一起删；早先只删 flag 会留下一个裸数字，
引擎打印 usage 后立刻退出（supervisor 报 `Exited too quickly`），lane A 因此停过约 20 分钟。
`p3_set_batch.py` 的删除分支已修正。