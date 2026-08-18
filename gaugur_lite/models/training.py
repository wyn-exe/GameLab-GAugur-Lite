"""Step 10 CM/RM 候选选择、最终重拟合和模型卡持久化。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from ..features.dataset import FEATURE_COLUMNS
from .baselines import fit_baseline_models
from .classification import candidate_classifiers, classification_metrics, positive_probability, select_threshold, threshold_predictions
from .common import ModelError, file_sha256, load_dataset_tables, load_feature_manifest, model_feature_frame, prediction_sha256, validate_split_contract, write_json_exclusive
from .regression import candidate_regressors, regression_metrics


def _write_joblib(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"拒绝覆盖模型文件: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(value, path, compress=3)


def _train_classification(train: pd.DataFrame, validation: pd.DataFrame, *, seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
    x_train = model_feature_frame(train)
    x_validation = model_feature_frame(validation)
    y_train = train["qos_satisfied"].astype(bool).to_numpy()
    y_validation = validation["qos_satisfied"].astype(bool).to_numpy()
    candidate_results: dict[str, Any] = {}
    fitted: dict[str, Any] = {}
    for name, candidate in candidate_classifiers(seed).items():
        try:
            candidate.fit(x_train, y_train)
            probabilities = positive_probability(candidate, x_validation)
            threshold, threshold_metrics = select_threshold(y_validation, probabilities)
            prediction = probabilities >= threshold
            candidate_results[name] = {
                "status": "passed",
                "decision_threshold": threshold,
                "validation": {**classification_metrics(y_validation, prediction), **threshold_metrics},
            }
            fitted[name] = candidate
        except Exception as exc:  # candidate failure is recorded, not hidden.
            candidate_results[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    passed = [name for name, result in candidate_results.items() if result["status"] == "passed"]
    if not passed:
        raise ModelError("CM 所有候选均训练失败")
    selected = min(
        passed,
        key=lambda name: (-float(candidate_results[name]["validation"]["f1"]), name),
    )
    final_model = candidate_classifiers(seed)[selected]
    combined = pd.concat([train, validation], ignore_index=True)
    final_model.fit(model_feature_frame(combined), combined["qos_satisfied"].astype(bool).to_numpy())
    threshold = float(candidate_results[selected]["decision_threshold"])
    bundle = {
        "schema_version": 1,
        "task": "cm",
        "selected_candidate": selected,
        "decision_threshold": threshold,
        "feature_columns": list(FEATURE_COLUMNS),
        "model": final_model,
        "validation_prediction_sha256": prediction_sha256(threshold_predictions(final_model, x_validation, threshold)),
    }
    return bundle, candidate_results


def _train_regression(train: pd.DataFrame, validation: pd.DataFrame, *, seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
    x_train = model_feature_frame(train)
    x_validation = model_feature_frame(validation)
    y_train = train["retention_ratio"].astype(float).to_numpy()
    y_validation = validation["retention_ratio"].astype(float).to_numpy()
    candidate_results: dict[str, Any] = {}
    fitted: dict[str, Any] = {}
    for name, candidate in candidate_regressors(seed).items():
        try:
            candidate.fit(x_train, y_train)
            prediction = candidate.predict(x_validation)
            candidate_results[name] = {
                "status": "passed",
                "validation": regression_metrics(y_validation, prediction, validation["solo_fps"]),
            }
            fitted[name] = candidate
        except Exception as exc:
            candidate_results[name] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    passed = [name for name, result in candidate_results.items() if result["status"] == "passed"]
    if not passed:
        raise ModelError("RM 所有候选均训练失败")
    selected = min(
        passed,
        key=lambda name: (float(candidate_results[name]["validation"]["retention_mae"]), name),
    )
    final_model = candidate_regressors(seed)[selected]
    combined = pd.concat([train, validation], ignore_index=True)
    final_model.fit(model_feature_frame(combined), combined["retention_ratio"].astype(float).to_numpy())
    bundle = {
        "schema_version": 1,
        "task": "rm",
        "selected_candidate": selected,
        "feature_columns": list(FEATURE_COLUMNS),
        "model": final_model,
        "validation_prediction_sha256": prediction_sha256(final_model.predict(x_validation)),
    }
    return bundle, candidate_results


def train_models(*, repo_root: Path, dataset_dir: Path, output_dir: Path, task: str = "both", seed: int = 20260811) -> dict[str, Any]:
    """训练 CM/RM 候选，按 validation 选择后在 train+validation 上重拟合。"""

    root = repo_root.resolve()
    dataset = dataset_dir.resolve()
    out = output_dir.resolve()
    if task not in {"cm", "rm", "both"}:
        raise ModelError(f"task 必须是 cm/rm/both: {task}")
    tables = load_dataset_tables(dataset)
    split_info = validate_split_contract(tables, strict=True)
    feature_manifest = load_feature_manifest(dataset)
    expected_outputs = ["model-manifest.json", "training-summary.json", "candidate-metrics.json", "baselines.joblib"]
    if task in {"cm", "both"}:
        expected_outputs.append("cm.joblib")
    if task in {"rm", "both"}:
        expected_outputs.append("rm.joblib")
    existing = [out / name for name in expected_outputs if (out / name).exists()]
    if existing:
        raise FileExistsError("模型产物已存在，拒绝覆盖: " + ", ".join(map(str, existing)))
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed",
        "seed": seed,
        "task": task,
        "dataset_directory": str(dataset),
        "split_counts": split_info["counts"],
    }
    candidate_metrics: dict[str, Any] = {}
    bundles: dict[str, dict[str, Any]] = {}
    train_rm = tables["rm"].query("split == 'train'").reset_index(drop=True)
    validation_rm = tables["rm"].query("split == 'validation'").reset_index(drop=True)
    train_cm = tables["cm"].query("split == 'train'").reset_index(drop=True)
    validation_cm = tables["cm"].query("split == 'validation'").reset_index(drop=True)
    if task in {"cm", "both"}:
        cm_bundle, cm_candidates = _train_classification(train_cm, validation_cm, seed=seed)
        bundles["cm"] = cm_bundle
        candidate_metrics["cm"] = cm_candidates
        summary["cm"] = {
            "selected_candidate": cm_bundle["selected_candidate"],
            "decision_threshold": cm_bundle["decision_threshold"],
            "validation_positive_count": int(validation_cm["qos_satisfied"].sum()),
            "validation_sample_count": len(validation_cm),
        }
        _write_joblib(out / "cm.joblib", cm_bundle)
    if task in {"rm", "both"}:
        rm_bundle, rm_candidates = _train_regression(train_rm, validation_rm, seed=seed)
        bundles["rm"] = rm_bundle
        candidate_metrics["rm"] = rm_candidates
        summary["rm"] = {
            "selected_candidate": rm_bundle["selected_candidate"],
            "validation_sample_count": len(validation_rm),
            "validation_retention_mae": rm_candidates[rm_bundle["selected_candidate"]]["validation"]["retention_mae"],
        }
        _write_joblib(out / "rm.joblib", rm_bundle)
    combined_rm = pd.concat([train_rm, validation_rm], ignore_index=True)
    baseline_models = fit_baseline_models(combined_rm, seed=seed)
    _write_joblib(out / "baselines.joblib", baseline_models)
    write_json_exclusive(out / "candidate-metrics.json", {"schema_version": 1, "status": "passed", "candidates": candidate_metrics})
    model_manifest = {
        "schema_version": 1,
        "status": "passed",
        "task": task,
        "seed": seed,
        "feature_columns": list(FEATURE_COLUMNS),
        "target_id_in_model_features": False,
        "dataset_feature_manifest_sha256": file_sha256(dataset / "feature_manifest.json"),
        "dataset_tables": {name: file_sha256(dataset / filename) for name, filename in (("rm", "rm_samples.parquet"), ("cm", "cm_samples.parquet"), ("extra_rm", "extra_rm_samples.parquet"), ("extra_cm", "extra_cm_samples.parquet"))},
        "models": {
            name: {
                "path": f"{name}.joblib",
                "sha256": file_sha256(out / f"{name}.joblib"),
                "selected_candidate": bundle["selected_candidate"],
                "validation_prediction_sha256": bundle["validation_prediction_sha256"],
            }
            for name, bundle in bundles.items()
        },
        "baselines_sha256": file_sha256(out / "baselines.joblib"),
    }
    write_json_exclusive(out / "model-manifest.json", model_manifest)
    write_json_exclusive(out / "training-summary.json", summary)
    return {**summary, "models": model_manifest["models"], "candidate_metrics": candidate_metrics}
