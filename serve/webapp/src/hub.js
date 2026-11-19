// 模型广场 / 体验页的调用层：把网关（rag-proxy）新增的 /spark/* /tts/* /tool/* 包装成函数。
//
// 为什么全都走后端代理：浏览器到不了集群内网（10.42.x），也绝不能持有 Spark 的 API key。
// 网关代持 key、按登录会话或 API token 鉴权，前端只认自己的同源路径。
import { headers } from "./api.js";

async function post(path, body, timeoutMs = 900000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(path, { method: "POST", headers: headers(true), body: JSON.stringify(body), signal: ctl.signal });
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
    if (!r.ok) {
      // 把 detail 一起带出来：工具失败时用户/模型需要知道「为什么」，只回「调用失败」等于没信息
      const err = data && data.error;
      const base = (err && typeof err === "object" ? err.message : err) || (data && data.detail) || `HTTP ${r.status}`;
      const detail = err && typeof err === "object" && err.detail ? `: ${err.detail}` : "";
      const msg = typeof base === "string" ? base + detail : JSON.stringify(base).slice(0, 200);
      // 上游网关（Spark / Voice Studio）挂掉时，nginx 会回一整页 HTML；解析不出 JSON 就只剩
      // 一个光秃秃的「HTTP 502」。这里换成能读懂的话，别让人以为是本地服务坏了。
      const html = data && data.raw && /<html[\s>]/i.test(data.raw);
      if (html || /^HTTP 5\d\d$/.test(msg)) {
        throw new Error(`上游服务暂时不可用（${/^HTTP 5\d\d$/.test(msg) ? msg.slice(5) : r.status}）——Spark / Voice Studio 网关没回 JSON，稍后再试`);
      }
      throw new Error(msg);
    }
    return data;
  } finally { clearTimeout(timer); }
}

async function get(path) {
  const r = await fetch(path, { headers: headers() });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/** 四个服务端的「现在怎么样」（与监控页同源，给 agent 当工具用）。 */
export const fetchStatus = () => get("/tool/status");

/** 联网搜索（网关走 Bing RSS，返回 title/url/snippet）。 */
export const searchWeb = (q, n = 5) => post("/tool/search", { q, n }, 30000);

/** Spark 生图只接受 16 倍数的宽高和 1..8 步；模型经常给 "16:9"/40 步，这里先归一。 */
const IMAGE_SIZE_BY_RATIO = { "16:9": "1280x720", "9:16": "720x1280", "1:1": "1024x1024",
                              "4:3": "1024x768", "3:4": "768x1024", "21:9": "2048x864" };
function normImageSize(raw) {
  let s = String(raw || "").trim().toLowerCase().replace(/×/g, "x").replace(/\s+/g, "");
  if (IMAGE_SIZE_BY_RATIO[s]) return IMAGE_SIZE_BY_RATIO[s];
  const m = s.match(/^(\d+)x(\d+)$/);
  if (m) {
    const w = Math.max(256, Math.min(2752, Math.round(+m[1] / 16) * 16));
    const h = Math.max(256, Math.min(2048, Math.round(+m[2] / 16) * 16));
    return `${w}x${h}`;
  }
  return "1024x1024";
}

/** 文生图 / 图编辑。refs = dataURL 数组；1 张走 edits，多张先拼成一张再 edits。 */
export async function generateImage({ prompt, refs = [], size = "1024x1024", steps = 8, seed = null, transparent = false }) {
  size = normImageSize(size);
  steps = Math.max(1, Math.min(Number(steps) || 8, 8));
  const body = { prompt, size, steps, transparent };
  if (seed != null && seed !== "") body.seed = Number(seed);
  if (!refs.length) {
    const out = await post("/spark/image/v1/images/generations", body);
    return pickImage(out);
  }
  const base = refs.length === 1 ? refs[0] : await composeGrid(refs);
  const out = await post("/spark/image/v1/images/edits", { ...body, image: base });
  return pickImage(out);
}

function pickImage(out) {
  const d = (out && out.data && out.data[0]) || {};
  if (!d.b64_json) throw new Error("响应里没有图片数据");
  return { url: `data:image/png;base64,${d.b64_json}`, seed: d.seed, tiling: out.tiling };
}

/** 多参考图 → 一张拼接图。图生图接口只收单张底图，所以把参考图并成一张再喂，
 *  这样模型是**真的看见**这些图，而不是靠文字转述（转述会丢细节）。 */
export function composeGrid(dataUrls, cell = 768) {
  return new Promise((resolve, reject) => {
    const n = dataUrls.length;
    const cols = Math.ceil(Math.sqrt(n));
    const rows = Math.ceil(n / cols);
    const cv = document.createElement("canvas");
    cv.width = cols * cell;
    cv.height = rows * cell;
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, cv.width, cv.height);
    let done = 0, failed = false;
    dataUrls.forEach((src, i) => {
      const img = new Image();
      img.onload = () => {
        if (failed) return;
        const cw = cv.width / cols, ch = cv.height / rows;
        const scale = Math.min(cw / img.width, ch / img.height);
        const w = img.width * scale, h = img.height * scale;
        const x = (i % cols) * cw + (cw - w) / 2;
        const y = Math.floor(i / cols) * ch + (ch - h) / 2;
        ctx.drawImage(img, x, y, w, h);
        if (++done === n) resolve(cv.toDataURL("image/png"));
      };
      img.onerror = () => { failed = true; reject(new Error("参考图解码失败")); };
      img.src = src;
    });
  });
}

/** 向量化（Spark wemm-embedding-9b，4096 维可截断）。 */
export async function embedText(input, { dims = 1024, mode = "query" } = {}) {
  const body = { input, mode };
  if (dims) body.dimensions = dims;
  return post("/spark/embed/v1/embeddings", body, 60000);
}

/** 精排（Spark qwen3-vl-reranker-8b）。 */
export async function rerankText(query, documents, topN = null) {
  const body = { query, documents };
  if (topN) body.top_n = topN;
  return post("/spark/rerank/v1/rerank", body, 60000);
}

/** 胡桃音色（网关本地 worker，不需要用户自己的 key）。 */
export const ttsLocalVoices = () => get("/tts/voices");

/** 用胡桃音色朗读一段回答。返回一个 blob URL，直接喂给 <audio>。 */
export async function ttsSay(text, speed = 1) {
  const r = await fetch("/tts/say", {
    method: "POST", headers: headers(true), body: JSON.stringify({ text, speed }),
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error?.message || msg; } catch (e) { /* not json */ }
    throw new Error(msg);
  }
  return URL.createObjectURL(await r.blob());
}

/** Voice Studio：列出音色（需要用户自己的 X-Voice-Key，代理原样转发）。 */
export async function ttsVoices(voiceKey) {
  const r = await fetch("/tts/api/v1/voices", { headers: { ...headers(), "X-Voice-Key": voiceKey || "" } });
  if (r.status === 401) throw new Error("需要 Voice Studio 的 API Key（在它的「个人空间」里创建）");
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/** Voice Studio：合成。返回 { job } —— 具体形状由后端决定，页面只展示音频地址。 */
export async function ttsSynth(voiceKey, payload) {
  const r = await fetch("/tts/api/v1/tts", {
    method: "POST",
    headers: { ...headers(true), "X-Voice-Key": voiceKey || "" },
    body: JSON.stringify(payload),
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { raw: text }; }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${r.status}`);
  return data;
}

/** Agent 优化提示词：先联网找资料，再让本地模型把一句话扩写成可执行的生图提示词。
 *  注意分工：**参考图不进这个函数** —— 生图时原图直接当底图喂给模型（generateImage），
 *  这里只写文字侧。让模型「看图再描述」会把细节丢光，用户明确不要那种做法。 */
export async function agentRefine(userPrompt, { refs = [], search = true, model = "qwen3.8-flash" } = {}) {
  let sources = [];
  if (search) {
    try { sources = (await searchWeb(userPrompt, 4)).results || []; } catch (e) { sources = []; }
  }
  const sys = "你是生图提示词工程师。把用户需求改写成一条可直接执行的图像生成提示词：主体、构图、光线、材质、风格、画幅。只输出提示词本身，不要解释，不要 markdown。";
  const ctx = sources.length ? "\n可参考的公开资料标题：\n" + sources.map((s) => `- ${s.title}`).join("\n") : "";
  const refNote = refs.length
    ? `\n用户提供了 ${refs.length} 张参考图，生成时会把参考图原图作为底图输入；不要用文字描述它们的长相，需要指代时写「以参考图中的主体为准」这类说法。`
    : "";
  const r = await post("/v1/chat/completions", {
    model, temperature: 0.7, max_tokens: 400,
    messages: [{ role: "system", content: sys }, { role: "user", content: userPrompt + ctx + refNote }],
  }, 180000);
  const prompt = ((r && r.choices && r.choices[0] && r.choices[0].message && r.choices[0].message.content) || "").trim();
  return { prompt: prompt || userPrompt, sources };
}

/** 把 Blob/dataURL 变成 dataURL（画布、上传都能用）。 */
export function fileToDataURL(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(new Error("读取文件失败"));
    r.readAsDataURL(file);
  });
}
// ------------------------------------------------------------------ 工作空间（每用户独立目录）
/** 工作空间元信息：skills / mcp / images。网关按登录会话或 API token 分目录，互不可见。 */
export const getWorkspace = () => get("/workspace");

export async function saveSkill(name, content) {
  return post("/workspace/skill", { name, content }, 30000);
}
export async function deleteSkill(name) {
  const r = await fetch(`/workspace/skill?name=${encodeURIComponent(name)}`, { method: "DELETE", headers: headers() });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
export async function getSkill(name) {
  return get(`/workspace/skill?name=${encodeURIComponent(name)}`);
}
export async function saveMcp(name, url) {
  return post("/workspace/mcp", { name, url }, 30000);
}
export async function deleteMcp(name) {
  const r = await fetch(`/workspace/mcp?name=${encodeURIComponent(name)}`, { method: "DELETE", headers: headers() });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
/** 把生成的图片存进工作空间，返回可长期访问的 URL（data URL 太大会把会话撑爆）。 */
export async function persistImage(dataUrl) {
  return post("/workspace/image", { data: dataUrl }, 60000);
}

// ------------------------------------------------------------------ 工具
export const fetchUrl = (url, maxChars = 8000) => post("/tool/fetch", { url, max_chars: maxChars }, 40000);
export const imageSearch = (q, n = 6) => post("/tool/image_search", { q, n }, 40000);
export const mcpList = () => post("/tool/mcp/list", {}, 60000);
export const mcpCall = (server, tool, args) => post("/tool/mcp/call", { server, tool, args }, 90000);

/** 把远程图片拉成 dataURL：参考图要直接喂给生图模型，而搜索结果的直链在浏览器里
 *  既读不到像素（CORS）也画不进 canvas —— 交给网关在服务端抓回来转 base64。 */
export async function urlToDataURL(url) {
  const j = await post("/tool/fetch_image", { url }, 60000);
  return j.data;
}

// ------------------------------------------------------------------ 服务端 Agent Runner
//
// 运行在网关里：页面刷新/切页面不会中断模型流和工具执行。客户端只负责提交和重放事件。
export async function startAgentRun({ chatId, model, messages, maxRounds = 8, params = {} }) {
  return post("/agent/run", { chat_id: chatId, model, messages, max_rounds: maxRounds, params }, 60000);
}

export async function stopAgentRun(runId) {
  return post("/agent/stop", { run_id: runId }, 30000);
}

/** 打开 SSE 事件流；调用方用 reader 逐行解析 data:。signal 用来在停止/卸载时断开。 */
export async function openAgentStream(runId, cursor = 0, signal) {
  const r = await fetch(`/agent/stream?run_id=${encodeURIComponent(runId)}&cursor=${cursor}`,
                        { headers: headers(), signal });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error?.message || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.body.getReader();
}
