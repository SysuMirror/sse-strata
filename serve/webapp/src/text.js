// 把模型返回的**自由文本**收拾成能直接用的东西。纯函数、零依赖 ——
// 所以能脱离浏览器单独跑回归（tools/test-parse.mjs）。
//
// 这段值得单独拿出来测：它面对的是模型输出，格式没有保证，模型会加编号、加引号、
// 加一句「当然，以下是……」，这些都是常见情况，不是边角。

export const clean = (s) => String(s == null ? "" : s).trim().replace(/\s+/g, " ");

/** 去重且保序 */
export function dedupe(list) {
  const seen = new Set();
  const out = [];
  for (const x of list) {
    const k = clean(x);
    if (!k || seen.has(k)) continue;
    seen.add(k);
    out.push(k);
  }
  return out;
}

// 行首的编号/项目符号：`1. `、`1、`、`1)`、`（1）`、`- `、`• `、`· `……
const BULLET = /^\s*(?:[-*•·—–]|\d+\s*[.、)）]|\(\d+\)|（\d+）)\s*/;
// 模型爱加的开场白/说明行，这些不是问题
const META = /^(好的|当然|可以|没问题|以下是|下面是|注意|说明|希望|这些|问题示例|示例|例如|比如)/;
// 首尾成对的引号（中英文都算）
const QUOTES = /^["'“”「『《]+|["'“”」』》]+$/g;

/**
 * 模型输出 → 一组短问题。
 * 规则：一行一条、去掉编号与引号、长度 4~42、必须含汉字、且不是客套/说明行。
 * 宁可少要几条，也不要把「当然，以下是 6 个问题：」当成问题收进来。
 *
 * @param {string} text
 * @param {number} [max] 最多返回几条
 * @returns {string[]}
 */
export function parseSuggestions(text, max = 8) {
  return dedupe(
    String(text == null ? "" : text)
      .split(/\r?\n/)
      .map((line) => line.replace(BULLET, "").replace(QUOTES, "").trim())
      .filter((line) => {
        const n = line.length;
        if (n < 4 || n > 42) return false;
        if (!/[一-鿿]/.test(line)) return false;   // 中文对话页，英文行基本是噪声
        if (META.test(line)) return false;
        return true;
      }),
  ).slice(0, max);
}

export default { clean, dedupe, parseSuggestions };
