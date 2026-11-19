<script setup>
// 对话页的侧边栏（ChatGPT 布局）：
//   新建对话 / 搜索 / 图库 / 知识库 / 按日期分组的历史会话。
// 图库与知识库直接读工作空间（同一个账号一个目录）；点缩略图打开工作空间里的原图，
// 点资料打开知识库面板 —— 不再需要单独的“首页”中转。
import { ref, computed, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import Icon from "./Icon.vue";
import { store, toast } from "../api.js";
import { app, setSide, toggleSide, isNarrow, setTab } from "../store.js";
import { activeChat, chats, cloud, deleteChat, grouped, newChat, openChat, pullCloud, restoreChat, saveActive } from "../chats.js";
import { getWorkspace } from "../hub.js";
import { kbList } from "../workspace.js";

const { t } = useI18n();

const q = ref("");
const gallery = ref([]);          // [{name, url, updated}]
const docs = ref([]);             // 知识库资料
const kbCount = ref(0);

const groups = computed(() => {
  const gs = grouped();
  const s = q.value.trim().toLowerCase();
  if (!s) return gs;
  return gs.map((g) => ({ ...g, items: g.items.filter((c) => (c.title || t("chat.untitled")).toLowerCase().includes(s)) }))
           .filter((g) => g.items.length);
});
const hitCount = computed(() => groups.value.reduce((n, g) => n + g.items.length, 0));
const title = (c) => c.title || t("chat.untitled");

async function loadPanels() {
  try { const d = await getWorkspace(); gallery.value = (d.images || []).slice(0, 4); } catch (e) { /* 未登录 */ }
  try { const d = await kbList(); docs.value = (d.docs || []).slice(0, 4); kbCount.value = d.count || 0; } catch (e) { /* 未登录 */ }
}
onMounted(loadPanels);

function onNew() {
  saveActive();                       // 先把当前会话的最后状态落盘
  newChat();
  if (isNarrow()) setSide(false);     // 手机上开完新会话就把抽屉收走，别挡着输入框
}

function onOpen(c) {
  if (c.id === chats.activeId) { if (isNarrow()) setSide(false); return; }
  saveActive();
  openChat(c.id);
  if (isNarrow()) setSide(false);
}

function onDelete(c) {
  const gone = deleteChat(c.id);
  if (!gone) return;
  toast("info", t("chat.deleted"), t("chat.deleted.sub"), 6000,
        { label: t("toast.undo"), run: () => restoreChat(gone) });
}

/** 打开工作空间里的生成图（原图预览）。 */
function openImage(im) {
  store.set("lab.open", `images/${im.name}`);
  store.set("train.mode", "code");     // 图库/知识库都在 Lab 里，先确保训练页是代码模式
  setTab("lab");
  if (isNarrow()) setSide(false);
}
/** 打开工作空间（图库/文件的完整视图）。 */
function openWorkspace() {
  store.set("train.mode", "code");
  setTab("lab");
  if (isNarrow()) setSide(false);
}
/** 打开知识库管理面板。 */
function openKb() {
  store.set("lab.tab", "kb");
  store.set("train.mode", "code");
  setTab("lab");
  if (isNarrow()) setSide(false);
}
</script>

<template>
  <aside class="side" :data-rail="app.side ? null : '1'" :aria-label="t('chat.history')">
    <!-- 收起后只剩一条窄轨 + 展开按钮。放在会话栏自己身上，而不是页头 ——
         页头里一旦有「只在这页出现」的元素，切标签时整条页头就会横移。 -->
    <button v-if="!app.side" class="side__expand" :title="t('nav.toggle')" :aria-label="t('nav.toggle')"
            @click="toggleSide"><Icon name="chevron" cls="st-icon st-icon--sm" /></button>

    <div class="side__head">
      <button class="st-btn st-btn--primary side__new" @click="onNew">
        <Icon name="plus" cls="st-icon st-icon--sm" /><span>{{ t("chat.new") }}</span>
      </button>
      <button class="side__collapse" :title="t('nav.toggle')" :aria-label="t('nav.toggle')" @click="toggleSide">
        <Icon name="panel" cls="st-icon st-icon--sm" /></button>
    </div>

    <label class="side__search">
      <Icon name="link" cls="st-icon st-icon--sm" />
      <input v-model="q" type="search" :placeholder="t('chat.search')" :aria-label="t('chat.search')">
      <button v-if="q" type="button" class="side__search-x" :aria-label="t('common.remove')" @click="q = ''">✕</button>
    </label>

    <div class="side__body">
      <!-- 图库：最近生成的图，点开右边工作空间里的原图 -->
      <section class="side__sec">
        <header class="side__sec-head">
          <span>{{ t("side.gallery") }}</span>
          <button type="button" @click="openWorkspace">{{ t("side.all") }}</button>
        </header>
        <div v-if="gallery.length" class="side__thumbs">
          <button v-for="im in gallery" :key="im.name" type="button" class="side__thumb"
                  :title="im.name" @click="openImage(im)">
            <img :src="im.url" :alt="im.name" loading="lazy">
          </button>
        </div>
        <p v-else class="side__sec-empty">{{ t("side.gallery.empty") }}</p>
      </section>

      <!-- 知识库：资料名 + 块数 -->
      <section class="side__sec">
        <header class="side__sec-head">
          <span>{{ t("side.kb") }}</span>
          <button type="button" @click="openKb">{{ t("side.manage") }}</button>
        </header>
        <template v-if="docs.length">
          <button v-for="d in docs" :key="d.id" type="button" class="side__doc" @click="openKb">
            <Icon name="memory" cls="st-icon st-icon--sm" />
            <span class="side__doc-name">{{ d.name }}</span>
            <span class="side__doc-n">{{ d.chunks }}</span>
          </button>
          <p v-if="kbCount > docs.length" class="side__more" @click="openKb">
            {{ t("side.more", { n: kbCount - docs.length }) }}</p>
        </template>
        <p v-else class="side__sec-empty">{{ t("side.kb.empty") }}</p>
      </section>

      <!-- 会话历史 -->
      <div class="side__list">
        <template v-for="g in groups" :key="g.key">
          <p class="side__group">{{ t(`chat.group.${g.label}`) }}</p>
          <div v-for="c in g.items" :key="c.id" class="side__conv"
               :data-active="c.id === chats.activeId ? '1' : null"
               role="button" tabindex="0" @click="onOpen(c)" @keydown.enter="onOpen(c)">
            <span class="side__conv-title">{{ title(c) }}</span>
            <span v-if="c.busy" class="side__busy" :title="t('chat.streaming')" :aria-label="t('chat.streaming')"></span>
            <button class="side__conv-del" :title="t('common.remove')" :aria-label="t('common.remove')"
                    @click.stop="onDelete(c)">
              <Icon name="trash" cls="st-icon st-icon--sm" />
            </button>
          </div>
        </template>
        <p v-if="!hitCount" class="side__empty">{{ q ? t("chat.search.empty") : t("chat.history.empty") }}</p>
      </div>
    </div>

    <div class="side__foot">
      <span v-if="activeChat && activeChat.messages.length" class="side__count">
        {{ t("chat.turns", { n: activeChat.messages.length }) }}</span>
      <!-- 对话存在哪儿：登录后跟着账号走（换设备也能看到同一批），没登录就只在本机 -->
      <button class="side__cloud" :data-s="cloud.state" :title="t('chat.cloud.hint')"
              @click="pullCloud()">
        <Icon :name="cloud.state === 'on' ? 'check' : cloud.state === 'error' ? 'warning' : 'disk'"
              cls="st-icon st-icon--sm" />
        {{ t(`chat.cloud.${cloud.state}`) }}
      </button>
    </div>
  </aside>
</template>

<style scoped>
.side { flex: none; width: 260px; display: flex; flex-direction: column; background: var(--st-surface);
        border-right: 1px solid var(--st-line); overflow: hidden; transition: width 140ms var(--st-ease); }
/* 窄轨：只留一个展开按钮 */
.side[data-rail] { width: 44px; align-items: center; padding-top: 10px; }
.side[data-rail] .side__head,
.side[data-rail] .side__search,
.side[data-rail] .side__body,
.side[data-rail] .side__foot { display: none; }
.side__expand { width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center;
                border: 1px solid var(--st-line); border-radius: 10px; background: transparent;
                color: var(--st-ink-muted); cursor: pointer; }
.side__expand:hover { background: var(--st-surface-2); color: var(--st-ink); }
.side__expand .st-icon { transform: rotate(180deg); }   /* chevron 朝下，转成朝右 */

.side__head { flex: none; display: flex; gap: 6px; align-items: center;
              padding: var(--st-s-3) var(--st-s-3) var(--st-s-2); }
.side__new { flex: 1; min-width: 0; height: 38px; font-size: var(--st-fs-sm); gap: 6px; }
.side__collapse { flex: none; width: 38px; height: 38px; display: inline-flex; align-items: center;
                  justify-content: center; border: 1px solid var(--st-line); border-radius: 10px;
                  background: transparent; color: var(--st-ink-muted); cursor: pointer; }
.side__collapse:hover { background: var(--st-surface-2); color: var(--st-ink); }

/* 搜索：和 ChatGPT 一样放在新建按钮下面，直接过滤会话标题 */
.side__search { position: relative; flex: none; display: flex; align-items: center; gap: 6px;
                margin: 0 var(--st-s-3) var(--st-s-2); padding: 0 8px; height: 34px;
                border: 1px solid var(--st-line); border-radius: 10px; background: var(--st-surface-2);
                color: var(--st-ink-muted); }
.side__search:focus-within { border-color: var(--st-accent); background: var(--st-surface); }
.side__search input { flex: 1; min-width: 0; border: 0; background: none; outline: none;
                      font: inherit; font-size: var(--st-fs-sm); color: var(--st-ink); }
.side__search-x { border: 0; background: none; color: var(--st-ink-muted); cursor: pointer; padding: 0 2px; }

.side__body { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.side__sec { flex: none; padding: 0 var(--st-s-3) var(--st-s-2); }
.side__sec + .side__sec { border-top: 1px solid var(--st-line-soft); padding-top: var(--st-s-2); }
.side__sec-head { display: flex; align-items: center; justify-content: space-between;
                  padding: 6px 10px 4px; color: var(--st-ink-muted); font-size: var(--st-fs-xs); font-weight: 600; }
.side__sec-head button { border: 0; background: none; padding: 0; cursor: pointer;
                         color: var(--st-accent-text); font: inherit; font-size: inherit; }
.side__sec-head button:hover { text-decoration: underline; }
.side__thumbs { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; padding: 0 6px; }
.side__thumb { padding: 0; border: 1px solid var(--st-line); border-radius: 8px; overflow: hidden;
               aspect-ratio: 1/1; background: var(--st-surface-2); cursor: pointer; }
.side__thumb:hover { border-color: var(--st-accent); }
.side__thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }
.side__doc { display: flex; align-items: center; gap: 6px; width: 100%; height: 30px; padding: 0 10px;
             border: 0; border-radius: 8px; background: none; cursor: pointer; text-align: left;
             color: var(--st-ink-soft); font: inherit; font-size: var(--st-fs-xs); }
.side__doc:hover { background: var(--st-surface-2); color: var(--st-ink); }
.side__doc-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.side__doc-n { flex: none; color: var(--st-ink-muted); font-variant-numeric: tabular-nums; }
.side__sec-empty { margin: 2px 0 0; padding: 0 10px; font-size: 11px; line-height: 1.4; color: var(--st-ink-muted); }
.side__more { margin: 2px 0 0; padding: 0 10px; font-size: 11px; color: var(--st-accent-text); cursor: pointer; }
.side__more:hover { text-decoration: underline; }

.side__list { flex: 1; min-height: 0; overflow-y: auto; padding: 0 var(--st-s-3) var(--st-s-3);
              border-top: 1px solid var(--st-line-soft); }
.side__group { margin: var(--st-s-3) 0 6px; padding: 0 10px; font-size: var(--st-fs-xs); font-weight: 600;
               color: var(--st-ink-muted); }
.side__empty { margin: var(--st-s-3) 0 0; padding: 0 10px; font-size: var(--st-fs-xs); color: var(--st-ink-muted); }

.side__conv { display: flex; align-items: center; gap: 6px; height: 36px; padding: 0 6px 0 10px;
              border-radius: 10px; cursor: pointer; color: var(--st-ink-soft); }
.side__conv:hover { background: var(--st-surface-2); }
.side__conv:focus-visible { outline: 2px solid var(--st-focus); outline-offset: 1px; }
.side__conv[data-active] { background: var(--st-accent-tint); color: var(--st-accent-text); font-weight: var(--st-fw-bold); }
.side__conv-title { flex: 1; min-width: 0; font-size: var(--st-fs-sm); white-space: nowrap; overflow: hidden;
                    text-overflow: ellipsis; }
.side__busy { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--st-accent);
              animation: sideBusy 1.4s ease-in-out infinite; }
@keyframes sideBusy { 0%,100% { opacity: 1; transform: scale(1); } 50% { opacity: .45; transform: scale(.75); } }
.side__conv-del { flex: none; width: 24px; height: 24px; display: inline-flex; align-items: center;
                  justify-content: center; border: 0; border-radius: 7px; background: transparent;
                  color: var(--st-ink-muted); cursor: pointer; opacity: 0; transition: opacity var(--st-dur); }
.side__conv:hover .side__conv-del, .side__conv:focus-within .side__conv-del { opacity: 1; }
.side__conv-del:hover { background: var(--st-danger-tint); color: var(--st-danger-text); }

.side__foot { flex: none; display: flex; align-items: center; justify-content: flex-end;
              padding: 10px var(--st-s-4); border-top: 1px solid var(--st-line); min-height: 40px; }
/* 同步状态：一行小字，颜色区分「在云上 / 只在本机 / 出错」 */
.side__cloud { display: inline-flex; align-items: center; gap: 5px; max-width: 100%; padding: 2px 6px;
               border: 0; border-radius: 6px; background: transparent; cursor: pointer;
               font-size: var(--st-fs-xs); color: var(--st-ink-muted); font-family: var(--st-font); }
.side__cloud:hover { background: var(--st-surface-2); color: var(--st-ink-soft); }
.side__cloud[data-s="on"] { color: var(--st-accent-text); }
.side__cloud[data-s="syncing"] { color: var(--st-ink-soft); }
.side__cloud[data-s="error"] { color: var(--st-danger-text); }

@media (max-width: 720px) {
  .side { width: 280px; }
  .side__thumbs { grid-template-columns: repeat(3, 1fr); }
}
</style>