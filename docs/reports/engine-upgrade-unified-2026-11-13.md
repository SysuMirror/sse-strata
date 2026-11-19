# 引擎升级 0.1.38 → 0.1.59.1（含并发槽位 PR）与四臂实测

日期：2026-11-13（北京）
参与：Claude Code（执行）、用户（决策）

## 1. 做了什么

1. **备份**（`~/deploy/backups/engine-0.1.38-b7-20261113/`）
   - `SSEStraBed-src-0.1.38-b7.tar.gz`（源码，排除 build*）
   - `strata-0.1.38-b7`（线上二进制）+ `BUILD.json` + `SHA256SUMS`
   - `server.py.0.1.38`、`run-iq3_xxs*.sh`、`profiles/`、`active-a.env`、`active-b.env`、`supervisord.conf`
2. **拉取上游分支**：`shoucandanghehe/Strata:upstream/batch-mtp-multistep`
   （= 上游 main 0.1.59.1 + PR #5662 固定物理槽 + #5726 并发多步 MTP），经 `gh-proxy.com` 下载 tarball，
   解压到 `~/engines/source/strata-unified-0.1.59`，CUDA sm_89 编译通过。
3. **移植 fork 的三个自定义 flag**（`~/engines/test/fork-flags.patch`，只改 `src/program/generate.cpp` 五处）：
   - `--mtp-varlen` → 映射为上游 `--batch-mtp-multistep`（按每槽剩余预算/`--spec-min-p` 决定草稿深度）；
   - `--batch-mtp-sampled` → 接受；0.1.59 批链默认按槽采样（`set_slot_sampling`），不再门控；
   - `--mtp-chain-floor N` → 在每槽草稿深度上设下限 N，受 `--spec-1` 与该槽预算约束，结束/取消的槽不复活。

## 2. 四臂实测（GPU0 独占，8097，生产同规格 262144 上下文）

每臂：生产参数为底；1/2/4 路并发，每档 2 轮，每条请求唯一前缀（`bench.py`）。

| 臂 | 引擎 / 参数 | 1 路 TTFT | 1 路端到端 tok/s | 2 路聚合 tok/s | 4 路聚合 tok/s | 4 路单请求 tok/s |
|---|---|---|---|---|---|---|
| A | 现网 0.1.38（生产参数） | 1.70 s | 39.5 | 51.7 | 61.1 | 17.4 |
| B | 0.1.59.1 纯上游参数 | 1.58 s | 39.2 | 62.1 | **75.3** | 20.9 |
| C | B + `--unified-slots --batch-mtp-multistep` | 1.61 s | 38.7 | 66.5 | 60.3 | 16.1 |
| D | B + 三个 fork flag + `--unified-slots` | 1.57 s | 39.8 | **70.8** | 70.6 | 18.7 |

要点：
- 新引擎在 2/4 路并发上全面优于旧引擎（2 路 +20%~+37%，4 路 +15%~+23%）。
- `--unified-slots` 明显降低并发 TTFT（2 路 1.50→0.81~0.96 s；4 路均值 1.97→1.43 s）。
- 单请求吞吐四臂基本持平（端到端 39 tok/s 左右；解码口径 ~64–75 tok/s）。
- C 的 4 路（60.3）与 D（70.6）差距偏大，单轮 2 次采样，**噪声未排除**，需复测。

## 3. 口径校准（前端后台库 `~/logs/infer.db`）

`decode_tok_s = completion_tokens /（结束 − 首帧）`，纯解码口径；按时间窗判独占/并发：

| 分组 | 快道 8091 | 普道 8092 |
|---|---|---|
| 独占 median（近 14 天） | **122 tok/s** | **96 tok/s** |
| 并发 median（近 14 天） | 118 | 36 |
| 近 2 天独占 median | 无流量 | 94 |

结论：实验里的 39 tok/s 是**端到端**（含 TTFT），换成解码口径为 64–75 tok/s；与线上 94–122 的差距
主要来自实验期间 lane B 正在接真实流量（解码 76% 时间等主机 CPU 专家，两引擎抢 CPU/PCIe）、
测试实例专家缓存冷（命中 70–90%）以及合成提示词的 MTP 接受率差异。**四臂只作相对比较。**

## 4. 现场状态

- lane A（8091）、lane B（8092）均已恢复为现网 0.1.38 引擎；实验实例已停止。
- 新引擎二进制：`~/engines/source/strata-unified-0.1.59/build/strata`
- 实验配置与结果：`~/engines/test/`（`results/arm-*.json`、`run_all.log`）

## 5. 待定

- 是否把 lane A 切到新引擎（需决定 server.py 用现网 fork 版还是上游 0.1.59 版；前者协议兼容性未验证）。
- C/D 的 4 路差异需要复测（lane B 空闲窗口、4–6 轮、512 token）。
## 6. 事故与教训（2026-11-13 15:4x UTC）

- 实验期间（GPU0 跑测试实例、GPU2 跑兼容性验证）同时有 3 个进程在读同一份 71 GB 权重，
  **lane B 的引擎触发内置看门狗**：`no progress for 60 s during a request (reading the prompt
  (batched): layer 1 ...) - stopping the engine so the server starts it again (issue #29)`，
  引擎自杀、服务端自动重载，8092 在重载期间请求挂起（实测一次请求 60 s 超时）。
- 处置：立即停掉 GPU2 兼容性验证实例；lane A 随后加载完成并恢复 8091；lane B 由服务端自动重载恢复。
- 教训：**引擎加载会独占磁盘/主机内存带宽**，任何"起第二个引擎实例"的实验都要在 lane B 空闲窗口做，
  或先停一条 lane；不能让第二个 71 GB 的加载和线上请求并行。兼容性验证（现网 server.py + 新引擎）
  因此中止，需在安静窗口重做。

## 7. lane A 切换到新引擎（2026-11-14 00:37 北京）

- 新 profile：`deploy/engines/profiles/unified/`（profile.json + strata-a/b.json，exe 指向 0.1.59.1 构建，
  lane a/b 都加 `--unified-slots`；server_py 仍用现网 `Strata/serve/server.py`）。
- `switch_engine.py switch unified --lanes a --yes`：drain → 停 lane A → 写 `active-a.env` → 启动 → `/health` → 1-token 冒烟
  **全部通过**，`lane 8091 ready ... engine=0.1.59.1 engine_version=legacy-0.1.59.1-sha.b81bcaa4`，代理 health 两条 upstream 均 ok。
- 结论：**现网 0.1.38 的 server.py 与 0.1.59.1 引擎协议兼容**，换引擎不必换服务端。
- 回滚：`switch_engine.py rollback --yes`（active-a.env.prev 指回 ssebed 0.1.38）。

## 8. 两卡合并实验（未跑通，2026-11-14 01:0x 北京）

双 lane 全停后用 GPU0+1 试了两种"两卡一起"的接法：

| 接法 | 参数 | 结果 |
|---|---|---|
| 层切分 | `--layer-split 24`（每卡 24 层） | 引擎按卡加载完权重后以 `cannot create the main stream` 退出（CUDA stream 创建失败；代码未打印 CUDA 错误码） |
| 专家缓存外挂 | `--expert-cache-device1 auto` | 引擎拒绝：`--expert-cache-remote with a layer split needs a GPU that runs no stage` —— 现网 `server.py:1102` 对多 GPU 配置会自动补 `--layer-split auto`，与 helper 冲突；上游的正确表达是 `--peer-device 1` |

- 单独小 CUDA 程序验证：三张卡各自 `cudaStreamCreateWithFlags` + `cudaMalloc` 均正常，不是容器 CUDA 问题。
- 结论：**两卡合并目前不是即插即用选项**；要回答"合并是否更好"，需要（a）给建流失败补 CUDA 错误码后定位，
  （b）用 `--peer-device 1` + 上游 server 的重试窗口。
- 理论判断：合并的潜在收益 = 48 GB 显存把专家缓存从 ~10 GB 抬到 ~40 GB（专家总量 47 GB），直接减少
  解码时的主机专家搬运（解码 94% 时间在等专家）；代价是失去双 lane 独立性与滚动升级能力。

## 9. 两卡合并实测（2026-11-14 01:2x–01:5x 北京，双 lane 全停、静默条件）

同一份权重、262144 上下文、batch 4、MTP、唯一前缀提示词、每档 2 轮：

| 部署 | 1 路 TTFT / 聚合 | 2 路 TTFT / 聚合 | 4 路 TTFT / 聚合 | 4 路单请求 | 4 路 ITL（解码口径） |
|---|---|---|---|---|---|
| 层切分 `--layer-split 24` | 1.03 s / 62.4 | 0.77 s / 92.4 | 1.16 s / **114.6** | 30.6 | 25.0 ms ≈ 40 tok/s |
| 专家缓存层 `--peer-device 1` | 1.51 s / 50.1 | 0.85 s / 94.2 | 1.44 s / 95.6 | 25.8 | 28.9 ms ≈ 35 tok/s |
| 单卡对照（同条件，arm D 配置） | 待填 | 待填 | 待填 | 待填 | 待填 |
| 参考：单卡（有 lane B 干扰，臂 D） | 1.57 s / 38.6 | 0.96 s / 70.8 | 1.43 s / 70.6 | 18.7 | 15.6 ms ≈ 64 |

- 层切分在 4 路聚合上比 peer 高约 20%，单请求也更快；两者都远高于"有干扰"的单卡数字。
- 关键待确认：同条件的单卡对照（避免把"机器静默"的红利算到两卡头上）。

## 10. 8 路对比（用户要求）：两卡单实例 vs 两条 lane

两卡层切分 + `--batch 8`（每卡 8 个槽会话，各 0.51 GiB，卡上余 ~15 GB 专家缓存），静默条件、每档 2 轮：

| 并发 | TTFT 均值/最大 | 聚合 tok/s | 单请求 tok/s | ITL（解码口径） |
|---|---|---|---|---|
| 1 | 1.18 / 1.31 s | 68.7 | 70.3 | 8.0 ms ≈ 125 |
| 2 | 0.85 / 1.55 s | 94.5 | 50.1 | 14.5 ms ≈ 69 |
| 4 | 1.26 / 2.61 s | 110.6 | 29.9 | 25.8 ms ≈ 39 |
| 8 | 2.28 / 5.85 s | **107.7** | 14.5 | 55.4 ms ≈ 18 |

- 两卡实例在 4 路左右到顶（~110），8 路只增加排队（TTFT 2.3 s 均值、5.8 s 最长）。
- 两条 lane 的 8 路基线（4+4 分散到 8091/8092，两 lane 均为 0.1.59.1 + unified）见下一节。

## 11. 关键对比：两卡合并 vs 两条 lane（同为 8 路、同为新引擎）

条件：两条 lane 均为 0.1.59.1 + `--unified-slots`（batch 4/道），两卡单实例为 `--layer-split 24` + `--batch 8`；
上游对外流量已关闭，客户端每条请求唯一前缀、1200 token 提示、256 输出、每档 2 轮。

| 部署 | 4 路聚合 | 8 路聚合 | 8 路 TTFT 均值/最大 | 8 路单请求 tok/s |
|---|---|---|---|---|
| **两条 lane 分别跑**（2+2 / 4+4） | 107.8 | **141.6** | 1.69 / 2.63 s | 20.1 |
| **两卡合并**（层切分单实例） | 110.6 | **107.7** | 2.28 / 5.85 s | 14.5 |
| 对比 | 基本持平（−2.5%） | **合并低 31%** | 合并更差 | 合并低 28% |

结论：**把两卡从「分别部署」改成「一起部署」并不会让并发更好**——4 路时两者持平，8 路时合并反而低约 31%，
TTFT 也更差。原因：合并只增加了一倍的专家缓存，但少了一套独立的调度/prefill 通道，并引入每层的跨卡
handoff；解码本就受主机专家带宽限制，缓存收益抵不过调度与通信开销。

两卡合并唯一明确领先的是**单请求解码速率**（1 路 ITL 8.0 ms ≈ 125 tok/s，两条 lane 单独约 60–70）。
若未来主打「少量用户但要快」，可以再评估；主打并发则保持两条 lane。
