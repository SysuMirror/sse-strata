// 工作空间文件 API：树、读、写、建目录、删。全部按登录会话/API token 分目录，互不可见。
import { apiKey } from "./api.js";

/** 显式拼鉴权头：工作空间所有接口都要「登录会话或 API token」。 */
function hdr(json = false) {
  const h = {};
  const k = apiKey();
  if (k) h.Authorization = "Bearer " + k;
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function req(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...hdr(!!opts.body), ...(opts.headers || {}) } });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!r.ok) {
    const err = data && data.error;
    const msg = (err && (err.message || err)) || (data && data.detail) || `HTTP ${r.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg).slice(0, 200));
  }
  return data;
}

export const tree = () => req("/workspace/tree");
export const search = (q) => req(`/workspace/search?q=${encodeURIComponent(q)}`);
export const readFile = (path) => req(`/workspace/file?path=${encodeURIComponent(path)}`);
export const writeFile = (path, text) => req("/workspace/file", { method: "POST", body: JSON.stringify({ path, text }) });
/** 二进制写（图片等）：走同一接口的 b64 分支。 */
export const writeFileBinary = (path, b64) => req("/workspace/file", { method: "POST", body: JSON.stringify({ path, b64 }) });
export const envs = () => req("/workspace/envs");
/** 跑一段代码/一个小项目。opts: {files, stdin, in_place, cwd, force, timeout}。 */
export const runCode = (env, code, timeout = 30, opts = {}) =>
  req("/workspace/run", { method: "POST", body: JSON.stringify({ env, code, timeout, ...opts }) });
export const mkdir = (path) => req("/workspace/mkdir", { method: "POST", body: JSON.stringify({ path }) });
export const remove = (path) => req(`/workspace/path?path=${encodeURIComponent(path)}`, { method: "DELETE" });

// ---- LLM Lab / 知识库 / 后台任务（新增）
export const labReset = (backup = true) =>
  req("/workspace/lab", { method: "POST", body: JSON.stringify({ reset: true, backup }) });
export const kbList = () => req("/workspace/kb");
export const kbAdd = (payload) => req("/workspace/kb", { method: "POST", body: JSON.stringify(payload) });
export const kbSearch = (q, k = 6, rerank = true) =>
  req("/workspace/kb/search", { method: "POST", body: JSON.stringify({ q, k, rerank }) });
export const kbDelete = (id) => req(`/workspace/kb?id=${encodeURIComponent(id)}`, { method: "DELETE" });

/** 后台任务：训练/长跑命令。args 是数组，例如 ["python","main.py","train","--steps","500"]。 */
export const jobStart = ({ env = "lab", args = [], files = null, cwd = "llm-study-lab",
                          timeout = 1800, force = false } = {}) =>
  req("/workspace/job", { method: "POST", body: JSON.stringify({ env, args, files, cwd, timeout, force }) });
export const jobStatus = (id, offset = 0) =>
  req(`/workspace/job?id=${encodeURIComponent(id)}&offset=${offset}`);
export const jobStop = (id) => req("/workspace/job/stop", { method: "POST", body: JSON.stringify({ id }) });
export const jobsList = () => req("/workspace/jobs");

/** 扁平清单 → 嵌套树。父目录没有显式条目时补一个。 */
export function toTree(entries) {
  const root = { path: "", name: "", dir: true, children: new Map() };
  const get = (p) => {
    if (root.children.has(p)) return root.children.get(p);
    const node = { path: p, name: p.split("/").pop(), dir: true, children: new Map() };
    root.children.set(p, node);
    return node;
  };
  for (const e of entries) {
    const parts = e.path.split("/");
    let parent = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const p = parts.slice(0, i + 1).join("/");
      parent = get(p);
    }
    if (e.dir) {
      const n = get(e.path);
      n.dir = true; n.mtime = e.mtime;
    } else {
      parent.children.set(e.path, { ...e, name: parts[parts.length - 1], dir: false, children: null });
    }
  }
  const sortRec = (node) => {
    if (!node.children) return [];
    const arr = [...node.children.values()].map((c) => (c.children ? { ...c, children: sortRec(c) } : c));
    arr.sort((a, b) => (a.dir === b.dir ? String(a.name || "").localeCompare(String(b.name || "")) : a.dir ? -1 : 1));
    return arr;
  };
  return sortRec(root);
}

/** 根据扩展名判断怎么展示。 */
export const kindOf = (path) => {
  const ext = (path.split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "webp", "gif"].includes(ext)) return "image";
  if (["md", "txt", "json", "yaml", "yml", "js", "ts", "py", "css", "html", "csv", "log", "toml", "sh"].includes(ext)) return "text";
  return "binary";
};