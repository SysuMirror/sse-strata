<script setup>
// 一次工具调用：一行摘要 + 展开看参数/结果。
// 设计取向：默认只占一行（聊天流里不打断阅读），状态用颜色 + 文字说清楚；
// 图标底色按工具类别（文件/代码/任务/知识库/联网/生图）区分，扫一眼就知道刚才干了什么。
import { ref, computed, onBeforeUnmount, onMounted, watch } from "vue";
import { useI18n } from "vue-i18n";
import Icon from "./Icon.vue";
import ImageViewer from "./ImageViewer.vue";

const { t } = useI18n();
const props = defineProps({ tool: { type: Object, required: true } });
const open = ref(false);
const copied = ref(false);
const viewer = ref(null);          // 点开生图结果看大图

const STATE = {
  running: { cls: "is-running", label: "执行中", icon: "refresh" },
  done: { cls: "is-done", label: "完成", icon: "check" },
  error: { cls: "is-error", label: "失败", icon: "error" },
  skipped: { cls: "is-skipped", label: "跳过", icon: "stop" },
};
const st = computed(() => STATE[props.tool.state] || STATE.done);

// 名称 → 显示名 / 图标 / 类别色
const META = {
  list_files: { label: "列目录", icon: "layers", tone: "file" },
  read_file: { label: "读文件", icon: "tool", tone: "file" },
  write_file: { label: "写文件", icon: "tool", tone: "file" },
  edit_file: { label: "改文件", icon: "tool", tone: "file" },
  delete_path: { label: "删除", icon: "trash", tone: "danger" },
  search_files: { label: "搜索文件", icon: "layers", tone: "file" },
  run_code: { label: "运行代码", icon: "play", tone: "code" },
  start_job: { label: "启动任务", icon: "play", tone: "job" },
  job_status: { label: "任务日志", icon: "activity", tone: "job" },
  job_stop: { label: "停止任务", icon: "stop", tone: "job" },
  kb_add: { label: "存入知识库", icon: "memory", tone: "kb" },
  kb_search: { label: "知识库检索", icon: "memory", tone: "kb" },
  kb_list: { label: "知识库", icon: "memory", tone: "kb" },
  search_web: { label: "联网搜索", icon: "link", tone: "web" },
  fetch_url: { label: "读网页", icon: "link", tone: "web" },
  image_search: { label: "联网搜图", icon: "image", tone: "image" },
  service_status: { label: "查服务", icon: "gauge", tone: "ops" },
  generate_image: { label: "生图", icon: "image", tone: "image" },
  embed_text: { label: "向量化", icon: "chart", tone: "ops" },
  mcpCall: { label: "MCP", icon: "link", tone: "mcp" },
};
const meta = computed(() => META[props.tool.name] || { label: props.tool.name || props.tool.tool || "tool", icon: "tool", tone: "default" });
const title = computed(() => meta.value.label);

/** 一行摘要：优先展示最有信息量的那个参数（路径 > 查询 > URL）。 */
const summary = computed(() => {
  const a = props.tool.arguments || {};
  const v = a.path || a.q || a.url || a.input || a.prompt || "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 60 ? s.slice(0, 60) + "…" : s;
});
const argsText = computed(() => JSON.stringify(props.tool.arguments || {}, null, 2));
const resultText = computed(() => (props.tool.result == null ? "" : String(props.tool.result)));
const ms = computed(() => (props.tool.ms == null ? "" : props.tool.ms < 1000 ? `${props.tool.ms}ms` : `${(props.tool.ms / 1000).toFixed(1)}s`));

// ---------------- 生图：进度条 + 轮换的「正在润色…」文案 ----------------
// 生图模型没有流式进度，只能给一个「时间驱动的假进度」：前段涨得快、后段越来越慢，
// 最多到 94%，等真正的图片回来再切走。文案每 3.5 秒换一句，至少让人知道没卡死。
const isImageTool = computed(() => props.tool.name === "generate_image");
const running = computed(() => props.tool.state === "running");
const nowTick = ref(Date.now());
let ticker = null;
const startedAt = computed(() => props.tool.startedAt || nowTick.value);
const elapsed = computed(() => Math.max(0, nowTick.value - startedAt.value));
const pct = computed(() => {
  if (!running.value) return 100;
  return Math.min(94, Math.round(94 - 89 * Math.exp(-elapsed.value / 22000)));
});
const STEP_KEYS = ["chat.image.step.0", "chat.image.step.1", "chat.image.step.2", "chat.image.step.3",
                   "chat.image.step.4", "chat.image.step.5", "chat.image.step.6"];
const stepText = computed(() => t(STEP_KEYS[Math.min(STEP_KEYS.length - 1, Math.floor(elapsed.value / 3500))]));
const refCount = computed(() => (Array.isArray(props.tool.arguments?.refs) ? props.tool.arguments.refs.length : 0));

function startTick() {
  if (ticker) return;
  nowTick.value = Date.now();
  ticker = setInterval(() => { nowTick.value = Date.now(); }, 400);
}
function stopTick() { if (ticker) { clearInterval(ticker); ticker = null; } }
onMounted(() => { if (isImageTool.value && running.value) startTick(); });
watch(() => props.tool.state, (s) => {
  if (isImageTool.value && s === "running") startTick();
  else stopTick();
});
onBeforeUnmount(stopTick);

async function copyResult() {
  try {
    await navigator.clipboard.writeText(resultText.value || argsText.value);
    copied.value = true;
    setTimeout(() => (copied.value = false), 1400);
  } catch (e) { /* 剪贴板不可用就静默 */ }
}
</script>

<template>
  <div class="tool" :class="[st.cls, `tool--${meta.tone}`]" :data-open="open ? '1' : null">
    <button class="tool__row" type="button" :aria-expanded="open" @click="open = !open">
      <span class="tool__icon"><Icon :name="meta.icon" cls="st-icon st-icon--sm" /></span>
      <span class="tool__main">
        <span class="tool__titleline">
          <span class="tool__name">{{ title }}</span>
          <span class="tool__state" :class="st.cls">{{ st.label }}</span>
        </span>
        <span v-if="summary" class="tool__summary">{{ summary }}</span>
      </span>
      <span class="tool__meta">
        <span v-if="ms" class="tool__ms">{{ ms }}</span>
        <span class="tool__chev"><Icon name="chevron" cls="st-icon st-icon--sm" /></span>
      </span>
      <span v-if="tool.state === 'running'" class="tool__pulse" aria-hidden="true"></span>
    </button>

    <!-- 生图进行中：假进度条 + 每几秒换一句状态，别让人以为卡死 -->
    <div v-if="isImageTool && running" class="tool__gen" role="status" aria-live="polite">
      <!-- 生图占位框：和最后那张图同样的比例，先占住位置，别让聊天流塌下去 -->
      <div class="tool__genbox">
        <span class="tool__genspinner" aria-hidden="true"></span>
        <span class="tool__genin">{{ stepText }}</span>
      </div>
      <div class="tool__gen-head">
        <span class="tool__gen-hint">正在生成图片</span>
        <span v-if="refCount" class="tool__gen-refs">{{ t("chat.image.refs", { n: refCount }) }}</span>
        <b class="tool__gen-pct">{{ pct }}%</b>
      </div>
      <div class="tool__gen-bar"><i :style="{ width: pct + '%' }"></i></div>
    </div>

    <!-- 生图结果：贴在工具卡下面，不再跑到消息顶部 -->
    <button v-if="isImageTool && tool.image" type="button" class="tool__imgwrap"
            :title="'点击查看大图 / 下载'" @click="viewer = { src: tool.image, name: 'generated' }">
      <img :src="tool.image" class="tool__img" alt="generated image">
      <span class="tool__imgzoom"><Icon name="image" cls="st-icon st-icon--sm" />查看大图</span>
    </button>

    <div v-if="open" class="tool__body">
      <div class="tool__section">
        <div class="tool__label">
          <span>参数</span>
        </div>
        <pre class="tool__pre">{{ argsText }}</pre>
      </div>
      <div v-if="resultText" class="tool__section">
        <div class="tool__label">
          <span>结果<span class="tool__len">{{ resultText.length }} 字符</span></span>
          <button type="button" class="tool__copy" :data-copied="copied ? '1' : null" @click.stop="copyResult">
            <Icon :name="copied ? 'check' : 'copy'" cls="st-icon st-icon--sm" />{{ copied ? "已复制" : "复制" }}
          </button>
        </div>
        <pre class="tool__pre">{{ resultText.slice(0, 6000) }}</pre>
      </div>
    </div>
    <ImageViewer v-if="viewer" :src="viewer.src" :name="viewer.name" @close="viewer = null" />
  </div>
</template>

<style scoped>
.tool { --tone: var(--st-ink-muted); border: 1px solid var(--st-line); border-radius: 12px;
        background: var(--st-surface); overflow: hidden; transition: border-color var(--st-dur), box-shadow var(--st-dur); }
.tool:hover { border-color: color-mix(in srgb, var(--tone) 35%, var(--st-line)); }
.tool--file { --tone: #64748b; }
.tool--code { --tone: #0f9f6e; }
.tool--job { --tone: #d97706; }
.tool--kb { --tone: #0d9488; }
.tool--web { --tone: #0ea5e9; }
.tool--image { --tone: #e0569a; }
.tool--ops { --tone: #8b5cf6; }
.tool--mcp { --tone: #7c3aed; }
.tool--danger { --tone: #dc2626; }

.tool__row { position: relative; display: flex; align-items: center; gap: 9px; width: 100%; padding: 8px 10px;
             border: none; background: none; cursor: pointer; text-align: left; font: inherit; color: var(--st-ink-soft); }
.tool__row:hover { background: var(--st-surface-2); }
.tool__icon { display: inline-flex; width: 24px; height: 24px; align-items: center; justify-content: center;
              border-radius: 8px; background: color-mix(in srgb, var(--tone) 12%, transparent);
              color: var(--tone); flex: none; }
.tool__main { flex: 1; min-width: 0; display: grid; gap: 1px; }
.tool__titleline { display: flex; align-items: center; gap: 6px; min-width: 0; }
.tool__name { font-size: 12.5px; font-weight: var(--st-fw-bold); color: var(--st-ink); white-space: nowrap; }
.tool__state { display: inline-flex; align-items: center; height: 16px; padding: 0 6px; border-radius: 999px;
               font-size: 10.5px; font-weight: var(--st-fw-bold); letter-spacing: .02em;
               background: var(--st-surface-2); color: var(--st-ink-muted); }
.tool__state.is-done { background: color-mix(in srgb, #16a34a 14%, transparent); color: #15803d; }
.tool__state.is-error { background: color-mix(in srgb, #dc2626 14%, transparent); color: #b91c1c; }
.tool__state.is-running { background: var(--st-accent-tint); color: var(--st-accent-text); }
.tool__state.is-skipped { opacity: .75; }
.tool__summary { font-size: 11.5px; color: var(--st-ink-muted); overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
.tool__meta { display: flex; align-items: center; gap: 6px; flex: none; }
.tool__ms { font-size: 11px; color: var(--st-ink-muted); font-variant-numeric: tabular-nums; }
.tool__chev { display: inline-flex; transition: transform .15s; color: var(--st-ink-muted); }
.tool[data-open] .tool__chev { transform: rotate(180deg); }
/* 执行中：图标转 + 底部一条来回的进度线，避免「卡住了」的错觉 */
.tool.is-running .tool__icon { color: var(--tone); animation: tool-spin 1.1s linear infinite; }
@keyframes tool-spin { to { transform: rotate(360deg); } }
.tool__pulse { position: absolute; left: 0; right: 0; bottom: 0; height: 2px; overflow: hidden;
               background: color-mix(in srgb, var(--tone) 18%, transparent); }
.tool__pulse::after { content: ""; position: absolute; top: 0; bottom: 0; width: 38%;
                      background: var(--tone); border-radius: 2px; animation: tool-pulse 1.2s var(--st-ease) infinite; }
@keyframes tool-pulse { from { left: -38%; } to { left: 100%; } }
.tool.is-error { border-color: color-mix(in srgb, #dc2626 35%, var(--st-line)); }

/* 生图进度：先占一个和成图同比例的方框（里面转圈 + 轮换文案），下面才是进度条 */
.tool__gen { display: grid; gap: 6px; padding: 0 10px 9px; }
.tool__genbox { position: relative; width: min(320px, 100%); aspect-ratio: 1 / 1; margin-bottom: 2px;
                display: grid; place-items: center; gap: 10px; align-content: center;
                border: 1px dashed var(--st-line); border-radius: 10px; overflow: hidden;
                background: color-mix(in srgb, var(--tone) 5%, var(--st-surface-2)); }
/* 斜向扫光：让空框看起来「在动」，不是坏了 */
.tool__genbox::before { content: ""; position: absolute; inset: 0; pointer-events: none;
                        background: linear-gradient(110deg, transparent 30%,
                                    color-mix(in srgb, var(--tone) 14%, transparent) 50%, transparent 70%);
                        animation: tool-sheen 2.2s linear infinite; }
.tool__genspinner { width: 30px; height: 30px; border-radius: 50%;
                    border: 3px solid color-mix(in srgb, var(--tone) 20%, transparent);
                    border-top-color: var(--tone); animation: tool-spin 1s linear infinite; }
.tool__genin { max-width: 80%; text-align: center; font-size: 11.5px; line-height: 1.5;
               color: var(--st-ink-muted); }
.tool__gen-head { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--st-ink-muted); }
.tool__gen-hint { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tool__gen-refs { flex: none; padding: 0 6px; height: 16px; display: inline-flex; align-items: center;
                  border-radius: 999px; background: var(--st-accent-tint); color: var(--st-accent-text);
                  font-size: 10.5px; font-weight: var(--st-fw-bold); }
.tool__gen-pct { flex: none; color: var(--tone); font-variant-numeric: tabular-nums; }
.tool__gen-bar { position: relative; height: 5px; border-radius: 999px; overflow: hidden;
                 background: color-mix(in srgb, var(--tone) 14%, var(--st-surface-2)); }
.tool__gen-bar i { display: block; height: 100%; border-radius: 999px;
                   background: linear-gradient(90deg, color-mix(in srgb, var(--tone) 55%, #fff), var(--tone));
                   transition: width .45s linear; }
.tool__gen-bar i::after { content: ""; position: absolute; inset: 0; border-radius: inherit;
                          background: linear-gradient(110deg, transparent 20%, rgba(255,255,255,.45) 50%, transparent 80%);
                          animation: tool-sheen 1.6s linear infinite; }
@keyframes tool-sheen { from { transform: translateX(-100%); } to { transform: translateX(100%); } }

/* 生图结果：贴在卡片下方，缩略图即可点开大图 */
.tool__imgwrap { position: relative; display: block; margin: 0 10px 10px; padding: 0; border: 0;
                 background: none; cursor: zoom-in; border-radius: 10px; overflow: hidden;
                 border: 1px solid var(--st-line); }
.tool__img { display: block; width: 100%; max-width: 360px; height: auto; }
.tool__imgwrap:hover .tool__imgzoom { opacity: 1; }
.tool__imgzoom { position: absolute; right: 8px; bottom: 8px; display: inline-flex; align-items: center; gap: 4px;
                 padding: 4px 8px; border-radius: 999px; background: rgba(15, 23, 42, .62); color: #fff;
                 font-size: 11px; opacity: 0; transition: opacity var(--st-dur); }

.tool__body { border-top: 1px solid var(--st-line); padding: 9px 10px 11px; display: grid; gap: 9px; }
.tool__section { display: grid; gap: 4px; }
.tool__label { display: flex; align-items: center; justify-content: space-between; gap: 8px;
               font-size: 11px; color: var(--st-ink-muted); }
.tool__len { margin-left: 6px; opacity: .7; }
.tool__copy { display: inline-flex; align-items: center; gap: 3px; padding: 2px 6px; border-radius: 6px;
              border: 0; background: none; color: var(--st-ink-muted); font: inherit; font-size: 11px; cursor: pointer; }
.tool__copy:hover { background: var(--st-surface-2); color: var(--st-ink); }
.tool__copy[data-copied] { color: #15803d; }
.tool__pre { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--st-surface-2); max-height: 260px;
             overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
             font: 400 12px/1.55 var(--st-font-mono), "PingFang SC", "Microsoft YaHei", sans-serif; color: var(--st-ink-soft); }
</style>