// 参数量 / 计算量 / 张量形状的估算与格式化。
// 估算公式都写在这里并带注释 —— 界面上的数字要能被解释，不能是拍脑袋的魔数。
import { fmt } from "../api.js";

/** 12345678 → "12.3M"。与监控页的 gb()/kfmt() 一个路数，只是单位按量级跳。 */
export function params(n) {
  if (n == null || Number.isNaN(n)) return "–";
  if (n >= 1e12) return `${fmt(n / 1e12, 2)}T`;
  if (n >= 1e9) return `${fmt(n / 1e9, 2)}B`;
  if (n >= 1e6) return `${fmt(n / 1e6, 1)}M`;
  if (n >= 1e3) return `${fmt(n / 1e3, 1)}K`;
  return fmt(n);
}

/** FLOPs（浮点运算次数），不是 FLOP/s。 */
export function flops(n) {
  if (n == null || Number.isNaN(n)) return "–";
  if (n >= 1e15) return `${fmt(n / 1e15, 2)} PFLOPs`;
  if (n >= 1e12) return `${fmt(n / 1e12, 2)} TFLOPs`;
  if (n >= 1e9) return `${fmt(n / 1e9, 1)} GFLOPs`;
  if (n >= 1e6) return `${fmt(n / 1e6, 1)} MFLOPs`;
  if (n >= 1e3) return `${fmt(n / 1e3, 1)} KFLOPs`;
  return fmt(n);
}

/**
 * 矩阵乘 M×K @ K×N 的浮点运算次数：每个输出元素 K 次乘加 = 2K 次浮点运算，
 * 共 M×N 个输出元素 → 2MKN。这是下面所有估算的地基。
 */
export const matmul = (M, K, N) => 2 * M * K * N;

/**
 * 模型的总参数量（权重共享的 lm_head 只算一次）。
 * - 词嵌入: V×d（lm_head 与它解绑时再加一份 V×d）
 * - 每层:   注意力 4×d×d（q/k/v/o 投影）+ FFN 2×d×d_ff + 2 个 LayerNorm 各 2×d
 * @param {import("./types.js").ModelInfo} m
 * @param {{ tied?: boolean }} [opts] tied=true（默认）表示 lm_head 与词嵌入共享权重
 */
export function totalParams(m, { tied = true } = {}) {
  const d = m.hiddenSize;
  const dff = m.intermediateSize || 4 * d;
  const perLayer = 4 * d * d + 2 * d * dff + 4 * d;   // ln_1、ln_2：各 weight+bias = 2d
  return m.vocabSize * d * (tied ? 1 : 2) + m.layers * perLayer;
}

/**
 * 每层的参数拆解。
 *
 * 真实模型带 paramBreakdown（tools/make-model-info.py 从 GGUF 张量表按类别真加出来的），
 * 那就**直接用真数**除以层数；没有才按公式估。这条分支很重要 ——
 * 本机那个是 MoE，用「2·d·dff」去估会差几十倍。
 */
export function layerParams(m) {
  const d = m.hiddenSize;
  const b = m.paramBreakdown;
  if (b && m.layers) {
    const per = (k) => Math.round(((b[k] && b[k].params) || 0) / m.layers);
    const attention = per("attention") + per("indexer");
    const ffn = per("moe_experts") + per("shared_expert") + per("router");
    const layernorm = per("hyper") + per("norms");
    return { attention, ffn, layernorm, residual: 0, total: attention + ffn + layernorm };
  }
  const dff = m.intermediateSize || 4 * d;
  return {
    attention: 4 * d * d,
    ffn: 2 * d * dff,
    layernorm: 4 * d,                                 // 2 个 LN ×(weight+bias)
    residual: 0,                                      // 逐元素加法，无参数
    total: 4 * d * d + 2 * d * dff + 4 * d,
  };
}

// ------------------------------------------------------------------ 形状

/** [7, 768] → "[7, 768]"，也给字符串用（token 列表那种非张量的步骤）。 */
export const shape = (...dims) => `[${dims.join(", ")}]`;

/** 词表 id 列表 → "[7] · id[101, 7592, 1010, …]" */
export function idList(ids, max = 6) {
  const head = ids.slice(0, max).join(", ");
  return ids.length > max ? `${head}, …` : head;
}

// ------------------------------------------------------------------ 分步 FLOPs
//
// 全部按「这批 token 一次前向」算。n = token 数，d = hiddenSize，dff = 中间维度，h = 头数。
// 注意力分数/上下文那两项都带 n²：短句时几乎为零，长句时才显出来 —— 演示里能直观看到。

/** @returns {Record<string, number>} 每个逻辑阶段一次前向的 FLOPs */
export function flopBreakdown(m, n) {
  const d = m.hiddenSize;
  const headDim = m.keyLength || d / m.heads;
  const qDim = m.heads * headDim;
  const kvDim = (m.kvHeads || m.heads) * headDim * 2;          // K 和 V 各一份；GQA 时 kvHeads < heads
  const proj = qDim + kvDim + qDim;                            // Q、K、V、输出投影（按 Q 的宽度算输出投影）
  // MoE：每个 token 只算命中的那几个专家 + 一份共享专家；路由打分是一次 d × 专家数 的小矩阵乘。
  const moe = m.expertCount && m.expertUsed;
  const activeDff = moe
    ? m.expertUsed * (m.expertFfn || d) + (m.sharedExpertFfn || 0)
    : (m.intermediateSize || 4 * d);
  return {
    tokenize: 0,
    embed: 0,                                        // 查表，不计浮点
    layernorm: 3 * n * d,                            // 均值 / 方差 / 归一化，每元素约 3 次
    attention: matmul(n, d, proj)                    // Q/K/V/O 投影
      + 2 * matmul(n, n, qDim),                      // 分数 QKᵀ 与 上下文 αV —— 这两项带 n²
    ffn: matmul(n, d, activeDff) + matmul(n, activeDff, d)
      + (moe ? matmul(n, d, m.expertCount) : 0),     // 路由打分
    residual: n * d,                                 // 逐元素加法
    output: matmul(n, d, m.vocabSize) + 3 * n * m.vocabSize,   // lm_head + softmax 的 exp/加/除
  };
}
