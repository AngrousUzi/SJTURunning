import math
import random
import unittest
from unittest.mock import patch

from src import road_router, data_generator
from utils.auxiliary_util import haversine_distance

START = (31.031599, 121.442938)


def _route_len(p):
    return sum(haversine_distance(a[0], a[1], b[0], b[1]) for a, b in zip(p, p[1:]))


def _dist_to_segment(p, a, b):
    # 局部平面近似(米)
    k = math.cos(math.radians(p[0]))

    def xy(q):
        return ((q[1] - p[1]) * 111320 * k, (q[0] - p[0]) * 110540)

    ax, ay = xy(a)
    bx, by = xy(b)
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
    return math.hypot(ax + t * dx, ay + t * dy)


class RoadRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = road_router.load_campus_graph()

    def test_graph_loads(self):
        self.assertIsNotNone(self.graph)
        self.assertGreater(len(self.graph["nodes"]), 0)

    def test_route_length(self):
        for target in (1000, 5500):
            r = road_router.generate_road_route(START[0], START[1], target, rng=random.Random(1), graph=self.graph)
            self.assertAlmostEqual(_route_len(r), target, delta=1.0)

    def test_vertices_on_edges(self):
        g = self.graph
        r = road_router.generate_road_route(START[0], START[1], 3000, rng=random.Random(2), graph=g)
        edges = [(g["nodes"][i], g["nodes"][j]) for i, nb in enumerate(g["adj"]) for j, _ in nb if i < j]
        for v in r[1:]:
            best = min(_dist_to_segment(v, a, b) for a, b in edges)
            self.assertLess(best, 1.0)

    def test_deterministic_and_no_uturn(self):
        r1 = road_router.generate_road_route(START[0], START[1], 5500, rng=random.Random(7), graph=self.graph)
        r2 = road_router.generate_road_route(START[0], START[1], 5500, rng=random.Random(7), graph=self.graph)
        self.assertEqual(r1, r2)
        idx = {n: i for i, n in enumerate(self.graph["nodes"])}
        seq = [idx[v] for v in r1[1:-1] if v in idx]
        for a, b, c in zip(seq, seq[1:], seq[2:]):
            if a == c:
                self.assertEqual(len(self.graph["adj"][b]), 1)

    def test_missing_asset_returns_none(self):
        self.assertIsNone(road_router.load_campus_graph("no/such/file.json"))


class PayloadTests(unittest.TestCase):
    CFG = {"INTERVAL_SECONDS": 3, "USER_ID": "u", "START_TIME_EPOCH_MS": 1_700_000_000_000}

    def _run(self, road, missing=False):
        app = {"沿道路跑步": road, "每日距离_米": 2000, "配速_分钟每公里": 5.0,
               "GPS采样间隔_秒": 3, "起点纬度": START[0], "起点经度": START[1]}
        logs = []
        patches = [
            patch.object(data_generator, "load_config", return_value=app),
            patch.object(data_generator, "log_output",
                         side_effect=lambda m, level="info", cb=None: logs.append((level, m))),
        ]
        if missing:
            patches.append(patch.object(road_router, "_default_graph_path", return_value="no/such.json"))
        for p in patches:
            p.start()
        try:
            body, _, _ = data_generator.generate_running_data_payload(self.CFG, [], {})
        finally:
            for p in patches:
                p.stop()
        pts = [p["latLng"] for t in body[0]["tracks"] for p in t["points"]]
        return pts, logs

    def test_fallback_when_asset_missing(self):
        pts, logs = self._run(True, missing=True)
        self.assertTrue(pts)
        self.assertTrue(any(level == "warning" for level, _ in logs))
        self.assertEqual(len({p["latitude"] for p in pts}), 1)

    def test_end_to_end_road_mode(self):
        pts, _ = self._run(True)
        speed = 2000 / (5.0 * 60 * 2)
        for a, b in zip(pts, pts[1:]):
            d = haversine_distance(a["latitude"], a["longitude"], b["latitude"], b["longitude"])
            self.assertLessEqual(d, speed * 3 + 0.5)
        self.assertGreater(len({p["latitude"] for p in pts}), 1)


if __name__ == "__main__":
    unittest.main()
