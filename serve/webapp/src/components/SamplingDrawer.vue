<script setup>
// 采样抽屉：思考档、温度/top-p/top-k、上限、种子、显示思考、MCP 工具、实验性提速投影、
// 「同时给其它应用用」（POST /settings）。与原 app.js 的采样段等价。
import { ref, reactive, computed, watch } from "vue";
import { useI18n } from "vue-i18n";
import { store, toast, headers } from "../api.js";
import { app, settings, DEFAULTS, projectionLoaded } from "../store.js";
import Icon from "../components/Icon.vue";

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(["close"]);
const { t } = useI18n();

const draft = reactive({ ...settings });
const sharedOn = ref(false);

const mcpCount = computed(() => (app.mcp.servers || []).length);
const showEsp = computed(() => projectionLoaded());
const greedy = computed(() => +draft.temperature === 0);
const thinkSub = computed(() => t(`thinking.${draft.thinking}.sub`));

function loadDrawer(s = settings) {
  Object.assign(draft, { ...DEFAULTS, ...s });
}
watch(() => props.open, async (isOpen) => {
  if (!isOpen) return;
  loadDrawer();
  try {
    const r = await fetch("settings", { headers: headers() });
    if (r.ok) sharedOn.value = !!(await r.json()).shared;
  } catch (e) { /* 旧版服务器：开关保持关闭 */ }
});

const sharedDefaults = (s) => {
  const d = { reasoning_effort: s.thinking, temperature: +s.temperature };
  if (+s.temperature > 0) Object.assign(d, { top_p: +s.top_p, top_k: +s.top_k });
  if (s.seed) d.seed = +s.seed;
  if (s.max) d.max_tokens = +s.max;
  if (projectionLoaded()) d.experimental_speed_projection = s.esp !== false;
  return d;
};
async function saveShared(on, s) {
  const r = await fetch("settings", { method: "POST", headers: headers(true),
                                     body: JSON.stringify({ defaults: on ? sharedDefaults(s) : null }) });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error.message || msg; } catch (e) { /* not json */ }
    throw new Error(msg);
  }
  sharedOn.value = !!(await r.json()).shared;
}

async function apply() {
  Object.assign(settings, {
    thinking: draft.thinking, temperature: +draft.temperature, top_p: +draft.top_p, top_k: +draft.top_k,
    max: String(draft.max).trim(), seed: String(draft.seed).trim(), show: !!draft.show,
    esp: !!draft.esp, mcp: !!draft.mcp,
  });
  store.set("sampling", { ...settings });
  const share = !!draft.share;
  emit("close");
  if (share || sharedOn.value) {
    try {
      await saveShared(share, settings);
      toast("success", t("toast.sampling.saved"), share ? t("toast.sampling.shared") : t("toast.sampling.unshared"));
    } catch (e) {
      toast("error", t("toast.shared.failed"), e.message, 6000);
    }
    return;
  }
  toast("success", t("toast.sampling.saved"), +settings.temperature === 0 ? t("toast.sampling.greedy") : "");
}
</script>

<template>
  <aside class="st-drawer" :data-open="String(open)" :aria-hidden="String(!open)" :aria-label="t('chat.sampling')">
    <div class="st-drawer__head">
      <span class="card-title">{{ t("sampling.title") }}</span>
      <button class="st-btn st-btn--icon" :aria-label="t('sampling.close')" @click="emit('close')">
        <Icon name="chevron" /></button>
    </div>
    <div class="st-drawer__body">
      <div class="st-field">
        <label class="st-label">{{ t("sampling.thinking") }} <output>{{ thinkSub }}</output></label>
        <div class="seg" role="radiogroup" :aria-label="t('sampling.thinking')">
          <button v-for="v in ['none', 'low', 'medium', 'high']" :key="v" type="button" role="radio"
                  :aria-checked="draft.thinking === v" @click="draft.thinking = v">{{ t(`thinking.${v}`) }}</button>
        </div>
      </div>

      <div class="st-field">
        <label class="st-label" for="s-temp">{{ t("sampling.temp") }}
          <output>{{ greedy ? t("sampling.temp.greedy") : (+draft.temperature).toFixed(2) }}</output></label>
        <input id="s-temp" v-model.number="draft.temperature" class="st-slider" type="range" min="0" max="1.5" step="0.05">
        <span class="muted small">{{ t("sampling.temp.hint") }}</span>
      </div>

      <div class="st-field">
        <label class="st-label" for="s-topp">{{ t("sampling.topp") }} <output>{{ (+draft.top_p).toFixed(2) }}</output></label>
        <input id="s-topp" v-model.number="draft.top_p" class="st-slider" type="range" min="0.05" max="1" step="0.05" :disabled="greedy">
      </div>
      <div class="st-field">
        <label class="st-label" for="s-topk">{{ t("sampling.topk") }} <output>{{ draft.top_k }}</output></label>
        <input id="s-topk" v-model.number="draft.top_k" class="st-slider" type="range" min="1" max="64" step="1" :disabled="greedy">
      </div>

      <div class="st-field">
        <label class="st-label" for="s-max">{{ t("sampling.max") }} <output>{{ t("sampling.max.hint") }}</output></label>
        <input id="s-max" v-model="draft.max" class="st-input" type="number" min="1" step="1" :placeholder="t('sampling.max.placeholder')">
      </div>
      <div class="st-field">
        <label class="st-label" for="s-seed">{{ t("sampling.seed") }} <output>{{ t("sampling.seed.hint") }}</output></label>
        <input id="s-seed" v-model="draft.seed" class="st-input" type="number" min="1" step="1" :placeholder="t('sampling.seed.placeholder')">
      </div>

      <div class="toggle-row"><span>{{ t("sampling.show") }}<br><span class="muted small">{{ t("sampling.show.sub") }}</span></span>
        <button class="st-toggle" role="switch" :aria-checked="!!draft.show" :aria-label="t('sampling.show')"
                @click="draft.show = !draft.show"></button></div>

      <div v-if="mcpCount" class="toggle-row"><span>{{ t("sampling.mcp") }}<br>
        <span class="muted small">{{ t("sampling.mcp.sub") }}</span></span>
        <button class="st-toggle" role="switch" :aria-checked="draft.mcp !== false" :aria-label="t('sampling.mcp')"
                @click="draft.mcp = draft.mcp === false"></button></div>

      <div v-if="showEsp" class="toggle-row"><span>{{ t("sampling.esp") }}<br>
        <span class="muted small">{{ t("sampling.esp.sub") }}</span></span>
        <button class="st-toggle" role="switch" :aria-checked="draft.esp !== false" :aria-label="t('sampling.esp')"
                @click="draft.esp = draft.esp === false"></button></div>

      <div class="toggle-row"><span>{{ t("sampling.share") }}<br>
        <span class="muted small">{{ t("sampling.share.sub") }}</span></span>
        <button class="st-toggle" role="switch" :aria-checked="!!draft.share" :aria-label="t('sampling.share')"
                @click="draft.share = !draft.share"></button></div>

      <div class="drawer-actions">
        <button class="st-btn st-btn--secondary" type="button" @click="loadDrawer(DEFAULTS)">{{ t("sampling.reset") }}</button>
        <button class="st-btn st-btn--primary" type="button" @click="apply">{{ t("sampling.apply") }}</button>
      </div>
    </div>
  </aside>
</template>
