from __future__ import annotations

import itertools
import hashlib
import json
from pathlib import Path

import pytest

from gaugur_lite import colocation
from gaugur_lite.colocation import (
    ColocationError,
    audit_colocation_inputs,
    build_colocation_truth,
    compute_colocation_truth,
    verify_colocation_truth,
)
from gaugur_lite.runner.plan import _FORMAL_EXTRA_QUADS, select_balanced_triples
from gaugur_lite.schema import make_colocation_id, make_combination_key


WORKLOADS = (
    "pyxel_jump",
    "pyxel_bubbles",
    "pyxel_snake",
    "pyxel_shooter",
    "pyxel_platformer",
    "daylight",
    "mega_wing",
    "space_rescue",
)


def _raw_row(
    combination: tuple[str, ...], repeat: int, *, stage: str, index: int, split: str | None = None
) -> dict[str, str]:
    workloads = tuple(sorted(combination))
    key = make_combination_key(workloads)
    mode = "extra_test" if stage == "colocation-extra-test" else "colocation"
    return {
        "execution_index": str(index),
        "run_id": f"formal-v1__{mode}__{key}__r{repeat:02d}",
        "experiment_id": "formal-v1",
        "stage": stage,
        "split": split or ("extra_test" if stage == "colocation-extra-test" else "train"),
        "mode": mode,
        "workload_ids": json.dumps(workloads),
        "target_id": workloads[0],
        "neighbor_ids": json.dumps(workloads[1:]),
        "combination_key": key,
        "colocation_id": make_colocation_id(key, repeat),
        "resource": "",
        "pressure_requested": "",
        "pressure_applied": "",
        "repeat": str(repeat),
        "seed": str(1000 + index),
        "warmup_s": "10",
        "duration_s": "30",
        "sample_interval_s": "1",
        "cooldown_s": "10",
        "host_id": "test-host",
        "gpu_index": "0",
        "display_index": "0",
        "window_layout": "grid_2x2",
        "require_visible_windows": "true",
        "max_gpu_temp_c": "80",
        "config_sha256": "a" * 64,
        "root_commit": "1" * 40,
        "run_directory": f"data/raw/formal-v1/{mode}-{index}",
        "game_entrypoints": "{}",
        "game_sha256s": "{}",
        "row_sha256": f"{index:064x}",
    }


def _formal_rows() -> list[dict[str, str]]:
    pairs = tuple(itertools.combinations(sorted(WORKLOADS), 2))
    triples, _ = select_balanced_triples(WORKLOADS, seed=20260811)
    main_combinations = (*pairs, *triples)
    ordered_main_keys = sorted(
        (make_combination_key(tuple(sorted(item))) for item in main_combinations),
        key=lambda key: hashlib.sha256(f"20260811:{key}".encode("utf-8")).hexdigest(),
    )
    main_splits = {
        key: "train" if index < 36 else "validation" if index < 48 else "test"
        for index, key in enumerate(ordered_main_keys)
    }
    rows = []
    index = 1
    for combination in main_combinations:
        for repeat in (1, 2, 3):
            rows.append(
                _raw_row(
                    combination,
                    repeat,
                    stage="colocation-main",
                    index=index,
                    split=main_splits[make_combination_key(tuple(sorted(combination)))],
                )
            )
            index += 1
    for combination in _FORMAL_EXTRA_QUADS:
        for repeat in (1, 2, 3):
            rows.append(
                _raw_row(
                    combination,
                    repeat,
                    stage="colocation-extra-test",
                    index=index,
                )
            )
            index += 1
    return rows


def _baseline_payload() -> dict[str, object]:
    baselines = []
    for index, workload_id in enumerate(sorted(WORKLOADS), start=1):
        baselines.append(
            {
                "workload_id": workload_id,
                "baseline_id": f"{index:064x}",
                "mean_fps": 30.0,
                "p05_fps": 29.0,
                "valid_for_retention": True,
            }
        )
    return {
        "status": "passed",
        "checks": {"all_baselines_stable": True},
        "baselines": baselines,
    }


def _prepare_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    plan = tmp_path / "formal.csv"
    plan.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False}), encoding="utf-8"
    )
    baseline = tmp_path / "solo-baselines.json"
    baseline.write_text(json.dumps(_baseline_payload()), encoding="utf-8")
    monkeypatch.setattr(colocation, "load_plan_rows", lambda _: _formal_rows())
    monkeypatch.setattr(
        colocation,
        "verify_plan",
        lambda **_: {"status": "passed", "plan_sha256": "b" * 64},
    )
    return plan, baseline


def test_audit_colocation_inputs_accepts_exact_formal_design(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, baseline = _prepare_audit(tmp_path, monkeypatch)

    result = audit_colocation_inputs(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=baseline,
    )

    assert result["status"] == "passed"
    assert result["main_physical_run_count"] == 180
    assert result["extra_physical_run_count"] == 36
    assert result["checks"]["main_pair_count_28"] is True
    assert result["checks"]["main_triple_count_32"] is True
    assert set(result["triple_workload_occurrences"].values()) == {12}
    assert set(result["extra_workload_occurrences"].values()) == {6}
    assert set(result["extra_pair_cooccurrence"].values()).issubset({2, 3})


def test_audit_rejects_missing_formal_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, baseline = _prepare_audit(tmp_path, monkeypatch)
    rows = _formal_rows()[:-1]
    monkeypatch.setattr(colocation, "load_plan_rows", lambda _: rows)

    with pytest.raises(ColocationError, match="计划行数不符"):
        audit_colocation_inputs(
            repo_root=tmp_path,
            plan_file=plan,
            solo_baselines_file=baseline,
        )


def test_compute_colocation_truth_preserves_retention_above_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, baseline = _prepare_audit(tmp_path, monkeypatch)

    def fake_collect(*, row: object, **_: object) -> tuple[dict[str, object], list[dict[str, object]]]:
        common = {
            "stage": row.stage,
            "run_id": row.run_id,
            "combination_key": row.raw["combination_key"],
            "repeat": row.repeat,
            "combination_size": len(row.workload_ids),
            "execution_source_tree_sha256": "c" * 64,
            "execution_root_commit": "2" * 40,
            "execution_root_dirty": False,
            "config_sha256": "a" * 64,
            "system_coverage_ratio": 1.0,
            "workload_overlap_ratio": 1.0,
            "windows_pairwise_nonoverlap": True,
            "gpu_thermal_slowdown_seen": False,
        }
        physical = {**common, "target_count": len(row.workload_ids)}
        targets = [
            {
                **common,
                "target_id": workload_id,
                "retention_ratio": 1.01 if workload_id == row.workload_ids[0] else 0.99,
                "measurement_coverage_ratio": 1.0,
            }
            for workload_id in row.workload_ids
        ]
        return physical, targets

    monkeypatch.setattr(colocation, "_collect_run_record", fake_collect)
    result, physical, targets = compute_colocation_truth(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=baseline,
    )

    assert result["status"] == "passed"
    assert len(physical) == 216
    assert len(targets) == 600
    assert result["main_target_truth_count"] == 456
    assert result["extra_target_truth_count"] == 144
    assert result["aggregate"]["retention_above_one_count"] == 216
    assert result["aggregate"]["retention_max"] == 1.01


def test_build_and_verify_colocation_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "formal.csv"
    baseline = tmp_path / "solo.json"
    plan.write_text("placeholder\n", encoding="utf-8")
    baseline.write_text("{}\n", encoding="utf-8")
    physical = [{"run_id": "run-1", "stage": "colocation-main"}]
    targets = [
        {
            "stage": "colocation-main",
            "combination_size": 2,
            "target_id": "game_0",
            "neighbor_ids": ["game_1"],
            "retention_ratio": 0.9,
        },
        {
            "stage": "colocation-main",
            "combination_size": 2,
            "target_id": "game_1",
            "neighbor_ids": ["game_0"],
            "retention_ratio": 1.1,
        },
    ]
    summary = {"schema_version": 1, "status": "passed", "inputs": {"plan_sha256": "d" * 64}, "checks": {"ok": True}}
    monkeypatch.setattr(
        colocation,
        "compute_colocation_truth",
        lambda **_: (summary.copy(), physical, targets),
    )
    runs = tmp_path / "out" / "runs.jsonl"
    truth = tmp_path / "out" / "truth.parquet"
    summary_file = tmp_path / "out" / "summary.json"
    plot = tmp_path / "out" / "retention.png"

    built = build_colocation_truth(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=baseline,
        runs_output_file=runs,
        truth_output_file=truth,
        summary_file=summary_file,
        plot_file=plot,
    )
    verified = verify_colocation_truth(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=baseline,
        runs_file=runs,
        truth_file=truth,
        summary_file=summary_file,
        plot_file=plot,
    )

    assert built["artifacts"]["colocation_truth_sha256"]
    assert verified["status"] == "passed"
    assert verified["passed_count"] == verified["check_count"] == 8
