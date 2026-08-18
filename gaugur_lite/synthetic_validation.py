"""用可控的非线性交互数据验证 CM/RM 的算法行为。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error

from .config import stable_json_dumps
from .models.classification import candidate_classifiers
from .models.regression import candidate_regressors


class SyntheticValidationError(RuntimeError):
    """合成算法验收输入或质量门失败。"""


def build_synthetic_interaction_table(
    *, seed: int = 20260818, workload_count: int = 16, repeats: int = 3
) -> pd.DataFrame:
    """生成带有均值/方差聚合和非线性交互项的固定表，不涉及真实游戏。"""

    if workload_count < 8 or repeats < 1:
        raise SyntheticValidationError("workload_count 至少为 8，repeats 至少为 1")
    rng = np.random.default_rng(seed)
    resources = 4
    sensitivity = rng.uniform(0.25, 0.95, size=(workload_count, resources))
    intensity = rng.uniform(0.10, 0.95, size=(workload_count, resources))
    solo_fps = rng.uniform(75.0, 150.0, size=workload_count)
    rows: list[dict[str, Any]] = []
    # 固定组合键作为分组单位，避免同一组合的重复样本跨 split。
    for size, combination_count in ((2, 180), (3, 100), (4, 60)):
        for combination_index in range(combination_count):
            members = tuple(sorted(rng.choice(workload_count, size=size, replace=False)))
            group_key = "+".join(f"w{member:02d}" for member in members)
            neighbor = intensity[list(members)]
            means = neighbor.mean(axis=0)
            variances = neighbor.var(axis=0)
            for repeat in range(repeats):
                for target in members:
                    target_sensitivity = sensitivity[target]
                    # 线性项 + 平方项 + 资源间交互项，故线性加和基线不完整。
                    linear_loss = 0.06 * float(np.dot(target_sensitivity, means))
                    nonlinear_loss = 0.55 * float(
                        (target_sensitivity[0] * means[1]) ** 2
                        + (target_sensitivity[2] * means[3]) ** 2
                    )
                    cross_loss = 0.25 * float(
                        target_sensitivity[0]
                        * means[1]
                        * target_sensitivity[2]
                        * means[3]
                    )
                    variance_loss = 0.06 * float(np.dot(target_sensitivity, variances))
                    noise = float(rng.normal(0.0, 0.004))
                    retention = float(
                        np.clip(1.04 - linear_loss - nonlinear_loss - cross_loss - variance_loss + noise, 0.20, 1.10)
                    )
                    row: dict[str, Any] = {
                        "combination_key": group_key,
                        "repeat": repeat,
                        "target_id": f"w{target:02d}",
                        "combination_size": size,
                        "solo_fps": float(solo_fps[target]),
                        "retention_ratio": retention,
                        "qos_satisfied": retention >= 0.80,
                    }
                    for resource_index in range(resources):
                        row[f"sensitivity_{resource_index}"] = float(target_sensitivity[resource_index])
                        row[f"neighbor_mean_{resource_index}"] = float(means[resource_index])
                        row[f"neighbor_var_{resource_index}"] = float(variances[resource_index])
                    rows.append(row)
    table = pd.DataFrame(rows)
    if table["qos_satisfied"].nunique() != 2:
        raise SyntheticValidationError("合成标签退化为单类，请调整生成公式")
    return table


def _feature_columns() -> list[str]:
    columns = ["combination_size"]
    for resource_index in range(4):
        columns.extend(
            [
                f"sensitivity_{resource_index}",
                f"neighbor_mean_{resource_index}",
                f"neighbor_var_{resource_index}",
            ]
        )
    return columns


def _group_split(table: pd.DataFrame, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    groups = np.array(sorted(table["combination_key"].unique()))
    rng = np.random.default_rng(seed)
    rng.shuffle(groups)
    cutoff = max(1, int(len(groups) * 0.70))
    train_groups = set(groups[:cutoff])
    train = table[table["combination_key"].isin(train_groups)].copy()
    test = table[~table["combination_key"].isin(train_groups)].copy()
    return train, test


def run_synthetic_validation(
    *, output_dir: Path, seed: int = 20260818
) -> dict[str, Any]:
    """训练 CM/RM 与两个简化基线，并写出可复核 JSON/CSV/PNG。"""

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "json": output_dir / "synthetic-validation.json",
        "csv": output_dir / "synthetic-validation-metrics.csv",
        "plot": output_dir / "synthetic-validation-metrics.png",
    }
    existing = [path for path in outputs.values() if path.exists()]
    if existing:
        raise FileExistsError(f"合成验收产物已存在，拒绝覆盖: {existing[0]}")
    table = build_synthetic_interaction_table(seed=seed)
    train, test = _group_split(table, seed)
    feature_columns = _feature_columns()
    x_train = train[feature_columns]
    x_test = test[feature_columns]
    y_train_cm = train["qos_satisfied"].astype(bool)
    y_test_cm = test["qos_satisfied"].astype(bool)
    y_train_rm = train["retention_ratio"].astype(float)
    y_test_rm = test["retention_ratio"].astype(float)

    cm_model = candidate_classifiers(seed)["gradient_boosting"]
    cm_model.fit(x_train, y_train_cm)
    cm_prediction = cm_model.predict(x_test).astype(bool)
    rm_model = candidate_regressors(seed)["gradient_boosting"]
    rm_model.fit(x_train, y_train_rm)
    rm_prediction = rm_model.predict(x_test)

    # 只用共置数量的 baseline；线性 baseline 只看一阶资源相互作用。
    count_train = train[["combination_size"]]
    count_test = test[["combination_size"]]
    count_cm = LogisticRegression(random_state=seed).fit(count_train, y_train_cm)
    count_cm_prediction = count_cm.predict(count_test).astype(bool)
    count_rm = LinearRegression().fit(count_train, y_train_rm)
    count_rm_prediction = count_rm.predict(count_test)
    additive_columns = [
        "combination_size",
        *[f"sensitivity_{index}" for index in range(4)],
        *[f"neighbor_mean_{index}" for index in range(4)],
    ]
    additive_cm = LogisticRegression(max_iter=1000, random_state=seed).fit(
        train[additive_columns], y_train_cm
    )
    additive_cm_prediction = additive_cm.predict(test[additive_columns]).astype(bool)
    additive_rm = LinearRegression().fit(train[additive_columns], y_train_rm)
    additive_rm_prediction = additive_rm.predict(test[additive_columns])

    metrics = [
        {
            "model": "gaugur_gradient_boosting",
            "task": "cm",
            "metric": "f1",
            "value": float(f1_score(y_test_cm, cm_prediction)),
        },
        {
            "model": "count_only",
            "task": "cm",
            "metric": "f1",
            "value": float(f1_score(y_test_cm, count_cm_prediction)),
        },
        {
            "model": "linear_additive",
            "task": "cm",
            "metric": "f1",
            "value": float(f1_score(y_test_cm, additive_cm_prediction)),
        },
        {
            "model": "gaugur_gradient_boosting",
            "task": "rm",
            "metric": "retention_mae",
            "value": float(mean_absolute_error(y_test_rm, rm_prediction)),
        },
        {
            "model": "count_only",
            "task": "rm",
            "metric": "retention_mae",
            "value": float(mean_absolute_error(y_test_rm, count_rm_prediction)),
        },
        {
            "model": "linear_additive",
            "task": "rm",
            "metric": "retention_mae",
            "value": float(mean_absolute_error(y_test_rm, additive_rm_prediction)),
        },
    ]
    cm_gaugur = next(item["value"] for item in metrics if item["model"] == "gaugur_gradient_boosting" and item["task"] == "cm")
    cm_baselines = [item["value"] for item in metrics if item["task"] == "cm" and item["model"] != "gaugur_gradient_boosting"]
    rm_gaugur = next(item["value"] for item in metrics if item["model"] == "gaugur_gradient_boosting" and item["task"] == "rm")
    rm_baselines = [item["value"] for item in metrics if item["task"] == "rm" and item["model"] != "gaugur_gradient_boosting"]
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed" if cm_gaugur >= max(cm_baselines) and rm_gaugur <= min(rm_baselines) else "failed",
        "validation": "synthetic_non_linear_interaction_only",
        "seed": seed,
        "rows": {"total": len(table), "train": len(train), "test": len(test)},
        "label_counts": {
            "train_positive": int(y_train_cm.sum()),
            "train_negative": int((~y_train_cm).sum()),
            "test_positive": int(y_test_cm.sum()),
            "test_negative": int((~y_test_cm).sum()),
        },
        "metrics": metrics,
        "checks": {
            "group_disjoint": not bool(set(train["combination_key"]) & set(test["combination_key"])),
            "both_test_labels_present": y_test_cm.nunique() == 2,
            "gaugur_cm_beats_baselines": cm_gaugur >= max(cm_baselines),
            "gaugur_rm_beats_baselines": rm_gaugur <= min(rm_baselines),
        },
        "interpretation": "仅验证实现能学习预先构造的非线性交互，不证明真实游戏域有效。",
    }
    outputs["json"].write_text(stable_json_dumps(result, indent=2) + "\n", encoding="utf-8")
    pd.DataFrame(metrics).to_csv(outputs["csv"], index=False)
    try:
        import matplotlib.pyplot as plt

        figure, axes = plt.subplots(1, 2, figsize=(10, 4.2))
        cm_items = [item for item in metrics if item["task"] == "cm"]
        rm_items = [item for item in metrics if item["task"] == "rm"]
        axes[0].bar([item["model"] for item in cm_items], [item["value"] for item in cm_items])
        axes[0].set_title("CM test F1 (higher is better)")
        axes[0].tick_params(axis="x", rotation=25)
        axes[1].bar([item["model"] for item in rm_items], [item["value"] for item in rm_items])
        axes[1].set_title("RM test retention MAE (lower is better)")
        axes[1].tick_params(axis="x", rotation=25)
        figure.tight_layout()
        figure.savefig(outputs["plot"], dpi=160)
        plt.close(figure)
    except ImportError as exc:  # pragma: no cover - formal environment includes matplotlib.
        raise SyntheticValidationError("合成验收图表需要 matplotlib") from exc
    return result
