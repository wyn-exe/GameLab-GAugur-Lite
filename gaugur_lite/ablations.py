"""Step 11 可复现消融：特征、标签、压力曲线和组合外推。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .config import config_sha256, load_yaml_mapping
from .features.dataset import FEATURE_COLUMNS, RESOURCES
from .models.classification import classification_metrics, candidate_classifiers, select_threshold, threshold_predictions
from .models.common import file_sha256, load_dataset_tables, load_feature_manifest, write_json_exclusive
from .models.regression import candidate_regressors, regression_metrics


class AblationError(RuntimeError):
    """消融配置、数据契约或结果质量门失败。"""


_SENSITIVITY_COLUMNS = tuple(column for column in FEATURE_COLUMNS if column.startswith("sensitivity_"))
_INTENSITY_COLUMNS = tuple(column for column in FEATURE_COLUMNS if column.startswith("intensity_"))
_P100_COLUMNS = tuple(column for column in _SENSITIVITY_COLUMNS if column.endswith("_p100"))


def _variant_features(table: pd.DataFrame, feature_mode: str) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """按照消融定义生成特征；所有派生列只来自当前样本已有字段。"""

    if feature_mode == "pressure_11_curve":
        raise AblationError("当前数据没有 11 档 profile，拒绝插值伪造")
    frame = table.loc[:, list(FEATURE_COLUMNS)].copy()
    if feature_mode == "full":
        columns = tuple(FEATURE_COLUMNS)
    elif feature_mode == "no_sensitivity":
        columns = tuple(column for column in FEATURE_COLUMNS if column not in _SENSITIVITY_COLUMNS)
        frame = frame.loc[:, list(columns)]
    elif feature_mode in {"no_intensity", "no_resource_utilization"}:
        columns = tuple(column for column in FEATURE_COLUMNS if column not in _INTENSITY_COLUMNS)
        frame = frame.loc[:, list(columns)]
    elif feature_mode == "intensity_sum":
        columns = tuple(column for column in FEATURE_COLUMNS if column not in _INTENSITY_COLUMNS) + tuple(
            f"intensity_sum_{resource}" for resource in RESOURCES
        )
        frame = frame.loc[:, [column for column in FEATURE_COLUMNS if column not in _INTENSITY_COLUMNS]]
        for resource in RESOURCES:
            frame[f"intensity_sum_{resource}"] = table[f"intensity_mean_{resource}"] * table["neighbor_count"]
    elif feature_mode == "max_pressure_only":
        columns = tuple(column for column in FEATURE_COLUMNS if column not in _SENSITIVITY_COLUMNS) + _P100_COLUMNS
        frame = pd.concat(
            [
                table.loc[:, [column for column in FEATURE_COLUMNS if column not in _SENSITIVITY_COLUMNS]],
                table.loc[:, list(_P100_COLUMNS)],
            ],
            axis=1,
        )
    else:
        raise AblationError(f"未知 feature_mode: {feature_mode}")
    if "target_id" in columns:
        raise AblationError("target_id 禁止进入消融特征")
    return frame.loc[:, list(columns)].astype(float), columns


def _targets(table: pd.DataFrame, label_mode: str, task: str) -> np.ndarray:
    if label_mode not in {"mean_fps", "p05_fps"}:
        raise AblationError(f"未知 label_mode: {label_mode}")
    if task == "rm":
        column = "retention_ratio" if label_mode == "mean_fps" else "p05_retention_ratio"
        return table[column].astype(float).to_numpy()
    if task == "cm":
        if label_mode == "mean_fps":
            return table["qos_satisfied"].astype(bool).to_numpy()
        return (table["p05_retention_ratio"].astype(float) >= table["qos_ratio"].astype(float)).to_numpy()
    raise AblationError(f"未知 task: {task}")


def _bootstrap(
    table: pd.DataFrame,
    prediction: np.ndarray,
    metric: Callable[[pd.DataFrame, np.ndarray], dict[str, Any]],
    *,
    seed: int,
    repeats: int,
) -> dict[str, Any]:
    groups = table["combination_key"].astype(str).to_numpy()
    unique_groups = np.asarray(sorted(set(groups)))
    if not len(unique_groups):
        raise AblationError("消融评估表为空")
    point = metric(table, prediction)
    numeric = {
        key: []
        for key, value in point.items()
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    }
    rng = np.random.default_rng(seed)
    for _ in range(repeats):
        selected = rng.choice(unique_groups, size=len(unique_groups), replace=True)
        indices = np.concatenate([np.flatnonzero(groups == group) for group in selected])
        result = metric(table.iloc[indices].reset_index(drop=True), prediction[indices])
        for key in numeric:
            numeric[key].append(float(result[key]))
    point["bootstrap_repeats"] = repeats
    point["bootstrap_ci95"] = {
        key: [float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))]
        for key, values in numeric.items()
    }
    return point


def _classification_metric(table: pd.DataFrame, prediction: np.ndarray, label_mode: str) -> dict[str, Any]:
    return classification_metrics(_targets(table, label_mode, "cm"), prediction)


def _regression_metric(table: pd.DataFrame, prediction: np.ndarray, label_mode: str) -> dict[str, Any]:
    return regression_metrics(_targets(table, label_mode, "rm"), prediction, table["solo_fps"].to_numpy())


def _fit_variant(
    *,
    tables: dict[str, pd.DataFrame],
    variant: dict[str, Any],
    seed: int,
    bootstrap_repeats: int,
    cm_candidate: str,
    rm_candidate: str,
) -> dict[str, Any]:
    variant_id = str(variant["id"])
    feature_mode = str(variant.get("feature_mode", "full"))
    label_mode = str(variant.get("label_mode", "mean_fps"))
    protocol = str(variant.get("protocol", "standard"))
    if feature_mode == "pressure_11_curve":
        return {
            "id": variant_id,
            "status": "skipped",
            "description": variant.get("description", ""),
            "reason": "Step 9 feature manifest contains only p000/p025/p050/p075/p100; no 11 档实测 profile",
            "fabricated_interpolation": False,
        }

    train_rm = tables["rm"].query("split == 'train'").copy()
    validation_rm = tables["rm"].query("split == 'validation'").copy()
    test_rm = tables["rm"].query("split == 'test'").copy()
    train_cm = tables["cm"].query("split == 'train'").copy()
    validation_cm = tables["cm"].query("split == 'validation'").copy()
    test_cm = tables["cm"].query("split == 'test'").copy()
    extra_rm = tables["extra_rm"].copy()
    extra_cm = tables["extra_cm"].copy()
    notes: list[str] = []
    if feature_mode == "no_resource_utilization":
        notes.append("Step 9 未保存独立 raw utilization 列；本变体保守移除现有 intensity mean/variance 代理，因此与 no_intensity 特征集合相同")
    if protocol == "pair_train_triple_test":
        train_rm = pd.concat([train_rm, validation_rm], ignore_index=True).query("combination_size == 2").reset_index(drop=True)
        train_cm = pd.concat([train_cm, validation_cm], ignore_index=True).query("combination_size == 2").reset_index(drop=True)
        validation_rm = train_rm.iloc[0:0].copy()
        validation_cm = train_cm.iloc[0:0].copy()
        test_rm = test_rm.query("combination_size == 3").reset_index(drop=True)
        test_cm = test_cm.query("combination_size == 3").reset_index(drop=True)
        notes.append("train 使用主 train+validation 中的 pair，test 只使用 triple；extra_test 保留为四元补充报告")

    fit_rm = pd.concat([train_rm, validation_rm], ignore_index=True) if len(validation_rm) else train_rm
    fit_cm = pd.concat([train_cm, validation_cm], ignore_index=True) if len(validation_cm) else train_cm
    x_train_rm, columns = _variant_features(fit_rm, feature_mode)
    x_test_rm, test_columns = _variant_features(test_rm, feature_mode)
    x_extra_rm, extra_columns = _variant_features(extra_rm, feature_mode)
    if columns != test_columns or columns != extra_columns:
        raise AblationError(f"{variant_id} RM 特征列不稳定")
    regressors = candidate_regressors(seed)
    if rm_candidate not in regressors:
        raise AblationError(f"未知 RM candidate: {rm_candidate}")
    rm_model = regressors[rm_candidate]
    y_train_rm = _targets(fit_rm, label_mode, "rm")
    rm_model.fit(x_train_rm, y_train_rm)
    rm_test_prediction = np.asarray(rm_model.predict(x_test_rm), dtype=float)
    rm_extra_prediction = np.asarray(rm_model.predict(x_extra_rm), dtype=float)

    x_train_cm, cm_columns = _variant_features(train_cm, feature_mode)
    x_test_cm, cm_test_columns = _variant_features(test_cm, feature_mode)
    x_extra_cm, cm_extra_columns = _variant_features(extra_cm, feature_mode)
    if cm_columns != cm_test_columns or cm_columns != cm_extra_columns or cm_columns != columns:
        raise AblationError(f"{variant_id} CM/RM 特征列不一致")
    classifiers = candidate_classifiers(seed)
    if cm_candidate not in classifiers:
        raise AblationError(f"未知 CM candidate: {cm_candidate}")
    cm_model = classifiers[cm_candidate]
    y_train_cm = _targets(train_cm, label_mode, "cm")
    cm_model.fit(x_train_cm, y_train_cm)
    if len(validation_cm):
        x_validation_cm, validation_columns = _variant_features(validation_cm, feature_mode)
        if validation_columns != columns:
            raise AblationError(f"{variant_id} validation 特征列不稳定")
        threshold = select_threshold(
            _targets(validation_cm, label_mode, "cm"),
            np.asarray(cm_model.predict_proba(x_validation_cm)[:, 1] if len(cm_model.classes_) > 1 else np.full(len(validation_cm), bool(cm_model.classes_[0])), dtype=float),
        )[0]
    else:
        threshold = 0.5
    # 阈值只用 validation 选择，冻结后再以 train+validation 重拟合最终 CM。
    if len(validation_cm):
        cm_model = candidate_classifiers(seed)[cm_candidate]
        x_fit_cm, fit_columns = _variant_features(fit_cm, feature_mode)
        if fit_columns != columns:
            raise AblationError(f"{variant_id} 最终 CM 特征列不稳定")
        cm_model.fit(x_fit_cm, _targets(fit_cm, label_mode, "cm"))
    cm_test_prediction = threshold_predictions(cm_model, x_test_cm, threshold)
    cm_extra_prediction = threshold_predictions(cm_model, x_extra_cm, threshold)

    rm_metric = lambda table, prediction: _regression_metric(table, prediction, label_mode)
    cm_metric = lambda table, prediction: _classification_metric(table, prediction, label_mode)
    result: dict[str, Any] = {
        "id": variant_id,
        "status": "passed",
        "description": variant.get("description", ""),
        "feature_mode": feature_mode,
        "label_mode": label_mode,
        "protocol": protocol,
        "feature_columns": list(columns),
        "feature_count": len(columns),
        "cm_decision_threshold": float(threshold),
        "cm_candidate": cm_candidate,
        "rm_candidate": rm_candidate,
        "row_counts": {
            "train_rm": len(train_rm),
            "train_cm": len(train_cm),
            "validation_rm": len(validation_rm),
            "validation_cm": len(validation_cm),
            "fit_rm": len(fit_rm),
            "fit_cm": len(fit_cm),
            "test_rm": len(test_rm),
            "test_cm": len(test_cm),
            "extra_rm": len(extra_rm),
            "extra_cm": len(extra_cm),
        },
        "notes": notes,
        "rm": {
            "test": _bootstrap(test_rm, rm_test_prediction, rm_metric, seed=seed + 11, repeats=bootstrap_repeats),
            "extra_test": _bootstrap(extra_rm, rm_extra_prediction, rm_metric, seed=seed + 12, repeats=bootstrap_repeats),
        },
        "cm": {
            "test": _bootstrap(test_cm, cm_test_prediction, cm_metric, seed=seed + 21, repeats=bootstrap_repeats),
            "extra_test": _bootstrap(extra_cm, cm_extra_prediction, cm_metric, seed=seed + 22, repeats=bootstrap_repeats),
        },
    }
    return result


def _plot_ablation(path: Path, results: list[dict[str, Any]]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover
        raise AblationError("Step 11 图表需要 matplotlib") from exc
    if path.exists():
        raise FileExistsError(f"拒绝覆盖消融图表: {path}")
    usable = [result for result in results if result["status"] == "passed"]
    names = [result["id"] for result in usable]
    test_values = [result["rm"]["test"]["retention_mae"] for result in usable]
    extra_values = [result["rm"]["extra_test"]["retention_mae"] for result in usable]
    x = np.arange(len(names))
    width = 0.38
    figure, axis = plt.subplots(figsize=(12, 5), constrained_layout=True)
    axis.bar(x - width / 2, test_values, width, label="test")
    axis.bar(x + width / 2, extra_values, width, label="extra_test")
    axis.set_xticks(x, names, rotation=35, ha="right")
    axis.set_ylabel("retention MAE")
    axis.set_title("Step 11 ablation RM retention MAE")
    axis.grid(axis="y", alpha=0.25)
    axis.legend()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=160)
    plt.close(figure)


def run_ablations(
    *,
    repo_root: Path,
    dataset_dir: Path,
    spec_path: Path,
    output_dir: Path,
    seed: int | None = None,
    bootstrap_repeats: int | None = None,
) -> dict[str, Any]:
    """运行配置中的消融并写入 JSON/图表；缺失 11 档只允许显式 skipped。"""

    del repo_root
    dataset = dataset_dir.resolve()
    spec_file = spec_path.resolve()
    out = output_dir.resolve()
    spec = load_yaml_mapping(spec_file)
    variants = spec.get("variants")
    if not isinstance(variants, list) or not variants:
        raise AblationError("消融 spec 必须包含非空 variants 列表")
    run_seed = int(spec.get("seed", 20260811) if seed is None else seed)
    repeats = int(spec.get("bootstrap_repeats", 200) if bootstrap_repeats is None else bootstrap_repeats)
    if repeats < 20 or repeats > 5000:
        raise AblationError("bootstrap_repeats 必须在 20..5000")
    tables = load_dataset_tables(dataset)
    load_feature_manifest(dataset)
    cm_candidate = str(spec.get("cm_candidate", "decision_tree"))
    rm_candidate = str(spec.get("rm_candidate", "gradient_boosting"))
    from .models.common import validate_split_contract

    split_info = validate_split_contract(tables, strict=True)
    required = {"id", "feature_mode", "label_mode"}
    if any(not required.issubset(item) for item in variants if isinstance(item, dict)):
        raise AblationError("每个消融 variant 必须含 id/feature_mode/label_mode")
    results = []
    for item in variants:
        if not isinstance(item, dict):
            raise AblationError("消融 variant 必须为对象")
        results.append(
            _fit_variant(
                tables=tables,
                variant=item,
                seed=run_seed,
                bootstrap_repeats=repeats,
                cm_candidate=cm_candidate,
                rm_candidate=rm_candidate,
            )
        )
    names = [str(item["id"]) for item in variants]
    result_names = [str(item["id"]) for item in results]
    checks = {
        "variant_set_complete": names == result_names and len(set(result_names)) == len(result_names),
        "split_contract_passed": split_info["main_key_count"] == 60 and split_info["extra_key_count"] == 12,
        "target_id_not_in_any_features": all("target_id" not in item.get("feature_columns", []) for item in results),
        "bootstrap_ci_present": all(
            "bootstrap_ci95" in result[task][split]
            for result in results
            if result["status"] == "passed"
            for task in ("cm", "rm")
            for split in ("test", "extra_test")
        ),
        "pair_triple_protocol_rows_present": next(
            (result["row_counts"]["test_rm"] > 0 and result["row_counts"]["train_rm"] > 0 for result in results if result["id"] == "pair_train_triple_test" and result["status"] == "passed"),
            False,
        ),
        "pressure_11_not_fabricated": all(
            result.get("fabricated_interpolation") is False
            for result in results
            if result["id"] == "pressure_11_curve"
        ),
    }
    if not all(checks.values()):
        raise AblationError(f"消融质量门失败: {checks}")
    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": 1,
        "status": "passed",
        "seed": run_seed,
        "bootstrap_repeats": repeats,
        "cm_candidate": cm_candidate,
        "rm_candidate": rm_candidate,
        "spec_sha256": file_sha256(spec_file),
        "spec_config_sha256": config_sha256(spec),
        "dataset_feature_manifest_sha256": file_sha256(dataset / "feature_manifest.json"),
        "checks": checks,
        "check_count": len(checks),
        "passed_count": sum(checks.values()),
        "split_counts": split_info["counts"],
        "variants": results,
        "artifacts": {"summary": "ablation-summary.json", "rm_mae_plot": "ablation-rm-mae.png"},
    }
    write_json_exclusive(out / "ablation-summary.json", summary)
    _plot_ablation(out / "ablation-rm-mae.png", results)
    return summary
