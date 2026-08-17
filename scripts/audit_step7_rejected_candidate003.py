"""独立封存 Candidate003：压力作用通过，但 5% 吞吐 CV 门失败。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = Path("artifacts/calibration/step7-safety-v2/formal-calibration-stable-v1.json")
OUTPUT = Path(
    "artifacts/calibration/step7-safety-v2/rejected-candidate-003-audit.json"
)
EXPECTED_PROTOCOL = "native_threads_1_warmup5_duration15_repeats5_v1"
EXPECTED_FAILED = {
    ("cpu_compute", 0.25),
    ("cpu_compute", 1.0),
    ("gpu_compute", 0.5),
    ("gpu_compute", 0.75),
    ("gpu_compute", 1.0),
}
THREAD_KEYS = {
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _tree_hash(root: Path) -> tuple[int, str]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    lines = [f"{path.relative_to(root).as_posix()}\t{_sha256(path)}" for path in files]
    payload = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
    return len(files), hashlib.sha256(payload).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    base = (REPO_ROOT / args.base).resolve() if not args.base.is_absolute() else args.base
    output = (
        (REPO_ROOT / args.output).resolve()
        if not args.output.is_absolute()
        else args.output
    )
    root = base.parent
    metrics = root / "formal-calibration-stable-v1-metrics.jsonl"
    status = root / "formal-calibration-stable-v1-status.json"
    verification = root / "formal-calibration-stable-v1-verification.json"
    plot = root / "pressure-calibration-stable-v1.png"
    workers = root / "formal-calibration-stable-v1-workers"
    required = (base, metrics, status, verification, plot)
    missing = [path for path in required if not path.is_file()]
    if missing or not workers.is_dir():
        raise FileNotFoundError(f"Candidate003 evidence missing: {missing or [workers]}")

    payload = _read_json(base)
    metrics_rows = [
        json.loads(line)
        for line in metrics.read_text(encoding="utf-8").splitlines()
        if line
    ]
    status_payload = _read_json(status)
    verification_payload = _read_json(verification)
    grouped: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for run in payload.get("runs", []):
        grouped[(str(run["resource"]), float(run["pressure_requested"]))].append(run)

    cells = []
    failed = set()
    for key, runs in sorted(grouped.items()):
        if key[1] == 0:
            continue
        values = [
            int(run["worker"]["operations"]) / float(run["worker"]["elapsed_s"])
            for run in sorted(runs, key=lambda item: int(item["repeat"]))
        ]
        mean = statistics.fmean(values)
        std = statistics.stdev(values)
        cv = std / mean * 100
        if cv > 5:
            failed.add(key)
        cells.append(
            {
                "resource": key[0],
                "pressure_requested": key[1],
                "throughputs_ops_per_s": values,
                "throughput_mean_ops_per_s": mean,
                "throughput_sample_std_ops_per_s": std,
                "throughput_cv_pct": cv,
                "status_at_5_pct": "failed" if cv > 5 else "passed",
            }
        )

    all_workers_valid = True
    for run in payload.get("runs", []):
        worker = run.get("worker", {})
        environment = worker.get("benchmark_environment", {})
        values = environment.get("environment", {})
        disk = REPO_ROOT / run["worker_directory"] / "status.json"
        all_workers_valid = all_workers_valid and (
            worker.get("status") == "completed"
            and disk.is_file()
            and _read_json(disk) == worker
            and environment.get("protocol") == EXPECTED_PROTOCOL
            and environment.get("native_thread_limit") == 1
            and set(values) == THREAD_KEYS
            and set(values.values()) == {"1"}
        )

    temperatures = [float(row["gpu_temp_c"]) for row in metrics_rows]
    slowdown_count = sum(
        bool(row.get("gpu_thermal_slowdown_active")) for row in metrics_rows
    )
    worker_file_count, worker_tree_sha = _tree_hash(workers)
    checks = [
        {
            "name": "calibration_identity",
            "passed": payload.get("cell_count") == 100
            and payload.get("request", {}).get("benchmark_protocol") == EXPECTED_PROTOCOL
            and payload.get("execution", {}).get("root_dirty_at_execution") is False,
        },
        {
            "name": "pressure_actuation_calibration_passed",
            "passed": payload.get("status") == "passed"
            and verification_payload.get("status") == "passed",
        },
        {
            "name": "all_100_workers_complete_and_match_disk",
            "passed": len(payload.get("runs", [])) == 100 and all_workers_valid,
        },
        {
            "name": "worker_tree_complete",
            "passed": worker_file_count == 400,
            "actual": worker_file_count,
            "expected": 400,
        },
        {
            "name": "metrics_complete",
            "passed": len(metrics_rows) == 1600,
            "actual": len(metrics_rows),
            "expected": 1600,
        },
        {
            "name": "status_preserved",
            "passed": status_payload.get("status") == "completed",
        },
        {
            "name": "exact_five_cv_failures_at_5_pct",
            "passed": failed == EXPECTED_FAILED,
            "actual": [f"{resource}/{pressure}" for resource, pressure in sorted(failed)],
            "expected": [
                f"{resource}/{pressure}" for resource, pressure in sorted(EXPECTED_FAILED)
            ],
        },
        {
            "name": "temperature_and_thermal_slowdown_safe",
            "passed": max(temperatures) <= 80 and slowdown_count == 0,
        },
    ]
    result = {
        "schema_version": 1,
        "status": "rejected" if all(check["passed"] for check in checks) else "audit_failed",
        "candidate": 3,
        "candidate_guarded_head": payload.get("execution", {}).get("root_commit"),
        "benchmark_protocol": EXPECTED_PROTOCOL,
        "checks": checks,
        "failed_nonzero_cells": len(failed),
        "nonzero_cells": cells,
        "maximum_throughput_cv_pct": max(float(cell["throughput_cv_pct"]) for cell in cells),
        "gpu_temperature": {
            "minimum_c": min(temperatures),
            "maximum_c": max(temperatures),
            "sample_count": len(temperatures),
            "samples_above_80_c": sum(value > 80 for value in temperatures),
            "thermal_slowdown_samples": slowdown_count,
        },
        "artifacts": {
            "calibration": _relative(base),
            "calibration_sha256": _sha256(base),
            "metrics": _relative(metrics),
            "metrics_sha256": _sha256(metrics),
            "status": _relative(status),
            "status_sha256": _sha256(status),
            "verification": _relative(verification),
            "verification_sha256": _sha256(verification),
            "plot": _relative(plot),
            "plot_sha256": _sha256(plot),
            "worker_file_count": worker_file_count,
            "worker_tree_sha256": worker_tree_sha,
        },
        "decision": {
            "cv_threshold_pct": 5.0,
            "included_in_profile_denominators": False,
            "included_in_model_training": False,
            "selective_retry_allowed": False,
            "fresh_candidate_required": True,
            "reason": "5/16 nonzero denominators exceeded the preregistered 5% CV gate",
        },
    }
    if result["status"] != "rejected":
        failed_checks = [check["name"] for check in checks if not check["passed"]]
        raise RuntimeError(f"Candidate003 audit failed: {failed_checks}")
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output.exists():
        if output.read_text(encoding="utf-8-sig") != encoded:
            raise FileExistsError(f"Existing audit differs: {output}")
    else:
        output.write_text(encoded, encoding="utf-8", newline="\n")
    print(
        "REJECTED Candidate003: "
        f"failed={len(failed)}/16, max CV={result['maximum_throughput_cv_pct']:.4f}%, "
        f"temperature={min(temperatures):.0f}-{max(temperatures):.0f} C"
    )
    print(f"PASS independent audit: {len(checks)}/{len(checks)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
