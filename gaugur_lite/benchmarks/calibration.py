"""Step 4 请求压力到实际作用压力的校准、汇总与独立复核。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import psutil

from ..config import config_sha256, load_local_config, stable_json_dumps
from ..metrics.system_sampler import SystemSampler
from ..metrics.writer import JsonlWriter, write_json_atomic
from .engine import BENCHMARK_RESOURCES, BenchmarkResource
from .protocol import (
    STABLE_BENCHMARK_PROTOCOL,
    STABLE_CALIBRATION_DURATION_S,
    STABLE_CALIBRATION_REPEATS,
    STABLE_CALIBRATION_WARMUP_S,
    apply_stable_benchmark_environment,
    stable_benchmark_environment_valid,
)

CALIBRATION_SCHEMA_VERSION = 1
CALIBRATION_TIMING_SEMANTICS = "worker_warmup_excluded_v1"
CONFIRMATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CalibrationRequest:
    config_path: Path
    resources: tuple[BenchmarkResource, ...]
    levels: tuple[float, ...]
    repeats: int
    warmup_s: float
    duration_s: float
    sample_interval_s: float
    gpu_index: int
    cpu_workers: int
    memory_buffer_mib: int
    gpu_matrix_size: int
    gpu_memory_max_mib: int
    output_file: Path
    metrics_file: Path
    status_file: Path
    workers_root: Path
    plot_file: Path | None = None
    pressure_caps: dict[str, float] = field(
        default_factory=lambda: {
            "cpu_compute": 1.0,
            "memory_bandwidth": 1.0,
            "gpu_compute": 1.0,
            "gpu_memory": 1.0,
        }
    )
    max_gpu_temp_c: float = 82.0
    benchmark_protocol: str | None = None

    def validate(self, repo_root: Path) -> None:
        if not self.resources or len(set(self.resources)) != len(self.resources):
            raise ValueError("resources 必须非空且不重复")
        if any(resource not in BENCHMARK_RESOURCES for resource in self.resources):
            raise ValueError("resources 包含未知项")
        if not self.levels or self.levels[0] != 0.0 or self.levels[-1] != 1.0:
            raise ValueError("levels 必须以 0 开始、以 1 结束")
        if tuple(sorted(self.levels)) != self.levels or len(set(self.levels)) != len(self.levels):
            raise ValueError("levels 必须严格递增且不重复")
        if any(not 0.0 <= level <= 1.0 for level in self.levels):
            raise ValueError("levels 必须位于 [0, 1]")
        if set(self.pressure_caps) != set(BENCHMARK_RESOURCES):
            raise ValueError("pressure_caps 必须完整声明四类资源")
        if any(not 0 < cap <= 1 for cap in self.pressure_caps.values()):
            raise ValueError("pressure_caps 必须位于 (0, 1]")
        if not 30 <= self.max_gpu_temp_c <= 110:
            raise ValueError("max_gpu_temp_c 必须位于 [30, 110]")
        if self.repeats < 2:
            raise ValueError("repeats 必须至少为 2")
        if self.warmup_s < 0 or self.duration_s <= 0 or self.sample_interval_s <= 0:
            raise ValueError("warmup/duration/sample interval 参数非法")
        if self.sample_interval_s > self.duration_s:
            raise ValueError("sample_interval_s 不得大于 duration_s")
        if not 1 <= self.cpu_workers <= 64:
            raise ValueError("cpu_workers 必须位于 [1, 64]")
        if not 8 <= self.memory_buffer_mib <= 4096:
            raise ValueError("memory_buffer_mib 必须位于 [8, 4096]")
        if not 128 <= self.gpu_matrix_size <= 4096:
            raise ValueError("gpu_matrix_size 必须位于 [128, 4096]")
        if not 64 <= self.gpu_memory_max_mib <= 12288:
            raise ValueError("gpu_memory_max_mib 必须位于 [64, 12288]")
        if self.benchmark_protocol not in (None, STABLE_BENCHMARK_PROTOCOL):
            raise ValueError(f"未知 benchmark_protocol: {self.benchmark_protocol}")
        if self.benchmark_protocol == STABLE_BENCHMARK_PROTOCOL and (
            self.repeats != STABLE_CALIBRATION_REPEATS
            or self.warmup_s != STABLE_CALIBRATION_WARMUP_S
            or self.duration_s != STABLE_CALIBRATION_DURATION_S
        ):
            raise ValueError("stable benchmark protocol 必须使用 5 repeats、5 秒 warmup、15 秒测量")
        for path in self._all_output_paths():
            _inside_repo(repo_root, path)
            if path.exists():
                raise FileExistsError(f"输出已存在，拒绝覆盖: {path}")

    def _all_output_paths(self) -> tuple[Path, ...]:
        paths: tuple[Path, ...] = (
            self.output_file,
            self.metrics_file,
            self.status_file,
            self.workers_root,
        )
        return paths if self.plot_file is None else (*paths, self.plot_file)

    def public_plan(self, repo_root: Path) -> dict[str, Any]:
        return {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "command": "benchmark calibrate",
            "timing_semantics": CALIBRATION_TIMING_SEMANTICS,
            "benchmark_protocol": self.benchmark_protocol,
            "dry_run": True,
            "resources": list(self.resources),
            "levels": list(self.levels),
            "pressure_caps": dict(self.pressure_caps),
            "applied_levels": {
                resource: [level * self.pressure_caps[resource] for level in self.levels]
                for resource in self.resources
            },
            "max_gpu_temp_c": self.max_gpu_temp_c,
            "repeats": self.repeats,
            "warmup_s": self.warmup_s,
            "duration_s": self.duration_s,
            "sample_interval_s": self.sample_interval_s,
            "cell_count": len(self.resources) * len(self.levels) * self.repeats,
            "estimated_measurement_s": len(self.resources)
            * len(self.levels)
            * self.repeats
            * (self.warmup_s + self.duration_s),
            "outputs": {
                "calibration": _repo_relative(repo_root, self.output_file),
                "metrics": _repo_relative(repo_root, self.metrics_file),
                "status": _repo_relative(repo_root, self.status_file),
                "workers": _repo_relative(repo_root, self.workers_root),
                "plot": _repo_relative(repo_root, self.plot_file)
                if self.plot_file is not None
                else None,
            },
            "mutations_planned": [
                "calibration JSON",
                "raw calibration JSONL",
                "status JSON",
                "per-worker ready/status/log files",
                "calibration plot" if self.plot_file is not None else None,
            ],
        }


def _inside_repo(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"输出必须位于仓库内且不能等于仓库根目录: {path}")
    return resolved


def _repo_relative(repo_root: Path, path: Path | None) -> str | None:
    return path.resolve().relative_to(repo_root.resolve()).as_posix() if path is not None else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _environment_fingerprint(gpu_index: int) -> dict[str, Any]:
    fingerprint: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "ram_total_bytes": psutil.virtual_memory().total,
        "gpu_index": gpu_index,
    }
    try:
        import torch

        fingerprint.update(
            {
                "torch": torch.__version__,
                "torch_cuda_runtime": torch.version.cuda,
                "cuda_available": torch.cuda.is_available(),
                "gpu_name": torch.cuda.get_device_name(gpu_index)
                if torch.cuda.is_available()
                else None,
            }
        )
    except (ImportError, RuntimeError):
        fingerprint.update({"torch": None, "cuda_available": False, "gpu_name": None})
    return fingerprint


def _execution_provenance(repo_root: Path) -> dict[str, Any]:
    """绑定实际校准源码；生成中的 artifacts 不会被误报为源码 dirty。"""

    source_paths = sorted((repo_root / "gaugur_lite").rglob("*.py"))
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        source_paths.append(pyproject)
    hashes = {
        path.relative_to(repo_root).as_posix(): _file_sha256(path) for path in source_paths
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    "gaugur_lite",
                    "pyproject.toml",
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
                shell=False,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        commit = None
        dirty = None
    return {
        "root_commit": commit,
        "root_dirty_at_execution": dirty,
        "source_tree_sha256": config_sha256(hashes),
        "source_files": hashes,
    }


def _pressure_token(level: float) -> str:
    return f"p{int(round(level * 100)):03d}"


def _hardware_signal(resource: BenchmarkResource, rows: list[dict[str, Any]]) -> float | None:
    field = {
        "cpu_compute": "cpu_util_pct",
        "memory_bandwidth": "cpu_util_pct",
        "gpu_compute": "gpu_util_pct",
        "gpu_memory": "gpu_mem_used_bytes",
    }[resource]
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return statistics.fmean(values) if values else None


def _terminate_exact_child(process: subprocess.Popen[str]) -> str:
    process.terminate()
    try:
        process.wait(timeout=5)
        return "terminated"
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        return "killed"


def _wait_for_ready(process: subprocess.Popen[str], ready_file: Path, timeout_s: float = 30.0) -> None:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if ready_file.is_file():
            return
        if process.poll() is not None:
            raise RuntimeError(f"benchmark worker 在 ready 前退出，exit_code={process.returncode}")
        time.sleep(0.05)
    raise RuntimeError("benchmark worker ready 超时")


def _build_worker_command(
    *,
    request: CalibrationRequest,
    resource: BenchmarkResource,
    pressure_applied: float,
    ready_file: Path,
    worker_status: Path,
) -> list[str]:
    """构造校准 worker 命令；worker 自己预热，预热操作不计入吞吐分母。"""

    # 额外半个采样周期只用于保证父进程能取得 measurement 终点样本。
    runtime_s = request.duration_s + max(0.5, request.sample_interval_s / 2)
    return [
        sys.executable,
        "-m",
        "gaugur_lite",
        "benchmark",
        "_worker",
        "--resource",
        resource,
        "--pressure",
        str(pressure_applied),
        "--warmup-s",
        str(request.warmup_s),
        "--runtime-s",
        str(runtime_s),
        "--cpu-workers",
        str(request.cpu_workers),
        "--memory-buffer-mib",
        str(request.memory_buffer_mib),
        "--gpu-matrix-size",
        str(request.gpu_matrix_size),
        "--gpu-memory-max-mib",
        str(request.gpu_memory_max_mib),
        "--ready-file",
        str(ready_file),
        "--status-file",
        str(worker_status),
    ]


def _run_cell(
    *,
    request: CalibrationRequest,
    resource: BenchmarkResource,
    level: float,
    repeat: int,
    writer: JsonlWriter,
) -> dict[str, Any]:
    worker_dir = request.workers_root / resource / _pressure_token(level) / f"r{repeat:02d}"
    worker_dir.mkdir(parents=True, exist_ok=False)
    ready_file = worker_dir / "ready.json"
    worker_status = worker_dir / "status.json"
    stdout_file = worker_dir / "stdout.log"
    stderr_file = worker_dir / "stderr.log"
    pressure_applied = level * request.pressure_caps[resource]
    command = _build_worker_command(
        request=request,
        resource=resource,
        pressure_applied=pressure_applied,
        ready_file=ready_file,
        worker_status=worker_status,
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if request.benchmark_protocol == STABLE_BENCHMARK_PROTOCOL:
        apply_stable_benchmark_environment(environment)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process: subprocess.Popen[str] | None = None
    records: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        with stdout_file.open("x", encoding="utf-8", newline="\n") as stdout, stderr_file.open(
            "x", encoding="utf-8", newline="\n"
        ) as stderr:
            process = subprocess.Popen(
                command,
                cwd=request.config_path.resolve().parents[1],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                text=True,
                creationflags=creationflags,
            )
            _wait_for_ready(process, ready_file)
            time.sleep(request.warmup_s)
            measurement_started = time.perf_counter()
            measurement_deadline = measurement_started + request.duration_s
            next_sample = measurement_started
            sample_index = 0
            run_key = f"step4-{resource}-{_pressure_token(level)}-r{repeat:02d}"
            with SystemSampler(
                run_id=run_key,
                gpu_index=request.gpu_index,
                process_pid=process.pid,
            ) as sampler:
                # 同时保留窗口起点和终点样本，原始序列可覆盖完整 duration_s。
                while next_sample <= measurement_deadline + 1e-9:
                    now = time.perf_counter()
                    if now < next_sample:
                        time.sleep(next_sample - now)
                    event = sampler.sample(sample_index)
                    row = event.model_dump(mode="json")
                    row.update(
                        {
                            "calibration_run_id": run_key,
                            "resource": resource,
                            "pressure_requested": level,
                            "pressure_applied": pressure_applied,
                            "repeat": repeat,
                        }
                    )
                    writer.write(row)
                    records.append(row)
                    if (
                        event.gpu_temp_c is not None
                        and float(event.gpu_temp_c) > request.max_gpu_temp_c
                    ):
                        raise RuntimeError(
                            "gpu_temperature_exceeded:"
                            f"{float(event.gpu_temp_c):.1f}>{request.max_gpu_temp_c:.1f}"
                        )
                    sample_index += 1
                    next_sample += request.sample_interval_s
        process.wait(timeout=max(5.0, request.sample_interval_s * 3))
        if process.returncode != 0:
            raise RuntimeError(f"benchmark worker 失败，exit_code={process.returncode}")
        if not worker_status.is_file():
            raise RuntimeError("benchmark worker 未写入 status.json")
        worker = json.loads(worker_status.read_text(encoding="utf-8"))
        if worker.get("status") != "completed":
            raise RuntimeError(f"benchmark worker 状态异常: {worker.get('status')}")
        elapsed_s = time.perf_counter() - started
        if resource == "gpu_memory":
            capacity = int(worker.get("capacity_bytes", 0))
            observed_pressure = int(worker.get("allocated_bytes", 0)) / capacity if capacity else 0.0
        else:
            observed_pressure = float(worker.get("active_fraction", 0.0))
        return {
            "resource": resource,
            "pressure_requested": level,
            "pressure_applied": pressure_applied,
            "repeat": repeat,
            "run_key": run_key,
            "elapsed_s": elapsed_s,
            "sample_count": len(records),
            "observed_pressure": observed_pressure,
            "hardware_signal": _hardware_signal(resource, records),
            "worker": worker,
            "worker_directory": worker_dir,
        }
    except BaseException:
        if process is not None and process.poll() is None:
            _terminate_exact_child(process)
        raise


def _sample_std(values: Iterable[float]) -> float:
    collected = list(values)
    return statistics.stdev(collected) if len(collected) >= 2 else 0.0


def summarize_calibration_records(
    *,
    request: CalibrationRequest,
    records: list[dict[str, Any]],
    repo_root: Path,
    config_hash: str,
    environment: dict[str, Any],
) -> dict[str, Any]:
    """将单次 records 聚合为每个资源的 requested → observed 曲线。"""

    grouped: dict[tuple[BenchmarkResource, float], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["resource"], float(record["pressure_requested"]))].append(record)

    resources: list[dict[str, Any]] = []
    all_checks_passed = True
    for resource in request.resources:
        points: list[dict[str, Any]] = []
        previous_observed = -1.0
        signal_values_available = True
        monotonic = True
        for level in request.levels:
            cells = grouped[(resource, level)]
            observed_values = [float(cell["observed_pressure"]) for cell in cells]
            signal_values = [
                float(cell["hardware_signal"])
                for cell in cells
                if cell["hardware_signal"] is not None
            ]
            observed_mean = statistics.fmean(observed_values) if observed_values else None
            observed_std = _sample_std(observed_values)
            applied = level * request.pressure_caps[resource]
            abs_error = abs(observed_mean - applied) if observed_mean is not None else None
            if observed_mean is None or observed_mean + 1e-9 < previous_observed:
                monotonic = False
            if observed_mean is not None:
                previous_observed = observed_mean
            if len(signal_values) != request.repeats:
                signal_values_available = False
            points.append(
                {
                    "pressure_requested": level,
                    "pressure_applied": applied,
                    "repeat_count": len(cells),
                    "observed_pressure_mean": observed_mean,
                    "observed_pressure_std": observed_std,
                    "abs_error": abs_error,
                    "hardware_signal_mean": statistics.fmean(signal_values)
                    if signal_values
                    else None,
                    "hardware_signal_std": _sample_std(signal_values),
                    "samples_per_repeat": [int(cell["sample_count"]) for cell in cells],
                }
            )
        max_abs_error = max(
            (float(point["abs_error"]) for point in points if point["abs_error"] is not None),
            default=None,
        )
        checks = {
            "all_levels_have_repeats": all(
                point["repeat_count"] == request.repeats for point in points
            ),
            "observed_pressure_monotonic": monotonic,
            "max_abs_error_at_most_0_05": max_abs_error is not None and max_abs_error <= 0.05,
            "hardware_signal_available": signal_values_available,
        }
        passed = all(checks.values())
        all_checks_passed = all_checks_passed and passed
        resources.append(
            {
                "resource": resource,
                "actuation_kind": "allocated_memory_fraction"
                if resource == "gpu_memory"
                else "measured_active_duty_fraction",
                "hardware_signal": {
                    "cpu_compute": "cpu_util_pct",
                    "memory_bandwidth": "cpu_util_pct",
                    "gpu_compute": "gpu_util_pct",
                    "gpu_memory": "gpu_mem_used_bytes",
                }[resource],
                "max_abs_error": max_abs_error,
                "checks": checks,
                "status": "passed" if passed else "failed",
                "points": points,
            }
        )

    worker_records = []
    for record in records:
        safe_record = dict(record)
        safe_record["worker_directory"] = _repo_relative(repo_root, Path(record["worker_directory"]))
        worker_records.append(safe_record)
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "status": "passed" if all_checks_passed else "failed",
        "config_sha256": config_hash,
        "environment": environment,
        "environment_sha256": config_sha256(environment),
        "execution": _execution_provenance(repo_root),
        "resources": resources,
        "cell_count": len(records),
        "expected_cell_count": len(request.resources) * len(request.levels) * request.repeats,
        "request": {
            "timing_semantics": CALIBRATION_TIMING_SEMANTICS,
            "benchmark_protocol": request.benchmark_protocol,
            "resources": list(request.resources),
            "levels": list(request.levels),
            "pressure_caps": dict(request.pressure_caps),
            "max_gpu_temp_c": request.max_gpu_temp_c,
            "repeats": request.repeats,
            "warmup_s": request.warmup_s,
            "duration_s": request.duration_s,
            "sample_interval_s": request.sample_interval_s,
            "gpu_index": request.gpu_index,
            "cpu_workers": request.cpu_workers,
            "memory_buffer_mib": request.memory_buffer_mib,
            "gpu_matrix_size": request.gpu_matrix_size,
            "gpu_memory_max_mib": request.gpu_memory_max_mib,
        },
        "runs": worker_records,
        "artifacts": {
            "metrics": _repo_relative(repo_root, request.metrics_file),
            "workers": _repo_relative(repo_root, request.workers_root),
            "plot": _repo_relative(repo_root, request.plot_file),
        },
    }


def denominator_cells(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从 calibration run 重算 16 个非零吞吐分母及样本 CV。"""

    grouped: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for run in payload.get("runs", []):
        pressure = float(run.get("pressure_requested", -1))
        if pressure > 0:
            grouped[(str(run.get("resource")), pressure)].append(run)
    cells: list[dict[str, Any]] = []
    for (resource, pressure), runs in sorted(grouped.items()):
        throughputs = [
            int(run["worker"]["operations"]) / float(run["worker"]["elapsed_s"])
            for run in runs
        ]
        mean = statistics.fmean(throughputs)
        cv = _sample_std(throughputs) / mean * 100
        cells.append(
            {
                "resource": resource,
                "pressure_requested": pressure,
                "run_keys": [str(run["run_key"]) for run in runs],
                "throughputs_ops_per_s": throughputs,
                "throughput_mean_ops_per_s": mean,
                "throughput_sample_std_ops_per_s": _sample_std(throughputs),
                "throughput_cv_pct": cv,
            }
        )
    return cells


def plan_calibration_confirmation(
    *,
    repo_root: Path,
    calibration_file: Path,
    output_file: Path,
    metrics_file: Path,
    status_file: Path,
    workers_root: Path,
    cv_threshold_pct: float = 5.0,
    eligibility_ceiling_pct: float = 10.0,
    additional_repeats: int = 2,
) -> dict[str, Any]:
    """只读确定追加集合；失败集合由 base calibration 唯一决定，禁止手选。"""

    payload = json.loads(calibration_file.read_text(encoding="utf-8"))
    if (
        payload.get("status") != "passed"
        or payload.get("cell_count") != 60
        or payload.get("request", {}).get("timing_semantics")
        != CALIBRATION_TIMING_SEMANTICS
    ):
        raise ValueError("base calibration 状态、cell 数或 timing_semantics 不兼容")
    cells = denominator_cells(payload)
    if len(cells) != 16 or any(len(cell["run_keys"]) != 3 for cell in cells):
        raise ValueError("base calibration 必须完整包含 16 个三重复非零分母")
    selected = [cell for cell in cells if float(cell["throughput_cv_pct"]) > cv_threshold_pct]
    if not selected:
        raise ValueError("base calibration 没有需要追加确认的分母")
    if any(float(cell["throughput_cv_pct"]) > eligibility_ceiling_pct for cell in selected):
        raise ValueError("存在超过追加确认适用上限的分母，必须拒绝整个 calibration")
    if additional_repeats != 2:
        raise ValueError("正式确认协议固定追加两个重复")
    return {
        "schema_version": CONFIRMATION_SCHEMA_VERSION,
        "command": "benchmark confirm-calibration",
        "dry_run": True,
        "base_calibration": _repo_relative(repo_root, calibration_file),
        "base_calibration_sha256": _file_sha256(calibration_file),
        "selection_rule": {
            "cv_sample_std": True,
            "cv_threshold_pct": cv_threshold_pct,
            "eligibility_ceiling_pct": eligibility_ceiling_pct,
            "additional_repeats": additional_repeats,
            "combined_repeat_count": 3 + additional_repeats,
            "selection": "all and only base cells with threshold < CV <= ceiling",
        },
        "selected_cell_count": len(selected),
        "additional_cell_count": len(selected) * additional_repeats,
        "selected_cells": selected,
        "outputs": {
            "confirmation": _repo_relative(repo_root, output_file),
            "metrics": _repo_relative(repo_root, metrics_file),
            "status": _repo_relative(repo_root, status_file),
            "workers": _repo_relative(repo_root, workers_root),
        },
    }


def run_calibration_confirmation(
    *,
    repo_root: Path,
    config_path: Path,
    calibration_file: Path,
    output_file: Path,
    metrics_file: Path,
    status_file: Path,
    workers_root: Path,
    cv_threshold_pct: float = 5.0,
    eligibility_ceiling_pct: float = 10.0,
    additional_repeats: int = 2,
) -> dict[str, Any]:
    """仅为窄幅失败分母追加 r04/r05，并用全部五次重复重新验收。"""

    for path in (output_file, metrics_file, status_file, workers_root):
        _inside_repo(repo_root, path)
        if path.exists():
            raise FileExistsError(f"确认输出已存在，拒绝覆盖: {path}")
    plan = plan_calibration_confirmation(
        repo_root=repo_root,
        calibration_file=calibration_file,
        output_file=output_file,
        metrics_file=metrics_file,
        status_file=status_file,
        workers_root=workers_root,
        cv_threshold_pct=cv_threshold_pct,
        eligibility_ceiling_pct=eligibility_ceiling_pct,
        additional_repeats=additional_repeats,
    )
    base = json.loads(calibration_file.read_text(encoding="utf-8"))
    request_data = base["request"]
    local_config = load_local_config(config_path)
    if config_sha256(local_config.model_dump(mode="json")) != base.get("config_sha256"):
        raise ValueError("confirmation config 与 base calibration 不一致")
    environment = _environment_fingerprint(int(request_data["gpu_index"]))
    if config_sha256(environment) != base.get("environment_sha256"):
        raise ValueError("confirmation 环境指纹与 base calibration 不一致")
    request = CalibrationRequest(
        config_path=config_path,
        resources=tuple(request_data["resources"]),  # type: ignore[arg-type]
        levels=tuple(float(value) for value in request_data["levels"]),
        repeats=3,
        warmup_s=float(request_data["warmup_s"]),
        duration_s=float(request_data["duration_s"]),
        sample_interval_s=float(request_data["sample_interval_s"]),
        gpu_index=int(request_data["gpu_index"]),
        cpu_workers=int(request_data["cpu_workers"]),
        memory_buffer_mib=int(request_data["memory_buffer_mib"]),
        gpu_matrix_size=int(request_data["gpu_matrix_size"]),
        gpu_memory_max_mib=int(request_data["gpu_memory_max_mib"]),
        output_file=output_file,
        metrics_file=metrics_file,
        status_file=status_file,
        workers_root=workers_root,
        pressure_caps={key: float(value) for key, value in request_data["pressure_caps"].items()},
        max_gpu_temp_c=float(request_data["max_gpu_temp_c"]),
    )
    workers_root.mkdir(parents=True, exist_ok=False)
    started_wall_ns = time.time_ns()
    write_json_atomic(
        status_file,
        {
            "schema_version": CONFIRMATION_SCHEMA_VERSION,
            "status": "running",
            "started_wall_time_ns": started_wall_ns,
            "base_calibration_sha256": plan["base_calibration_sha256"],
        },
    )
    records: list[dict[str, Any]] = []
    try:
        with JsonlWriter(metrics_file, batch_size=10) as writer:
            for cell in plan["selected_cells"]:
                resource = str(cell["resource"])
                level = float(cell["pressure_requested"])
                for repeat in (4, 5):
                    records.append(
                        _run_cell(
                            request=request,
                            resource=resource,  # type: ignore[arg-type]
                            level=level,
                            repeat=repeat,
                            writer=writer,
                        )
                    )
        combined_cells = []
        for selected in plan["selected_cells"]:
            key = (str(selected["resource"]), float(selected["pressure_requested"]))
            extra = [
                run
                for run in records
                if (str(run["resource"]), float(run["pressure_requested"])) == key
            ]
            extra_throughputs = [
                int(run["worker"]["operations"]) / float(run["worker"]["elapsed_s"])
                for run in extra
            ]
            combined = [*selected["throughputs_ops_per_s"], *extra_throughputs]
            mean = statistics.fmean(combined)
            cv = _sample_std(combined) / mean * 100
            combined_cells.append(
                {
                    "resource": key[0],
                    "pressure_requested": key[1],
                    "base_run_keys": selected["run_keys"],
                    "confirmation_run_keys": [run["run_key"] for run in extra],
                    "base_throughputs_ops_per_s": selected["throughputs_ops_per_s"],
                    "confirmation_throughputs_ops_per_s": extra_throughputs,
                    "combined_throughputs_ops_per_s": combined,
                    "combined_throughput_mean_ops_per_s": mean,
                    "combined_throughput_sample_std_ops_per_s": _sample_std(combined),
                    "combined_throughput_cv_pct": cv,
                    "status": "passed" if math.isfinite(cv) and cv <= cv_threshold_pct else "failed",
                }
            )
        safe_records = []
        for record in records:
            safe = dict(record)
            safe["worker_directory"] = _repo_relative(repo_root, Path(record["worker_directory"]))
            safe_records.append(safe)
        passed = all(cell["status"] == "passed" for cell in combined_cells)
        result = {
            "schema_version": CONFIRMATION_SCHEMA_VERSION,
            "status": "passed" if passed else "failed",
            "base_calibration": plan["base_calibration"],
            "base_calibration_sha256": plan["base_calibration_sha256"],
            "environment": environment,
            "environment_sha256": config_sha256(environment),
            "timing_semantics": CALIBRATION_TIMING_SEMANTICS,
            "selection_rule": plan["selection_rule"],
            "selected_cell_count": plan["selected_cell_count"],
            "additional_cell_count": len(records),
            "combined_cells": combined_cells,
            "runs": safe_records,
            "artifacts": {
                "metrics": _repo_relative(repo_root, metrics_file),
                "metrics_sha256": _file_sha256(metrics_file),
                "workers": _repo_relative(repo_root, workers_root),
            },
        }
        write_json_atomic(output_file, result)
        write_json_atomic(
            status_file,
            {
                "schema_version": CONFIRMATION_SCHEMA_VERSION,
                "status": "completed" if passed else "failed",
                "started_wall_time_ns": started_wall_ns,
                "finished_wall_time_ns": time.time_ns(),
                "base_calibration_sha256": plan["base_calibration_sha256"],
                "confirmation_file": _repo_relative(repo_root, output_file),
            },
        )
        return result
    except BaseException as exc:
        write_json_atomic(
            status_file,
            {
                "schema_version": CONFIRMATION_SCHEMA_VERSION,
                "status": "failed",
                "started_wall_time_ns": started_wall_ns,
                "finished_wall_time_ns": time.time_ns(),
                "base_calibration_sha256": plan["base_calibration_sha256"],
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise


def _write_plot(result: dict[str, Any], output_file: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - 由正式 Conda 环境验证。
        raise RuntimeError("--plot 需要 matplotlib") from exc
    output_file.parent.mkdir(parents=True, exist_ok=True)
    figure, axes = plt.subplots(2, 2, figsize=(11, 8), constrained_layout=True)
    for axis, resource_result in zip(axes.flat, result["resources"], strict=True):
        points = resource_result["points"]
        requested = [point["pressure_requested"] for point in points]
        applied = [point["pressure_applied"] for point in points]
        observed = [point["observed_pressure_mean"] for point in points]
        error = [point["observed_pressure_std"] for point in points]
        axis.plot(requested, applied, "--", color="gray", label="expected applied")
        axis.errorbar(requested, observed, yerr=error, marker="o", capsize=3, label="observed")
        axis.set_title(resource_result["resource"])
        axis.set_xlabel("requested pressure")
        axis.set_ylabel("observed pressure")
        axis.set_xlim(-0.05, 1.05)
        axis.set_ylim(-0.05, 1.05)
        axis.grid(alpha=0.25)
        axis.legend(loc="best")
    figure.savefig(output_file, dpi=160)
    plt.close(figure)


def run_calibration(*, repo_root: Path, request: CalibrationRequest) -> dict[str, Any]:
    """顺序运行所有 cell；任一失败保留原始 JSONL、worker 日志和 failed status。"""

    request.validate(repo_root)
    local_config = load_local_config(request.config_path)
    if dict(local_config.measurement.pressure_caps) != request.pressure_caps:
        raise ValueError("CalibrationRequest pressure_caps 与主机配置不一致")
    if local_config.host.max_gpu_temp_c != request.max_gpu_temp_c:
        raise ValueError("CalibrationRequest max_gpu_temp_c 与主机配置不一致")
    config_hash = config_sha256(local_config.model_dump(mode="json"))
    environment = _environment_fingerprint(request.gpu_index)
    request.workers_root.mkdir(parents=True, exist_ok=False)
    started_wall_ns = time.time_ns()
    write_json_atomic(
        request.status_file,
        {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "status": "running",
            "started_wall_time_ns": started_wall_ns,
            "config_sha256": config_hash,
        },
    )
    records: list[dict[str, Any]] = []
    try:
        with JsonlWriter(request.metrics_file, batch_size=10) as writer:
            for resource in request.resources:
                for level in request.levels:
                    for repeat in range(1, request.repeats + 1):
                        records.append(
                            _run_cell(
                                request=request,
                                resource=resource,
                                level=level,
                                repeat=repeat,
                                writer=writer,
                            )
                        )
        result = summarize_calibration_records(
            request=request,
            records=records,
            repo_root=repo_root,
            config_hash=config_hash,
            environment=environment,
        )
        result["artifacts"]["metrics_sha256"] = _file_sha256(request.metrics_file)
        if request.plot_file is not None:
            _write_plot(result, request.plot_file)
            result["artifacts"]["plot_sha256"] = _file_sha256(request.plot_file)
        write_json_atomic(request.output_file, result)
        write_json_atomic(
            request.status_file,
            {
                "schema_version": CALIBRATION_SCHEMA_VERSION,
                "status": "completed" if result["status"] == "passed" else "failed",
                "started_wall_time_ns": started_wall_ns,
                "finished_wall_time_ns": time.time_ns(),
                "config_sha256": config_hash,
                "calibration_file": _repo_relative(repo_root, request.output_file),
                "cell_count": len(records),
                "error_type": None if result["status"] == "passed" else "QualityGateFailed",
            },
        )
        return result
    except BaseException as exc:
        write_json_atomic(
            request.status_file,
            {
                "schema_version": CALIBRATION_SCHEMA_VERSION,
                "status": "failed",
                "started_wall_time_ns": started_wall_ns,
                "finished_wall_time_ns": time.time_ns(),
                "config_sha256": config_hash,
                "completed_cell_count": len(records),
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:1000],
            },
        )
        raise


def verify_calibration(*, repo_root: Path, calibration_file: Path) -> dict[str, Any]:
    """只读验证校准 JSON、原始 JSONL 哈希与每资源质量门。"""

    calibration_path = _inside_repo(repo_root, calibration_file)
    result = json.loads(calibration_path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    checks.append(
        {
            "name": "schema_version",
            "passed": result.get("schema_version") == CALIBRATION_SCHEMA_VERSION,
            "actual": result.get("schema_version"),
            "expected": CALIBRATION_SCHEMA_VERSION,
        }
    )
    checks.append(
        {
            "name": "calibration_status",
            "passed": result.get("status") == "passed",
            "actual": result.get("status"),
            "expected": "passed",
        }
    )
    expected_cells = result.get("expected_cell_count")
    checks.append(
        {
            "name": "cell_count",
            "passed": result.get("cell_count") == expected_cells,
            "actual": result.get("cell_count"),
            "expected": expected_cells,
        }
    )
    resources = result.get("resources", [])
    checks.append(
        {
            "name": "resource_count",
            "passed": len(resources) == len(result.get("request", {}).get("resources", [])),
            "actual": len(resources),
            "expected": len(result.get("request", {}).get("resources", [])),
        }
    )
    for resource in resources:
        checks.append(
            {
                "name": f"{resource.get('resource')}_quality_gate",
                "passed": resource.get("status") == "passed"
                and all(resource.get("checks", {}).values()),
                "actual": resource.get("checks"),
                "expected": True,
            }
        )
    artifacts = result.get("artifacts", {})
    metrics_relative = artifacts.get("metrics")
    metrics_path = _inside_repo(repo_root, repo_root / metrics_relative) if metrics_relative else None
    metrics_hash = _file_sha256(metrics_path) if metrics_path is not None and metrics_path.is_file() else None
    checks.append(
        {
            "name": "metrics_sha256",
            "passed": metrics_hash == artifacts.get("metrics_sha256"),
            "actual": metrics_hash,
            "expected": artifacts.get("metrics_sha256"),
        }
    )
    if result.get("request", {}).get("benchmark_protocol") == STABLE_BENCHMARK_PROTOCOL:
        request = result["request"]
        checks.append(
            {
                "name": "stable_benchmark_protocol",
                "passed": request.get("repeats") == STABLE_CALIBRATION_REPEATS
                and request.get("warmup_s") == STABLE_CALIBRATION_WARMUP_S
                and request.get("duration_s") == STABLE_CALIBRATION_DURATION_S
                and all(
                    stable_benchmark_environment_valid(
                        run.get("worker", {}).get("benchmark_environment")
                    )
                    for run in result.get("runs", [])
                ),
                "actual": request.get("benchmark_protocol"),
                "expected": STABLE_BENCHMARK_PROTOCOL,
            }
        )
        execution = result.get("execution", {})
        checks.append(
            {
                "name": "clean_execution_provenance",
                "passed": execution.get("root_dirty_at_execution") is False
                and bool(execution.get("root_commit"))
                and bool(execution.get("source_tree_sha256")),
                "actual": execution,
                "expected": "clean commit and bound source tree",
            }
        )
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "calibration": _repo_relative(repo_root, calibration_path),
        "checks": checks,
        "calibration_sha256": _file_sha256(calibration_path),
    }


def format_calibration_result(result: dict[str, Any]) -> str:
    return stable_json_dumps(result, indent=2)
