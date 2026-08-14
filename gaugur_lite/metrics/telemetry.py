"""Step 2 probe 与采样器 proxy overhead 实验。"""

from __future__ import annotations

import math
import statistics
import time
from pathlib import Path
from typing import Any

from ..config import stable_json_dumps
from .summarize import describe, summarize_events
from .system_sampler import SystemSampler
from .writer import JsonlWriter, StatusTracker, write_json_atomic


def expected_sample_count(duration_s: float, interval_s: float) -> int:
    return max(1, math.ceil(duration_s / interval_s))


def run_probe(
    *,
    duration_s: float,
    interval_s: float,
    gpu_index: int,
    output_directory: Path,
    run_id: str = "step2-probe",
    batch_size: int = 10,
) -> dict[str, Any]:
    if duration_s <= 0 or interval_s <= 0:
        raise ValueError("duration_s 和 interval_s 必须为正数")
    output_directory.mkdir(parents=True, exist_ok=True)
    metrics_file = output_directory / "system_metrics.jsonl"
    status_file = output_directory / "status.json"
    summary_file = output_directory / "summary.json"
    existing = [path.name for path in (metrics_file, status_file, summary_file) if path.exists()]
    if existing:
        raise FileExistsError(
            f"拒绝覆盖已有遥测产物 {existing!r}；请使用新的 --output-directory"
        )

    events = []
    target_count = expected_sample_count(duration_s, interval_s)
    start = time.perf_counter()
    next_sample = start
    deadline = start + duration_s
    with StatusTracker(status_file, run_id=run_id) as status:
        with JsonlWriter(metrics_file, batch_size=batch_size) as writer:
            with SystemSampler(run_id=run_id, gpu_index=gpu_index) as sampler:
                for sequence in range(target_count):
                    now = time.perf_counter()
                    if now < next_sample:
                        time.sleep(next_sample - now)
                    event = sampler.sample(sequence)
                    writer.write(event)
                    events.append(event)
                    status.update_samples(writer.count)
                    next_sample += interval_s

        # duration 表示完整采集会话时长；最后一个样本后仍等待到计划终点。
        remaining = deadline - time.perf_counter()
        if remaining > 0:
            time.sleep(remaining)

        summary = summarize_events(events, requested_interval_s=interval_s)
        summary.update(
            {
                "run_id": run_id,
                "duration_requested_s": duration_s,
                "capture_elapsed_s": time.perf_counter() - start,
                "output_files": {
                    "metrics": metrics_file.name,
                    "status": status_file.name,
                    "summary": summary_file.name,
                },
            }
        )
        write_json_atomic(summary_file, summary)
        if summary["quality_gate"]["status"] != "passed":
            raise RuntimeError(
                f"telemetry quality gate failed: {summary['quality_gate']['failed_checks']}"
            )
        status.mark_completed(samples_written=len(events), summary_file=summary_file.name)
    return summary


def _proxy_work(iterations: int) -> float:
    accumulator = 0.0
    for index in range(iterations):
        accumulator += ((index * 17) % 101) * 0.000001
    return accumulator


def _run_proxy_phase(
    *,
    duration_s: float,
    work_iterations: int,
    sampler: SystemSampler | None,
    writer: JsonlWriter | None,
    sample_interval_s: float,
    sample_sequence_start: int,
) -> dict[str, Any]:
    start = time.perf_counter()
    deadline = start + duration_s
    next_sample = start
    frames = 0
    sample_calls = 0
    sentinel = 0.0
    while time.perf_counter() < deadline:
        sentinel += _proxy_work(work_iterations)
        frames += 1
        now = time.perf_counter()
        if sampler is not None and now >= next_sample:
            event = sampler.sample(sample_sequence_start + sample_calls)
            if writer is not None:
                writer.write(event)
            sample_calls += 1
            next_sample += sample_interval_s
    elapsed = time.perf_counter() - start
    return {
        "frames": frames,
        "elapsed_s": elapsed,
        "proxy_fps": frames / elapsed,
        "sample_calls": sample_calls,
        "sentinel": sentinel,
    }


def run_overhead(
    *,
    duration_s: float,
    interval_s: float,
    gpu_index: int,
    output_file: Path,
    repeats: int = 3,
    work_iterations: int = 20_000,
) -> dict[str, Any]:
    """量化采样器对合成帧循环的影响，不将 proxy FPS 冒充 game_fps。"""

    if duration_s <= 0 or interval_s <= 0 or repeats < 2 or work_iterations < 1:
        raise ValueError("duration/interval 必须为正，repeats >= 2，work_iterations >= 1")

    raw_output_file = output_file.with_name(f"{output_file.stem}-metrics.jsonl")
    status_file = output_file.with_name(f"{output_file.stem}-status.json")
    existing = [
        path.name for path in (output_file, raw_output_file, status_file) if path.exists()
    ]
    if existing:
        raise FileExistsError(
            f"拒绝覆盖已有开销产物 {existing!r}；请使用新的 --output"
        )
    phase_duration_s = duration_s / (repeats * 2)
    phases: list[dict[str, Any]] = []
    sample_sequence = 0
    # 交替起始顺序抵消机器热身和时间趋势；paired repeat 内两种模式相邻。
    orders = (("without_sampler", "with_sampler"), ("with_sampler", "without_sampler"))
    with StatusTracker(status_file, run_id="step2-overhead") as status_tracker:
        with JsonlWriter(raw_output_file, batch_size=10) as writer:
            with SystemSampler(run_id="step2-overhead", gpu_index=gpu_index) as sampler:
                for repeat in range(repeats):
                    for mode in orders[repeat % len(orders)]:
                        phase = _run_proxy_phase(
                            duration_s=phase_duration_s,
                            work_iterations=work_iterations,
                            sampler=sampler if mode == "with_sampler" else None,
                            writer=writer if mode == "with_sampler" else None,
                            sample_interval_s=interval_s,
                            sample_sequence_start=sample_sequence,
                        )
                        sample_sequence += phase["sample_calls"]
                        status_tracker.update_samples(writer.count)
                        phase.update({"repeat": repeat + 1, "mode": mode})
                        phases.append(phase)

        by_repeat: list[dict[str, Any]] = []
        for repeat in range(1, repeats + 1):
            pair = {row["mode"]: row for row in phases if row["repeat"] == repeat}
            baseline_fps = pair["without_sampler"]["proxy_fps"]
            sampled_fps = pair["with_sampler"]["proxy_fps"]
            impact_pct = (sampled_fps / baseline_fps - 1.0) * 100
            by_repeat.append(
                {
                    "repeat": repeat,
                    "without_sampler_proxy_fps": baseline_fps,
                    "with_sampler_proxy_fps": sampled_fps,
                    "impact_pct": impact_pct,
                }
            )

        impacts = [row["impact_pct"] for row in by_repeat]
        absolute_impacts = [abs(value) for value in impacts]
        result = {
            "schema_version": 1,
            "benchmark_kind": "synthetic_frame_loop_proxy",
            "duration_requested_s": duration_s,
            "phase_duration_s": phase_duration_s,
            "sample_interval_s": interval_s,
            "repeats": repeats,
            "work_iterations_per_frame": work_iterations,
            "raw_metrics_file": raw_output_file.name,
            "status_file": status_file.name,
            "raw_sample_count": sample_sequence,
            "phases": phases,
            "paired_results": by_repeat,
            "impact_pct": describe(impacts),
            "absolute_impact_pct": describe(absolute_impacts),
            "median_impact_pct": statistics.median(impacts),
            "acceptance_threshold_abs_median_pct": 5.0,
            "status": (
                "passed" if abs(statistics.median(impacts)) <= 5.0 else "failed"
            ),
            "note": "proxy_fps only; Step 5 must repeat overhead validation with real Pyxel game_fps",
        }
        output_file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(output_file, result)
        status_tracker.mark_completed(
            samples_written=sample_sequence, summary_file=output_file.name
        )
    return result


def format_result(result: dict[str, Any]) -> str:
    return stable_json_dumps(result, indent=2)
