# Strata 前端（Vue 3 + Vite）

8080 端口上那个网页就是这里构建出来的。原先是 `serve/web/` 下的一堆静态 HTML/JS，已重写为
Vue 3 单页应用：中文为主语言、可切换英文，Monitor 页支持**并发监控**（两条通道 + 每个在途请求），
另有**推理过程 3D 可视化**一页。

## 目录

```
webapp/
  index.html            入口（构建时被 Vite 处理，产出到 ../web/index.html）
  vite.config.js        base=/web/、产物平铺到 ../web、只产出 .js/.css/.svg
  public/               原样拷进产物：设计系统 CSS（tokens/components/app.css）、sprite.svg、logo.svg、fonts/
  brand/image.png       品牌原图（不进产物；用 tools/make-logo.py 生成 public/logo.svg）
  src/
    main.js             挂载 + 语言初始化
    App.vue             外壳：左侧栏 + 页头（视图名/状态胶囊/语言/主题）、视图切换、抽屉、toast
    i18n.js             中英文词表 + setLang()（**加语言只在这里加一列**）
    store.js            全局状态与轮询（/metrics 1s、/concurrency 2s、/mcp 10s）+ 并发趋势采样
    chats.js            多会话：列表/当前会话/落盘（含老版本单会话数据的迁移）
    api.js              格式化、toast、剪贴板、带 key 的 fetch
    markdown.js         极简 Markdown 渲染（先转义再格式化）
    icons.js            sprite 图标
    components/         Icon / SideBar / LineChart / Pager / GeoBackdrop /
                        SparkLine / ToolCall / MessageItem / SamplingDrawer
    views/              ChatView / MonitorView / AboutView
    viz/                推理可视化：store / graph / num / api / model.local.js / components/
  tools/
    make-logo.py        品牌位图 → public/logo.svg（内嵌 base64 PNG）
    make-font.sh        TTF → WOFF2（引擎的 /fonts/ 只放行 woff2，见下）
    make-model-info.py  从本机 GGUF 读出真实架构与张量形状 → src/viz/model.local.js
    test-parse.mjs      解析模型输出的回归测试（纯函数，不用浏览器）
    deploy.sh           构建并安全发布到 ../web（index.html 最后写）
    smoke.mjs           渲染级冒烟测试（模拟引擎路由 + 模拟 API + 真实浏览器）
    verify-live.mjs     对**真实服务**的端到端验证（真 key、真数据）
```

## 界面字体

默认用**腾讯体**（`TencentSans W7`），在「关于 → 设置 → 界面字体」里可以换成 Outfit（只含拉丁）
或系统默认。三条字体栈都带 CJK 兜底族 —— 只写拉丁字体的话中文会掉到宋体上。

原始 TTF 有 8.1MB，`tools/make-font.sh` 转成 3.3MB 的 WOFF2 放进 `public/fonts/`：

```bash
bash tools/make-font.sh /path/to/TencentSans.ttf     # 需要 fonttools + brotli
```

两个坑：

1. **只能给 woff2**。引擎的 `/fonts/` 路由写死了 `name.endswith(".woff2")`，TTF/OTF 会 404；
   而 `/web/` 只认 `.css/.js/.svg`，字体也走不了那边。
2. **`@font-face` 的字重必须写成区间**（`font-weight:100 900`）。腾讯体只有 700 一档，
   声明成单一字重的话浏览器会对它再合成一次加粗，本来就粗的字会糊成一团。
3. 不做子集化：模型输出什么字都可能出现，砍字集等于给自己埋雷。3.3MB 靠 `font-display:swap`
   和引擎的 `Cache-Control: max-age=86400` 扛过去。

## 主题色

主色取自 logo 的蓝（渐变 `#1060f8 → #38b0f8 → #58e0f8`，取主调 `#1a7cf8`；深色下提亮成 `#3d9bff`）。
全部在 `public/tokens.css` 的 `--st-accent*` 一组里，组件不该出现硬编码色值。

## 图表配色

折线图（Monitor 的并发趋势）用 `--st-chart-1..4` 四个分类色，**顺序固定、不循环使用**。
这四色是跑校验脚本挑出来的，不是挑好看：相邻两两在红/绿色盲模拟下 OKLab ΔE 与对底色对比度都要过线。
浅色底一套、深色底另取一套同色相步阶（不是简单翻转）。换色请重新跑校验，别凭眼睛定。

## 构建与发布

```bash
cd ~/Strata/serve/webapp
npm ci                 # 或 npm install（本 Pod 走 npmmirror，见 .npmrc）
npm run build          # 直接产出到 ../web
bash tools/deploy.sh   # 推荐：先构建到暂存目录再逐文件发布，index.html 最后写
```

**前端改动不需要重启引擎**：`serve/server.py` 每个请求都从磁盘重读静态文件。
只有改动代理（`~/rag-proxy/rag_proxy.py`，提供 `/concurrency`）才需要
`supervisorctl -c ~/deploy/supervisord.conf restart rag-proxy`。

## 必须知道的约束（踩过的坑）

1. **引擎只服务扁平文件**：`/web/<名字>` 要求名字里没有 `/`，且后缀只认 `.css` `.js` `.svg`
   （`serve/server.py:2420-2435`）。所以 Vite 的 `assetsDir` 必须为空（产物平铺），
   且任何图片要么是 SVG，要么内嵌进 SVG —— `favicon.ico` / `logo.png` 一律 404。
2. **字体走 `/fonts/`，且只收 woff2**：CSS 里是 `url("../fonts/…")`，CSS 落在 `/web/x.css` 时正好
   解析到 `/fonts/…`。`tokens.css` 的字体栈带 CJK 兜底族（只写拉丁字体的话，中文会掉到宋体上）。
3. **Vue 模板里的资源路径不会被改写 base**：写 `src="/web/logo.svg"` 时 Vite 会当模块解析并**构建失败**；
   用动态绑定（`:src="logoSrc"`）或从 JS 里引用。HTML 里的 `/logo.svg` 才会被 Vite 改写成 `/web/logo.svg`。
4. **页面由引擎服务、并发数据由代理提供**：`/concurrency` 是代理的端点（鉴权同引擎：Bearer / x-api-key）。
   直连引擎（8091/8092）时它 404，页面会**自动隐藏并发区块**、退回原有的单引擎视图。

## 并发视图的行为

数据源：代理的 `GET /concurrency`（各通道 `/v1/status` + `/metrics` 的轮询缓存 + 代理侧在途请求登记表）。

- **通道趋势用折线图**（不是卡片列表）：每个通道一条线，颜色取自 `--st-chart-*`，顺序固定。
  代理只报「此刻是多少」，所以前端每次轮询留一个采样点（`store.js` 的 `concHistory`，
  90 点 × 2 秒 ≈ 3 分钟滚动窗口）。通道掉线时线**断开**而不是掉到 0 —— 0 是个真实的值，会撒谎。
- **两张图分开放**：速度(tok/s) 和在途请求数是两套量纲，各给一根 y 轴。
  绝不画双轴 —— 两个刻度可以随便调，看上去的「相关性」是画出来的、不是数据里的。
- 单路部署自动适配：只有一条线，其余照常。
- 图例即通道状态行：色块 + 通道名 + 此刻的模型/槽位/GPU/显存/在线时长，颜色与折线一一对应。
- 在途请求表：时间 / 通道 / 状态 / 模型 / 已用时 / 首字(TTFT) / 输出 / 速度；流式请求的输出
  按 SSE 事件数计（≈ token 数），非流式按字节估算，界面标注「估算」。**表格分页，每页 8 条。**
- 引擎状态卡会注明它轮询的是哪条通道（`/metrics` 经代理按路径哈希只会落到一条通道）。

## 对话页

- 左侧栏：视图导航 + 「新对话」+ 按日期分组的会话历史（今天 / 昨天 / 更早），可删除并撤销。
- 会话存 `localStorage`（`strata.chats` / `strata.chatActive`），**落盘时会剔掉图片 dataURL 和附件正文**
  —— 那些一次几 MB，几条就能把 5MB 配额顶爆（老版本的单条对话 `strata.chat` 会自动迁移过来）。
- 空态是居中的：大字标题 + 三条建议问题 + 输入框，背景铺细网格与立方体线稿（`GeoBackdrop`）。
- 一页建议问题点一下直接发出去。
- 左侧栏可以收起（页头左边的按钮），收起后回到整幅布局、品牌名回到页头。

## 推理可视化（`src/viz/`）

把一次前向拆成一串可逐步播放的帧：文本 → 分词 → id → 嵌入 →（每层 LayerNorm / Self-Attention /
FFN / 残差）→ logits → 采样。本机那个模型 48 层，共 198 步。支持拖动时间轴、单步、自动播放、
调速、点算子看详情。

**视图是纯 SVG 的线稿**，不用 WebGL、不引 3D 库：

- **柜式投影（cabinet projection）** 把 48 层画成一摞宽板。为什么不用等轴测：等轴测里一块宽板的
  投影高度 =（宽+深）×0.5，宽板会鼓成一个巨大的菱形，48 层根本排不下；柜式投影把深度按比例斜过去，
  宽的仍然是宽的 —— 这正是机械制图里用的画法，也才装得下 48 层。
- 每片板上 4 个小方块 = 那 4 个算子，**四列贯穿整摞板的竖线**就是列间数据流（第 4 列画虚线，因为
  残差本来就是跳连）。当前层高亮，已算过的层淡淡染一层。
- 交互：拖动 = 转投影角度；点一层 = 跳到那层的第一步；点方块 = 选中该算子看详情。
- **体积对比**：原来这一页要额外拉 628KB（three.js，gzip 160KB），现在 **40KB（gzip 14KB）**，
  降了 15 倍；也随之不再需要 GPU 上下文，和整个应用的浅色/深色主题完全一致。

数据：`/api/models`、`/api/models/:id/graph`、`/api/models/:id/infer-steps` 三条接口还不存在
（引擎 404 正是回落到本地生成的触发条件）。模型列表的第一项是**本机实测** —— 架构与张量形状由
`tools/make-model-info.py` 从这台机器上的 GGUF 里读出来，落成 `src/viz/model.local.js`；
参数量是按类别真加出来的，不是估的。界面上如实标注来源（本机实测 / 示例数据 / 真实接口）。

## 测试

```bash
node tools/smoke.mjs /tmp/strata-web-check          # 多路（折线图 + 分页 + 侧栏多会话）
SINGLE=1 node tools/smoke.mjs /tmp/strata-web-check # 单路自动适配
node tools/verify-live.mjs                          # 对真实 8080 验证（需真 key）
node tools/test-parse.mjs                           # 解析模型输出的回归（秒出）
```

覆盖范围包括：中文/英文界面、流式（思考 + 工具调用）、多会话新建/切换/删除/撤销/刷新落盘、
空态建议问题与「换一批」、并发折线图（线数/配色/网格/hover tooltip）、表格分页翻页、
**页头在切标签时一个像素都不动**、登录闸门（只有对话要凭据；填 token 与登录两条路）、
可视化整条链路（48 片板 / 192 个算子标记 / 点板跳层 / 点标记出详情 / 换模型重算）、
采样抽屉、以及**页面无 JS 报错**。

冒烟测试需要一个无头浏览器（`puppeteer`，Chromium 走 npmmirror；系统库见
`libglib2.0-0 libnss3 libgbm1 libasound2t64 …`）。CI 环境里若没有，可只跑 `verify-live.mjs`
的静态部分（curl 资源 200）。

## 回滚

重写前的原前端完整备份在 `../web-legacy-20261015/`：把它拷回 `../web/` 即恢复旧版（引擎无需重启）。
