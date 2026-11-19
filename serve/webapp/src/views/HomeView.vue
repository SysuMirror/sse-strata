<script setup>
// 首页 = 工作台。刻意做成「控制台」而不是「能力卡广场」：
// 一屏里只有最近对话、文件和右侧的工作区状态，能力说明收成一行文字。
// 生图/知识库/语音不是独立入口，它们是 agent 的工具。
import { ref, reactive, computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { app } from "../store.js";
import { fmt, toast } from "../api.js";
import { openLogin } from "../auth.js";
import { activeChat, chats, newChat, openChat, saveActive, titleOf } from "../chats.js";
import { getWorkspace, saveSkill, deleteSkill, saveMcp, deleteMcp, mcpList } from "../hub.js";
import { tree as wsTree, kbDelete } from "../workspace.js";
import Icon from "../components/Icon.vue";
import ImageViewer from "../components/ImageViewer.vue";

const { t } = useI18n();
const ws = reactive({ loaded: false, lab: null, kb: { docs: [], count: 0, chunks: 0 }, jobs: [],
                      images: [], skills: [], mcp: [], files: [], error: "" });
const needLogin = ref(false);
const viewer = ref(null);
const busy = ref(false);
const mcpForm = reactive({ name: "", url: "", status: "" });

const services = computed(() => (app.concurrency && app.concurrency.services) || []);
const online = computed(() => services.value.filter((s) => s.up).length);
const total = computed(() => services.value.length || 0);
const recentFiles = computed(() => ws.files.filter((f) => !f.dir).sort((a, b) => (b.mtime || 0) - (a.mtime || 0)).slice(0, 6));
const runningJobs = computed(() => ws.jobs.filter((j) => j.status === "running"));

/** 最近一次有内容的对话：首页的主入口，点回去状态原样还在。 */
const lastChat = computed(() => {
  const c = activeChat.value;
  if (c && c.messages && c.messages.length) return c;
  return [...(chats.list || [])]
    .filter((x) => x.messages && x.messages.length)
    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))[0] || null;
});
/** 除当前这条以外的最近对话（有就列出来，没有就显示一行空态）。 */
const otherChats = computed(() => [...(chats.list || [])]
  .filter((c) => c.messages && c.messages.length && c.id !== (lastChat.value && lastChat.value.id))
  .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)).slice(0, 3));

const chatSnippet = (c) => {
  const m = [...(c.messages || [])].reverse().find((x) => x.text);
  return String((m && m.text) || "").replace(/\s+/g, " ").slice(0, 140);
};
const whenStr = (ts) => {
  const d = new Date(ts || Date.now());
  const now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
};

async function load() {
  ws.error = "";
  needLogin.value = false;
  try {
    const [m, tree] = await Promise.all([getWorkspace(), wsTree()]);
    ws.lab = m.lab || null;
    ws.kb = m.kb || { docs: [], count: 0, chunks: 0 };
    ws.jobs = m.jobs || [];
    ws.images = m.images || [];
    ws.skills = m.skills || [];
    ws.mcp = m.mcp || [];
    ws.files = (tree && tree.entries) || [];
    ws.loaded = true;
  } catch (e) {
    const msg = String(e.message || e);
    if (/401|登录|NOT_LOGGED_IN|token/i.test(msg)) needLogin.value = true;
    else ws.error = msg;
    ws.loaded = true;
  }
}
onMounted(load);

/** 首页的动作一律开新页面：不把当前这个工作台页面顶掉，可以同时开多个对话/文件。
 *  要打开的文件/预填提示词放 URL query 里（不用 localStorage），同时开多个 tab 也不打架。 */
function openNew(hash, params = {}) {
  const q = new URLSearchParams(params).toString();
  const url = `${location.origin}${location.pathname}${q ? "?" + q : ""}${hash}`;
  const w = window.open(url, "_blank");
  if (w) { try { w.opener = null; } catch (e) { /* 同源，忽略 */ } }
  else toast("warn", "浏览器拦截了新窗口", "允许此站点弹出窗口后重试", 6000);
  return w;
}
function startChat(prefill = "") {
  saveActive();
  // 每次开新页面都要是一条独立会话：两个 tab 共用一个 chat id 会互相覆盖
  newChat();
  openNew("#chat", prefill ? { prefill } : {});
}
function resumeChat(c) {
  if (!c) return;
  openChat(c.id);
  openNew("#chat");
}
function openFeature(kind) {
  if (kind === "kb") return openKB();
  if (kind === "img") return startChat(t("home.feat.img.prompt"));
  if (kind === "tts") return startChat(t("home.feat.tts.prompt"));
  return startChat();
}
function openLab(path = "") {
  openNew("#lab", path ? { open: path } : {});
}
function openKB() {
  openNew("#lab", { tab: "kb" });
}

async function onSkillFile(e) {
  busy.value = true;
  try {
    for (const f of [...(e.target.files || [])]) {
      if (!/\.md$/i.test(f.name)) { toast("warn", "只收 .md", f.name); continue; }
      await saveSkill(f.name.replace(/\.md$/i, ""), await f.text());
    }
    await load();
  } catch (err) { toast("error", "上传失败", String(err.message || err)); }
  finally { e.target.value = ""; busy.value = false; }
}
async function rmSkill(name) {
  try { await deleteSkill(name); await load(); } catch (e) { toast("error", "删除失败", String(e.message || e)); }
}
async function addMcp() {
  if (!mcpForm.name.trim() || !mcpForm.url.trim()) { toast("warn", "名称和地址都要填"); return; }
  busy.value = true;
  try {
    await saveMcp(mcpForm.name.trim(), mcpForm.url.trim());
    mcpForm.name = ""; mcpForm.url = ""; mcpForm.status = "";
    await load();
  } catch (e) { toast("error", "保存失败", String(e.message || e)); }
  finally { busy.value = false; }
}
async function rmMcp(name) {
  try { await deleteMcp(name); await load(); } catch (e) { toast("error", "删除失败", String(e.message || e)); }
}
async function checkMcp() {
  mcpForm.status = "检查中…";
  try {
    const r = await mcpList();
    mcpForm.status = (r.servers || []).map((x) => `${x.name}: ${x.ok ? x.tools.length + " 个工具" : (x.error || "失败")}`).join(" · ") || "没有配置";
  } catch (e) { mcpForm.status = String(e.message || e); }
}
async function rmDoc(d) {
  if (!confirm(`从知识库删除「${d.name}」？`)) return;
  try { await kbDelete(d.id); await load(); } catch (e) { toast("error", "删除失败", String(e.message || e)); }
}
</script>

<template>
  <section class="view view--home">
    <div class="home">
      <header class="home-head">
        <div>
          <h1 class="home-head__title">工作台</h1>
          <p class="home-head__sub">继续上次对话，或者从文件和资料开始。</p>
        </div>
        <div class="home-head__meta">
          <span class="home-dot" :data-up="online ? '1' : '0'"></span>
          <span>{{ t("home.online", { up: online, n: total }) }}</span>
        </div>
      </header>

      <div class="home-actions">
        <button class="st-btn st-btn--primary" @click="startChat()">
          <Icon name="new-chat" cls="st-icon st-icon--sm" />{{ t("home.newchat") }}</button>
        <button class="st-btn st-btn--secondary" @click="openLab()">
          <Icon name="cpu" cls="st-icon st-icon--sm" />{{ t("home.openlab") }}</button>
        <span class="home-actions__hint">Agent 内置：文件 · 代码 · 知识库 · 生图（可先联网找参考图）· 语音</span>
      </div>

      <p v-if="needLogin" class="home-login">
        <Icon name="info" cls="st-icon st-icon--sm" />
        登录后会自动创建你的 LLM Lab 和个人知识库。
        <button class="st-btn st-btn--secondary" @click="openLogin()">{{ t("login.button") }}</button>
      </p>

      <!-- 最近一次对话：整行可点，回到原会话（消息 / 工具 / 图片都在） -->
      <button v-if="lastChat" type="button" class="home-resume" @click="resumeChat(lastChat)">
        <span class="home-resume__label">{{ t("home.resume.cta") }}</span>
        <span class="home-resume__title">{{ titleOf(lastChat) }}</span>
        <span class="home-resume__snippet">{{ chatSnippet(lastChat) }}</span>
        <span class="home-resume__meta">{{ t("home.resume.meta", { n: lastChat.messages.length, time: whenStr(lastChat.updatedAt) }) }} · {{ t("home.resume.saved") }}</span>
        <span class="home-resume__arrow">→</span>
      </button>

      <div class="home-cols">
        <main class="home-main">
          <section class="home-sec">
            <header class="home-sec__head">
              <h2>对话</h2>
              <button type="button" @click="startChat()">新建</button>
            </header>
            <ul v-if="otherChats.length" class="home-rows">
              <li v-for="c in otherChats" :key="c.id">
                <button type="button" class="home-row" @click="resumeChat(c)">
                  <span class="home-row__title">{{ titleOf(c) }}</span>
                  <span class="home-row__time">{{ whenStr(c.updatedAt) }}</span>
                  <span class="home-row__sub">{{ chatSnippet(c) }}</span>
                </button>
              </li>
            </ul>
            <p v-else class="home-empty">没有更早的对话。新建一个，或者点上面的继续。</p>
          </section>

          <section class="home-sec">
            <header class="home-sec__head">
              <h2>文件</h2>
              <button type="button" @click="openLab()">全部</button>
            </header>
            <ul v-if="recentFiles.length" class="home-rows">
              <li v-for="f in recentFiles" :key="f.path">
                <button type="button" class="home-row home-row--file" @click="openLab(f.path)">
                  <span class="home-row__title">{{ f.path }}</span>
                  <span class="home-row__time">{{ whenStr(f.mtime * 1000) }}</span>
                  <span class="home-row__sub">{{ f.size > 1024 ? fmt(f.size / 1024, 1) + " KB" : f.size + " B" }}</span>
                </button>
              </li>
            </ul>
            <p v-else class="home-empty">工作空间里还没有文件。LLM Lab 会在你登录后自动创建。</p>
          </section>
        </main>

        <aside class="home-side">
          <section class="home-block">
            <header class="home-sec__head"><h2>LLM Lab</h2>
              <button type="button" @click="openLab()">打开</button></header>
            <p class="home-kv"><span>工程</span><b>llm-study-lab</b></p>
            <p class="home-kv"><span>环境</span><b>PyTorch 2.6 · GPU{{ ws.lab ? ws.lab.gpu : "2" }}</b></p>
            <p class="home-kv"><span>进度</span><b>27 个 TODO · 9 个阶段</b></p>
            <button type="button" class="home-textbtn" @click="openLab('llm-study-lab/START_HERE.md')">
              打开 START_HERE →</button>
          </section>

          <section class="home-block">
            <header class="home-sec__head"><h2>知识库</h2>
              <button type="button" @click="openKB()">管理</button></header>
            <p class="home-kv"><span>资料</span><b>{{ ws.kb.count }} 份 · {{ ws.kb.chunks }} 块</b></p>
            <ul v-if="ws.kb.docs.length" class="home-side-list">
              <li v-for="d in ws.kb.docs.slice(0, 3)" :key="d.id">
                <button type="button" @click="openKB()">{{ d.name }}</button>
                <span>{{ d.chunks }} 块</span>
              </li>
            </ul>
            <p v-else class="home-empty">还没有资料。上传 PDF/Markdown 或粘贴文本，提问时自动检索。</p>
          </section>

          <section v-if="ws.images.length" class="home-block">
            <header class="home-sec__head"><h2>最近生成</h2>
              <button type="button" @click="openFeature('img')">生图</button></header>
            <div class="home-thumbs">
              <button v-for="im in ws.images.slice(0, 3)" :key="im.name" type="button" class="home-thumb"
                      :title="`查看 / 下载 ${im.name}`" @click="viewer = { src: im.url, name: im.name }">
                <img :src="im.url" :alt="im.name" loading="lazy">
              </button>
            </div>
          </section>

          <section class="home-block">
            <details class="home-details">
              <summary>
                <span>Skill / MCP</span>
                <span class="home-details__meta">{{ ws.skills.length }} / {{ ws.mcp.length }}</span>
              </summary>
              <div class="home-details__body">
                <div class="home-details__head">
                  <span>Skill</span>
                  <label class="home-link">上传 .md
                    <input type="file" accept=".md,text/markdown" multiple hidden @change="onSkillFile"></label>
                </div>
                <ul v-if="ws.skills.length" class="home-side-list">
                  <li v-for="k in ws.skills" :key="k.name">
                    <button type="button" @click="openLab('skills/' + (k.file || k.name + '.md'))">{{ k.title || k.name }}</button>
                    <button type="button" class="home-x" @click="rmSkill(k.name)">✕</button>
                  </li>
                </ul>
                <p v-else class="home-empty">还没有 skill。</p>

                <div class="home-details__head home-details__head--mt">
                  <span>MCP</span>
                  <button type="button" class="home-link" @click="checkMcp">检查</button>
                </div>
                <ul v-if="ws.mcp.length" class="home-side-list">
                  <li v-for="m in ws.mcp" :key="m.name">
                    <span>{{ m.name }}</span>
                    <button type="button" class="home-x" @click="rmMcp(m.name)">✕</button>
                  </li>
                </ul>
                <div class="home-mcp-form">
                  <input v-model="mcpForm.name" class="st-input" :placeholder="t('ws.mcp.name')">
                  <input v-model="mcpForm.url" class="st-input" :placeholder="t('ws.mcp.url')">
                  <button class="st-btn st-btn--secondary" :disabled="busy" @click="addMcp">添加</button>
                </div>
                <p v-if="mcpForm.status" class="home-empty">{{ mcpForm.status }}</p>
              </div>
            </details>
          </section>
        </aside>
      </div>

      <footer class="home-tools">
        <span>Agent 工具</span>
        <button type="button" @click="openFeature('img')">生图</button>
        <button type="button" @click="openFeature('tts')">胡桃语音</button>
        <button type="button" @click="openKB()">知识库</button>
        <button type="button" @click="startChat()">代码 / 测试 / 训练</button>
      </footer>

      <p v-if="ws.error" class="home-empty">{{ ws.error }}</p>
    </div>
    <ImageViewer v-if="viewer" :src="viewer.src" :name="viewer.name" @close="viewer = null" />
  </section>
</template>

<style scoped>
.view--home { padding: 28px 28px 40px; overflow: auto; }
.home { max-width: 1120px; margin: 0 auto; display: grid; gap: 22px; }

.home-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px;
             padding-bottom: 14px; border-bottom: 1px solid var(--st-line); }
.home-head__title { margin: 0; font-size: 26px; font-weight: 700; letter-spacing: -.01em; line-height: 1.2; }
.home-head__sub { margin: 5px 0 0; color: var(--st-ink-muted); font-size: 14px; }
.home-head__meta { display: inline-flex; align-items: center; gap: 7px; color: var(--st-ink-muted);
                   font-size: 12px; white-space: nowrap; }
.home-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--st-danger, #e0283f); }
.home-dot[data-up="1"] { background: var(--st-success, #0e9f6e); }

.home-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.home-actions .st-btn { height: 38px; }
.home-actions__hint { color: var(--st-ink-muted); font-size: 12px; margin-left: 2px; }
.home-login { display: flex; align-items: center; gap: 8px; margin: 0; padding: 10px 12px;
              border: 1px solid var(--st-line); border-radius: 8px; background: var(--st-surface);
              color: var(--st-ink-soft); font-size: 13px; flex-wrap: wrap; }
.home-login .st-btn { height: 30px; padding: 0 12px; font-size: 12px; }

/* 继续对话：一条带左强调线的行，不是卡片 */
.home-resume { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 2px 14px;
               align-items: center; width: 100%; padding: 13px 16px; text-align: left; font: inherit;
               color: inherit; cursor: pointer; background: var(--st-surface); border: 1px solid var(--st-line);
               border-left: 3px solid var(--st-accent); border-radius: 8px; }
.home-resume:hover { border-color: var(--st-accent); }
.home-resume__label { grid-row: 1 / span 2; align-self: center; font-size: 11px; font-weight: 700;
                      color: var(--st-accent-text); letter-spacing: .02em; }
.home-resume__title { font-size: 15px; font-weight: 700; color: var(--st-ink); overflow: hidden;
                      text-overflow: ellipsis; white-space: nowrap; }
.home-resume__snippet { grid-column: 2; font-size: 12.5px; color: var(--st-ink-muted); overflow: hidden;
                        text-overflow: ellipsis; white-space: nowrap; }
.home-resume__meta { grid-column: 2; font-size: 11px; color: var(--st-ink-muted); font-variant-numeric: tabular-nums; }
.home-resume__arrow { grid-row: 1 / span 2; grid-column: 3; color: var(--st-accent); font-size: 18px; }

.home-cols { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(260px, .9fr); gap: 34px; }
.home-main { display: grid; gap: 26px; align-content: start; min-width: 0; }
.home-side { display: grid; gap: 24px; align-content: start; min-width: 0;
             border-left: 1px solid var(--st-line); padding-left: 26px; }
.home-block { display: grid; gap: 7px; min-width: 0; }
.home-sec { display: grid; gap: 6px; min-width: 0; }
.home-sec__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.home-sec__head h2 { margin: 0; font-size: 13px; font-weight: 700; color: var(--st-ink); }
.home-sec__head button, .home-textbtn, .home-link { border: 0; background: none; padding: 0; cursor: pointer;
  font: inherit; font-size: 12px; color: var(--st-ink-muted); }
.home-sec__head button:hover, .home-textbtn:hover, .home-link:hover { color: var(--st-accent-text); }
.home-textbtn { justify-self: start; margin-top: 2px; color: var(--st-accent-text); }

.home-rows { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--st-line-soft); }
.home-rows li { border-bottom: 1px solid var(--st-line-soft); }
.home-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 1px 12px; width: 100%;
            padding: 10px 6px; text-align: left; background: none; border: 0; cursor: pointer;
            font: inherit; color: inherit; }
.home-row:hover { background: var(--st-surface-2); }
.home-row__title { font-size: 13.5px; color: var(--st-ink); overflow: hidden; text-overflow: ellipsis;
                   white-space: nowrap; }
.home-row__time { font-size: 11px; color: var(--st-ink-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.home-row__sub { grid-column: 1 / -1; font-size: 12px; color: var(--st-ink-muted); overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
.home-empty { margin: 4px 0 0; color: var(--st-ink-muted); font-size: 12px; line-height: 1.6; }

.home-kv { display: flex; justify-content: space-between; gap: 12px; margin: 0; font-size: 12.5px;
           color: var(--st-ink-muted); }
.home-kv b { font-weight: 500; color: var(--st-ink-soft); text-align: right; }
.home-side-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.home-side-list li { display: flex; align-items: center; justify-content: space-between; gap: 8px;
                     font-size: 12.5px; color: var(--st-ink-soft); min-width: 0; }
.home-side-list li button { border: 0; background: none; padding: 0; font: inherit; text-align: left;
                            color: var(--st-ink-soft); cursor: pointer; overflow: hidden; text-overflow: ellipsis;
                            white-space: nowrap; }
.home-side-list li button:hover { color: var(--st-accent-text); }
.home-side-list li span { flex: none; color: var(--st-ink-muted); font-size: 11px; }

.home-thumbs { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.home-thumb { border: 0; padding: 0; background: none; cursor: zoom-in; border-radius: 6px; overflow: hidden; }
.home-thumb img { display: block; width: 100%; aspect-ratio: 1/1; object-fit: cover; border: 1px solid var(--st-line); }

.home-details { border-top: 1px solid var(--st-line-soft); padding-top: 8px; }
.home-details summary { display: flex; align-items: center; justify-content: space-between; cursor: pointer;
                        list-style: none; font-size: 13px; font-weight: 700; color: var(--st-ink); }
.home-details summary::-webkit-details-marker { display: none; }
.home-details__meta { font-size: 11px; font-weight: 400; color: var(--st-ink-muted); }
.home-details__body { display: grid; gap: 7px; padding-top: 10px; }
.home-details__head { display: flex; align-items: center; justify-content: space-between; font-size: 12px;
                      color: var(--st-ink-muted); }
.home-details__head--mt { margin-top: 8px; }
.home-mcp-form { display: grid; grid-template-columns: 1fr; gap: 6px; }
.home-mcp-form .st-input { height: 30px; font-size: 12px; }
.home-mcp-form .st-btn { height: 30px; font-size: 12px; }
.home-x { border: 0; background: none; color: var(--st-ink-muted); cursor: pointer; padding: 0 2px; font-size: 11px; }
.home-x:hover { color: var(--st-danger-text, #b01a2e); }

.home-tools { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding-top: 14px;
              border-top: 1px solid var(--st-line); font-size: 12px; color: var(--st-ink-muted); }
.home-tools > span:first-child { font-weight: 700; color: var(--st-ink-soft); }
.home-tools button { border: 0; background: none; padding: 0; cursor: pointer; font: inherit;
                     color: var(--st-accent-text); }
.home-tools button:hover { text-decoration: underline; }

@media (max-width: 900px) {
  .home-cols { grid-template-columns: 1fr; gap: 22px; }
  .home-side { border-left: 0; padding-left: 0; border-top: 1px solid var(--st-line); padding-top: 20px;
               grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px 24px; }
  .home-side .home-block:last-child { grid-column: 1 / -1; }
}
@media (max-width: 720px) {
  .view--home { padding: 18px 14px calc(20px + env(safe-area-inset-bottom)); }
  .home { gap: 18px; }
  .home-head { align-items: flex-start; flex-direction: column; gap: 6px; }
  .home-head__title { font-size: 23px; }
  .home-actions .st-btn { height: 42px; }
  .home-actions__hint { display: none; }
  .home-resume { grid-template-columns: 1fr auto; padding: 12px; row-gap: 3px; }
  .home-resume__label { grid-column: 1; grid-row: 1; }
  .home-resume__title { grid-column: 1; grid-row: 2; }
  .home-resume__snippet { grid-column: 1 / -1; grid-row: 3; }
  .home-resume__meta { grid-column: 1 / -1; grid-row: 4; }
  .home-resume__arrow { grid-column: 2; grid-row: 1 / span 2; }
  .home-side { grid-template-columns: 1fr; }
  .home-tools { gap: 10px; }
}
</style>