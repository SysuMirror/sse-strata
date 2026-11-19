# sse-strata

软工集市（SSE Market）上对外的大模型推理服务：**Strata 引擎 + 双 lane 部署 + 网关调度**，服务
`https://ai.ssemarket.cn/`。

模型：Qwen3.8-Flash-Next（GSQ-RCO IQ3_XXS，MoE，权重约 71 GB），两张 RTX 4090 D 各跑一条 lane，
GPU2 由邻居租户的 TTS 占用。

## 架构

```
                     ┌──────────── rag-proxy :8080（对外唯一入口）────────────┐
 浏览器 / API  ─────▶ │ 鉴权 · RAG · 文档抽取 · 双 lane 调度 · 推理元数据入库     │
                     └───────┬───────────────────────────────┬──────────────┘
                             ▼                               ▼
                    lane A  8091 / GPU0               lane B  8092 / GPU1
                    快道（低延迟、长上下文）            普道（主力流量、短请求）
                    4 个批槽                            4 个批槽
```

- **双 lane**：每条 lane 是独立的 `Strata/serve/server.py` 进程 + 引擎子进程，批槽 4。
- **网关调度**（`rag-proxy/rag_proxy.py: schedule_lane`）：快道请求去实时在途更少的一条；普道优先普道，
  普道满且快道有余量时溢出；按北京时间给快道预留槽；空闲时把请求分摊到两条 lane；
  `strata-a` / `strata-b` 是调用方点名的车道，保持死钉。
- **引擎 fork**：在上游 [Niko1221/Strata](https://github.com/Niko1221/Strata) **0.1.61** 基础上并入我们自己的
  并发改造（见下），源码在 `engine/`，合并过程记录在 `engine/MERGE.txt`。

## 引擎侧的关键改造

上游 0.1.59.1 上没有并发槽位相关的实现，我们 fork 了贡献者分支并自己维护：

| 改动 | 作用 |
|---|---|
| `--unified-slots`：固定物理槽 | 请求从 prefill 到结束留在自己的槽，并发变化不再触发 solo↔slot 会话迁移（逐层 KV 拷贝 + 前缀重读） |
| 槽内单序列 kernel（`Verifier::run_slot`） | 独处请求不回 solo，在原地全速跑 |
| `--batch-mtp-multistep`：并发多步 MTP | 每槽按剩余输出/上下文预算验证多个投机候选；采样槽也参与（每槽私有 `logit_bias`） |
| 稀疏活跃槽 + 槽位轮转 | 批窗口只组活跃槽，空槽不补 pad |
| fork 兼容 flags | `--mtp-varlen`（映射为多步）、`--batch-mtp-sampled`（默认已按槽采样）、`--mtp-chain-floor N`（草稿深度下限） |

实测（同机同权重，262144 上下文，唯一前缀、1200 token 提示）：

| 并发 | 聚合吞吐（旧 0.1.38 / 新引擎） | 单请求解码（旧 / 新） | 并发 TTFT（旧 / 新） |
|---|---|---|---|
| 1 路 | 36.3 / 38.6 tok/s | 75.0 / 64.1 tok/s | 1.70 / 1.57 s |
| 2 路 | 51.7 / **70.8** tok/s | 43.5 / 46.6 tok/s | 1.50 / **0.96** s |
| 4 路 | 61.1 / **70.6** tok/s | 22.8 / 21.9 tok/s | 1.97 / **1.43** s |

生产 14 天实测（单请求解码，按并发分组的同 lane 活跃数）：普道 95.2 / 41.1 / 28.4 / 21.3 / 15.8 tok/s
（1–5 路），快道 79.2 / 30.8 / 24.4 / 16.3 / 10.6 tok/s。完整报告见 `docs/reports/`。

## 目录

```
serve/                 服务端（server.py / frontend.py / telemetry.py / tests）+ Vue 前端源码 serve/webapp
rag-proxy/             对外网关：鉴权、RAG、文档抽取、双 lane 调度、元数据库
engine/                我们维护的引擎源码（上游 0.1.61 + 并发改造），含 MERGE.txt
deploy/                lane 的 engine profile（unified / unified-061）与 supervisord 配置样例
tools/                 压测与运维脚本（bench.py / bench2.py / mixed8.py / mtp_depth_sweep.py …）
docs/                  技术报告与运维笔记
```

## 构建

引擎（CUDA sm_89；本机用 venv 里的 cmake/ninja）：

```bash
cd engine
CUDACXX=/usr/local/cuda/bin/nvcc cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=89 \
  -DSTRATA_ENABLE_CUDA=ON -DSTRATA_ENABLE_HIP=OFF -DSTRATA_ENABLE_SYCL=OFF \
  -DSTRATA_GEOMETRY=flash_next -DSTRATA_BUILD_TESTS=OFF \
  -DSTRATA_AMD_COMMUNITY_TUNING=ON -DSTRATA_MMQ_KQUANTS=ON -DSTRATA_MMQ_TAB=OFF \
  -DSTRATA_MMQ_TQ_OVERLAY=ON -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_PREFILL_MMQ=OFF
cmake --build build -j 10
```

前端（`serve/webapp`）：

```bash
cd serve/webapp && npm ci && npm run build   # 产物由 server.py 从 serve/web 提供
```

## 运行

两条 lane 由 supervisord 拉起（样例 `deploy/supervisord.conf.example`），每条 lane 的环境写在
`active-<lane>.env`，指向一个 engine profile：

```bash
# profile 里描述端口、卡号、taskset、server.py 与引擎二进制
deploy/engines/profiles/unified-061/profile.json
deploy/engines/profiles/unified-061/strata-b.json
```

引擎关键参数（与线上一致）：`--batch 4 --batch-mtp --spec 4 --spec-min-p 0.5 --mtp <draft> --unified-slots
--mtp-varlen --batch-mtp-sampled --mtp-chain-floor 2 --max-context 262144 --kv int8 --kv-resident 32768
--expert-cache auto --pool-workers 13`。

## 配置与密钥（重要）

仓库里**不含任何密钥**。以下内容需要在本机/部署环境单独提供：

| 需要的东西 | 位置（本机约定） | 说明 |
|---|---|---|
| 引擎 API key | profile 的 `"api_key"`（仓库里已置空）或 `api_key_file` | 服务端与网关共用的 Bearer key |
| OAuth 客户端密钥 | `config.json` 的 `oauth.secret_file` 指向的文件 | 软工集市登录用 |
| MinIO / MySQL / Redis / Qdrant 凭证 | 环境变量（平台注入） | 仅 `rag-proxy` 的归档/监控用到 |
| 模型权重 | `/shared/weights/...`（只读共享盘） | 不进仓库、不进本机工作目录 |

`deploy/` 里只有 profile 与进程配置，没有数据库/对象存储凭证；`rag-proxy/config.json` 里的
`api_key_file` / `oauth.secret_file` 都指向运行时文件。

## 压测

```bash
python3 tools/bench.py --base http://127.0.0.1:8092 --key-file <key-file> \
  --model qwen3.8-flash-next-iq3_xxs --conc 1,2,4,8 --rounds 3 \
  --prompt-tokens 1200 --max-tokens 512
```

`--prompt-tokens` 是字符数（约 1.5 字符/token）；每条请求唯一前缀，避免前缀缓存干扰。