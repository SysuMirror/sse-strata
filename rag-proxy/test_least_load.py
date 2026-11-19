"""S1a 最少负载粘性路由的单测（不起服务，纯函数级）。跑法：cd ~/rag-proxy && python3 -m unittest test_least_load -v"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import rag_proxy as RP  # noqa: E402


class LeastLoad(unittest.TestCase):
    LANE_A = "http://127.0.0.1:8091"
    LANE_B = "http://127.0.0.1:8092"

    def setUp(self):
        self._us = RP.upstreams
        self._alive = RP.upstream_alive
        RP.upstreams = lambda: [self.LANE_A, self.LANE_B]
        RP.upstream_alive = lambda u, **kw: True
        RP._STICKY.clear()
        with RP.LANE_LOCK:
            self._saved = dict(RP.LANE_CACHE)
            RP.LANE_CACHE.clear()

    def tearDown(self):
        RP.upstreams = self._us
        RP.upstream_alive = self._alive
        RP._STICKY.clear()
        with RP.LANE_LOCK:
            RP.LANE_CACHE.clear()
            RP.LANE_CACHE.update(self._saved)

    def feed(self, u, queued, generating):
        with RP.LANE_LOCK:
            RP.LANE_CACHE[u] = {"ts": time.time(), "status": {"live": {
                "state": "generating" if generating else "idle", "queued": queued}}, "metrics": None}

    def test_first_seen_goes_to_least_loaded(self):
        self.feed(self.LANE_A, 0, False)
        self.feed(self.LANE_B, 5, True)
        got = RP.pick_upstream("会话-甲")
        self.assertEqual(got, self.LANE_A)

    def test_sticky_wins_over_load(self):
        self.feed(self.LANE_A, 0, False)
        self.feed(self.LANE_B, 0, False)
        first = RP.pick_upstream("会话-乙")
        # 让另一条道变忙、已定道之外全堵
        other = self.LANE_B if first == self.LANE_A else self.LANE_A
        self.feed(first, 9, True)
        self.feed(other, 0, False)
        again = RP.pick_upstream("会话-乙")
        self.assertEqual(again, first)          # 粘性优先于负载均衡（缓存不换道）

    def test_dead_sticky_lane_refalls(self):
        self.feed(self.LANE_A, 0, False)
        self.feed(self.LANE_B, 0, False)
        first = RP.pick_upstream("会话-丙")
        RP.upstream_alive = lambda u, **kw: u == first
        other = self.LANE_B if first == self.LANE_A else self.LANE_A
        RP.upstream_alive = lambda u, **kw: u != first    # 原道死了
        self.assertEqual(RP.pick_upstream("会话-丙"), other)

    def test_stale_data_conservative(self):
        # 没有 LANE_CACHE 数据时（全 99 保守值）也要稳定返回一条活路
        self.assertEqual(RP.pick_upstream("会话-丁") in (self.LANE_A, self.LANE_B), True)

    def test_switch_off_is_plain_hash(self):
        self.feed(self.LANE_A, 0, False)
        self.feed(self.LANE_B, 9, True)
        RP.CFG["least_load_routing"] = False
        try:
            h = RP.pick_upstream("会话-戊")
            self.assertEqual(h, RP.upstreams()[int(RP.sha1("会话-戊"), 16) % 2])
        finally:
            RP.CFG.pop("least_load_routing", None)


if __name__ == "__main__":
    unittest.main()


class Route(unittest.TestCase):
    """route_upstream：显式模型钉道不变；未登记走粘性最少负载；开关关回 default_upstream 硬管。"""
    LANE_A = "http://127.0.0.1:8091"
    LANE_B = "http://127.0.0.1:8092"

    def setUp(self):
        self._us, self._alive = RP.upstreams, RP.upstream_alive
        self._routes, self._def = RP.CFG.get("model_routes"), RP.CFG.get("default_upstream")
        RP.upstreams = lambda: [self.LANE_A, self.LANE_B]
        RP.upstream_alive = lambda u, **kw: True
        RP.CFG["model_routes"] = {"qwen3.8-flash-fast": self.LANE_A, "qwen3.8-flash": self.LANE_B}
        RP.CFG["default_upstream"] = self.LANE_B
        RP._STICKY.clear()
        with RP.LANE_LOCK:
            self._saved = dict(RP.LANE_CACHE)
            RP.LANE_CACHE.clear()
            RP.LANE_CACHE[self.LANE_A] = {"ts": time.time(), "status": {"live": {"state": "idle", "queued": 0}}}
            RP.LANE_CACHE[self.LANE_B] = {"ts": time.time(), "status": {"live": {"state": "generating", "queued": 4}}}

    def tearDown(self):
        RP.upstreams, RP.upstream_alive = self._us, self._alive
        RP.CFG["model_routes"], RP.CFG["default_upstream"] = self._routes, self._def
        RP.CFG.pop("least_load_routing", None)
        RP._STICKY.clear()
        with RP.LANE_LOCK:
            RP.LANE_CACHE.clear()
            RP.LANE_CACHE.update(self._saved)

    def test_explicit_model_pins(self):
        self.assertEqual(RP.route_upstream("qwen3.8-flash", "会话-x"), self.LANE_B)   # 登记的模型名不受均衡影响
        self.assertEqual(RP.route_upstream("qwen3.8-flash-fast", "会话-y"), self.LANE_A)

    def test_unregistered_model_least_load(self):
        self.assertEqual(RP.route_upstream("qwen3.8-flash-next-iq3_xxs", "会话-z"), self.LANE_A)  # 未登记且 B 忙 → A

    def test_switch_off_returns_hard_default(self):
        RP.CFG["least_load_routing"] = False
        self.assertEqual(RP.route_upstream("whatever", "会话-w"), self.LANE_B)
