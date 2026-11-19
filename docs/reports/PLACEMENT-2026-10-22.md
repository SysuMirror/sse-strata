# P1：专家放置（placement）能不能把 CPU 专家的活减下来？—— NO-GO

日期：2026-10-22（北京时间）
结论一句话：**换放置策略最多把命中率从 91.6% 抬到 91.7%（+0.12 个百分点），不值得做。**
真正的杠杆是「每个 miss 的字节数 / 每次 miss 的固定开销」，那属于 P4，不是 P1。

## 1. 问题

引擎现在按 `expert-profile.bin` 的全局排名顺序做**字节打包**（`generate.cpp` 的 sized_slots 逻辑），
显存预算内装下多少专家就装多少，剩下的每次 decode 从主机内存 / PCIe 读。

P1 的原假设：**放置顺序选得更好，常驻集能覆盖更多路由 → CPU 专家副本的 miss 变少 → 每步更快**。

要回答的是：在同样的显存预算下，换一种选择策略能多拿多少命中率？

## 2. 方法（离线求解器）

工具：`~/tools/placement_solver.py`（本次已归档到
`~/archive/patches/p1-placement-2026-10-22/`）。

- 规模：48 层 × 512 专家 = 24,576 个专家槽；每层 blob 字节数取自
  `/home/cloud/Strata-data/packs/iq3_xxs/native_experts.txt`（按 256 B 向上取整，与引擎口径一致）。
- 预算：`cap = 10.55 GiB`（lane A 启动日志：free 12.15 GiB − `--vram-reserve-mib 1600`）。
- 概率模型：名次 Zipf `p ∝ 1/(rank+1)^α`，α 用**实测命中率 0.916** 标定（lane A 日志的
  `decode expert cache hit rate` 中位数），α = 1.078。
  ⚠️ 这是**模型**，不是真实热度；`expert-profile.bin` 只存名次不存概率。
- 三种策略：
  - `current`：按 profile 名次顺序做字节打包（引擎现状，基线）
  - `quota`：每层等字节配额，层内按名次
  - `ratio`：按 收益/字节 贪心

## 3. 结果

```
model=global alpha=1.078 cap=10.55 GiB
current: 6518 experts  10.55 GiB  预测命中 0.9160  miss 8.40%  平均 miss blob 1.668 MiB
quota  : 6714 experts  10.51 GiB  预测命中 0.9163  miss 8.37%  平均 miss blob 1.711 MiB
ratio  : 6743 experts  10.55 GiB  预测命中 0.9172  miss 8.28%  平均 miss blob 1.713 MiB
```

- 最好策略（ratio）相对现状：**+225 个专家（+3.5%），命中率 +0.12 pp**。
- 结构上限：10.55 GiB ÷ 最小 blob 1.25 MiB = **8,676 个专家 = 全部 24,576 的 35.3%**。
  也就是说任何放置策略都装不下三分之一以上的专家。
- 每步 CPU 字节（模型估算，25 GB/s）：S=1 0.066 GiB / S=4 0.263 GiB / S=8 0.525 GiB，
  三种策略之间差异 < 2%。

## 4. 判定：NO-GO

命中率的差异（0.916 → 0.917）在测量噪声之内（同进程内实测命中率在 84%–91% 之间抖动，
见 `~/docs/reports/PARALLEL-BASELINE-2026-10-21.md`）。改放置顺序不解决问题。

不做的理由不止「收益小」：
1. 需要重排 `expert-profile.bin` 的生成路径（`make_profile.py`）+ 打包路径（`generate.cpp`），
   会改动启动期显存布局，回归面大（放置错位 = 直接崩或读错权重）。
2. 收益上限被显存边界锁死：26.5% → 35.3% 是天花板，命中率上限也就 ~0.93。

## 5. 下一步（P4 的方向，不是 P1）

真正要减的是**每次 miss 的代价**，不是 miss 次数：
- miss 字节数：同一个专家在一个窗口内被多个 token 命中时，只读一次（P2 的按专家分组 + 一次 blob 读取）。
- miss 固定开销：每层门铃串行、kStagingBlobs=16 的暂存环、CPU job 的分块与同步
  （见 `memory/strata-concurrency-limits.md`）。
- 真实热度替代模型：若要做放置，必须先有 `--dump-routing` / `--expert-profile-save` 的真实轨迹，
  再用 `placement_solver.py --trace` 复算；没有轨迹之前，一切放置结论都是模型外推。

## 6. 出处

- 求解器：`~/tools/placement_solver.py`（本次归档 `~/archive/patches/p1-placement-2026-10-22/`）
- 输入：`/home/cloud/Strata-data/packs/iq3_xxs/native_experts.txt`、
  `/home/cloud/Strata/data/expert-profile.bin`
- 命中率基线：lane A 引擎日志 `~/SSEStraBed/logs/strata-iq3_xxs-a-abprod.log`
  （`decode expert cache hit rate`）
- 并发/解码瓶颈背景：`memory/strata-concurrency-limits.md`、`docs/reports/WHY-NO-GAIN-2026-10-21.md`