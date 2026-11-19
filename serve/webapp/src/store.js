// 全局状态与轮询：标签页/主题/引擎指标/MCP/并发数据。所有视图从这里取数，只有一个轮询器。
import { reactive } from "vue";
import { store, headers, toast } from "./api.js";
import i18n from "./i18n.js";   // 这里不在组件里，只能直接取全局词条
import { auth, openLogin } from "./auth.js";

const t = (k, p) => i18n.global.t(k, p);

// temperature 默认 0（贪心）。原因不是口味，是机制：引擎里只要一个槽带采样参数，整组就退回逐 token
// 窗口、MTP 投机被关掉（generate.cpp 的 `if (sl.active && sl.sampled) return one_token()`）；
// 实测同一负载贪心 98 tok/s vs 采样 62 tok/s。用户仍可在「采样」抽屉里调高。
export const DEFAULTS = { thinking: "high", temperature: 0, top_p: 0.95, top_k: 20, max: "", seed: "",
                         show: true, esp: true, mcp: true };

// 对话框的「用哪条通道的引擎」。值必须是网关 model_routes 里登记过的模型名：
//   qwen3.8-flash      -> 8092 普道 B
//   qwen3.8-flash-fast -> 8091 快道 A
// 网关按 model 字段先路由，引擎本身不认识这个名字也没关系（它照常服务已加载的模型）。
export const ENGINES = [
  { id: "qwen3.8-flash", key: "chat.engine.normal" },
  { id: "qwen3.8-flash-fast", key: "chat.engine.fast" },
];
const _savedEngine = store.get("engine.model", ENGINES[0].id);
export const engine = reactive({
  model: ENGINES.some((e) => e.id === _savedEngine) ? _savedEngine : ENGINES[0].id,
});
export function setEngine(id) {
  if (!ENGINES.some((e) => e.id === id)) return;
  engine.model = id;
  store.set("engine.model", id);
}

// 标签页白名单：地址栏 #xxx 只认这几个，别的都退回对话页。
// `lab` 保留在名单里只为兼容旧深链 —— LLM Lab 已经并进训练页，路由时统一折算成 train。
const TABS = ["chat", "lab", "train", "monitor", "records", "viz", "about"];
const normTab = (n) => (n === "lab" ? "train" : n);

/** 页头导航只放「公共页」。打开服务就是对话页（没有单独的首页）。 */
export const VIEWS = [
  { id: "chat", icon: "chat" },
  { id: "monitor", icon: "activity" },
  { id: "records", icon: "disk" },
  { id: "viz", icon: "cube" },
  { id: "train", icon: "chart" },
  { id: "about", icon: "info" },
];

// 界面字体三选一。默认腾讯体（见 public/tokens.css 的 @font-face 与 html[data-font] 规则）。
export const FONTS = ["tencent", "outfit", "system"];

export const app = reactive({
  tab: TABS.includes(normTab(location.hash.slice(1))) ? normTab(location.hash.slice(1)) : "chat",
  theme: document.documentElement.dataset.theme || "light",
  font: FONTS.includes(document.documentElement.dataset.font) ? document.documentElement.dataset.font : "tencent",
  health: { model: "strata", images: false, max_context: 0 },
  metrics: null,                 // GET /metrics（单台引擎：页面上那八张卡与请求表）
  mcp: { servers: [], tools: 0 },
  concurrency: null,             // GET /concurrency（代理侧：两条通道 + 全部在途请求）
  reqShowAll: false,             // 监控页：请求表显示全部（引擎自己的表，退回模式用）
  concView: store.get("concView", "aggregate"),   // 并发区块：聚合 / 分路
  needKey: false,                // 服务器要 key 而没给
  unreachable: false,            // 连续失败：连不上
  // 会话栏展开 / 收起。手机上**一律从收起开始**，不沿用桌面存下来的偏好 ——
  // 那边它是覆盖式抽屉，而桌面那份偏好很可能是「展开」，一开页就盖住正文。
  side: window.matchMedia("(max-width: 720px)").matches ? false : store.get("side", true),
});

export const settings = reactive({ ...DEFAULTS, ...store.get("sampling", {}) });

export function setTab(name) {
  if (name === "login") { auth.showLogin = true; return; }   // 登录页不进标签页体系
  auth.showLogin = false;
  name = normTab(name);
  app.tab = TABS.includes(name) ? name : "chat";
  // 所有页面都带 hash —— 刷新/收藏/H5 分享都能回到同一页；"/" 默认落到对话页。
  if (location.hash.slice(1) !== app.tab) history.replaceState(null, "", `#${app.tab}`);
  if (app.tab === "monitor") loadMcp();
}

// ---- 主题
export function setTheme(t, save) {
  app.theme = t === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = app.theme;
  if (save) try { localStorage.setItem("strata.theme", app.theme); } catch (e) { /* ignore */ }
}
export const flipTheme = () => setTheme(app.theme === "dark" ? "light" : "dark", true);

// ---- 左侧栏
export function setSide(on) {
  app.side = !!on;
  store.set("side", app.side);
}

export function toggleSide() {
  setSide(!app.side);
}

/** 窄屏（手机）判断。手机上会话栏是覆盖式抽屉，行为跟桌面的窄轨不一样。 */
export const isNarrow = () => window.matchMedia("(max-width: 720px)").matches;

// ---- 界面字体
export function setFont(f) {
  app.font = FONTS.includes(f) ? f : "tencent";
  document.documentElement.dataset.font = app.font;
  store.set("font", app.font);
}
export function applySystemTheme() { setTheme(document.documentElement.dataset.theme || "light", false); }
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  let saved = null;
  try { saved = localStorage.getItem("strata.theme"); } catch (err) { /* ignore */ }
  if (!saved) setTheme(e.matches ? "dark" : "light", false);
});

// ---- 引擎能力（模型名、是否支持图片）
export async function loadHealth() {
  try {
    const h = await (await fetch("health")).json();
    // 代理的 /health 没有 model 字段；保留默认/当前选择的模型名，别让它变成 undefined。
    app.health = { ...app.health, ...h, model: h.model || engine.model };
  } catch (e) {
    setTimeout(loadHealth, 2000);
  }
}

// ---- 引擎指标（八张卡 + 请求表 + 关于页）
// 交给引擎的那条通道由代理按路径哈希决定；代理的 /concurrency 里会给出是哪一条（metrics_lane）。
let metricsFailures = 0, keyWarned = false;
export async function pollMetrics() {
  try {
    const r = await fetch(app.reqShowAll ? "metrics?requests=all" : "metrics", { headers: headers() });
    if (r.status === 401) {
      app.needKey = true;
      // 401 现在有两个来源：没登录，或登录了但服务端没用 key —— 提示要把两条路都说清楚
      if (!keyWarned) {
        keyWarned = true;
        toast("warn", t("toast.nokey"), t("toast.nokey.sub"), 8000,
              { label: t("toast.nokey.go"), run: () => openLogin() });
      }
    } else if (r.ok) {
      app.metrics = await r.json();
      app.needKey = false; app.unreachable = false;
      metricsFailures = 0;
    } else {
      throw new Error(`HTTP ${r.status}`);
    }
  } catch (e) {
    if (++metricsFailures === 3) {
      app.unreachable = true;
      toast("error", "连不上服务", String(e.message || e), 6000);
    }
  }
}

// ---- 并发数据（代理侧；直连引擎时 404 → 保持 null，页面退回单引擎视图）
export async function pollConcurrency() {
  try {
    const r = await fetch("concurrency", { headers: headers() });
    app.concurrency = r.ok ? await r.json() : null;
  } catch (e) {
    app.concurrency = null;      // 直连引擎、或代理版本较旧：隐藏并发区块
  }
  pushConcSample(app.concurrency);
}

// 并发趋势的滚动窗口。代理只会告诉你「此刻是多少」，折线图要的是「刚才一直是多少」，
// 所以在这边每次轮询都留一个采样点。90 点 × 2 秒 ≈ 3 分钟，够看出趋势又不至于撑爆内存。
export const CONC_WINDOW = 90;
export const concHistory = reactive([]);      // reactive：折线图直接依赖它，别让重渲染靠别的字段带着走

/** 数值就要数值：代理那边缺字段时给的是 null，字符串/undefined 一律当缺测（null），别让它把线拉到 0。 */
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** 一条通道上报的**逐卡**读数，归一化成「一张卡一项」（监控页一卡一组的数据源）。
 *  · 新引擎下发 `gpus[]`：每项带**卡号** `index`（还有功耗上限与 PCIe）；层切分时一条通道会有好几项。
 *  · 老引擎只有 `gpu`（一张卡、**没有卡号**）→ 合成一项、`index` 留 null，页面写「卡号未上报」，不去猜
 *    （两条通道两张同型号卡时显存只差十几 MiB，猜就是编；卡号只有坐在卡上的进程知道）。
 *  · `up:false` → 空数组：通道挂了下发的缓存还是**上一次成功**的那份（代理出错只加 `error` 不清
 *    `LANE_CACHE.status`），照画就是「卡还活着」的谎，宁可断线。 */
export function laneCards(l) {
  if (!l || !l.up) return [];
  const rows = l.gpus && l.gpus.length ? l.gpus : (l.gpu ? [l.gpu] : []);
  return rows.map((g) => ({
    index: num(g.index), name: g.name || "", util: num(g.util_pct), vram: num(g.used_mib),
    vram_total: num(g.total_mib), temp: num(g.temp_c), power: num(g.power_w),
    power_limit: num(g.power_limit_w), pcie_gen: num(g.pcie_gen), pcie_gen_max: num(g.pcie_gen_max),
    pcie_width: num(g.pcie_width), pcie_rx_mb: num(g.pcie_rx_mb), pcie_tx_mb: num(g.pcie_tx_mb),
  })).filter((c) => c.index != null || c.util != null || c.vram != null || c.name);
}

function pushConcSample(c) {
  if (!c || !c.lanes) return;
  concHistory.push({
    t: Date.now(),
    lanes: c.lanes.map((l) => ({
      url: l.url, label: l.label, up: !!l.up,
      tok_s: (l.live || {}).tok_s == null ? null : (l.live || {}).tok_s,
      in_flight: l.in_flight || 0,
      state: (l.live || {}).state || "idle",
      // 逐卡通电：每条通道把自己**每张卡**的读数（含卡号）报上来，这里留下的就是每张卡自己的历史 ——
      // 别的数据源做不到这件事（/metrics 被网关按会话粘性钉在一条道上，见 MonitorView 的同名注释）。
      gpus: laneCards(l),
    })),
  });
  if (concHistory.length > CONC_WINDOW) concHistory.splice(0, concHistory.length - CONC_WINDOW);
}

// ---- MCP 服务
let mcpRetry = null;
export async function loadMcp() {
  try {
    const r = await fetch("mcp", { headers: headers() });
    if (!r.ok) return;
    app.mcp = await r.json();
  } catch (e) { return; /* 旧版服务器：没有 MCP */ }
  clearTimeout(mcpRetry);
  if ((app.mcp.servers || []).some((s) => s.status === "starting")) mcpRetry = setTimeout(loadMcp, 3000);
}

/** 引擎是否带实验性提速投影（INFO cvec=…）——「关于」与采样抽屉用它决定是否显示该开关。 */
export function projectionLoaded() {
  const c = app.metrics && app.metrics.engine ? app.metrics.engine.cvec : 0;
  return !!c && c !== "0";
}

// ---- 全部 AI 服务（网关 /concurrency 自 2026-11-02 起带 services / gpus / spark）
// 监控页与模型广场共用这三个取值：数据只有一份，谁也别自己再去拉一遍。
export const servicesNow = () => (app.concurrency && app.concurrency.services) || [];
export const gpusNow = () => (app.concurrency && app.concurrency.gpus) || [];
export const sparkNow = () => (app.concurrency && app.concurrency.spark) || { cards: [] };

let started = false;
export function startPolling() {
  if (started) return;
  started = true;
  loadHealth().then(loadMcp);
  pollMetrics().then(pollConcurrency);
  setInterval(pollMetrics, 1000);
  setInterval(pollConcurrency, 2000);
  setInterval(() => { if (app.tab === "monitor") loadMcp(); }, 10000);   // 服务状态变得慢：10 秒
}
