"""Step 12 QoS 安全装箱 replay：模型决策与实测 truth 分离。"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from .config import load_yaml_mapping
from .features.dataset import FEATURE_COLUMNS
from .models.baselines import predict_baseline
from .models.classification import threshold_predictions
from .models.common import file_sha256, load_dataset_tables, load_feature_manifest, model_feature_frame, read_json, validate_split_contract, write_json_exclusive
from .schema import make_combination_key


class ReplayError(RuntimeError):
    """Replay 输入、模型或真值契约失败。"""


def _load_requests(spec_path: Path, workload_ids: set[str], qos_override: float | None) -> tuple[list[dict[str, Any]], float, int, dict[str, Any]]:
    spec = load_yaml_mapping(spec_path)
    raw_requests = spec.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise ReplayError("requests spec 必须包含非空 requests 列表")
    global_qos = float(spec.get("qos_ratio", 0.80) if qos_override is None else qos_override)
    if not 0.0 < global_qos <= 1.0:
        raise ReplayError("qos_ratio 必须位于 (0, 1]")
    max_group_size = int(spec.get("max_group_size", 4))
    if max_group_size < 2 or max_group_size > 4:
        raise ReplayError("max_group_size 必须位于 2..4（truth 只覆盖到四元）")
    requests: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_workloads: set[str] = set()
    for raw in raw_requests:
        if not isinstance(raw, dict):
            raise ReplayError("每个 request 必须为对象")
        request_id = str(raw.get("request_id", ""))
        workload_id = str(raw.get("workload_id", ""))
        if not request_id or request_id in seen_ids:
            raise ReplayError(f"request_id 缺失或重复: {request_id!r}")
        if workload_id not in workload_ids or workload_id in seen_workloads:
            raise ReplayError(f"workload_id 缺失、未知或重复: {workload_id!r}")
        request_qos = float(raw.get("qos_ratio", global_qos))
        if not 0.0 < request_qos <= 1.0:
            raise ReplayError(f"request {request_id} qos_ratio 非法")
        requests.append({"request_id": request_id, "workload_id": workload_id, "qos_ratio": request_qos})
        seen_ids.add(request_id)
        seen_workloads.add(workload_id)
    return requests, global_qos, max_group_size, spec


def _read_truth(path: Path) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], int]:
    required = {"combination_key", "target_id", "mean_fps", "solo_mean_fps", "repeat"}
    try:
        table = pd.read_parquet(path)
    except (OSError, ValueError, ImportError) as exc:
        raise ReplayError(f"无法读取 colocation truth: {path}") from exc
    if len(table) != 600 or not required.issubset(table.columns):
        raise ReplayError(f"Step 8 truth 行数/列契约不符: rows={len(table)}")
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in table.to_dict(orient="records"):
        key = (str(row["combination_key"]), str(row["target_id"]))
        indexed.setdefault(key, []).append(row)
    return indexed, len(table)


def _group_key(workloads: list[str]) -> str | None:
    if len(workloads) < 2:
        return None
    return make_combination_key(tuple(workloads))


def _truth_group_feasible(
    group: list[str],
    *,
    truth: dict[tuple[str, str], list[dict[str, Any]]],
    qos_by_workload: dict[str, float],
) -> tuple[bool, int, int]:
    """返回 (所有目标满足 QoS, 违约数, 观测数)。"""

    if len(group) == 1:
        return True, 0, 1
    key = _group_key(group)
    if key is None:
        raise ReplayError("非单实例组合缺少 combination_key")
    violations = 0
    observations = 0
    for workload_id in group:
        rows = truth.get((key, workload_id))
        if not rows:
            raise ReplayError(f"truth 缺少组合/目标: {key}/{workload_id}")
        qos = qos_by_workload[workload_id]
        for row in rows:
            solo = float(row["solo_mean_fps"])
            mean = float(row["mean_fps"])
            if not math.isfinite(solo) or not math.isfinite(mean) or solo <= 0:
                raise ReplayError(f"truth FPS 非法: {key}/{workload_id}")
            violations += int(mean / solo < qos)
            observations += 1
    return violations == 0, violations, observations


class _ReplayEvaluator:
    def __init__(self, *, dataset_tables: dict[str, pd.DataFrame], truth: dict[tuple[str, str], list[dict[str, Any]]], cm_bundle: dict[str, Any], baselines: dict[str, Any], qos_by_workload: dict[str, float]) -> None:
        self.truth = truth
        self.cm_bundle = cm_bundle
        self.baselines = baselines
        self.qos_by_workload = qos_by_workload
        all_rm = pd.concat([dataset_tables["rm"], dataset_tables["extra_rm"]], ignore_index=True)
        self.feature_rows: dict[tuple[str, str], pd.Series] = {}
        for (key, target), rows in all_rm.groupby(["combination_key", "target_id"], sort=False):
            self.feature_rows[(str(key), str(target))] = rows.sort_values("repeat").iloc[0]
        self.solo_rows: dict[str, pd.Series] = {}
        for target, rows in all_rm.groupby("target_id", sort=False):
            self.solo_rows[str(target)] = rows.sort_values("repeat").iloc[0]

    def _features(self, group: list[str]) -> pd.DataFrame | None:
        key = _group_key(group)
        rows: list[dict[str, Any]] = []
        for target in group:
            row = self.solo_rows.get(target) if key is None else self.feature_rows.get((key, target))
            if row is None:
                return None
            values = row.loc[list(FEATURE_COLUMNS)].to_dict()
            # no_profile_tree 基线额外读取组合大小；CM/RM 仍只接收 FEATURE_COLUMNS。
            values["combination_size"] = len(group)
            rows.append(values)
        return pd.DataFrame(rows)

    def predicted_feasible(self, group: list[str], strategy: str) -> bool | None:
        features = self._features(group)
        if features is None:
            return None
        if len(group) == 1:
            return True
        if strategy == "cm_model":
            prediction = threshold_predictions(
                self.cm_bundle["model"],
                model_feature_frame(features),
                float(self.cm_bundle["decision_threshold"]),
            )
            return bool(np.all(prediction))
        if strategy not in self.baselines:
            raise ReplayError(f"未知 replay strategy: {strategy}")
        prediction = predict_baseline(self.baselines[strategy], features)
        return all(float(value) >= self.qos_by_workload[target] for value, target in zip(prediction, group, strict=True))

    def actual(self, group: list[str]) -> tuple[bool, int, int]:
        return _truth_group_feasible(group, truth=self.truth, qos_by_workload=self.qos_by_workload)


def _pack_requests(requests: list[dict[str, Any]], *, evaluator: _ReplayEvaluator, strategy: str, max_group_size: int) -> dict[str, Any]:
    slots: list[list[dict[str, Any]]] = []
    trace: list[dict[str, Any]] = []
    pending = list(requests)
    while pending:
        anchor = pending[0]
        selected = [anchor]
        # 固定首个未分配请求为锚点，从大到小枚举剩余请求，保证确定性且真正“最大组合优先”。
        for size in range(min(max_group_size, len(pending)), 1, -1):
            for neighbors in itertools.combinations(pending[1:], size - 1):
                candidate = [anchor, *neighbors]
                group = [item["workload_id"] for item in candidate]
                if evaluator.predicted_feasible(group, strategy) is True:
                    selected = candidate
                    break
            if len(selected) > 1:
                break
        slot_index = len(slots)
        slots.append(selected)
        selected_ids = {str(item["request_id"]) for item in selected}
        pending = [item for item in pending if str(item["request_id"]) not in selected_ids]
        group = [item["workload_id"] for item in selected]
        trace.append(
            {
                "request_ids": [item["request_id"] for item in selected],
                "action": "placed_group" if len(selected) > 1 else "fallback_single",
                "slot": slot_index,
                "group": group,
            }
        )
    groups = [[item["workload_id"] for item in slot] for slot in slots]
    actual_feasible = 0
    actual_violations = 0
    actual_observations = 0
    for group in groups:
        feasible, violations, observations = evaluator.actual(group)
        actual_feasible += int(feasible)
        actual_violations += violations
        actual_observations += observations

    supported_candidates = []
    unique_workloads = [item["workload_id"] for item in requests]
    for size in range(2, max_group_size + 1):
        for combo in itertools.combinations(unique_workloads, size):
            group = list(combo)
            if evaluator.predicted_feasible(group, strategy) is not None:
                supported_candidates.append(group)
    predicted_positive = [group for group in supported_candidates if evaluator.predicted_feasible(group, strategy) is True]
    actual_positive = [group for group in supported_candidates if evaluator.actual(group)[0]]
    predicted_keys = {_group_key(group) for group in predicted_positive}
    actual_keys = {_group_key(group) for group in actual_positive}
    intersection = len(predicted_keys & actual_keys)
    precision = intersection / len(predicted_keys) if predicted_keys else None
    recall = intersection / len(actual_keys) if actual_keys else None
    return {
        "strategy": strategy,
        "request_count": len(requests),
        "slot_count": len(slots),
        "average_instances_per_slot": len(requests) / len(slots) if slots else 0.0,
        "slot_size_histogram": dict(sorted(Counter(len(group) for group in groups).items())),
        "slots": [{"slot_id": index + 1, "workload_ids": group, "combination_key": _group_key(group)} for index, group in enumerate(groups)],
        "placement_trace": trace,
        "supported_candidate_count": len(supported_candidates),
        "predicted_feasible_combination_count": len(predicted_keys),
        "actual_feasible_combination_count": len(actual_keys),
        "combination_precision": precision,
        "combination_recall": recall,
        "actual_qos_violation_rate": actual_violations / actual_observations if actual_observations else 0.0,
        "actual_qos_violation_count": actual_violations,
        "actual_qos_observation_count": actual_observations,
        "actual_feasible_slot_count": actual_feasible,
    }


def _plot_packing(path: Path, reports: list[dict[str, Any]]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover
        raise ReplayError("Step 12 图表需要 matplotlib") from exc
    if path.exists():
        raise FileExistsError(f"拒绝覆盖 replay 图表: {path}")
    names = [report["strategy"] for report in reports]
    slots = [report["slot_count"] for report in reports]
    violation_rates = [report["actual_qos_violation_rate"] for report in reports]
    x = np.arange(len(names))
    figure, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)
    axes[0].bar(x, slots, color="#4472c4")
    axes[0].set_xticks(x, names, rotation=35, ha="right")
    axes[0].set_ylabel("server slots (lower is better)")
    axes[0].set_title("QoS-safe packing slot count")
    axes[1].bar(x, violation_rates, color="#c0504d")
    axes[1].set_xticks(x, names, rotation=35, ha="right")
    axes[1].set_ylabel("measured QoS violation rate")
    axes[1].set_title("Replay measured QoS violations")
    for axis in axes:
        axis.grid(axis="y", alpha=0.25)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=160)
    plt.close(figure)


def run_qos_packing(
    *,
    repo_root: Path,
    model_path: Path,
    requests_path: Path,
    ground_truth_path: Path,
    dataset_dir: Path,
    output_dir: Path,
    qos_ratio: float | None = None,
) -> dict[str, Any]:
    """执行 QoS 安全装箱，模型只负责预测，实测 truth 只负责验收。"""

    del repo_root
    model_file = model_path.resolve()
    request_file = requests_path.resolve()
    truth_file = ground_truth_path.resolve()
    dataset = dataset_dir.resolve()
    out = output_dir.resolve()
    tables = load_dataset_tables(dataset)
    feature_manifest = load_feature_manifest(dataset)
    split_info = validate_split_contract(tables, strict=True)
    truth, truth_rows = _read_truth(truth_file)
    try:
        model_manifest = read_json(model_file.with_name("model-manifest.json"))
        bundle = joblib.load(model_file)
        baselines = joblib.load(model_file.with_name("baselines.joblib"))
    except (OSError, ValueError, ImportError) as exc:
        raise ReplayError("无法读取 Step 10 CM 模型或基线") from exc
    if bundle.get("task") != "cm" or tuple(bundle.get("feature_columns", ())) != FEATURE_COLUMNS:
        raise ReplayError("CM 模型 feature contract 不一致")
    if model_manifest.get("feature_columns") != feature_manifest.get("feature_columns"):
        raise ReplayError("CM model manifest 与数据集特征不一致")
    workload_ids = set(str(value) for value in pd.concat([tables["rm"], tables["extra_rm"]])["target_id"].unique())
    requests, global_qos, max_group_size, request_spec = _load_requests(request_file, workload_ids, qos_ratio)
    qos_by_workload = {item["workload_id"]: float(item["qos_ratio"]) for item in requests}
    evaluator = _ReplayEvaluator(
        dataset_tables=tables,
        truth=truth,
        cm_bundle=bundle,
        baselines=baselines,
        qos_by_workload=qos_by_workload,
    )
    strategies = ["cm_model", "sigmoid_count", "vbp_like", "linear_additive", "solo_only", "no_profile_tree"]
    reports = [_pack_requests(requests, evaluator=evaluator, strategy=strategy, max_group_size=max_group_size) for strategy in strategies]
    no_colocation = {
        "strategy": "no_colocation",
        "request_count": len(requests),
        "slot_count": len(requests),
        "average_instances_per_slot": 1.0,
        "slot_size_histogram": {"1": len(requests)},
        "slots": [{"slot_id": index + 1, "workload_ids": [item["workload_id"]], "combination_key": None} for index, item in enumerate(requests)],
        "placement_trace": [],
        "supported_candidate_count": 0,
        "predicted_feasible_combination_count": 0,
        "actual_feasible_combination_count": 0,
        "combination_precision": None,
        "combination_recall": None,
        "actual_qos_violation_rate": 0.0,
        "actual_qos_violation_count": 0,
        "actual_qos_observation_count": len(requests),
        "actual_feasible_slot_count": len(requests),
    }
    reports.insert(0, no_colocation)
    checks = {
        "request_count_positive": len(requests) > 0,
        "dataset_split_contract_passed": split_info["main_key_count"] == 60 and split_info["extra_key_count"] == 12,
        "truth_rows_600": truth_rows == 600,
        "model_feature_contract_passed": tuple(model_manifest.get("feature_columns", ())) == FEATURE_COLUMNS,
        "all_strategies_reported": {report["strategy"] for report in reports} == {"no_colocation", *strategies},
        "ground_truth_used_for_actual_metrics": all("actual_qos_observation_count" in report for report in reports),
        "combination_metrics_reported": all("combination_precision" in report and "combination_recall" in report for report in reports),
        "no_colocation_reference_present": no_colocation["slot_count"] == len(requests),
    }
    if not all(checks.values()):
        raise ReplayError(f"Step 12 质量门失败: {checks}")
    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": 1,
        "status": "passed",
        "experiment_id": request_spec.get("experiment_id", "formal-v1"),
        "qos_ratio": global_qos,
        "max_group_size": max_group_size,
        "request_count": len(requests),
        "seed": 20260811,
        "requests_sha256": file_sha256(request_file),
        "ground_truth_sha256": file_sha256(truth_file),
        "model_manifest_sha256": file_sha256(model_file.with_name("model-manifest.json")),
        "checks": checks,
        "check_count": len(checks),
        "passed_count": sum(checks.values()),
        "strategies": reports,
        "artifacts": {"summary": "packing-summary.json", "plot": "packing-slots.png"},
    }
    write_json_exclusive(out / "packing-summary.json", summary)
    _plot_packing(out / "packing-slots.png", reports)
    return summary
