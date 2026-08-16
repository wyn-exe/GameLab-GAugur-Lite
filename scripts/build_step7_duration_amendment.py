"""构建 Step 7/8 的 10/30/10 短时序协议修订证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

from gaugur_lite.config import discover_repo_root, stable_json_dumps
from gaugur_lite.profiles import _verify_short_profile_amendment
from gaugur_lite.runner.plan import load_plan_rows, verify_plan


ORIGINAL_PROTOCOL = (20.0, 60.0, 20.0, 82.0)
SHORT_PROTOCOL = (10.0, 30.0, 10.0, 84.0)
EXPECTED_T84_INVALID_REASON = "RunInvalidError:gpu_temperature_exceeded:85.0>84.0"
ALLOWED_CHANGED_COLUMNS = {
    "execution_index",
    "warmup_s",
    "duration_s",
    "cooldown_s",
    "max_gpu_temp_c",
    "config_sha256",
    "root_commit",
    "run_directory",
    "row_sha256",
}
SHORT_PLAN_PATH = "artifacts/plans/formal-v1-remaining-s30.csv"
ALL_STAGE_COUNTS = {
    "solo": 24,
    "profile": 480,
    "colocation-main": 180,
    "colocation-extra-test": 36,
}
REMAINING_STAGE_COUNTS = {
    stage: count for stage, count in ALL_STAGE_COUNTS.items() if stage != "solo"
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _protocol(row: dict[str, str]) -> tuple[float, float, float, float]:
    return (
        float(row["warmup_s"]),
        float(row["duration_s"]),
        float(row["cooldown_s"]),
        float(row["max_gpu_temp_c"]),
    )


def _verify_stage_plan(
    *, root: Path, original_plan: Path, short_plan: Path, stage: str, expected_count: int
) -> dict[str, Any]:
    original = [row for row in load_plan_rows(original_plan) if row["stage"] == stage]
    amended = [row for row in load_plan_rows(short_plan) if row["stage"] == stage]
    if len(original) != expected_count or len(amended) != expected_count:
        raise RuntimeError(
            f"{stage} 行数不符: original={len(original)}, short={len(amended)}"
        )
    original_by_id = {row["run_id"]: row for row in original}
    amended_by_id = {row["run_id"]: row for row in amended}
    if len(original_by_id) != expected_count or set(original_by_id) != set(amended_by_id):
        raise RuntimeError(f"{stage} 修订前后 run_id 集合不一致")

    changed: set[str] = set()
    for run_id in sorted(original_by_id):
        before = original_by_id[run_id]
        after = amended_by_id[run_id]
        if set(before) != set(after):
            raise RuntimeError(f"{stage} CSV schema 不一致: {run_id}")
        differences = {
            column for column in before if str(before[column]) != str(after[column])
        }
        unexpected = differences - ALLOWED_CHANGED_COLUMNS
        if unexpected:
            raise RuntimeError(
                f"{stage} 修改了未声明字段 {run_id}: {', '.join(sorted(unexpected))}"
            )
        changed.update(differences)

    required = {
        "warmup_s",
        "duration_s",
        "cooldown_s",
        "max_gpu_temp_c",
        "config_sha256",
        "run_directory",
        "row_sha256",
    }
    if not required.issubset(changed):
        raise RuntimeError(f"{stage} 缺少必要修订字段: {sorted(required - changed)}")
    if {_protocol(row) for row in original} != {ORIGINAL_PROTOCOL}:
        raise RuntimeError(f"{stage} 原计划不是 20/60/20 + 82°C")
    if {_protocol(row) for row in amended} != {SHORT_PROTOCOL}:
        raise RuntimeError(f"{stage} 新计划不是 10/30/10 + 84°C")
    if {row["run_directory"] for row in original} & {
        row["run_directory"] for row in amended
    }:
        raise RuntimeError(f"{stage} 新旧 raw 目录重叠")

    verification = verify_plan(repo_root=root, plan_file=short_plan)
    if verification["status"] != "passed" or verification["row_count"] != 720:
        raise RuntimeError("s30 全阶段计划未通过 720-row 独立 plan verify")
    manifest_path = short_plan.with_name(f"{short_plan.stem}-manifest.json")
    combinations_path = short_plan.with_name(f"{short_plan.stem}-combinations.json")
    manifest = _json(manifest_path)
    if (
        manifest.get("root_dirty_at_generation") is not False
        or manifest.get("selected_stage") != "all"
        or int(manifest.get("row_count", 0)) != 720
    ):
        raise RuntimeError("s30 全阶段计划必须来自干净提交并包含 720 行")
    return {
        "stage": stage,
        "path": _rel(root, short_plan),
        "sha256": verification["plan_sha256"],
        "row_count": expected_count,
        "manifest": _rel(root, manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "combinations": _rel(root, combinations_path),
        "combinations_sha256": _sha256(combinations_path),
        "changed_columns": sorted(changed),
        "unmodified_fields_equal": True,
        "raw_directories_disjoint": True,
    }


def _audit_t84_trial(root: Path, t84_plan: Path) -> dict[str, Any]:
    valid_attempts: list[dict[str, Any]] = []
    invalid_attempts: list[dict[str, Any]] = []
    valid_run_ids: set[str] = set()
    invalid_run_ids: set[str] = set()
    max_temperatures: list[float] = []
    root_commits: set[str] = set()
    source_trees: set[str] = set()

    for row in load_plan_rows(t84_plan):
        if row["stage"] != "profile":
            continue
        index_path = root / row["run_directory"] / "index.json"
        if not index_path.is_file():
            continue
        for attempt in _json(index_path).get("attempts", []):
            record = {"run_id": row["run_id"], **attempt}
            if attempt.get("status") == "completed" and attempt.get("valid") is True:
                valid_attempts.append(record)
                valid_run_ids.add(row["run_id"])
                directory = root / str(attempt["directory"])
                summary = _json(directory / "summary.json")
                manifest = _json(directory / "manifest.json")
                max_temperatures.append(float(summary["gpu_temp_c_max"]))
                root_commits.add(str(manifest["execution_provenance"]["root_commit"]))
                source_trees.add(
                    str(manifest["execution_provenance"]["source_tree_sha256"])
                )
            else:
                invalid_attempts.append(record)
                invalid_run_ids.add(row["run_id"])

    if len(valid_run_ids) != 71 or len(valid_attempts) != 71:
        raise RuntimeError(
            f"封存的 t84 trial 必须恰有 71 个有效单元/attempt，实际 "
            f"{len(valid_run_ids)}/{len(valid_attempts)}"
        )
    if len(invalid_attempts) != 3 or any(
        item.get("status") != "invalid"
        or item.get("valid") is not False
        or item.get("reason") != EXPECTED_T84_INVALID_REASON
        for item in invalid_attempts
    ):
        raise RuntimeError("t84 trial 必须保留三次同因 85°C>84°C 的 invalid attempt")
    resolved = sorted(invalid_run_ids & valid_run_ids)
    unresolved = sorted(invalid_run_ids - valid_run_ids)
    if len(resolved) != 2 or len(unresolved) != 1:
        raise RuntimeError("t84 trial 应有两个冷启动恢复单元和一个未恢复单元")
    if max(max_temperatures) > 84 or len(root_commits) != 1 or len(source_trees) != 1:
        raise RuntimeError("t84 有效 attempt 的温度或执行 provenance 不一致")

    reports = sorted(
        (root / "artifacts/profiles/step7/t84/invocations").glob(
            "invocation-*-batch-*-run.json"
        )
    )
    full_batch_elapsed = []
    report_evidence = []
    for report_path in reports:
        report = _json(report_path)
        if (
            int(report.get("selected_runs", 0)) == 24
            and int(report.get("skipped", 0)) == 0
            and int(report.get("completed", 0))
            + int(report.get("failed_or_invalid", 0))
            == 24
        ):
            full_batch_elapsed.append(float(report["elapsed_s"]))
        report_evidence.append(
            {"path": _rel(root, report_path), "sha256": _sha256(report_path)}
        )
    if len(full_batch_elapsed) != 3:
        raise RuntimeError("t84 trial 应保留三个 24-row 首次批次报告")

    return {
        "completed_run_count": len(valid_run_ids),
        "valid_attempt_count": len(valid_attempts),
        "invalid_attempt_count": len(invalid_attempts),
        "invalid_run_ids": sorted(invalid_run_ids),
        "resolved_by_one_cold_retry": resolved,
        "unresolved_run_ids": unresolved,
        "invalid_reason": EXPECTED_T84_INVALID_REASON,
        "max_valid_gpu_temp_c": max(max_temperatures),
        "root_commit": next(iter(root_commits)),
        "source_tree_sha256": next(iter(source_trees)),
        "full_batch_elapsed_s": full_batch_elapsed,
        "median_full_batch_elapsed_s": statistics.median(full_batch_elapsed),
        "included_in_final_profiles": False,
        "reports": report_evidence,
    }


def build_payload(root: Path) -> dict[str, Any]:
    original_plan = root / "artifacts/plans/formal-v1.csv"
    t84_plan = root / "artifacts/plans/formal-v1-profile-t84.csv"
    original_verification = verify_plan(repo_root=root, plan_file=original_plan)
    t84_verification = verify_plan(repo_root=root, plan_file=t84_plan)
    if original_verification["status"] != "passed" or t84_verification["status"] != "passed":
        raise RuntimeError("原计划和 t84 trial 计划都必须通过 verify")

    short_plan = root / SHORT_PLAN_PATH
    short_verification = verify_plan(repo_root=root, plan_file=short_plan)
    short_stages = []
    for stage, count in ALL_STAGE_COUNTS.items():
        short_stages.append(
            _verify_stage_plan(
                root=root,
                original_plan=original_plan,
                short_plan=short_plan,
                stage=stage,
                expected_count=count,
            )
        )
    compatibility = _verify_short_profile_amendment(
        repo_root=root,
        profile_plan_file=short_plan,
        baseline_plan_file=original_plan,
        profile_plan_sha256=short_verification["plan_sha256"],
        baseline_plan_sha256=original_verification["plan_sha256"],
    )
    trial = _audit_t84_trial(root, t84_plan)

    remaining_rows = sum(REMAINING_STAGE_COUNTS.values())
    short_directory_sets = [
        {
            row["run_directory"]
            for row in load_plan_rows(short_plan)
            if row["stage"] == stage
        }
        for stage in ALL_STAGE_COUNTS
    ]
    t84_directories = {
        row["run_directory"]
        for row in load_plan_rows(t84_plan)
        if row["stage"] == "profile"
    }
    short_directories = set().union(*short_directory_sets)
    old_nominal_s = sum(ORIGINAL_PROTOCOL[:3])
    short_nominal_s = sum(SHORT_PROTOCOL[:3])
    return {
        "schema_version": 1,
        "status": "passed",
        "amendment_id": "step7-step8-short-timing-s30-v2",
        "scope": ["profile", "colocation-main", "colocation-extra-test"],
        "decision": {
            "preserve_workloads": 8,
            "reuse_existing_solo_runs": 24,
            "execute_short_plan_solo_rows": False,
            "preserve_profile_rows": 480,
            "preserve_profile_repeats": 3,
            "preserve_main_combination_groups": 60,
            "preserve_main_runs": 180,
            "preserve_extra_test_groups": 12,
            "preserve_extra_test_runs": 36,
            "original_protocol": {
                "warmup_s": ORIGINAL_PROTOCOL[0],
                "duration_s": ORIGINAL_PROTOCOL[1],
                "cooldown_s": ORIGINAL_PROTOCOL[2],
                "max_gpu_temp_c": ORIGINAL_PROTOCOL[3],
            },
            "short_protocol": {
                "warmup_s": SHORT_PROTOCOL[0],
                "duration_s": SHORT_PROTOCOL[1],
                "cooldown_s": SHORT_PROTOCOL[2],
                "max_gpu_temp_c": SHORT_PROTOCOL[3],
                "sample_interval_s": 1.0,
            },
            "adaptive_cooldown_target_c": 74,
            "adaptive_cooldown_max_s": 300,
            "reason": (
                "保留全部实验单元和三次重复，将每个 run 的名义时序减半，为后续共置、模型与调度实验释放时间；"
                "t84 长协议 trial 完整封存且不与正式特征混用。"
            ),
        },
        "time_budget": {
            "remaining_row_count": remaining_rows,
            "original_nominal_seconds_per_run": old_nominal_s,
            "short_nominal_seconds_per_run": short_nominal_s,
            "original_nominal_hours": remaining_rows * old_nominal_s / 3600,
            "short_nominal_hours": remaining_rows * short_nominal_s / 3600,
            "nominal_hours_saved": remaining_rows
            * (old_nominal_s - short_nominal_s)
            / 3600,
            "note": "不含进程启动、批间冷启动等待、按 74°C 目标延长的 cooldown 和失败审计。",
        },
        "plans": {
            "original": {
                "path": _rel(root, original_plan),
                "sha256": original_verification["plan_sha256"],
            },
            "t84_trial": {
                "path": _rel(root, t84_plan),
                "sha256": t84_verification["plan_sha256"],
            },
            "short_full": {
                "path": _rel(root, short_plan),
                "sha256": short_verification["plan_sha256"],
                "row_count": short_verification["row_count"],
                "manifest": _rel(
                    root, short_plan.with_name(f"{short_plan.stem}-manifest.json")
                ),
                "combinations": _rel(
                    root,
                    short_plan.with_name(f"{short_plan.stem}-combinations.json"),
                ),
            },
            "stage_compatibility": short_stages,
            "profile_baseline_compatibility": compatibility,
        },
        "t84_timing_trial": trial,
        "checks": {
            "all_696_remaining_rows_preserved": remaining_rows == 696,
            "unused_solo_rows_compatible": short_stages[0]["row_count"] == 24,
            "profile_cartesian_product_preserved": short_stages[1]["row_count"] == 480,
            "main_60_groups_three_repeats_preserved": short_stages[2]["row_count"] == 180,
            "extra_12_groups_three_repeats_preserved": short_stages[3]["row_count"] == 36,
            "new_raw_directories_disjoint": all(
                item["raw_directories_disjoint"] for item in short_stages
            ),
            "unmodified_fields_equal": all(
                item["unmodified_fields_equal"] for item in short_stages
            ),
            "short_stage_directories_pairwise_disjoint": len(short_directories)
            == sum(len(item) for item in short_directory_sets),
            "short_and_t84_directories_disjoint": not (
                short_directories & t84_directories
            ),
            "t84_trial_excluded": trial["included_in_final_profiles"] is False,
            "single_short_profile_protocol": compatibility["profile_protocol"]
            == {
                "warmup_s": 10.0,
                "duration_s": 30.0,
                "cooldown_s": 10.0,
                "max_gpu_temp_c": 84.0,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = discover_repo_root(Path.cwd())
    output = args.output.resolve()
    if root.resolve() not in output.parents:
        raise ValueError("输出必须位于仓库内")
    payload = build_payload(root)
    rendered = stable_json_dumps(payload, indent=2) + "\n"
    if output.exists():
        if output.read_text(encoding="utf-8") != rendered:
            raise RuntimeError("既有短时序修订证据与当前计划/原始数据重算结果不一致")
        mode = "verified"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
        mode = "created"
    print(
        stable_json_dumps(
            {
                "status": "passed",
                "mode": mode,
                "output": _rel(root, output),
                "short_plan_count": 1,
                "remaining_rows": payload["time_budget"]["remaining_row_count"],
                "short_nominal_hours": payload["time_budget"]["short_nominal_hours"],
                "t84_valid_runs_excluded": payload["t84_timing_trial"][
                    "completed_run_count"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
