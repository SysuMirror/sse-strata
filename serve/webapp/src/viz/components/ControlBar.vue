<script setup>
// 顶栏：选模型、写一句话、开始/暂停/重置、调速。
// 模型在下拉框和左侧卡片里都能选 —— 两处写的是同一个 store 字段，没有第二份状态。
//
// **演示模式下这条栏变成画布顶上的一条胶囊**：选模型、输入框、数据来源、步骤数整块退场
// （它们只在「准备演示」阶段有用，演起来了再摆着就是干扰），只留播放、调速和一个退出。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import Icon from "../../components/Icon.vue";
import { SOURCE } from "../types.js";
import { SPEEDS, dataSource, reset, selectModel, setSpeed, startDemo, togglePlay, totalSteps, viz } from "../store.js";

const { t } = useI18n();

const canStart = computed(() => !!viz.modelId && viz.text.trim().length > 0);

// 播放状态。**只用一个按钮**表达四种状态是这里的重点：
// 之前是「主动作 + 暂停/继续」两个按钮各算各的文案，结果播放中主动作还挂着「开始演示」，
// 跟旁边的「暂停」自相矛盾（用户实测报的 bug）。一个按钮、四段文案，不可能再对不上。
const playState = computed(() => {
  if (!viz.steps.length || viz.index < 0) return "idle";
  if (viz.playing) return "playing";
  if (viz.index >= viz.steps.length - 1) return "done";
  return "paused";
});
const playLabel = computed(() => ({
  idle: t("viz.start"), playing: t("viz.pause"), paused: t("viz.resume"), done: t("viz.restart"),
}[playState.value]));

// 第二个按钮在两种模式下是两件事：平时是「重置」，演示中是「退出演示」（都走 reset，
// 区别只在文案 —— 演起来了还想按「重置」的人，其实想的就是退出）。
const secondLabel = computed(() => (viz.focus ? t("viz.focus.exit") : t("viz.reset")));

function onPlay() {
  if (!canStart.value) return;
  // 播放中 → 暂停；暂停中 → 接着放；没开始/已放完 → 从头开始
  if (playState.value === "playing" || playState.value === "paused") togglePlay();
  else startDemo(viz.text);
}
</script>

<template>
  <div class="bar" :data-focus="viz.focus ? '1' : null">
    <!-- 准备阶段的两项：演示中整块退场（宽度收到 0 + 淡出） -->
    <div class="bar__setup">
      <label class="bar__field bar__field--model">
        <span class="bar__label">{{ t("viz.model") }}</span>
        <span class="bar__select">
          <Icon name="layers" cls="st-icon st-icon--sm" />
          <select :value="viz.modelId" :aria-label="t('viz.model')" @change="selectModel($event.target.value)">
            <option v-for="m in viz.models" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
        </span>
      </label>

      <label class="bar__field bar__field--text">
        <span class="bar__label">{{ t("viz.sentence") }}</span>
        <input
          v-model="viz.text" class="bar__input" type="text" :aria-label="t('viz.sentence')"
          :placeholder="t('viz.sentence.placeholder')" @keydown.enter="onPlay">
      </label>
    </div>

    <div class="bar__group" role="group" :aria-label="t('viz.controls')">
      <button class="st-btn st-btn--primary bar__go bar__play" :disabled="!canStart" :data-state="playState"
              @click="onPlay">
        <Icon :name="playState === 'playing' ? 'pause' : 'play'" cls="st-icon st-icon--sm" />{{ playLabel }}
      </button>
      <button class="st-btn st-btn--secondary bar__reset" @click="reset">
        <Icon :name="viz.focus ? 'logout' : 'refresh'" cls="st-icon st-icon--sm" />{{ secondLabel }}
      </button>
    </div>

    <div class="bar__group" role="group" :aria-label="t('viz.speed')">
      <span class="bar__label bar__label--inline"><Icon name="gauge" cls="st-icon st-icon--sm" />{{ t("viz.speed") }}</span>
      <div class="seg">
        <button v-for="s in SPEEDS" :key="s" class="seg__btn" :aria-pressed="viz.speed === s"
                @click="setSpeed(s)">{{ s }}×</button>
      </div>
    </div>

    <span class="bar__spacer"></span>

    <span class="bar__source" :data-src="dataSource" :title="t('viz.source.hint')">
      <Icon :name="dataSource === SOURCE.API ? 'link' : dataSource === SOURCE.LOCAL ? 'check' : 'tool'" cls="st-icon st-icon--sm" />
      {{ t(`viz.source.${dataSource}`) }}
    </span>
    <span v-if="totalSteps" class="bar__count">{{ t("viz.steps", { n: totalSteps }) }}</span>
  </div>
</template>

<style scoped>
.bar {
  flex: none; display: flex; align-items: flex-end; gap: var(--st-s-4); flex-wrap: wrap;
  padding: 12px var(--st-s-5); border-bottom: 1px solid var(--st-line);
  background: var(--st-surface);
  transition: padding .3s, gap .3s, border-color .3s;
}
.bar__setup { flex: 1 1 auto; min-width: 0; display: flex; align-items: flex-end; gap: var(--st-s-4);
              transition: flex-basis .42s cubic-bezier(.22, .61, .36, 1), opacity .22s; }
.bar__field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.bar__field--model { flex: 0 1 250px; }
.bar__field--text { flex: 1 1 280px; min-width: 200px; }
.bar__label { display: flex; align-items: center; gap: 6px; font-size: var(--st-fs-xs); font-weight: 600;
              letter-spacing: .04em; text-transform: uppercase; color: var(--st-ink-muted); }
.bar__label--inline { text-transform: none; letter-spacing: 0; font-size: var(--st-fs-sm); align-self: center; }

.bar__select { position: relative; display: flex; align-items: center; gap: 8px; height: 42px; padding: 0 12px;
               border: 1px solid var(--st-line); border-radius: var(--st-r-md); background: var(--st-surface); color: var(--st-ink); }
.bar__select select { flex: 1; min-width: 0; appearance: none; border: 0; background: transparent;
                      color: inherit; font: 500 var(--st-fs-md) var(--st-font); cursor: pointer; }
.bar__select select:focus-visible { outline: 2px solid var(--st-accent); outline-offset: 2px; border-radius: 4px; }
.bar__select option { background: var(--st-surface); color: var(--st-ink); }

.bar__input { height: 42px; padding: 0 14px; border-radius: var(--st-r-md); border: 1px solid var(--st-line);
              background: var(--st-surface); color: var(--st-ink); font: 400 var(--st-fs-md) var(--st-font); }
.bar__input::placeholder { color: var(--st-ink-muted); }
.bar__input:focus { outline: 0; border-color: var(--st-accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--st-accent) 18%, transparent); }

.bar__group { display: flex; align-items: center; gap: var(--st-s-2); }
/* 按钮上的字不许折行：手机上这条栏被压窄时，「退出演示」会被挤成竖排四行 */
.bar__go, .bar__reset { height: 42px; white-space: nowrap; }
.bar__spacer { flex: 1 1 0; min-width: 0; }

.bar__source { display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 11px; align-self: center;
               border-radius: var(--st-r-pill); font-size: var(--st-fs-xs); font-weight: 600; white-space: nowrap;
               transition: opacity .22s;
               border: 1px solid color-mix(in srgb, var(--st-accent-text) 50%, transparent); color: var(--st-accent-text); background: color-mix(in srgb, var(--st-accent-text) 10%, transparent); }
/* 三种来源三种颜色：绿=真实接口、青=本机实测、紫=示例数据。
   宁可标签显眼，也不能让人把示例数字当成这台机器的实测值。 */
.bar__source[data-src="api"] { border-color: color-mix(in srgb, var(--st-accent) 45%, transparent); color: var(--st-accent); background: color-mix(in srgb, var(--st-accent) 8%, transparent); }
.bar__source[data-src="local"] { border-color: color-mix(in srgb, var(--st-accent) 50%, transparent); color: var(--st-accent-text); background: color-mix(in srgb, var(--st-accent) 10%, transparent); }
.bar__count { align-self: center; font-size: var(--st-fs-xs); color: var(--st-ink-muted); font-variant-numeric: tabular-nums;
              transition: opacity .22s; }

/* 三档速度的分段控件（组件库里有 .seg，但那是浅色主题的，这里自己铺一层深色的） */
.seg { display: inline-flex; padding: 3px; gap: 3px; border-radius: var(--st-r-md);
       border: 1px solid var(--st-line); background: var(--st-surface); }
.seg__btn { min-width: 46px; height: 32px; padding: 0 10px; border: 0; border-radius: 10px; cursor: pointer;
            background: transparent; color: var(--st-ink-muted); font: 600 var(--st-fs-sm) var(--st-font);
            transition: background var(--st-dur), color var(--st-dur); }
.seg__btn:hover { color: var(--st-ink); }
.seg__btn[aria-pressed="true"] { background: color-mix(in srgb, var(--st-accent) 14%, transparent); color: var(--st-accent-text);
                                 box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--st-accent) 45%, transparent); }

/* ---- 演示模式：这条栏只该剩「播放 / 速度 / 退出」 ---- */
.bar[data-focus] { align-items: center; gap: var(--st-s-3); flex-wrap: nowrap; }
/* 宽度收 0 之外**高度也得收 0**：手机上这两个输入框是竖排的（≈150px 高），
   不收的话这条悬浮胶囊会被它们撑成一根大柱子，盖掉半个画布。 */
.bar[data-focus] .bar__setup { flex: 0 0 0; max-height: 0; overflow: hidden; opacity: 0; pointer-events: none; }
.bar[data-focus] .bar__spacer { display: none; }
.bar[data-focus] .bar__source, .bar[data-focus] .bar__count { display: none; }
.bar[data-focus] .bar__go { height: 38px; }
.bar[data-focus] .seg__btn { min-width: 42px; height: 28px; }

@media (max-width: 1100px) {
  /* 步骤数不再藏掉：这条栏本来就 flex-wrap，挤了就换行，没必要砍信息 */
  .bar { gap: var(--st-s-3); }
}
@media (max-width: 720px) {
  /* 手机上「选模型 / 输入句子」竖着排。
     横排会让这两个最小宽度加起来变成这条栏的最小内容宽（≈580px），而 .viz 是 flex 子项、
     不会缩到内容最小宽以下 —— 整页因此被撑到 622px，画布右边全被切掉。
     竖排之后最小内容宽 = 两者里大的那个（≈250px），390px 装得下。 */
  .bar__setup { flex-direction: column; align-items: stretch; gap: 10px; }
  .bar__field--model { flex: 0 1 auto; }
  .bar__field--text { flex: 0 1 auto; }
  /* 下拉框里那句模型名（Qwen3.8-Flash-Next GSQ-RCO）撑出 380px 的固有宽度，
     一路顶到最外层。overflow:hidden 让这几层的「最小内容宽」按 0 计，
     整条栏的最小宽度就只剩那几个按钮。 */
  .bar__setup, .bar__field, .bar__select { overflow: hidden; min-width: 0; }
  .bar[data-focus] .bar__label--inline { display: none; }   /* 演示中速度只留三个数字 */
  .bar[data-focus] .seg__btn { min-width: 38px; padding: 0 6px; }
  .bar[data-focus] .bar__go, .bar[data-focus] .bar__reset { padding: 0 10px; }
}
</style>
