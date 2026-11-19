# PDF / DOC / Excel 等文档格式的当前处理方式（2026-10-19）

> **2026-10-19 晚更新：网关已实现文档预处理。** 本文记录的是**改造前**的行为与实测证据；
> 现在的 `rag-proxy` 会自动识别 PDF/DOCX/XLSX/XLS/CSV/文本并抽取（扫描 PDF 渲染页面图），
> 按剩余预算截断。实现、配置、测试与限制见
> `docs/reports/DOCUMENT-GATEWAY-EXTRACT-2026-10-19.md`。

> 问题：上游传 PDF、DOC、Excel 文档时，模型会怎么处理？是不是很容易直接超上下文？
> 结论：**Pod 没有文档解析器。** 文档作为 `document`/`file` 部件会被**静默丢弃**；
> 作为 `image` 会**400**；只有上游先抽成**文本**或**页面图片**，模型才看得到。
> 抽成文本后就是普通文本，大小不受文档结构约束——**大文档确实会直接超 256K**。

---

## 1. 行为总表

| 上游发送方式 | Pod 行为 | 模型看到什么 | 会不会超上下文 |
|---|---|---|---|
| Anthropic `{"type":"document", "source":{...pdf...}}` | **静默丢弃**（`anthropic_to_messages` 只处理 text/thinking/tool_use/tool_result） | 只看到同一条消息里的 text；文档内容完全没有 | 不会，但**内容丢失** |
| OpenAI `{"type":"file", "file":{...}}` | **静默丢弃**（`_parts_of`→`_text_of` 只保留 text/input_text） | 只看到同一条消息里的 text | 不会，但**内容丢失** |
| OpenAI `image_url` / Anthropic `image` 里放 PDF/DOC/XLSX 字节 | **400**：`the image could not be read (cannot identify image file ...)` | 不生成 | 不会，快速失败 |
| 上游把文档**抽成文本**再发（普通 text） | 当普通文本处理；没有文档感知的分块/摘要 | 全文/看到多少算多少 | **会**：超过 262144 就 400 |
| 上游把 PDF **逐页渲染成 PNG/JPEG** 再发 image | 走 vision 编码；每页 ≤1024 image token | 看到页面图像（可读文字/版面） | 页数多会累积超窗 |
| DOCX / XLSX 直接发 | 同 document/file：丢弃；当 image 发：400 | 看不到 | — |

---

## 2. 实测证据（2026-10-19，经 8080）

### 2.1 Anthropic `document` block：被丢弃

```json
{"type":"document","source":{"type":"base64","media_type":"application/pdf","data":"<PDF>"}}
```

结果：

```text
HTTP 200，0.7 s
usage: input_tokens 21, output_tokens 22
content: "Please provide the text content of the PDF or upload the file,
          as I cannot access external documents directly."
```

- prompt 只有 21 tokens，**PDF 的字节完全没有进入 prompt**；
- 模型没有报错，但明确说看不到文档。

### 2.2 OpenAI `file` part：被丢弃

```json
{"type":"file","file":{"filename":"x.pdf","file_data":"data:application/pdf;base64,<...>"}}
```

结果：

```text
HTTP 200，0.5 s
usage: prompt_tokens 21, completion_tokens 15
content: "Please provide the content of the file you would like me to summarize."
```

- 同样只有 21 tokens；文件内容没有进入 prompt。

### 2.3 PDF 当 image 发：400

```text
HTTP 400
{"error":{"type":"invalid_request_error",
 "message":"the image could not be read (cannot identify image file ...)"}}
```

- `Vision.normalize()` 只认 JPEG/PNG/BMP/GIF；PDF/DOC/XLSX 不是图片，Pillow 也读不了；
- 不会进入引擎，也不会占上下文。

### 2.4 抽成文本后：能处理，但大文档会超窗

| 文本大小 | token | 结果 |
|---|---:|---|
| 340,000 汉字 | **180,012** | HTTP 200，55.8 s prefill（还在 256K 内，但很慢） |
| 595,000 汉字 | **315,012** | **HTTP 400**：`prompt (315012 tokens) + max tokens (1) exceeds the context (262144); requests are never truncated.` |

- 595k 汉字请求在 16.4 s 后 400，infer 记录 `http_status=400`；
- 现在 RAG 关闭，代理不会把长文本分块/组装；`overfit` 只能压 max_tokens，压不掉 prompt。

---

## 3. 代码上的原因

| 位置 | 行为 |
|---|---|
| `Strata/serve/frontend.py` `_text_of()` | 只拼接 `type in ("text","input_text",None)` 的 part；`file`/`document` 不在内 |
| `Strata/serve/frontend.py` `_parts_of()` | 无 image 时直接走 `_text_of()`；有 image 时只保留 image/text，其他 part 丢掉 |
| `Strata/serve/frontend.py` `anthropic_to_messages()` | 只处理 `text` / `thinking` / `tool_use` / `tool_result`；`document` 分支不存在 |
| `Strata/serve/server.py` `Vision.normalize()` | 只做图片格式归一化（JPEG/PNG/BMP/GIF 直接过；WebP/TIFF/AVIF→PNG）；PDF/DOC/XLSX 不是图片 |
| `rag-proxy` | 现在 `rag_enabled=false`，不解析内容、不分块、不组装，纯透传 |

> 所以：**文档既不会被解析，也不会被自动分块/摘要；唯一的“文档处理”是上游自己做。**

---

## 4. 建议

### 4.1 上游侧（当前最现实）

1. **PDF**：用 `pypdf` / `pdfplumber` 抽文本；如果扫描版，渲染成图片再走 vision。
2. **DOCX**：用 `python-docx` 抽段落/表格，转 Markdown。
3. **XLSX**：用 `openpyxl` / `pandas` 转 CSV/Markdown 表格；大表先筛选/汇总。
4. **控制 token**：抽完后按 256K 预算分块/摘要；不要直接把整本 PDF/整张表塞进一条消息。
5. **扫描件/版式**：渲染页面为 PNG/JPEG（建议长边 ≤2048），逐页作为 image 发送；
   每页 ≤1024 image token，100 页约 100k token，注意总预算。
6. **不要**把 PDF/DOC/XLSX 原始字节塞进 `image_url`/`image`——会 400。

### 4.2 如果希望 Pod 原生支持文档（新功能，需改代码 + 重启）

可以在 `rag-proxy` 或 serve 的 `frontend` 增加一个文档预处理层：

- 识别 `document` / `file` part；
- PDF：抽文本（pypdf/pdfplumber）；扫描页可渲染成图片走 vision；
- DOCX：python-docx；XLSX：openpyxl/pandas；
- 按 token 预算分块；可选先用小模型摘要，再把摘要+关键片段送给主模型；
- 超预算时返回明确 400（而不是静默丢弃）；
- 注意：这会引入 PDF/Office 解析依赖（`pypdf`、`python-docx`、`openpyxl` 等），
  且要定义“扫描件/表格/版式”的降级策略。

### 4.3 最小改进（低成本）

- 当前 `document`/`file` part 被**静默丢弃**，模型只能反问“请提供内容”，用户体验差；
- 可以在 `frontend` 里对不支持的 part 直接返回 400：
  “document/file parts are not supported; send extracted text or page images”，
  避免模型浪费一轮；这是小改动，但需要重启 serve（会冷启动引擎）。

---

## 5. 结论

- **PDF/DOC/Excel 不会被模型“读懂”**：`document`/`file` 被丢弃，`image` 方式 400；
- **只有上游抽成文本或页面图片**，模型才能处理；
- **抽成文本后没有文档感知的分块/组装**（RAG 关闭），大文档会直接 400——
  595k 汉字 = 315k tokens，实测 400；
- 如果这是主业务形态，建议优先在**上游**做文档抽取/分块；需要 Pod 原生支持时，
  再评估加文档预处理层（改代理/serve + 重启）。
