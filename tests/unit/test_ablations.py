from __future__ import annotations

import numpy as np
import pandas as pd

from gaugur_lite.ablations import _fit_variant, _targets, _variant_features
from gaugur_lite.config import load_yaml_mapping
from gaugur_lite.features.dataset import FEATURE_COLUMNS, RESOURCES
from gaugur_lite.models.classification import candidate_classifiers, positive_probability


def _feature_table() -> pd.DataFrame:
    values = {column: np.ones(3, dtype=float) for column in FEATURE_COLUMNS}
    values["neighbor_count"] = np.asarray([1.0, 2.0, 3.0])
    for resource in RESOURCES:
        values[f"intensity_mean_{resource}"] = np.asarray([1.0, 2.0, 3.0])
        values[f"intensity_var_{resource}"] = np.asarray([0.1, 0.2, 0.3])
    return pd.DataFrame(values)


def test_variant_feature_modes_keep_target_out_and_change_expected_columns() -> None:
    table = _feature_table()
    no_sensitivity, no_sensitivity_columns = _variant_features(table, "no_sensitivity")
    intensity_sum, intensity_sum_columns = _variant_features(table, "intensity_sum")
    max_pressure, max_pressure_columns = _variant_features(table, "max_pressure_only")

    assert all(not column.startswith("sensitivity_") for column in no_sensitivity_columns)
    assert any(column.startswith("intensity_mean_") for column in no_sensitivity_columns)
    assert all(column.startswith("intensity_sum_") or not column.startswith("intensity_") for column in intensity_sum_columns)
    assert intensity_sum.loc[1, "intensity_sum_cpu_compute"] == 4.0
    assert all(column.endswith("_p100") or not column.startswith("sensitivity_") for column in max_pressure_columns)
    assert len(no_sensitivity) == len(intensity_sum) == len(max_pressure) == 3
    assert "target_id" not in set(no_sensitivity_columns + intensity_sum_columns + max_pressure_columns)


def test_p05_targets_use_p05_retention_and_qos_threshold() -> None:
    table = pd.DataFrame(
        {
            "p05_retention_ratio": [0.70, 0.79, 0.91],
            "qos_ratio": [0.70, 0.80, 0.90],
            "qos_satisfied": [True, True, True],
            "retention_ratio": [0.8, 0.8, 0.8],
        }
    )

    assert np.allclose(_targets(table, "p05_fps", "rm"), [0.70, 0.79, 0.91])
    assert _targets(table, "p05_fps", "cm").tolist() == [True, False, True]
    assert _targets(table, "mean_fps", "cm").tolist() == [True, True, True]


def test_ablation_spec_declares_all_required_variants() -> None:
    spec = load_yaml_mapping("configs/experiments/ablations.yaml")
    names = [item["id"] for item in spec["variants"]]
    assert names == [
        "full_mean_fps",
        "no_sensitivity",
        "no_intensity",
        "intensity_sum",
        "max_pressure_only",
        "no_resource_utilization",
        "p05_fps_label",
        "pair_train_triple_test",
        "pressure_11_curve",
    ]
    assert spec["bootstrap_repeats"] == 200


def test_one_class_cm_pipeline_exposes_safe_probability() -> None:
    table = _feature_table()
    model = candidate_classifiers(20260811)["decision_tree"]
    model.fit(table, np.asarray([True, True, True]))

    assert np.all(positive_probability(model, table) == 1.0)


def test_fit_variant_runs_with_train_validation_test_and_extra_tables() -> None:
    def table(rows: int, prefix: str, *, cm: bool) -> pd.DataFrame:
        frame = _feature_table().iloc[:rows].copy()
        frame["combination_key"] = [f"{prefix}-{index}" for index in range(rows)]
        frame["combination_size"] = [2] * rows
        frame["split"] = "train"
        frame["retention_ratio"] = np.linspace(0.90, 0.99, rows)
        frame["p05_retention_ratio"] = frame["retention_ratio"] - 0.01
        frame["qos_ratio"] = 0.70
        frame["qos_satisfied"] = True
        if cm:
            frame["qos_satisfied"] = frame["retention_ratio"] >= frame["qos_ratio"]
        return frame

    rm_train = table(3, "train", cm=False)
    rm_validation = table(2, "validation", cm=False).assign(split="validation")
    rm_test = table(2, "test", cm=False).assign(split="test")
    rm_extra = table(2, "extra", cm=False).assign(split="extra_test")
    cm_train = table(3, "train", cm=True)
    cm_validation = table(2, "validation", cm=True).assign(split="validation")
    cm_test = table(2, "test", cm=True).assign(split="test")
    cm_extra = table(2, "extra", cm=True).assign(split="extra_test")

    result = _fit_variant(
        tables={"rm": pd.concat([rm_train, rm_validation, rm_test]), "cm": pd.concat([cm_train, cm_validation, cm_test]), "extra_rm": rm_extra, "extra_cm": cm_extra},
        variant={"id": "test", "feature_mode": "full", "label_mode": "mean_fps"},
        seed=20260811,
        bootstrap_repeats=20,
        cm_candidate="decision_tree",
        rm_candidate="gradient_boosting",
    )

    assert result["status"] == "passed"
    assert "bootstrap_ci95" in result["rm"]["test"]
    assert result["row_counts"]["fit_rm"] == 5
