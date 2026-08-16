"""独立重算 Candidate 002 追加确认失败，并封存其安全与完整性证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(root: Path, value: str | Path) -> Path:
    path = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if root.resolve() not in path.parents:
        raise ValueError(f"artifact path escapes repository: {value}")
    return path


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _throughput(run: dict[str, Any]) -> float:
    return int(run["worker"]["operations"]) / float(run["worker"]["elapsed_s"])


def build_audit(
    *, root: Path, base_path: Path, confirmation_path: Path, guarded_head: str
) -> dict[str, Any]:
    base = json.loads(base_path.read_text(encoding="utf-8"))
    confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    metrics_path = _inside(root, confirmation["artifacts"]["metrics"])
    status_path = confirmation_path.with_name("formal-calibration-confirmation-v1-status.json")
    metrics = [
        json.loads(line)
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    status = json.loads(status_path.read_text(encoding="utf-8"))

    base_hash = _sha256(base_path)
    metrics_hash = _sha256(metrics_path)
    expected_cells = {
        ("cpu_compute", 0.5),
        ("gpu_compute", 0.5),
        ("memory_bandwidth", 0.5),
    }
    base_runs: dict[tuple[str, float, int], dict[str, Any]] = {
        (str(run["resource"]), float(run["pressure_requested"]), int(run["repeat"])): run
        for run in base["runs"]
    }
    confirmation_runs: dict[tuple[str, float, int], dict[str, Any]] = {
        (str(run["resource"]), float(run["pressure_requested"]), int(run["repeat"])): run
        for run in confirmation["runs"]
    }

    recomputed_cells = []
    worker_files_match = True
    for key, run in confirmation_runs.items():
        worker_path = _inside(root, run["worker_directory"]) / "status.json"
        disk_worker = json.loads(worker_path.read_text(encoding="utf-8"))
        worker_files_match = worker_files_match and disk_worker == run["worker"]

    for resource, pressure in sorted(expected_cells):
        base_values = [_throughput(base_runs[(resource, pressure, repeat)]) for repeat in (1, 2, 3)]
        added_values = [
            _throughput(confirmation_runs[(resource, pressure, repeat)]) for repeat in (4, 5)
        ]
        combined = [*base_values, *added_values]
        mean = statistics.fmean(combined)
        cv = statistics.stdev(combined) / mean * 100
        recomputed_cells.append(
            {
                "resource": resource,
                "pressure_requested": pressure,
                "base_throughputs_ops_per_s": base_values,
                "confirmation_throughputs_ops_per_s": added_values,
                "combined_throughputs_ops_per_s": combined,
                "combined_throughput_mean_ops_per_s": mean,
                "combined_throughput_sample_std_ops_per_s": statistics.stdev(combined),
                "combined_throughput_cv_pct": cv,
                "passed_5_pct_gate": math.isfinite(cv) and cv <= 5.0,
            }
        )

    serialized_cells = {
        (str(cell["resource"]), float(cell["pressure_requested"])): cell
        for cell in confirmation["combined_cells"]
    }
    numerical_match = True
    for cell in recomputed_cells:
        serialized = serialized_cells[(cell["resource"], cell["pressure_requested"])]
        numerical_match = numerical_match and math.isclose(
            cell["combined_throughput_cv_pct"],
            float(serialized["combined_throughput_cv_pct"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        )

    temperatures = [
        float(row["gpu_temp_c"]) for row in metrics if row.get("gpu_temp_c") is not None
    ]
    run_keys = {str(run["run_key"]) for run in confirmation["runs"]}
    expected_run_keys = {
        f"step4-{resource}-p050-r{repeat:02d}"
        for resource, _ in expected_cells
        for repeat in (4, 5)
    }
    failed = [cell for cell in recomputed_cells if not cell["passed_5_pct_gate"]]
    checks = [
        {
            "name": "base_hash_binding",
            "passed": base_hash == confirmation.get("base_calibration_sha256"),
            "actual": base_hash,
            "expected": confirmation.get("base_calibration_sha256"),
        },
        {
            "name": "metrics_hash_binding",
            "passed": metrics_hash == confirmation.get("artifacts", {}).get("metrics_sha256"),
            "actual": metrics_hash,
            "expected": confirmation.get("artifacts", {}).get("metrics_sha256"),
        },
        {
            "name": "failed_status_preserved",
            "passed": confirmation.get("status") == "failed" and status.get("status") == "failed",
        },
        {
            "name": "exact_three_selected_cells",
            "passed": set(serialized_cells) == expected_cells
            and confirmation.get("selected_cell_count") == 3,
        },
        {
            "name": "exact_six_r04_r05_runs",
            "passed": run_keys == expected_run_keys
            and confirmation.get("additional_cell_count") == 6,
        },
        {
            "name": "all_workers_completed_and_match_disk",
            "passed": worker_files_match
            and all(run["worker"].get("status") == "completed" for run in confirmation["runs"]),
        },
        {
            "name": "metrics_complete",
            "passed": len(metrics) == 42
            and {str(row["run_id"]) for row in metrics} == expected_run_keys,
            "actual_rows": len(metrics),
            "expected_rows": 42,
        },
        {
            "name": "combined_values_recomputed",
            "passed": numerical_match,
        },
        {
            "name": "unchanged_5_pct_gate_rejects_only_cpu_compute_p050",
            "passed": {(cell["resource"], cell["pressure_requested"]) for cell in failed}
            == {("cpu_compute", 0.5)},
        },
        {
            "name": "temperature_and_thermal_slowdown_safe",
            "passed": bool(temperatures)
            and max(temperatures) <= 80.0
            and not any(row.get("gpu_thermal_slowdown_active") is True for row in metrics),
        },
    ]
    if not all(check["passed"] for check in checks):
        failed_checks = [check["name"] for check in checks if not check["passed"]]
        raise RuntimeError(f"confirmation audit checks failed: {failed_checks}")

    return {
        "schema_version": 1,
        "status": "rejected",
        "candidate_guarded_head": guarded_head,
        "base_calibration": _relative(root, base_path),
        "base_calibration_sha256": base_hash,
        "confirmation": _relative(root, confirmation_path),
        "confirmation_sha256": _sha256(confirmation_path),
        "metrics": _relative(root, metrics_path),
        "metrics_sha256": metrics_hash,
        "status_file": _relative(root, status_path),
        "status_file_sha256": _sha256(status_path),
        "decision": {
            "included_in_profile_denominators": False,
            "included_in_model_training": False,
            "selective_retry_allowed": False,
            "cv_threshold_pct_unchanged": 5.0,
            "fresh_protocol_required": True,
        },
        "checks": checks,
        "gpu_temperature": {
            "sample_count": len(temperatures),
            "minimum_c": min(temperatures),
            "maximum_c": max(temperatures),
            "samples_above_80_c": sum(value > 80.0 for value in temperatures),
            "thermal_slowdown_samples": sum(
                row.get("gpu_thermal_slowdown_active") is True for row in metrics
            ),
        },
        "standalone_throughput": {
            "threshold_pct": 5.0,
            "failed_cell_count": len(failed),
            "failed_cells": failed,
            "cells": recomputed_cells,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-head", required=True)
    parser.add_argument(
        "--base",
        type=Path,
        default=Path("artifacts/calibration/step7-safety-v2/formal-calibration-warmup-v1.json"),
    )
    parser.add_argument(
        "--confirmation",
        type=Path,
        default=Path(
            "artifacts/calibration/step7-safety-v2/formal-calibration-confirmation-v1.json"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "artifacts/calibration/step7-safety-v2/rejected-candidate-002-confirmation-audit.json"
        ),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    base = _inside(root, args.base)
    confirmation = _inside(root, args.confirmation)
    output = _inside(root, args.output)
    audit = build_audit(
        root=root,
        base_path=base,
        confirmation_path=confirmation,
        guarded_head=args.candidate_head,
    )
    encoded = (json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if output.exists():
        if output.read_bytes() != encoded:
            raise FileExistsError(f"existing audit differs; refusing overwrite: {output}")
        action = "verified"
    else:
        output.write_bytes(encoded)
        action = "written"
    failed = audit["standalone_throughput"]["failed_cells"]
    print(
        "REJECTED Candidate 002 confirmation: "
        f"failed={failed[0]['resource']}/p{failed[0]['pressure_requested']:.2f}, "
        f"combined CV={failed[0]['combined_throughput_cv_pct']:.4f}%, "
        f"temperature={audit['gpu_temperature']['minimum_c']:.0f}-"
        f"{audit['gpu_temperature']['maximum_c']:.0f} C"
    )
    print(f"PASS independent audit: {len(audit['checks'])}/{len(audit['checks'])} checks")
    print(f"Audit {action}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
