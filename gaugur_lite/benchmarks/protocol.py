"""正式 benchmark 子进程的可审计原生线程合同。"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

CANDIDATE003_BENCHMARK_PROTOCOL = "native_threads_1_warmup5_duration15_repeats5_v1"
STABLE_BENCHMARK_PROTOCOL = "native_threads_1_warmup5_duration15_repeats5_cv10_v2"
SUPPORTED_STABLE_BENCHMARK_PROTOCOLS = frozenset(
    {CANDIDATE003_BENCHMARK_PROTOCOL, STABLE_BENCHMARK_PROTOCOL}
)
STABLE_CALIBRATION_WARMUP_S = 5.0
STABLE_CALIBRATION_DURATION_S = 15.0
STABLE_CALIBRATION_REPEATS = 5
STABLE_DENOMINATOR_CV_THRESHOLD_PCT = 10.0
NATIVE_THREAD_LIMIT = 1
NATIVE_THREAD_ENV_KEYS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
PROTOCOL_ENV_KEY = "GAUGUR_BENCHMARK_PROTOCOL"


def apply_stable_benchmark_environment(
    environment: MutableMapping[str, str],
    *,
    protocol: str = STABLE_BENCHMARK_PROTOCOL,
) -> None:
    """在启动 Python 子进程前固定所有常见原生数学线程池。"""

    if protocol not in SUPPORTED_STABLE_BENCHMARK_PROTOCOLS:
        raise ValueError(f"未知 stable benchmark protocol: {protocol}")
    for key in NATIVE_THREAD_ENV_KEYS:
        environment[key] = str(NATIVE_THREAD_LIMIT)
    environment[PROTOCOL_ENV_KEY] = protocol


def benchmark_environment_snapshot(
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """由 worker 记录实际继承的合同，供校准和正式结果独立复核。"""

    source = os.environ if environment is None else environment
    values = {key: source.get(key) for key in NATIVE_THREAD_ENV_KEYS}
    return {
        "protocol": source.get(PROTOCOL_ENV_KEY),
        "native_thread_limit": NATIVE_THREAD_LIMIT
        if all(value == str(NATIVE_THREAD_LIMIT) for value in values.values())
        else None,
        "environment": values,
    }


def stable_benchmark_environment_valid(
    payload: object,
    *,
    expected_protocol: str | None = None,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    values = payload.get("environment")
    actual_protocol = payload.get("protocol")
    return (
        actual_protocol in SUPPORTED_STABLE_BENCHMARK_PROTOCOLS
        and (expected_protocol is None or actual_protocol == expected_protocol)
        and payload.get("native_thread_limit") == NATIVE_THREAD_LIMIT
        and isinstance(values, dict)
        and set(values) == set(NATIVE_THREAD_ENV_KEYS)
        and all(value == str(NATIVE_THREAD_LIMIT) for value in values.values())
    )
