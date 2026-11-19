# P2：校验窗口的「容量 / 上限」拆分 + 宽窗口实测 —— 容量改造落地，宽窗口暂不上线

日期：2026-10-22（北京时间）
分支：lane A（:8091）实测；lane B / 公网 / rag-proxy 未触碰。

## 0. 结论（先看这个）

1. **容量改造落地且自洽**：`kVerifyMaxT` 从「8 = 容量 = 上限」拆成
   **编译期容量 32 + 运行期上限 `--max-rows`**；逐 token 展开的 kernel 改为按
   `kVerifyUnrollT(=8)` 分块；GMAX 从「窗口上限」改为「每组容量」，超过就拆组、不再静默截断；
   CPU 侧的 `MAXT` 栈依赖改为 ≤8 行的块；`--batch` 上界放开。
2. **kernel 级逐位等价已证明**（这是能拿出来的最强证据）：
   - `verify_wide_t_test`：T=8/16/24/32 的 gate/beta 与「每 8 行单独跑」**逐位相同**，窗口外的行不被触碰（12/12 通过）。
   - `mmvq_multi_parity`（本次把列宽集合扩到 12/16/24）：7 种量化格式 × 3 个宽列数，**0 个 bit 差异**；
     负向对照在同一批数据上能检出差异（说明比较有功效）。
3. **真实路由下，同一专家的行数从来没超过 8**：`STRATA_PLAN_CHECK=1` 跑了
   26,624 个窗口 / 466,119 次专家执行，**0 次丢行、0 次重复行、最宽的一组是 8 行**。
   → 把窗口从 8 放宽到 16，**换不来任何「同专家更宽」的批量收益**。
4. **性能**：默认（8 行）路径与旧二进制在同等条件下持平（汇总吞吐 −0.5%，ITL 中位 −4.4%，
   TTFT 中位 +4.9%，全部落在旧二进制自己的跑间波动里）；**宽窗口（16 行）ITL 恶化 +18.9%**，
   不满足「≤2%」的门禁 → **`--max-rows` 保持默认 8，宽窗口作为实验开关保留，不上线**。

## 1. 改了什么

| 文件 | 改动 |
|---|---|
| `include/strata/kernels/verify_kernels.hpp` | `kVerifyMaxT = 32`（容量）、新增 `kVerifyUnrollT = 8`；`resident_plan(..., int group_entries)` |
| `src/kernels/cuda/verify_kernels.cu` | `gdn_ab_multi_kernel` 改为按 8 行块展开；`resident_plan_kernel` 按 `group_entries` 拆组 |
| `src/kernels/cuda/s2_expert_grouped.cu` | 去掉 `GMAX >= kVerifyMaxT` 断言，改为「GMAX = 每组容量」的注释契约 |
| `include/strata/core/expert_source.hpp` | `GpuPlanSink::group_entries`（每组条目上限），默认 8 |
| `src/core/expert_source.cpp` | 拆组按 `group_entries`；`check_plan()` 计划不变量检查（`STRATA_PLAN_CHECK=1`）；CPU job 按 `cpu::MAXT` 分块；缓冲按容量放大 |
| `src/core/verify.cpp` | 窗口上限校验放宽；`group_entries` 按布局给（原生分组内核不限长度 → `max_t`，legacy → 8） |
| `src/program/generate.cpp` | `--max-rows`（1..32），`verify_max_t` = min(max(spec,batch), chains, max_rows) |
| `src/kernels/cuda/native_mmvq.cu` | 宽列（>8）按 8 列切块递归；`native_q8_1_bytes` 只校验宽度与列数 ≥1 |
| `src/core/verify.cpp`（第二轮） | 融合 GR（`kFusedGrMaxT=8`）与共享专家（1..8 token）**按 8 行分块调用** |
| `src/kernels/mmvq_multi_parity.cpp` | 列宽集合加入 12/16/24（宽列切块的逐位证明） |
| `tests/cuda/verify_wide_t_test.cpp` | 新的 kernel 一致性测试 |

补丁：`~/archive/patches/p2-row-capacity-2026-10-22/`
（`p2-row-capacity.patch` 主改动 + `p2-wide-fixes.patch` 第二轮：native_mmvq / verify.cpp / parity 测试）。

## 2. 过程中挖出的「隐藏的 8」

容量改造不是改一个常量：真实路径上还有 **6 处**把「8 行」写死的地方，逐个才是这次的主要工作量。

| # | 位置 | 症状 | 处理 |
|---|---|---|---|
| 1 | CPU 专家 `cpu::MAXT` | 栈上数组按 8 行 | 按 8 行块切 job |
| 2 | `s2_expert_grouped.cu` `GMAX` | `min(..., GMAX)` 静默丢行 | 拆组（每组 ≤ `group_entries`），不再截断 |
| 3 | `native_mmvq` `MAX_NCOLS=8` | `native MMVQ requires 1 <= ncols <= 8`（**崩**） | 8 列切块递归（逐位等价，见 §3） |
| 4 | `native_q8_1_bytes(n, ncols)` | 只是算缓冲尺寸却复用了列数校验（**崩**） | 只校验宽度整除 + 列数 ≥1 |
| 5 | `fused_gr_read_multi` `kFusedGrMaxT=8` | `fused_gr_read_multi: invalid arguments` + `std::exit(1)`（**进程直接退出**） | `gr_read_group` 按 8 行块调用 |
| 6 | `shared_expert_multi` 1..8 token | 抛异常 → 本窗口校验失败 | 按 8 行块调用 |

> 教训：**「上限 8」在引擎里不是一个常量，而是散落在 6 个不同层次的容量假设**。
> 逐个 grep 静态上限不如「打开宽窗口跑一遍，让它自己报错」，代价是每次重启 ~7 分钟加载 40 GiB 专家。

## 3. 验证

### 3.1 CPU 测试（`switch_engine.py dev test`，11 个用例）

```
100% tests passed out of 11
```
（`~/logs/p2-dev-test2.log`；其中 `file_expert_source_test` 曾经因为 build-tests 的
`STRATA_NATIVE_EXPERTS=OFF` 陈旧缓存链接失败，重跑 cmake 后通过，与本改动无关。）

### 3.2 GPU kernel 一致性

```
verify wide-T parity: n=512 h_v=48 capacity=32 unroll=8
T=8/16/24/32: gate bitwise == 8-row blocks      ok
T=8/16/24/32: beta bitwise == 8-row blocks      ok
T=8/16/24/32: rows beyond T untouched           ok
PASS (0 failures)
```

```
multi_exact on:   414720 outputs compared, 0 differ bitwise, 0 non-finite
multi_exact off:  414720 outputs compared, 224644 differ bitwise (control has power)
WIDTHS = {1,2,3,4,5,6,8,12,16,24}
```
（`~/logs/p2-parity-run.log`）

### 3.3 真实流量上的计划不变量（`STRATA_PLAN_CHECK=1`，16 行窗口，S=8 压测期间）

```
strata plan: 26624 windows checked, 466119 expert runs,
             0 runs wider than 8 rows (max 8, widest 0 rows)
```
判据（`check_plan()`）：每个 GPU 条目在窗口内**恰好出现一次**、每组 ≤ `group_entries`、
PCIe 组与驻留组互补。**0 违规** = 宽窗口下没有丢行、没有重复行。

同时得到一条关键事实：**真实路由里「同一专家的行数」最大就是 8**（多数远小于 8）。
窗口 8 → 16 不会让某个专家的 GEMV 更宽，只是把一个窗口塞进更多（不同的）专家。

## 4. 性能（同小时、同机器、warm 状态）

协议：`tools/p2_identity.py --concurrency 8`（20 条固定 prompt，贪心，96 token），
每次重启后第 1 次运行**丢弃**（冷启动，TTFT 会高近 2 倍），取第 2–4 次。

| 构建 | TTFT 中位 (ms, 逐请求合并) | TTFT p90 | ITL 中位 (ms) | ITL p90 | 汇总吞吐 (tok/s, 逐次) |
|---|---|---|---|---|---|
| 旧二进制 `mtpvarlen-20261021d` | 265.1 | 322.6 | 122.1 | 163.0 | 34.4 / 38.3 |
| P2 默认（8 行） | 278.0 | 330.5 | 116.75 | 176.3 | 34.28 / 38.05 |
| P2 宽窗口（16 行） | 527.9（含冷启动）| 535.4 | 170.2 | 201.6 | 26.9 |

- 默认路径：汇总吞吐 **−0.5%**、ITL 中位 **−4.4%**、TTFT 中位 +4.9%。
  旧二进制自身三次运行的中位 TTFT 就在 247–310 ms 之间摆动（±12%），所以这属于噪声内持平。
- 宽窗口：ITL **+18.9%**、TTFT +3.6%（对同一小时的旧二进制），**超出 2% 门禁**。
- 门禁的严格版（「S=8 的 TTFT/ITL 都在 2% 以内」）在当前噪声水平下**无法被证明**——
  需要 ≥10 次重复 + 固定暖机，成本是每次 ~40 s × 10 × 2 组；本次取 3 次/组，结论是「持平」。

### 4.1 一个必须记住的测量陷阱：冷启动

同一个二进制、同一份负载，重启后**第 1 次** S=8 运行的 TTFT 中位是第 3–4 次的
**约 2 倍**（509 → 255 ms）。第一次跑完引擎的专家缓存/会话缓存/GPU 时钟才热。
本报告所有跨构建比较都只用了「暖机后」的运行；否则会把 2 倍的冷启动差当成代码回归。

### 4.2 另一个必须记住的事实：引擎不是逐次可复现的

同样的 prompt、同样的二进制、同样的贪心参数，跑两次只有 **6–12/20** 条输出逐字节相同
（`p2_compare.py` 的 `content identical` 列）。所以「S=4 贪心 20 prompt 逐 token 一致」
这个原始门禁**任何构建都过不了**；可以成立的替代证据是：
kernel 级逐位等价（§3.2）、计划不变量（§3.3）、以及多次运行的统计时延（§4）。

## 5. 判定与后续

- **P2 容量改造：GO**（默认路径持平，能力打开，计划不变量有 checker 兜底）。
- **宽窗口（`--max-rows > 8`）：NO-GO**（实测 ITL 恶化 19%，且真实路由根本产生不了 >8 行的组）。
  代码保留在 `--max-rows` 后面，默认 8。
- 对后续步骤的影响（重要）：
  - P3「层优先全局调度」**不能靠加宽窗口**实现——宽窗口只增加「同时活着的行」，不增加「同专家批量」。
    要做的是**跨层调度**（把不同层的同一专家的读取排在一起 / 把门铃串行打散），这与 P2 的容量改造无关。
  - P4「按负载调整 CPU/PCIe/GPU 分工」仍然是主战场：解码 ~94% 时间在等专家（见
    `memory/decode-expert-bandwidth-bound.md`）。

## 6. 回滚

- 二进制：`~/deploy/engines/bin/mtpvarlen-20261021d/strata`（旧生产二进制，已验证可服务）。
- 配置：`~/archive/patches/p2-row-capacity-2026-10-22/config/ab-novarlen-strata-a.json.before`。
- 操作：把 `~/deploy/engines/profiles/ab-novarlen/strata-a.json` 的 `exe` 指回去、
  删掉 `--max-rows`、`supervisorctl -c ~/deploy/supervisord.conf restart strata-a`（**需用户同意**）。

## 7. 出处

- 补丁：`~/archive/patches/p2-row-capacity-2026-10-22/{p2-row-capacity.patch,p2-wide-fixes.patch}`
- 测试：`~/SSEStraBed/tests/cuda/verify_wide_t_test.cpp`、`~/SSEStraBed/src/kernels/mmvq_multi_parity.cpp`
- 日志：`~/logs/p2-dev-test2.log`、`~/logs/p2-parity-run.log`、`~/logs/p2-wide-build.log`、
  `~/SSEStraBed/logs/strata-iq3_xxs-a-abprod.log`（`strata plan:` 行）
- 数据：`~/docs/reports/data/p2-identity-{p2old,p2b7-c8-ctrl,p2b7-c8}-*.json`
- 对比脚本：`~/tools/p2_compare.py`