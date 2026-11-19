<script setup>
// 一条消息：用户气泡，或模型回答（思考折叠块 + 正文 + 交错其中的工具调用块）。
// 与原 app.js 的 msgEl/updateAssistant/answerHtml 等价，只是改由响应式渲染。
import { ref, computed, watch } from "vue";
import { useI18n } from "vue-i18n";
import { fmt, copyText } from "../api.js";
import { markdown } from "../markdown.js";
import Icon from "./Icon.vue";
import ToolCall from "./ToolCall.vue";
import ImageViewer from "./ImageViewer.vue";

const props = defineProps({
  m: { type: Object, required: true },
  i: { type: Number, required: true },
  streaming: { type: Boolean, default: false },
  showThinking: { type: Boolean, default: true },
});
const emit = defineEmits(["speak", "toKb"]);

const { t } = useI18n();
const thinkOpen = ref(false);
const viewer = ref(null);              // 点开的图片：{src, name}
function openImage(im) { if (im && im.url) viewer.value = { src: im.url, name: im.name || "" }; }
const touched = ref(false);          // 用户自己点开过：之后不再自动开合
let auto = false;                    // 本次开合是我们做的，不是用户点的

const timeStr = (x) => new Date(x).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const thinkingNow = computed(() => props.streaming && !props.m.text);
// 引擎偶发只吐思考就 EOS：流结束后没有正文、没有工具、没有图，给一条明确的空态说明，
// 而不是留一个空白气泡（新消息也会被客户端自动重试一次）。
const emptyAnswer = computed(() => props.m.role === "assistant" && !props.streaming
  && !(props.m.text || "").trim() && !(props.m.tools && props.m.tools.length)
  && !(props.m.images && props.m.images.length) && !props.m.error);

// 正文：文本段与工具块按模型调用的位置交错（工具块的原地位置记在 t.at）。
// generate_image 不画通用 tool 卡片：在它所在的位置直接画「生图占位框」或生成好的图片。
const imageForTool = (tool) => {
  if (tool && tool.image) return { url: tool.image, name: "generated", toolId: tool.id };
  return (props.m.images || []).find((im) => im.toolId && im.toolId === tool.id) || null;
};
const orphanImages = computed(() => (props.m.images || []).filter((im) => !im.toolId));

const segments = computed(() => {
  const m = props.m;
  if (!m.tools || !m.tools.length) return [{ type: "text", html: markdown(m.text || "") }];
  const out = [];
  let pos = 0;
  m.tools.forEach((tool, k) => {
    const at = Math.min(Math.max(tool.at || 0, pos), m.text.length);
    if (at > pos) out.push({ type: "text", html: markdown(m.text.slice(pos, at)) });
    pos = at;
    if (tool.name === "generate_image") {
      const im = imageForTool(tool);
      if (im) out.push({ type: "image", image: im, tool, k });
      else if (tool.state === "error" || tool.state === "skipped") out.push({ type: "tool", tool, k });
      else out.push({ type: "imgSkeleton", tool, k });
    } else {
      out.push({ type: "tool", tool, k });
    }
  });
  out.push({ type: "text", html: markdown(m.text.slice(pos)) });
  return out;
});

const thinkTitle = computed(() => (thinkingNow.value ? t("chat.thinking.now")
  : props.m.thinkSecs != null ? t("chat.thought.for", { n: fmt(props.m.thinkSecs, 1) }) : t("chat.thoughts")));
const metaText = computed(() => props.m.meta || (props.streaming ? "" : props.m.stopped ? t("chat.stopped") : ""));

// 流式思考时按偏好自动展开，答案一开始就自动收起 —— 用户自己动过之后不再自动
watch(() => [thinkingNow.value, props.m.reasoning, props.showThinking], () => {
  if (!props.m.reasoning) return;
  if (thinkingNow.value && props.showThinking && !touched.value && !thinkOpen.value) { auto = true; thinkOpen.value = true; }
  if (!thinkingNow.value && thinkOpen.value && !touched.value) { auto = true; thinkOpen.value = false; }
});

function onThinkToggle(e) {
  // 用户点 summary 时浏览器会自己改 <details open>，但 Vue 的 thinkOpen 不会跟着变；
  // 正文又是 v-if="thinkOpen || thinkingNow"，不同步就会出现「卡片展开了、里面空的」。
  const open = !!(e && e.currentTarget && e.currentTarget.open);
  thinkOpen.value = open;
  if (auto) { auto = false; return; }          // 我们自己开合的，不算用户操作
  touched.value = true;
}
</script>

<template>
  <div class="st-msg" :class="`st-msg--${m.role}`" :data-i="i">
    <template v-if="m.role === 'user'">
      <div v-if="m.files && m.files.length" class="msg-images">
        <span v-for="(f, k) in m.files" :key="`f${k}`" class="chip"><Icon name="attach" cls="st-icon st-icon--sm" />{{ f.name }}</span>
      </div>
      <div v-if="m.images && m.images.length" class="msg-images">
        <template v-for="(im, k) in m.images" :key="`i${k}`">
          <img v-if="im.url" :src="im.url" :alt="im.name || 'image'" class="msg-img" title="点击查看大图 / 下载"
               @click="openImage(im)">
          <span v-else class="chip"><Icon name="image" cls="st-icon st-icon--sm" />{{ im.name || "image" }}</span>
        </template>
      </div>
      <div class="st-bubble">{{ m.text }}</div>
      <div class="st-msg__meta">{{ t("chat.you") }} · {{ timeStr(m.time) }}</div>
    </template>

    <template v-else>
      <!-- 老记录/其它来源的图：没有工具归属的才放在消息顶部；生图结果在工具调用的位置 -->
      <div v-if="orphanImages.length" class="msg-images">
        <template v-for="(im, k) in orphanImages" :key="`ai${k}`">
          <img v-if="im.url" :src="im.url" :alt="im.name || 'generated image'" class="msg-img"
               title="点击查看大图 / 下载" @click="openImage(im)">
        </template>
      </div>
      <details v-if="m.reasoning" class="st-collapse think" :open="thinkOpen" @toggle="onThinkToggle">
        <summary>
          <Icon name="thinking" cls="st-icon st-icon--sm" />
          <span class="think-title">{{ thinkTitle }}</span>
          <Icon name="chevron" cls="st-icon st-icon--sm st-chev" />
        </summary>
        <div class="st-collapse__body thinking">{{ m.reasoning }}</div>
      </details>
      <div class="st-bubble" :class="{ cursor: streaming && (m.text || (m.tools && m.tools.length)) }">
        <div v-if="m.error" class="msg-error">{{ m.error }}</div>
        <template v-else-if="emptyAnswer">
          <span class="muted">{{ t("chat.empty.answer") }}</span>
        </template>
        <template v-else-if="!m.text && streaming && !(m.tools && m.tools.length)">
          <span v-if="m.reasoning" class="muted cursor">{{ t("chat.writing") }}</span>
          <span v-else class="cursor"></span>
        </template>
        <template v-else>
          <template v-for="(seg, k) in segments" :key="k">
            <ToolCall v-if="seg.type === 'tool'" :tool="seg.tool" :k="seg.k" />
            <!-- 生图调用：不画通用工具卡，直接占一个图片位，生成完就在原位换成图片 -->
            <div v-else-if="seg.type === 'imgSkeleton'" class="img-skeleton"
                 style="display:flex;align-items:center;gap:10px;max-width:380px;width:fit-content;margin:8px 0">
              <div class="img-skeleton__box"
                   style="width:64px;height:64px;min-width:64px;aspect-ratio:auto;flex:none">
                <span class="img-skeleton__spinner"></span></div>
              <div class="img-skeleton__body" style="display:grid;gap:3px;min-width:0">
                <div class="img-skeleton__label" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0">
                  <span class="img-skeleton__badge">{{ t("chat.image.generating") }}</span>
                  <span class="img-skeleton__prompt"
                        style="max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ (seg.tool.arguments && seg.tool.arguments.prompt) || "" }}</span>
                </div>
                <p class="img-skeleton__hint" style="margin:0">{{ t("chat.image.generating.hint") }}</p>
              </div>
            </div>
            <div v-else-if="seg.type === 'image'" class="msg-images msg-images--inline">
              <img v-if="seg.image.url" :src="seg.image.url" :alt="seg.image.name || 'generated image'"
                   class="msg-img" style="max-width:240px;max-height:240px"
                   title="点击查看大图 / 下载" @click="openImage(seg.image)">
            </div>
            <span v-else v-html="seg.html"></span>
          </template>
          <div v-if="streaming && m.tools && m.tools.length
                    && !m.tools.some((x) => x.state === 'running' || x.state === 'writing')"
               class="msg-working">
            <span class="msg-working__spin" aria-hidden="true"></span>{{ t("chat.working") }}
          </div>
        </template>
      </div>
      <div class="st-msg__meta">
        <span class="meta-text">{{ metaText }}</span>
        <button v-show="!streaming && m.text" class="st-btn st-btn--icon" title="朗读（胡桃）"
                aria-label="朗读" @click="emit('speak', m)"><Icon name="play" /></button>
        <button v-show="!streaming && m.text" class="st-btn st-btn--icon" title="存入知识库"
                aria-label="存入知识库" @click="emit('toKb', m)"><Icon name="memory" /></button>
        <button v-show="!streaming && m.text" class="st-btn st-btn--icon" :aria-label="t('chat.copy.answer')"
                :title="t('chat.copy')" @click="copyText(m.text)"><Icon name="copy" /></button>
      </div>
    </template>
    <ImageViewer v-if="viewer" :src="viewer.src" :name="viewer.name" @close="viewer = null" />
  </div>
</template>

<style scoped>
.msg-img { cursor: zoom-in; }
</style>

<style scoped>
.msg-img { cursor: zoom-in; }
.msg-images--inline { justify-content: flex-start; margin: 8px 0; }
.msg-images--inline img { max-width: min(100%, 260px); max-height: 260px; }
</style>

<style scoped>
.msg-working { display: inline-flex; align-items: center; gap: 6px; margin-top: 6px;
               color: var(--st-ink-muted); font-size: 12px; }
.msg-working__spin { width: 11px; height: 11px; border-radius: 50%; border: 2px solid var(--st-line);
                     border-top-color: var(--st-accent); animation: msgWorkSpin 900ms linear infinite; }
@keyframes msgWorkSpin { to { transform: rotate(360deg); } }
</style>

<style scoped>
/* 思考不再是一块独立的灰卡片：只留一行「思考了 X 秒」，展开时用左侧细线缩进。
   这样它和下面的回答是同一条消息流，中间没有「分段」。 */
.st-msg--assistant { gap: 4px; }
.st-msg--assistant .think { background: transparent; border: 0; border-radius: 0; }
.st-msg--assistant .think > summary { padding: 2px 0; font-size: 12px; font-weight: 500;
                                      color: var(--st-ink-muted); }
.st-msg--assistant .think > summary:hover { color: var(--st-ink); }
.st-msg--assistant .think .st-collapse__body { padding: 4px 0 8px 12px; background: transparent;
                                               border-left: 2px solid var(--st-line); }
</style>

<style scoped>
/* 工具卡也不再是一块实心浅灰：透明底 + 细线，和消息正文融在一起 */
.st-msg--assistant .tool-call { background: transparent; }
.st-msg--assistant .tool-call > summary:hover { background: var(--st-surface-2); }
</style>
