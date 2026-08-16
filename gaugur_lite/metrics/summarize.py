"""遥测时序、间隔和资源字段的确定性汇总。"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from typing import Any

from ..schema import SystemMetricEvent


def percentile(values: Sequence[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(ratio * len(ordered)) - 1)])


def describe(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "p95": percentile(values, 0.95),
    }


def summarize_events(
    events: Sequence[SystemMetricEvent], *, requested_interval_s: float
) -> dict[str, Any]:
    monotonic_ns = [event.monotonic_time_ns for event in events]
    wall_ns = [event.wall_time_ns for event in events]
    interval_s = [
        (right - left) / 1_000_000_000
        for left, right in zip(monotonic_ns, monotonic_ns[1:], strict=False)
    ]
    interval_errors = [abs(value - requested_interval_s) for value in interval_s]
    interval_tolerance_s = max(0.05, requested_interval_s * 0.10)

    metric_names = (
        "cpu_util_pct",
        "cpu_freq_mhz",
        "ram_used_bytes",
        "ram_available_bytes",
        "process_cpu_util_pct",
        "process_rss_bytes",
        "gpu_util_pct",
        "gpu_mem_util_pct",
        "gpu_mem_used_bytes",
        "gpu_clock_mhz",
        "gpu_power_w",
        "gpu_temp_c",
        "gpu_clock_event_reasons",
        "gpu_thermal_slowdown_active",
    )
    metrics: dict[str, Any] = {}
    for name in metric_names:
        values = [float(value) for event in events if (value := getattr(event, name)) is not None]
        metrics[name] = describe(values)

    monotonic_non_decreasing = all(left <= right for left, right in zip(monotonic_ns, monotonic_ns[1:], strict=False))
    wall_non_decreasing = all(left <= right for left, right in zip(wall_ns, wall_ns[1:], strict=False))
    sequence_contiguous = [event.sequence for event in events] == list(range(len(events)))
    interval_error_summary = describe(interval_errors)
    interval_p95_error = interval_error_summary["p95"]
    quality_checks = [
        {
            "name": "sample_count_nonzero",
            "passed": bool(events),
            "actual": len(events),
            "threshold": 1,
        },
        {
            "name": "sequence_contiguous",
            "passed": sequence_contiguous,
            "actual": sequence_contiguous,
            "threshold": True,
        },
        {
            "name": "monotonic_non_decreasing",
            "passed": monotonic_non_decreasing,
            "actual": monotonic_non_decreasing,
            "threshold": True,
        },
        {
            "name": "wall_non_decreasing",
            "passed": wall_non_decreasing,
            "actual": wall_non_decreasing,
            "threshold": True,
        },
        {
            "name": "interval_p95_abs_error_s",
            "passed": (
                interval_p95_error is not None
                and float(interval_p95_error) <= interval_tolerance_s
            ),
            "actual": interval_p95_error,
            "threshold": interval_tolerance_s,
        },
    ]
    return {
        "schema_version": 1,
        "sample_count": len(events),
        "requested_interval_s": requested_interval_s,
        "observed_duration_s": (
            (monotonic_ns[-1] - monotonic_ns[0]) / 1_000_000_000
            if len(monotonic_ns) >= 2
            else 0.0
        ),
        "monotonic_non_decreasing": monotonic_non_decreasing,
        "wall_non_decreasing": wall_non_decreasing,
        "sequence_contiguous": sequence_contiguous,
        "interval_s": describe(interval_s),
        "interval_abs_error_s": interval_error_summary,
        "interval_tolerance_s": interval_tolerance_s,
        "metrics": metrics,
        "quality_gate": {
            "status": (
                "passed" if all(check["passed"] for check in quality_checks) else "failed"
            ),
            "checks": quality_checks,
            "failed_checks": [
                check["name"] for check in quality_checks if not check["passed"]
            ],
        },
    }
