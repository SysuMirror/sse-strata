<script setup>
// 应用外壳：左侧栏（品牌 / 视图导航 / 会话历史）+ 页头（视图名、状态胶囊、主题、语言）
// + 各视图 + 采样抽屉 + toast。
import { ref, computed, watch, onMounted, onBeforeUnmount, defineAsyncComponent } from "vue";
import { useI18n } from "vue-i18n";
import { app, setTab, setSide, flipTheme, applySystemTheme, startPolling, VIEWS } from "./store.js";
import { LANGS, setLang } from "./i18n.js";
import { apiKey, toasts, hide, fmt } from "./api.js";
import { setMarkdownLabels } from "./markdown.js";
import { initChats, pullCloud } from "./chats.js";
import { auth, backToApp, fetchAuthConfig, fetchMe, login, logout } from "./auth.js";
import { NAME } from "./brand.js";
import Icon from "./components/Icon.vue";
import SideBar from "./components/SideBar.vue";
import LoginView from "./views/LoginView.vue";
import ChatView from "./views/ChatView.vue";
import MonitorView from "./views/MonitorView.vue";
import RecordsView from "./views/RecordsView.vue";
import AboutView from "./views/AboutView.vue";
import SamplingDrawer from "./components/SamplingDrawer.vue";
import VizLoading from "./viz/VizLoading.vue";

// 可视化页按需加载：它带着 three.js（约 200KB gzip），不该让聊天页的用户一起下载。
// 动态 import 出来的 chunk 落在产物根下（chunk-<hash>.js，扁平、无子目录），
// 与引擎只服务 /web/<扁平名字> 的约束一致 —— 见 README「必须知道的约束」。
const VizView = defineAsyncComponent({ loader: () => import("./viz/VizView.vue"), loadingComponent: VizLoading, delay: 0 });
// 训练页同样按需加载（只有在看训练曲线的人才会下载它）
const TrainView = defineAsyncComponent({ loader: () => import("./train/TrainView.vue"), loadingComponent: VizLoading, delay: 0 });

const { t, locale } = useI18n();
const drawerOpen = ref(false);

// 登录**不**拦整个应用 —— 只有发对话时才需要凭据（见 ChatView.send）。
// 登录页是应用内的一页（#login），页头照常在，随时能切回别的标签页。
// 动态绑定：字面量 src 会被 Vite 当模块解析（public/ 里的文件解析不到，构建直接失败）
const logoSrc = "/web/logo.svg";

// 状态胶囊报的是**整台机器**的聚合状况：两条通道、所有在途请求全加起来，不区分是谁的。
// 数据取自代理的 /concurrency（它按通道汇总），比引擎单实例那份 live 更全 ——
// 而且引擎那份 status/rate 是整个实例共用的，多请求并发时会互相覆盖（见 server.py:1253），
// 拿它当「我现在怎样」本来就不准。没有并发数据时（单路直连引擎）再退回单引擎那份。
const agg = computed(() => (app.concurrency && app.concurrency.aggregate) || null);

// 速率是引擎按 2 秒窗口算的，请求刚开始时样本太少，算出来会低得离谱（实测开头几秒只有
// 1~2 tok/s，看着像坏了）。所以「刚开始生成」的头一秒只报状态、不报速率。
let genSince = 0;
watch(() => !!(agg.value && agg.value.generating > 0), (on) => { genSince = on ? performance.now() : 0; });

const pill = computed(() => {
  if (app.needKey) return { state: "queued", text: t("pill.nokey") };
  if (app.unreachable) return { state: "queued", text: t("pill.unreachable") };

  // 有并发数据就**完全以它为准**，空闲时也一样 —— 不能让「聚合说空闲」和「引擎说在生成」
  // 两个来源交替生效，那样胶囊会来回抖。
  if (agg.value) {
    const a = agg.value;
    if (a.in_flight > 0 && a.generating > 0) {
      const warm = genSince && performance.now() - genSince >= 1000;
      return a.tok_s && warm
        ? { state: "generating", rate: fmt(a.tok_s, 1), text: t("pill.generating", { rate: fmt(a.tok_s, 1) }) }
        : { state: "generating", text: t("pill.generating.norate") };
    }
    if (a.in_flight > 0) return { state: "reading", text: t("pill.reading") };
    return { state: "idle", text: t("pill.idle") };
  }

  // 没有并发数据（单路部署直连引擎、或代理版本较旧）→ 退回单引擎那份
  if (!app.metrics) return { state: "idle", text: t("pill.connecting") };
  const live = app.metrics.live || {};
  if (live.queued > 0) return { state: "queued", text: t("pill.queued", { n: live.queued }) };
  if (live.state === "reading") {
    const pct = live.prompt_total ? Math.round((100 * live.prompt_read) / live.prompt_total) : null;
    return { state: "reading", text: pct != null ? t("pill.reading.pct", { pct }) : t("pill.reading") };
  }
  if (live.state === "generating") return { state: "generating", rate: fmt(live.tok_s, 1), text: t("pill.generating", { rate: fmt(live.tok_s, 1) }) };
  return { state: "idle", text: t("pill.idle") };
});

// 手机上胶囊只报速率，不报「生成中 · 」那一截前缀 —— 页头挤不下整串，
// 而状态那个圆点的颜色本来就在说忙闲。**速率必须留下**：用户明确要求别把 token 速度藏掉
// （之前手机上这条文字是整块被 display:none 掉的，等于什么都看不到）。
const narrowHdr = ref(false);
const mqHdr = window.matchMedia("(max-width: 560px)");
const syncNarrow = () => { narrowHdr.value = mqHdr.matches; };
mqHdr.addEventListener("change", syncNarrow);
syncNarrow();
const pillText = computed(() => (narrowHdr.value && pill.value.state === "generating" && pill.value.rate
  ? t("pill.generating.short", { rate: pill.value.rate }) : pill.value.text));

const langLabel = computed(() => (LANGS.find((l) => l.code === locale.value) || LANGS[0]).label);
function cycleLang() {
  const i = LANGS.findIndex((l) => l.code === locale.value);
  setLang(LANGS[(i + 1) % LANGS.length].code);
}

// 打开服务就是对话页；LLM Lab 是从侧栏/对话里进的工作区，不占页头导航。
const navActive = (id) => app.tab === id;

const TOAST_ICON = { info: "info", success: "check", warn: "warning", error: "error" };

function onHash() { setTab(location.hash.slice(1)); }
function onKey(e) { if (e.key === "Escape" && drawerOpen.value) drawerOpen.value = false; }

// 让 Markdown 渲染里的文案跟着语言走
watch(() => locale.value, () => setMarkdownLabels({ copyCode: t("tool.copy.code") }), { immediate: true });

// 登录态一变就拉一次云端对话：刚才没登录时拉不到，现在能了。
watch(() => [auth.user && auth.user.id, apiKey()], () => pullCloud());

onMounted(async () => {
  applySystemTheme();
  initChats();                       // 会话列表要在第一次渲染对话页之前就位
  await Promise.all([fetchMe(), fetchAuthConfig()]);
  startPolling();                    // 监控这些不登录也能用，照常轮询
  window.addEventListener("hashchange", onHash);
  document.addEventListener("keydown", onKey);
});
onBeforeUnmount(() => {
  window.removeEventListener("hashchange", onHash);
  document.removeEventListener("keydown", onKey);
});


</script>

<template>
  <div class="app">
    <!-- 页头拉通整个宽度、内容固定：品牌永远在最左上，切标签时**一个像素都不动**。
         会话栏放到页头**下面**（见下），这样它在不在、宽窄如何，都影响不到页头。 -->
    <header class="st-header">
      <button class="st-header__brand st-header__brand--home" type="button" title="回到对话" aria-label="回到对话"
              @click="setTab('chat')">
        <img class="st-logo" :src="logoSrc" alt="" width="26" height="26">{{ NAME }}</button>
      <nav class="st-tabs" role="tablist" :aria-label="t('nav.views')">
        <!-- title 是给手机上准备的：≤560px 时标签只剩图标（font-size:0），长按/悬停能看到名字 -->
        <button v-for="v in VIEWS" :key="v.id" class="st-tab" role="tab" :data-view="v.id"
                :title="t(`tab.${v.id}`)" :aria-selected="navActive(v.id)" :aria-controls="`view-${v.id}`" @click="setTab(v.id)">
          <Icon :name="v.icon" cls="st-icon st-icon--sm" />{{ t(`tab.${v.id}`) }}
        </button>
      </nav>
      <span class="st-header__spacer"></span>
      <span class="st-pill" :data-state="pill.state" role="status" aria-live="polite" :title="pill.text">
        <span class="st-pill__dot"></span><span>{{ pillText }}</span></span>
      <button class="st-btn st-btn--icon st-btn--lang" :title="t('lang.title')" :aria-label="t('lang.aria')"
              @click="cycleLang">{{ langLabel }}</button>
      <button class="st-btn st-btn--icon" :title="t('theme.title')" :aria-label="t('theme.aria')" @click="flipTheme">
        <Icon :name="app.theme === 'dark' ? 'sun' : 'moon'" /></button>
      <!-- 没登录给个入口；登录了显示是谁 + 退出。两者都在右半边（spacer 之后），左边不会因此移动 -->
      <button v-if="!auth.user" class="st-btn st-btn--secondary st-signin" @click="login">
        <Icon name="link" cls="st-icon st-icon--sm" />{{ t("login.button") }}
      </button>
      <span v-else class="st-user" :title="auth.user.email || ''">
        <img v-if="auth.user.avatar" class="st-user__img" :src="auth.user.avatar" alt="" referrerpolicy="no-referrer">
        <span class="st-user__name">{{ auth.user.name }}</span>
        <button class="st-btn st-btn--icon st-user__out" :title="t('user.logout')" :aria-label="t('user.logout')"
                @click="logout"><Icon name="logout" /></button>
      </span>
    </header>

    <div class="app__body">
      <!-- 会话栏只在对话页出现。收起/展开的按钮在它自己身上 —— 页头里不放任何会变的元素 -->
      <SideBar v-if="app.tab === 'chat' && !auth.showLogin" />

      <main>
        <!-- 对话页用 v-show（不是 v-if）：跳登录页时它**保持挂载**，用户敲了一半的字不会丢。
             监控/可视化用 v-if 是另一回事 —— 离开就得销毁 WebGL 上下文，别在后台空转抢 GPU。 -->
        <ChatView v-show="!auth.showLogin && app.tab === 'chat'" @open-sampling="drawerOpen = true" />
        <MonitorView v-if="!auth.showLogin && app.tab === 'monitor'" />
        <!-- 记录页只有元信息，没有 WebGL，不需要为它做销毁回收 -->
        <RecordsView v-if="!auth.showLogin && app.tab === 'records'" />
        <VizView v-if="!auth.showLogin && app.tab === 'viz'" />
        <!-- 训练页：离开就卸载（轮询定时器跟着停，不在后台空转拉文件） -->
        <TrainView v-if="!auth.showLogin && app.tab === 'train'" />
        <AboutView v-if="!auth.showLogin && app.tab === 'about'" />
        <!-- 登录页：应用内的一页，页头还在，随时能切走 -->
        <LoginView v-if="auth.showLogin" @back="backToApp" />
      </main>
    </div>
  </div>

  <SamplingDrawer :open="drawerOpen" @close="drawerOpen = false" />
  <div class="scrim" v-show="drawerOpen" @click="drawerOpen = false"></div>
  <!-- 手机上会话栏是抽屉：点旁边任意处收起。桌面下被 CSS 藏掉（那边是窄轨，不需要遮罩） -->
  <div v-if="app.tab === 'chat' && app.side" class="side-scrim" @click="setSide(false)"></div>

  <div class="st-toasts" aria-live="polite">
    <div v-for="x in toasts" :key="x.id" class="st-toast" :class="`st-toast--${x.kind}`">
      <Icon :name="TOAST_ICON[x.kind] || 'info'" />
      <div><div class="st-toast__title">{{ x.title }}</div><div class="t-text">{{ x.text }}</div></div>
      <button v-if="x.action" class="st-btn st-btn--secondary toast-action"
              @click="x.action.run(); hide(x.id)">{{ x.action.label }}</button>
    </div>
  </div>
</template>

<style scoped>
.toast-action { height: 32px; margin-left: auto; }
.st-btn--lang { min-width: 46px; font-size: var(--st-fs-sm); font-weight: var(--st-fw-bold); }
/* 品牌：图标 + 文字，与原版只多了一个图标 */
.st-header__brand { display: inline-flex; align-items: center; gap: 9px; font-size: var(--st-fs-lg); }
.st-header__brand--home { border: 0; padding: 0; background: none; color: inherit; font: inherit;
                          font-size: var(--st-fs-lg); cursor: pointer; }
.st-header__brand--home:hover { color: var(--st-accent-text); }
.st-logo { display: block; width: 26px; height: 26px; border-radius: 7px; }
/* 登录用户：头像 + 名字 + 退出，一整块可点区域，和页头其它按钮同一档高度 */
.st-user { display: inline-flex; align-items: center; gap: 6px; padding-left: var(--st-s-2);
           border-left: 1px solid var(--st-line); }
.st-user__img { width: 26px; height: 26px; border-radius: 50%; object-fit: cover; flex: none; }
.st-user__name { font-size: var(--st-fs-sm); color: var(--st-ink-soft); max-width: 120px;
                 overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 会话栏抽屉的遮罩：只在手机上存在（桌面是窄轨，不需要） */
.side-scrim { display: none; }

/* 手机上页头要「只留必要的」：品牌只留 logo 的图形、文字藏掉。
   这条必须写在**本组件的 scoped 块里** —— 上面 .st-header__brand 那条 scoped 规则带
   [data-v-*]，特异性比 app.css 里的同名规则高，写在那边会被压住（页头因此撑到 607px）。 */
@media (max-width: 720px) {
  .side-scrim { display: block; position: fixed; inset: 0; z-index: 39; background: rgba(15, 23, 42, .22); }
  .st-header__brand { font-size: 0; }
  .st-logo { width: 24px; height: 24px; }
  .st-btn--lang { min-width: 36px; }
}
</style>
