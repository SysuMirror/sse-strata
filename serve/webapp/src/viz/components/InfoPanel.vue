<script setup>
// 右栏：当前步骤的账（形状 / 参数量 / 计算量 / 用到的权重），
// 以及被点开（或悬停）的那个节点的详情。两块内容互不遮挡，可以同时看。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { flops, params } from "../num.js";
import { NODE_STYLE } from "../style.js";
import { activeWeights, currentModel, currentStep, inspectedNode, layerCursor, selectNode, totalSteps, viz } from "../store.js";

const { t } = useI18n();

const step = computed(() => currentStep.value);
const node = computed(() => inspectedNode.value);
const nodeStyle = computed(() => (node.value ? NODE_STYLE[node.value.type] : null));

/** 这一步「主要在算什么」——取最后一个参与节点，它的类型决定标题旁边那个色块。
 *  （activeNodeIds 是按数据流排的，最后一个才是这一刻落到的那一环。） */
const stepStyle = computed(() => {
  const s = step.value;
  if (!s || !s.activeNodeIds || !s.activeNodeIds.length) return null;
  const id = s.activeNodeIds[s.activeNodeIds.length - 1];
  const n = viz.graph.nodes.find((x) => x.id === id);
  return n ? NODE_STYLE[n.type] : null;
});

/** 进度条：走到第几步了 */
const stepPct = computed(() => (totalSteps.value > 1 && viz.index >= 0
  ? ((viz.index + 1) / totalSteps.value) * 100 : 0));

// 左栏在 ≤1000px 被收起来了（手机上放不下）。规格是必要信息，不能就这么没了 ——
// 窄屏时在这一栏顶上补一行，桌面端不显示（那边左栏已经写得很清楚）。
const spec = computed(() => {
  const m = currentModel.value;
  if (!m) return null;
  return [params(m.paramCount), `${m.layers} ${t("viz.layers")}`, `${m.hiddenSize} ${t("viz.hidden")}`,
          `${m.heads} ${t("viz.heads")}`, `${(m.vocabSize / 1000).toFixed(0)}k ${t("viz.vocab")}`].join(" · ");
});
</script>

<template>
  <aside class="panel">
    <!-- ---------------------------------------------------------- 当前步骤 -->
    <section class="panel__block">
      <header class="panel__head">
        <span class="panel__title">{{ t("viz.panel.step") }}</span>
        <span v-if="viz.index >= 0" class="panel__idx">
          {{ viz.index + 1 }} / {{ totalSteps }}
          <em v-if="layerCursor">{{ t("viz.layer.of", { n: layerCursor.layer, total: layerCursor.total }) }}</em>
        </span>
      </header>

      <div v-if="viz.index >= 0" class="panel__bar" aria-hidden="true">
        <span :style="{ width: `${stepPct}%` }"></span>
      </div>

      <template v-if="step">
        <h3 class="panel__name">
          <span v-if="stepStyle" class="panel__swatch" :style="{ background: stepStyle.color }"></span>{{ step.name }}
        </h3>
        <p class="panel__desc">{{ step.description }}</p>

        <dl class="kv">
          <div class="kv__row">
            <dt>{{ t("viz.panel.shape") }}</dt>
            <dd class="mono">{{ step.tensorShape }}</dd>
          </div>
          <div class="kv__row">
            <dt>{{ t("viz.panel.params") }}</dt>
            <dd>{{ params(step.paramCount) }}</dd>
          </div>
          <div class="kv__row">
            <dt>{{ t("viz.panel.flops") }}</dt>
            <dd>{{ flops(step.flops) }}</dd>
          </div>
        </dl>

        <div v-if="step.detail && step.detail.length" class="panel__detail">
          <span v-for="(d, i) in step.detail" :key="i" class="chip mono">{{ d }}</span>
        </div>

        <div class="panel__sub">
          <span class="panel__subtitle">{{ t("viz.panel.highlight") }}</span>
          <div v-if="activeWeights.length" class="panel__weights">
            <span v-for="w in activeWeights" :key="w" class="wchip mono">{{ w }}</span>
          </div>
          <p v-else class="panel__none">{{ t("viz.panel.no.weights") }}</p>
        </div>
      </template>

      <p v-else class="panel__none">{{ t("viz.panel.idle") }}</p>
    </section>

    <!-- ---------------------------------------------------------- 节点详情 -->
    <section class="panel__block" :data-empty="node ? null : '1'">
      <header class="panel__head">
        <span class="panel__title">{{ t("viz.panel.node") }}</span>
        <button v-if="node" class="panel__close" :aria-label="t('common.remove')" @click="selectNode(null)">×</button>
      </header>

      <template v-if="node">
        <h3 class="panel__name">
          <span class="panel__swatch" :style="{ background: nodeStyle.color }"></span>{{ node.label }}
        </h3>
        <p class="panel__desc">{{ t(`viz.type.${node.type}`) }}<template v-if="node.layer != null">
          · {{ t("viz.layer.n", { n: node.layer + 1 }) }}</template></p>

        <dl class="kv">
          <div v-if="node.inputShape" class="kv__row">
            <dt>{{ t("viz.panel.in") }}</dt><dd class="mono">{{ node.inputShape }}</dd>
          </div>
          <div v-if="node.outputShape" class="kv__row">
            <dt>{{ t("viz.panel.out") }}</dt><dd class="mono">{{ node.outputShape }}</dd>
          </div>
          <div class="kv__row">
            <dt>{{ t("viz.panel.params") }}</dt><dd>{{ params(node.paramCount) }}</dd>
          </div>
        </dl>

        <div v-if="node.weights && node.weights.length" class="panel__sub">
          <span class="panel__subtitle">{{ t("viz.panel.matrices") }}</span>
          <table class="wtable">
            <tbody>
              <tr v-for="w in node.weights" :key="w.name">
                <td class="mono wtable__name">{{ w.name }}</td>
                <td class="mono wtable__shape">{{ w.shape }}</td>
                <td class="wtable__n">{{ params(w.paramCount) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="panel__none">{{ t("viz.panel.no.params") }}</p>
      </template>

      <p v-else class="panel__none">{{ t("viz.panel.click") }}</p>
    </section>

    <!-- 窄屏才出现的一行模型规格（桌面端左栏写得很清楚，这里不重复） -->
    <section v-if="spec" class="panel__block panel__block--spec">
      <span class="panel__title">{{ t("viz.model") }}</span>
      <b class="spec__name">{{ currentModel.name }}</b>
      <span class="spec__nums">{{ spec }}</span>
    </section>
  </aside>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; gap: 12px; padding: var(--st-s-4); }
.panel__block { padding: 13px 14px; border-radius: var(--st-r-md); border: 1px solid var(--st-line);
                background: var(--st-surface-2); }
.panel__head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.panel__title { flex: 1; font-size: var(--st-fs-xs); font-weight: 700; letter-spacing: .06em;
                text-transform: uppercase; color: var(--st-ink-muted); }
.panel__idx { font-size: var(--st-fs-xs); color: var(--st-ink-soft); font-variant-numeric: tabular-nums; }
.panel__idx em { font-style: normal; margin-left: 8px; color: var(--st-accent); }
.panel__close { width: 22px; height: 22px; border: 0; border-radius: 6px; cursor: pointer; line-height: 1;
                background: transparent; color: var(--st-ink-muted); font-size: 18px; }
.panel__close:hover { background: var(--st-surface-2); color: var(--st-ink); }

.panel__name { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; font-size: var(--st-fs-base);
               font-weight: 700; color: var(--st-ink); line-height: 1.3; }
.panel__swatch { flex: none; width: 10px; height: 10px; border-radius: 3px; }
.panel__desc { margin: 0 0 10px; font-size: var(--st-fs-sm); line-height: 1.6; color: var(--st-ink-soft); }
.panel__none { margin: 0; font-size: var(--st-fs-sm); color: var(--st-ink-muted); line-height: 1.6; }
.panel__sub { margin-top: 12px; }
.panel__subtitle { display: block; margin-bottom: 7px; font-size: var(--st-fs-xs); font-weight: 600; color: var(--st-ink-muted); }
.panel__weights { display: flex; flex-wrap: wrap; gap: 6px; }
.panel__detail { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }

/* 进度条：这一步在整个演示里的位置，一眼看得出来还剩多少 */
.panel__bar { height: 3px; margin: -4px 0 10px; border-radius: 2px; overflow: hidden;
              background: color-mix(in srgb, var(--st-ink) 8%, transparent); }
.panel__bar span { display: block; height: 100%; border-radius: 2px; background: var(--st-accent);
                   transition: width 160ms linear; }

.kv { display: flex; flex-direction: column; gap: 0; margin: 0; }
.kv__row { display: flex; align-items: baseline; justify-content: space-between; gap: 10px;
           padding: 4px 0; border-top: 1px solid var(--st-line-soft); }
.kv__row:first-child { border-top: 0; padding-top: 0; }
.kv__row dt { flex: none; font-size: var(--st-fs-xs); color: var(--st-ink-muted); }
.kv__row dd { margin: 0; font-size: var(--st-fs-sm); font-weight: 600; color: var(--st-ink-soft); text-align: right;
              overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }

/* 模型规格那一块只在窄屏（左栏收起来时）出现 */
.panel__block--spec { display: none; flex-direction: column; gap: 4px; }
.spec__name { font-size: var(--st-fs-sm); font-weight: 700; color: var(--st-ink); overflow-wrap: anywhere; }
.spec__nums { font-size: var(--st-fs-xs); color: var(--st-ink-muted); line-height: 1.5; font-variant-numeric: tabular-nums; }
@media (max-width: 1000px) { .panel__block--spec { display: flex; } }

.mono { font-family: var(--st-font-mono); font-size: 11.5px; letter-spacing: -.01em; }
.chip { padding: 3px 8px; border-radius: 6px; background: color-mix(in srgb, var(--st-accent) 8%, transparent);
        border: 1px solid color-mix(in srgb, var(--st-accent) 22%, transparent); color: var(--st-accent-text); }
.wchip { padding: 3px 8px; border-radius: 6px; background: color-mix(in srgb, var(--st-accent-text) 10%, transparent);
         border: 1px solid color-mix(in srgb, var(--st-accent-text) 30%, transparent); color: var(--st-accent-text); }

.wtable { width: 100%; border-collapse: collapse; }
.wtable td { padding: 4px 0; vertical-align: top; }
.wtable__name { color: var(--st-ink-soft); overflow-wrap: anywhere; }
.wtable__shape { color: var(--st-accent-text); white-space: nowrap; padding-left: 8px !important; }
.wtable__n { text-align: right; color: var(--st-ink-muted); font-variant-numeric: tabular-nums; white-space: nowrap;
             padding-left: 8px !important; font-size: var(--st-fs-xs); }

</style>
