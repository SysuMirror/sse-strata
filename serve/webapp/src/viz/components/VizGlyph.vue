<script setup>
// 画布上的一个「字形」：方块 / 算子 / 张量格 / 运算符号 / 小方块 / 概率条 / 并行条 / 卡片 / 文字。
//
// 抽成独立组件，是为了让这套图形语言能**复用**：它只认 kind 与形状，
// 不认具体是哪个模型、哪一层、哪一步 —— 换一套权重形状照样画得出来。
// 状态（点亮 / 变暗 / 能不能点）由父级通过 props 传入，这里只负责画。
defineProps({
  it: { type: Object, required: true },
  on: Boolean, hot: Boolean, sel: Boolean, dim: Boolean, click: Boolean, hover: Boolean,
  acc: String, type: String,
});
const emit = defineEmits(["pick", "enter", "leave"]);

/** 这一刻真正在参与的那一行 / 那一列 */
const marked = (it, i, j) => (it.mark === "row0" && i === 0) || (it.mark === "col0" && j === 0);
</script>

<template>
  <g class="cv__item" :class="[`is-${it.kind}`]"
     :data-on="on ? '1' : null" :data-hot="hot ? '1' : null" :data-sel="sel ? '1' : null"
     :data-dim="dim ? '1' : null" :data-acc="acc || null" :data-click="click ? '1' : null"
     :data-type="type || null"
     @click="click && emit('pick')" @pointerenter="hover && emit('enter')" @pointerleave="hover && emit('leave')">

    <!-- 单步卡的外框（里面那张图由父级另摆） -->
    <template v-if="it.kind === 'card'">
      <rect class="card__bg" :x="it.x" :y="it.y" :width="it.w" :height="it.h" rx="16" />
      <text class="card__title" :x="it.x + 20" :y="it.y + 27">{{ it.title }}</text>
      <line class="card__rule" :x1="it.x + 20" :x2="it.x + it.w - 20" :y1="it.y + 38" :y2="it.y + 38" />
    </template>

    <!-- 张量：平面格子阵列，标记的格子＝这一刻在用的那一行/列 -->
    <template v-else-if="it.kind === 'grid'">
      <rect class="g__bg" :x="it.x" :y="it.y" :width="it.w" :height="it.h" rx="6" />
      <template v-for="(r, i) in it.rows" :key="`r${i}`">
        <rect v-for="(c, j) in it.cols" :key="`c${j}`" class="g__cell"
              :class="{ 'is-mark': marked(it, i, j) }"
              :x="it.x + (it.w * j) / it.cols + 1" :y="it.y + (it.h * i) / it.rows + 1"
              :width="it.w / it.cols - 2" :height="it.h / it.rows - 2" rx="2" />
      </template>
      <text v-if="it.name" class="g__name" :x="it.x + it.w / 2" :y="it.y - 12" text-anchor="middle">{{ it.name }}</text>
      <text class="g__shape" :x="it.x + it.w / 2" :y="it.y + it.h + 22" text-anchor="middle">{{ it.label }}</text>
      <text v-if="it.wi != null" class="g__enter" :x="it.x + it.w - 12" :y="it.y + it.h - 10" text-anchor="end">›</text>
    </template>

    <!-- 运算符号 -->
    <template v-else-if="it.kind === 'sym'">
      <circle class="sym__dot" :cx="it.x + it.w / 2" :cy="it.y + it.h / 2 - 10" :r="it.w / 2 - 2" />
      <text class="sym__label" :x="it.x + it.w / 2" :y="it.y + it.h / 2 - 3" text-anchor="middle">{{ it.label }}</text>
      <text v-if="it.sub" class="sym__sub" :x="it.x + it.w / 2" :y="it.y + it.h / 2 + 26" text-anchor="middle">{{ it.sub }}</text>
    </template>

    <!-- 小方块：上「词」下「id」，成对画在一起才看得懂谁对应谁 -->
    <template v-else-if="it.kind === 'chip'">
      <rect class="chip__bg" :x="it.x" :y="it.y" :width="it.w" :height="it.h" rx="8" />
      <text class="chip__top" :x="it.x + it.w / 2" :y="it.y + (it.bottom ? 24 : it.h / 2 + 5)"
            text-anchor="middle">{{ it.top === "\n" ? "⏎" : it.top }}</text>
      <text v-if="it.bottom" class="chip__bot" :class="{ 'is-focus': it.focus === 'id' }"
            :x="it.x + it.w / 2" :y="it.y + it.h - 11" text-anchor="middle">{{ it.bottom }}</text>
    </template>

    <!-- 词表概率条 -->
    <template v-else-if="it.kind === 'bars'">
      <g v-for="(b, i) in it.bars" :key="i" :transform="`translate(${it.x} ${it.y + i * 30})`">
        <text class="bar__tok" x="0" y="18">{{ b.token === "\n" ? "⏎" : b.token }}</text>
        <rect class="bar__track" x="42" y="4" :width="it.w - 96" height="14" rx="7" />
        <rect class="bar__fill" x="42" y="4" :width="Math.max(4, (it.w - 96) * b.p)" height="14" rx="7" />
        <text class="bar__pct" :x="it.w" y="18" text-anchor="end">{{ (b.p * 100).toFixed(1) }}%</text>
        <text v-if="it.pick === i" class="bar__pick" :x="it.w + 16" y="18" text-anchor="start">← 选中的</text>
      </g>
      <text v-if="it.cap" class="bar__cap" :x="it.x" :y="it.y + it.h + 22" text-anchor="start">{{ it.cap }}</text>
    </template>

    <!-- 并行的那几路：同色小方块排一排 -->
    <template v-else-if="it.kind === 'lanes'">
      <rect v-for="n in it.n" :key="n" class="lane__blk"
            :x="it.x + (n - 1) * (it.bw + it.bg)" :y="it.y" :width="it.bw" :height="it.h" rx="4" />
      <text class="lane__cap" :x="it.x + it.n * (it.bw + it.bg) + 10" :y="it.y + it.h - 4">{{ it.label }}</text>
    </template>

    <!-- 纯文字 -->
    <text v-else-if="it.kind === 'note'" class="cv__note" :class="{ 'is-link': click }"
          :x="it.x" :y="it.y" :text-anchor="it.align === 'start' ? 'start' : 'middle'">{{ it.label }}</text>

    <!-- 方块 / 算子 / 端口 / ⋯ -->
    <template v-else>
      <rect :x="it.x" :y="it.y" :width="it.w" :height="it.h" :rx="(it.kind === 'op' ? 12 : 14)" />
      <rect v-if="it.kind === 'layer' && it.tone" class="cv__tone" :x="it.x + 5" :y="it.y + 9"
            width="4" :height="it.h - 18" rx="2" :style="{ fill: it.tone }" />
      <template v-if="it.kind === 'dots'">
        <text class="cv__dots" :x="it.x + it.w / 2" :y="it.y + (it.sub ? it.h / 2 - 2 : it.h / 2 + 5)">
          ⋯ {{ it.label }}</text>
        <text v-if="it.sub" class="cv__sub" :x="it.x + it.w / 2" :y="it.y + it.h / 2 + 16">{{ it.sub }}</text>
      </template>
      <template v-else>
        <text class="cv__label" :x="it.x + it.w / 2" :y="it.y + (it.sub ? it.h / 2 - 3 : it.h / 2 + 5)">{{ it.label }}</text>
        <text v-if="it.sub" class="cv__sub" :x="it.x + it.w / 2 + (it.kind === 'layer' ? 6 : 0)"
              :y="it.y + it.h / 2 + 16">{{ it.sub }}</text>
        <text v-if="click" class="cv__enter" :x="it.x + it.w - 14" :y="it.y + it.h / 2 + 6">›</text>
      </template>
    </template>
  </g>
</template>

<style scoped>
/* 方块：白底细描边，点亮换主色 */
.cv__item { cursor: pointer; }
.cv__item rect { fill: var(--st-surface); stroke: var(--st-line); stroke-width: 1.4; }
.cv__item:hover rect { stroke: var(--st-ink-muted); }
.cv__item.is-layer rect { fill: var(--st-surface-2); }
.cv__item.is-port rect { fill: var(--st-bg); stroke-dasharray: 5 4; }
.cv__item { cursor: default; }
/* 能点进去的：手型 + 悬停描边（点不动的东西不该装成能点） */
.cv__item[data-click] { cursor: pointer; }
.cv__item[data-click]:hover rect { stroke: var(--st-accent); }
.cv__item[data-click]:hover .cv__enter { fill: var(--st-accent); }
.cv__item.is-dots { cursor: default; }
.cv__item.is-dots rect { fill: none; stroke-dasharray: 5 4; }

/* ---- 单步卡：平面矩阵格、乘法连线、运算符、token/id 小方块、概率条 ---- */
.cv__item.is-grid, .cv__item.is-sym, .cv__item.is-bars, .cv__item.is-note,
.cv__item.is-chip, .cv__item.is-card { cursor: default; }
/* 没轮到的块调暗：现在在哪儿干活，一眼就看得出来 */
.cv__item[data-dim] { opacity: .26; transition: opacity .3s; }
.cv__item.is-card { pointer-events: none; }
.card__bg { fill: var(--st-surface); stroke: var(--st-line); stroke-width: 1.4;
            filter: drop-shadow(0 10px 24px color-mix(in srgb, #000 12%, transparent));
            animation: cardIn .3s cubic-bezier(.22,.61,.36,1) both; }
.card__title { fill: var(--st-ink); font: 700 15px var(--st-font); }
.card__rule { stroke: var(--st-line-soft); stroke-width: 1; }
@keyframes cardIn { from { opacity: 0; transform: translateY(10px) scale(.97); } to { opacity: 1; } }

.g__bg { fill: var(--st-surface-2); stroke: var(--st-line); stroke-width: 1.2; }
.g__cell { fill: var(--st-surface); stroke: var(--st-line-soft); stroke-width: 1; }
/* 这一刻真正在参与相乘的那一行 / 那一列 */
.g__cell.is-mark { fill: color-mix(in srgb, var(--st-accent) 26%, var(--st-surface)); stroke: var(--st-accent); }
.g__shape { fill: var(--st-ink); font: 700 15px var(--st-font-mono); }
.g__name { fill: var(--st-ink-soft); font: 600 11px var(--st-font-mono); }
.g__enter { fill: var(--st-ink-muted); font: 700 16px var(--st-font); }
/* 能继续点进去的格子：手型 + hover 描边 */
.cv__item.is-grid:has(.g__enter) { cursor: pointer; }
.cv__item.is-grid:hover .g__bg { stroke: var(--st-accent); }
.cv__item.is-grid:hover .g__enter { fill: var(--st-accent); }
/* 乘法连线：连起来的那几对就是要相乘的数 */
.cv__wire { stroke: var(--st-accent); stroke-width: 1.8; opacity: .85; }

.sym__dot { fill: var(--st-surface); stroke: var(--st-line); }
.sym__label { fill: var(--st-ink); font: 700 18px var(--st-font); }
.sym__sub { fill: var(--st-ink-muted); font: 500 12.5px var(--st-font); }

.chip__bg { fill: var(--st-surface-2); stroke: var(--st-line); stroke-width: 1.2; }
.chip__top { fill: var(--st-ink); font: 600 15px var(--st-font); }
.chip__bot { fill: var(--st-ink-muted); font: 500 12px var(--st-font-mono); }
.chip__bot.is-focus { fill: var(--st-accent-text); font-weight: 700; }

/* 并行的几路：同色，一排小方块 */
.lane__blk { fill: var(--st-accent); stroke: var(--st-accent); }
.lane__cap { fill: var(--st-accent-text); font: 600 13px var(--st-font); }

.bar__tok { fill: var(--st-ink); font: 600 14px var(--st-font); }
.bar__track { fill: color-mix(in srgb, var(--st-ink) 7%, transparent); }
.bar__fill { fill: var(--st-accent); }
.bar__pick { fill: var(--st-accent-text); font: 700 12px var(--st-font); }
.bar__pct { fill: var(--st-ink-soft); font: 600 12px var(--st-font-mono); }
.bar__cap { fill: var(--st-ink-muted); font: 500 12px var(--st-font); }
.cv__note { fill: var(--st-accent-text); font: 600 13px var(--st-font); }
/* 可点的说明（比如「完整回答见对话页」）：加下划线，别让人猜 */
.cv__note.is-link { text-decoration: underline; cursor: pointer; }
.cv__item[data-click]:hover .cv__note.is-link { fill: var(--st-accent); }
/* 点亮 = **实色底 + 白字**，按类型给色。
   这里踩过一次：浅色底 + 白字，字整个看不见（用户实测报的「文字看不清」）。
   浅底深字只留给非算子的方块（总览里的层块之类）。 */
/* 单步卡指向的那一块 = **当前就在这儿干活**：填色 + 粗边 + 粗体字 */
.cv__item[data-hot] rect { fill: color-mix(in srgb, var(--st-accent) 16%, var(--st-surface));
                           stroke: var(--st-accent); stroke-width: 2.6; }
.cv__item[data-hot] .cv__label { fill: var(--st-ink); font-weight: 800; }
/* 这一步还牵涉到的别的块（比如它的输入来自哪）：只描边、不填色 ——
   都填上色的话会看起来「好几个都是当前」，等于没说清聚焦在哪。 */
.cv__item[data-on]:not([data-hot]):not(.is-op) rect { stroke: var(--st-accent); stroke-width: 1.8; }
.cv__item[data-sel] rect { stroke: var(--st-accent-text); stroke-width: 2.2; }
.cv__item.is-op[data-on] rect { stroke-width: 2.2; }
.cv__item.is-op[data-on][data-type="layernorm"] rect { fill: var(--st-accent); stroke: var(--st-accent); }
.cv__item.is-op[data-on][data-type="attention"] rect { fill: var(--st-info); stroke: var(--st-info); }
.cv__item.is-op[data-on][data-type="linear"] rect { fill: var(--st-chart-2); stroke: var(--st-chart-2); }
.cv__item.is-op[data-on][data-type="ffn"] rect { fill: var(--st-warn); stroke: var(--st-warn); }
.cv__item.is-op[data-on][data-type="residual"] rect { fill: var(--st-accent-text); stroke: var(--st-accent-text); }
.cv__item.is-op[data-on] .cv__label { fill: #fff; }

.cv__label { fill: var(--st-ink); font: 600 15px var(--st-font); text-anchor: middle; }
.cv__dots { fill: var(--st-ink-muted); font: 500 13px var(--st-font); text-anchor: middle; }
/* 方块下面的小字：这一层的性质（线性注意力 / 全注意力）、⋯ 里都是些什么 */
.cv__sub { fill: var(--st-ink-muted); font: 500 12px var(--st-font); text-anchor: middle; }
/* 窄屏方块也小，字跟着收一档，免得「Softmax · 下一个词」顶出格子 */
@media (max-width: 720px) {
  .cv__label { font-size: 13px; }
  .cv__dots { font-size: 11px; }
}
.cv__enter { fill: var(--st-ink-muted); font: 700 20px var(--st-font); }
.cv__item:hover .cv__enter { fill: var(--st-accent); }


</style>
