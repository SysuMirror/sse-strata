<script setup>
// 推理可视化页面。平时是「顶栏（选模型/输入/播放/调速）→ 左栏模型卡片 → 中间画布 → 右栏详情 → 底部时间轴」；
// **点开始演示后整页切换成演示模式**：左右两栏与顶栏的选型、输入整块退场，画布铺满，
// 播放按钮与时间轴变成悬浮在画布上的两条胶囊 —— 页面上只剩「画布 + 流程控制」。
// 退出演示（= 重置）面板回场。
//
// 只有本页用 v-if 而不是 v-show 挂载：切到别的标签页就把画布整棵卸掉。
import { onBeforeUnmount, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import ControlBar from "./components/ControlBar.vue";
import InfoPanel from "./components/InfoPanel.vue";
import ModelSelector from "./components/ModelSelector.vue";
import IsoView from "./components/IsoView.vue";
import TimelineSlider from "./components/TimelineSlider.vue";
import { goTo, init, reset, stepBy, togglePane, togglePlay, viz } from "./store.js";

const { t } = useI18n();

// 快捷键只在浏览这个页面、且焦点不在输入框里时生效
function onKey(e) {
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || e.metaKey || e.ctrlKey) return;
  if (e.key === "ArrowRight") { stepBy(1); e.preventDefault(); }
  else if (e.key === "ArrowLeft") { stepBy(-1); e.preventDefault(); }
  else if (e.key === " ") { togglePlay(); e.preventDefault(); }
  else if (e.key === "Home") { goTo(-1); e.preventDefault(); }
  else if (e.key === "Escape" && viz.focus) { reset(); e.preventDefault(); }   // Esc = 退出演示
}

onMounted(() => {
  if (!viz.models.length) init();
  window.addEventListener("keydown", onKey);
});
onBeforeUnmount(() => window.removeEventListener("keydown", onKey));
</script>

<template>
  <div class="view viz" :data-focus="viz.focus ? '1' : null">
    <ControlBar />

    <div class="viz__body" :data-left="viz.panes.left ? null : 'off'" :data-right="viz.panes.right ? null : 'off'">
      <div class="viz__side viz__side--left"><ModelSelector /></div>
      <div class="viz__stage">
        <IsoView />
        <!-- 面板与画布交界处的小把手：开着就是「收起」，收起就是「拉开」。位置不变，不用去找。 -->
        <button v-if="!viz.focus" class="viz__handle viz__handle--left" :data-open="viz.panes.left ? '1' : null"
                :title="viz.panes.left ? t('viz.pane.hide') : t('viz.pane.models')"
                :aria-label="viz.panes.left ? t('viz.pane.hide') : t('viz.pane.models')"
                @click="togglePane('left')">{{ viz.panes.left ? '‹' : '›' }}</button>
        <button v-if="!viz.focus" class="viz__handle viz__handle--right" :data-open="viz.panes.right ? '1' : null"
                :title="viz.panes.right ? t('viz.pane.hide') : t('viz.pane.detail')"
                :aria-label="viz.panes.right ? t('viz.pane.hide') : t('viz.pane.detail')"
                @click="togglePane('right')">{{ viz.panes.right ? '›' : '‹' }}</button>
      </div>
      <div class="viz__side viz__side--right"><InfoPanel /></div>
    </div>

    <TimelineSlider />

    <p v-if="viz.error" class="viz__error" role="alert">
      {{ viz.error === "empty" ? t("viz.error.empty") : viz.error }}
    </p>
  </div>
</template>

<style scoped>
/* 这一页跟随应用主题（和别处一致）。线稿视图要的就是「工程图」那种干净感。 */
/* min-width: 0 是必须的：这一页是 flex 子项，默认 min-width:auto = 内容最小宽，
   而顶栏里那个下拉框（模型名很长）的最小内容宽就有 380px 左右 —— 于是整页被撑到 473px，
   手机上画布右边全被切掉。归零之后顶栏自己换行收纳，页面老老实实按视口宽度布局。 */
.viz { background: var(--st-bg); color: var(--st-ink); position: relative; min-width: 0; }

.viz__body { flex: 1; min-height: 0; display: grid;
             grid-template-columns: var(--viz-l, 272px) minmax(0, 1fr) var(--viz-r, 320px);
             transition: grid-template-columns .42s cubic-bezier(.22, .61, .36, 1); }
/* 收起某一块：那一列的宽度收到 0（CSS 变量驱动，桌面与窄屏共用同一条规则） */
.viz__body[data-left="off"] { --viz-l: 0px; }
.viz__body[data-right="off"] { --viz-r: 0px; }
.viz__side { overflow-y: auto; overflow-x: hidden; background: var(--st-surface);
             transition: opacity .26s ease, transform .42s cubic-bezier(.22, .61, .36, 1), background .3s; }
.viz__side--left { border-right: 1px solid var(--st-line); }
.viz__side--right { border-left: 1px solid var(--st-line); }
.viz__stage { position: relative; min-width: 0; min-height: 0; }

/* 面板收起来了：跟演示模式一样的退场处理（收 0 宽 + 淡出） */
.viz__body[data-left="off"] .viz__side--left,
.viz__body[data-right="off"] .viz__side--right {
  opacity: 0; transform: scale(.96); pointer-events: none; padding: 0; background: transparent; border-width: 0;
}

/* 交界处的把手：面板开着时它就是「收起」，收起来了它就是「拉开」 */
.viz__handle { position: absolute; top: 50%; transform: translateY(-50%); z-index: 3;
               width: 18px; height: 64px; padding: 0; cursor: pointer;
               display: inline-flex; align-items: center; justify-content: center;
               border: 1px solid var(--st-line); background: var(--st-surface); color: var(--st-ink-muted);
               font-size: 15px; line-height: 1; box-shadow: var(--st-sh-card);
               transition: color var(--st-dur), background var(--st-dur); }
.viz__handle:hover { color: var(--st-ink); background: var(--st-surface-2); }
.viz__handle--left { left: 0; border-left: 0; border-radius: 0 9px 9px 0; }
.viz__handle--right { right: 0; border-right: 0; border-radius: 9px 0 0 9px; }

/* ---- 演示模式：两栏退场、画布铺满、控制条浮起来 ----
   两栏不是 display:none（那没法过渡），而是把栅格列宽收到 0 并整体淡出；
   .bar / .tl 是子组件的根元素，父组件的 scoped 样式能在它们身上生效。 */
.viz[data-focus] .viz__body { grid-template-columns: 0 minmax(0, 1fr) 0; }
.viz[data-focus] .viz__side { opacity: 0; transform: scale(.96); pointer-events: none; padding: 0;
                              background: transparent; border-width: 0; }

/* 铺满：正文块绝对定位，把整块舞台让给画布；上下两条控制条悬浮在画布上 */
.viz[data-focus] .viz__body { position: absolute; inset: 0; }
.viz[data-focus] .bar {
  position: absolute; left: 50%; top: 12px; transform: translateX(-50%); z-index: 4;
  width: auto; max-width: calc(100% - 24px);
  border: 1px solid var(--st-line); border-radius: var(--st-r-pill); background: var(--st-surface);
  box-shadow: 0 10px 30px rgb(0 0 0 / .10), 0 2px 8px rgb(0 0 0 / .06);
  padding: 8px 14px; animation: viz-rise .32s cubic-bezier(.22, .61, .36, 1) both;
}
.viz[data-focus] .tl {
  position: absolute; left: 50%; bottom: 12px; transform: translateX(-50%); z-index: 4;
  width: min(940px, calc(100% - 24px));
  border: 1px solid var(--st-line); border-radius: var(--st-r-md); background: var(--st-surface);
  box-shadow: 0 10px 30px rgb(0 0 0 / .10), 0 2px 8px rgb(0 0 0 / .06);
  animation: viz-rise .32s cubic-bezier(.22, .61, .36, 1) both;
}
@keyframes viz-rise { from { opacity: 0; transform: translate(-50%, 8px); } to { opacity: 1; transform: translate(-50%, 0); } }

/* 深色滚动条（浅色主题下浏览器默认的那条会突兀） */
.viz__side::-webkit-scrollbar { width: 10px; }
.viz__side::-webkit-scrollbar-thumb { background: var(--st-line); border-radius: 6px; border: 2px solid var(--st-surface); }
.viz__side::-webkit-scrollbar-thumb:hover { background: var(--st-ink-muted); }

.viz__error { position: absolute; left: 50%; bottom: 96px; transform: translateX(-50%); margin: 0; z-index: 5;
              padding: 8px 16px; border-radius: var(--st-r-pill); font-size: var(--st-fs-sm);
              background: color-mix(in srgb, var(--st-danger) 12%, transparent); border: 1px solid color-mix(in srgb, var(--st-danger) 50%, transparent); color: var(--st-danger-text); }

@media (max-width: 1280px) { .viz__body { --viz-l: 240px; --viz-r: 296px; } }
/* 窄屏收起左栏：模型在顶栏下拉框里照样能选，画布别被挤没了 */
@media (max-width: 1000px) { .viz__body { --viz-l: 0px; }
                             .viz__side--left { display: none; }
                             .viz__handle--left { display: none; } }
/* 手机上两栏怎么排都放不下：改成上下堆叠 —— 先看图，往下滑看详情。 */
@media (max-width: 720px) {
  .viz__body { display: block; overflow-y: auto; }
  .viz__body[data-right="off"] .viz__side--right { display: none; }
  .viz__stage { height: 54vh; min-height: 300px; }
  .viz__handle { height: 48px; width: 16px; }
  .viz__side--right { border-left: 0; border-top: 1px solid var(--st-line); overflow: visible; }
  /* 演示模式下手机上右栏整个撤掉，画布独占整屏 —— 屏幕上只该有流程和图 */
  .viz[data-focus] .viz__body { display: flex; flex-direction: column; overflow: hidden; }
  .viz[data-focus] .viz__side--right { display: none; }
  .viz[data-focus] .viz__stage { flex: 1; height: auto; min-height: 0; }
}
</style>
