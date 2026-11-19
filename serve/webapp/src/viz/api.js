// 数据来源：本机实测 + 预留的真实接口 + 示例兜底。
//
//   本机实测  src/viz/model.local.js —— tools/make-model-info.py 从 GGUF 头里读出来的
//             架构元数据与真实张量形状。**不是 mock**：层数、头数、专家数、参数量都是文件里的真值。
//   真实接口  GET  /api/models               模型列表
//             GET  /api/models/:id/graph     模型图结构（节点带坐标）
//             POST /api/models/:id/infer-steps  推理步骤（body: { text }）
//             引擎目前没有这三条路由（serve/server.py 只服务 /web/<扁平文件名> 和几个固定端点），
//             请求会拿到 404。回落是静默的，但来源会如实标在界面上，不会让人把示例数字当实测。
//   示例兜底  mock.js 里两个小模型，用来演示不同规模
//
// 三条路径产出的图与步骤格式完全一样（graph.js 统一生成），所以后端就绪后前端不用改结构。
//
// TODO: 替换为真实 API —— 后端就绪后这里什么都不用改（下面已经在打真实接口了），
// 只是回落的数据会被顶掉；若想彻底不发起探测请求，把 PROBE_API 置 false。
import { headers } from "../api.js";
import { SOURCE } from "./types.js";
import { buildGraph, buildSteps } from "./graph.js";
import { mockModels } from "./mock.js";
import { LOCAL_MODEL } from "./model.local.js";

const PROBE_API = true;          // 先打真实接口，打不通再回落

const URLS = {
  models: "/api/models",
  graph: (id) => `/api/models/${encodeURIComponent(id)}/graph`,
  steps: (id) => `/api/models/${encodeURIComponent(id)}/infer-steps`,
};

/** 带鉴权头的请求；超时、非 2xx、解析失败都当「没有这个接口」处理。 */
async function tryApi(path, init, ms = 4000) {
  if (!PROBE_API) return null;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(path, { headers: headers(!!init?.body), signal: ctl.signal, ...init });
    if (!r.ok) return null;
    const data = await r.json();
    return data && typeof data === "object" ? data : null;
  } catch (e) {
    return null;                                   // 404 / 超时 / 引擎没起：一律回落
  } finally {
    clearTimeout(timer);
  }
}

/** 接口形状校验：字段不全就当作没有，宁可回落也不要半截数据。 */
const isModel = (m) => m && typeof m.id === "string" && typeof m.layers === "number"
  && typeof m.hiddenSize === "number" && typeof m.vocabSize === "number";

/**
 * 模型列表。本机实测的那个排在最前（它是这台机器真正在跑的东西），
 * 然后是接口给的，最后是示例。每一项都带 source，界面按选中项标注来源。
 */
export async function fetchModels() {
  const r = await tryApi(URLS.models);
  const fromApi = r && Array.isArray(r.models) ? r.models.filter(isModel).map((m) => ({ ...m, source: SOURCE.API })) : [];

  const models = [];
  if (LOCAL_MODEL && LOCAL_MODEL.layers) models.push({ ...LOCAL_MODEL, source: SOURCE.LOCAL });
  models.push(...fromApi);
  models.push(...mockModels());
  return { models, source: models.length ? models[0].source : SOURCE.MOCK };
}

export async function fetchGraph(model) {
  const r = await tryApi(URLS.graph(model.id));
  if (r && Array.isArray(r.nodes) && r.nodes.length && Array.isArray(r.edges)) {
    return { graph: { nodes: r.nodes, edges: r.edges }, source: SOURCE.API };
  }
  return { graph: buildGraph(model), source: model.source || SOURCE.MOCK };
}

export async function fetchSteps(model, text) {
  const r = await tryApi(URLS.steps(model.id), { method: "POST", body: JSON.stringify({ text }) });
  if (r && Array.isArray(r.steps) && r.steps.length) {
    return { steps: r.steps, source: SOURCE.API };
  }
  return { steps: buildSteps(model, text), source: model.source || SOURCE.MOCK };
}

/**
 * **真的跑一次推理**，拿这台机器上模型的实际输出。
 *
 * 演示最后要落到「模型回答了啥」——那就不能编：编出来的答案配不上前面那套真实的形状与张量。
 * 这里直接打 `/v1/chat/completions`（和对话页同一条路：登录或用本机 token 时由网关代持密钥），
 * 拿到真实的回答与 token 数；拿不到就如实说拿不到，界面上标「未取到」。
 *
 * @param {string} model 引擎认得的名字（health 里的）
 * @param {string} text  演示句子
 */
export async function fetchAnswer(model, text, maxTokens = 220) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 90000);
  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: headers(true),
      signal: ctl.signal,
      body: JSON.stringify({
        model,
        messages: [{ role: "user", content: text }],
        stream: false,
        max_tokens: maxTokens,
        temperature: 0.6,
        // 这台机器上跑的是**会思考**的模型：不给它一点预算，40 个 token 全花在思考上，
        // content 会是空的（实测踩过）。这里要的是「它怎么答」，不是深度推理，所以压低思考预算。
        reasoning_effort: "low",
      }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const msg = (j.choices && j.choices[0] && j.choices[0].message) || {};
    const content = String(msg.content || "").trim();
    if (!content) throw new Error("empty");
    return {
      text: content,
      reasoning: String(msg.reasoning_content || "").trim(),
      tokens: (j.usage && j.usage.completion_tokens) || null,
      finish: (j.choices && j.choices[0] && j.choices[0].finish_reason) || null,
    };
  } finally {
    clearTimeout(timer);
  }
}
