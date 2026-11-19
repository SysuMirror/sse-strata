// 由一份模型配置生成「图」与「推理步骤」。真实模型和示例模型走的是同一条路 ——
// 差别只在配置里有没有 layerKinds / layerTensors 这两样（由 tools/make-model-info.py 从
// GGUF 里读出来）。有，就按真实层型与真实张量名来；没有，就按通用稠密 Transformer 编个名。
//
// 节点坐标在这里算好写进 position —— 后端将来返回图时也照这个格式带上坐标，
// 渲染端（three/）完全不需要知道布局规则。
import { flopBreakdown, layerParams, matmul, shape } from "./num.js";

// ------------------------------------------------------------------ 布局
//
// 层数少：主干沿 Y 轴向上，每层的 4 个子节点绕主干一圈（方位角固定）：
//     0° 归一化  90° 注意力  180° FFN  270° 残差
// 方位角固定，是为了让「同一种算子」在塔上排成一整列。
//
// 层数多（本机那个是 48 层）：直塔会变成 30:1 的细高条，节点小到看不清 ——
// 改成**螺旋**：每 4 层绕一圈，整座塔盘成一根弹簧，宽高比回到 4:1 左右。
export const LAYOUT = {
  ring: 3.4,
  gap: 4.6,
  minGap: 2.2,
  inputY: -40,
  tokenizerY: -33.5,
  embedY: -27,
  block0Y: -21,
  gapAbove: 9,
  helixFrom: 17,        // 从多少层开始改螺旋
  helixRadius: 8.5,
  perTurn: 4,           // 螺旋每圈几层
};

const AZIMUTH = { layernorm: 0, attention: 90, ffn: 180, residual: 270 };
const rad = (deg) => (deg * Math.PI) / 180;

/** 某一层的垂直间距：12 层用满 gap，层数更多就压紧，别让塔长到镜头装不下。 */
export const blockGap = (layers) =>
  Math.min(LAYOUT.gap, Math.max(LAYOUT.minGap, (LAYOUT.gap * 12) / layers));

/** 第 i 层的落位：直塔就是 (0, y, 0)，螺旋则再绕一圈偏移 */
export function layerFrame(i, layers) {
  const y = LAYOUT.block0Y + i * blockGap(layers);
  if (layers < LAYOUT.helixFrom) return { y, cx: 0, cz: 0, dense: false };
  const th = (i * 2 * Math.PI) / LAYOUT.perTurn;
  return { y, cx: LAYOUT.helixRadius * Math.cos(th), cz: LAYOUT.helixRadius * Math.sin(th), dense: true };
}

const at = (f, deg, ring = LAYOUT.ring) => [
  +(f.cx + ring * Math.cos(rad(deg))).toFixed(3),
  +f.y.toFixed(3),
  +(f.cz + ring * Math.sin(rad(deg))).toFixed(3),
];

const prod = (dims) => dims.reduce((a, b) => a * b, 1);

// ------------------------------------------------------------------ 权重表
//
// 真实模型：名字和形状都来自 layerTensors（tools/make-model-info.py 从 GGUF 张量表里抽出
// 每种层型最全的那一层当样板，层号写成 {i}）。所以面板里显示的是**文件里的真名真形状**。
// 示例模型：没有这份数据，退回通用稠密 Transformer 的命名。

// 键是**节点角色**（graph 里用的名字），值是生成器里那几个张量分组（LAYER_ROLES）。
// 归一化节点把 hyper-connections 一起收进来 —— 本机那个模型的归一化与超连接是同一套 (hc_*_norm)。
const REAL_ROLES = {
  layernorm: ["hyper", "ln"],
  attention: ["attn"],
  ffn: ["ffn"],
  residual: [],
};

function weightsFor(m, i, kind, role) {
  const table = m.layerTensors && m.layerTensors[kind];
  if (table) {
    const out = [];
    for (const r of REAL_ROLES[role] || []) {
      for (const [name, dims] of table[r] || []) {
        out.push({ name: name.replace("{i}", String(i)), shape: shape(...dims), paramCount: prod(dims) });
      }
    }
    return out;
  }
  // 示例模型：通用命名
  const d = m.hiddenSize;
  const dff = m.intermediateSize || 4 * d;
  if (role === "layernorm") {
    return [{ name: `l${i}.ln_1.weight`, shape: shape(d), paramCount: d },
            { name: `l${i}.ln_2.weight`, shape: shape(d), paramCount: d }];
  }
  if (role === "attention") {
    return [["q_proj", "k_proj", "v_proj", "o_proj"].map((p) =>
      ({ name: `l${i}.${p}.weight`, shape: shape(d, d), paramCount: d * d }))].flat();
  }
  if (role === "ffn") {
    return [{ name: `l${i}.up_proj.weight`, shape: shape(dff, d), paramCount: dff * d },
            { name: `l${i}.down_proj.weight`, shape: shape(d, dff), paramCount: dff * d }];
  }
  return [];
}

/** 这一层是哪种注意力：真实配置里读得到就用真的，否则全按标准自注意力。 */
export const kindOf = (m, i) => (m.layerKinds && m.layerKinds[i]) || "dense";

const kindLabel = (m, i) => {
  const k = kindOf(m, i);
  if (k === "full") return { type: "attention", label: "自注意力" };
  if (k === "ssm") return { type: "linear", label: "线性注意力" };
  return { type: "attention", label: "自注意力" };
};

const isMoe = (m) => !!(m.expertCount && m.expertUsed);

// ------------------------------------------------------------------ 图

/**
 * @param {import("./types.js").ModelInfo} m
 * @returns {{ nodes: import("./types.js").GraphNode[], edges: import("./types.js").GraphEdge[] }}
 */
export function buildGraph(m) {
  const d = m.hiddenSize;
  const V = m.vocabSize;
  const n = m.layers;
  const dense = n >= LAYOUT.helixFrom;
  const k = dense ? 0.66 : 1;              // 层多了把节点和环一起缩小
  const lp = layerParams(m);
  const f0 = layerFrame(0, n);
  const fl = layerFrame(n - 1, n);
  const topY = fl.y + blockGap(n) + LAYOUT.gapAbove * 0.55;

  /** @type {import("./types.js").GraphNode[]} */
  const nodes = [];
  /** @type {import("./types.js").GraphEdge[]} */
  const edges = [];
  const push = (x) => { nodes.push(x); return x.id; };
  const link = (from, to, extra = {}) => edges.push({ id: `${from}->${to}`, from, to, kind: "data", ...extra });

  // ---- 头部：文本 → 分词 → 嵌入
  push({ id: "input", label: "文本输入", type: "input", position: [0, LAYOUT.inputY, 0], size: 1.15,
         outputShape: "string", paramCount: 0 });
  push({ id: "tokenize", label: "Tokenizer", type: "tokenizer", position: [0, LAYOUT.tokenizerY, 0], size: 1.05,
         inputShape: "string", outputShape: shape("n"), paramCount: 0, weightNames: [] });
  const embedParams = V * d;
  const embedWeights = [{ name: m.embedWeightName || "embed_tokens.weight", shape: shape(V, d), paramCount: embedParams }];
  push({ id: "embed", label: "词嵌入", type: "embedding", position: [0, LAYOUT.embedY, 0], size: 2.1 * k,
         inputShape: shape("n"), outputShape: shape("n", d), paramCount: embedParams,
         weightNames: embedWeights.map((w) => w.name), weights: embedWeights });
  link("input", "tokenize");
  link("tokenize", "embed", { weightName: embedWeights[0].name });

  // ---- 主干
  for (let i = 0; i < n; i++) {
    const frame = layerFrame(i, n);
    const kind = kindOf(m, i);
    const att = kindLabel(m, i);
    const id = (x) => `l${i}.${x}`;
    const moe = isMoe(m);

    const lnW = weightsFor(m, i, kind, "layernorm");
    const atW = weightsFor(m, i, kind, "attention");
    const ffW = weightsFor(m, i, kind, "ffn");

    push({
      id: id("layernorm"), label: `L${i + 1} · 归一化`, type: "layernorm", layer: i,
      position: at(frame, AZIMUTH.layernorm), size: 1.0 * k,
      inputShape: shape("n", d), outputShape: shape("n", d), paramCount: lp.layernorm,
      weightNames: lnW.map((w) => w.name), weights: lnW,
    });
    push({
      id: id("attention"), label: `L${i + 1} · ${att.label}`, type: att.type, layer: i,
      position: at(frame, AZIMUTH.attention), size: 1.35 * k,
      inputShape: shape("n", d), outputShape: shape("n", d), paramCount: lp.attention,
      weightNames: atW.map((w) => w.name), weights: atW,
    });
    push({
      id: id("ffn"), label: moe ? `L${i + 1} · MoE FFN` : `L${i + 1} · FFN`, type: "ffn", layer: i,
      position: at(frame, AZIMUTH.ffn), size: 1.5 * k,
      inputShape: shape("n", d), outputShape: shape("n", d), paramCount: lp.ffn,
      weightNames: ffW.map((w) => w.name), weights: ffW,
    });
    push({
      id: id("residual"), label: `L${i + 1} · 残差`, type: "residual", layer: i,
      position: at(frame, AZIMUTH.residual), size: 0.95 * k,
      inputShape: shape("n", d), outputShape: shape("n", d), paramCount: 0, weightNames: [], weights: [],
    });

    const src = i === 0 ? "embed" : `l${i - 1}.residual`;
    link(src, id("layernorm"));
    link(id("layernorm"), id("attention"), { weightName: atW.length ? atW[0].name : undefined });
    link(id("attention"), id("ffn"), { weightName: ffW.length ? ffW[0].name : undefined });
    link(id("ffn"), id("residual"));
    // 跳过中间三个算子的那条直连（残差）
    edges.push({ id: `${src}~~>${id("residual")}`, from: src, to: id("residual"), kind: "residual" });
  }

  // ---- 尾部：词表投影 → softmax
  const last = `l${n - 1}.residual`;
  const headW = [{ name: m.headWeightName || "lm_head.weight", shape: shape(V, d), paramCount: embedParams }];
  push({ id: "lm_head", label: "输出投影", type: "output", position: [0, topY, 0], size: 2.0 * k,
         inputShape: shape("n", d), outputShape: shape("n", V), paramCount: embedParams,
         weightNames: headW.map((w) => w.name), weights: headW });
  push({ id: "output", label: "Softmax · 下一个词", type: "output", position: [0, topY + 7 * Math.max(k, 0.8), 0],
         size: 1.25 * k, inputShape: shape("n", V), outputShape: shape("1"), paramCount: 0, weightNames: [], weights: [] });
  link(last, "lm_head", { weightName: headW[0].name, kind: "output" });
  link("lm_head", "output", { kind: "output" });

  return { nodes, edges };
}

// ------------------------------------------------------------------ 推理步骤

/**
 * 把一次前向拆成可逐帧播放的步骤。
 * 每层 4 步（归一化 / 注意力 / FFN / 残差），加上头尾 4 步与收尾 2 步。
 * 本机那个模型里，36 层的注意力是线性的、12 层是全注意力，步骤名会如实区分。
 *
 * @param {import("./types.js").ModelInfo} m
 * @param {string} text
 * @param {{ tokens?: {text:string,id:number}[] }} [opts]
 * @returns {import("./types.js").InferenceStep[]}
 */
export function buildSteps(m, text, opts = {}) {
  const d = m.hiddenSize;
  const dff = m.intermediateSize || 4 * d;
  const V = m.vocabSize;
  const toks = (opts.tokens && opts.tokens.length) ? opts.tokens : mockTokens(text, V);
  const nTok = toks.length;
  const f = flopBreakdown(m, nTok);
  const lp = layerParams(m);
  const { edges } = buildGraph(m);
  const moe = isMoe(m);

  const touching = (ids) => {
    const set = new Set(ids);
    return edges.filter((e) => set.has(e.from) || set.has(e.to)).map((e) => e.id);
  };

  /** @type {import("./types.js").InferenceStep[]} */
  const steps = [];
  const add = (s) => steps.push({ id: steps.length, activeEdgeIds: touching(s.activeNodeIds),
                                  paramCount: 0, flops: 0, ...s });

  // 每一步都带一份 vis：这一步在算什么、进出的张量是什么形状、几路并行。
  // 演示时中间画布按它画「步骤图」——用户看不懂形状串，得看得见方块与乘法。
  add({
    name: "文本输入", kind: "input", activeNodeIds: ["input"],
    description: `收到一句话：「${text}」。此时还是一串字符，模型还看不见它。`,
    activeWeightNames: [], tensorShape: "string", flops: 0, paramCount: 0,
    vis: { op: "chars", title: "原始字符", chips: [...String(text).slice(0, 28)],
           note: "模型不认识字，只认识数字 —— 下一步先把它切开。" },
  });
  add({
    name: "Tokenizer 分词", kind: "tokenize", activeNodeIds: ["input", "tokenize"],
    description: `切成 ${nTok} 个 token，句首句尾补上特殊标记。真实分词由引擎做，这里按中英混排的粒度做展示切分。`,
    activeWeightNames: [], tensorShape: shape(nTok), paramCount: 0, flops: 0,
    detail: toks.map((t) => t.text),
    vis: { op: "pairs", title: `切成 ${nTok} 段`, pairs: toks.map((t) => ({ top: t.text })),
           note: "按「汉字单字 / 拉丁词 / 标点」切开，句首句尾补上 <s> </s>。下一步给每一段编号。" },
  });
  add({
    name: "Token ID 映射", kind: "tokenize", activeNodeIds: ["tokenize"],
    description: `每个 token 查表换成词表里的整数 id，共 ${V.toLocaleString()} 个候选。`,
    activeWeightNames: [], tensorShape: shape(nTok), paramCount: 0, flops: 0,
    detail: toks.map((t) => String(t.id)),
    // **上下成对**：上面是这个 token 本身，下面是它在词表里的编号 —— 少了对照就没人看得懂
    vis: { op: "pairs", focus: "id", title: `${nTok} 个 token 各自的编号`,
           pairs: toks.map((t) => ({ top: t.text, bottom: `#${t.id}` })),
           note: `每个 token 在 ${V.toLocaleString()} 词的词表里都有固定编号；模型只认这些编号。`
             + `编号是拿 token 的字面算出来的（同一个词每次都是同一个号）。` },
  });
  add({
    name: "Embedding 查表", kind: "embed", activeNodeIds: ["tokenize", "embed"],
    description: `把 ${nTok} 个 id 换成 ${d} 维稠密向量。这一步只是查表，没有矩阵乘法，但表本身有 ${(V * d / 1e6).toFixed(1)}M 个权重。`,
    activeWeightNames: [m.embedWeightName || "embed_tokens.weight"], tensorShape: shape(nTok, d),
    paramCount: V * d, flops: 0,
    // 进来的是 n 个编号（n×1），出去的是 n 行 d 维向量；表有 V 行，这里只取其中 n 行
    vis: { op: "lookup", title: `查表：${nTok} 个编号 → ${nTok} 行向量`,
           in: [String(nTok), "1"], table: [String(V), String(d)], out: [String(nTok), String(d)],
           note: `每个编号对应表中一行 ${d} 维向量。取这 ${nTok} 行出来 —— 没有乘法，只是搬运；`
             + `表本身有 ${V.toLocaleString()} × ${d} ≈ ${(V * d / 1e6).toFixed(0)}M 个权重。` },
  });

  // 演示策略：**每种性质的层只演两层**，其余自动跳过（48 层逐层放完是 198 步，没人看得下去）。
  // 跳过的那些仍留在步骤表里 —— 拖时间轴或点层块照样能单独看。
  const firstByKind = new Map();
  for (let i = 0; i < m.layers; i++) {
    const k = kindOf(m, i);
    const arr = firstByKind.get(k) || [];
    if (arr.length < 2) { arr.push(i); firstByKind.set(k, arr); }
  }
  const demoLayers = new Set([...firstByKind.values()].flat());
  const reprOf = (k) => (firstByKind.get(k) || [0])[0] + 1;
  const kindNameOf = (i) => (kindOf(m, i) === "ssm" ? "线性注意力" : kindOf(m, i) === "full" ? "自注意力" : "普通 FFN 层");

  const head = (m.kvHeads && m.kvHeads < m.heads) ? `${m.heads} 个 Query 头共用 ${m.kvHeads} 组 K/V（GQA）` : `${m.heads} 个头并行`;
  for (let i = 0; i < m.layers; i++) {
    const kind = kindOf(m, i);
    const ln = `l${i}.layernorm`, att = `l${i}.attention`, ffn = `l${i}.ffn`, res = `l${i}.residual`;
    // 步骤里的权重名与节点详情同源（都走 weightsFor）—— 两处对不上就等于自相矛盾
    const names = (role, max = 6) => weightsFor(m, i, kind, role).map((w) => w.name).slice(0, max);
    const lnNames = names("layernorm", 4), atNames = names("attention", 8), ffNames = names("ffn", 8);
    // 非演示层：播放时会整段跳过，界面上说明跟前面哪一层是同一种
    const same = demoLayers.has(i) ? null : { layer: reprOf(kind), kind: kindNameOf(i) };
    add({
      name: `第 ${i + 1} 层 · 归一化`, kind: "layernorm", layer: i, activeNodeIds: [ln], same,
      description: "按最后一维归一化到均值 0、方差 1，再乘可学习的缩放和平移。让后面的计算不被数值尺度带偏。",
      activeWeightNames: lnNames, tensorShape: shape(nTok, d),
      paramCount: lp.layernorm, flops: f.layernorm,
      vis: { op: "norm", title: "逐 token 归一化",
             in: [String(nTok), String(d)], out: [String(nTok), String(d)],
             note: `每个 token 的 ${d} 维各自算均值/方差，归一化后再乘一组可学习的缩放与平移。` },
    });
    if (kind === "ssm") {
      add({
        name: `第 ${i + 1} 层 · 线性注意力`, kind: "attention", layer: i, activeNodeIds: [att], same,
        description: `这一层是线性注意力（状态空间）：把注意力写成可递推的形式，每步只维护一个固定大小的状态，`
          + `计算量随长度线性增长而不是平方 —— 48 层里 36 层用这种，正是长上下文的成本关键。`,
        activeWeightNames: atNames,
        tensorShape: `${shape(nTok, d)} · 状态 ${shape(m.ssmStateSize || 128)}`,
        paramCount: lp.attention, flops: f.attention,
        detail: [`QKV 与门投影 ${shape(nTok, d)}`, "递推状态更新（不物化 n×n 分数矩阵）"],
        vis: { op: "matmul", title: "线性注意力 · 投影",
               in: [String(nTok), String(d)], w: [String(d), String(m.ssmStateSize || 128)],
               out: [String(nTok), String(m.ssmStateSize || 128)],
               note: `不物化 ${nTok}×${nTok} 的分数矩阵，只把状态推进一步 —— 代价随长度线性增长。` },
      });
    } else {
      add({
        name: `第 ${i + 1} 层 · 自注意力`, kind: "attention", layer: i, activeNodeIds: [att], same,
        description: `${head}：每个 token 发一个 Query，与此前所有 token 的 Key 做点积得到 ${nTok}×${nTok} 的注意力分数，`
          + `softmax 后加权汇总 Value。每 ${m.fullAttentionInterval || 4} 层才有一次，其余层走线性的那套。`,
        activeWeightNames: atNames,
        tensorShape: `${shape(nTok, d)} · 分数 ${shape(nTok, nTok)}`,
        paramCount: lp.attention, flops: f.attention,
        detail: [`Q/K/V 投影`, `多头切分 ${m.heads} 头${m.kvHeads && m.kvHeads < m.heads ? `（KV ${m.kvHeads} 头）` : ""}`, `缩放点积 ${shape(nTok, nTok)}`],
        vis: { op: "matmul", title: "注意力分数 Q·Kᵀ",
               in: [String(nTok), String(d)], w: [String(d), String(nTok)], out: [String(nTok), String(nTok)],
               branches: m.heads, branchTotal: m.heads,     // 多头并行：同色摆出来
               note: `每个 token 都要和前面每个 token 打分：${nTok} 行 × ${nTok} 列，开销随长度平方增长。` },
      });
    }
    if (moe) {
      add({
        name: `第 ${i + 1} 层 · MoE FFN`, kind: "ffn", layer: i, activeNodeIds: [ffn], same,
        description: `这一层是混合专家：路由器按 token 从 ${m.expertCount} 个专家里挑出 ${m.expertUsed} 个来算，`
          + `再叠加一份每个 token 都走的共享专家。全模型 ${(m.paramCount / 1e9).toFixed(0)}B 参数里绝大部分在这里，`
          + `但每个 token 只激活其中很小一部分。`,
        activeWeightNames: ffNames,
        tensorShape: `${shape(nTok, d)} → ${m.expertUsed} × ${shape(nTok, m.expertFfn || dff)} → ${shape(nTok, d)}`,
        paramCount: lp.ffn, flops: f.ffn,
        detail: [`路由：${m.expertCount} 选 ${m.expertUsed}`, `专家中间维 ${m.expertFfn || dff}`, `共享专家中间维 ${m.sharedExpertFfn || dff}`],
        vis: { op: "moe", title: `路由 ${m.expertCount} 选 ${m.expertUsed}`,
               in: [String(nTok), String(d)], w: [String(d), String(m.expertFfn || dff)], out: [String(nTok), String(d)],
               branches: m.expertUsed, branchTotal: m.expertCount,
               note: `每个 token 只挑 ${m.expertUsed} 个专家来算（${m.expertUsed} 路并行），其余专家这一步不动 —— 这就是 MoE 省算力的地方。` },
      });
    } else {
      add({
        name: `第 ${i + 1} 层 · FFN`, kind: "ffn", layer: i, activeNodeIds: [ffn], same,
        description: `两层 MLP：${d} → ${dff} → ${d}，中间过激活函数。逐 token 独立计算，承担大部分参数和算力。`,
        activeWeightNames: ffNames,
        tensorShape: `${shape(nTok, d)} → ${shape(nTok, dff)} → ${shape(nTok, d)}`,
        paramCount: lp.ffn, flops: f.ffn,
        vis: { op: "matmul", title: "升维 → 激活 → 降维",
               in: [String(nTok), String(d)], w: [String(d), String(dff)], out: [String(nTok), String(dff)],
               note: `先把 ${d} 维升到 ${dff} 维、过激活函数，再降回 ${d} 维；每个 token 各算各的。` },
      });
    }
    add({
      name: `第 ${i + 1} 层 · 残差连接`, kind: "residual", layer: i, activeNodeIds: [res], same,
      description: "把这一层的输出直接加回输入，梯度可以沿这条线一路回到嵌入层 —— 深模型能训起来的关键。",
      activeWeightNames: [], tensorShape: shape(nTok, d), paramCount: 0, flops: f.residual,
      vis: { op: "add", title: "残差：加回主干",
             in: [String(nTok), String(d)], in2: [String(nTok), String(d)], out: [String(nTok), String(d)],
             note: `这一层的输出和进来的那一份逐元素相加 —— 形状完全一样，所以能直接加。` },
    });
  }

  add({
    name: "输出投影", kind: "output", activeNodeIds: ["lm_head"],
    description: `把最后一层每个位置的 ${d} 维向量投影到 ${V.toLocaleString()} 维词表空间，得到 logits。`,
    activeWeightNames: [m.headWeightName || "lm_head.weight"], tensorShape: shape(nTok, V),
    paramCount: V * d, flops: matmul(nTok, d, V),
    vis: { op: "matmul", title: "投影到词表",
           in: ["1", String(d)], w: [String(d), V.toLocaleString()], out: ["1", V.toLocaleString()],
           note: `只取最后一个位置的向量（生成下一个词只需要它），乘 ${d} × ${V.toLocaleString()} 的输出矩阵，`
             + `得到词表里每一个词的原始打分（logits）。` },
  });
  add({
    name: "Softmax · 下一个词", kind: "output", activeNodeIds: ["lm_head", "output"],
    description: "只取最后一个位置的 logits 做 softmax 得到概率分布，再按采样策略挑出下一个 token。",
    activeWeightNames: [], tensorShape: shape(1), paramCount: 0, flops: f.output,
    detail: ["采样 → 新 token 回到上下文，循环下一次前向"],
    vis: { op: "softmax", title: `softmax → ${V.toLocaleString()} 个概率`,
           in: ["1", V.toLocaleString()], out: ["1", V.toLocaleString()],
           bars: nextTokenBars(text),
           note: `logits 过 softmax 变成加起来为 1 的概率分布，再按温度 / top_p 采样出一个 token，`
             + `它被接回上下文末尾，整个流程从头再走一遍。` },
  });

  // ---- 收尾三步：采样 → 重复 → 回答。
  // 演示只**完整走第一个 token**（后面的图案变化就是「再来一遍」），
  // 最后把模型的真实回答放出来 —— 这条链子才算落到人能看懂的地方。
  const usedBars = nextTokenBars(text);
  add({
    name: "采样 · 第 1 个词", kind: "output", activeNodeIds: ["output"],
    description: "从概率分布里按温度 / top_p 采出一个 token —— 这就是模型吐出的第一个字。"
      + "它不是「最大的那个」，而是按概率随机抽的。",
    activeWeightNames: [], tensorShape: shape(1), paramCount: 0, flops: 0,
    vis: { op: "sample", title: "采样出第 1 个 token", bars: usedBars,
           note: "分布是示意；真正选中的那一个来自**本机实测**的一次真实推理（见下方）。"
             + "采样带随机性，所以同样一句话两次结果可能不同。" },
  });
  add({
    name: "重复生成 · 输出回答", kind: "output", activeNodeIds: ["output"],
    description: "把这个 token 接回上下文末尾，整条流程从第 1 层再走一遍，生成下一个词 ——"
      + "直到吐出结束符。生成 N 个词就要走 N 遍全部 48 层。",
    activeWeightNames: [], tensorShape: "n 个 token", paramCount: 0, flops: 0,
    vis: { op: "loop", title: "重复 N 次 → 得到回答",
           note: "已经算过的部分存在 KV 缓存里：下一轮只算新接上的那个 token，"
             + "不必把前文重算一遍 —— 这就是「第一个词最慢、后面越来越快」的原因。" },
  });

  return steps;
}

/**
 * 词表上的概率**示意**（不是这台机器的实测输出）。
 * 真实概率要把提示词喂给模型做一次前向才有；这里是固定的候选表按提示词长度错开，
 * 只用来讲清「softmax → 概率 → 采样」这件事，界面上明确标了「示意」。
 */
function nextTokenBars(text) {
  const CAND = ["的", "，", "是", "可以", "这个", "\n", "什么", "。"];
  const seed = [...String(text)].reduce((a, c) => a + c.charCodeAt(0), 0);
  const picks = CAND.map((t, i) => ({ token: t, w: Math.max(0.02, 1 / (i + 1 + (seed % 3))) }));
  const sum = picks.reduce((s, p) => s + p.w, 0);
  return picks.map((p) => ({ token: p.token, p: p.w / sum })).sort((a, b) => b.p - a.p);
}

// ------------------------------------------------------------------ 分词（展示用）
//
// 不是真的 BPE：按「汉字单字 / 拉丁词 / 标点」切开，再哈希成稳定的 id。
// 目的是让演示里能看出分词粒度，而且同一个词每次得到同一个 id。
// 真实分词在引擎里做；这里只保证形状与 id 稳定，面板上也如实这么标。

const CJK = "\\u4e00-\\u9fff";
const WORD = new RegExp(`[${CJK}]|[A-Za-z0-9_']+|[^\\sA-Za-z0-9_'${CJK}]+`, "g");

const hash = (s) => {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
};

/** @returns {{ text: string, id: number }[]} */
export function mockTokens(text, vocabSize) {
  const out = [];
  const push = (t) => out.push({ text: t, id: hash(t) % Math.max(1, vocabSize - 1) });
  push("<s>");
  for (const raw of String(text).trim().split(/\s+/).filter(Boolean)) {
    (raw.match(WORD) || [raw]).forEach(push);
  }
  push("</s>");
  return out;
}
