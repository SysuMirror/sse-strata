// 示例模型：给「没接真实数据时」兜底，也用来演示不同的规模。
//
// 注意分工：**图与步骤的生成在 graph.js**（真实模型和示例模型走同一条路），
// 这里只有模型列表本身和默认演示句子。
//
// 本机真跑的那个模型不在这个文件里 —— 它由 tools/make-model-info.py 从 GGUF 里读出来，
// 落在 src/viz/model.local.js（参数量、层型、张量形状都是文件里的真值）。
import { totalParams } from "./num.js";

/** @returns {import("./types.js").ModelInfo[]} */
export function mockModels() {
  return [
    {
      id: "demo-12l-768h",
      name: "示例模型 · 12L / 768H",
      source: "mock",
      layers: 12, hiddenSize: 768, heads: 12, vocabSize: 50000, intermediateSize: 3072,
      paramCount: totalParams({ layers: 12, hiddenSize: 768, vocabSize: 50000, intermediateSize: 3072 }),
      note: "12 层稠密 Transformer，层数少、一次能看完全程",
    },
    {
      id: "tiny-6l-512h",
      name: "示例模型 · 6L / 512H",
      source: "mock",
      layers: 6, hiddenSize: 512, heads: 8, vocabSize: 50000, intermediateSize: 2048,
      paramCount: totalParams({ layers: 6, hiddenSize: 512, vocabSize: 50000, intermediateSize: 2048 }),
      note: "更小，适合先熟悉这套图",
    },
  ];
}

/** 默认演示句子：中英混排，能看出分词粒度的差别。 */
export const DEMO_TEXT = "LLM 推理过程可视化";
