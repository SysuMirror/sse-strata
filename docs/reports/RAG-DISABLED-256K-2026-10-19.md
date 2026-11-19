# 关闭 RAG、回调引擎原生 256K 上下文（2026-10-19）

> 用户指令：**先去掉现在的 RAG，回调成 256K 上下文，外面同步调整**；RAG 目前问题较多。  
> 执行：`rag-proxy` 增加 `rag_enabled` 开关并设为 **false**，代理改为纯透传。  
> 状态：**已生效**（2026-10-19 13:07 重启代理；引擎未重启，两条 lane 全程在线）。

---

## 1. 现在是什么行为

- `rag-proxy` **不再检索、不再组装、不再建 RAG 会话**；请求按模型 ID 选 lane 后**原样透传**。
- 有效上下文 = 引擎原生 **262144 token（256K）**。
- `thinking_budget_guard` 仍保留（它不是 RAG；只是给答复留预算）。
- 超窗：
  - `prompt + max_tokens + 8 > 262144`：引擎返回 400；代理的 `retry_on_overfit`
    会按引擎报的余量**压一次 max_tokens 重发**；
  - 如果 **prompt 本身**就超 262144，压 max_tokens 救不了，客户端会收到 400。
- 外部客户端/中转需要自己把上下文控制在 256K 内（用户已说明会外部同步调整）。

## 2. 改了什么

| 文件 | 改动 |
|---|---|
| `rag-proxy/config.json` | 新增 `"rag_enabled": false` |
| `rag-proxy/rag_proxy.py` | `DEFAULTS` 新增 `"rag_enabled": true`；`_rag_request` 在 `thinking_guard` 后短路：不建会话、不 ingest、不计数、不组装，直接透传；`/health` 暴露 `config.rag_enabled` 与 `counters.no_rag`；启动日志打印 `rag_enabled=False` |
| 推理留档 | 新请求 `mode` 变为 **`no_rag`**（不再是 `est/exact/assembled`） |

## 3. 生效与验证

```bash
supervisorctl -c ~/deploy/supervisord.conf restart rag-proxy
```

- 2026-10-19 13:07:00 重启；13:07:06 恢复；**引擎没有重启**，8091/8092 全程在线。
- 重启前 in-flight = 0；代理重启只中断了极短的网关窗口。

实测（经 8080）：

```text
/health: config.rag_enabled=false
         counters = {"no_rag":1,"assembled":0,"requests":1,"passthrough":0}
         sessions = 0
请求 model=qwen3.8-flash-fast:
  finish=stop, content="OK", engine_version=ssebed-0.1.38-b0
infer.db: id 20261019-130717-98e9e2
          lane=快道, mode=no_rag, prompt=56, completion=30,
          content_chars=2, finish=stop
/concurrency: 快道/普道 both up, in_flight=0
```

## 4. 回滚

```bash
BK=~/archive/patches/rag-off-256k-2026-10-19
cp "$BK/rag_proxy.py.before" ~/rag-proxy/rag_proxy.py
cp "$BK/config.json.before"  ~/rag-proxy/config.json
supervisorctl -c ~/deploy/supervisord.conf restart rag-proxy
```

或只把 `config.json` 的 `"rag_enabled"` 改回 `true` 再重启代理（代码向后兼容）。

## 5. 产物

- 补丁/备份：`~/archive/patches/rag-off-256k-2026-10-19/`
  （`rag_proxy.py.before/after`、`config.json.before/after`、`rag-off-256k.patch`、`README.md`）。
- 代理交接文档：`~/rag-proxy/README.md` 顶部已加 2026-10-19 运行模式说明。
- 项目总览：`docs/项目背景与问题总览.md` §3.4 已加“当前 RAG 关闭”的说明。
