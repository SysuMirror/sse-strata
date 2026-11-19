<script setup>
// 监控视图 = 原有的单引擎仪表盘（状态卡、八张指标卡、上下文占用、最近请求、MCP）
//            + 新的并发区块（代理侧 /concurrency：各通道状态与全部在途请求）。
// 自动适配：只有一条通道时按单路显示（无聚合条、无切换）；两条及以上时同时提供「聚合 / 分路」。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { app, concHistory, laneCards } from "../store.js";
import { store, fmt, gb, kfmt, ctxfmt } from "../api.js";
import Icon from "../components/Icon.vue";
import SparkLine from "../components/SparkLine.vue";
import LineChart from "../components/LineChart.vue";
import Pager from "../components/Pager.vue";

const { t } = useI18n();

const m = computed(() => app.metrics || {});
const live = computed(() => m.value.live || {});
const hw = computed(() => m.value.hardware || {});
const st = computed(() => m.value.hardware_static || {});
const eng = computed(() => m.value.engine || {});
const h = computed(() => m.value.history || {});
const requests = computed(() => m.value.requests || []);
const last = computed(() => requests.value[0]);

// ---------------------------------------------------------------- 并发（代理侧）
const conc = computed(() => app.concurrency);
const lanes = computed(() => (conc.value ? conc.value.lanes || [] : []));
const multi = computed(() => lanes.value.length > 1);
const agg = computed(() => (conc.value ? conc.value.aggregate || {} : {}));
const proxyInfo = computed(() => (conc.value ? conc.value.proxy || {} : {}));

const metricsLaneLabel = computed(() => {
  const u = proxyInfo.value.metrics_lane;
  const l = lanes.value.find((x) => x.url === u);
  return l ? l.label : "";
});

const LANE_BADGE = { generating: ["st-badge--generating", "mon.badge.generating"],
                     reading: ["st-badge--reading", "mon.badge.reading"],
                     queued: ["st-badge--queued", "mon.badge.queued"], idle: ["", "mon.badge.idle"] };
function laneBadge(l) { return !l.up ? "st-badge--error" : (LANE_BADGE[(l.live || {}).state] || LANE_BADGE.idle)[0]; }
function laneStateText(l) {
  if (!l.up) return t("conc.lane.offline");
  return t((LANE_BADGE[(l.live || {}).state] || LANE_BADGE.idle)[1]);
}
const vramText = (l) => {
  const g = l.gpu || {};
  return g.total_mib == null ? "–" : `${fmt(g.used_mib / 1024, 1)} / ${fmt(g.total_mib / 1024, 0)} GB`;
};
const uptime = (s) => {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${hh}h` : hh ? `${hh}h ${mm}m` : `${mm}m`;
};

const STATE_KEY = { queued: "conc.state.queued", forwarding: "conc.state.forwarding", streaming: "conc.state.streaming",
                    completed: "conc.state.completed", error: "conc.state.error", disconnected: "conc.state.disconnected" };
const STATE_BADGE = { queued: "st-badge--queued", forwarding: "st-badge--reading", streaming: "st-badge--generating",
                      completed: "", error: "st-badge--error", disconnected: "st-badge--queued" };
// 在途（实时）在前，已结束（新→旧）在后；分路视图里按通道聚拢
const rows = computed(() => {
  const c = conc.value;
  if (!c) return [];
  const liveRows = (c.in_flight || []).map((e) => ({ ...e, isLive: true }));
  const doneRows = (c.recent || []).slice(-20).reverse().map((e) => ({ ...e, isLive: false }));
  // 在途在前，其余按开始时间新→旧（原来还有一套「按通道聚拢」的排序，
  // 那个是配合通道卡的；现在通道靠颜色区分，同一张表里混着看更自然）
  return [...liveRows, ...doneRows].slice(0, 60);
});

// ---------------------------------------------------------------- 多路趋势（折线图）
//
// 通道身份与配色「跟着通道走，不跟着排名走」：先出现的通道固定占住第一个色位。
// 某条通道掉线时，剩下的线**不会**重新着色 —— 否则一眼看过去像是换了条通道。
const LANE_COLORS = ["var(--st-chart-1)", "var(--st-chart-2)", "var(--st-chart-3)", "var(--st-chart-4)"];
const laneColor = (i) => LANE_COLORS[i] || "var(--st-ink-muted)";

const laneList = computed(() => {
  const seen = new Map();
  for (const s of concHistory) for (const l of s.lanes) if (!seen.has(l.url)) seen.set(l.url, l.label);
  if (!seen.size) for (const l of lanes.value) seen.set(l.url, l.label);      // 还没采到样，先用当前值垫上
  return [...seen].map(([url, label], i) => ({ url, label, color: laneColor(i) }));
});

const chartTimes = computed(() => concHistory.map((s) => s.t));
const hasChartData = computed(() => concHistory.length > 1);
/** 窗口跨度：按首尾采样点的真实时间差算，别用「点数 × 2 秒」估 —— 轮询会漂 */
const chartSpan = computed(() => {
  if (concHistory.length < 2) return "";
  const secs = Math.round((concHistory[concHistory.length - 1].t - concHistory[0].t) / 1000);
  return secs < 90 ? `${secs} s` : `${(secs / 60).toFixed(1)} min`;
});

/** 把「每帧取通道的某个字段」写成一个序列集合 */
function seriesOf(pick) {
  return computed(() => laneList.value.map((l) => ({
    key: l.url, label: l.label, color: l.color,
    values: concHistory.map((s) => {
      const row = s.lanes.find((x) => x.url === l.url);
      return row ? pick(row) : null;          // 那一刻这条通道没上报 → null；线会断开而不是掉到 0
    }),
  })));
}
const speedSeries = seriesOf((r) => r.tok_s);
const inflightSeries = seriesOf((r) => r.in_flight);
const liveLaneOf = (url) => lanes.value.find((x) => x.url === url) || {};
const speedNow = (l) => {
  const x = liveLaneOf(l.url).live || {};
  return x.tok_s == null ? "–" : fmt(x.tok_s, 1);
};

// ---------------------------------------------------------------- 逐卡 GPU（一张卡一组）
//
// 为什么非走 /concurrency 不可：代理把 "/metrics" 当**会话键**丢进粘性最少负载路由
// （rag_proxy.py:216-238，TTL 1800s）⇒ 引擎侧的 `hardware.gpu_*` 那套标量只反映**一条**通道，
// 粘性一过期换道，曲线会无声串到另一张卡上。逐卡的负载/显存/温度/功耗/PCIe 只有
// `/concurrency` 的 `lanes[].gpus` 里有（代理每 2s 向各通道各取一次），历史则是本地按采样点攒的。
// 没有并发数据时（直连引擎、这端点 404）退回 /metrics 的那一张卡，块头不消失。
//
// **卡号（GPU 0 / GPU 1）只有坐在卡上的进程知道**：新引擎在 `gpus[].index` 里带上来（serve/telemetry.py），
// 老引擎不带 → 这里标「卡号未上报」，**不去猜**。两条通道两张同型号卡时显存只差十几 MiB，
// 拿数值配对就是编故事，宁可不写。
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** 卡的身份键 = 「这条通道的第几张」。
 *  为什么不拿卡号当键：一条道报不报卡号（引擎升级前后、或换了个不上报的构建）会让同一个物理设备换键，
 *  折线和配色就当两张新卡重来。位置在「一条道一张卡」的常见布局里就是那条卡的稳定身份；卡号是**显示用的数据**，
 *  每帧从最新一份上报里取（gpuCards 里的 idx）。 */
const cardKey = (url, pos) => `${url}#${pos}`;
const cardOf = (gpus, c) => (gpus || [])[c.pos] || null;

/** 卡的身份：先见的先着色、掉线不重排（与 laneList 同一规矩）。色位跟着通道，一条通道切多张卡时顺位补色。 */
const cardIdentities = computed(() => {
  const seen = new Map();
  const add = (url, lane, c, pos) => { const k = cardKey(url, pos);
    if (!seen.has(k)) seen.set(k, { key: k, url, lane, index: c.index, pos }); };
  for (const s of concHistory) for (const l of s.lanes) (l.gpus || []).forEach((c, pos) => add(l.url, l.label, c, pos));
  for (const l of lanes.value) laneCards(l).forEach((c, pos) => add(l.url, l.label, c, pos));
  const used = new Set();
  return [...seen.values()].map((c) => {
    const li = laneList.value.findIndex((l) => l.url === c.url);
    let color = li >= 0 ? laneColor(li) : null;
    if (!color || used.has(color)) color = LANE_COLORS.find((x) => !used.has(x)) || "var(--st-ink-muted)";
    used.add(color);
    return { ...c, color };
  });
});

/** 没有并发数据时（直连引擎、/concurrency 404）：本引擎自己那几张卡。新引擎的 `hardware.gpus[]` 就是逐卡那份
 *  （层切分时不止一张）；老引擎只有 `gpu_*` 标量 → 合成一张，卡号取 `hardware_static.gpu_indices[0]`，没有就标
 *  「卡号未上报」。曲线只能用 `history` 里的**合计**序列（引擎侧没给每张卡单独留历史）。 */
const selfCards = computed(() => {
  const rows = (hw.value.gpus || []).map((x, pos) => ({ key: `self#${pos}`, url: null, lane: "", pos,
    index: x.index ?? null, name: x.name || "", raw: x, color: laneColor(pos) }));
  if (rows.length) return rows;
  if (!st.value.gpu_name && hw.value.gpu_util == null) return [];
  return [{ key: "self#0", url: null, lane: "", pos: 0, index: (st.value.gpu_indices || [null])[0],
            name: st.value.gpu_name || "", raw: null, color: laneColor(0) }];
});
const cardList = computed(() => (cardIdentities.value.length ? cardIdentities.value : selfCards.value));
const hasGpu = computed(() => allCards.value.length > 0);

const SELF_SERIES = { util: "gpu_util", vram: "gpu_mem_used", temp: "gpu_temp", power: "gpu_power",
                      pcie_rx: "gpu_pcie_rx_mb" };

/** 这张卡此刻的读数 `{ g, live }`。通道掉线/从列表里消失时代理下发的还是**最后一次成功**的那份
 *  （出错只加 error 不清 LANE_CACHE.status）→ 退回本地采样的最新一份并标 live:false，别当现值读。 */
function cardReading(c) {
  const w = hw.value;
  if (!c.url) {
    // /metrics 的显存是**字节**，而这里整套 g.vram 的口径是 MiB（代理那条道给的 used_mib 就是 MiB）→ 先换算
    const mib = (v) => (typeof v === "number" && Number.isFinite(v) ? v / 2 ** 20 : null);
    if (c.raw) {                       // 引擎直接给了这张卡自己的读数（hardware.gpus[]）
      const x = c.raw;
      return { live: true, g: { index: x.index ?? null, name: x.name, util: num(x.util), vram: mib(x.mem_used),
        vram_total: mib(x.mem_total), temp: num(x.temp), power: num(x.power), power_limit: num(x.power_limit),
        pcie_gen: num(x.pcie_gen), pcie_gen_max: num(x.pcie_gen_max), pcie_width: num(x.pcie_width),
        pcie_rx_mb: num(x.pcie_rx_mb) } };
    }
    return { live: true, g: { index: c.index ?? null, util: num(w.gpu_util),
      vram: mib(w.gpu_mem_used), vram_total: mib(w.gpu_mem_total), temp: num(w.gpu_temp), power: num(w.gpu_power),
      power_limit: num(w.gpu_power_limit), pcie_gen: num(w.gpu_pcie_gen), pcie_gen_max: num(w.gpu_pcie_gen_max),
      pcie_width: num(w.gpu_pcie_width), pcie_rx_mb: num(w.gpu_pcie_rx_mb) } };
  }
  const g = cardOf(laneCards(liveLaneOf(c.url)), c);
  if (g) return { live: true, g };
  for (let i = concHistory.length - 1; i >= 0; i--) {
    const row = concHistory[i].lanes.find((x) => x.url === c.url) || {};
    const last = cardOf(row.gpus || [], c);
    if (last) return { live: false, g: last };
  }
  return { live: false, g: {} };
}
const cardOffline = (c) => (c.url ? !liveLaneOf(c.url).up : false);
/** 这张卡的历史：有并发就按采样点取它自己那份，没有就用引擎侧的 1 秒序列。
 *  单位与 cardReading 对齐（vram = MiB；/metrics 那侧给的是字节 → 换算，别按 MiB 直读 GB 刻度）。 */
function cardSeries(c, field) {
  if (!c.url) {
    const raw = h.value[SELF_SERIES[field]] || [];
    return field === "vram" ? raw.map((v) => (v == null ? null : v / 2 ** 20)) : raw;
  }
  return concHistory.map((s) => { const x = cardOf((s.lanes.find((y) => y.url === c.url) || {}).gpus || [], c);
    return x ? x[field] : null; });
}

/** 一卡一组：标题（卡号优先）、这张卡上跑的是哪条通道，五格读数 + 各自的曲线 */
const gpuCards = computed(() => cardList.value.map((c) => {
  const { g, live } = cardReading(c);
  const lane = c.url ? liveLaneOf(c.url) : null;
  const gen = g.pcie_gen_max || g.pcie_gen;
  const rx = g.pcie_rx_mb;
  // 卡号**以这一次上报为准**：通道在线时它报了什么就是什么（老引擎没报 → 「未上报」，哪怕三分钟里曾经报过 ——
  // 换引擎 / 改 profile 会把同一条道挪到另一张卡上，不能拿旧账署名）。通道掉线时才沿用最后一次报到的号。
  const idx = live ? (g.index ?? null) : (g.index ?? c.index ?? null);
  return {
    ...c, key: c.key, index: idx,
    title: idx == null ? t("mon.gpu.noname") : `GPU ${idx}`,
    // 短标签：给「上下文占用」那种一行塞不下一句解释的地方用（没卡号时退回通道名，不至于重复两遍「卡号未上报」）
    short: idx == null ? (c.lane || t("mon.gpu.noname")) : `GPU ${idx}`,
    name: g.name || c.name || "",
    vramGb: g.vram_total == null ? null : g.vram_total / 1024,
    temp: num(g.temp), util: num(g.util),
    offline: cardOffline(c),
    badge: lane ? { text: laneStateText(lane), cls: laneBadge(lane) } : null,
    metrics: [
      { key: "util", label: t("metric.gpu"), value: g.util == null ? "–" : fmt(g.util), unit: "%",
        series: cardSeries(c, "util"), max: 100 },
      { key: "vram", label: t("metric.vram"), value: g.vram == null ? "–" : fmt(g.vram / 1024, 1),
        unit: g.vram_total == null ? "GB" : `/ ${fmt(g.vram_total / 1024, 0)} GB`,
        // 曲线与刻度都用 GB（cardSeries 给的是 MiB，和读数同一口径）
        series: cardSeries(c, "vram").map((v) => (v == null ? null : v / 1024)),
        max: g.vram_total == null ? 0 : g.vram_total / 1024 },
      { key: "temp", label: t("metric.temp"), value: g.temp == null ? "–" : fmt(g.temp), unit: "°C",
        series: cardSeries(c, "temp"), max: 90, tone: "warn" },
      { key: "power", label: t("metric.power"), value: g.power == null ? "–" : fmt(g.power), unit: "W",
        sub: g.power_limit ? t("sub.gpu.of", { n: fmt(g.power_limit) }) : "",
        series: cardSeries(c, "power"), max: g.power_limit },
      { key: "pcie", label: t("metric.pcie"), value: gen ? `Gen${gen}` : "–",
        unit: g.pcie_width ? `x${g.pcie_width}` : "",
        // 没上报就别空着不说话：逐卡 PCIe 是新构建才有的字段
        sub: gen ? (rx == null ? "" : `to GPU ${fmt(rx, rx < 10 ? 1 : 0)} MB/s`
             + (g.pcie_gen && gen && g.pcie_gen < gen ? ` · idle Gen${g.pcie_gen}` : "")) : t("mon.gpu.pcie.none"),
        series: cardSeries(c, "pcie_rx"), tone: "info" },
    ],
  };
}));

const gpuSeriesOf = (field, scale = 1) => computed(() => gpuCards.value.filter((c) => c.url).map((c) => ({
  key: c.key, label: c.title, color: c.color, values: cardSeries(c, field).map((v) => (v == null ? null : v / scale)),
})));
const gpuUtilSeries = gpuSeriesOf("util");
const gpuVramSeries = gpuSeriesOf("vram", 1024);                                    // MiB → GB
const gpuTempSeries = gpuSeriesOf("temp");
const gpuPowerSeries = gpuSeriesOf("power");

/** 显存图的上限用**卡的实际容量**（两张同型号就同一根轴），别让自适应刻度把 21/24 GB 画成半张图 */
const vramTop = computed(() => {
  const t = Math.max(0, ...gpuCards.value.map((c) => num(c.vramGb) || 0));
  return t ? Math.ceil(t) : 0;
});

// ---------------------------------------------------------------- 分页
const PAGE = 8;
const concPage = ref(1);
const concSlice = computed(() => rows.value.slice((concPage.value - 1) * PAGE, concPage.value * PAGE));

const REQ_PER = 10;
const reqPage = ref(1);
const reqSlice = computed(() => requests.value.slice((reqPage.value - 1) * REQ_PER, reqPage.value * REQ_PER));

// 数据变短了（下线的请求被淘汰、或换了通道）就把页码夹回最后一页，别停在空白的第 5 页上
const clamp = (page, total, per) => {
  const last = Math.max(1, Math.ceil(total / per));
  if (page.value > last) page.value = last;
};
watch(() => rows.value.length, (n) => clamp(concPage, n, PAGE));
watch(() => requests.value.length, (n) => clamp(reqPage, n, REQ_PER));

const clock = (x) => new Date(x * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const secs = (x) => (x == null ? "–" : `${Number(x).toFixed(1)} s`);

// ---------------------------------------------------------------- 状态卡
const stateOn = computed(() => (live.value.queued > 0 ? "queued" : live.value.state));
const stateTone = computed(() => (live.value.state === "reading" ? "info" : ""));
const statePct = computed(() => {
  if (live.value.state === "reading") return live.value.prompt_total ? (100 * live.value.prompt_read) / live.value.prompt_total : 0;
  if (live.value.state === "generating") return live.value.max_tokens ? Math.min(100, (100 * live.value.generated) / live.value.max_tokens) : 0;
  return 0;
});
const stateLabel = computed(() => {
  if (live.value.state === "reading") return t("mon.reading");
  if (live.value.state === "generating") return live.value.phase ? live.value.phase[0].toUpperCase() + live.value.phase.slice(1) : t("mon.generating");
  return t("mon.waiting");
});
const stateDetail = computed(() => {
  const l = live.value;
  if (l.state === "reading") {
    return l.prompt_total ? `${fmt(l.prompt_read)} / ${fmt(l.prompt_total)} tokens · ${fmt(statePct.value)}%` : `${fmt(l.prompt_tokens)} tokens`;
  }
  if (l.state === "generating") return `${fmt(l.generated)} tokens · ${fmt(l.tok_s, 1)} tok/s`;
  if (last.value) return t("mon.last", { n: fmt(last.value.output_tokens), rate: last.value.decode_tok_s ? `，${fmt(last.value.decode_tok_s, 1)} tok/s` : "" });
  return "";
});

// ---------------------------------------------------------------- 指标卡（非 GPU 的那几张）
//
// GPU 相关的五张（负载 / 显存 / 温度 / 功耗 / PCIe）2026-11-02 起**搬进「逐卡 GPU 详细数据」**：
// 它们的数据源是 /metrics，被网关的会话粘性钉在一条通道上，所以永远只有**一张**卡、还不署名是哪张。
// 逐卡那几组的数据源换成 /concurrency 的 lanes[].gpus（各通道各报自己那张卡，带卡号），两条道 = 两组。
const cards = computed(() => {
  const w = hw.value, s = st.value;
  const decoding = live.value.state === "generating";
  const speed = decoding ? live.value.tok_s : last.value ? last.value.decode_tok_s : null;
  const prefill = live.value.state !== "idle" ? live.value.prefill_tok_s_mean
    : last.value && last.value.prompt_ms > 0 ? Math.max(0, last.value.prompt_tokens - (last.value.reused || 0)) / (last.value.prompt_ms / 1000) : null;
  return [
    { key: "speed", label: t("metric.speed"), icon: "gauge", value: speed == null ? null : fmt(speed, 1), unit: "t/s",
      sub: decoding ? t("sub.decode.now") : last.value ? t("sub.decode.last") : t("sub.decode"),
      series: h.value.tok_s, dual: { value: prefill == null ? null : fmt(prefill), unit: "t/s",
        sub: live.value.state === "reading" ? t("sub.prefill.now") : decoding ? t("sub.prefill.this") : last.value ? t("sub.prefill.last") : t("sub.prefill"),
        series: h.value.prefill_tok_s_mean } },
    { key: "cpu", label: t("metric.cpu"), icon: "cpu", value: w.cpu == null ? null : fmt(w.cpu), unit: "%",
      sub: s.threads ? (s.cores ? t("sub.cores", { cores: s.cores, threads: s.threads }) : t("sub.threads", { n: s.threads })) : "",
      series: h.value.cpu, max: 100 },
    w.disk_read_mb == null
      ? { key: "disk", label: t("metric.disk"), icon: "disk", value: null, unit: "",
          sub: s.psutil ? "" : t("sub.needs.psutil"), series: h.value.disk_read_mb, tone: "info" }
      : (() => {
          const big = w.disk_read_mb >= 1000;
          return { key: "disk", label: t("metric.disk"), icon: "disk",
                   value: big ? fmt(w.disk_read_mb / 1024, 2) : fmt(w.disk_read_mb, w.disk_read_mb < 10 ? 1 : 0),
                   unit: big ? "GB/s" : "MB/s",
                   sub: w.disk_write_mb == null ? "" : t("sub.disk.write", { n: fmt(w.disk_write_mb, 1) }),
                   series: h.value.disk_read_mb, tone: "info" };
        })(),
  ];
});

// ---------------------------------------------------------------- 上下文占用
const ctx = computed(() => eng.value.max_context || 0);
const ctxUsed = computed(() => {
  if (live.value.state !== "idle") return (live.value.prompt_tokens || 0) + (live.value.generated || 0);
  if (last.value) return (last.value.prompt_tokens || 0) + (last.value.output_tokens || 0);
  return 0;
});
const ctxFrac = computed(() => (ctx.value ? Math.min(1, ctxUsed.value / ctx.value) : 0));
const expertBytes = computed(() => (eng.value.expert_cache_mib || 0) * 1048576);
const ramPct = computed(() => (hw.value.ram_total ? (100 * hw.value.ram_used) / hw.value.ram_total : 0));
// 温度一行现在**一张卡一项**（原来只有一个数，还是粘性那条道的，说不出是哪张卡的）；进度条取最热的那张。
const cardTemps = computed(() => gpuCards.value.map((c) => ({ short: c.short, temp: num(c.temp) }))
  .filter((x) => x.temp != null));
const tempPct = computed(() => (cardTemps.value.length ? Math.min(100, Math.max(...cardTemps.value.map((x) => x.temp))) : 0));

/** 「上下文占用」**一条道一行**：每条道的已用 token 取它自己上报的 `live`（空闲时退回它最近一条请求），
 *  窗口大小与专家驻留取 `/concurrency` 里每条道的 `engine`（代理 2026-11-02 起透传；没这个字段时该行只报
 *  已用 token、百分数留空，不拿另一条道的窗口凑数）。 */
const ctxLaneRows = computed(() => laneList.value.map((l) => {
  const lane = liveLaneOf(l.url), eng = lane.engine || {}, lv = lane.live || {};
  const lastReq = (lane.requests || [])[0] || {};
  const active = lv.state && lv.state !== "idle";
  const used = active ? (num(lv.prompt_tokens) || 0) + (num(lv.generated) || 0)
                      : (num(lastReq.prompt_tokens) || 0) + (num(lastReq.output_tokens) || 0);
  // 窗口优先取 /health 的静态值（代理 2026-11-02 起放在 lane.max_context）：
  // 引擎空闲/未加载时 /metrics 的 engine.max_context 与 /v1/status 的 context 都会报 0，
  // 只看那两处会让每一条道都算不出占用率 —— 用户看到的就是「只有一条上下文」。
  const total = num(lane.max_context) || num(eng.max_context) || num(eng.context) || 0;
  const card = (laneCards(lane)[0] || {});            // 那条道所在那张卡的容量（专家驻留条的分母）
  return {
    url: l.url, label: l.label, color: l.color, offline: lane.up === false,
    used, total, pct: total ? Math.min(1, used / total) : null,
    expertMib: num(eng.expert_cache_mib) || 0, expertSlots: num(eng.expert_slots) || 0,
    cardIndex: num(card.index), capGb: num(card.vram_total) == null ? null : card.vram_total / 1024,
  };
}));
/** 圆环仪表现在指向**占用最高的那条道**（原来它读 /metrics，于是永远只跟着粘性那一条，另一条道看不见）。 */
const ctxTop = computed(() => {
  const known = ctxLaneRows.value.filter((r) => r.pct != null);
  return known.length ? known.reduce((a, b) => (b.pct > a.pct ? b : a)) : null;
});
const ctxGauge = computed(() => (ctxTop.value
  ? { frac: ctxTop.value.pct, used: ctxTop.value.used, total: ctxTop.value.total, who: ctxTop.value.label }
  : { frac: ctxFrac.value, used: ctxUsed.value, total: ctx.value, who: "" }));
/** 专家驻留：一条道一段文字 + 取最挤的那条画条 */
const ctxExpertRows = computed(() => ctxLaneRows.value.filter((r) => r.expertMib || r.expertSlots));
const expertMaxPct = computed(() => {
  const pcts = ctxExpertRows.value.filter((r) => r.capGb).map((r) => Math.min(100, (100 * r.expertMib) / r.capGb));
  return pcts.length ? Math.max(...pcts) : ctxExpertPct.value;
});
// 这块整份**属于谁**：仪表里的 token 数与「常驻显存专家」都来自 /metrics，而 /metrics 被网关的会话粘性
// 钉在**一条通道**上（`proxy.metrics_lane`）⇒ 那个 GB 数不是全机的，是那条道那张卡上的。所以标题署名到
// 「哪条道 · 哪张卡」，专家条的分母也用那张卡的容量（层切分时 = 它那几张卡的合计），别再让人猜。
const ctxCards = computed(() => {
  const u = proxyInfo.value.metrics_lane;
  const mine = gpuCards.value.filter((c) => !c.url || c.url === u);
  return mine.length ? mine : gpuCards.value;
});
// 标题署名跟着**圆环仪表**走：仪表指谁就写谁（有卡号带上卡号）；没有逐道数据时退回「粘性那条道」。
const ctxSource = computed(() => {
  const top = ctxTop.value;
  if (top) return [`${top.label}${top.cardIndex == null ? "" : ` · GPU ${top.cardIndex}`}`,
                  ctxLaneRows.value.length > 1 ? t("ctx.source.peak") : ""].filter(Boolean).join(" · ");
  const lane = metricsLaneLabel.value || (!conc.value ? t("ctx.source.self") : "");
  const idxs = ctxCards.value.map((c) => c.index).filter((i) => i != null);
  return [lane, idxs.length ? `GPU ${idxs.join(" + GPU ")}` : ""].filter(Boolean).join(" · ");
});
const ctxCapGb = computed(() => {
  const sum = ctxCards.value.reduce((a, c) => a + (num(c.vramGb) || 0), 0);
  return sum || (hw.value.gpu_mem_total ? hw.value.gpu_mem_total / 2 ** 30 : 0);
});
const ctxExpertPct = computed(() => (ctxCapGb.value ? Math.min(100, (100 * expertBytes.value) / (ctxCapGb.value * 2 ** 30)) : 0));

// ---------------------------------------------------------------- 全部显卡（本机 3 张 + Spark 1 张）
// 本机三张卡由网关的 `/concurrency.gpus` 直接给（nvidia-smi 一次全读，含邻居在用的 GPU 2）；
// Spark 那张是统一内存，总量只能在那边算，网关每 10s SSH 采一次合成 `spark.cards`。
const CARD_COLORS = ["#2563eb", "#0891b2", "#94a3b8", "#7c3aed"];
const allCards = computed(() => {
  const c = app.concurrency || {};
  const out = ((c.gpus) || []).map((g) => {
    const i = num(g.index) || 0;
    const mine = i === 0 || i === 1;
    return {
      key: "gpu-" + i, color: CARD_COLORS[i] || "#64748b", title: `GPU ${i}`,
      sub: mine ? (i === 0 ? t("mon.allcards.laneA") : t("mon.allcards.laneB")) : t("mon.allcards.neighbor"),
      badge: mine ? t("mon.allcards.mine") : t("mon.allcards.shared"),
      badgeCls: mine ? "st-badge--generating" : "st-badge--queued",
      util: num(g.util), used: num(g.used_mib), total: num(g.total_mib),
      temp: num(g.temp), power: num(g.power),
    };
  });
  for (const g of (((c.spark || {}).cards) || [])) {
    out.push({ key: "spark-" + (g.index || 0), color: CARD_COLORS[3], title: `GPU ${g.index || 0}`,
               sub: t("mon.allcards.spark"), badge: "Spark", badgeCls: "st-badge--reading",
               util: num(g.util), used: num(g.used_mib), total: num(g.total_mib),
               temp: num(g.temp), power: num(g.power) });
  }
  return out;
});

// ---------------------------------------------------------------- 全部 AI 服务
// 数据源：网关的 `/concurrency.services`（2026-11-02 起）。calls/errors/in_flight 来自各服务自己的
// `/metrics`；没有计数端点的（Spark 四条路由）就是 null —— 页面显示「–」，不编 0。
const serviceList = computed(() => (app.concurrency && app.concurrency.services) || []);
const servicesUp = computed(() => serviceList.value.filter((s) => s.up).length);
function ago(ts) {
  const s = Math.max(0, Math.round((Date.now() / 1000) - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

// ---------------------------------------------------------------- 请求表（引擎自己的那份；有并发数据时用代理那份）
// 引擎自己的请求表：和并发表一样分页。原先是用「显示全部」按钮一刀切，
// 条数一多就变成一堵墙；现在保留那个按钮（它决定向引擎要多少条），显示交给分页。
const kept = computed(() => (m.value.requests_kept == null ? requests.value.length : m.value.requests_kept));
const FINISH = { stop: ["", "req.badge.done"], length: ["", "req.badge.maxtokens"], cancel: ["st-badge--queued", "req.badge.stopped"],
                 disconnect: ["st-badge--queued", "req.badge.closed"], error: ["st-badge--error", "req.badge.error"] };
const finishOf = (f) => FINISH[f] || ["", null];
const hitOf = (r) => (r.hit_rate == null ? "–" : `${(r.hit_rate * 100).toFixed(1)}%`);
const reqTime = (r) => new Date(r.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const totalsText = computed(() => {
  const tt = m.value.totals;
  if (!tt || !tt.requests) return "";
  const since = new Date(tt.since * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const read = tt.prompt_tokens - tt.reused;
  const pSpeed = tt.prompt_ms > 0 && read > 0 ? ` at ${fmt(read / (tt.prompt_ms / 1000))} tok/s` : "";
  const oSpeed = tt.decode_ms > 0 && tt.output_tokens > 0 ? ` at ${fmt(tt.output_tokens / (tt.decode_ms / 1000), 1)} tok/s` : "";
  return t("req.since", { since, requests: fmt(tt.requests), read: fmt(read), pSpeed, reused: fmt(tt.reused), out: fmt(tt.output_tokens), oSpeed });
});

// ---------------------------------------------------------------- MCP
const mcpServers = computed(() => app.mcp.servers || []);
const MCP_STATE = { ready: ["st-badge--generating", "mcp.state.ready"], starting: ["st-badge--reading", "mcp.state.starting"],
                    failed: ["st-badge--error", "mcp.state.failed"], stopped: ["st-badge--queued", "mcp.state.stopped"],
                    idle: ["", "mcp.state.idle"] };
const mcpReady = computed(() => mcpServers.value.filter((s) => s.status === "ready" || s.status === "stopped"));
</script>

<template>
  <section class="view view--monitor">
    <div class="monitor">
      <!-- ===================== 并发（代理侧） ===================== -->
      <template v-if="conc">
        <!-- 多路：汇总数字 + 各通道状态 + 两张趋势图（速度 / 在途）。
             这里原来是「每条通道一张卡」，换成了折线图 —— 卡只能告诉你此刻的瞬时值，
             趋势（谁在掉、什么时候掉的）得看线。通道的身份在下面的图例行里，一眼对得上颜色。 -->
        <div class="st-card conc-head">
          <div>
            <span class="card-title">{{ t("conc.title") }}</span>
            <span class="muted small conc-head__sub">{{ t("conc.sub") }}</span>
          </div>
          <span class="st-badge" :class="agg.lanes_up === agg.lanes ? 'st-badge--generating' : 'st-badge--queued'">
            {{ t("conc.lanes.up", { up: agg.lanes_up, all: agg.lanes }) }}</span>
        </div>

        <div class="st-card conc-sum">
          <div class="conc-nums">
            <div><span>{{ t("conc.inflight") }}</span><b>{{ agg.in_flight || 0 }}</b></div>
            <div><span>{{ t("mon.badge.generating") }}</span><b>{{ agg.generating || 0 }}</b></div>
            <div><span>{{ t("conc.agg.speed") }}</span><b>{{ fmt(agg.tok_s, 1) }}</b></div>
            <div><span>{{ t("conc.th.out") }}</span><b>{{ kfmt(agg.est_tokens_out || 0) }}</b></div>
          </div>
        </div>

        <!-- 图例（同时也是通道状态行）：色块 = 折线颜色，右边是此刻的模型/槽位/GPU/显存 -->
        <ul class="lane-key">
          <li v-for="l in laneList" :key="l.url" class="lane-key__row">
            <span class="lane-key__swatch" :style="{ background: l.color }"></span>
            <span class="lane-key__name">{{ l.label }}</span>
            <span class="st-badge" :class="laneBadge(liveLaneOf(l.url))">{{ laneStateText(liveLaneOf(l.url)) }}</span>
            <span class="lane-key__model muted small">{{ liveLaneOf(l.url).model || "–" }}</span>
            <span class="lane-key__spacer"></span>
            <span class="lane-key__now">{{ t("conc.th.speed") }} <b>{{ speedNow(l) }}</b></span>
            <span class="lane-key__meta muted small">
              {{ t("conc.lane.slots") }} {{ liveLaneOf(l.url).in_flight || 0 }}/{{ liveLaneOf(l.url).serving_slots == null ? "–" : liveLaneOf(l.url).serving_slots }}
              · GPU {{ (liveLaneOf(l.url).gpu || {}).util_pct == null ? "–" : fmt((liveLaneOf(l.url).gpu || {}).util_pct) }}%
              · {{ vramText(liveLaneOf(l.url)) }}
              · {{ uptime(liveLaneOf(l.url).uptime_s) }}</span>
          </li>
        </ul>

        <div class="conc-charts">
          <div class="st-card chart-card">
            <div class="req-head"><span class="card-title">{{ t("conc.chart.speed") }}</span>
              <span class="muted small">{{ t("conc.chart.window", { span: chartSpan }) }}</span></div>
            <LineChart v-if="hasChartData" :series="speedSeries" :times="chartTimes" unit=" t/s" :height="136" />
            <p v-else class="muted small chart-card__wait">{{ t("conc.chart.wait") }}</p>
          </div>
          <div class="st-card chart-card">
            <div class="req-head"><span class="card-title">{{ t("conc.chart.inflight") }}</span>
              <span class="muted small">{{ t("conc.lane.slots") }}</span></div>
            <LineChart v-if="hasChartData" :series="inflightSeries" :times="chartTimes" :height="136"
                       :format="(v) => fmt(v)" />
            <p v-else class="muted small chart-card__wait">{{ t("conc.chart.wait") }}</p>
          </div>
        </div>

        <!-- ===================== 逐卡 GPU（每条通道一张卡，历史由本地采样攒出） ===================== -->
        <template v-if="hasGpu">
          <div class="st-card conc-head gpu-head">
            <div>
              <span class="card-title">{{ t("mon.gpu.title") }}</span>
              <span class="muted small conc-head__sub">{{ t("mon.gpu.sub") }}</span>
            </div>
            <span class="muted small">{{ t("conc.chart.window", { span: chartSpan }) }}</span>
          </div>

          <div class="gpu-lanes">
            <div v-for="g in allCards" :key="g.key" class="st-card gpu-lane">
              <div class="gpu-lane__head">
                <span class="gpu-lane__swatch" :style="{ background: g.color }"></span>
                <span class="gpu-lane__name">{{ g.title }}</span>
                <span class="gpu-lane__lane muted small">{{ g.sub }}</span>
                <span class="st-badge" :class="g.badgeCls">{{ g.badge }}</span>
              </div>
              <div class="gpu-lane__metrics">
                <div class="gpu-metric" :data-s="'util'"><span class="gpu-metric__label">{{ t("metric.short.load") }}</span>
                  <span class="gpu-metric__value">{{ fmt(g.util) }}<small>%</small></span></div>
                <div class="gpu-metric" :data-s="'vram'"><span class="gpu-metric__label">{{ t("metric.short.vram") }}</span>
                  <span class="gpu-metric__value">{{ fmt(g.used / 1024, 1) }}<small>/ {{ fmt(g.total / 1024, 0) }} GB</small></span></div>
                <div class="gpu-metric" :data-s="'temp'"><span class="gpu-metric__label">{{ t("metric.short.temp") }}</span>
                  <span class="gpu-metric__value">{{ fmt(g.temp) }}<small>°C</small></span></div>
                <div class="gpu-metric" :data-s="'power'"><span class="gpu-metric__label">{{ t("metric.short.power") }}</span>
                  <span class="gpu-metric__value">{{ fmt(g.power) }}<small>W</small></span></div>
              </div>
            </div>
          </div>

          <!-- 一张图一根轴：利用率(%)、显存(GB)、温度(°C)、功耗(W) 各一张，一条线一张卡。
               四套量纲绝不叠双轴 —— 刻度可以随便调，看上去的「相关性」是画出来的、不是数据里的。 -->
          <div class="gpu-charts">
            <div class="st-card chart-card">
              <div class="req-head"><span class="card-title">{{ t("metric.gpu") }}</span>
                <span class="muted small">{{ t("mon.gpu.chart.max", { n: 100 }) }}</span></div>
              <LineChart v-if="hasChartData" :series="gpuUtilSeries" :times="chartTimes" unit=" %" :max="100" :height="136" />
              <p v-else class="muted small chart-card__wait">{{ t("conc.chart.wait") }}</p>
            </div>
            <div class="st-card chart-card">
              <div class="req-head"><span class="card-title">{{ t("metric.vram") }}</span>
                <span class="muted small">{{ vramTop ? t("mon.gpu.chart.max", { n: vramTop }) : "" }}</span></div>
              <LineChart v-if="hasChartData" :series="gpuVramSeries" :times="chartTimes" unit=" GB" :max="vramTop" :height="136" />
              <p v-else class="muted small chart-card__wait">{{ t("conc.chart.wait") }}</p>
            </div>
            <div class="st-card chart-card">
              <div class="req-head"><span class="card-title">{{ t("metric.temp") }}</span></div>
              <LineChart v-if="hasChartData" :series="gpuTempSeries" :times="chartTimes" unit=" °C" :height="136"
                         :format="(v) => fmt(v)" />
              <p v-else class="muted small chart-card__wait">{{ t("conc.chart.wait") }}</p>
            </div>
            <div class="st-card chart-card">
              <div class="req-head"><span class="card-title">{{ t("metric.power") }}</span></div>
              <LineChart v-if="hasChartData" :series="gpuPowerSeries" :times="chartTimes" unit=" W" :height="136"
                         :format="(v) => fmt(v)" />
              <p v-else class="muted small chart-card__wait">{{ t("conc.chart.wait") }}</p>
            </div>
          </div>
          <p class="muted small gpu-note">{{ t("mon.gpu.note") }}</p>
        </template>
        <p v-else class="muted small gpu-note">{{ t("mon.gpu.none") }}</p>

        <!-- ===================== 全部 AI 服务：每个调用的计数 ===================== -->
        <div class="st-card conc-head gpu-head">
          <div>
            <span class="card-title">{{ t("mon.svc.title") }}</span>
            <span class="muted small conc-head__sub">{{ t("mon.svc.sub") }}</span>
          </div>
          <span class="muted small">{{ servicesUp }}/{{ serviceList.length }} {{ t("mon.svc.up") }}</span>
        </div>
        <div class="svc-grid">
          <div v-for="s in serviceList" :key="s.id" class="st-card svc-card" :data-up="s.up ? '1' : '0'">
            <div class="svc-card__head">
              <span class="svc-dot" :data-up="s.up ? '1' : '0'"></span>
              <span class="svc-card__name">{{ s.label }}</span>
              <span class="muted small svc-card__group">{{ s.group }}</span>
            </div>
            <div class="svc-card__nums">
              <div><b>{{ s.calls == null ? "–" : fmt(s.calls) }}</b><span>{{ t("mon.svc.calls") }}</span></div>
              <div><b>{{ s.errors == null ? "–" : fmt(s.errors) }}</b><span>{{ t("mon.svc.errors") }}</span></div>
              <div><b>{{ s.in_flight == null ? "–" : fmt(s.in_flight) }}</b><span>{{ t("mon.svc.inflight") }}</span></div>
              <div><b>{{ s.latency_ms == null ? "–" : fmt(s.latency_ms, 0) }}<small>ms</small></b><span>{{ t("mon.svc.probe") }}</span></div>
            </div>
            <div class="svc-card__foot muted small">
              <span v-if="s.gpu_alloc_mib != null">{{ t("mon.svc.gpu") }} {{ fmt(s.gpu_alloc_mib / 1024, 1) }} GB</span>
              <span v-if="s.last_call_ts">{{ ago(s.last_call_ts) }}</span>
              <span v-if="!s.up && s.error" class="svc-card__err">{{ s.error }}</span>
            </div>
          </div>
        </div>

        <!-- 在途 + 最近结束（分页） -->
        <div class="st-card conc-req">
          <div class="req-head">
            <span class="card-title">{{ t("conc.inflight") }}</span>
            <span class="muted small">{{ t("conc.est") }} · {{ agg.in_flight || 0 }} {{ t("conc.th.inflight") }}<template
              v-if="(conc.recent || []).length"> · {{ t("conc.recent") }} {{ (conc.recent || []).length }}</template></span>
          </div>
          <div class="table-wrap">
            <table class="st-table">
              <thead><tr>
                <th>{{ t("conc.th.time") }}</th><th v-if="multi">{{ t("conc.th.lane") }}</th>
                <th>{{ t("conc.th.state") }}</th><th>{{ t("conc.th.model") }}</th>
                <th class="num">{{ t("conc.th.elapsed") }}</th><th class="num">{{ t("conc.th.ttft") }}</th>
                <th class="num">{{ t("conc.th.out") }}</th><th class="num">{{ t("conc.th.speed") }}</th>
              </tr></thead>
              <tbody>
                <tr v-if="!concSlice.length"><td :colspan="multi ? 8 : 7" class="muted">{{ t("conc.none") }}</td></tr>
                <tr v-for="(r, i) in concSlice" :key="`${r.id}-${i}`">
                  <td>{{ clock(r.started_at) }}</td>
                  <td v-if="multi">{{ r.lane_label }}</td>
                  <td><span class="st-badge" :class="STATE_BADGE[r.state] || ''">{{ STATE_KEY[r.state] ? t(STATE_KEY[r.state]) : r.state }}</span></td>
                  <td class="muted small">{{ r.model || "–" }}</td>
                  <td class="num">{{ secs(r.elapsed_s) }}</td>
                  <td class="num">{{ r.ttft_s == null ? "–" : secs(r.ttft_s) }}</td>
                  <td class="num">{{ r.est_tokens ? kfmt(r.est_tokens) : "–" }}</td>
                  <td class="num">{{ fmt(r.tok_s, 1) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <Pager :page="concPage" :per-page="PAGE" :total="rows.length" @update:page="concPage = $event" />
        </div>

        <!-- 网关自身的状态 -->
        <div class="st-card conc-proxy">
          <div class="req-head"><span class="card-title">{{ t("conc.proxy") }}</span>
            <span class="muted small">{{ t("conc.proxy.uptime") }} {{ uptime(proxyInfo.uptime_s) }}</span></div>
          <div class="conc-nums conc-nums--proxy">
            <div><span>{{ t("conc.proxy.sessions") }}</span><b>{{ proxyInfo.sessions == null ? "–" : fmt(proxyInfo.sessions) }}</b></div>
            <div><span>{{ t("conc.proxy.embed") }}</span><b>{{ proxyInfo.embed_cache == null ? "–" : fmt(proxyInfo.embed_cache) }}</b></div>
            <div><span>{{ t("conc.proxy.queue") }}</span><b>{{ proxyInfo.embed_queue == null ? "–" : fmt(proxyInfo.embed_queue) }}</b></div>
            <div><span>{{ t("conc.ctr.passthrough") }}</span><b>{{ fmt((proxyInfo.counters || {}).passthrough) }}</b></div>
            <div><span>{{ t("conc.ctr.assembled") }}</span><b>{{ fmt((proxyInfo.counters || {}).assembled) }}</b></div>
            <div><span>{{ t("conc.ctr.degraded") }}</span><b>{{ fmt((proxyInfo.counters || {}).degraded) }}</b></div>
            <div><span>{{ t("conc.ctr.rebuilds") }}</span><b>{{ fmt((proxyInfo.counters || {}).block_rebuilds) }}</b></div>
            <div><span>{{ t("conc.ctr.retries") }}</span><b>{{ fmt((proxyInfo.counters || {}).overfit_retries) }}</b></div>
          </div>
        </div>
      </template>

      <!-- ===================== 单引擎仪表盘（原样保留） ===================== -->
      <div class="st-card state-card">
        <div class="state-card__head">
          <span class="card-title">{{ t("mon.model.state") }}<template v-if="metricsLaneLabel">
            <span class="muted small"> · {{ metricsLaneLabel }}</span></template></span>
          <span class="badges">
            <span class="st-badge" :class="{ on: stateOn === 'idle' }" data-s="idle">{{ t("mon.badge.idle") }}</span>
            <span class="st-badge st-badge--reading" :class="{ on: stateOn === 'reading' }" data-s="reading">{{ t("mon.badge.reading") }}</span>
            <span class="st-badge st-badge--generating" :class="{ on: stateOn === 'generating' }" data-s="generating">{{ t("mon.badge.generating") }}</span>
            <span class="st-badge st-badge--queued" :class="{ on: stateOn === 'queued' }" data-s="queued">{{ t("mon.badge.queued") }}</span>
            <span class="st-badge st-badge--error" :class="{ on: stateOn === 'error' }" data-s="error">{{ t("mon.badge.error") }}</span>
          </span>
        </div>
        <div class="state-card__row"><span>{{ stateLabel }}</span><span class="muted">{{ stateDetail }}</span></div>
        <div class="st-progress" :data-tone="stateTone || null"><div class="st-progress__bar" :style="{ width: statePct + '%' }"></div></div>
      </div>

      <div class="metrics">
        <div v-for="c in cards" :key="c.key" class="st-card metric-card">
          <div class="st-metric">
            <span class="st-metric__label"><Icon :name="c.icon" cls="st-icon st-icon--sm" />{{ c.label }}</span>
            <template v-if="c.dual">
              <div class="speed-values">
                <div><span class="st-metric__value">{{ c.value == null ? "–" : c.value }}</span>
                  <span class="st-metric__sub">{{ c.sub }}</span></div>
                <div class="speed-prefill"><span class="st-metric__value">{{ c.dual.value == null ? "–" : c.dual.value }}</span>
                  <span class="st-metric__sub">{{ c.dual.sub }}</span></div>
              </div>
            </template>
            <template v-else>
              <span class="st-metric__value">{{ c.value == null ? "–" : c.value }}<small v-if="c.unit">{{ c.unit }}</small></span>
              <span class="st-metric__sub">{{ c.sub }}</span>
            </template>
            <SparkLine :values="c.series" :max="c.max || 0" :tone="c.tone || ''" />
          </div>
        </div>
      </div>

      <!-- ===================== GPU 详细数据：一张卡一组 =====================
           这里原来是「GPU 负载 / 显存 / 温度 / 功耗 / PCIe」五张指标卡 —— 它们读的是 /metrics，
           被网关的会话粘性钉在一条通道上，于是永远只有**一张**卡的数，还不署名是哪张。
           现在一张卡一组，各带自己的曲线（数据源 /concurrency 的 lanes[].gpus；卡号由引擎上报）。 -->
      <template v-if="hasGpu">
        <div class="st-card conc-head gpu-head">
          <div>
            <span class="card-title">{{ t("mon.gpudetail.title") }}</span>
            <span class="muted small conc-head__sub">{{ t("mon.gpudetail.sub") }}</span>
          </div>
          <span class="muted small">{{ t("mon.gpudetail.n", { n: gpuCards.length }) }}</span>
        </div>
        <div class="gpu-cards">
          <div v-for="g in gpuCards" :key="g.key" class="st-card gpu-card" :data-offline="g.offline ? '1' : null">
            <div class="gpu-card__head">
              <span class="gpu-card__swatch" :style="{ background: g.color }"></span>
              <span class="gpu-card__name">{{ g.title }}</span>
              <span v-if="g.badge" class="st-badge" :class="g.badge.cls">{{ g.badge.text }}</span>
              <span class="gpu-card__model muted small" :title="g.name">{{ g.name || "–" }}</span>
            </div>
            <p class="gpu-card__who muted small">
              <template v-if="g.lane">{{ t("mon.gpudetail.lane", { lane: g.lane }) }}</template>
              <template v-else>{{ t("mon.gpudetail.self") }}</template>
              <template v-if="g.index == null"> · {{ t("mon.gpu.noname.why") }}</template>
            </p>
            <div v-for="m in g.metrics" :key="m.key" class="gpu-line" :data-s="m.key">
              <span class="gpu-line__label">{{ m.label }}</span>
              <span class="gpu-line__value">{{ m.value }}<small v-if="m.unit">{{ m.unit }}</small>
                <em v-if="m.sub" class="gpu-line__sub muted">{{ m.sub }}</em></span>
              <SparkLine :values="m.series" :max="m.max || 0" :tone="m.tone || ''" cls="gpu-line__spark" />
            </div>
          </div>
        </div>
      </template>

      <!-- 上下文占用：**一条道一张卡**，各带自己的仪表。
           原来是一张卡里塞两条 + 一个只指向其中一条的仪表，看着就像「只有一条」。 -->
      <div v-if="conc && ctxLaneRows.length" class="ctx-row">
        <div v-for="r in ctxLaneRows" :key="r.url" class="st-card ctx-card" :data-offline="r.offline ? '1' : null">
          <div class="ctx-card__head">
            <span class="ctx-card__swatch" :style="{ background: r.color }"></span>
            <span class="card-title">{{ r.label }}</span>
            <span class="muted small">{{ r.cardIndex == null ? t("ctx.card.unknown") : `GPU ${r.cardIndex}` }}</span>
          </div>
          <div class="ctx-gauge">
            <svg class="st-gauge" viewBox="0 0 120 120" aria-hidden="true">
              <circle class="st-gauge__track" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round"
                      stroke-dasharray="235.6 314.2" transform="rotate(135 60 60)" />
              <circle class="st-gauge__fill" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round"
                      :stroke-dasharray="`${(235.6 * (r.pct || 0)).toFixed(1)} 314.2`"
                      :style="{ opacity: 235.6 * (r.pct || 0) >= 3 ? 1 : 0 }" transform="rotate(135 60 60)" />
            </svg>
            <div class="ctx-gauge__label">
              <span class="ctx-gauge__pct">{{ r.pct == null ? "–" : Math.round(r.pct * 100) + "%" }}</span>
              <span class="muted">{{ r.total ? `${kfmt(r.used)} / ${ctxfmt(r.total)}` : "–" }}</span>
            </div>
          </div>
          <div class="bar-row"><span>{{ t("ctx.experts") }}</span>
            <span class="muted">{{ r.expertMib ? `${gb(r.expertMib * 1048576)} / ${r.capGb ? fmt(r.capGb, 0) : "–"} GB` : "–" }}</span></div>
          <div class="st-progress"><div class="st-progress__bar"
            :style="{ width: (r.capGb ? Math.min(100, (100 * r.expertMib) / r.capGb) : 0) + '%' }"></div></div>
        </div>
        <div class="st-card ctx-card">
          <span class="card-title">{{ t("ctx.system") }}</span>
          <div class="bar-row"><span>{{ t("ctx.ram") }}</span>
            <span class="muted">{{ hw.ram_total ? `${gb(hw.ram_used)} / ${gb(hw.ram_total, 0)} GB` : "–" }}</span></div>
          <div class="st-progress" :data-tone="ramPct > 92 ? 'danger' : null"><div class="st-progress__bar" :style="{ width: ramPct + '%' }"></div></div>
          <div class="bar-row"><span>{{ t("ctx.temp") }}</span>
            <span class="muted">{{ cardTemps.length ? cardTemps.map((x) => `${x.short} ${fmt(x.temp)} °C`).join(" · ") : "–" }}</span></div>
          <div class="st-progress" data-tone="warn"><div class="st-progress__bar" :style="{ width: tempPct + '%' }"></div></div>
        </div>
      </div>

      <div v-if="!conc" class="monitor-row">
        <div class="st-card ctx-card">
          <span class="card-title">{{ t("ctx.title") }}<template v-if="ctxSource">
            <span class="muted small"> · {{ ctxSource }}</span></template></span>
          <div class="ctx-gauge">
            <svg class="st-gauge" viewBox="0 0 120 120" aria-hidden="true">
              <circle class="st-gauge__track" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round"
                      stroke-dasharray="235.6 314.2" transform="rotate(135 60 60)" />
              <circle class="st-gauge__fill" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round"
                      :stroke-dasharray="`${(235.6 * ctxGauge.frac).toFixed(1)} 314.2`" :style="{ opacity: 235.6 * ctxGauge.frac >= 3 ? 1 : 0 }"
                      transform="rotate(135 60 60)" />
            </svg>
            <div class="ctx-gauge__label"><span class="ctx-gauge__pct">{{ Math.round(ctxGauge.frac * 100) }}%</span>
              <span class="muted">{{ ctxGauge.total ? `${kfmt(ctxGauge.used)} / ${ctxfmt(ctxGauge.total)}` : "–" }}</span></div>
          </div>
          <!-- 一条道一行：各道自己上报的 live / engine ⇒ 快道普道都在，不必只知道粘性那一条 -->
          <div v-if="ctxLaneRows.length > 1" class="ctx-lanes">
            <div v-for="r in ctxLaneRows" :key="r.url" class="ctx-lane" :data-offline="r.offline ? '1' : null">
              <div class="ctx-lane__top">
                <span class="ctx-lane__swatch" :style="{ background: r.color }"></span>
                <span class="ctx-lane__name">{{ r.label }}</span>
                <span class="ctx-lane__val muted small">{{ r.total ? `${kfmt(r.used)} / ${ctxfmt(r.total)}`
                  : (r.used ? `${kfmt(r.used)} · ${t("ctx.window.unknown")}` : "–") }}</span>
                <span class="ctx-lane__pct">{{ r.pct == null ? "–" : Math.round(r.pct * 100) + "%" }}</span>
              </div>
              <div class="st-progress"><div class="st-progress__bar" :style="{ width: (r.pct || 0) * 100 + '%' }"></div></div>
            </div>
          </div>
          <div class="bar-row"><span>{{ t("ctx.experts") }}</span>
            <span class="muted">{{ ctxExpertRows.length
              ? ctxExpertRows.map((r) => `${r.label} ${gb(r.expertMib * 1048576)}${r.capGb ? ` / ${fmt(r.capGb, 0)}` : ""} GB`).join(" · ")
              : (eng.expert_slots ? `${fmt(eng.expert_slots)} · ${gb(expertBytes)} GB${ctxCapGb ? ` / ${fmt(ctxCapGb, 0)} GB` : ""}` : "–") }}</span></div>
          <!-- 分母是**各道自己那张卡的容量**（原来用粘性那条道的 gpu_mem_total，两张卡时看着像是全机的） -->
          <div class="st-progress"><div class="st-progress__bar" :style="{ width: expertMaxPct + '%' }"></div></div>
          <div class="bar-row"><span>{{ t("ctx.ram") }}</span>
            <span class="muted">{{ hw.ram_total ? `${gb(hw.ram_used)} / ${gb(hw.ram_total, 0)} GB` : "–" }}</span></div>
          <div class="st-progress" :data-tone="ramPct > 92 ? 'danger' : null"><div class="st-progress__bar" :style="{ width: ramPct + '%' }"></div></div>
          <div class="bar-row"><span>{{ t("ctx.temp") }}</span>
            <span class="muted">{{ cardTemps.length ? cardTemps.map((x) => `${x.short} ${fmt(x.temp)} °C`).join(" · ") : "–" }}</span></div>
          <div class="st-progress" data-tone="warn"><div class="st-progress__bar" :style="{ width: tempPct + '%' }"></div></div>
        </div>

        <!-- 有并发数据时请求表在上面（跨两条通道）；没有时保留引擎自己这张 -->
        <div v-if="!conc" class="st-card req-card">
          <div class="req-head"><span class="card-title">{{ t("req.title") }}</span>
            <button v-if="kept > 12" class="st-btn st-btn--secondary" @click="app.reqShowAll = !app.reqShowAll">
              {{ app.reqShowAll ? t("req.showfewer") : t("common.loadmore", { n: kept }) }}</button></div>
          <div class="table-wrap" :class="{ all: app.reqShowAll }">
            <table class="st-table">
              <thead><tr><th>{{ t("req.th.time") }}</th><th>{{ t("req.th.status") }}</th><th class="num">{{ t("req.th.prompt") }}</th>
                <th class="num">{{ t("req.th.reused") }}</th><th class="num">{{ t("req.th.output") }}</th>
                <th class="num">{{ t("req.th.speed") }}</th><th class="num" :title="t('req.hit.title')">{{ t("req.th.hit") }}</th>
                <th class="num">{{ t("req.th.duration") }}</th></tr></thead>
              <tbody>
                <tr v-if="!reqSlice.length"><td colspan="8" class="muted">{{ t("req.none") }}</td></tr>
                <tr v-for="(r, i) in reqSlice" :key="i">
                  <td>{{ reqTime(r) }}</td>
                  <td><span class="st-badge" :class="finishOf(r.finish)[0]">{{ finishOf(r.finish)[1] ? t(finishOf(r.finish)[1]) : r.finish }}</span>
                    <span v-if="r.projection != null" class="st-badge" :class="{ 'st-badge--reading': r.projection }"
                          :title="r.projection ? t('req.esp.on') : t('req.esp.off')">{{ r.projection ? t("req.esp") : t("req.stock") }}</span></td>
                  <td class="num">{{ fmt(r.prompt_tokens) }}</td>
                  <td class="num">{{ fmt(r.reused) }}</td>
                  <td class="num">{{ fmt(r.output_tokens) }}</td>
                  <td class="num">{{ fmt(r.decode_tok_s, 1) }}</td>
                  <td class="num">{{ hitOf(r) }}</td>
                  <td class="num">{{ fmt(r.duration_s, 1) }} s</td>
                </tr>
              </tbody>
            </table>
          </div>
          <Pager :page="reqPage" :per-page="REQ_PER" :total="requests.length" @update:page="reqPage = $event" />
          <p class="muted req-totals">{{ totalsText }}</p>
        </div>
      </div>

      <div v-if="mcpServers.length" class="st-card mcp-card">
        <div class="req-head"><span class="card-title">{{ t("mcp.title") }}</span>
          <span class="muted small">{{ t("mcp.connected", { tools: fmt(app.mcp.tools), ready: mcpReady.length, all: mcpServers.length }) }}</span></div>
        <div class="mcp-list">
          <div v-for="s in mcpServers" :key="s.name" class="mcp-server">
            <div class="mcp-server__head">
              <span class="st-badge" :class="(MCP_STATE[s.status] || ['', ''])[0]">{{ MCP_STATE[s.status] ? t(MCP_STATE[s.status][1]) : s.status }}</span>
              <strong>{{ s.name }}</strong>
              <span class="muted small">{{ s.transport }} · {{ t("mcp.tools", { n: fmt(s.tools.length) }) }}<template
                v-if="s.info && s.info.name"> · {{ s.info.name }}<template v-if="s.info.version"> {{ s.info.version }}</template></template></span>
            </div>
            <div v-if="s.error" class="msg-error">{{ s.error }}</div>
            <div v-if="s.tools.length" class="mcp-server__tools">
              <span v-for="tool in s.tools" :key="tool.tool" class="chip" :title="tool.description || ''">{{ tool.tool }}</span>
            </div>
          </div>
        </div>
        <p class="muted small mcp-note">{{ t("mcp.note") }}</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.conc-head { display: flex; align-items: center; justify-content: space-between; gap: var(--st-s-4); }
.conc-head__sub { margin-left: var(--st-s-3); }
.conc-nums { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--st-s-3); margin: var(--st-s-3) 0; }
.conc-nums > div { background: var(--st-bg); border-radius: 8px; padding: 7px 10px; }
.conc-nums span { display: block; font-size: var(--st-fs-xs); color: var(--st-ink-muted); }
.conc-nums b { font-weight: 500; font-variant-numeric: tabular-nums; }
.conc-nums--proxy { grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); }
.conc-sum { padding-bottom: var(--st-s-2); }
.conc-sum .conc-nums { margin-bottom: 0; }

/* 通道图例：一行一条通道，色块与折线颜色一一对应。
   它同时承担「图例」和「通道状态」两个职责 —— 折线图右侧不重复标名字，省得挤在一起。 */
.lane-key { list-style: none; margin: 0; padding: 0; }
.lane-key__row { display: flex; align-items: center; gap: var(--st-s-3); padding: 7px 0;
                 border-bottom: 1px solid var(--st-line-soft); }
.lane-key__row:last-child { border-bottom: 0; }
.lane-key__swatch { flex: none; width: 12px; height: 3px; border-radius: 2px; }
.lane-key__name { min-width: 56px; font-size: var(--st-fs-sm); font-weight: var(--st-fw-medium); color: var(--st-ink); }
.lane-key__model { flex: 0 1 220px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lane-key__spacer { flex: 1; }
.lane-key__now { font-size: var(--st-fs-xs); color: var(--st-ink-muted); white-space: nowrap; }
.lane-key__now b { color: var(--st-ink); font-variant-numeric: tabular-nums; }
.lane-key__meta { white-space: nowrap; font-variant-numeric: tabular-nums; }

/* 两张趋势图并排：速度和在途是两套量纲，各自一根 y 轴 —— 绝不叠成双轴 */
.conc-charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: var(--st-s-4); }
.chart-card__wait { margin: var(--st-s-6) 0; text-align: center; }

/* 逐卡 GPU：一条通道一张卡。`.gpu-lane` 是 grid 子项，**必须 min-width:0** ——
   默认 min-width:auto 不肯收到内容以下，卡型名那一长串就能把整页撑宽（同一个坑在 .view/.records/.viz 都踩过）。 */
.gpu-lanes { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: var(--st-s-4); }
.gpu-lane { display: flex; flex-direction: column; gap: var(--st-s-3); min-width: 0; padding: var(--st-s-4) var(--st-s-5); }
.gpu-lane__head { display: flex; align-items: center; gap: var(--st-s-2); min-width: 0; }
.gpu-lane__swatch { flex: none; width: 12px; height: 3px; border-radius: 2px; }
.gpu-lane__name { flex: none; font-size: var(--st-fs-sm); font-weight: var(--st-fw-medium); color: var(--st-ink); }
.gpu-lane__lane { flex: none; }                       /* 用起来的通道名（快道 / 普道） */
.gpu-lane__model { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gpu-lane__metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--st-s-2); }
.gpu-metric { background: var(--st-bg); border-radius: 8px; padding: 7px 9px; min-width: 0; }
.gpu-metric__label { display: block; font-size: var(--st-fs-xs); color: var(--st-ink-muted);
                     overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gpu-metric__value { display: block; font-variant-numeric: tabular-nums; }
.gpu-metric__value small { font-size: var(--st-fs-xs); font-weight: 400; color: var(--st-ink-muted); margin-left: 3px; }
/* 掉线的通道读数会停在最后一次成功的那份：整卡压暗 + 靠徽章说话，别让人当现值读 */
.gpu-lane[data-offline] .gpu-metric__value { color: var(--st-ink-muted); }

/* GPU 详细数据：**一张卡一组**（这里原来是五张读 /metrics 的指标卡 = 只有一张卡的数、还没署名）。
   一根行 = 标签 + 读数 + 该卡该指标的曲线。同样必须 min-width:0，否则卡型名会把整页撑宽。 */
.gpu-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: var(--st-s-4); }
.gpu-card { display: flex; flex-direction: column; gap: var(--st-s-2); min-width: 0; padding: var(--st-s-4) var(--st-s-5); }
.gpu-card__head { display: flex; align-items: center; gap: var(--st-s-2); min-width: 0; }
.gpu-card__swatch { flex: none; width: 12px; height: 3px; border-radius: 2px; }
.gpu-card__name { flex: none; font-size: var(--st-fs-sm); font-weight: var(--st-fw-medium); color: var(--st-ink); }
.gpu-card__model { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gpu-card__who { margin: 0; }
.gpu-line { display: grid; grid-template-columns: 68px auto minmax(0, 1fr); align-items: center; gap: var(--st-s-3);
            border-top: 1px solid var(--st-line-soft); padding: 6px 0; min-width: 0; }
.gpu-line__label { font-size: var(--st-fs-xs); color: var(--st-ink-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gpu-line__value { font-variant-numeric: tabular-nums; white-space: nowrap; }
.gpu-line__value small { font-size: var(--st-fs-xs); font-weight: 400; color: var(--st-ink-muted); margin-left: 3px; }
/* 上限 / 「idle Gen1」这类附注挤在读数下面一行，别去抢曲线的宽度 */
.gpu-line__sub { display: block; font-style: normal; font-size: var(--st-fs-xs); color: var(--st-ink-muted);
                 white-space: normal; }
.gpu-line__spark { width: 100%; height: 22px; color: var(--st-chart-1); }
.gpu-card[data-offline] .gpu-line__value { color: var(--st-ink-muted); }
/* 逐卡四张图：**固定两列**（2×2），不用 auto-fit —— 1440 下 auto-fit 会排成 3+1，
   最后一张孤零零占一行，宽度还和上面那两张对不齐（截图实测）。窄屏再退回一列。 */
.gpu-charts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--st-s-4); }
.gpu-note { margin: 0; }
/* 上下文占用**一条道一行**：色块与折线同色，掉线的那条压暗（别把上一次的数当现值读） */
.ctx-lanes { display: flex; flex-direction: column; gap: var(--st-s-2); margin: var(--st-s-2) 0 var(--st-s-3); }
.ctx-lane__top { display: flex; align-items: center; gap: var(--st-s-2); min-width: 0; }
.ctx-lane__swatch { flex: none; width: 12px; height: 3px; border-radius: 2px; }
.ctx-lane__name { flex: none; font-size: var(--st-fs-sm); color: var(--st-ink); }
.ctx-lane__val { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctx-lane__pct { flex: none; font-variant-numeric: tabular-nums; }
.ctx-lane[data-offline] .ctx-lane__pct, .ctx-lane[data-offline] .ctx-lane__val { color: var(--st-ink-muted); }

@media (max-width: 900px) {
  /* 槽位占用 / GPU 利用率是这条通道「忙不忙」的关键数字，**不藏**，让它换行排到第二行去。
     （原来在这里直接 display:none 掉，手机上就只剩一个速率了。） */
  .lane-key__row { flex-wrap: wrap; row-gap: 2px; }
  .lane-key__model { flex-basis: 120px; }
  /* 900 以下两列会把每张图压到 400px 以内（y 轴刻度 + 三个时间戳就吃掉一半）→ 改一列 */
  .gpu-charts { grid-template-columns: minmax(0, 1fr); }
}
/* 手机上：四格汇总数字改两列（390px 下四格每格只剩 80px，数字都挤在一起）；
   通道图例那一行也让它换行排，别把模型名压没。 */
@media (max-width: 720px) {
  .conc-nums { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .lane-key__row { flex-wrap: wrap; row-gap: 2px; }
  .lane-key__model { flex: 1 1 100%; order: 9; padding-left: 22px; }
  .lane-key__spacer { display: none; }
  /* 逐卡四格在 390px 下每格只剩 ~80px（"23.0 / 24 GB" 会顶出去）→ 改两列，让每格有 150px+ */
  .gpu-lane__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
/* 四张卡并排时一格只剩 ~70px：四格改两列，并且数字不换行 */
.gpu-lane__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.gpu-metric__value { white-space: nowrap; }
.gpu-metric__value small { white-space: nowrap; }

/* ---- 全部 AI 服务（一服务一卡） ---- */
/* 上下文占用：一条道一张卡 + 系统一张，铺满整行 */
.ctx-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: var(--st-s-3); }
.ctx-card { display: grid; gap: 10px; padding: var(--st-s-4); align-content: start; }
.ctx-card__head { display: flex; align-items: center; gap: 8px; }
.ctx-card__swatch { width: 10px; height: 10px; border-radius: 3px; flex: none; }
.ctx-card[data-offline] { opacity: .6; }
.ctx-card .ctx-gauge { margin: 4px auto 0; }

.svc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: var(--st-s-3); }
.svc-card { display: grid; gap: 8px; padding: var(--st-s-4); border: 1px solid var(--st-line); }
.svc-card[data-up="0"] { opacity: .78; }
.svc-card__head { display: flex; align-items: center; gap: 8px; }
.svc-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--st-danger, #dc2626); flex: none; }
.svc-dot[data-up="1"] { background: var(--st-success, #16a34a); }
.svc-card__name { font-weight: var(--st-fw-bold); color: var(--st-ink); }
.svc-card__group { margin-left: auto; }
.svc-card__nums { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }
.svc-card__nums > div { display: grid; gap: 2px; }
.svc-card__nums b { font-variant-numeric: tabular-nums; font-size: var(--st-fs-md); color: var(--st-ink); }
.svc-card__nums b small { font-size: var(--st-fs-xs); color: var(--st-ink-muted); margin-left: 1px; }
.svc-card__nums span { font-size: var(--st-fs-xs); color: var(--st-ink-muted); }
.svc-card__foot { display: flex; gap: 8px; flex-wrap: wrap; min-height: 16px; }
.svc-card__err { color: var(--st-danger, #dc2626); }

@media (max-width: 720px) {
  /* 手机上每张卡一整行：服务名和时间都放得下，也不会出现横向滚动 */
  .ctx-row { grid-template-columns: 1fr; }
  .svc-grid { grid-template-columns: 1fr; gap: 10px; }
  .svc-card { padding: 14px; }
  .gpu-lanes { grid-template-columns: 1fr; }
}

@media (max-width: 560px) {
  .svc-card__nums { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
