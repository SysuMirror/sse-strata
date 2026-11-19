#!/usr/bin/env python3
"""反证：把会话级锁换成空操作（= 2026-10-08 改动前的行为），跑同一套并发测试。

用途：证明 test_concurrent_session.py 的 T1 判的是**锁**，不是别的什么东西。
实测结果（2026-10-08 13:21 UTC）：
  T1 同键第二个请求被挡住   FAIL（1.02s，没有被挡住——改动前就是各跑各的）
  T2 异键请求不被挡住        PASS（1.03s）
  T3 分叉被判定出一次 reset  PASS（**这一项不判别锁**：守卫在 assemble 之前就发布了历史，
                                 所以没有锁时它照样通过；真正被锁消除的是"分叉请求的 reset 把
                                 在飞请求已入库的块清掉、在飞的组装改而检索到对方内容"这条路）

用法：/home/cloud/Strata/.venv/bin/python test_concurrent_session_nolock.py
"""
import contextlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rag_proxy as R                                              # noqa: E402

_orig_init = R.Session.__init__


def _no_lock(self, key):
    _orig_init(self, key)
    self.lock = contextlib.nullcontext()          # 不做串行化

R.Session.__init__ = _no_lock

import test_concurrent_session as T                                # noqa: E402

T.LOG_PATH = HERE / "concurrent-nolock.out.log"

if __name__ == "__main__":
    raise SystemExit(T.main())
