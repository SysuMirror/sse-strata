<script setup>
// 多序列折线图（SVG，无第三方图表库 —— 这一点点几何自己画更可控，也不用给页面再加 100KB）。
//
// 几条硬规矩（都是踩过的坑，不是审美）：
//  · **一根 y 轴**。两套量纲（tok/s 和「在途个数」）绝不共用一张图 —— 要放两个图，不是画双轴。
//    双轴的两个刻度可以随便调，看上去的「相关性」是画出来的、不是数据里的。
//  · 线宽 2px、圆角连接；网格与坐标轴压到最淡，数据才是主角。
//  · 数值文字用墨色令牌，**不用序列色** —— 颜色只用来标记「哪条线是谁」，不承担可读性。
//  · 缺测（通道离线那一刻没有 tok/s）断开，不要用 0 顶替：0 是个真实的值，会撒谎。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { xTicks } from "../chart-axis.js";

const props = defineProps({
  /** [{ key, label, color, values: (number|null)[] }] —— 颜色由调用方给（来自 --st-chart-*） */
  series: { type: Array, default: () => [] },
  /** 每个点的时间戳（毫秒），用于 x 轴刻度与 tooltip */
  times: { type: Array, default: () => [] },
  unit: { type: String, default: "" },
  height: { type: Number, default: 132 },
  /** 固定 y 轴上限（给了就不按数据自适应，比如百分比类） */
  max: { type: Number, default: 0 },
  format: { type: Function, default: null },
});

const host = ref(null);
const width = ref(640);
let ro = null;

onMounted(() => {
  ro = new ResizeObserver(() => { width.value = Math.max(160, host.value ? host.value.clientWidth : 640); });
  ro.observe(host.value);
  width.value = Math.max(160, host.value.clientWidth);
});
onBeforeUnmount(() => ro && ro.disconnect());

// 内边距：左边留给 y 轴刻度，下边留给时间，右边留一点点给最后一个点
const PAD = { l: 46, r: 10, t: 10, b: 20 };

const fmtV = (v) => (props.format ? props.format(v) : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 }));

/** y 轴刻度：取一个「整齐」的上限，别出现 37.4 这种刻度。
 *  档位给密一点 —— 只用 1/2/5/10 的话，最大值 51 会直接跳到 100，半张图白给。 */
const STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
function niceTop(raw) {
  if (!(raw > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (STEPS.find((s) => n <= s + 1e-9) || 10) * mag;
}

const geom = computed(() => {
  const w = width.value;
  const h = props.height;
  const iw = Math.max(10, w - PAD.l - PAD.r);
  const ih = Math.max(10, h - PAD.t - PAD.b);
  const n = Math.max(props.times.length, ...props.series.map((s) => s.values.length), 0);
  const flat = props.series.flatMap((s) => s.values.filter((v) => v != null));
  const top = props.max > 0 ? props.max : niceTop(Math.max(0, ...flat) * 1.15 || 1);
  const x = (i) => PAD.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => PAD.t + ih - (Math.max(0, v) / top) * ih;

  const lines = props.series.map((s) => {
    let d = "";
    let pen = false;
    s.values.forEach((v, i) => {
      if (v == null) { pen = false; return; }              // 缺测：抬笔，别连过去
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return { ...s, d, last: [...s.values].reverse().find((v) => v != null) };
  });
  return { w, h, iw, ih, n, top, x, y, lines };
});

// 网格线与刻度：4 档
const ticks = computed(() => {
  const { top, ih } = geom.value;
  return [0, 0.25, 0.5, 0.75, 1].map((f) => ({ v: top * f, y: PAD.t + ih - f * ih }));
});

// 24 小时制且不带 AM/PM：轴上的三个时间戳要短，不然右边的刻度会顶到边
const clock = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
/** x 轴的时间戳：两端 + 中间，但**装不下就不硬塞**（选择逻辑在 src/chart-axis.js，可脱离浏览器测）。
 *  原来这里写死三个下标，采样点只有 2 个时中间那个算出 i=0 → 同一个 x 画两遍 → 时间戳叠成乱码。 */
const xLabels = computed(() => xTicks(props.times.length, geom.value.iw).map((t) => ({
  i: t.i, x: geom.value.x(t.i), text: clock(props.times[t.i]), anchor: t.anchor,
})));

// ---------------------------------------------------------------- hover 十字线
const hover = ref(null);

function onMove(e) {
  const r = host.value.getBoundingClientRect();
  const px = e.clientX - r.left;
  const { n, iw } = geom.value;
  if (n < 2) return;
  const i = Math.round(((px - PAD.l) / iw) * (n - 1));
  hover.value = Math.max(0, Math.min(n - 1, i));
}
const leave = () => { hover.value = null; };

const tip = computed(() => {
  const i = hover.value;
  if (i == null) return null;
  const { x, w } = geom.value;
  const at = x(i);
  const rows = props.series
    .map((s) => ({ label: s.label, color: s.color, v: s.values[i] }))
    .filter((r) => r.v != null);
  if (!rows.length) return null;
  return { x: at, rows, time: props.times[i] == null ? "" : clock(props.times[i]),
           flip: at > w - 130 };          // 靠右就把 tooltip 翻到左边，别被裁掉
});
</script>

<template>
  <div ref="host" class="lc" :style="{ height: `${height}px` }" @mousemove="onMove" @mouseleave="leave">
    <svg :width="geom.w" :height="geom.h" :viewBox="`0 0 ${geom.w} ${geom.h}`" role="img">
      <!-- 网格 + y 轴刻度 -->
      <g class="lc__grid">
        <template v-for="(t, i) in ticks" :key="i">
          <line :x1="PAD.l" :x2="geom.w - PAD.r" :y1="t.y" :y2="t.y" shape-rendering="crispEdges" />
          <text class="lc__ytick" :x="PAD.l - 8" :y="t.y + 3.5" text-anchor="end">{{ fmtV(t.v) }}</text>
        </template>
      </g>

      <!-- 数据线 -->
      <path v-for="s in geom.lines" :key="s.key" class="lc__line" :d="s.d" :stroke="s.color" />

      <!-- hover：竖直十字线 + 每个序列一个点 -->
      <g v-if="tip" class="lc__hover">
        <line :x1="tip.x" :x2="tip.x" :y1="PAD.t" :y2="geom.h - PAD.b" shape-rendering="crispEdges" />
        <circle v-for="r in tip.rows" :key="r.label" :cx="tip.x" :cy="geom.y(r.v)" r="3.5"
                :fill="r.color" class="lc__dot" />
      </g>

      <!-- x 轴时间 -->
      <text v-for="l in xLabels" :key="l.i" class="lc__xtick" :x="l.x" :y="geom.h - 5"
            :text-anchor="l.anchor">{{ l.text }}</text>
    </svg>

    <div v-if="tip" class="lc__tip" :style="{ left: `${tip.x}px`, transform: tip.flip ? 'translateX(-100%) translateX(-10px)' : 'translateX(10px)' }">
      <div class="lc__tip-time">{{ tip.time }}</div>
      <div v-for="r in tip.rows" :key="r.label" class="lc__tip-row">
        <span class="lc__tip-key"><i :style="{ background: r.color }"></i>{{ r.label }}</span>
        <b>{{ fmtV(r.v) }}<em v-if="unit">{{ unit }}</em></b>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lc { position: relative; width: 100%; }
.lc svg { display: block; }

/* 网格与刻度一律用最淡的线色和 muted 墨色 —— 它们存在的意义是「能对齐读数」，不是被看见 */
.lc__grid line { stroke: var(--st-line-soft); stroke-width: 1; }
.lc__ytick, .lc__xtick { fill: var(--st-ink-muted); font: 400 11px var(--st-font); font-variant-numeric: tabular-nums; }

.lc__line { fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.lc__hover line { stroke: var(--st-line); stroke-width: 1; }
.lc__dot { stroke: var(--st-surface); stroke-width: 2; }   /* 点压在线上时留一圈底色，读数更清楚 */

.lc__tip { position: absolute; top: 6px; z-index: 3; pointer-events: none; min-width: 108px;
           padding: 7px 9px; border-radius: var(--st-r-sm); background: var(--st-surface);
           border: 1px solid var(--st-line); box-shadow: var(--st-sh-pop); }
.lc__tip-time { font: 500 11px var(--st-font); color: var(--st-ink-muted); font-variant-numeric: tabular-nums;
                margin-bottom: 4px; }
.lc__tip-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.lc__tip-key { display: inline-flex; align-items: center; gap: 6px; font: 400 12px var(--st-font);
               color: var(--st-ink-soft); }
.lc__tip-key i { width: 8px; height: 8px; border-radius: 2px; flex: none; }
.lc__tip-row b { font: 700 12px var(--st-font); color: var(--st-ink); font-variant-numeric: tabular-nums; }
.lc__tip-row b em { font-style: normal; font-weight: 400; color: var(--st-ink-muted); margin-left: 2px; }
</style>
