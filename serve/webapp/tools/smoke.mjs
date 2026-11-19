// 渲染级冒烟测试：用一个"像引擎一样"的静态服务器 + 一套模拟 API，把构建产物放进真浏览器跑一遍。
// 覆盖：中文界面、三个标签页、并发区块（双通道聚合/分路）、聊天流式（思考 + 工具调用）、语言切换。
// 用法：node tools/smoke.mjs [构建产物目录]，默认 /tmp/strata-web-check
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import puppeteer from "puppeteer";

const ROOT = process.argv[2] || "/tmp/strata-web-check";
const PORT = 8123;
const SINGLE = !!process.env.SINGLE;      // SINGLE=1：模拟"只有一条通道"的部署，验证自动适配

const HEALTH = { status: "ok", model: "qwen3.8-flash-fast", images: true, max_context: 262144, loaded: true };
const METRICS = {
  engine: { model: "qwen3.8-flash-fast", max_context: 262144, images: true, version: "0.1.15",
            expert_slots: 11549, expert_cache_mib: 22000, kv: "int8", kv_resident: 32768, spec: 4, mtp_max: 5, cvec: 0 },
  live: { state: "generating", queued: 0, phase: null, prompt_tokens: 1200, generated: 300, max_tokens: 4096,
          tok_s: 42.5, tok_s_mean: 40.1, prefill_tok_s_mean: 320.0, elapsed_s: 12.3 },
  requests: [{ time: Date.now() / 1000 - 30, finish: "stop", prompt_tokens: 1200, reused: 900, output_tokens: 300,
               decode_tok_s: 41.2, hit_rate: 0.95, duration_s: 12.1, projection: false }],
  requests_kept: 1,
  totals: { requests: 12, prompt_tokens: 20000, reused: 15000, output_tokens: 3000, prompt_ms: 4000, decode_ms: 60000, since: Date.now() / 1000 - 3600 },
  hardware: { gpu_util: 88, gpu_mem_used: 23 * 2 ** 30, gpu_mem_total: 24 * 2 ** 30, gpu_temp: 72, gpu_power: 320,
              gpu_power_limit: 450, gpu_pcie_gen: 4, gpu_pcie_gen_max: 4, gpu_pcie_width: 16, gpu_pcie_rx_mb: 3.2,
              cpu: 45, ram_used: 100 * 2 ** 30, ram_total: 216 * 2 ** 30, disk_read_mb: 2.5, disk_write_mb: 0.1 },
  hardware_static: { gpu_name: "NVIDIA GeForce RTX 4090 D", gpu_count: 1, gpu_indices: [0],
                     cores: 16, threads: 32, cpu_name: "Xeon", psutil: true },
  history: { tok_s: [40, 41, 42, 43, 42], gpu_util: [80, 85, 88, 87, 88], gpu_mem_used: [2e10, 2.1e10, 2.2e10],
             gpu_temp: [70, 71, 72], gpu_power: [300, 310, 320], gpu_pcie_rx_mb: [1, 2, 3], cpu: [40, 45, 50],
             ram_used: [1e11, 1.01e11], disk_read_mb: [1, 2, 2.5], prefill_tok_s_mean: [300, 310, 320] },
  time: Date.now() / 1000,
};
const LANES = [
  { url: "http://127.0.0.1:8091", label: "快道", model: "qwen3.8-flash-fast", up: true, loaded: true, serving_slots: 4,
    in_flight: 1, engine_in_flight: 1, uptime_s: 400000,
    gpu: { name: "RTX 4090 D", used_mib: 23552, total_mib: 24564, util_pct: 91, temp_c: 71, power_w: 320 },
    // 新引擎：各通道把自己那张卡**带卡号**报上来（老引擎只有上面的 gpu，没号）
    gpus: [{ index: 0, name: "RTX 4090 D", used_mib: 23552, total_mib: 24564, util_pct: 91, temp_c: 71,
             power_w: 320, power_limit_w: 425, pcie_gen: 4, pcie_gen_max: 4, pcie_width: 16, pcie_rx_mb: 118 }],
    ram: { used_gib: 100, total_gib: 216 },
    live: { state: "generating", tok_s: 42.5, prompt_tokens: 6894, generated: 624 },
    engine: { model: "qwen3.8-flash-fast", max_context: 262144, expert_slots: 11549, expert_cache_mib: 22000 },
    totals: {}, requests: [], history: {}, error: null },
  { url: "http://127.0.0.1:8092", label: "普道", model: "qwen3.8-flash", up: true, loaded: true, serving_slots: 4,
    in_flight: 2, engine_in_flight: 2, uptime_s: 400000,
    gpu: { name: "RTX 4090 D", used_mib: 22000, total_mib: 24564, util_pct: 77, temp_c: 68, power_w: 290 },
    gpus: [{ index: 1, name: "RTX 4090 D", used_mib: 22000, total_mib: 24564, util_pct: 77, temp_c: 68,
             power_w: 290, power_limit_w: 425, pcie_gen: 1, pcie_gen_max: 4, pcie_width: 16, pcie_rx_mb: 0.4 }],
    ram: { used_gib: 100, total_gib: 216 },
    live: { state: "generating", tok_s: 31.0, prompt_tokens: 33098, generated: 324 },
    engine: { model: "qwen3.8-flash", max_context: 131072, expert_slots: 11549, expert_cache_mib: 12288 },
    totals: {}, requests: [], history: {}, error: null },
];
const GPUS = [
  { index: 0, name: "NVIDIA GeForce RTX 4090 D", used_mib: 23552, total_mib: 24564, util: 91, temp: 71, power: 210 },
  { index: 1, name: "NVIDIA GeForce RTX 4090 D", used_mib: 21000, total_mib: 24564, util: 55, temp: 61, power: 160 },
  { index: 2, name: "NVIDIA GeForce RTX 4090 D", used_mib: 11895, total_mib: 24564, util: 3, temp: 31, power: 25 },
];
const SPARK = { cards: [{ index: 0, name: "NVIDIA GB10 (统一内存)", used_mib: 74495, total_mib: 122880, util: 2, temp: 46, power: 11 }], up: true, error: null, ts: Date.now() / 1000 };
const SERVICES = [
  { id: "face", label: "人脸识别", kind: "face", group: "本机", up: true, calls: 4, errors: 0, in_flight: 0, latency_ms: 5.7 },
  { id: "embed", label: "bge-m3 向量", kind: "embedding", group: "邻居 pod", up: true, calls: 12, errors: 0, in_flight: 0, latency_ms: 5.1, gpu_alloc_mib: 1101 },
  { id: "tts", label: "Voice Studio", kind: "tts", group: "邻居 pod", up: true, calls: 1, errors: 0, in_flight: 0, latency_ms: 35.4 },
  { id: "spark-image", label: "Qwen-Image-2.1", kind: "image", group: "Spark", up: true, calls: null, latency_ms: 611 },
];
const CONCURRENCY = {
  time: Date.now() / 1000, mode: "multi", lanes: LANES, gpus: GPUS, spark: SPARK, services: SERVICES,
  aggregate: { lanes: 2, lanes_up: 2, in_flight: 3, generating: 3, est_tokens_out: 900, tok_s: 73.5 },
  in_flight: [
    { id: "r1", lane: LANES[0].url, lane_label: "快道", model: "qwen3.8-flash-fast", state: "streaming",
      started_at: Date.now() / 1000 - 5, first_chunk_at: Date.now() / 1000 - 4.7, elapsed_s: 5.0, ttft_s: 0.3,
      est_tokens: 120, tok_s: 24.0, bytes_out: 400, stream: true, path: "/v1/chat/completions", session: null, error: null },
    { id: "r2", lane: LANES[1].url, lane_label: "普道", model: "qwen3.8-flash", state: "streaming",
      started_at: Date.now() / 1000 - 9, first_chunk_at: Date.now() / 1000 - 8.6, elapsed_s: 9.0, ttft_s: 0.4,
      est_tokens: 300, tok_s: 33.0, bytes_out: 900, stream: true, path: "/v1/chat/completions", session: null, error: null },
    { id: "r3", lane: LANES[1].url, lane_label: "普道", model: "qwen3.8-flash", state: "queued",
      started_at: Date.now() / 1000 - 1, first_chunk_at: null, elapsed_s: 1.0, ttft_s: null,
      est_tokens: 0, tok_s: null, bytes_out: 0, stream: true, path: "/v1/messages", session: null, error: null },
  ],
  recent: Array.from({ length: 11 }, (_, i) => ({
    id: `r${i}`, lane: LANES[i % 2].url, lane_label: i % 2 ? "普道" : "快道",
    model: i % 2 ? "qwen3.8-flash" : "qwen3.8-flash-fast", state: i === 3 ? "error" : "completed",
    started_at: Date.now() / 1000 - 60 - i * 7, first_chunk_at: Date.now() / 1000 - 59 - i * 7,
    elapsed_s: 20.0 - i, ttft_s: 1.0, est_tokens: 800 - i * 20, tok_s: 40.0 - i, bytes_out: 2400,
    stream: true, path: "/v1/chat/completions", session: null, error: null,
  })),
  proxy: { uptime_s: 390000, counters: { passthrough: 120, assembled: 12, degraded: 1, block_rebuilds: 3, overfit_retries: 2 },
           sessions: 5, embed_queue: 0, embed_cache: 4211, metrics_lane: LANES[1].url },
};

const TYPES = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml" };

// 登录态的开关：默认「已登录」，让其余用例按正常用户走；用例末尾会翻成未登录来验闸门。
let LOGGED_OUT = false;
// 云端那份对话（夹具）。默认是空的 —— 前面的用例都按「干净的本机」起手；
// 「多端合并」那条用例会先用 /__cloud?seed=1 塞一条别的设备留下的会话进来。
const CLOUD_SEED = () => ({ chats: [{ id: "cloud-1", title: "另一台设备上的对话",
                                      createdAt: Date.now() - 86400000, updatedAt: Date.now() - 3600000,
                                      messages: [{ role: "user", text: "这台机器能跑多大的模型？" }] }],
                            activeId: null });
let CLOUD_CHATS = { chats: [], activeId: null, store: "local" };
let QUEUE_ONLY = false;      // /concurrency 报「有在途但没有在生成」——用来验证胶囊的「读取」态
let OLDGPU = false;          // 通道不下发 gpus[]（老引擎）：逐卡还在，但**卡号只能标「未上报」**

// ---- 推理记录夹具（/infer/*）
// 只有元信息：接口本来就不返回用户输入 / 思考 / 回复，所以夹具里也**不放**这些内容 ——
// 页面要是把它们画出来，断言会立刻发现（这是需求里明确的一条：内容存，但不在界面上露）。
let RAW_CALLS = 0;                            // 数一数页面有没有去取内容（应该永远是 0）
const NOW = Date.now() / 1000;
const INFER_ROWS = [
  { id: "20261016-181500-a1b2c3", ts: NOW - 300, iso: "2026-10-16 18:15:00",
    path: "/v1/chat/completions", model: "qwen3.8-flash", lane: "http://127.0.0.1:8092", lane_label: "普道",
    session: "auto:9edd27dd9210eaef", stream: 1, state: "completed", http_status: 200, error: null,
    mode: "assembled", prompt_tokens: 238412, cached_tokens: 230000, completion_tokens: 512,
    est_prompt_tokens: 240000, recent_tokens: 65536, memory_tokens: 150000, block_reused: 1,
    ttft_ms: 1840.2, total_ms: 12300.5, decode_tok_s: 41.6, prompt_tok_s: 320.4, retrieve_ms: 88,
    events: 512, bytes_out: 24000, client_bytes: 950000, sent_bytes: 780000, resp_bytes: 24000,
    finish_reason: "stop", content_chars: 1200, reasoning_chars: 340, tool_calls: 0,
    truncated: 0, retries: 0, ua: "Mozilla/5.0 (smoke)", seg: "seg-181500-9f3a2.jsonl.gz", line: 0, uploaded: 1,
    // 触发过 RAG 组装的那条：前后对照的计数与检索结果（正文不在这些字段里）
    rag_query_chars: 42, rag_pool: 73, rag_candidates: 73, rag_chunks: 12,
    rag_score_max: 0.83, rag_score_min: 0.41, rag_block_chars: 21000, rag_rerank: 1, rag_rebuild: 1,
    client_msgs: 40, sent_msgs: 42, client_chars: 950000, sent_chars: 780000, differs: 1 },
  { id: "20261016-181244-d4e5f6", ts: NOW - 1300, iso: "2026-10-16 18:12:44",
    path: "/v1/chat/completions", model: "qwen3.8-flash-fast", lane: "http://127.0.0.1:8091", lane_label: "快道",
    session: null, stream: 1, state: "error", http_status: 400, error: "HTTPError 400",
    mode: "est", prompt_tokens: 271000, cached_tokens: 12000, completion_tokens: null,
    est_prompt_tokens: 268000, recent_tokens: null, memory_tokens: null, block_reused: null,
    ttft_ms: null, total_ms: 220.4, decode_tok_s: null, prompt_tok_s: null, retrieve_ms: null,
    events: 0, bytes_out: 180, client_bytes: 1100000, sent_bytes: 1100000, resp_bytes: 180,
    finish_reason: null, content_chars: 0, reasoning_chars: 0, tool_calls: 0,
    truncated: 0, retries: 1, ua: "curl/8.5.0", seg: "seg-181244-77bd1.jsonl.gz", line: 3, uploaded: 0 },
  { id: "20261016-181001-c7d8e9", ts: NOW - 2200, iso: "2026-10-16 18:10:01",
    path: "/v1/chat/completions", model: "qwen3.8-flash", lane: "http://127.0.0.1:8092", lane_label: "普道",
    session: "auto:4961fc9e705ed838", stream: 0, state: "disconnected", http_status: 200, error: null,
    mode: "exact", prompt_tokens: 4200, cached_tokens: 4000, completion_tokens: 88,
    est_prompt_tokens: 4300, recent_tokens: null, memory_tokens: null, block_reused: null,
    ttft_ms: 320.0, total_ms: 3400.9, decode_tok_s: 28.4, prompt_tok_s: 210.0, retrieve_ms: null,
    events: 1, bytes_out: 1400, client_bytes: 12000, sent_bytes: 12000, resp_bytes: 1400,
    finish_reason: "stop", content_chars: 120, reasoning_chars: 0, tool_calls: 0,
    truncated: 0, retries: 0, ua: "Mozilla/5.0 (smoke)", seg: "seg-181001-1c2d3.jsonl.gz", line: 7, uploaded: 1 },
];
const INFER_STATS = {
  days: 7, total: 3, today: 3, today_out_tokens: 600, out_tokens: 600, in_tokens: 513612,
  cached_tokens: 246000, avg_tok_s: 35.0, avg_ttft_ms: 1080.1, busy_s: 15.9,
  by_model: [{ model: "qwen3.8-flash", n: 2, tok_s: 35.0, out_tok: 600 },
             { model: "qwen3.8-flash-fast", n: 1, tok_s: null, out_tok: 0 }],
  by_state: [{ state: "completed", n: 2 }, { state: "error", n: 1 }],
  hourly: [{ h: 1, n: 2, tok_s: 35.0 }, { h: 2, n: 1, tok_s: 41.6 }],
  store: { enabled: true, minio_ok: true, bucket: "qwen-3.8-flash", pending_upload: 1, segments: 2, uploaded: 4 },
};

const server = http.createServer((req, res) => {
  const url = req.url.split("?")[0];
  const json = (o) => { const b = Buffer.from(JSON.stringify(o)); res.writeHead(200, { "Content-Type": "application/json" }); res.end(b); };
  if (url === "/health") return json(HEALTH);
  if (url === "/metrics") return json(METRICS);
  if (url.startsWith("/__queue")) {              // 仅供测试：让 /concurrency 报「有在途、但没在生成」
    QUEUE_ONLY = req.url.includes("?1");
    return json({ queueOnly: QUEUE_ONLY });
  }
  if (url.startsWith("/__oldgpu")) {             // 仅供测试：把通道的 gpus[] 摘掉 = 还没上报卡号的老引擎
    OLDGPU = req.url.includes("?1");
    return json({ oldGpu: OLDGPU });
  }
  // 工作空间 / 工具：首页会读它（每个账号一个目录）。夹具给空数据即可。
  if (url === "/workspace") return json({ user: "test", root: "/tmp/ws", skills: [], mcp: [], images: [],
    lab: { dir: "llm-study-lab", python: "/tmp/py", gpu: "2", exists: true },
    kb: { count: 0, chunks: 0, docs: [] }, jobs: [] });
  if (url === "/workspace/envs") return json({ envs: [
    { id: "python", label: "Python（标准库）", note: "" },
    { id: "ds", label: "Python 数据科学", note: "" },
    { id: "lab", label: "LLM Lab（PyTorch）", note: "" },
    { id: "cpp", label: "C++17（g++）", note: "" },
  ] });
  if (url === "/workspace/search") return json({ q: "", hits: [] });
  // 这些端点可能收到 POST：先把请求体读掉，否则 keep-alive 下一条请求会卡住
  if (url === "/workspace/kb") { req.resume(); return json({ count: 0, chunks: 0, docs: [] }); }
  if (url === "/workspace/kb/search") { req.resume(); return json({ query: "", count: 0, results: [], reranked: false }); }
  if (url === "/workspace/jobs") { req.resume(); return json({ jobs: [] }); }
  if (url === "/workspace/job") { req.resume(); return json({ id: "jtest", status: "done", exit: 0, log: "ok\n", offset: 0, next_offset: 3 }); }
  if (url === "/tts/say") {
    req.resume();
    // 一个最小可播放的 WAV 头 + 静音；测的是「回答会自动朗读」这条链路
    const wav = Buffer.concat([Buffer.from("RIFF"), Buffer.alloc(60)]);
    res.writeHead(200, { "Content-Type": "audio/wav", "Content-Length": wav.length });
    return res.end(wav);
  }
  if (url === "/workspace/tree") return json({ user: "test", entries: [
    { path: "notes", dir: true, size: 0, mtime: 1 },
    { path: "notes/todo.md", dir: false, size: 42, mtime: 1 },
    { path: "readme.md", dir: false, size: 12, mtime: 1 },
    { path: "llm-study-lab", dir: true, size: 0, mtime: 1 },
    { path: "llm-study-lab/START_HERE.md", dir: false, size: 120, mtime: 2 },
  ] });
  if (url === "/workspace/file") return json({ path: "readme.md", text: "# hi" });
  if (url === "/tool/mcp/list") return json({ servers: [], tools: [] });
  if (url === "/tool/status") return json({ time: Date.now() / 1000, lanes: LANES, services: SERVICES, gpus: GPUS, spark: SPARK });
  if (url === "/concurrency") {
    if (QUEUE_ONLY) {
      return json({ ...CONCURRENCY, aggregate: { ...CONCURRENCY.aggregate, in_flight: 2, generating: 0, tok_s: 0 } });
    }
    // 单路部署：只暴露一条通道。proxy.metrics_lane 也要跟着指到那条存在的通道上 ——
    // 否则夹具自相矛盾（说 /metrics 来自一条不存在的通道），页面就注不出通道名了。
    const payload = SINGLE
      ? { ...CONCURRENCY, mode: "single", lanes: [LANES[0]],
          aggregate: { lanes: 1, lanes_up: 1, in_flight: 1, generating: 1, est_tokens_out: 120, tok_s: 42.5 },
          in_flight: CONCURRENCY.in_flight.slice(0, 1),
          proxy: { ...CONCURRENCY.proxy, metrics_lane: LANES[0].url } }
      : CONCURRENCY;
    // 老引擎只有 gpu（没卡号）：逐卡还得是两组，只是标题标「卡号未上报」，页面不许按数值猜是哪张
    return json(OLDGPU ? { ...payload, lanes: payload.lanes.map(({ gpus, ...rest }) => rest) } : payload);
  }
  if (url.startsWith("/__cloud")) {             // 仅供测试：给云端塞一条「别的设备」的会话
    if (req.url.includes("?seed=1")) CLOUD_CHATS = { ...CLOUD_SEED(), store: "local" };
    return json({ seeded: CLOUD_CHATS.chats.length });
  }
  if (url === "/chats") {                       // 对话同步（夹具版：存在内存里）
    if (LOGGED_OUT) {
      res.writeHead(401, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ error: { code: "NOT_LOGGED_IN", message: "对话同步需要登录" } }));
    }
    if (req.method === "POST") {
      let raw = "";
      req.on("data", (c) => { raw += c; });
      req.on("end", () => {
        try { CLOUD_CHATS = JSON.parse(raw || "{}"); } catch (e) { /* 忽略 */ }
        return json({ ok: true, store: "local", count: (CLOUD_CHATS.chats || []).length });
      });
      return;
    }
    // GET：夹具里预置一条「另一台设备」的会话，用来验合并
    return json(CLOUD_CHATS);
  }
  if (url === "/auth/me") {
    if (LOGGED_OUT) {
      res.writeHead(401, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ error: { code: "NOT_LOGGED_IN", message: "未登录" } }));
    }
    // 头像指向一个不存在的地址也没关系：加载失败时 <img> 自己塌掉，不影响断言
    return json({ user: { id: "561", name: "张三", email: "zhangsan@example.com", avatar: "" } });
  }
  if (url.startsWith("/infer/")) {              // 推理记录（夹具版）
    if (LOGGED_OUT) {
      res.writeHead(401, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ error: { code: "NOT_LOGGED_IN", message: "推理记录需要登录" } }));
    }
    if (url === "/infer/raw") {                 // 内容接口：**界面不该碰它**（只有错误分析时人手动取）
      RAW_CALLS++;
      return json({ _error: "夹具不提供内容" });
    }
    if (url === "/infer/stats") return json(INFER_STATS);
    if (url === "/infer/rag") {                 // RAG 前后对照的**元信息**（块明细，无正文）
      const id = new URL(req.url, "http://x").searchParams.get("id");
      const r = INFER_ROWS.find((x) => x.id === id);
      if (!r) { res.writeHead(404, { "Content-Type": "application/json" }); return res.end(JSON.stringify({ error: { message: "没有这条记录" } })); }
      return json({ id, mode: r.mode, meta: r, rag: {
        query_chars: 42, query_sha: "9f3a2c1b", pool: 73, candidates: 73, reranked: true,
        block_chars: 21000, budget: 200000, need_compact: false, block_cov: 0.81,
        chosen_detail: { chunks: [
          { idx: 12, turn: 3, turn_label: "第 3 轮", role: "tool", kind: "text", chars: 1200, tokens: 700,
            score: 0.83, cos: 0.61, rerank: 0.9, expanded: false },
          { idx: 31, turn: 9, turn_label: "第 9 轮", role: "user", kind: "text", chars: 800, tokens: 500,
            score: 0.41, cos: 0.5, rerank: 0.6, expanded: false },
          { idx: 32, turn: 9, turn_label: "第 9 轮(补齐)", role: "assistant", kind: "text", chars: 300,
            tokens: 180, score: null, expanded: true },
        ] } } });
    }
    if (url === "/infer/item") {
      const id = new URL(req.url, "http://x").searchParams.get("id");
      const r = INFER_ROWS.find((x) => x.id === id);
      if (!r) { res.writeHead(404, { "Content-Type": "application/json" }); return res.end(JSON.stringify({ error: { message: "没有这条记录" } })); }
      return json(r);
    }
    const q = new URL(req.url, "http://x").searchParams;
    const pick = (k, v) => !q.get(k) || String(v) === q.get(k);
    const rows = INFER_ROWS.filter((r) => pick("model", r.model) && pick("state", r.state)
      && pick("mode", r.mode) && (!q.get("stream") || String(r.stream) === q.get("stream")));
    const limit = Number(q.get("limit") || 20), offset = Number(q.get("offset") || 0);
    return json({ rows: rows.slice(offset, offset + limit), total: rows.length, limit, offset,
                  live: INFER_ROWS.slice(0, 2), source: "sqlite" });
  }
  if (url === "/auth/config") return json({ configured: true, app_name: "软工集市", app_icon: "" });
  if (url === "/auth/logout") return json({ ok: true });
  if (url.startsWith("/__loggedout")) {           // 仅供测试：切换假服务的登录态
    LOGGED_OUT = req.url.includes("?1");          // 注意用 req.url：上面的 url 已经把查询串剥了
    return json({ loggedOut: LOGGED_OUT });
  }
  if (url === "/mcp") return json({ servers: [], tools: 0 });
  if (url === "/settings") return json({ shared: false });
  if (url === "/web/train-data.js") {
    res.writeHead(200, { "Content-Type": "application/javascript; charset=utf-8" });
    return res.end("window.__TRAIN_METRICS__ = null;");
  }
  if (url === "/agent/run") {
    req.resume();
    return json({ run_id: "ar-smoke", status: "running" });
  }
  if (url === "/agent/stream") {
    res.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache" });
    const evs = [
      { seq: 1, type: "reasoning", content: "先想一下这个问题……", round: 1 },
      { seq: 2, type: "tool_call", id: "t1", name: "read_file", arguments: { path: "a.txt" }, round: 1 },
      { seq: 3, type: "tool_result", id: "t1", name: "read_file", ok: true, text: "文件内容", ms: 120, round: 1 },
      { seq: 4, type: "content", content: "你", round: 2 },
      { seq: 5, type: "content", content: "好", round: 2 },
      { seq: 6, type: "content", content: "，世界", round: 2 },
      { seq: 7, type: "usage", usage: { completion_tokens: 3 } },
      { seq: 8, type: "done", finish: "stop", usage: { completion_tokens: 3 }, round: 2 },
    ];
    for (const ev of evs) res.write(`data: ${JSON.stringify(ev)}\n\n`);
    return res.end();
  }
  if (url === "/v1/chat/completions") {
    // 演示页最后要真跑一次推理：非流式请求回一段普通 JSON
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("end", () => {
      let body = {};
      try { body = JSON.parse(raw || "{}"); } catch (e) { body = {}; }
      if (body.stream === false) {
        const long = "LLM 推理过程可视化：\n演示用的真实回答。"
          + "这一句是为了把回答撑长一点，好让画布上出现「还有 N 个字，完整回答见对话页」那一行，"
          + "测试会点它跳过去看这条会话。";
        const o = { choices: [{ message: { role: "assistant", content: long }, finish_reason: "stop" }],
                    usage: { completion_tokens: 48 } };
        const buf = Buffer.from(JSON.stringify(o));
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(buf);
      }
      // 流式（原样保留）
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      setTimeout(() => {
        const send = (o) => res.write(`data: ${JSON.stringify(o)}\n\n`);
        send({ choices: [{ delta: { reasoning_content: "先想一下这个问题……" } }] });
        send({ strata_mcp: { event: "call", id: "t1", name: "read_file", server: "本地文件", tool: "read",
                             arguments: { path: "a.txt" }, round: 1 } });
        send({ strata_mcp: { event: "result", id: "t1", text: "文件内容", ok: true, chars: 4, ms: 120 } });
        send({ choices: [{ delta: { content: "你" } }] });
        send({ choices: [{ delta: { content: "好" } }] });
        send({ choices: [{ delta: { content: "，世界" } }] });
        send({ usage: { completion_tokens: 3 } });
        res.write("data: [DONE]\n\n");
        res.end();
      }, 500);
    });
    return;
  }
  // 静态：按引擎的规则（/ → index.html，/web/<扁平名>，/fonts/<名>）
  let file = null;
  if (url === "/" || url === "") file = path.join(ROOT, "index.html");
  else if (url.startsWith("/web/")) { const name = url.slice(5); if (!name.includes("/") && TYPES[path.extname(name)]) file = path.join(ROOT, name); }
  else if (url.startsWith("/fonts/")) { const name = url.slice(7); if (!name.includes("/")) file = path.join(ROOT, "fonts", name); }
  if (file && fs.existsSync(file)) {
    const ext = path.extname(file);
    res.writeHead(200, { "Content-Type": ext === ".woff2" ? "font/woff2" : (TYPES[ext] || "application/octet-stream") });
    return res.end(fs.readFileSync(file));
  }
  res.writeHead(404, { "Content-Type": "application/json" });
  res.end("{}");
});

const FAILS = [];
const ck = (name, ok, detail = "") => {
  console.log((ok ? "  PASS  " : "  FAIL  ") + name + (detail ? `  ${detail}` : ""));
  if (!ok) FAILS.push(name);
};

// 推理可视化那一页要 WebGL。无头环境没有 GPU，靠 SwiftShader 软件光栅化顶上；
// --enable-unsafe-swiftshader 是较新 Chrome 放行软件 WebGL 的开关（不加会直接拿不到上下文）。
const browser = await puppeteer.launch({
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--use-gl=angle", "--use-angle=swiftshader",
         "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage();
const waitPopup = () => new Promise((resolve) => page.once("popup", resolve));
await page.setViewport({ width: 1440, height: 1000 });
// 数一下往 /v1/chat/completions 发了几次请求：一次是聊天用例自己发的，
// 多出来的就只可能是「空闲生成建议」那条链路漏发了。
let suggestCalls = 0;
const consoleErrors = [];
// 这些 404 是**设计内的**，不算错：
//  · favicon.ico：原版页面同样没有图标，浏览器照样会去要一次；
//  · /api/models*：推理可视化预留的三个后端接口，现在还不存在，404 正是「回落到 mock」的触发条件。
const EXPECTED_404 = /favicon\.ico|\/api\/models/;
page.on("console", (msg) => {
  const where = (msg.location() && msg.location().url) || "";
  if (msg.type() === "error" && !EXPECTED_404.test(where) && !EXPECTED_404.test(msg.text())) {
    consoleErrors.push(`${msg.text()} @ ${where}`);
  }
});
page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${e.message}`));
page.on("request", (req) => {
  if (req.method() !== "POST" || !/\/v1\/chat\/completions$/.test(req.url())) return;
  // 只数「建议问题」那种空转请求（它的提示词是固定的那一段）；
  // 对话测试与可视化演示各会真的发一次推理，那是应该发的，不算「偷偷调用」。
  if ((req.postData() || "").includes("你只输出问题本身")) suggestCalls += 1;
});

await new Promise((r) => server.listen(PORT, "127.0.0.1", r));
try {
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: "networkidle2" });

  // ---- 中文界面 + 页头
  const brand = await page.$eval(".st-header__brand", (e) => e.textContent.trim());
  ck("页头品牌渲染", brand === "SSEStraBed", brand);
  const logo = await page.$eval(".st-logo", (e) => ({ src: e.getAttribute("src"), w: e.naturalWidth, h: e.naturalHeight }));
  ck("品牌 logo 加载成功", logo.w > 0 && logo.h > 0 && logo.src === "/web/logo.svg", JSON.stringify(logo));
  const icon = await page.$eval('link[rel="icon"]', (e) => e.getAttribute("href")).catch(() => null);
  ck("站点图标指向 logo.svg", icon === "/web/logo.svg", String(icon));
  const tabs = await page.$$eval(".st-tab", (els) => els.map((e) => e.textContent.trim()));
  // 现在的信息架构：打开就是对话页，LLM Lab 并进训练页，没有单独首页
  ck("标签页中文化", JSON.stringify(tabs) === JSON.stringify(["对话", "监控", "记录", "推理可视化", "训练", "关于"]), JSON.stringify(tabs));
  ck("默认页是对话页", await page.$(".st-composer textarea") !== null, "");
  // 训练页 = LLM Lab（文件树 + agent）+ 监控
  await page.goto(`http://127.0.0.1:${PORT}/#train`, { waitUntil: "networkidle2" });
  await page.waitForSelector(".ws__files", { timeout: 8000 });
  const shell = await page.$$eval(".ws__files, .agent", (els) => els.length);
  ck("训练页包含 LLM Lab（文件树 + agent 面板）", shell === 2, String(shell));
  // 对话页从导航进入 —— 下面这些断言针对它。
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.waitForSelector(".chat-empty__title", { timeout: 5000 });
  const empty = await page.$eval(".chat-empty__title", (e) => e.textContent);
  ck("对话页中文空态", empty === "新对话", empty);
  const chips = () => page.$$eval(".chat-suggest:not(.chat-suggest--more) .chat-suggest__text",
                                  (els) => els.map((e) => e.textContent.trim()));
  const suggests = await chips();
  ck("空态给出三条建议问题", suggests.length === 3 && suggests.every((c) => c.length >= 4), JSON.stringify(suggests));
  ck("有「换一批」按钮", await page.$(".chat-suggest--more") !== null, "");
  // 池子里有 10 条、每次抽 3 条，连抽几次总该变一次（每次都撞上同一组的概率可以忽略）
  let picked = suggests.join("|"), changed = false;
  for (let i = 0; i < 4 && !changed; i++) {
    await page.click(".chat-suggest--more");
    await new Promise((r) => setTimeout(r, 200));
    const now = (await chips()).join("|");
    if (now !== picked) changed = true;
    picked = now;
  }
  ck("「换一批」会换掉建议问题", changed, picked);
  ck("语言切换按钮存在", await page.$(".st-btn--lang") !== null, "");

  // ---- 聊天：发一条，验证流式（思考 / 工具块 / 正文 / meta）
  await page.type(".st-composer textarea", "你好");
  await page.click(".st-btn--send");
  await page.waitForFunction(() => {
    const b = document.querySelector(".st-msg--assistant .st-bubble");
    return b && b.textContent.includes("世界");
  }, { timeout: 8000 });
  // 胶囊报的是**整台机器**的聚合：夹具里单条通道是 42.5 tok/s、聚合是 73.5 ——
  // 显示 73.5 才说明真的走了聚合口径，而不是随便挑了某一条通道。
  const expectAgg = SINGLE ? "42.5" : "73.5";
  await page.waitForFunction((v) => document.querySelector(".st-pill")?.textContent.includes(v), { timeout: 6000 }, expectAgg);
  ck(`胶囊显示的是聚合速率（${expectAgg}，不是某条通道的瞬时值）`, true,
     await page.$eval(".st-pill", (e) => e.textContent.trim()));
  const bubble = await page.$eval(".st-msg--assistant .st-bubble", (e) => e.textContent);
  ck("流式正文渲染", bubble.includes("你好，世界"), bubble.slice(0, 40));
  const tool = await page.$eval(".st-msg--assistant .tool-call", (e) => ({
    name: e.querySelector(".tool-call__name").textContent, badge: e.querySelector(".st-badge").textContent, state: e.dataset.state,
  })).catch(() => null);
  ck("工具调用块渲染并完结", !!tool && tool.state === "done" && tool.name === "read_file", JSON.stringify(tool));
  const think = await page.$eval(".st-msg--assistant .think .think-title", (e) => e.textContent).catch(() => null);
  ck("思考块有标题", !!think, String(think));
  // 点开思考卡片必须能看到思考正文（曾经因为 thinkOpen 不同步，展开后是空的）
  if (await page.$eval(".st-msg--assistant .think", (e) => e.open)) {
    await page.click(".st-msg--assistant .think summary");
    await new Promise((r) => setTimeout(r, 120));
  }
  await page.click(".st-msg--assistant .think summary");
  await new Promise((r) => setTimeout(r, 160));
  const thinkBody = await page.evaluate(() => {
    const d = document.querySelector(".st-msg--assistant .think");
    const b = d && d.querySelector(".st-collapse__body");
    return { open: !!(d && d.open), text: b ? b.textContent : "", h: b ? Math.round(b.getBoundingClientRect().height) : 0 };
  });
  ck("展开思考卡片能看到思考正文", thinkBody.open && thinkBody.text.includes("先想一下") && thinkBody.h > 0,
     JSON.stringify(thinkBody).slice(0, 120));
  const meta = await page.$eval(".st-msg--assistant .meta-text", (e) => e.textContent);
  ck("回答 meta 含 tokens", /tokens/.test(meta), meta);

  // ---- 左侧栏：多会话（新建 / 切换 / 删除 / 撤销 / 落盘）
  // 去掉空会话，后面的断言只针对「你好」这一条，避免用例之间互相干扰。
  await page.evaluate(() => {
    const list = JSON.parse(localStorage.getItem("strata.chats") || "[]");
    const keep = list.filter((c) => (c.messages || []).some((m) => m.text === "你好"));
    localStorage.setItem("strata.chats", JSON.stringify(keep));
    if (keep[0]) localStorage.setItem("strata.chatActive", JSON.stringify(keep[0].id));
  });
  await page.reload({ waitUntil: "networkidle2" });
  await page.waitForSelector(".side__conv", { timeout: 8000 });
  const convs = await page.$$eval(".side__conv-title", (els) => els.map((e) => e.textContent.trim()));
  const baseCount = convs.length;
  ck("会话按第一句话命名并出现在左侧栏", convs.includes("你好"), JSON.stringify(convs));
  const groupLabel = await page.$eval(".side__group", (e) => e.textContent.trim());
  ck("会话按日期分组（今天）", groupLabel === "今天", groupLabel);

  await page.click(".side__new");
  await page.waitForFunction((n) => document.querySelectorAll(".side__conv").length === n + 1, { timeout: 3000 }, baseCount);
  ck("「新对话」开出一条新会话", true, "");
  ck("新会话是空态", await page.$(".chat-empty__title") !== null, "");

  // 切回上一条：消息应当还在
  await page.evaluate(() => {
    const el = [...document.querySelectorAll(".side__conv")].find((e) => e.textContent.includes("你好"));
    if (el) el.click();
  });
  await page.waitForFunction(() => document.querySelector(".st-msg--user"), { timeout: 3000 });
  const back = await page.$eval(".st-msg--user .st-bubble", (e) => e.textContent);
  ck("切回旧会话能拿回原来的消息", back.includes("你好"), back.slice(0, 20));

  // 删除 + 撤销
  await page.hover(".side__conv");
  await page.click(".side__conv .side__conv-del");
  await page.waitForFunction((n) => document.querySelectorAll(".side__conv").length === n - 1, { timeout: 3000 }, baseCount + 1);
  ck("删除后左侧栏少一条", true, "");
  const undoText = await page.$eval(".st-toast", (e) => e.textContent).catch(() => "");
  ck("删除给了「撤销」入口", /撤销/.test(undoText), undoText.slice(0, 24));
  await page.click(".st-toast .st-btn");
  await page.waitForFunction((n) => document.querySelectorAll(".side__conv").length === n, { timeout: 3000 }, baseCount + 1);
  ck("撤销把会话找回来", true, "");

  // 刷新：会话要还在（落盘生效）
  await page.reload({ waitUntil: "networkidle2" });
  await page.waitForSelector(".side__conv", { timeout: 5000 });
  const afterReload = await page.$$eval(".side__conv-title", (els) => els.map((e) => e.textContent.trim()));
  ck("刷新后会话仍在（已落盘）", afterReload.length >= 2 && afterReload.includes("你好"), JSON.stringify(afterReload));

  // ---- 页头在切标签时**一个像素都不能动**
  // 这是踩过的坑：会话栏曾经放在页头左边、且只在对话页存在，一切标签整条页头就横向窜。
  const geom = () => page.evaluate(() => {
    const b = document.querySelector(".st-header__brand").getBoundingClientRect();
    const t = document.querySelector(".st-tabs").getBoundingClientRect();
    return { brandX: Math.round(b.x), tabsX: Math.round(t.x), side: !!document.querySelector(".side") };
  });
  const chatGeom = await geom();
  await page.click('.st-tab[data-view="monitor"]');
  await new Promise((r) => setTimeout(r, 500));
  const monGeom = await geom();
  ck("对话页有会话栏", chatGeom.side, "");
  ck("监控页没有会话栏", !monGeom.side, "");
  ck("切标签时品牌不动", chatGeom.brandX === monGeom.brandX, `${chatGeom.brandX} → ${monGeom.brandX}`);
  ck("切标签时标签栏不动", chatGeom.tabsX === monGeom.tabsX, `${chatGeom.tabsX} → ${monGeom.tabsX}`);
  ck("品牌贴在窗口最左上", monGeom.brandX < 40, String(monGeom.brandX));

  // 会话栏自己的收起 / 展开（控件在会话栏里，不在页头）
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.waitForSelector(".side__collapse", { timeout: 5000 });
  await page.click(".side__collapse");
  // 宽度有 140ms 过渡：直接量会量到动画中间（曾经量出 102px）
  await page.waitForFunction(() => {
    const e = document.querySelector(".side");
    return e && Math.round(e.getBoundingClientRect().width) <= 48;
  }, { timeout: 3000 });
  const railW = await page.$eval(".side", (e) => Math.round(e.getBoundingClientRect().width));
  ck("会话栏能收起成窄轨", railW <= 48, `${railW}px`);
  ck("收起会话栏也不动页头", (await geom()).brandX === monGeom.brandX, "");
  await page.click(".side__expand");
  await page.waitForSelector(".side__conv", { timeout: 3000 });
  ck("再点一下展开回来", true, "");

  // ---- 监控页：单引擎卡片
  await page.click('.st-tab[data-view="monitor"]');
  await page.waitForSelector(".lane-key__row", { timeout: 5000 });
  // GPU 那五张（负载/显存/温度/功耗/PCIe）2026-11-02 起搬进「GPU 详细数据」按卡显示，
  // 指标卡这里只剩 速度(+预填) / CPU / 磁盘 —— 它们不是每张卡一份的量。
  const cards = await page.$$eval(".metric-card", (els) => els.length);
  ck("指标卡只剩非 GPU 的三张", cards === 3, String(cards));
  const gauge = await page.$(".st-gauge__fill");
  ck("上下文仪表存在", !!gauge, "");

  // ---- 并发区块：通道图例 + 两张趋势折线图 + 分页表
  // （原来是「聚合/分路切换 + 每条通道一张卡」，已按需求换成折线图；通道身份改由图例承担）
  const laneNames = await page.$$eval(".lane-key__name", (els) => els.map((e) => e.textContent.trim()));
  if (SINGLE) {
    ck("单路：只列出一条通道", laneNames.length === 1 && laneNames[0] === "快道", JSON.stringify(laneNames));
    const sumText1 = await page.$eval(".conc-sum", (e) => e.textContent);
    ck("单路：汇总数字跟着只有一条通道", /42\.5/.test(sumText1), sumText1.replace(/\s+/g, " ").slice(0, 50));
  } else {
    ck("多路：图例列出两条通道", laneNames.includes("快道") && laneNames.includes("普道"), JSON.stringify(laneNames));
    const headText = await page.$eval(".conc-head", (e) => e.textContent);
    ck("汇总条标注通道在线数", /2\/2/.test(headText), headText.slice(0, 40));
    const sumText = await page.$eval(".conc-sum", (e) => e.textContent);
    ck("汇总数字来自聚合字段", /73\.5/.test(sumText), sumText.replace(/\s+/g, " ").slice(0, 60));
  }

  const keyText = await page.$eval(".lane-key__row", (e) => e.textContent);
  ck("通道图例显存按 MiB 换算正确", /23\.0 \/ 24 GB/.test(keyText), keyText.replace(/\s+/g, " ").slice(0, 70));
  const swatch = await page.$eval(".lane-key__swatch", (e) => getComputedStyle(e).backgroundColor);
  ck("图例色块用上了图表分类色", /rgb\(/.test(swatch) && swatch !== "rgba(0, 0, 0, 0)", swatch);

  // 折线图：等采样点攒够（代理每 2 秒轮询一次）
  await page.waitForFunction(() => document.querySelectorAll(".conc-charts .lc__line").length >= 2, { timeout: 20000 });
  const chartSvgs = await page.$$eval(".conc-charts .lc svg", (els) => els.length);
  ck("两张趋势图，各自一根 y 轴（速度 / 在途）", chartSvgs === 2, String(chartSvgs));
  // x 轴的时间戳不许互相压叠（这条在**刚攒出两个采样点**时最灵 —— 老逻辑会在那一刻把三个刻度
  // 画成 [0,0,1]，同一个 x 上叠出「03:583:53:52」这种乱码；纯函数那套判据见 tools/test-axis.mjs）
  const tickCheck = async (where) => page.evaluate(() => {
    const bad = [];
    let charts = 0;
    for (const lc of document.querySelectorAll(".lc")) {
      charts++;
      const bs = [...lc.querySelectorAll(".lc__xtick")].map((e) => e.getBoundingClientRect());
      for (let a = 0; a < bs.length; a++) for (let b = a + 1; b < bs.length; b++) {
        const ow = Math.min(bs[a].right, bs[b].right) - Math.max(bs[a].left, bs[b].left);
        const oh = Math.min(bs[a].bottom, bs[b].bottom) - Math.max(bs[a].top, bs[b].top);
        if (ow > 1 && oh > 1) bad.push(`${where}:${a}×${b}@${ow.toFixed(1)}px`);
      }
    }
    return { charts, bad };
  });
  const tick0 = await tickCheck("桌面");
  ck("折线图 x 轴时间戳互不压叠", tick0.charts > 0 && !tick0.bad.length, JSON.stringify(tick0));
  const strokeColors = await page.$$eval(".conc-charts .lc__line", (els) =>
    [...new Set(els.map((e) => e.getAttribute("stroke")))]);
  ck(SINGLE ? "单路：折线用第一个分类色" : "每个通道一条线且颜色不同",
     SINGLE ? strokeColors.length === 1 : strokeColors.length >= 2, JSON.stringify(strokeColors));
  const zeroAxis = await page.$$eval(".conc-charts .lc__grid line", (els) => els.length > 0);
  ck("折线图有网格与刻度", zeroAxis, "");

  // hover：十字线 + tooltip
  const cbox = await page.$eval(".conc-charts .lc", (el) => {
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  });
  await page.mouse.move(cbox.x + cbox.w * 0.6, cbox.y + cbox.h * 0.5);
  await page.waitForSelector(".lc__tip", { timeout: 4000 });
  const tipText = await page.$eval(".lc__tip", (e) => e.textContent);
  ck("折线图 hover 出 tooltip（时间 + 各通道读数）", /快道/.test(tipText), tipText.replace(/\s+/g, " ").slice(0, 40));
  await page.mouse.move(cbox.x - 20, cbox.y - 20);

  // ---- 逐卡 GPU（数据源 = /concurrency 的 lanes[].gpus：各通道把自己**那张带卡号的卡**报上来；
  //      历史由本地 2s 采样攒出来）
  // 这一块存在的理由：/metrics 走代理的会话粘性路由 → 只有一张卡、换道时曲线还会串卡；
  // 而卡号（GPU 几）只有坐在卡上的进程知道，两张同型号卡的数值本身分不出彼此。
  await page.waitForFunction(() => document.querySelectorAll(".gpu-charts .lc__line").length >= 4, { timeout: 20000 });
  const gpuNames = await page.$$eval(".gpu-lane__name", (els) => els.map((e) => e.textContent.trim()));
  ck("逐卡：本机三张卡 + Spark 一张，标题写的是**卡号**",
     JSON.stringify(gpuNames) === JSON.stringify(["GPU 0", "GPU 1", "GPU 2", "GPU 0"]), JSON.stringify(gpuNames));
  const gpuLaneNames = await page.$$eval(".gpu-lane__lane", (els) => els.map((e) => e.textContent.trim()));
  ck("逐卡：每组署上归属（本项目两条道各一张 / 邻居共用 / Spark）",
     gpuLaneNames.length === 4 && /快道/.test(gpuLaneNames[0]) && /普道/.test(gpuLaneNames[1])
       && gpuLaneNames.slice(2).join(" ").length > 0, JSON.stringify(gpuLaneNames));
  const gpuMetrics = await page.$$eval(".gpu-metric", (els) => els.length);
  ck("逐卡：每卡四格（负载 / 显存 / 温度 / 功耗）", gpuMetrics === 16, String(gpuMetrics));
  const gpuUtilTxt = await page.$eval('.gpu-metric[data-s="util"]', (e) => e.textContent.replace(/\s+/g, " "));
  ck("逐卡：负载读数取自该通道上报（快道 91%）", /91\s*%/.test(gpuUtilTxt), gpuUtilTxt.slice(0, 40));
  const gpuVramTxt = await page.$eval('.gpu-metric[data-s="vram"]', (e) => e.textContent.replace(/\s+/g, " "));
  ck("逐卡：显存 MiB→GB 换算并带上卡容量", /23\.0/.test(gpuVramTxt) && /\/ 24 GB/.test(gpuVramTxt), gpuVramTxt.slice(0, 40));
  const gpuTempTxt = await page.$eval('.gpu-metric[data-s="temp"]', (e) => e.textContent.replace(/\s+/g, " "));
  ck("逐卡：温度/功耗两格不是空的", /71/.test(gpuTempTxt), gpuTempTxt.slice(0, 40));
  const gpuChartSvgs = await page.$$eval(".gpu-charts .lc svg", (els) => els.length);
  ck("逐卡：四张图各一根 y 轴（%、GB、°C、W 不叠轴）", gpuChartSvgs === 4, String(gpuChartSvgs));
  // 布局：固定两列（2×2）。原来是 auto-fit minmax(320px,1fr)，1440 下排成 3+1 —— 最后一张孤零零一行、
  // 宽度还和上面两张对不齐（截图实测）。这条断言就是钉住那次改版的。
  const gpuGrid = await page.evaluate(() => {
    const g = document.querySelector(".gpu-charts");
    const cols = getComputedStyle(g).gridTemplateColumns.split(" ").length;
    const tops = [...g.querySelectorAll(".chart-card")].map((e) => Math.round(e.getBoundingClientRect().top));
    return { cols, rows: new Set(tops).size, widths: [...new Set([...g.querySelectorAll(".chart-card")].map((e) => Math.round(e.getBoundingClientRect().width)))].length };
  });
  ck("逐卡四图排成 2×2（不是 3+1 孤儿行，且两两等宽）", gpuGrid.cols === 2 && gpuGrid.rows === 2 && gpuGrid.widths === 1,
     JSON.stringify(gpuGrid));
  const gpuStrokes = await page.$$eval(".gpu-charts .lc__line", (els) => [...new Set(els.map((e) => e.getAttribute("stroke")))]);
  ck(SINGLE ? "逐卡：单路时只有一条线色" : "逐卡：一条线一张卡且两色不同",
     gpuStrokes.length === (SINGLE ? 1 : 2), JSON.stringify(gpuStrokes));
  const gpuCardNames = await page.$$eval(".gpu-charts .lc__tip", (els) => els.length);   // tooltip 未 hover，应为 0
  ck("逐卡图默认不挂 tooltip", gpuCardNames === 0, String(gpuCardNames));
  // 逐卡图在监控页更靠下：不先滚进视口，getBoundingClientRect 拿到的是屏幕外坐标，hover 会点空
  await page.evaluate(() => document.querySelector(".gpu-charts .lc")?.scrollIntoView({ block: "center" }));
  const gbox = await page.$eval(".gpu-charts .lc", (el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
  await page.mouse.move(gbox.x + gbox.w * 0.5, gbox.y + gbox.h * 0.5);
  await page.waitForSelector(".gpu-charts .lc__tip", { timeout: 4000 });
  const gtip = (await page.$eval(".gpu-charts .lc__tip", (e) => e.textContent.replace(/\s+/g, " ")));
  ck("逐卡图 hover 按**卡号**读出两张卡的数（能横向比）",
     SINGLE ? /GPU 0/.test(gtip) : /GPU 0/.test(gtip) && /GPU 1/.test(gtip), gtip.slice(0, 60));
  await page.mouse.move(gbox.x - 20, gbox.y - 20);
  // 单元名要能对上颜色：色块与对应折线同色
  const gpuSwatch = await page.$eval(".gpu-lane__swatch", (e) => getComputedStyle(e).backgroundColor);
  ck("逐卡：卡头色块与折线用同一套通道色", /rgb\(/.test(gpuSwatch) && gpuSwatch !== "rgba(0, 0, 0, 0)", gpuSwatch);
  const metricCardsStill = await page.$$eval(".metric-card", (els) => els.length);
  ck("逐卡区块不蹭 .metric-card（指标卡仍是那三张）", metricCardsStill === 3, String(metricCardsStill));

  // ---- GPU 详细数据（底部，一张卡一组）：这里原来是「GPU 负载/显存/温度/功耗/PCIe」五张指标卡，
  //      读的是被粘性钉在一条通道上的 /metrics → 只有一张卡的数、还没署名。现在按卡拆开、各带曲线。
  const gpuDetail = await page.$$eval(".gpu-card", (els) => els.map((e) => ({
    title: (e.querySelector(".gpu-card__name") || {}).textContent?.trim(),
    who: (e.querySelector(".gpu-card__who") || {}).textContent?.replace(/\s+/g, " ").trim(),
    lines: [...e.querySelectorAll(".gpu-line")].map((x) => [x.dataset.s,
            x.querySelector(".gpu-line__value").textContent.replace(/\s+/g, " ").trim(),
            (x.querySelector(".gpu-line__sub") || {}).textContent?.trim() || ""]),
    sparks: e.querySelectorAll(".gpu-line__spark").length,
  })));
  ck("详细数据：一张卡一组", gpuDetail.length === (SINGLE ? 1 : 2), JSON.stringify(gpuDetail.map((d) => d.title)));
  ck("详细数据：每组五行（负载 / 显存 / 温度 / 功耗 / PCIe）",
     gpuDetail.every((d) => d.lines.map((x) => x[0]).join(",") === "util,vram,temp,power,pcie"),
     JSON.stringify(gpuDetail[0] && gpuDetail[0].lines.map((x) => x[0])));
  ck("详细数据：每行一根该卡自己的曲线", gpuDetail.every((d) => d.sparks === 5),
     JSON.stringify(gpuDetail.map((d) => d.sparks)));
  ck("详细数据：标题是卡号、副标说清是哪条道在用",
     gpuDetail.every((d, i) => d.title === `GPU ${SINGLE ? 0 : i}` && /在用/.test(d.who)),
     JSON.stringify(gpuDetail.map((d) => `${d.title}|${d.who}`)));
  const gd0 = gpuDetail[0] && gpuDetail[0].lines;
  ck("详细数据：快道那张卡的读数来自它自己的上报（91% / 23.0 GB / 71°C）",
     !!gd0 && /^91\s*%$/.test(gd0[0][1]) && /23\.0/.test(gd0[1][1]) && /^71\s*°C$/.test(gd0[2][1]), JSON.stringify(gd0));
  ck("详细数据：功耗带上这张卡的瓦数上限", !!gd0 && /425/.test(gd0[3][2]), JSON.stringify(gd0 && gd0[3]));
  const gd1 = gpuDetail[1] && gpuDetail[1].lines;
  ck(SINGLE ? "详细数据：PCIe 一格显链路档位（这条卡没降速）" : "详细数据：PCIe 一格能看出链路降速（普道那张卡在 idle Gen1）",
     SINGLE ? !/idle Gen/.test(gd0[4][2])
            : (!!gd1 && gd1[4][1].startsWith("Gen4x16") && /idle Gen1/.test(gd1[4][2]) && !/idle Gen/.test(gd0[4][2])),
     JSON.stringify([gd1 && gd1[4], gd0 && gd0[4]]));
  // 上下文占用里的温度行也跟着按卡列（原来只有一个数，还是粘性那条道的）
  const tempRow = await page.$$eval(".ctx-card .bar-row", (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()));
  ck("上下文占用：温度按卡列", /GPU 0 71 °C/.test(tempRow.join(" | ")) && (SINGLE || /GPU 1 68 °C/.test(tempRow.join(" | "))),
     JSON.stringify(tempRow.filter((x) => /°C/.test(x))));
  // 上下文占用现在是**一条道一张卡**（各带自己的仪表）：快道/普道各一张 + 系统一张。
  // 这样「两条道只在页面上显示一条」的问题从结构上就不存在了。
  const ctxCards = await page.$$eval(".ctx-row .ctx-card", (els) => els.map((e) => ({
    name: (e.querySelector(".card-title") || {}).textContent?.replace(/\s+/g, " ").trim() || "",
    pct: (e.querySelector(".ctx-gauge__pct") || {}).textContent?.trim() || "",
    val: (e.querySelector(".ctx-gauge__label .muted") || {}).textContent?.replace(/\s+/g, " ").trim() || "",
    bars: [...e.querySelectorAll(".bar-row")].map((b) => b.textContent.replace(/\s+/g, " ").trim()),
  })));
  const laneCards = ctxCards.filter((c) => c.pct);
  if (SINGLE) {
    ck("上下文占用：单条道只有它自己一张卡", laneCards.length === 1, JSON.stringify(laneCards.map((c) => c.name)));
  } else {
    ck("上下文占用：快道、普道各一张卡（不再挤在同一张卡里）",
       laneCards.length === 2 && laneCards[0].name.includes("快道") && laneCards[1].name.includes("普道"),
       JSON.stringify(laneCards.map((c) => c.name)));
    ck("上下文占用：每张卡读的是**它自己那条道**的窗口",
       laneCards[0].pct === "3%" && laneCards[0].val.includes("256K")
         && laneCards[1].pct === "25%" && laneCards[1].val.includes("128K"), JSON.stringify(laneCards));
    ck("上下文占用：常驻专家按道写，分母是那张卡自己的容量",
       laneCards.some((c) => c.bars.some((x) => /21\.5 \/ 24 GB/.test(x)))
         && laneCards.some((c) => c.bars.some((x) => /12\.0 \/ 24 GB/.test(x))), JSON.stringify(laneCards.map((c) => c.bars)));
  }
  ck("上下文占用：系统内存与温度单独一张卡", ctxCards.some((c) => c.name.includes("系统")),
     JSON.stringify(ctxCards.map((c) => c.name)));
  // 顶部「逐卡 GPU」现在由**网关直接读整机 nvidia-smi**（/concurrency.gpus），不依赖引擎上报卡号：
  // 老引擎（不上报卡号）也不会让这一块少卡 —— 这正是它比原来「按道拼卡」更稳的地方。
  // 每条道自己的卡号归属仍由 /concurrency 的 lanes[].gpus 提供，缺了就显示「卡号未上报」，不猜。
  await page.evaluate(() => fetch("/__oldgpu?1"));
  await new Promise((r) => setTimeout(r, 2500));
  const hostGpu = await page.$$eval(".gpu-lane__name", (els) => els.map((e) => e.textContent.trim()));
  ck("老引擎不上报卡号时，整机四张卡照样按卡号显示",
     JSON.stringify(hostGpu) === JSON.stringify(["GPU 0", "GPU 1", "GPU 2", "GPU 0"]), JSON.stringify(hostGpu));
  await page.evaluate(() => fetch("/__oldgpu"));
  await new Promise((r) => setTimeout(r, 1200));

  // 分页：多路下 3 条在途 + 11 条已结束 = 14 条，单路下只有 1 条在途 = 12 条；每页 8 条 → 都是 2 页
  const expectTotal = SINGLE ? 12 : 14;
  const expectPage2 = expectTotal - 8;
  await page.waitForSelector(".conc-req .pager", { timeout: 4000 });
  const pagerText = await page.$eval(".conc-req .pager", (e) => e.textContent);
  ck("在途表出现分页条并给出总数", new RegExp(`共 ${expectTotal} 条`).test(pagerText),
     pagerText.replace(/\s+/g, " ").slice(0, 40));
  const page1Rows = await page.$$eval(".conc-req tbody tr", (els) => els.length);
  ck("分页后每页 8 条", page1Rows === 8, `${page1Rows} 行`);
  await page.click(".conc-req .pager__btn:last-child");
  await new Promise((r) => setTimeout(r, 400));
  const nowPage = await page.$eval(".conc-req .pager__now", (e) => e.textContent.trim());
  const page2Rows = await page.$$eval(".conc-req tbody tr", (els) => els.length);
  ck("翻到第 2 页且行数变少", nowPage.startsWith("2") && page2Rows === expectPage2, `${nowPage} / ${page2Rows} 行`);
  await page.click(".conc-req .pager__btn:first-child");
  await new Promise((r) => setTimeout(r, 300));

  // 引擎 /metrics 只会落到一条通道，那条通道的标签应当被注明（单路夹具里就是那一条）
  const stateCard = await page.$eval(".state-card .card-title", (e) => e.textContent);
  ck("引擎单路卡标注所属通道", /快道|普道/.test(stateCard), stateCard);


  const panel = await page.$eval(".conc-proxy", (e) => e.textContent);
  ck("网关状态卡渲染", /透传/.test(panel) && /组装/.test(panel), "");
  // 最后再验胶囊的「读取」态：让假服务报「有在途、但没人正在生成」——这时不该说生成中。
  // 放在监控页断言之后，因为它要重新导航（会切回首页）。
  await page.goto(`http://127.0.0.1:${PORT}/__queue?1`, { waitUntil: "networkidle2" });
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.waitForFunction(() => /读取/.test(document.querySelector(".st-pill")?.textContent || ""), { timeout: 8000 });
  ck("聚合口径下，有在途但没在生成 → 胶囊说「读取提示词」", true,
     await page.$eval(".st-pill", (e) => e.textContent.trim()));
  // 开关拨回去之后必须回到真正的页面 —— 否则浏览器还停在 JSON 响应上，后面的用例找不到任何元素
  await page.goto(`http://127.0.0.1:${PORT}/__queue?0`, { waitUntil: "networkidle2" });
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });

  await page.screenshot({ path: "/tmp/smoke-monitor.png", fullPage: true });

  // ---- 记录页：推理日志的元信息（内容只存不显）
  await page.click('.st-tab[data-view="records"]');
  await page.waitForSelector(".rec-table .st-table", { timeout: 8000 });
  await page.waitForFunction(() => document.querySelectorAll(".rec-row").length > 0, { timeout: 8000 });
  const recRows = await page.$$eval(".rec-row", (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()));
  ck("记录页列出全部记录", recRows.length === 3, `${recRows.length} 行`);
  ck("行里有读入 token 数", /238,412/.test(recRows[0]), recRows[0].slice(0, 60));
  ck("行里有缓存命中数", /230k/.test(recRows[0]), recRows[0].slice(0, 80));
  ck("行里有输出 token 数", /512/.test(recRows[0]), recRows[0].slice(0, 80));
  ck("行里有解码速度", /41\.6/.test(recRows[0]), recRows[0].slice(0, 80));
  ck("行里有首字延迟（毫秒→秒自动换算）", /1\.84 s/.test(recRows[0]), recRows[0].slice(0, 80));
  ck("错误行标了出错状态", /出错/.test(recRows[1]), recRows[1].slice(0, 60));
  ck("上下文模式有中文说明（组装/透传）", /检索组装/.test(recRows[0]) && /透传/.test(recRows[1]),
     `${recRows[0].slice(0, 40)} | ${recRows[1].slice(0, 40)}`);
  ck("「检索块」列给出块数与最高分（没组装的是 –）",
     /12 · 0\.83/.test(recRows[0]) && /–/.test(recRows[1]), `${recRows[0].slice(0, 80)}`);
  const statText = await page.$eval(".rec-stats", (e) => e.textContent.replace(/\s+/g, " "));
  ck("概览卡有总数/今日", /近 7 天记录/.test(statText) && /今日 3/.test(statText), statText.slice(0, 80));
  ck("概览卡有输出 token 与速度", /600/.test(statText) && /35\.0/.test(statText), statText.slice(0, 120));
  ck("隐私说明在位（内容存对象存储、界面只给元信息）",
     /对象存储/.test(await page.$eval(".rec-note", (e) => e.textContent)), "");
  ck("实时条列出内存里的最新记录", (await page.$$(".rec-live__i")).length === 2, "");

  // 点开一行：详情面板只有元信息，绝不能出现用户输入 / 模型回复
  await page.click(".rec-row");
  await page.waitForSelector(".rec-detail", { timeout: 5000 });
  const recDetail = await page.$eval(".rec-detail", (e) => e.textContent.replace(/\s+/g, " "));
  ck("详情面板给出记录 id", /20261016-181500-a1b2c3/.test(recDetail), "");
  ck("详情面板给出组装构成（最近窗口 / 检索块）", /最近窗口/.test(recDetail) && /检索块/.test(recDetail), "");
  ck("详情面板给出存储位置与归档状态", /seg-181500-9f3a2\.jsonl\.gz/.test(recDetail) && /已归档/.test(recDetail), "");
  ck("详情面板给出正文/思考的**字数**而不给正文", /回复 1,200 字/.test(recDetail) && /思考 340 字/.test(recDetail),
     recDetail.slice(0, 100));
  // RAG 前后对照：只给计数与分数，正文一律不出现
  await page.waitForSelector(".rag-list .rag-item", { timeout: 5000 }).catch(() => {});
  const recDetail2 = await page.$eval(".rec-detail", (e) => e.textContent.replace(/\s+/g, " "));
  ck("详情里有 RAG 前后对照", /RAG 前后对照/.test(recDetail2), recDetail2.slice(0, 60));
  ck("标出前后不同（已改写）", /已改写/.test(recDetail2) && /40 条消息/.test(recDetail2) && /42 条消息/.test(recDetail2),
     "");
  ck("给出候选池 → 重排与选中块", /73 → 73/.test(recDetail2) && /12 · 分数 0\.41 ~ 0\.83/.test(recDetail2), "");
  ck("列出检索回来的块（回合标签 + 分数）",
     (await page.$$(".rag-list .rag-item")).length === 3
     && /第 3 轮/.test(recDetail2) && /score 0\.830/.test(recDetail2), "");
  ck("结构补全的块标成「结构补全」而不是分数", /结构补全/.test(recDetail2), "");
  ck("详情面板里没有任何正文内容", !/LLM 推理过程/.test(recDetail2) && !/你好/.test(recDetail2), "");
  await page.click(".rec-detail .st-btn--icon");                  // 关掉
  await page.waitForFunction(() => !document.querySelector(".rec-detail"), { timeout: 3000 });

  // 筛选：按状态筛完只剩一条
  await page.select(".rec-filters select:nth-of-type(1)", "");    // 先确保模型是「全部」
  const sel2 = (await page.$$(".rec-filters select"))[1];
  await sel2.select("error");
  await page.waitForFunction(() => document.querySelectorAll(".rec-row").length === 1, { timeout: 5000 });
  const one = await page.$eval(".rec-row", (e) => e.textContent.replace(/\s+/g, " "));
  ck("按状态筛选只剩出错那条", /出错/.test(one), one.slice(0, 60));
  // 筛选控件的外观：必须是**自绘**的（原生 select 那副样子被吐槽过：方角 + 原生箭头 + 硬蓝描边）
  const look = await page.evaluate(() => {
    const sel = document.querySelector(".rec-sel select");
    const wrap = document.querySelector(".rec-sel");
    const cs = getComputedStyle(sel);
    const ws = getComputedStyle(wrap);
    return { appearance: cs.appearance || cs.webkitAppearance, border: cs.borderWidth,
             radius: ws.borderRadius, wrapBorder: ws.borderStyle,
             arrow: !!document.querySelector(".rec-sel .rec-sel__i use") };
  });
  ck("下拉是自绘的（原生外观已去掉、边框移交给外层）",
     look.appearance === "none" && look.border === "0px" && look.wrapBorder === "solid", JSON.stringify(look));
  ck("自绘下拉有自己的箭头图标", look.arrow, "");
  ck("筛选生效时出现「清空筛选」", await page.$(".rec-clear") !== null, "");
  await page.click(".rec-clear");
  await page.waitForFunction(() => document.querySelectorAll(".rec-row").length === 3, { timeout: 5000 });
  ck("点「清空筛选」恢复三条、按钮自己也消失",
     (await page.$(".rec-clear")) === null, "");

  // 页面**从不**去取内容（/infer/raw 是给错误分析用的口子，不该被界面调用）
  ck("记录页没有请求过内容接口", RAW_CALLS === 0, `raw 调用 ${RAW_CALLS} 次`);

  await page.screenshot({ path: "/tmp/smoke-records.png", fullPage: true });

  // ---- 推理可视化：可拖可缩的画布 + 钻进层内部
  // 中间这块是**真画布**（能拖、能滚轮缩放、点层块钻进它的内部），不是一张贴死的图。
  await page.click('.st-tab[data-view="viz"]');
  await page.waitForSelector(".canvas__svg", { timeout: 8000 });
  const svgBox = await page.$eval(".canvas__svg", (c) => { const r = c.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; });
  ck("画布铺满舞台", svgBox.w > 300 && svgBox.h > 300, JSON.stringify(svgBox));
  ck("页面里不再有 WebGL 画布", await page.$("canvas") === null, "");
  ck("画布给出拖动/缩放的操作提示", /拖动/.test(await page.$eval(".canvas__hint", (e) => e.textContent)), "");

  // 总览：文本输入 → 分词 → 词嵌入 → 层堆叠（只画三块）→ 输出投影 → Softmax
  const items = await page.$$eval(".cv__item", (els) => els.map((e) => e.textContent.trim()));
  ck("流程里画出了头尾方块", ["文本输入", "Tokenizer", "词嵌入", "输出投影"].every((s) => items.some((b) => b.includes(s))), JSON.stringify(items));
  const layerBlocks = await page.$$eval(".cv__item.is-layer", (els) => els.map((e) => e.textContent.trim()));
  ck("层堆叠只画三块（不是 48 块）", layerBlocks.length === 3, JSON.stringify(layerBlocks));
  const dots = await page.$$eval(".cv__item.is-dots", (els) => els.map((e) => e.textContent.trim()));
  ck("用 ⋯ 表示中间还有很多层", dots.length >= 1 && /还有 \d+ 层/.test(dots.join(" ")), JSON.stringify(dots));
  // 总览是**数据流图**：分几行、行内自左向右、行与行之间用曲线接上
  const flow = await page.$$eval(".cv__item", (els) => els.map((e) => {
    const r = e.getBoundingClientRect();
    return { t: e.textContent.trim().slice(0, 6), x: Math.round(r.x), y: Math.round(r.y), h: Math.round(r.height) };
  }));
  const rowKeys = [...new Set(flow.map((f) => Math.round(f.y / 10)))];
  ck("总览排成多行数据流（不是一根竖线）", rowKeys.length >= 3 && rowKeys.length <= 5, `${rowKeys.length} 行`);
  const perRow = (k) => flow.filter((f) => Math.round(f.y / 10) === k).sort((a, b) => a.x - b.x);
  ck("每一行内部自左向右", rowKeys.every((k) => { const r = perRow(k); return r.every((v, i) => i === 0 || v.x > r[i - 1].x); }), "");
  ck("跨行用的是曲线连接", (await page.$$eval(".cv__edge", (els) => els.filter((e) => (e.getAttribute("d") || "").includes("C")).length)) >= 2, "");

  // 拖得动 —— 这是这一版最核心的手感，专门验一次：按住空白处拖，相机位移必须跟着变
  const camNow = () => page.$eval(".canvas__svg > g", (e) => e.getAttribute("transform"));
  const cam0 = await camNow();
  const dragBox = await page.$eval(".canvas", (e) => { const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; });
  await page.mouse.move(dragBox.x + dragBox.w * 0.5, dragBox.y + dragBox.h * 0.96);
  await page.mouse.down();
  await page.mouse.move(dragBox.x + dragBox.w * 0.5 + 96, dragBox.y + dragBox.h * 0.96 - 48, { steps: 8 });
  await page.mouse.up();
  await new Promise((r) => setTimeout(r, 150));
  const cam1 = await camNow();
  ck("画布可以拖动平移", cam0 !== cam1, `${cam0} → ${cam1}`);

  const modelRows = await page.$$eval(".mrow__name", (els) => els.map((e) => e.textContent.trim()));
  ck("模型列表渲染（本机实测 1 + 示例 2）", modelRows.length === 3, JSON.stringify(modelRows));
  ck("第一条是读自本机模型文件的真模型", /Qwen3\.8-Flash-Next/.test(modelRows[0]), modelRows[0]);
  ck("默认选中第一条", await page.$eval(".mrow", (e) => e.getAttribute("aria-pressed")) === "true", "");
  // 默认选中的是「本机实测」那个（读自 GGUF），不是示例 —— 标注必须跟着选中项走
  const src = await page.$eval(".bar__source", (e) => ({ text: e.textContent.trim(), src: e.dataset.src }));
  ck("界面如实标注数据来源是「本机实测」", src.src === "local" && src.text === "本机实测", JSON.stringify(src));

  const stepsBadge = await page.$eval(".bar__count", (e) => e.textContent.trim());
  ck("步骤数由真实层数推出来（48 层 → 4 + 192 + 2 + 采样/重复 = 200）", /200/.test(stepsBadge), stepsBadge);
  const idle = await page.$eval(".panel__block", (e) => e.textContent);
  ck("未开始时右栏是提示态", idle.includes("演示还没开始"), "");

  const stageW0 = await page.$eval(".viz__stage", (e) => Math.round(e.getBoundingClientRect().width));
  await page.click(".bar__go");                                  // 开始演示
  // 等**演示模式本身**出现，而不是等刻度条：刻度在选完模型那一刻就有了，
  // 拿它当「演示开始了」的信号会抢跑到 startDemo 还在重算步骤的时候（后面全都跟着乱）。
  await page.waitForSelector(".viz[data-focus]", { timeout: 15000 });
  await page.waitForFunction(() => document.querySelectorAll(".tl__tick").length === 200, { timeout: 15000 });
  // 演示模式：无关元素退场，页面上只剩画布 + 悬浮的流程控制
  ck("开始演示 → 进入演示模式", await page.$(".viz[data-focus]") !== null, "");
  ck("演示模式：顶栏变成画布上的一条悬浮胶囊",
     (await page.$eval(".bar", (e) => getComputedStyle(e).position)) === "absolute", "");
  ck("演示模式：时间轴也浮起来了",
     (await page.$eval(".tl", (e) => getComputedStyle(e).position)) === "absolute", "");
  // 退场是**动画**（两栏收拢 0.42s）：等条件成立，而不是「睡 700ms 再赌它已经走完」
  await page.waitForFunction(() => {
    const m = (s) => document.querySelector(s).getBoundingClientRect().width;
    const o = (s) => Number(getComputedStyle(document.querySelector(s)).opacity);
    return m(".viz__side--left") <= 8 && o(".viz__side--left") < 0.05
        && m(".viz__side--right") <= 8 && o(".viz__side--right") < 0.05;
  }, { timeout: 3000 });
  const gone = await page.evaluate(() => {
    const m = (s) => { const e = document.querySelector(s); const r = e.getBoundingClientRect();
                       return { w: Math.round(r.width), op: Number(getComputedStyle(e).opacity) }; };
    return { left: m(".viz__side--left"), right: m(".viz__side--right"), setup: m(".bar__setup") };
  });
  // 收成 0 宽：栅格轨道过渡到 0，亚像素上还剩几个像素（边框/滚动条），按 ≤8px 判
  ck("演示模式：左右两栏退场（收成 0 宽 + 透明）",
     gone.left.w <= 8 && gone.left.op < 0.05 && gone.right.w <= 8 && gone.right.op < 0.05, JSON.stringify(gone));
  ck("演示模式：选模型/输入框整块退场", gone.setup.w <= 1 && gone.setup.op < 0.05, JSON.stringify(gone.setup));
  const stageW1 = await page.$eval(".viz__stage", (e) => Math.round(e.getBoundingClientRect().width));
  ck("演示模式：画布把让出来的宽度吃满", stageW1 > stageW0, `${stageW0} → ${stageW1}`);

  await page.click(".bar__play");                                // 立刻暂停，别让自动播放和断言抢
  ck("暂停后按钮变成「继续」", /继续/.test(await page.$eval(".bar__play", (e) => e.textContent)), "");
  ck("演示模式里第二个按钮是「退出演示」", /退出演示/.test(await page.$eval(".bar__reset", (e) => e.textContent)), "");

  // 拖时间轴到指定步（等价于用户拖动；顺便验证 scrub → 3D + 右栏同步）
  const scrub = async (i) => {
    await page.$eval(".tl__range", (el, v) => {
      el.value = String(v);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }, i);
  };
  const panelText = () => page.$eval(".panel__block", (e) => e.textContent);

  await scrub(0);
  ck("第 0 步：文本输入", (await page.$eval(".panel__name", (e) => e.textContent)).trim() === "文本输入", "");
  ck("第 0 步张量是 string", /string/.test(await panelText()), "");

  await page.click(".tl__nav:nth-of-type(2)");                   // 下一步
  const s1 = await page.$eval(".panel__name", (e) => e.textContent);
  ck("下一步 → Tokenizer 分词", s1.includes("Tokenizer"), s1);

  await scrub(3);                                                // Embedding
  const embed = await panelText();
  ck("Embedding 张量形状用的是真实的 2560 维", embed.includes("[10, 2560]"), "");

  await scrub(4);                                                // 第 1 层 归一化
  const ln = await panelText();
  ck("层进度按真实的 48 层给", /第 1 \/ 48 层/.test(ln), "");
  ck("归一化这一步列出真实张量名（hyper-connection）", ln.includes("hc_attn_norm") || ln.includes("hc_ffn_norm"), "");

  await scrub(5);                                                // 第 1 层是线性注意力层（ssm）
  const lin = await panelText();
  ck("第 1 层标成线性注意力", /线性注意力/.test(lin), "");
  ck("线性注意力列出真实的 ssm/attn_qkv 张量", lin.includes("attn_qkv") && lin.includes("ssm_a"), "");

  await scrub(6);                                                // MoE FFN
  const moe = await panelText();
  ck("FFN 步骤标出 MoE 与专家数（512 选 10）", /MoE/.test(moe) && /512/.test(moe) && /10/.test(moe), "");
  ck("MoE 列出路由与专家张量", moe.includes("ffn_gate_inp") && moe.includes("ffn_gate_exps"), "");

  // 第 4 层（下标 3）是全注意力层：每 4 层一个，和 full_attention_interval 对得上
  await scrub(4 + 3 * 4 + 1);                                    // = 17 → L4 自注意力
  const full = await panelText();
  ck("第 4 层是全注意力，列出 GQA 与真实投影名", /自注意力/.test(full) && full.includes("attn_q.weight") && full.includes("attn_k.weight"), "");
  ck("全注意力步骤说明里点出 KV 头数", /KV 2 头|2 组 K\/V|共用 2/.test(full), full.slice(0, 40));

  await scrub(197);                                              // 最后一步
  ck("最后一步是 Softmax / 下一个词", /Softmax/.test(await page.$eval(".panel__name", (e) => e.textContent)), "");

  // 演示走到某一层 → **画布直接换成「这一步在算什么」**：张量、权重、怎么相乘
  await scrub(5);                                                // 第 1 层 · 线性注意力
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-grid").length >= 2, { timeout: 4000 });
  const hud = await page.$eval(".canvas__hud", (e) => e.textContent.replace(/\s+/g, " "));
  ck("HUD 报当前步骤名", /第 1 层/.test(hud), hud.slice(0, 30));
  const shapes = await page.$$eval(".cv__item.is-grid .g__shape", (els) => els.map((e) => e.textContent.trim()));
  ck("单步图上写清了进出张量的形状", shapes.length >= 2 && shapes.every((s) => /^\[[\d, ]+\]$/.test(s)), JSON.stringify(shapes));
  ck("单步图画的是平面矩阵格（不是立体块）",
     (await page.$$eval(".cv__item.is-grid .g__cell", (els) => els.length)) >= 12, "");
  ck("矩阵相乘画出了符号与说明",
     /×/.test(await page.$eval(".cv__item.is-sym .sym__label", (e) => e.textContent))
     && /相乘/.test(await page.$eval(".cv__item.is-sym .sym__sub", (e) => e.textContent)), "");
  ck("相乘的格子之间连了线（哪对乘哪对）",
     (await page.$$eval(".cv__wire", (els) => els.length)) >= 3, "");
  ck("参与相乘的格子被标了出来",
     (await page.$$eval(".g__cell.is-mark", (els) => els.length)) >= 3, "");
  ck("单步卡长在当前这个块下面（总图还在，块被点亮）",
     (await page.$$eval(".cv__item.is-card", (els) => els.length)) === 1
     && (await page.$$eval(".cv__item.is-layer[data-on]", (els) => els.length)) >= 1, "");
  ck("单步图给出这一步的说明文字",
     (await page.$eval(".canvas__note", (e) => e.textContent.trim())).length > 12, "");
  ck("面包屑给出退回总览的入口", await page.$(".canvas__crumb-link") !== null, "");

  // MoE 那一步：并行的那几路要用**同一个颜色**摆出来
  await scrub(6);                                                // 第 1 层 · MoE FFN
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-lanes .lane__blk").length >= 3, { timeout: 4000 });
  ck("MoE 一步标出并行专家（同色多路）", (await page.$$eval(".cv__item.is-lanes .lane__blk", (els) => els.length)) >= 8, "");
  ck("并行那几路写了「几路并行」",
     /路并行/.test(await page.$eval(".lane__cap", (e) => e.textContent)), "");

  // 最后一步：落到词表概率上
  await scrub(197);
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-bars .bar__tok").length >= 3, { timeout: 4000 });
  ck("最后一步画的是词表上的概率分布",
     (await page.$$eval(".cv__item.is-bars .bar__pct", (els) => els.map((e) => e.textContent))).every((t) => /%$/.test(t)),
     "");
  ck("概率条明确标注是示意（不是本机实测）",
     /示意/.test(await page.$eval(".bar__cap", (e) => e.textContent)), "");

  // 收尾：采样出第 1 个词 → 重复生成 → 给出模型的真实回答
  await scrub(198);
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-bars").length >= 1, { timeout: 4000 });
  ck("采样那一步：分布里标出「选中的」那一个",
     /选中的/.test(await page.$eval(".cv__item.is-bars", (e) => e.textContent))
     || /本机实测/.test(await page.$eval(".cv__item.is-card, .canvas", (e) => e.textContent)), "");
  ck("采样选中项标了「本机实测」（真跑过一次推理）",
     /本机实测/.test(await page.$eval(".canvas", (e) => e.textContent)), "");

  await scrub(199);
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-chip").length >= 5, { timeout: 8000 });
  const ans = await page.$eval(".canvas", (e) => e.textContent);
  ck("最后一步给出模型的**真实回答**（逐字摆开）",
     ans.includes("LLM 推理过程可视化") && /token/.test(ans), ans.replace(/\s+/g, " ").slice(0, 70));
  ck("并说明「每生成一个字都要重走一遍」", /一遍/.test(ans), "");

  // 面包屑回总览 → 点层块：**进入**这一层的内部网状图（这里才是算子级视图）
  await scrub(5);
  await page.evaluate(() => { const l = document.querySelector(".canvas__crumb-link"); if (l) l.click(); });                       // 总览
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-layer").length === 3, { timeout: 4000 });
  ck("面包屑退回总览", /总览/.test(await page.$eval(".canvas__crumb-now", (e) => e.textContent)), "");
  const blockEls2 = await page.$$(".cv__item.is-layer");
  await blockEls2[0].click();                                    // 第 1 层
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-op").length >= 3, { timeout: 4000 });
  ck("点层块 → 进入这一层的内部网状图",
     /第 1 层/.test(await page.$eval(".canvas__crumb-now", (e) => e.textContent)), "");

  // 层内部：算子按**拓扑深度**自左向右分列，两端是进/出的端口
  const ops = await page.$$eval(".cv__item.is-op", (els) => els.map((e) => e.textContent.trim()));
  ck("层内部列出这一层的算子", ops.length >= 3 && ops[0].includes("归一化"), JSON.stringify(ops));
  // 按行看：同一行内自左向右（行放不下会折行，跨行的 x 会回到左边）
  const opPos = await page.$$eval(".cv__item.is-op", (els) => els.map((e) => {
    const r = e.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y / 10) };
  }));
  const okLR = [...new Set(opPos.map((o) => o.y))].every((row) => {
    const r = opPos.filter((o) => o.y === row);
    return r.every((v, i) => i === 0 || v.x > r[i - 1].x);
  });
  ck("算子按依赖关系自左向右排开（放不下才折行）", okLR, JSON.stringify(opPos));
  ck("两端有输入/输出端口", (await page.$$eval(".cv__item.is-port", (els) => els.length)) === 2, "");
  ck("画了残差跳连那条虚线", await page.$(".cv__edge.is-skip") !== null, "");
  ck("当前算子被点亮", (await page.$$eval(".cv__item.is-op[data-on]", (els) => els.length)) >= 1, "");

  // 点算子 → 信息面板出该节点的详情（面板此时退场了，但内容还在，回来就能看到）
  // 用坐标点而不是 page.click(selector)：SVG 的分组元素在 Puppeteer 里算出来的
  // 「可点中心」不一定落在图形上，报出来也方便下次定位。
  const settleCam = async () => {                 // 等镜头动画停住：它还在飘的时候量坐标，点一定偏
    let prev = null;
    for (let i = 0; i < 40; i++) {
      const now = await camNow();
      if (now === prev) return now;
      prev = now;
      await new Promise((r) => setTimeout(r, 60));
    }
    return prev;
  };
  await new Promise((r) => setTimeout(r, 500));   // 先给镜头动画一点起步时间（它由 rAF 排的队）
  await settleCam();
  const opHit = await page.$eval(".cv__item.is-op", (e) => {
    const r = e.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  });
  await page.mouse.click(opHit.x, opHit.y);
  await page.waitForSelector(".panel__swatch", { timeout: 4000 }).catch(() => {});
  const detail = await page.$$eval(".panel__block", (els) => els.map((e) => e.textContent).join(" "));
  const shot = await page.evaluate(() => ({
    canvas: `${document.querySelector(".canvas").clientWidth}x${document.querySelector(".canvas").clientHeight}`,
    cam: document.querySelector(".canvas__svg > g").getAttribute("transform"),
  }));
  ck("点算子 → 信息面板出节点详情", /输入/.test(detail) && /参数量/.test(detail),
     `@${opHit.x},${opHit.y} ${shot.canvas} ${shot.cam} :: ${detail.slice(0, 24)}`);

  // 一路点到底：算子 → 它由哪些矩阵组成 → 单个矩阵（最底层）
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-grid").length >= 2, { timeout: 4000 });
  const matNames = await page.$$eval(".cv__item.is-grid .g__name", (els) => els.map((e) => e.textContent.trim()));
  ck("点算子 → 进去看它由哪些权重矩阵组成", matNames.length >= 3, JSON.stringify(matNames.slice(0, 4)));
  ck("矩阵之间连成一条数据流", (await page.$$eval(".cv__edge", (els) => els.length)) >= 3, "");
  ck("每个矩阵都写了自己的形状",
     (await page.$$eval(".cv__item.is-grid .g__shape", (els) => els.map((e) => e.textContent.trim()))).every((s) => /^\[.*\]$/.test(s)), "");
  await page.click(".cv__item.is-grid .g__enter");
  await new Promise((r) => setTimeout(r, 600));
  ck("点矩阵 → 进到最底下这一张矩阵",
     /weight/.test(await page.$eval(".canvas__crumb-now", (e) => e.textContent)), "");
  ck("最底层给出了参数量说明",
     /个数/.test(await page.$eval(".canvas__note", (e) => e.textContent)), "");
  ck("面包屑能一层层退回去", (await page.$$eval(".canvas__crumb-link", (els) => els.length)) >= 3, "");
  await page.evaluate(() => { const l = document.querySelector(".canvas__crumb-link"); if (l) l.click(); });                       // 回总览
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-layer").length === 3, { timeout: 4000 });
  // 回总览后面包屑必须只剩「总览」——之前只改了层级、没清掉「当前算子」，
  // 于是画面已经是总览、标签还挂着「总览 › 第 1 层 › 归一化」（用户实测报的）
  ck("从深处回总览后，面包屑只剩「总览」",
     (await page.$eval(".canvas__crumb", (e) => e.textContent.replace(/\s+/g, " ").trim())) === "总览",
     await page.$eval(".canvas__crumb", (e) => e.textContent.replace(/\s+/g, " ").trim()));

  // 点一个**没有权重矩阵**的算子（残差）也要能进去看：以前点不动，用户实测报过
  await page.evaluate(() => { const l = document.querySelector(".canvas__crumb-link"); if (l) l.click(); });                       // 回总览
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-layer").length === 3, { timeout: 4000 });
  const before = await page.$eval(".tl__range", (e) => e.value);
  await page.click(".cv__item.is-layer");                        // 第 1 层
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-op").length >= 3, { timeout: 4000 });
  await page.click(".cv__item.is-op[data-type='residual']");
  await new Promise((r) => setTimeout(r, 600));
  ck("没有权重矩阵的算子也能点进去（看到进出张量）",
     /残差/.test(await page.$eval(".canvas__crumb-now", (e) => e.textContent))
     && (await page.$$eval(".cv__item.is-grid", (els) => els.length)) >= 2, "");
  ck("**点块不会改动演示进度**（点块是看结构，不是跳步）",
     (await page.$eval(".tl__range", (e) => e.value)) === before, `${before} → ${await page.$eval(".tl__range", (e) => e.value)}`);

  // 总览里的头尾块：点一下进「这一步在算什么」那张图（和演示时是同一张）
  await page.evaluate(() => { const l = document.querySelector(".canvas__crumb-link"); if (l) l.click(); });
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-layer").length === 3, { timeout: 4000 });
  ck("能点进去的块都带「›」记号，没有装成可点的死块",
     (await page.$$eval(".cv__item[data-click]", (els) => els.length)) ===
     (await page.$$eval(".cv__item[data-click] .cv__enter, .cv__item.is-grid[data-click] .g__enter", (els) => els.length)), "");
  const boxes = await page.$$(".cv__item.is-box[data-click='1']");
  ck("总览里的头尾块都可点", boxes.length >= 4, `${boxes.length} 个`);
  await boxes[boxes.length - 1].click();                         // Softmax
  await new Promise((r) => setTimeout(r, 700));
  ck("点尾块 → 进到它那一步的图（概率分布）",
     /Softmax/.test(await page.$eval(".canvas__crumb-now", (e) => e.textContent))
     && (await page.$$eval(".cv__item.is-bars .bar__pct", (els) => els.length)) >= 3, "");

  // 缩放按钮：+ 要真的把比例推大
  const kOf = () => page.$eval(".canvas__svg > g", (e) => Number(/scale\(([\d.]+)\)/.exec(e.getAttribute("transform"))[1]));
  const k0 = await kOf();
  await page.click(".canvas__zoom button:last-child");
  await new Promise((r) => setTimeout(r, 350));
  const k1 = await kOf();
  ck("放大按钮生效", k1 > k0, `${k0} → ${k1}`);
  await page.click(".canvas__zoom button:nth-child(2)");         // 复位（放大后最后一个层块会被推出可视区，点不中）
  await new Promise((r) => setTimeout(r, 400));

  // 点层块 = **进入它的结构图**（不碰演示进度）
  // 先回总览：上一步停在「某一步的图」上，取景把层块顶到了顶部悬浮条底下，点不中
  await page.evaluate(() => { const l = document.querySelector(".canvas__crumb-link"); if (l) l.click(); });
  await page.waitForFunction(() => document.querySelectorAll(".cv__item.is-layer").length === 3, { timeout: 4000 });
  // 取景动画要几百毫秒才回到总览；没等它回来就点，点的是还在画面外的坐标
  await page.waitForFunction(() => {
    const r = document.querySelector(".cv__item.is-layer").getBoundingClientRect();
    return r.top > 60 && r.right < innerWidth;
  }, { timeout: 3000 });
  const idxBefore = await page.$eval(".tl__range", (e) => e.value);
  const blockEls = await page.$$(".cv__item.is-layer");
  await blockEls[blockEls.length - 1].click();
  await new Promise((r) => setTimeout(r, 600));
  ck("点最后一层 → 进入它的结构图",
     /第 48 层/.test(await page.$eval(".canvas__crumb", (e) => e.textContent)), 
     await page.$eval(".canvas__crumb", (e) => e.textContent.replace(/\s+/g, " ").trim()));
  ck("点层块不会顺带把演示跳到那一步",
     (await page.$eval(".tl__range", (e) => e.value)) === idxBefore, `${idxBefore} → ${await page.$eval(".tl__range", (e) => e.value)}`);

  await page.click(".bar__reset");                               // 「退出演示」= 重置
  await page.waitForFunction(() => !document.querySelector(".viz[data-focus]"), { timeout: 3000 });
  ck("退出演示 → 无关元素回场", await page.$(".viz[data-focus]") === null, "");
  await page.waitForFunction(() => !document.querySelector(".tl__range").disabled
    && Number(document.querySelector(".tl__range").value) === -1, { timeout: 3000 });
  ck("重置回到起点（滑块归 -1）", true, "");
  // 左栏也是「滑」回来的：没滑到位之前它还是 pointer-events:none，点不中
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector(".viz__side--left")).opacity) > 0.99,
                             { timeout: 3000 });

  // 换模型：第 3 条是 6 层 → 4 + 24 + 2 = 32 步，整张图和步骤表都得重算
  await page.click(".mrow:nth-of-type(3)");
  await page.waitForFunction(() => {
    const b = document.querySelector(".bar__count");
    return b && b.textContent.trim() === "32 步";
  }, { timeout: 8000 });
  const badge6 = await page.$eval(".bar__count", (e) => e.textContent.trim());
  ck("换模型后步骤数跟着变（6 层 → 32 步）", badge6 === "32 步", badge6);
  const ticks6 = await page.$$eval(".tl__tick", (els) => els.length);
  ck("换模型后时间轴刻度重建", ticks6 === 32, String(ticks6));
  ck("换模型回到未开始的干净状态", (await page.$eval(".panel__block", (e) => e.textContent)).includes("演示还没开始"), "");

  // 「还有 N 个字，完整回答见对话页」是可点的：点一下真的把人带到那条会话
  // 先重新起演示（刚才手动点过块，画面被钉在手动那层；起演示会把跟随交还给演示）
  await page.click(".bar__go");
  await page.waitForSelector(".viz[data-focus]", { timeout: 15000 });
  await page.click(".bar__play");                                // 暂停，别让自动播放抢
  const visStep = (await page.$$eval(".tl__tick", (els) => els.length)) - 1;
  await scrub(visStep);
  await page.waitForSelector(".cv__note.is-link", { timeout: 8000 });
  await new Promise((r) => setTimeout(r, 800));                  // 取景动画没停就点，点的是还在动的坐标
  await page.click(".cv__note.is-link");
  await page.waitForFunction(() => {
    const t = [...document.querySelectorAll(".st-bubble")].map((e) => e.textContent).join(" ");
    return /演示用的真实回答/.test(t);
  }, { timeout: 8000 }).catch(() => {});
  // 对话页不再有导航按钮（改成深链 #chat），所以这里按「页面切过去了 + 会话内容在」来断言
  ck("点「完整回答见对话页」→ 跳到对话页并打开那条会话",
     page.url().endsWith("#chat")
     && (await page.$$eval(".st-bubble", (els) => els.map((e) => e.textContent).join(" "))).includes("演示用的真实回答"),
     (await page.$$eval(".st-bubble", (els) => els.map((e) => e.textContent.trim().slice(0, 18)))).join(" | "));
  await page.click('.st-tab[data-view="viz"]');                  // 回到可视化，后面的用例还要用
  await page.waitForSelector(".canvas__svg", { timeout: 8000 });

  await page.screenshot({ path: "/tmp/smoke-viz.png" });

  // ---- 关于页
  await page.click('.st-tab[data-view="about"]');
  await page.waitForSelector(".facts dd", { timeout: 3000 });
  const about = await page.$eval(".view--about", (e) => e.textContent);
  ck("关于页有中文标题", about.includes("模型与引擎") && about.includes("本机"), "");
  // 「本机」那一节原来只有一行 GPU（来源 /metrics = 粘性钉住的一条通道）→ 双卡部署下看着像只有一张卡。
  // 现在一张卡一行，且署上「GPU 几（哪条道）」。
  const facts = await page.$$eval(".view--about .facts", (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()));
  const gpuFacts = facts.join("\n").match(/GPU[^（]*（[^）]*）/g) || [];
  ck(SINGLE ? "关于页：本机一卡一行并署到通道" : "关于页：本机两张卡 = 两行，各署卡号与通道",
     gpuFacts.length === (SINGLE ? 1 : 2) && gpuFacts.some((x) => /GPU 0（快道）/.test(x))
       && (SINGLE || gpuFacts.some((x) => /GPU 1（普道）/.test(x))), JSON.stringify(gpuFacts));
  ck("接入地址显示模型名", about.includes("qwen3.8-flash-fast"), "");

  // ---- 采样抽屉
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.click('.st-btn[title="采样与思考"]');
  await page.waitForSelector(".st-drawer[data-open=true]", { timeout: 3000 });
  await page.waitForFunction(() => getComputedStyle(document.querySelector(".st-drawer")).transform === "none", { timeout: 3000 });
  const drawer = await page.$eval(".st-drawer", (e) => e.textContent);
  ck("采样抽屉中文化", drawer.includes("采样") && drawer.includes("温度") && drawer.includes("应用"), "");

  // ---- 语言切换 → 英文（点完等遮罩消失、等重渲染，别抢跑）
  await page.click(".st-drawer__head .st-btn--icon");
  // 抽屉是覆盖式的（和原版一样），滑出动画没走完时仍会盖住页头右侧 → 等它完全出屏再点页头
  await page.waitForFunction(() => {
    const d = document.querySelector(".st-drawer");
    return getComputedStyle(document.querySelector(".scrim")).display === "none"
        && d.getBoundingClientRect().left >= innerWidth - 1;
  }, { timeout: 3000 });
  await page.click(".st-btn--lang");
  await page.waitForFunction(() => [...document.querySelectorAll(".st-tab")].some((e) => e.textContent.trim() === "Monitor"), { timeout: 3000 });
  const tabsEn = await page.$$eval(".st-tab", (els) => els.map((e) => e.textContent.trim()));
  ck("语言切换到英文", tabsEn.includes("Chat") && tabsEn.includes("Monitor"), JSON.stringify(tabsEn));
  await page.click(".st-btn--lang");                                   // 切回中文
  await page.waitForFunction(() => [...document.querySelectorAll(".st-tab")].some((e) => e.textContent.trim() === "监控"), { timeout: 3000 });
  const tabsZh = await page.$$eval(".st-tab", (els) => els.map((e) => e.textContent.trim()));
  ck("切回中文", tabsZh.includes("监控"), JSON.stringify(tabsZh));

  await page.screenshot({ path: "/tmp/smoke-chat.png", fullPage: true });
  ck("页面无 JS 报错", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));
  // 建议问题的「空闲时生成」只有在引擎真的空闲时才会发请求。夹具里 live.state 一直是 generating
  // （除了跑别的用例时），所以整轮下来只该有聊天那次 POST —— 多发一条就说明空转判断漏了。
  ck("引擎忙时不偷偷调用推理生成建议", suggestCalls === 0, `建议类请求 ${suggestCalls} 次`);

  // ---- 登录：**只拦对话**，其余功能不登录就能用
  await page.evaluate(() => localStorage.removeItem("strata.apikey"));
  await page.goto(`http://127.0.0.1:${PORT}/__loggedout?1`, { waitUntil: "networkidle2" });
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.waitForSelector(".app", { timeout: 6000 });
  ck("未登录也能进应用（登录只拦对话）", await page.$(".gate") === null, "");
  ck("未登录时页头给登录入口", await page.$(".st-signin") !== null, "");

  // 别的标签页照常可用
  await page.click('.st-tab[data-view="monitor"]');
  await page.waitForSelector(".metric-card", { timeout: 5000 });
  ck("未登录也能看监控页", await page.$(".gate") === null, "");
  await page.click('.st-tab[data-view="viz"]');
  await page.waitForSelector(".canvas__svg", { timeout: 8000 });
  ck("未登录也能看推理可视化", await page.$(".gate") === null, "");

  // 记录页是例外：它是**推理日志**（含会话键等运营信息），要凭据才给看 —— 但也不能是一堵白墙
  await page.click('.st-tab[data-view="records"]');
  await page.waitForSelector(".rec-empty", { timeout: 6000 });
  ck("未登录看记录页 → 明确提示要登录（不是空表）",
     /看记录需要登录/.test(await page.$eval(".rec-empty", (e) => e.textContent)) &&
     await page.$(".rec-table") === null, "");
  ck("未登录时给得出登录入口", await page.$(".rec-empty .st-btn") !== null, "");

  // 发对话 → 自动跳登录页
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.waitForSelector(".st-composer textarea", { timeout: 5000 });
  await page.type(".st-composer textarea", "你好");
  await page.click(".st-btn--send");
  await page.waitForSelector(".gate", { timeout: 6000 });
  ck("未登录发对话 → 自动跳登录页", true, "");
  ck("跳登录页后输入框里的字还在", (await page.$eval(".st-composer textarea", (e) => e.value)) === "你好", "");
  ck("登录页给出集市登录按钮", await page.$(".gate .st-btn--primary") !== null, "");
  ck("登录页显示品牌与新中文名", /SSEStraBed/.test(await page.$eval(".gate__name", (e) => e.textContent))
     && /层磐/.test(await page.$eval(".gate__name", (e) => e.textContent)), "");
  ck("登录页给出填 token 的另一条路", await page.$(".gate input[type=password]") !== null, "");
  const gateText = await page.$eval(".gate", (e) => e.textContent);
  ck("登录页说明了只有对话需要登录", /只有「对话」|只有.*对话/.test(gateText), gateText.replace(/\s+/g, " ").slice(0, 44));

  // 填 token 这条路：验一次 /health 通过后回应用
  await page.type(".gate input[type=password]", "smoke-test-token");
  await page.click(".gate .st-btn--secondary");
  await page.waitForFunction(() => !document.querySelector(".gate"), { timeout: 8000 });
  ck("填了 token 就回到应用", await page.$(".app") !== null, "");
  ck("token 存在本机 localStorage",
     (await page.evaluate(() => JSON.parse(localStorage.getItem("strata.apikey") || '""'))) === "smoke-test-token", "");

  // 这回对话要真的发出去。先开一条新会话：前面可视化那条「见对话页」用例把当前会话
  // 切到了演示那条上，不新开的话断言命中的是它里面的第一条助手消息。
  await page.click(".side__new");
  await new Promise((r) => setTimeout(r, 300));
  // 不依赖登录前的草稿/按钮是否被浮层挡住：显式设置输入，再用 DOM click 触发提交
  await page.evaluate(() => {
    const el = document.querySelector(".st-composer textarea");
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(el, "你好");
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await new Promise((r) => setTimeout(r, 100));
  await page.evaluate(() => document.querySelector(".st-btn--send")?.click());
  await page.waitForFunction(() => {
    const b = document.querySelector(".st-msg--assistant .st-bubble");
    return b && b.textContent.includes("世界");
  }, { timeout: 15000 });
  ck("填了 token 之后对话可用", true, "");

  // 登录状态：页头显示是谁
  await page.goto(`http://127.0.0.1:${PORT}/__loggedout?0`, { waitUntil: "networkidle2" });
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.waitForSelector(".st-user", { timeout: 6000 });
  ck("已登录时页头显示用户", /张三/.test(await page.$eval(".st-user", (e) => e.textContent)), "");
  ck("已登录时不再显示登录入口", await page.$(".st-signin") === null, "");

  // ---- 对话云同步：登录后对话跟着账号走（换设备看到同一批）
  await page.goto(`http://127.0.0.1:${PORT}/__loggedout?0`, { waitUntil: "networkidle2" });
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await new Promise((r) => setTimeout(r, 1500));
  await page.waitForSelector(".side__cloud", { timeout: 8000 });
  await page.waitForFunction(() => {
    const b = document.querySelector(".side__cloud");
    return b && /已同步到云端|只在本机/.test(b.textContent);
  }, { timeout: 8000 });
  // 在页面里直接往「云端」塞一条别的设备的会话，然后立刻同步 ——
  // 不能靠导航过去塞：那中间的自动回写会先把它冲掉。
  await page.evaluate(() => fetch("/__cloud?seed=1"));
  await new Promise((r) => setTimeout(r, 200));
  await page.evaluate(() => document.querySelector(".side__cloud").click());      // 手动同步（拉+合并+回写）
  await page.waitForFunction(() => JSON.parse(localStorage.getItem("strata.chats") || "[]")
    .some((c) => c.title === "另一台设备上的对话"), { timeout: 8000 }).catch(() => {});
  const cloudTxt = await page.$eval(".side__cloud", (e) => e.textContent.trim());
  ck("登录后侧栏标出同步状态", /已同步到云端|只在本机/.test(cloudTxt), cloudTxt);
  ck("云端那条「另一台设备」的会话被合并进列表",
     (await page.$$eval(".side__item, .side-chat, .chat-item", (els) => els.map((e) => e.textContent).join(" "))).includes("另一台设备")
     || (await page.evaluate(() => JSON.parse(localStorage.getItem("strata.chats") || "[]").some((c) => c.title === "另一台设备上的对话"))), "");
  ck("同步状态落在本机存储里（刷新不丢）",
     (await page.evaluate(() => JSON.parse(localStorage.getItem("strata.chats") || "[]").length)) >= 1, "");

  // ---- 手机端适配（390px，iPhone 14/15 的宽度）
  // 这一档踩过两个真坑，所以断言写得很具体：
  //   ① .st-header__brand 的 font-size:0 被 App.vue 的 scoped 规则压住（那边带 [data-v-*]，
  //      特异性更高），品牌文字没藏掉，页头被撑到 607px；
  //   ② .app 作为 flex 子项默认 min-width:auto，拒绝收缩到内容宽度以下，
  //      于是整页按 498px 布局、右边被 body 的 overflow:hidden 裁掉 —— 正文全是残的。
  await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await page.goto(`http://127.0.0.1:${PORT}/#chat`, { waitUntil: "networkidle2" });
  await page.waitForSelector(".app", { timeout: 8000 });
  await new Promise((r) => setTimeout(r, 600));

  const mob = await page.evaluate(() => {
    // 视口真值必须取 documentElement.clientWidth，**不能**用 window.innerWidth：
    // isMobile 模拟下页面一旦横向溢出，布局视口会被溢出内容顶大（实测 390 的屏报 480），
    // 拿被顶大的 innerWidth 当阈值 = 这条断言对「溢出本身」是瞎的，永远测不出来。
    const cw = document.documentElement.clientWidth;
    const over = (root) => [...document.querySelectorAll(`${root} *`)]
      .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right > cw + 1; }).length;
    return {
      vw: cw,
      bodyScrollW: document.body.scrollWidth,
      app: Math.round(document.querySelector(".app").getBoundingClientRect().width),
      main: Math.round(document.querySelector("main").getBoundingClientRect().width),
      headerBad: over(".st-header"),
      bodyBad: over("main"),
      sidePos: getComputedStyle(document.querySelector(".side")).position,
      sideRight: Math.round(document.querySelector(".side").getBoundingClientRect().right),
      menuVisible: getComputedStyle(document.querySelector(".chat-menu")).display !== "none",
      tabs: [...document.querySelectorAll(".st-tab")].map((e) => {
        const r = e.getBoundingClientRect();
        return { view: e.dataset.view, w: Math.round(r.width), left: Math.round(r.x), right: Math.round(r.right) };
      }),
    };
  });
  ck("手机端整页按视口宽度布局（不横向溢出）", mob.app === mob.vw && mob.main === mob.vw,
     `app=${mob.app} main=${mob.main} 视口=${mob.vw}`);
  // 页面级溢出（= 手机上整页被缩小、右边一片空白）最直接的定义就是这一个数字。
  ck("手机端整页没有横向滚动（body.scrollWidth == 视口）", mob.bodyScrollW === mob.vw,
     `scrollWidth=${mob.bodyScrollW} 视口=${mob.vw}`);
  ck("手机端页头没有元素越界", mob.headerBad === 0, `${mob.headerBad} 个`);
  ck("手机端正文没有元素越界", mob.bodyBad === 0, `${mob.bodyBad} 个`);
  ck("手机端会话栏是覆盖式（fixed）", mob.sidePos === "fixed", mob.sidePos);
  ck("手机端会话栏默认收起（整块在屏幕外）", mob.sideRight <= 1, `right=${mob.sideRight}`);
  ck("手机端有叫出会话栏的按钮", mob.menuVisible, "");
  // 页头折成两行：标签独占一行平分宽度 —— 之前挤在一行里，标签只露出两个半，等于切不了页。
  // **个数拿桌面上方那一组（tabs）比，不写死数字**：2026-10-28 加「训练」时这里写死的 5 就过期了，
  // 报错信息看不出是页面问题还是断言问题。
  ck("手机端视图标签与桌面同数、全都看得见点得着",
     mob.tabs.length === tabs.length && mob.tabs.every((t) => t.w >= 40 && t.left >= 0 && t.right <= mob.vw),
     `手机 ${mob.tabs.length} / 桌面 ${tabs.length} ${JSON.stringify(mob.tabs)}`);

  await page.click(".chat-menu");
  await page.waitForFunction(() => document.querySelector(".side").getBoundingClientRect().left >= -1, { timeout: 3000 });
  ck("点按钮后会话栏滑进来", true, "");
  // 点抽屉**右边**的遮罩区（page.click 会点元素中心，而遮罩中心正被抽屉盖着）
  await page.mouse.click(370, 400);
  await page.waitForFunction(() => document.querySelector(".side").getBoundingClientRect().right <= 1, { timeout: 3000 });
  ck("点遮罩收起会话栏", true, "");

  // 手机上**不藏关键信息**：速率、当前步骤、槽位与 GPU 都得看得见
  ck("手机端胶囊仍显示文字（速率不再被整块藏掉）",
     (await page.$eval(".st-pill > span:last-child", (e) => getComputedStyle(e).display)) !== "none", "");
  await page.click('.st-tab[data-view="viz"]');
  await page.waitForSelector(".canvas__svg", { timeout: 8000 });
  const vizMob = await page.evaluate(() => {
    const vis = (s) => { const e = document.querySelector(s); return e ? getComputedStyle(e).display !== "none" : false; };
    return { hud: vis(".canvas__hud"), hint: vis(".canvas__hint"), crumb: vis(".canvas__crumb"), zoom: vis(".canvas__zoom") };
  });
  ck("手机端画布上的面包屑与缩放都还在", vizMob.crumb && vizMob.zoom, JSON.stringify(vizMob));
  // 没开始演示时那条长提示在手机上不占地方（右栏下面就是同一句）；**演示一开始它必须出现**
  ck("手机端未开始时不在画布上摆长提示", vizMob.hud === false, JSON.stringify(vizMob));
  await page.click(".bar__go");
  await page.waitForSelector(".canvas__hud[data-on]", { timeout: 12000 });
  ck("手机端演示中看得见当前步骤与张量形状", true,
     (await page.$eval(".canvas__hud", (e) => e.textContent.replace(/\s+/g, " "))).slice(0, 30));
  await page.click('.st-tab[data-view="monitor"]');
  await page.waitForSelector(".metric-card", { timeout: 6000 });
  await page.click('.st-tab[data-view="records"]');
  await page.waitForSelector(".rec-table .st-table", { timeout: 8000 });
  await page.waitForFunction(() => document.querySelectorAll(".rec-row").length === 3, { timeout: 8000 }).catch(() => {});
  const mobRec = await page.evaluate(() => {
    const card = document.querySelector(".rec-table");
    const wrap = card.querySelector(".table-wrap");
    return { rows: document.querySelectorAll(".rec-row").length,
             pageW: Math.round(document.querySelector("main").scrollWidth),
             vw: document.documentElement.clientWidth, bodyW: document.body.scrollWidth,
             cardW: Math.round(card.getBoundingClientRect().width),
             scrolls: wrap.scrollWidth > wrap.clientWidth + 1 };
  });
  // 表格宽是正常的（11 列），要点是**页面本身**不被撑破、且宽出来的部分由卡片内部横向滚动消化
  ck("手机端记录页不撑破页面（宽表格自己在卡片里横向滚）",
     mobRec.pageW <= mobRec.vw + 1 && mobRec.cardW <= mobRec.vw && mobRec.scrolls,
     JSON.stringify(mobRec));
  await page.click(".rec-row");
  await page.waitForSelector(".rec-detail", { timeout: 5000 });
  const mobDet = await page.evaluate(() => Math.round(document.querySelector(".rec-detail").getBoundingClientRect().width));
  ck("手机端详情面板整屏宽（不是半张卡）", mobDet >= 380, `${mobDet}px`);
  await page.click(".rec-detail .st-btn--icon");
  await page.waitForFunction(() => !document.querySelector(".rec-detail"), { timeout: 3000 });
  await page.click('.st-tab[data-view="monitor"]');

  ck("手机端通道条的槽位占用 / GPU 数字仍在",
     (await page.$eval(".lane-key__meta", (e) => getComputedStyle(e).display)) !== "none", "");

  // 逐卡区块手机上不能被藏掉（GPU 是否被挤爆是移动端最想看的东西之一），四格要降成两列才不顶宽
  const gpuMob = await page.evaluate(() => {
    const lane = document.querySelector(".gpu-lane");
    const grid = document.querySelector(".gpu-lane__metrics");
    return { lanes: document.querySelectorAll(".gpu-lane").length,
             cols: grid ? getComputedStyle(grid).gridTemplateColumns.split(" ").length : 0,
             metrics: document.querySelectorAll(".gpu-metric").length,
             chartW: (() => { const e = document.querySelector(".gpu-charts .lc"); return e ? Math.round(e.getBoundingClientRect().width) : 0; })(),
             vw: document.documentElement.clientWidth };
  });
  ck("手机端逐卡区块仍在（一张卡一块 + 四格）", gpuMob.lanes >= 1 && gpuMob.metrics >= 4, JSON.stringify(gpuMob));
  ck("手机端逐卡四格降为两列", gpuMob.cols === 2, `cols=${gpuMob.cols}`);
  ck("手机端逐卡图不越出视口", gpuMob.chartW > 0 && gpuMob.chartW <= gpuMob.vw, `chartW=${gpuMob.chartW} vw=${gpuMob.vw}`);
  // 窄屏是时间戳最容易叠在一起的地方（绘图区被压到 300px 上下），同一套判据再来一遍
  const tickMob = await tickCheck("手机");
  ck("手机端：折线图 x 轴时间戳照样互不压叠（挤不下就少画一个）",
     tickMob.charts > 0 && !tickMob.bad.length, JSON.stringify(tickMob));

  // 底部的「GPU 详细数据」同样不能被藏掉：一张卡一组、单列铺开，五行的曲线跟着缩
  const gpuDetailMob = await page.evaluate(() => {
    const grid = document.querySelector(".gpu-cards"), card = document.querySelector(".gpu-card");
    return { cards: document.querySelectorAll(".gpu-card").length,
             lines: card ? card.querySelectorAll(".gpu-line").length : 0,
             cols: grid ? getComputedStyle(grid).gridTemplateColumns.split(" ").length : 0,
             cardW: card ? Math.round(card.getBoundingClientRect().width) : 0,
             pageW: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth };
  });
  ck("手机端：GPU 详细数据仍按卡一组、五行齐全", gpuDetailMob.cards >= 1 && gpuDetailMob.lines === 5,
     JSON.stringify(gpuDetailMob));
  ck("手机端：详细数据收成单列、不撑宽页面",
     gpuDetailMob.cols === 1 && gpuDetailMob.cardW > 0 && gpuDetailMob.pageW <= gpuDetailMob.vw + 1,
     JSON.stringify(gpuDetailMob));

  // ---- 手机端：回答里的「撑不破的内容」不能把整页顶宽（脏内容守卫）
  // 2026-11-01 实测：助手气泡没有 overflow-wrap，一段 300 字符不间断的长串把整页顶到 **752px**
  // （页头还是 390px，右边一片空白，真机被整页缩小 ~0.5×）；宽 markdown 表同理。
  // 这类内容只有模型会吐出来，夹具里编不出来，所以直接拿真脏文本压。
  const savedChats = await page.evaluate(() => localStorage.getItem("strata.chats"));
  // 表格必须是**连续的行**（中间不能有空行）才会被 markdown 认出来 —— 用 \n 拼，别 join("\n\n")。
  // 格子里放**不可断的拉丁长词**：中文按字就能断行，那样表格本来就放得下、滚不起来，
  // 断言会变成「测了个寂寞」。要测的就是「表格实在比屏幕宽」这一种情形。
  const W = "supercalifragilisticexpialidocioussupercalifragilisticexpialidocious";
  const tableMd = [`| ${W} | 列二 | 列三 | 列四 | 列五 |`, "| --- | --- | --- | --- | --- |",
    `| ${W} | b1 | c1 | d1 | e1 |`, `| ${W} | b2 | c2 | d2 | e2 |`].join("\n");
  await page.evaluate((tbl) => {
    localStorage.setItem("strata.chats", JSON.stringify([{ id: "cnasty", title: "脏内容", createdAt: Date.now(),
      updatedAt: Date.now(), messages: [
        { role: "user", text: "压一下宽度", time: Date.now() },
        { role: "assistant", time: Date.now(), text: [
          "AAAAAAAAAA".repeat(30),
          "https://example.com/" + "very-long-path-segment/".repeat(12),
          tbl,
        ].join("\n\n") }] }]));
    localStorage.setItem("strata.chatActive", '"cnasty"');
    location.hash = "#chat";                  // 上一段用例停在 #monitor；#chat 才能看见气泡）
  }, tableMd);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".st-msg--assistant .st-bubble", { timeout: 8000 });
  await new Promise((r) => setTimeout(r, 800));
  const nasty = await page.evaluate(() => {
    const cw = document.documentElement.clientWidth;
    const t = document.querySelector(".st-bubble table");
    return { cw, bodyW: document.body.scrollWidth,
             bubbles: [...document.querySelectorAll(".st-bubble")].map((e) => Math.round(e.getBoundingClientRect().width)),
             table: t ? { w: Math.round(t.getBoundingClientRect().width), scrolls: t.scrollWidth > t.clientWidth + 1 } : null };
  });
  // bubbles 里必须出现**有宽度**的气泡：整页被 v-show 藏掉时全是 0，那是一条空过的断言
  ck("手机端：回答里的长串不撑破页面（body.scrollWidth == 视口）",
     nasty.bodyW === nasty.cw && nasty.bubbles.some((w) => w > 0) && nasty.bubbles.every((w) => w <= nasty.cw),
     JSON.stringify(nasty));
  ck("手机端：宽表格在气泡里自己横向滚（不是把页面撑宽）",
     !!nasty.table && nasty.table.w > 0 && nasty.table.w <= nasty.cw && nasty.table.scrolls, JSON.stringify(nasty.table));
  await page.evaluate((s) => { if (s) localStorage.setItem("strata.chats", s); else localStorage.removeItem("strata.chats"); },
                     savedChats);

  await page.setViewport({ width: 1440, height: 1000 });

} finally {
  await browser.close();
  server.close();
}

console.log("=".repeat(58));
if (FAILS.length) { console.log(`FAIL：${FAILS.length} 项未通过 -> ${JSON.stringify(FAILS)}`); process.exit(1); }
console.log("ALL PASS");
