"""从真实共置 attempts 构建可追溯的 Step 8 truth table。"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .benchmarks.protocol import STABLE_BENCHMARK_PROTOCOL, stable_benchmark_environment_valid
from .config import config_sha256, stable_json_dumps
from .runner.plan import load_plan_rows, verify_plan
from .runner.runner import ParsedPlanRow, inspect_resume
from .schema import make_colocation_id, make_combination_key


class ColocationError(RuntimeError):
    """共置 run、独占基线或 truth table 未满足数据质量契约。"""


_MAIN_STAGE = "colocation-main"
_EXTRA_STAGE = "colocation-extra-test"
_EXPECTED_MAIN_RUNS = 180
_EXPECTED_EXTRA_RUNS = 36
_EXPECTED_MAIN_TARGETS = 456
_EXPECTED_EXTRA_TARGETS = 144
_MIN_STRESS_ACTIVE_FRACTION = 0.90


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_repo(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"路径必须位于仓库内且不能等于仓库根目录: {path}")
    return resolved


def _relative(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ColocationError(f"无法读取 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ColocationError(f"JSON 顶层必须为对象: {path}")
    return payload


def _colocation_rows(
    plan_file: Path, *, allow_pressure: bool = False
) -> tuple[list[ParsedPlanRow], list[ParsedPlanRow]]:
    """读取并验证固定的 180+36 个物理 run；压力修复实验可显式允许 benchmark。"""

    rows = [ParsedPlanRow.from_csv(raw) for raw in load_plan_rows(plan_file)]
    main_rows = [row for row in rows if row.stage == _MAIN_STAGE]
    extra_rows = [row for row in rows if row.stage == _EXTRA_STAGE]
    if len(main_rows) != _EXPECTED_MAIN_RUNS or len(extra_rows) != _EXPECTED_EXTRA_RUNS:
        raise ColocationError(
            "Step 8 计划行数不符: "
            f"main={len(main_rows)}/{_EXPECTED_MAIN_RUNS}, "
            f"extra={len(extra_rows)}/{_EXPECTED_EXTRA_RUNS}"
        )

    for row in (*main_rows, *extra_rows):
        expected_mode = "colocation" if row.stage == _MAIN_STAGE else "extra_test"
        expected_size = 4 if row.stage == _EXTRA_STAGE else None
        combination_key = make_combination_key(row.workload_ids)
        pressure_contract_ok = (
            row.resource is not None
            and row.pressure_requested is not None
            and row.pressure_applied is not None
            if allow_pressure
            else row.resource is None
            and row.pressure_requested is None
            and row.pressure_applied is None
        )
        if (
            row.mode != expected_mode
            or row.raw.get("combination_key") != combination_key
            or row.raw.get("colocation_id")
            != make_colocation_id(combination_key, row.repeat)
            or not pressure_contract_ok
            or (expected_size is not None and len(row.workload_ids) != expected_size)
            or (row.stage == _MAIN_STAGE and len(row.workload_ids) not in {2, 3})
        ):
            raise ColocationError(f"非法共置计划行: {row.run_id}")
        if row.stage == _EXTRA_STAGE and row.split != "extra_test":
            raise ColocationError(f"extra_test split 错误: {row.run_id}")
        if row.stage == _MAIN_STAGE and row.split not in {"train", "validation", "test"}:
            raise ColocationError(f"main split 错误: {row.run_id}")
    return main_rows, extra_rows


def _validate_plan_shape(
    main_rows: list[ParsedPlanRow], extra_rows: list[ParsedPlanRow]
) -> dict[str, Any]:
    main_by_key: dict[str, list[ParsedPlanRow]] = defaultdict(list)
    extra_by_key: dict[str, list[ParsedPlanRow]] = defaultdict(list)
    for row in main_rows:
        main_by_key[make_combination_key(row.workload_ids)].append(row)
    for row in extra_rows:
        extra_by_key[make_combination_key(row.workload_ids)].append(row)

    def repeat_check(grouped: dict[str, list[ParsedPlanRow]]) -> bool:
        return all(
            len(items) == 3 and sorted(item.repeat for item in items) == [1, 2, 3]
            for items in grouped.values()
        )

    main_pairs = [key for key, items in main_by_key.items() if len(items[0].workload_ids) == 2]
    main_triples = [key for key, items in main_by_key.items() if len(items[0].workload_ids) == 3]
    main_triple_workload_counts: Counter[str] = Counter()
    for key in main_triples:
        main_triple_workload_counts.update(main_by_key[key][0].workload_ids)
    extra_workload_counts: Counter[str] = Counter()
    extra_pair_counts: Counter[tuple[str, str]] = Counter()
    for items in extra_by_key.values():
        workload_ids = items[0].workload_ids
        extra_workload_counts.update(workload_ids)
        for first, second in itertools.combinations(workload_ids, 2):
            extra_pair_counts[(first, second)] += 1

    split_by_main_key = {
        key: {row.split for row in items} for key, items in main_by_key.items()
    }
    checks = {
        "main_pair_count_28": len(main_pairs) == 28,
        "main_triple_count_32": len(main_triples) == 32,
        "main_combination_count_60": len(main_by_key) == 60,
        "triple_workload_occurrences_twelve": len(main_triple_workload_counts) == 8
        and set(main_triple_workload_counts.values()) == {12},
        "extra_quad_count_12": len(extra_by_key) == 12,
        "main_three_repeats_per_key": repeat_check(main_by_key),
        "extra_three_repeats_per_key": repeat_check(extra_by_key),
        "main_extra_disjoint": not (set(main_by_key) & set(extra_by_key)),
        "main_split_stable_per_key": all(len(value) == 1 for value in split_by_main_key.values()),
        "main_split_counts_36_12_12": Counter(
            next(iter(value)) for value in split_by_main_key.values()
        )
        == Counter({"train": 36, "validation": 12, "test": 12}),
        "extra_workload_occurrences_six": len(extra_workload_counts) == 8
        and set(extra_workload_counts.values()) == {6},
        "extra_pair_cooccurrence_two_or_three": len(extra_pair_counts) == 28
        and set(extra_pair_counts.values()).issubset({2, 3}),
    }
    return {
        "checks": checks,
        "main_keys": sorted(main_by_key),
        "extra_keys": sorted(extra_by_key),
        "main_split_counts": dict(
            sorted(Counter(next(iter(value)) for value in split_by_main_key.values()).items())
        ),
        "triple_workload_occurrences": dict(sorted(main_triple_workload_counts.items())),
        "extra_workload_occurrences": dict(sorted(extra_workload_counts.items())),
        "extra_pair_cooccurrence": {
            "+".join(pair): count for pair, count in sorted(extra_pair_counts.items())
        },
    }


def _load_baselines(
    *, baseline_file: Path, expected_workload_ids: set[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = _read_json(baseline_file)
    baselines = payload.get("baselines")
    if (
        payload.get("status") != "passed"
        or not all(payload.get("checks", {}).values())
        or not isinstance(baselines, list)
    ):
        raise ColocationError("solo baseline 未通过质量门")
    indexed: dict[str, dict[str, Any]] = {}
    for baseline in baselines:
        if not isinstance(baseline, dict):
            raise ColocationError("solo baseline 条目必须为对象")
        workload_id = baseline.get("workload_id")
        mean_fps = baseline.get("mean_fps")
        if (
            not isinstance(workload_id, str)
            or workload_id in indexed
            or workload_id not in expected_workload_ids
            or baseline.get("valid_for_retention") is not True
            or not isinstance(mean_fps, (int, float))
            or not math.isfinite(float(mean_fps))
            or float(mean_fps) <= 0
        ):
            raise ColocationError(f"非法或不可用的 solo baseline: {workload_id!r}")
        indexed[workload_id] = baseline
    if set(indexed) != expected_workload_ids:
        raise ColocationError("solo baseline workload 集合与 Step 8 计划不一致")
    return indexed, payload


def audit_colocation_inputs(
    *, repo_root: Path, plan_file: Path, solo_baselines_file: Path, allow_pressure: bool = False
) -> dict[str, Any]:
    """只读预检 frozen plan 和 Step 6 baseline；不要求 216 个 attempt 已存在。"""

    root = repo_root.resolve()
    plan = _inside_repo(root, plan_file)
    baselines_file = _inside_repo(root, solo_baselines_file)
    verification = verify_plan(repo_root=root, plan_file=plan)
    if verification["status"] != "passed":
        raise ColocationError("共置计划未通过 verify")
    main_rows, extra_rows = _colocation_rows(plan, allow_pressure=allow_pressure)
    shape = _validate_plan_shape(main_rows, extra_rows)
    expected_workload_ids = {
        workload_id for row in (*main_rows, *extra_rows) for workload_id in row.workload_ids
    }
    baselines, baseline_payload = _load_baselines(
        baseline_file=baselines_file, expected_workload_ids=expected_workload_ids
    )
    plan_manifest = _read_json(plan.with_name(f"{plan.stem}-manifest.json"))
    config_hashes = {row.config_sha256 for row in (*main_rows, *extra_rows)}
    pressure_cells = {
        (row.resource, row.pressure_requested, row.pressure_applied)
        for row in (*main_rows, *extra_rows)
    }
    checks = {
        "plan_verified": True,
        "plan_generated_from_clean_commit": plan_manifest.get("root_dirty_at_generation") is False,
        "single_colocation_config_sha256": len(config_hashes) == 1,
        "baseline_status_passed": baseline_payload.get("status") == "passed",
        "baseline_workloads_match_plan": set(baselines) == expected_workload_ids,
        "single_pressure_cell": len(pressure_cells) == 1,
        **shape["checks"],
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise ColocationError("Step 8 输入预检失败: " + ", ".join(failed))
    return {
        "schema_version": 1,
        "status": "passed",
        "experiment_id": main_rows[0].experiment_id,
        "plan": _relative(root, plan),
        "plan_sha256": verification["plan_sha256"],
        "solo_baselines": _relative(root, baselines_file),
        "solo_baselines_sha256": _file_sha256(baselines_file),
        "main_physical_run_count": len(main_rows),
        "extra_physical_run_count": len(extra_rows),
        "expected_main_target_count": _EXPECTED_MAIN_TARGETS,
        "expected_extra_target_count": _EXPECTED_EXTRA_TARGETS,
        "config_sha256": next(iter(config_hashes)),
        "allow_pressure": allow_pressure,
        "pressure_cells": [
            {
                "resource": resource,
                "pressure_requested": requested,
                "pressure_applied": applied,
            }
            for resource, requested, applied in sorted(pressure_cells, key=str)
        ],
        "main_split_counts": shape["main_split_counts"],
        "triple_workload_occurrences": shape["triple_workload_occurrences"],
        "extra_workload_occurrences": shape["extra_workload_occurrences"],
        "extra_pair_cooccurrence": shape["extra_pair_cooccurrence"],
        "checks": checks,
    }


def _collect_run_record(
    *,
    repo_root: Path,
    row: ParsedPlanRow,
    plan_sha256: str,
    baselines: dict[str, dict[str, Any]],
    allow_pressure: bool = False,
    expected_benchmark_cpu_workers: int | None = None,
    expected_workload_fps_multiplier: float | None = None,
    expected_workload_cpu_affinity: tuple[int, ...] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """从一个哈希验证过的有效 attempt 提取物理 run 和每目标 truth。"""

    decision = inspect_resume(repo_root=repo_root, row=row)
    if decision.get("action") != "skip":
        raise ColocationError(f"共置 run 尚未形成有效 attempt: {row.run_id}")
    attempt_dir = _inside_repo(repo_root, repo_root / str(decision["directory"]))
    manifest = _read_json(attempt_dir / "manifest.json")
    summary = _read_json(attempt_dir / "summary.json")
    if (
        manifest.get("plan_sha256") != plan_sha256
        or manifest.get("row_sha256") != row.row_sha256
        or summary.get("status") != "completed"
        or summary.get("valid") is not True
        or summary.get("run_id") != row.run_id
        or summary.get("stage") != row.stage
        or summary.get("split") != row.split
        or tuple(summary.get("workload_ids", ())) != row.workload_ids
        or (not allow_pressure and summary.get("benchmark") is not None)
        or float(summary.get("system_coverage_ratio", 0.0)) < 0.95
        or float(summary.get("workload_overlap_ratio", 0.0)) < 0.95
        or summary.get("windows_pairwise_nonoverlap") is not True
        or summary.get("gpu_thermal_slowdown_seen") is True
        or summary.get("cleanup", {}).get("global_kill_used") is not False
        or (
            expected_workload_fps_multiplier is not None
            and abs(
                float(summary.get("workload_fps_multiplier", 0.0))
                - expected_workload_fps_multiplier
            )
            > 1e-9
        )
        or (
            expected_workload_cpu_affinity is not None
            and tuple(summary.get("workload_cpu_affinity") or ())
            != expected_workload_cpu_affinity
        )
        or (
            expected_workload_cpu_affinity is not None
            and tuple(
                manifest.get("runtime_contract", {}).get(
                    "workload_cpu_affinity"
                )
                or ()
            )
            != expected_workload_cpu_affinity
        )
        or (
            expected_workload_fps_multiplier is not None
            and abs(
                float(
                    manifest.get("runtime_contract", {}).get(
                        "workload_fps_multiplier", 0.0
                    )
                )
                - expected_workload_fps_multiplier
            )
            > 1e-9
        )
        or (
            allow_pressure
            and manifest.get("execution_provenance", {}).get("root_dirty_at_execution") is not False
        )
    ):
        raise ColocationError(f"共置 attempt 质量门未通过: {row.run_id}")
    provenance = manifest.get("execution_provenance")
    if not isinstance(provenance, dict) or not provenance.get("source_tree_sha256"):
        raise ColocationError(f"共置 attempt 缺少执行 provenance: {row.run_id}")
    workloads = summary.get("workloads")
    if not isinstance(workloads, list):
        raise ColocationError(f"共置 attempt 缺少 workload summaries: {row.run_id}")
    by_workload = {
        item.get("workload_id"): item for item in workloads if isinstance(item, dict)
    }
    if set(by_workload) != set(row.workload_ids) or len(by_workload) != len(row.workload_ids):
        raise ColocationError(f"共置 attempt workload 集合不一致: {row.run_id}")
    benchmark = summary.get("benchmark")
    if allow_pressure:
        if row.resource is None or row.pressure_applied is None or not isinstance(benchmark, dict):
            raise ColocationError(f"压力共置 attempt 缺少 benchmark: {row.run_id}")
        if (
            benchmark.get("resource") != row.resource
            or abs(float(benchmark.get("pressure_requested", -1.0)) - row.pressure_applied) > 1e-9
            or benchmark.get("status") != "completed"
            or benchmark.get("barrier_used") is not True
            or float(benchmark.get("active_fraction", 0.0)) < _MIN_STRESS_ACTIVE_FRACTION
            or int(benchmark.get("operations", 0)) <= 0
            or not stable_benchmark_environment_valid(
                benchmark.get("benchmark_environment"),
                expected_protocol=STABLE_BENCHMARK_PROTOCOL,
            )
        ):
            raise ColocationError(f"压力 benchmark 质量门未通过: {row.run_id}")
        if expected_benchmark_cpu_workers is not None and int(benchmark.get("cpu_workers", 0)) != expected_benchmark_cpu_workers:
            raise ColocationError(
                f"benchmark cpu_workers 不符: {row.run_id}; "
                f"actual={benchmark.get('cpu_workers')} expected={expected_benchmark_cpu_workers}"
            )

    combination_key = make_combination_key(row.workload_ids)
    common = {
        "schema_version": 1,
        "experiment_id": row.experiment_id,
        "run_id": row.run_id,
        "stage": row.stage,
        "split": row.split,
        "mode": row.mode,
        "combination_key": combination_key,
        "colocation_id": make_colocation_id(combination_key, row.repeat),
        "combination_size": len(row.workload_ids),
        "workload_ids": list(row.workload_ids),
        "repeat": row.repeat,
        "run_seed": int(row.raw["seed"]),
        "row_sha256": row.row_sha256,
        "config_sha256": row.config_sha256,
        "resource": row.resource,
        "pressure_requested": row.pressure_requested,
        "pressure_applied": row.pressure_applied,
        "benchmark_cpu_workers": benchmark.get("cpu_workers") if isinstance(benchmark, dict) else None,
        "benchmark_active_fraction": benchmark.get("active_fraction") if isinstance(benchmark, dict) else None,
        "benchmark_operations": benchmark.get("operations") if isinstance(benchmark, dict) else None,
        "benchmark_protocol": (
            benchmark.get("benchmark_environment", {}).get("protocol")
            if isinstance(benchmark, dict)
            else None
        ),
        "workload_fps_multiplier": summary.get("workload_fps_multiplier", 1.0),
        "workload_cpu_affinity": summary.get("workload_cpu_affinity"),
        "attempt": int(summary["attempt"]),
        "attempt_directory": _relative(repo_root, attempt_dir),
        "summary_sha256": _file_sha256(attempt_dir / "summary.json"),
        "manifest_sha256": _file_sha256(attempt_dir / "manifest.json"),
        "execution_root_commit": provenance.get("root_commit"),
        "execution_root_dirty": provenance.get("root_dirty_at_execution"),
        "execution_source_tree_sha256": provenance["source_tree_sha256"],
        "system_coverage_ratio": float(summary["system_coverage_ratio"]),
        "system_sample_count": int(summary["system_sample_count"]),
        "window_sample_count": int(summary["window_sample_count"]),
        "workload_overlap_ratio": float(summary["workload_overlap_ratio"]),
        "gpu_temp_c_max": summary.get("gpu_temp_c_max"),
        "gpu_thermal_slowdown_seen": bool(summary.get("gpu_thermal_slowdown_seen")),
        "windows_pairwise_nonoverlap": bool(summary.get("windows_pairwise_nonoverlap")),
    }
    target_records = []
    for workload_id in row.workload_ids:
        workload = by_workload[workload_id]
        fps = workload.get("game_fps")
        if (
            workload.get("status") != "completed"
            or workload.get("barrier_used") is not True
            or workload.get("quality_gate", {}).get("status") != "passed"
            or not isinstance(fps, dict)
            or float(workload.get("measurement_coverage_ratio", 0.0)) < 0.95
            or (
                expected_workload_fps_multiplier is not None
                and abs(
                    float(workload.get("fps_multiplier", 0.0))
                    - expected_workload_fps_multiplier
                )
                > 1e-9
            )
            or (
                expected_workload_cpu_affinity is not None
                and tuple(workload.get("cpu_affinity") or ())
                != expected_workload_cpu_affinity
            )
        ):
            raise ColocationError(f"目标 workload 质量门未通过: {row.run_id}/{workload_id}")
        mean_fps = float(fps["mean"])
        p05_fps = float(fps["p05"])
        min_fps = float(fps["min"])
        if not all(math.isfinite(value) and value > 0 for value in (mean_fps, p05_fps, min_fps)):
            raise ColocationError(f"目标 FPS 非法: {row.run_id}/{workload_id}")
        baseline = baselines[workload_id]
        baseline_mean = float(baseline["mean_fps"])
        baseline_p05 = float(baseline["p05_fps"])
        target_records.append(
            {
                **common,
                "target_id": workload_id,
                "neighbor_ids": [item for item in row.workload_ids if item != workload_id],
                "target_fps": int(workload["target_fps"]),
                "controller_trace_sha256": workload.get("controller_trace_sha256"),
                "game_metrics_sha256": summary["artifact_sha256"].get(
                    f"workloads/{workload_id}/game_metrics.jsonl"
                ),
                "mean_fps": mean_fps,
                "p05_fps": p05_fps,
                "min_fps": min_fps,
                "fps_windows_used": int(fps["windows_used"]),
                "measurement_coverage_ratio": float(workload["measurement_coverage_ratio"]),
                "frame_time_p95_ms": float(workload["draw_interval_ms"]["p95"]),
                "missed_deadline_count": int(workload["missed_deadline_count"]),
                "solo_baseline_id": baseline["baseline_id"],
                "solo_mean_fps": baseline_mean,
                "solo_p05_fps": baseline_p05,
                "retention_ratio": mean_fps / baseline_mean,
                "loss_ratio": 1.0 - mean_fps / baseline_mean,
                "p05_retention_ratio": p05_fps / baseline_p05,
            }
        )
    target_records.sort(key=lambda item: item["target_id"])
    physical_record = {
        **common,
        "target_count": len(target_records),
        "target_mean_fps": {item["target_id"]: item["mean_fps"] for item in target_records},
        "target_retention_ratio": {
            item["target_id"]: item["retention_ratio"] for item in target_records
        },
    }
    return physical_record, target_records


def _aggregate_targets(targets: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for item in targets:
        grouped[(str(item["stage"]), int(item["combination_size"]))].append(item)
    by_stage_size = []
    for (stage, size), items in sorted(grouped.items()):
        retention = [float(item["retention_ratio"]) for item in items]
        by_stage_size.append(
            {
                "stage": stage,
                "combination_size": size,
                "target_sample_count": len(items),
                "retention_mean": statistics.fmean(retention),
                "retention_sample_std": statistics.stdev(retention)
                if len(retention) > 1
                else 0.0,
                "retention_min": min(retention),
                "retention_max": max(retention),
                "retention_above_one_count": sum(value > 1.0 for value in retention),
            }
        )
    return {
        "by_stage_and_size": by_stage_size,
        "retention_above_one_count": sum(
            float(item["retention_ratio"]) > 1.0 for item in targets
        ),
        "retention_min": min(float(item["retention_ratio"]) for item in targets),
        "retention_max": max(float(item["retention_ratio"]) for item in targets),
    }


def compute_colocation_truth(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    allow_pressure: bool = False,
    expected_benchmark_cpu_workers: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """从所有正式共置 attempts 精确重算物理 run 与 target-level truth。"""

    root = repo_root.resolve()
    audit = audit_colocation_inputs(
        repo_root=root,
        plan_file=plan_file,
        solo_baselines_file=solo_baselines_file,
        allow_pressure=allow_pressure,
    )
    plan = _inside_repo(root, plan_file)
    main_rows, extra_rows = _colocation_rows(plan, allow_pressure=allow_pressure)
    expected_workload_ids = {
        workload_id for row in (*main_rows, *extra_rows) for workload_id in row.workload_ids
    }
    baselines, _ = _load_baselines(
        baseline_file=_inside_repo(root, solo_baselines_file),
        expected_workload_ids=expected_workload_ids,
    )
    physical_records: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for row in (*main_rows, *extra_rows):
        physical, target_records = _collect_run_record(
            repo_root=root,
            row=row,
            plan_sha256=str(audit["plan_sha256"]),
            baselines=baselines,
            allow_pressure=allow_pressure,
            expected_benchmark_cpu_workers=expected_benchmark_cpu_workers,
        )
        physical_records.append(physical)
        targets.extend(target_records)
    physical_records.sort(key=lambda item: (item["stage"], item["combination_key"], item["repeat"]))
    targets.sort(
        key=lambda item: (
            item["stage"],
            item["combination_key"],
            item["repeat"],
            item["target_id"],
        )
    )

    main_targets = [item for item in targets if item["stage"] == _MAIN_STAGE]
    extra_targets = [item for item in targets if item["stage"] == _EXTRA_STAGE]
    physical_keys = {(item["stage"], item["run_id"]) for item in physical_records}
    target_keys = {
        (item["stage"], item["combination_key"], item["repeat"], item["target_id"])
        for item in targets
    }
    source_hashes = sorted({str(item["execution_source_tree_sha256"]) for item in physical_records})
    root_commits = sorted(
        {
            str(item["execution_root_commit"])
            for item in physical_records
            if item["execution_root_commit"] is not None
        }
    )
    config_hashes = sorted({str(item["config_sha256"]) for item in physical_records})
    aggregate = _aggregate_targets(targets)
    checks = {
        **audit["checks"],
        "main_physical_run_count_180": len(
            [item for item in physical_records if item["stage"] == _MAIN_STAGE]
        )
        == _EXPECTED_MAIN_RUNS,
        "extra_physical_run_count_36": len(
            [item for item in physical_records if item["stage"] == _EXTRA_STAGE]
        )
        == _EXPECTED_EXTRA_RUNS,
        "main_target_count_456": len(main_targets) == _EXPECTED_MAIN_TARGETS,
        "extra_target_count_144": len(extra_targets) == _EXPECTED_EXTRA_TARGETS,
        "unique_physical_run_keys": len(physical_keys) == len(physical_records),
        "unique_target_truth_keys": len(target_keys) == len(targets),
        "all_target_counts_match_combination_size": all(
            int(item["target_count"]) == int(item["combination_size"])
            for item in physical_records
        ),
        "all_target_coverage": all(
            float(item["measurement_coverage_ratio"]) >= 0.95 for item in targets
        ),
        "all_system_coverage": all(
            float(item["system_coverage_ratio"]) >= 0.95 for item in physical_records
        ),
        "all_workload_overlap": all(
            float(item["workload_overlap_ratio"]) >= 0.95 for item in physical_records
        ),
        "all_windows_nonoverlap": all(
            item["windows_pairwise_nonoverlap"] is True for item in physical_records
        ),
        "no_gpu_thermal_slowdown": not any(
            item["gpu_thermal_slowdown_seen"] is True for item in physical_records
        ),
        "single_execution_source_tree": len(source_hashes) == 1,
        "single_execution_root_commit": len(root_commits) == 1,
        "single_colocation_config_sha256": len(config_hashes) == 1,
        "finite_positive_retention": all(
            math.isfinite(float(item["retention_ratio"]))
            and float(item["retention_ratio"]) > 0
            for item in targets
        ),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise ColocationError("共置 truth 质量门失败: " + ", ".join(failed))
    result = {
        "schema_version": 1,
        "status": "passed",
        "experiment_id": audit["experiment_id"],
        "definition": {
            "retention_ratio": "target colocated mean FPS / frozen solo baseline mean FPS",
            "loss_ratio": "1 - retention_ratio; values below zero are preserved rather than clipped",
            "physical_run": "one synchronized process set identified by colocation_id",
            "target_truth": "one target workload extracted from one physical run",
        },
        "inputs": {
            "plan": audit["plan"],
            "plan_sha256": audit["plan_sha256"],
            "solo_baselines": audit["solo_baselines"],
            "solo_baselines_sha256": audit["solo_baselines_sha256"],
        },
        "execution": {
            "source_tree_sha256s": source_hashes,
            "root_commits": root_commits,
            "root_dirty_values": sorted(
                {str(item["execution_root_dirty"]) for item in physical_records}
            ),
        },
        "physical_run_count": len(physical_records),
        "target_truth_count": len(targets),
        "main_physical_run_count": len(
            [item for item in physical_records if item["stage"] == _MAIN_STAGE]
        ),
        "extra_physical_run_count": len(
            [item for item in physical_records if item["stage"] == _EXTRA_STAGE]
        ),
        "main_target_truth_count": len(main_targets),
        "extra_target_truth_count": len(extra_targets),
        "aggregate": aggregate,
        "checks": checks,
        "allow_pressure": allow_pressure,
        "expected_benchmark_cpu_workers": expected_benchmark_cpu_workers,
    }
    return result, physical_records, targets


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(stable_json_dumps(value, indent=2) + "\n")


def _jsonl_payload(rows: list[dict[str, Any]]) -> str:
    """以唯一的稳定序列化格式构造 JSONL，供恢复时逐字节核对。"""

    return "".join(stable_json_dumps(row) + "\n" for row in rows)


def _write_jsonl_exclusive(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_jsonl_payload(rows))


def _validate_recoverable_runs_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """只允许复用与本次重算结果逐字节相同的中间 JSONL。"""

    try:
        actual = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ColocationError(f"无法读取待恢复的共置 JSONL: {path}") from exc
    if actual != _jsonl_payload(rows):
        raise ColocationError(f"已有共置 JSONL 与 raw attempt 重算结果不一致: {path}")


def _write_parquet_exclusive(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - formal environment locks pyarrow.
        raise RuntimeError("colocation parquet 需要 pyarrow") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"拒绝覆盖 parquet: {path}")
    if not rows:
        raise ValueError("colocation parquet 不允许写入空 truth table")
    column_names = list(rows[0])
    if any(list(row) != column_names for row in rows):
        raise ValueError("colocation truth 的字段集合或顺序不一致")
    arrays = []
    for name in column_names:
        values = [row[name] for row in rows]
        if name == "run_seed":
            # 冻结计划使用完整 uint64 种子；不能让 PyArrow 误推断为 int64。
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value >= 2**64
                for value in values
            ):
                raise ValueError("run_seed 必须是 uint64 范围内的整数")
            arrays.append(pa.array(values, type=pa.uint64()))
        else:
            arrays.append(pa.array(values))
    pq.write_table(pa.Table.from_arrays(arrays, names=column_names), path, compression="zstd")


def _write_plot_exclusive(path: Path, targets: list[dict[str, Any]]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - formal environment locks matplotlib.
        raise RuntimeError("colocation plot 需要 matplotlib") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"拒绝覆盖图表: {path}")
    sizes = sorted({int(item["combination_size"]) for item in targets})
    values = [
        [
            float(item["retention_ratio"])
            for item in targets
            if int(item["combination_size"]) == size
        ]
        for size in sizes
    ]
    figure, axis = plt.subplots(figsize=(9, 6), constrained_layout=True)
    box = axis.boxplot(values, positions=sizes, widths=0.45, patch_artist=True)
    for patch, color in zip(box["boxes"], ("#4C78A8", "#F58518", "#54A24B"), strict=False):
        patch.set_facecolor(color)
        patch.set_alpha(0.45)
    for offset, size in enumerate(sizes):
        selected = [
            item
            for item in targets
            if int(item["combination_size"]) == size
        ]
        # 使用 record index 生成固定微偏移，图在独立复核时无需随机状态。
        x_values = [size + (((index % 9) - 4) * 0.025) for index in range(len(selected))]
        colors = [
            "#E45756" if item["stage"] == _EXTRA_STAGE else "#4C78A8"
            for item in selected
        ]
        axis.scatter(x_values, [item["retention_ratio"] for item in selected], s=14, alpha=0.55, c=colors)
    axis.axhline(1.0, color="black", linewidth=1.0, linestyle="--", label="solo retention = 1")
    axis.set_xticks(sizes)
    axis.set_xlabel("colocation size")
    axis.set_ylabel("mean-FPS retention ratio")
    axis.set_title("Measured retention by colocation size (Step 8)")
    axis.grid(axis="y", alpha=0.25)
    axis.legend(loc="best")
    with path.open("xb") as stream:
        figure.savefig(stream, format="png", dpi=160)
    plt.close(figure)


def build_colocation_truth(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    runs_output_file: Path,
    truth_output_file: Path,
    summary_file: Path,
    plot_file: Path,
    allow_pressure: bool = False,
    expected_benchmark_cpu_workers: int | None = None,
) -> dict[str, Any]:
    """独占写出 216-row 物理记录、600-row truth、summary 和实测图。"""

    root = repo_root.resolve()
    outputs = [
        _inside_repo(root, runs_output_file),
        _inside_repo(root, truth_output_file),
        _inside_repo(root, summary_file),
        _inside_repo(root, plot_file),
    ]
    result, physical_records, targets = compute_colocation_truth(
        repo_root=root,
        plan_file=plan_file,
        solo_baselines_file=solo_baselines_file,
        allow_pressure=allow_pressure,
        expected_benchmark_cpu_workers=expected_benchmark_cpu_workers,
    )
    runs_output, truth_output, summary_output, plot_output = outputs
    existing_nonrecoverable = [
        path for path in (truth_output, summary_output, plot_output) if path.exists()
    ]
    if existing_nonrecoverable:
        raise FileExistsError(
            "共置最终产物已存在，拒绝覆盖: " + ", ".join(map(str, existing_nonrecoverable))
        )
    if runs_output.exists():
        # Parquet 写入前异常时可保留 JSONL；仅在内容完全一致时断点恢复。
        _validate_recoverable_runs_jsonl(runs_output, physical_records)
    else:
        _write_jsonl_exclusive(runs_output, physical_records)
    _write_parquet_exclusive(truth_output, targets)
    _write_plot_exclusive(plot_output, targets)
    result["artifacts"] = {
        "colocation_runs": _relative(root, runs_output),
        "colocation_runs_sha256": _file_sha256(runs_output),
        "colocation_truth": _relative(root, truth_output),
        "colocation_truth_sha256": _file_sha256(truth_output),
        "retention_by_size_plot": _relative(root, plot_output),
        "retention_by_size_plot_sha256": _file_sha256(plot_output),
    }
    _write_json_exclusive(summary_output, result)
    return result


def verify_colocation_truth(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    runs_file: Path,
    truth_file: Path,
    summary_file: Path,
    plot_file: Path,
    allow_pressure: bool = False,
    expected_benchmark_cpu_workers: int | None = None,
) -> dict[str, Any]:
    """从 raw attempts 独立重算并核对 Step 8 全部派生产物。"""

    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("colocation verify 需要 pyarrow") from exc
    root = repo_root.resolve()
    stored = _read_json(_inside_repo(root, summary_file))
    recomputed, physical_records, targets = compute_colocation_truth(
        repo_root=root,
        plan_file=plan_file,
        solo_baselines_file=solo_baselines_file,
        allow_pressure=allow_pressure,
        expected_benchmark_cpu_workers=expected_benchmark_cpu_workers,
    )
    artifacts = stored.get("artifacts", {})
    stored_core = dict(stored)
    stored_core.pop("artifacts", None)
    disk_records = [
        json.loads(line)
        for line in _inside_repo(root, runs_file).read_text(encoding="utf-8").splitlines()
        if line
    ]
    disk_targets = pq.read_table(_inside_repo(root, truth_file)).to_pylist()
    plot = _inside_repo(root, plot_file)
    checks = [
        {
            "name": "summary_recomputed_exactly",
            "passed": stored_core == recomputed,
            "actual": config_sha256(stored_core),
            "expected": config_sha256(recomputed),
        },
        {
            "name": "physical_runs_recomputed_exactly",
            "passed": disk_records == physical_records,
            "actual": len(disk_records),
            "expected": len(physical_records),
        },
        {
            "name": "target_truth_recomputed_exactly",
            "passed": disk_targets == targets,
            "actual": len(disk_targets),
            "expected": len(targets),
        },
        {
            "name": "runs_sha256",
            "passed": _file_sha256(_inside_repo(root, runs_file))
            == artifacts.get("colocation_runs_sha256"),
            "actual": _file_sha256(_inside_repo(root, runs_file)),
            "expected": artifacts.get("colocation_runs_sha256"),
        },
        {
            "name": "truth_sha256",
            "passed": _file_sha256(_inside_repo(root, truth_file))
            == artifacts.get("colocation_truth_sha256"),
            "actual": _file_sha256(_inside_repo(root, truth_file)),
            "expected": artifacts.get("colocation_truth_sha256"),
        },
        {
            "name": "plot_sha256",
            "passed": plot.is_file()
            and _file_sha256(plot) == artifacts.get("retention_by_size_plot_sha256"),
            "actual": _file_sha256(plot) if plot.is_file() else None,
            "expected": artifacts.get("retention_by_size_plot_sha256"),
        },
        {
            "name": "plot_png_signature",
            "passed": plot.is_file() and plot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
            "actual": plot.suffix.lower() if plot.is_file() else None,
            "expected": "valid PNG",
        },
        {
            "name": "all_quality_checks",
            "passed": stored.get("status") == "passed"
            and all(stored.get("checks", {}).values()),
            "actual": stored.get("checks"),
            "expected": True,
        },
    ]
    return {
        "schema_version": 1,
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "check_count": len(checks),
        "passed_count": sum(bool(item["passed"]) for item in checks),
        "plan_sha256": recomputed["inputs"]["plan_sha256"],
        "summary_sha256": _file_sha256(_inside_repo(root, summary_file)),
        "checks": checks,
    }
