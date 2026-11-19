import puppeteer from "puppeteer";
const b = await puppeteer.launch({ args: ["--no-sandbox"] });
const p = await b.newPage();
const errs = [];
p.on("pageerror", (e) => errs.push(e.message));
await p.setViewport({ width: 1440, height: 1000 });
// 用本机 token 走真实推理这条路
const fsmod = (await import("fs")).default;
const cfg = JSON.parse((await import("fs")).default.readFileSync("/home/cloud/rag-proxy/config.json", "utf8"));
const key = (await import("fs")).default.readFileSync(cfg.api_key_file, "utf8").trim();
await p.goto("http://127.0.0.1:8092/#viz", { waitUntil: "networkidle2" });
await p.evaluate((k) => localStorage.setItem("strata.apikey", JSON.stringify(k)), key);
await p.reload({ waitUntil: "networkidle2" });
await new Promise((r) => setTimeout(r, 1800));
await p.click(".bar__go");
await p.waitForSelector(".viz[data-focus]", { timeout: 9000 });
await new Promise((r) => setTimeout(r, 1000));
await p.click(".bar__play");
// 等真实回答回来
for (let i = 0; i < 40; i++) {
  await new Promise((r) => setTimeout(r, 1000));
  const st = await p.evaluate(() => document.body.textContent.includes("模型回答") && !document.body.textContent.includes("正在本机跑"));
  if (st) break;
}
for (const [n, i] of [["sample", 198], ["loop", 199]]) {
  await p.$eval(".tl__range", (el, v) => { el.value = String(v); el.dispatchEvent(new Event("input", { bubbles: true })); }, i);
  await new Promise((r) => setTimeout(r, 1500));
  await p.evaluate(() => document.querySelectorAll(".st-toast").forEach((e) => e.remove()));
  await p.screenshot({ path: `/tmp/zz-${n}.png` });
}
console.log("ERR:", errs.slice(0, 2).join(" | ") || "none");
await b.close();
