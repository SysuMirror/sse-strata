// 训练监控页的端到端验证：真浏览器打开生产入口的 #train，确认
//   1) 页面无 JS 错误、徽章与曲线渲染出来；
//   2) 数据在自动刷新（两次读到的 step 不同）；
//   3) **刷新不丢**：重新加载后曲线点数不减、step 不回零（数据源是全量文件，不是内存尾巴）。
// 用法：node tools/verify-train.mjs [地址]
import puppeteer from "puppeteer";

const BASE = process.argv[2] || "http://127.0.0.1:8080";
const URL = `${BASE}/#train`;
const FAILS = [];
const ck = (n, ok, d = "") => { console.log((ok ? "  PASS  " : "  FAIL  ") + n + (d ? `  ${d}` : "")); if (!ok) FAILS.push(n); };

const browser = await puppeteer.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1200 });
const errs = [];
// 设计内的噪音（同 verify-live.mjs）：favicon 缺失；没带登录会话时 /chats、/auth/me 必然 401
const EXPECTED_401 = /\/chats|\/auth\/me/;
page.on("console", (m) => {
  const u = (m.location() || {}).url || "";
  const t = m.text() + " " + u;
  if (m.type() === "error" && !/favicon\.ico/.test(t) && !(/401/.test(t) && EXPECTED_401.test(t))) errs.push(t);
});
page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));

const snapshot = () => page.evaluate(() => {
  const w = window.__TRAIN_METRICS__ || {};
  return {
    step: w.step, total: w.total_steps, run: w.run, alive: w.alive,
    points: (w.steps || []).length, evals: (w.evals || []).length,
    best: w.best && w.best.val,
    svgs: document.querySelectorAll(".train svg").length,
    badge: (document.querySelector(".train-head .st-badge") || {}).textContent || "",
    rows: document.querySelectorAll(".train table tbody tr").length,
    bars: document.querySelectorAll(".train .prog__nums > div").length,
  };
});

await page.goto(URL, { waitUntil: "networkidle2" });
await new Promise((r) => setTimeout(r, 7000));            // 等注入 + 首轮画图
const a = await snapshot();
console.log("首次:", JSON.stringify(a));
ck("页面无 JS 错误", errs.length === 0, errs.slice(0, 3).join(" | "));
ck("读到训练数据", !!a.run && a.points > 0, `run=${a.run} points=${a.points}`);
ck("徽章显示在训/已结束", /在训|已结束|Training|Finished/.test(a.badge), a.badge.trim());
ck("曲线已渲染（≥4 张图）", a.svgs >= 4, `svg=${a.svgs}`);
ck("进度卡 5 个数字", a.bars === 5, `bars=${a.bars}`);
ck("评估台账有行", a.rows > 0, `rows=${a.rows}`);
// 布局回归：训练页曾经因为根元素没声明宽度而只占半屏；反向的错是横向溢出（出现滚动条）。
const geo = await page.evaluate(() => ({
  vw: window.innerWidth,
  overflow: document.scrollingElement.scrollWidth - window.innerWidth,
  used: Math.round(document.querySelector(".train")?.getBoundingClientRect().width || 0),
  pageH: Math.round(document.scrollingElement.scrollHeight),
}));
ck("页面无横向溢出", geo.overflow <= 2, `溢出 ${geo.overflow}px`);
ck("内容占满可用宽度", geo.used > geo.vw * 0.6, `.train 宽 ${geo.used}px / 视口 ${geo.vw}px`);
ck("页面不过长（台账已限高）", geo.pageH < 2200, `整页高 ${geo.pageH}px`);
// 滚动能力：外壳 body 是 overflow:hidden，训练页必须自己滚（曾经内容被裁掉、滚不动）
const scr = await page.evaluate(() => {
  const el = document.querySelector(".train");
  el.scrollTop = 0;
  const can = el.scrollHeight - el.clientHeight;
  el.scrollTop = 400;
  const got = el.scrollTop;
  el.scrollTop = 0;
  return { can, got };
});
// 要求：要么本来就没溢出（视口够高），要么设多大就能滚到多大（不超过可滚范围）
ck("训练页能自己上下滚动", scr.can <= 4 || scr.got >= Math.min(400, scr.can) - 2,
   `可滚 ${scr.can}px，设 400 实得 ${scr.got}px`);

await new Promise((r) => setTimeout(r, 12000));            // 自动刷新（5s 一轮）
const b = await snapshot();
ck("自动刷新在推进 step", b.step > a.step, `${a.step} → ${b.step}`);
ck("点数不减（全量历史）", b.points >= a.points, `${a.points} → ${b.points}`);

await page.reload({ waitUntil: "networkidle2" });          // 刷新不丢
await new Promise((r) => setTimeout(r, 6000));
const c = await snapshot();
ck("刷新后 step 不回零", c.step >= b.step, `${b.step} → ${c.step}`);
ck("刷新后曲线仍完整", c.points >= b.points && c.evals >= b.evals, `points=${c.points} evals=${c.evals}`);

await page.screenshot({ path: "/tmp/train-page.png", fullPage: true });
console.log("截图: /tmp/train-page.png");
await browser.close();
console.log(FAILS.length ? `\n失败 ${FAILS.length} 项: ${FAILS.join(", ")}` : "\n全部通过");
process.exit(FAILS.length ? 1 : 0);
