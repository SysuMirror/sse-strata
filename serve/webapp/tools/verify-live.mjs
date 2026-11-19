// 对**真实生产入口**的端到端验证：真浏览器打开 8080 的页面，带真 key 拉真实数据。
// 用法：STRATA_KEY=$(cat ~/logs/strata-api-key.txt) node tools/verify-live.mjs [地址]
import fs from "node:fs";
import puppeteer from "puppeteer";

const BASE = process.argv[2] || "http://127.0.0.1:8080";
const KEY = process.env.STRATA_KEY || fs.readFileSync("/home/cloud/logs/strata-api-key.txt", "utf8").trim();
const FAILS = [];
const ck = (n, ok, d = "") => { console.log((ok ? "  PASS  " : "  FAIL  ") + n + (d ? `  ${d}` : "")); if (!ok) FAILS.push(n); };

const browser = await puppeteer.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1100 });
const errs = [];
// 这些是**设计内的**，不算错：
//  · favicon.ico：浏览器都会去要一次，原版页面同样没有；
//  · /chats、/auth/me 的 401：这次跑没有登录会话（只带了 API key），
//    「没登录 → 401 → 界面标「只在本机」」正是要的行为，不是故障。
const EXPECTED_401 = /\/chats|\/auth\/me/;
page.on("console", (m) => {
  const u = (m.location() && m.location().url) || "";
  const t = m.text() + " " + u;
  if (m.type() === "error" && !/favicon\.ico/.test(t)
      && !(/401/.test(t) && EXPECTED_401.test(t))) errs.push(`${m.text()} @ ${u}`);
});
page.on("pageerror", (e) => errs.push(`pageerror: ${e.message}`));
// 预置 key（等价于用户在「关于」里填过）
await page.evaluateOnNewDocument((k) => {
  try { localStorage.setItem("strata.apikey", JSON.stringify(k)); localStorage.setItem("strata.lang", JSON.stringify("zh")); } catch (e) {}
}, KEY);

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 30000 });
console.log("== 对真实服务 " + BASE + " 的验证 ==");
const tabs = await page.$$eval(".st-tab", (e) => e.map((x) => x.textContent.trim()));
// 标签页清单跟着 VIEWS 走（2026-10-28 加了「训练」）；顺序变了要同时改这里
ck("中文标签页", JSON.stringify(tabs) === JSON.stringify(["对话", "监控", "记录", "推理可视化", "训练", "关于"]), JSON.stringify(tabs));
const logo = await page.$eval(".st-logo", (e) => ({ src: e.getAttribute("src"), w: e.naturalWidth }));
ck("页头 logo 真实加载", logo.w > 0, JSON.stringify(logo));

await page.click('.st-tab[aria-controls="view-monitor"]');
// 通道现在是图例行（.lane-key__row）+ 两张趋势图，不再是「每通道一张卡」——
// 这段原来还在找 .conc-lane / .conc-seg，早就对不上了，会一直等到超时。
await page.waitForFunction(() => document.querySelectorAll(".lane-key__row").length >= 1, { timeout: 15000 });
const lanes = await page.$$eval(".lane-key__row", (els) => els.map((e) => ({
  name: (e.querySelector(".lane-key__name") || {}).textContent?.trim(),
  state: (e.querySelector(".st-badge") || {}).textContent?.trim(),
  meta: (e.querySelector(".lane-key__meta") || {}).textContent?.replace(/\s+/g, " ").trim(),
})));
ck("并发区块渲染（真实通道）", lanes.length >= 1, JSON.stringify(lanes.map((l) => l.name)));
ck("通道名来自 lane_labels", lanes.map((l) => l.name).join(",") === "快道,普道",
   JSON.stringify(lanes.map((l) => l.name)));
ck("通道行有真实槽位 / GPU / 显存",
   lanes.every((l) => /GPU/.test(l.meta || "")) && lanes.some((l) => /GB/.test(l.meta || "")),
   JSON.stringify(lanes.map((l) => l.meta)));
ck("聚合数字卡在位", await page.$(".conc-sum .conc-nums") !== null, "");
ck("趋势图在位", (await page.$$(".chart-card")).length >= 1, "");
// x 轴时间戳不许压叠 —— 真实页面上最容易露馅的时刻是**刚加载完、折线只有两三个点**那一下
// （老逻辑在那一刻把中间刻度算成下标 0，与左端叠在一起，屏幕上是「03:583:53:52」这种乱码）
await page.waitForFunction(() => document.querySelectorAll(".lc__xtick").length >= 2, { timeout: 25000 }).catch(() => {});
const tickBad = await page.evaluate(() => {
  const hits = [];
  let charts = 0;
  for (const lc of document.querySelectorAll(".lc")) {
    charts++;
    const bs = [...lc.querySelectorAll(".lc__xtick")].map((e) => e.getBoundingClientRect());
    for (let a = 0; a < bs.length; a++) for (let b = a + 1; b < bs.length; b++) {
      const ow = Math.min(bs[a].right, bs[b].right) - Math.max(bs[a].left, bs[b].left);
      const oh = Math.min(bs[a].bottom, bs[b].bottom) - Math.max(bs[a].top, bs[b].top);
      if (ow > 1 && oh > 1) hits.push(`${charts}:${a}×${b}@${ow.toFixed(1)}px`);
    }
  }
  return { charts, hits };
});
ck("折线图 x 轴时间戳互不压叠", tickBad.charts > 0 && !tickBad.hits.length, JSON.stringify(tickBad));
// GPU 那五张（负载/显存/温度/功耗/PCIe）已搬进「GPU 详细数据」按卡显示；指标卡只剩速度(+预填)/CPU/磁盘
const metrics = await page.$$eval(".metric-card .st-metric__value", (els) => els.map((e) => e.textContent.trim()));
ck("指标卡只剩非 GPU 的三张且有真实数值", (await page.$$(".metric-card")).length === 3
   && metrics.filter((v) => v && v !== "–").length >= 3, JSON.stringify(metrics));
const card = await page.$eval(".state-card .card-title", (e) => e.textContent.replace(/\s+/g, " ").trim());
ck("引擎状态卡标注所属通道", /普道|快道/.test(card), card);
const proxy = await page.$eval(".conc-proxy", (e) => e.textContent.replace(/\s+/g, " ").trim());
ck("网关计数器可见", /透传|组装/.test(proxy), proxy.slice(0, 80));

// ---- 逐卡 GPU（数据源 = /concurrency 的 lanes[].gpus：各通道把自己**那张卡**报上来；老引擎只有 lanes[].gpu）
// 断言方式是「DOM 读数 == 报文读数」，而不是「读数应该是多少」：机器负载会变，
// 但页面不许在中间偷换数字（MiB→GB 的换算、掉线时不许拿陈旧缓存冒充现值，都在这一条里）。
await page.waitForFunction(() => document.querySelectorAll(".gpu-charts .lc__line").length >= 4, { timeout: 25000 });
const gpuLive = await page.$$eval(".gpu-lane", (els) => els.map((e) => ({
  title: (e.querySelector(".gpu-lane__name") || {}).textContent?.trim(),      // 「GPU 0」或「卡号未上报」
  lane: (e.querySelector(".gpu-lane__lane") || {}).textContent?.trim(),       // 用它的那条通道
  model: (e.querySelector(".gpu-lane__model") || {}).textContent?.replace(/\s+/g, " ").trim(),
  offline: e.hasAttribute("data-offline"),
  vals: [...e.querySelectorAll(".gpu-metric__value")].map((x) => x.textContent.replace(/\s+/g, " ").trim()),
})));
const concLive = await page.evaluate(async (k) =>
  (await fetch("concurrency", { headers: { Authorization: `Bearer ${k}` } })).json(), KEY);
// 报文侧同样归一化成「一张卡一项」：新引擎取 gpus[0]，老引擎只有 gpu（没卡号）
const laneCard = (l) => (l.gpus && l.gpus.length ? l.gpus[0] : l.gpu) || {};
const apiOf = Object.fromEntries((concLive.lanes || []).map((l) => [l.label, laneCard(l)]));
ck("逐卡：两条通道各一组", gpuLive.map((g) => g.lane).join(",") === "快道,普道", JSON.stringify(gpuLive.map((g) => g.lane)));
ck("逐卡：卡型由各通道上报", gpuLive.every((g) => /4090/.test(g.model || "")), JSON.stringify(gpuLive.map((g) => g.model)));
ck("逐卡：负载/显存/温度/功耗四格都有真实读数",
   gpuLive.every((g) => g.vals.length === 4 && g.vals.every((v) => v && v !== "–")), JSON.stringify(gpuLive.map((g) => g.vals)));
ck("逐卡：两条通道在线（掉线时不许用陈旧缓存冒充现值）", gpuLive.every((g) => !g.offline), "");
const domNum = (txt) => Number((txt || "").replace(/[^\d.]/g, ""));
ck("逐卡：负载读数与报文逐条一致（DOM 没偷换数字）",
   gpuLive.every((g) => apiOf[g.lane] && domNum(g.vals[0]) === Math.round(apiOf[g.lane].util_pct)),
   JSON.stringify(gpuLive.map((g) => [g.lane, g.vals[0], apiOf[g.lane] && apiOf[g.lane].util_pct])));
ck("逐卡：显存 MiB→GB 换算对得上报文（±0.1 GB）",
   gpuLive.every((g) => apiOf[g.lane] && Math.abs(domNum(g.vals[1]) - apiOf[g.lane].used_mib / 1024) <= 0.1),
   JSON.stringify(gpuLive.map((g) => [g.lane, g.vals[1], apiOf[g.lane] && apiOf[g.lane].used_mib])));
// 卡号只有坐在卡上的进程知道：新构建在 gpus[].index 里带上来。线上引擎还没重启到新构建时，
// 标题就该是「卡号未上报」——**报没报都算对，但绝不允许页面自己猜一个号**（所以口径在这里留痕）。
const titled = gpuLive.every((g) => /^GPU \d+$/.test(g.title || ""));
ck("逐卡：标题要么是卡号要么是「卡号未上报」，且与报文里的 index 一致",
   gpuLive.every((g) => (apiOf[g.lane].index == null ? g.title === "卡号未上报" : g.title === `GPU ${apiOf[g.lane].index}`)),
   `${JSON.stringify(gpuLive.map((g) => g.title))} 引擎已上报卡号=${titled}`);
ck("逐卡：曲线一条线一张卡（4 张图 × 卡数）",
   (await page.$$(".gpu-charts .lc__line")).length === gpuLive.length * 4,
   String((await page.$$(".gpu-charts .lc__line")).length));
ck("逐卡：四张图各一根 y 轴（不叠双轴）", (await page.$$(".gpu-charts .lc svg")).length === 4, "");
ck("逐卡：区块不蹭指标卡类（指标卡仍是 3 张）", (await page.$$(".metric-card")).length === 3,
   String((await page.$$(".metric-card")).length));

// ---- GPU 详细数据（底部，一卡一组：负载/显存/温度/功耗/PCIe，各带曲线）
const detail = await page.$$eval(".gpu-card", (els) => els.map((e) => ({
  title: (e.querySelector(".gpu-card__name") || {}).textContent?.trim(),
  who: (e.querySelector(".gpu-card__who") || {}).textContent?.replace(/\s+/g, " ").trim(),
  lines: [...e.querySelectorAll(".gpu-line")].map((x) => [x.dataset.s,
          x.querySelector(".gpu-line__value").textContent.replace(/\s+/g, " ").trim()]),
  sparks: e.querySelectorAll(".gpu-line__spark").length,
})));
ck("详细数据：两条通道两张卡 = 两组", detail.length === gpuLive.length && detail.length >= 2,
   JSON.stringify(detail.map((d) => d.title)));
ck("详细数据：每组五行（负载/显存/温度/功耗/PCIe）且各带曲线",
   detail.every((d) => d.lines.map((x) => x[0]).join(",") === "util,vram,temp,power,pcie" && d.sparks === 5),
   JSON.stringify(detail.map((d) => d.lines.map((x) => x[0]))));
// 负载/显存/温度/功耗这四格现在就该有数；PCIe 那格要等**新构建**（逐卡 PCIe 只在 gpus[] 里下发，
// 老引擎的 machine.gpu 没这个字段）→ 允许它是「–」，但不许它是空标签。
ck("详细数据：各卡各报各的数（负载/显存/温度/功耗不缺）",
   detail.every((d) => d.lines.slice(0, 4).every((x) => x[1] && !/–/.test(x[1]))),
   JSON.stringify(detail[0] && detail[0].lines));
ck("详细数据：与逐卡区块同一口径（同一张卡的负载数一致）",
   detail.every((d) => { const g = gpuLive.find((x) => x.lane && d.who.includes(x.lane));
     return !g || domNum(d.lines[0][1]) === domNum(g.vals[0]); }),
   JSON.stringify(detail.map((d) => [d.title, d.lines[0][1]])));
// PCIe 那一格要等新构建：逐卡的 Gen/宽度只在 machine.gpus[] 里下发，老引擎的 machine.gpu 没有，
// 所以「–」是**此刻的诚实值**，算通过；等新构建上线它就变成 Gen4x16（届时这条断言自动更严）。
// 「上下文占用」整块都来自 /metrics = 会话粘性钉住的那一条道 ⇒ 标题要署到那条道（有卡号就带卡号）
const ctxHead = await page.$eval(".ctx-card .card-title", (e) => e.textContent.replace(/\s+/g, " ").trim());
const mLaneUrl = (concLive.proxy || {}).metrics_lane;
const mLane = (concLive.lanes.find((l) => l.url === mLaneUrl) || {}).label;
const mCard = laneCard(concLive.lanes.find((l) => l.url === mLaneUrl) || {});
ck("上下文占用：标题署名到粘性那条道（报文里 metrics_lane 是谁就写谁）",
   !!mLane && ctxHead.includes(mLane) && (mCard.index == null || ctxHead.includes(`GPU ${mCard.index}`)),
   `${ctxHead}  报文=${mLaneUrl}→${mLane}(index=${mCard.index})`);
ck("详细数据：PCIe 一格要么有档位、要么明说没上报（不假装有数）",
   detail.every((d) => /Gen\d\s*x\d+/.test(d.lines[4][1]) || /^–/.test(d.lines[4][1])),
   JSON.stringify(detail.map((d) => d.lines[4][1])));

await page.screenshot({ path: "/tmp/live-monitor.png", fullPage: true });

// 记录页：拿真实元信息（线上可能还没有记录 —— 那就该是空态而不是报错）
await page.click('.st-tab[aria-controls="view-records"]');
await page.waitForSelector(".rec-table .st-table, .rec-empty", { timeout: 15000 });
const recState = await page.evaluate(() => {
  const rows = document.querySelectorAll(".rec-row").length;
  const stats = document.querySelector(".rec-stats");
  const empty = document.querySelector(".rec-empty");
  const head = document.querySelector(".rec-row") ? document.querySelector(".rec-row").textContent.replace(/\s+/g, " ").trim() : "";
  return { rows, hasStats: !!stats, needLogin: !!empty, head,
           statsText: stats ? stats.textContent.replace(/\s+/g, " ").trim().slice(0, 90) : "" };
});
ck("记录页拿得到元信息（不是登录提示）", !recState.needLogin, JSON.stringify(recState).slice(0, 120));
ck("记录页有概览卡（真实统计）", recState.hasStats, recState.statsText);
ck("记录页有内容或明确空态", recState.rows > 0 || /还没有记录/.test(await page.$eval(".rec-table", (e) => e.textContent)),
   `${recState.rows} 行`);
await page.screenshot({ path: "/tmp/live-records.png", fullPage: true });

// 关于页「本机」：一张卡一行、署到通道（原来只有一行，来源 /metrics = 粘性那条道，双卡看着像单卡）
await page.click('.st-tab[aria-controls="view-about"]');
await page.waitForSelector(".facts dd", { timeout: 15000 });
const aboutText = await page.$eval(".view--about", (e) => e.textContent.replace(/\s+/g, " "));
const gpuFacts = aboutText.match(/GPU[^（]*（[^）]*）/g) || [];
// 期望的行数 = 报文里「报得出逐卡读数」的卡数（一条通道可能 0 张或几张，所以不拿通道数直接比）
const wantCards = (concLive.lanes || []).reduce((a, l) => a + ((l.gpus && l.gpus.length) || (l.gpu ? 1 : 0)), 0);
ck("关于页：本机有几张卡就列几行（与报文里的逐卡数一致）",
   gpuFacts.length === wantCards && wantCards >= 2, `${JSON.stringify(gpuFacts)} 报文=${wantCards} 张`);
ck("关于页：每行都署到用它的那条通道",
   (concLive.lanes || []).filter((l) => l.gpu || (l.gpus || []).length)
     .every((l) => gpuFacts.some((x) => x.includes(l.label))), JSON.stringify(gpuFacts));

await page.click('.st-tab[aria-controls="view-chat"]');
ck("页面无 JS 报错", errs.length === 0, errs.slice(0, 3).join(" | "));
await browser.close();

console.log("=".repeat(56));
if (FAILS.length) { console.log(`FAIL: ${JSON.stringify(FAILS)}`); process.exit(1); }
console.log("ALL PASS（真实服务）");
