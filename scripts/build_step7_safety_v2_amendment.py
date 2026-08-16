"""封存 s30 首批安全试验，并声明它不进入最终 profile。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gaugur_lite.config import discover_repo_root, stable_json_dumps

PLAN_SHA256 = "4d6510a6c036582c20272883007ba5fdd68809e00cd9ae4134f2b5a7836d2af1"
SOURCE_TREE_SHA256 = "2126d7dc20614e291f82952cbadb113f9f01eea1f5895299e97e9d2fa0821969"
EXECUTION_COMMIT = "9f473f46f3374e2642c4fcff674c88bd55fa1a6c"
INVALID_RUN_ID = "formal-v1__profile__pyxel_snake__gpu_compute__p100__r03"
INVALID_REASON = "RunInvalidError:gpu_temperature_exceeded:85.0>84.0"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(repo_root: Path) -> dict[str, Any]:
    report_path = repo_root / "artifacts/profiles/step7/s30/invocations/invocation-001-batch-001-run.json"
    unit_log = repo_root / "artifacts/profiles/step7/s30/invocations/invocation-002-unit-tests.txt"
    plan_path = repo_root / "artifacts/plans/formal-v1-remaining-s30.csv"
    raw_root = repo_root / "data/raw/remaining-s30/formal-v1"
    for path in (report_path, unit_log, plan_path, raw_root):
        if not path.exists():
            raise FileNotFoundError(f"缺少 safety pilot 输入: {path}")
    if _sha256(plan_path) != PLAN_SHA256:
        raise RuntimeError("s30 计划 SHA-256 与冻结值不一致")

    report = _read(report_path)
    results = report.get("results", [])
    completed = [item for item in results if item.get("valid") is True]
    invalid = [item for item in results if item.get("valid") is not True]
    provenance = report.get("execution_provenance", {})
    if (
        report.get("status") != "failed"
        or int(report.get("selected_runs", 0)) != 24
        or len(completed) != 23
        or len(invalid) != 1
        or report.get("plan_sha256") != PLAN_SHA256
        or provenance.get("root_commit") != EXECUTION_COMMIT
        or provenance.get("source_tree_sha256") != SOURCE_TREE_SHA256
        or provenance.get("root_dirty_at_execution") is not False
    ):
        raise RuntimeError("s30 首批报告身份或 23/1 计数不一致")
    failed = invalid[0]
    if failed.get("run_id") != INVALID_RUN_ID or failed.get("reason") != INVALID_REASON:
        raise RuntimeError("s30 高温无效单元与已审计身份不一致")

    max_valid_temperature = 0.0
    for result in results:
        index_path = raw_root / str(result["run_id"]) / "index.json"
        index = _read(index_path)
        attempts = index.get("attempts", [])
        if len(attempts) != 1 or int(attempts[0].get("attempt", 0)) != 1:
            raise RuntimeError(f"pilot run 不得出现自动重试: {result['run_id']}")
        if result.get("valid") is True:
            summary = _read(repo_root / str(result["directory"]) / "summary.json")
            max_valid_temperature = max(
                max_valid_temperature, float(summary.get("gpu_temp_c_max") or 0)
            )

    failed_dir = repo_root / str(failed["directory"])
    failure = _read(failed_dir / "failure.json")
    if (
        failure.get("reason") != INVALID_REASON
        or failure.get("cleanup", {}).get("global_kill_used") is not False
        or len(failure.get("cleanup", {}).get("actions", [])) != 2
        or failure.get("cooldown", {}).get("thermal_target_reached") is not True
    ):
        raise RuntimeError("高温 attempt 的精确进程清理或 cooldown 证据不完整")
    if (raw_root / INVALID_RUN_ID / "attempts/a002").exists():
        raise RuntimeError("检测到禁止的 a002：safety pilot 封存失败")

    unit_text = unit_log.read_text(encoding="utf-8-sig")
    if (
        "test_harness_uses_shared_barrier_and_reports_measurement_coverage" not in unit_text
        or "1 failed, 75 passed" not in unit_text
    ):
        raise RuntimeError("invocation-002 必须只留下已审计的计时单测抖动证据")
    if any((report_path.parent).glob("invocation-002-*-run.json")):
        raise RuntimeError("invocation-002 单测失败后不应存在 Runner 报告")

    return {
        "schema_version": 1,
        "status": "sealed",
        "decision": "replace_with_safety_v2",
        "included_in_final_profiles": False,
        "included_in_model_training": False,
        "deletion_performed": False,
        "pilot": {
            "plan": plan_path.relative_to(repo_root).as_posix(),
            "plan_sha256": PLAN_SHA256,
            "run_report": report_path.relative_to(repo_root).as_posix(),
            "run_report_sha256": _sha256(report_path),
            "selected_runs": 24,
            "valid_runs": 23,
            "invalid_runs": 1,
            "max_valid_gpu_temp_c": max_valid_temperature,
            "invalid_run_id": INVALID_RUN_ID,
            "invalid_reason": INVALID_REASON,
            "attempts_per_run": 1,
            "a002_created": False,
            "global_kill_used": False,
            "execution_commit": EXECUTION_COMMIT,
            "execution_source_tree_sha256": SOURCE_TREE_SHA256,
        },
        "stop_evidence": {
            "next_invocation_unit_log": unit_log.relative_to(repo_root).as_posix(),
            "next_invocation_unit_log_sha256": _sha256(unit_log),
            "runner_started_after_unit_failure": False,
            "flaky_test_fixed_before_safety_v2": True,
        },
        "safety_v2": {
            "max_gpu_temp_c": 80.0,
            "adaptive_cooldown_target_c": 70.0,
            "batch_start_gpu_temp_max_c": 50.0,
            "normalized_pressure_levels": [0.0, 0.25, 0.5, 0.75, 1.0],
            "gpu_compute_applied_levels": [0.0, 0.0625, 0.125, 0.1875, 0.25],
            "gpu_compute_pressure_cap": 0.25,
            "requires_new_calibration": True,
            "isolated_raw_root": "data/raw/safety-v2-s30",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/profiles/step7/safety-v2-amendment.json"),
    )
    args = parser.parse_args()
    root = discover_repo_root(Path.cwd())
    payload = build(root)
    text = stable_json_dumps(payload, indent=2) + "\n"
    output = (root / args.output).resolve() if not args.output.is_absolute() else args.output
    if output.exists():
        if output.read_text(encoding="utf-8") != text:
            raise FileExistsError(f"既有 safety-v2 证据不同，拒绝覆盖: {output}")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")
    print(stable_json_dumps(payload, indent=2))


if __name__ == "__main__":
    main()
