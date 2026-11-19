<script setup>
// Monaco Editor（VS Code 同一个编辑器内核）的薄封装。
//
// 为什么用它：自己用 textarea 拼的编辑器只有「等宽字体 + 行号」，没有语法高亮、括号匹配、
// 多光标、查找替换、矩形选择、代码折叠 —— 用户要的是「成熟框架」，那就直接用 VS Code 的内核。
//
// 体积：Monaco 只在训练页的异步 chunk 里加载（ChatView 不下载它），一次缓存后复用。
// 语言：import 完整 editor.main，Python/C++/JS/Markdown/JSON 等全部带高亮与括号规则。
// 字体：Vite 的 assetsInlineLimit 已调高，codicon 图标字体内联进 CSS —— 引擎静态服务只发
//       .css/.js/.svg，独立的 .ttf 请求会 404。
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";
import JsonWorker from "monaco-editor/languages/features/json/json.worker.js?worker";
import CssWorker from "monaco-editor/languages/features/css/css.worker.js?worker";
import HtmlWorker from "monaco-editor/languages/features/html/html.worker.js?worker";
import TsWorker from "monaco-editor/languages/features/typescript/ts.worker.js?worker";

// 编辑器后台线程。语法高亮在主线程，worker 只负责解析/校验；Vite 会把它们打成扁平的 chunk-*.js，
// 正好符合引擎静态服务「只发 /web/<扁平名>.js」的约束。
self.MonacoEnvironment = {
  getWorker(_, label) {
    if (label === "json") return new JsonWorker();
    if (label === "css" || label === "scss" || label === "less") return new CssWorker();
    if (label === "html" || label === "handlebars" || label === "razor") return new HtmlWorker();
    if (label === "typescript" || label === "javascript") return new TsWorker();
    return new EditorWorker();
  },
};

const props = defineProps({
  modelValue: { type: String, default: "" },
  language: { type: String, default: "plaintext" },
  dark: { type: Boolean, default: false },
  wrap: { type: Boolean, default: false },
  readOnly: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "save", "cursor"]);

const host = ref(null);
let editor = null;
let suppress = false;             // 程序性 setValue 时不要回写 v-model（否则会把光标弹回开头）

const FONT = 'ui-monospace, "SF Mono", "Cascadia Code", Consolas, "Liberation Mono", "Noto Sans Mono CJK SC", monospace';

onMounted(() => {
  editor = monaco.editor.create(host.value, {
    value: props.modelValue,
    language: props.language,
    theme: props.dark ? "vs-dark" : "vs",
    automaticLayout: true,
    minimap: { enabled: false },
    fontFamily: FONT,
    fontSize: 13.5,
    lineHeight: 22,
    wordWrap: props.wrap ? "on" : "off",
    scrollBeyondLastLine: false,
    smoothScrolling: true,
    cursorBlinking: "smooth",
    cursorSmoothCaretAnimation: "on",
    renderLineHighlight: "all",
    padding: { top: 12, bottom: 16 },
    readOnly: props.readOnly,
    tabSize: 4,
    detectIndentation: true,
    stickyScroll: { enabled: false },
    overviewRulerBorder: false,
    scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10, useShadows: false },
    // 小地图关掉：训练页右边已经够挤，省下的宽度给代码
    glyphMargin: true,
    folding: true,
    bracketPairColorization: { enabled: true },
  });

  editor.onDidChangeModelContent(() => {
    if (suppress) return;
    emit("update:modelValue", editor.getValue());
  });
  editor.onDidChangeCursorPosition((e) => {
    emit("cursor", { line: e.position.lineNumber, col: e.position.column });
  });
  // Ctrl/⌘+S 交给页面保存，而不是浏览器的「保存网页」
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => emit("save"));
  emit("cursor", { line: 1, col: 1 });
});

watch(() => props.modelValue, (v) => {
  if (!editor || v === editor.getValue()) return;
  suppress = true;
  editor.setValue(v ?? "");
  suppress = false;
});
watch(() => props.language, (l) => {
  const m = editor && editor.getModel();
  if (m) monaco.editor.setModelLanguage(m, l);
});
watch(() => props.dark, (d) => monaco.editor.setTheme(d ? "vs-dark" : "vs"));
watch(() => props.wrap, (w) => editor && editor.updateOptions({ wordWrap: w ? "on" : "off" }));
watch(() => props.readOnly, (r) => editor && editor.updateOptions({ readOnly: r }));

onBeforeUnmount(() => { if (editor) { editor.dispose(); editor = null; } });

defineExpose({ focus: () => editor && editor.focus() });
</script>

<template>
  <div ref="host" class="code-editor"></div>
</template>

<style scoped>
/* 高度交给父容器（编辑器内部自己滚动）；min-height:0 防止 flex 子项把页面撑高 */
.code-editor { width: 100%; height: 100%; min-height: 0; overflow: hidden; }
</style>