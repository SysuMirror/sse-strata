<script setup>
// 推理过程的画布。纯 SVG，零依赖。
//
// 这一页前后重做过三版，教训是一致的：**先决定「给用户看什么」，再决定怎么画**。
// 第一版 3D 塔、第二版 48 片斜叠的板，都栽在「48 个东西全画出来」上 —— 糊成一团。
// 第三版改成流程图 + 右栏详情，能看懂了，但**不能拖**、点一下只在旁边显示，用起来很别扭。
//
// 现在这版是个真正的画布，两级：
//   · **总览**（第 0 层）：文本输入 → 分词 → 词嵌入 → Transformer → 输出投影 → Softmax，
//     层堆叠只画三块（第一层 / 当前层 / 最后一层）+ ⋯，48 层压成左边一把刻度尺；
//   · **层内部**（第 1 层）：点层块（或演示走到那一层）**进入它**，这是另一块画布，
//     画这一层的算子与它们之间的连线和跳连；面包屑能退回总览。
// 两块画布都能**拖动平移、滚轮缩放**（光标处缩放），带一键复位。
//
// 演示时自动导航：走到某一层 → 自动钻进去；走到输出 → 自动退回总览；
// 层内部还会跟着当前算子平移聚焦，所以「现在算到哪」永远是画面中心的那个东西。
//
// **与模型无关**：每层画哪些算子、谁连谁，全部来自 graph.js 的节点与连线；
// 层内部按**拓扑深度**自动分列（带路由器、多注意力的模型一样能画），
// 点层块跳步也是按节点 id 去步骤表里反查的，没有一处写死「4 个算子 / 4 步一层」。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { activeNodes, closeHud, currentStep, goTo, hoverNode, selectNode, viz } from "../store.js";
import { NODE_STYLE } from "../style.js";
import { setTab } from "../../store.js";
import { openChat } from "../../chats.js";
import VizGlyph from "./VizGlyph.vue";

const { t } = useI18n();
const host = ref(null);
const size = ref({ w: 900, h: 700 });

// 画布视图：屏幕坐标 = 世界坐标 × k + (x, y)
const cam = reactive({ k: 1, x: 0, y: 0 });
// 当前在哪一级：0 = 总览，1 = 某一层的内部
// 层级：0 总览 → 1 层内部 → 2 算子的矩阵构成 → 3 单个矩阵。
// card = 演示模式（总图 + 从当前块长出来的单步卡），它压在这几级之上。
const nav = reactive({ level: 0, layer: 0, node: null, w: 0, card: false, cardStep: null,
                       step: null, stepFocus: null, hold: false });

// 圆角与算子方块高度是定值；总览方块的**宽 / 高 / 间距**跟着画布走（见下）。
const OP = { h: 62, r: 12 };
const BOX_R = 14;
const MIN_K = 0.25, MAX_K = 4;

// 窄屏（手机）整套尺寸都要收：手机屏上要是还按桌面那套 340×58、间距 26 画，
// 取景为了让整条流程塞进 390×456 会把比例压到 0.55 —— 15px 的字变成 8px，等于给张看不清的缩略图。
// 方块收窄收矮、间距压紧之后，同样的画面比例能留在 0.75 上下，字还是字。
const narrow = computed(() => size.value.w < 700);
const opW = computed(() => (narrow.value ? 132 : Math.round(Math.min(170, size.value.w * 0.3))));

// ------------------------------------------------------------------ 模型结构

const model = computed(() => {
  const nodes = viz.graph.nodes || [];
  const edges = viz.graph.edges || [];
  const byLayer = new Map();
  const head = [];
  const tail = [];
  for (const n of nodes) {
    if (n.layer != null) {
      if (!byLayer.has(n.layer)) byLayer.set(n.layer, []);
      byLayer.get(n.layer).push(n);
    } else if (n.type === "output") tail.push(n);
    else head.push(n);
  }
  // 每层的**性质**：看这一层的注意力节点是「线性」，还是「全注意力」。
  // 48 层里 36 层线性、12 层全注意力，两者不是一回事，图上必须分得开。
  const layers = [...byLayer.entries()].sort((a, b) => a[0] - b[0]).map(([i, ns]) => {
    const att = ns.find((n) => n.type === "attention" || n.type === "linear") || null;
    return { i, nodes: ns, kind: att ? att.type : null,
             tone: att ? NODE_STYLE[att.type].color : "var(--st-line)",
             kindLabel: att ? t(NODE_STYLE[att.type].labelKey) : "" };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const kinds = new Map(layers.map((l) => [l.i, l.kind]));
  return { layers, head, tail, edges, byId, kinds };
});

const activeLayer = computed(() => {
  const ids = currentStep.value ? currentStep.value.activeNodeIds : [];
  const hit = ids.find((id) => /^l\d+\./.test(id));
  return hit ? Number(hit.slice(1).split(".")[0]) : -1;
});

// ------------------------------------------------------------------ 总览的内容

/**
 * 总览按**数据流图**排：从左往右流，一行放不下就换到下一行（跟读段落一样），
 * 换行处用一条 S 形曲线把上一行的末尾接到下一行的开头。
 * 段与段之间一定换行 —— 头（输入/分词/词嵌入）、层堆叠、尾（投影/Softmax）各自成段，
 * 免得出现「第 1 层和词嵌入挤在同一行」这种读起来莫名其妙的分行。
 * 一行放几个跟屏幕宽度走：桌面 3 个、手机 2 个。
 */
function overviewContent() {
  const nar = narrow.value;
  const bw = nar ? 152 : 232, bh = nar ? 44 : 54;
  const GAP = nar ? 30 : 40;
  const COLS = nar ? 2 : 4;                     // 一行放几个「整块」；⋯ 只算半块
  const ROWH = bh + (nar ? 74 : 88);            // 行距：留出跨行曲线的空间

  const { head, tail, layers } = model.value;
  const n = layers.length;
  const items = [];

  // 层堆叠：第一层 / 当前层 / 最后一层 —— 当前层永远在画面里，演示时不用移镜头。
  // 当前层就是首尾那两层时要补一个邻居，**保证恒为三块**（少一块会让人以为模型只有两层）。
  const act = activeLayer.value >= 0 ? activeLayer.value : 1;
  const shown = [];
  for (const i of [0, act, n - 1, act - 1, act + 1, 1, 2, n - 2]) {
    if (shown.length >= 3) break;
    if (i >= 0 && i < n && !shown.includes(i)) shown.push(i);
  }
  shown.sort((a, b) => a - b);

  // 层块要写清「这一层是啥」：编号 + 性质（线性 / 全注意力），并按性质上色
  const layerSeg = [];
  shown.forEach((li, k) => {
    const L = model.value.layers[li] || {};
    layerSeg.push({ kind: "layer", id: `L${li}`, layer: li, tone: L.tone,
                    label: t("viz.layer.n", { n: li + 1 }), sub: L.kindLabel });
    if (k < shown.length - 1) {
      const rest = shown[k + 1] - li - 1;
      if (rest > 0) {
        // 中间那一段里有几种性质、各多少层 —— 「还有 45 层」太糊了
        let a = 0, b = 0;
        for (let j = li + 1; j < shown[k + 1]; j++) {
          const kind = model.value.kinds.get(j);
          if (kind === "linear") a++; else if (kind === "attention") b++;
        }
        const mix = [a ? `${a} 层线性` : "", b ? `${b} 层全注意力` : ""].filter(Boolean).join(" · ");
        layerSeg.push({ kind: "dots", id: `dots${k}`, span: 0.5,
                        label: t("viz.more.layers", { n: rest }), sub: mix });
      }
    }
  });

  const segs = [
    head.map((nd) => ({ kind: "box", id: nd.id, node: nd, label: nd.label })),
    layerSeg,
    tail.map((nd) => ({ kind: "box", id: nd.id, node: nd, label: nd.label })),
  ];

  // 摆位：每个元素占 span 格（⋯ 只占半格，省得把「第 48 层」挤到单独一行），
  // 一行放满就换行；段与段之间一定换行。
  const rows = [];
  let row = 0, col = 0, cur = [];
  const place = (seg) => {
    for (const it of seg) {
      const span = it.span || 1;
      if (col + span > COLS) { rows.push(cur); cur = []; row++; col = 0; }
      it.w = Math.round(bw * span); it.h = bh;
      it.x = Math.round(col * (bw + GAP)); it.y = row * ROWH;
      cur.push(it); items.push(it); col += span;
    }
    if (cur.length) { rows.push(cur); cur = []; }
    if (col > 0) { row++; col = 0; }
  };
  segs.forEach(place);

  // 每一行横向居中（左对齐会显得右边缘参差）
  const maxW = Math.max(...rows.map((r) => r.reduce((s, it) => Math.max(s, it.x + it.w), 0)));
  rows.forEach((r) => {
    const w = r.reduce((s, it) => Math.max(s, it.x + it.w), 0);
    const shift = (maxW - w) / 2;
    r.forEach((it) => { it.x += shift; });
  });

  // 连线：整条流水按顺序接起来，同行走直线（带箭头），跨行绕一条 S 形曲线
  const edges = [];
  for (let i = 0; i < items.length - 1; i++) {
    const a = items[i], b = items[i + 1];
    edges.push({ id: `${a.id}->${b.id}`, from: a.id, to: b.id, d: flowPath(a, b) });
  }

  if (!items.length) return { items, edges, rail: [], w: 1, h: 1 };
  const width = Math.max(...items.map((it) => it.x + it.w));
  const height = Math.max(...items.map((it) => it.y + it.h));
  return { items, edges, rail: [], w: width, h: height };
}

/** 同一行：右边缘 → 左边缘；跨行：底部 → 下一行第一个的顶部，走一条 S 形曲线 */
function flowPath(a, b) {
  if (Math.abs(a.y - b.y) < 1) {
    const y = a.y + a.h / 2, x1 = a.x + a.w, x2 = b.x;
    return `M${x1},${y} L${x2},${y}`;
  }
  const x1 = a.x + a.w / 2, y1 = a.y + a.h;
  const x2 = b.x + b.w / 2, y2 = b.y;
  const my = (y1 + y2) / 2;
  return `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`;
}

// ------------------------------------------------------------------ 单个步骤的内容（第 2 级）

// 张量一律画成**平面格子**（不是立体块）：行 × 列的小格 + 形状标签。
// 乘法用**连线**画出来——A 的第 i 行格子连到 B 的第 k 列格子，连起来的那几对就是要相乘的，
// 这比画两个立体方块清楚得多，也是矩阵乘法的本来面目。格子数只是示意（最多 6×6），
// 真实大小写在下面的形状标签里。
const T = (id, x, y, rows, cols, label, mark, side) => ({
  kind: "grid", id, x, y, w: cols * 21, h: rows * 21, rows, cols, label, mark, side, hold: 30,
});

/** 格子阵列多大：按真实形状的比例来（最多 5 行 6 列）。
 *  [10, 1] 就画成一列、[1, 248320] 就画成一行 —— 不然 n×1 和 n×2560 长得一样，等于没画。 */
function cellsFor(shape, capR = 5, capC = 6) {
  const n = (v, cap) => Math.min(cap, Math.max(1, parseInt(String(v).replace(/[^\d]/g, ""), 10) || 1));
  const rows = n(shape?.[0], capR), cols = n(shape?.[1], capC);
  // **格子画成正方形**，整块的长宽比就跟着形状走：
  // [10, 2560] 是近似方形，[1, 248320] 是一条横着的长条 —— 而不是不管什么形状都撑成同一个方框
  // （那样 1×n 会被拉成几根瘦高的竖条，很怪）。
  const cell = narrow.value ? 15 : 21;
  return { rows, cols, w: cols * cell, h: rows * cell };
}

const fmt = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));

/** 没有权重矩阵的算子，用符号 + 类型名说清它在干什么 */
const OP_SYMBOL = { layernorm: "归一化", attention: "注意力", linear: "递推", ffn: "FFN",
                    residual: "＋", embedding: "查表", output: "投影", input: "文本", tokenizer: "切分" };
const typeKey = (t) => (t === "linear" ? "linear" : t);

/**
 * 这一步在算什么：进来的张量、权重、怎么算、出去什么。
 * 返回的坐标是**相对 (0,0)** 的，由调用方决定摆在哪。
 */
function stepBody(s) {
  const v = s && s.vis;
  const nar = narrow.value;
  const GH = nar ? 78 : 104;                            // 符号与并行条用的行高（格子阵列自己按形状定高）
  const SYM = nar ? 26 : 40, GAP = nar ? 26 : 46;
  const items = [], wires = [];
  if (!v) return { items, wires, w: 1, h: 1 };

  // ---- 字符 / token / id：**token 与 id 成对画在一张卡上**，对照着看
  if (v.op === "chars" || v.op === "chips" || v.op === "pairs") {
    const pairs = v.pairs || v.chips.map((c) => ({ top: c }));
    const cg = 8, ch = v.pairs ? (nar ? 52 : 60) : (nar ? 34 : 44);
    let x = 0;
    pairs.forEach((p, i) => {
      const w = chipW(p, v.pairs ? 15 : 16) + (v.pairs ? 26 : 18);
      items.push({ kind: "chip", id: `chip${i}`, top: p.top, bottom: p.bottom,
                   x, y: 0, w, h: ch, focus: v.focus });
      x += w + cg;
    });
    const total = x - cg;
    return { items, wires, w: total, h: ch };
  }

  // ---- 张量 → 运算 → 张量
  const seq = [];
  const sym = (label, sub) => ({ kind: "sym", id: `sym${seq.length}`, label, sub, w: SYM, h: GH });
  const ca = cellsFor(v.in);
  const A = T("__a", 0, 0, ca.rows, ca.cols, fmtShape(v.in), null, v.inSide);
  seq.push(A);

  if (v.op === "matmul") {
    seq.push(sym("×", "逐对相乘再求和"));
    const B = T("__w", 0, 0, cellsFor(v.w).rows, cellsFor(v.w).cols, fmtShape(v.w), "col0");
    seq.push(B);
    seq.push(sym("=", null));
    seq.push(T("__c", 0, 0, cellsFor(v.out).rows, cellsFor(v.out).cols, fmtShape(v.out), "row0"));
  } else if (v.op === "lookup") {
    seq.push(sym("查表", `${v.table[0]} 行里取`));
    seq.push(T("__w", 0, 0, cellsFor(v.table).rows, cellsFor(v.table).cols, fmtShape(v.table), "row0"));
    seq.push(sym("=", null));
    seq.push(T("__c", 0, 0, cellsFor(v.out).rows, cellsFor(v.out).cols, fmtShape(v.out), "row0"));
  } else if (v.op === "moe") {
    seq.push(sym("路由", `${v.branchTotal} 选 ${v.branches}`));
    seq.push(T("__w", 0, 0, cellsFor(v.w).rows, cellsFor(v.w).cols, fmtShape(v.w), "col0"));
    seq.push(sym("=", null));
    seq.push(T("__c", 0, 0, cellsFor(v.out).rows, cellsFor(v.out).cols, fmtShape(v.out), "row0"));
  } else if (v.op === "norm") {
    seq.push(sym("归一化", "逐 token 沿最后一维"));
    seq.push(T("__c", 0, 0, cellsFor(v.out).rows, cellsFor(v.out).cols, fmtShape(v.out), "row0"));
  } else if (v.op === "add") {
    seq.push(sym("＋", "逐元素相加"));
    seq.push(T("__b", 0, 0, cellsFor(v.in2).rows, cellsFor(v.in2).cols, fmtShape(v.in2), "row0"));
    seq.push(sym("=", null));
    seq.push(T("__c", 0, 0, cellsFor(v.out).rows, cellsFor(v.out).cols, fmtShape(v.out), "row0"));
  } else if (v.op === "softmax") {
    seq.push(sym("softmax", "归一成概率"));
    seq.push({ kind: "bars", id: "__bars", bars: v.bars, cap: t("viz.step.schematic"),
               w: nar ? 200 : 280, h: v.bars.length * 30 });
  } else if (v.op === "sample") {
    // 采样：左边是分布示意，右边是**真实**选出来的那个 token
    const a = viz.answer;
    const first = a.state === "done" ? [...a.text][0] || "" : "";
    const idx = v.bars.findIndex((b) => b.token === first);
    seq.push({ kind: "bars", id: "__bars", bars: v.bars, pick: idx >= 0 ? idx : null,
               cap: t("viz.step.schematic"), w: nar ? 200 : 280, h: v.bars.length * 30 });
    seq.push(sym("→", "采样"));
    seq.push({ kind: "chip", id: "__pick", top: a.state === "done" ? first : (a.state === "loading" ? "…" : "未取到"),
               bottom: a.state === "done" ? t("viz.step.real") : null,
               x: 0, y: 0, w: 120, h: 74, focus: "id" });
  } else if (v.op === "loop") {
    // 重复生成：把回答按字摆开，末尾接一条回到开头的循环箭头
    const a = viz.answer;
    const all = a.state === "done" ? [...a.text] : [];
    const cap = nar ? 24 : 60;
    const chars = all.slice(0, cap);
    const cw = 30, ch = 34, cg = 6;
    const perRow = Math.max(6, Math.floor((nar ? 320 : 700) / (cw + cg)));
    chars.forEach((c, i) => {
      items.push({ kind: "chip", id: `o${i}`, top: c === "\n" ? "⏎" : c,
                   x: (i % perRow) * (cw + cg), y: 96 + Math.floor(i / perRow) * (ch + cg), w: cw, h: ch });
    });
    // 没显示完的：补一个「…」，并说明还有多少字（全文在对话页能看到）
    if (all.length > cap) {
      const last = items.filter((i) => i.kind === "chip").pop();
      items.push({ kind: "chip", id: "more", top: "…",
                   x: last.x + last.w + cg, y: last.y, w: Math.max(cw, 54), h: ch });
      // 这行是可点的：直接把人带到对话页那条完整回答上（不然「见对话页」还是句空话）
      items.push({ kind: "note", id: "rest", align: "start", action: "openChat",
                   chatId: a.chatId || null,
                   label: t("viz.step.moreText", { n: all.length - cap }),
                   x: last.x + last.w + cg + 64, y: last.y + 22, w: 320, h: 20 });
    }
    if (!chars.length) {
      // 如实说：没跑过 / 正在跑 / 没取到（登录或用本机 token 才能发对话）
      const label = a.state === "loading" ? t("viz.step.running")
        : a.state === "error" ? t("viz.step.failed", { err: a.error })
        : t("viz.step.noanswer");
      items.push({ kind: "note", id: "wait", align: "start", label, x: 0, y: 110, w: 620, h: 20 });
    }
    const tok = a.tokens ? t("viz.step.tokens", { n: a.tokens }) : "";
    items.push({ kind: "note", id: "cnt", align: "start", label: `${t("viz.step.answer")} ${tok}`,
                 x: 0, y: 60, w: 460, h: 20 });
    if (chars.length) {
      // 循环箭头：从最后一个字回到开头，表示「整条流程重走一遍」
      const last = items.filter((i) => i.kind === "chip").pop();
      wires.push({ id: "loop", arc: true,
                   from: { x: last.x + last.w / 2, y: last.y + last.h },
                   to: { x: 16, y: 92 } });
      items.push({ kind: "note", id: "loopcap", align: "start", label: t("viz.step.loop"),
                   x: 24, y: last.y + last.h + 76, w: 560, h: 20 });
    }
  }

  // 竖向居中：格子阵列的高度跟着形状走，符号跟最高的那个对齐
  const rowH = Math.max(...seq.map((i) => i.h));
  let x = 0;
  for (const it of seq) {
    it.x = x;
    it.y = (rowH - it.h) / 2;
    x += it.w + GAP;
    items.push(it);
  }

  // ---- 乘法连线：A 第 0 行的前三格 ↔ B 第 0 列的前三格（就是 c[0][0] 那一串乘法）
  const A2 = items.find((i) => i.id === "__a");
  const B2 = items.find((i) => i.id === "__w");
  if ((v.op === "matmul" || v.op === "moe") && A2 && B2) {
    for (let k = 0; k < Math.min(3, A2.cols, B2.rows); k++) {
      wires.push({ id: `w${k}`, from: cell(A2, 0, k), to: cell(B2, k, 0) });
    }
  }
  // 查表：从「要取的那几行」连到结果
  if (v.op === "lookup" && A2 && B2) {
    for (let k = 0; k < Math.min(3, A2.cols, B2.rows); k++) {
      wires.push({ id: `w${k}`, from: cell(A2, 0, k), to: cell(B2, k, 0) });
    }
  }
  // 并行的那几路（MoE 专家 / 注意力头）：**同一种颜色**排成一排，一眼看出「这一步是并行的」
  if (v.branches) {
    const bw = nar ? 16 : 22, bg = 6, n = Math.min(v.branches, nar ? 8 : 14);
    const label = `${v.branches} 路并行 · 共 ${v.branchTotal} 个${v.op === "moe" ? "专家" : "头"}`;
    const bot = Math.max(...items.map((i) => i.y + i.h + (i.hold || 0)));
    items.push({ kind: "lanes", id: "__lanes", x: 0, y: bot + 34, n, bw, bg, label,
                 w: n * (bw + bg) + 12 + chipW({ top: label }, 13), h: 26 });
  }

  return { items, wires, w: Math.max(...items.map((i) => i.x + i.w)),
           h: Math.max(...items.map((i) => i.y + i.h + (i.hold || 0))) };
}

/**
 * 把一串元素按**画布宽度**折行摆开，并连成一条数据流（同行走直线、跨行用 S 形曲线）。
 * 一排放不下就换行 —— 硬排一行会被取景压到 0.5 倍，字就糊了（用户实测报过）。
 * 层层复用：层内部的算子链、算子内部的矩阵链、演示的单步卡。
 */
function flowLayout(seq, gap = 44, rowGap = 62, top = 26) {
  const maxW = Math.max(260, size.value.w - 120);
  const rows = [];
  let cur = [], used = 0;
  for (const it of seq) {
    const w = it.w || 140;
    const add = cur.length ? gap + w : w;
    if (cur.length && used + add > maxW) { rows.push(cur); cur = []; used = 0; }
    cur.push(it);
    used += cur.length === 1 ? w : gap + w;
  }
  if (cur.length) rows.push(cur);

  let y = top;
  const items = [];
  rows.forEach((r) => {
    const h = Math.max(...r.map((i) => (i.h || 0) + (i.hold || 0)));
    let x = 0;
    r.forEach((it) => { it.x = x; it.y = y; x += it.w + gap; items.push(it); });
    y += h + rowGap;
  });

  const edges = [];
  for (let i = 0; i < items.length - 1; i++) {
    edges.push({ id: `f${i}`, from: items[i].id, to: items[i + 1].id, d: flowPath(items[i], items[i + 1]) });
  }
  const w = Math.max(...items.map((i) => i.x + i.w));
  const h = Math.max(...items.map((i) => i.y + i.h + (i.hold || 0))) + 34;
  return { items, edges, w, h };
}

/** 某个格子的中心点（行列从 0 起） */
function cell(it, r, c) {
  return { x: it.x + (it.w * (c + 0.5)) / it.cols, y: it.y + (it.h * (r + 0.5)) / it.rows };
}

/** 乘法连线：从 a 格连到 b 格，走一条 S 形曲线 */
function wirePath(w) {
  const a = w.from, b = w.to;
  if (w.arc) {                       // 循环箭头：往下绕一大圈再回到起点
    const y = Math.max(a.y, b.y) + 58;
    return `M${a.x},${a.y} C${a.x},${y} ${b.x},${y} ${b.x},${b.y}`;
  }
  const mx = (a.x + b.x) / 2;
  return `M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`;
}

/** 小方块宽度：按最长那行文字估（SVG 文字不会自己撑开格子，中文按 15px、其余按 8px 估） */
function chipW(p, fs) {
  const m = (s) => [...String(s || "")].reduce((a, ch) => a + (ch.charCodeAt(0) > 255 ? fs : fs * 0.56), 0);
  return Math.ceil(Math.max(m(p.top), m(p.bottom || "")));
}

// ------------------------------------------------------------------ 第 2 级：算子的内部（它由哪些矩阵组成）

/**
 * 点进一个算子看它里面是什么：进来张量 → 一个个权重矩阵 → 出去张量。
 * 权重名与形状来自 graph.js 的节点（本机模型的真实张量），这里只是把它们摆成一条链 ——
 * 「一路点到底」的最后一层就是这些线性矩阵本身。
 */
function opContent(node) {
  const nar = narrow.value;
  const GW = nar ? 92 : 124, GH = nar ? 74 : 98, SYM = nar ? 26 : 38, GAP = nar ? 30 : 44;
  const items = [], wires = [], edges = [];
  if (!node) return { items, edges, rail: [], w: 1, h: 1 };

  const seq = [];
  // 名字去掉 "blk.12." 这种前缀（右栏有全名）：留着的话相邻两块的名字会叠在一起
  const shortName = (n) => String(n || "").replace(/^blk\.\d+\./, "");
  // 每张格子下面还要写一行形状标签（[2560, 248,320] 比格子本身还宽），
  // 不算进去的话标签会顶出卡片的白底 —— 用户实测报过。
  const grid = (id, shape, name, mark, wi) => {
    const c = cellsFor(parseShape(shape));
    const label = shape || "";
    const w = Math.max(c.w, chipW({ top: label }, 15) + 16);
    return { kind: "grid", id, w, hold: 30, h: c.h, rows: c.rows, cols: c.cols,
             label, name: shortName(name), mark, wi };
  };
  const ws = node.weights || [];
  if (node.inputShape) seq.push(grid("__in", node.inputShape, t("viz.panel.in")));
  if (ws.length) {
    // 「×」只画一次：每个权重前面都摆一个乘号只会把链拉长、把画面压小
    seq.push({ kind: "sym", label: "×", sub: null, w: SYM, h: GH });
    ws.forEach((w, i) => seq.push(grid(`w${i}`, w.shape, w.name, i === 0 ? "col0" : null, i)));
  } else {
    // 这一步没有权重矩阵（残差相加、归一化、注意力打分…）也要**能进来看到在算什么**：
    // 画成「进 → 运算 → 出」。以前这里点不动 / 进去一片空白，用户实测报过。
    seq.push({ kind: "sym", label: OP_SYMBOL[node.type] || "→",
               sub: t(`viz.type.${typeKey(node.type)}`), w: SYM, h: GH });
  }
  if (node.outputShape) {
    seq.push({ kind: "sym", label: "=", sub: null, w: SYM, h: GH });
    seq.push(grid("__out", node.outputShape, t("viz.panel.out")));
  }
  if (seq.length === 1) seq[0].mark = null;

  const laid = flowLayout(seq, GAP);
  const A = laid.items.find((i) => i.id === "__in");
  const W = laid.items.find((i) => i.id === "w0");
  if (A && W) for (let k = 0; k < Math.min(3, A.cols, W.rows); k++) {
    wires.push({ id: `w${k}`, from: cell(A, 0, k), to: cell(W, k, 0) });
  }
  const stp = viz.steps.find((x) => x.activeNodeIds && x.activeNodeIds.includes(node.id)) || null;
  return { items: laid.items, edges: laid.edges, wires, rail: [],
           note: stp ? stp.description : "", w: laid.w, h: laid.h };
}

// ------------------------------------------------------------------ 第 3 级：一个矩阵

/** 点进一个权重矩阵：放大的格子 + 名字 + 形状 + 参数量 + 它是干什么的 */
function matrixContent(node, idx) {
  const w = node && node.weights && node.weights[idx];
  if (!w) return { items: [], edges: [], rail: [], w: 1, h: 1 };
  const nar = narrow.value;
  const c = cellsFor(parseShape(w.shape), 8, 8);
  const box = Math.max(nar ? 220 : 340, Math.max(c.w, c.h));      // 放大到最底层那一张：尽量方一点
  const scale = box / Math.max(c.w, c.h);
  const mw = Math.round(c.w * scale), mh = Math.round(c.h * scale);
  const { rows, cols } = c;
  const gw = Math.max(mw, chipW({ top: w.shape }, 15) + 16);
  return {
    items: [{ kind: "grid", id: "__m", x: 0, y: 26, w: gw, hold: 30,
              h: mh, rows: Math.max(rows, 4), cols: Math.max(cols, 4), label: w.shape, name: w.name }],
    edges: [], wires: [], rail: [],
    note: t("viz.matrix.note", { n: fmt(w.paramCount) }),
    w: gw, h: mh + 60,
  };
}

/** "[2560, 640]" → ["2560", "640"] */
function parseShape(s) {
  return String(s || "").replace(/[[\]]/g, "").split(",").map((x) => x.trim()).filter(Boolean);
}

// ------------------------------------------------------------------ 演示：总图 + 从块里长出来的单步卡

/**
 * 演示时的画面**仍然是总图**：这一步落在哪个块上，就把哪个块点亮、其余调暗，
 * 并从这个块下面「长」出一张单步卡讲它内部在算什么；相机只框住「块 + 卡」这一小块。
 * 下一步换块时，相机先退回去、再推进下一个块 —— 进出这个过程本身就是「在哪个块里干活」的交代。
 */
function demoContent(s, focusId) {
  const base = overviewContent();
  // 这一步「落在哪个块」：点了哪块就是哪块（focusId）；演示时取 activeNodeIds 的**最后一个**
  // —— activeNodeIds 是按数据流排的，最后一个才是这一刻真正落到的那一环。
  // （之前用「第一个匹配上的」，于是点词嵌入高亮的却是 Tokenizer、点 Tokenizer 高亮的是文本输入。
  //   因为词嵌入那一步的 activeNodeIds 是 ["tokenize","embed"]，第一个命中的永远是 tokenize。）
  const ids = s.activeNodeIds || [];
  const want = focusId || ids[ids.length - 1] || null;
  const pick = (fn) => base.items.find(fn);
  const tgt = (s.layer != null ? pick((it) => it.layer === s.layer) : null)
    || pick((it) => it.id === want)
    || pick((it) => it.node && ids.includes(it.id));
  if (!tgt) return base;

  const body = stepBody(s);
  const PAD = 22, HEAD = 40;
  const w = body.w + PAD * 2, h = body.h + PAD * 2 + HEAD;
  const cx = Math.max(0, Math.min(tgt.x + tgt.w / 2 - w / 2, base.w - w));
  const cy = tgt.y + tgt.h + 34;
  const card = { kind: "card", id: `card${s.id}`, x: cx, y: cy, w, h, title: s.name };

  // key 里带上步骤号：换一步就重新挂载，卡片出场动画才会重放
  const items = body.items.map((it) => ({ ...it, x: it.x + cx + PAD, y: it.y + cy + PAD + HEAD,
                                          id: `c${s.id}_${it.id}`, body: true }));
  const wires = body.wires.map((wr) => ({ ...wr, id: `c${s.id}_${wr.id}`,
    from: { x: wr.from.x + cx + PAD, y: wr.from.y + cy + PAD + HEAD },
    to: { x: wr.to.x + cx + PAD, y: wr.to.y + cy + PAD + HEAD } }));

  base.items.forEach((it) => { if (it !== tgt) it.dim = true; });
  tgt.hot = true;

  const fx = Math.min(tgt.x, cx), fy = tgt.y;
  // 分三层画：总图 + 卡壳 → **乘法连线** → 卡里的张量。
  // 连线必须在卡壳之上，否则被卡的白底整个盖住（之前就是这么丢的）。
  return {
    items: [...base.items, card, ...items],
    body: items,
    edges: base.edges,
    wires,
    rail: [], caption: null, note: s.vis.note,
    frame: { x: fx, y: fy, w: Math.max(tgt.x + tgt.w, cx + w) - fx, h: cy + h - fy },
    w: Math.max(base.w, cx + w), h: Math.max(base.h, cy + h),
  };
}

/** ["10","2560"] → "[10, 2560]" */
function fmtShape(a) {
  if (!a) return "";
  return Array.isArray(a) ? `[${a.filter((s) => s != null).join(", ")}]` : String(a);
}

// ------------------------------------------------------------------ 层内部的内容

/**
 * 把这一层的算子摆开：按**拓扑深度**分列（谁依赖谁就往右一列），列内垂直居中。
 * 深度是从连线里算的，所以换成「注意力前面多个路由器」「FFN 拆成专家+共享」这类形状，
 * 不用改这里 —— 多几个节点自然会多几列。
 */
function layerContent(index) {
  const layer = model.value.layers[index];
  if (!layer || !layer.nodes.length) return { items: [], edges: [], rail: [], w: 1, h: 1 };
  const { edges: allEdges } = model.value;
  const ids = new Set(layer.nodes.map((n) => n.id));
  const inner = allEdges.filter((e) => ids.has(e.from) && ids.has(e.to));
  const cross = allEdges.filter((e) => ids.has(e.to) !== ids.has(e.from));

  // 拓扑深度：反复松弛，直到不再变（对环有界，最多跑 |V| 轮）。深度只用来定**先后顺序**，
  // 摆位交给 flowLayout —— 原来按深度分列，6 个盒子横着一排挤在 848px 里，字全糊了。
  const depth = new Map(layer.nodes.map((n) => [n.id, 0]));
  for (let pass = 0; pass < layer.nodes.length; pass++) {
    let changed = false;
    for (const e of inner) {
      const d = depth.get(e.from) + 1;
      if (d > depth.get(e.to)) { depth.set(e.to, d); changed = true; }
    }
    if (!changed) break;
  }
  const ordered = [...layer.nodes].sort((a, b) => (depth.get(a.id) - depth.get(b.id))
    || (layer.nodes.indexOf(a) - layer.nodes.indexOf(b)));

  const ow = opW.value;
  const inPort = { kind: "port", id: "__in", label: t("viz.layer.in"), w: 74, h: 44 };
  const outPort = { kind: "port", id: "__out", label: t("viz.layer.out"), w: 74, h: 44 };
  const ops = ordered.map((nd) => ({
    kind: "op", id: nd.id, node: nd, label: nd.label.replace(/^L\d+ · /, ""), w: ow, h: OP.h,
  }));

  const laid = flowLayout([inPort, ...ops, outPort]);
  const items = laid.items, edges = [];
  const pos = new Map(items.map((it) => [it.id, it]));

  // 真实连线：算子之间按图里的边连（这正是「同一条数据流」）
  for (const e of inner) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (a && b && !edges.some((x) => x.id === e.id)) {
      edges.push({ id: e.id, kind: "inner", from: e.from, to: e.to,
                   d: flowPath(a, b) });
    }
  }
  // 跨边界的：进来的（残差那条是跳连，画虚线绕下去）、出去的
  for (const e of cross) {
    if (pos.has(e.to) && !pos.has(e.from)) {
      const b = pos.get(e.to);
      const skip = e.kind === "residual";
      const x1 = inPort.x + inPort.w, y1 = inPort.y + inPort.h / 2, y2 = b.y + b.h / 2;
      edges.push({ id: e.id, kind: skip ? "skip" : "in", from: "__in", to: e.to,
                   d: skip ? skipCurve(x1, y1, b.x, y2) : curve(x1, y1, b.x, y2) });
    } else if (pos.has(e.from) && !pos.has(e.to)) {
      const a = pos.get(e.from);
      edges.push({ id: e.id, kind: "out", from: e.from, to: "__out",
                   d: curve(a.x + a.w, a.y + a.h / 2, outPort.x, outPort.y + outPort.h / 2) });
    }
  }
  return { items, edges, rail: [], w: laid.w, h: laid.h };
}

function curve(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

/** 跳连（残差）单独走一条从下方绕过去的弧。
 *  要是直接连直线，它会从那一排方块**背后**穿过去，只在方块缝里露几段 —— 看着像虚线，
 *  根本认不出这是「跳连」。绕到下面去，这条线才是它该有的样子。 */
function skipCurve(x1, y1, x2, y2) {
  const span = Math.max(60, Math.abs(x2 - x1) * 0.25);
  const drop = 74;
  return `M${x1},${y1} C${x1 + span},${y1 + drop} ${x2 - span},${y2 + drop} ${x2},${y2}`;
}

const content = computed(() => {
  // 演示时：**仍然画总图**，只是当前那个块被点亮、底下长出一张单步卡
  if (nav.card && (nav.cardStep || currentStep.value)) {
    return demoContent(nav.cardStep || currentStep.value, nav.cardStep ? nav.stepFocus : null);
  }
  if (nav.level === 4) return stepCanvas(nav.step || currentStep.value);
  if (nav.level === 3) return matrixContent(nav.node, nav.w);
  if (nav.level === 2) return opContent(nav.node);
  return nav.level === 0 ? overviewContent() : layerContent(nav.layer);
});

/** 上一步是什么、产出了什么 —— 每一步都孤立地摆着，人看不出这是从哪来的 */
const prevStep = computed(() => (viz.index > 0 ? viz.steps[viz.index - 1] : null));

// ------------------------------------------------------------------ 取景与聚焦

// 相机「意图」。窗口一变就得按意图重算机位，而**进演示模式时画布会突然变大** ——
// 那时候如果无脑 fitView，演示刚摆好的聚焦就被冲掉了。所以把意图记下来重新应用：
//   fit = 整体装进视野；element = 盯住某个方块；free = 用户自己拖过/缩过，别动。
let intent = { kind: "fit" };

function fitView(animate = false) {
  intent = { kind: "fit" };
  const full = content.value;
  // 有 frame 就只框住那一小块（演示时＝「当前这个块 + 它长出来的单步卡」）——
  // 相机先推进去、下一步再退出来，进出这个过程就是「现在在哪个块里干活」的交代。
  const c = full.frame
    ? { w: full.frame.w, h: full.frame.h, ox: full.frame.x, oy: full.frame.y }
    : { w: full.w, h: full.h, ox: 0, oy: 0 };
  const nar = narrow.value;
  // 上下留白在窄屏是**不对称**的：上面要让开悬浮的面包屑/缩放（约 44px），下面要让开操作提示。
  const padX = nar ? 20 : 48, padT = nar ? 48 : 48, padB = nar ? 30 : 48;
  const kw = (size.value.w - padX * 2) / Math.max(1, c.w);
  const kh = (size.value.h - padT - padB) / Math.max(1, c.h);
  let k = Math.min(kw, kh, 1.6);
  // 窄屏给一个「至少看得清」的下限：手机竖着放时高度那一项会狠压比例，
  // 15px 的字变成 8px，等于给了张看不清的缩略图。横向绝不许溢出。
  if (nar) k = Math.min(Math.max(k, 0.7), kw);
  if (!isFinite(k) || k <= 0) return;         // 图还没加载出来（items 为空）时别把相机推成 NaN
  setCam({
    k,
    x: (size.value.w - c.w * k) / 2 - c.ox * k,
    y: padT + (size.value.h - padT - padB - c.h * k) / 2 - c.oy * k,
  }, animate);
}

/** 让某个元素居中（演示时跟着当前算子走）。
 *  放大倍数**不许超过「整体刚好装得下」那档** —— 否则聚焦到中间那个算子时，
 *  两头的方块会被挤出画面（之前末尾的「残差」就只露半个）。 */
function focusOn(item, zoom = 1.35, animate = true) {
  if (!item) return fitView(animate);
  intent = { kind: "element", id: item.id, zoom };
  const c = content.value;
  const pad = 48;
  const fitK = Math.min((size.value.w - pad * 2) / Math.max(1, c.w), (size.value.h - pad * 2) / Math.max(1, c.h));
  // 窄屏（手机）另给一个「至少看得清」的下限：那儿内容本来就塞不下，
  // 硬按 fitK 缩到 0.28 只会得到一堆看不懂的小字，不如放大盯着当前这个算子看，其余靠拖。
  const readable = size.value.w < 700 ? 0.88 : 0;
  const k = Math.min(Math.max(Math.min(zoom, Math.max(fitK, readable)), MIN_K), MAX_K);
  // 摆到正中之后再夹一次平移，让**整块内容**留在画面里：只居中的话，聚焦靠边的方块时
  // 另一头会被推出画面（聚焦倒数第二个算子，最左边的「输入」就没了）。
  // 装不下（内容比视口大）时就不夹了，老老实实把当前这个元素摆正中。
  const clampAxis = (v, world, total) => {
    const lo = pad, hi = total - pad - world * k;
    return hi >= lo ? Math.min(Math.max(v, lo), hi) : v;
  };
  setCam({
    k,
    x: clampAxis(size.value.w / 2 - (item.x + item.w / 2) * k, c.w, size.value.w),
    y: clampAxis(size.value.h / 2 - (item.y + item.h / 2) * k, c.h, size.value.h),
  }, animate);
}

/** 尺寸变了：按记下来的意图把机位重算一遍 */
function reapply() {
  if (intent.kind === "free") return;
  if (intent.kind === "element") {
    const it = content.value.items.find((i) => i.id === intent.id);
    if (it) return focusOn(it, intent.zoom, false);
  }
  fitView(false);
}

/** 以画布中心为锚点缩放（工具栏那几个按钮用） */
function zoomBy(f) {
  const k = Math.min(MAX_K, Math.max(MIN_K, cam.k * f));
  const real = k / cam.k;
  intent = { kind: "free" };
  setCam({ k, x: size.value.w / 2 - (size.value.w / 2 - cam.x) * real,
           y: size.value.h / 2 - (size.value.h / 2 - cam.y) * real }, true);
}

// 相机动画：简单地朝目标插值，到得够近就停
let raf = null;
let target = null;
function setCam(next, animate) {
  target = next;
  if (!animate) { Object.assign(cam, next); cancelAnimationFrame(raf); raf = null; return; }
  if (raf) return;
  const step = () => {
    raf = null;
    const dx = target.x - cam.x, dy = target.y - cam.y, dk = target.k - cam.k;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5 && Math.abs(dk) < 0.002) { Object.assign(cam, target); return; }
    // 0.3 而不是更小的插值系数：演示每 1.5 秒换一步，镜头要是慢慢飘过去，
    // 用户想点的那一刻方块还在动 —— 手感是「点不中」。要快，但别硬切。
    cam.x += dx * 0.3; cam.y += dy * 0.3; cam.k += dk * 0.3;
    raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
}

/** 当前该聚焦哪个元素（演示时用它自动平移）。
 *  单步图整张就是这一步，没有「聚焦某一个」的说法，直接整体取景。 */
const focusItem = computed(() => {
  if (nav.level !== 1) return null;
  const c = content.value;
  if (!c.items.length) return null;
  const ids = activeNodes.value;
  return c.items.find((it) => ids.has(it.id)) || null;
});

// 演示走到哪、画面就跟到哪：**每一步都进到「这一步在算什么」那张图**（张量、权重、相乘、并行）。
// 用户自己点进某一层看内部时（hold）就不再抢画面 —— 手动导航优先，按播放才交还自动跟随。
watch(() => viz.playing, (p) => { if (p) nav.hold = false; });
watch([() => currentStep.value && currentStep.value.id, activeLayer], () => {
  if (nav.hold) return;
  const s = currentStep.value;
  if (s && s.vis) {
    nav.card = true;                                       // 演示：总图 + 当前块的单步卡
    nav.cardStep = null;                                   // 交还给「跟着当前步骤走」
    requestAnimationFrame(() => fitView(true));            // 换了一步就重新取景，别停在上一张图的机位
  } else {
    nav.card = false;
  }
}, { immediate: true });

watch(() => viz.graph, () => requestAnimationFrame(() => {
  nav.level = 0;
  requestAnimationFrame(() => fitView(false));
}));

// ------------------------------------------------------------------ 交互

const drag = ref(null);
const moved = ref(false);
const pointers = new Map();          // 当前按下的指针（触摸屏上双指 = 缩放 + 平移）
const pinch = ref(null);

/** 以某个屏幕点为锚点缩放（滚轮、双指、工具栏都走它） */
function zoomAt(px, py, k, animate = false) {
  const next = Math.min(MAX_K, Math.max(MIN_K, k));
  const wx = (px - cam.x) / cam.k, wy = (py - cam.y) / cam.k;
  intent = { kind: "free" };
  setCam({ k: next, x: px - wx * next, y: py - wy * next }, animate);
}

function onWheel(e) {
  e.preventDefault();
  const r = host.value.getBoundingClientRect();
  zoomAt(e.clientX - r.left, e.clientY - r.top, cam.k * Math.exp(-e.deltaY * 0.0015));
}

const local = (e) => {
  const r = host.value.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
};
const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

function onDown(e) {
  // 浮在画布上的工具条按钮 / 下拉框不参与拖拽
  if (e.target.closest && e.target.closest("button, a, input, select")) return;
  if (e.pointerType !== "touch" && e.button !== 0 && e.button !== 1) return;
  // 注意**不要** setPointerCapture：把指针捕到画布容器上，click 就会派发到容器本身而不是
  // 手指底下那个方块，点选节点、点面包屑全都失效（触摸指针本来就有隐式捕获，够用）。
  pointers.set(e.pointerId, local(e));
  moved.value = false;
  if (pointers.size === 2) {                     // 第二根手指落下 → 转成缩放
    const [a, b] = [...pointers.values()];
    pinch.value = { d: dist(a, b), at: mid(a, b) };
    drag.value = null;
    moved.value = true;                          // 双指操作别被当成点击
    return;
  }
  drag.value = { x: e.clientX, y: e.clientY, cx: cam.x, cy: cam.y };
}
function onMove(e) {
  if (!pointers.has(e.pointerId)) return;
  const p = local(e);
  if (pointers.size === 2 && pinch.value) {
    const otherId = [...pointers.keys()].find((id) => id !== e.pointerId);
    const other = pointers.get(otherId);
    pointers.set(e.pointerId, p);
    const a = p, b = other;
    const m = mid(a, b), d = dist(a, b);
    if (pinch.value.d > 0) {
      zoomAt(m.x, m.y, cam.k * (d / pinch.value.d));
      // 手指整体移动也算平移（捏合的同时拖）
      const dx = m.x - pinch.value.at.x, dy = m.y - pinch.value.at.y;
      setCam({ k: cam.k, x: cam.x + dx, y: cam.y + dy }, false);
    }
    pinch.value = { d, at: m };
    moved.value = true;
    return;
  }
  pointers.set(e.pointerId, p);
  if (!drag.value) return;
  const dx = e.clientX - drag.value.x, dy = e.clientY - drag.value.y;
  if (!moved.value && Math.abs(dx) + Math.abs(dy) <= 4) return;   // 手抖不算拖动
  moved.value = true;
  intent = { kind: "free" };
  setCam({ k: cam.k, x: drag.value.cx + dx, y: drag.value.cy + dy }, false);
}
function onUp(e) {
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch.value = null;
  if (pointers.size === 1) {                     // 从双指回到单指：接着让剩下那根拖
    const [only] = [...pointers.values()];
    drag.value = { x: only.x + host.value.getBoundingClientRect().left,
                   y: only.y + host.value.getBoundingClientRect().top, cx: cam.x, cy: cam.y };
  }
  if (pointers.size === 0) drag.value = null;
}

/** 拖过的那一下不要当成点击 */
function pick(fn) { return () => { if (!moved.value) fn(); }; }

/** 工具栏那个「复位」：整体装进视野（层内部则回到当前算子） */
function resetView() {
  if (nav.level === 1) focusOn(focusItem.value, 1.35);
  else fitView(true);
}

function enterLayer(i) {
  nav.hold = true;                 // 手动钻进某一层：演示再往前走也别把画面抢走
  nav.card = false;
  nav.cardStep = null;
  nav.level = 1;
  nav.layer = i;
  nav.node = null; nav.w = 0;
  requestAnimationFrame(() => fitView(true));
  // **不碰演示进度**：点块是「看它的结构」，不是「跳到那一步」。
  // （这里原来有一句 goTo()，结果点一下时间轴就跳走了，用户实测报过。）
}

/** 点总览里的头尾块（文本输入 / Tokenizer / 词嵌入 / 输出投影 / Softmax）：
 *  进入「这一步在算什么」那张图 —— 和演示时自动出现的**是同一张**。 */
function enterCard(node) {
  const step = viz.steps.find((s) => s.activeNodeIds.includes(node.id)) || null;
  if (!step) return;
  // 手动点 = **进一张新画布**（这一步的图单独铺满画布）；演示才是「总图 + 卡」那种。
  nav.hold = true;
  nav.card = false;
  nav.cardStep = null;
  nav.step = step;
  nav.stepFocus = node.id;
  nav.level = 4;
  requestAnimationFrame(() => fitView(true));
}

/** 第 4 级：这一步在算什么，单独铺满一张画布（没有总图垫底） */
function stepCanvas(s) {
  const b = stepBody(s);
  if (!b.items.length) return { items: [], edges: [], rail: [], wires: [], w: 1, h: 1 };
  const PAD = 30, TOP = 34;
  const items = b.items.map((it) => ({ ...it, x: it.x + PAD, y: it.y + TOP, body: true }));
  const wires = b.wires.map((w) => ({ ...w, from: { x: w.from.x + PAD, y: w.from.y + TOP },
                                       to: { x: w.to.x + PAD, y: w.to.y + TOP } }));
  return {
    items, edges: [], wires, rail: [], caption: s.vis.title, note: s.vis.note,
    w: b.w + PAD * 2, h: b.h + TOP + 56,
  };
}

/** 点算子：进它的内部（它由哪些矩阵组成） */
function enterOp(id) {
  const node = viz.graph.nodes.find((n) => n.id === id);
  if (!node) return;
  nav.hold = true;                 // 手动钻进去之后，演示别把画面抢走
  nav.card = false;
  nav.cardStep = null;
  nav.level = 2;
  nav.node = node;
  requestAnimationFrame(() => fitView(true));
}

/** 点矩阵：进到这一个矩阵本身（最底层） */
function enterMatrix(i) {
  nav.level = 3;
  nav.w = i;
  requestAnimationFrame(() => fitView(true));
}

/** 这一项点了有没有反应：有反应的要给出可见的记号（鼠标手型 + 右下角 ›），
 *  不能让用户去猜哪些能点 —— 「很多点不动」就是这么来的。 */
function clickable(it) {
  if (it.kind === "layer") return true;
  if (it.wi != null) return true;
  if (it.action === "openChat") return true;                // 「见对话页」那行
  if (it.node) return nav.level === 0 || nav.level === 1;   // 总览的块、层内部的算子
  return false;
}

/** 画布上点一个东西：层块 → 钻进这一层；算子 → 进它的矩阵构成；权重矩阵 → 进到单个矩阵 */
function onItemClick(it) {
  // 「还有 N 个字，完整回答见对话页」——点一下真的带过去（打开那条会话）
  if (it.action === "openChat") {
    if (it.chatId) openChat(it.chatId);
    setTab("chat");
    return;
  }
  if (it.kind === "layer") return enterLayer(it.layer);
  if (it.wi != null) return enterMatrix(it.wi);
  if (!it.node) return;
  selectNode(it.id);                                   // 右栏跟着换（只会调一次，调两次会取消选中）
  // **每一块都能点进去**，不许有「点了没反应」的：总览里的块 → 这一步的说明图；
  // 层内部的算子 → 它的矩阵构成。算子就算没有权重矩阵，也进去看进出的张量。
  if (nav.level === 0) enterCard(it.node);
  else if (nav.level === 1) enterOp(it.id);
}

/** 点面包屑：回到任意一级 */
function goLevel(level, layer) {
  nav.hold = level !== 0;
  nav.card = false;
  nav.level = level;
  if (level <= 1) { nav.node = null; nav.w = 0; }   // 回到总览/层内部：把「某个算子」的记忆清掉
  if (layer != null) nav.layer = layer;
  requestAnimationFrame(() => fitView(true));
}

const crumb = computed(() => {
  const out = [{ label: t("viz.crumb.overview"), level: 0 }];
  if (nav.card) {                                    // 演示中：总览 › [当前层] › 步骤名
    const al = activeLayer.value;
    if (al >= 0) out.push({ label: t("viz.layer.n", { n: al + 1 }), level: 1, layer: al });
    const cs = nav.cardStep || currentStep.value;
    if (cs) out.push({ label: cs.name, level: 2 });
    return out;
  }
  if (nav.level === 4) {
    const s4 = nav.step || currentStep.value;
    if (s4 && s4.layer != null) out.push({ label: t("viz.layer.n", { n: s4.layer + 1 }), level: 1, layer: s4.layer });
    if (s4) out.push({ label: s4.name, level: 4 });
    return out;
  }
  if (nav.level === 0) return out;          // 回到总览：就只有「总览」一节，别把上一步的残余带出来
  if (nav.level === 1) { out.push({ label: t("viz.layer.n", { n: nav.layer + 1 }), level: 1 }); return out; }
  if (nav.level >= 2 && nav.node) {
    if (nav.node.layer != null) out.push({ label: t("viz.layer.n", { n: nav.node.layer + 1 }), level: 1, layer: nav.node.layer });
    out.push({ label: nav.node.label.replace(/^L\d+ · /, ""), level: 2 });
    if (nav.level >= 3 && nav.node.weights) out.push({ label: nav.node.weights[nav.w].name, level: 3 });
  }
  return out;
});

const isOn = (id) => activeNodes.value.has(id);
const isSel = (id) => viz.selectedNodeId === id;

let ro = null;
onMounted(() => {
  ro = new ResizeObserver(() => {
    if (!host.value) return;
    size.value = { w: host.value.clientWidth || 900, h: host.value.clientHeight || 700 };
    if (!drag.value && !raf) reapply();
  });
  ro.observe(host.value);
  size.value = { w: host.value.clientWidth || 900, h: host.value.clientHeight || 700 };
  fitView(false);
});
onBeforeUnmount(() => { ro && ro.disconnect(); cancelAnimationFrame(raf); });
</script>

<template>
  <div ref="host" class="canvas" :data-drag="drag && moved ? '1' : null" :data-focus="viz.focus ? '1' : null"
       @pointerdown="onDown" @pointermove="onMove" @pointerup="onUp" @pointercancel="onUp" @wheel="onWheel">
    <!-- 点阵底：跟着相机一起动。没有它，拖动时画面里没有任何东西在动 —— 手感等于拖白墙。 -->
    <div class="canvas__grid" :style="{ backgroundSize: `${26 * cam.k}px ${26 * cam.k}px`,
                                       backgroundPosition: `${cam.x}px ${cam.y}px` }" aria-hidden="true"></div>

    <svg class="canvas__svg" :viewBox="`0 0 ${size.w} ${size.h}`" preserveAspectRatio="xMidYMid meet">
      <defs>
        <marker id="cv-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5"
                orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="context-stroke" /></marker>
      </defs>

      <g :transform="`translate(${cam.x} ${cam.y}) scale(${cam.k})`">
        <!-- 连线 -->
        <path v-for="e in content.edges" :key="e.id" class="cv__edge" :class="`is-${e.kind}`"
              :d="e.d" :data-on="isOn(e.to) || isOn(e.from) ? '1' : null" fill="none"
              marker-end="url(#cv-arrow)" />

        <!-- 方块 / 算子 -->
        <VizGlyph v-for="it in content.items" :key="it.id" :it="it"
                  :on="isOn(it.id) || (it.layer != null && it.layer === activeLayer)"
                  :hot="!!it.hot" :sel="isSel(it.id)" :dim="!!it.dim" :click="clickable(it)"
                  :hover="!!it.node && !drag" :acc="it.accent" :type="it.node ? it.node.type : null"
                  @pick="pick(() => onItemClick(it))()"
                  @enter="hoverNode(it.id)" @leave="hoverNode(null)" />

        <!-- 乘法连线画在最后（最上层）：卡片是不透明的白底，画在它下面会被整个盖掉。
             注意要留在相机那层 g 里面 —— 拿到外面就变成屏幕坐标，线会跑到别处去。 -->
        <path v-for="wr in content.wires" :key="`w-${wr.id}`" class="cv__wire" fill="none" :d="wirePath(wr)" />
      </g>
    </svg>

    <!-- 画布工具栏：面包屑 + 缩放。放在 HTML 层，不跟着画布缩放 -->
    <div class="canvas__tools">
      <nav class="canvas__crumb" :aria-label="t('viz.crumb.aria')">
        <template v-for="(c, i) in crumb" :key="i">
          <button v-if="i < crumb.length - 1" class="canvas__crumb-link"
                  @click="goLevel(c.level, c.layer)">{{ c.label }}</button>
          <span v-else class="canvas__crumb-now">{{ c.label }}</span>
          <span v-if="i < crumb.length - 1" class="canvas__crumb-sep">›</span>
        </template>
      </nav>
      <div class="canvas__zoom">
        <button :title="t('viz.zoom.out')" :aria-label="t('viz.zoom.out')" @click="zoomBy(1 / 1.3)">−</button>
        <button :title="t('viz.zoom.fit')" :aria-label="t('viz.zoom.fit')" @click="resetView">⤢</button>
        <button :title="t('viz.zoom.in')" :aria-label="t('viz.zoom.in')" @click="zoomBy(1.3)">＋</button>
      </div>
    </div>

    <p v-if="!viz.hudClosed" class="canvas__hud" :data-on="currentStep ? '1' : null">
      <template v-if="currentStep"><b>{{ currentStep.name }}</b></template>
      <template v-else>{{ t("viz.panel.idle") }}</template>
      <button class="canvas__hud-x" :title="t('common.remove')" :aria-label="t('common.remove')"
              @click="closeHud">×</button>
    </p>
    <!-- 单步卡的补充说明：上面接上一句「上一步产出了什么」，下面一句「为什么这么算」 -->
    <p v-if="(nav.card || nav.level === 4) && prevStep" class="canvas__prev">
      {{ t("viz.step.prev") }} <b>{{ prevStep.name }}</b><span>{{ prevStep.tensorShape }}</span>
    </p>
    <p v-if="content.note" class="canvas__note">{{ content.note }}</p>
    <!-- 跳过了同性质的一大段：得说一句，不然用户以为演示出错了 -->
    <p v-if="viz.skipFlash" class="canvas__skip">
      {{ t("viz.step.skipped", { n: viz.skipFlash.n, layer: viz.skipFlash.to, kind: viz.skipFlash.kind }) }}
    </p>

    <!-- 触屏上说「滚轮」等于没说：按输入方式给不同的提示 -->
    <p class="canvas__hint">
      <span class="canvas__hint--mouse">{{ t("viz.iso.hint") }}</span>
      <span class="canvas__hint--touch">{{ t("viz.iso.hint.touch") }}</span>
    </p>
  </div>
</template>

<style scoped>
.canvas { position: relative; width: 100%; height: 100%; overflow: hidden; background: var(--st-surface);
          cursor: grab; touch-action: none; }
.canvas[data-drag] { cursor: grabbing; }
/* 点阵在**最底下**（绝对定位的盒子会盖在静态内容之上，所以图那边得显式抬一层） */
.canvas__grid { position: absolute; inset: 0; z-index: 0; pointer-events: none; opacity: .5;
                background-image: radial-gradient(circle, var(--st-line) 1px, transparent 1.2px); }
.canvas__svg { position: relative; z-index: 1; }
/* 工具条 / HUD / 提示浮在图上面 */
.canvas__tools, .canvas__hud, .canvas__hint { z-index: 2; }
.canvas__svg { display: block; width: 100%; height: 100%; color: var(--st-line); }

/* 连线：默认淡，点亮时换主色；跳连是虚线（残差本来就绕开中间几个） */
.cv__edge { stroke: var(--st-line); stroke-width: 1.4; }
.cv__edge.is-skip { stroke: var(--st-accent-text); stroke-dasharray: 7 5; opacity: .8; }
.cv__edge[data-on] { stroke: var(--st-accent); stroke-width: 2; opacity: 1; }

.canvas__tools { position: absolute; left: 14px; top: 12px; display: flex; align-items: center; gap: 10px; }
.canvas__crumb { display: flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: var(--st-r-sm);
                 background: var(--st-surface); border: 1px solid var(--st-line); box-shadow: var(--st-sh-card); }
.canvas__crumb-link { border: 0; background: none; padding: 0; cursor: pointer; color: var(--st-accent-text);
                      font: 600 var(--st-fs-sm) var(--st-font); }
.canvas__crumb-link:hover { text-decoration: underline; }
.canvas__crumb-sep { color: var(--st-ink-muted); font-size: var(--st-fs-sm); }
.canvas__crumb-now { color: var(--st-ink); font: 700 var(--st-fs-sm) var(--st-font); }
.canvas__zoom { display: flex; gap: 2px; padding: 3px; border-radius: var(--st-r-sm); background: var(--st-surface);
                border: 1px solid var(--st-line); box-shadow: var(--st-sh-card); }
.canvas__zoom button { width: 30px; height: 26px; border: 0; border-radius: 7px; background: transparent;
                       color: var(--st-ink-muted); cursor: pointer; font-size: 15px; line-height: 1; }
.canvas__zoom button:hover { background: var(--st-surface-2); color: var(--st-ink); }

.canvas__hud { position: absolute; right: 14px; top: 12px; margin: 0; display: flex; align-items: baseline; gap: 8px;
               padding: 6px 12px; border-radius: var(--st-r-sm); background: var(--st-surface);
               border: 1px solid var(--st-line); box-shadow: var(--st-sh-card); max-width: 40%; }
.canvas__hud b { font: 700 var(--st-fs-sm) var(--st-font); color: var(--st-ink); white-space: nowrap;
                 overflow: hidden; text-overflow: ellipsis; min-width: 0; }
/* 提示卡上的叉：这张卡是浮在画布上的，什么时候不想看就该能关掉 */
.canvas__hud-x { flex: none; align-self: flex-start; margin: -2px -6px 0 2px; width: 20px; height: 20px;
                 border: 0; border-radius: 6px; background: transparent; color: var(--st-ink-muted);
                 font-size: 16px; line-height: 1; cursor: pointer; }
.canvas__hud-x:hover { background: var(--st-surface-2); color: var(--st-ink); }
.canvas__hud span { font: 400 11px var(--st-font-mono); color: var(--st-ink-muted); white-space: nowrap;
                    overflow: hidden; text-overflow: ellipsis; min-width: 0; }
/* 单步图的两句说明：上面「在算什么」，下面「为什么」。用 HTML 画，
   因为 SVG 的 text 不会自动折行，而这两句是整句话。 */
.canvas__cap { position: absolute; left: 50%; top: 14px; transform: translateX(-50%); margin: 0; z-index: 2;
               padding: 5px 14px; border-radius: var(--st-r-pill); max-width: 70%;
               background: var(--st-surface); border: 1px solid var(--st-line); box-shadow: var(--st-sh-card);
               font-size: var(--st-fs-sm); font-weight: 700; color: var(--st-ink);
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none; }
/* 「上一步产出了什么」：每一步都孤立地摆着，人看不出这一步的东西是从哪来的 */
.canvas__prev { position: absolute; left: 14px; top: 56px; margin: 0; z-index: 2; max-width: 46%;
                display: flex; align-items: baseline; gap: 6px; padding: 4px 11px;
                border-radius: var(--st-r-pill); background: var(--st-surface-2);
                border: 1px solid var(--st-line); font-size: var(--st-fs-xs); color: var(--st-ink-muted);
                white-space: nowrap; overflow: hidden; pointer-events: none; }
.canvas__prev b { color: var(--st-ink-soft); font-weight: 700; }
.canvas__prev span { font-family: var(--st-font-mono); color: var(--st-ink-muted);
                     overflow: hidden; text-overflow: ellipsis; }
.canvas[data-focus] .canvas__prev { top: 84px; }
/* 跳过同性质的一大段时闪一句说明 */
.canvas__skip { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); margin: 0; z-index: 3;
                padding: 10px 18px; border-radius: var(--st-r-md); max-width: min(560px, 80%); text-align: center;
                background: color-mix(in srgb, var(--st-accent) 12%, var(--st-surface));
                border: 1px solid color-mix(in srgb, var(--st-accent) 40%, transparent);
                color: var(--st-accent-text); font-size: var(--st-fs-sm); font-weight: 600; line-height: 1.6;
                pointer-events: none; animation: skipIn .25s ease both; }
@keyframes skipIn { from { opacity: 0; transform: translate(-50%, -50%) scale(.96); } to { opacity: 1; } }
.canvas__note { position: absolute; left: 50%; bottom: 34px; transform: translateX(-50%); margin: 0; z-index: 2;
                max-width: min(760px, calc(100% - 40px)); text-align: center; line-height: 1.6;
                font-size: var(--st-fs-sm); color: var(--st-ink-soft); pointer-events: none; }
/* 演示模式下顶上那条悬浮胶囊与底下那条时间轴会压住这两句，各让一步 */
.canvas[data-focus] .canvas__cap { top: 84px; }
.canvas[data-focus] .canvas__note { bottom: 84px; }

.canvas__hint { position: absolute; left: 14px; bottom: 10px; margin: 0; font-size: var(--st-fs-xs);
                color: var(--st-ink-muted); pointer-events: none; transition: bottom .3s; }
.canvas__hint--touch { display: none; }
@media (hover: none) {                     /* 触屏设备：换一句说人话的 */
  .canvas__hint--mouse { display: none; }
  .canvas__hint--touch { display: inline; }
}
/* 演示模式下时间轴浮在底部中央 —— 提示要让开，不然被压在下面（手机上尤其窄） */
.canvas[data-focus] .canvas__hint { bottom: 84px; }

/* 手机上**不藏东西**：面包屑、缩放、当前步骤与张量形状全留着（这几样是「看懂现在算到哪」的最小集），
   只把字号与留白收紧、位置错开。真正能省的是那句操作提示 —— 演示模式下时间轴浮在底部，正好压住它。 */
@media (max-width: 720px) {
  .canvas__tools { left: 10px; top: 10px; gap: 6px; }
  .canvas__crumb { padding: 5px 10px; }
  .canvas__zoom button { width: 28px; height: 24px; }
  /* 步骤名与张量形状都留着（手机上看懂「现在算到哪」就靠这条），放不下就省略号，不整个砍掉。
     没开始演示时那条长提示在手机上纯属占地方（右栏下面就有同一句），这时不显示。 */
  .canvas__hud { right: 10px; top: 10px; max-width: 48%; padding: 5px 10px; gap: 6px; }
  .canvas__hud:not([data-on]) { display: none; }
  /* 演示模式下那条悬浮胶囊占满了手机顶部一整条，面包屑和 HUD 让到它下面去 */
  .canvas[data-focus] .canvas__tools, .canvas[data-focus] .canvas__hud { top: 82px; }
  .canvas__hud b { font-size: var(--st-fs-xs); }
  .canvas__hud span { font-size: 10px; }
  .canvas__hint { left: 10px; bottom: 8px; right: 10px; }
}
</style>
