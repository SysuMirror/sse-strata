# P5：KV 只加容量 —— 三选一的实测结论

日期：2026-10-22（北京时间）
范围：只动 lane A（:8091，GPU0）。**不碰 decode 的 KV 读取路径**（QSA 稀疏选择已让 decode 与上下文解耦）。
配置：`~/deploy/engines/profiles/ab-novarlen/strata-a.json`。
数据：`~/docs/reports/data/p3-p5-*.json`。

---

## 0. 结论

| 杠杆 | 结果 | 判定 |
|---|---|---|
| ① `--kv k8v4`（816 B/cell，省 23%） | 不支持 `--kv-resident`（`generate.cpp:1816`），要求全部 KV 进显存 = +2.2 GiB VRAM；而本卡只剩 1.17 GiB，只能从专家缓存里抠（6226 → ~5400 槽，命中率下降） | **NO-GO** |
| ② 提高 `--kv-resident`（32768 → 65536） | **实测无收益**：主机池仍 3.09 GiB、显存仍 1172 MiB（几乎不变），吞吐反而略降（平台期 agg 42.9–45.3 vs 基线 48.0–52.2） | **NO-GO** |
| ③ 提高 `--max-context`（262144 → 524288） | **有效**：+3.10 GiB 锁页内存、显存 −2 MiB；单条 **353,443 token** 提示词正确作答；**2 × 199,550 并发**都成功；标准负载无退化 | **GO，已落在 lane A** |

一句话：**KV 容量真正的旋钮是 `--max-context`，不是编码格式，也不是驻留比例。**

---

## 1. 为什么 ② 完全无效（源码）

`src/core/layer.cpp:1067`：

```cpp
// KV streaming: the authoritative K/V of every cell, pinned and device-mapped, in the identity layout
const uint64_t bytes = (uint64_t) pages * strata::kernels::kv_block_bytes(s, qsa_kv_format(st)) + 4 * 256;
```

主机池按 **`pages`（= max_context 的全部页）** 分配，**与 `--kv-resident` 无关**；
`--kv-resident` 只决定其中多少页额外镜像到显存（`kv_plan()` 的 `p.slots`，`layer.cpp:830-835`）。

副作用：显存里那份镜像的尺寸在这张卡上几乎没有变化（实测 1170 → 1172 MiB free），
说明当前 batch 4 下显存 KV 池本来就不是按 kv-resident 线性增长的（受 plan 的页对齐与预算限制）。
所以这个参数**只影响「哪些块命中显存」**，不影响任何容量上限——实测的 8% 吞吐下降也说明调它没好处。

## 2. 为什么 ① k8v4 不划算

- 816 B/cell 对 int8 的 1056 B/cell，省 22.7%（`include/strata/kernels/kv_q4.hpp:43`、`kv_q8.hpp:31`）；
- 但它**拒绝流式**：`generate.cpp:1816-1817` + `layer.cpp` 的 `hybrid + mode==1` 直接返回 0；
- 全量 KV 进显存 = 262144 × 12 层 × 816 B ≈ **2.57 GiB**，现在显存已用满（只剩 1.17 GiB），
  必须从 10.08 GiB 的专家缓存里抠 1.4 GiB（6226 槽 → ~5400 槽）；
- 而**总量没有变**：max_context 仍是 262144。等于用命中率换「不用主机池」，净亏。

## 3. ③ 实测：256K → 512K

启动即见（`~/SSEStraBed/logs/strata-iq3_xxs-a-abprod.log`）：

| 项 | 262,144 | 524,288 | 变化 |
|---|---|---|---|
| 主机 K/V 池（锁页 + 设备可映射） | 3.09 GiB | **6.19 GiB** | **+3.10 GiB RAM** |
| 加载后剩余显存 | 1170 MiB | **1168 MiB** | −2 MiB |
| 容器可用内存 | 19 GiB | 17 GiB | −2 GiB |
| 每 QSA 层显存驻留 cells | 32,768 | 32,768 | 不变 |

### 3.1 单条超长提示词

原来 >262,144 的请求会被服务层拒绝；现在：

```
prompt 353,443 token → 122.5 s，答案正确
```

### 3.2 并发 200k（P5 的验收指标）

两条**各 199,550 token** 、并发到达、内容不同（各自带唯一秘密编号）：

| 请求 | 耗时 | 答案 |
|---|---|---|
| 秘密 3847 | 67.0 s | `3847` ✅ |
| 秘密 7295 | 131.5 s | `7295` ✅ |

合计 399,100 token **同时驻留**（旧配置 262,144 装不下，只能靠会话停车换出，第二条要等到 97 s 量级）。
两条耗时差别来自 **prefill 仍然串行**（P3 结论），不是 KV。

### 3.3 标准负载无退化

32 路并发、64 token、贪心；每臂取平台期（重启后丢前 3 轮）：

| 指标 | 262K（4 轮） | 512K（6 轮） |
|---|---|---|
| 聚合 tok/s（中位） | 48.97 | **51.65** |
| 每请求 tok/s（中位） | 15.40 | **16.75** |
| TTFT 中位（中位） | ~8.2 s | ~7.8 s |
| ITL 中位（中位） | 30.7 ms | 36.4 ms |
| ITL p90（中位） | 119.9 ms | 111.1 ms |

聚合吞吐与每请求吞吐**没有退化**（略高，在噪声内），TTFT 不变，ITL p90 持平；
唯一变差的是 **ITL 中位 +5.7 ms**。两臂不是同一天同一时刻测的（lane B 负载会漂），
所以按「**没有可测的退化，也没有可测的收益**」记，容量收益是确定的那部分。

---

## 4. 落地与回滚

lane A 当前配置（已验证 ready）：

```json
"--max-context", "524288", "--kv", "int8", "--kv-resident", "32768"
```

回滚（一行）：

```bash
python3 - <<'EOF'
import json
p='/home/cloud/deploy/engines/profiles/ab-novarlen/strata-a.json'
d=json.load(open(p)); a=d['args']; a[a.index('--max-context')+1]='262144'
json.dump(d,open(p,'w'),ensure_ascii=False,indent=2)
EOF
supervisorctl -c ~/deploy/supervisord.conf restart strata-a
```

**成本提醒**：512K 让 lane A 常驻多占 3.1 GiB **锁页**内存（不可换出）。容器总 216 GiB、
当时可用 17 GiB；若要再往上加（1M 需要再 +3.1 GiB），先看 `free -g` 和 lane B 的余量。

## 5. 下一步（不在本次范围）

- 真正的并发提升不在 KV，而在 **prefill 合并**（P3 §3，需内核级重构）；
- `--kv k8v4` 只有在换更大显存的卡、或把专家缓存让出来之后才值得重新评估；
- 会话缓存（`--conversation-cache-slots 20 --conversation-cache-mib 8192`）已经在用，
  长 prompt 重复时靠它省 prefill，别再加。