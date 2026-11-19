<script setup>
// 生图工作台：两种模式
//   ① 直接生成：用户提示词 + 最多 4 张参考图（参考图**原图**作底图，不是让模型看图转述）
//   ② Agent 模式：自己联网找参考图 → 读公开资料 → 迭代提示词 → 再生成，过程逐步展示
import { ref, reactive, computed } from "vue";
import { toast } from "../api.js";
import { generateImage, imageSearch, urlToDataURL, agentRefine, searchWeb, persistImage } from "../hub.js";
import Icon from "./Icon.vue";
import ToolCard from "./ToolCard.vue";

const emit = defineEmits(["saved"]);

const mode = ref("direct");                 // direct | agent
const prompt = ref("");
const size = ref("1024x1024");
const steps = ref(8);
const seed = ref("");
const transparent = ref(false);
const refs = reactive([]);                  // [{url, name, from}]
const busy = ref(false);
const result = ref(null);                   // {url, seed}
const stepsLog = reactive([]);              // agent 过程
const foundImages = reactive([]);           // 联网找图结果

const SIZES = ["512x512", "832x480", "1024x1024", "832x1248", "1664x1664", "2048x2048"];
const canRun = computed(() => !busy.value && (prompt.value.trim() || refs.length));

async function pickFiles(e) {
  for (const f of [...(e.target.files || [])].slice(0, 4 - refs.length)) {
    if (!f.type.startsWith("image/")) continue;
    refs.push({ url: await fileToDataURL(f), name: f.name, from: "upload" });
  }
  e.target.value = "";
}
function fileToDataURL(file) {
  return new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(file); });
}
function addFound(i) {
  const f = foundImages[i];
  if (!f || refs.length >= 4) return;
  refs.push({ url: f.url, name: "web", from: "web" });
}
function dropRef(i) { refs.splice(i, 1); }

async function searchRefs() {
  if (!prompt.value.trim()) return toast("warn", "先写一句提示词");
  busy.value = true;
  try {
    const r = await imageSearch(prompt.value.trim(), 8);
    foundImages.splice(0, foundImages.length);
    for (const x of (r.results || []).slice(0, 8)) {
      try {
        const data = await urlToDataURL(x.image);
        foundImages.push({ url: data, src: x.image });
      } catch (e) { /* 抓不到的跳过 */ }
    }
    if (!foundImages.length) toast("warn", "没找到可用的图", "换一个描述试试");
  } catch (e) { toast("error", "搜图失败", String(e.message || e)); }
  finally { busy.value = false; }
}

async function run() {
  if (!canRun.value) return;
  busy.value = true;
  result.value = null;
  stepsLog.splice(0, stepsLog.length);
  try {
    let finalPrompt = prompt.value.trim();
    let useRefs = refs.map((r) => r.url);
    if (mode.value === "agent") {
      await agentRun((p) => (finalPrompt = p), (r) => (useRefs = r));
    }
    stepsLog.push({ name: "generate_image", state: "running", arguments: { prompt: finalPrompt.slice(0, 80), size: size.value, refs: useRefs.length } });
    const out = await generateImage({ prompt: finalPrompt, refs: useRefs, size: size.value,
                                      steps: Number(steps.value) || 8, seed: seed.value, transparent: transparent.value });
    let url = out.url;
    try { url = (await persistImage(out.url)).url; emit("saved"); } catch (e) { /* 存不进去就用 dataURL */ }
    Object.assign(stepsLog[stepsLog.length - 1], { state: "done", result: `seed ${out.seed}` });
    result.value = { url, seed: out.seed };
    if (mode.value === "agent") stepsLog.push({ name: "final_prompt", state: "done", arguments: {}, result: finalPrompt });
  } catch (e) {
    toast("error", "生图失败", String(e.message || e));
    if (stepsLog.length) stepsLog[stepsLog.length - 1].state = "error";
  } finally { busy.value = false; }
}

/** Agent 流程：找参考图 → 找资料 → 迭代提示词。每一步都落到 stepsLog 里，用户看得见。 */
async function agentRun(setPrompt, setRefs) {
  const userPrompt = prompt.value.trim();
  // 1) 联网找参考图
  stepsLog.push({ name: "image_search", state: "running", arguments: { q: userPrompt } });
  const picked = [];
  try {
    const r = await imageSearch(userPrompt, 6);
    for (const x of (r.results || []).slice(0, 3)) {
      try { picked.push(await urlToDataURL(x.image)); } catch (e) { /* 跳过 */ }
    }
    Object.assign(stepsLog[stepsLog.length - 1], { state: "done", result: `${picked.length} 张` });
  } catch (e) {
    Object.assign(stepsLog[stepsLog.length - 1], { state: "error", result: String(e.message || e) });
  }
  if (picked.length) setRefs([...refs.map((x) => x.url), ...picked].slice(0, 4));

  // 2) 读公开资料（标题级）
  stepsLog.push({ name: "search_web", state: "running", arguments: { q: userPrompt } });
  try {
    const r = await searchWeb(userPrompt, 4);
    Object.assign(stepsLog[stepsLog.length - 1], { state: "done", result: `${(r.results || []).length} 条` });
  } catch (e) {
    Object.assign(stepsLog[stepsLog.length - 1], { state: "error", result: String(e.message || e) });
  }

  // 3) 迭代提示词（两轮：先写，再按「画面可执行性」收紧）
  stepsLog.push({ name: "refine_prompt", state: "running", arguments: { round: 2 } });
  const r1 = await agentRefine(userPrompt, { refs: refs.map((x) => x.url), search: true });
  const r2 = await agentRefine(`${userPrompt}\n\n上一版提示词：${r1.prompt}\n请把它改得更具体：明确主体、构图、光线、材质、镜头，去掉抽象形容词。`,
                               { refs: refs.map((x) => x.url), search: false });
  setPrompt(r2.prompt || r1.prompt);
  Object.assign(stepsLog[stepsLog.length - 1], { state: "done", result: (r2.prompt || r1.prompt).slice(0, 120) });
}

function download() {
  const a = document.createElement("a");
  a.href = result.value.url; a.download = `image-${result.value.seed || Date.now()}.png`; a.click();
}
function useAsRef() {
  if (result.value) refs.push({ url: result.value.url, name: "result", from: "result" });
}
</script>

<template>
  <div class="imgstudio">
    <div class="imgstudio__modes" role="tablist">
      <button class="imgstudio__mode" :data-on="mode === 'direct'" @click="mode = 'direct'">
        <b>直接生成</b><small>你的提示词 + 参考图</small>
      </button>
      <button class="imgstudio__mode" :data-on="mode === 'agent'" @click="mode = 'agent'">
        <b>Agent 模式</b><small>自己找参考图、迭代提示词</small>
      </button>
    </div>

    <textarea v-model="prompt" rows="3" class="st-input imgstudio__prompt"
              placeholder="画面描述，例如：雨夜的霓虹街道，一只戴宇航头盔的柴犬，电影光"></textarea>

    <div class="imgstudio__refs">
      <div v-for="(r, i) in refs" :key="i" class="imgstudio__ref">
        <img :src="r.url" alt="">
        <button class="imgstudio__x" @click="dropRef(i)" :aria-label="'remove'">✕</button>
      </div>
      <label v-if="refs.length < 4" class="imgstudio__add" title="上传参考图">
        <Icon name="plus" cls="st-icon st-icon--sm" />
        <input type="file" accept="image/*" multiple hidden @change="pickFiles">
      </label>
      <button v-if="refs.length < 4" class="imgstudio__add" title="联网找参考图" :disabled="busy" @click="searchRefs">
        <Icon name="link" cls="st-icon st-icon--sm" />
      </button>
    </div>

    <div v-if="foundImages.length" class="imgstudio__found">
      <div class="imgstudio__found-head muted small">联网找到的参考图（点一张加入）</div>
      <div class="imgstudio__found-row">
        <button v-for="(f, i) in foundImages" :key="i" class="imgstudio__found-item" @click="addFound(i)">
          <img :src="f.url" alt="">
        </button>
      </div>
    </div>

    <div class="imgstudio__opts">
      <select v-model="size" class="st-input"><option v-for="s in SIZES" :key="s">{{ s }}</option></select>
      <input v-model="steps" type="number" min="1" max="8" class="st-input imgstudio__num" title="步数 1–8">
      <input v-model="seed" class="st-input imgstudio__seed" placeholder="seed">
      <label class="imgstudio__check"><input v-model="transparent" type="checkbox">透明</label>
    </div>

    <button class="st-btn st-btn--primary imgstudio__run" :disabled="!canRun" @click="run">
      <Icon name="bolt" cls="st-icon st-icon--sm" />
      {{ busy ? "生成中…" : mode === "agent" ? "让 Agent 来做" : "生成" }}
    </button>

    <div v-if="busy || stepsLog.length" class="imgstudio__log">
      <ToolCard v-for="(t, i) in stepsLog" :key="i" :tool="t" />
    </div>

    <div v-if="busy" class="imgstudio__skeleton"><span></span></div>

    <figure v-if="result" class="imgstudio__out">
      <img :src="result.url" alt="">
      <figcaption>
        <span class="muted small">seed {{ result.seed }}</span>
        <span class="imgstudio__acts">
          <button class="st-btn st-btn--secondary" @click="useAsRef">用作参考</button>
          <button class="st-btn st-btn--secondary" @click="download">下载</button>
        </span>
      </figcaption>
    </figure>
  </div>
</template>

<style scoped>
.imgstudio { display: flex; flex-direction: column; gap: 10px; }
.imgstudio__modes { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.imgstudio__mode { display: grid; gap: 2px; padding: 8px 10px; border: 1px solid var(--st-line); border-radius: 10px;
                   background: var(--st-surface); cursor: pointer; text-align: left; color: var(--st-ink-soft); }
.imgstudio__mode b { font-size: var(--st-fs-sm); color: var(--st-ink); }
.imgstudio__mode small { font-size: 11px; color: var(--st-ink-muted); }
.imgstudio__mode[data-on="true"] { border-color: var(--st-accent); background: color-mix(in srgb, var(--st-accent) 8%, var(--st-surface)); }
.imgstudio__prompt { resize: vertical; min-height: 76px; font-size: 14px; }
.imgstudio__refs { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.imgstudio__ref { position: relative; }
.imgstudio__ref img { width: 56px; height: 56px; object-fit: cover; border-radius: 8px; border: 1px solid var(--st-line); }
.imgstudio__x { position: absolute; top: -6px; right: -6px; width: 18px; height: 18px; border-radius: 50%; border: none;
                background: var(--st-ink); color: #fff; font-size: 10px; cursor: pointer; }
.imgstudio__add { display: inline-flex; width: 56px; height: 56px; align-items: center; justify-content: center;
                  border: 1px dashed var(--st-line); border-radius: 8px; background: none; cursor: pointer; color: var(--st-ink-muted); }
.imgstudio__add:hover { border-color: var(--st-accent); color: var(--st-accent); }
.imgstudio__found-head { margin-bottom: 4px; }
.imgstudio__found-row { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 4px; }
.imgstudio__found-item { padding: 0; border: 1px solid var(--st-line); border-radius: 8px; background: none; cursor: pointer; flex: none; }
.imgstudio__found-item img { width: 64px; height: 64px; object-fit: cover; border-radius: 7px; display: block; }
.imgstudio__opts { display: grid; grid-template-columns: 1fr 64px 72px auto; gap: 6px; align-items: center; }
.imgstudio__num, .imgstudio__seed { text-align: center; }
.imgstudio__check { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--st-ink-muted); }
.imgstudio__run { height: 42px; font-size: var(--st-fs-md); }
.imgstudio__log { display: grid; gap: 6px; }
.imgstudio__skeleton { position: relative; width: 100%; aspect-ratio: 1/1; border-radius: 12px; overflow: hidden;
                       background: var(--st-surface-2); border: 1px dashed var(--st-line); }
.imgstudio__skeleton span { position: absolute; inset: 0; background: linear-gradient(100deg, transparent 20%,
                             color-mix(in srgb, var(--st-ink) 6%, transparent) 40%, transparent 60%);
                             animation: imgsweep 1.4s linear infinite; }
@keyframes imgsweep { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
.imgstudio__out { margin: 0; display: grid; gap: 8px; }
.imgstudio__out img { width: 100%; border-radius: 12px; border: 1px solid var(--st-line); }
.imgstudio__out figcaption { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.imgstudio__acts { display: flex; gap: 6px; }
</style>