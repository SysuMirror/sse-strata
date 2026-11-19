#!/usr/bin/env python3
"""会话级并发的离线测试（2026-10-08）。

守的一句话：**同一个会话键上，两个请求不允许同时在飞**。历史守卫（比历史）、分块入库、计数
前缀缓存、组装块复用这几步共享同一份会话状态；交错执行时，两个内容分叉、却都"与旧历史对得上"
的请求会各自入库，随后互相检索到对方的内容——那是唯一一条"串上下文"的路（见
技术文档-RAG网关-2026-10-08.md §5.3 的 R1/R2/R4）。

不联网、不碰真引擎、不碰生产代理：起一个**进程内**的 rag_proxy（线程里跑 HTTP），把 assemble
换成一个"睡 1 秒"的替身，用时间差证明：
  T1 同键并发      → 第二个请求被挡住（等第一个出锁），耗时 ≈ 2×sleep
  T2 异键并发      → 互不阻塞，耗时 ≈ 1×sleep
  T3 同键分叉并发  → 分叉的第二个请求必须被判定出来（一次 session reset），且重置不影响可用性。
                    注意 T3 **不是**会话锁的判别项：守卫在 assemble 之前就发布了历史，所以没有锁时
                    T3 一样通过（已实测，见 test_concurrent_session_nolock.py）。判别项是 T1。
"第一个请求已经进锁"由 assemble 替身置一个事件来确认，不靠 sleep 赌。

用法：/home/cloud/Strata/.venv/bin/python test_concurrent_session.py
"""
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PY = "/home/cloud/Strata/.venv/bin/python"

MOCK_PORT = 8096          # 与 test_window_guard(8097/8087)、test_session_isolation(8086) 错开
PROXY_PORT = 8085
SLEEP = 1.0               # assemble 替身睡多久：判定阈值按它算

import rag_proxy as R                                              # noqa: E402

OUT = sys.stdout          # 断言结果走这里；rag_proxy 的日志会被临时重定向到文件
LOG_PATH = HERE / "concurrent-test.out.log"
FAILS = []
IN_ASSEMBLE = threading.Event()


def ck(name: str, ok: bool, detail: str = ""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {detail}" if detail else ""), file=OUT, flush=True)
    if not ok:
        FAILS.append(name)


def post(body: dict, key: str, timeout: float = 60.0) -> tuple[int, float]:
    """发一条请求，返回 (状态码, 耗时秒)。"""
    req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Strata-Session", key)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, time.perf_counter() - t0


def bg(body: dict, key: str, out: dict):
    out["r"] = post(body, key)


def filler(n_chars: int) -> str:
    return "归档材料描述了流程、命名与迁移计划；复核周期为季度，日志保留九十天。" * (n_chars // 30 + 1)


def msgs(first_user: str, tail: list[str]) -> list:
    """[user, assistant, user, assistant, ...]：第一条用户消息固定 → 同一会话的后续轮次。"""
    out = [{"role": "user", "content": first_user}]
    for i, t in enumerate(tail):
        out.append({"role": "assistant" if i % 2 == 0 else "user", "content": t})
    return out


def main() -> int:
    logf = open(LOG_PATH, "w", encoding="utf-8")
    mock = subprocess.Popen([PY, str(HERE / "mock_upstream.py"), "--port", str(MOCK_PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = dict(R.DEFAULTS)
    cfg.update({
        "listen_port": PROXY_PORT, "upstream": f"http://127.0.0.1:{MOCK_PORT}",
        "upstreams": [f"http://127.0.0.1:{MOCK_PORT}"], "default_upstream": f"http://127.0.0.1:{MOCK_PORT}",
        "max_context": 4096, "recent_window_tokens": 1024, "chunk_tokens": 256,
        "count_timeout_s": 5, "embed_url": "http://127.0.0.1:9/", "rerank_url": "http://127.0.0.1:9/",
        "debug": False,
    })
    R.CFG = cfg
    R.API_KEY = "test"

    real_assemble = R.assemble

    def slow_assemble(sess, body, messages, total_tokens, up=None):
        """替身：报个到，睡够 SLEEP 秒，再返回一个合法的组装结果——慢，但完全确定。"""
        IN_ASSEMBLE.set()
        time.sleep(SLEEP)
        info = {"recent_tokens": 10, "memory_tokens": 0, "reused_block": False, "retrieve_ms": 0.0,
                "final_tokens": 10, "final_exact": True}
        return dict(body, messages=messages[-1:]), info

    R.assemble = slow_assemble
    old_stdout = sys.stdout
    try:
        time.sleep(0.4)                                       # 等 mock 起来
        srv = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), R.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.3)

        big = filler(9000)                                    # 逼出"估算装不下 → 精确计数 → 组装"这条路
        A = {"model": "mock", "max_tokens": 32, "messages": msgs(big, ["A 的第一轮回答"])}
        A2 = {"model": "mock", "max_tokens": 32,
              "messages": msgs(big, ["A 的第一轮回答", "接着补充一点", "A 的第二轮回答"])}
        B = {"model": "mock", "max_tokens": 32, "messages": msgs(big, ["B 的完全不同的第一轮"])}
        C = {"model": "mock", "max_tokens": 32, "messages": msgs("换一个开头：" + big, ["C 自己的一条"])}
        # 分叉判定要看**历史长度**：守卫故意允许"最后一轮不同"（客户端重写最后一轮是常态），
        # 所以要触发 reset，历史得够长、且分叉发生在更早的位置（k < len(hist)-1）。
        A3 = {"model": "mock", "max_tokens": 32,
              "messages": msgs(big, ["A 的第一轮回答", "接着补充一点", "A 的第二轮回答"])}      # 4 条
        B4 = {"model": "mock", "max_tokens": 32,
              "messages": msgs(big, ["B 的第一轮回答", "B 补充一点", "B 的第二轮回答"])}        # 只共享第 0 条

        print("== T1/T2 同键 vs 异键（并发）==", file=OUT, flush=True)
        sys.stdout = logf                                     # rag_proxy.log() 的 print 收进文件
        o1, o2, o3 = {}, {}, {}
        t1 = threading.Thread(target=bg, args=(A, "sess-A", o1))
        t1.start()
        ck("第一个请求确实进了锁（assemble 替身报到）", IN_ASSEMBLE.wait(5.0), "")
        t2 = threading.Thread(target=bg, args=(A2, "sess-A", o2))      # 同键：必须排在后面
        t3 = threading.Thread(target=bg, args=(C, "sess-C", o3))       # 异键：不该被挡
        t2.start()
        t3.start()
        for t in (t1, t2, t3):
            t.join(timeout=60)
        e_same, e_other = o2["r"][1], o3["r"][1]
        ck("同键第二个请求被挡住（≈2×sleep）", e_same > SLEEP * 1.6, f"{e_same:.2f}s")
        ck("异键请求不被挡住（≈1×sleep）", e_other < SLEEP * 1.5, f"{e_other:.2f}s")
        ck("三个请求都拿到了 200", o1["r"][0] == o2["r"][0] == o3["r"][0] == 200,
           f"{o1['r'][0]}/{o2['r'][0]}/{o3['r'][0]}")

        print("== T3 同键分叉（并发）==", file=OUT, flush=True)
        IN_ASSEMBLE.clear()
        o4, o5 = {}, {}
        t4 = threading.Thread(target=bg, args=(A3, "sess-D", o4))
        t4.start()
        ck("分叉测试的第一个请求进了锁", IN_ASSEMBLE.wait(5.0), "")
        t5 = threading.Thread(target=bg, args=(B4, "sess-D", o5))      # 只共享第一条 user → 必须判定分叉
        t5.start()
        for t in (t4, t5):
            t.join(timeout=60)
        logf.flush()
        text = LOG_PATH.read_text(encoding="utf-8")
        resets = [ln.strip() for ln in text.splitlines() if "session reset" in ln and "sess-D" in ln]
        ck("分叉的第二个请求触发了一次 session reset", len(resets) == 1, f"命中 {len(resets)} 次 {resets}")
        ck("两个请求都拿到了 200（重置不影响可用性）", o4["r"][0] == 200 and o5["r"][0] == 200,
           f"{o4['r'][0]}/{o5['r'][0]}")
        n_asm = text.count("ctx[组装")
        ck("组装路径确实走了（不是透传）", n_asm >= 4, f"组装行 {n_asm} 条")
        ck("窗口算术日志正常（无异常栈）", "Traceback" not in text, "")
    finally:
        R.assemble = real_assemble
        sys.stdout = old_stdout
        mock.terminate()
        logf.close()

    print("=" * 62, file=OUT, flush=True)
    if FAILS:
        print(f"FAIL：{len(FAILS)} 项未通过 -> {FAILS}（日志 {LOG_PATH}）", file=OUT, flush=True)
        return 1
    print(f"ALL PASS  （日志 {LOG_PATH}）", file=OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
