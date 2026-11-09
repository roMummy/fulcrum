"""接口响应耗时的进程内统计。

只保留最近一段时间的有限条记录(环形缓冲),按路由模板聚合,
不落库、不增加写压力;进程重启后重新累计,仅用于平台后台判断响应性能。
"""

from __future__ import annotations

import threading
import time
from collections import deque

# 最多保留的请求条数与统计窗口(秒);超出窗口的记录在汇总时忽略。
MAX_SAMPLES = 20000
WINDOW_SECONDS = 3600

_lock = threading.Lock()
_samples: deque[tuple[float, str, int, float]] = deque(maxlen=MAX_SAMPLES)
_started_at = time.time()


def record_request(route: str, status_code: int, duration_ms: float) -> None:
    """记录一次请求。route 用路由模板(如 /channels/{id}),避免路径参数撑爆分组。"""

    with _lock:
        _samples.append((time.time(), route, int(status_code), float(duration_ms)))


def _percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(len(sorted_values) * ratio))
    return sorted_values[index]


def summarize(limit: int = 15) -> dict:
    """返回窗口内的总体指标与按路由的耗时排行(按 p95 倒序)。"""

    cutoff = time.time() - WINDOW_SECONDS
    with _lock:
        rows = [item for item in _samples if item[0] >= cutoff]
    groups: dict[str, list[tuple[int, float]]] = {}
    for _, route, status, ms in rows:
        groups.setdefault(route, []).append((status, ms))

    def build(name: str, values: list[tuple[int, float]]) -> dict:
        durations = sorted(ms for _, ms in values)
        return {
            "route": name,
            "count": len(values),
            "avg_ms": round(sum(durations) / len(durations), 1),
            "p95_ms": round(_percentile(durations, 0.95), 1),
            "max_ms": round(durations[-1], 1),
            "errors": sum(1 for status, _ in values if status >= 500),
        }

    items = [build(name, values) for name, values in groups.items()]
    all_values = [(status, ms) for values in groups.values() for status, ms in values]
    overall = build("全部", all_values) if all_values else None
    items.sort(key=lambda item: item["p95_ms"], reverse=True)
    return {
        "window_minutes": WINDOW_SECONDS // 60,
        "uptime_seconds": int(time.time() - _started_at),
        "overall": overall,
        "slowest": items[:limit],
        "busiest": sorted(items, key=lambda item: item["count"], reverse=True)[:limit],
    }
