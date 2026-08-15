from __future__ import annotations

import json
from pathlib import Path

import pytest

from gaugur_lite import baselines
from gaugur_lite.baselines import BaselineError, compute_solo_baselines


WORKLOADS = tuple(f"game_{index}" for index in range(8))


def _raw_row(workload_id: str, repeat: int) -> dict[str, str]:
    return {
        "execution_index": str(repeat),
        "run_id": f"formal-v1__solo__{workload_id}__r{repeat:02d}",
        "experiment_id": "formal-v1",
        "stage": "solo",
        "split": "not_applicable",
        "mode": "solo",
        "workload_ids": json.dumps([workload_id]),
        "target_id": workload_id,
        "neighbor_ids": "[]",
        "combination_key": "",
        "colocation_id": "",
        "resource": "",
        "pressure_requested": "",
        "repeat": str(repeat),
        "seed": str(1000 + repeat),
        "warmup_s": "20",
        "duration_s": "60",
        "sample_interval_s": "1",
        "cooldown_s": "20",
        "host_id": "test-host",
        "gpu_index": "0",
        "display_index": "0",
        "window_layout": "grid_2x2",
        "require_visible_windows": "true",
        "max_gpu_temp_c": "82",
        "config_sha256": "a" * 64,
        "root_commit": "1" * 40,
        "run_directory": f"data/raw/formal-v1/{workload_id}-r{repeat}",
        "game_entrypoints": "{}",
        "game_sha256s": "{}",
        "row_sha256": (f"{repeat:x}" * 64)[:64],
    }


def _prepare(
    tmp_path: Path,
    monkeypatch: object,
    *,
    unstable_workload: str | None = None,
) -> Path:
    plan = tmp_path / "formal.csv"
    plan.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-manifest.json").write_text(
        json.dumps(
            {
                "root_commit": "1" * 40,
                "root_dirty_at_generation": False,
                "config_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    rows = [_raw_row(workload, repeat) for workload in WORKLOADS for repeat in (1, 2, 3)]
    monkeypatch.setattr(baselines, "load_plan_rows", lambda _: rows)
    monkeypatch.setattr(
        baselines,
        "verify_plan",
        lambda **_: {"status": "passed", "plan_sha256": "b" * 64},
    )

    def fake_collect(*, row: object, **_: object) -> dict[str, object]:
        workload_index = WORKLOADS.index(row.workload_ids[0])
        mean = 10.0 + workload_index + row.repeat * 0.001
        if row.workload_ids[0] == unstable_workload and row.repeat == 3:
            mean *= 1.5
        return {
            "schema_version": 1,
            "experiment_id": "formal-v1",
            "workload_id": row.workload_ids[0],
            "repeat": row.repeat,
            "run_id": row.run_id,
            "run_seed": int(row.raw["seed"]),
            "row_sha256": row.row_sha256,
            "config_sha256": "a" * 64,
            "attempt": 1,
            "attempt_directory": f"data/raw/{row.run_id}/attempts/a001",
            "summary_sha256": f"{workload_index:x}" * 64,
            "game_metrics_sha256": f"{row.repeat:x}" * 64,
            "execution_root_commit": "1" * 40,
            "execution_root_dirty": True,
            "execution_source_tree_sha256": "c" * 64,
            "target_fps": 30,
            "mean_fps": mean,
            "p05_fps": mean - 0.05,
            "min_fps": mean - 0.1,
            "fps_windows_used": 60,
            "measurement_coverage_ratio": 1.0,
            "system_coverage_ratio": 1.0,
            "system_sample_count": 61,
            "window_sample_count": 61,
            "gpu_temp_c_max": 50.0,
            "missed_deadline_count": 0,
        }

    monkeypatch.setattr(baselines, "_collect_run_record", fake_collect)
    return plan


def test_compute_solo_baselines_builds_exact_retention_keys(
    tmp_path: Path, monkeypatch: object
) -> None:
    plan = _prepare(tmp_path, monkeypatch)

    result, records = compute_solo_baselines(repo_root=tmp_path, plan_file=plan)

    assert result["status"] == "passed"
    assert result["run_count"] == len(records) == 24
    assert result["workload_count"] == 8
    assert result["repeats"] == [1, 2, 3]
    assert len({item["baseline_id"] for item in result["baselines"]}) == 8
    assert all(item["mean_fps_cv_pct"] < 5 for item in result["baselines"])
    assert result["checks"]["single_execution_source_tree"] is True


def test_compute_solo_baselines_rejects_unstable_repeat(
    tmp_path: Path, monkeypatch: object
) -> None:
    plan = _prepare(tmp_path, monkeypatch, unstable_workload="game_3")

    with pytest.raises(BaselineError, match="all_baselines_stable"):
        compute_solo_baselines(repo_root=tmp_path, plan_file=plan)


def test_solo_plan_rejects_neighbor_leakage(tmp_path: Path, monkeypatch: object) -> None:
    row = _raw_row("game_0", 1)
    row["neighbor_ids"] = '["game_1"]'
    monkeypatch.setattr(baselines, "load_plan_rows", lambda _: [row])

    with pytest.raises(BaselineError, match="包含邻居或压力"):
        baselines._solo_plan_rows(tmp_path / "formal.csv")
