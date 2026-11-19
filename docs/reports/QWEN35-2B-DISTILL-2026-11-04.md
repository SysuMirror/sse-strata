# Qwen3.5-2B 蒸馏流水线（自我认知：小秋）

- 日期：2026-11-04（北京时间）
- 目标：以线上 qwen-3.8-flash-next（卡0快道 8091）为 teacher，蒸馏到 Qwen3.5-2B；
  同时把自我认知改成「小秋」，训练过程在网页训练页可见。
- 硬件：GPU2（TTS 已停，torch 可用 ~13.0 GiB）；teacher 走 GPU0（快道）。

## 1. 资产

| 项 | 路径 |
|---|---|
| 学生底座 | `/home/cloud/models/Qwen3.5-2B`（2.274B，bf16 4.55GB，SHA256 已校验） |
| 4B 备选 | `/home/cloud/models/Qwen3.5-4B`（4.66B，9.32GB） |
| 工程 | `/home/cloud/distill-qwen35/` |
| 训练产物 | `out/sft-v1/`（一阶段）、`out/sft-v2-teacher/`（二阶段） |
| 网页指标 | `/home/cloud/Strata/serve/web/train-data.js` |
| 561 任务 log | `/home/cloud/workspaces/u-561/runs/jobs/<jid>.log` |

## 2. 数据

- 对齐池 12,000 条：COIG-CQIA 4k + qa_ext 4k + Belle 1M 2.5k + ShareGPT 1.5k。
- 小秋身份：70 条训练 + 41 条留出验收，覆盖「你是谁/你叫什么/你是不是千问」等问法；
  答案统一自称小秋，不冒充其它模型。
- teacher 蒸馏：从 12k 对齐池提 prompt，走 `8091 /v1/chat/completions/batch`
  （`enable_thinking=false`，max_tokens 160，temperature 0.6），断点续跑；
  实测约 0.4 条/s，持续向 12k 提示词跑。
- 二阶段（teacher-heavy）：teacher 全量 + 4,000 条对齐 replay + 身份×4，防止遗忘/过拟合。

## 3. 训练

- 文本塔：把 `model.language_model.*` 重映射为 `model.*`，直接构造
  `Qwen3_5ForCausalLM`，省掉 0.33B 视觉塔（约 0.66GB）。
- LoRA：r=16 / alpha=32 / dropout=0.1，target 覆盖 q/k/v/o、gate/up/down 与 GDN 的
  `in_proj_*`/`out_proj`，可训练 16.8M（0.89%）。
- 分块 CE：vocab=248,320，按 256 token 分块算 logits，避免 B×T×V 爆显存。
- 一阶段：12k 对齐 + 身份×4，1 epoch / 745 步，lr 1e-4，wd 0.01，bf16，batch 4×accum 4。

## 4. 踩过的坑（重要）

1. **label 未右移**：第一版 hidden[t] 对 labels[t]，等于让模型抄输入，loss 掉到 0.0002。
   已改为 hidden[:-1] 对 labels[1:]。
2. **val 暴露过拟合**：修复后 step 100 train≈0.5、**val 2.5575 / ppl 12.90**。
   已把 lr 从 2e-4 降到 1e-4、dropout 0.05→0.1、wd 0.01，并继续扩 teacher 数据。
3. **batch 端点会被一条慢请求拖住**：改用逐条并发也受引擎 4 批槽限制；
   最终用 batch 端点 + 300s 超时 + 断点续跑，实测 0.4 条/s。
4. **fla 在 import 时探测 CUDA**：不先 `torch.cuda.init()` 会判定“Triton 不支持”，
   GDN 回退 CPU（训练慢到不可用），已在 import transformers 前初始化 CUDA。
5. **train loss 显示口径偏小 4 倍**：`loss_acc += loss/grad_accum` 之后又除了一次
   `grad_accum`。页面上的 0.3 实际约 1.6；val 是独立计算，2.54 是真实值。
   已修正，三阶段起按正确口径记录。

## 5. 网页 / 用户可见性

- `TrainView.vue`（监控模式）每 5s 读 `/web/train-data.js`；训练脚本用
  `MetricsRecorder(web_path=...)` 原子重写该文件。
- 原始 log 写入 `/home/cloud/workspaces/u-561/runs/jobs/<jid>.log`，并在同目录写
  `<jid>.json`（uid=u-561），561 登录后「后台任务」面板可直接按 offset 拉取。
- 不新增服务端接口、不重启引擎/代理（沿用静态 .js 机制）。

## 6. 当前实测（2026-11-04 17:15 UTC）

- 一阶段 step 100 val 2.5613 / ppl 12.95；step 200 val 2.5449 / ppl 12.74，
  train 约 0.3——val 几乎不动，确认对齐池在“记样本”，不是泛化。
  已在一阶段 200 步停止，用 step-200 adapter 作二阶段起点。
- 小秋身份：第一版 41 条留出 34/41 = 82.9%，且有 2 条仍说成“通义/阿里”。
  把身份数据扩到 **117 条训练 / 51 条验收**，跑了一轮强化
  （identity×20 + 2k 对齐 replay，lr 5e-5，150 步）后：
  **51/51 = 100%**；出现的“通义/Qwen”全是否定句（“不是 Qwen/通义”）。
- teacher 数据：第一版 160 token 有 380/413 条被截断，已丢弃；
  现在只收 `finish_reason=stop`，max_tokens 384，重新生成中。
- 二阶段已于 17:41 UTC 自动启动：teacher 305 条 + 4,000 对齐 replay + 身份 117×4
  = 4,773 条 / 297 步，lr 5e-5；页面切到 `sft-v2-teacher`，job 写 u-561 面板。
- 二阶段完成（297 步 / 9.5 分钟）：val 2.5412（100 步）→ **2.5387**（200 步）。
  补测基座：**base val 2.9488 / base train 2.8359** ⇒ 训练把 val 降了约 0.41（14%），
  不是“完全不学”；train 实际约 1.6（页面显示被 4 倍缩小），仍有中等过拟合。
- 二阶段后小秋复测 **51/51 = 100%**；示例质量：身份、光合作用、回文函数、AI 观点都正常；
  “北京景点”出现重复列举（贪心解码退化），后续可用 repetition penalty / 更多数据缓解。

## 7. 连续 5 小时蒸馏结果（2026-11-05 02:10–07:16）

- `scripts/continuous_distill.sh 5` 共跑 **49 轮**，每轮重建 teacher-dominant 集
  （teacher + 1k replay + 身份×2），从上一轮 adapter 继续；每轮 149–176 步、lr 2e-5。
  teacher 累计 **2,327 条**（后期去掉 200 字限制，max_tokens 1024、只收自然结束）。
- **train 均值 1.49 → 0.79**，但 **val 2.552 → 2.616**（最好 r11 = 2.54828）——
  后半程在过拟合/漂移。**推荐用 `out/cont-r11/final`**；r49 也能用但 val 更差。
- 定性对比：r11 的小秋/光合作用/北京景点/短诗/量子纠缠都正常，北京景点无复读；
  r49 更详细，但北京景点有重复条目（“故宫”出现两次）。
- **loss 跳跃的原因**（用 r11 实测 per-sample loss）：
  <50 token 的样本 mean 1.08（std 0.88，最低 0.002），50–150 为 1.94，
  150–400 为 1.93，>400 为 2.80。训练每步只有 4×4=16 条、按 token 加权，
  批里混进一条短身份样本或一条长难样本，整步均值就会在 0.1–1.8 之间跳。
  这不是发散；是采样方差。要平滑就记 20–50 步滑动平均/加大 batch。
- 试效果：`CUDA_VISIBLE_DEVICES=2 .venv/bin/python scripts/chat.py --adapter out/cont-r11/final`
  （REPL；单条加 `--prompt "你是谁？"`；greedy 用 `--temperature 0`）。

## 8. 追加方向（2026-11-05 后续）

- **长上下文**：架构原生 262K，但训练内存不够。实测 seq=8192 / batch2 / accum8
  峰值 ~18.4GB（含其它租户 ~10.4GB，训练本体 ~8GB）；16K 约翻倍会 OOM，
  **128K 训练单卡 24GB 不可能**（仅激活就 24×128K×2048×2B ≈ 12.6GB，再加权重/注意力）。
  128K 只能推理或换多卡。
- **数据扩展**：Infinity-Instruct 中文 8k、Magpie 1k、Hermes 多工具 3k、
  ReAct 单轮 1.5k + 多轮 600、长对话 3k、故事/总结（teacher 生成中）、
  Agent 自博弈 120 条（teacher 扮 user/worker，本地工具+沙箱，收集完整轨迹）。
- **情感**：按用户要求不做分类标签，改成 teacher 的情感化故事（温暖/悲伤/紧张等）
  与长文总结。
- **RL**：按用户后续要求先做“基础对齐微调”，DPO 暂缓（脚本已备）。
- **当前流水线（2026-11-05 12:24 北京起）**：
  1. `mix-long-v1` SFT：29,199 条混合数据，seq 8192、batch2×accum8、lr 1e-5、early stop；
     val 2.5599 → 2.5499（step 100），step 200 未再降。
  2. SFT 结束后 `post_sft_chain.sh` 自动接 **tool-align-v1**（tools×3、ReAct×2、
     多轮 ReAct×2、agent 轨迹×3、身份×4、1.5k replay，seq 4096、lr 1e-5）。
  3. 再接 **identity-final**（132 条身份×8 + 2k replay，120 步、lr 3e-5）。
  4. 验收：`test_tools.py` 5 场景 + `eval_identity.py` 58 条（含“自称 Qwen”硬违规检测）。
- **Agent 自博弈**：`agent_selfplay.py`，teacher 扮 user/worker，7 工具 + 沙箱，
  收集完整轨迹（已 41/120），跑完自动接故事/总结生成。
- **128K**：只做推理，不训练；训练上限 8K（16K 会 OOM）。
- **结果（2026-11-05 12:40–12:56 北京）**：
  - `mix-long-v1`：215 步手动停（val 100 步 2.5499 见顶）。
  - `tool-align-v1`：200 步，val 2.5550 → 2.5538，early stop。
  - `identity-final`：120 步；身份留出 **57/58 = 98.3%**，
    **自称 Qwen/通义/阿里训练 = 0 次**；唯一 miss 是“请只报名字”被理解成反问
    （回答“请提供需要报名字的词语”），不涉及 Qwen。
  - 工具验收：5/5 单工具场景成功（天气/计算/汇率/时间/航班）；
    复合任务一次发出 `get_weather×2 + search_flights`，拿到结果后给出总预算总结。
  - Agent 自博弈收集 **86 条**完整轨迹（user→tool calls→tool_response→final→追问）。

- 一阶段正在重训（低 lr + dropout），等 step 100/200 val 再判断；
  val 若仍不降，提前停并调整数据配比（增加 teacher、减少模板化短答案）。
- 二阶段在 teacher≥1000 且一阶段 adapter 落盘后自动启动。
- 训练完用 `scripts/eval_identity.py` 验收小秋命中率（41 条留出问题）。
## 9. 多模态 / thinking / 服务（2026-11-05 下午）

- 原生 Qwen3.5-2B：thinking（enable_thinking=true 有思考过程，数学答对）与视觉（红图→红色）
  都支持。
- 之前的 `merged/xiaoqiu` 是**纯文本 CausalLM**（剥了视觉塔），不支持图片；
  SFT 全程 `enable_thinking=false`，thinking 退化。
- 已重建 `merged/xiaoqiu-vl`：adapter 合进完整 VL 底座（视觉塔保留），实测
  身份“我是小秋”+ 红图“红色”；thinking 能输出思考文本但质量差（9.11/9.8 仍答错）。
- 正在补 teacher 思考轨迹（qwen3.8-flash 开 thinking、系统提示中文推理），
  做约 2 小时 thinking SFT；完成后重新合并 VL 并复测。
- 服务：rag-proxy 已加 `model_routes.xiaoqiu → 127.0.0.1:8093`，公网 8080 可用；
  API token 在 `~/logs/strata-api-key.txt`。
- 128K 配置并发：4 路 81 tok/s、8 路 155、16 路 200（纯文本）；VL 版 128K/8 路 129 tok/s。

## 10. Thinking 蒸馏最终结果（2026-11-05 17:56 北京）

- 数据：teacher qwen3.8-flash 开 thinking、系统提示“必须用中文思考”，共 426 条
  （过滤英文思考后 299 条进训练）；另生成同质留出：general 47 条、thinking 28 条。
- 训练三轮约 2 小时：think-v1 900 步（28.9min，best val 2.5424）→ think-v2 1077 步
  （35.1min，2.5403）→ think-v3 1187 步（38.9min，2.5408）。
- 同质 teacher 留出 loss（base → final）：thinking **1.6336 → 1.3718**；
  general **0.9898 → 1.0650**（思考能力学到，通用略有漂移）。
- 最终 `out/think-v3/final` 合并为 `merged/xiaoqiu-vl3`（完整 VL 底座），
  vLLM 128K / max-num-seqs 16 服务，model id `xiaoqiu`，公网网关已通。
- 验收：身份 **58/58=100%**、自称 Qwen/通义/阿里 **0 次**；工具 **5/5**；
  视觉红/蓝图识别正确；thinking 中文推理，9.11/9.8、苹果题正确，复杂逻辑仍弱。
- 并发（128K 配置）：4 路 61 tok/s、8 路 129、16 路 182（p50≈0.9s）。
