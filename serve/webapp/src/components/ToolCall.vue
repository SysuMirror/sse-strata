<script setup>
// 一次 MCP 工具调用：紧凑的一块（名字、状态、一行预览），展开后是参数与模型读到的结果。
// 结果可能有两万字符，所以正文只在展开时构建 —— 与原 app.js 的 toolHtml 等价。
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { useI18n } from "vue-i18n";
import { fmt } from "../api.js";
import Icon from "./Icon.vue";

const props = defineProps({ tool: { type: Object, required: true }, k: { type: Number, required: true } });
const { t } = useI18n();
const open = ref(false);

const STATE = {
  writing: ["st-badge--reading", "tool.writing"], running: ["st-badge--generating", "tool.running"],
  done: ["", "tool.done"], error: ["st-badge--error", "tool.error"], skipped: ["st-badge--queued", "tool.skipped"],
};
const cls = computed(() => (STATE[props.tool.state] || ["", props.tool.state])[0]);
const stateText = computed(() => {
  const k = (STATE[props.tool.state] || [null, null])[1];
  return k ? t(k) : props.tool.state;
});
const args = computed(() => (props.tool.arguments == null ? "" : JSON.stringify(props.tool.arguments, null, 2)));
const preview = computed(() => (props.tool.result != null ? props.tool.result : args.value.replace(/\s+/g, " ")));
const label = computed(() => (props.tool.ok ? t("tool.result") : t("tool.error.out")));
const copyText = computed(() => {
  const a = args.value;
  const r = props.tool.result;
  return r == null ? a : `${a}\n\n---\n${r}`;
});
defineExpose({ copyText });

function toggle(e) { e.preventDefault(); open.value = !open.value; }

// 运行中实时计时：工具一多，用户需要看到「还在跑」而不是一个静止的「运行中」。
const liveMs = ref(0);
let liveTimer = null;
const startLive = () => {
  if (liveTimer) return;
  const started = props.tool.startedAt || Date.now();
  liveMs.value = Date.now() - started;
  liveTimer = setInterval(() => { liveMs.value = Date.now() - started; }, 100);
};
const stopLive = () => {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
};
watch(() => props.tool.state, (s) => (s === "running" || s === "writing" ? startLive() : stopLive()), { immediate: true });
onMounted(() => { if (props.tool.state === "running" || props.tool.state === "writing") startLive(); });
onUnmounted(stopLive);

const RUNNING_HINT = {
  generate_image: "正在生成图片…",
  image_search: "正在搜图…",
  search_web: "正在联网搜索…",
  fetch_url: "正在抓取网页…",
  run_code: "正在运行代码…",
  start_job: "正在启动任务…",
  job_status: "正在读取日志…",
  kb_search: "正在检索知识库…",
  kb_add: "正在入库…",
  read_file: "正在读取文件…",
  write_file: "正在写入文件…",
  edit_file: "正在修改文件…",
  list_files: "正在列目录…",
  service_status: "正在检查服务…",
};
const runningHint = computed(() => (props.tool.state === "running" || props.tool.state === "writing")
  ? (RUNNING_HINT[props.tool.name] || "") : "");
const liveText = computed(() => ((props.tool.state === "running" || props.tool.state === "writing")
  ? `${(liveMs.value / 1000).toFixed(1)}s` : ""));
</script>

<template>
  <details class="st-collapse tool-call" :data-tool="k" :data-state="tool.state" :open="open">
    <summary @click="toggle">
      <Icon name="tool" cls="st-icon st-icon--sm" />
      <span class="tool-call__name" :title="tool.name || ''">{{ tool.tool || tool.name || "tool" }}</span>
      <span v-if="tool.server" class="muted small">{{ tool.server }}</span>
      <span class="tool-call__preview muted">{{ preview.slice(0, 200) }}</span>
      <span v-if="runningHint" class="tool-call__hint muted">{{ runningHint }}</span>
      <span v-if="liveText" class="tool-call__spin" aria-hidden="true"></span>
      <span v-if="liveText" class="tool-call__live">{{ liveText }}</span>
      <span class="st-badge" :class="cls">{{ stateText }}</span>
      <span v-if="tool.ms != null && tool.state !== 'skipped'" class="muted small">{{ fmt(tool.ms / 1000, 1) }} s</span>
      <Icon name="chevron" cls="st-icon st-icon--sm st-chev" />
    </summary>
    <div v-if="open" class="st-collapse__body">
      <div class="tool-call__label">{{ t("tool.arguments") }}</div>
      <pre class="tool-call__pre">{{ args || t("tool.being.written") }}</pre>
      <template v-if="tool.result != null">
        <div class="tool-call__label">
          {{ label }}<template v-if="tool.chars"> · {{ t("tool.chars", { n: fmt(tool.chars) }) }}</template><template
            v-if="tool.truncated">{{ t("tool.cut") }}</template>
        </div>
        <pre class="tool-call__pre">{{ tool.result }}</pre>
      </template>
    </div>
  </details>
</template>

<style scoped>
.tool-call__hint { flex: none; font-size: 11px; color: var(--st-ink-muted); white-space: nowrap; }
.tool-call__spin { flex: none; width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--st-line);
                  border-top-color: var(--st-accent); animation: toolcall-spin 900ms linear infinite; }
.tool-call__live { flex: none; font-size: 11px; color: var(--st-ink-muted); font-variant-numeric: tabular-nums; }
@keyframes toolcall-spin { to { transform: rotate(360deg); } }
</style>
