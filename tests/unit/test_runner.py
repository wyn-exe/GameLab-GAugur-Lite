from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gaugur_lite.runner import runner
from gaugur_lite.runner import window_layout
from gaugur_lite.runner.runner import (
    ParsedPlanRow,
    build_execution_provenance,
    inspect_resume,
)
from gaugur_lite.runner.window_layout import (
    grid_rectangles,
    rectangles_overlap,
    wait_for_windows,
)


def _row(run_directory: str, run_id: str = "test-exp__solo__game_0__r01") -> ParsedPlanRow:
    raw = {
        "execution_index": "1",
        "run_id": run_id,
        "experiment_id": "test-exp",
        "stage": "solo",
        "mode": "solo",
        "workload_ids": '["game_0"]',
        "resource": "",
        "pressure_requested": "",
        "repeat": "1",
        "warmup_s": "1",
        "duration_s": "2",
        "sample_interval_s": "1",
        "cooldown_s": "0",
        "gpu_index": "0",
        "display_index": "0",
        "window_layout": "grid_2x2",
        "require_visible_windows": "true",
        "max_gpu_temp_c": "82",
        "config_sha256": "a" * 64,
        "run_directory": run_directory,
        "row_sha256": "b" * 64,
    }
    return ParsedPlanRow.from_csv(raw)


def test_grid_rectangles_are_pairwise_disjoint() -> None:
    rectangles = grid_rectangles(left=0, top=0, right=1920, bottom=1080, count=4)
    observations = [
        {
            "window_left": left,
            "window_top": top,
            "window_width": width,
            "window_height": height,
        }
        for left, top, width, height in rectangles
    ]

    assert all(
        not rectangles_overlap(observations[first], observations[second])
        for first in range(4)
        for second in range(first + 1, 4)
    )


def test_window_ready_is_bound_to_the_managed_pid(monkeypatch: object) -> None:
    snapshots = iter(
        [
            {"found": True, "process_pid": 999},
            {"found": True, "process_pid": 123},
        ]
    )
    monkeypatch.setattr(window_layout, "capture_window", lambda _title: next(snapshots))
    monkeypatch.setattr(window_layout.time, "sleep", lambda _seconds: None)

    observations = wait_for_windows(
        titles=("Pyxel Test",),
        expected_pids={"Pyxel Test": 123},
        timeout_s=1,
    )

    assert observations[0]["process_pid"] == 123


def test_window_ready_reports_wrong_pid_after_timeout(monkeypatch: object) -> None:
    monkeypatch.setattr(
        window_layout,
        "capture_window",
        lambda _title: {"found": True, "process_pid": 999},
    )

    with pytest.raises(RuntimeError, match="actual_pid.*999.*expected_pid.*123"):
        wait_for_windows(
            titles=("Pyxel Test",),
            expected_pids={"Pyxel Test": 123},
            timeout_s=0.001,
        )


def test_resume_skips_only_hash_verified_completed_attempt(tmp_path: Path) -> None:
    row = _row("data/raw/test-exp/test-exp__solo__game_0__r01")
    run_root = tmp_path / row.run_directory
    attempt = run_root / "attempts" / "a001"
    attempt.mkdir(parents=True)
    (attempt / "status.json").write_text(
        json.dumps({"status": "completed", "valid": True}), encoding="utf-8"
    )
    (attempt / "manifest.json").write_text(
        json.dumps({"row_sha256": row.row_sha256}), encoding="utf-8"
    )
    artifact = attempt / "system_metrics.jsonl"
    artifact.write_text('{"sequence":0}\n', encoding="utf-8")
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (attempt / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "valid": True,
                "system_coverage_ratio": 1.0,
                "workload_overlap_ratio": 1.0,
                "artifact_sha256": {"system_metrics.jsonl": artifact_hash},
            }
        ),
        encoding="utf-8",
    )
    (run_root / "index.json").write_text(
        json.dumps(
            {
                "run_id": row.run_id,
                "config_sha256": row.config_sha256,
                "row_sha256": row.row_sha256,
                "attempts": [
                    {
                        "attempt": 1,
                        "directory": attempt.relative_to(tmp_path).as_posix(),
                        "status": "completed",
                        "valid": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert inspect_resume(repo_root=tmp_path, row=row)["action"] == "skip"
    artifact.write_text("tampered\n", encoding="utf-8")
    decision = inspect_resume(repo_root=tmp_path, row=row)
    assert decision["action"] == "run"
    assert decision["attempt"] == 2


def test_run_plan_continues_after_one_isolated_failure(tmp_path: Path, monkeypatch: object) -> None:
    rows = [_row("data/raw/test-exp/first", "test-exp__solo__game_0__r01").raw,
            _row("data/raw/test-exp/second", "test-exp__solo__game_1__r01").raw]
    monkeypatch.setattr(runner, "verify_plan", lambda **_: {"status": "passed", "plan_sha256": "c" * 64})
    monkeypatch.setattr(runner, "load_plan_rows", lambda _: rows)
    monkeypatch.setattr(runner, "inspect_resume", lambda **_: {"action": "run", "attempt": 1})

    def fake_run_one(*, row: ParsedPlanRow, **_: object) -> dict[str, object]:
        if row.run_id.endswith("game_0__r01"):
            return {"run_id": row.run_id, "status": "failed", "valid": False}
        return {"run_id": row.run_id, "status": "completed", "valid": True}

    monkeypatch.setattr(runner, "run_one", fake_run_one)
    plan = tmp_path / "plan.csv"
    plan.write_text("placeholder\n", encoding="utf-8")
    result = runner.run_plan(repo_root=tmp_path, plan_file=plan, resume=True)

    assert result["status"] == "failed"
    assert result["completed"] == 1
    assert result["failed_or_invalid"] == 1
    assert len(result["results"]) == 2


def test_run_plan_filters_stage_before_max_runs(tmp_path: Path, monkeypatch: object) -> None:
    solo = _row("data/raw/test-exp/solo", "test-exp__solo__game_0__r01").raw
    profile = dict(_row("data/raw/test-exp/profile", "test-exp__profile__game_1__r01").raw)
    profile["stage"] = "profile"
    profile["mode"] = "pressure_profile"
    profile["resource"] = "cpu_compute"
    profile["pressure_requested"] = "0.5"
    monkeypatch.setattr(runner, "verify_plan", lambda **_: {"status": "passed", "plan_sha256": "c" * 64})
    monkeypatch.setattr(runner, "load_plan_rows", lambda _: [profile, solo])
    monkeypatch.setattr(runner, "inspect_resume", lambda **_: {"action": "run", "attempt": 1})
    plan = tmp_path / "plan.csv"
    plan.write_text("placeholder\n", encoding="utf-8")

    result = runner.run_plan(
        repo_root=tmp_path,
        plan_file=plan,
        resume=True,
        stage="solo",
        max_runs=1,
        dry_run=True,
    )

    assert result["stage"] == "solo"
    assert result["selected_runs"] == 1
    assert result["decisions"][0]["run_id"] == solo["run_id"]


def test_execution_provenance_changes_with_source_tree(tmp_path: Path) -> None:
    package = tmp_path / "gaugur_lite"
    package.mkdir()
    source = package / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

    first = build_execution_provenance(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = build_execution_provenance(tmp_path)

    assert len(first["source_tree_sha256"]) == 64
    assert first["source_tree_sha256"] != second["source_tree_sha256"]
    assert "gaugur_lite/module.py" in first["source_files"]
