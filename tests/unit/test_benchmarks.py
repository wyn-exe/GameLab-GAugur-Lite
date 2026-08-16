from __future__ import annotations

import hashlib
import json
import time
import warnings
from pathlib import Path

import pytest

from gaugur_lite.benchmarks.calibration import (
    CALIBRATION_TIMING_SEMANTICS,
    CalibrationRequest,
    _build_worker_command,
    summarize_calibration_records,
    verify_calibration,
)
from gaugur_lite.benchmarks.engine import BenchmarkWorkerConfig, run_benchmark_worker


def _request(repo_root: Path) -> CalibrationRequest:
    return CalibrationRequest(
        config_path=repo_root / "configs" / "local.example.yaml",
        resources=("cpu_compute", "gpu_memory"),
        levels=(0.0, 0.5, 1.0),
        repeats=2,
        warmup_s=1.0,
        duration_s=2.0,
        sample_interval_s=1.0,
        gpu_index=0,
        cpu_workers=2,
        memory_buffer_mib=8,
        gpu_matrix_size=128,
        gpu_memory_max_mib=64,
        output_file=repo_root / "artifacts" / "calibration.json",
        metrics_file=repo_root / "artifacts" / "calibration-metrics.jsonl",
        status_file=repo_root / "artifacts" / "calibration-status.json",
        workers_root=repo_root / "artifacts" / "calibration-workers",
    )


def _make_repo_root(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    (tmp_path / "games").mkdir()
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "local.example.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return tmp_path


def test_worker_zero_pressure_writes_ready_and_completed_status(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    status = tmp_path / "status.json"

    result = run_benchmark_worker(
        config=BenchmarkWorkerConfig(resource="cpu_compute", pressure=0.0, runtime_s=0.1),
        ready_file=ready,
        status_file=status,
    )

    assert result["status"] == "completed"
    assert result["active_fraction"] == 0.0
    assert json.loads(ready.read_text(encoding="utf-8"))["status"] == "ready"
    assert json.loads(status.read_text(encoding="utf-8"))["status"] == "completed"


def test_worker_waits_for_shared_barrier_and_separates_warmup(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    status = tmp_path / "status.json"
    barrier = tmp_path / "barrier.json"
    start_ns = time.perf_counter_ns() + 10_000_000
    end_ns = start_ns + 60_000_000
    barrier.write_text(
        json.dumps(
            {
                "status": "released",
                "measurement_start_monotonic_ns": start_ns,
                "measurement_end_monotonic_ns": end_ns,
            }
        ),
        encoding="utf-8",
    )

    result = run_benchmark_worker(
        config=BenchmarkWorkerConfig(
            resource="cpu_compute",
            pressure=0.0,
            runtime_s=0.06,
            warmup_s=0.01,
            barrier_file=barrier,
        ),
        ready_file=ready,
        status_file=status,
    )

    assert result["barrier_used"] is True
    assert result["warmup_elapsed_s"] > 0
    assert 0.04 <= float(result["elapsed_s"]) <= 0.15


def test_worker_config_rejects_invalid_pressure_and_resource() -> None:
    with pytest.raises(ValueError, match="pressure"):
        BenchmarkWorkerConfig(resource="cpu_compute", pressure=1.1, runtime_s=1)
    with pytest.raises(ValueError, match="未知"):
        BenchmarkWorkerConfig(resource="not_a_resource", pressure=0.1, runtime_s=1)  # type: ignore[arg-type]


def test_cpu_compute_worker_keeps_input_bounded_without_runtime_warning(tmp_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        result = run_benchmark_worker(
            config=BenchmarkWorkerConfig(
                resource="cpu_compute", pressure=1.0, runtime_s=0.15, cpu_workers=1
            ),
            ready_file=tmp_path / "ready.json",
            status_file=tmp_path / "status.json",
        )

    assert result["status"] == "completed"
    assert int(result["operations"]) > 0


def test_request_rejects_existing_output_and_bad_levels(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    request = _request(repo_root)
    request.output_file.parent.mkdir()
    request.output_file.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        request.validate(repo_root)

    invalid = CalibrationRequest(
        **{**request.__dict__, "output_file": repo_root / "artifacts" / "new.json", "levels": (0.0, 0.75)}
    )
    with pytest.raises(ValueError, match="以 1 结束"):
        invalid.validate(repo_root)


def test_calibration_worker_excludes_warmup_from_measurement(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    request = _request(repo_root)

    command = _build_worker_command(
        request=request,
        resource="cpu_compute",
        pressure_applied=0.25,
        ready_file=tmp_path / "ready.json",
        worker_status=tmp_path / "status.json",
    )

    assert command[command.index("--warmup-s") + 1] == "1.0"
    assert command[command.index("--runtime-s") + 1] == "2.5"
    assert request.public_plan(repo_root)["timing_semantics"] == CALIBRATION_TIMING_SEMANTICS


def test_summary_builds_monotonic_requested_to_observed_curve(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    request = _request(repo_root)
    records: list[dict[str, object]] = []
    for resource in request.resources:
        for level in request.levels:
            for repeat in range(1, 3):
                observed = level if resource == "gpu_memory" else level + (0.001 if repeat == 1 else -0.001)
                records.append(
                    {
                        "resource": resource,
                        "pressure_requested": level,
                        "repeat": repeat,
                        "sample_count": 2,
                        "observed_pressure": observed,
                        "hardware_signal": 100.0 * level,
                        "worker": {"status": "completed"},
                        "worker_directory": repo_root
                        / "artifacts"
                        / "workers"
                        / resource
                        / f"r{repeat:02d}",
                    }
                )

    result = summarize_calibration_records(
        request=request,
        records=records,
        repo_root=repo_root,
        config_hash="a" * 64,
        environment={"host": "test"},
    )

    assert result["status"] == "passed"
    assert result["cell_count"] == 12
    assert result["request"]["timing_semantics"] == CALIBRATION_TIMING_SEMANTICS
    assert all(item["checks"]["observed_pressure_monotonic"] for item in result["resources"])
    assert result["resources"][0]["points"][1]["observed_pressure_mean"] == pytest.approx(0.5)


def test_summary_compares_observed_with_capped_applied_pressure(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    request = CalibrationRequest(
        **{
            **_request(repo_root).__dict__,
            "resources": ("gpu_compute",),
            "levels": (0.0, 0.5, 1.0),
            "pressure_caps": {
                "cpu_compute": 1.0,
                "memory_bandwidth": 1.0,
                "gpu_compute": 0.25,
                "gpu_memory": 1.0,
            },
        }
    )
    records = []
    for level in request.levels:
        for repeat in (1, 2):
            records.append(
                {
                    "resource": "gpu_compute",
                    "pressure_requested": level,
                    "pressure_applied": level * 0.25,
                    "repeat": repeat,
                    "sample_count": 2,
                    "observed_pressure": level * 0.25,
                    "hardware_signal": 10.0,
                    "worker": {"status": "completed"},
                    "worker_directory": repo_root / "artifacts" / f"r{repeat}",
                }
            )

    result = summarize_calibration_records(
        request=request,
        records=records,
        repo_root=repo_root,
        config_hash="a" * 64,
        environment={"host": "test"},
    )

    assert result["status"] == "passed"
    assert result["resources"][0]["points"][-1]["pressure_applied"] == 0.25
    assert result["resources"][0]["max_abs_error"] == 0.0


def test_verify_checks_metrics_hash_and_resource_quality_gates(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    artifacts = repo_root / "artifacts"
    artifacts.mkdir()
    metrics = artifacts / "calibration-metrics.jsonl"
    metrics.write_text('{"sequence":0}\n', encoding="utf-8")
    metrics_hash = hashlib.sha256(metrics.read_bytes()).hexdigest()
    calibration = artifacts / "calibration.json"
    calibration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "cell_count": 2,
                "expected_cell_count": 2,
                "request": {"resources": ["cpu_compute"]},
                "resources": [
                    {
                        "resource": "cpu_compute",
                        "status": "passed",
                        "checks": {"observed_pressure_monotonic": True},
                    }
                ],
                "artifacts": {
                    "metrics": "artifacts/calibration-metrics.jsonl",
                    "metrics_sha256": metrics_hash,
                },
            }
        ),
        encoding="utf-8",
    )

    verified = verify_calibration(repo_root=repo_root, calibration_file=calibration)

    assert verified["status"] == "passed"
    assert all(check["passed"] for check in verified["checks"])
