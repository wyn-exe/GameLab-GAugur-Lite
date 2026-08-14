"""GAugur-Lite Windows 轻量复现实验包。"""

from .schema import (
    HostSpec,
    MetricEvent,
    RunSpec,
    RunStatus,
    SystemMetricEvent,
    TelemetryStatus,
    WorkloadSpec,
)

__all__ = [
    "HostSpec",
    "MetricEvent",
    "RunSpec",
    "RunStatus",
    "SystemMetricEvent",
    "TelemetryStatus",
    "WorkloadSpec",
]
__version__ = "0.1.0"
