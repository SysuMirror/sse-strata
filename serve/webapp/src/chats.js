// 多会话：会话列表与当前会话。
//
// 存储布局（都在这个浏览器里，服务端一概不知情）：
//   strata.chats        [{ id, title, createdAt, updatedAt, messages: [...] }]
//   strata.chatActive   当前会话 id
// 老版本只存一条对话（strata.chat）。第一次打开时把它搬成一条会话，历史不丢。
//
// 落盘时会**剔掉大块内容**（图片的 dataURL、附件正文）—— 和原版 app.js 一样：
// 那些东西一次几 MB，几条会话就能把 localStorage 的 5MB 配额顶爆。
// 它们只在当次会话的内存里有效，刷新后附件本来也就没了（原版也是这个行为）。
import { computed, reactive } from "vue";
import { apiKey, headers, store } from "./api.js";
import { auth } from "./auth.js";

const MAX_CHATS = 60;                 // 超了就丢最旧的（侧栏滚动条也不该无限长）
const TITLE_MAX = 40;

export const chats = reactive({ list: [], activeId: null });

// 云端同步状态（侧栏底部如实显示）：off=没登录（只在本机）/ syncing / on / error
export const cloud = reactive({ state: "off", store: "", at: 0, error: "" });

export const activeChat = computed(() => chats.list.find((c) => c.id === chats.activeId) || null);

const uid = () => `c${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;

// ------------------------------------------------------------------ 存取

/** 会话标题：取第一条用户消息的头几个字，没有就「新对话」。 */
export function titleOf(c) {
  const first = (c.messages || []).find((m) => m.role === "user" && (m.text || "").trim());
  if (!first) return "";
  const t = first.text.trim().replace(/\s+/g, " ");
  return t.length > TITLE_MAX ? `${t.slice(0, TITLE_MAX)}…` : t;
}

/** 消息瘦身：只留能重新渲染的部分，图片/附件正文不落盘。 */
function slim(m) {
  const out = { role: m.role, text: m.text || "", time: m.time };
  if (m.reasoning) out.reasoning = m.reasoning;
  if (m.thinkSecs != null) out.thinkSecs = m.thinkSecs;
  if (m.meta) out.meta = m.meta;
  if (m.error) out.error = m.error;
  if (m.stopped) out.stopped = true;
  if (m.limit) out.limit = m.limit;
  if (m.tools && m.tools.length) out.tools = m.tools.map((x) => ({ ...x }));
  if (m.images && m.images.length) out.images = m.images.map((i) => ({ name: i.name }));
  if (m.files && m.files.length) out.files = m.files.map((f) => ({ name: f.name }));
  return out;
}

/** 直接写 localStorage 并**如实报出**成败：api.js 的 store.set 会把异常吞掉，
 *  那样配额爆了我们只会静默丢数据 —— 这里要能感知到，好做「丢最旧的」降级。 */
function write(key, value) {
  try {
    localStorage.setItem("strata." + key, JSON.stringify(value));
    return true;
  } catch (e) {
    return false;
  }
}

const dump = () => chats.list.map((c) => {
  // `busy` holds an AbortController and the live message object; it is runtime-only.
  const { busy, ...rest } = c;
  return { ...rest, messages: (c.messages || []).map(slim) };
});

function persistLocal() {
  if (!write("chats", dump())) {
    // 配额炸了：砍掉一半最旧的会话再试一次，别让「保存」把整个界面卡住
    chats.list = chats.list.slice(0, Math.max(1, Math.floor(chats.list.length / 2)));
    write("chats", dump());
  }
  write("chatActive", chats.activeId);
}

function persist() {
  persistLocal();
  pushCloud();                 // 登录了就顺带同步到服务端：换设备也能看到同一批会话
}

// ------------------------------------------------------------------ 云端同步
//
// 登录之后对话跟着**账号**走（服务端 /chats，见 rag-proxy）。没登录就老样子，只在本机。
// 合并策略：按会话 id 取 updatedAt 较**新**的那一份 —— 两块屏各聊各的也不会互相覆盖。

const canSync = () => !!(auth.user || apiKey());

async function cloudGet() {
  const r = await fetch("chats", { headers: headers(true) });
  // 404 = 服务端还没有这个路由（代理没升级）。那就老实当「只在本机」，
  // 不要报成「同步失败」吓人 —— 请求本身没错，是功能还没上线。
  if (r.status === 401 || r.status === 404) throw new Error("nosync");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

let pushTimer = null;

/** 推到云端（防抖：连续改动只发最后一次）。 */
export function pushCloud() {
  if (!canSync()) { cloud.state = "off"; return; }
  clearTimeout(pushTimer);
  pushTimer = setTimeout(async () => {
    cloud.state = "syncing";
    try {
      const r = await fetch("chats", {
        method: "POST",
        headers: { ...headers(true), "Content-Type": "application/json" },
        body: JSON.stringify({ chats: dump(), activeId: chats.activeId }),
      });
      if (r.status === 401 || r.status === 404) throw new Error("nosync");
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      cloud.state = "on"; cloud.store = j.store || ""; cloud.at = Date.now(); cloud.error = "";
    } catch (e) {
      const m = String((e && e.message) || e);
      cloud.state = m === "nosync" ? "off" : "error";
      cloud.error = m;
    }
  }, 1200);
}

/** 拉云端并合并（登录后调用一次）。返回是否真的合并了云端的数据。 */
export async function pullCloud() {
  if (!canSync()) { cloud.state = "off"; return false; }
  cloud.state = "syncing";
  try {
    const j = await cloudGet();
    const remote = Array.isArray(j.chats) ? j.chats : [];
    cloud.store = j.store || "";
    if (!remote.length) { cloud.state = "on"; pushCloud(); return false; }   // 云端是空的：把本机推上去
    const mine = new Map(chats.list.map((c) => [c.id, c]));
    for (const c of remote) {
      const cur = mine.get(c.id);
      if (!cur || (c.updatedAt || 0) > (cur.updatedAt || 0)) mine.set(c.id, c);
    }
    chats.list = [...mine.values()].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    for (const c of chats.list) c.messages = mergeAssistantRuns(c.messages || []);
    if (!chats.list.some((c) => c.id === chats.activeId)) chats.activeId = chats.list[0].id;
    persistLocal();
    cloud.state = "on"; cloud.at = Date.now(); cloud.error = "";
    pushCloud();                       // 合并结果回写云端，下次别的设备就能拿到
    return true;
  } catch (e) {
    const m = String((e && e.message) || e);
    cloud.state = m === "nosync" ? "off" : "error";
    cloud.error = m;
    return false;
  }
}

/**
 * 别的页面真的跑过一次推理（推理可视化演示的收尾）：把这次问答存成一条会话。
 * 这样演示里那句「完整回答见对话页」才是**真的** —— 之前只是句空话（用户实测报过）。
 * 同一句话再演示一次就覆盖那条旧会话，不会越攒越多。
 */
export function addExternalChat({ title, question, answer, meta }) {
  let c = chats.list.find((x) => x.external && x.question === question);
  if (c) {
    c.messages = [
      { role: "user", text: question, time: Date.now() },
      { role: "assistant", text: answer, time: Date.now(), meta },
    ];
    c.updatedAt = Date.now();
  } else {
    c = { id: uid(), title: title || question.slice(0, TITLE_MAX), createdAt: Date.now(),
          updatedAt: Date.now(), external: true, question,
          messages: [{ role: "user", text: question, time: Date.now() },
                     { role: "assistant", text: answer, time: Date.now(), meta }] };
    chats.list.unshift(c);
    if (chats.list.length > MAX_CHATS) chats.list.splice(MAX_CHATS);
  }
  persist();
  return c;                      // 调用方要拿它的 id：演示画布上「见对话页」那行点一下要跳过去
}

// ------------------------------------------------------------------ 动作

export function saveActive() {
  saveChat(activeChat.value);
}

/** Persist ONE chat, even if it is no longer the active one (parallel streams finish in the background). */
export function saveChat(c) {
  if (!c) return;
  c.messages = c.messages || [];
  c.title = titleOf(c) || c.title;
  c.updatedAt = Date.now();
  persist();
}

export function newChat({ activate = true } = {}) {
  const c = { id: uid(), title: "", createdAt: Date.now(), updatedAt: Date.now(), messages: [] };
  chats.list.unshift(c);
  if (chats.list.length > MAX_CHATS) {
    const dropped = chats.list.splice(MAX_CHATS);
    if (dropped.some((d) => d.id === chats.activeId)) chats.activeId = c.id;
  }
  if (activate) chats.activeId = c.id;
  persist();
  return c;
}

export function openChat(id) {
  if (chats.list.some((c) => c.id === id)) {
    chats.activeId = id;
    store.set("chatActive", id);
  }
}

export function deleteChat(id) {
  const i = chats.list.findIndex((c) => c.id === id);
  if (i < 0) return null;
  const [gone] = chats.list.splice(i, 1);
  if (chats.activeId === id) {
    chats.activeId = chats.list.length ? chats.list[Math.min(i, chats.list.length - 1)].id : newChat().id;
  }
  persist();
  return gone;                    // 交给调用方做「撤销」
}

export function restoreChat(c) {
  if (!c || chats.list.some((x) => x.id === c.id)) return;
  chats.list.unshift(c);
  chats.activeId = c.id;
  persist();
}

// ------------------------------------------------------------------ 分组（侧栏按日期分段）

const dayStart = (ts) => { const d = new Date(ts); d.setHours(0, 0, 0, 0); return d.getTime(); };

/** @returns {{ key: string, label: string, items: object[] }[]} 今天 / 昨天 / 更早 */
export function grouped() {
  const today = dayStart(Date.now());
  const groups = [
    { key: "today", label: "today", items: [] },
    { key: "yesterday", label: "yesterday", items: [] },
    { key: "earlier", label: "earlier", items: [] },
  ];
  const sorted = [...chats.list].sort((a, b) => b.updatedAt - a.updatedAt);
  for (const c of sorted) {
    const d = dayStart(c.updatedAt);
    const bucket = d >= today ? groups[0] : d >= today - 86400000 ? groups[1] : groups[2];
    bucket.items.push(c);
  }
  return groups.filter((g) => g.items.length);
}

// ------------------------------------------------------------------ 启动

/** 旧数据里 agent 每轮工具是一条 assistant 消息，界面上就是「一段一段」的。
 *  载入时把相邻的 assistant 合并成一条：文本用空行接、工具卡 at 平移、round 重新编号，
 *  这样旧会话也和新版一样是连续的一条消息。 */
export function mergeAssistantRuns(messages) {
  const out = [];
  for (const raw of messages || []) {
    const m = {
      ...raw,
      tools: (raw.tools || []).map((t) => ({ ...t })),
      images: (raw.images || []).map((i) => ({ ...i })),
    };
    const prev = out[out.length - 1];
    if (m.role === "assistant" && prev && prev.role === "assistant" && !prev.error && !m.error) {
      const base = prev.text ? prev.text.length + 2 : 0;
      prev.text = prev.text ? `${prev.text}\n\n${m.text || ""}` : (m.text || "");
      prev.reasoning = [prev.reasoning, m.reasoning].filter(Boolean).join("\n\n");
      prev.tools = prev.tools || [];
      const maxRound = Math.max(0, ...prev.tools.map((t) => t.round || 0));
      const roundMap = new Map();
      let nextRound = maxRound;
      for (const t of m.tools || []) {
        const orig = t.round || 1;
        if (!roundMap.has(orig)) roundMap.set(orig, ++nextRound);
        prev.tools.push({ ...t, at: base + (t.at || 0), round: roundMap.get(orig) });
      }
      prev.images = [...(prev.images || []), ...(m.images || [])];
      prev.meta = m.meta || "";
      prev.stopped = prev.stopped || m.stopped;
      prev.limit = prev.limit || m.limit;
      continue;
    }
    out.push(m);
  }
  return out;
}

export function initChats() {
  const saved = store.get("chats", null);
  if (Array.isArray(saved) && saved.length) {
    chats.list = saved.map((c) => ({
      id: c.id || uid(),
      title: c.title || "",
      createdAt: c.createdAt || Date.now(),
      updatedAt: c.updatedAt || c.createdAt || Date.now(),
      messages: Array.isArray(c.messages) ? mergeAssistantRuns(c.messages) : [],
      // 刷新续跑靠它：上一轮 agent 是否还没结束
      agentRun: c.agentRun || null,
      lastRun: c.lastRun || null,
    }));
    const want = store.get("chatActive", null);
    chats.activeId = chats.list.some((c) => c.id === want) ? want : chats.list[0].id;
  } else {
    // 从老版本的单条对话迁过来
    const legacy = store.get("chat", null);
    const c = newChat({ activate: false });
    if (Array.isArray(legacy) && legacy.length) c.messages = legacy;
    c.title = titleOf(c) || "";
    c.updatedAt = Date.now();
    chats.activeId = c.id;
    persist();
  }
  pullCloud();                     // 登录了就拉一次云端，跟本机合并（异步，不挡首屏）
  return chats;
}
