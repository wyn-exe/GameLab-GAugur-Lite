"""采集 Step 0 的 Windows 空载 CPU/GPU 基线。"""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil
from pynvml import (
    NVMLError,
    nvmlDeviceGetClockInfo,
    nvmlDeviceGetHandleByIndex,
    nvmlDeviceGetMemoryInfo,
    nvmlDeviceGetPowerUsage,
    nvmlDeviceGetTemperature,
    nvmlDeviceGetUtilizationRates,
    nvmlInit,
    nvmlShutdown,
    NVML_CLOCK_GRAPHICS,
    NVML_TEMPERATURE_GPU,
)


def _safe(call: Callable[[], Any]) -> Any:
    """NVML 某些字段不可用时返回空值，不中断整次采集。"""

    try:
        return call()
    except (NVMLError, NotImplementedError):
        return None


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(ratio * len(ordered)) - 1)
    return float(ordered[index])


def _describe(values: list[float]) -> dict[str, float | None]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "p95": _percentile(values, 0.95),
    }


def _process_snapshot() -> dict[int, dict[str, Any]]:
    """记录进程累计 CPU 时间，用窗口前后差值定位空载噪声来源。"""

    snapshot: dict[int, dict[str, Any]] = {}
    for process in psutil.process_iter(
        attrs=["pid", "name", "create_time", "cpu_times", "memory_info"]
    ):
        try:
            info = process.info
            cpu_times = info["cpu_times"]
            memory_info = info["memory_info"]
            if cpu_times is None:
                continue
            snapshot[info["pid"]] = {
                "name": info["name"] or "unknown",
                "create_time": info["create_time"],
                "cpu_seconds": float(cpu_times.user + cpu_times.system),
                "rss_mib": (
                    round(memory_info.rss / 1024**2, 2)
                    if memory_info is not None
                    else None
                ),
            }
        except (psutil.Error, OSError):
            continue
    return snapshot


def _top_cpu_processes(
    before: dict[int, dict[str, Any]],
    after: dict[int, dict[str, Any]],
    elapsed_s: float,
    limit: int,
) -> list[dict[str, Any]]:
    logical_cpu_count = psutil.cpu_count(logical=True) or 1
    rows: list[dict[str, Any]] = []
    for pid, final in after.items():
        # PID 0 表示未被使用的 CPU 时间，不是干扰进程。
        if pid == 0 or final["name"] == "System Idle Process":
            continue
        initial = before.get(pid)
        if initial is None or initial["create_time"] != final["create_time"]:
            continue
        delta = max(0.0, final["cpu_seconds"] - initial["cpu_seconds"])
        if delta <= 0:
            continue
        rows.append(
            {
                "name": final["name"],
                "pid": pid,
                "cpu_seconds_delta": round(delta, 3),
                "one_core_util_pct": round(delta / elapsed_s * 100, 2),
                "host_cpu_util_pct": round(
                    delta / elapsed_s / logical_cpu_count * 100, 3
                ),
                "rss_mib_end": final["rss_mib"],
            }
        )
    return sorted(rows, key=lambda row: row["cpu_seconds_delta"], reverse=True)[:limit]


def _quality_gate(
    metrics: dict[str, dict[str, float | None]],
    sample_count: int,
    expected_sample_count: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """执行项目级空载门槛；这些门槛不参与论文 QoS 标签计算。"""

    raw_checks = (
        ("sample_count", sample_count, ">=", expected_sample_count),
        ("cpu_mean_pct", metrics["cpu_util_pct"]["mean"], "<=", args.max_cpu_mean),
        ("cpu_p95_pct", metrics["cpu_util_pct"]["p95"], "<=", args.max_cpu_p95),
        ("gpu_mean_pct", metrics["gpu_util_pct"]["mean"], "<=", args.max_gpu_mean),
        ("gpu_p95_pct", metrics["gpu_util_pct"]["p95"], "<=", args.max_gpu_p95),
        ("gpu_temp_max_c", metrics["gpu_temp_c"]["max"], "<=", args.max_gpu_temp),
    )
    checks = []
    for name, actual, operator, threshold in raw_checks:
        passed = actual is not None and (
            actual >= threshold if operator == ">=" else actual <= threshold
        )
        checks.append(
            {
                "name": name,
                "actual": actual,
                "operator": operator,
                "threshold": threshold,
                "passed": passed,
            }
        )
    failed = [check["name"] for check in checks if not check["passed"]]
    return {
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "failed_checks": failed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-cpu-mean", type=float, default=15.0)
    parser.add_argument("--max-cpu-p95", type=float, default=35.0)
    parser.add_argument("--max-gpu-mean", type=float, default=5.0)
    parser.add_argument("--max-gpu-p95", type=float, default=20.0)
    parser.add_argument("--max-gpu-temp", type=float, default=70.0)
    parser.add_argument("--top-process-count", type=int, default=15)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration <= 0 or args.interval <= 0 or args.top_process_count <= 0:
        raise ValueError("duration、interval 和 top-process-count 必须为正数")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)

    nvmlInit()
    handle = nvmlDeviceGetHandleByIndex(args.gpu_index)
    psutil.cpu_percent(interval=None)  # 丢弃第一次无时间窗口的 CPU 采样。

    samples: list[dict[str, Any]] = []
    processes_before = _process_snapshot()
    start = time.perf_counter()
    next_sample = start

    try:
        with args.output.open("w", encoding="utf-8", newline="\n") as writer:
            while True:
                now = time.perf_counter()
                elapsed = now - start
                if elapsed >= args.duration:
                    break

                util = _safe(lambda: nvmlDeviceGetUtilizationRates(handle))
                memory = _safe(lambda: nvmlDeviceGetMemoryInfo(handle))
                sample = {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "elapsed_s": elapsed,
                    "cpu_util_pct": psutil.cpu_percent(interval=None),
                    "ram_util_pct": psutil.virtual_memory().percent,
                    "gpu_util_pct": getattr(util, "gpu", None),
                    "gpu_mem_util_pct": getattr(util, "memory", None),
                    "gpu_mem_used_mib": (
                        memory.used / 1024**2 if memory is not None else None
                    ),
                    "gpu_temp_c": _safe(
                        lambda: nvmlDeviceGetTemperature(handle, NVML_TEMPERATURE_GPU)
                    ),
                    "gpu_power_w": (
                        value / 1000
                        if (value := _safe(lambda: nvmlDeviceGetPowerUsage(handle)))
                        is not None
                        else None
                    ),
                    "gpu_clock_mhz": _safe(
                        lambda: nvmlDeviceGetClockInfo(handle, NVML_CLOCK_GRAPHICS)
                    ),
                }
                samples.append(sample)
                writer.write(json.dumps(sample, ensure_ascii=False) + "\n")
                writer.flush()

                next_sample += args.interval
                time.sleep(max(0.0, next_sample - time.perf_counter()))
    finally:
        nvmlShutdown()

    elapsed_total_s = time.perf_counter() - start
    processes_after = _process_snapshot()

    numeric_fields = (
        "cpu_util_pct",
        "ram_util_pct",
        "gpu_util_pct",
        "gpu_mem_util_pct",
        "gpu_mem_used_mib",
        "gpu_temp_c",
        "gpu_power_w",
        "gpu_clock_mhz",
    )
    metrics = {
        field: _describe(
            [float(row[field]) for row in samples if row[field] is not None]
        )
        for field in numeric_fields
    }
    expected_sample_count = max(1, math.ceil(args.duration / args.interval))
    quality_gate = _quality_gate(
        metrics, len(samples), expected_sample_count, args
    )
    summary = {
        "schema_version": 2,
        "duration_requested_s": args.duration,
        "duration_observed_s": elapsed_total_s,
        "interval_requested_s": args.interval,
        "sample_count": len(samples),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "metrics": metrics,
        "top_cpu_processes": _top_cpu_processes(
            processes_before,
            processes_after,
            elapsed_total_s,
            args.top_process_count,
        ),
        "quality_gate": quality_gate,
    }
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if quality_gate["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
