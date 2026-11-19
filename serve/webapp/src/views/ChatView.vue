<script setup>
// 对话视图：与原 app.js 的 Chat 段等价（流式、思考、MCP 工具调用、附件/拖拽/粘贴、停止、新对话、导出）。
// 会话本身（列表、当前会话、落盘）在 src/chats.js —— 这一页只管当前会话的消息。
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from "vue";
import { useI18n } from "vue-i18n";
import { store, toast, copyText, headers, fmt, apiKey } from "../api.js";
import { auth, openLogin } from "../auth.js";
import { app, settings, projectionLoaded, setSide, engine, setEngine, ENGINES, setTab } from "../store.js";
import { activeChat, chats, newChat as startNewChat, saveChat as saveChatRecord } from "../chats.js";
import { loadSuggestions, rotate, startSuggestionLoop, stopSuggestionLoop, suggest } from "../suggestions.js";
import { FILE_SLUG } from "../brand.js";
import Icon from "../components/Icon.vue";
import MessageItem from "../components/MessageItem.vue";
import { searchWeb, fetchStatus, generateImage, embedText, fetchUrl, imageSearch,
         mcpList, mcpCall, getWorkspace, getSkill, persistImage, urlToDataURL, ttsSay, startAgentRun, stopAgentRun, openAgentStream } from "../hub.js";
import { tree as wsTree, readFile as wsRead, writeFile as wsWrite, remove as wsRemove,
         search as wsSearch, runCode as wsRun, jobStart, jobStatus, jobStop,
         kbAdd, kbSearch } from "../workspace.js";

const { t } = useI18n();
const emit = defineEmits(["open-sampling"]);

const chat = activeChat;
const messages = computed(() => (chat.value ? chat.value.messages : []));
const attachments = ref([]);
// Each chat owns its own in-flight stream, so several conversations can generate at once.
const busy = computed(() => (chat.value && chat.value.busy) || null);
const resuming = ref(false);          // 刷新后自动续跑中：防止重复进入
const input = ref("");
const inputEl = ref(null);
// 不管是用户输入还是程序清空（发送/新会话/预填），都在 DOM 更新后重新量一次高度。
// 之前发送后立刻 autosize，读到的还是旧文本的 scrollHeight，输入框会一直保持很高。
watch(input, () => nextTick(() => autosize()));
// 每个会话有自己的草稿与附件。之前输入框是全局的：在 A 里敲了字、切到 B，字会跟着跑过去。
// 放在 Map 里（按 chat id）而不是挂在 chat 对象上 —— 附件是 dataURL，绝不能落盘/同步。
const drafts = new Map();
const attachDrafts = new Map();
const curChatId = () => (chat.value && chat.value.id) || "";
watch(() => curChatId(), (id, old) => {
  if (old) { drafts.set(old, input.value); attachDrafts.set(old, attachments.value); }
  if (!id) return;
  if (drafts.has(id)) {
    input.value = drafts.get(id) || "";
    attachments.value = attachDrafts.get(id) || [];
  } else if (!old) {
    // 首次绑定（应用刚起来）：把已经预填好的文字/附件接到这个会话上，别清空
    drafts.set(id, input.value);
    attachDrafts.set(id, attachments.value);
  } else {
    input.value = "";
    attachments.value = [];
  }
  nextTick(() => autosize());
});
watch(input, (v) => { const id = curChatId(); if (id) drafts.set(id, v); });
// 首页能力卡/「开始对话」带过来的预填提示词：切到对话页时填进输入框，用户确认后再发。
watch(() => app.tab, (tab) => {
  if (tab !== "chat") return;
  const prefill = store.get("chat.prefill", "");
  if (!prefill) return;
  store.set("chat.prefill", "");
  input.value = prefill;
  nextTick(() => { autosize(); inputEl.value?.focus(); });
});
const scrollEl = ref(null);
const fileEl = ref(null);
const dragging = ref(false);
// 「对话 / Agent」两种模式。默认就是 coding agent：能读写文件、跑代码/测试、开训练任务、
// 查知识库、生图、语音；普通模式只用于想纯聊天时。自动续轮最多 8 轮。
const agentMode = ref(store.get("agentMode", true));
const autoSpeak = ref(store.get("agentAutoSpeak", true));
const autoKb = ref(store.get("agentAutoKb", true));
watch(autoSpeak, (v) => store.set("agentAutoSpeak", !!v));
watch(autoKb, (v) => store.set("agentAutoKb", !!v));
const kbContext = ref("");
const audioEl = ref(null);
let audioUrl = null;
// 流式朗读：回答边生成边按句切块，一块一块送去合成并排队播放。
let ttsGen = 0, ttsQueue = [], ttsBuf = "", ttsMsgRef = null, ttsPlaying = false;
let ttsPrefetch = null, ttsPrefetchText = "";
const ttsBusy = ref(false);
const ttsBlocked = ref(false);
let audioCtx = null;
/** 在用户的第一次点击里解锁音频上下文：流式朗读在几秒后才开始，别被浏览器自动播放策略拦掉。 */
function unlockAudio() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) { /* 不支持就算了，后面有「继续朗读」按钮兜底 */ }
}
// 工作空间：每个用户一个独立目录（网关按登录会话 / API token 分）。skill 进系统提示，
// MCP 的工具合并进 agent 的工具表。没登录/没 token 时是空的，agent 就只用内置工具。
const ws = reactive({ loaded: false, skills: [], mcpTools: [], mcpServers: [] });
async function loadAgentContext() {
  try {
    const meta = await getWorkspace();
    const skills = [];
    for (const sk of (meta.skills || []).slice(0, 8)) {
      try {
        const d = await getSkill(sk.name);
        skills.push({ name: sk.name, content: String(d.content || "").slice(0, 4000) });
      } catch (e) { /* 单个 skill 读失败不影响其它 */ }
    }
    ws.skills = skills;
    const m = await mcpList();
    ws.mcpServers = m.servers || [];
    ws.mcpTools = m.tools || [];
    ws.loaded = true;
  } catch (e) {
    ws.loaded = true;              // 失败也算加载过，别每轮都重试打扰
  }
}
watch(agentMode, (on) => { if (on && !ws.loaded) loadAgentContext(); });
watch(agentMode, (v) => store.set("agentMode", !!v));

const empty = computed(() => messages.value.length === 0);
const fmtDur = (s) => (s >= 60 ? `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}s`
                               : `${fmt(s, 1)}s`);
const streamingFor = (m) => busy.value && busy.value.msg === m;
const engineLabel = computed(() => {
  const e = ENGINES.find((x) => x.id === engine.model) || ENGINES[0];
  return t(e.key);
});

// 空态的示例任务：人工挑选、和 LLM Lab/知识库/代码相关，不再让模型在后台生成。
// 点一条就照原样发出去。
const SUGGESTIONS = computed(() => suggest.shown);
function useSuggestion(s) {
  input.value = typeof s === "string" ? s : (s && s.text) || "";
  send();
}


// ---- 滚动
const nearBottom = () => {
  const s = scrollEl.value;
  return s ? s.scrollHeight - s.scrollTop - s.clientHeight < 120 : true;
};
function scrollDown(force) {
  const s = scrollEl.value;
  if (s && (force || nearBottom())) s.scrollTop = s.scrollHeight;
}
const queueScroll = () => nextTick(() => scrollDown(false));

// ---- 请求体组装（与原 apiMessages/assistantMessages/userText/fileBlock 等价）
function fileBlock(f) {
  const longest = Math.max(2, ...(f.text.match(/`+/g) || []).map((s) => s.length));
  const fence = "`".repeat(longest + 1);
  return `File: ${f.name}\n${fence}\n${f.text}\n${fence}`;
}
function userText(m) {
  const files = (m.files || []).filter((f) => f.text != null);
  return [m.text, ...files.map(fileBlock)].filter((s) => s).join("\n\n");
}
/** 工具结果回喂模型。生图成功时把图片 URL 补进结果 —— 旧记录里只存了 seed，
 *  模型会以为没生成、转去 list_files 找文件，甚至把工具调用写进 thinking 后 EOS。 */
function toolResultForModel(x) {
  let content = x.result;
  if (x.image && content) {
    try {
      const o = JSON.parse(content);
      if (o && !o.image_url) {
        o.image_url = x.image;
        o.note = o.note || "图片已生成并显示在对话里";
        content = JSON.stringify(o);
      }
    } catch (e) { /* 非 JSON 结果保持原样 */ }
  }
  return content;
}

function assistantMessages(m) {
  const ran = (m.tools || []).filter((x) => x.round != null && x.result != null && x.state !== "skipped");
  if (!ran.length) return m.text ? [{ role: "assistant", content: m.text }] : [];
  const out = [];
  let pos = 0;
  for (const r of [...new Set(ran.map((x) => x.round))]) {
    const calls = ran.filter((x) => x.round === r);
    const at = Math.min(Math.max(pos, calls[0].at || 0), m.text.length);
    out.push({ role: "assistant", content: m.text.slice(pos, at).trim(),
               tool_calls: calls.map((x) => ({ id: x.id, type: "function", function: { name: x.name, arguments: JSON.stringify(x.arguments || {}) } })) });
    for (const x of calls) out.push({ role: "tool", tool_call_id: x.id, content: toolResultForModel(x) });
    pos = at;
  }
  const rest = m.text.slice(pos).trim();
  if (rest) out.push({ role: "assistant", content: rest });
  return out;
}
function apiMessages(forChat = chat.value) {
  const out = [];
  if (agentMode.value) {
    const kb = kbContext.value ? `\n\n个人知识库检索结果（引用时写资料名）：\n${kbContext.value}` : "";
    out.push({ role: "system", content: agentSystem() + kb });
  }
  for (const m of (forChat.messages || [])) {
    if (m.role === "user") {
      const imgs = (m.images || []).filter((i) => i.url);
      const text = userText(m);
      out.push({ role: "user", content: imgs.length ? [{ type: "text", text },
        ...imgs.map((i) => ({ type: "image_url", image_url: { url: i.url } }))] : text });
    } else if (!m.error) {
      out.push(...assistantMessages(m));
      // 被 max_tokens 截断后的续写：把半截回答当 assistant，再补一条「接着写」的指令
      if (m.continueHint) out.push({ role: "user", content: m.continueHint });
    }
  }
  return out;
}

// ---- 流式里的一个工具事件（chunk 的 strata_mcp 字段）
function onTool(m, x) {
  if (x.event === "limit") { m.limit = x.max_rounds; return; }
  m.tools = m.tools || [];
  let tool = m.tools.find((y) => y.id === x.id);
  if (!tool) { tool = reactive({ id: x.id, name: x.name, at: m.text.length, rat: m.reasoning.length, state: "writing" }); m.tools.push(tool); }
  if (x.event === "call") {
    Object.assign(tool, { name: x.name, server: x.server, tool: x.tool, arguments: x.arguments, round: x.round, state: "running" });
  } else if (x.event === "result") {
    Object.assign(tool, { result: x.text, ok: x.ok, chars: x.chars, truncated: x.truncated, ms: x.ms,
                          state: x.skipped ? "skipped" : x.ok ? "done" : "error" });
  }
}

// ---------------------------------------------------------------- Agent 模式
// 做法：把「可用工具」写进系统提示，模型要用工具时只输出一行 JSON；
// 前端执行完把结果作为一条 user 消息喂回去，再继续这一轮。没有依赖引擎端的 tool_calls，
// 任何模型都能跑；工具调用块复用 MessageItem 现有的 ToolCall 卡片渲染。
// 一次提问里 agent 最多自动跑多少轮「工具 → 观察 → 再决策」。
// 8 轮对真实的 coding 任务远远不够（改一个文件 + 跑测试 + 修错就没了），用户明确要求
// 别频繁打断；这里放到 40 轮，正常任务基本不会碰到，真碰到也会在输入框下面提示。
const AGENT_MAX_ROUNDS = 40;
const AGENT_TOOLS = {
  list_files: { args: "{path?:string}", desc: "列出工作空间文件" },
  read_file: { args: "{path:string}", desc: "读一个文本文件" },
  write_file: { args: "{path:string, text:string}", desc: "写/覆盖文件" },
  edit_file: { args: "{path:string, old_text:string, new_text:string}", desc: "精确替换文件内容" },
  search_files: { args: "{q:string}", desc: "工作空间全文搜索" },
  run_code: { args: "{env:string, code:string, in_place?:bool, cwd?:string, timeout?:number}", desc: "运行代码/小项目（python/ds/node/cpp/lab）" },
  start_job: { args: "{args:string[], env?:string, cwd?:string}", desc: "启动后台任务（训练/长跑）" },
  job_status: { args: "{id:string, offset?:number}", desc: "看后台任务日志" },
  job_stop: { args: "{id:string}", desc: "停止后台任务" },
  kb_add: { args: "{name?:string, text?:string, path?:string, url?:string}", desc: "存入个人知识库" },
  kb_search: { args: "{q:string, k?:number}", desc: "检索个人知识库" },
  search_web: { args: "{q:string}", desc: "联网搜索" },
  fetch_url: { args: "{url:string}", desc: "读网页正文" },
  image_search: { args: "{q:string}", desc: "搜图（可交给 generate_image 当参考图）" },
  service_status: { args: "{}", desc: "查看 AI 服务与 GPU 状态" },
  generate_image: { args: "{prompt:string, size?:string, steps?:number, refs?:string[]}", desc: "生图；refs = image_search 找到的参考图直链，本轮附件也会作为底图" },
};
// 原生 tool calling 的 schema（引擎实测支持：finish_reason="tool_calls"）。
// 文字协议仍然保留作兜底 —— 模型偶尔不按 schema 走，那时还能从正文里认出那行 JSON。
const AGENT_SCHEMA = [
  { type: "function", function: { name: "list_files", description: "列出工作空间文件（可给子目录 path）",
    parameters: { type: "object", properties: { path: { type: "string" } } } } },
  { type: "function", function: { name: "read_file", description: "读取工作空间里的一个文本文件",
    parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] } } },
  { type: "function", function: { name: "write_file", description: "写/覆盖一个文件（自动建目录）",
    parameters: { type: "object", properties: { path: { type: "string" }, text: { type: "string" } }, required: ["path", "text"] } } },
  { type: "function", function: { name: "edit_file", description: "把文件里的 old_text 替换成 new_text（先读再改）",
    parameters: { type: "object", properties: { path: { type: "string" }, old_text: { type: "string" }, new_text: { type: "string" } }, required: ["path", "old_text", "new_text"] } } },
  { type: "function", function: { name: "search_files", description: "在工作空间里全文搜索",
    parameters: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } } },
  { type: "function", function: { name: "run_code", description:
    "运行代码/小项目并拿回输出。env: python/ds/node/cpp/lab；改 lab 代码后验证用 in_place=true, cwd=llm-study-lab",
    parameters: { type: "object", properties: { env: { type: "string" }, code: { type: "string" },
      timeout: { type: "number" }, in_place: { type: "boolean" }, cwd: { type: "string" },
      force: { type: "boolean" }, files: { type: "object" } }, required: ["code"] } } },
  { type: "function", function: { name: "start_job", description: "启动后台任务（训练/长跑），立刻返回 job id",
    parameters: { type: "object", properties: { args: { type: "array", items: { type: "string" } },
      env: { type: "string" }, cwd: { type: "string" }, timeout: { type: "number" }, force: { type: "boolean" } }, required: ["args"] } } },
  { type: "function", function: { name: "job_status", description: "看后台任务状态与增量日志",
    parameters: { type: "object", properties: { id: { type: "string" }, offset: { type: "number" } }, required: ["id"] } } },
  { type: "function", function: { name: "job_stop", description: "停止后台任务",
    parameters: { type: "object", properties: { id: { type: "string" } }, required: ["id"] } } },
  { type: "function", function: { name: "kb_add", description: "把文本/文件/网页存进个人知识库",
    parameters: { type: "object", properties: { name: { type: "string" }, text: { type: "string" },
      path: { type: "string" }, url: { type: "string" } } } } },
  { type: "function", function: { name: "kb_search", description: "检索个人知识库（向量召回 + 精排）",
    parameters: { type: "object", properties: { q: { type: "string" }, k: { type: "number" } }, required: ["q"] } } },
  { type: "function", function: { name: "search_web", description: "联网搜索，返回标题、链接与摘要",
    parameters: { type: "object", properties: { q: { type: "string", description: "检索词" } }, required: ["q"] } } },
  { type: "function", function: { name: "fetch_url", description: "打开网页读正文",
    parameters: { type: "object", properties: { url: { type: "string" } }, required: ["url"] } } },
  { type: "function", function: { name: "image_search", description: "搜图，返回图片直链；可交给 generate_image 当参考图",
    parameters: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } } },
  { type: "function", function: { name: "service_status", description: "查看全部 AI 服务、两条对话通道与所有 GPU 的实时状态",
    parameters: { type: "object", properties: {} } } },
  { type: "function", function: { name: "generate_image", description: "调用生图模型出图；refs 传参考图直链（image_search 的结果），用户本轮附的图也会原图作为底图输入",
    parameters: { type: "object", properties: {
      prompt: { type: "string", description: "画面描述" },
      size: { type: "string", description: "如 1024x1024 / 1664x1664" },
      steps: { type: "integer", description: "1..8，默认 8" },
      refs: { type: "array", items: { type: "string" },
              description: "参考图直链（最多 4 张），来自 image_search；会作为底图喂给生图模型" } }, required: ["prompt"] } } },
];

function agentSystem() {
  const lines = Object.entries(AGENT_TOOLS).map(([k, v]) => `- ${k} ${v.args}  ${v.desc}`).join("\n");
  const mcpLines = (ws.mcpTools || []).map((x) => `- ${mcpToolName(x)}  ${(x.description || "").slice(0, 160)}`).join("\n");
  const skillText = (ws.skills || []).map((k) => `### skill: ${k.name}\n${k.content}`).join("\n\n");
  return `你是用户的 coding agent，工作空间是用户自己的目录；LLM Lab 在 llm-study-lab/（PyTorch 教学工程，minillm/ 有 TODO，tests/ 是验收）。
规则：
1. 需要工具时按工具调用返回；不要编造文件内容或命令输出 —— 不确定就先 list_files / read_file。
2. 改代码优先 edit_file（精确替换），新建文件才 write_file；路径用工作空间相对路径。
3. 改完必须验证：Python 用 run_code（env=lab, in_place=true, cwd=llm-study-lab）；C++ 用 env=cpp；
   测试用 run_code 跑短测试，或 start_job + ["pytest","-m","stageN","-q"] 跑长测试。
4. 训练/长时间任务用 start_job，再用 job_status 轮询日志；不要傻等。
5. 用户问资料时先 kb_search；没有命中再联网，并说明来源。
6. 画图用 generate_image。**不要一上来就闷头生成**：主体/风格有不确定时，先 image_search 找参考图
   （把返回的直链放进 generate_image 的 refs），需要真实信息（地点、服饰、年代、产品外观）先 search_web
   收集要点，再写进 prompt；用户本轮附的图会作为底图。参考图/资料没找到就直说，不要编来源。
7. 删除、重置、杀任务这类不可逆操作先向用户确认。
8. 回答用中文，简洁；做完说清楚改了哪些文件、验证结果如何。不要删测试或调容差让它变绿。
可用工具：
${lines}${mcpLines ? "\n来自 MCP 的工具：\n" + mcpLines : ""}
${skillText ? "\n用户安装的 skill（按需遵循）：\n" + skillText : ""}`;
}

/** MCP 工具在 chat 里的扁平名字：mcp__<server>__<tool>，避免两家重名。 */
function mcpToolName(x) { return `mcp__${x.server}__${x.name}`; }

/** 本轮要发给模型的工具表：内置 + 工作空间里的 MCP 工具。 */
function agentSchema() {
  return [...AGENT_SCHEMA, ...(ws.mcpTools || []).map((x) => ({
    type: "function",
    function: { name: mcpToolName(x), description: (x.description || "").slice(0, 300) || `MCP ${x.server}/${x.name}`,
                parameters: x.input_schema || { type: "object", properties: {} } },
  }))];
}

function parseToolCall(text) {
  const m = String(text || "").match(/\{\s*"tool"\s*:\s*"([a-zA-Z_]+)"\s*,\s*"args"\s*:\s*(\{[\s\S]*?\})\s*\}/);
  if (!m) return null;
  try { return { tool: m[1], args: JSON.parse(m[2] || "{}"), raw: m[0] }; } catch (e) { return null; }
}

function fmtTree(entries, path) {
  const base = (path || "").replace(/\/+$/, "");
  const prefix = base ? base + "/" : "";
  return JSON.stringify((entries || [])
    .filter((e) => !base || e.path === base || e.path.startsWith(prefix))
    .map((e) => (e.dir ? e.path + "/" : `${e.path} (${e.size}B)`)));
}

async function runTool(c, name, args) {
  const out = (v) => ({ text: typeof v === "string" ? v : JSON.stringify(v), image: null });
  if (name === "list_files") {
    const d = await wsTree();
    return out(fmtTree(d.entries, args.path));
  }
  if (name === "read_file") {
    const d = await wsRead(args.path || "");
    return out(d.text != null ? d.text.slice(0, 40000) : `[binary ${d.mime || "file"}]`);
  }
  if (name === "write_file") {
    await wsWrite(args.path || "", args.text || "");
    return out({ ok: true, path: args.path, bytes: String(args.text || "").length });
  }
  if (name === "edit_file") {
    const d = await wsRead(args.path || "");
    const text = d.text != null ? d.text : "";
    const old = String(args.old_text || "");
    if (!old) return out({ ok: false, error: "old_text 不能为空" });
    const at = text.indexOf(old);
    if (at < 0) return out({ ok: false, error: "没找到 old_text（先 read_file 确认原文，包括缩进）" });
    await wsWrite(args.path, text.slice(0, at) + String(args.new_text || "") + text.slice(at + old.length));
    return out({ ok: true, path: args.path, replaced_at: at });
  }
  if (name === "search_files") {
    return out((await wsSearch(args.q || "")).hits || []);
  }
  if (name === "run_code") {
    const r = await wsRun(args.env || "python", args.code || "", Number(args.timeout) || 60, {
      files: args.files || undefined, in_place: !!args.in_place, cwd: args.cwd || undefined, force: !!args.force,
    });
    return out({ exit: r.exit, timed_out: r.timed_out, ms: r.ms, stdout: (r.stdout || "").slice(-12000),
                 stderr: (r.stderr || "").slice(-6000), files: r.files || [], cwd: r.cwd });
  }
  if (name === "start_job") {
    return out(await jobStart({ env: args.env || "lab", args: args.args || [],
                                cwd: args.cwd || "llm-study-lab", timeout: Number(args.timeout) || 1800,
                                force: !!args.force }));
  }
  if (name === "job_status") {
    const r = await jobStatus(args.id || "", Number(args.offset) || 0);
    return out({ ...r, log: (r.log || "").slice(-12000) });
  }
  if (name === "job_stop") return out(await jobStop(args.id || ""));
  if (name === "kb_add") {
    const r = await kbAdd({ name: args.name || "", text: args.text, path: args.path, url: args.url });
    return out(r);
  }
  if (name === "kb_search") {
    const r = await kbSearch(args.q || "", Number(args.k) || 6);
    return out({ count: r.count, reranked: r.reranked, results: (r.results || []).slice(0, 8) });
  }
  if (name === "search_web") {
    const r = await searchWeb(args.q || "", 5);
    return out(r.results || []);
  }
  if (name === "fetch_url") {
    return out(await fetchUrl(args.url || "", 8000));
  }
  if (name === "image_search") {
    return out(await imageSearch(args.q || "", 6));
  }
  if (name === "service_status") {
    const s = await fetchStatus();
    return out({
      lanes: (s.lanes || []).map((l) => ({ label: l.label, up: l.up, model: l.model })),
      services: (s.services || []).map((x) => ({ label: x.label, up: x.up, calls: x.calls, latency_ms: x.latency_ms })),
      gpus: (s.gpus || []).map((g) => ({ index: g.index, used_gib: +(g.used_mib / 1024).toFixed(1), util: g.util })),
      spark: ((s.spark || {}).cards || []).map((g) => ({ used_gib: +(g.used_mib / 1024).toFixed(1), util: g.util })),
    });
  }
  if (name === "generate_image") {
    // 参考图两种来源：用户本轮附的、以及 image_search 找到的直链（服务端抓回来转 base64）
    const refs = [];
    for (const m2 of [...(c.messages || [])].reverse()) {
      if (m2.role === "user" && m2.images && m2.images.length) { refs.push(...m2.images.map((x) => x.url)); break; }
    }
    for (const u of (args.refs || []).slice(0, 4)) {
      try { refs.push(await urlToDataURL(u)); } catch (e) { /* 抓不到就跳过 */ }
    }
    const r = await generateImage({ prompt: args.prompt || "", refs,
                                    size: args.size || "1024x1024", steps: Number(args.steps) || 8 });
    // 存进工作空间，页面用短 URL 显示（dataURL 会几 MB，塞进消息里整个会话都变重）
    let url = r.url, saved = null;
    try { saved = await persistImage(r.url); url = saved.url; } catch (e) { /* 存不进去就先用 dataURL 顶着 */ }
    // 工具结果要告诉模型「图已经出来了、在哪儿」；只给 seed 的话它会以为没生成，转去 list_files 找文件
    return { text: JSON.stringify({ ok: true, seed: r.seed, image_url: url,
                                    path: saved ? `images/${saved.name}` : null,
                                    note: "图片已生成并显示在对话里" }), image: url };
  }
  if (name === "embed_text") {
    const r = await embedText(args.input || "", { dims: 1024 });
    const v = (r.data && r.data[0] && r.data[0].embedding) || [];
    return out({ dim: v.length, head: v.slice(0, 8) });
  }
  if (name.startsWith("mcp__")) {
    const [, server, tool] = name.split("__");
    return out(await mcpCall(server, tool, args || {}));
  }
  return out({ error: `unknown tool: ${name}` });
}

/** 一轮流式生成（send 与 agent 的后续轮共用）。 */
async function streamTurn(c, m, controller, run = null) {
  const body = { model: engine.model, messages: apiMessages(c), stream: true, reasoning_effort: settings.thinking };
  if (settings.temperature > 0) {
    Object.assign(body, { temperature: +settings.temperature, top_p: +settings.top_p, top_k: +settings.top_k });
  } else {
    body.temperature = 0;
  }
  if (settings.seed) body.seed = +settings.seed;
  // 默认给足输出预算：agent 一轮要写文件/长解释，4096 常常写到一半就被截断；
  // 用户仍可在「采样与思考」里显式改小，网关会按上下文窗口再夹一次。
  body.max_tokens = settings.max ? +settings.max : (agentMode.value ? 16384 : 8192);
  if (projectionLoaded()) body.experimental_speed_projection = !!settings.esp;
  if (settings.mcp !== false && app.mcp.tools > 0) body.strata_mcp = true;
  if (agentMode.value) { body.tools = agentSchema(); body.tool_choice = "auto"; }

  let firstAt = null, thinkStart = null, usage = null, finish = null;
  m.finish = null;
  try {
    const r = await fetch("v1/chat/completions", { method: "POST", headers: headers(true),
                                                   body: JSON.stringify(body), signal: controller.signal });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { msg = (await r.json()).error.message || msg; } catch (e) { /* not json */ }
      if (r.status === 401) { msg = t("toast.nokey.sub"); openLogin(); }
      throw new Error(msg);
    }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) continue;          // ": keep-alive" 注释行（长提示词读取期间）
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let j;
        try { j = JSON.parse(data); } catch (e) { continue; }
        if (j.error) throw new Error(j.error.message || "the engine reported an error");
        if (j.usage) usage = j.usage;
        if (j.strata_mcp) onTool(m, j.strata_mcp);
        const ch0 = (j.choices && j.choices[0]) || null;
        if (ch0 && ch0.finish_reason) finish = ch0.finish_reason;   // length = 被 max_tokens 截断
        const d = (ch0 && ch0.delta) || {};
        // 原生 tool_calls 是分片来的：按 index 累积 name 与 arguments 字符串
        if (d.tool_calls && agentMode.value) {
          m.calls = m.calls || [];
          for (const tc of d.tool_calls) {
            const i = tc.index == null ? 0 : tc.index;
            const cur = m.calls[i] || (m.calls[i] = { id: tc.id, name: "", args: "" });
            if (tc.id) cur.id = tc.id;
            if (tc.function && tc.function.name) cur.name += tc.function.name;
            if (tc.function && tc.function.arguments) cur.args += tc.function.arguments;
          }
        }
        const lastTool = m.tools && m.tools.length ? m.tools[m.tools.length - 1] : null;
        if (d.reasoning_content) {
          if (!firstAt) firstAt = performance.now();
          if (!thinkStart) thinkStart = performance.now();
          if (lastTool && m.reasoning && lastTool.rat === m.reasoning.length) m.reasoning += "\n\n";
          m.reasoning += d.reasoning_content;
        }
        if (d.content) {
          if (!firstAt) firstAt = performance.now();
          if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
          if (lastTool && m.text && lastTool.at === m.text.length) m.text += "\n\n";
          m.text += d.content;
          if (c === chat.value) ttsFeed(m, d.content);
        }
        if (c === chat.value) queueScroll();
      }
    }
  } catch (e) {
    if (e.name === "AbortError") m.stopped = true;
    else { m.error = e.message || String(e); toast("error", t("toast.request.failed"), m.error, 6000); }
  }
  if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
  m.finish = m.stopped ? "stopped" : finish;
  const n = usage ? usage.completion_tokens : null;
  if (run) {
    // 整轮统计：不在每一段回答下面各写一行「耗时/工具数」，统一到输入框下面的汇总行。
    if (n) run.tokens += n;
    if (m.stopped) run.stopped = true;
    if (m.limit) run.limit = true;
  } else if (n && firstAt) {
    const secs = (performance.now() - firstAt) / 1000;
    m.meta = `${fmt(n)} tokens${secs > 0.25 ? ` · ${fmt(n / secs, 1)} tok/s` : ""}${m.stopped ? ` · ${t("chat.stopped")}` : ""}` +
             (projectionLoaded() ? (settings.esp ? ` · ${t("req.esp.on")}` : ` · ${t("req.esp.off")}`) : "");
  } else if (m.stopped) {
    m.meta = t("chat.stopped");
  }
  for (const tool of m.tools || []) if (tool.state === "writing" || tool.state === "running") { tool.state = "skipped"; tool.ms = null; }
  if (!run) {
    const ran = (m.tools || []).filter((x) => x.state === "done" || x.state === "error").length;
    if (ran) m.meta = `${m.meta ? `${m.meta} · ` : ""}${ran} tool call${ran > 1 ? "s" : ""}`;
  }
  if (!run && m.limit) m.meta = `${m.meta || ""} · stopped at the limit of ${m.limit} tool rounds (mcp.max_rounds)`;
  if (c === chat.value && ttsMsgRef === m) ttsFlush();
  // 不要在这里清 c.busy：agent 的工具轮次/下一轮还在跑，清掉会让界面看起来「已经停了」。
  // 整轮结束时由 finishRun 统一清（普通对话也在 finishRun 里立刻清）。
  saveChatRecord(c);
  if (c === chat.value) scrollDown(true);
}

/** 收集本轮的工具调用：优先原生 tool_calls，模型没按 schema 走时退回正文里的那行 JSON。 */
/** 引擎偶发把工具调用写进 reasoning（<tool_call><function=…>…）然后就 EOS。
 *  这种「只有 thinking」的回答里其实有完整调用：认出来、当正常 tool_calls 执行。 */
function parseReasoningToolCalls(reasoning) {
  const src = String(reasoning || "");
  if (!src.includes("<tool_call>")) return [];
  const re = /<tool_call>\s*<function=([A-Za-z0-9_]+)>\s*([\s\S]*?)<\/function>\s*<\/tool_call>/g;
  const pre = /<parameter=([A-Za-z0-9_]+)>\s*([\s\S]*?)\s*<\/parameter>/g;
  const out = [];
  let m;
  while ((m = re.exec(src))) {
    const name = m[1];
    const body = m[2] || "";
    const args = {};
    let p;
    pre.lastIndex = 0;
    while ((p = pre.exec(body))) args[p[1]] = p[2];
    if (name) out.push({ id: `reason_${out.length}_${Math.random().toString(36).slice(2, 7)}`,
                         name, args, native: true, raw: m[0] });
  }
  return out;
}

function collectCalls(m) {
  const out = [];
  for (const c of (m.calls || [])) {
    if (!c || !c.name) continue;
    let args = {};
    try { args = JSON.parse(c.args || "{}"); } catch (e) { args = {}; }
    out.push({ id: c.id || `call_${Math.random().toString(36).slice(2)}`, name: c.name, args, native: true });
  }
  if (out.length) return out;
  const t = parseToolCall(m.text);
  if (t) return [{ id: `call_${Date.now()}`, name: t.tool, args: t.args, native: false, raw: t.raw }];
  const fromReasoning = parseReasoningToolCalls(m.reasoning);
  if (fromReasoning.length) {
    // 调用已经从思考里认出来了；把 XML 从思考正文里拿掉，别让用户看到一坨标签
    m.reasoning = String(m.reasoning || "").replace(/<tool_call>[\s\S]*?<\/tool_call>/g, "").trim();
    return fromReasoning;
  }
  return [];
}

const CONTINUE_HINT = "上一条回答因长度上限被截断。请从断点自然接着写，不要重复已写内容，也不要重新开头或道歉。";
const EMPTY_HINT = "上一次回答只输出了思考就结束了，没有给用户任何内容。请直接给出最终回答；如果需要工具，请正常发起工具调用，不要把工具调用写进思考里。";
const RESUME_HINT = "上一次生成被页面刷新中断了。请从中断处继续完成当前任务；已经成功完成的工具调用不要重复执行。";

/** max_tokens 截断（finish_reason=length）时自动续写，最多 3 次；续写内容拼回同一条消息，
 *  用户看到的是完整回答，而不是「说到一半 + 一句被截断提示」。 */
async function continueIfLength(c, m, controller, run, maxTimes = 3) {
  let n = 0;
  while (m.finish === "length" && !m.stopped && !m.error && n < maxTimes) {
    n += 1;
    m.continueHint = CONTINUE_HINT;
    if (run) run.continues = (run.continues || 0) + 1;
    await streamTurn(c, m, controller, run);
  }
  m.continueHint = "";
}

/** 模型只输出思考就 EOS（引擎偶发）：自动补一句「直接回答」再试一次，
 *  避免用户看到一条只有 thinking、没有 content 的空回答。最多重试一次。 */
async function retryIfEmpty(c, m, controller, run, maxTimes = 1) {
  let n = 0;
  while (!m.stopped && !m.error && !(m.text || "").trim() && !(m.calls || []).length
         && !parseReasoningToolCalls(m.reasoning).length
         && (m.reasoning || "").trim() && n < maxTimes) {
    n += 1;
    m.continueHint = EMPTY_HINT;
    if (run) run.emptyRetries = (run.emptyRetries || 0) + 1;
    await streamTurn(c, m, controller, run);
  }
  m.continueHint = "";
}

/** Agent 一轮：执行工具 → 把结果放进上下文 → 自动续一轮（最多 AGENT_MAX_ROUNDS）。
 *  历史用标准 OpenAI 形状（assistant.tool_calls + role:"tool"），引擎能接。 */
async function agentFollowUp(c, m, calls, controller, run = null) {
  const round = m.agentRound || 1;
  m.tools = m.tools || [];
  // 工具执行期间也保持「正在生成」：否则发送按钮会回来、消息动作按钮会出现，看起来像停了
  c.busy = { controller, msg: m };
  if (run) {
    c.agentRun = { ...(c.agentRun || {}), active: true, round };
    saveChatRecord(c);
  }
  for (const call of calls) {
    const t0 = performance.now();
    const card = reactive({ id: call.id, name: call.name, tool: call.name, arguments: call.args,
                            at: m.text ? m.text.length : 0, rat: m.reasoning.length,
                            state: "running", round, startedAt: Date.now() });
    m.tools.push(card);
    if (run) saveChatRecord(c);       // 工具卡先落盘：长工具执行中刷新也能看到「运行中」
    let result = "", imgUrl = null;
    try {
      const r = await runTool(c, call.name, call.args);
      result = r.text;
      imgUrl = r.image || null;
      // 生成的图挂在**这张工具卡上**，在它自己的位置展开 —— 不再往消息顶部塞一张图
      Object.assign(card, { result, ok: true, state: "done", ms: Math.round(performance.now() - t0),
                            ...(imgUrl ? { image: imgUrl } : {}) });
    } catch (e) {
      result = `ERROR: ${e.message || e}`;
      Object.assign(card, { result, ok: false, state: "error", ms: Math.round(performance.now() - t0) });
    }
    if (!call.native && call.raw) m.text = String(m.text || "").replace(call.raw, "").trim();
  }
  if (run) {
    run.tools += calls.length;
    // 中间轮次不保留分段的 meta（历史记录里旧的 meta 也清掉），最后只在输入框下显示一行汇总
    m.meta = "";
  }
  saveChatRecord(c);

  if (round >= AGENT_MAX_ROUNDS) {
    if (run) run.limit = true;
    else m.meta = `${m.meta ? `${m.meta} · ` : ""}stopped at ${AGENT_MAX_ROUNDS} tool rounds`;
    return;
  }
  // 历史由 assistantMessages(m) 从 m.text + m.tools 重建：一轮工具一次往返，
// 不再额外维护 m.agentMsgs（同一条消息续写时它会把工具历史重复喂给模型）。

  // 续轮继续写进**同一条** assistant 消息：工具卡、旁白、后续正文按顺序排在一起，
  // 不会一轮一个气泡（那样看起来像「说完就停，又冒出一条」）。
  m.agentRound = round + 1;
  m.calls = [];
  m.finish = null;
  c.busy = { controller, msg: m };
  await nextTick();
  if (c === chat.value) scrollDown(true);
  await streamTurn(c, m, controller, run);
  await continueIfLength(c, m, controller, run);
  await retryIfEmpty(c, m, controller, run);
  if (agentMode.value && !m.stopped && !m.error) {
    const next = collectCalls(m);
    if (next.length) await agentFollowUp(c, m, next, controller, run);
  }
}

async function send() {
  unlockAudio();
  const text = input.value.trim();
  if ((!text && !attachments.value.length) || busy.value) return;
  const c = chat.value;
  if (!c) return;
  // 对话是对外的唯一门槛：没登录也没 token，就把人带到登录页（监控等页面不受影响）
  if (!auth.user && !apiKey()) {
    // 集市登录是整页跳走再跳回来，光靠「页面不卸载」留不住草稿 —— 落到本地，回来时再填回去
    store.set("draft", text);
    toast("warn", t("toast.nokey"), t("toast.nokey.sub"), 6000);
    openLogin();
    return;
  }
  const user = reactive({ role: "user", text,
                          images: attachments.value.filter((a) => a.kind !== "file").map((a) => reactive({ ...a })),
                          files: attachments.value.filter((a) => a.kind === "file").map((a) => reactive({ ...a })),
                          time: Date.now() });
  c.messages.push(user);
  attachments.value = [];
  input.value = "";
  store.set("draft", "");          // 发出去了，草稿就该清掉
  autosize();
  // 自动知识库检索：这句话能在个人资料里找到内容就直接带上，省一次工具往返
  kbContext.value = "";
  if (agentMode.value && autoKb.value && text) {
    try {
      const r = await kbSearch(text, 4);
      if ((r.results || []).length) {
        kbContext.value = r.results.map((x, i) => `[${i + 1}] ${x.name}：${String(x.text || "").slice(0, 700)}`).join("\n");
      }
    } catch (e) { /* 知识库不可用不影响对话 */ }
  }
  const m = reactive({ role: "assistant", text: "", reasoning: "", time: Date.now() });
  c.messages.push(m);
  // Agent 模式交给网关里的后台 runner：页面刷新/切页面都不会中断。
  if (agentMode.value) {
    await nextTick();
    scrollDown(true);
    await sendAgentServer(c, m);
    return;
  }
  // 整轮（含全部工具往返）的统计：最后只在输入框下面显示一行汇总
  const run = { t0: Date.now(), tokens: 0, tools: 0, msgs: [m], stopped: false, limit: false };
  await nextTick();
  scrollDown(true);

  const controller = new AbortController();
  c.busy = { controller, msg: m };
  await streamTurn(c, m, controller, run);
  await continueIfLength(c, m, controller, run);
  await retryIfEmpty(c, m, controller, run);
  // Agent 模式：本轮如果写了工具调用，就执行并自动续一轮（人不用再点发送）
  if (agentMode.value && !m.stopped && !m.error) {
    const calls = collectCalls(m);
    if (calls.length) await agentFollowUp(c, m, calls, controller, run);
  }
  finishRun(c, run);
}

/** 一次提问（含 agent 的多轮工具往返）结束后，落一行总耗时/总用量；不再逐段显示。 */
function finishRun(c, run) {
  c.busy = null;                 // 整轮（含全部工具往返）结束，发送按钮才回来
  c.agentRun = { active: false, finishedAt: Date.now() };
  const secs = Math.max(0, (Date.now() - run.t0) / 1000);
  c.lastRun = { secs: Math.round(secs * 10) / 10, tokens: run.tokens || 0, tools: run.tools || 0,
                stopped: !!run.stopped, limit: !!run.limit, at: Date.now() };
  for (const x of run.msgs) x.meta = "";
  saveChatRecord(c);
}

/** 提交给服务端 runner，然后从事件流重建同一条 assistant 消息。 */
async function sendAgentServer(c, m) {
  const controller = new AbortController();
  c.busy = { controller, msg: m };
  c.agentRun = { active: true, startedAt: Date.now(), runId: null, cursor: 0 };
  saveChatRecord(c);
  let r;
  try {
    r = await startAgentRun({
      chatId: c.id,
      model: engine.model,
      messages: apiMessages(c),
      maxRounds: AGENT_MAX_ROUNDS,
      params: { reasoning_effort: settings.thinking,
                ...(settings.temperature > 0
                  ? { temperature: +settings.temperature, top_p: +settings.top_p, top_k: +settings.top_k }
                  : { temperature: 0 }) },
    });
  } catch (e) {
    m.error = `agent 启动失败：${e.message || e}`;
    c.busy = null;
    c.agentRun = { active: false, error: String(e.message || e) };
    saveChatRecord(c);
    toast("error", "agent 启动失败", String(e.message || e), 8000);
    return;
  }
  c.agentRun = { active: true, runId: r.run_id, cursor: 0, startedAt: Date.now() };
  saveChatRecord(c);
  await readAgentStream(c, m, r.run_id, 0, false);
}

/** 把服务端事件应用到消息上（内容/思考/工具卡）。 */
function applyAgentEvent(c, m, ev) {
  const type = ev.type;
  if (type === "reasoning") {
    m.reasoning = (m.reasoning || "") + (ev.content || "");
  } else if (type === "content") {
    if (m.thinkSecs == null && m.reasoning) m.thinkSecs = (Date.now() - m.time) / 1000;
    const lt = (m.tools || []).slice(-1)[0];
    if (lt && m.text && lt.at === m.text.length) m.text += "\n\n";
    m.text = (m.text || "") + (ev.content || "");
    if (c === chat.value) ttsFeed(m, ev.content || "");
  } else if (type === "tool_call") {
    if (!m.tools) m.tools = [];
    m.tools.push(reactive({ id: ev.id, name: ev.name, tool: ev.name, arguments: ev.arguments || {},
                            at: (m.text || "").length, round: ev.round || 1,
                            state: "running", startedAt: Date.now() }));
  } else if (type === "tool_result") {
    const card = (m.tools || []).find((x) => x.id === ev.id);
    if (card) Object.assign(card, { result: ev.text, ok: !!ev.ok, state: ev.ok ? "done" : "error",
                                    ms: ev.ms, ...(ev.image ? { image: ev.image } : {}) });
  } else if (type === "usage") {
    m._usage = ev.usage;
  } else if (type === "done") {
    m.finish = ev.finish;
    if (ev.usage) m._usage = ev.usage;
  } else if (type === "error") {
    m.error = ev.message || "agent 执行出错";
  } else if (type === "cancelled") {
    m.stopped = true;
  }
}

/** 从 cursor 开始读事件流；fresh=true 时（刷新重连）先清空本地消息再按事件重放。 */
async function readAgentStream(c, m, runId, cursor, fresh = false) {
  if (!c.busy || !c.busy.controller) {
    c.busy = { controller: new AbortController(), msg: m };
  }
  const controller = c.busy.controller;
  let reader;
  try {
    reader = await openAgentStream(runId, cursor, controller.signal);
  } catch (e) {
    if (e.name === "AbortError") return;
    c.busy = null;
    const msg = String(e.message || e);
    if (/404|没有这个/.test(msg) && c.agentRun) c.agentRun = { active: false, error: msg };
    else if (c.agentRun) c.agentRun.reconnecting = false;
    toast("warn", "agent 连接失败", msg, 6000);
    return;
  }
  if (fresh) {
    m.text = "";
    m.reasoning = "";
    m.tools = [];
    m.images = [];
    m.calls = [];
    m.meta = "";
    m.error = null;
    m.finish = null;
    m.stopped = false;
    m._usage = null;
  }
  const dec = new TextDecoder();
  let buf = "";
  let terminal = false;
  let lastSave = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
        applyAgentEvent(c, m, ev);
        if (ev.seq && c.agentRun) c.agentRun.cursor = ev.seq;
        const isTerminal = ev.type === "done" || ev.type === "error" || ev.type === "cancelled";
        if (isTerminal || ev.type === "tool_call" || ev.type === "tool_result") {
          saveChatRecord(c);
          lastSave = Date.now();
        } else if (Date.now() - lastSave > 2000) {
          saveChatRecord(c);
          lastSave = Date.now();
        }
        if (c === chat.value) scrollDown(false);
        if (isTerminal) { terminal = true; break; }
      }
      if (terminal) break;
    }
  } catch (e) {
    if (e.name !== "AbortError") toast("warn", "agent 连接中断", String(e.message || e), 6000);
  } finally {
    try { reader.cancel(); } catch (e) { /* ignore */ }
  }
  if (terminal) {
    c.busy = null;
    ttsFlush();
    const usage = m._usage;
    if (usage) m.meta = `${usage.completion_tokens || 0} tokens`;
    c.agentRun = { active: false, runId, cursor: (c.agentRun && c.agentRun.cursor) || cursor,
                   finishedAt: Date.now() };
    c.lastRun = { secs: Math.max(0, Math.round((Date.now() - m.time) / 100) / 10),
                  tokens: (usage && usage.completion_tokens) || 0, tools: (m.tools || []).length,
                  stopped: !!m.stopped, limit: m.finish === "limit", at: Date.now() };
    saveChatRecord(c);
    if (c === chat.value) scrollDown(true);
  } else if (c.agentRun && c.agentRun.active) {
    // 连接断了但 run 还在服务端跑：稍后重连，刷新页面也会重新接上
    const n = (c.agentRun.retries || 0) + 1;
    c.agentRun.retries = n;
    if (n <= 6) {
      setTimeout(() => { if (c.agentRun && c.agentRun.active) readAgentStream(c, m, runId, c.agentRun.cursor || cursor, false); },
                 Math.min(5000, 500 * n));
    } else {
      c.busy = null;
    }
  }
}

/** 刷新后自动续跑：重放未完成的工具，然后让模型接着写。
 *  不能恢复「刷新那一刻正在传输的 token」，但能把整轮任务继续做完，而不是卡在运行中。 */
async function resumeAgent(c) {
  const m = [...(c.messages || [])].reverse().find((x) => x.role === "assistant");
  if (!m) { c.agentRun = { active: false, empty: true }; saveChatRecord(c); return; }
  if (!agentMode.value) agentMode.value = true;     // 这本来就是 agent 轮次
  const controller = new AbortController();
  c.busy = { controller, msg: m };
  const run = { t0: Date.now(), tokens: 0, tools: 0, msgs: [m], stopped: false, limit: false, resumed: true };
  try {
    // 未完成的工具重跑（读写/搜索/生图是幂等的；后台任务不敢自动重跑）
    const pending = (m.tools || []).filter((x) => x.state === "running" || x.state === "writing");
    for (const card of pending) {
      if (card.name === "start_job" || card.name === "job_stop") {
        Object.assign(card, { ok: false, state: "error",
          result: "页面刷新中断了这次任务；后台任务可能已经启动，请先用 job_status 确认，再决定是否重试。" });
        continue;
      }
      const t0 = performance.now();
      try {
        const r = await runTool(c, card.name, card.arguments || {});
        Object.assign(card, { result: r.text, ok: true, state: "done",
                              ms: Math.round(performance.now() - t0),
                              ...(r.image ? { image: r.image } : {}) });
      } catch (e) {
        Object.assign(card, { result: `ERROR: ${e.message || e}`, ok: false, state: "error",
                              ms: Math.round(performance.now() - t0) });
      }
      saveChatRecord(c);
    }
    const maxRound = Math.max(0, ...(m.tools || []).map((x) => x.round || 0));
    m.agentRound = Math.max(m.agentRound || 1, maxRound + 1);
    m.calls = [];
    m.finish = null;
    m.continueHint = RESUME_HINT;
    await streamTurn(c, m, controller, run);
    await continueIfLength(c, m, controller, run);
    await retryIfEmpty(c, m, controller, run);
    if (agentMode.value && !m.stopped && !m.error) {
      const next = collectCalls(m);
      if (next.length) await agentFollowUp(c, m, next, controller, run);
    }
  } catch (e) {
    m.error = m.error || `恢复未完成的任务失败：${e.message || e}`;
  } finally {
    finishRun(c, run);
  }
}

async function resumeIfNeeded() {
  const c = chat.value;
  if (!c || resuming.value || busy.value) return;
  if (!c.agentRun || !c.agentRun.active) return;
  resuming.value = true;
  try {
    if (c.agentRun.runId) {
      let m = [...(c.messages || [])].reverse().find((x) => x.role === "assistant");
      if (!m) {
        m = reactive({ role: "assistant", text: "", reasoning: "", time: Date.now() });
        c.messages.push(m);
      }
      // 从 0 重放：服务端事件是完整日志，刷新时本地可能只存了半截
      await readAgentStream(c, m, c.agentRun.runId, 0, true);
    } else {
      await resumeAgent(c);       // 旧版本客户端 run（没有 run_id）
    }
  } finally {
    resuming.value = false;
  }
}

function stop() {
  const c = chat.value;
  if (c && c.agentRun && c.agentRun.runId && c.agentRun.active) {
    stopAgentRun(c.agentRun.runId).catch(() => {});
    if (c.busy) c.busy.controller.abort();
    c.agentRun = { active: false, runId: c.agentRun.runId, stopped: true };
    saveChatRecord(c);
    ttsReset();
    return;
  }
  if (busy.value) busy.value.controller.abort();
  if (c) { c.agentRun = { active: false, stopped: true }; saveChatRecord(c); }
  ttsReset();
}

// ------------------------------------------------------------------ 流式语音（胡桃）/ 知识库
//
// 不把整段回答一次性丢给 TTS：回答还在流式生成时就按句切块（跳过代码块），
// 每句单独合成，前一块在播、后一块已经在合成。长回答的第一声能早十几秒出来。
function ttsReset() {
  ttsGen += 1;
  ttsQueue = [];
  ttsBuf = "";
  ttsMsgRef = null;
  ttsPlaying = false;
  ttsPrefetch = null;
  ttsPrefetchText = "";
  ttsBusy.value = false;
  if (audioEl.value) { try { audioEl.value.pause(); audioEl.value.removeAttribute("src"); } catch (e) {} }
  if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
}

/** 把「未闭合的 ``` 代码块」切出来：闭合之前的才允许朗读。 */
function ttsSafePrefix(s) {
  let open = -1, from = 0;
  for (;;) {
    const i = s.indexOf("```", from);
    if (i < 0) break;
    const j = s.indexOf("```", i + 3);
    if (j < 0) { open = i; break; }
    from = j + 3;
  }
  return open >= 0 ? [s.slice(0, open), s.slice(open)] : [s, ""];
}
const ttsStrip = (s) => s.replace(/```[\s\S]*?```/g, "\n").replace(/`[^`]*`/g, " ");

/** 从缓冲区里取出已经成句的块，塞进播放队列。 */
function ttsDrain() {
  let [safe, rest] = ttsSafePrefix(ttsBuf);
  safe = ttsStrip(safe);
  const parts = safe.match(/[^。！？!?；;…\n.]+[。！？!?；;…\n.]?/g) || [];
  let consumed = 0;
  for (const p of parts) {
    if (!/[。！？!?；;…\n.]$/.test(p)) break;      // 最后半句留在缓冲区，等下一批 token
    consumed += p.length;
    const text = p.trim();
    if (text.length >= 4) enqueueTts(text);
  }
  let tail = safe.slice(consumed);
  // 一直没标点：攒到 120 字先切一刀，别让第一声等太久
  if (tail.length >= 120) {
    enqueueTts(tail.slice(0, 120));
    tail = tail.slice(120);
  }
  ttsBuf = tail + rest;
}

function ttsFeed(m, delta, force = false) {
  if (!force && !autoSpeak.value) return;
  if (!delta) return;
  if (ttsMsgRef !== m) { ttsReset(); ttsMsgRef = m; }
  ttsBuf += delta;
  ttsDrain();
}

function ttsFlush() {
  if (!ttsMsgRef || !ttsBuf.trim()) return;
  const [safe] = ttsSafePrefix(ttsBuf);
  const text = ttsStrip(safe).trim();
  ttsBuf = "";
  if (text) enqueueTts(text.slice(0, 1200));
}

function enqueueTts(text) {
  const t = String(text || "").trim();
  if (!t) return;
  ttsQueue.push(t);
  // 被浏览器拦截时先攒着，不要每一块都去撞一次播放策略；用户点「继续朗读」再统一放
  if (!ttsPlaying && !ttsBlocked.value) playNextTts();
}

function prefetchTts() {
  if (ttsPrefetch || !ttsQueue.length) return;
  ttsPrefetchText = ttsQueue[0];
  ttsPrefetch = ttsSay(ttsPrefetchText.slice(0, 1200)).catch(() => null);
}

async function playNextTts() {
  if (ttsPlaying) return;
  const text = ttsQueue.shift();
  if (!text) { ttsBusy.value = false; return; }
  const gen = ttsGen;
  ttsPlaying = true;
  ttsBusy.value = true;
  try {
    const url = (ttsPrefetchText === text && ttsPrefetch) ? await ttsPrefetch : await ttsSay(text.slice(0, 1200));
    ttsPrefetch = null; ttsPrefetchText = "";
    if (gen !== ttsGen) { if (url) URL.revokeObjectURL(url); return; }
    if (!url) throw new Error("TTS 合成失败");
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = url;
    prefetchTts();
    const el = audioEl.value;
    if (!el) { ttsPlaying = false; ttsBusy.value = false; return; }
    el.src = url;
    try { await el.play(); ttsBlocked.value = false; }
    catch (e) {
      // 浏览器拦截自动播放：不弹错，把这一块放回队首，用户点「继续朗读」后从它开始
      ttsBlocked.value = true;
      ttsQueue.unshift(text);
      ttsPlaying = false;
      ttsBusy.value = false;
    }
  } catch (e) {
    if (gen === ttsGen) {
      ttsPlaying = false;
      ttsBusy.value = false;
      if (!ttsBlocked.value) toast("warn", "朗读失败", String(e.message || e), 6000);
      playNextTts();          // 跳过这一块，别把后面的也卡死
    }
  }
}

function onTtsEnded() {
  ttsPlaying = false;
  ttsBusy.value = false;
  playNextTts();
}

/** 手动点「朗读」：把这条消息切块后从头播。 */
function speakMessage(m) {
  if (!m || !m.text) return;
  ttsReset();
  ttsBlocked.value = false;       // 手动点击本身是用户手势，允许播放
  ttsMsgRef = m;
  ttsFeed(m, m.text, true);
  ttsFlush();
  if (!ttsPlaying) playNextTts();
}
async function saveToKb(m) {
  if (!m || !m.text) return;
  try {
    const r = await kbAdd({ name: `对话-${new Date().toISOString().slice(5, 16)}`, text: m.text });
    toast("success", "已存入知识库", `${r.chunks} 块`, 3000);
  } catch (e) { toast("error", "存入失败", String(e.message || e)); }
}

// 新对话。原版用一个「撤销」toast 兜底；现在会话都留在左侧栏里，不会丢，
// 所以不再需要撤销 —— 想回去点一下就行。
function newChat() {
  // Do not block on an in-flight answer: the old chat keeps streaming in the background
  // (its `busy` lives on the chat object), and the new chat can start independently.
  ttsReset();
  saveChatRecord(chat.value);
  startNewChat();
  nextTick(() => inputEl.value?.focus());
}

function exportChat() {
  if (!messages.value.length) { toast("info", t("toast.nothing.save")); return; }
  const tools = (m) => (m.tools || []).filter((x) => x.result != null).map((x) =>
    `<details><summary>Tool ${x.server ? `${x.server} / ` : ""}${x.tool || x.name}${x.ok ? "" : " (error)"}</summary>\n\n` +
    `\`\`\`json\n${JSON.stringify(x.arguments || {}, null, 2)}\n\`\`\`\n\n\`\`\`\n${x.result}\n\`\`\`\n\n</details>\n\n`).join("");
  const md = messages.value.map((m) => m.role === "user" ? `## You\n\n${m.text}\n` :
    `## ${engineLabel.value}\n\n${m.reasoning ? `<details><summary>Thinking</summary>\n\n${m.reasoning}\n\n</details>\n\n` : ""}${tools(m)}${m.text || m.error || ""}\n`).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
  a.download = `${FILE_SLUG}-chat-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// ---- 附件：按钮、拖拽、粘贴图片
const TEXT_EXT = /\.(txt|md|markdown|rst|tex|py|pyi|ipynb|js|mjs|cjs|ts|tsx|jsx|vue|svelte|json|jsonl|csv|tsv|log|ya?ml|toml|ini|cfg|conf|env|xml|html?|css|scss|less|c|cc|cpp|cxx|h|hh|hpp|cu|cuh|rs|go|java|kt|kts|swift|rb|php|pl|lua|r|jl|scala|sql|sh|bash|zsh|fish|ps1|psm1|bat|cmd|diff|patch|gradle|cmake|mk|dockerfile|gitignore|proto|graphql)$/i;
const MAX_TEXT_FILE = 512 * 1024;
function isTextFile(f) {
  return f.type.startsWith("text/") || /json|xml|javascript|yaml|toml|x-sh|x-python/.test(f.type) ||
         TEXT_EXT.test(f.name) || /(^|[\\/])(makefile|dockerfile|readme|license)$/i.test(f.name);
}
function addFiles(files) {
  for (const f of files) {
    if (f.type.startsWith("image/")) {
      if (!app.health.images) { toast("warn", t("toast.images.off"), t("toast.images.off.sub")); continue; }
      if (f.size > 20e6) { toast("warn", t("toast.image.big"), t("toast.image.big.sub", { name: f.name })); continue; }
      const r = new FileReader();
      r.onload = () => { attachments.value.push(reactive({ kind: "image", name: f.name || "pasted image", url: r.result })); };
      r.readAsDataURL(f);
      continue;
    }
    if (!isTextFile(f)) {
      toast("warn", t("toast.not.text"),
            t("toast.not.text.sub", { name: f.name, extra: app.health.images ? t("toast.or.pictures") : "" }));
      continue;
    }
    if (f.size > MAX_TEXT_FILE) { toast("warn", t("toast.file.big"), t("toast.file.big.sub", { name: f.name })); continue; }
    const r = new FileReader();
    r.onload = () => {
      const text = String(r.result);
      if (text.includes("\u0000")) { toast("warn", t("toast.binary"), t("toast.binary.sub", { name: f.name })); return; }
      attachments.value.push(reactive({ kind: "file", name: f.name, text }));
    };
    r.readAsText(f);
  }
}
function onFilePick(e) { addFiles(e.target.files); e.target.value = ""; }
function onDrop(e) {
  dragging.value = false;
  if (!e.dataTransfer || !e.dataTransfer.files.length) return;
  e.preventDefault();
  addFiles(e.dataTransfer.files);
  inputEl.value?.focus();
}
function onDragOver(e) {
  if (![...(e.dataTransfer || {}).types || []].includes("Files")) return;
  e.preventDefault();
  dragging.value = true;
}
function onPaste(e) {
  if (!app.health.images) return;
  const files = [...((e.clipboardData || {}).files || [])].filter((f) => f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); addFiles(files); }
}

// ---- 输入框
function autosize() {
  const el = inputEl.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, innerHeight * 0.4)}px`;
}
function onKeydown(e) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
}

// 正文里的代码块复制按钮（事件委托，与原 chat 容器上的处理一致）
function onChatClick(e) {
  const cc = e.target.closest("[data-code-copy]");
  if (cc) copyText(cc.closest(".st-code").querySelector("pre").textContent);
}

defineExpose({ startWith });
function startWith(q) { input.value = q; send(); }

// 换到另一场对话就换一批建议（同一场里不换，免得刚看中的那条被抽走）
watch(() => chat.value && chat.value.id, () => { rotate(); nextTick(resumeIfNeeded); });

onMounted(() => {
  document.addEventListener("pointerdown", unlockAudio, { once: true });
  // 首页的能力卡会开新页面并把提示词放在 URL query（同时兼容旧的 localStorage 键）
  const u = new URL(location.href);
  const qPrefill = u.searchParams.get("prefill") || "";
  if (qPrefill) {
    u.searchParams.delete("prefill");
    history.replaceState(null, "", u.pathname + (u.search ? u.search : "") + u.hash);
  }
  const prefill = qPrefill || store.get("chat.prefill", "");
  if (prefill) { store.set("chat.prefill", ""); input.value = prefill; nextTick(() => inputEl.value?.focus()); }
  if (!input.value) {
    const d = store.get("draft", "");   // 上次去登录时留下的草稿（用完就清，别串到别的会话）
    if (d) { input.value = d; store.set("draft", ""); }
  }
  autosize();
  // 空态里现在有「模型广场」卡片，内容比一屏高：滚到底会把标题顶掉，所以空态从顶部开始。
  if (messages.value.length) scrollDown(true);
  else nextTick(() => { if (scrollEl.value) scrollEl.value.scrollTop = 0; });
  loadSuggestions();
  startSuggestionLoop();
  const q = new URLSearchParams(location.search).get("q");     // /?q=... 直接开聊（快捷方式）
  if (q) { history.replaceState(null, "", location.pathname + location.hash); startWith(q); }
  // 上次刷新/关闭页面时还有 agent 轮次没跑完：自动接着跑
  nextTick(resumeIfNeeded);
});
onBeforeUnmount(() => {
  // ChatView unmounts when switching tabs: stop every chat's stream, not only the visible one.
  for (const c of chats.list) if (c.busy) c.busy.controller.abort();
  stopSuggestionLoop();
  ttsReset();
});
</script>

<template>
  <section class="view view--chat" :data-empty="empty ? '1' : null">
    <!-- 手机上才有：会话栏在手机上是抽屉，得有个东西把它叫出来。
         放正文里而不是页头 —— 页头里放「只在这页出现」的元素，切标签时整条页头会横移。 -->
    <button class="chat-menu" :title="t('chat.history')" :aria-label="t('chat.history')" @click="setSide(true)">
      <Icon name="panel" /></button>
    <div ref="scrollEl" class="chat-scroll">
      <div class="chat" @click="onChatClick">
        <div v-if="empty" class="chat-empty">
          <!-- 回到最初的空态：居中大标题 + 两列建议卡（「换一批」占第四格）。
               这是用户明确要的「最开始的样子」，别再改成左对齐的任务列表。 -->
          <div class="chat-empty__title">{{ t("chat.empty.title") }}</div>
          <div class="chat-empty__sub">{{ engineLabel }} — {{ t("chat.empty.sub") }}</div>
          <div class="chat-suggests">
            <button v-for="s in SUGGESTIONS" :key="s.text || s" class="chat-suggest" type="button" @click="useSuggestion(s)">
              <span class="chat-suggest__text">{{ s.text || s }}</span>
              <Icon name="arrow" cls="st-icon st-icon--sm chat-suggest__arrow" />
            </button>
            <button class="chat-suggest chat-suggest--more" type="button" @click="rotate()">
              <Icon name="refresh" cls="st-icon st-icon--sm" /><span>{{ t("chat.suggest.refresh") }}</span>
            </button>
          </div>
          <div class="chat-empty__links">
            <button type="button" @click="store.set('train.mode', 'code'); setTab('lab')">打开 LLM Lab</button>
            <button type="button" @click="store.set('lab.tab', 'kb'); store.set('train.mode', 'code'); setTab('lab')">{{ t("chat.open.kb") }}</button>
          </div>
        </div>
        <MessageItem v-for="(m, i) in messages" :key="m.key || i" :m="m" :i="i"
                     :streaming="!!streamingFor(m)" :show-thinking="settings.show"
                     @speak="speakMessage" @to-kb="saveToKb" />
      </div>
    </div>

    <div class="composer-wrap">
      <form class="st-composer" :class="{ dragging }" @submit.prevent="send"
            @dragover="onDragOver" @dragleave="dragging = false" @drop="onDrop">
        <audio ref="audioEl" class="chat-audio" @ended="onTtsEnded" @error="onTtsEnded"></audio>
        <div v-if="attachments.length" class="attachments">
          <span v-for="(a, i) in attachments" :key="i" class="chip">
            <Icon :name="a.kind === 'file' ? 'attach' : 'image'" cls="st-icon st-icon--sm" />{{ a.name }}
            <button type="button" class="st-btn st-btn--icon" :aria-label="t('common.remove')"
                    @click="attachments.splice(i, 1)"><Icon name="trash" /></button>
          </span>
        </div>
        <textarea ref="inputEl" v-model="input" rows="2" :placeholder="t('chat.placeholder')"
                  :aria-label="t('chat.aria.message')" @input="autosize" @keydown="onKeydown" @paste="onPaste"></textarea>
        <div class="st-composer__bar">
          <!-- 模式与通道都做成同一种下拉，统一在输入框下面这一行；不再在输入框上方堆一排胶囊 -->
          <select v-model="agentMode" class="st-input engine-select mode-select" :aria-label="t('chat.mode.aria')"
                  :title="t('chat.mode.hint')">
            <option :value="false">{{ t("chat.mode.chat") }}</option>
            <option :value="true">{{ t("chat.mode.agent") }}</option>
          </select>
          <button type="button" class="st-btn st-btn--secondary composer-new" :title="t('chat.new')"
                  @click="newChat"><Icon name="new-chat" cls="st-icon st-icon--sm" /><span>{{ t("chat.new") }}</span></button>
          <button type="button" class="st-btn st-btn--icon" :title="t('chat.attach')" :aria-label="t('chat.aria.attach')"
                  @click="fileEl.click()"><Icon name="attach" /></button>
          <input ref="fileEl" type="file" multiple hidden @change="onFilePick">
          <button type="button" class="st-btn st-btn--icon composer-export" :title="t('chat.export')" :aria-label="t('chat.aria.export')"
                  @click="exportChat"><Icon name="download" /></button>
          <button type="button" class="st-btn st-btn--icon composer-more" :title="t('chat.sampling')" :aria-label="t('chat.sampling')"
                  @click="emit('open-sampling')"><Icon name="settings" /></button>
          <label class="mode-toggle" :data-on="autoSpeak ? '1' : null" :data-busy="ttsBusy ? '1' : null"
                 title="回答边生成边用胡桃音色朗读（按句切块）">
            <input v-model="autoSpeak" type="checkbox"><Icon :name="ttsBusy ? 'refresh' : 'play'" cls="st-icon st-icon--sm" />
            <span>{{ ttsBusy ? t("chat.tts.reading") : t("chat.tts.read") }}</span>
          </label>
          <button v-if="ttsBlocked" type="button" class="st-btn st-btn--secondary composer-resume" title="浏览器拦截了自动播放，点这里继续"
                  @click="ttsBlocked = false; playNextTts()">
            <Icon name="play" cls="st-icon st-icon--sm" /><span>{{ t("chat.tts.resume") }}</span></button>
          <label v-if="agentMode" class="mode-toggle" :data-on="autoKb ? '1' : null" title="发送前自动检索个人知识库">
            <input v-model="autoKb" type="checkbox"><Icon name="memory" cls="st-icon st-icon--sm" /><span>知识库</span>
          </label>
          <span class="spacer"></span>
          <select class="st-input engine-select" :value="engine.model" :aria-label="t('chat.engine')"
                  :title="t('chat.engine.hint')" @change="setEngine($event.target.value)">
            <option v-for="e in ENGINES" :key="e.id" :value="e.id">{{ t(e.key) }}</option>
          </select>
          <button v-if="busy" type="button" class="st-btn st-btn--secondary" @click="stop">
            <Icon name="stop" cls="st-icon st-icon--sm" />{{ t("chat.stop") }}</button>
          <button v-else type="submit" class="st-btn st-btn--primary st-btn--send" :aria-label="t('chat.aria.send')"
                  :disabled="!input.trim() && !attachments.length"><Icon name="send" /></button>
        </div>
      </form>
      <!-- 一次提问（含 agent 的所有工具往返）的汇总：只在这里出现一行，不再每段回答各写一行 -->
      <div v-if="chat && chat.lastRun" class="chat-runstats">
        <Icon name="clock" cls="st-icon st-icon--sm" />
        <span>{{ t("chat.run.prefix") }}</span>
        <span>{{ t("chat.run.secs", { s: fmtDur(chat.lastRun.secs) }) }}</span>
        <span v-if="chat.lastRun.tokens">· {{ fmt(chat.lastRun.tokens) }} tokens</span>
        <span v-if="chat.lastRun.tools">· {{ t("chat.run.tools", { n: chat.lastRun.tools }) }}</span>
        <span v-if="chat.lastRun.limit">· {{ t("chat.run.limit") }}</span>
        <span v-if="chat.lastRun.stopped">· {{ t("chat.stopped") }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 输入框下方只有**一行**工具条：模式、通道、朗读/知识库都做成同一种小胶囊/下拉，
   不再在输入框上方堆第二排按钮。 */
.st-composer__bar { gap: 6px; }
.mode-select { min-width: 104px; height: 32px; font-weight: var(--st-fw-bold); }
.composer-new { height: 32px; padding: 0 10px; font-size: 12px; gap: 4px; white-space: nowrap; }
.mode-toggle { display: inline-flex; align-items: center; gap: 4px; height: 32px; padding: 0 9px;
               border: 1px solid var(--st-line); border-radius: var(--st-r-pill); background: var(--st-surface);
               color: var(--st-ink-muted); font-size: 12px; cursor: pointer; user-select: none; white-space: nowrap; }
.mode-toggle input { position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none; }
.mode-toggle[data-on] { color: var(--st-accent-text); border-color: color-mix(in srgb, var(--st-accent) 45%, var(--st-line));
                        background: var(--st-accent-tint); }
.mode-toggle:hover { color: var(--st-ink); }
.mode-toggle[data-busy] .st-icon { animation: ttsSpin 1s linear infinite; }
@keyframes ttsSpin { to { transform: rotate(360deg); } }
.composer-resume { height: 32px; padding: 0 10px; font-size: 12px; gap: 4px; white-space: nowrap; }
.chat-audio { display: none; }
@media (max-width: 720px) {
  .st-composer__bar { gap: 5px; flex-wrap: nowrap; overflow-x: auto; }
  .st-composer__bar .spacer { display: none; }
  .mode-select { flex: 0 0 76px; min-width: 0; width: 76px; height: 34px; font-size: 12px; padding: 0 6px; }
  .composer-new { flex: none; width: 34px; height: 34px; padding: 0; justify-content: center; }
  .composer-new span { display: none; }
  .composer-export, .composer-more { display: none; }
  .st-composer__bar .st-btn--icon { width: 34px; height: 34px; flex: none; }
  .mode-toggle { flex: none; width: 34px; height: 34px; padding: 0; justify-content: center; }
  .mode-toggle span { display: none; }
  .composer-resume { width: 34px; height: 34px; padding: 0; justify-content: center; flex: none; }
  .composer-resume span { display: none; }
  .st-composer__bar .engine-select { flex: 0 0 62px; width: 62px; min-width: 0; max-width: 62px; padding: 0 6px; }
  .st-composer__bar .st-btn--send { width: 38px; height: 38px; flex: none; }
}
</style>

<style scoped>
/* 空态回到 public/app.css 最初的那套（居中大标题 + 两列建议卡 + 第四格「换一批」+ 入场浮起），
   这里只补两个次级入口的排版；不要在本组件里再覆盖 .chat-empty/.chat-suggest 的几何。 */
.chat-empty__links { display: flex; justify-content: center; gap: 16px; margin-top: 14px; }
.chat-empty__links button { border: 0; background: none; padding: 0; font: inherit; font-size: 12px;
                            color: var(--st-accent-text); cursor: pointer; }
.chat-empty__links button:hover { text-decoration: underline; }
@media (max-width: 720px) {
  .chat-empty { margin-top: 8vh; }
  .chat-suggests { grid-template-columns: minmax(0, 1fr); max-width: 100%; }
}
/* 输入框下面的整轮汇总：一行小字，不参与消息流 */
.chat-runstats { display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 5px;
                 margin-top: 7px; color: var(--st-ink-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.chat-runstats .st-icon { opacity: .7; }
</style>
