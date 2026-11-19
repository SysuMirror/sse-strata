<script setup>
// 指标卡里的小趋势图：与原 app.js 的 spark() 等价（0..100 宽、0..30 高，最大值归一化）。
import { computed } from "vue";

const props = defineProps({
  values: { type: Array, default: () => [] },
  max: { type: Number, default: 0 },
  tone: { type: String, default: "" },
  cls: { type: String, default: "" },
});

const lines = computed(() => {
  const v = (props.values || []).map((x) => (x == null ? 0 : x));
  if (v.length < 2) return null;
  const top = Math.max(props.max || 0, ...v, 1e-9);
  const pts = v.map((x, i) => [(i / (v.length - 1)) * 100, 30 - (x / top) * 26]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join("");
  return { line, area: `${line}L100,32L0,32Z` };
});
</script>

<template>
  <svg :class="['st-metric__spark', cls]" viewBox="0 0 100 32" preserveAspectRatio="none" :data-tone="tone || null">
    <path class="area" fill="currentColor" opacity=".12" :d="lines ? lines.area : ''" />
    <path class="line" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"
          stroke-linecap="round" vector-effect="non-scaling-stroke" :d="lines ? lines.line : ''" />
  </svg>
</template>
