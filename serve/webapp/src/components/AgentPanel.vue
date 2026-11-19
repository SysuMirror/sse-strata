<script setup>
// 右侧 coding agent：一个会话、一组工具、一个工作空间。
//
// 它不是「体验面板」——生图/向量/精排/TTS 不再各占一个 tab，而是 agent 的工具：
//   · 文件：list/read/write/edit/search/delete
//   · 代码：run_code（python/ds/node/cpp/lab）+ 后台任务（训练/长跑）
//   · 知识库：kb_add / kb_search（bge-m3 召回 + bge-reranker 精排）
//   · 生图：generate_image（参考图直接当底图）
//   · 语音：回答自动用胡桃音色朗读（可关）
//   · 联网：search_web / fetch_url
import { ref, reactive, computed, nextTick, watch, onMounted, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";
import { headers, toast, store } from "../api.js";
import { searchWeb, fetchStatus, generateImage, ttsSay, imageSearch, urlToDataURL } from "../hub.js";
import { tree as wsTree, readFile as wsRead, writeFile as wsWrite, remove as wsRemove,
         search as wsSearch, runCode as wsRun, jobStart, jobStatus, jobStop,
         kbAdd, kbSearch, kbList } from "../workspace.js";
import Icon from "./Icon.vue";
import ToolCard from "./ToolCard.vue";
import ImageViewer from "./ImageViewer.vue";
import { markdown } from "../markdown.js";

const { t } = useI18n();
const props = defineProps({
  openPath: { type: String, default: "" },
  openText: { type: String, default: "" },
});
const emit = defineEmits(["changed", "open-file", "insert"]);

const MODELS = [
  { id: "qwen3.8-flash", label: "普道 · 长上下文" },
  { id: "qwen3.8-flash-fast", label: "快道 · 低延迟" },
];
const model = ref(store.get("ws.model", "qwen3.8-flash"));
watch(model, (v) => store.set("ws.model", v));

const autoSpeak = ref(store.get("ws.autoSpeak", true));
watch(autoSpeak, (v) => store.set("ws.autoSpeak", !!v));
const autoKb = ref(store.get("ws.autoKb", true));
watch(autoKb, (v) => store.set("ws.autoKb", !!v));

const md = (x) => markdown(x || "");
const msgs = reactive(store.get("ws.msgs", []));
const input = ref("");
const busy = ref(false);
const scroller = ref(null);
const MAX_ROUNDS = 8;
let ctl = null;
const kbState = reactive({ count: 0, chunks: 0, docs: [] });

watch(msgs, (v) => { store.set("ws.msgs", v.slice(-60)); scrollDown(); }, { deep: true });

const TOOLS = [
  { type: "function", function: { name: "list_files", description: "列出工作空间文件（可给子目录 path）",
    parameters: { type: "object", properties: { path: { type: "string" } } } } },
  { type: "function", function: { name: "read_file", description: "读取工作空间里的一个文本文件",
    parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } } },
  { type: "function", function: { name: "write_file", description: "写/覆盖一个文件（自动建目录）",
    parameters: { type: "object", properties: { path: { type: "string" }, text: { type: "string" } }, required: ["path", "text"] } } },
  { type: "function", function: { name: "edit_file", description: "把文件里的 old_text 替换成 new_text（精确匹配，先读再改）",
    parameters: { type: "object", properties: { path: { type: "string" }, old_text: { type: "string" }, new_text: { type: "string" } }, required: ["path", "old_text", "new_text"] } } },
  { type: "function", function: { name: "delete_path", description: "删除文件或目录（只在用户明确要求时用）",
    parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } } },
  { type: "function", function: { name: "search_files", description: "在工作空间里全文搜索",
    parameters: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } } },
  { type: "function", function: { name: "run_code", description:
    "跑一段代码/小项目并拿回 stdout/stderr。env: python（标准库）/ ds（numpy/pandas/matplotlib）/ node / cpp（g++ 编译运行）/ lab（PyTorch+CUDA）。改完 lab 里的代码要验证时用 in_place=true, cwd=llm-study-lab",
    parameters: { type: "object", properties: {
      env: { type: "string" }, code: { type: "string" }, timeout: { type: "number" },
      in_place: { type: "boolean" }, cwd: { type: "string" }, force: { type: "boolean" },
      files: { type: "object", description: "多文件项目：{相对路径: 内容}" } }, required: ["code"] } } },
  { type: "function", function: { name: "start_job", description:
    "启动后台任务（训练/长跑），立刻返回 job id。args 是参数数组，例如 [\"python\",\"main.py\",\"train\",\"--steps\",\"500\"]；env 用 lab",
    parameters: { type: "object", properties: {
      args: { type: "array", items: { type: "string" } }, env: { type: "string" },
      cwd: { type: "string" }, timeout: { type: "number" }, force: { type: "boolean" } }, required: ["args"] } } },
  { type: "function", function: { name: "job_status", description: "看后台任务的状态与增量日志",
    parameters: { type: "object", properties: { id: { type: "string" }, offset: { type: "number" } }, required: ["id"] } } },
  { type: "function", function: { name: "job_stop", description: "停止后台任务",
    parameters: { type: "object", properties: { id: { type: "string" } }, required: ["id"] } } },
  { type: "function", function: { name: "kb_add", description: "把文本/工作空间文件/网页存进个人知识库",
    parameters: { type: "object", properties: { name: { type: "string" }, text: { type: "string" },
      path: { type: "string" }, url: { type: "string" } } } } },
  { type: "function", function: { name: "kb_search", description: "检索个人知识库（向量召回 + 精排）",
    parameters: { type: "object", properties: { q: { type: "string" }, k: { type: "number" } }, required: ["q"] } } },
  { type: "function", function: { name: "kb_list", description: "列出知识库里的资料", parameters: { type: "object", properties: {} } } },
  { type: "function", function: { name: "search_web", description: "联网搜索",
    parameters: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } } },
  { type: "function", function: { name: "fetch_url", description: "打开网页读正文",
    parameters: { type: "object", properties: { url: { type: "string" } }, required: ["url"] } } },
  { type: "function", function: { name: "service_status", description: "查看全部 AI 服务与 GPU 的实时状态", parameters: { type: "object", properties: {} } } },
  { type: "function", function: { name: "image_search", description: "联网搜图，返回图片直链；可交给 generate_image 当参考图",
    parameters: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } } },
  { type: "function", function: { name: "generate_image", description: "生成图片并保存到工作空间；refs 传参考图直链（image_search 的结果）",
    parameters: { type: "object", properties: { prompt: { type: "string" }, size: { type: "string" },
      refs: { type: "array", items: { type: "string" }, description: "参考图直链，最多 4 张" } }, required: ["prompt"] } } },
];

const SYS = `你是用户工作空间里的 coding agent。你能读写文件、运行代码、跑测试、启动训练任务、查知识库、联网、生图。
工作空间是用户自己的目录；LLM Lab 在 llm-study-lab/（Python + PyTorch 教学工程，minillm/ 里有 TODO，tests/ 是验收）。

规则：
1. 先看再改：不确定文件内容就先 list_files / read_file；不要编造文件或命令输出。
2. 改代码优先用 edit_file（精确替换），新建文件才用 write_file；路径一律用工作空间相对路径。
3. 改完要验证：Python 用 run_code（env=lab, in_place=true, cwd=llm-study-lab）；C++ 用 env=cpp。
   测试用 start_job + [ "pytest", "-m", "stage3", "-q" ] 或 run_code 跑短测试。
4. 训练/长时间任务用 start_job，然后用 job_status 轮询日志；不要用一个请求傻等几十分钟。
5. 用户问资料/文档时，先用 kb_search 查个人知识库；没有命中再联网，并说明来源。
6. 画图用 generate_image，但别一上来就闷头生成：主体/风格不确定时先 image_search 找参考图
   （直链放进 generate_image 的 refs），需要真实信息时先 search_web 查要点，再写进 prompt。
   用户本轮附的图会作为底图；参考图没找到就直说，不要编来源。
7. 删除文件、重置 lab、杀掉训练任务这类不可逆操作，先向用户确认。
8. 回答用中文，简洁；做完后说清楚「改了哪些文件、验证结果如何」。
9. 不要为了测试变绿而删测试或改容差；失败就把真实报错贴出来。`;

function scrollDown() { nextTick(() => { const s = scroller.value; if (s) s.scrollTop = s.scrollHeight; }); }

async function loadKb() {
  try { const d = await kbList(); Object.assign(kbState, d); }
  catch (e) { /* 未登录/没工作空间时静默 */ }
}
onMounted(() => { loadKb(); scrollDown(); });

function newSession() {
  if (busy.value) { toast("warn", "当前回答还在生成", "先停止再新建会话"); return; }
  msgs.splice(0, msgs.length);
  store.set("ws.msgs", []);
  input.value = "";
  nextTick(() => document.querySelector(".agent__composer textarea")?.focus());
}

function fmtTree(entries, path) {
  const base = (path || "").replace(/\/+$/, "");
  const prefix = base ? base + "/" : "";
  return JSON.stringify((entries || [])
    .filter((e) => !base || e.path === base || e.path.startsWith(prefix))
    .map((e) => (e.dir ? e.path + "/" : `${e.path} (${e.size}B)`)));
}

async function callTool(name, args) {
  if (name === "list_files") {
    const d = await wsTree();
    return fmtTree(d.entries, args.path);
  }
  if (name === "read_file") {
    const d = await wsRead(args.path || "");
    if (d.text != null) return d.text.slice(0, 40000);
    return `[binary ${d.mime || "file"}] 不能当文本读；图片可用 open-file 在编辑器里看。`;
  }
  if (name === "write_file") {
    await wsWrite(args.path || "", args.text || "");
    emit("changed");
    return JSON.stringify({ ok: true, path: args.path, bytes: String(args.text || "").length });
  }
  if (name === "edit_file") {
    const d = await wsRead(args.path || "");
    const text = d.text != null ? d.text : "";
    const old = String(args.old_text || "");
    if (!old) return JSON.stringify({ ok: false, error: "old_text 不能为空" });
    const at = text.indexOf(old);
    if (at < 0) return JSON.stringify({ ok: false, error: "没找到 old_text（先 read_file 确认原文，包括缩进）" });
    const next = text.slice(0, at) + String(args.new_text || "") + text.slice(at + old.length);
    await wsWrite(args.path, next);
    emit("changed");
    return JSON.stringify({ ok: true, path: args.path, replaced_at: at, bytes: next.length });
  }
  if (name === "delete_path") {
    await wsRemove(args.path || "");
    emit("changed");
    return JSON.stringify({ ok: true, path: args.path });
  }
  if (name === "search_files") {
    return JSON.stringify((await wsSearch(args.q || "")).hits || []);
  }
  if (name === "run_code") {
    const r = await wsRun(args.env || "python", args.code || "", Number(args.timeout) || 60, {
      files: args.files || undefined,
      in_place: !!args.in_place,
      cwd: args.cwd || undefined,
      force: !!args.force,
    });
    if (r.files && r.files.length) emit("changed");
    return JSON.stringify({ exit: r.exit, timed_out: r.timed_out, ms: r.ms,
                            stdout: (r.stdout || "").slice(-12000), stderr: (r.stderr || "").slice(-6000),
                            files: r.files || [], cwd: r.cwd });
  }
  if (name === "start_job") {
    const r = await jobStart({ env: args.env || "lab", args: args.args || [], cwd: args.cwd || "llm-study-lab",
                               timeout: Number(args.timeout) || 1800, force: !!args.force });
    emit("changed");
    return JSON.stringify(r);
  }
  if (name === "job_status") {
    const r = await jobStatus(args.id || "", Number(args.offset) || 0);
    return JSON.stringify({ ...r, log: (r.log || "").slice(-12000) });
  }
  if (name === "job_stop") {
    return JSON.stringify(await jobStop(args.id || ""));
  }
  if (name === "kb_add") {
    const r = await kbAdd({ name: args.name || "", text: args.text, path: args.path, url: args.url });
    await loadKb();
    emit("changed");
    return JSON.stringify(r);
  }
  if (name === "kb_search") {
    const r = await kbSearch(args.q || "", Number(args.k) || 6);
    return JSON.stringify({ count: r.count, reranked: r.reranked, results: (r.results || []).slice(0, 8) });
  }
  if (name === "kb_list") return JSON.stringify(await kbList());
  if (name === "search_web") return JSON.stringify((await searchWeb(args.q || "", 5)).results || []);
  if (name === "fetch_url") {
    const { fetchUrl } = await import("../hub.js");
    return JSON.stringify(await fetchUrl(args.url || "", 8000));
  }
  if (name === "service_status") {
    const s = await fetchStatus();
    return JSON.stringify({
      services: (s.services || []).map((x) => ({ label: x.label, up: x.up, calls: x.calls })),
      gpus: (s.gpus || []).map((g) => ({ index: g.index, used_gib: +(g.used_mib / 1024).toFixed(1), util: g.util })),
    });
  }
  if (name === "image_search") return JSON.stringify(await imageSearch(args.q || "", 6));
  if (name === "generate_image") {
    const refs = [];
    for (const u of (args.refs || []).slice(0, 4)) {
      try { refs.push(await urlToDataURL(u)); } catch (e) { /* 抓不到就跳过 */ }
    }
    const out = await generateImage({ prompt: args.prompt || "", refs, size: args.size || "1024x1024", steps: 8 });
    const { persistImage } = await import("../hub.js");
    let url = out.url, saved = null;
    try { saved = await persistImage(out.url); url = saved.url; emit("changed"); } catch (e) { /* 回落 dataURL */ }
    return { text: JSON.stringify({ ok: true, saved: true, seed: out.seed, image_url: url,
                                    path: saved ? `images/${saved.name}` : null,
                                    note: "图片已生成并显示在对话里" }), image: url };
  }
  return JSON.stringify({ error: `unknown tool ${name}` });
}

async function send() {
  const text = input.value.trim();
  if (!text || busy.value) return;
  input.value = "";
  msgs.push({ role: "user", text });
  busy.value = true;
  ctl = new AbortController();
  const m = reactive({ role: "assistant", text: "", tools: [], calls: [] });
  msgs.push(m);
  scrollDown();
  await stream(m);
  for (let round = 1; round <= MAX_ROUNDS && !m.stopped; round++) {
    const calls = (m.calls || []).map((c) => {
      let args = {};
      try { args = JSON.parse(c.args || "{}"); } catch (e) { args = {}; }
      return { id: c.id || `c${Date.now()}`, name: c.name, args };
    }).filter((c) => c.name);
    if (!calls.length) break;
    for (const c of calls) {
      const card = reactive({ id: c.id, name: c.name, tool: c.name, arguments: c.args, at: m.text.length,
                              state: "running", round, startedAt: Date.now() });
      m.tools.push(card);
      try {
        const r = await callTool(c.name, c.args);
        const txt = typeof r === "string" ? r : r.text;
        if (r && r.image) { card.image = r.image; emit("changed"); }
        Object.assign(card, { result: txt, ok: true, state: "done" });
      } catch (e) {
        Object.assign(card, { result: `ERROR: ${e.message || e}`, ok: false, state: "error" });
      }
    }
    m.history = [...(m.history || []),
      { role: "assistant", content: m.text || "", tool_calls: calls.map((c) => ({ id: c.id, type: "function", function: { name: c.name, arguments: JSON.stringify(c.args) } })) },
      ...m.tools.slice(-calls.length).map((c) => ({ role: "tool", tool_call_id: c.id, content: c.result }))];
    const m2 = reactive({ role: "assistant", text: "", tools: [], calls: [] });
    msgs.push(m2);
    scrollDown();
    await stream(m2);
    if (m2.text) m.text = m.text ? `${m.text}\n\n${m2.text}` : m2.text;
    if (m2.tools && m2.tools.length) m.tools = [...m.tools, ...m2.tools];
    m.meta = m2.meta || m.meta;
    m.calls = m2.calls || [];
    const at = msgs.indexOf(m2);
    if (at >= 0) msgs.splice(at, 1);
    if (!m.calls.length) break;
  }
  busy.value = false;
  if (autoSpeak.value && m.text) speak(m, null, true);
}

async function stream(m) {
  const history = [];
  for (const x of msgs) {
    if (x === m) continue;
    if (x.role === "user") history.push({ role: "user", content: x.text });
    else if (x.text || (x.history && x.history.length)) {
      if (x.history) history.push(...x.history);
      if (x.text) history.push({ role: "assistant", content: x.text });
    }
  }
  let sys = SYS;
  if (props.openPath) {
    sys += `\n\n用户当前打开的文档：${props.openPath}\n\`\`\`\n${(props.openText || "").slice(0, 8000)}\n\`\`\``;
  }
  // 自动知识库检索：用户这句话如果能在资料里找到内容，直接带上（不用模型再调一次工具）
  const lastUser = [...msgs].reverse().find((x) => x.role === "user");
  if (autoKb.value && lastUser && lastUser.text) {
    try {
      const r = await kbSearch(lastUser.text, 4);
      if ((r.results || []).length) {
        sys += "\n\n个人知识库检索结果（供参考，引用时写资料名）：\n" +
          r.results.map((x, i) => `[${i + 1}] ${x.name}：${String(x.text || "").slice(0, 700)}`).join("\n");
      }
    } catch (e) { /* 知识库不可用不影响对话 */ }
  }
  const body = { model: model.value, messages: [{ role: "system", content: sys }, ...history], stream: true, temperature: 0, max_tokens: 4096, tools: TOOLS, tool_choice: "auto" };
  let firstAt = null, usage = null;
  const t0 = performance.now();
  try {
    const r = await fetch("/v1/chat/completions", { method: "POST", headers: headers(true),
                                                    body: JSON.stringify(body), signal: ctl?.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let j; try { j = JSON.parse(data); } catch (e) { continue; }
        if (j.usage) usage = j.usage;
        const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
        if (d.content) {
          if (!firstAt) { firstAt = performance.now(); m.ttft = ((firstAt - t0) / 1000).toFixed(2); }
          m.text += d.content;
          m.liveSpeed = (((usage && usage.completion_tokens) || m.text.length / 1.6) / Math.max(0.2, (performance.now() - firstAt) / 1000)).toFixed(1);
          scrollDown();
        }
        if (d.tool_calls) {
          m.calls = m.calls || [];
          for (const tc of d.tool_calls) {
            const i = tc.index == null ? 0 : tc.index;
            const cur = m.calls[i] || (m.calls[i] = { id: tc.id, name: "", args: "" });
            if (tc.id) cur.id = tc.id;
            if (tc.function && tc.function.name) cur.name += tc.function.name;
            if (tc.function && tc.function.arguments) cur.args += tc.function.arguments;
          }
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") m.stopped = true;
    else m.text = m.text || `请求失败：${e.message || e}`;
  }
  const n = (usage && usage.completion_tokens) || Math.round((m.text || "").length / 1.6);
  if (firstAt && n) {
    const secs = Math.max(0.2, (performance.now() - firstAt) / 1000);
    m.meta = `${n} tokens · ${(n / secs).toFixed(1)} tok/s${m.ttft ? ` · 首字 ${m.ttft}s` : ""}`;
  }
  m.liveSpeed = null;
}

// ------------------------------------------------------------------ 语音（胡桃）
const audioEl = ref(null);
const speaking = ref(null);      // 正在朗读的消息索引
const viewer = ref(null);        // 点开的图片
let audioUrl = null;
// 按句切块朗读：第一句很快能出声，后面的块边播边合成。
let spGen = 0, spQueue = [], spPlaying = false, spSilent = false, spPrefetch = null, spPrefetchText = "";
function splitSpeech(text) {
  const clean = String(text || "").replace(/```[\s\S]*?```/g, "\n").replace(/`[^`]*`/g, " ");
  return (clean.match(/[^。！？!?；;…\n.]+[。！？!?；;…\n.]?/g) || [])
    .map((s) => s.trim()).filter((s) => s.length >= 4);
}
function prefetchSpeak() {
  if (spPrefetch || !spQueue.length) return;
  spPrefetchText = spQueue[0];
  spPrefetch = ttsSay(spPrefetchText.slice(0, 1200)).catch(() => null);
}
async function playNextSpeak() {
  if (spPlaying) return;
  const text = spQueue.shift();
  if (!text) { speaking.value = null; return; }
  const gen = spGen;
  spPlaying = true;
  try {
    const url = (spPrefetchText === text && spPrefetch) ? await spPrefetch : await ttsSay(text.slice(0, 1200));
    spPrefetch = null; spPrefetchText = "";
    if (gen !== spGen) { if (url) URL.revokeObjectURL(url); return; }
    if (!url) throw new Error("TTS 合成失败");
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = url;
    prefetchSpeak();
    const el = audioEl.value;
    if (el) { el.src = url; await el.play(); }
  } catch (e) {
    spPlaying = false;
    if (gen === spGen) {
      if (!spSilent) toast("warn", "朗读失败", String(e.message || e), 6000);
      playNextSpeak();
    }
  }
}
function onSpeakEnded() {
  spPlaying = false;
  playNextSpeak();
}
function speak(m, i = null, silent = false) {
  if (!m || !m.text) return;
  spGen += 1;
  spQueue = splitSpeech(m.text);
  spPlaying = false;
  spSilent = silent;
  spPrefetch = null;
  spPrefetchText = "";
  speaking.value = i;
  if (audioEl.value) { try { audioEl.value.pause(); } catch (e) {} }
  if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
  playNextSpeak();
}

function sourcesOf(m) {
  const out = [];
  for (const t of (m.tools || [])) {
    if (!["search_web", "fetch_url"].includes(t.name)) continue;
    const raw = String(t.result || "");
    const urls = raw.match(/https?:\/\/[^\s"'\\]+/g) || [];
    for (const u of urls) {
      if (/\.(png|jpe?g|webp|gif)(\?|$)/i.test(u)) continue;
      if (!out.some((x) => x.url === u)) out.push({ url: u, host: (u.split("/")[2] || "").replace(/^www\./, "") });
      if (out.length >= 6) return out;
    }
  }
  return out;
}

function copyMsg(m) {
  navigator.clipboard?.writeText(m.text || "").then(() => toast("success", "已复制"), () => {});
}
async function saveMsg(m) {
  if (!m.text) return;
  const base = (props.openPath || "notes/agent").replace(/\.md$/i, "");
  const path = `${base}-${new Date().toISOString().slice(5, 16).replace(/[:T]/g, "")}.md`.replace(/^\//, "");
  try {
    await wsWrite(path, m.text);
    emit("changed");
    toast("success", "已保存", path, 3000);
  } catch (e) { toast("error", "保存失败", String(e.message || e)); }
}
async function saveToKb(m) {
  if (!m.text) return;
  try {
    const r = await kbAdd({ name: `对话-${new Date().toISOString().slice(5, 16)}`, text: m.text });
    await loadKb();
    toast("success", "已存入知识库", `${r.chunks} 块`, 3000);
  } catch (e) { toast("error", "存入失败", String(e.message || e)); }
}
function stop() {
  if (ctl) { try { ctl.abort(); } catch (e) {} }
  busy.value = false;
}
onBeforeUnmount(() => { spGen += 1; spQueue = []; if (audioUrl) URL.revokeObjectURL(audioUrl); });
</script>

<template>
  <aside class="agent">
    <div class="agent__head">
      <span class="agent__title"><Icon name="tool" cls="st-icon st-icon--sm" /> Coding Agent</span>
      <span class="agent__kb" :title="`知识库：${kbState.count} 份资料 / ${kbState.chunks} 块`">
        <Icon name="memory" cls="st-icon st-icon--sm" />{{ kbState.count }}</span>
      <label class="agent__toggle" title="回答自动用胡桃音色朗读">
        <input v-model="autoSpeak" type="checkbox"><span>朗读</span></label>
      <label class="agent__toggle" title="发送前自动检索个人知识库">
        <input v-model="autoKb" type="checkbox"><span>知识库</span></label>
      <select v-model="model" class="agent__model" aria-label="通道">
        <option v-for="x in MODELS" :key="x.id" :value="x.id">{{ x.label }}</option>
      </select>
      <button class="st-btn st-btn--secondary agent__new" :disabled="busy" @click="newSession">
        <Icon name="new-chat" cls="st-icon st-icon--sm" />新会话</button>
    </div>

    <div ref="scroller" class="agent__log">
      <p v-if="!msgs.length" class="agent__hint">
        直接说你要做什么：<br>
        · 「读一下 llm-study-lab/START_HERE.md，带我从阶段 3 开始」<br>
        · 「把 minillm/model/attention.py 的 TODO 补上，跑 pytest -m stage2」<br>
        · 「把这份资料存进知识库，然后总结要点」<br>
        · 「画一张赛博朋克街景，用你搜到的参考图当底图」
      </p>
      <div v-for="(m, i) in msgs" :key="i" class="agent__msg" :class="`agent__msg--${m.role}`">
        <div v-if="m.role === 'user'" class="agent__bubble">{{ m.text }}</div>
        <template v-else>
          <template v-for="(x, k) in (m.tools || [])" :key="k">
            <div v-if="x.name === 'generate_image' && x.state !== 'error'" class="agent-img-slot">
              <img v-if="x.image" :src="x.image" class="agent__img" alt=""
                   title="点击查看大图 / 下载" @click="viewer = { src: x.image, name: 'generated' }">
              <div v-else class="img-skeleton"
                   style="display:flex;align-items:center;gap:10px;max-width:380px;width:fit-content;margin:8px 0">
                <div class="img-skeleton__box"
                     style="width:64px;height:64px;min-width:64px;aspect-ratio:auto;flex:none">
                  <span class="img-skeleton__spinner"></span></div>
                <div class="img-skeleton__body" style="display:grid;gap:3px;min-width:0">
                  <div class="img-skeleton__label" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0">
                    <span class="img-skeleton__badge">{{ t("chat.image.generating") }}</span>
                    <span class="img-skeleton__prompt"
                          style="max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ (x.arguments && x.arguments.prompt) || "" }}</span>
                  </div>
                  <p class="img-skeleton__hint" style="margin:0">{{ t("chat.image.generating.hint") }}</p>
                </div>
              </div>
            </div>
            <ToolCard v-else :tool="x" />
          </template>
          <img v-for="(im, k) in (m.images || []).filter((i) => !i.toolId)" :key="`i${k}`" :src="im.url"
               class="agent__img" alt="" title="点击查看大图 / 下载"
               @click="viewer = { src: im.url, name: im.name }">
          <div v-if="m.text" class="agent__bubble agent__bubble--bot md" v-html="md(m.text)"></div>
          <div v-else-if="busy" class="agent__bubble agent__bubble--bot muted">…</div>
          <div v-if="m.text && !busy && sourcesOf(m).length" class="agent__sources">
            <span class="agent__sources-label">来源</span>
            <a v-for="(s2, k) in sourcesOf(m)" :key="k" :href="s2.url" target="_blank" rel="noopener noreferrer"
               class="agent__source">{{ s2.host }}</a>
          </div>
          <div v-if="m.text && !busy" class="agent__acts">
            <button class="agent__act" @click="copyMsg(m)">复制</button>
            <button class="agent__act" @click="saveMsg(m)">存为文件</button>
            <button class="agent__act" @click="saveToKb(m)">存入知识库</button>
            <button class="agent__act" @click="speak(m, i)">
              {{ speaking === i ? "朗读中…" : "朗读（胡桃）" }}</button>
            <button v-if="openPath" class="agent__act" @click="emit('insert', m.text)">插入到文档</button>
          </div>
          <div v-if="m.meta || m.liveSpeed" class="agent__meta">
            <span v-if="m.meta">{{ m.meta }}</span>
            <span v-else-if="m.liveSpeed">{{ m.liveSpeed }} tok/s</span>
          </div>
        </template>
      </div>
    </div>
    <audio ref="audioEl" class="agent__audio" @ended="onSpeakEnded" @error="onSpeakEnded"></audio>
    <form class="agent__composer" @submit.prevent="send">
      <textarea v-model="input" rows="2" class="st-input" placeholder="让 agent 读文件、改代码、跑测试、开训练任务…"
                @keydown.enter.exact.prevent="send"></textarea>
      <button v-if="busy" class="st-btn st-btn--secondary agent__send" type="button" @click="stop">
        <Icon name="stop" cls="st-icon st-icon--sm" /></button>
      <button v-else class="st-btn st-btn--primary agent__send" type="submit" :disabled="!input.trim()">
        <Icon name="send" cls="st-icon st-icon--sm" /></button>
    </form>
  <ImageViewer v-if="viewer" :src="viewer.src" :name="viewer.name" @close="viewer = null" />
  </aside>
</template>

<style scoped>
.agent { display: flex; flex-direction: column; min-height: 0; border-left: 1px solid var(--st-line); background: var(--st-surface); }
.agent__head { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--st-line); flex: none; flex-wrap: wrap; }
.agent__title { display: inline-flex; align-items: center; gap: 5px; font-weight: var(--st-fw-bold); font-size: var(--st-fs-sm); }
.agent__kb { display: inline-flex; align-items: center; gap: 3px; font-size: 11px; color: var(--st-ink-muted);
             padding: 2px 6px; border-radius: var(--st-r-pill); background: var(--st-surface-2); }
.agent__toggle { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--st-ink-muted); cursor: pointer; }
.agent__toggle input { margin: 0; }
.agent__model { height: 26px; padding: 0 6px; border: 1px solid var(--st-line); border-radius: var(--st-r-pill);
                background: var(--st-surface); color: var(--st-ink-soft); font-size: 11px; flex: none; }
.agent__new { height: 28px; padding: 0 10px; font-size: 12px; margin-left: auto; }
.agent__log { flex: 1; min-height: 0; overflow: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
.agent__hint { margin: 0; color: var(--st-ink-muted); font-size: var(--st-fs-sm); line-height: 1.8; }
.agent__msg { display: flex; flex-direction: column; gap: 6px; }
.agent__msg--user { align-items: flex-end; }
.agent__bubble { padding: 8px 12px; border-radius: 12px; background: var(--st-surface-2); font-size: var(--st-fs-sm);
                 line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; max-width: 100%; }
.agent__msg--user .agent__bubble { background: var(--st-accent); color: #fff; }
.agent__img { max-width: 100%; border-radius: 10px; border: 1px solid var(--st-line); cursor: zoom-in; }
.agent-img-slot { display: grid; gap: 6px; }
.agent-img-slot .img-skeleton { max-width: 100%; }
.agent__composer { flex: none; display: flex; gap: 8px; padding: 10px; border-top: 1px solid var(--st-line); align-items: flex-end; }
.agent__composer textarea { flex: 1; min-height: 52px; resize: none; font-size: 14px; }
.agent__send { width: 42px; height: 42px; flex: none; }
.agent__audio { display: none; }
.agent__spacer { flex: 1; }
.agent__sources { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding-left: 2px; }
.agent__sources-label { font-size: 11px; color: var(--st-ink-muted); }
.agent__source { font-size: 11px; padding: 2px 8px; border-radius: var(--st-r-pill); background: var(--st-surface-2);
                 color: var(--st-ink-soft); text-decoration: none; border: 1px solid var(--st-line); }
.agent__source:hover { border-color: var(--st-accent); color: var(--st-accent); }
.agent__acts { display: flex; gap: 6px; padding-left: 2px; flex-wrap: wrap; }
.agent__act { height: 24px; padding: 0 10px; border: 1px solid var(--st-line); border-radius: var(--st-r-pill);
              background: var(--st-surface); color: var(--st-ink-muted); font-size: 11px; cursor: pointer; }
.agent__act:hover { color: var(--st-ink); border-color: var(--st-accent); }
.agent__meta { font-size: 11px; color: var(--st-ink-muted); font-variant-numeric: tabular-nums; padding-left: 2px; }

.agent__bubble.md :deep(> :first-child) { margin-top: 0; }
.agent__bubble.md :deep(> :last-child) { margin-bottom: 0; }
.agent__bubble.md :deep(p) { margin: 0 0 8px; }
.agent__bubble.md :deep(h1), .agent__bubble.md :deep(h2), .agent__bubble.md :deep(h3) { margin: 12px 0 6px; font-size: 1.05em; }
.agent__bubble.md :deep(ul), .agent__bubble.md :deep(ol) { margin: 0 0 8px; padding-left: 20px; }
.agent__bubble.md :deep(li) { margin: 2px 0; }
.agent__bubble.md :deep(code) { padding: 1px 5px; border-radius: 5px; background: var(--st-surface-2);
                          font-family: var(--st-font-mono), monospace; font-size: .92em; }
.agent__bubble.md :deep(pre) { margin: 8px 0; padding: 10px; border-radius: 8px; background: var(--st-surface-2);
                        overflow: auto; }
.agent__bubble.md :deep(pre code) { padding: 0; background: none; }
.agent__bubble.md :deep(table) { border-collapse: collapse; margin: 8px 0; font-size: .95em; }
.agent__bubble.md :deep(th), .agent__bubble.md :deep(td) { border: 1px solid var(--st-line); padding: 4px 8px; }
.agent__bubble.md :deep(a) { color: var(--st-accent); text-decoration: underline; }
.agent__bubble.md :deep(blockquote) { margin: 8px 0; padding-left: 10px; border-left: 3px solid var(--st-line); color: var(--st-ink-muted); }
@media (max-width: 720px) {
  .agent__head { gap: 6px; }
  .agent__new { margin-left: 0; }
}
</style>