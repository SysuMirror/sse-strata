<script setup>
// 按需加载可视化页面时的占位。three.js 那坨有 200 多 KB(gzip)，
// 单独切了一个 chunk，首次进这一页要等它下来 —— 这里给个交代，别让人对着白屏。
import { useI18n } from "vue-i18n";
const { t } = useI18n();
</script>

<template>
  <div class="view vload">
    <span class="vload__ring" aria-hidden="true"></span>
    <p class="vload__text">{{ t("viz.loading") }}</p>
  </div>
</template>

<style scoped>
.vload { align-items: center; justify-content: center; gap: var(--st-s-4); background: #0a0a12; }
.vload__ring { width: 34px; height: 34px; border-radius: 50%; border: 2px solid #1e2a37; border-top-color: #00e5ff;
               animation: spin 900ms linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .vload__ring { animation-duration: 2.4s; } }
.vload__text { margin: 0; font: 500 var(--st-fs-sm) var(--st-font); color: #6f8ba3; }
</style>
