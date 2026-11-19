<script setup>
// 底部时间轴：可拖的滑块 + 可点的步骤刻度条 + 上一步/下一步 + 跟随开关。
// 拖到哪、点到哪，3D 高亮和右侧面板都跟着走 —— 因为它们读的是同一个 viz.index。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import Icon from "../../components/Icon.vue";
import { goTo, stepBy, stop, togglePane, totalSteps, viz } from "../store.js";

const { t } = useI18n();

/** 手机上那条进度条的填充比例（桌面端不显示它） */
const fillPct = computed(() => (totalSteps.value > 1
  ? `${(Math.max(0, viz.index + 1) / totalSteps.value) * 100}%` : "0%"));

/** 拖动时先停播 —— 一边播一边被拖，两边都在改 index，位置会打架。 */
function onScrub(e) {
  stop();
  goTo(Number(e.target.value));
}

/** 刻度按阶段上色：开头/结尾是灰的，中间十二层是青的，当前那格泛白。 */
function kindOf(s) {
  if (!s) return "edge";
  return s.layer != null ? "layer" : "edge";
}
</script>

<template>
  <div class="tl" :data-idle="viz.index < 0 ? '1' : null" :data-folded="viz.panes.timeline ? null : '1'">
    <div class="tl__now">
      <button class="tl__nav" :disabled="viz.index < 0" :title="t('viz.prev')" :aria-label="t('viz.prev')"
              @click="stepBy(-1)"><Icon name="step-back" cls="st-icon st-icon--sm" /></button>
      <span class="tl__name">{{ viz.index >= 0 && viz.steps[viz.index] ? viz.steps[viz.index].name : t("viz.notstarted") }}</span>
      <button class="tl__nav" :disabled="viz.index >= totalSteps - 1" :title="t('viz.next')" :aria-label="t('viz.next')"
              @click="stepBy(1)"><Icon name="step-forward" cls="st-icon st-icon--sm" /></button>
    </div>

    <div v-if="viz.panes.timeline" class="tl__body">
      <!-- 刻度条：一格一步，点一下就跳到那一步。
           手机上 198 格排不下（每格 0 宽、光 1px 的间距就要 200px），那边改画一条进度条 ——
           进度照样看得见，跳步用下面的滑块。 -->
      <div class="tl__track" role="presentation">
        <div class="tl__fill" :style="{ width: fillPct }"></div>
        <button
          v-for="(s, i) in viz.steps" :key="s.id ?? i" class="tl__tick"
          :class="{ 'is-done': i < viz.index, 'is-now': i === viz.index }" :data-kind="kindOf(s)"
          :data-skip="s.same ? '1' : null"
          :title="`${i + 1}. ${s.name}`" :aria-label="`${i + 1}. ${s.name}`" @click="stop(); goTo(i)">
        </button>
      </div>

      <input
        class="tl__range" type="range" :min="-1" :max="Math.max(0, totalSteps - 1)" :value="viz.index"
        :aria-label="t('viz.panel.step')" :disabled="!totalSteps" @input="onScrub">
    </div>
    <span v-else class="tl__count">{{ totalSteps ? t("viz.steps", { n: totalSteps }) : "" }}</span>

    <!-- 时间轴也能收起来（收起后只剩「第几步 + 上一步 / 下一步」，画布多出一整条） -->
    <button class="tl__fold" :title="viz.panes.timeline ? t('viz.pane.hide') : t('viz.pane.timeline')"
            :aria-label="viz.panes.timeline ? t('viz.pane.hide') : t('viz.pane.timeline')"
            :aria-expanded="viz.panes.timeline ? 'true' : 'false'"
            @click="togglePane('timeline')">{{ viz.panes.timeline ? "⌄" : "⌃" }}</button>
  </div>
</template>

<style scoped>
.tl {
  flex: none; display: flex; align-items: center; gap: var(--st-s-4);
  padding: 10px var(--st-s-5) 12px; border-top: 1px solid var(--st-line);
  background: var(--st-surface);
}
.tl[data-idle] .tl__name { color: var(--st-ink-muted); }
/* 收起来：只剩「第几步 + 上一步/下一步」，条子变矮一条 */
.tl[data-folded] { padding: 5px var(--st-s-5) 6px; }
.tl[data-folded] .tl__now { flex: 1; min-width: 0; max-width: none; }

.tl__now { flex: none; display: flex; align-items: center; gap: 8px; min-width: 290px; max-width: 380px; }
.tl__name { flex: 1; min-width: 0; font-size: var(--st-fs-sm); font-weight: 600; color: var(--st-ink);
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tl__nav { flex: none; width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center;
           border-radius: 9px; border: 1px solid var(--st-line); background: var(--st-surface); color: var(--st-ink-soft); cursor: pointer;
           transition: border-color var(--st-dur), color var(--st-dur); }
.tl__nav:hover:not(:disabled) { border-color: var(--st-line); color: var(--st-ink); }
.tl__nav:disabled { opacity: .35; cursor: not-allowed; }

.tl__body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 5px; }
.tl__count { flex: 1; max-width: 160px; font-size: var(--st-fs-xs); color: var(--st-ink-muted);
             font-variant-numeric: tabular-nums; }
.tl__fold { flex: none; width: 26px; height: 26px; border: 1px solid var(--st-line); border-radius: 8px;
            background: var(--st-surface); color: var(--st-ink-muted); cursor: pointer; font-size: 12px; line-height: 1;
            transition: color var(--st-dur); }
.tl__fold:hover { color: var(--st-ink); background: var(--st-surface-2); }

/* 刻度条：每格等宽，用 flex 排，天然跟滑块对齐 */
.tl__track { position: relative; display: flex; gap: 1px; height: 10px; align-items: stretch; }
.tl__fill { display: none; }                    /* 桌面端用刻度，进度条只在手机上出现 */
.tl__tick { flex: 1 1 0; min-width: 0; padding: 0; border: 0; border-radius: 2px; cursor: pointer;
            background: var(--st-line); transition: background 120ms, box-shadow 120ms; }
.tl__tick[data-kind="layer"] { background: var(--st-line); }
.tl__tick:hover { background: var(--st-line); }
.tl__tick.is-done { background: color-mix(in srgb, var(--st-accent) 38%, transparent); }
/* 播放时会整段跳过的那些（同性质的层）：淡一档，让人知道这里不会逐帧走 */
.tl__tick[data-skip] { background: color-mix(in srgb, var(--st-line) 60%, transparent); }
.tl__tick[data-skip].is-done { background: color-mix(in srgb, var(--st-accent) 20%, transparent); }
.tl__tick.is-now { background: var(--st-accent); box-shadow: 0 0 8px color-mix(in srgb, var(--st-accent) 80%, transparent); }

.tl__range { width: 100%; height: 18px; margin: 0; accent-color: var(--st-accent); cursor: pointer; }
.tl__range:disabled { opacity: .4; cursor: not-allowed; }


@media (max-width: 1100px) {
  .tl__now { min-width: 0; max-width: 220px; }
}

/* 手机上：刻度换进度条（198 格排不下），步骤名窄一点 */
@media (max-width: 720px) {
  .tl { gap: var(--st-s-3); padding: 10px var(--st-s-3) 12px; }
  .tl__now { max-width: 58%; }
  .tl__tick { display: none; }
  .tl__track { height: 8px; border-radius: 4px; background: var(--st-line); overflow: hidden; }
  .tl__fill { display: block; height: 100%; border-radius: 4px; background: var(--st-accent);
              transition: width 160ms linear; }
}
</style>
