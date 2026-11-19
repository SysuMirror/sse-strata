<script setup>
// 关于页：模型与引擎、本机、接入地址、设置（API key / 深色主题）。与原 app.js 的 renderAbout 等价。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { app, laneCards, setTheme, setFont, FONTS } from "../store.js";
import { store, fmt, gb, copyText, toast } from "../api.js";
import Icon from "../components/Icon.vue";

const { t } = useI18n();

const eng = computed(() => (app.metrics && app.metrics.engine) || {});
const hw = computed(() => (app.metrics && app.metrics.hardware) || {});
const st = computed(() => (app.metrics && app.metrics.hardware_static) || {});

// INFO cvec=project:4-44[:singleL] | add:A-B | 0
function projectionText(c) {
  if (!c || c === "0" || c === 0) return null;
  const [mode, range, single] = String(c).split(":");
  const [a, b] = (range || "").split("-");
  return t("proj.text", {
    mode: t(mode === "project" ? "proj.project" : "proj.add"), a, b,
    direction: single ? t("proj.direction", { n: single.replace("single", "") }) : "",
  });
}

const engineRows = computed(() => {
  const e = eng.value;
  const kv = { int8: "8-bit", q4_0: "4-bit (Hadamard-rotated)", fp16: "16-bit" }[e.kv] || e.kv;
  return [
    [t("fact.model"), e.model],
    [t("fact.engine"), e.version ? `v${e.version}` : t("fact.built.from.source")],
    [t("fact.context"), e.max_context ? t("fact.tokens.n", { n: fmt(e.max_context) }) : null],
    [t("fact.kv"), kv ? `${kv}${e.kv_resident ? t("fact.kv.streamed", { n: fmt(e.kv_resident) }) : t("fact.kv.all.vram")}` : null],
    [t("fact.experts"), e.expert_slots ? t("fact.experts.n", { n: fmt(e.expert_slots), gb: gb((e.expert_cache_mib || 0) * 1048576) }) : null],
    [t("fact.spec"), e.spec ? `${t("fact.spec.n", { n: Math.max(0, (e.mtp_max || e.spec) - 1) })}${e.lookup ? t("fact.lookup") : ""}` : null],
    [t("fact.images"), e.images ? t("fact.images.on") : t("fact.images.off")],
    [t("fact.projection"), projectionText(e.cvec)],
  ];
});
/** 本机有几张卡在干活 = **一张卡一行**，并署上「GPU 几 · 哪条道」。
 *  原来这里只有一行 `hardware_static.gpu_name` —— 它的来源 `/metrics` 被会话粘性钉在一条通道上，
 *  于是双卡部署下这行既像「本机只有一张卡」又说不清是哪张（口径通则：来自 /metrics 的读数要自己署名）。 */
const gpuRows = computed(() => {
  const lanes = (app.concurrency && app.concurrency.lanes) || [];
  const out = [];
  for (const l of lanes) {
    for (const c of laneCards(l)) {
      const label = `${t("fact.gpu")}${c.index == null ? "" : " " + c.index}（${l.label || l.url}）`;
      out.push([label, c.name ? t("fact.gpu.name", { name: c.name, gb: gb((c.vram_total || 0) * 1048576, 0) })
                               : t("fact.gpu.none")]);
    }
  }
  if (out.length) return out;
  // 没有并发数据（直连引擎）：只有这一张，能给卡号就给
  const s = st.value, w = hw.value;
  const i = (s.gpu_indices || [null])[0];
  return [[`${t("fact.gpu")}${i == null ? "" : " " + i}`,
           s.gpu_name ? t("fact.gpu.name", { name: s.gpu_name, gb: gb(w.gpu_mem_total, 0) }) : t("fact.gpu.none")]];
});
const hwRows = computed(() => [
  ...gpuRows.value,
  [t("fact.cpu"), st.value.cpu_name ? t("fact.cpu.name", { name: st.value.cpu_name, threads: st.value.threads }) : null],
  [t("fact.ram"), hw.value.ram_total ? t("fact.ram.n", { gb: gb(hw.value.ram_total, 0) }) : null],
]);
const apiRows = computed(() => {
  const base = location.origin;
  return [
    [t("fact.openai"), `${base}/v1`, true],
    [t("fact.anthropic"), base, true],
    [t("fact.modelname"), eng.value.model, true],
  ];
});

function rowsOf(list) { return list.filter((r) => r[1] != null && r[1] !== ""); }

const apiKeyValue = computed({
  get: () => store.get("apikey", ""),
  set: (v) => { store.set("apikey", (v || "").trim()); toast("success", t("api.key.saved"), t("api.key.saved.sub")); },
});
</script>

<template>
  <section class="view view--about">
    <div class="about">
      <div class="st-card">
        <span class="card-title">{{ t("about.engine") }}</span>
        <dl class="facts">
          <template v-for="(r, i) in rowsOf(engineRows)" :key="i">
            <dt>{{ r[0] }}</dt><dd><code v-if="r[2]">{{ r[1] }}</code><template v-else>{{ r[1] }}</template></dd>
          </template>
        </dl>
      </div>
      <div class="st-card">
        <span class="card-title">{{ t("about.pc") }}</span>
        <dl class="facts">
          <template v-for="(r, i) in rowsOf(hwRows)" :key="i">
            <dt>{{ r[0] }}</dt><dd><code v-if="r[2]">{{ r[1] }}</code><template v-else>{{ r[1] }}</template></dd>
          </template>
        </dl>
      </div>
      <div class="st-card">
        <span class="card-title">{{ t("about.connect") }}</span>
        <p class="muted">{{ t("about.connect.sub") }}</p>
        <dl class="facts">
          <template v-for="(r, i) in rowsOf(apiRows)" :key="i">
            <dt>{{ r[0] }}</dt>
            <dd><code>{{ r[1] }}</code>
              <button class="st-btn st-btn--icon" :aria-label="t('common.copy')" @click="copyText(r[1])"><Icon name="copy" /></button></dd>
          </template>
        </dl>
      </div>
      <div class="st-card">
        <span class="card-title">{{ t("about.settings") }}</span>
        <div class="st-field">
          <label class="st-label" for="api-key">{{ t("about.apikey") }} <output>{{ t("about.apikey.note") }}</output></label>
          <input id="api-key" v-model="apiKeyValue" class="st-input" type="password" autocomplete="off"
                 :placeholder="t('about.apikey.placeholder')">
        </div>
        <div class="toggle-row"><span>{{ t("about.dark") }}</span>
          <button class="st-toggle" role="switch" :aria-checked="app.theme === 'dark'" :aria-label="t('about.dark')"
                  @click="setTheme(app.theme === 'dark' ? 'light' : 'dark', true)"></button></div>
        <div class="st-field">
          <span class="st-label">{{ t("about.font") }} <output>{{ t("about.font.note") }}</output></span>
          <div class="seg seg--3" role="radiogroup" :aria-label="t('about.font')">
            <button v-for="f in FONTS" :key="f" type="button" role="radio" :aria-checked="app.font === f"
                    @click="setFont(f)">{{ t(`about.font.${f}`) }}</button>
          </div>
        </div>
        <p class="muted small">{{ t("about.privacy") }}
          <a href="https://github.com/Niko1221/Strata" target="_blank" rel="noopener noreferrer">{{ t("about.github") }}</a></p>
      </div>
    </div>
  </section>
</template>
