"""
从 OpenStreetMap 下载上海交通大学闵行校区的道路网络，裁剪到校园范围内，
转换为 GCJ-02 坐标（高德/跑步 App 使用的坐标系），保存为 assets/campus_roads.json。

用法:
    python tools/build_campus_map.py              # 在线下载并生成
    python tools/build_campus_map.py raw.json campus.json   # 使用已下载的 Overpass 结果

地图数据 © OpenStreetMap contributors, ODbL 1.0.
"""
import json
import math
import os
import sys
from collections import defaultdict

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BBOX = "31.012,121.415,31.046,121.460"  # 南,西,北,东 (WGS-84)
CAMPUS_WAY_ID = 288249651  # Shanghai Jiao Tong University (Minhang Campus)
BOUNDARY_BUFFER_M = 15  # 校园边界外扩距离，保留紧贴围墙的环路
HIGHWAY_TYPES = (
    "footway|path|pedestrian|living_street|service|residential|"
    "unclassified|tertiary|secondary|track|cycleway"
)  # 不含 steps（台阶）

ROADS_QUERY = f"""[out:json][timeout:60];
(way["highway"~"^({HIGHWAY_TYPES})$"]({BBOX});way["leisure"="track"]({BBOX}););
(._;>;);out body;"""
CAMPUS_QUERY = f"""[out:json][timeout:60];way({CAMPUS_WAY_ID});out geom tags;"""

OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "campus_roads.json")


# ---------------- WGS-84 -> GCJ-02 ----------------
_A = 6378245.0
_EE = 0.00669342162296594323


def _t_lat(x, y):
    r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    r += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    return r


def _t_lon(x, y):
    r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    r += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    return r


def wgs84_to_gcj02(lat, lon):
    d_lat = _t_lat(lon - 105.0, lat - 35.0)
    d_lon = _t_lon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - _EE * math.sin(rad) ** 2
    sqrt_magic = math.sqrt(magic)
    d_lat = d_lat * 180.0 / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    d_lon = d_lon * 180.0 / (_A / sqrt_magic * math.cos(rad) * math.pi)
    return lat + d_lat, lon + d_lon


# ---------------- geometry helpers ----------------
def _xy(lat, lon, lat0):
    return lon * 111320.0 * math.cos(math.radians(lat0)), lat * 110574.0


def _point_in_polygon(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _dist_to_polygon_edge(x, y, poly):
    best = float("inf")
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i - 1], poly[i]
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def _overpass(query):
    resp = requests.post(
        OVERPASS_URL,
        data={"data": query},
        headers={"User-Agent": "SJTURunning-map-builder/1.0", "Accept": "application/json"},
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def build(roads, campus):
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in roads["elements"] if e["type"] == "node"}
    ways = [e for e in roads["elements"] if e["type"] == "way"]

    boundary = next(e for e in campus["elements"] if e["id"] == CAMPUS_WAY_ID)["geometry"]
    lat0 = boundary[0]["lat"]
    poly = [_xy(p["lat"], p["lon"], lat0) for p in boundary]

    def in_campus(nid):
        x, y = _xy(*nodes[nid], lat0)
        return _point_in_polygon(x, y, poly) or _dist_to_polygon_edge(x, y, poly) <= BOUNDARY_BUFFER_M

    keep = {nid: in_campus(nid) for nid in nodes}
    adj = defaultdict(set)
    for w in ways:
        ns = w["nodes"]
        for a, b in zip(ns, ns[1:]):
            if a != b and keep.get(a) and keep.get(b):
                adj[a].add(b)
                adj[b].add(a)

    # 只保留最大连通分量，保证任意两点可达
    seen, best = set(), []
    for start in adj:
        if start in seen:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            n = stack.pop()
            comp.append(n)
            for m in adj[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        if len(comp) > len(best):
            best = comp
    comp = set(best)

    order = sorted(comp)
    index = {nid: i for i, nid in enumerate(order)}
    out_nodes = []
    for nid in order:
        g_lat, g_lon = wgs84_to_gcj02(*nodes[nid])
        out_nodes.append([round(g_lat, 7), round(g_lon, 7)])
    edges = sorted({tuple(sorted((index[a], index[b]))) for a in comp for b in adj[a]})

    return {
        "source": "OpenStreetMap contributors (ODbL 1.0), https://www.openstreetmap.org/copyright",
        "area": "Shanghai Jiao Tong University, Minhang Campus",
        "crs": "GCJ-02",
        "nodes": out_nodes,
        "edges": [list(e) for e in edges],
    }


def main():
    if len(sys.argv) == 3:
        with open(sys.argv[1], encoding="utf-8") as f:
            roads = json.load(f)
        with open(sys.argv[2], encoding="utf-8") as f:
            campus = json.load(f)
    else:
        roads = _overpass(ROADS_QUERY)
        campus = _overpass(CAMPUS_QUERY)

    data = build(roads, campus)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {OUT_PATH}: {len(data['nodes'])} nodes, {len(data['edges'])} edges")


if __name__ == "__main__":
    main()
