from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gaugur_lite.models.baselines import LinearAdditiveRegressor, SigmoidCountRegressor
from gaugur_lite.models.classification import classification_metrics
from gaugur_lite.models.common import ModelError, validate_split_contract
from gaugur_lite.models.regression import regression_metrics


def _small_table() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "combination_key": ["a", "a", "b"],
            "split": ["train", "train", "test"],
            "neighbor_count": [1, 2, 1],
            "solo_fps": [10.0, 10.0, 10.0],
            "combination_size": [2, 3, 2],
            "retention_ratio": [0.9, 0.8, 0.7],
            "sensitivity_cpu_compute_p100": [1.0, 1.0, 1.0],
            "sensitivity_gpu_compute_p100": [1.0, 1.0, 1.0],
            "sensitivity_gpu_memory_p100": [1.0, 1.0, 1.0],
            "sensitivity_memory_bandwidth_p100": [1.0, 1.0, 1.0],
            "intensity_mean_cpu_compute": [1.0, 1.5, 1.0],
            "intensity_mean_gpu_compute": [1.0, 1.0, 1.0],
            "intensity_mean_gpu_memory": [1.0, 1.0, 1.0],
            "intensity_mean_memory_bandwidth": [1.0, 1.0, 1.0],
        }
    )


def test_validate_split_contract_rejects_combination_leakage() -> None:
    table = _small_table()
    leaked = table.copy()
    leaked.loc[2, "combination_key"] = "a"

    with pytest.raises(ModelError, match="组合跨 split"):
        validate_split_contract({"rm": leaked, "cm": leaked, "extra_rm": table.iloc[0:0], "extra_cm": table.iloc[0:0]}, strict=False)


def test_classification_metrics_reports_single_class_without_nan() -> None:
    result = classification_metrics([True, True], [True, False])

    assert result["accuracy"] == 0.5
    assert result["false_positive_rate"] == 0.0
    assert result["confusion_matrix"] == [[0, 0], [1, 1]]


def test_regression_metrics_reports_retention_and_fps_errors() -> None:
    result = regression_metrics([0.8, 0.9], [0.7, 1.0], [10.0, 20.0])

    assert result["sample_count"] == 2
    assert result["retention_mae"] == pytest.approx(0.1)
    assert result["fps_mae"] == pytest.approx(1.5)
    assert np.isfinite(result["mape_delta"])


def test_baseline_estimators_fit_and_predict() -> None:
    table = _small_table()
    sigmoid = SigmoidCountRegressor().fit(table, table["retention_ratio"])
    additive = LinearAdditiveRegressor().fit(table, table["retention_ratio"])

    assert len(sigmoid.predict(table)) == len(table)
    assert len(additive.predict(table)) == len(table)
