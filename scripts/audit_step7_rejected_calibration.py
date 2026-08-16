"""从原始文件重算 Safety-v2 首次校准为何不能作为正式分母。"""

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


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _artifact_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if root.resolve() not in path.parents:
        raise ValueError(f"artifact path escapes repository: {value}")
    return path


def build_audit(*, root: Path, candidate: Path, candidate_head: str) -> dict[str, Any]:
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    metrics_path = _artifact_path(root, payload["artifacts"]["metrics"])
    metrics = [
        json.loads(line)
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if line
    ]

    grouped: dict[tuple[str, float], list[float]] = {}
    for run in payload["runs"]:
        pressure = float(run["pressure_requested"])
        if pressure == 0:
            continue
        key = (str(run["resource"]), pressure)
        grouped.setdefault(key, []).append(
            int(run["worker"]["operations"]) / float(run["worker"]["elapsed_s"])
        )

    cells = []
    for (resource, pressure), throughputs in sorted(grouped.items()):
        mean = statistics.fmean(throughputs)
        cv = statistics.stdev(throughputs) / mean * 100
        cells.append(
            {
                "resource": resource,
                "pressure_requested": pressure,
                "throughputs_ops_per_s": throughputs,
                "throughput_mean_ops_per_s": mean,
                "throughput_cv_pct": cv,
                "passed_5_pct_gate": math.isfinite(cv) and cv <= 5.0,
            }
        )

    worst = max(cells, key=lambda item: item["throughput_cv_pct"])
    warmup_elapsed = [float(run["worker"].get("warmup_elapsed_s", 0)) for run in payload["runs"]]
    warmup_operations = [int(run["worker"].get("warmup_operations", 0)) for run in payload["runs"]]
    temperatures = [float(row["gpu_temp_c"]) for row in metrics if row.get("gpu_temp_c") is not None]
    thermal_slowdowns = [
        row for row in metrics if row.get("gpu_thermal_slowdown_active") is True
    ]
    quality_passed = all(
        item.get("status") == "passed" and all(item.get("checks", {}).values())
        for item in payload["resources"]
    )
    metrics_sha = _sha256(metrics_path)
    checks = [
        {
            "name": "calibration_pressure_quality_gates",
            "passed": payload.get("status") == "passed" and quality_passed,
        },
        {
            "name": "cell_count_60",
            "passed": payload.get("cell_count") == 60,
        },
        {
            "name": "metrics_sha256",
            "passed": metrics_sha == payload["artifacts"].get("metrics_sha256"),
            "actual": metrics_sha,
            "expected": payload["artifacts"].get("metrics_sha256"),
        },
        {
            "name": "gpu_temperature_at_most_80_c",
            "passed": bool(temperatures) and max(temperatures) <= 80.0,
            "actual_max_c": max(temperatures),
            "expected_max_c": 80.0,
        },
        {
            "name": "all_nonzero_throughput_cv_at_most_5_pct",
            "passed": all(item["passed_5_pct_gate"] for item in cells),
            "actual_max_pct": worst["throughput_cv_pct"],
            "expected_max_pct": 5.0,
        },
        {
            "name": "worker_warmup_excluded_from_measurement",
            "passed": payload["request"].get("timing_semantics")
            == "worker_warmup_excluded_v1",
            "actual": payload["request"].get("timing_semantics"),
            "expected": "worker_warmup_excluded_v1",
        },
    ]
    return {
        "schema_version": 1,
        "status": "rejected",
        "candidate_guarded_head": candidate_head,
        "candidate": _relative(root, candidate),
        "candidate_sha256": _sha256(candidate),
        "metrics": _relative(root, metrics_path),
        "metrics_sha256": metrics_sha,
        "decision": {
            "included_in_profile_denominators": False,
            "included_in_model_training": False,
            "rerun_required": True,
            "cv_threshold_pct_unchanged": 5.0,
            "replacement_timing_semantics": "worker_warmup_excluded_v1",
        },
        "checks": checks,
        "pressure_quality_status": payload.get("status"),
        "gpu_temperature": {
            "sample_count": len(temperatures),
            "minimum_c": min(temperatures),
            "maximum_c": max(temperatures),
            "samples_above_80_c": sum(value > 80.0 for value in temperatures),
            "thermal_slowdown_samples": len(thermal_slowdowns),
        },
        "worker_warmup_evidence": {
            "requested_warmup_s": payload["request"].get("warmup_s"),
            "timing_semantics": payload["request"].get("timing_semantics"),
            "maximum_reported_warmup_elapsed_s": max(warmup_elapsed),
            "maximum_reported_warmup_operations": max(warmup_operations),
        },
        "standalone_throughput": {
            "nonzero_cell_count": len(cells),
            "threshold_pct": 5.0,
            "maximum_cv_pct": worst["throughput_cv_pct"],
            "failed_cells": [item for item in cells if not item["passed_5_pct_gate"]],
            "cells": cells,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidate",
        type=Path,
        default=Path("artifacts/calibration/step7-safety-v2/formal-calibration.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/calibration/step7-safety-v2/rejected-candidate-001-audit.json"),
    )
    parser.add_argument("--candidate-head", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    candidate = (root / args.candidate).resolve()
    output = (root / args.output).resolve()
    if root not in candidate.parents or root not in output.parents:
        raise ValueError("input and output must stay inside repository")
    audit = build_audit(root=root, candidate=candidate, candidate_head=args.candidate_head)
    encoded = (json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if output.exists():
        if output.read_bytes() != encoded:
            raise FileExistsError(f"existing audit differs; refusing to overwrite: {output}")
        output_action = "Audit verified at"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(encoded)
        output_action = "Audit written to"
    failed = [check["name"] for check in audit["checks"] if not check["passed"]]
    print(
        "REJECTED calibration candidate: "
        f"max temperature={audit['gpu_temperature']['maximum_c']:.0f} C, "
        f"max CV={audit['standalone_throughput']['maximum_cv_pct']:.4f}%, "
        f"failed checks={','.join(failed)}"
    )
    print(f"{output_action}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
