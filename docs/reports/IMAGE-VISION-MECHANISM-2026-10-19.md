# 图片处理机制与用户用法（2026-10-19）

> 当前状态：两条 lane 都开着 vision；`rag-proxy` 已关闭 RAG（`rag_enabled=false`），
> **图片不再经过网关的估算/组装，纯透传给 serve + 引擎的 vision encoder**。
> 本文讲清：图从客户端到引擎经过什么、用户怎么发、token 怎么算、有什么限制。

---

## 1. 一句话机制

```
客户端（OpenAI image_url / Anthropic image block / 终端 /image）
   ▼
rag-proxy :8080（no-RAG：原样透传，不做图片估算/组装）
   ▼
Strata serve（server.py）
   · frontend._image_source() 统一成 data: / http(s): / 本地路径
   · images_of() 抽出图片源
   · Vision.encode()：加载 → 格式归一化 → 调 strata-vision（mmproj）→ (embeddings, N tokens)
   · 把模板里的一个 <|image_pad|> 替换成 N 个 image pad
   ▼
C++ 引擎（strata --serve --vision）：消费图片 embeddings，图片 token 计入上下文
```

- 编码器：`/home/cloud/Strata/engine/strata-vision`（llama.cpp mtmd + `mmproj-Qwen3.8-Flash-Next-BF16.gguf`）。
- 两条 lane 的 config 都是 `"gpu": true`、`"max_tokens": 1024`：
  - **单张图最多 1024 个 image token**；640×480 约 300；96×96 很小。
  - GPU 编码 0.1–0.5 s/张；图片按 SHA-256 缓存，同一张图（每轮都发）只编码一次。
- 图片 token 计入 **262144** 上下文；现在没有 RAG 组装，超窗就是引擎 400
  （代理会按引擎报的余量压一次 max_tokens，但压不掉 prompt 里的图片 token）。

---

## 2. 用户侧怎么发

### 2.1 OpenAI Chat Completions（`/v1/chat/completions`）

`image_url` 的 `url` 可以是 **data: URL、http(s) URL 或本地文件路径**：

```python
import base64
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="<你的 key>")
img = base64.b64encode(open("photo.jpg", "rb").read()).decode()
r = client.chat.completions.create(
    model="qwen3.8-flash-fast",          # A 快道；qwen3.8-flash = B 普道
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "这张图里是什么？"},
        {"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{img}"}},
    ]}],
    max_tokens=256,
)
print(r.choices[0].message.content)
```

也可以：

```python
{"type": "image_url", "image_url": {"url": "https://example.com/x.jpg"}}
{"type": "image_url", "image_url": {"url": "/home/cloud/photo.jpg"}}   # serve 进程能读到的本地路径
```

### 2.2 Anthropic Messages（`/v1/messages`）

图片用 `image` block，`source` 支持 `base64` 或 `url`：

```python
import base64, anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:8080", api_key="<你的 key>")
img = base64.b64encode(open("photo.jpg", "rb").read()).decode()
r = client.messages.create(
    model="strata-a",
    max_tokens=256,
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "这张图里是什么？"},
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": img}},
    ]}],
)
```

`source` 也可以写成 `{"type": "url", "url": "https://..."}`。

### 2.3 终端 / 其他客户端

- 终端聊天：`/image <图片路径>`，再输入问题。
- Web 聊天：图片上传走同一套 `image_url` / `image` 解析。
- 外部中转站 `api.ssemarket.cn`：Pod 侧两种协议都支持；中转站是 Anthropic→OpenAI 转译层，
  图片 block 是否透传取决于它的转译实现。**建议用同一张图走一次外部出口验证**；
  如果外部丢图，直接用 Pod 的 OpenAI 入口 `image_url`。

---

## 3. 格式、缓存与 token

### 3.1 支持的格式

- 直接透传：**JPEG、PNG、BMP、GIF**（`strata-vision` 的 stb_image 能读）。
- 需要 Pillow：**WebP、TIFF、AVIF** 等 → serve 里用 Pillow 打开、转成 RGB PNG 再送编码器
  （透明区域填白）。
- 读不出来 → 400，错误信息说明图片无法读取。

### 3.2 缓存

- **图片编码缓存**：`Vision.cache` 按图片 SHA-256 缓存 `(embeddings, N tokens)`，
  同一张图重复发送只编码一次。
- **前缀缓存**：同一图片 + 同一前缀的对话，引擎可以复用；实测第二次同图请求
  Anthropic usage 出现 `cache_read_input_tokens`。

### 3.3 实测 token 数（2026-10-19，A/8091，经 8080）

| 图片 | 请求 | prompt tokens | 结果 |
|---|---:|---:|---|
| 96×96 红方块 | OpenAI `image_url` + 一句问题 | **33** | content ` red`，0.9 s |
| 96×96 红方块（再发一次） | Anthropic `image` block | **33**（input 7 + cache_read 26） | content ` red`，0.2 s |
| 640×480 蓝底 | OpenAI `image_url` + 一句问题 | **324**（约 300 是图片） | content ` Blue`，1.3 s |
| 96×96 红方块 | B/8092 `qwen3.8-flash` | **33** | content ` red`，0.8 s |

> 图片 token 直接体现在 `usage.prompt_tokens` / `prompt_tokens_details` 和
> `~/logs/infer.db` 的 `prompt_tokens` 里；单图上限 1024。

### 3.4 单图压缩/限制：有 token 上限，没有文件级自动压缩

- **有自动缩放（token 级）**：serve 启动 `strata-vision` 时传 `--max-tokens 1024`
  （`Strata/tools/vision/strata_vision.cpp:105` 的 `cp.image_max_tokens = max_tokens`）。
  mtmd 在预处理时会把大图**缩放到这个 token 上限**（源码注释：*“A square image well above any
  cap; mtmd scales it to the token limit.”*）。所以单图 token 不会无限增长。
- **没有文件级压缩/重采样**：`Strata/serve/server.py` 的 `Vision.normalize()` 只做格式归一化：
  JPEG/PNG/BMP/GIF 直接过；WebP/TIFF/AVIF 等用 Pillow 转 PNG（透明填白）。
  它**不按文件大小/像素尺寸做 JPEG 质量压缩、不重采样**；原始文件仍会被完整读取/解码，
  只是送进 mtmd 后按 token 上限缩放。
- **没有按上下文余量动态降级**：不会因为 prompt 快满 256K 就把图压到更小；单图始终最多 1024 token。
  如果 prompt 本身接近 256K，图片再小也可能 400。
- **代理层**：`rag-proxy` 的 `image_tokens=1024` 只是 RAG 打开时的预算估算；
  现在 `rag_enabled=false`，代理不估算、不压缩，直接透传。
- **实测（2026-10-19，A/8091，经 8080）**：

| 图片尺寸 | 总 prompt tokens | 说明 |
|---|---:|---|
| 96×96 | 33 | 小图，image 部分很小 |
| 640×480 | 324 | 约 300 是图片 |
| **4000×3000** | **996** | 大图被 mtmd 缩放到 ~1024 token 上限，不是 12MP 的巨量 token；2.1 s |

> 结论：**单图有 1024 token 的硬上限，大图会自动缩放；但没有文件级自动压缩/质量重编码**。
> 如果还想要“按像素/文件大小自动压缩”，需要在 serve 层加 `max_side`/`max_pixels`/`max_bytes`
> 之类的预处理；目前没有。

---

## 4. 限制与注意

1. **图片计入 262144（256K）**：RAG 关闭后没有组装，长文本 + 图片超窗就是引擎 400；
   客户端需要自己控制总 token。
2. **单图最多 1024 token**：640×480 ≈ 300；更大的图会到 1024 上限。
   如果一张图让上下文“从 100k 直接超 1M”，先检查是不是发了多张图/超大图，
   以及客户端自己的图片展开逻辑；Pod 侧每图上限是 1024。
3. **本地路径只对 serve 有效**：`/home/cloud/...` 这类路径是 serve 进程读的；
   远程客户端请用 data URL 或 http(s)。
4. **编码器在 GPU 上**：约占 1.4 GB 显存，文本吞吐会低几个百分点（官方实测 2–8%）；
   `"cuda_device"` 可以指定编码器单独用一张卡（当前未配）。
5. **RAG 打开时**才会用代理的 `image_tokens=1024` 估算；现在是 `rag_enabled=false`，
   代理不估算、不组装，图片 token 以引擎实际计数为准。
6. 图片编码器进程随 serve 启动；重启 serve 会清空图片编码缓存（对话前缀缓存另算）。

---

## 5. 验证命令

```bash
# 两条 lane 是否开了 vision
curl -s http://127.0.0.1:8091/health | python3 -c 'import sys,json; d=json.load(sys.stdin); print("A images",d["images"])'
curl -s http://127.0.0.1:8092/health | python3 -c 'import sys,json; d=json.load(sys.stdin); print("B images",d["images"])'

# 发一张图（OpenAI 格式，经 8080）
KEY=$(cat ~/logs/strata-api-key.txt)
B64=$(base64 -w0 /tmp/test-red.png)
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:8080/v1/chat/completions \
  -d "{\"model\":\"qwen3.8-flash-fast\",\"messages\":[{\"role\":\"user\",\"content\":[
       {\"type\":\"text\",\"text\":\"What color is this image? Answer one word.\"},
       {\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,$B64\"}}]}],
       \"max_tokens\":64,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["choices"][0]["message"]["content"], d["usage"])'
```

对应代码：

| 位置 | 作用 |
|---|---|
| `Strata/serve/frontend.py` `IMAGE_PARTS` / `_image_source` / `_parts_of` / `images_of` | 解析 OpenAI/Anthropic 图片部件，统一成 source |
| `Strata/serve/server.py` `Vision` | 加载/格式归一化/缓存/调 `strata-vision`/返回 token 数 |
| `Strata/serve/server.py` `Service.prepare` | 用 `N` 个 image pad 替换模板里的一个 `<|image_pad|>` |
| `Strata/serve/server.py` `--vision` / config `vision` | 启动编码器；`max_tokens:1024`、GPU/CPU |
| `rag-proxy/rag_proxy.py` `count_image_blocks` / `image_allowance` | **仅 RAG 路径使用**；当前 no-RAG 不调用 |

> 一句话：**Pod 侧图片能力是 serve + 引擎的 `strata-vision` 提供的；当前 RAG 关闭不影响图片，
> 图片按实际 token 计入 256K 上下文。**
