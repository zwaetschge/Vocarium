"""Small in-process metrics collector for Vocarium.

The API intentionally avoids a Prometheus dependency. Metrics are kept in
memory and rendered as Prometheus-compatible text from ``/api/metrics``.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from dataclasses import dataclass


LabelValue = tuple[tuple[str, str], ...]


def _label_value(labels: dict[str, object] | None) -> LabelValue:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _label_text(labels: LabelValue) -> str:
    if not labels:
        return ""
    escaped = []
    for key, value in labels:
        safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        escaped.append(f'{key}="{safe}"')
    return "{" + ",".join(escaped) + "}"


@dataclass
class _SummaryValue:
    count: int = 0
    total: float = 0.0
    max_value: float = 0.0


_lock = threading.Lock()
_counters: dict[str, dict[LabelValue, float]] = defaultdict(lambda: defaultdict(float))
_summaries: dict[str, dict[LabelValue, _SummaryValue]] = defaultdict(dict)
_gauges: dict[str, dict[LabelValue, float]] = defaultdict(dict)


def inc(name: str, amount: float = 1.0, labels: dict[str, object] | None = None) -> None:
    """Increment a counter by ``amount``."""
    if amount < 0:
        raise ValueError("counter increments must be non-negative")
    with _lock:
        _counters[name][_label_value(labels)] += amount


def observe(name: str, value: float, labels: dict[str, object] | None = None) -> None:
    """Observe a duration/value summary sample."""
    if math.isnan(value) or math.isinf(value):
        return
    label_value = _label_value(labels)
    with _lock:
        item = _summaries[name].setdefault(label_value, _SummaryValue())
        item.count += 1
        item.total += max(0.0, float(value))
        item.max_value = max(item.max_value, float(value))


def set_gauge(name: str, value: float, labels: dict[str, object] | None = None) -> None:
    if math.isnan(value) or math.isinf(value):
        return
    with _lock:
        _gauges[name][_label_value(labels)] = float(value)


def render_prometheus() -> str:
    """Render all metrics in Prometheus text exposition format."""
    lines: list[str] = []
    with _lock:
        for name in sorted(_counters):
            lines.append(f"# TYPE {name} counter")
            for labels, value in sorted(_counters[name].items()):
                lines.append(f"{name}{_label_text(labels)} {value:.12g}")

        for name in sorted(_summaries):
            lines.append(f"# TYPE {name} summary")
            for labels, value in sorted(_summaries[name].items()):
                text = _label_text(labels)
                lines.append(f"{name}_count{text} {value.count}")
                lines.append(f"{name}_sum{text} {value.total:.12g}")
                lines.append(f"{name}_max{text} {value.max_value:.12g}")

        for name in sorted(_gauges):
            lines.append(f"# TYPE {name} gauge")
            for labels, value in sorted(_gauges[name].items()):
                lines.append(f"{name}{_label_text(labels)} {value:.12g}")

    return "\n".join(lines) + "\n"
