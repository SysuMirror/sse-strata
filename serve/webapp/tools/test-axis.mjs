// x 轴刻度选择的回归测试（纯函数、不用浏览器，秒出结果）。用法：node tools/test-axis.mjs
//
// 起因：监控页折线在**刚攒出两个采样点**时，x 轴三个时间戳叠成一坨（`03:583:53:52`）。
// 根因是「两端 + 中间」的下标里，中间那个 `floor((n-1)/2)` 在 n=2 时算出 0 = 与左端同一个点。
// 下面钉的就是这类「少画一个」的判断 —— 不是钉住「必须画三个」。
import { xTicks, span, GUTTER } from "../src/chart-axis.js";

const FAILS = [];
const ck = (name, ok, detail = "") => {
  console.log((ok ? "  PASS  " : "  FAIL  ") + name + (detail ? `  ${detail}` : ""));
  if (!ok) FAILS.push(name);
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// 通用不变量：任何 (n, 宽度) 组合下，**任意两个时间戳实际占的区间都不许压在一起**，且下标不许重复。
// 这条比逐条列举有用得多 —— 页面宽度与采样点数都是外部给的，谁都可能递上来一个没见过的组合。
// 判据在这里**自己重算一遍**（用 span 的几何，不是「中心差 ≥ 62」）：中心距够不等于不打架，
// 贴左的往右长 62px、居中的两边各 31px，这两种撞法只有按区间才看得见。
function overlaps(a, b) { return a[0] < b[1] - GUTTER && b[0] < a[1] - GUTTER; }
function invariants(n, iw, where) {
  const t = xTicks(n, iw);
  const at = (i) => (i / Math.max(1, n - 1)) * iw;
  const dup = new Set(t.map((s) => s.i)).size !== t.length;
  const boxes = t.map((s) => span(at(s.i), s.anchor));
  const hits = [];
  for (let a = 0; a < boxes.length; a++) for (let b = a + 1; b < boxes.length; b++) if (overlaps(boxes[a], boxes[b])) hits.push(`${a}×${b}`);
  // 标签总宽不该超出绘图区（贴右的那个不许倒着咬穿左边界）
  const spill = boxes.some(([l, r]) => r > iw + 0.5 || l < -0.5);
  const bad = dup || hits.length > 0 || (t.length > 1 && spill);
  if (bad) console.log(`   · 违例 ${where}: n=${n} iw=${iw} → ${JSON.stringify(t)} 重叠=${hits} 越界=${spill}`);
  return !bad;
}
let allOk = true;
for (let n = 1; n <= 120; n++) {
  for (let iw = 20; iw <= 1400; iw += 10) if (!invariants(n, iw, "扫描")) allOk = false;
}
ck("扫描 n=1..120 × 宽度 20..1400（每 10px 一档）：下标不重复、标签互不压叠、不越出绘图区", allOk);

// ---- 那条把它逼出来的具体形状
ck("两个点时不再画三遍（原来中间那个 = 下标 0，与左端重合）",
   eq(xTicks(2, 640).map((t) => t.i), [0, 1]), JSON.stringify(xTicks(2, 640)));
ck("两个点 + 窄绘图区：只留最新那一刻", eq(xTicks(2, 50).map((t) => t.i), [1]), JSON.stringify(xTicks(2, 50)));
ck("三个点 + 够宽：两端 + 中间（正常情形照旧画三个）",
   eq(xTicks(3, 640).map((t) => t.i), [0, 1, 2]), JSON.stringify(xTicks(3, 640)));
ck("窗口 90 个点 + 常规宽度：三个刻度，中间落在窗口正中",
   eq(xTicks(90, 640).map((t) => t.i), [0, 44, 89]), JSON.stringify(xTicks(90, 640)));

// ---- 窄屏：中点离端点不够一个字宽就丢掉中点，而不是叠上去
ck("中等宽度（140px）丢中点、保留两端", eq(xTicks(90, 140).map((t) => t.i), [0, 89]), JSON.stringify(xTicks(90, 140)));
ck("极窄（< 一个字宽）只剩一个时间戳", xTicks(90, 40).length === 1);

// ---- 锚点：贴左的用 start、贴右的用 end，中间那个居中，别让字压出绘图区
ck("锚点跟着位置走", eq(xTicks(90, 640).map((t) => t.anchor), ["start", "middle", "end"]),
   JSON.stringify(xTicks(90, 640).map((t) => t.anchor)));
ck("只剩一个时间戳时贴右（end）", xTicks(2, 50)[0].anchor === "end");

// ---- 退化输入不许炸
ck("没有点 / 一个点：什么都不标", xTicks(0, 640).length === 0 && xTicks(1, 640).length === 0);
ck("宽度为 0 或负数：什么都不标", xTicks(30, 0).length === 0 && xTicks(30, -80).length === 0);

console.log("=".repeat(58));
if (FAILS.length) { console.log(`FAIL：${FAILS.length} 项未通过 -> ${JSON.stringify(FAILS)}`); process.exit(1); }
console.log(`ALL PASS（x 轴刻度 ${11} 条断言，其中第 1 条是 n=1..120 × 139 种宽度的扫描）`);
