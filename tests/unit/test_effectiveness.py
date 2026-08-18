from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gaugur_lite import effectiveness
from gaugur_lite.effectiveness import _high_fps_rows, _stress_rows, audit_stress_pilot
from gaugur_lite.runner.plan import PLAN_COLUMNS


def _base_row(index: int) -> dict[str, str]:
    row = {column: "" for column in PLAN_COLUMNS}
    row.update(
        {
            "schema_version": "2",
            "execution_index": str(index),
            "run_id": f"old-{index}",
            "experiment_id": "formal-v1",
            "stage": "colocation-main",
            "split": "train",
            "mode": "colocation",
            "workload_ids": '["a", "b"]',
            "target_id": "a",
            "neighbor_ids": '["b"]',
            "combination_key": "a+b",
            "colocation_id": "a+b__r01",
            "repeat": "1",
            "seed": "1",
            "warmup_s": "1",
            "duration_s": "2",
            "sample_interval_s": "1",
            "cooldown_s": "1",
            "host_id": "host",
            "gpu_index": "0",
            "display_index": "0",
            "window_layout": "grid_2x2",
            "require_visible_windows": "true",
            "max_gpu_temp_c": "80",
            "config_sha256": "old-config",
            "root_commit": "a" * 40,
            "run_directory": f"data/raw/formal-v1/old-{index}",
        }
    )
    return row


def test_stress_rows_attach_real_pressure_and_new_run_identity() -> None:
    rows = [_base_row(index) for index in range(216)]
    stressed = _stress_rows(
        rows,
        experiment_id="formal-effectiveness-v1",
        resource="cpu_compute",
        pressure_requested=1.0,
        pressure_applied=1.0,
        config_hash="new-config",
        root_commit="b" * 40,
        raw_root="data/raw/formal-effectiveness-v1",
    )

    assert len(stressed) == 216
    assert all(row["resource"] == "cpu_compute" for row in stressed)
    assert all(row["pressure_requested"] == "1" for row in stressed)
    assert all(row["pressure_applied"] == "1" for row in stressed)
    assert all(row["config_sha256"] == "new-config" for row in stressed)
    assert all(row["root_commit"] == "b" * 40 for row in stressed)
    assert all(row["run_directory"].startswith("data/raw/formal-effectiveness-v1/") for row in stressed)
    assert all(row["row_sha256"] for row in stressed)
    assert all(row["run_id"].startswith("formal-effectiveness-v1__colocation__a+b__cpu_compute__p100__r01") for row in stressed)


def test_high_fps_rows_preserve_shape_without_external_benchmark() -> None:
    rows = [_base_row(index) for index in range(240)]
    high_fps = _high_fps_rows(
        rows,
        experiment_id="formal-highfps-v1",
        fps_multiplier=8.0,
        config_hash="highfps-config",
        root_commit="c" * 40,
        raw_root="data/raw/formal-highfps-v1",
    )

    assert len(high_fps) == 240
    assert all(row["resource"] == "" for row in high_fps)
    assert all(row["pressure_requested"] == "" for row in high_fps)
    assert all(row["pressure_applied"] == "" for row in high_fps)
    assert all(row["config_sha256"] == "highfps-config" for row in high_fps)
    assert all(row["root_commit"] == "c" * 40 for row in high_fps)
    assert all(row["run_id"].startswith("formal-highfps-v1__colocation__a+b__r01") for row in high_fps)
    assert all(row["row_sha256"] for row in high_fps)


@pytest.mark.parametrize(
    ("retentions", "expected_status"),
    [([0.90, 0.70], "passed"), ([0.90, 0.91], "failed")],
)
def test_stress_pilot_requires_non_degenerate_qos_labels(
    tmp_path, monkeypatch: pytest.MonkeyPatch, retentions: list[float], expected_status: str
) -> None:
    plan = tmp_path / "stress.csv"
    plan.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "stress-manifest.json").write_text(
        json.dumps(
            {
                "experiment_id": "formal-effectiveness-v1",
                "resource": "cpu_compute",
                "pressure_requested": 1.0,
                "pressure_applied": 1.0,
                "benchmark_cpu_workers": 64,
            }
        ),
        encoding="utf-8",
    )
    row = SimpleNamespace(run_id="stress-run", workload_ids=("a", "b"))
    monkeypatch.setattr(
        effectiveness,
        "audit_colocation_inputs",
        lambda **_: {
            "status": "passed",
            "plan_sha256": "a" * 64,
            "pressure_cells": [
                {
                    "resource": "cpu_compute",
                    "pressure_requested": 1.0,
                    "pressure_applied": 1.0,
                }
            ],
        },
    )
    monkeypatch.setattr(effectiveness, "_colocation_rows", lambda *_args, **_kwargs: ([row], []))
    monkeypatch.setattr(effectiveness, "_load_baselines", lambda **_: ({"a": {}, "b": {}}, {}))
    monkeypatch.setattr(effectiveness, "inspect_resume", lambda **_: {"action": "skip"})
    monkeypatch.setattr(
        effectiveness,
        "_collect_run_record",
        lambda **_: (
            {"run_id": row.run_id},
            [{"retention_ratio": value} for value in retentions],
        ),
    )

    result = audit_stress_pilot(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=tmp_path / "solo.json",
        qos_ratio=0.80,
        min_completed_runs=1,
        min_positive_targets=1,
        min_negative_targets=1,
        expected_benchmark_cpu_workers=64,
    )

    assert result["status"] == expected_status
    assert result["checks"]["non_degenerate_qos_labels"] is (expected_status == "passed")
