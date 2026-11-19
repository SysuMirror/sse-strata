// 空态的示例任务：一组人工挑选的、和这个工作台真正相关的入口。
//
// 之前这里会在空闲时让模型自己生成几条「快捷提问」，结果是越生成越像通用 AI 助手：
// 主题发散、句式雷同，而且还要占一次 GPU 请求。现在固定成一小池可执行的任务，
// 既贴合 LLM Lab / 知识库 / 代码，又不会在后台偷偷调用推理。
//
// 每条带 tag（分类）与 icon（sprite 里的图标名），空态卡片按这两个字段排版；
// tone 只决定图标底色，不参与业务。
import { reactive } from "vue";

const SHOW = 3;

const BUILTIN = [
  { tag: "LLM Lab", icon: "cube", tone: "code", text: "带我从 llm-study-lab 阶段 3 开始" },
  { tag: "编码", icon: "tool", tone: "code", text: "补上 attention.py 的 TODO 并跑 stage2" },
  { tag: "环境", icon: "cpu", tone: "ops", text: "跑 main.py doctor 看看缺什么" },
  { tag: "知识库", icon: "memory", tone: "kb", text: "把这份资料存进知识库并总结要点" },
  { tag: "训练", icon: "gpu", tone: "gpu", text: "检查 GPU 压力，开一个 500 步的训练任务" },
  { tag: "排查", icon: "activity", tone: "ops", text: "看看最近的报错日志，定位到具体行" },
  { tag: "生图 · 联网参考", icon: "image", tone: "image", text: "先联网找几张参考图，再画一张赛博朋克雨夜街道" },
  { tag: "原理", icon: "thinking", tone: "learn", text: "解释一下 KV cache 是干什么的" },
  { tag: "脚本", icon: "tool", tone: "code", text: "写一个脚本：统计目录下每个文件的行数" },
  { tag: "翻译", icon: "chat", tone: "learn", text: "把这段中文翻译成地道的英文" },
];

export const suggest = reactive({ pool: [...BUILTIN], shown: [], busy: false, lastAt: 0 });

/** 从池子里随机抽 n 条（不放回），池子不够就按顺序补。 */
export function rotate(n = SHOW) {
  const bag = (suggest.pool.length ? suggest.pool : BUILTIN).slice();
  const out = [];
  while (out.length < n && bag.length) out.push(...bag.splice(Math.floor(Math.random() * bag.length), 1));
  for (const b of BUILTIN) {
    if (out.length >= n) break;
    if (!out.includes(b)) out.push(b);
  }
  suggest.shown = out.slice(0, n);
  return suggest.shown;
}

export function loadSuggestions() {
  suggest.pool = [...BUILTIN];
  suggest.lastAt = 0;
  rotate();
}

/** 示例文案（老调用方只想要字符串时用）。 */
export const suggestText = (s) => (typeof s === "string" ? s : (s && s.text) || "");

// 保留这两个名字：ChatView 在挂载/卸载时调用它们。现在不再有后台生成循环。
export function startSuggestionLoop() {}
export function stopSuggestionLoop() {}
export function idleEnough() { return false; }