# PR #559（batch slots）审查记录

审查对象：blange48/Strata PR #559 @ `bd400f77`（base = 本地 `99f3dbd`，+2057/−110，17 文件）
方法：两路独立静态审查（主会话逐行 + 独立对抗性审查代理）；未编译未运行；复核了关键指控的源码行。

## 结论

- **引擎侧（C++：verify.cpp / generate.cpp / 状态与快照）——两路审查均未发现内存安全、越界、算术缺陷**：
  - 定长缓冲全部 ≥ 批上限 8（`kVerifyMaxT=8`、`MAXT=8`、`b_out_[8]`、`max_t_=max(spec,batch)`）；
  - 槽位状态覆盖完整（准入经 checkpoint+KV 全量恢复，K/V 池、索引器、GDN、PLE ring 全覆盖）；
  - 协议两端逐字对齐（BGEN/BADM/BT/BDONE/BSTOP；STOP→"cancel" 语义是"单路→批槽迁移"能成立的关键）；
  - **不开 --batch 时行为与现状一致**（回滚安全性成立）。
- **服务端（Python）在 --batch 模式下存在并发生命周期缺陷**（去掉 FIFO 串行化后的新竞态面）：

| # | 严重度 | 缺陷 | 触发条件 | 我们的暴露 |
|---|---|---|---|---|
| 1 | **Major** | 引擎崩溃→自动重启时，在途批请求读**新**队列（`restart()` 重建 `slot_q`，请求侧按属性重读）：可跨请求串流（一个客户端收到别人的 token）+ 挂死 | 引擎死亡 + 在途批请求 | **必须修**（我们 RAM 顶格，引擎崩溃非罕见） |
| 2 | Major | `Service.unload`（显式 /unload 或 idle 定时器）可与在途批请求并发（batch 模式不再拿 fifo，busy 又被先完成者清掉） | 调用 /unload 或开 idle_unload_s | 潜伏（我们 `idle_unload_s=0`，无人调 /unload） |
| 3 | Major | --batch 路径未接 #481 的"引擎沉默看门狗"（solo 有 EngineSilent，批路径只留 10s 心跳） | 引擎活着但沉默 | 部分被引擎自身 no-progress 看门狗兜底 |
| 4 | Minor | `engine.last`/status 在并发请求间串读（finish/timings 归错人；busy 被先完成者清掉——也是 #2 的使能条件） | 任意并发 | 统计/监控失真为主 |
| 5 | Minor | 触发 STOP 的请求已离开时，solo 仍被迁移进批槽（无草稿，纯吞吐损失） | 常见 | 可接受 |
| 6 | Minor | solo 只在 token 边界被 STOP；长 prompt 读取中不响应（准入延迟） | 第二条请求在长 prompt 中途到达 | 可接受（行为已文档化近似） |
| 7 | Minor | 引擎 ERR 后批路径要先把 300s drain 超时走完才报错（持有 ctl，堵住其他请求） | 引擎报错 | 错误路径，低概率 |
| 8 | Minor | 单个批窗口失败会终止整个引擎（非批模式是 continue） | 真实错误 | 低概率；错误后一键重启可恢复 |
| 9 | Nit | server 硬编码 `EOS_IDS={248044,248046}`（= 引擎默认 --eos-ids，我们不改） | — | 无 |

## 部署前加固补丁（仅 server.py，纯 Python，无需重编引擎）

1. **#1 修复（必须）**：引入 `self.epoch`（`__init__` 里自增）；`generate_batched` 在持 ctl 后捕获 `epoch0`，每次循环迭代、每次队列读取前校验 `self.epoch == epoch0`，不等则 `EngineDied`（早失败，不串流）；`_release_slot_when_done` 捕获队列引用与 epoch；`waiting` 递减改为按 epoch 校验后执行（防负数）。
2. **#2/#4 修复（建议）**：维护 `active_batch` 计数（批请求进出各 ±1）；`Service.unload` 在 `active_batch>0` 时拒绝；`busy=False` 只在 `active_batch==0` 时置位；`engine.last` 归属用 epoch+请求 id 校验。
3. **#3 修复（可选）**：批路径的心跳循环接入 `silence_s`（超时抛 EngineSilent）。

## 集成后必做的验证（编译 + 实测阶段）

- [ ] 引擎编译通过 + `--batch` 出现在 `strata --help`
- [ ] PR 自带 `tools/batch_test.py`：批 vs solo **逐 token 一致**（需在服务停止的窗口跑，它要独占 GPU）
- [ ] HTTP 实测：1/2/4 路速度矩阵（每路 + 总吞吐）、多波次换槽（2 槽跑 3+ 请求）、断连（early_close 模式）
- [ ] **加固 #1 的专项测试**：2 个批请求在途时 `kill -9` 引擎 → 两个请求必须以 EngineDied 干净失败，无串流、无永久挂起
- [ ] 回滚演练：恢复 `engine/strata.pre-batch` + 去掉 `--batch` 即回到现状
