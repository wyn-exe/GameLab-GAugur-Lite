"""四类资源压力器的独立 worker 实现。"""

from __future__ import annotations

import gc
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from ..config import stable_json_dumps
from ..metrics.writer import write_json_atomic

BenchmarkResource = Literal["cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory"]
BENCHMARK_RESOURCES: tuple[BenchmarkResource, ...] = (
    "cpu_compute",
    "memory_bandwidth",
    "gpu_compute",
    "gpu_memory",
)
_DUTY_CYCLE_S = 0.25


@dataclass(frozen=True)
class BenchmarkWorkerConfig:
    """单个 worker 的不可变配置；runtime 包含 warmup 与测量窗口。"""

    resource: BenchmarkResource
    pressure: float
    runtime_s: float
    cpu_workers: int = 8
    memory_buffer_mib: int = 64
    gpu_matrix_size: int = 1024
    gpu_memory_max_mib: int = 1024

    def __post_init__(self) -> None:
        if self.resource not in BENCHMARK_RESOURCES:
            raise ValueError(f"未知 benchmark 资源: {self.resource}")
        if not math.isfinite(self.pressure) or not 0.0 <= self.pressure <= 1.0:
            raise ValueError("pressure 必须位于 [0, 1]")
        if not math.isfinite(self.runtime_s) or self.runtime_s <= 0:
            raise ValueError("runtime_s 必须大于 0")
        if not 1 <= self.cpu_workers <= 64:
            raise ValueError("cpu_workers 必须位于 [1, 64]")
        if not 8 <= self.memory_buffer_mib <= 4096:
            raise ValueError("memory_buffer_mib 必须位于 [8, 4096]")
        if not 128 <= self.gpu_matrix_size <= 4096:
            raise ValueError("gpu_matrix_size 必须位于 [128, 4096]")
        if not 64 <= self.gpu_memory_max_mib <= 12288:
            raise ValueError("gpu_memory_max_mib 必须位于 [64, 12288]")


class _Load(Protocol):
    allocated_bytes: int
    capacity_bytes: int

    def work_once(self) -> int: ...

    def close(self) -> None: ...


class _NoopLoad:
    allocated_bytes = 0
    capacity_bytes = 0

    def work_once(self) -> int:
        return 0

    def close(self) -> None:
        return None


class _CpuComputeLoad:
    """小矩阵乘法在多个原生 NumPy 调用中执行，避免 Python GIL 成为唯一瓶颈。"""

    allocated_bytes = 0
    capacity_bytes = 0

    def __init__(self, workers: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gaugur-cpu")
        generator = np.random.default_rng(20260815)
        self._matrices = [
            generator.random((256, 256), dtype=np.float32) for _ in range(workers)
        ]

    @staticmethod
    def _multiply(matrix: np.ndarray) -> int:
        # 输入矩阵保持固定，避免连续回写导致浮点数指数增长而溢出。
        result = matrix @ matrix
        return int(result.size)

    def work_once(self) -> int:
        return sum(self._executor.map(self._multiply, self._matrices))

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._matrices.clear()


class _MemoryBandwidthLoad:
    """独立的大数组原地读改写，计数以读写字节数表示。"""

    capacity_bytes = 0

    def __init__(self, workers: int, buffer_mib: int) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gaugur-memory")
        elements = buffer_mib * 1024 * 1024 // np.dtype(np.float32).itemsize
        self._buffers = [np.ones(elements, dtype=np.float32) for _ in range(workers)]
        self.allocated_bytes = sum(buffer.nbytes for buffer in self._buffers)

    @staticmethod
    def _stream(buffer: np.ndarray) -> int:
        np.multiply(buffer, np.float32(1.000001), out=buffer)
        return int(buffer.nbytes * 2)

    def work_once(self) -> int:
        return sum(self._executor.map(self._stream, self._buffers))

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._buffers.clear()
        gc.collect()


class _GpuComputeLoad:
    allocated_bytes = 0
    capacity_bytes = 0

    def __init__(self, matrix_size: int) -> None:
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - 依赖由运行环境决定。
            raise RuntimeError("gpu_compute 需要 PyTorch CUDA wheel") from exc
        if not torch.cuda.is_available():
            raise RuntimeError("gpu_compute 需要可用的 CUDA GPU")
        self._torch = torch
        torch.cuda.set_device(0)
        self._left = torch.randn((matrix_size, matrix_size), device="cuda", dtype=torch.float32)
        self._right = torch.randn((matrix_size, matrix_size), device="cuda", dtype=torch.float32)
        self._output = torch.empty_like(self._left)
        torch.cuda.synchronize()

    def work_once(self) -> int:
        self._torch.mm(self._left, self._right, out=self._output)
        self._torch.cuda.synchronize()
        return int(self._left.numel())

    def close(self) -> None:
        del self._left, self._right, self._output
        self._torch.cuda.empty_cache()


class _GpuMemoryLoad:
    def __init__(self, pressure: float, max_mib: int) -> None:
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - 依赖由运行环境决定。
            raise RuntimeError("gpu_memory 需要 PyTorch CUDA wheel") from exc
        if not torch.cuda.is_available():
            raise RuntimeError("gpu_memory 需要可用的 CUDA GPU")
        self._torch = torch
        torch.cuda.set_device(0)
        self.capacity_bytes = max_mib * 1024 * 1024
        requested_bytes = int(round(self.capacity_bytes * pressure))
        if requested_bytes == 0:
            self._buffer = None
            self.allocated_bytes = 0
        else:
            count = max(1, requested_bytes // 4)
            self._buffer = torch.empty(count, device="cuda", dtype=torch.float32)
            self._buffer.fill_(1.0)
            self.allocated_bytes = int(self._buffer.numel() * self._buffer.element_size())
        torch.cuda.synchronize()

    def work_once(self) -> int:
        if self._buffer is None:
            return 0
        self._buffer.add_(0.000001)
        self._torch.cuda.synchronize()
        return int(self._buffer.numel() * self._buffer.element_size() * 2)

    def close(self) -> None:
        if self._buffer is not None:
            del self._buffer
        self._torch.cuda.empty_cache()


def _create_load(config: BenchmarkWorkerConfig) -> _Load:
    if config.pressure == 0:
        return _NoopLoad()
    if config.resource == "cpu_compute":
        return _CpuComputeLoad(config.cpu_workers)
    if config.resource == "memory_bandwidth":
        return _MemoryBandwidthLoad(config.cpu_workers, config.memory_buffer_mib)
    if config.resource == "gpu_compute":
        return _GpuComputeLoad(config.gpu_matrix_size)
    return _GpuMemoryLoad(config.pressure, config.gpu_memory_max_mib)


def run_benchmark_worker(
    *,
    config: BenchmarkWorkerConfig,
    ready_file: str | Path,
    status_file: str | Path,
) -> dict[str, object]:
    """运行到给定时长并落盘 ready/status，父进程据此精确采样与回收。"""

    ready_path = Path(ready_file)
    status_path = Path(status_file)
    started_wall_ns = time.time_ns()
    load: _Load | None = None
    try:
        load = _create_load(config)
        write_json_atomic(
            ready_path,
            {
                "schema_version": 1,
                "status": "ready",
                "pid": os.getpid(),
                "resource": config.resource,
                "pressure_requested": config.pressure,
                "wall_time_ns": time.time_ns(),
            },
        )
        started = time.perf_counter()
        deadline = started + config.runtime_s
        active_s = 0.0
        sleep_s = 0.0
        operations = 0
        while time.perf_counter() < deadline:
            cycle_started = time.perf_counter()
            active_deadline = min(deadline, cycle_started + _DUTY_CYCLE_S * config.pressure)
            while time.perf_counter() < active_deadline:
                work_started = time.perf_counter()
                operations += load.work_once()
                active_s += time.perf_counter() - work_started
            remaining = min(deadline, cycle_started + _DUTY_CYCLE_S) - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
                sleep_s += remaining
        elapsed_s = time.perf_counter() - started
        result: dict[str, object] = {
            "schema_version": 1,
            "status": "completed",
            "pid": os.getpid(),
            "resource": config.resource,
            "pressure_requested": config.pressure,
            "started_wall_time_ns": started_wall_ns,
            "finished_wall_time_ns": time.time_ns(),
            "elapsed_s": elapsed_s,
            "active_s": active_s,
            "sleep_s": sleep_s,
            "active_fraction": active_s / elapsed_s if elapsed_s else 0.0,
            "operations": operations,
            "allocated_bytes": load.allocated_bytes,
            "capacity_bytes": load.capacity_bytes,
            "duty_cycle_s": _DUTY_CYCLE_S,
        }
        write_json_atomic(status_path, result)
        return result
    except BaseException as exc:
        failure = {
            "schema_version": 1,
            "status": "failed",
            "pid": os.getpid(),
            "resource": config.resource,
            "pressure_requested": config.pressure,
            "started_wall_time_ns": started_wall_ns,
            "finished_wall_time_ns": time.time_ns(),
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:1000],
        }
        write_json_atomic(status_path, failure)
        raise
    finally:
        if load is not None:
            load.close()


def worker_result_text(result: dict[str, object]) -> str:
    """隐藏 CLI 子命令使用的稳定文本，方便单独诊断。"""

    return stable_json_dumps(result, indent=2)
