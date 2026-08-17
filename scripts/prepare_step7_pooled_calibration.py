"""审计 Candidate003/004，并从两轮完整 campaign 派生 10-repeat 分母。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from gaugur_lite.benchmarks.protocol import (  # noqa: E402
    CANDIDATE003_BENCHMARK_PROTOCOL,
    NATIVE_THREAD_ENV_KEYS,
    POOLED_CALIBRATION_PROTOCOL,
    POOLED_CALIBRATION_REPEATS,
    POOLED_CAMPAIGN_DRIFT_THRESHOLD_PCT,
    POOLED_DENOMINATOR_RSE_THRESHOLD_PCT,
    STABLE_BENCHMARK_PROTOCOL,
    STABLE_DENOMINATOR_CV_THRESHOLD_PCT,
    stable_benchmark_environment_valid,
)


ROOT = REPO_ROOT / "artifacts" / "calibration" / "step7-safety-v2"
RESOURCES = ("cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory")
PRESSURES = (0.0, 0.25, 0.5, 0.75, 1.0)
CAMPAIGNS = (
    (3, "formal-calibration-stable-v1", CANDIDATE003_BENCHMARK_PROTOCOL),
    (4, "formal-calibration-stable-v2", STABLE_BENCHMARK_PROTOCOL),
)
POOLED_PATH = ROOT / "formal-calibration-pooled-v3.json"
ACCEPTANCE_PATH = ROOT / "formal-calibration-pooled-v3-acceptance.json"
CANDIDATE003_AUDIT_PATH = ROOT / "rejected-candidate-003-audit.json"
CANDIDATE004_AUDIT_PATH = ROOT / "rejected-candidate-004-audit.json"


class AuditError(RuntimeError):
    """输入不完整、被修改或不符合合并合同。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"无法读取 JSON: {_relative(path)}: {error}") from error
    if not isinstance(value, dict):
        raise AuditError(f"JSON 顶层必须是对象: {_relative(path)}")
    return value


def _encoded(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_or_verify(path: Path, payload: dict[str, Any], verify_only: bool) -> None:
    expected = _encoded(payload)
    if path.exists():
        if path.read_text(encoding="utf-8") != expected:
            raise AuditError(f"派生文件与原始数据重算结果不一致: {_relative(path)}")
        return
    if verify_only:
        raise AuditError(f"缺少已封存的派生文件: {_relative(path)}")
    path.write_text(expected, encoding="utf-8", newline="")


def _tree_digest(directory: Path) -> tuple[int, str]:
    if not directory.is_dir():
        raise AuditError(f"worker 目录缺失: {_relative(directory)}")
    files = sorted((path for path in directory.rglob("*") if path.is_file()), key=lambda p: p.as_posix())
    lines = [f"{path.relative_to(directory).as_posix()}\t{_sha256(path)}" for path in files]
    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    return len(files), hashlib.sha256(payload).hexdigest()


def _metrics_audit(path: Path) -> dict[str, Any]:
    count = 0
    temperatures: list[float] = []
    thermal = 0
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise AuditError(f"metrics 第 {line_number} 行非法: {error}") from error
            count += 1
            if row.get("gpu_temp_c") is not None:
                temperatures.append(float(row["gpu_temp_c"]))
            thermal += int(row.get("gpu_thermal_slowdown_active") is True)
    if count != 1600 or not temperatures or max(temperatures) > 80 or thermal:
        raise AuditError(
            f"metrics 安全/完整性失败: rows={count}, max_temp={max(temperatures, default=-1)}, thermal={thermal}"
        )
    return {
        "sample_count": count,
        "minimum_c": min(temperatures),
        "maximum_c": max(temperatures),
        "samples_above_80_c": sum(value > 80 for value in temperatures),
        "thermal_slowdown_samples": thermal,
    }


def _campaign(candidate: int, stem: str, protocol: str) -> dict[str, Any]:
    calibration_path = ROOT / f"{stem}.json"
    metrics_path = ROOT / f"{stem}-metrics.jsonl"
    status_path = ROOT / f"{stem}-status.json"
    verification_path = ROOT / f"{stem}-verification.json"
    workers_path = ROOT / f"{stem}-workers"
    plot_path = ROOT / f"pressure-calibration-stable-v{candidate - 2}.png"
    for path in (calibration_path, metrics_path, status_path, verification_path, plot_path):
        if not path.is_file():
            raise AuditError(f"campaign 输入缺失: {_relative(path)}")

    payload = _read_json(calibration_path)
    status = _read_json(status_path)
    verification = _read_json(verification_path)
    request = payload.get("request", {})
    execution = payload.get("execution", {})
    if (
        payload.get("status") != "passed"
        or payload.get("cell_count") != 100
        or payload.get("expected_cell_count") != 100
        or request.get("benchmark_protocol") != protocol
        or request.get("resources") != list(RESOURCES)
        or request.get("levels") != list(PRESSURES)
        or request.get("repeats") != 5
        or float(request.get("warmup_s", -1)) != 5
        or float(request.get("duration_s", -1)) != 15
        or float(request.get("sample_interval_s", -1)) != 1
        or float(request.get("max_gpu_temp_c", -1)) != 80
        or execution.get("root_dirty_at_execution") is not False
        or not execution.get("root_commit")
        or not execution.get("source_tree_sha256")
        or status.get("status") != "completed"
        or status.get("cell_count") != 100
        or verification.get("status") != "passed"
        or any(check.get("passed") is not True for check in verification.get("checks", []))
        or verification.get("calibration_sha256") != _sha256(calibration_path)
    ):
        raise AuditError(f"Candidate00{candidate} campaign 合同或 verification 非法")

    grouped: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for run in payload.get("runs", []):
        resource = str(run.get("resource"))
        pressure = float(run.get("pressure_requested", -1))
        repeat = int(run.get("repeat", 0))
        worker = run.get("worker", {})
        worker_directory = REPO_ROOT / str(run.get("worker_directory", ""))
        status_file = worker_directory / "status.json"
        if (
            resource not in RESOURCES
            or pressure not in PRESSURES
            or repeat not in range(1, 6)
            or worker.get("status") != "completed"
            or not stable_benchmark_environment_valid(
                worker.get("benchmark_environment"), expected_protocol=protocol
            )
            or not status_file.is_file()
            or _read_json(status_file) != worker
        ):
            raise AuditError(f"Candidate00{candidate} worker 不完整: {run.get('run_key')}")
        grouped[(resource, pressure)].append(run)
    if len(payload.get("runs", [])) != 100 or set(grouped) != {
        (resource, pressure) for resource in RESOURCES for pressure in PRESSURES
    } or any({int(run["repeat"]) for run in runs} != set(range(1, 6)) for runs in grouped.values()):
        raise AuditError(f"Candidate00{candidate} 未完整覆盖 4×5×5")

    cells: list[dict[str, Any]] = []
    for key in sorted(grouped):
        resource, pressure = key
        runs = sorted(grouped[key], key=lambda run: int(run["repeat"]))
        values = [] if pressure == 0 else [
            int(run["worker"]["operations"]) / float(run["worker"]["elapsed_s"])
            for run in runs
        ]
        mean = statistics.fmean(values) if values else None
        std = statistics.stdev(values) if values else None
        cv = std / mean * 100 if values else None
        cells.append(
            {
                "resource": resource,
                "pressure_requested": pressure,
                "throughputs_ops_per_s": values,
                "throughput_mean_ops_per_s": mean,
                "throughput_sample_std_ops_per_s": std,
                "throughput_cv_pct": cv,
            }
        )

    file_count, tree_sha = _tree_digest(workers_path)
    if file_count != 400:
        raise AuditError(f"Candidate00{candidate} worker 文件数不是 400: {file_count}")
    return {
        "candidate": candidate,
        "stem": stem,
        "protocol": protocol,
        "payload": payload,
        "grouped": grouped,
        "cells": cells,
        "temperature": _metrics_audit(metrics_path),
        "artifacts": {
            "calibration": _relative(calibration_path),
            "calibration_sha256": _sha256(calibration_path),
            "metrics": _relative(metrics_path),
            "metrics_sha256": _sha256(metrics_path),
            "status": _relative(status_path),
            "status_sha256": _sha256(status_path),
            "verification": _relative(verification_path),
            "verification_sha256": _sha256(verification_path),
            "plot": _relative(plot_path),
            "plot_sha256": _sha256(plot_path),
            "workers": _relative(workers_path),
            "worker_file_count": file_count,
            "worker_tree_sha256": tree_sha,
        },
    }


def _candidate004_audit(campaign: dict[str, Any]) -> dict[str, Any]:
    nonzero = [dict(cell) for cell in campaign["cells"] if cell["pressure_requested"] > 0]
    for cell in nonzero:
        cell["status_at_10_pct"] = (
            "passed" if float(cell["throughput_cv_pct"]) <= 10 else "failed"
        )
    failed = [cell for cell in nonzero if cell["status_at_10_pct"] == "failed"]
    failed_keys = [f"{cell['resource']}/{cell['pressure_requested']}" for cell in failed]
    if failed_keys != ["gpu_compute/0.25"]:
        raise AuditError(f"Candidate004 失败集合与已观察结果不符: {failed_keys}")
    return {
        "schema_version": 1,
        "status": "rejected",
        "candidate": 4,
        "benchmark_protocol": campaign["protocol"],
        "artifacts": campaign["artifacts"],
        "gpu_temperature": campaign["temperature"],
        "nonzero_cells": nonzero,
        "failed_nonzero_cells": len(failed),
        "maximum_throughput_cv_pct": max(float(cell["throughput_cv_pct"]) for cell in nonzero),
        "checks": [
            {"name": "calibration_and_verification_passed", "passed": True},
            {"name": "all_100_workers_complete_and_match_disk", "passed": True},
            {"name": "worker_tree_complete", "passed": True, "expected": 400, "actual": 400},
            {"name": "metrics_complete", "passed": True, "expected": 1600, "actual": 1600},
            {"name": "exact_one_cv_failure_at_10_pct", "passed": True, "actual": failed_keys},
            {"name": "temperature_and_thermal_slowdown_safe", "passed": True},
        ],
        "decision": {
            "cv_threshold_pct": 10.0,
            "reason": "1/16 nonzero denominators exceeded the preregistered 10% CV gate",
            "included_in_profile_denominators": False,
            "included_in_model_training": False,
            "selective_retry_allowed": False,
            "fresh_candidate_required": False,
            "pooled_post_hoc_amendment_allowed_only_with_complete_candidate003": True,
        },
    }


def _validate_candidate003_audit(campaign: dict[str, Any]) -> dict[str, Any]:
    audit = _read_json(CANDIDATE003_AUDIT_PATH)
    if (
        audit.get("status") != "rejected"
        or audit.get("candidate") != 3
        or audit.get("failed_nonzero_cells") != 5
        or audit.get("artifacts", {}).get("calibration_sha256")
        != campaign["artifacts"]["calibration_sha256"]
        or audit.get("artifacts", {}).get("worker_tree_sha256")
        != campaign["artifacts"]["worker_tree_sha256"]
        or audit.get("decision", {}).get("included_in_profile_denominators") is not False
        or audit.get("decision", {}).get("selective_retry_allowed") is not False
        or any(check.get("passed") is not True for check in audit.get("checks", []))
    ):
        raise AuditError("Candidate003 rejection audit 与原始 campaign 不一致")
    return audit


def _pooled(c3: dict[str, Any], c4: dict[str, Any]) -> dict[str, Any]:
    request3 = c3["payload"]["request"]
    request4 = c4["payload"]["request"]
    comparable3 = {key: value for key, value in request3.items() if key != "benchmark_protocol"}
    comparable4 = {key: value for key, value in request4.items() if key != "benchmark_protocol"}
    engine3 = c3["payload"]["execution"]["source_files"].get("gaugur_lite/benchmarks/engine.py")
    engine4 = c4["payload"]["execution"]["source_files"].get("gaugur_lite/benchmarks/engine.py")
    if (
        comparable3 != comparable4
        or c3["payload"].get("config_sha256") != c4["payload"].get("config_sha256")
        or c3["payload"].get("environment_sha256") != c4["payload"].get("environment_sha256")
        or not engine3
        or engine3 != engine4
    ):
        raise AuditError("两轮 campaign 的参数、环境、配置或 benchmark engine 不可合并")

    runs: list[dict[str, Any]] = []
    quality_cells: list[dict[str, Any]] = []
    for resource in RESOURCES:
        for pressure in PRESSURES:
            campaign_values: list[list[float]] = []
            for campaign_index, campaign in enumerate((c3, c4)):
                source_runs = sorted(campaign["grouped"][(resource, pressure)], key=lambda run: int(run["repeat"]))
                values: list[float] = []
                for source in source_runs:
                    pooled_repeat = int(source["repeat"]) + campaign_index * 5
                    copied = dict(source)
                    copied.update(
                        {
                            "run_key": f"pooled-v3-{resource}-p{int(pressure * 100):03d}-r{pooled_repeat:02d}",
                            "repeat": pooled_repeat,
                            "source_candidate": campaign["candidate"],
                            "source_repeat": int(source["repeat"]),
                            "source_run_key": source["run_key"],
                            "source_calibration_sha256": campaign["artifacts"]["calibration_sha256"],
                            "source_benchmark_protocol": campaign["protocol"],
                        }
                    )
                    runs.append(copied)
                    if pressure > 0:
                        values.append(int(source["worker"]["operations"]) / float(source["worker"]["elapsed_s"]))
                campaign_values.append(values)
            all_values = [*campaign_values[0], *campaign_values[1]]
            if pressure == 0:
                mean = std = cv = rse = drift = None
                means: list[float] = []
                passed = True
            else:
                mean = statistics.fmean(all_values)
                std = statistics.stdev(all_values)
                cv = std / mean * 100
                rse = cv / math.sqrt(POOLED_CALIBRATION_REPEATS)
                means = [statistics.fmean(values) for values in campaign_values]
                drift = abs(means[1] - means[0]) / statistics.fmean(means) * 100
                passed = (
                    cv <= STABLE_DENOMINATOR_CV_THRESHOLD_PCT
                    and rse <= POOLED_DENOMINATOR_RSE_THRESHOLD_PCT
                    and drift <= POOLED_CAMPAIGN_DRIFT_THRESHOLD_PCT
                )
            quality_cells.append(
                {
                    "resource": resource,
                    "pressure_requested": pressure,
                    "throughputs_ops_per_s": all_values,
                    "campaign_mean_ops_per_s": means,
                    "throughput_mean_ops_per_s": mean,
                    "throughput_sample_std_ops_per_s": std,
                    "throughput_cv_pct": cv,
                    "throughput_standard_error_pct": rse,
                    "campaign_mean_drift_pct": drift,
                    "status": "passed" if passed else "failed",
                }
            )
    nonzero = [cell for cell in quality_cells if cell["pressure_requested"] > 0]
    if any(cell["status"] != "passed" for cell in nonzero):
        raise AuditError("合并后的 CV/RSE/campaign drift 质量门未通过")

    request = dict(request4)
    request.update({"benchmark_protocol": POOLED_CALIBRATION_PROTOCOL, "repeats": POOLED_CALIBRATION_REPEATS})
    source_campaigns = []
    for campaign in (c3, c4):
        execution = campaign["payload"]["execution"]
        source_campaigns.append(
            {
                "candidate": campaign["candidate"],
                "status": "rejected_as_standalone",
                "benchmark_protocol": campaign["protocol"],
                "root_commit": execution["root_commit"],
                "source_tree_sha256": execution["source_tree_sha256"],
                "artifacts": campaign["artifacts"],
            }
        )
    return {
        "schema_version": 1,
        "status": "passed",
        "cell_count": 200,
        "expected_cell_count": 200,
        "config_sha256": c4["payload"]["config_sha256"],
        "environment": c4["payload"]["environment"],
        "environment_sha256": c4["payload"]["environment_sha256"],
        "request": request,
        "resources": c4["payload"]["resources"],
        "runs": runs,
        "source_campaigns": source_campaigns,
        "compatibility": {
            "profile_worker_benchmark_protocol": STABLE_BENCHMARK_PROTOCOL,
            "benchmark_engine_sha256": engine4,
            "native_thread_limit": 1,
            "native_thread_environment_keys": list(NATIVE_THREAD_ENV_KEYS),
            "config_sha256": c4["payload"]["config_sha256"],
        },
        "quality": {
            "status": "passed",
            "nonzero_cell_count": 16,
            "criteria": {
                "throughput_cv_max_pct": STABLE_DENOMINATOR_CV_THRESHOLD_PCT,
                "throughput_standard_error_max_pct": POOLED_DENOMINATOR_RSE_THRESHOLD_PCT,
                "campaign_mean_drift_max_pct": POOLED_CAMPAIGN_DRIFT_THRESHOLD_PCT,
            },
            "maximum_throughput_cv_pct": max(float(cell["throughput_cv_pct"]) for cell in nonzero),
            "maximum_throughput_standard_error_pct": max(float(cell["throughput_standard_error_pct"]) for cell in nonzero),
            "maximum_campaign_mean_drift_pct": max(float(cell["campaign_mean_drift_pct"]) for cell in nonzero),
            "cells": quality_cells,
        },
        "derivation": {
            "post_hoc_method_amendment": True,
            "user_confirmed": True,
            "new_measurements_created": False,
            "complete_campaigns_only": True,
            "source_run_count": 200,
            "selected_source_run_count": 200,
            "selective_retry_or_cherry_picking": False,
            "candidate003_included_as_standalone": False,
            "candidate004_included_as_standalone": False,
            "description": "Candidate003 与 Candidate004 各 5 次完整重复按原样合并；两候选单独仍为 rejected。",
        },
    }


def _acceptance(pooled: dict[str, Any], c3: dict[str, Any], c4: dict[str, Any]) -> dict[str, Any]:
    quality = pooled["quality"]
    return {
        "schema_version": 1,
        "status": "passed",
        "candidate": "pooled-v3-post-hoc-amendment",
        "benchmark_protocol": POOLED_CALIBRATION_PROTOCOL,
        "cell_count": 200,
        "denominator_repeat_count": 10,
        "denominator_nonzero_cell_count": 16,
        "denominator_cv_threshold_pct": 10.0,
        "denominator_cv_max_pct": quality["maximum_throughput_cv_pct"],
        "denominator_standard_error_threshold_pct": 5.0,
        "denominator_standard_error_max_pct": quality["maximum_throughput_standard_error_pct"],
        "campaign_mean_drift_threshold_pct": 10.0,
        "campaign_mean_drift_max_pct": quality["maximum_campaign_mean_drift_pct"],
        "calibration": _relative(POOLED_PATH),
        "calibration_sha256": hashlib.sha256(_encoded(pooled).encode("utf-8")).hexdigest(),
        "candidate003_rejection_audit_sha256": _sha256(CANDIDATE003_AUDIT_PATH),
        "candidate004_rejection_audit_sha256": _sha256(CANDIDATE004_AUDIT_PATH),
        "source_campaigns": [
            {
                "candidate": campaign["candidate"],
                "calibration_sha256": campaign["artifacts"]["calibration_sha256"],
                "worker_file_count": campaign["artifacts"]["worker_file_count"],
                "worker_tree_sha256": campaign["artifacts"]["worker_tree_sha256"],
                "standalone_status": "rejected",
            }
            for campaign in (c3, c4)
        ],
        "post_hoc_method_amendment": True,
        "user_confirmed": True,
        "new_measurements_created": False,
        "all_200_source_runs_used": True,
        "selective_retry_or_cherry_picking": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        c3 = _campaign(*CAMPAIGNS[0])
        c4 = _campaign(*CAMPAIGNS[1])
        _validate_candidate003_audit(c3)
        candidate004_audit = _candidate004_audit(c4)
        _write_or_verify(CANDIDATE004_AUDIT_PATH, candidate004_audit, args.verify_only)
        pooled = _pooled(c3, c4)
        _write_or_verify(POOLED_PATH, pooled, args.verify_only)
        acceptance = _acceptance(pooled, c3, c4)
        _write_or_verify(ACCEPTANCE_PATH, acceptance, args.verify_only)
    except AuditError as error:
        print(f"FAIL pooled calibration: {error}", file=sys.stderr)
        return 2
    quality = pooled["quality"]
    print(
        "PASS pooled calibration: runs=200, repeats=10, "
        f"max CV={quality['maximum_throughput_cv_pct']:.4f}%, "
        f"max RSE={quality['maximum_throughput_standard_error_pct']:.4f}%, "
        f"max drift={quality['maximum_campaign_mean_drift_pct']:.4f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
