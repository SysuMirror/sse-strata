// 节点类型 → 图例里的颜色与文案。
//
// 颜色**一律走设计令牌**，不写死色值：这样浅色/深色主题自动跟着走，
// 也和线稿视图里那些标记用的是同一份颜色（见 IsoView.vue 里各算子的填充）。
export const NODE_STYLE = {
  input: { color: "var(--st-ink-muted)", labelKey: "viz.type.input" },
  tokenizer: { color: "var(--st-ink-muted)", labelKey: "viz.type.tokenizer" },
  embedding: { color: "var(--st-info)", labelKey: "viz.type.embedding" },
  layernorm: { color: "var(--st-accent)", labelKey: "viz.type.layernorm" },
  // 全注意力与线性注意力**必须不同色**：这个模型 48 层里 36 层是线性、12 层是全，
  // 拿同一个颜色画它们等于说「这两种是一回事」，是错的（用户实测指出过）。
  attention: { color: "var(--st-info)", labelKey: "viz.type.attention" },
  linear: { color: "var(--st-chart-2)", labelKey: "viz.type.linear" },
  ffn: { color: "var(--st-warn)", labelKey: "viz.type.ffn" },
  residual: { color: "var(--st-accent-text)", labelKey: "viz.type.residual" },
  output: { color: "var(--st-info)", labelKey: "viz.type.output" },
};
