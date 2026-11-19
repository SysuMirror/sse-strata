// 通用工具与服务器访问：格式化、剪贴板、toast、带 API key 的 fetch。
// 这些都是原 app.js 的同名函数，行为保持一致（格式化口径、toast 时长、鉴权头都一样）。
import { reactive } from "vue";

export const SPRITE = "/web/sprite.svg";

export const store = {
  get(k, d) {
    try {
      const v = localStorage.getItem("strata." + k);
      return v === null ? d : JSON.parse(v);
    } catch (e) { return d; }
  },
  set(k, v) {
    try { localStorage.setItem("strata." + k, JSON.stringify(v)); } catch (e) { /* 隐私模式：只留在内存 */ }
  },
};

export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "–" : Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
export const kfmt = (n) => (n == null ? "–" : n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : fmt(n));
// 上下文长度：32768 → "32K"（2 的幂），否则同 kfmt
export const ctxfmt = (n) => (n && n % 1024 === 0 ? `${fmt(n / 1024)}K` : kfmt(n));
export const gb = (b, d = 1) => (b == null ? "–" : fmt(b / 1073741824, d));   // 内存按二进制 GB，与系统显示一致

// ------------------------------------------------------------------ toasts
export const toasts = reactive([]);
let seq = 0;
export function toast(kind, title, text = "", ms = 3500, action = null) {
  const id = ++seq;
  toasts.push({ id, kind, title, text, action });
  setTimeout(() => hide(id), ms);
}
export function hide(id) {
  const i = toasts.findIndex((t) => t.id === id);
  if (i >= 0) toasts.splice(i, 1);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {                                   // 另一台主机上的 http：没有异步剪贴板
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  toast("success", "已复制到剪贴板", "", 1800);
}

// ------------------------------------------------------------------ server access
export const apiKey = () => store.get("apikey", "");
export function headers(json = false) {
  const h = {};
  const key = apiKey();
  if (key) h.Authorization = "Bearer " + key;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

/** 取一段 JSON：失败时抛出带服务器消息的错误（401 的提示与原来一致）。 */
export async function getJson(path, { key = false } = {}) {
  const r = await fetch(path, { headers: headers() });
  if (key && r.status === 401) throw new Error("这个服务需要 API key：请在「关于 → 设置」里填。");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
