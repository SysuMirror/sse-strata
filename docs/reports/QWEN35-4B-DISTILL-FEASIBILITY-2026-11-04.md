# Qwen3.5-4B 下载与 GPU2 训练容量评估

- 日期：2026-11-04（北京时间）
- 目的：为「把 qwen-3.8-flash 蒸馏到小模型」选定学生底座，并评估停掉邻居 TTS 后 GPU2 能否承载训练。

## 1. 选型与下载

| 项 | 值 |
|---|---|
| 模型 | `Qwen/Qwen3.5-4B`（ModelScope） |
| 本地路径 | `/home/cloud/models/Qwen3.5-4B` |
| 许可 | Apache-2.0 |
| 参数量 | 4.66B（文本 4.21B + MTP 0.12B + 视觉塔 0.33B） |
| 权重体积 | 9.32 GB（bf16，2 个 safetensors 分片） |
| 上下文 | 原生 262,144，可扩展至 ~1,010,000 |
| 架构 | hybrid：24 层 GDN 线性注意力 + 8 层全注意力（3:1），head_dim 256，vocab 248,320，带 MTP 与视觉塔 |

下载于 2026-11-04 15:46 UTC（32 分 36 秒）完成，共 14 个文件。两个分片与 index 的
SHA256 均与 ModelScope 官方值逐字节一致；safetensors header 解析出 4.66B 参数
（文本 4.21B + MTP 0.12B + 视觉 0.33B，按 bf16 计）。HF 的
`Qwen3_5ForConditionalGeneration` 会忽略 `mtp.*`（`_keys_to_ignore_on_load_unexpected`），
训练时实际加载约 4.54B / 9.08GB。

为什么是 4B：

- Qwen3.5 家族只有 **2B / 4B / 9B / 27B**，没有 3B；4B 是「3B 左右」里唯一与
  Qwen3.8-Flash-Next 同谱系的（同为 GDN+全注意力 3:1、head_dim 256、vocab 248,320、MTP、262K、vision）。
- Qwen3-4B（经典 dense，36 层 GQA）与 Qwen2.5-3B 都不是这个架构；作为 Flash-Next 的蒸馏学生，
  Qwen3.5-4B 的架构亲和性最好，后续转 GGUF（`qwen35` / `qwen3vl`）也有现成转换路径
  （`third_party/llama.cpp/conversion/__init__.py` 已映射 `Qwen3_5ForConditionalGeneration`）。
- 若想更省显存，可再下 `Qwen/Qwen3.5-2B`（同架构，权重约 4.7GB，ModelScope 可下）。

## 2. GPU2 现状（2026-11-04 15:20 UTC 实测）

物理卡：RTX 4090D，nvidia-smi 总显存 24,564 MiB（torch 视角 24,080 MiB）。

| 项 | MiB |
|---|---|
| 当前已用 | 19,279 |
| 当前可用 | ~5,285（torch `mem_get_info` 约 4,456） |
| 邻居 TTS：3 × `gpt-sovits-infer` | 2,964 + 2,948 + 2,936 = **8,848** |
| 其他 pod 租户（非 TTS，不可回收） | ~10,416（4,802 / 2,216 / 2,052 / 732 / 580 / 34） |

**停掉 3 个 TTS 实例后**：已用降到约 10,431 MiB，**空闲约 13.3 GB（13.0 GiB，torch 口径）**。
`voice-studio` 不占大显存，不用停。

> TTS 是邻居 pod（`group-gpt-sovits-*`）的服务，停之前必须征得同意；
> `supervisorctl stop gpt-sovits-infer gpt-sovits-infer-2 gpt-sovits-infer-3`，
> 用完 `start` 还回去。详见 memory `gpu2-tts-neighbor`。

## 3. 训练可行性

学生底座：4.66B（文本 4.08B），bf16 权重 9.32GB。GPU2 预算按停 TTS 后 **~13GB** 计（保守）。

| 方案 | 显存估算 | 结论 |
|---|---|---|
| **QLoRA（NF4 4bit + LoRA r16）** | 权重 ~3.5–4.5GB + 激活/logits ~4–6GB ≈ **8–10GB** | ✅ 推荐；batch 4–8 @ 2–4k，开梯度检查点 |
| LoRA（bf16 底座，不量化） | 8.2–9.3GB + 激活 ~3–4GB ≈ **11–13GB** | ⚠️ 临界；只能 seq ≤2k、micro-batch 1–2，邻居一涨就 OOM |
| 全参 bf16 + AdamW | 权重 8.2 + 梯度 8.2 + Adam fp32 32.6 ≈ **49GB** | ❌ 不可能 |
| 全参 + 8bit Adam | ≈ **25GB+** | ❌ 24GB 卡本身就装不下 |
| 从零预训练 | 远超 | ❌ 不可能 |

要点：

- **vocab 248,320，logits 很大**：batch×seq×vocab 的 logits 必须分块算 CE/KL（如 512 token 一块），
  否则单是 logits 就能吃掉数 GB。
- 文本蒸馏可只加载文本塔、冻结视觉塔，省 ~1.2GB。
- 若一定要全参训练，只有更小的 2B + DeepSpeed CPU offload 这条路（优化器状态放主机内存，
  主机当前只剩 ~36GB 可用，速度会明显变慢），4B 即便 offload 也放不下。
- 粗估吞吐：4090D 上 4B QLoRA 约 1–2k tok/s，1000 万 token 约 2–3 小时/轮（仅供排期）。

## 4. 蒸馏路线的一个硬约束

当前 `Strata/serve/server.py` 没有实现 `logprobs` / `top_logprobs`，所以**拿不到 teacher 的逐 token
分布**，做不了经典 logit KD。可行的是**序列级蒸馏**：

1. 用线上 lane（8091/8092，含 `/v1/chat/completions/batch`）批量生成 teacher 回答；
2. 学生 SFT / LoRA 学这些回答。

瓶颈在 teacher 生成：单 lane 约 70–140 tok/s，生成 100 万 token 就要数小时；学生训练反而快。

## 5. 风险与前提

- GPU2 与其他 pod 共享、无硬隔离；停 TTS 后仍有 ~10.4GB 被别的租户占着，可能波动，训练要留余量。
- 内存：pod cgroup 192/216GB，宿主机只剩 ~36GB 可用，DataLoader worker 要少、pin_memory 慎用。
- CPU：cgroup 配额 32 核，两条推理 lane 已按核绑定；训练别开太多 worker，避免拖慢线上。
- 训练栈：`transformers 5.22.1` 已含 `models/qwen3_5`（dense+MoE），当前 `llm-lab/.venv` 尚未安装；
  peft / trl / bitsandbytes 均有可用版本。
- 部署回引擎：本地 `SSEStraBed/build/strata` 二进制里没有 `qwen35`，本地树也没有 `src/qwen35`；
  要上我们的引擎需并入上游 qwen35 路径并转 GGUF。训练本身不受影响。

## 6. 结论

**停 TTS 后 GPU2 的 ~13GB 余量足够做 4B 的 QLoRA（推荐）或（很紧的）bf16 LoRA 蒸馏训练，
但不足以做全参微调或预训练。** 若目标是全参，需要换卡/借卡，或退到 2B + CPU offload。

## 7. 下一步

1. 征得同意后停 TTS，跑一次「4bit 加载 + 单步前反向」实测，校准上表数字；
2. 装训练栈（transformers/peft/bitsandbytes/trl）——纯 CPU，不占卡；
3. 定蒸馏数据方案（teacher batch 生成 → 学生 LoRA）。