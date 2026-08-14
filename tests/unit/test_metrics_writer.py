from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gaugur_lite.metrics import writer as writer_module
from gaugur_lite.metrics.writer import JsonlWriter, StatusTracker, write_json_atomic


def test_jsonl_writer_flushes_in_batches_and_every_line_parses(tmp_path: Path) -> None:
    output = tmp_path / "metrics.jsonl"
    with JsonlWriter(output, batch_size=2) as writer:
        writer.write({"sequence": 0, "value": "一"})
        assert output.read_text(encoding="utf-8") == ""
        writer.write({"sequence": 1, "value": "二"})
        assert len(output.read_text(encoding="utf-8").splitlines()) == 2
        writer.write({"sequence": 2, "value": "三"})

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["sequence"] for row in rows] == [0, 1, 2]


def test_jsonl_writer_preserves_flushed_and_pending_rows_on_exception(tmp_path: Path) -> None:
    output = tmp_path / "metrics.jsonl"
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with JsonlWriter(output, batch_size=10) as writer:
            writer.write({"sequence": 0})
            writer.write({"sequence": 1})
            raise RuntimeError("synthetic failure")

    assert [json.loads(line)["sequence"] for line in output.read_text().splitlines()] == [0, 1]


def test_jsonl_writer_refuses_to_overwrite_existing_raw_data(tmp_path: Path) -> None:
    output = tmp_path / "metrics.jsonl"
    output.write_text('{"existing":true}\n', encoding="utf-8")

    with pytest.raises(FileExistsError):
        with JsonlWriter(output):
            pass
    assert output.read_text(encoding="utf-8") == '{"existing":true}\n'


def test_status_tracker_writes_failed_status_without_deleting_raw_data(tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    raw_file = tmp_path / "system_metrics.jsonl"

    with pytest.raises(ValueError, match="boom"):
        with StatusTracker(status_file, run_id="step2-test") as status:
            raw_file.write_text('{"sequence":0}\n', encoding="utf-8")
            status.update_samples(1)
            raise ValueError("boom")

    status = json.loads(status_file.read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["samples_written"] == 1
    assert status["error_type"] == "ValueError"
    assert raw_file.read_text(encoding="utf-8") == '{"sequence":0}\n'


def test_status_tracker_completes_and_atomic_writer_leaves_no_temp(tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    summary_file = tmp_path / "summary.json"
    write_json_atomic(summary_file, {"sample_count": 3})

    with StatusTracker(status_file, run_id="step2-test") as status:
        status.mark_completed(samples_written=3, summary_file="summary.json")

    parsed = json.loads(status_file.read_text(encoding="utf-8"))
    assert parsed["status"] == "completed"
    assert parsed["summary_file"] == "summary.json"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_writer_retries_transient_windows_replace_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "heartbeat.json"
    real_replace = os.replace
    calls = 0

    def flaky_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(5, "synthetic Windows file lock")
        real_replace(source, target)

    monkeypatch.setattr(writer_module.os, "replace", flaky_replace)
    monkeypatch.setattr(writer_module.time, "sleep", lambda _: None)

    write_json_atomic(output, {"status": "running", "draw_count": 645})

    assert calls == 3
    assert json.loads(output.read_text(encoding="utf-8"))["draw_count"] == 645
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_writer_stops_retrying_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "heartbeat.json"
    calls = 0

    def locked_replace(source: Path, target: Path) -> None:
        del source, target
        nonlocal calls
        calls += 1
        raise PermissionError(5, "synthetic persistent Windows file lock")

    monkeypatch.setattr(writer_module.os, "replace", locked_replace)
    monkeypatch.setattr(writer_module.time, "sleep", lambda _: None)

    with pytest.raises(PermissionError, match="persistent Windows file lock"):
        write_json_atomic(output, {"status": "running"})

    assert calls == writer_module._ATOMIC_REPLACE_ATTEMPTS
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))
