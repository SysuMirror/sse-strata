# 网关文档自动识别与抽取（PDF / DOCX / XLSX / CSV / 文本）— 2026-10-19

> 背景：上游直接发 PDF/DOC/Excel 时，旧链路会把 `document`/`file` part **静默丢弃**；
> 把原始字节塞进 `image` 会 **400**；上游抽成整段文本后，大文档又会直接顶爆 262144 窗口。
> 本次在 **rag-proxy 网关**加了一层文档预处理：自动识别格式、抽成可读文本（扫描 PDF 渲染成页面图），
> 并按**剩余上下文预算**截断。**RAG 关闭（`rag_enabled=false`）时同样生效**，不需要改引擎。
>
> 代码：`rag-proxy/doc_extract.py`（新）、`rag-proxy/rag_proxy.py`（接线）、
> `rag-proxy/test_doc_extract.py`（12 个单测）；配置：`rag-proxy/config.json` 的 `documents` 段。

---

## 1. 行为总表（实现后）

| 上游发送方式 | 网关行为 | 模型看到什么 |
|---|---|---|
| Anthropic `{"type":"document", "source":{"type":"base64","media_type":"application/pdf"...}}` | 识别 PDF，`pypdf` 抽文本 | `[document: ...]` + 逐页文本 |
| Anthropic `document` 的 `source.type="text"` / `"content"` | 直接用文本块 | 文本 |
| OpenAI `{"type":"file"/"input_file", "file":{"file_data":"data:...;base64,..."}}` | 按 MIME/magic 识别并抽文本 | `[document: ...]` + 文本 |
| OpenAI/Anthropic 把 PDF/DOCX/XLSX 放进 `image_url`/`image` | **识别为文档**（不再 400） | 文本；扫描件为页面图 |
| `image_url` / `image` 里是真正的图片（PNG/JPEG/...） | **原样保留**，继续走 vision | 图片（行为不变） |
| 扫描版 PDF（无文字层） | `pymupdf` 渲染页面为 PNG，按预算最多 8 页 | 页面图（vision，单页约 1024 token） |
| DOCX | `python-docx` 抽段落 + 表格（`a \| b`） | 文本 |
| XLSX / XLS | `openpyxl` / `xlrd` 抽工作表，最多 500 行 × 30 列 | `--- sheet: ... ---` + `a \| b` |
| CSV / TXT / Markdown | 按文本解码（UTF-8/GB18030/UTF-16/latin-1） | 文本 |
| HTML / RTF | 去标签/控制字 | 文本 |
| 旧版 `.doc` | **明确 400**：转 `.docx`/PDF/文本 | — |
| PPTX | **明确 400**：暂不支持，转 PDF/文本 | — |
| 超过预算的文档 | 头部 70% + 尾部 30%，中间加 `[... document truncated: N characters omitted ...]` | 头尾 + 截断标记 |
| 文档 + 对话本身已超窗 | **400**：`document + conversation leaves no room...` | — |

> 关键点：**引擎侧零改动**；文档在网关变成普通 text/image part，再走原来的 OpenAI/Anthropic 链路。

---

## 2. 预算算法（避免“一下子超上下文”）

网关在每个请求上计算文档可用的 token 预算：

```text
other   = 估算(去掉文档后的 messages + system + tools + 真实图片)
max_new = 客户端 max_tokens / max_completion_tokens（缺省 8192，夹在 [0, 262144]）
room    = 262144 - ctx_slack(8) - other - max_new - reserve_tokens(4096)
doc_budget = min(documents.max_tokens(120000), room)

若 doc_budget < 256 -> 明确 400（不静默丢文档、也不把必然 400 的请求放出去）
否则把 doc_budget 按文档数量分配；文本用头 70% + 尾 30% 截断；
扫描件每页按 1024 token 计入，最多 max_pages=8 页。
```

- `other` 用代理已有的 `est_prompt_tokens()`（按字符分类的上界估算，含每消息开销、真实图片展开）。
- **不再把 `max_tokens` 夹到 `gen_reserve_max` 后再算文档预算**：RAG 关闭时请求原样转发，
  客户端要生成 200K 就必须按 200K 留空间，否则文档会把 prompt 顶爆。
- 文档头会写明来源：`[document: 文件名 | 格式 | 页数 | 字符数 | truncated: N chars omitted]`。

### 配置（`config.json` → `documents`）

| 键 | 默认 | 含义 |
|---|---:|---|
| `enabled` | `true` | 总开关；关掉后完全回到旧行为 |
| `allow_urls` | `false` | **公网网关默认不替客户端抓 URL**（防 SSRF）；要求 base64/`file_data` |
| `max_file_mb` | 20 | 单文件上限（解码前也按 4/3 预检） |
| `max_tokens` | 120000 | 文档文本总预算上限 |
| `reserve_tokens` | 4096 | 对话/系统提示的安全余量 |
| `truncate_head_ratio` | 0.7 | 截断时头部比例 |
| `min_text_chars` | 200 | PDF 文字层过少的判定辅助 |
| `pdf_render_scanned` | `true` | 扫描 PDF 渲染页面图 |
| `max_pages` | 8 | 扫描件最多渲染页数 |
| `page_tokens` | 1024 | 每页图像的预算 |
| `pdf_dpi` / `max_image_side` | 150 / 2048 | 渲染 DPI / 长边上限 |
| `max_xlsx_rows` / `max_xlsx_cols` | 500 / 30 | 表格抽取上限 |

---

## 3. 安全边界

- **不读客户端给的本地路径**：`/etc/passwd`、`file:///...` 一律 400，避免把容器文件读进 prompt。
- **默认不抓 URL**：`documents.allow_urls=false`；需要远程 URL 时才显式打开（并自行评估 SSRF）。
- 只允许 data URL / base64 / 客户端提供的 `file_data`。
- 单文件 20MB 上限；base64 解码前预检，避免超大 payload 占内存。
- 不把文档原文写入日志；只记 `N chars / N page images / budget`。

---

## 4. 实测（2026-10-19，临时代理 :18080 + 生产 :8080 → 快道 A/8091）

> 临时实例验证后，生产代理已于 **2026-10-19 13:56（北京时间）** 重启（`rag-proxy` only，
> 引擎 8091/8092 未重启）；下面的 §4.2 是临时实例结果，§4.3 是生产 8080 的真实结果。

### 4.1 单测（无 GPU）

```bash
/home/cloud/Strata/.venv/bin/python ~/rag-proxy/test_doc_extract.py
# Ran 12 tests ... OK
```

覆盖：PDF/DOCX/XLSX/CSV/图片识别；Anthropic `document`；OpenAI `file`；PDF 当 `image_url`；
扫描 PDF 渲染；头尾截断；多文档；旧 `.doc` 报错；预算过小报错；本地路径/URL 拒绝；真实图片跳过。

### 4.2 端到端

| 用例 | 请求 | 结果 |
|---|---|---|
| PDF（文字版）当 `image_url` | `data:application/pdf;base64,...`，问 secret code | **HTTP 200，prompt 103，content `ZEBRA-42`** |
| Anthropic `/v1/messages` + `document` PDF | 同一份 PDF | **HTTP 200，input 102，content `ZEBRA-42`** |
| OpenAI `file` + DOCX | 问 launch code | **HTTP 200，prompt 113，content `DOCX-777`** |
| 扫描版 PDF（图片里写 `SCAN-CODE-888`） | OpenAI `file` + `application/pdf` | **HTTP 200，prompt 1104（1 页图），content `SCAN-CODE-888`** |
| 264,029 字符大文本（临时预算 4000） | 首尾各埋一个 code | **HTTP 200，prompt 2226，头尾两个 code 都答对**；日志 `doc_truncated` |
| 旧版 `.doc` | `application/msword` | **HTTP 400**：`legacy .doc is not supported...` |
| 普通 PNG 红图 | `image_url` | **HTTP 200，prompt 87，content `Red`**；文档计数不增加 |

> 说明：大文本测试把临时实例的 `documents.max_tokens` 降到 4000，验证“截断 + 头尾保留 + 不超窗”；
> 生产默认 120000，公式保证 `120000 + 对话 + max_tokens + 4096 ≤ 262144`（在文档预算未被 room 进一步压低时）。
> 生产规模的 120K 截断没有单独压测（当时宿主机 GPU 显存压力 94%，避免再压一个长 prefill）。

### 4.3 生产 :8080 验证（重启后）

| 用例 | 结果 |
|---|---|
| PDF 当 `image_url`（`data:application/pdf`） | **HTTP 200，0.7 s，prompt 103，content `ZEBRA-42`** |
| DOCX 当 `file` | **HTTP 200，1.0 s，prompt 113，content `DOCX-777`** |
| 扫描 PDF 当 `file`（页面图 `SCAN-CODE-888`） | **HTTP 200，2.6 s，prompt 1103，content `SCAN-CODE-888`** |
| 普通 PNG 红图（回归） | **HTTP 200，0.7 s，prompt 87，content `Red`**；`documents_*` 计数不增加 |
| 流式 PDF（`stream:true`） | **HTTP 200，`text/event-stream`，SSE 正常**；首个 delta 即带 `engine_version` |
| `/health` | `documents_parsed=4, documents_chars=202, documents_truncated=0, documents_images=1, documents_errors=0` |
| 引擎 | 8091/8092 均仍在线（未重启） |

---

## 5. 已知限制

1. **PPTX 未实现**；旧 `.doc` 未实现（无 antiword/soffice），返回明确 400。
2. **扫描 PDF 只做页面渲染，不做 OCR**；文字由 vision 模型读取，版面复杂/字太小时可能不准。
3. **只扫描顶层 `messages[].content` 的 part**；Anthropic `tool_result.content` 里嵌套的 `document`
   暂不处理（如需要可扩展递归 walker）。
4. **表格是简化文本**（`a | b`），不保留合并单元格/样式；超大表按 500×30 截断。
5. **截断是头尾保留，不是摘要**；中间内容会标 `omitted`，需要全文时上游仍应分块/摘要。
6. **文档解析在请求线程内同步执行**；超大 PDF/DOCX 会增加 TTFT（目前 20MB 上限）。
7. **URL 默认禁用**；`allow_urls=true` 时没有做 DNS/IP 级别的 SSRF 防护，需自行评估。
8. 只处理 `/v1/chat/completions` 与 `/v1/messages`；`/v1/responses` 的 `input` 数组仍走原透传。
9. 只识别客户端**直接发送**的文档；如果客户端已经自己抽成超长纯文本，网关仍按普通文本处理
   （不会重新分块/摘要），这时需要上游控制或重新打开 RAG。
10. 文档文本若含 `<|endoftext|>`/`<|im_end|>` 等控制 token 字面量：快道 A 的 serve 已做转义；
    普道 B 尚未部署该修复，走 B 时仍可能触发早停（见 `CLAUDE-EARLY-STOP-2026-10-18.md`）。

---

## 6. 部署与回滚

- 改动只在 `rag-proxy`，**不需要重启引擎**；重启代理即可生效：
  ```bash
  CTL="/home/cloud/.local/bin/supervisorctl -c /home/cloud/deploy/supervisord.conf"
  $CTL restart rag-proxy
  ```
- 代理重启是**短暂停服**，按项目红线需先经用户同意；本次已于 **2026-10-19 13:56（北京时间）**
  经用户同意重启，`supervisorctl` 显示 `rag-proxy` RUNNING，引擎未重启。
- 回滚：`config.json` 里 `documents.enabled=false` + 重启（完全回到旧透传）；或恢复旧 `rag_proxy.py`。
- 依赖（已装入 `~/Strata/.venv`）：`pypdf`、`python-docx`、`openpyxl`、`pymupdf`、`xlrd`。
- 验证：
  ```bash
  curl -s localhost:8080/health | jq '.counters | {documents_parsed, documents_truncated, documents_images, documents_errors}'
  ```
  然后发一个带 PDF `file` part 的请求，期望 200 且模型能引用文档内容。

---

## 7. 文件与备份

- 实现：`~/rag-proxy/doc_extract.py`、`~/rag-proxy/rag_proxy.py`、`~/rag-proxy/test_doc_extract.py`
- 配置：`~/rag-proxy/config.json` 的 `documents` 段
- 补丁归档：`archive/patches/gateway-document-extract-2026-10-19/`
- 旧行为报告（历史证据）：`docs/reports/DOCUMENT-FORMATS-HANDLING-2026-10-19.md`
