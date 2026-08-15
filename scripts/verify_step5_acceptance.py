"""独立复核 Step 5 正式计划、四窗口 run、哈希与 resume 证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# 以脚本路径直接执行时，Python 默认只加入 scripts/；显式加入仓库根目录。
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psutil

from gaugur_lite.config import discover_repo_root, stable_json_dumps
from gaugur_lite.metrics.writer import write_json_atomic
from gaugur_lite.runner.plan import load_plan_rows, verify_plan


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(repo_root: Path, artifact_root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, actual: Any, expected: Any) -> None:
        checks.append(
            {"name": name, "passed": bool(passed), "actual": actual, "expected": expected}
        )

    formal_plan = artifact_root / "formal-plan.csv"
    formal_verified = verify_plan(repo_root=repo_root, plan_file=formal_plan)
    formal_rows = load_plan_rows(formal_plan)
    formal_manifest = _read_json(artifact_root / "formal-plan-manifest.json")
    combinations = _read_json(artifact_root / "formal-plan-combinations.json")
    add("formal_plan_verify", formal_verified["status"] == "passed", formal_verified["status"], "passed")
    add("formal_plan_row_count", len(formal_rows) == 720, len(formal_rows), 720)
    add(
        "formal_stage_counts",
        formal_manifest.get("stage_counts")
        == {
            "solo": 24,
            "profile": 480,
            "colocation-main": 180,
            "colocation-extra-test": 36,
        },
        formal_manifest.get("stage_counts"),
        {"solo": 24, "profile": 480, "colocation-main": 180, "colocation-extra-test": 36},
    )
    main = combinations["main"]
    extra = combinations["extra_test"]
    add("main_combinations", (main["pair_count"], main["triple_count"], main["total_count"]) == (28, 32, 60), [main["pair_count"], main["triple_count"], main["total_count"]], [28, 32, 60])
    add(
        "triple_balance",
        set(main["triple_selection"]["workload_occurrences"].values()) == {12},
        main["triple_selection"]["workload_occurrences"],
        "each workload occurs 12 times",
    )
    add(
        "main_split_counts",
        main["split"]["counts"] == {"train": 36, "validation": 12, "test": 12},
        main["split"]["counts"],
        {"train": 36, "validation": 12, "test": 12},
    )
    add("extra_combinations", extra["count"] == 12, extra["count"], 12)
    add(
        "extra_balance",
        set(extra["workload_occurrences"].values()) == {6}
        and set(extra["pair_cooccurrence"].values()).issubset({2, 3}),
        {
            "workloads": extra["workload_occurrences"],
            "pair_count_values": sorted(set(extra["pair_cooccurrence"].values())),
        },
        "workload=6 and pair in {2,3}",
    )
    add("combination_disjoint", all(combinations["checks"].values()), combinations["checks"], True)

    acceptance_plan = artifact_root / "quad-plan.csv"
    acceptance_verified = verify_plan(repo_root=repo_root, plan_file=acceptance_plan)
    add("quad_plan_verify", acceptance_verified["status"] == "passed", acceptance_verified["status"], "passed")
    add("quad_plan_row_count", acceptance_verified["row_count"] == 1, acceptance_verified["row_count"], 1)

    first_report = _read_json(artifact_root / "first-run.json")
    recovery_path = artifact_root / "recovery-run.json"
    recovery_report = _read_json(recovery_path) if recovery_path.is_file() else None
    resume_report = _read_json(artifact_root / "resume-run.json")
    if recovery_report is None:
        execution_report = first_report
        expected_attempt_count = 1
        add(
            "first_run_completed",
            first_report.get("status") == "passed"
            and first_report.get("completed") == 1
            and first_report.get("failed_or_invalid") == 0,
            {
                key: first_report.get(key)
                for key in ("status", "completed", "failed_or_invalid")
            },
            {"status": "passed", "completed": 1, "failed_or_invalid": 0},
        )
    else:
        execution_report = recovery_report
        expected_attempt_count = 2
        first_result = first_report.get("results", [{}])[0]
        add(
            "failed_a001_preserved",
            first_report.get("status") == "failed"
            and first_report.get("failed_or_invalid") == 1
            and first_report.get("global_kill_used") is False
            and first_result.get("attempt") == 1
            and first_result.get("status") == "failed"
            and str(first_result.get("directory", "")).endswith("/attempts/a001"),
            {
                "report_status": first_report.get("status"),
                "failed_or_invalid": first_report.get("failed_or_invalid"),
                "attempt": first_result.get("attempt"),
                "attempt_status": first_result.get("status"),
                "directory": first_result.get("directory"),
            },
            "failed a001 preserved without global kill",
        )
        add(
            "recovery_run_completed",
            recovery_report.get("status") == "passed"
            and recovery_report.get("completed") == 1
            and recovery_report.get("failed_or_invalid") == 0
            and recovery_report.get("results", [{}])[0].get("attempt") == 2,
            {
                "status": recovery_report.get("status"),
                "completed": recovery_report.get("completed"),
                "failed_or_invalid": recovery_report.get("failed_or_invalid"),
                "attempt": recovery_report.get("results", [{}])[0].get("attempt"),
            },
            {"status": "passed", "completed": 1, "failed_or_invalid": 0, "attempt": 2},
        )
    add(
        "resume_skipped_without_rerun",
        resume_report.get("status") == "passed"
        and resume_report.get("skipped") == 1
        and resume_report.get("completed") == 0,
        {key: resume_report.get(key) for key in ("status", "completed", "skipped")},
        {"status": "passed", "completed": 0, "skipped": 1},
    )

    result_row = execution_report["results"][0]
    attempt_dir = (repo_root / result_row["directory"]).resolve()
    if repo_root.resolve() not in attempt_dir.parents:
        raise ValueError("first-run directory 逃逸仓库")
    run_root = attempt_dir.parent.parent
    index = _read_json(run_root / "index.json")
    add(
        "attempt_history_after_resume",
        len(index["attempts"]) == expected_attempt_count,
        len(index["attempts"]),
        expected_attempt_count,
    )
    if recovery_report is not None:
        failure_path = run_root / "attempts" / "a001" / "failure.json"
        add(
            "failed_attempt_artifact_preserved",
            failure_path.is_file()
            and [item.get("status") for item in index["attempts"]] == ["failed", "completed"],
            {
                "failure_json_exists": failure_path.is_file(),
                "statuses": [item.get("status") for item in index["attempts"]],
            },
            {"failure_json_exists": True, "statuses": ["failed", "completed"]},
        )

    status = _read_json(attempt_dir / "status.json")
    summary = _read_json(attempt_dir / "summary.json")
    lifecycle = _read_jsonl(attempt_dir / "lifecycle.jsonl")
    system_rows = _read_jsonl(attempt_dir / "system_metrics.jsonl")
    window_rows = _read_jsonl(attempt_dir / "window_observations.jsonl")
    phases = [item["phase"] for item in lifecycle]
    expected_phases = [
        "PREPARING",
        "STARTING",
        "READY",
        "WARMUP",
        "MEASURING",
        "STOPPING",
        "COOLDOWN",
        "COMPLETED",
    ]
    add("lifecycle_order", phases == expected_phases, phases, expected_phases)
    add(
        "final_status",
        status.get("status") == "completed" and status.get("valid") is True,
        {"status": status.get("status"), "valid": status.get("valid")},
        {"status": "completed", "valid": True},
    )
    add("four_workloads", len(summary["workloads"]) == 4, len(summary["workloads"]), 4)
    add(
        "workload_barrier_and_coverage",
        all(
            item.get("barrier_used") is True
            and float(item.get("measurement_coverage_ratio", 0.0)) >= 0.95
            and item.get("status") == "completed"
            for item in summary["workloads"]
        ),
        [
            {
                "id": item["workload_id"],
                "barrier": item.get("barrier_used"),
                "coverage": item.get("measurement_coverage_ratio"),
                "status": item.get("status"),
            }
            for item in summary["workloads"]
        ],
        "all barrier=true, coverage>=0.95, completed",
    )
    starts = [int(item["measurement_started_monotonic_ns"]) for item in summary["workloads"]]
    start_skew_ms = (max(starts) - min(starts)) / 1_000_000
    add(
        "workload_overlap",
        float(summary["workload_overlap_ratio"]) >= 0.95,
        {"ratio": summary["workload_overlap_ratio"], "start_skew_ms": start_skew_ms},
        "ratio>=0.95",
    )
    add("system_samples", len(system_rows) == 9, len(system_rows), 9)
    add(
        "system_coverage",
        float(summary["system_coverage_ratio"]) >= 0.95,
        summary["system_coverage_ratio"],
        ">=0.95",
    )
    add(
        "temperature_gate",
        summary.get("gpu_temp_c_max") is not None
        and float(summary["gpu_temp_c_max"]) <= float(summary["gpu_temp_limit_c"]),
        {"max": summary.get("gpu_temp_c_max"), "limit": summary.get("gpu_temp_limit_c")},
        "max<=limit",
    )
    add("window_sample_count", len(window_rows) == 9, len(window_rows), 9)
    add(
        "four_visible_nonoverlapping_windows",
        all(
            item.get("healthy") is True
            and not item.get("overlaps")
            and len(item.get("observations", [])) == 4
            and all(
                observation.get("found") is True
                and observation.get("process_pid") is not None
                and observation.get("visible") is True
                and observation.get("minimized") is False
                for observation in item["observations"]
            )
            for item in window_rows
        ),
        {"samples": len(window_rows), "healthy": sum(bool(item.get("healthy")) for item in window_rows)},
        {"samples": 9, "healthy": 9},
    )
    add(
        "external_occlusion_limitation_recorded",
        summary.get("external_occlusion_checked") is False,
        summary.get("external_occlusion_checked"),
        False,
    )
    nonempty_stderr = [
        path.relative_to(attempt_dir).as_posix()
        for path in (attempt_dir / "workloads").glob("*/stderr.log")
        if path.stat().st_size > 0
    ]
    add("workload_stderr_empty", not nonempty_stderr, nonempty_stderr, [])
    bad_hashes = []
    for relative, expected in summary["artifact_sha256"].items():
        path = attempt_dir / relative
        if not path.is_file() or _sha256(path) != expected:
            bad_hashes.append(relative)
    add("artifact_sha256", not bad_hashes, bad_hashes, [])
    live_pids = [pid for pid in status.get("child_pids", []) if psutil.pid_exists(int(pid))]
    add("no_managed_child_alive", not live_pids, live_pids, [])
    add(
        "no_global_kill",
        first_report.get("global_kill_used") is False
        and execution_report.get("global_kill_used") is False
        and summary.get("cleanup", {}).get("global_kill_used") is False,
        {
            "first_report": first_report.get("global_kill_used"),
            "execution_report": execution_report.get("global_kill_used"),
            "summary": summary.get("cleanup", {}).get("global_kill_used"),
        },
        False,
    )

    return {
        "schema_version": 1,
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "check_count": len(checks),
        "passed_count": sum(item["passed"] for item in checks),
        "formal_plan_sha256": formal_verified["plan_sha256"],
        "quad_plan_sha256": acceptance_verified["plan_sha256"],
        "attempt_directory": attempt_dir.relative_to(repo_root).as_posix(),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("artifacts/runner/step5"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/runner/step5/formal-acceptance-verification.json"),
    )
    args = parser.parse_args()
    repo_root = discover_repo_root(Path.cwd())
    artifact_root = args.artifact_root.resolve()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"验证输出已存在，拒绝覆盖: {output}")
    result = verify(repo_root, artifact_root)
    write_json_atomic(output, result)
    print(stable_json_dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
