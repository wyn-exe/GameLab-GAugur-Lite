"""CM/RM 主测试、四元外推、基线比较与组合级 bootstrap。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

import joblib
import numpy as np
import pandas as pd

from .baselines import predict_baseline
from .classification import classification_metrics, threshold_predictions
from .common import ModelError, file_sha256, load_dataset_tables, load_feature_manifest, model_feature_frame, prediction_sha256, read_json, validate_split_contract, write_json_exclusive
from .regression import regression_metrics


def _bootstrap(
    table: pd.DataFrame,
    prediction: np.ndarray,
    metric: Callable[[pd.DataFrame, np.ndarray], dict[str, Any]],
    *,
    seed: int,
    repeats: int = 200,
) -> dict[str, Any]:
    groups = table["combination_key"].astype(str).to_numpy()
    unique_groups = np.asarray(sorted(set(groups)))
    rng = np.random.default_rng(seed)
    point = metric(table, prediction)
    samples: dict[str, list[float]] = {key: [] for key, value in point.items() if isinstance(value, (int, float)) and math.isfinite(float(value))}
    for _ in range(repeats):
        selected_groups = rng.choice(unique_groups, size=len(unique_groups), replace=True)
        indices = np.concatenate([np.flatnonzero(groups == group) for group in selected_groups])
        result = metric(table.iloc[indices].reset_index(drop=True), prediction[indices])
        for key in samples:
            samples[key].append(float(result[key]))
    point["bootstrap_repeats"] = repeats
    point["bootstrap_ci95"] = {
        key: [float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))]
        for key, values in samples.items()
    }
    return point


def _classification_metric(table: pd.DataFrame, prediction: np.ndarray) -> dict[str, Any]:
    return classification_metrics(table["qos_satisfied"].astype(bool).to_numpy(), prediction)


def _regression_metric(table: pd.DataFrame, prediction: np.ndarray) -> dict[str, Any]:
    return regression_metrics(table["retention_ratio"].to_numpy(), prediction, table["solo_fps"].to_numpy())


def _save_plot(path: Path, writer: Callable[[Any], None]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - formal env supplies matplotlib.
        raise ModelError("Step 10 图表需要 matplotlib") from exc
    if path.exists():
        raise FileExistsError(f"拒绝覆盖评估图表: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(8, 5), constrained_layout=True)
    writer((figure, axis))
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _plot_error_cdf(path: Path, errors: dict[str, np.ndarray]) -> None:
    def writer(canvas: Any) -> None:
        _, axis = canvas
        for name, values in errors.items():
            selected = np.sort(np.abs(values))
            y = np.linspace(1.0 / len(selected), 1.0, len(selected))
            axis.plot(selected, y, label=name)
        axis.set_xlabel("absolute retention error")
        axis.set_ylabel("CDF")
        axis.set_title("RM absolute retention error CDF (test)")
        axis.grid(alpha=0.25)
        axis.legend(loc="best")

    _save_plot(path, writer)


def _plot_confusion(path: Path, matrices: dict[str, list[list[int]]]) -> None:
    def writer(canvas: Any) -> None:
        import matplotlib.pyplot as plt

        _, axis = canvas
        names = list(matrices)
        values = np.asarray([matrices[name] for name in names], dtype=int)
        image = axis.imshow(values.reshape(len(names), 4), cmap="Blues", aspect="auto")
        axis.set_xticks(range(4), ["TN", "FP", "FN", "TP"])
        axis.set_yticks(range(len(names)), names)
        for row_index, row in enumerate(values.reshape(len(names), 4)):
            for column_index, value in enumerate(row):
                axis.text(column_index, row_index, str(int(value)), ha="center", va="center")
        axis.set_title("CM confusion matrices (test)")
        plt.colorbar(image, ax=axis, fraction=0.046, pad=0.04)

    _save_plot(path, writer)


def evaluate_models(
    *, repo_root: Path, model_dir: Path, dataset_dir: Path, output_dir: Path, seed: int = 20260811, bootstrap_repeats: int = 200
) -> dict[str, Any]:
    """加载已保存模型，在 test/extra_test 上统一评估并保存指标与图表。"""

    model_root = model_dir.resolve()
    dataset = dataset_dir.resolve()
    out = output_dir.resolve()
    manifest = load_feature_manifest(dataset)
    tables = load_dataset_tables(dataset)
    split_info = validate_split_contract(tables, strict=True)
    model_manifest = read_json(model_root / "model-manifest.json")
    if model_manifest.get("feature_columns") != manifest.get("feature_columns"):
        raise ModelError("模型与数据集 feature_columns 不一致")
    cm_bundle = joblib.load(model_root / "cm.joblib")
    rm_bundle = joblib.load(model_root / "rm.joblib")
    baseline_models = joblib.load(model_root / "baselines.joblib")
    expected_outputs = [out / name for name in ("evaluation-summary.json", "rm-error-cdf.png", "cm-confusion-matrices.png")]
    existing = [path for path in expected_outputs if path.exists()]
    if existing:
        raise FileExistsError("评估产物已存在，拒绝覆盖: " + ", ".join(map(str, existing)))
    checks: dict[str, bool] = {}
    validation_cm = tables["cm"].query("split == 'validation'").reset_index(drop=True)
    validation_rm = tables["rm"].query("split == 'validation'").reset_index(drop=True)
    cm_validation_prediction = threshold_predictions(cm_bundle["model"], model_feature_frame(validation_cm), cm_bundle["decision_threshold"])
    rm_validation_prediction = np.asarray(rm_bundle["model"].predict(model_feature_frame(validation_rm)), dtype=float)
    checks["cm_model_load_prediction_matches"] = prediction_sha256(cm_validation_prediction) == model_manifest["models"]["cm"]["validation_prediction_sha256"]
    checks["rm_model_load_prediction_matches"] = prediction_sha256(rm_validation_prediction) == model_manifest["models"]["rm"]["validation_prediction_sha256"]
    checks["model_feature_columns_exclude_target_id"] = "target_id" not in model_manifest.get("feature_columns", [])
    metrics: dict[str, Any] = {"cm": {}, "rm": {}, "baselines": {}}
    errors_for_plot: dict[str, np.ndarray] = {}
    confusion_for_plot: dict[str, list[list[int]]] = {}
    for split_name, rm_table, cm_table in (
        ("test", tables["rm"].query("split == 'test'").reset_index(drop=True), tables["cm"].query("split == 'test'").reset_index(drop=True)),
        ("extra_test", tables["extra_rm"].reset_index(drop=True), tables["extra_cm"].reset_index(drop=True)),
    ):
        cm_prediction = threshold_predictions(cm_bundle["model"], model_feature_frame(cm_table), cm_bundle["decision_threshold"])
        rm_prediction = np.asarray(rm_bundle["model"].predict(model_feature_frame(rm_table)), dtype=float)
        cm_result = _bootstrap(cm_table, cm_prediction, _classification_metric, seed=seed + len(split_name), repeats=bootstrap_repeats)
        rm_result = _bootstrap(rm_table, rm_prediction, _regression_metric, seed=seed + len(split_name) + 100, repeats=bootstrap_repeats)
        rm_result["by_combination_size"] = {
            str(size): regression_metrics(
                group["retention_ratio"],
                rm_prediction[group.index.to_numpy()],
                group["solo_fps"],
            )
            for size, group in rm_table.groupby("combination_size", sort=True)
        }
        metrics["cm"][split_name] = {"selected_model": cm_result}
        metrics["rm"][split_name] = {"selected_model": rm_result}
        # 图表固定展示主 test split，避免 extra_test 覆盖 test 误差。
        if split_name == "test":
            errors_for_plot["selected_model"] = rm_prediction - rm_table["retention_ratio"].to_numpy()
            confusion_for_plot["selected_model"] = cm_result["confusion_matrix"]
        for name, baseline in baseline_models.items():
            baseline_rm_prediction = predict_baseline(baseline, rm_table)
            baseline_cm_prediction = predict_baseline(baseline, cm_table) >= cm_table["qos_ratio"].to_numpy()
            metrics["baselines"].setdefault(name, {})[split_name] = {
                "rm": _bootstrap(rm_table, baseline_rm_prediction, _regression_metric, seed=seed + len(name) + 200, repeats=bootstrap_repeats),
                "cm": _bootstrap(cm_table, baseline_cm_prediction, _classification_metric, seed=seed + len(name) + 300, repeats=bootstrap_repeats),
            }
            if split_name == "test":
                errors_for_plot[name] = baseline_rm_prediction - rm_table["retention_ratio"].to_numpy()
                confusion_for_plot[name] = metrics["baselines"][name][split_name]["cm"]["confusion_matrix"]
    _plot_error_cdf(out / "rm-error-cdf.png", errors_for_plot)
    _plot_confusion(out / "cm-confusion-matrices.png", confusion_for_plot)
    checks["test_and_extra_test_reported_separately"] = set(metrics["cm"]["test"]) == {"selected_model"} and set(metrics["cm"]["extra_test"]) == {"selected_model"}
    checks["bootstrap_ci_present"] = all(
        "bootstrap_ci95" in metrics[task][split]["selected_model"]
        for task in ("cm", "rm")
        for split in ("test", "extra_test")
    )
    checks["same_test_rows_for_baselines"] = all(
        metrics["baselines"][name]["test"]["rm"]["sample_count"] == len(tables["rm"].query("split == 'test'") )
        for name in baseline_models
    )
    checks["split_contract_passed"] = split_info["main_key_count"] == 60 and split_info["extra_key_count"] == 12
    result = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "seed": seed,
        "bootstrap_repeats": bootstrap_repeats,
        "model_manifest_sha256": file_sha256(model_root / "model-manifest.json"),
        "checks": checks,
        "check_count": len(checks),
        "passed_count": sum(checks.values()),
        "metrics": metrics,
        "artifacts": {
            "evaluation_summary": "evaluation-summary.json",
            "rm_error_cdf": "rm-error-cdf.png",
            "cm_confusion_matrices": "cm-confusion-matrices.png",
        },
    }
    write_json_exclusive(out / "evaluation-summary.json", result)
    return result
