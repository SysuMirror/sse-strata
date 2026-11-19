// 可视化的全部状态与动作：选中模型 / 图 / 步骤 / 当前帧 / 播放 / 选择与悬停。
//
// 与 src/store.js 一个路数（reactive + 模块级函数），不引 Pinia —— 这个页面只有一个
// 消费者，多一层 store 库不划算。播放器也在这里，组件里不出现任何定时器。
import { computed, reactive } from "vue";
import { store } from "../api.js";
import { app, engine } from "../store.js";
import { addExternalChat } from "../chats.js";
import { fetchAnswer, fetchGraph, fetchModels, fetchSteps } from "./api.js";
import { DEMO_TEXT } from "./mock.js";
import { SOURCE } from "./types.js";

export const SPEEDS = [0.5, 1, 2];
const BASE_MS = 1500;                 // 1x 时每步停留多久

export const viz = reactive({
  models: [],
  modelId: store.get("viz.model", null),
  graph: { nodes: [], edges: [] },
  steps: [],
  index: -1,                          // -1 = 还没开始演
  playing: false,
  speed: SPEEDS.includes(store.get("viz.speed", 1)) ? store.get("viz.speed", 1) : 1,   // 只认三档
  text: DEMO_TEXT,
  selectedNodeId: null,               // 点击选中的节点（右侧面板跟着换）
  hoverNodeId: null,
  loading: false,
  error: "",
  skipFlash: null,               // 刚刚跳过了多少个同性质的步骤（画布上闪一句说明）
  // 演示最后一步要给出「模型到底回答了啥」——这是**真跑一次**的结果（见 api.js fetchAnswer）
  answer: { state: "idle", text: "", tokens: null, chatId: null, error: "" },
  // 演示模式：开始演示后置上，左栏模型卡片、右栏详情、顶栏的选型与输入整块退场，
  // 只留画布 + 悬浮的播放按钮与时间轴。退出靠「退出演示」（= 重置）或自己再点一次。
  focus: false,
  // 三块面板各自能不能收起（模型列表 / 步骤详情 / 时间轴）。收起来只剩画布 ——
  // 想安静看图的时候，不必逐个去关。收起来后画布边缘会留一个小把手把它拉回来。
  panes: { left: true, right: true, timeline: true },
  hudClosed: false,                // 画布上那张提示卡被叉掉过
});

/** 收起 / 展开某一块面板 */
export function togglePane(name) {
  if (name in viz.panes) viz.panes[name] = !viz.panes[name];
}

/** 一键只留画布 / 恢复三块 */
export function toggleAllPanes() {
  const any = viz.panes.left || viz.panes.right || viz.panes.timeline;
  viz.panes.left = viz.panes.right = viz.panes.timeline = !any;
}

/** 关掉画布上那张提示卡（再进演示时会自己回来） */
export function closeHud() { viz.hudClosed = true; }

// ------------------------------------------------------------------ 派生值

export const currentModel = computed(() => viz.models.find((m) => m.id === viz.modelId) || null);
export const currentStep = computed(() => viz.steps[viz.index] || null);
export const totalSteps = computed(() => viz.steps.length);
/** 进度 0~1，滑块用 */
export const progress = computed(() => (viz.steps.length <= 1 ? 0 : viz.index / (viz.steps.length - 1)));
/** 当前帧参与的节点集合 —— 3D 场景每一帧都读它，做成 Set 免得在渲染循环里反复建数组 */
export const activeNodes = computed(() => new Set(currentStep.value ? currentStep.value.activeNodeIds : []));
export const activeEdges = computed(() => new Set(currentStep.value ? currentStep.value.activeEdgeIds : []));
/** 被点开详情的节点：优先选中的，其次悬停的 */
export const inspectedNode = computed(() => {
  const id = viz.selectedNodeId || viz.hoverNodeId;
  return viz.graph.nodes.find((n) => n.id === id) || null;
});
/** 数据来源：跟着选中的模型走 —— 列表里既有本机实测项也有示例项，标错一个都不行 */
export const dataSource = computed(() => (currentModel.value && currentModel.value.source) || SOURCE.MOCK);
/** 当前帧正在用的权重名（右侧面板的「高亮权重」列表） */
export const activeWeights = computed(() => (currentStep.value ? currentStep.value.activeWeightNames : []));
/** 演示走到第几层 —— 顶部「层 3 / 12」用 */
export const layerCursor = computed(() => {
  const s = currentStep.value;
  if (!s || s.layer == null) return null;
  const total = currentModel.value ? currentModel.value.layers : 0;
  return { layer: s.layer + 1, total };
});

// ------------------------------------------------------------------ 加载

let loadToken = 0;                  // 快速切模型时丢弃过期请求的结果

export async function init() {
  viz.loading = true;
  viz.error = "";
  try {
    const { models } = await fetchModels();
    viz.models = models;
    if (!models.some((m) => m.id === viz.modelId)) viz.modelId = models[0] ? models[0].id : null;
  } catch (e) {
    viz.error = String((e && e.message) || e);
    viz.loading = false;
    return;
  }
  viz.loading = false;
  if (viz.modelId) await selectModel(viz.modelId);
}

/**
 * 切模型：重新取图与步骤，并把播放位置归零。
 * @param {string} id
 */
export async function selectModel(id) {
  const model = viz.models.find((m) => m.id === id);
  if (!model) return;
  viz.modelId = id;
  store.set("viz.model", id);
  stop();
  viz.focus = false;
  viz.index = -1;
  viz.selectedNodeId = null;
  viz.hoverNodeId = null;
  viz.loading = true;
  const mine = ++loadToken;
  try {
    const [{ graph }, { steps }] = await Promise.all([
      fetchGraph(model),
      fetchSteps(model, viz.text),
    ]);
    if (mine !== loadToken) return;                 // 期间又切了模型
    viz.graph = graph;
    viz.steps = steps;
    viz.error = "";
  } catch (e) {
    if (mine !== loadToken) return;
    viz.error = String((e && e.message) || e);
    viz.graph = { nodes: [], edges: [] };
    viz.steps = [];
  } finally {
    if (mine === loadToken) viz.loading = false;
  }
}

/** 真的跑一次推理，拿模型的回答。失败就如实标出来，不编答案。 */
let answerToken = 0;
export async function loadAnswer(text) {
  const mine = ++answerToken;
  viz.answer = { state: "loading", text: "", tokens: null, error: "" };
  try {
    const a = await fetchAnswer(engine.model, text);
    if (mine !== answerToken) return;
    // 把这次真实问答存进对话页 —— 演示里写着「完整回答见对话页」，那就得真的在那儿。
    // 同一句话再演示一次会覆盖那条会话，不会越攒越多。
    const rec = addExternalChat({
      title: `推理可视化 · ${text.slice(0, 14)}`,
      question: text,
      answer: a.text,
      meta: `推理可视化演示 · 本机实测${a.tokens ? ` · ${a.tokens} tokens` : ""}`,
    });
    viz.answer = { state: "done", text: a.text, reasoning: a.reasoning, tokens: a.tokens,
                   chatId: rec && rec.id, error: "" };
  } catch (e) {
    if (mine !== answerToken) return;
    viz.answer = { state: "error", text: "", tokens: null, error: String((e && e.message) || e) };
  }
}

// ------------------------------------------------------------------ 演示控制

/** 开始（或重新开始）演示：用当前输入句重算步骤，然后从头播。 */
export async function startDemo(text) {
  const model = currentModel.value;
  if (!model) return;
  const sentence = String(text == null ? viz.text : text).trim();
  if (!sentence) {
    viz.error = "empty";
    return;
  }
  viz.text = sentence;
  viz.error = "";
  viz.loading = true;
  const mine = ++loadToken;
  try {
    const { steps } = await fetchSteps(model, sentence);
    if (mine !== loadToken) return;
    viz.steps = steps;
    viz.index = 0;
    viz.selectedNodeId = null;
    viz.playing = true;
    viz.focus = true;                 // 进入演示模式：无关的面板退场，画布占满
    loadAnswer(sentence);             // 同时发一次真实推理，供最后两步使用（不挡演示）
    viz.hudClosed = false;            // 提示卡重新登场（它讲的是「现在算到哪」，演示时有用）
    schedule();
  } catch (e) {
    viz.error = String((e && e.message) || e);
  } finally {
    if (mine === loadToken) viz.loading = false;
  }
}

/** 回到初始状态（步骤保留，滑块归零）。**顺带退出演示模式** ——
 *  重来一遍就是回到能改模型、能改句子的那个界面，面板要回场。 */
export function reset() {
  stop();
  viz.index = -1;
  viz.selectedNodeId = null;
  viz.hoverNodeId = null;
  viz.focus = false;
}

/** 进出演示模式（开始演示自动进，见 startDemo；出来走 reset） */
export function setFocus(on) { viz.focus = !!on; }

export function togglePlay() {
  if (viz.playing) { stop(); return; }
  if (!viz.steps.length) return;
  if (viz.index >= viz.steps.length - 1) viz.index = 0;   // 播到底了再按就是重播
  viz.playing = true;
  schedule();
}

export function stop() {
  viz.playing = false;
  clearTimeout(timer);
  timer = null;
}

export function stepBy(delta) {
  stop();
  goTo(viz.index + delta);
}

export function goTo(i) {
  if (!viz.steps.length) return;
  const clamped = Math.max(-1, Math.min(viz.steps.length - 1, Math.round(i)));
  viz.index = clamped;
}

export function setSpeed(s) {
  viz.speed = s;
  store.set("viz.speed", s);
  if (viz.playing) schedule();                    // 立刻按新速度重排下一次
}

export function selectNode(id) {
  viz.selectedNodeId = viz.selectedNodeId === id ? null : id;   // 再点一次取消选中
}

export function hoverNode(id) {
  viz.hoverNodeId = id;
}

// ------------------------------------------------------------------ 播放器
//
// setTimeout 链而不是 setInterval：每帧走完再排下一帧，读到的永远是当下的 speed，
// 改速度、暂停都不用去同步一个已经在跑的计时器。

let timer = null;

function schedule() {
  clearTimeout(timer);
  if (viz.playing) timer = setTimeout(tick, BASE_MS / viz.speed);
}

function tick() {
  if (!viz.playing) return;
  let i = viz.index + 1;
  if (i > viz.steps.length - 1) { stop(); return; }
  // 同性质的层**只演两层，其余整段跳过**（48 层逐层放完是 198 步，没人看得下去）。
  // 跳过的区间仍在时间轴上，随时可以拖回去单独看。
  let skipped = 0;
  while (i < viz.steps.length - 1 && viz.steps[i].same) { i++; skipped++; }
  if (viz.steps[i] && viz.steps[i].same) { stop(); return; }   // 末尾全是同性质的：收尾
  viz.index = i;
  if (skipped) {
    const s = viz.steps[i - 1] || viz.steps[i];
    viz.skipFlash = { n: skipped, to: s.same.layer, kind: s.same.kind };
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { viz.skipFlash = null; }, 3000);
  } else {
    viz.skipFlash = null;
  }
  schedule();
}
let flashTimer = null;
