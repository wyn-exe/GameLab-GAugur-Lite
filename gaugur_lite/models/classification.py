"""CM 候选模型、阈值选择和分类指标。"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_score, recall_score


def candidate_classifiers(seed: int) -> dict[str, Pipeline]:
    def pipe(model: Any) -> Pipeline:
        return Pipeline([("imputer", SimpleImputer(strategy="median")), ("scaler", StandardScaler()), ("model", model)])

    return {
        "decision_tree": pipe(DecisionTreeClassifier(max_depth=5, random_state=seed, class_weight="balanced")),
        "random_forest": pipe(RandomForestClassifier(n_estimators=80, max_depth=8, random_state=seed, class_weight="balanced")),
        "gradient_boosting": pipe(GradientBoostingClassifier(n_estimators=80, max_depth=2, random_state=seed)),
        "svc": pipe(SVC(C=1.0, probability=True, random_state=seed, class_weight="balanced")),
    }


def positive_probability(model: Any, features: Any) -> np.ndarray:
    if not hasattr(model, "predict_proba"):
        return np.asarray(model.predict(features), dtype=float)
    probabilities = model.predict_proba(features)
    classes = np.asarray(model.classes_)
    if len(classes) == 1:
        return np.ones(len(features), dtype=float) if bool(classes[0]) else np.zeros(len(features), dtype=float)
    positive_index = int(np.flatnonzero(classes == True)[0])  # noqa: E712
    return np.asarray(probabilities[:, positive_index], dtype=float)


def threshold_predictions(model: Any, features: Any, threshold: float) -> np.ndarray:
    return positive_probability(model, features) >= float(threshold)


def select_threshold(y_true: Any, probabilities: np.ndarray) -> tuple[float, dict[str, float]]:
    candidates = np.linspace(0.1, 0.9, 9)
    scored = []
    for threshold in candidates:
        predicted = probabilities >= threshold
        scored.append((f1_score(y_true, predicted, zero_division=0), -threshold, float(threshold)))
    _, _, selected = max(scored)
    predicted = probabilities >= selected
    return selected, {"f1": float(f1_score(y_true, predicted, zero_division=0)), "accuracy": float(accuracy_score(y_true, predicted))}


def classification_metrics(y_true: Any, predicted: Any) -> dict[str, Any]:
    truth = np.asarray(y_true, dtype=bool)
    pred = np.asarray(predicted, dtype=bool)
    matrix = confusion_matrix(truth, pred, labels=[False, True])
    tn, fp, fn, tp = matrix.ravel()
    return {
        "sample_count": int(len(truth)),
        "positive_count": int(truth.sum()),
        "predicted_positive_count": int(pred.sum()),
        "accuracy": float(accuracy_score(truth, pred)),
        "precision": float(precision_score(truth, pred, zero_division=0)),
        "recall": float(recall_score(truth, pred, zero_division=0)),
        "f1": float(f1_score(truth, pred, zero_division=0)),
        "false_positive_rate": float(fp / (fp + tn)) if fp + tn else 0.0,
        "confusion_matrix": [[int(tn), int(fp)], [int(fn), int(tp)]],
    }
