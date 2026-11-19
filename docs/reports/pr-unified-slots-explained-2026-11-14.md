# 合并进来的上游 PR 到底改了什么、为什么并发更好

对象：上游 `shoucandanghehe/Strata` 的 **#5662 固定物理槽（`--unified-slots`）** 与 **#5726 并发多步 MTP
（`--batch-mtp-multistep`）**，连同上游 main 0.1.59.1 的批调度重写，一起并入了我们的引擎
（`~/engines/source/strata-unified-0.1.59`，已作为新引擎部署到两条 lane）。

## 1. 旧引擎（0.1.38）在并发下做了什么"多余的事"

旧版服务循环的形状是「**主会话 + 批槽**」两套状态：

1. 一个请求单独来 → 走 solo 路径（`GEN`），跑在主会话 `ss` 上，使用单序列 kernel 和完整 MTP 窗口；
2. 第二个请求来了 → 服务端把第一个请求 `STOP`，再用 `BGEN` 带着"提示词 + 已生成的 token"重新进槽；
   引擎要把它在主会话上的状态**搬到槽会话**：`copy_to_slot()` 对每层做
   `conversation_checkpoint_save/restore` + `conversation_kv_save/restore`（逐层 KV 拷贝），
   还要重新读一遍前缀；
3. 槽里只剩它自己时，服务端又可能让它回 solo（`_may_go_solo`），再搬回去；
4. 批窗口按"最高活跃槽 + 1"组行，中间的槽补 pad token —— 空槽也在算。

也就是说，**并发数每变一次，就有一次状态迁移**；迁移是逐层 KV 拷贝 + 前缀重读，正是 TTFT 和吞吐的主要浪费。
另外旧版 `--batch-mtp` 在批槽里**每个槽只验 1 个 MTP 候选**（单步投机），并发时投机收益几乎归零；
且批链只允许贪心槽（采样槽整组退回单 token 路径）。

## 2. #5662 改了什么（固定物理槽）

核心一句话：**请求从读 prompt、解码到结束，永远待在它自己那个物理槽里；邻居来去只改计算批，不动它的状态。**

具体改动（按代码路径）：

- **服务端不再"solo 优先"**：引擎 READY 时打印 `INFO unified_slots=1`；服务端看到后
  `if alone and left > 1 and info.unified_slots != 1` —— 永远发 `BGEN` 从起始就进槽；
  `_may_go_solo` 也直接返回 False。于是旧版「GEN → STOP → BGEN 前缀重读」和「槽 → solo 回迁」两条路径
  整体消失。
- **请求级 owner 指针**：GEN/BGEN 入口把 `request_state / request_draft / request_df2` 指向该槽自己的
  会话 / MTP drafter / DFlash2 drafter；槽会话有自己的 PLE 历史指针（`ple.hist/prev/token`），
  不再和主会话共享。
- **验证器重绑**：`Verifier::bind_session(ss)` 把空闲的单级验证器绑到"同样 carve 出来的"槽会话上，
  销毁捕获了旧地址的 solo 图，保留显式槽地址的批图。
- **单序列 kernel 在槽内运行**：新增 `Verifier::run_slot()` —— 只剩一个活跃请求时，用单序列 kernel
  在那个槽里跑（旧版只能整批窗口跑），commit 走 `commit(keep) + wait_commit`。
  这样"独处"不再意味着"回 solo 迁移"，而是**原地全速**。
- **每槽私有状态**：
  - MTP drafter 新增自有 `logit_bias` 设备副本 `cbias_`（`set_draft_sampling(sp, err)` 先等空闲再拷），
    并新增 `prepare_draft()` / `chain_launch(..., coupled)`，让每个槽的草稿链带着该请求的采样参数；
  - 视觉位置表从"全局一张"改成**会话级**（`session_mrope_init/upload/reset` + `MropeScope`），
    邻居切图/切文不再互相踩；
  - DFlash2 每槽一个 drafter（`fixed_df2`）。
- **预填 yield 留在原槽**：并发时预填被抢占，状态（`owner.partial/partial_plan`）记在该槽，
  恢复时回同一个槽，不重新读前缀。

## 3. #5726 改了什么（并发多步 MTP）

- 新 flag `--batch-mtp-multistep`（需要 `--unified-slots --batch-mtp`）：把"并发槽每槽最多验 1 个候选"
  放开到 **`--spec - 1`**；
- 每槽的草稿深度由 `slot_draft_limit(sl)` 决定 —— 受该请求**剩余输出数、剩余上下文**约束，
  并受 `--spec-min-p`、`--mtp-max-t` 限制；窗口按 `proposal_budget[]` 记录每槽的提案预算；
- 统计口径从"行数"改成"押出的候选数 `bt_tokens`"，避免把被拒绝的草稿算成收益。

一句话：并发时投机解码不再退化成"每槽一个 token"，而是**按每个请求还剩多少空间，尽量多地押注**。

## 4. 为什么并发更好（机制 → 数字）

| 机制 | 旧 0.1.38 | 新 0.1.59 + #5662/#5726 |
|---|---|---|
| 并发变化时的状态迁移 | 逐层 KV 拷贝 + 前缀重读 | **无**（请求原地不动） |
| 独处请求 | solo 路径 / 或批路径补 pad 行 | 槽内单序列 kernel |
| 并发时的投机深度 | 每槽 1 个候选，采样槽整组退回 | 每槽最多 `--spec-1` 个候选，按预算 |
| 批窗口行布局 | 最高活跃槽 +1，空槽补 pad | 稀疏槽位 + 轮转，按活跃槽组行 |
| 视觉/采样状态 | 全局共享 | 每槽私有 |

实测（GPU0 独占、同配置 262144 上下文、每档 2 轮；数字为端到端聚合 tok/s）：

| 并发 | 旧引擎 A | 新引擎纯上游 B | 新引擎 + unified + multistep D |
|---|---|---|---|
| 1 路 | 39.5 | 39.2 | 39.8 |
| 2 路 | 51.7 | 62.1 | **70.8** |
| 4 路 | 61.1 | 75.3 | 70.6（该轮波动） |

- 纯升级 0.1.59 就已把 2/4 路聚合提高 **20–23%**（批调度重写、稀疏槽位、按槽采样）；
- 加上 `--unified-slots` 后 **2 路 TTFT 从 1.30–1.50 s 降到 0.81–0.96 s**，
  4 路 TTFT 均值到 1.43 s（旧 1.97 s）——直接对应"不再迁移状态"；
- `--batch-mtp-multistep` 让"独处/并发"都能押更多候选，2 路聚合到 70.8。

需要注意的边界：**合并两卡成一个实例并不会更快**（8 路：双 lane 141.6 vs 合并 107.7 tok/s，见
`engine-upgrade-unified-2026-11-13.md` 第 11 节）。这些 PR 优化的是"一条 lane 内的并发行为"，
不是"把两张卡拼起来"。

## 5. 我们额外做的 fork 兼容

三个旧 flag 在新引擎上的映射（`generate.cpp` 五处改动，`~/engines/test/fork-flags.patch`）：

- `--mtp-varlen` → 映射为 `--batch-mtp-multistep`（0.1.59 的按预算草稿深度）；
- `--batch-mtp-sampled` → 接受但不再门控（0.1.59 批链本来就按槽 `set_slot_sampling` 采样）；
- `--mtp-chain-floor N` → 在每槽草稿深度上设下限 N（受 `--spec-1` 与该槽预算夹住，结束/取消的槽不复活）。

## 6. 源码位置（新引擎树）

- 服务端：`serve/server.py`（`unified_slots` 判断、`generate_batched`、`_may_go_solo`）
- 引擎主循环：`src/program/generate.cpp`（`request_state/request_draft/request_df2`、`window_mtp`、
  `multistep_window`、`slot_draft_limit`、`proposal_budget`、`run_slot`、FF2 槽位）
- 验证器：`src/core/verify.cpp`（`bind_session`、`run_slot`）、`include/strata/core/verify.hpp`
- MTP：`src/core/mtp.cpp`（`set_draft_sampling(sp,err)`/`cbias_`/`prepare_draft`/`chain_launch(coupled)`）
- 会话视觉：`src/core/session.cpp`（`session_mrope_init/upload/reset`）、`include/strata/kernels/mrope.hpp`