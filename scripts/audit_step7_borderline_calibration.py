"""独立封存 warmup-v1 候选 002 的窄幅 CV 失败集合与安全状态。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gaugur_lite.benchmarks.calibration import (  # noqa: E402
    CALIBRATION_TIMING_SEMANTICS,
    denominator_cells,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_audit(candidate: Path, candidate_head: str) -> dict[str, Any]:
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    metrics_path = REPO_ROOT / payload["artifacts"]["metrics"]
    metrics = [
        json.loads(line)
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    cells = denominator_cells(payload)
    failed = [cell for cell in cells if float(cell["throughput_cv_pct"]) > 5.0]
    temperatures = [float(row["gpu_temp_c"]) for row in metrics if row.get("gpu_temp_c") is not None]
    warmup_elapsed = [float(run["worker"]["warmup_elapsed_s"]) for run in payload["runs"]]
    nonzero_warmup_operations = [
        int(run["worker"]["warmup_operations"])
        for run in payload["runs"]
        if float(run["pressure_requested"]) > 0
    ]
    return {
        "schema_version": 1,
        "status": "requires_confirmation",
        "candidate_guarded_head": candidate_head,
        "candidate": candidate.relative_to(REPO_ROOT).as_posix(),
        "candidate_sha256": _sha256(candidate),
        "metrics": metrics_path.relative_to(REPO_ROOT).as_posix(),
        "metrics_sha256": _sha256(metrics_path),
        "decision": {
            "included_in_profile_denominators": False,
            "included_in_model_training": False,
            "cv_threshold_pct_unchanged": 5.0,
            "confirmation_rule": "append r04/r05 to all and only failed cells; recompute five-repeat sample CV",
            "confirmation_required": True,
        },
        "calibration": {
            "status": payload.get("status"),
            "cell_count": payload.get("cell_count"),
            "timing_semantics": payload.get("request", {}).get("timing_semantics"),
            "timing_semantics_passed": payload.get("request", {}).get("timing_semantics")
            == CALIBRATION_TIMING_SEMANTICS,
            "max_abs_errors": {
                item["resource"]: item["max_abs_error"] for item in payload["resources"]
            },
        },
        "worker_warmup": {
            "minimum_elapsed_s": min(warmup_elapsed),
            "maximum_elapsed_s": max(warmup_elapsed),
            "minimum_nonzero_pressure_operations": min(nonzero_warmup_operations),
            "all_nonzero_pressure_workers_executed_warmup": all(
                value > 0 for value in nonzero_warmup_operations
            ),
        },
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
            "nonzero_cell_count": len(cells),
            "threshold_pct": 5.0,
            "passed_cell_count": len(cells) - len(failed),
            "failed_cell_count": len(failed),
            "maximum_cv_pct": max(float(cell["throughput_cv_pct"]) for cell in cells),
            "failed_cells": failed,
            "cells": cells,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-head", required=True)
    parser.add_argument(
        "--candidate",
        type=Path,
        default=Path("artifacts/calibration/step7-safety-v2/formal-calibration-warmup-v1.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/calibration/step7-safety-v2/borderline-candidate-002-audit.json"),
    )
    args = parser.parse_args()
    candidate = (REPO_ROOT / args.candidate).resolve()
    output = (REPO_ROOT / args.output).resolve()
    audit = build_audit(candidate, args.candidate_head)
    encoded = (json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if output.exists():
        if output.read_bytes() != encoded:
            raise FileExistsError(f"existing audit differs; refusing overwrite: {output}")
        action = "verified"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(encoded)
        action = "written"
    print(
        "REQUIRES_CONFIRMATION candidate: "
        f"failed={audit['standalone_throughput']['failed_cell_count']}, "
        f"max CV={audit['standalone_throughput']['maximum_cv_pct']:.4f}%, "
        f"max temperature={audit['gpu_temperature']['maximum_c']:.0f} C"
    )
    print(f"Audit {action}: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
