# 引擎版本字段（engine_version / engine_build）

日期：2026-10-18  
状态：**已部署到快道 A（8091，2026-10-18 22:45–22:51 北京时间）**；
普道 B（8092）仍运行旧代码，待另约窗口。A 的响应现在已带 `engine_version` / `engine_build`。

## 1. 为什么

`engine=0.1.38` 是语义版本，legacy 和 ssebed 两个引擎相同，无法判断“切换/重编是否真的生效”。
需求：响应里多一个字段，表示**当前引擎的确切构建**；版本号自动维护——每成功 build 一次 +1。

## 2. 字段契约

以下响应都会多两个字段：

- `GET /health`
- `GET /v1/status`
- OpenAI `POST /v1/chat/completions`（非流式；流式在首个和最后一个 chunk）
- Anthropic `POST /v1/messages`（非流式；流式在 `message_start.message`）

```json
"engine_version": "ssebed-0.1.38-b17",
"engine_build": {
  "name": "ssebed",
  "version": "0.1.38",
  "build": 17,
  "built_at": "2026-10-18 22:00:00",
  "sha256": "3d3f3c9de7a6ca38..."
}
```

- `engine_version`：一行可读字符串，`<name>-<version>-b<build>`；
  没有 `BUILD.json` 的旧 legacy 二进制回退为 `<name>-<version>-sha.<8位>`。
- `engine_build`：结构化明细，便于前端/脚本读取。
- 旧的 `/v1/status.engine` = `"0.1.38"` **保留兼容**；判断新 build 请用
  `engine_version` / `engine_build.build`。

## 3. 版本号怎么自动维护

### ssebed（日常开发引擎）

```bash
switch-engine dev build ssebed
```

成功构建后：

1. `~/SSEStraBed/build/build-number.txt` 读取旧值并 +1（原子替换）；
2. 写 `~/SSEStraBed/build/BUILD.json`：
   ```json
   {"engine":"ssebed","version":"0.1.38","build":17,
    "built_at":"...","sha256":"...","size":...,"mtime":...}
   ```
3. `~/SSEStraBed/build/dev-build.json` 里也记录同一个 `build`。

serve 的 `StrataEngine` 从**二进制同目录**的 `BUILD.json` 读取这些字段；
每次 `dev build` 后，下一次重启该 lane 就会看到新的 `engine_version`。

### legacy（原引擎）

`Strata/setup.py --build` 在真正编译后同样会：

- `~/Strata/build/build-number.txt` +1；
- 写 `~/Strata/build/BUILD.json`（`"engine":"legacy"`）。

如果 legacy 是旧二进制、没有 `BUILD.json`，serve 会用二进制的 SHA-256 前 8 位作为标记，
所以重新编译后仍然能看出变化。

## 4. 当前已生成的初始状态

```text
~/SSEStraBed/build/BUILD.json  → ssebed-0.1.38-b0  (sha 3d3f3c9d…)
~/Strata/build/BUILD.json      → legacy-0.1.38-b0  (sha 0f495a5f…)
```

计数器从 0 开始；下一次成功 build → `b1`。这样第一次部署时就能看到明确基线。

## 5. 怎么验证

```bash
# 1) 看构建状态（构建号/hash/mtime/stale）
switch-engine dev status ssebed

# 2) 看线上响应（serve 重启后）
KEY=$(cat ~/logs/strata-api-key.txt)
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8091/v1/status \
  | python3 -m json.tool | grep -A7 engine_version

# 3) 看一次对话响应（非流式）
curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  http://127.0.0.1:8091/v1/chat/completions \
  -d '{"model":"strata-a","messages":[{"role":"user","content":"hi"}],"max_tokens":8}' \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["engine_version"], d["engine_build"])'
```

切换脚本的 ready 日志也会带 `engine_version=...`：

```text
lane 8091 ready: model=... engine=0.1.38 engine_version=ssebed-0.1.38-b17
```

## 6. 改动文件

| 文件 | 改动 |
|---|---|
| `SSEStraBed/deploy/switch_engine.py` | 成功 build 后递增 `build-number.txt`，写 `build/BUILD.json`；`dev status`/`status`/ready 日志显示构建号 |
| `Strata/setup.py` | legacy 本地编译后递增计数器并写 `build/BUILD.json` |
| `Strata/serve/server.py` | `StrataEngine` 读 `BUILD.json` 的 build 字段；`Service.engine_identity()/engine_version()`；`/health`、`/v1/status`、OpenAI/Anthropic 响应加字段 |
| `Strata/serve/test_server.py` | 新增 `EngineVersion` 6 个测试 |
| `SSEStraBed/切换引擎用户手册.md` | 增加 `engine_version` 字段说明与验证命令 |

## 7. 测试

- `serve.test_server.EngineVersion`：6/6 通过（身份字符串、`/health`、`/v1/status`、
  OpenAI 非流式/流式、Anthropic 非流式/流式）。
- 全量 `serve.test_server`：130/131 通过；唯一失败是既有的
  `WebApp.test_page_and_files`（线上 index 已换 `app-CXUPqADB.js`，与本次无关）。
- 构建计数器 helper 用临时目录测试：连续调用得到 1/2/3，`BUILD.json` 的
  `engine/version/build/sha256/size` 正确。

## 8. 部署结果（快道 A，2026-10-18）

- 22:45:11 重启 `strata-a`；22:51:47 `ready`（约 6 分 36 秒冷启动）；普道 B 未动、保持在线。
- `/health` 与 `/v1/status` 现在返回：
  ```text
  engine_version = "ssebed-0.1.38-b0"
  engine_build   = {"name":"ssebed","version":"0.1.38","build":0,
                    "built_at":"2026-10-17 23:17:15","sha256":"3d3f3c9d..."}
  ```
- 对话响应验证（A）：
  - OpenAI 非流式：`engine_version` / `engine_build` 在顶层；`content="OK"`、`finish=stop`；
  - 代理 `model=qwen3.8-flash-fast` → 8091：字段同样存在；
  - OpenAI 流式：首个和最后一个 chunk 都带字段；
  - Anthropic 非流式：字段在 message 顶层。
- `infer.db` 也记录了代理请求；`/v1/status` 的 `engine_version` 现在是判断 A 构建的权威字段。
- **B 仍是旧代码**：`/v1/status` 没有 `engine_version`；B 的部署需要另约窗口。
- 下一次 `switch-engine dev build ssebed` 成功 → `b1`；重启 A 后会显示
  `ssebed-0.1.38-b1`，用于确认新 build 已生效。

## 9. 部署注意

- 该字段由 serve 进程读取 `BUILD.json` 生成；重启对应 lane 后生效（会连带引擎冷启动，
  属停服，必须先经用户同意）。
- 旧代码的 `/v1/status` 没有 `engine_version` 键；看到 `None` 说明该 lane 还没重启。
- 建议先在一条 lane 验证（A 已完成），再切 B。
