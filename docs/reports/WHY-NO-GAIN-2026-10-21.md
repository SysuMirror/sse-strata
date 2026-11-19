# 为什么 `--mtp-varlen`「没有差别」：三层原因 + DeepInfer 架构对本服务到底适用多少

> 提问：变长验证预算移植过来以后，TTFT 与吞吐都和改造前持平——是 DeepSeek 开源的架构对我们的系统没有优化吗？
> 本文用**本机实测**回答第一半，用**DeepInfer 源码逐行核对**回答第二半。全部数据 2026-10-21 当天、北京时区。
> 相关：`DEEPINFER-VARLEN-2026-10-21.md`（改造与实验）、`CURRENT-VERSION-2026-10-21.md`（现网快照）。

---

## 0. 一句话结论

**不是实现错了，也不是 DeepInfer 没用——是我们只移植了它的一小块调度策略，而这一小块在它自己的线上数据里也只值
+4~7%；剩下能解释它「快」的部分（DSA 稀疏注意力、FP8/MXFP4、FlashMLA/DeepGEMM/DeepEP、专家常驻显存、SM100）
在我们的 3×4090D + IQ3_XXS + 专家主机分页上逐条不成立。** 再加上我们的解码 94% 的时间花在等专家取数上，
而不是花在验证窗口里——所以「窗口开多深」这个旋钮能撬动的上限本来就很小。

三层原因，后面逐层给证据：

1. **可撬动的空间只有 ±5%**：按我们自己的 `(S, rows) → step_ms` 与接受率，L 的所有可选值里最优与次优只差几个百分点，
   而改造前的固定 L=4 已经在最优点附近。
2. **真实流量根本没走投机路径**：网页默认 `temperature=0.6`（采样），引擎见「采样槽」就整组退回逐 token 窗口
   （`generate.cpp:5821-5823`），普道 29,058 个窗口里只有 59 个是链式。变长预算对采样请求**零作用**。
3. **瓶颈不在验证窗口**：一个 batch 窗口 20.29 ms = run 19.43 ms（**等专家 11.91 + CPU 取专家 6.93**）+ commit 0.84
   + 发送 0.02。窗口里真正「算」的时间小到看不见。容器里 3 张卡共享一条 PCIe 与一个 CPU 池，
   解码是**专家搬运带宽问题**，不是投机调度问题。

---

## 1. 第一层：我们自己测出来的「选项空间」只有 ±5%

快道 A（`--mtp-varlen`，warm，11:36–13:58 的 `step-stats-a.jsonl`）按 `(slots, rows)` 分桶，
用同一窗口里的 `step_ms` 与接受 token 数算实际吞吐：

| S（槽） | L=1（rows=S） | L=2 | L=3 | L=4（rows=8 上限） |
|---|---|---|---|---|
| 1 | 71 tok/s（batch 路径） | 140 | **179** | **201** |
| 2 | 81 | 174 | **182** | 173 |
| 3 | 86 | **240**（rows=6） | —（GMAX=8 限制） | — |
| 4 | 80 | **241**（rows=8） | — | — |

读法：
- **S=1 越深越好，最好的恰好是 L=4 —— 也就是 `--spec 4` 的原设定**。GMAX=8 与 `chain_rows_per_slot=min(--mtp-max-t, --spec)=4`
  已经把窗口卡在 4 行/槽，规划器没有「更深」可选，最优点落在边界上。
- **S=2 的三个选项 174/182/173，差 5%**；S=3/4 只剩 L=1 与 L=2 两个选择，且批处理已经把基础开销摊薄，
  L=2 相对 L=1 靠的是「多出来的 token 是否比多出来的行便宜」。

为什么这么平？把「多一行」的边际收益与成本一起看（S=1，实测中位数）：

| 窗口 | 中位 step_ms | 平均产出 token | 每 token |
|---|---|---|---|
| rows=2（L=1） | 13.18 | 1.85 | 7.1 ms |
| rows=3（L=2） | 15.38 | 2.75 | 5.6 ms |
| rows=4（L=3） | 18.09 | 3.63 | 5.0 ms |

**每加一行约 +2.2~2.7 ms，换来 +0.8~0.9 个 token**（逐位置接受率 0.84 / 0.49 / 0.50）。
「多一行」的性价比在 1.4~1.6 之间几乎不变 —— 决策面天然是个平台，不是尖峰。
DeepInfer 的规划器要在这个平台上找最优点，我们的规划器也在同一个平台上，所以两者选出来的 L 才「一样好」。

**同一时刻的直接对照**（14:36，引擎同态，前后两分钟内）：

| 路径 | 中位 step_ms | token/窗 | 吞吐 |
|---|---|---|---|
| chain S=4 rows=8（投机） | 38.47 | 7.04 | **174 tok/s** |
| batch S=4 rows=4（逐 token） | 22.95 | 4.00 | **169 tok/s** |

四个槽同时跑时，「开投机」与「纯批处理」每 token 成本几乎相等（5.47 vs 5.74 ms）。这就是「没有差别」最直接的画像。

---

## 2. 第二层：网页流量的采样设置让投机整条路径被跳过

- 前端默认 `temperature: 0.6, top_p: 0.95, top_k: 20`（`Strata/serve/webapp/src/store.js:9`，`ChatView.vue:143-146` 原样下发）。
- 引擎在 `generate.cpp:7192` 记 `sl.sampled = !req_sp.greedy`；`chain_step` 一开头
  （`generate.cpp:5821-5823`）：

  ```cpp
  for (int k = 0; k < S; ++k) {   // v1: one sampled slot sends the whole group through the old path
      if (sl.active && sl.sampled) return one_token();
  }
  ```

  **只要组里有一个采样槽，整组退回逐 token 窗口**（`batch_step`，`drafts=0`）。
- 现场计数：普道 8092 的 29,117 个解码窗口里，`batch` 29,058、`chain` 59；那 59 个还是我用 `temperature=0`
  打的探针。快道 A 同期 4,478 chain / 4,633 batch（A 上有我的压测）。

后果：变长预算只作用于**贪心**请求；对网页（以及任何带 `temperature`/`top_p` 的 API 调用）完全无效。
这也是「改造前后网页速度没变」的**直接原因**，比第 1 层更硬。

顺带把「采样到底贵多少」量了（A 道，128 token，交替顺序，warm）：

| 工况 | 贪心（走链式） | 采样（走逐 token） | 差 |
|---|---|---|---|
| 4 路并发聚合 | **111.3 tok/s** | **92.8 tok/s** | **+19%** |
| 单路串行 | 87.4 tok/s | 85.0 tok/s | +3%（单路本来就靠 solo 路径的投机，两者都用 T≈2–3） |

引擎自己的每请求日志（单路贪心，128 token）：`108–111 tok/s，drafts accepted 76 of 116`（65%），
解码期专家缓存命中 **92.4–94.0%**。也就是说：**即使命中率九成以上，每 token 仍有几十次专家搬运**，
而这部分开销在链式与逐 token 两条路径上是同价的。

---

## 3. 第三层：时间花在「等专家」而不是「验证窗口」

引擎自己打印的窗口分解（普道 B，2026-10-21 14:0x）：

```
strata batch: 850 windows, avg 2.00 rows, 22.26 ms/window
  = run 21.39 (CUDA0 GPU-reach wait 13.53 + CPU experts 7.17) + commit 0.84 + emit 0.02
  per layer-window: CPU experts 2.51, VRAM hits 15.34, PCIe 2.09
```

- 22.26 ms 里 **21.39 ms 是「等专家到位」**（GPU 停在 flag A 上等专家池发布计划 + CPU 池取专家），
  真正提交 0.84 ms、发送 0.02 ms。
- 每层窗口 15.34 次命中、2.51 次走 CPU、2.09 次走 PCIe；一路乘 60 层，就是**每个窗口几百 MB 的专家搬运**。
- 3 张 4090D 共享一条 PCIe 链路、一个 CPU 池；专家 miss 的成本远大于「多验证 3 个 token」的算术成本。

这条解释了为什么**所有「减少计算量」的优化都收效有限**，包括投机、varlen、以及 DeepInfer 的 FP8/kernel 类优化：
我们的解码不是 compute-bound，是 **PCIe / 主机内存带宽 bound**。
它同时也解释了为什么**冷启动后头十几分钟特别慢**（专家 arena 从盘重建，0.07–0.14 GiB/s，见 `CURRENT-VERSION` §6）。

---

## 4. 第四层：DeepInfer 到底是什么形状，哪些能搬

（以下结论来自 `docs/references/DeepInfer-src/` 源码逐行核对，文件行号可查。）

### 4.1 它的「快」不是来自 MTP，而是来自整套软硬件共设计

| 层 | 它做了什么 | 对我们的可行性 |
|---|---|---|
| 硬件 | 只支持 **SM100（Blackwell）/ 昇腾 950**（`README.md:33-36`），构建要 CUDA 13 | ❌ 4090D 是 SM89/Ada |
| 注意力 | MLA + **DSA 稀疏索引器**（`v4_modules/index_attn.py:59`），`flash_mla_sparse_fwd` / `flash_mla_with_kvcache` | ❌ 模型结构不含索引器，vendor 子模块为空；Ada 无对应 kernel |
| 量化 | 权重 **FP8(e4m3)+专家 MXFP4**，block 32、`ue8m0` scale，**硬校验**（`hugging_face/config.py:254-264`） | ❌ 我们的是 IQ3_XXS；Ada 无 FP8 tensor core；全仓库无 GGUF/INT 路径 |
| 权重放置 | 所有参数必须 `p.is_cuda`（`model_executor/model_loader.py:76-79`），**没有 CPU offload / 分页** | ❌ 125B 在 3×24G 只能靠主机内存分页 |
| 专家并行 | 纯 EP，每 rank 只放 `n_routed_experts/ep_size`（`v4_modules/moe.py:94`），DeepEP all-to-all | ❌ 无 NVLink，3 卡；仓库无 PCIe 回退 |
| 图捕获 | decode 按 `total_q` 分桶 + padding（`worker/cuda_graph_runner.py:41,143`） | ✅ 我们已经在做（chain/batch 窗口图） |
| 调度 | chunked prefill、CPU/GPU 异步调度（`csrc/engine/scheduler.hpp:460-510`） | ✅ 思路可用；PD 分离未开源（`README.md:38-42`） |
| 投机 | **batched + 变长验证**：总预算固定，按 confidence 逐请求分 T，ragged verify（`csrc/engine/mtp_varlen.hpp:150-266`、`schedule_mtp_varlen.py:11`） | ⚠️ 我们只搬了「整组统一 T」的一半；逐请求 T 需要 ragged verify kernel |

### 4.2 它自己公布的收益也就是个位数

- 变长验证：验证长度 5.00 → 3.2–4.4（−30%），接受长度 3.29 vs 3.20，吞吐 **+4~7%**
  （`doc/inference_deployment_qa.md:245-258`，条件 EP64/DP64、每 rank ~60 条序列）。
  我们搬过来实测「持平」，与这个量级一致——本来就不是一个数量级的优化。
- decode 3k–26k tok/s/GPU、prefill 60k–89k tok/s/GPU 的成绩单来自
  **4 张 SM100 做 prefill + 64 张做 decode** 的集群（`README.md:83`），不是单机可比口径。
- 它的 MTP 头是模型自带的 DFlash（默认 nextn=5、一次并行出 5 个草稿），与我们的 Qwen MTP 草稿头不是一回事。

### 4.3 一句话

**DeepInfer 是「为 DeepSeek-V4.1-Flash + SM100 + 大集群」整体设计出来的系统，可摘取的只有调度与统计这两块；
而调度这块我们已经摘过了，它的收益上限是个位数百分比。** 它的架构没有「失效」，是没有一个能落在我们瓶颈上的着力点。

---

## 5. 那到底该往哪打（按性价比，全部需要重启=需要你同意）

1. **专家取数（最大头，94% 的窗口时间）**
   目标：把 `GPU-reach wait 13 ms/窗` 与 `CPU experts 7 ms/窗` 压下去。可动的：提高 VRAM 专家驻留/命中率、
   让下一层专家预取与当前层计算真正重叠（现在 GPU 是在 flag A 上空等）、`--batch-groups` 流水线拆分。
   先做**度量**：按层统计 `multi_misses / pcie_experts / ms_wait`，定位是「命中率低」还是「预取没重叠」。
2. **让采样请求也能走链式投机**（网页流量目前 0% 受益）
   需要：链式窗口里的逐行采样（batch 路径已有 `sample_rows`，链式只有 argmax，`verify.cpp:1822`）
   + 草稿采样（`STRATA_SPEC_COUPLED` 已有雏形，但目前只接到 solo 路径）。
   实测上限 +19%（4 路，warm）；S=1/2 与冷缓存时更高。工作量中等、要动 verify 与 MTP 两处。
3. **把「窗口外开销」量出来**：单路 87 tok/s，而链式窗口内部是 174–240 tok/s —— 有一半时间不在窗口里
   （admission / commit / solo 与 batch 的切换）。先加时间戳，再决定优化哪一段。
4. **代价曲线升级成二维 `(S, rows)`**（已知 `rows=3` 中位 38 ms > `rows=4` 19 ms 的非单调），小改动、可离线验证。
5. **ragged verify（逐请求不同 T）**：DeepInfer 的正牌玩法，但需要 ragged verify kernel，
   在 S≤4 的小 batch 上预期收益 ≈ 它的 +4~7% × 我们的规模 —— 排在最后。

**不建议**：为了追 DeepInfer 的绝对数字去换 FP8/稀疏注意力路线——那要先换模型、换量化、换硬件，等于重做项目。

---

## 6. 数据出处 / 复现

- 窗口统计：`~/logs/step-stats-{a,b}.jsonl`；`bench/deepinfer/analyze_steps.py`。
- 引擎窗口分解：`~/SSEStraBed/logs/strata-iq3_xxs-{a,b}.log` 的 `strata batch:` 与
  `strata serve: prompt … drafts accepted …` 行。
- 贪心 vs 采样：`/tmp/conc2.py`（4 路 128 token 交替）、`/tmp/serial2.py`（串行交替）——协议写在文件头，
  两侧请求内容完全一致，只差 `temperature`。
- 源码位置：`SSEStraBed/src/program/generate.cpp:5821-5823, 7192, 5781-5806`；
  `SSEStraBed/src/core/verify.cpp:1277, 1822`；`SSEStraBed/src/core/mtp.cpp:330-400`。
- DeepInfer 位置见 §4 表内行号，源码在 `docs/references/DeepInfer-src/`。