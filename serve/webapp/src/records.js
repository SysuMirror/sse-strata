// 推理记录（/infer/*）：页面上**只取元信息**——id、时间、模型、通道、读/缓存/写的 token 数、
// 速度、TTFT、耗时、状态。
//
// 用户输入与模型的思考/回复也写在记录里（对象存储为主），但**接口不返回内容**，
// 这一层自然拿不到；内容只在代理侧为错误分析留了口子（/infer/raw，且只认登录会话）。
// 所以这里的数据可以放心渲染，不存在"把用户的话画到屏幕上"的风险。
import { reactive } from "vue";
import { headers } from "./api.js";

export const rec = reactive({
  rows: [], total: 0, live: [], stats: null,
  loading: false, error: "", needLogin: false, at: 0,
});

let seq = 0;                       // 只认最后一次请求的结果（翻页快了会有返回乱序）

async function getJson(path) {
  const r = await fetch(path, { headers: headers() });
  if (r.status === 401) {
    const e = new Error("need-login");
    e.needLogin = true;
    throw e;
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/** 列表 + 实时尾巴。q 里的空值会被丢掉（不然 ?model= 会把"全部"过滤成空）。 */
export async function loadRecords(q = {}) {
  const mine = ++seq;
  rec.loading = true;
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== "" && v != null) p.set(k, v);
  try {
    const j = await getJson(`/infer/list?${p.toString()}`);
    if (mine !== seq) return;                     // 有更新的请求在跑，丢弃这次结果
    rec.rows = j.rows || [];
    rec.total = j.total || 0;
    rec.live = j.live || [];
    rec.error = "";
    rec.needLogin = false;
    rec.at = Date.now();
  } catch (e) {
    if (mine !== seq) return;
    rec.needLogin = !!e.needLogin;
    rec.error = e.needLogin ? "" : String(e.message || e);
    rec.rows = rec.live = [];
    rec.total = 0;
  } finally {
    if (mine === seq) rec.loading = false;
  }
}

export async function loadStats(days = 7) {
  try {
    rec.stats = await getJson(`/infer/stats?days=${days}`);
    rec.needLogin = false;
  } catch (e) {
    rec.needLogin = !!e.needLogin;
  }
}

/** 单条元信息（点开一行看细节用；列表里那份通常就够，这个保证拿到完整字段）。 */
export async function loadItem(id) {
  try {
    return await getJson(`/infer/item?id=${encodeURIComponent(id)}`);
  } catch (e) {
    return null;
  }
}

/** 这一条的 RAG 前后对照元信息（检索回来的每块历史：哪个回合、多长、三阶段分数）。
 *  **不含正文** —— 正文只在服务端的错误分析口子（/infer/raw）里。 */
export async function loadRag(id) {
  try {
    return await getJson(`/infer/rag?id=${encodeURIComponent(id)}`);
  } catch (e) {
    return null;
  }
}
