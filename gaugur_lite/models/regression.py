"""RM 候选模型与回归指标。"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVR
from sklearn.tree import DecisionTreeRegressor
from sklearn.metrics import mean_absolute_error, r2_score


def candidate_regressors(seed: int) -> dict[str, Pipeline]:
    def pipe(model: Any) -> Pipeline:
        return Pipeline([("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler()), ("model", model)])

    return {
        "decision_tree": pipe(DecisionTreeRegressor(max_depth=5, random_state=seed)),
        "random_forest": pipe(RandomForestRegressor(n_estimators=80, max_depth=8, random_state=seed, n_jobs=1)),
        "gradient_boosting": pipe(GradientBoostingRegressor(n_estimators=80, max_depth=2, random_state=seed, loss="huber")),
        "svr": pipe(SVR(C=1.0, epsilon=0.02, kernel="rbf")),
    }


def regression_metrics(y_true: Any, predicted: Any, solo_fps: Any) -> dict[str, float | int]:
    truth = np.asarray(y_true, dtype=float)
    pred = np.asarray(predicted, dtype=float)
    solo = np.asarray(solo_fps, dtype=float)
    loss_truth = 1.0 - truth
    loss_pred = 1.0 - pred
    epsilon = 1e-3
    return {
        "sample_count": int(len(truth)),
        "retention_mae": float(mean_absolute_error(truth, pred)),
        "fps_mae": float(mean_absolute_error(truth * solo, pred * solo)),
        "r2": float(r2_score(truth, pred)) if len(truth) > 1 else 0.0,
        "mape_delta": float(np.mean(np.abs(loss_pred - loss_truth) / np.maximum(np.abs(loss_truth), epsilon))),
        "retention_min": float(np.min(truth)),
        "retention_max": float(np.max(truth)),
    }
