from __future__ import annotations

import pytest

from gaugur_lite.features import dataset


def _profile_index() -> dict[tuple[str, str, float], dict[str, object]]:
    index: dict[tuple[str, str, float], dict[str, object]] = {}
    for workload_id in ("target", "neighbor_a", "neighbor_b"):
        for resource_index, resource in enumerate(dataset.RESOURCES, start=1):
            for pressure in dataset.PRESSURES:
                index[(workload_id, resource, pressure)] = {
                    "sensitivity_mean": resource_index + pressure,
                    "intensity_slowdown_mean": (
                        None
                        if pressure == 0.0
                        else resource_index + (0.1 if workload_id == "neighbor_a" else 0.3)
                    ),
                }
    return index


def _truth_row() -> dict[str, object]:
    return {
        "experiment_id": "formal-v1",
        "stage": "colocation-main",
        "split": "train",
        "combination_key": "neighbor_a+target",
        "colocation_id": "neighbor_a+target__r01",
        "run_id": "run-1",
        "repeat": 1,
        "target_id": "target",
        "workload_ids": ["neighbor_a", "target"],
        "neighbor_ids": ["neighbor_a"],
        "combination_size": 2,
        "mean_fps": 9.0,
        "p05_fps": 8.0,
        "min_fps": 7.0,
        "solo_mean_fps": 10.0,
        "retention_ratio": 0.9,
        "loss_ratio": 0.1,
        "p05_retention_ratio": 0.8,
    }


def test_build_feature_rows_uses_target_curve_and_neighbor_intensity_population_variance() -> None:
    row = dataset._build_feature_rows([_truth_row()], _profile_index())[0]

    assert row["sensitivity_cpu_compute_p000"] == 1.0
    assert row["sensitivity_gpu_memory_p075"] == 3.75
    assert row["intensity_mean_cpu_compute"] == pytest.approx(1.1)
    assert row["intensity_var_cpu_compute"] == pytest.approx(0.0)
    assert row["neighbor_count"] == 1
    assert "target_id" not in dataset.FEATURE_COLUMNS


def test_expand_cm_preserves_three_qos_ratios_and_relative_threshold() -> None:
    rows = dataset._expand_cm(
        [dataset._build_feature_rows([_truth_row()], _profile_index())[0]]
    )

    assert [row["qos_ratio"] for row in rows] == [0.7, 0.8, 0.9]
    assert [row["qos_threshold"] for row in rows] == [7.0, 8.0, 9.0]
    assert [row["qos_satisfied"] for row in rows] == [True, True, True]


def test_validate_truth_rejects_split_leakage() -> None:
    row = _truth_row()
    duplicate_split = {**row, "split": "test", "repeat": 2}

    with pytest.raises(dataset.DatasetError, match="同一组合跨 split"):
        dataset._validate_truth([row, duplicate_split], strict=False)
