"""为方法有效性验证准备真实资源压力共置实验。"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Iterable

from .benchmarks.engine import BENCHMARK_RESOURCES
from .colocation import (
    _EXPECTED_EXTRA_RUNS,
    _EXPECTED_MAIN_RUNS,
    _collect_run_record,
    _colocation_rows,
    _load_baselines,
    audit_colocation_inputs,
)
from .config import config_sha256, load_local_config, stable_json_dumps
from .runner.plan import (
    PLAN_COLUMNS,
    _file_sha256,
    _git_state,
    load_plan_rows,
    verify_plan,
)
from .runner.runner import inspect_resume
from .schema import RunMode, make_run_id


class EffectivenessError(RuntimeError):
    """有效性修复实验的计划、压力或标签质量门失败。"""


def summarize_qos_thresholds(
    retentions: Iterable[float], thresholds: Iterable[float]
) -> list[dict[str, Any]]:
    """在不重跑实验的前提下，统计不同 QoS ratio 的标签分布。"""

    values = [float(value) for value in retentions]
    if not values or not all(math.isfinite(value) and value > 0 for value in values):
        raise EffectivenessError("retention 样本必须非空、有限且为正数")
    normalized: list[float] = []
    for threshold in thresholds:
        value = float(threshold)
        if not 0.0 < value <= 1.0 or not math.isfinite(value):
            raise EffectivenessError("QoS ratio 必须位于 (0, 1]")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise EffectivenessError("至少需要一个 QoS ratio")
    result: list[dict[str, Any]] = []
    for threshold in normalized:
        negative = sum(value < threshold for value in values)
        positive = len(values) - negative
        result.append(
            {
                "qos_ratio": threshold,
                "target_count": len(values),
                "positive_target_count": positive,
                "negative_target_count": negative,
                "positive_fraction": positive / len(values),
                "negative_fraction": negative / len(values),
                "retention_min": min(values),
                "retention_max": max(values),
                "retention_mean": sum(values) / len(values),
            }
        )
    return result


def analyze_qos_thresholds(
    *, source: Path, output_dir: Path, thresholds: Iterable[float]
) -> dict[str, Any]:
    """读取既有共置 truth，输出阈值敏感性 JSON/CSV/PNG，不修改原始数据。"""

    source = source.resolve()
    output_dir = output_dir.resolve()
    if not source.is_file():
        raise EffectivenessError(f"truth parquet 不存在: {source}")
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - formal environment supplies pyarrow.
        raise EffectivenessError("阈值敏感性分析需要 pyarrow") from exc
    rows = pq.read_table(source, columns=["retention_ratio"]).to_pylist()
    summary = summarize_qos_thresholds(
        (row["retention_ratio"] for row in rows), thresholds
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "json": output_dir / "qos-threshold-sensitivity.json",
        "csv": output_dir / "qos-threshold-sensitivity.csv",
        "plot": output_dir / "qos-threshold-sensitivity.png",
    }
    existing = [path for path in outputs.values() if path.exists()]
    if existing:
        raise FileExistsError(f"阈值敏感性产物已存在，拒绝覆盖: {existing[0]}")
    try:
        source_hash = _file_sha256(source)
    except OSError as exc:
        raise EffectivenessError(f"无法计算 truth 哈希: {source}") from exc
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed",
        "analysis": "exploratory_qos_threshold_sensitivity",
        "source": {"path": str(source), "sha256": source_hash, "rows": len(rows)},
        "thresholds": summary,
        "interpretation": (
            "仅重算标签分布，不改变历史 truth、不训练模型，也不构成论文有效性验收。"
        ),
    }
    outputs["json"].write_text(stable_json_dumps(result, indent=2) + "\n", encoding="utf-8")
    with outputs["csv"].open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    try:
        import matplotlib.pyplot as plt

        x_values = [item["qos_ratio"] for item in summary]
        negative_values = [item["negative_target_count"] for item in summary]
        positive_values = [item["positive_target_count"] for item in summary]
        figure, axis = plt.subplots(figsize=(7.2, 4.2))
        axis.plot(x_values, positive_values, marker="o", label="positive targets")
        axis.plot(x_values, negative_values, marker="o", label="negative targets")
        axis.set_xlabel("QoS ratio threshold")
        axis.set_ylabel("Target count")
        axis.set_title("QoS threshold sensitivity (existing truth only)")
        axis.grid(alpha=0.25)
        axis.legend()
        figure.tight_layout()
        figure.savefig(outputs["plot"], dpi=160)
        plt.close(figure)
    except ImportError as exc:  # pragma: no cover - formal environment includes matplotlib.
        raise EffectivenessError("阈值敏感性图表需要 matplotlib") from exc
    return result


_EXPECTED_SOLO_RUNS = 24
_EXPECTED_HIGH_FPS_ROWS = _EXPECTED_SOLO_RUNS + _EXPECTED_MAIN_RUNS + _EXPECTED_EXTRA_RUNS


def _format_cell(value: float | None) -> str:
    return "" if value is None else format(value, ".10g")


def _parse_cpu_affinity(value: str | None) -> tuple[int, ...] | None:
    """把计划参数规范化为可写入 manifest 的逻辑 CPU 元组。"""

    if value is None or not value.strip():
        return None
    try:
        cpus = tuple(sorted({int(token.strip()) for token in value.split(",")}))
    except ValueError as exc:
        raise EffectivenessError("cpu_affinity 必须是逗号分隔的非负整数") from exc
    if not cpus or any(cpu < 0 for cpu in cpus):
        raise EffectivenessError("cpu_affinity 必须至少包含一个非负 CPU 编号")
    return cpus


def _stress_rows(
    rows: list[dict[str, str]],
    *,
    experiment_id: str,
    resource: str,
    pressure_requested: float,
    pressure_applied: float,
    config_hash: str,
    root_commit: str,
    raw_root: str,
) -> list[dict[str, str]]:
    """把原有 216 个组合 run 转成同结构的真实压力共置 run。"""

    selected = [
        row
        for row in rows
        if row.get("stage") in {"colocation-main", "colocation-extra-test"}
    ]
    if len(selected) != _EXPECTED_MAIN_RUNS + _EXPECTED_EXTRA_RUNS:
        raise EffectivenessError(f"基础共置计划必须含 216 行，实际 {len(selected)} 行")
    output: list[dict[str, str]] = []
    for index, source in enumerate(selected, start=1):
        mode = RunMode(source["mode"])
        if mode not in {RunMode.COLOCATION, RunMode.EXTRA_TEST}:
            raise EffectivenessError(f"基础共置计划含非法 mode: {source['run_id']}")
        combination_key = source["combination_key"]
        target_id = source["target_id"]
        repeat = int(source["repeat"])
        run_id = make_run_id(
            experiment_id=experiment_id,
            mode=mode,
            target_id=target_id,
            repeat=repeat,
            resource=resource,
            pressure_requested=pressure_requested,
            combination_key=combination_key,
        )
        row = {column: source.get(column, "") for column in PLAN_COLUMNS}
        row.update(
            {
                "schema_version": "2",
                "execution_index": str(index),
                "run_id": run_id,
                "experiment_id": experiment_id,
                "resource": resource,
                "pressure_requested": _format_cell(pressure_requested),
                "pressure_applied": _format_cell(pressure_applied),
                "config_sha256": config_hash,
                "root_commit": root_commit,
                "run_directory": f"{raw_root.rstrip('/')}/{run_id}",
                "row_sha256": "",
            }
        )
        row["row_sha256"] = config_sha256(
            {key: value for key, value in row.items() if key != "row_sha256"}
        )
        output.append(row)
    return output


def _high_fps_rows(
    rows: list[dict[str, str]],
    *,
    experiment_id: str,
    fps_multiplier: float,
    config_hash: str,
    root_commit: str,
    raw_root: str,
) -> list[dict[str, str]]:
    """克隆 solo/共置形状，只提高真实游戏循环频率，不注入外部 benchmark。"""

    selected = [
        row
        for row in rows
        if row.get("stage") in {"solo", "colocation-main", "colocation-extra-test"}
    ]
    if len(selected) != _EXPECTED_HIGH_FPS_ROWS:
        raise EffectivenessError(
            f"高帧率计划必须含 240 行 solo/共置，实际 {len(selected)} 行"
        )
    output: list[dict[str, str]] = []
    for index, source in enumerate(selected, start=1):
        mode = RunMode(source["mode"])
        if mode not in {RunMode.SOLO, RunMode.COLOCATION, RunMode.EXTRA_TEST}:
            raise EffectivenessError(f"高帧率计划含非法 mode: {source['run_id']}")
        combination_key = source["combination_key"] or None
        run_id = make_run_id(
            experiment_id=experiment_id,
            mode=mode,
            target_id=source["target_id"],
            repeat=int(source["repeat"]),
            combination_key=combination_key,
        )
        row = {column: source.get(column, "") for column in PLAN_COLUMNS}
        row.update(
            {
                "schema_version": "2",
                "execution_index": str(index),
                "run_id": run_id,
                "experiment_id": experiment_id,
                "resource": "",
                "pressure_requested": "",
                "pressure_applied": "",
                "config_sha256": config_hash,
                "root_commit": root_commit,
                "run_directory": f"{raw_root.rstrip('/')}/{run_id}",
                "row_sha256": "",
            }
        )
        row["row_sha256"] = config_sha256(
            {key: value for key, value in row.items() if key != "row_sha256"}
        )
        output.append(row)
    return output


def build_high_fps_plan(
    *,
    repo_root: Path,
    base_plan: Path,
    local_config: Path,
    output_plan: Path,
    experiment_id: str,
    fps_multiplier: float,
    raw_root: str,
    cpu_affinity: str | None = None,
) -> dict[str, Any]:
    """生成真实游戏高帧率压力计划；共置阶段不携带外部 benchmark。"""

    root = repo_root.resolve()
    base = base_plan.resolve()
    output = output_plan.resolve()
    affinity_tuple = _parse_cpu_affinity(cpu_affinity)
    if not math.isfinite(fps_multiplier) or not 1.0 < fps_multiplier <= 16.0:
        raise EffectivenessError("高帧率修复 fps_multiplier 必须位于 (1, 16]")
    if root not in output.parents:
        raise EffectivenessError("高帧率计划输出必须位于仓库内")
    sidecar_manifest = output.with_name(f"{output.stem}-manifest.json")
    sidecar_combinations = output.with_name(f"{output.stem}-combinations.json")
    existing = [path for path in (output, sidecar_manifest, sidecar_combinations) if path.exists()]
    if existing:
        raise FileExistsError("高帧率计划产物已存在，拒绝覆盖: " + ", ".join(map(str, existing)))
    verification = verify_plan(repo_root=root, plan_file=base)
    if verification["status"] != "passed":
        raise EffectivenessError("基础计划未通过 verify_plan")
    load_local_config(local_config)
    root_commit, root_dirty = _git_state(root)
    if root_commit is None or root_dirty is not False:
        raise EffectivenessError("生成高帧率计划前必须处于已提交且 clean 的源码状态")
    source_sha = _file_sha256(base)
    campaign_hash = config_sha256(
        {
            "source_plan_sha256": source_sha,
            "experiment_id": experiment_id,
            "fps_multiplier": fps_multiplier,
            "raw_root": raw_root,
            "cpu_affinity": list(affinity_tuple) if affinity_tuple is not None else None,
        }
    )
    rows = _high_fps_rows(
        load_plan_rows(base),
        experiment_id=experiment_id,
        fps_multiplier=fps_multiplier,
        config_hash=campaign_hash,
        root_commit=root_commit,
        raw_root=raw_root,
    )
    source_combinations = json.loads(
        base.with_name(f"{base.stem}-combinations.json").read_text(encoding="utf-8")
    )
    combinations = dict(source_combinations)
    combinations.update(
        {
            "experiment_id": experiment_id,
            "config_sha256": campaign_hash,
            "effectiveness": {
                "source_plan": base.relative_to(root).as_posix(),
                "source_plan_sha256": source_sha,
                "workload_fps_multiplier": fps_multiplier,
                "workload_cpu_affinity": list(affinity_tuple)
                if affinity_tuple is not None
                else None,
            },
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PLAN_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    with sidecar_combinations.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(stable_json_dumps(combinations, indent=2) + "\n")
    stage_counts = {
        stage: sum(row["stage"] == stage for row in rows)
        for stage in ("solo", "colocation-main", "colocation-extra-test")
    }
    manifest = {
        "schema_version": 2,
        "status": "completed",
        "experiment_id": experiment_id,
        "plan_file": output.relative_to(root).as_posix(),
        "plan_sha256": _file_sha256(output),
        "combination_manifest": sidecar_combinations.relative_to(root).as_posix(),
        "combination_manifest_sha256": _file_sha256(sidecar_combinations),
        "row_count": len(rows),
        "stage_counts": stage_counts,
        "source_plan": base.relative_to(root).as_posix(),
        "source_plan_sha256": source_sha,
        "config_sha256": campaign_hash,
        "workload_fps_multiplier": fps_multiplier,
        "workload_cpu_affinity": list(affinity_tuple)
        if affinity_tuple is not None
        else None,
        "root_commit": root_commit,
        "root_dirty_at_generation": root_dirty,
    }
    with sidecar_manifest.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(stable_json_dumps(manifest, indent=2) + "\n")
    return manifest


def build_stress_plan(
    *,
    repo_root: Path,
    base_plan: Path,
    local_config: Path,
    output_plan: Path,
    experiment_id: str,
    resource: str,
    pressure: float,
    cpu_workers: int,
    raw_root: str,
) -> dict[str, Any]:
    """独占生成压力共置计划，不改写原 Safety-v2 计划。"""

    root = repo_root.resolve()
    base = base_plan.resolve()
    output = output_plan.resolve()
    if resource not in BENCHMARK_RESOURCES:
        raise EffectivenessError(f"未知 benchmark resource: {resource}")
    if not math.isfinite(pressure) or not 0.0 < pressure <= 1.0:
        raise EffectivenessError("压力修复实验 pressure 必须位于 (0, 1]")
    if not 32 <= cpu_workers <= 64:
        raise EffectivenessError("压力修复实验 cpu_workers 必须位于 [32, 64]")
    if root not in output.parents:
        raise EffectivenessError("压力计划输出必须位于仓库内")
    sidecar_manifest = output.with_name(f"{output.stem}-manifest.json")
    sidecar_combinations = output.with_name(f"{output.stem}-combinations.json")
    existing = [path for path in (output, sidecar_manifest, sidecar_combinations) if path.exists()]
    if existing:
        raise FileExistsError("压力计划产物已存在，拒绝覆盖: " + ", ".join(map(str, existing)))

    verification = verify_plan(repo_root=root, plan_file=base)
    if verification["status"] != "passed":
        raise EffectivenessError("基础共置计划未通过 verify_plan")
    local = load_local_config(local_config)
    applied = pressure * float(local.measurement.pressure_caps[resource])
    root_commit, root_dirty = _git_state(root)
    if root_commit is None or root_dirty is not False:
        raise EffectivenessError("生成压力计划前必须处于已提交且 clean 的源码状态")
    source_sha = _file_sha256(base)
    campaign_hash = config_sha256(
        {
            "source_plan_sha256": source_sha,
            "experiment_id": experiment_id,
            "resource": resource,
            "pressure_requested": pressure,
            "pressure_applied": applied,
            "cpu_workers": cpu_workers,
            "raw_root": raw_root,
        }
    )
    rows = _stress_rows(
        load_plan_rows(base),
        experiment_id=experiment_id,
        resource=resource,
        pressure_requested=pressure,
        pressure_applied=applied,
        config_hash=campaign_hash,
        root_commit=root_commit,
        raw_root=raw_root,
    )
    source_combinations = json.loads(
        base.with_name(f"{base.stem}-combinations.json").read_text(encoding="utf-8")
    )
    combinations = dict(source_combinations)
    combinations.update(
        {
            "experiment_id": experiment_id,
            "config_sha256": campaign_hash,
            "effectiveness": {
                "source_plan": base.relative_to(root).as_posix(),
                "source_plan_sha256": source_sha,
                "resource": resource,
                "pressure_requested": pressure,
                "pressure_applied": applied,
                "cpu_workers": cpu_workers,
            },
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PLAN_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    with sidecar_combinations.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(stable_json_dumps(combinations, indent=2) + "\n")
    stage_counts = {
        stage: sum(row["stage"] == stage for row in rows)
        for stage in ("colocation-main", "colocation-extra-test")
    }
    manifest = {
        "schema_version": 2,
        "status": "completed",
        "experiment_id": experiment_id,
        "plan_file": output.relative_to(root).as_posix(),
        "plan_sha256": _file_sha256(output),
        "combination_manifest": sidecar_combinations.relative_to(root).as_posix(),
        "combination_manifest_sha256": _file_sha256(sidecar_combinations),
        "row_count": len(rows),
        "stage_counts": stage_counts,
        "source_plan": base.relative_to(root).as_posix(),
        "source_plan_sha256": source_sha,
        "config_sha256": campaign_hash,
        "resource": resource,
        "pressure_requested": pressure,
        "pressure_applied": applied,
        "benchmark_cpu_workers": cpu_workers,
        "root_commit": root_commit,
        "root_dirty_at_generation": root_dirty,
    }
    with sidecar_manifest.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(stable_json_dumps(manifest, indent=2) + "\n")
    return manifest


def audit_stress_pilot(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    qos_ratio: float,
    min_completed_runs: int,
    min_positive_targets: int,
    min_negative_targets: int,
    expected_benchmark_cpu_workers: int,
    output: Path | None = None,
) -> dict[str, Any]:
    """只审计已完成的压力 pilot；没有正负标签时明确失败，不启动全量建模。"""

    if not 0.0 < qos_ratio <= 1.0:
        raise EffectivenessError("qos_ratio 必须位于 (0, 1]")
    root = repo_root.resolve()
    plan = plan_file.resolve()
    audit = audit_colocation_inputs(
        repo_root=root,
        plan_file=plan,
        solo_baselines_file=solo_baselines_file,
        allow_pressure=True,
    )
    main_rows, extra_rows = _colocation_rows(plan, allow_pressure=True)
    all_rows = (*main_rows, *extra_rows)
    baselines, _ = _load_baselines(
        baseline_file=solo_baselines_file.resolve(),
        expected_workload_ids={workload for row in all_rows for workload in row.workload_ids},
    )
    manifest = json.loads(plan.with_name(f"{plan.stem}-manifest.json").read_text(encoding="utf-8"))
    completed_rows = []
    pending_rows = []
    targets: list[dict[str, Any]] = []
    for row in all_rows:
        decision = inspect_resume(repo_root=root, row=row)
        if decision.get("action") != "skip":
            pending_rows.append(row)
            continue
        physical, row_targets = _collect_run_record(
            repo_root=root,
            row=row,
            plan_sha256=str(audit["plan_sha256"]),
            baselines=baselines,
            allow_pressure=True,
            expected_benchmark_cpu_workers=expected_benchmark_cpu_workers,
        )
        completed_rows.append(physical)
        targets.extend(row_targets)
    positive = [item for item in targets if float(item["retention_ratio"]) >= qos_ratio]
    negative = [item for item in targets if float(item["retention_ratio"]) < qos_ratio]
    retention = [float(item["retention_ratio"]) for item in targets]
    plan_pressure_cells = audit.get("pressure_cells", [])
    manifest_pressure_cell = {
        "resource": manifest.get("resource"),
        "pressure_requested": manifest.get("pressure_requested"),
        "pressure_applied": manifest.get("pressure_applied"),
    }
    pressure_metadata_matches = False
    if len(plan_pressure_cells) == 1:
        planned = plan_pressure_cells[0]
        try:
            pressure_metadata_matches = (
                planned.get("resource") == manifest_pressure_cell["resource"]
                and abs(float(planned.get("pressure_requested")) - float(manifest_pressure_cell["pressure_requested"])) <= 1e-9
                and abs(float(planned.get("pressure_applied")) - float(manifest_pressure_cell["pressure_applied"])) <= 1e-9
            )
        except (TypeError, ValueError):
            pressure_metadata_matches = False
    checks = {
        "plan_pressure_contract_passed": audit["status"] == "passed",
        "manifest_pressure_matches_plan": pressure_metadata_matches,
        "pilot_completed_run_count": len(completed_rows) >= min_completed_runs,
        "stress_benchmark_cpu_workers": int(manifest.get("benchmark_cpu_workers", 0)) == expected_benchmark_cpu_workers,
        "retention_values_finite": bool(retention) and all(math.isfinite(value) and value > 0 for value in retention),
        "positive_label_minimum": len(positive) >= min_positive_targets,
        "negative_label_minimum": len(negative) >= min_negative_targets,
        "non_degenerate_qos_labels": bool(positive) and bool(negative),
    }
    result = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "experiment_id": manifest.get("experiment_id"),
        "resource": manifest.get("resource"),
        "pressure_requested": manifest.get("pressure_requested"),
        "pressure_applied": manifest.get("pressure_applied"),
        "benchmark_cpu_workers": expected_benchmark_cpu_workers,
        "qos_ratio": qos_ratio,
        "completed_run_count": len(completed_rows),
        "pending_run_count": len(pending_rows),
        "target_count": len(targets),
        "positive_target_count": len(positive),
        "negative_target_count": len(negative),
        "retention_min": min(retention) if retention else None,
        "retention_max": max(retention) if retention else None,
        "retention_mean": sum(retention) / len(retention) if retention else None,
        "checks": checks,
        "plan_sha256": audit["plan_sha256"],
    }
    if output is not None:
        output = output.resolve()
        if output.exists():
            raise FileExistsError(f"压力 pilot 验收输出已存在，拒绝覆盖: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(stable_json_dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def audit_high_fps_pilot(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    qos_ratio: float,
    min_completed_runs: int,
    min_positive_targets: int,
    min_negative_targets: int,
    expected_fps_multiplier: float,
    expected_cpu_affinity: tuple[int, ...] | None = None,
    output: Path | None = None,
) -> dict[str, Any]:
    """审计真实游戏高帧率共置 pilot，拒绝外部 benchmark 混入。"""

    if not 0.0 < qos_ratio <= 1.0:
        raise EffectivenessError("qos_ratio 必须位于 (0, 1]")
    if not math.isfinite(expected_fps_multiplier) or not 1.0 < expected_fps_multiplier <= 16.0:
        raise EffectivenessError("expected_fps_multiplier 必须位于 (1, 16]")
    root = repo_root.resolve()
    plan = plan_file.resolve()
    audit = audit_colocation_inputs(
        repo_root=root,
        plan_file=plan,
        solo_baselines_file=solo_baselines_file,
    )
    main_rows, extra_rows = _colocation_rows(plan)
    all_rows = (*main_rows, *extra_rows)
    baselines, _ = _load_baselines(
        baseline_file=solo_baselines_file.resolve(),
        expected_workload_ids={workload for row in all_rows for workload in row.workload_ids},
    )
    baseline_multiplier_matches = all(
        abs(float(item.get("fps_multiplier", 1.0)) - expected_fps_multiplier) <= 1e-9
        for item in baselines.values()
    )
    baseline_affinity_matches = expected_cpu_affinity is None or all(
        tuple(item.get("cpu_affinity") or ()) == expected_cpu_affinity
        for item in baselines.values()
    )
    manifest = json.loads(plan.with_name(f"{plan.stem}-manifest.json").read_text(encoding="utf-8"))
    completed_rows = []
    pending_rows = []
    targets: list[dict[str, Any]] = []
    for row in all_rows:
        decision = inspect_resume(repo_root=root, row=row)
        if decision.get("action") != "skip":
            pending_rows.append(row)
            continue
        physical, row_targets = _collect_run_record(
            repo_root=root,
            row=row,
            plan_sha256=str(audit["plan_sha256"]),
            baselines=baselines,
            expected_workload_fps_multiplier=expected_fps_multiplier,
            expected_workload_cpu_affinity=expected_cpu_affinity,
        )
        completed_rows.append(physical)
        targets.extend(row_targets)
    positive = [item for item in targets if float(item["retention_ratio"]) >= qos_ratio]
    negative = [item for item in targets if float(item["retention_ratio"]) < qos_ratio]
    retention = [float(item["retention_ratio"]) for item in targets]
    try:
        manifest_multiplier_matches = (
            abs(float(manifest.get("workload_fps_multiplier")) - expected_fps_multiplier)
            <= 1e-9
        )
    except (TypeError, ValueError):
        manifest_multiplier_matches = False
    manifest_affinity_matches = (
        expected_cpu_affinity is None
        or tuple(manifest.get("workload_cpu_affinity") or ())
        == expected_cpu_affinity
    )
    checks = {
        "plan_without_external_benchmark": audit["status"] == "passed",
        "manifest_fps_multiplier_matches": manifest_multiplier_matches,
        "manifest_cpu_affinity_matches": manifest_affinity_matches,
        "baseline_fps_multiplier_matches": baseline_multiplier_matches,
        "baseline_cpu_affinity_matches": baseline_affinity_matches,
        "pilot_completed_run_count": len(completed_rows) >= min_completed_runs,
        "retention_values_finite": bool(retention)
        and all(math.isfinite(value) and value > 0 for value in retention),
        "positive_label_minimum": len(positive) >= min_positive_targets,
        "negative_label_minimum": len(negative) >= min_negative_targets,
        "non_degenerate_qos_labels": bool(positive) and bool(negative),
    }
    result = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "experiment_id": manifest.get("experiment_id"),
        "workload_fps_multiplier": expected_fps_multiplier,
        "workload_cpu_affinity": list(expected_cpu_affinity)
        if expected_cpu_affinity is not None
        else None,
        "qos_ratio": qos_ratio,
        "completed_run_count": len(completed_rows),
        "pending_run_count": len(pending_rows),
        "target_count": len(targets),
        "positive_target_count": len(positive),
        "negative_target_count": len(negative),
        "retention_min": min(retention) if retention else None,
        "retention_max": max(retention) if retention else None,
        "retention_mean": sum(retention) / len(retention) if retention else None,
        "checks": checks,
        "plan_sha256": audit["plan_sha256"],
    }
    if output is not None:
        output = output.resolve()
        if output.exists():
            raise FileExistsError(f"高帧率 pilot 验收输出已存在，拒绝覆盖: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(stable_json_dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
