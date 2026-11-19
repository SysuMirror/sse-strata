// 直连引擎（没有 /concurrency，代理不在或版本太旧）时，监控页的逐卡区块只能靠 /metrics。
// smoke.mjs 起的是**带代理**的夹具（SINGLE 也只是「一条通道」），这条路它走不到 —— 而它正是
// 「一进程多张卡（层切分）」时唯一能一张张看卡的地方，所以单独一个脚本钉住它。
// 用法（第 1 个参数 = 构建产物目录）：npx vite build --outDir /tmp/x --emptyOutDir && node tools/check-direct-gpus.mjs /tmp/x
// 夹具刻意做成**两条卡**（GPU 0 / GPU 2）：断言「一卡一组、各带曲线」而不是「有一块 GPU 区域」。
// 注意别用 JS 的 `<<` 造字节数：`20 << 30 == 0`、`18 << 30 == -2147483648`（32 位有符号移位），
// 这个坑真踩过 —— 它把显存显示成 0.0 GB，看起来像换算写错了。
import fs from "node:fs";
import http from "node:http";
import puppeteer from "puppeteer";

const DIR = process.argv[2] || "/tmp/strata-web-check";
const METRICS = {
  live: { state: "generating", tok_s: 41.5, generated: 10, max_tokens: 100 },
  totals: { requests: 3, prompt_tokens: 300, reused: 100, output_tokens: 90, prompt_ms: 500, decode_ms: 2000,
            since: Date.now() / 1000 - 600 },
  requests: [], requests_kept: 0,
  engine: { model: "m", max_context: 262144, expert_slots: 6226, expert_cache_mib: 10341, version: "0.1.38", build: 8 },
  hardware: {
    // 层切分：一条道两张卡，逐卡那份必须一张张显示，标量 gpu_* 是合计
    gpus: [{ index: 0, name: "RTX 4090 D", util: 90, mem_used: 20 * 2 ** 30, mem_total: 24 * 2 ** 30, temp: 66, power: 300,
             power_limit: 425, pcie_gen: 4, pcie_gen_max: 4, pcie_width: 16, pcie_rx_mb: 900 },
           { index: 2, name: "RTX 4090 D", util: 40, mem_used: 18 * 2 ** 30, mem_total: 24 * 2 ** 30, temp: 55, power: 200,
             power_limit: 425, pcie_gen: 1, pcie_gen_max: 4, pcie_width: 16, pcie_rx_mb: 0.5 }],
    gpu_util: 65, gpu_mem_used: 38e9, gpu_mem_total: 48e9, gpu_temp: 66, gpu_power: 500, gpu_power_limit: 850,
    cpu: 30, ram_used: 90e9, ram_total: 200e9, disk_read_mb: 1.5, disk_write_mb: 0.2,
  },
  hardware_static: { gpu_name: "RTX 4090 D + RTX 4090 D", gpu_count: 2, gpu_indices: [0, 2], cores: 76, threads: 76,
                     psutil: true },
  history: { gpu_util: [60, 65], gpu_mem_used: [37e9, 38e9], gpu_temp: [64, 66], gpu_power: [480, 500],
             gpu_pcie_rx_mb: [800, 900], cpu: [28, 30], tok_s: [40, 41], prefill_tok_s_mean: [300, 320] },
  time: Date.now() / 1000,
};
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml" };
const srv = http.createServer((req, res) => {
  const url = req.url.split("?")[0];
  if (url === "/metrics") { res.writeHead(200, { "Content-Type": "application/json" }); return res.end(JSON.stringify(METRICS)); }
  if (url === "/concurrency" || url === "/mcp") { res.writeHead(404, { "Content-Type": "application/json" }); return res.end('{"error":{"message":"no proxy"}}'); }
  if (url === "/health") { res.writeHead(200, { "Content-Type": "application/json" }); return res.end(JSON.stringify({ status: "ok", model: "m", images: true, max_context: 262144 })); }
  if (url === "/chats") { res.writeHead(200, { "Content-Type": "application/json" }); return res.end('{"chats":[],"activeId":null}'); }
  const name = url === "/" ? "/index.html" : url.replace("/web/", "/");
  const f = DIR + name;
  const ext = f.slice(f.lastIndexOf("."));
  if (!fs.existsSync(f)) { res.writeHead(404); return res.end("nope"); }
  res.writeHead(200, { "Content-Type": TYPES[ext] || "text/plain" });
  res.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(8124, r));

const browser = await puppeteer.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1000 });
await page.evaluateOnNewDocument(() => localStorage.setItem("strata.lang", JSON.stringify("zh")));
await page.goto("http://127.0.0.1:8124/", { waitUntil: "domcontentloaded", timeout: 60000 });
await page.click('.st-tab[data-view="monitor"]').catch(() => {});
await new Promise((r) => setTimeout(r, 4000));
const out = await page.evaluate(() => ({
  concHidden: !document.querySelector(".lane-key"),
  cards: [...document.querySelectorAll(".gpu-card")].map((e) => ({
    title: e.querySelector(".gpu-card__name").textContent.trim(),
    model: e.querySelector(".gpu-card__model").textContent.trim(),
    lines: [...e.querySelectorAll(".gpu-line")].map((x) => x.dataset.s + "=" +
            x.querySelector(".gpu-line__value").textContent.replace(/\s+/g, " ").trim()),
    sparks: [...e.querySelectorAll(".gpu-line__spark path.line")].filter((p) => p.getAttribute("d")).length,
  })),
  laneTiles: [...document.querySelectorAll(".gpu-lane__name")].map((e) => e.textContent.trim()),
  tempRow: [...document.querySelectorAll(".ctx-card .bar-row")].map((e) => e.textContent.replace(/\s+/g, " ").trim()).filter((x) => /温度/.test(x)),
  // 没有 /concurrency 时，「上下文占用」这块也得说清是谁的：署「本引擎」+ 它那两张卡
  ctxHead: (document.querySelector(".ctx-card .card-title") || {}).textContent?.replace(/\s+/g, " ").trim(),
  expertRow: [...document.querySelectorAll(".ctx-card .bar-row")].map((e) => e.textContent.replace(/\s+/g, " ").trim()).find((x) => /专家/.test(x)),
  metricCards: document.querySelectorAll(".metric-card").length,
}));
console.log(JSON.stringify(out, null, 1));
await browser.close();
srv.close();
