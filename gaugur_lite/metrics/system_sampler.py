"""psutil + NVML 的低开销系统采样。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import psutil

from ..schema import SystemMetricEvent

try:
    from pynvml import (
        NVMLError,
        NVML_CLOCK_GRAPHICS,
        NVML_TEMPERATURE_GPU,
        nvmlDeviceGetClockInfo,
        nvmlDeviceGetCurrentClocksEventReasons,
        nvmlDeviceGetHandleByIndex,
        nvmlDeviceGetMemoryInfo,
        nvmlDeviceGetPowerUsage,
        nvmlDeviceGetTemperature,
        nvmlDeviceGetUtilizationRates,
        nvmlInit,
        nvmlShutdown,
        nvmlClocksEventReasonHwThermalSlowdown,
        nvmlClocksEventReasonSwThermalSlowdown,
    )
except (ImportError, OSError):  # 单元测试和无 NVIDIA 主机仍可导入模块。
    NVMLError = RuntimeError
    NVML_CLOCK_GRAPHICS = 0
    NVML_TEMPERATURE_GPU = 0
    nvmlDeviceGetClockInfo = None
    nvmlDeviceGetCurrentClocksEventReasons = None
    nvmlDeviceGetHandleByIndex = None
    nvmlDeviceGetMemoryInfo = None
    nvmlDeviceGetPowerUsage = None
    nvmlDeviceGetTemperature = None
    nvmlDeviceGetUtilizationRates = None
    nvmlInit = None
    nvmlShutdown = None
    nvmlClocksEventReasonHwThermalSlowdown = 0
    nvmlClocksEventReasonSwThermalSlowdown = 0


def _safe_nvml(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except (NVMLError, NotImplementedError):
        return None


class SystemSampler:
    """上下文内复用 psutil Process 和 NVML handle，避免每次重新初始化。"""

    def __init__(
        self,
        *,
        run_id: str,
        gpu_index: int = 0,
        process_pid: int | None = None,
        enable_gpu: bool = True,
    ) -> None:
        self.run_id = run_id
        self.gpu_index = gpu_index
        self.process = psutil.Process(process_pid or os.getpid())
        self.enable_gpu = enable_gpu
        self._gpu_handle: Any = None
        self._nvml_initialized = False

    def __enter__(self) -> "SystemSampler":
        psutil.cpu_percent(interval=None)
        self.process.cpu_percent(interval=None)
        if self.enable_gpu:
            if nvmlInit is None or nvmlDeviceGetHandleByIndex is None:
                raise RuntimeError("NVML 不可用，无法执行 GPU 系统采样")
            nvmlInit()
            self._nvml_initialized = True
            self._gpu_handle = nvmlDeviceGetHandleByIndex(self.gpu_index)
        return self

    def sample(self, sequence: int) -> SystemMetricEvent:
        # 连续读取两个时钟，事件排序一律使用 monotonic_time_ns。
        wall_time_ns = time.time_ns()
        monotonic_time_ns = time.monotonic_ns()
        virtual_memory = psutil.virtual_memory()
        cpu_frequency = psutil.cpu_freq()
        process_memory = self.process.memory_info()

        gpu_util = None
        gpu_memory = None
        gpu_clock = None
        gpu_power_mw = None
        gpu_temp = None
        gpu_clock_event_reasons = None
        if self._gpu_handle is not None:
            gpu_util = _safe_nvml(
                lambda: nvmlDeviceGetUtilizationRates(self._gpu_handle)  # type: ignore[misc]
            )
            gpu_memory = _safe_nvml(
                lambda: nvmlDeviceGetMemoryInfo(self._gpu_handle)  # type: ignore[misc]
            )
            gpu_clock = _safe_nvml(
                lambda: nvmlDeviceGetClockInfo(  # type: ignore[misc]
                    self._gpu_handle, NVML_CLOCK_GRAPHICS
                )
            )
            gpu_power_mw = _safe_nvml(
                lambda: nvmlDeviceGetPowerUsage(self._gpu_handle)  # type: ignore[misc]
            )
            gpu_temp = _safe_nvml(
                lambda: nvmlDeviceGetTemperature(  # type: ignore[misc]
                    self._gpu_handle, NVML_TEMPERATURE_GPU
                )
            )
            if nvmlDeviceGetCurrentClocksEventReasons is not None:
                gpu_clock_event_reasons = _safe_nvml(
                    lambda: nvmlDeviceGetCurrentClocksEventReasons(self._gpu_handle)  # type: ignore[misc]
                )

        thermal_mask = int(
            nvmlClocksEventReasonSwThermalSlowdown
            | nvmlClocksEventReasonHwThermalSlowdown
        )

        return SystemMetricEvent(
            run_id=self.run_id,
            wall_time_ns=wall_time_ns,
            monotonic_time_ns=monotonic_time_ns,
            sequence=sequence,
            process_pid=self.process.pid,
            cpu_util_pct=psutil.cpu_percent(interval=None),
            cpu_freq_mhz=cpu_frequency.current if cpu_frequency is not None else None,
            ram_used_bytes=virtual_memory.used,
            ram_available_bytes=virtual_memory.available,
            process_cpu_util_pct=self.process.cpu_percent(interval=None),
            process_rss_bytes=process_memory.rss,
            gpu_util_pct=getattr(gpu_util, "gpu", None),
            gpu_mem_util_pct=getattr(gpu_util, "memory", None),
            gpu_mem_used_bytes=getattr(gpu_memory, "used", None),
            gpu_clock_mhz=gpu_clock,
            gpu_power_w=(gpu_power_mw / 1000 if gpu_power_mw is not None else None),
            gpu_temp_c=gpu_temp,
            gpu_clock_event_reasons=(
                int(gpu_clock_event_reasons)
                if gpu_clock_event_reasons is not None
                else None
            ),
            gpu_thermal_slowdown_active=(
                bool(int(gpu_clock_event_reasons) & thermal_mask)
                if gpu_clock_event_reasons is not None
                else None
            ),
        )

    def __exit__(self, *_: Any) -> None:
        if self._nvml_initialized and nvmlShutdown is not None:
            nvmlShutdown()
        self._gpu_handle = None
        self._nvml_initialized = False
