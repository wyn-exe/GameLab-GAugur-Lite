"""批量 flush 的 JSONL writer 和原子 status.json。"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from pydantic import BaseModel

from ..config import stable_json_dumps
from ..schema import RunStatus, TelemetryStatus


_ATOMIC_REPLACE_ATTEMPTS = 10
_ATOMIC_REPLACE_INITIAL_DELAY_S = 0.02
_ATOMIC_REPLACE_MAX_DELAY_S = 0.25


class JsonlWriter:
    """逐行写入稳定 JSON，在批次边界 flush，异常退出也保留已有数据。"""

    def __init__(self, path: str | Path, *, batch_size: int = 10) -> None:
        if batch_size < 1:
            raise ValueError("batch_size 必须 >= 1")
        self.path = Path(path)
        self.batch_size = batch_size
        self.count = 0
        self._pending = 0
        self._stream: TextIO | None = None

    def __enter__(self) -> "JsonlWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 原始指标不可静默覆盖；重试应使用新的 run/output 目录。
        self._stream = self.path.open("x", encoding="utf-8", newline="\n")
        return self

    def write(self, event: BaseModel | dict[str, Any]) -> None:
        if self._stream is None:
            raise RuntimeError("JsonlWriter 必须在 with 块中使用")
        self._stream.write(stable_json_dumps(event) + "\n")
        self.count += 1
        self._pending += 1
        if self._pending >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if self._stream is not None and self._pending:
            self._stream.flush()
            self._pending = 0

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
            self._stream = None
            self._pending = 0


def _replace_with_retry(source: Path, target: Path) -> None:
    """Retry transient Windows locks while keeping replacement bounded."""

    delay_s = _ATOMIC_REPLACE_INITIAL_DELAY_S
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _ATOMIC_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay_s)
            delay_s = min(delay_s * 2, _ATOMIC_REPLACE_MAX_DELAY_S)


def write_json_atomic(path: str | Path, value: BaseModel | dict[str, Any]) -> None:
    """先写同目录临时文件，再原子替换，避免半截 status/summary JSON。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(stable_json_dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


class StatusTracker:
    """维护 running/completed/failed 状态，不触碰原始 JSONL。"""

    def __init__(self, path: str | Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.started_wall_time_ns = time.time_ns()
        self.samples_written = 0
        self.completed = False

    def __enter__(self) -> "StatusTracker":
        now = time.time_ns()
        self._write(
            TelemetryStatus(
                run_id=self.run_id,
                status=RunStatus.RUNNING,
                started_wall_time_ns=self.started_wall_time_ns,
                updated_wall_time_ns=max(now, self.started_wall_time_ns),
            )
        )
        return self

    def update_samples(self, samples_written: int) -> None:
        if samples_written < self.samples_written:
            raise ValueError("samples_written 不得倒退")
        self.samples_written = samples_written

    def mark_completed(self, *, samples_written: int, summary_file: str) -> None:
        self.update_samples(samples_written)
        now = max(time.time_ns(), self.started_wall_time_ns)
        self._write(
            TelemetryStatus(
                run_id=self.run_id,
                status=RunStatus.COMPLETED,
                started_wall_time_ns=self.started_wall_time_ns,
                updated_wall_time_ns=now,
                finished_wall_time_ns=now,
                samples_written=self.samples_written,
                summary_file=summary_file,
            )
        )
        self.completed = True

    def _write(self, status: TelemetryStatus) -> None:
        write_json_atomic(self.path, status)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del traceback
        if exc_type is None and self.completed:
            return
        if exc_type is None:
            exc_type = RuntimeError
            exc = RuntimeError("telemetry session exited without completion")
        now = max(time.time_ns(), self.started_wall_time_ns)
        self._write(
            TelemetryStatus(
                run_id=self.run_id,
                status=RunStatus.FAILED,
                started_wall_time_ns=self.started_wall_time_ns,
                updated_wall_time_ns=now,
                finished_wall_time_ns=now,
                samples_written=self.samples_written,
                error_type=exc_type.__name__,
                error_message=str(exc)[:1000] if exc is not None else None,
            )
        )
