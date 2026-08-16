from __future__ import annotations

from types import SimpleNamespace

import pytest

from gaugur_lite.metrics import system_sampler
from gaugur_lite.metrics.summarize import summarize_events
from gaugur_lite.schema import SystemMetricEvent


def _event(sequence: int, monotonic_ns: int, wall_ns: int) -> SystemMetricEvent:
    return SystemMetricEvent(
        run_id="step2-test",
        wall_time_ns=wall_ns,
        monotonic_time_ns=monotonic_ns,
        sequence=sequence,
        process_pid=1,
        cpu_util_pct=10,
        cpu_freq_mhz=2000,
        ram_used_bytes=100,
        ram_available_bytes=200,
        process_cpu_util_pct=1,
        process_rss_bytes=50,
        gpu_util_pct=2,
        gpu_mem_util_pct=3,
        gpu_mem_used_bytes=400,
        gpu_clock_mhz=500,
        gpu_power_w=6,
        gpu_temp_c=50,
        gpu_clock_event_reasons=0,
        gpu_thermal_slowdown_active=False,
    )


def test_summary_checks_timestamps_and_intervals() -> None:
    events = [
        _event(0, 1_000_000_000, 10_000_000_000),
        _event(1, 2_000_000_000, 11_000_000_000),
        _event(2, 3_020_000_000, 12_020_000_000),
    ]
    summary = summarize_events(events, requested_interval_s=1.0)

    assert summary["sample_count"] == 3
    assert summary["monotonic_non_decreasing"] is True
    assert summary["wall_non_decreasing"] is True
    assert summary["interval_s"]["min"] == 1.0
    assert summary["interval_s"]["max"] == pytest.approx(1.02)
    assert summary["interval_abs_error_s"]["max"] == pytest.approx(0.02)
    assert summary["sequence_contiguous"] is True
    assert summary["quality_gate"]["status"] == "passed"


def test_summary_detects_clock_regression() -> None:
    events = [_event(0, 2, 20), _event(1, 1, 19)]
    summary = summarize_events(events, requested_interval_s=1.0)
    assert summary["monotonic_non_decreasing"] is False
    assert summary["wall_non_decreasing"] is False
    assert set(summary["quality_gate"]["failed_checks"]) == {
        "monotonic_non_decreasing",
        "wall_non_decreasing",
        "interval_p95_abs_error_s",
    }


def test_system_sampler_uses_psutil_and_nvml_without_real_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"init": 0, "shutdown": 0}
    fake_process = SimpleNamespace(
        pid=123,
        cpu_percent=lambda interval=None: 4.0,
        memory_info=lambda: SimpleNamespace(rss=456),
    )
    monkeypatch.setattr(system_sampler.psutil, "Process", lambda pid: fake_process)
    monkeypatch.setattr(system_sampler.psutil, "cpu_percent", lambda interval=None: 5.0)
    monkeypatch.setattr(system_sampler.psutil, "cpu_freq", lambda: SimpleNamespace(current=2500.0))
    monkeypatch.setattr(
        system_sampler.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(used=1000, available=2000),
    )
    monkeypatch.setattr(system_sampler, "nvmlInit", lambda: calls.__setitem__("init", 1))
    monkeypatch.setattr(system_sampler, "nvmlShutdown", lambda: calls.__setitem__("shutdown", 1))
    monkeypatch.setattr(system_sampler, "nvmlDeviceGetHandleByIndex", lambda index: f"gpu-{index}")
    monkeypatch.setattr(
        system_sampler,
        "nvmlDeviceGetUtilizationRates",
        lambda handle: SimpleNamespace(gpu=6, memory=7),
    )
    monkeypatch.setattr(
        system_sampler,
        "nvmlDeviceGetMemoryInfo",
        lambda handle: SimpleNamespace(used=3000),
    )
    monkeypatch.setattr(system_sampler, "nvmlDeviceGetClockInfo", lambda handle, clock: 800)
    monkeypatch.setattr(system_sampler, "nvmlDeviceGetPowerUsage", lambda handle: 9000)
    monkeypatch.setattr(system_sampler, "nvmlDeviceGetTemperature", lambda handle, sensor: 55)
    monkeypatch.setattr(
        system_sampler,
        "nvmlDeviceGetCurrentClocksEventReasons",
        lambda handle: system_sampler.nvmlClocksEventReasonSwThermalSlowdown,
    )

    with system_sampler.SystemSampler(run_id="step2-test", process_pid=123) as sampler:
        event = sampler.sample(0)

    assert event.process_pid == 123
    assert event.gpu_util_pct == 6
    assert event.gpu_mem_used_bytes == 3000
    assert event.gpu_power_w == 9
    assert event.gpu_thermal_slowdown_active is True
    assert calls == {"init": 1, "shutdown": 1}
