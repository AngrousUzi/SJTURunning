"""沿校园道路生成跑步路线(基于 OpenStreetMap 导出的路网,仅标准库)。"""
import bisect
import json
import math
import os
import random
import sys

from utils.auxiliary_util import haversine_distance

_GRAPH_CACHE = {}
_REPEAT_PENALTY = 4.0


def _default_graph_path():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = sys._MEIPASS
    else:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "assets", "campus_roads.json")


def load_campus_graph(path=None):
    """加载路网,返回 {'nodes': [(lat, lon)], 'adj': [[(nbr, len_m)]]};失败返回 None。"""
    path = os.path.abspath(path or _default_graph_path())
    if path in _GRAPH_CACHE:
        return _GRAPH_CACHE[path]
    graph = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        nodes = [(float(p[0]), float(p[1])) for p in data["nodes"]]
        adj = [[] for _ in nodes]
        seen = set()
        for a, b in data["edges"]:
            a, b = int(a), int(b)
            if a == b or (min(a, b), max(a, b)) in seen:
                continue
            seen.add((min(a, b), max(a, b)))
            d = haversine_distance(nodes[a][0], nodes[a][1], nodes[b][0], nodes[b][1])
            adj[a].append((b, d))
            adj[b].append((a, d))
        if nodes and seen:
            graph = {"nodes": nodes, "adj": adj}
    except Exception:
        graph = None
    if graph is not None:
        _GRAPH_CACHE[path] = graph
    return graph


def _nearest_node(graph, lat, lon):
    best, best_d = 0, float("inf")
    for i, (la, lo) in enumerate(graph["nodes"]):
        d = haversine_distance(lat, lon, la, lo)
        if d < best_d:
            best, best_d = i, d
    return best


def generate_road_route(start_lat, start_lon, target_distance_m, rng=None, graph=None):
    """返回沿路网随机游走、总长等于 target_distance_m 的折线 [(lat, lon), ...];无路网返回 None。"""
    if graph is None:
        graph = load_campus_graph()
    if graph is None:
        return None
    rng = rng or random
    nodes, adj = graph["nodes"], graph["adj"]

    poly = [(start_lat, start_lon)]
    total = 0.0
    cur = _nearest_node(graph, start_lat, start_lon)
    first = nodes[cur]
    seg = haversine_distance(start_lat, start_lon, first[0], first[1])
    if seg > 0:
        if seg >= target_distance_m:
            f = target_distance_m / seg
            poly.append((start_lat + f * (first[0] - start_lat), start_lon + f * (first[1] - start_lon)))
            return poly
        poly.append(first)
        total = seg

    uses = {}
    prev = None
    while total < target_distance_m:
        options = adj[cur]
        if not options:
            break
        cands = [(n, d) for n, d in options if n != prev] or list(options)
        weights = [1.0 / (1.0 + _REPEAT_PENALTY * uses.get((min(cur, n), max(cur, n)), 0)) for n, _ in cands]
        nxt, d = rng.choices(cands, weights=weights, k=1)[0]
        key = (min(cur, nxt), max(cur, nxt))
        uses[key] = uses.get(key, 0) + 1
        a, b = nodes[cur], nodes[nxt]
        if total + d >= target_distance_m:
            f = (target_distance_m - total) / d
            poly.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
            total = target_distance_m
            break
        poly.append(b)
        total += d
        prev, cur = cur, nxt
    return poly


def polyline_cumdist(polyline):
    cum = [0.0]
    for p, q in zip(polyline, polyline[1:]):
        cum.append(cum[-1] + haversine_distance(p[0], p[1], q[0], q[1]))
    return cum


def point_at_distance(polyline, cumdist, d):
    """沿折线 d 米处的 (lat, lon),段内线性插值,越界则取端点。"""
    if d <= 0 or len(polyline) == 1:
        return polyline[0]
    if d >= cumdist[-1]:
        return polyline[-1]
    i = bisect.bisect_right(cumdist, d) - 1
    seg = cumdist[i + 1] - cumdist[i]
    f = (d - cumdist[i]) / seg if seg > 0 else 0.0
    a, b = polyline[i], polyline[i + 1]
    return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
