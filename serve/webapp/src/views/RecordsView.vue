<script setup>
// 记录页：**推理日志的元信息**——每一条是谁、什么时候、走哪条通道、读/缓存/写了多少 token、
// 多快、TTFT 多少、成功还是失败。
//
// 页面上**不显示**用户输入、模型思考与回复；那些内容照样存着（对象存储为主、本机分段做缓存），
// 需要时由代理侧的口子取（/infer/raw，只认登录会话），留给事后错误分析。
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { rec, loadRecords, loadStats, loadItem, loadRag } from "../records.js";
import { fmt, kfmt } from "../api.js";
import { auth, openLogin } from "../auth.js";
import Icon from "../components/Icon.vue";
import Pager from "../components/Pager.vue";
import SparkLine from "../components/SparkLine.vue";

const { t } = useI18n();

const PAGE = 20;
const page = ref(1);
const days = ref(7);
const auto = ref(true);
const f = ref({ model: "", state: "", mode: "", stream: "", q: "" });
const sel = ref(null);              // 点开的那条（元信息详情）
const selLoading = ref(false);

const MODES = ["est", "exact", "assembled", "degraded", "direct"];
const STATES = ["completed", "error", "disconnected"];
const modelOpts = computed(() => (rec.stats?.by_model || []).map((m) => m.model).filter(Boolean));
const hourly = computed(() => (rec.stats?.hourly || []).map((h) => h.n));

// 每 5 秒刷一次列表（只刷第一页，别把正在翻页的人拽回去）；统计 30 秒一次。
let timer = null;
let ticks = 0;
function tick() {
  if (!auto.value || auth.showLogin) return;
  if (page.value === 1) loadRecords(query());
  if (++ticks % 6 === 0) loadStats(days.value);
}

onMounted(() => {
  loadStats(days.value);
  loadRecords(query());
  timer = setInterval(tick, 5000);
});
onBeforeUnmount(() => clearInterval(timer));

function query() {
  return { ...f.value, limit: PAGE, offset: (page.value - 1) * PAGE };
}

// 有没有真的在筛（只有这时才给"清空"按钮）
const filtersOn = computed(() => days.value !== 7 || Object.values(f.value).some((v) => v !== ""));
function clearFilters() {
  f.value = { model: "", state: "", mode: "", stream: "", q: "" };
  days.value = 7;
}
function reload() {
  page.value = 1;
  loadRecords(query());
}
watch(f, reload, { deep: true });
watch(page, () => loadRecords(query()));
watch(days, (d) => loadStats(d));
watch(() => auth.user, () => { loadStats(days.value); loadRecords(query()); });

async function open(row) {
  sel.value = { ...row };           // 先用列表里那份（字段够看），拿到完整元信息再替换
  selLoading.value = true;
  const full = await loadItem(row.id);
  if (full && sel.value && sel.value.id === row.id) sel.value = { ...row, ...full };
  selLoading.value = false;
  // 组装过的才去拉"检索回来哪些块"（拉的是元信息，不含正文）
  if (row.mode === "assembled") {
    const r = await loadRag(row.id);
    if (r && sel.value && sel.value.id === row.id) sel.value = { ...sel.value, rag_detail: r.rag };
  }
}

const pad = (n) => String(n).padStart(2, "0");
function tsfmt(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
const ms = (v) => (v == null ? "–" : v >= 1000 ? `${fmt(v / 1000, 2)} s` : `${fmt(v, 0)} ms`);
const pct = (a, b) => (a && b ? `${Math.round((100 * a) / b)}%` : "–");

const STATE_BADGE = { completed: "st-badge--generating", error: "st-badge--error", disconnected: "st-badge--queued" };
const MODE_KEY = { est: "rec.mode.est", exact: "rec.mode.exact", assembled: "rec.mode.assembled",
                   degraded: "rec.mode.degraded", direct: "rec.mode.direct" };
const modeText = (m) => (MODES.includes(m) ? t(MODE_KEY[m]) : m || "–");
const stateText = (s) => (STATES.includes(s) ? t(`rec.state.${s}`) : s || "–");

// 详情面板要展示的字段（成对列出来，省得模板里写一长串同样的结构）
const detail = computed(() => {
  const r = sel.value;
  if (!r) return [];
  return [
    ["rec.f.id", r.id, "mono"],
    ["rec.f.time", `${r.iso || ""} (${tsfmt(r.ts)})`],
    ["rec.f.model", [`${r.model || "–"}`, r.lane_label ? `${r.lane_label} · ${r.lane || ""}` : r.lane]
      .filter(Boolean).join("  ")],
    ["rec.f.path", r.path, "mono"],
    ["rec.f.mode", modeText(r.mode)],
    ["rec.f.stream", r.stream == null ? "–" : r.stream ? t("rec.yes") : t("rec.no")],
    ["rec.f.state", `${stateText(r.state)}${r.http_status ? ` · HTTP ${r.http_status}` : ""}`],
    ["rec.f.finish", r.finish_reason || "–"],
    ["rec.f.tok.in", `${fmt(r.prompt_tokens)}${r.cached_tokens != null ? ` （${t("rec.f.cached")} ${fmt(r.cached_tokens)}，${pct(r.cached_tokens, r.prompt_tokens)}）` : ""}`],
    ["rec.f.tok.out", r.completion_tokens == null ? "–" : fmt(r.completion_tokens)],
    ["rec.f.tok.est", r.est_prompt_tokens == null ? "–" : fmt(r.est_prompt_tokens)],
    ["rec.f.tok.asm", r.memory_tokens == null ? "–" : `${t("rec.f.recent")} ${kfmt(r.recent_tokens)} ＋ ${t("rec.f.memory")} ${kfmt(r.memory_tokens)}`],
    ["rec.f.reuse", r.block_reused == null ? "–" : r.block_reused ? t("rec.yes") : t("rec.no")],
    ["rec.f.retrieve", ms(r.retrieve_ms)],
    ["rec.f.speed", `${fmt(r.decode_tok_s, 1)} tok/s`],
    ["rec.f.prefill", r.prompt_tok_s == null ? "–" : `${fmt(r.prompt_tok_s, 0)} tok/s`],
    ["rec.f.ttft", ms(r.ttft_ms)],
    ["rec.f.total", ms(r.total_ms)],
    ["rec.f.events", r.events == null ? "–" : `${fmt(r.events)} ${t("rec.f.events.unit")}`],
    ["rec.f.bytes", `${t("rec.f.bytes.in")} ${kfmt(r.client_bytes)} B → ${t("rec.f.bytes.sent")} ${kfmt(r.sent_bytes)} B → ${t("rec.f.bytes.out")} ${kfmt(r.bytes_out)} B`],
    ["rec.f.text", `${t("rec.f.text.body")} ${fmt(r.content_chars)} ${t("rec.f.text.chars")} · ${t("rec.f.text.think")} ${fmt(r.reasoning_chars)} ${t("rec.f.text.chars")} · ${t("rec.f.text.tools")} ${fmt(r.tool_calls)}`],
    ["rec.f.retries", fmt(r.retries)],
    ["rec.f.trunc", r.truncated ? t("rec.f.trunc.yes") : t("rec.no")],
    ["rec.f.session", r.session || "–", "mono"],
    ["rec.f.ua", r.ua || "–"],
    ["rec.f.storage", `${r.seg || "–"} #${r.line ?? "–"} · ${r.uploaded ? t("rec.f.stored") : t("rec.f.pending")}`],
  ].filter(([k]) => k);
});

// RAG 触发时的**前后对照**：客户端发来什么形状、引擎实际收到什么形状、检索回来哪些块。
// 全是计数与分数——正文（原输入/检索块/回复）照存不显示，见文档。
const rag = computed(() => {
  const r = sel.value;
  if (!r || r.mode !== "assembled") return null;
  return {
    reqBefore: `${fmt(r.client_msgs)} ${t("rec.f.msgs")} · ${kfmt(r.client_chars)} ${t("rec.f.text.chars")}`,
    reqAfter: `${fmt(r.sent_msgs)} ${t("rec.f.msgs")} · ${kfmt(r.sent_chars)} ${t("rec.f.text.chars")}`,
    differs: !!r.differs,
    assembled: true,
    reused: r.block_reused ? t("rec.yes") : t("rec.no"),
    rebuilt: r.rag_rebuild == null ? "–" : r.rag_rebuild ? t("rec.yes") : t("rec.no"),
    query: r.rag_query_chars == null ? "–" : `${kfmt(r.rag_query_chars)} ${t("rec.f.text.chars")}`,
    pool: r.rag_pool == null ? "–" : `${fmt(r.rag_pool)} → ${fmt(r.rag_candidates)}`,
    score: r.rag_score_max == null ? "–"
      : `${fmt(r.rag_score_min, 2)} ~ ${fmt(r.rag_score_max, 2)}`,
    block: r.rag_block_chars == null ? "–" : `${kfmt(r.rag_block_chars)} ${t("rec.f.text.chars")}`,
    chunks: fmt(r.rag_chunks),
    win: `${kfmt(r.recent_tokens)} + ${kfmt(r.memory_tokens)}`,
    rerank: r.rag_rerank == null ? "–" : r.rag_rerank ? t("rec.yes") : t("rec.no"),
  };
});

// 检索回来的每一块（**只看形状与分数**：哪一段历史、多长、三阶段各多少分）
const ragChunks = computed(() => {
  const d = sel.value && sel.value.rag_detail;          // = /infer/rag 返回的 rag 段
  return (d && (d.chosen_detail || {}).chunks) || [];
});
</script>

<template>
  <!-- 骨架跟监控页一致：外层 .view（吃掉 main 的 flex 空间）+ 内层定宽居中容器。
      **不能**只用一个 flex 子项当根 —— 子项默认 min-width:auto，宽表格会把它撑出视口（手机整页横向溢出）。 -->
  <section class="view view--records">
    <div class="records">
      <div class="rec-head">
      <div>
        <h2 class="rec-title">{{ t("rec.title") }}</h2>
        <p class="muted small rec-sub">{{ t("rec.sub") }}</p>
      </div>
      <div class="rec-head__ctl">
        <label class="rec-auto">
          <input type="checkbox" v-model="auto">{{ t("rec.auto") }}</label>
        <button class="st-btn st-btn--secondary" :disabled="rec.loading" @click="loadRecords(query()); loadStats(days)">
          <Icon name="refresh" cls="st-icon st-icon--sm" />{{ t("rec.refresh") }}</button>
      </div>
    </div>

    <!-- 未登录：元信息接口要凭据（登录会话或本机 token） -->
    <div v-if="rec.needLogin" class="st-card rec-empty">
      <Icon name="info" />
      <div>
        <div class="rec-empty__t">{{ t("rec.needLogin") }}</div>
        <div class="muted small">{{ t("rec.needLogin.hint") }}</div>
      </div>
      <button v-if="!auth.user" class="st-btn st-btn--secondary" @click="openLogin">{{ t("login.button") }}</button>
    </div>

    <template v-else>
      <!-- 概览 -->
      <div class="rec-stats">
        <div class="st-card rec-stat">
          <div class="rec-stat__k">{{ t("rec.k.total", { days }) }}</div>
          <div class="rec-stat__v">{{ fmt(rec.stats?.total) }}</div>
          <div class="muted small">{{ t("rec.k.today") }} {{ fmt(rec.stats?.today) }}</div>
        </div>
        <div class="st-card rec-stat">
          <div class="rec-stat__k">{{ t("rec.k.out") }}</div>
          <div class="rec-stat__v">{{ kfmt(rec.stats?.out_tokens) }}</div>
          <div class="muted small">{{ t("rec.k.in") }} {{ kfmt(rec.stats?.in_tokens) }}</div>
        </div>
        <div class="st-card rec-stat">
          <div class="rec-stat__k">{{ t("rec.k.speed") }}</div>
          <div class="rec-stat__v">{{ fmt(rec.stats?.avg_tok_s, 1) }}</div>
          <div class="muted small">{{ t("rec.k.ttft") }} {{ ms(rec.stats?.avg_ttft_ms) }}</div>
        </div>
        <div class="st-card rec-stat">
          <div class="rec-stat__k">{{ t("rec.k.trend") }}</div>
          <SparkLine :values="hourly" cls="rec-spark" />
          <div class="muted small">{{ t("rec.k.busy") }} {{ ms((rec.stats?.busy_s || 0) * 1000) }}</div>
        </div>
      </div>

      <!-- 筛选 -->
      <div class="st-card rec-filters">
        <label class="rec-f"><span class="rec-f__l">{{ t("rec.f.model") }}</span>
          <span class="rec-sel">
            <select v-model="f.model"><option value="">{{ t("rec.all") }}</option>
              <option v-for="m in modelOpts" :key="m" :value="m">{{ m }}</option></select>
            <Icon name="chevron" cls="st-icon st-icon--sm rec-sel__i" />
          </span></label>
        <label class="rec-f"><span class="rec-f__l">{{ t("rec.f.state") }}</span>
          <span class="rec-sel">
            <select v-model="f.state"><option value="">{{ t("rec.all") }}</option>
              <option v-for="s in STATES" :key="s" :value="s">{{ stateText(s) }}</option></select>
            <Icon name="chevron" cls="st-icon st-icon--sm rec-sel__i" />
          </span></label>
        <label class="rec-f"><span class="rec-f__l">{{ t("rec.f.mode") }}</span>
          <span class="rec-sel">
            <select v-model="f.mode"><option value="">{{ t("rec.all") }}</option>
              <option v-for="m in MODES" :key="m" :value="m">{{ modeText(m) }}</option></select>
            <Icon name="chevron" cls="st-icon st-icon--sm rec-sel__i" />
          </span></label>
        <label class="rec-f"><span class="rec-f__l">{{ t("rec.f.stream") }}</span>
          <span class="rec-sel">
            <select v-model="f.stream"><option value="">{{ t("rec.all") }}</option>
              <option value="1">{{ t("rec.stream.on") }}</option>
              <option value="0">{{ t("rec.stream.off") }}</option></select>
            <Icon name="chevron" cls="st-icon st-icon--sm rec-sel__i" />
          </span></label>
        <label class="rec-f"><span class="rec-f__l">{{ t("rec.f.range") }}</span>
          <span class="rec-sel">
            <select v-model.number="days">
              <option :value="1">24 h</option><option :value="7">7 d</option>
              <option :value="30">30 d</option><option :value="3650">∞</option></select>
            <Icon name="chevron" cls="st-icon st-icon--sm rec-sel__i" />
          </span></label>
        <label class="rec-f rec-f--grow"><span class="rec-f__l">{{ t("rec.f.q") }}</span>
          <span class="rec-input">
            <input v-model="f.q" type="search" :placeholder="t('rec.f.q.ph')">
          </span></label>
        <!-- 只有真的筛过才出现：一排下拉永远摆着"清空"会显得吵 -->
        <button v-if="filtersOn" class="st-btn st-btn--secondary rec-clear" @click="clearFilters">
          {{ t("rec.f.clear") }}</button>
      </div>

      <!-- 实时（内存里最新若干条，不查库） -->
      <div v-if="rec.live.length" class="st-card rec-live">
        <div class="rec-live__h"><span class="rec-dot"></span>{{ t("rec.live") }}
          <span class="muted small">{{ t("rec.live.hint") }}</span></div>
        <div class="rec-live__list">
          <button v-for="r in [...rec.live].reverse()" :key="r.id" class="rec-live__i" @click="open(r)">
            <span class="muted small">{{ tsfmt(r.ts) }}</span>
            <span class="mono small">{{ r.id.slice(-6) }}</span>
            <span class="muted small rec-ell">{{ r.model || "–" }}</span>
            <span class="small">{{ fmt(r.decode_tok_s, 1) }} tok/s</span>
            <span class="muted small">{{ kfmt(r.completion_tokens) }} tok</span>
          </button>
        </div>
      </div>

      <!-- 主表 -->
      <div class="st-card rec-table">
        <div class="table-wrap">
          <table class="st-table">
            <thead><tr>
              <th>{{ t("rec.th.time") }}</th><th>{{ t("rec.th.id") }}</th><th>{{ t("rec.th.model") }}</th>
              <th>{{ t("rec.th.mode") }}</th>
              <th class="num">{{ t("rec.th.in") }}</th><th class="num">{{ t("rec.th.cached") }}</th>
              <th class="num">{{ t("rec.th.out") }}</th><th class="num">{{ t("rec.th.rag") }}</th>
              <th class="num">{{ t("rec.th.speed") }}</th><th class="num">{{ t("rec.th.ttft") }}</th>
              <th class="num">{{ t("rec.th.total") }}</th><th>{{ t("rec.th.state") }}</th>
            </tr></thead>
            <tbody>
              <tr v-if="!rec.rows.length"><td colspan="12" class="muted">{{ t("rec.none") }}</td></tr>
              <tr v-for="r in rec.rows" :key="r.id" class="rec-row" @click="open(r)">
                <td class="muted small">{{ tsfmt(r.ts) }}</td>
                <td class="mono small">{{ r.id }}</td>
                <td class="muted small">{{ r.model || "–" }}</td>
                <td class="muted small">{{ modeText(r.mode) }}</td>
                <td class="num">{{ r.prompt_tokens == null ? "–" : fmt(r.prompt_tokens) }}</td>
                <td class="num muted">{{ r.cached_tokens == null ? "–" : kfmt(r.cached_tokens) }}</td>
                <td class="num">{{ r.completion_tokens == null ? "–" : fmt(r.completion_tokens) }}</td>
                <!-- 检索块：块数 · 最高分（没组装就是 –）。RAG 有没有真的在跑，看这一列最直接 -->
                <td class="num" :title="r.rag_pool == null ? '' : `${t('rec.f.pool')} ${r.rag_pool} → ${r.rag_candidates}`">
                  <template v-if="r.mode === 'assembled'">
                    {{ r.rag_chunks == null ? "0" : fmt(r.rag_chunks) }}<span v-if="r.rag_score_max != null"
                      class="muted"> · {{ fmt(r.rag_score_max, 2) }}</span>
                  </template><template v-else>–</template>
                </td>
                <td class="num">{{ fmt(r.decode_tok_s, 1) }}</td>
                <td class="num">{{ ms(r.ttft_ms) }}</td>
                <td class="num">{{ ms(r.total_ms) }}</td>
                <td><span class="st-badge" :class="STATE_BADGE[r.state] || ''">{{ stateText(r.state) }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <Pager :page="page" :per-page="PAGE" :total="rec.total" @update:page="page = $event" />
      </div>

      <p class="muted small rec-note"><Icon name="disk" cls="st-icon st-icon--sm" />{{ t("rec.privacy") }}</p>
    </template>

    <!-- 详情（只有元信息） -->
    <div v-if="sel" class="rec-scrim" @click="sel = null"></div>
    <aside v-if="sel" class="rec-detail" role="dialog" :aria-label="t('rec.detail')">
      <div class="rec-detail__h">
        <span class="card-title">{{ t("rec.detail") }}</span>
        <span v-if="selLoading" class="muted small">{{ t("rec.loading") }}</span>
        <button class="st-btn st-btn--icon" :aria-label="t('rec.close')" @click="sel = null">
          <Icon name="chevron" cls="st-icon st-icon--sm rec-x" /></button>
      </div>
      <dl class="rec-dl">
        <template v-for="([k, v, cls]) in detail" :key="k">
          <dt>{{ t(k) }}</dt><dd :class="cls">{{ v }}</dd>
        </template>
      </dl>

      <!-- RAG 前后对照：客户端发来的形状 → 引擎实际收到的形状 → 检索回来哪些块 -->
      <template v-if="rag">
        <div class="rec-sec">{{ t("rec.rag.title") }}</div>
        <dl class="rec-dl">
          <dt>{{ t("rec.rag.before") }}</dt><dd>{{ rag.reqBefore }}</dd>
          <dt>{{ t("rec.rag.after") }}</dt><dd>{{ rag.reqAfter }}
            <span v-if="rag.differs" class="st-badge st-badge--generating">{{ t("rec.rag.rewritten") }}</span></dd>
          <dt>{{ t("rec.rag.query") }}</dt><dd>{{ rag.query }}</dd>
          <dt>{{ t("rec.rag.pool") }}</dt><dd>{{ rag.pool }}</dd>
          <dt>{{ t("rec.rag.chunks") }}</dt><dd>{{ rag.chunks }} · {{ t("rec.rag.score") }} {{ rag.score }}</dd>
          <dt>{{ t("rec.rag.block") }}</dt><dd>{{ rag.block }}</dd>
          <dt>{{ t("rec.rag.win") }}</dt><dd>{{ rag.win }}</dd>
          <dt>{{ t("rec.rag.reused") }}</dt><dd>{{ rag.reused }} · {{ t("rec.rag.rebuilt") }} {{ rag.rebuilt }}
            · {{ t("rec.rag.rerank") }} {{ rag.rerank }}</dd>
        </dl>
        <div v-if="ragChunks.length" class="rec-sec">{{ t("rec.rag.list") }}
          <span class="muted small">{{ t("rec.rag.list.hint", { n: ragChunks.length }) }}</span></div>
        <ul v-if="ragChunks.length" class="rag-list">
          <li v-for="(c, i) in ragChunks" :key="i" class="rag-item">
            <span class="mono small">{{ c.turn_label || `#${c.turn}` }}</span>
            <span class="muted small">{{ c.role }}·{{ c.kind }}</span>
            <span class="small">{{ kfmt(c.tokens) }} tok</span>
            <span class="muted small">{{ kfmt(c.chars) }} {{ t("rec.f.text.chars") }}</span>
            <span class="rag-scores small">
              <template v-if="c.score != null">score {{ fmt(c.score, 3) }}</template>
              <template v-else>{{ t("rec.rag.expanded") }}</template>
              <span v-if="c.rerank != null" class="muted"> · rerank {{ fmt(c.rerank, 3) }}</span>
              <span v-if="c.cos != null" class="muted"> · cos {{ fmt(c.cos, 3) }}</span>
            </span>
          </li>
        </ul>
      </template>

      <p class="muted small">{{ t("rec.privacy") }}</p>
    </aside>
    </div>
  </section>
</template>

<style scoped>
.view--records { overflow-y: auto; }
.records { max-width: 1240px; width: 100%; margin: 0 auto; padding: var(--st-s-6);
           display: flex; flex-direction: column; gap: var(--st-s-3); }
/* flex 子项默认 min-width:auto —— 表格一宽就把卡片撑出视口（手机上整页横向溢出）。
   这里压住卡片、让 .table-wrap 自己去横向滚动（app.css 里它本来就是 overflow-x:auto）。 */
.records > * { min-width: 0; max-width: 100%; }
.rec-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--st-s-3); flex-wrap: wrap; }
.rec-title { margin: 0; font-size: var(--st-fs-lg); }
.rec-sub { margin: 4px 0 0; max-width: 62ch; }
.rec-head__ctl { display: inline-flex; align-items: center; gap: var(--st-s-3); }
.rec-auto { display: inline-flex; align-items: center; gap: 6px; font-size: var(--st-fs-sm); color: var(--st-ink-soft); }

.rec-empty { display: flex; align-items: center; gap: var(--st-s-3); }
.rec-empty__t { font-weight: var(--st-fw-bold); }

.rec-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--st-s-3); }
.rec-stat { padding: var(--st-s-3); }
.rec-stat__k { font-size: var(--st-fs-sm); color: var(--st-ink-soft); }
.rec-stat__v { font-size: 24px; font-weight: var(--st-fw-bold); font-variant-numeric: tabular-nums; }
.rec-spark { width: 100%; height: 26px; color: var(--st-accent); }

.rec-filters { display: flex; flex-wrap: wrap; align-items: flex-end; gap: var(--st-s-4); }
.rec-f { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.rec-f__l { font-size: var(--st-fs-xs); font-weight: 600; letter-spacing: .04em;
            text-transform: uppercase; color: var(--st-ink-muted); }
.rec-f--grow { flex: 1 1 180px; }

/* 下拉框：**不要**浏览器原生控件那副样子（方角 + 原生箭头 + 那道生硬的蓝描边）。
   做法与可视化控制条一致：外面这层自己画边框/圆角/聚焦环，里面那个原生 select 透明化，
   箭头换成我们自己的 chevron —— 于是 hover / focus / 深色主题全部跟着令牌走。 */
.rec-sel, .rec-input { position: relative; display: inline-flex; align-items: center; height: 38px;
                       min-width: 0; border: 1px solid var(--st-line); border-radius: var(--st-r-md);
                       background: var(--st-surface); color: var(--st-ink);
                       transition: border-color .16s, box-shadow .16s; }
.rec-sel { min-width: 126px; }
.rec-sel select { flex: 1; min-width: 0; height: 100%; padding: 0 30px 0 12px; border: 0;
                  appearance: none; background: transparent; color: inherit; cursor: pointer;
                  font: 500 var(--st-fs-sm) var(--st-font); }
.rec-sel select:focus { outline: 0; }                      /* 描边交给外层的 focus-within 环 */
.rec-sel option { background: var(--st-surface); color: var(--st-ink); }
.rec-sel__i { position: absolute; right: 9px; pointer-events: none; color: var(--st-ink-muted);
              transition: transform .18s var(--st-ease), color .16s; }
.rec-input input { flex: 1; min-width: 0; height: 100%; padding: 0 12px; border: 0; background: transparent;
                   color: inherit; font: 400 var(--st-fs-sm) var(--st-font); }
.rec-input, .rec-input input { width: 100%; }
.rec-input input:focus { outline: 0; }
.rec-input input::placeholder { color: var(--st-ink-muted); }
.rec-input input::-webkit-search-cancel-button { cursor: pointer; }
/* 悬停：边框往主色偏一点，让人知道这是可点的 */
.rec-sel:hover, .rec-input:hover { border-color: color-mix(in srgb, var(--st-accent) 40%, var(--st-line)); }
.rec-sel:hover .rec-sel__i { color: var(--st-ink); }
/* 聚焦：柔和外环（与 .st-input 同一档），不是浏览器那圈硬蓝线 */
.rec-sel:focus-within, .rec-input:focus-within {
  border-color: var(--st-focus);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--st-focus) 18%, transparent); }
.rec-sel:focus-within .rec-sel__i { transform: rotate(180deg); color: var(--st-focus); }
/* 清空按钮跟着筛选行末尾走；宽度够就同一行（不留半行空），不够自然换行 */
.rec-clear { height: 38px; flex: 0 0 auto; margin-left: auto; white-space: nowrap; }

.rec-live__h { display: flex; align-items: center; gap: 8px; font-size: var(--st-fs-sm); }
.rec-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--st-accent); animation: rec-pulse 1.6s infinite; }
@keyframes rec-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
.rec-live__list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: var(--st-s-2); }
.rec-live__i { display: inline-flex; align-items: center; gap: 8px; padding: 5px 9px; border-radius: var(--st-r-sm);
               border: 1px solid var(--st-line-soft); background: var(--st-surface-2); cursor: pointer;
               font: inherit; font-variant-numeric: tabular-nums; }
.rec-live__i:hover { border-color: var(--st-accent); }
.rec-ell { max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.rec-table { padding-bottom: var(--st-s-2); }
.rec-row { cursor: pointer; }
.rec-row:hover td { background: var(--st-surface-2); }
.rec-note { display: flex; align-items: center; gap: 6px; }

.rec-scrim { position: fixed; inset: 0; background: rgba(15, 23, 42, .28); z-index: 60; }
.rec-detail { position: fixed; top: 0; right: 0; bottom: 0; width: min(560px, 92vw); z-index: 61;
              overflow: auto; padding: var(--st-s-4); background: var(--st-surface);
              border-left: 1px solid var(--st-line); box-shadow: var(--st-shadow-lg, 0 8px 32px rgba(0,0,0,.18)); }
.rec-detail__h { display: flex; align-items: center; justify-content: space-between; gap: var(--st-s-3); }
.rec-x { transform: rotate(-90deg); }
.rec-dl { display: grid; grid-template-columns: minmax(96px, 34%) 1fr; gap: 6px var(--st-s-3);
          margin: var(--st-s-3) 0; font-size: var(--st-fs-sm); }
.rec-dl dt { color: var(--st-ink-soft); }
.rec-dl dd { margin: 0; font-variant-numeric: tabular-nums; word-break: break-word; }
.rec-dl dd.mono { font-family: var(--st-font-mono, ui-monospace, monospace); }

/* RAG 前后对照 */
.rec-sec { margin: var(--st-s-4) 0 var(--st-s-2); font-weight: var(--st-fw-bold); font-size: var(--st-fs-sm);
           color: var(--st-ink-soft); border-top: 1px solid var(--st-line-soft); padding-top: var(--st-s-3); }
.rag-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px;
            max-height: 320px; overflow-y: auto; }
.rag-item { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 5px 8px;
            border: 1px solid var(--st-line-soft); border-radius: var(--st-r-sm); background: var(--st-surface-2);
            font-variant-numeric: tabular-nums; }
.rag-scores { margin-left: auto; }

@media (max-width: 900px) {
  .rec-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  /* 手机上两列排布：下拉自己撑满所在列，别把窄屏撑破 */
  .rec-f { flex: 1 1 44%; }
  .rec-sel { min-width: 0; width: 100%; }
  .rec-clear { margin-left: 0; flex: 1 1 100%; }
  .rec-detail { width: 100vw; }
}
</style>
