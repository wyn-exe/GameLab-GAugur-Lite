"""从正式 solo attempts 构建可追溯的 FPS 基线与重复稳定性报告。"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import config_sha256, stable_json_dumps
from .runner.plan import load_plan_rows, verify_plan
from .runner.runner import ParsedPlanRow, inspect_resume


class BaselineError(RuntimeError):
    """正式基线缺失、混用 provenance 或未通过质量门。"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repo_path(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve()
    candidate = path.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"路径必须位于仓库内且不能等于仓库根目录: {path}")
    return candidate


def _relative(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _solo_plan_rows(plan_file: Path) -> list[ParsedPlanRow]:
    rows = [
        ParsedPlanRow.from_csv(raw)
        for raw in load_plan_rows(plan_file)
        if raw["stage"] == "solo"
    ]
    if not rows:
        raise BaselineError("计划中没有 solo 行")
    for row in rows:
        neighbors = json.loads(row.raw["neighbor_ids"])
        if (
            row.mode != "solo"
            or len(row.workload_ids) != 1
            or row.raw["target_id"] != row.workload_ids[0]
            or neighbors != []
            or row.resource is not None
            or row.pressure_requested is not None
        ):
            raise BaselineError(f"solo 计划行包含邻居或压力: {row.run_id}")
    return rows


def _collect_run_record(
    *, repo_root: Path, row: ParsedPlanRow, plan_sha256: str
) -> dict[str, Any]:
    decision = inspect_resume(repo_root=repo_root, row=row)
    if decision.get("action") != "skip":
        raise BaselineError(
            f"solo run 尚无完整有效 attempt: {row.run_id} ({decision.get('reason')})"
        )
    attempt_dir = _repo_path(repo_root, repo_root / str(decision["directory"]))
    manifest = _read_json(attempt_dir / "manifest.json")
    summary = _read_json(attempt_dir / "summary.json")
    workload_id = row.workload_ids[0]
    if manifest.get("plan_sha256") != plan_sha256:
        raise BaselineError(f"attempt plan SHA-256 不匹配: {row.run_id}")
    provenance = manifest.get("execution_provenance")
    if not isinstance(provenance, dict) or not provenance.get("source_tree_sha256"):
        raise BaselineError(f"attempt 缺少 execution provenance: {row.run_id}")
    if (
        summary.get("status") != "completed"
        or summary.get("valid") is not True
        or summary.get("mode") != "solo"
        or summary.get("benchmark") is not None
        or summary.get("workload_ids") != [workload_id]
        or (attempt_dir / "benchmark").exists()
    ):
        raise BaselineError(f"solo attempt 混入 benchmark/邻居或状态非法: {row.run_id}")
    workloads = summary.get("workloads", [])
    if len(workloads) != 1 or workloads[0].get("workload_id") != workload_id:
        raise BaselineError(f"solo workload summary 非唯一: {row.run_id}")
    workload = workloads[0]
    fps = workload.get("game_fps", {})
    required_fps = ("mean", "p05", "min")
    if any(fps.get(key) is None or not math.isfinite(float(fps[key])) for key in required_fps):
        raise BaselineError(f"solo FPS 字段缺失或非有限: {row.run_id}")
    if any(float(fps[key]) <= 0 for key in required_fps):
        raise BaselineError(f"solo FPS 必须为正数: {row.run_id}")
    return {
        "schema_version": 1,
        "experiment_id": row.experiment_id,
        "workload_id": workload_id,
        "repeat": row.repeat,
        "run_id": row.run_id,
        "run_seed": int(row.raw["seed"]),
        "row_sha256": row.row_sha256,
        "config_sha256": row.config_sha256,
        "attempt": int(decision["attempt"]),
        "attempt_directory": _relative(repo_root, attempt_dir),
        "summary_sha256": _file_sha256(attempt_dir / "summary.json"),
        "game_metrics_sha256": summary["artifact_sha256"][
            f"workloads/{workload_id}/game_metrics.jsonl"
        ],
        "execution_root_commit": provenance.get("root_commit"),
        "execution_root_dirty": provenance.get("root_dirty_at_execution"),
        "execution_source_tree_sha256": provenance["source_tree_sha256"],
        "target_fps": int(workload["target_fps"]),
        "registry_target_fps": int(
            workload.get("registry_target_fps", workload["target_fps"])
        ),
        "fps_multiplier": float(workload.get("fps_multiplier", 1.0)),
        "cpu_affinity": workload.get("cpu_affinity"),
        "mean_fps": float(fps["mean"]),
        "p05_fps": float(fps["p05"]),
        "min_fps": float(fps["min"]),
        "fps_windows_used": int(fps["windows_used"]),
        "measurement_coverage_ratio": float(workload["measurement_coverage_ratio"]),
        "system_coverage_ratio": float(summary["system_coverage_ratio"]),
        "system_sample_count": int(summary["system_sample_count"]),
        "window_sample_count": int(summary["window_sample_count"]),
        "gpu_temp_c_max": summary.get("gpu_temp_c_max"),
        "missed_deadline_count": int(workload["missed_deadline_count"]),
    }


def compute_solo_baselines(
    *,
    repo_root: Path,
    plan_file: Path,
    fps_cv_threshold_pct: float = 5.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """只读重算 run-level 记录和 workload baseline。"""

    if not math.isfinite(fps_cv_threshold_pct) or fps_cv_threshold_pct <= 0:
        raise ValueError("fps_cv_threshold_pct 必须是正有限数")
    verification = verify_plan(repo_root=repo_root, plan_file=plan_file)
    if verification["status"] != "passed":
        raise BaselineError("正式计划未通过 verify")
    plan_manifest = _read_json(
        plan_file.with_name(f"{plan_file.stem}-manifest.json")
    )
    rows = _solo_plan_rows(plan_file)
    records = [
        _collect_run_record(
            repo_root=repo_root,
            row=row,
            plan_sha256=verification["plan_sha256"],
        )
        for row in rows
    ]
    records.sort(key=lambda item: (item["workload_id"], item["repeat"]))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["workload_id"]].append(record)

    repeat_counts = {workload: len(items) for workload, items in grouped.items()}
    repeats = sorted({int(item["repeat"]) for item in records})
    if len(repeats) < 3 or any(count != len(repeats) for count in repeat_counts.values()):
        raise BaselineError(f"每个 workload 必须有同样且至少 3 个重复: {repeat_counts}")
    for workload, items in grouped.items():
        actual = [int(item["repeat"]) for item in items]
        if actual != repeats:
            raise BaselineError(f"{workload} repeat 编号不连续一致: {actual}")

    source_hashes = sorted({str(item["execution_source_tree_sha256"]) for item in records})
    execution_commits = sorted({str(item["execution_root_commit"]) for item in records})
    config_hashes = sorted({str(item["config_sha256"]) for item in records})
    # 兼容 Step 6 的历史 solo records；旧记录等价于原生倍率 1。
    fps_multipliers = sorted({float(item.get("fps_multiplier", 1.0)) for item in records})
    cpu_affinities = sorted(
        {tuple(item.get("cpu_affinity") or ()) for item in records}, key=str
    )
    baselines = []
    for workload_id, items in sorted(grouped.items()):
        mean_values = [float(item["mean_fps"]) for item in items]
        p05_values = [float(item["p05_fps"]) for item in items]
        mean_fps = statistics.fmean(mean_values)
        sample_std = statistics.stdev(mean_values)
        cv_pct = sample_std / mean_fps * 100
        max_relative_deviation_pct = max(abs(value - mean_fps) / mean_fps * 100 for value in mean_values)
        coverage_ok = all(
            float(item["measurement_coverage_ratio"]) >= 0.95
            and float(item["system_coverage_ratio"]) >= 0.95
            for item in items
        )
        checks = {
            "repeat_count": len(items) == len(repeats) and len(items) >= 3,
            "mean_fps_cv": cv_pct <= fps_cv_threshold_pct,
            "coverage": coverage_ok,
            "target_fps_consistent": len({item["target_fps"] for item in items}) == 1,
            "fps_multiplier_consistent": len({float(item.get("fps_multiplier", 1.0)) for item in items}) == 1,
            "cpu_affinity_consistent": len(
                {tuple(item.get("cpu_affinity") or ()) for item in items}
            )
            == 1,
        }
        baseline_id = config_sha256(
            {
                "plan_sha256": verification["plan_sha256"],
                "config_sha256": items[0]["config_sha256"],
                "workload_id": workload_id,
                "runs": [
                    {
                        "run_id": item["run_id"],
                        "summary_sha256": item["summary_sha256"],
                    }
                    for item in items
                ],
            }
        )
        baselines.append(
            {
                "workload_id": workload_id,
                "baseline_id": baseline_id,
                "target_fps": items[0]["target_fps"],
                "registry_target_fps": int(
                    items[0].get("registry_target_fps", items[0]["target_fps"])
                ),
                "fps_multiplier": float(items[0].get("fps_multiplier", 1.0)),
                "cpu_affinity": items[0].get("cpu_affinity"),
                "repeat_count": len(items),
                "repeats": [item["repeat"] for item in items],
                "run_ids": [item["run_id"] for item in items],
                "run_mean_fps": mean_values,
                "run_p05_fps": p05_values,
                "mean_fps": mean_fps,
                "p05_fps": statistics.fmean(p05_values),
                "min_fps": min(float(item["min_fps"]) for item in items),
                "mean_fps_sample_std": sample_std,
                "mean_fps_cv_pct": cv_pct,
                "mean_fps_max_relative_deviation_pct": max_relative_deviation_pct,
                "valid_for_retention": all(checks.values()),
                "checks": checks,
            }
        )

    checks = {
        "plan_verified": True,
        "plan_generated_from_clean_commit": plan_manifest.get("root_dirty_at_generation") is False,
        "solo_run_count": len(records) == len(grouped) * len(repeats),
        "workload_count": len(grouped) == 8,
        "repeat_count_per_workload": set(repeat_counts.values()) == {len(repeats)},
        "no_neighbor_or_benchmark": True,
        "single_config_sha256": len(config_hashes) == 1,
        "single_execution_source_tree": len(source_hashes) == 1,
        "single_execution_root_commit": len(execution_commits) == 1,
        "single_fps_multiplier": len(fps_multipliers) == 1,
        "single_cpu_affinity": len(cpu_affinities) == 1,
        "all_baselines_stable": all(item["valid_for_retention"] for item in baselines),
    }
    result = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "experiment_id": rows[0].experiment_id,
        "definition": {
            "mean_fps": "arithmetic mean of per-run mean FPS",
            "p05_fps": "arithmetic mean of per-run p05 FPS",
            "min_fps": "minimum per-window FPS across all valid repeats",
            "cv": "sample standard deviation of per-run mean FPS divided by their mean",
        },
        "quality_thresholds": {
            "minimum_repeats": 3,
            "fps_cv_max_pct": fps_cv_threshold_pct,
            "measurement_coverage_min": 0.95,
            "system_coverage_min": 0.95,
        },
        "plan": {
            "path": _relative(repo_root, plan_file),
            "sha256": verification["plan_sha256"],
            "root_commit": plan_manifest.get("root_commit"),
            "root_dirty_at_generation": plan_manifest.get("root_dirty_at_generation"),
            "config_sha256": plan_manifest.get("config_sha256"),
        },
        "execution": {
            "root_commits": execution_commits,
            "source_tree_sha256s": source_hashes,
            "dirty_values": sorted(
                {str(item["execution_root_dirty"]) for item in records}
            ),
            "fps_multipliers": fps_multipliers,
            "cpu_affinities": [list(value) for value in cpu_affinities],
        },
        "run_count": len(records),
        "workload_count": len(grouped),
        "repeats": repeats,
        "repeat_counts": dict(sorted(repeat_counts.items())),
        "baselines": baselines,
        "checks": checks,
    }
    if result["status"] != "passed":
        failed = [name for name, passed in checks.items() if not passed]
        raise BaselineError("solo baseline 质量门失败: " + ", ".join(failed))
    return result, records


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(stable_json_dumps(value, indent=2) + "\n")


def _write_jsonl_exclusive(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(stable_json_dumps(row) + "\n")


def _write_plot_exclusive(path: Path, result: dict[str, Any]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - 正式环境已锁定 matplotlib。
        raise RuntimeError("baseline plot 需要 matplotlib") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(4, 2, figsize=(11, 14), constrained_layout=True)
    for axis, baseline in zip(axes.flat, result["baselines"], strict=True):
        repeats = baseline["repeats"]
        values = baseline["run_mean_fps"]
        axis.plot(repeats, values, marker="o", linewidth=1.5, label="run mean FPS")
        axis.axhline(baseline["mean_fps"], linestyle="--", color="tab:orange", label="baseline mean")
        spread = max(max(values) - min(values), baseline["mean_fps"] * 0.002, 0.02)
        axis.set_ylim(min(values) - spread, max(values) + spread)
        axis.set_xticks(repeats)
        axis.set_title(f"{baseline['workload_id']}  CV={baseline['mean_fps_cv_pct']:.3f}%")
        axis.set_xlabel("repeat")
        axis.set_ylabel("FPS")
        axis.grid(alpha=0.25)
        axis.legend(loc="best")
    with path.open("xb") as stream:
        figure.savefig(stream, format="png", dpi=160)
    plt.close(figure)


def build_solo_baselines(
    *,
    repo_root: Path,
    plan_file: Path,
    output_file: Path,
    runs_output_file: Path,
    plot_file: Path,
    fps_cv_threshold_pct: float = 5.0,
) -> dict[str, Any]:
    """独占写出 run-level JSONL、baseline JSON 和重复稳定性图。"""

    output = _repo_path(repo_root, output_file)
    runs_output = _repo_path(repo_root, runs_output_file)
    plot = _repo_path(repo_root, plot_file)
    existing = [path for path in (output, runs_output, plot) if path.exists()]
    if existing:
        raise FileExistsError("baseline 产物已存在，拒绝覆盖: " + ", ".join(map(str, existing)))
    result, records = compute_solo_baselines(
        repo_root=repo_root,
        plan_file=plan_file,
        fps_cv_threshold_pct=fps_cv_threshold_pct,
    )
    _write_jsonl_exclusive(runs_output, records)
    _write_plot_exclusive(plot, result)
    result["artifacts"] = {
        "runs": _relative(repo_root, runs_output),
        "runs_sha256": _file_sha256(runs_output),
        "plot": _relative(repo_root, plot),
        "plot_sha256": _file_sha256(plot),
    }
    _write_json_exclusive(output, result)
    return result


def verify_solo_baselines(
    *,
    repo_root: Path,
    plan_file: Path,
    summary_file: Path,
    runs_file: Path,
    plot_file: Path,
) -> dict[str, Any]:
    """从原始 attempt 重算并与落盘 baseline、JSONL、PNG 哈希交叉核对。"""

    stored = _read_json(summary_file)
    recomputed, records = compute_solo_baselines(
        repo_root=repo_root,
        plan_file=plan_file,
        fps_cv_threshold_pct=float(stored["quality_thresholds"]["fps_cv_max_pct"]),
    )
    artifacts = stored.get("artifacts", {})
    stored_core = dict(stored)
    stored_core.pop("artifacts", None)
    disk_records = [
        json.loads(line)
        for line in runs_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    checks = [
        {
            "name": "summary_recomputed_exactly",
            "passed": stored_core == recomputed,
            "actual": config_sha256(stored_core),
            "expected": config_sha256(recomputed),
        },
        {
            "name": "run_records_recomputed_exactly",
            "passed": disk_records == records,
            "actual": len(disk_records),
            "expected": len(records),
        },
        {
            "name": "runs_sha256",
            "passed": _file_sha256(runs_file) == artifacts.get("runs_sha256"),
            "actual": _file_sha256(runs_file),
            "expected": artifacts.get("runs_sha256"),
        },
        {
            "name": "plot_sha256",
            "passed": _file_sha256(plot_file) == artifacts.get("plot_sha256"),
            "actual": _file_sha256(plot_file),
            "expected": artifacts.get("plot_sha256"),
        },
        {
            "name": "plot_png_signature",
            "passed": plot_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
            "actual": plot_file.suffix.lower(),
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
        "plan_sha256": recomputed["plan"]["sha256"],
        "summary_sha256": _file_sha256(summary_file),
        "runs_sha256": _file_sha256(runs_file),
        "plot_sha256": _file_sha256(plot_file),
        "checks": checks,
    }
