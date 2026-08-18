"""RM/CM 可比基线；所有基线只在 train+validation 上拟合。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..features.dataset import RESOURCES


class SoloOnlyRegressor(BaseEstimator, RegressorMixin):
    """只假设共置不造成性能变化。"""

    def fit(self, x: Any, y: Any) -> "SoloOnlyRegressor":
        del x, y
        return self

    def predict(self, x: Any) -> np.ndarray:
        return np.ones(len(x), dtype=float)


class SigmoidCountRegressor(BaseEstimator, RegressorMixin):
    """只用邻居数量拟合有界 sigmoid；小样本/退化时稳定回退到按数量均值。"""

    def fit(self, x: Any, y: Any) -> "SigmoidCountRegressor":
        values = np.asarray(x["neighbor_count"], dtype=float)
        target = np.asarray(y, dtype=float)
        self.lookup_ = {float(count): float(target[values == count].mean()) for count in sorted(set(values))}
        self.default_ = float(target.mean())
        self.parameters_ = None
        try:
            from scipy.optimize import curve_fit

            def sigmoid(count: np.ndarray, alpha_1: float, alpha_2: float, alpha_3: float) -> np.ndarray:
                return alpha_1 / (1.0 + np.exp(np.clip(-alpha_2 * count + alpha_3, -60, 60)))

            self.parameters_, _ = curve_fit(
                sigmoid,
                values,
                target,
                p0=(1.0, 0.5, 1.0),
                bounds=([0.0, -10.0, -20.0], [2.0, 10.0, 20.0]),
                maxfev=20000,
            )
        except (ImportError, RuntimeError, ValueError, FloatingPointError):
            self.parameters_ = None
        return self

    def predict(self, x: Any) -> np.ndarray:
        values = np.asarray(x["neighbor_count"], dtype=float)
        if self.parameters_ is None:
            return np.asarray([self.lookup_.get(value, self.default_) for value in values], dtype=float)
        alpha_1, alpha_2, alpha_3 = self.parameters_
        return alpha_1 / (1.0 + np.exp(np.clip(-alpha_2 * values + alpha_3, -60, 60)))


class LinearAdditiveRegressor(BaseEstimator, RegressorMixin):
    """最大压力 target sensitivity × 邻居 intensity 的线性加和基线。"""

    def fit(self, x: pd.DataFrame, y: Any) -> "LinearAdditiveRegressor":
        design = self._design(x)
        self.model_ = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("regressor", Ridge(alpha=1e-3)),
        ])
        self.model_.fit(design, y)
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.model_.predict(self._design(x)), dtype=float)

    @staticmethod
    def _design(x: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(
            {
                f"linear_{resource}": x[f"sensitivity_{resource}_p100"] * x[f"intensity_mean_{resource}"]
                for resource in RESOURCES
            },
            index=x.index,
        )


def _resource_columns(prefix: str) -> list[str]:
    return [f"{prefix}_{resource}" for resource in RESOURCES]


def fit_baseline_models(train_validation: pd.DataFrame, *, seed: int) -> dict[str, Any]:
    intensity_columns = _resource_columns("intensity_mean") + _resource_columns("intensity_var")
    vbp = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("regressor", Ridge(alpha=1.0)),
    ])
    vbp.fit(train_validation[intensity_columns], train_validation["retention_ratio"])
    no_profile_columns = ["solo_fps", "neighbor_count", "combination_size"]
    no_profile = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("regressor", RandomForestRegressor(n_estimators=80, max_depth=5, random_state=seed, n_jobs=1)),
    ])
    no_profile.fit(train_validation[no_profile_columns], train_validation["retention_ratio"])
    sigmoid = SigmoidCountRegressor().fit(train_validation, train_validation["retention_ratio"])
    linear = LinearAdditiveRegressor().fit(train_validation, train_validation["retention_ratio"])
    return {
        "sigmoid_count": sigmoid,
        "vbp_like": (vbp, intensity_columns),
        "linear_additive": linear,
        "solo_only": SoloOnlyRegressor().fit(train_validation, train_validation["retention_ratio"]),
        "no_profile_tree": (no_profile, no_profile_columns),
    }


def predict_baseline(model: Any, table: pd.DataFrame) -> np.ndarray:
    if isinstance(model, tuple):
        estimator, columns = model
        return np.asarray(estimator.predict(table[columns]), dtype=float)
    return np.asarray(model.predict(table), dtype=float)
