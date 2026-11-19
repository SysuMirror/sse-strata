<script setup>
// 左栏：模型列表。点一行 = 换模型（和顶栏下拉框写同一个字段）。
// 数字全部来自 ModelInfo，由 num.js 按配置算出来，不是写死的。
//
// 刻意做得很轻：只有选中那一行铺底色，没有卡片边框。规格压成一行四格对齐 ——
// 单位用「层 / 维 / 头 / 词表」而不是「层数 / 隐藏维度」这类名词，读起来才是「48 层」
// 而不是「48 层数」。来源（本机实测 / 示例数据）标在行首，别让人把示例数字当实测值。
import { useI18n } from "vue-i18n";
import { params } from "../num.js";
import { selectModel, viz } from "../store.js";

const { t } = useI18n();

const spec = (m) => [
  `${m.layers}${t("viz.unit.layers")}`,
  `${m.hiddenSize}${t("viz.unit.hidden")}`,
  `${m.heads}${t("viz.unit.heads")}`,
  `${(m.vocabSize / 1000).toFixed(0)}k${t("viz.unit.vocab")}`,
];
</script>

<template>
  <aside class="models">
    <h2 class="models__title">{{ t("viz.models") }}</h2>

    <button
      v-for="m in viz.models" :key="m.id" class="mrow" :aria-pressed="m.id === viz.modelId"
      @click="selectModel(m.id)">
      <span class="mrow__head">
        <span class="mrow__name">{{ m.name }}</span>
        <span class="mrow__dot" aria-hidden="true"></span>
      </span>
      <span class="mrow__sum">
        <b>{{ params(m.paramCount) }}</b>
        <em>{{ t("viz.params") }}</em>
        <span class="mrow__src" :data-src="m.source">{{ t(`viz.source.${m.source}`) }}</span>
      </span>
      <span class="mrow__spec">
        <span v-for="s in spec(m)" :key="s">{{ s }}</span>
      </span>
      <span v-if="m.note" class="mrow__note">{{ m.note }}</span>
    </button>

    <p v-if="!viz.models.length && !viz.loading" class="models__empty">{{ t("viz.models.empty") }}</p>
  </aside>
</template>

<style scoped>
.models { display: flex; flex-direction: column; gap: 2px; padding: var(--st-s-4) var(--st-s-3) var(--st-s-4) var(--st-s-4); }
.models__title { margin: 0 0 8px 10px; font-size: var(--st-fs-xs); font-weight: 700; letter-spacing: .06em;
                 text-transform: uppercase; color: var(--st-ink-muted); }
.models__empty { margin: 0 10px; font-size: var(--st-fs-xs); color: var(--st-ink-muted); }

.mrow { position: relative; display: flex; flex-direction: column; gap: 5px; width: 100%; text-align: left;
        cursor: pointer; padding: 10px 11px 11px; border: 0; border-radius: var(--st-r-md);
        background: transparent; color: var(--st-ink); font-family: var(--st-font);
        transition: background var(--st-dur); }
.mrow:hover { background: var(--st-surface-2); }
.mrow[aria-pressed="true"] { background: color-mix(in srgb, var(--st-accent) 9%, transparent);
                             box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--st-accent) 34%, transparent); }
/* 选中那行左边一道竖杠：比整块变色更安静，也更好找 */
.mrow[aria-pressed="true"]::before { content: ""; position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px;
                                     border-radius: 0 3px 3px 0; background: var(--st-accent); }

.mrow__head { display: flex; align-items: center; gap: 8px; }
.mrow__name { flex: 1; min-width: 0; font-weight: 700; font-size: var(--st-fs-sm); line-height: 1.35;
              overflow-wrap: anywhere; }
.mrow__dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--st-line);
             transition: background var(--st-dur), box-shadow var(--st-dur); }
.mrow[aria-pressed="true"] .mrow__dot { background: var(--st-accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--st-accent) 18%, transparent); }

.mrow__sum { display: flex; align-items: baseline; gap: 5px; }
.mrow__sum b { font-size: var(--st-fs-base); font-weight: 800; color: var(--st-ink);
               font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.mrow__sum em { font-style: normal; font-size: var(--st-fs-xs); color: var(--st-ink-muted); }
.mrow__src { margin-left: auto; padding: 2px 7px; border-radius: var(--st-r-pill); font-size: 10px; font-weight: 600;
             border: 1px solid var(--st-line); color: var(--st-ink-muted); white-space: nowrap; }
.mrow__src[data-src="local"] { border-color: color-mix(in srgb, var(--st-accent) 45%, transparent);
                               color: var(--st-accent-text); background: color-mix(in srgb, var(--st-accent) 8%, transparent); }
.mrow__src[data-src="api"] { border-color: color-mix(in srgb, var(--st-accent) 45%, transparent);
                             color: var(--st-accent); background: color-mix(in srgb, var(--st-accent) 8%, transparent); }

/* 2×2 对齐：数字竖着看是一列一列的，比挤在一行好扫；四列会把「248k 词表」挤断行 */
.mrow__spec { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 8px;
              font-size: var(--st-fs-xs); color: var(--st-ink-soft); font-variant-numeric: tabular-nums;
              white-space: nowrap; }
.mrow__note { font-size: var(--st-fs-xs); line-height: 1.45; color: var(--st-ink-muted);
              padding-top: 5px; border-top: 1px solid var(--st-line-soft); }
</style>
