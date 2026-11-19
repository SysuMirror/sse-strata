# P0：把网页默认采样温度改成 0（贪心），把「白送的投机解码」拿回来

日期：2026-10-22（北京时间）
结论：**改动已上线**（前端默认 `temperature: 0`）。**只有精确的 0 有用**，
0.3 / 0.6 一样会让窗口全部退化成 batch；实测并发下收益是 ITL −7.4%、TTFT −10.1%、
汇总吞吐在噪声内（−2% ~ +2%）。

## 1. 机制：为什么一个采样参数能关掉整条投机解码

引擎的校验窗口有两种形态（`STRATA_STEP_STATS` 里直接记为 `chain` / `batch`）：

- `chain`：投机解码路径——一次草稿 + 整链校验，一个窗口吐出多个 token
  （实测 S=8 贪心时 264 个窗口共接受 706/1044 个草稿 = **67.6% 接受率**）。
- `batch`：逐 token 路径——一次窗口只吐 1 个 token，因为链式校验要求「整链贪心可复现」，
  带采样参数的槽无法参与。

**只要组里有一个槽带采样参数（temperature > 0），整组就退回 batch。**
这不是 bug，是投机解码的数学前提。网页默认 `temperature: 0.6`，于是：

- lane B（真公网流量，默认 0.6）：`batch 631,953 / chain 1,359` → **99.8% 的窗口是逐 token**。
  也就是说线上绝大多数算力花在没有投机的路径上。
- lane A（本次压测，贪心）：`chain 264 / batch 0`。

## 2. 改了什么

`Strata/serve/webapp/src/store.js` 的 `DEFAULTS`：

```js
// temperature 默认 0（贪心）。原因不是口味，是机制：引擎里只要一个槽带采样参数，整组就退回逐 token
export const DEFAULTS = { thinking: "high", temperature: 0, top_p: 0.95, top_k: 20, ... }
```

已构建并发布：`Strata/serve/web` 的产物里 `temperature:0`（`app-CJcE40X2.js`，10-21 13:13）。
回滚 = 把 0 改回 0.6 并重新构建（前端构建与发布流程见 `memory/strata-frontend-vue.md`）。

## 3. 实测（lane A，20 条固定 prompt，S=8，96 token，暖机后）

| 采样 | 窗口形态 | TTFT 中位 | ITL 中位 | ITL p90 | 汇总吞吐 |
|---|---|---|---|---|---|
| `temperature=0`（贪心） | chain 264 / batch 0 | **247.5 ms** | **109.3 ms** | **130.4 ms** | 36.78 tok/s |
| `temperature=0.3` | batch 477 / chain 0 | 262.2 ms | 116.5 ms | 137.7 ms | 37.43 tok/s |
| `temperature=0.6`（旧默认） | batch 443 / chain 0 | 275.3 ms | 118.0 ms | 158.1 ms | 36.02 tok/s |

- 贪心 vs 0.6：**ITL −7.4%、TTFT −10.1%、ITL p90 −17.5%**；汇总吞吐 −2.1%（在 ±5% 噪声内）。
- **0.3 没有任何作用**：只要不是精确的 0，就完全不走 chain（上图 `batch 477 / chain 0`）。
  所以「0.3 折中」这条路线作废，默认只能二选一。
- 串行单请求不受影响（贪心 ITL 11.87 ms vs 0.6 的 12.01 ms，差 1%）：
  采样只在**多槽并发**时把整组打回 batch，这也解释了为什么以前只用单条 curl 测不出这个问题。

数据：`~/docs/reports/data/p0-conc-{t0s8,t03s8,t06s8}.json`、`p0-ab-{t0,t06}.json`

## 4. 代价（必须写清楚）

贪心 = 同一 prompt 每次输出一样，创意写作/头脑风暴的多样性没了；temperature 仍然是前端可调项，
用户想要采样可以自己拉高（代价是该会话失去投机加速）。这是个产品取舍，不是纯性能优化。

## 5. 附带发现（对后续有用）

- 引擎**逐次不可复现**：同样的二进制、同样的贪心，两次运行只有 6–12/20 条输出逐字节相同。
  任何「换二进制后输出逐 token 一致」的门禁都过不了，必须换成 kernel 级逐位证明（见 P2 报告）。
- 冷启动：重启后第一次压测的 TTFT 是暖机后的约 2 倍。所有 A/B 都要丢弃第一次运行。

## 6. 出处

- 前端改动：`~/archive/patches/p0-greedy-default-2026-10-22/{store.js.new,web-before/}`
- 测量脚本：`~/tools/p0_temp_ab.py`（串行）、`~/tools/p0_conc_ab.py`（并发）
- 窗口统计：`~/logs/step-stats-{ab-prod,b}.jsonl`（`kind` 字段）
- 相关记忆：`memory/strata-sampling-skips-mtp.md`、`memory/inference-speed-no-throttle.md`