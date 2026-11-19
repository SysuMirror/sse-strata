// 折线图 x 轴的刻度选择（纯函数，浏览器外也能测 —— 见 tools/test-axis.mjs）。
//
// 为什么单独抽出来：这段逻辑的坑不在「画得对不对」，在**什么时候该少画一个**。
// 原来固定标「两端 + 中间」三个点，而中间那个的下标是 `floor((n-1)/2)`：
//   · 采样点只有 2 个时（页面刚打开、折线刚攒出两个点）它算出 `0` ⇒ 三个刻度变成 `[0, 0, 1]`，
//     同一个 x 上画了两遍时间戳，`03:53:52` 与 `03:58:33` 直接叠成一坨乱码（截图实测）；
//   · 窗口很短或窄屏时，中间那个离端点不够一个字宽，同样糊在一起。
// 所以这里只回答一个问题：**给定 n 个点和这么多像素，哪些下标配得上一个时间戳**。
// 判据是**标签实际占的区间**不许重叠（锚点不同占位就不同：贴左的往右长、贴右的往左长、居中的两边各半），
// 而不是「两个 x 坐标相差多少」—— 后者会放过 start/middle 这种一边长一边短的撞法。

/** 一个 `HH:MM:SS` 在 11px 等宽数字下的占位（≈ 53px）+ 余量。 */
export const LABEL_W = 62;
/** 两个标签之间至少留的空隙。 */
export const GUTTER = 6;

/** 带锚点的标签在 x 上实际占的区间。 */
export function span(x, anchor, w = LABEL_W) {
  return anchor === "start" ? [x, x + w] : anchor === "end" ? [x - w, x] : [x - w / 2, x + w / 2];
}

const clash = (a, b) => a[0] < b[1] - GUTTER && b[0] < a[1] - GUTTER;

/**
 * @param n       采样点数（= times.length）
 * @param iw      绘图区内宽（像素，已去掉左右内边距）
 * @param labelW  一个时间戳的占位宽
 * @returns [{ i, anchor }] —— anchor 给 SVG text 的 text-anchor
 */
export function xTicks(n, iw, labelW = LABEL_W) {
  if (!(n >= 2) || !(iw > 0)) return [];
  const at = (i) => (i / (n - 1)) * iw;
  const fits = (list) => list.every((a, ai) => list.every((b, bi) => ai === bi
    || !clash(span(at(a.i), a.anchor, labelW), span(at(b.i), b.anchor, labelW))));
  const ends = [{ i: 0, anchor: "start" }, { i: n - 1, anchor: "end" }];
  // 连两端都摆不下 → 只留最新那一刻（读图的人最先要看的也是它）
  if (!fits(ends)) return [{ i: n - 1, anchor: "end" }];
  const mid = Math.floor((n - 1) / 2);
  const three = mid > 0 && mid < n - 1 ? [ends[0], { i: mid, anchor: "middle" }, ends[1]] : null;
  return three && fits(three) ? three : ends;
}
