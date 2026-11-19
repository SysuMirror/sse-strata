<script setup>
// 工作台：左边文件树、中间编辑器、右边 agent。
// 工作空间不一定装代码 —— 笔记、提示词、生成图都在这里，所以预览按扩展名分文本/图片两种。
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } from "vue";
import { useI18n } from "vue-i18n";
import { toast, fmt, store } from "../api.js";
import { app } from "../store.js";
import { openLogin } from "../auth.js";
import { tree as wsTree, toTree, readFile, writeFile, writeFileBinary, mkdir, remove, kindOf,
         search as wsSearch, envs as wsEnvs, runCode as wsRun, labReset,
         kbList, kbAdd, kbDelete, jobsList, jobStart, jobStatus, jobStop } from "../workspace.js";
import { markdown } from "../markdown.js";
import { highlight, langOf } from "../highlight.js";
import AgentPanel from "../components/AgentPanel.vue";
import CodeEditor from "../components/CodeEditor.vue";
import Icon from "../components/Icon.vue";
import ImageViewer from "../components/ImageViewer.vue";

const { t } = useI18n();
// 从训练页外壳点「启动训练」时把后台任务面板顶出来（计数信号，父组件每次 +1）。
const props = defineProps({ openJobs: { type: Number, default: 0 } });
const entries = ref([]);
const collapsed = reactive(new Set());
// 多文件标签：每开一个文件就是一条 tab，切换时各自保留内容与未保存状态。
const tabs = reactive([]);                 // [{path, kind, text, b64, dirty, savedAt}]
const activeIdx = ref(-1);
const EMPTY = { path: "", kind: "", text: "", b64: "", dirty: false, savedAt: 0, scrollTop: 0 };
const open = computed(() => tabs[activeIdx.value] || EMPTY);
const cur = () => tabs[activeIdx.value] || EMPTY;
const creating = ref(null);          // "file" | "dir" | null
const view = ref("edit");            // edit | preview | split（仅 Markdown）
const gutter = ref(null);            // 旧 textarea 行号栏的引用（保留兼容，Monaco 自己画行号）
const cursor = ref({ line: 1, col: 1 });
const dark = computed(() => app.theme === "dark");

// Monaco 的语言 id（有高亮、括号匹配、注释规则；没有补全/诊断，避免把 LSP 线程也拖下来）。
const LANG_BY_EXT = {
  py: "python", pyi: "python", ipynb: "json",
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", tsx: "typescript",
  json: "json", jsonc: "json", md: "markdown", markdown: "markdown",
  html: "html", htm: "html", css: "css", scss: "scss", less: "less",
  c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", cu: "cpp", cuh: "cpp",
  sh: "shell", bash: "shell", zsh: "shell", ps1: "powershell",
  yml: "yaml", yaml: "yaml", toml: "ini", ini: "ini", cfg: "ini",
  xml: "xml", svg: "xml", sql: "sql", rs: "rust", go: "go", java: "java",
  tex: "latex", dockerfile: "dockerfile",
};
const editorLang = computed(() => {
  const name = (open.value.path || "").split("/").pop().toLowerCase();
  if (name === "dockerfile") return "dockerfile";
  const ext = name.includes(".") ? name.split(".").pop() : "";
  return LANG_BY_EXT[ext] || "plaintext";
});
const langLabel = computed(() => ({
  python: "Python", javascript: "JavaScript", typescript: "TypeScript", json: "JSON",
  markdown: "Markdown", html: "HTML", css: "CSS", scss: "SCSS", less: "Less",
  c: "C", cpp: "C++", shell: "Shell", powershell: "PowerShell", yaml: "YAML",
  ini: "INI", xml: "XML", sql: "SQL", rust: "Rust", go: "Go", java: "Java",
  latex: "LaTeX", dockerfile: "Dockerfile",
}[editorLang.value] || "Plain Text"));
const editorEl = ref(null);
const fileEl = ref(null);
const newName = ref("");
const loading = ref(false);
const needLogin = ref(false);
const viewer = ref(null);
const q = ref("");                    // 工作空间全文搜索
const hits = ref([]);
let searchTimer = null;
// 终端：跑一段代码（受限子进程 + 共享镜像 venv）
const term = reactive({ open: false, env: "lab", code: "", out: "", err: "", running: false, ms: 0,
                        exit: null, timedOut: false, inPlace: true, cwd: "llm-study-lab" });
const envList = ref([]);
// 后台任务：训练/长跑命令（start_job → job_status 轮询增量日志）
const jobs = reactive({ open: false, list: [], cmd: "python main.py doctor", env: "lab",
                        cwd: "llm-study-lab", out: "", active: null, offset: 0, running: false, timer: null });
// 知识库管理
const kb = reactive({ open: false, docs: [], count: 0, chunks: 0, busy: false,
                      form: { name: "", text: "", path: "", url: "" } });
// 窄屏：三栏挤不下，改成「文件 / 编辑 / agent」三选一（和 IDE 的手机版一致）
const small = ref(window.innerWidth <= 860);
const pane = ref("editor");
const onResize = () => { small.value = window.innerWidth <= 860; };
onMounted(() => window.addEventListener("resize", onResize));
onBeforeUnmount(() => { window.removeEventListener("resize", onResize); clearTimeout(jobs.timer); });
const wrap = ref(true);              // 散文默认折行；代码文件自动关掉折行（否则行号会对不上）
let saveTimer = null;

onMounted(async () => {
  await reload();
  loadEnvs();
  loadKb();
  loadJobs();
  openInitial();
});

watch(() => props.openJobs, (v) => {
  if (!v) return;
  jobs.open = true;
  loadJobs();
});

/** 从首页/agent 带过来的「打开某个文件」请求；没有就默认打开 Lab 的 START_HERE。 */
async function openInitial() {
  // 首页开新页面时把要打开的文件/面板放在 URL query 里，避免多 tab 抢同一个 localStorage 键
  const u = new URL(location.href);
  const qOpen = u.searchParams.get("open") || "";
  const qTab = u.searchParams.get("tab") || "";
  if (qOpen || qTab) {
    u.searchParams.delete("open"); u.searchParams.delete("tab");
    history.replaceState(null, "", u.pathname + (u.search ? u.search : "") + u.hash);
  }
  const want = qOpen || store.get("lab.open", "");
  if (want) { store.set("lab.open", ""); await openFile(want); return; }
  const tab = qTab || store.get("lab.tab", "");
  if (tab === "kb") { kb.open = true; store.set("lab.tab", ""); return; }
  if (!tabs.length) {
    const start = entries.value.find((e) => e.path === "llm-study-lab/START_HERE.md");
    if (start) openFile(start.path);
    else {
      const readme = entries.value.find((e) => e.path === "llm-study-lab/README.md");
      if (readme) openFile(readme.path);
    }
  }
}

async function onChanged() {
  await reload();
  await loadJobs();
}

// ---------------------------------------------------------------- 后台任务
async function loadJobs() {
  try { jobs.list = (await jobsList()).jobs || []; } catch (e) { /* 未登录时静默 */ }
}
function splitArgs(s) {
  return (String(s || "").match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || []).map((x) => x.replace(/^["']|["']$/g, ""));
}
async function startJob() {
  const args = splitArgs(jobs.cmd);
  if (!args.length) { toast("warn", "先写命令"); return; }
  jobs.running = true;
  try {
    const r = await jobStart({ env: jobs.env, args, cwd: jobs.cwd, timeout: 1800 });
    jobs.active = r.id; jobs.offset = 0; jobs.out = "";
    await loadJobs();
    pollJob(r.id);
  } catch (e) { toast("error", "启动失败", String(e.message || e), 8000); jobs.running = false; }
}
function pollJob(id) {
  clearTimeout(jobs.timer);
  const tick = async () => {
    try {
      const r = await jobStatus(id, jobs.offset);
      if (r.log) { jobs.out += r.log; jobs.offset = r.next_offset; }
      if (r.status !== "running") {
        jobs.running = false;
        jobs.out += `\n— 任务 ${r.status}，exit ${r.exit} —\n`;
        await loadJobs();
        return;
      }
      jobs.running = true;
      jobs.timer = setTimeout(tick, 1500);
    } catch (e) {
      jobs.running = false;
      toast("error", "读取任务日志失败", String(e.message || e));
    }
  };
  tick();
}
async function stopJob() {
  if (!jobs.active) return;
  try { await jobStop(jobs.active); jobs.out += "\n— 已发送停止信号 —\n"; } catch (e) { toast("error", "停止失败", String(e.message || e)); }
}
async function watchJob(j) {
  jobs.active = j.id; jobs.offset = 0; jobs.out = "";
  pollJob(j.id);
}

// ---------------------------------------------------------------- 知识库
async function loadKb() {
  try { const d = await kbList(); kb.docs = d.docs || []; kb.count = d.count || 0; kb.chunks = d.chunks || 0; }
  catch (e) { /* 静默 */ }
}
async function addKb() {
  const f = kb.form;
  if (!f.text.trim() && !f.path.trim() && !f.url.trim()) { toast("warn", "文本/文件/URL 至少填一个"); return; }
  kb.busy = true;
  try {
    const r = await kbAdd({ name: f.name, text: f.text || undefined, path: f.path || undefined, url: f.url || undefined });
    toast("success", "已存入知识库", `${r.name} · ${r.chunks} 块`, 4000);
    f.name = ""; f.text = ""; f.path = ""; f.url = "";
    await loadKb();
  } catch (e) { toast("error", "存入失败", String(e.message || e), 8000); }
  finally { kb.busy = false; }
}
async function rmKb(d) {
  if (!confirm(`从知识库删除「${d.name}」？`)) return;
  try { await kbDelete(d.id); await loadKb(); } catch (e) { toast("error", "删除失败", String(e.message || e)); }
}

/** 重置 Lab：旧副本先改名备份，再拷一份干净的模板。 */
async function resetLab() {
  if (!confirm("重置 LLM Lab？当前副本会被改名备份（llm-study-lab.bak-时间），然后拷一份干净模板。")) return;
  try {
    await labReset(true);
    toast("success", "Lab 已重置", "旧副本已备份");
    tabs.splice(0, tabs.length); activeIdx.value = -1;
    await reload();
    await openInitial();
  } catch (e) { toast("error", "重置失败", String(e.message || e), 8000); }
}

// 文件树里不该出现的东西：Python 缓存、依赖目录、系统垃圾。后端 ws_tree 也会跳（下次代理重启生效），
// 但前端必须先过滤 —— 否则一打开就是满屏 __pycache__ 和 .pyc，谁都看不懂。
const HIDE_DIRS = new Set(["__pycache__", "node_modules", ".git", ".venv", "venv", ".ipynb_checkpoints",
  ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache"]);
const HIDE_FILES = /(^|\/)\.DS_Store$|\.(pyc|pyo)$/i;
function visibleEntry(e) {
  const parts = String(e.path || "").split("/");
  if (parts.some((p) => p.startsWith(".") || HIDE_DIRS.has(p))) return false;
  return !HIDE_FILES.test(e.path || "");
}

async function reload() {
  loading.value = true;
  needLogin.value = false;
  try {
    const d = await wsTree();
    entries.value = (d.entries || []).filter(visibleEntry);
  } catch (e) {
    const msg = String(e.message || e);
    if (/401|登录|NOT_LOGGED_IN|token/i.test(msg)) needLogin.value = true;
    else toast("error", "读取工作空间失败", msg);
  } finally { loading.value = false; }
}

/** 扁平化的可见行（按折叠状态过滤），带层级用于缩进。 */
const rows = computed(() => {
  const tree = toTree(entries.value);
  const out = [];
  const walk = (nodes, depth) => {
    for (const n of nodes) {
      out.push({ ...n, depth });
      if (n.dir && !collapsed.has(n.path)) walk(n.children || [], depth + 1);
    }
  };
  walk(tree, 0);
  return out;
});

/** 文件树按类型给图标：图片、文档、其它各一档，扫一眼就知道是什么。 */
function iconOf(n) {
  if (n.dir) return collapsed.has(n.path) ? "layers" : "layers";
  const k = kindOf(n.path);
  return k === "image" ? "image" : k === "text" ? "attach" : "disk";
}

// 文件树里的类型徽标（VS Code 那种彩色文件图标的轻量替代）：两三个字母 + 按语言着色，
// 比统一的回形针图标好认，也不用额外下载图标字体。
const EXT_LABEL = {
  py: "PY", pyi: "PY", ipynb: "NB", js: "JS", mjs: "JS", cjs: "JS", jsx: "JSX",
  ts: "TS", tsx: "TSX", json: "{}", md: "MD", markdown: "MD",
  html: "<>", htm: "<>", css: "CSS", scss: "SCSS", less: "LESS",
  c: "C", h: "H", cpp: "C++", cc: "C++", cxx: "C++", hpp: "C++", cu: "CU", cuh: "CU",
  sh: "SH", bash: "SH", yml: "YML", yaml: "YML", toml: "TOML", ini: "INI",
  txt: "TXT", log: "LOG", csv: "CSV", xml: "XML", sql: "SQL",
  png: "IMG", jpg: "IMG", jpeg: "IMG", gif: "IMG", webp: "IMG", svg: "SVG", pdf: "PDF",
};
const extOf = (n) => {
  const s = String(n.name || "").split(".");
  return s.length > 1 ? s.pop().toLowerCase() : "";
};
const extLabel = (n) => EXT_LABEL[extOf(n)] || (extOf(n) ? extOf(n).slice(0, 3).toUpperCase() : "·");

function toggle(n) {
  if (!n.dir) return openFile(n.path);
  if (collapsed.has(n.path)) collapsed.delete(n.path); else collapsed.add(n.path);
}

async function openFile(path) {
  // 已经开着就直接切过去（多标签的核心：不再打断手上的工作）
  const at = tabs.findIndex((t2) => t2.path === path);
  if (at >= 0) { activeIdx.value = at; return; }
  try {
    const d = await readFile(path);
    tabs.push({ path, kind: kindOf(path), text: d.text || "", b64: d.b64 || "", dirty: false, savedAt: 0 });
    activeIdx.value = tabs.length - 1;
  } catch (e) { toast("error", "打开失败", String(e.message || e)); }
}

function closeTab(i) {
  const t2 = tabs[i];
  if (!t2) return;
  if (t2.dirty && !confirm(`${t2.path} 还有未保存修改，仍然关闭？`)) return;
  tabs.splice(i, 1);
  if (activeIdx.value >= tabs.length) activeIdx.value = tabs.length - 1;
  else if (i < activeIdx.value) activeIdx.value -= 1;
}

async function save({ silent = false } = {}) {
  const o = cur();
  if (!o.path || o.kind !== "text") return;
  try {
    await writeFile(o.path, o.text);
    o.dirty = false;
    o.savedAt = Date.now();
    if (!silent) { toast("success", "已保存", o.path, 1800); await reload(); }
  } catch (e) { toast("error", "保存失败", String(e.message || e)); }
}

/** 自动保存：停止输入 1.2s 后写盘（写文章时不用惦记 ⌘S）。 */
watch(() => open.value.text, () => {
  const o = cur();
  if (!o.dirty || !o.path || o.kind !== "text") return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => save({ silent: true }), 1200);
});

/** 打开文件时决定折行策略：代码不折行（保证行号对齐），散文折行。 */
watch(() => open.value.path, () => {
  wrap.value = !/\.(js|ts|py|json|css|html|sh|yaml|yml|toml|csv|log)$/i.test(open.value.path);
});

/** 工具栏插入：把选中的文字包起来，没选就插一对空标记并把光标放中间。 */
function wrapSel(before, after = before) {
  const el = editorEl.value, o = cur();
  if (!el) return;
  const s0 = el.selectionStart, s1 = el.selectionEnd;
  const sel = o.text.slice(s0, s1) || "";
  o.text = o.text.slice(0, s0) + before + sel + after + o.text.slice(s1);
  o.dirty = true;
  requestAnimationFrame(() => {
    el.focus();
    el.selectionStart = s0 + before.length;
    el.selectionEnd = s0 + before.length + sel.length;
  });
}
function prefixLine(mark) {
  const el = editorEl.value, o = cur();
  if (!el) return;
  const s0 = el.selectionStart;
  const lineStart = o.text.lastIndexOf("\n", s0 - 1) + 1;
  o.text = o.text.slice(0, lineStart) + mark + o.text.slice(lineStart);
  o.dirty = true;
  requestAnimationFrame(() => { el.focus(); el.selectionStart = el.selectionEnd = s0 + mark.length; });
}

/** 往工作空间上传文件：图片按 base64，文本按文本。做调研时把素材直接丢进来。 */
const BIN_EXT = /\.(png|jpe?g|webp|gif|pdf|docx|xlsx|xls|pptx|zip|gz|bin)$/i;
function readAsB64(f) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.onerror = rej;
    r.readAsDataURL(f);
  });
}
async function uploadFiles(e) {
  const files = [...(e.target.files || [])];
  for (const f of files) {
    const name = f.name || `file-${Date.now()}`;
    const path = `uploads/${name}`;
    try {
      if (f.type.startsWith("image/") || BIN_EXT.test(name)) {
        await writeFileBinary(path, await readAsB64(f));
      } else {
        await writeFile(path, await f.text());
      }
    } catch (err) { toast("error", `上传失败：${name}`, String(err.message || err)); }
  }
  e.target.value = "";
  await reload();
}

/** 知识库面板里直接传文档：先落到工作空间 uploads/，再让网关抽取正文/嵌入。 */
async function onKbFile(e) {
  const f = (e.target.files || [])[0];
  if (!f) return;
  const path = `uploads/${f.name}`;
  try {
    if (f.type.startsWith("image/") || BIN_EXT.test(f.name)) await writeFileBinary(path, await readAsB64(f));
    else await writeFile(path, await f.text());
    kb.form.path = path;
    kb.form.name = kb.form.name || f.name.replace(/\.[^.]+$/, "");
    await reload();
    toast("info", "文件已上传", `路径已填入：${path}`, 4000);
  } catch (err) { toast("error", "上传失败", String(err.message || err)); }
  finally { e.target.value = ""; }
}

async function del(path) {
  if (!confirm(`删除 ${path}？`)) return;
  try {
    await remove(path);
    // 关掉引用到被删文件的标签
    for (let i = tabs.length - 1; i >= 0; i--) {
      if (tabs[i].path === path || tabs[i].path.startsWith(path + "/")) closeTab(i);
    }
    await reload();
  } catch (e) { toast("error", "删除失败", String(e.message || e)); }
}

async function create() {
  const name = newName.value.trim();
  if (!name) { creating.value = null; return; }
  const base = open.path.includes("/") ? open.path.slice(0, open.path.lastIndexOf("/") + 1) : "";
  const path = base + name;
  try {
    if (creating.value === "dir") await mkdir(path);
    else { await writeFile(path, ""); await openFile(path); }
    creating.value = null; newName.value = "";
    await reload();
  } catch (e) { toast("error", "创建失败", String(e.message || e)); }
}

/** 把当前文件下载到本地：写好的文章、生成的图都能带走。 */
function downloadFile() {
  const o = cur();
  if (!o.path) return;
  let blob;
  if (o.kind === "image" && o.b64) {
    const bin = atob(o.b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    blob = new Blob([arr]);
  } else {
    blob = new Blob([o.text], { type: "text/plain;charset=utf-8" });
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = o.path.split("/").pop();
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/** 重命名：读 → 写新路径 → 删旧路径（文本与图片都走这条）。 */
async function renameFile() {
  const o = cur();
  if (!o.path) return;
  const from = o.path;
  const next = prompt("新路径（可在前面加目录，例如 notes/标题.md）", from);
  if (!next || next === from) return;
  try {
    if (o.kind === "image" && o.b64) await writeFileBinary(next, o.b64);
    else await writeFile(next, o.text);
    await remove(from);
    await reload();
    activeIdx.value = -1;
    await openFile(next);
    toast("success", "已重命名", next);
  } catch (e) { toast("error", "重命名失败", String(e.message || e)); }
}

/** 全文搜索：输入停 250ms 再查，点结果直接打开并跳到那一行。 */
function onSearch() {
  clearTimeout(searchTimer);
  const v = q.value.trim();
  if (!v) { hits.value = []; return; }
  searchTimer = setTimeout(async () => {
    try { hits.value = (await wsSearch(v)).hits || []; }
    catch (e) { hits.value = []; }
  }, 250);
}
async function openHit(hit) {
  await openFile(hit.path);
  await nextTick();
  gotoLine(hit.line);
}
/** 把光标移到第 n 行并滚动过去。 */
function gotoLine(n) {
  const el = editorEl.value;
  if (!el) return;
  const lines = open.value.text.split("\n");
  let pos = 0;
  for (let i = 0; i < Math.min(n - 1, lines.length); i++) pos += lines[i].length + 1;
  el.focus();
  el.selectionStart = el.selectionEnd = pos;
  el.scrollTop = Math.max(0, (n - 3) * 22);
}

async function loadEnvs() {
  try { envList.value = (await wsEnvs()).envs || []; } catch (e) { envList.value = []; }
}
async function runTerm(code) {
  if (term.running) return;
  const src = code != null ? code : term.code;
  if (!src.trim()) return;
  term.running = true; term.out = ""; term.err = ""; term.exit = null; term.timedOut = false;
  const t0 = performance.now();
  try {
    const opts = term.env === "lab"
      ? { in_place: term.inPlace, cwd: term.cwd || "llm-study-lab" }
      : {};
    const r = await wsRun(term.env, src, term.env === "lab" ? 300 : 60, opts);
    term.out = r.stdout || ""; term.err = r.stderr || "";
    term.exit = r.exit; term.timedOut = r.timed_out; term.ms = r.ms;
    // 代码写出来的文件已经回拷到 runs/，刷新一下文件树并在终端里说一声
    if (r.files && r.files.length) {
      await reload();
      term.out = `${term.out}${term.out ? "\n" : ""}— 产出 ${r.files.length} 个文件：${r.files.join("、")}`;
    }
  } catch (e) {
    term.err = String(e.message || e);
  } finally { term.running = false; }
}
/** 跑当前打开的文件：把正文塞进终端，环境按扩展名挑。 */
function runCurrentFile() {
  const o = cur();
  if (!o || !o.text) return;
  term.open = true;
  term.code = o.text;
  const inLab = o.path.startsWith("llm-study-lab/");
  term.env = /\.(cpp|cc|cxx|c)$/i.test(o.path) ? "cpp"
    : /\.(js|mjs|cjs)$/i.test(o.path) ? "node"
    : inLab || /torch|pytest|minillm/.test(o.text) ? "lab"
    : /(numpy|pandas|matplotlib)/.test(o.text) ? "ds" : "python";
  term.inPlace = term.env === "lab";
  term.cwd = inLab ? "llm-study-lab" : "";
  runTerm(o.text);
}

/** 把 agent 的回答插进当前文档：有光标就插在光标处，没有就接在文末。 */
function insertIntoDoc(text) {
  const o = cur();
  if (!o || o.kind !== "text" || !text) { toast("warn", "先打开一个文本文件", "左侧选一个 .md 再插入"); return; }
  const el = editorEl.value;
  const at = el ? el.selectionStart : (o.text || "").length;
  const body = o.text || "";
  const pad = at > 0 && !body.slice(0, at).endsWith("\n") ? "\n\n" : "";
  o.text = body.slice(0, at) + pad + text + body.slice(at);
  o.dirty = true;
  if (small.value) pane.value = "editor";
  nextTick(() => { if (el) { el.focus(); el.selectionStart = el.selectionEnd = at + pad.length + text.length; } });
  toast("success", "已插入", "记得检查并保存");
}

function onKey(e) {
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
  // ⌘/Ctrl+J 收起或展开终端；⌘/Ctrl+K 聚焦搜索
  if (mod && e.key.toLowerCase() === "j") { e.preventDefault(); term.open = !term.open; }
  if (mod && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (small.value) pane.value = "files";
    nextTick(() => document.querySelector(".ws__search input")?.focus());
  }
}

const imageSrc = computed(() => (open.value.b64 ? `data:${open.value.b64.startsWith("iVBOR") ? "image/png" : "image/jpeg"};base64,${open.value.b64}` : ""));
const isMd = computed(() => /\.(md|markdown)$/i.test(open.value.path));
/** 代码文件也能切「预览」（只读高亮），写代码时不用离开页面就能看颜色。 */
const isCode = computed(() => !!langOf(open.value.path));
const codeHtml = computed(() => highlight(open.value.text || "", open.value.path));
const canPreview = computed(() => isMd.value || isCode.value);
/** 只有「看起来是可执行代码」的文件才允许一键运行（Markdown/文本里点了只会报语法错）。 */
const runnable = computed(() => !!open.value.path && /[.](py|js|mjs|cjs|ts|sh|cpp|cc|cxx|c)$/i.test(open.value.path));

/** 写作要看的两个数：字数（中文按字算）与行数。 */
const stats = computed(() => {
  const t = open.value.text || "";
  const chars = t.length;
  const cjk = (t.match(/[\u4e00-\u9fff]/g) || []).length;
  const words = (t.replace(/[\u4e00-\u9fff]/g, " ").match(/[A-Za-z0-9_'-]+/g) || []).length + cjk;
  return { chars, words, lines: t ? t.split("\n").length : 0 };
});
const previewHtml = computed(() => markdown(open.value.text || "", { doc: true }));

function syncScroll(e) { if (gutter.value) gutter.value.scrollTop = e.target.scrollTop; }
function onTab(e) {
  // 在编辑器里 Tab 应该缩进，而不是跳走焦点
  e.preventDefault();
  const el = e.target, start = el.selectionStart, end = el.selectionEnd, o = cur();
  o.text = o.text.slice(0, start) + "  " + o.text.slice(end);
  o.dirty = true;
  requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = start + 2; });
}
</script>

<template>
  <section class="ws" :data-mobile="small ? '1' : null" @keydown="onKey">
    <div v-if="small" class="ws__mtabs" role="tablist">
      <button :data-on="pane === 'files'" @click="pane = 'files'">文件</button>
      <button :data-on="pane === 'editor'" @click="pane = 'editor'">编辑</button>
      <button :data-on="pane === 'agent'" @click="pane = 'agent'">agent</button>
    </div>
    <!-- ===================== 文件树 ===================== -->
    <aside class="ws__files" :data-hide="small && pane !== 'files' ? '1' : null">
      <header class="ws__files-head">
        <span class="card-title">LLM Lab</span>
        <span class="ws__actions">
          <button class="st-btn st-btn--icon" title="新建文件" @click="creating = 'file'; newName = ''">
            <Icon name="plus" cls="st-icon st-icon--sm" /></button>
          <button class="st-btn st-btn--icon" title="新建文件夹" @click="creating = 'dir'; newName = ''">
            <Icon name="layers" cls="st-icon st-icon--sm" /></button>
          <button class="st-btn st-btn--icon" title="上传文件" @click="fileEl.click()">
            <Icon name="download" cls="st-icon st-icon--sm" /></button>
          <button class="st-btn st-btn--icon" title="知识库" @click="kb.open = true; loadKb()">
            <Icon name="memory" cls="st-icon st-icon--sm" /></button>
          <button class="st-btn st-btn--icon" title="后台任务 / 训练" @click="jobs.open = true; loadJobs()">
            <Icon name="play" cls="st-icon st-icon--sm" /></button>
          <button class="st-btn st-btn--icon" title="重置 Lab（会先备份旧副本）" @click="resetLab">
            <Icon name="refresh" cls="st-icon st-icon--sm" /></button>
          <input ref="fileEl" type="file" multiple hidden @change="uploadFiles">
        </span>
      </header>

      <div v-if="needLogin" class="ws__login">
        <p class="muted small">登录后会自动创建你的 LLM Lab（干净副本）。</p>
        <button class="st-btn st-btn--primary" @click="openLogin()">登录</button>
      </div>

      <form v-if="creating" class="ws__new" @submit.prevent="create">
        <input v-model="newName" class="st-input" autofocus
               :placeholder="creating === 'dir' ? '文件夹名' : '文件名（如 notes.md）'">
        <button class="st-btn st-btn--primary" type="submit">建</button>
      </form>

      <div class="ws__search">
        <input v-model="q" class="st-input" placeholder="搜索工作空间内容…" @input="onSearch">
      </div>

      <ul v-if="q.trim()" class="ws__tree ws__hits">
        <li v-for="(h, i) in hits" :key="i" class="ws__hit" @click="openHit(h)">
          <span class="ws__hit-path">{{ h.path }}<span class="muted">{{ ":" + h.line }}</span></span>
          <span class="ws__hit-text">{{ h.text }}</span>
        </li>
        <li v-if="!hits.length" class="ws__empty muted small">没有匹配</li>
      </ul>
      <ul v-else class="ws__tree">
        <li v-for="n in rows" :key="n.path" class="ws__row" :class="{ 'ws__row--active': open.path === n.path }"
            :style="{ paddingLeft: 8 + n.depth * 14 + 'px' }" @click="toggle(n)">
          <Icon v-if="n.dir" :name="iconOf(n)" cls="st-icon st-icon--sm" />
          <span v-else class="ws__fico" :data-ext="extOf(n) || 'txt'">{{ extLabel(n) }}</span>
          <span class="ws__name">{{ n.name }}</span>
          <span v-if="!n.dir" class="ws__size muted">{{ n.size > 1024 ? fmt(n.size / 1024, 1) + 'K' : n.size + 'B' }}</span>
          <button v-if="!n.dir" class="ws__del" title="删除" @click.stop="del(n.path)">✕</button>
        </li>
        <li v-if="!rows.length && !loading" class="ws__empty">
          <p class="muted small">{{ t("ws.files.empty") }}</p>
          <button class="st-btn st-btn--secondary ws__start" @click="creating = 'file'; newName = 'article.md'">
            新建一篇文章
          </button>
        </li>
      </ul>
    </aside>

    <!-- ===================== 编辑器 ===================== -->
    <main class="ws__editor" :data-hide="small && pane !== 'editor' ? '1' : null">
      <!-- 多文件标签：切来切去不会丢未保存的内容 -->
      <div v-if="tabs.length" class="ws__tabs">
        <button v-for="(tb, i) in tabs" :key="tb.path" class="ws__tab" :data-on="i === activeIdx"
                :title="tb.path" @click="activeIdx = i">
          <span class="ws__tab-name">{{ tb.path.split("/").pop() }}</span>
          <span v-if="tb.dirty" class="ws__tab-dot">●</span>
          <span class="ws__tab-x" role="button" :aria-label="'关闭 ' + tb.path" @click.stop="closeTab(i)">✕</span>
        </button>
      </div>
      <header v-if="open.path" class="ws__ed-head">
        <span class="ws__path">{{ open.path }}<span v-if="open.dirty" class="ws__dirty">●</span></span>
        <span class="ws__ed-actions">
          <button v-if="open.kind === 'text'" class="st-btn st-btn--primary" @click="save">保存</button>
          <button class="st-btn st-btn--secondary" @click="renameFile">重命名</button>
          <button class="st-btn st-btn--secondary" @click="downloadFile">下载</button>
          <button class="st-btn st-btn--secondary" @click="del(open.path)">删除</button>
        </span>
      </header>

      <div v-if="!open.path" class="ws__placeholder">
        <Icon name="layers" cls="st-icon" />
        <p class="muted">{{ t("ws.editor.empty") }}</p>
      </div>

      <template v-else-if="open.kind === 'text'">
        <div class="ws__bar">
          <span class="muted small">{{ stats.words }} 字 · {{ stats.lines }} 行</span>
          <span v-if="open.dirty" class="ws__dirty">● 编辑中…</span>
          <span v-else class="ws__saved muted small">已保存</span>
          <span class="ws__spacer"></span>
          <button class="ws__tool" :title="wrap ? '关闭折行' : '开启折行'" @click="wrap = !wrap">折行</button>
          <span v-if="isMd" class="ws__mdbar">
            <button class="ws__tool" title="标题" @click="prefixLine('## ')">H</button>
            <button class="ws__tool" title="粗体" @click="wrapSel('**')"><b>B</b></button>
            <button class="ws__tool" title="斜体" @click="wrapSel('*')"><i>I</i></button>
            <button class="ws__tool" title="列表" @click="prefixLine('- ')">≡</button>
            <button class="ws__tool" title="引用" @click="prefixLine('> ')">❝</button>
            <button class="ws__tool" title="代码" @click="wrapSel('`')">&lt;/&gt;</button>
            <button class="ws__tool" title="链接" @click="wrapSel('[', '](https://)')">link</button>
          </span>
          <div v-if="canPreview" class="ws__seg" role="tablist">
            <button :data-on="view === 'edit'" @click="view = 'edit'">编辑</button>
            <button :data-on="view === 'split'" @click="view = 'split'">分栏</button>
            <button :data-on="view === 'preview'" @click="view = 'preview'">预览</button>
          </div>
        </div>
        <div class="ws__work" :data-view="canPreview ? view : 'edit'">
          <div v-if="!canPreview || view !== 'preview'" class="ws__code">
            <CodeEditor :key="open.path" v-model="open.text" :language="editorLang" :dark="dark" :wrap="wrap"
                        @update:model-value="open.dirty = true" @cursor="cursor = $event" @save="save" />
          </div>
          <div v-if="isMd && view !== 'edit'" class="ws__preview" v-html="previewHtml"></div>
          <div v-else-if="isCode && view !== 'edit'" class="ws__preview ws__preview--code">
            <pre><code v-html="codeHtml"></code></pre>
          </div>
        </div>
        <!-- 状态栏：和 VS Code 一样，光标位置 / 语言 / 编码 / 缩进放在最下面一行 -->
        <div class="ws__status">
          <span class="ws__status-i">{{ langLabel }}</span>
          <span class="ws__status-i">Ln {{ cursor.line }}, Col {{ cursor.col }}</span>
          <span class="ws__status-i">{{ stats.lines }} 行 · {{ stats.words }} 字</span>
          <span class="ws__spacer"></span>
          <span class="ws__status-i" :data-dirty="open.dirty ? '1' : null">{{ open.dirty ? "未保存" : "已保存" }}</span>
          <span class="ws__status-i">Spaces: 4</span>
          <span class="ws__status-i">UTF-8</span>
        </div>
      </template>

      <div v-else-if="open.kind === 'image'" class="ws__image">
        <img :src="imageSrc" :alt="open.path" title="点击查看大图 / 下载" style="cursor: zoom-in"
             @click="viewer = { src: imageSrc, name: open.path.split('/').pop() }">
      </div>

      <div v-else class="ws__placeholder">
        <p class="muted">这个文件类型不支持预览。</p>
      </div>

      <!-- ===================== 终端：跑一段代码 ===================== -->
      <div class="ws__termbar">
        <button class="ws__tool" @click="term.open = !term.open">{{ term.open ? "收起终端" : "终端" }}</button>
        <span class="muted small">受限子进程运行（30s / 2 GiB / 20 MiB 写盘上限），工作目录是临时的</span>
      </div>
      <section v-if="term.open" class="ws__term">
        <div class="ws__term-head">
          <select v-model="term.env" class="st-input ws__term-env">
            <option v-for="e in envList" :key="e.id" :value="e.id">{{ e.label }}</option>
          </select>
          <button class="st-btn st-btn--primary" :disabled="term.running" @click="runTerm()">
            {{ term.running ? "运行中…" : "运行" }}</button>
          <button class="st-btn st-btn--secondary" :disabled="term.running || !runnable" @click="runCurrentFile"
                  :title="runnable ? '把当前文件内容交给终端运行' : '这个文件不是可运行的代码'">
            跑当前文件</button>
          <span class="muted small ws__term-status" v-if="term.exit !== null">
            exit {{ term.exit }} · {{ term.ms }} ms<template v-if="term.timedOut"> · 超时被终止</template>
          </span>
        </div>
        <textarea v-model="term.code" class="ws__term-in" spellcheck="false" rows="5"
                  placeholder="在这里写代码，Ctrl/⌘+Enter 运行" @keydown.ctrl.enter.prevent="runTerm()" @keydown.meta.enter.prevent="runTerm()"></textarea>
        <pre v-if="term.out" class="ws__term-out">{{ term.out }}</pre>
        <pre v-if="term.err" class="ws__term-out ws__term-err">{{ term.err }}</pre>
      </section>
    </main>

    <!-- ===================== agent ===================== -->
    <AgentPanel :data-hide="small && pane !== 'agent' ? '1' : null" :open-path="open.path" :open-text="open.text"
                @changed="onChanged" @open-file="openFile" @insert="insertIntoDoc" />

    <!-- ===================== 后台任务（训练/长跑） ===================== -->
    <div v-if="jobs.open" class="ws-modal" @click.self="jobs.open = false">
      <section class="st-card ws-modal__panel">
        <header class="ws-modal__head">
          <span class="card-title">后台任务 · 训练 / 长跑</span>
          <button class="st-btn st-btn--icon" @click="jobs.open = false">✕</button>
        </header>
        <div class="ws-job-form">
          <select v-model="jobs.env" class="st-input">
            <option v-for="e in envList" :key="e.id" :value="e.id">{{ e.label }}</option>
          </select>
          <input v-model="jobs.cwd" class="st-input" placeholder="工作目录（默认 llm-study-lab）">
          <input v-model="jobs.cmd" class="st-input ws-job-cmd" placeholder="python main.py doctor">
          <button class="st-btn st-btn--primary" :disabled="jobs.running" @click="startJob">
            {{ jobs.running ? "运行中…" : "启动" }}</button>
          <button class="st-btn st-btn--secondary" :disabled="!jobs.active || !jobs.running" @click="stopJob">停止</button>
        </div>
        <p class="muted small">命令在用户自己的工作目录里跑；GPU 压力高时会拒绝启动（可让 agent 用 force 重试）。
          训练建议：<code>python main.py train --steps 500</code>。</p>
        <div class="ws-job-list">
          <button v-for="j in jobs.list.slice(0, 12)" :key="j.id" class="ws-job-item" :data-on="j.id === jobs.active"
                  @click="watchJob(j)">
            <span class="ws-job-id">{{ j.id }}</span>
            <span class="ws-job-cmd2">{{ (j.args || []).join(" ") }}</span>
            <span class="home-badge" :data-on="j.status === 'running' ? '1' : '0'">{{ j.status }}</span>
          </button>
          <p v-if="!jobs.list.length" class="muted small">还没有任务。</p>
        </div>
        <pre v-if="jobs.out" class="ws-job-out">{{ jobs.out }}</pre>
      </section>
    </div>

    <!-- ===================== 知识库 ===================== -->
    <div v-if="kb.open" class="ws-modal" @click.self="kb.open = false">
      <section class="st-card ws-modal__panel">
        <header class="ws-modal__head">
          <span class="card-title">我的知识库 <span class="muted small">（{{ kb.count }} 份 / {{ kb.chunks }} 块）</span></span>
          <button class="st-btn st-btn--icon" @click="kb.open = false">✕</button>
        </header>
        <div class="ws-kb-form">
          <input v-model="kb.form.name" class="st-input" placeholder="资料名（可留空）">
          <div class="ws-kb-file">
            <input v-model="kb.form.path" class="st-input" placeholder="工作空间文件路径（如 uploads/讲义.pdf）">
            <label class="st-btn st-btn--secondary ws-kb-upload">上传文档
              <input type="file" accept=".pdf,.docx,.xlsx,.xls,.csv,.md,.txt,.html" hidden @change="onKbFile"></label>
          </div>
          <input v-model="kb.form.url" class="st-input" placeholder="或网页 URL">
          <textarea v-model="kb.form.text" class="st-input" rows="4" placeholder="或直接粘贴文本"></textarea>
          <button class="st-btn st-btn--primary" :disabled="kb.busy" @click="addKb">
            {{ kb.busy ? "嵌入中…" : "存入知识库" }}</button>
        </div>
        <ul class="ws-kb-list">
          <li v-for="d in kb.docs" :key="d.id">
            <b>{{ d.name }}</b>
            <span class="muted small">{{ d.source }} · {{ d.chunks }} 块</span>
            <button class="home-x" @click="rmKb(d)">✕</button>
          </li>
          <p v-if="!kb.docs.length" class="muted small">知识库还是空的。上传/粘贴资料后，agent 回答前会自动检索。</p>
        </ul>
      </section>
    </div>

    <ImageViewer v-if="viewer" :src="viewer.src" :name="viewer.name" @close="viewer = null" />
  </section>
</template>

<style scoped>
.ws { flex: 1; min-width: 0; display: grid; grid-template-columns: 240px minmax(0, 1fr) 420px; height: 100%; min-height: 0; }
@media (max-width: 1200px) { .ws { grid-template-columns: 200px minmax(0, 1fr) 340px; } }
/* 窄屏：一次只显示一栏（文件 / 编辑 / agent），顶上给切换条 */
.ws__mtabs { grid-column: 1 / -1; display: flex; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--st-line);
             background: var(--st-surface); }
.ws__mtabs button { flex: 1 1 0; height: 34px; border: 1px solid var(--st-line); border-radius: var(--st-r-pill);
                    background: var(--st-surface); color: var(--st-ink-muted); font-size: var(--st-fs-sm); cursor: pointer; }
.ws__mtabs button[data-on="true"] { background: var(--st-accent); border-color: var(--st-accent); color: #fff; font-weight: var(--st-fw-bold); }
.ws[data-mobile] { grid-template-columns: 1fr; grid-template-rows: auto minmax(0, 1fr); }
.ws [data-hide] { display: none !important; }
.ws[data-mobile] .ws__files, .ws[data-mobile] .ws__editor { border: none; }
.ws[data-mobile] .agent { border-left: none; }

.ws__files { display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--st-line);
             background: var(--st-surface); }
.ws__files-head { display: flex; align-items: center; justify-content: space-between; gap: 4px; padding: 10px 10px;
                  border-bottom: 1px solid var(--st-line); flex: none; }
.ws__files-head .card-title { white-space: nowrap; font-size: var(--st-fs-sm); overflow: hidden; text-overflow: ellipsis; }
.ws__actions { display: flex; gap: 0; flex: none; }
.ws__actions .st-btn--icon { width: 26px; height: 26px; }
.ws__login { display: grid; gap: 8px; padding: 12px 10px; border-bottom: 1px solid var(--st-line); }
.ws__login .st-btn { height: 34px; }
.ws__new { display: flex; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--st-line); }
.ws__new .st-input { height: 30px; }
.ws__search { padding: 8px 10px; border-bottom: 1px solid var(--st-line); flex: none; }
.ws__search .st-input { height: 30px; font-size: 12.5px; }
.ws__hit { display: grid; gap: 2px; padding: 6px 10px; cursor: pointer; border-radius: 6px; margin: 0 6px; }
.ws__hit:hover { background: var(--st-surface-2); }
.ws__hit-path { font-size: 11px; color: var(--st-accent); }
.ws__hit-text { font-size: 12px; color: var(--st-ink-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ws__tree { list-style: none; margin: 0; padding: 6px 0; overflow: auto; flex: 1; min-height: 0; }
.ws__row { display: flex; align-items: center; gap: 6px; padding: 5px 10px 5px 8px; cursor: pointer;
           border-radius: 6px; margin: 0 6px; color: var(--st-ink-soft); font-size: var(--st-fs-sm); }
.ws__row:hover { background: var(--st-surface-2); }
.ws__row--active { background: var(--st-surface-2); color: var(--st-ink); font-weight: var(--st-fw-medium); }
.ws__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
/* 文件类型徽标：字母 + 语言色。尺寸固定，长后缀（SCSS/TOML）也放得下 */
.ws__fico { flex: none; display: inline-flex; align-items: center; justify-content: center;
            width: 24px; height: 16px; border-radius: 4px; font-size: 8.5px; font-weight: 800;
            letter-spacing: -.02em; color: var(--st-ink-muted); background: var(--st-surface-2); }
.ws__fico[data-ext="py"] { color: #3572a5; background: #3572a518; }
.ws__fico[data-ext="js"], .ws__fico[data-ext="mjs"], .ws__fico[data-ext="cjs"],
.ws__fico[data-ext="jsx"] { color: #b58a00; background: #d4a72c18; }
.ws__fico[data-ext="ts"], .ws__fico[data-ext="tsx"] { color: #3178c6; background: #3178c618; }
.ws__fico[data-ext="json"] { color: #b45309; background: #b4530918; }
.ws__fico[data-ext="md"], .ws__fico[data-ext="markdown"] { color: #4f6e8c; background: #4f6e8c18; }
.ws__fico[data-ext="html"], .ws__fico[data-ext="htm"] { color: #d9480f; background: #d9480f18; }
.ws__fico[data-ext="css"], .ws__fico[data-ext="scss"], .ws__fico[data-ext="less"] { color: #264de4; background: #264de418; }
.ws__fico[data-ext="c"], .ws__fico[data-ext="h"] { color: #5c6bc0; background: #5c6bc018; }
.ws__fico[data-ext="cpp"], .ws__fico[data-ext="cc"], .ws__fico[data-ext="cxx"],
.ws__fico[data-ext="hpp"], .ws__fico[data-ext="cu"], .ws__fico[data-ext="cuh"] { color: #00599c; background: #00599c18; }
.ws__fico[data-ext="sh"], .ws__fico[data-ext="bash"] { color: #2f8f46; background: #2f8f4618; }
.ws__fico[data-ext="yml"], .ws__fico[data-ext="yaml"] { color: #cb171e; background: #cb171e14; }
.ws__fico[data-ext="png"], .ws__fico[data-ext="jpg"], .ws__fico[data-ext="jpeg"], .ws__fico[data-ext="gif"],
.ws__fico[data-ext="webp"], .ws__fico[data-ext="svg"] { color: #8b5cf6; background: #8b5cf618; }
.ws__fico[data-ext="pdf"] { color: #dc2626; background: #dc262618; }
.ws__size { font-variant-numeric: tabular-nums; }
.ws__del { border: none; background: none; color: var(--st-ink-muted); cursor: pointer; opacity: 0; }
.ws__row:hover .ws__del { opacity: 1; }
.ws__empty { padding: 12px; display: grid; gap: 8px; }
.ws__start { height: 32px; }

.ws__editor { display: flex; flex-direction: column; min-height: 0; background: var(--st-bg); }
.ws__tabs { display: flex; gap: 2px; padding: 6px 8px 0; overflow-x: auto; background: var(--st-surface);
             border-bottom: 1px solid var(--st-line); flex: none; }
.ws__tab { display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 8px 0 12px; flex: none;
           border: 1px solid transparent; border-bottom: none; border-radius: 8px 8px 0 0; background: none;
           color: var(--st-ink-muted); font-size: var(--st-fs-sm); cursor: pointer; }
.ws__tab:hover { background: var(--st-surface-2); }
.ws__tab[data-on="true"] { background: var(--st-bg); border-color: var(--st-line); color: var(--st-ink); font-weight: var(--st-fw-medium); }
.ws__tab-dot { color: var(--st-accent); font-size: 9px; }
.ws__tab-x { width: 16px; height: 16px; display: inline-flex; align-items: center; justify-content: center;
             border-radius: 4px; font-size: 10px; opacity: .55; }
.ws__tab-x:hover { background: var(--st-line); opacity: 1; }
.ws__ed-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 14px;
               border-bottom: 1px solid var(--st-line); background: var(--st-surface); flex: none; }
.ws__path { font-family: var(--st-font-mono); font-size: var(--st-fs-sm); color: var(--st-ink-soft); }
.ws__dirty { color: var(--st-accent); margin-left: 6px; }
.ws__ed-actions { display: flex; gap: 8px; }
.ws__bar { display: flex; align-items: center; gap: 10px; padding: 6px 10px; border-bottom: 1px solid var(--st-line);
           background: var(--st-surface); flex: none; }
.ws__spacer { flex: 1; }
.ws__seg { display: inline-flex; gap: 2px; padding: 2px; border-radius: 8px; background: var(--st-surface-2); }
.ws__seg button { height: 26px; padding: 0 12px; border: none; background: none; border-radius: 6px; cursor: pointer;
                  font-size: 12px; color: var(--st-ink-muted); }
.ws__seg button[data-on="true"] { background: var(--st-surface); color: var(--st-ink); font-weight: var(--st-fw-bold); }
.ws__work { flex: 1; min-height: 0; display: grid; grid-template-columns: 1fr; }
.ws__work[data-view="split"] { grid-template-columns: 1fr 1fr; }
/* 编辑器下面的状态栏（VS Code 风格）：语言 / 光标 / 行字 / 保存态 / 缩进 / 编码 */
.ws__status { flex: none; display: flex; align-items: center; gap: 12px; height: 24px; padding: 0 10px;
              border-top: 1px solid var(--st-line); background: var(--st-surface);
              color: var(--st-ink-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.ws__status-i { white-space: nowrap; }
.ws__status-i[data-dirty] { color: var(--st-accent-text); font-weight: var(--st-fw-bold); }
.ws__code { display: flex; min-height: 0; min-width: 0; overflow: hidden; }
.ws__gutter { margin: 0; padding: 16px 6px 16px 12px; overflow: hidden; text-align: right; user-select: none;
              font: 400 13px/1.7 var(--st-font-mono); color: var(--st-ink-muted); opacity: .5; flex: none;
              border-right: 1px solid var(--st-line-soft); background: var(--st-surface); display: flex; flex-direction: column; }
.ws__preview { min-height: 0; overflow: auto; padding: 18px 24px; border-left: 1px solid var(--st-line); line-height: 1.75; }
.ws__preview :deep(:first-child) { margin-top: 0; }
.ws__preview :deep(h1), .ws__preview :deep(h2), .ws__preview :deep(h3) { line-height: 1.35; margin: 18px 0 8px; }
.ws__preview :deep(h1) { font-size: 1.7em; } .ws__preview :deep(h2) { font-size: 1.3em; } .ws__preview :deep(h3) { font-size: 1.1em; }
.ws__preview :deep(p) { margin: 0 0 10px; }
.ws__preview :deep(ul), .ws__preview :deep(ol) { margin: 0 0 10px; padding-left: 22px; }
.ws__preview :deep(code) { padding: 1px 5px; border-radius: 5px; background: var(--st-surface-2); font-family: var(--st-font-mono), monospace; font-size: .92em; }
.ws__preview :deep(pre) { padding: 12px; border-radius: 10px; background: var(--st-surface-2); overflow: auto; }
.ws__preview :deep(pre code) { padding: 0; background: none; }
.ws__preview :deep(table) { border-collapse: collapse; margin: 10px 0; }
.ws__preview :deep(th), .ws__preview :deep(td) { border: 1px solid var(--st-line); padding: 6px 10px; }
.ws__preview :deep(blockquote) { margin: 10px 0; padding-left: 12px; border-left: 3px solid var(--st-line); color: var(--st-ink-muted); }
.ws__preview :deep(img) { max-width: 100%; border-radius: 10px; }
.ws__saved { opacity: .7; }
.ws__mdbar { display: inline-flex; gap: 2px; margin-right: 8px; }
.ws__tool { height: 26px; min-width: 28px; padding: 0 8px; border: 1px solid transparent; border-radius: 6px;
            background: none; color: var(--st-ink-muted); font-size: 12px; cursor: pointer; }
.ws__tool:hover { background: var(--st-surface-2); color: var(--st-ink); }
.ws__text--nowrap { white-space: pre; overflow-x: auto; }
.ws__preview--code { padding: 16px 20px; background: var(--st-bg); }
.ws__preview--code pre { margin: 0; padding: 0; background: none; font: 400 13px/1.7 var(--st-font-mono), monospace; }
.ws__preview--code code { background: none; padding: 0; font-size: inherit; }
/* 高亮配色：只分四类，明暗两套都读得清 */
.ws__preview--code :deep(.hl-k) { color: #7c3aed; font-weight: 600; }
.ws__preview--code :deep(.hl-s) { color: #16a34a; }
.ws__preview--code :deep(.hl-n) { color: #b45309; }
.ws__preview--code :deep(.hl-c) { color: var(--st-ink-muted); font-style: italic; }
:global(html[data-theme="dark"]) .ws__preview--code :deep(.hl-k) { color: #c4b5fd; }
:global(html[data-theme="dark"]) .ws__preview--code :deep(.hl-s) { color: #86efac; }
:global(html[data-theme="dark"]) .ws__preview--code :deep(.hl-n) { color: #fbbf24; }
.ws__text { flex: 1; min-height: 0; width: 100%; border: none; outline: none; resize: none; padding: 16px 20px;
            background: var(--st-bg); color: var(--st-ink); font-size: 13.5px;
            font-family: var(--st-font-mono), "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
            line-height: 1.7; }
.ws__image { flex: 1; min-height: 0; display: grid; place-items: center; padding: 20px; overflow: auto; }
.ws__image img { max-width: 100%; max-height: 100%; border-radius: 12px; border: 1px solid var(--st-line); }
.ws__placeholder { flex: 1; display: grid; place-items: center; gap: 10px; align-content: center; color: var(--st-ink-muted); }
.ws__placeholder .st-icon { width: 40px; height: 40px; opacity: .35; }

/* 终端 */
.ws__termbar { display: flex; align-items: center; gap: 10px; padding: 4px 10px; border-top: 1px solid var(--st-line);
               background: var(--st-surface); flex: none; }
.ws__term { flex: none; display: flex; flex-direction: column; gap: 6px; padding: 8px 10px 10px;
            border-top: 1px solid var(--st-line); background: var(--st-bg); max-height: 42vh; overflow: auto; }
.ws__term-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ws__term-env { height: 30px; width: auto; min-width: 160px; }
.ws__term-status { font-variant-numeric: tabular-nums; }
.ws__term-in { width: 100%; min-height: 92px; resize: vertical; padding: 8px 10px; border: 1px solid var(--st-line);
               border-radius: 8px; background: var(--st-surface); color: var(--st-ink); font: 400 12.5px/1.6 var(--st-font-mono), monospace; }
.ws__term-out { margin: 0; padding: 8px 10px; border-radius: 8px; background: #0f172a0d; white-space: pre-wrap;
                overflow-wrap: anywhere; font: 400 12px/1.55 var(--st-font-mono), monospace; max-height: 240px; overflow: auto; }
.ws__term-err { color: var(--st-danger, #dc2626); background: color-mix(in srgb, var(--st-danger, #dc2626) 7%, transparent); }

/* ---- 后台任务 / 知识库弹层 ---- */
.ws-modal { position: fixed; inset: 0; z-index: 60; background: rgba(15,23,42,.45); display: grid; place-items: center;
            padding: var(--st-s-4); }
.ws-modal__panel { width: min(920px, 100%); max-height: 86vh; overflow: auto; display: grid; gap: 12px;
                   padding: var(--st-s-4); }
.ws-modal__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.ws-job-form { display: grid; grid-template-columns: 170px 190px minmax(200px, 1fr) auto auto; gap: 8px; align-items: center; }
.ws-job-form .st-input { height: 34px; }
.ws-job-list { display: grid; gap: 6px; max-height: 220px; overflow: auto; }
.ws-job-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border: 1px solid var(--st-line);
               border-radius: 8px; background: var(--st-surface); cursor: pointer; font: inherit; text-align: left; }
.ws-job-item[data-on="1"] { border-color: var(--st-accent); background: var(--st-accent-tint); }
.ws-job-id { font-family: var(--st-font-mono); font-size: 11px; color: var(--st-ink-muted); flex: none; }
.ws-job-cmd2 { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12.5px; }
.home-badge { font-size: 11px; padding: 2px 8px; border-radius: var(--st-r-pill); background: var(--st-surface-2);
              color: var(--st-ink-muted); border: 1px solid var(--st-line); flex: none; }
.home-badge[data-on="1"] { color: var(--st-success-text, #15803d); border-color: color-mix(in srgb, #16a34a 40%, transparent); }
.ws-job-out { margin: 0; padding: 10px; border-radius: 8px; background: #0f172a0d; max-height: 320px; overflow: auto;
              white-space: pre-wrap; overflow-wrap: anywhere; font: 400 12px/1.55 var(--st-font-mono), monospace; }
.ws-kb-form { display: grid; grid-template-columns: 1fr 1.2fr 1.2fr; gap: 8px; }
.ws-kb-form textarea { grid-column: 1 / -1; resize: vertical; }
.ws-kb-form .st-btn { grid-column: 1 / -1; justify-self: start; }
.ws-kb-form .st-input { height: 34px; }
.ws-kb-file { display: flex; gap: 6px; align-items: center; min-width: 0; }
.ws-kb-file .st-input { flex: 1; min-width: 0; }
.ws-kb-upload { white-space: nowrap; height: 34px; display: inline-flex; align-items: center; cursor: pointer; }
.ws-kb-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.ws-kb-list li { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 8px;
                 background: var(--st-surface-2); }
.ws-kb-list li b { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.home-x { border: 0; background: none; color: var(--st-ink-muted); cursor: pointer; padding: 0 4px; }
.home-x:hover { color: var(--st-danger-text, #dc2626); }
@media (max-width: 860px) {
  .ws-job-form, .ws-kb-form { grid-template-columns: 1fr; }
  .ws-modal { padding: 0; align-items: end; }
  .ws-modal__panel { width: 100%; max-height: 92vh; border-radius: 20px 20px 0 0;
                     padding: 14px 16px calc(16px + env(safe-area-inset-bottom)); }
  .ws-kb-form .st-btn { justify-self: stretch; }
}
</style>