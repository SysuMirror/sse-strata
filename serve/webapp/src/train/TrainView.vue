<script setup>
// 训练工作台 = LLM Lab（代码编辑器）+ 训练监控，一个页面两种模式。
//
// 左上是「代码 / 监控」切换：
//   · 代码：完整的 LLM Lab（文件树 / 编辑器 / agent / 后台任务），训练时改代码、跑测试都在这里；
//   · 监控：把当前这次训练的 loss / lr / gnorm / tok·s⁻¹ 放大成一整页，训练时切过来盯着。
// 两种模式都保持挂载（v-show），切走再切回来不会丢编辑器里没保存的内容。
//
// 数据为什么走 /web/train-data.js 而不是接口：
//  · 引擎静态服务只发 /web/<扁平名> 的 .css/.js/.svg（serve/server.py 的 do_GET），JSON 发不出去；
//  · 加接口要动代理或引擎 = 要重启，而服务正对外在线（见 docs 红线）。
// 所以训练进程把全量指标原子重写成 `window.__TRAIN_METRICS__ = {...}`，本页用 <script> 注入轮询：
//  同源、无 CORS、不改一行服务端代码。刷新/换设备都不丢 —— 曲线的数据源是整个历史文件，
//  不是内存里的尾巴；权威档案（每步一行）落在训练机 checkpoints/<run>/metrics.jsonl。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { store } from "../api.js";
import Icon from "../components/Icon.vue";
import LineChart from "../components/LineChart.vue";
import WorkspaceView from "../views/WorkspaceView.vue";

const { t } = useI18n();

const SRC = "/web/train-data.js";
const EVERY_MS = 5000;
const STALE_S = 90;            // 超过 90 秒没更新 = 训练进程不在了（结束或被杀）

const d = ref(null);
const auto = ref(true);
const err = ref("");
const mode = ref(store.get("train.mode", "code") === "monitor" ? "monitor" : "code");
const jobsSignal = ref(0);
let timer = null;

function setMode(m) {
  mode.value = m === "monitor" ? "monitor" : "code";
  store.set("train.mode", mode.value);
}

/** 从监控模式直接开训练：先回代码模式（后台任务面板在 Lab 里），再让 Lab 弹出面板。 */
function startTraining() {
  setMode("code");
  nextTick(() => { jobsSignal.value += 1; });
}

// <script> 注入：每次带一个 ?t= 破缓存（静态服务按 path 找文件，查询串它自己会去掉）
function load() {
  const s = document.createElement("script");
  s.src = `${SRC}?t=${Date.now()}`;
  s.onload = () => {
    err.value = "";
    const m = window.__TRAIN_METRICS__;
    if (m) d.value = JSON.parse(JSON.stringify(m));    // 拷一份：全局对象会被下一次注入整体替换
    s.remove();
  };
  s.onerror = () => { err.value = "failed"; s.remove(); };
  document.head.appendChild(s);
}

onMounted(() => { load(); timer = setInterval(() => { if (auto.value) load(); }, EVERY_MS); });
onBeforeUnmount(() => clearInterval(timer));

// ---------------------------------------------------------------------- 状态
const age = computed(() => (d.value ? Math.max(0, Date.now() / 1000 - (d.value.updated_unix || 0)) : null));
const state = computed(() => {
  if (err.value === "failed") return { kind: "queued", text: t("train.state.missing") };
  if (!d.value) return { kind: "queued", text: t("train.state.wait") };
  if (d.value.finished) return { kind: "done", text: t("train.state.done") };
  if (age.value > STALE_S) return { kind: "stale", text: t("train.state.stale", { m: Math.floor(age.value / 60) }) };
  return { kind: "running", text: t("train.state.running") };
});
// 徽章只用 components.css 里实存的四个态（generating / reading / error / queued），
// 别自造 st-badge--done —— 没有这个类会退成灰底，看着像坏了。
const badge = computed(() => ({
  running: "st-badge--generating", done: "st-badge--reading",
  stale: "st-badge--error", missing: "st-badge--queued", wait: "st-badge--queued",
}[state.value.kind] || "st-badge--queued"));

const fmt4 = (v) => (v == null ? "–" : Number(v).toFixed(4));
const fmt2 = (v) => (v == null ? "–" : Number(v).toFixed(2));
const fmt1 = (v) => (v == null ? "–" : Number(v).toFixed(1));
const int = (v) => (v == null ? "–" : Math.round(Number(v)).toLocaleString());
const dur = (s) => (s == null ? "–" : `${Math.floor(s / 60)}m${String(Math.floor(s % 60)).padStart(2, "0")}s`);

const elapsed = computed(() => (d.value && d.value.started_unix ? Date.now() / 1000 - d.value.started_unix : null));
const pct = computed(() => (d.value ? Math.min(100, (d.value.step / (d.value.total_steps || 1)) * 100) : 0));

// ---------------------------------------------------------------------- 折线
// 每条序列各自一张图：train loss 与 val loss 虽然同量纲，但取样密度差 50 倍，
// 合成一张会让稀疏的那条断成渣（LineChart 缺测是真断开，不拿 0 顶）。分两张更诚实。
function seriesOf(rows, idx, label, color) {
  return [{ key: label, label, color, values: rows.map((r) => (r[idx] == null ? null : r[idx])) }];
}
const times = (rows, idx) => (rows.some((r) => r[idx] > 0) ? rows.map((r) => r[idx] * 1000) : []);

const steps = computed(() => (d.value?.steps || []));
const evals = computed(() => (d.value?.evals || []));
const sTrain = computed(() => seriesOf(steps.value, 1, t("train.s.train"), "var(--st-chart-1)"));
const sVal = computed(() => seriesOf(evals.value, 2, t("train.s.val"), "var(--st-chart-2)"));
const sLr = computed(() => seriesOf(steps.value, 2, t("train.s.lr"), "var(--st-chart-3)"));
const sGn = computed(() => seriesOf(steps.value, 3, t("train.s.gnorm"), "var(--st-chart-4)"));
const sTps = computed(() => seriesOf(steps.value, 4, t("train.s.tok"), "var(--st-chart-1)"));
const tSteps = computed(() => times(steps.value, 5));
const tEvals = computed(() => times(evals.value, 4));    // eval 行没有独立时间戳，x 轴留空

const cfgRows = computed(() => {
  const m = d.value?.meta || {};
  return [
    ["run", d.value?.run], ["step", `${d.value?.step ?? "–"} / ${d.value?.total_steps ?? "–"}`],
    ["epoch", `${fmt2(d.value?.epoch)} / ${fmt2(d.value?.total_epochs)}`],
    ["batch × block", `${m.batch_size ?? "?"} × ${m.block_size ?? "?"}${m.grad_accum > 1 ? ` ×${m.grad_accum}` : ""}`],
    ["峰值 lr", m.lr], ["warmup", m.warmup], ["seed", m.seed], ["dtype", m.dtype], ["device", m.device],
    ["参数", m.n_params ? `${(m.n_params / 1e6).toFixed(2)} M` : "?"],
    ["train token", d.value?.train_tokens ? d.value.train_tokens.toLocaleString() : "?"],
    ["每步 token", d.value?.tokens_per_step ? d.value.tokens_per_step.toLocaleString() : "?"],
    ["数据", m.data_dir], ["产物", d.value?.out_dir],
  ].filter(([, v]) => v !== undefined && v !== null && v !== "");
});
</script>

<template>
  <!-- 根元素自己就是 flex 列：顶栏固定，下面（Lab / 监控）占满剩余高度。
       两个模式都用 v-show 保持挂载 —— 切到监控再回来，编辑器里的未保存内容还在。 -->
  <div class="view labtrain" :data-mode="mode">
    <header class="lt-bar">
      <div class="lt-brand">
        <span class="lt-logo"><Icon name="layers" cls="st-icon st-icon--sm" /></span>
        <span class="lt-name">LLM Lab</span>
        <span class="lt-sub">{{ t("train.workbench") }}</span>
      </div>

      <div class="lt-facts">
        <span class="st-badge lt-state" :class="badge"><i class="lt-dot"></i>{{ state.text }}</span>
        <template v-if="d">
          <span class="lt-fact"><i>{{ t("train.step") }}</i><b>{{ int(d.step) }}</b>
            <em>/ {{ int(d.total_steps) }}</em></span>
          <span class="lt-fact"><i>{{ t("train.loss") }}</i><b>{{ fmt4(d.loss) }}</b></span>
          <span class="lt-fact lt-fact--best"><i>{{ t("train.best") }}</i>
            <b>{{ d.best ? fmt4(d.best.val) : "–" }}</b></span>
          <span class="lt-prog" :title="`${fmt1(pct)}%`"><i :style="{ width: pct + '%' }"></i></span>
        </template>
      </div>

      <div class="lt-actions">
        <button type="button" class="st-btn st-btn--secondary lt-start" :title="t('train.start.hint')"
                @click="startTraining">
          <Icon name="play" cls="st-icon st-icon--sm" /><span>{{ t("train.start") }}</span>
        </button>
        <div class="lt-switch" role="tablist" :aria-label="t('train.mode')">
          <button type="button" role="tab" :data-on="mode === 'code'" @click="setMode('code')">
            <Icon name="tool" cls="st-icon st-icon--sm" />{{ t("train.mode.code") }}</button>
          <button type="button" role="tab" :data-on="mode === 'monitor'" @click="setMode('monitor')">
            <Icon name="chart" cls="st-icon st-icon--sm" />{{ t("train.mode.monitor") }}</button>
        </div>
      </div>
    </header>

    <div class="lt-body">
      <!-- 代码模式：完整 LLM Lab（文件树 / 编辑器 / agent / 后台任务） -->
      <WorkspaceView v-show="mode === 'code'" :open-jobs="jobsSignal" class="lt-ws" />

      <!-- 监控模式：当前这次训练放大成一整页 -->
      <div v-show="mode === 'monitor'" class="lt-monitor">
        <div class="lt-monitor__inner">
          <div class="lt-monitor__bar">
            <div class="lt-monitor__title">
              <Icon name="chart" cls="st-icon st-icon--sm" />
              <span class="card-title">{{ t("train.title") }}</span>
              <span class="st-badge" :class="badge">{{ state.text }}</span>
              <span v-if="d" class="muted small">{{ t("train.updated", { at: d.updated_bj, s: int(age) }) }}</span>
            </div>
            <div class="lt-monitor__tools">
              <label class="chip"><input v-model="auto" type="checkbox">{{ t("train.auto") }}</label>
              <button class="st-btn st-btn--icon" :title="t('train.refresh')" @click="load">
                <Icon name="refresh" cls="st-icon st-icon--sm" /></button>
            </div>
          </div>

          <p v-if="err === 'failed'" class="muted small">{{ t("train.missing.hint") }}</p>

          <template v-if="d">
            <!-- 进度条：step / 总步数，配 epoch 与剩余时间 -->
            <div class="st-card prog">
              <div class="prog__bar"><i :style="{ width: pct + '%' }"></i></div>
              <div class="prog__nums">
                <div><span>{{ t("train.step") }}</span><b>{{ d.step.toLocaleString() }}</b>
                  <em>/ {{ d.total_steps.toLocaleString() }}（{{ fmt1(pct) }}%）</em></div>
                <div><span>{{ t("train.epoch") }}</span><b>{{ fmt2(d.epoch) }}</b>
                  <em>/ {{ fmt2(d.total_epochs) }}</em></div>
                <div><span>{{ t("train.eta") }}</span><b>{{ d.eta_min == null ? "–" : fmt1(d.eta_min) + " min" }}</b>
                  <em>{{ t("train.elapsed", { v: dur(elapsed) }) }}</em></div>
                <div><span>{{ t("train.loss") }}</span><b>{{ fmt4(d.loss) }}</b>
                  <em>{{ int(d.tok_s) }} tok/s</em></div>
                <div><span>{{ t("train.best") }}</span><b>{{ d.best ? fmt4(d.best.val) : "–" }}</b>
                  <em>{{ d.best ? `step ${d.best.step} · ppl ${fmt2(d.best.ppl)}` : "" }}</em></div>
              </div>
            </div>

            <!-- 曲线分两层：上面两张 loss 是要盯的（大），下面三张是诊断量（小） -->
            <div class="charts charts--big">
              <div class="st-card chart-card">
                <div class="req-head"><span class="card-title">{{ t("train.c.train") }}</span>
                  <span class="muted small">{{ t("train.ax.loss") }} · {{ t("train.points", { n: steps.length }) }}</span></div>
                <LineChart v-if="steps.length" :series="sTrain" :times="tSteps" :height="188"
                           :format="(v) => Number(v).toFixed(3)" />
                <p v-else class="muted small">{{ t("train.wait.first") }}</p>
                <p class="muted small">{{ t("train.hump") }}</p>
              </div>
              <div class="st-card chart-card">
                <div class="req-head"><span class="card-title">{{ t("train.c.val") }}</span>
                  <span class="muted small">{{ t("train.ax.loss") }} · {{ t("train.val.every") }}</span></div>
                <LineChart v-if="evals.length" :series="sVal" :times="tEvals" :height="188"
                           :format="(v) => Number(v).toFixed(3)" />
                <p v-else class="muted small">{{ t("train.wait.first") }}</p>
              </div>
            </div>
            <div class="charts charts--small">
              <div class="st-card chart-card">
                <div class="req-head"><span class="card-title">{{ t("train.c.lr") }}</span>
                  <span class="muted small">{{ t("train.ax.lr") }} · {{ d.lr == null ? "" : Number(d.lr).toExponential(2) }}</span></div>
                <LineChart v-if="steps.length" :series="sLr" :times="tSteps" :height="126"
                           :format="(v) => Number(v).toExponential(1)" />
              </div>
              <div class="st-card chart-card">
                <div class="req-head"><span class="card-title">{{ t("train.c.gnorm") }}</span>
                  <span class="muted small">{{ t("train.ax.gn") }} · {{ fmt2(d.grad_norm) }}</span></div>
                <LineChart v-if="steps.length" :series="sGn" :times="tSteps" :height="126"
                           :format="(v) => Number(v).toFixed(2)" />
              </div>
              <div class="st-card chart-card">
                <div class="req-head"><span class="card-title">{{ t("train.c.tok") }}</span>
                  <span class="muted small">{{ t("train.ax.tok") }} · {{ d.tok_s ? int(d.tok_s) : "–" }}</span></div>
                <LineChart v-if="steps.length" :series="sTps" :times="tSteps" :height="126"
                           :format="(v) => Math.round(v).toLocaleString()" />
                <p class="muted small">{{ t("train.c.tok.note") }}</p>
              </div>
            </div>

            <!-- 台账与配置并排：宽屏时右边那列不用再往下翻 -->
            <div class="bottom">
              <div class="st-card">
                <div class="req-head"><span class="card-title">{{ t("train.evals") }}</span>
                  <span class="muted small">{{ t("train.val.gap") }}</span></div>
                <div class="tbl"><table class="st-table">
                  <thead><tr><th class="num">step</th><th class="num">epoch</th><th class="num">train</th>
                    <th class="num">val</th><th class="num">ppl</th><th></th></tr></thead>
                  <tbody>
                    <tr v-for="e in evals.slice().reverse().slice(0, 40)" :key="e[0]">
                      <td class="num">{{ e[0].toLocaleString() }}</td>
                      <td class="num">{{ fmt2(e[0] * (d.tokens_per_step || 1) / (d.train_tokens || 1)) }}</td>
                      <td class="num">{{ fmt4(e[1]) }}</td>
                      <td class="num">{{ fmt4(e[2]) }}</td>
                      <td class="num">{{ fmt2(e[3]) }}</td>
                      <td><span v-if="d.best && d.best.step === e[0]" class="st-badge st-badge--generating">BEST</span></td>
                    </tr>
                  </tbody>
                </table></div>
              </div>

              <div class="st-card meta">
                <div class="req-head"><span class="card-title">{{ t("train.cfg") }}</span>
                  <span class="muted small">{{ t("train.cfg.note") }}</span></div>
                <dl class="kv">
                  <template v-for="[k, v] in cfgRows" :key="k">
                    <dt>{{ k }}</dt><dd>{{ typeof v === "number" ? v.toLocaleString() : v }}</dd>
                  </template>
                </dl>
                <p class="muted small">{{ t("train.source", { src: SRC }) }}</p>
              </div>
            </div>
          </template>

          <!-- 还没跑过训练：给一条能直接照抄的命令，别让人对着空白页猜 -->
          <div v-else-if="err !== 'failed'" class="lt-empty st-card">
            <span class="lt-empty__icon"><Icon name="chart" cls="st-icon" /></span>
            <h3 class="lt-empty__title">{{ t("train.empty.t") }}</h3>
            <p class="lt-empty__text">{{ t("train.empty.d") }}</p>
            <code class="lt-empty__cmd">python main.py train --steps 500</code>
            <div class="lt-empty__actions">
              <button type="button" class="st-btn st-btn--primary" @click="startTraining">
                <Icon name="play" cls="st-icon st-icon--sm" />{{ t("train.start") }}</button>
              <button type="button" class="st-btn st-btn--secondary" @click="setMode('code')">
                <Icon name="tool" cls="st-icon st-icon--sm" />{{ t("train.mode.code") }}</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 根：上下两段（固定顶栏 + 占满剩余高度的模式区）。外壳 body 是 overflow:hidden，
   所以这里自己管滚动 —— 代码页内部各栏自己滚，监控页整块滚。 */
.labtrain { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column;
            background: var(--st-bg); overflow: hidden; }

/* ---------------- 顶栏：品牌 / 实时训练摘要 / 启动训练 / 模式切换 ---------------- */
.lt-bar { flex: none; display: flex; align-items: center; gap: var(--st-s-4); min-height: 48px;
          padding: 6px 14px; border-bottom: 1px solid var(--st-line); background: var(--st-surface); }
.lt-brand { display: flex; align-items: center; gap: 8px; min-width: 0; flex: none; }
.lt-logo { display: inline-flex; width: 26px; height: 26px; align-items: center; justify-content: center;
           border-radius: 8px; background: var(--st-accent-tint); color: var(--st-accent); }
.lt-name { font-size: var(--st-fs-sm); font-weight: var(--st-fw-black); color: var(--st-ink); white-space: nowrap; }
.lt-sub { font-size: var(--st-fs-xs); color: var(--st-ink-muted); white-space: nowrap; }

.lt-facts { flex: 1; min-width: 0; display: flex; align-items: center; gap: var(--st-s-4); overflow: hidden; }
.lt-state .lt-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 5px;
                    background: currentColor; vertical-align: middle; }
.lt-fact { display: inline-flex; align-items: baseline; gap: 4px; white-space: nowrap; }
.lt-fact i { font-style: normal; font-size: 11px; color: var(--st-ink-muted); }
.lt-fact b { font-size: var(--st-fs-sm); color: var(--st-ink); font-variant-numeric: tabular-nums; }
.lt-fact em { font-style: normal; font-size: 11px; color: var(--st-ink-muted); }
.lt-prog { flex: 1; max-width: 180px; min-width: 60px; height: 6px; border-radius: 999px;
           background: var(--st-surface-2); overflow: hidden; }
.lt-prog i { display: block; height: 100%; border-radius: 999px; background: var(--st-chart-1);
             transition: width .5s linear; }

.lt-actions { display: flex; align-items: center; gap: var(--st-s-2); flex: none; }
.lt-start { height: 32px; padding: 0 10px; font-size: 12px; gap: 4px; }
/* 分段切换：一个滑块感的分段控件，点哪边整页就切到哪边 */
.lt-switch { display: inline-flex; gap: 2px; padding: 2px; border-radius: 9px; background: var(--st-surface-2); }
.lt-switch button { display: inline-flex; align-items: center; gap: 4px; height: 28px; padding: 0 10px;
                    border: 0; border-radius: 7px; background: none; cursor: pointer;
                    font: inherit; font-size: 12px; color: var(--st-ink-muted); }
.lt-switch button:hover { color: var(--st-ink); }
.lt-switch button[data-on="true"] { background: var(--st-surface); color: var(--st-ink);
                                    font-weight: var(--st-fw-bold); box-shadow: var(--st-sh-card); }

.lt-body { flex: 1; min-height: 0; display: flex; }
.lt-ws { flex: 1; min-width: 0; }

/* ---------------- 监控模式 ---------------- */
.lt-monitor { flex: 1; min-width: 0; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.lt-monitor__inner { max-width: 1560px; margin: 0 auto; padding: var(--st-s-5) var(--st-s-6) var(--st-s-8);
                     display: flex; flex-direction: column; gap: var(--st-s-4); }
.lt-monitor__inner > * { flex: none; }
.lt-monitor__bar { display: flex; align-items: center; gap: var(--st-s-3); flex-wrap: wrap; }
.lt-monitor__title { display: flex; align-items: center; gap: var(--st-s-2); min-width: 0; flex: 1; }
.lt-monitor__tools { display: flex; align-items: center; gap: var(--st-s-2); margin-left: auto; }

.prog__bar { height: 8px; border-radius: 999px; background: var(--st-surface-2); overflow: hidden; }
.prog__bar i { display: block; height: 100%; background: var(--st-chart-1); border-radius: 999px;
  transition: width .5s linear; }
.prog__nums { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: var(--st-s-3);
  margin-top: var(--st-s-4); }
.prog__nums span { display: block; font-size: var(--st-fs-xs); color: var(--st-ink-muted); }
.prog__nums b { font-size: var(--st-fs-lg); font-variant-numeric: tabular-nums; color: var(--st-ink); }
/* Vue 的 whitespace:condense 会吃掉「换行分隔的 <b> 与 <em>」之间的空格，
   所以这里不靠文本空格排版 —— 明细单独一行更稳（长数字也不会挤出行）。 */
.prog__nums em { display: block; margin-top: 2px; font-style: normal;
  font-size: var(--st-fs-xs); color: var(--st-ink-muted); }
/* 用固定的两档网格，不用 auto-fit：宽屏下 auto-fit 会把五张图排成 4+1，
   第二行左边一张、右边全空 —— 看着像坏了。层级也顺带分清：loss 大、诊断量小。 */
.charts { display: grid; gap: var(--st-s-4); min-width: 0; }
.charts--big { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.charts--small { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.bottom { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: var(--st-s-4); align-items: start; }
.chart-card { display: flex; flex-direction: column; gap: var(--st-s-2); min-width: 0; }
@media (max-width: 1100px) { .charts--small { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 880px) {
  .charts--big, .charts--small, .bottom { grid-template-columns: minmax(0, 1fr); }
}
/* 标题与「纵轴 …」注释放一行了：标题占剩余宽度、注释右对齐不换行，
   否则窄卡片里两者互相挤成上下两截（截图里那个断字）。 */
.chart-card .req-head { align-items: baseline; }
.chart-card .req-head .card-title { flex: 1 1 auto; }
.chart-card .req-head .muted { flex: 0 1 auto; text-align: right; white-space: nowrap; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px var(--st-s-4); margin: var(--st-s-2) 0 0; }
.kv dt { font-size: var(--st-fs-xs); color: var(--st-ink-muted); }
.kv dd { margin: 0; font-size: var(--st-fs-sm); color: var(--st-ink); word-break: break-all; }
/* 台账限高 + 表头吸顶：40 行能把页面拖到几千像素高，而训练时要看的是"最近这段有没有变好"。
   全量台账在 out_dir 的 metrics.csv / loss-curve.png，页面上不占篇幅。 */
.tbl { max-height: 420px; overflow: auto; }
.tbl thead th { position: sticky; top: 0; z-index: 1; background: var(--st-surface); }

/* ---------------- 空态：还没有训练记录 ---------------- */
.lt-empty { display: grid; justify-items: center; gap: 10px; padding: 48px 20px; text-align: center; }
.lt-empty__icon { display: inline-flex; width: 46px; height: 46px; align-items: center; justify-content: center;
                  border-radius: 14px; background: var(--st-accent-tint); color: var(--st-accent); }
.lt-empty__icon .st-icon { width: 22px; height: 22px; }
.lt-empty__title { margin: 0; font-size: var(--st-fs-lg); color: var(--st-ink); }
.lt-empty__text { margin: 0; max-width: 520px; font-size: var(--st-fs-sm); line-height: 1.6; color: var(--st-ink-muted); }
.lt-empty__cmd { padding: 7px 12px; border-radius: 8px; background: var(--st-surface-2); border: 1px solid var(--st-line);
                 font: 400 12.5px var(--st-font-mono), monospace; color: var(--st-ink-soft); }
.lt-empty__actions { display: flex; gap: 8px; margin-top: 4px; }

/* ---------------- 窄屏：摘要减到最少，切换和启动按钮保留 ---------------- */
@media (max-width: 900px) {
  .lt-bar { gap: var(--st-s-2); padding: 6px 10px; }
  .lt-sub, .lt-fact--best, .lt-prog { display: none; }
  .lt-start span { display: none; }
  .lt-start { width: 34px; padding: 0; justify-content: center; }
  .lt-switch button { padding: 0 8px; }
  .lt-monitor__inner { padding: var(--st-s-4) var(--st-s-3) var(--st-s-8); }
}
@media (max-width: 560px) {
  .lt-facts { display: none; }
}
</style>