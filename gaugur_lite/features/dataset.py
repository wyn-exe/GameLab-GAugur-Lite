"""从已验收的 profile 与共置 truth 构建 RM/CM 模型数据集。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..config import stable_json_dumps


class DatasetError(RuntimeError):
    """Step 9 输入、特征或派生数据集不满足质量契约。"""


RESOURCES = ("cpu_compute", "gpu_compute", "gpu_memory", "memory_bandwidth")
PRESSURES = (0.0, 0.25, 0.5, 0.75, 1.0)
QOS_RATIOS = (0.70, 0.80, 0.90)
EXPECTED_PROFILE_ROWS = 8 * len(RESOURCES) * len(PRESSURES)
EXPECTED_MAIN_RM_ROWS = 456
EXPECTED_EXTRA_RM_ROWS = 144
EXPECTED_MAIN_CM_ROWS = EXPECTED_MAIN_RM_ROWS * len(QOS_RATIOS)
EXPECTED_EXTRA_CM_ROWS = EXPECTED_EXTRA_RM_ROWS * len(QOS_RATIOS)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_repo(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise DatasetError(f"路径必须位于仓库内且不能等于仓库根目录: {path}")
    return resolved


def _relative(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _read_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - formal env supplies pyarrow.
        raise DatasetError("Step 9 parquet 读写需要 pyarrow") from exc
    try:
        return pq.read_table(path).to_pylist()
    except (OSError, ValueError, RuntimeError) as exc:
        raise DatasetError(f"无法读取 parquet: {path}") from exc


def _write_parquet_exclusive(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - formal env supplies pyarrow.
        raise DatasetError("Step 9 parquet 读写需要 pyarrow") from exc
    if path.exists():
        raise FileExistsError(f"拒绝覆盖数据集 parquet: {path}")
    if not rows:
        raise DatasetError(f"不允许写入空数据集: {path}")
    fields = list(rows[0])
    if any(list(row) != fields for row in rows):
        raise DatasetError(f"数据集字段顺序不一致: {path}")
    table = pa.Table.from_arrays(
        [pa.array([row[field] for row in rows]) for field in fields], names=fields
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"拒绝覆盖数据集 JSON: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json_dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"无法读取数据集 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise DatasetError(f"数据集 JSON 顶层必须为对象: {path}")
    return payload


def _pressure_token(pressure: float) -> str:
    return f"p{round(pressure * 100):03d}"


def _sensitivity_columns() -> tuple[str, ...]:
    return tuple(
        f"sensitivity_{resource}_{_pressure_token(pressure)}"
        for resource in RESOURCES
        for pressure in PRESSURES
    )


def _intensity_columns() -> tuple[str, ...]:
    return tuple(
        item
        for resource in RESOURCES
        for item in (
            f"intensity_mean_{resource}",
            f"intensity_var_{resource}",
        )
    )


FEATURE_COLUMNS = (
    "solo_fps",
    "neighbor_count",
    *_sensitivity_columns(),
    *_intensity_columns(),
)
METADATA_COLUMNS = (
    "experiment_id",
    "stage",
    "split",
    "combination_key",
    "colocation_id",
    "run_id",
    "repeat",
    "target_id",
    "neighbor_ids",
    "combination_size",
)
LABEL_COLUMNS = (
    "mean_fps",
    "p05_fps",
    "min_fps",
    "retention_ratio",
    "loss_ratio",
    "p05_retention_ratio",
)


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DatasetError(f"{name} 不是数值: {value!r}") from exc
    if not math.isfinite(result):
        raise DatasetError(f"{name} 不是有限值: {value!r}")
    return result


def _validate_profiles(rows: list[dict[str, Any]], *, strict: bool) -> dict[tuple[str, str, float], dict[str, Any]]:
    if strict and len(rows) != EXPECTED_PROFILE_ROWS:
        raise DatasetError(f"profile 行数不符: {len(rows)}/{EXPECTED_PROFILE_ROWS}")
    index: dict[tuple[str, str, float], dict[str, Any]] = {}
    workloads: set[str] = set()
    for row in rows:
        workload_id = str(row.get("workload_id", ""))
        resource = str(row.get("resource", ""))
        pressure = round(_finite(row.get("pressure_requested"), "pressure_requested"), 8)
        key = (workload_id, resource, pressure)
        if not workload_id or resource not in RESOURCES or pressure not in PRESSURES:
            raise DatasetError(f"profile key 非法: {key}")
        if key in index:
            raise DatasetError(f"profile key 重复: {key}")
        _finite(row.get("sensitivity_mean"), f"{key}.sensitivity_mean")
        if pressure == 1.0:
            intensity = row.get("intensity_slowdown_mean")
            if intensity is None:
                raise DatasetError(f"最高压力缺少 intensity: {key}")
            _finite(intensity, f"{key}.intensity_slowdown_mean")
        index[key] = row
        workloads.add(workload_id)
    if strict:
        if workloads != {
            "pyxel_jump",
            "pyxel_bubbles",
            "pyxel_snake",
            "pyxel_shooter",
            "pyxel_platformer",
            "daylight",
            "mega_wing",
            "space_rescue",
        }:
            raise DatasetError(f"profile workload 集合不符: {sorted(workloads)}")
        expected_keys = {
            (workload, resource, pressure)
            for workload in workloads
            for resource in RESOURCES
            for pressure in PRESSURES
        }
        if set(index) != expected_keys:
            raise DatasetError("profile 未完整覆盖 8×4×5 网格")
    return index


def _validate_truth(rows: list[dict[str, Any]], *, strict: bool) -> None:
    if strict and len(rows) != EXPECTED_MAIN_RM_ROWS + EXPECTED_EXTRA_RM_ROWS:
        raise DatasetError(f"truth 行数不符: {len(rows)}/600")
    keys: set[tuple[str, str, int, str]] = set()
    split_by_combination: dict[str, str] = {}
    stage_counts: Counter[str] = Counter()
    for row in rows:
        stage = str(row.get("stage", ""))
        combination = str(row.get("combination_key", ""))
        target_id = str(row.get("target_id", ""))
        repeat = int(row.get("repeat", 0))
        key = (stage, combination, repeat, target_id)
        if key in keys:
            raise DatasetError(f"truth 主键重复: {key}")
        keys.add(key)
        if stage not in {"colocation-main", "colocation-extra-test"}:
            raise DatasetError(f"truth stage 非法: {stage}")
        split = str(row.get("split", ""))
        if stage == "colocation-extra-test" and split != "extra_test":
            raise DatasetError(f"额外测试 split 非法: {combination}/{split}")
        if stage == "colocation-main" and split not in {"train", "validation", "test"}:
            raise DatasetError(f"主数据 split 非法: {combination}/{split}")
        previous = split_by_combination.setdefault(combination, split)
        if previous != split:
            raise DatasetError(f"同一组合跨 split: {combination}")
        workload_ids = tuple(str(item) for item in row.get("workload_ids", []))
        neighbors = tuple(str(item) for item in row.get("neighbor_ids", []))
        if len(workload_ids) != int(row.get("combination_size", -1)):
            raise DatasetError(f"组合大小不匹配: {key}")
        if target_id not in workload_ids or set(neighbors) != set(workload_ids) - {target_id}:
            raise DatasetError(f"target/neighbors 不匹配: {key}")
        for name in ("mean_fps", "p05_fps", "min_fps", "solo_mean_fps", "retention_ratio", "loss_ratio"):
            value = _finite(row.get(name), f"{key}.{name}")
            if name in {"mean_fps", "p05_fps", "min_fps", "solo_mean_fps"} and value <= 0:
                raise DatasetError(f"{key}.{name} 必须为正数")
        stage_counts[stage] += 1
    if strict and stage_counts != Counter({"colocation-main": EXPECTED_MAIN_RM_ROWS, "colocation-extra-test": EXPECTED_EXTRA_RM_ROWS}):
        raise DatasetError(f"truth stage 行数不符: {dict(stage_counts)}")


def _build_feature_rows(
    truth_rows: list[dict[str, Any]],
    profile_index: dict[tuple[str, str, float], dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for truth in truth_rows:
        target_id = str(truth["target_id"])
        neighbors = tuple(str(item) for item in truth["neighbor_ids"])
        feature_row: dict[str, Any] = {
            "experiment_id": str(truth["experiment_id"]),
            "stage": str(truth["stage"]),
            "split": str(truth["split"]),
            "combination_key": str(truth["combination_key"]),
            "colocation_id": str(truth["colocation_id"]),
            "run_id": str(truth["run_id"]),
            "repeat": int(truth["repeat"]),
            "target_id": target_id,
            "neighbor_ids": list(neighbors),
            "combination_size": int(truth["combination_size"]),
            "solo_fps": _finite(truth["solo_mean_fps"], "solo_mean_fps"),
            "neighbor_count": len(neighbors),
            **{
                name: _finite(truth[name], name)
                for name in LABEL_COLUMNS
            },
        }
        for resource in RESOURCES:
            for pressure in PRESSURES:
                profile = profile_index[(target_id, resource, pressure)]
                feature_row[f"sensitivity_{resource}_{_pressure_token(pressure)}"] = _finite(
                    profile["sensitivity_mean"],
                    f"{target_id}/{resource}/{pressure}.sensitivity_mean",
                )
            neighbor_values = [
                _finite(
                    profile_index[(neighbor, resource, 1.0)]["intensity_slowdown_mean"],
                    f"{neighbor}/{resource}/1.0.intensity_slowdown_mean",
                )
                for neighbor in neighbors
            ]
            if not neighbor_values:
                raise DatasetError(f"共置样本缺少邻居: {truth['run_id']}/{target_id}")
            mean = sum(neighbor_values) / len(neighbor_values)
            variance = sum((value - mean) ** 2 for value in neighbor_values) / len(neighbor_values)
            feature_row[f"intensity_mean_{resource}"] = mean
            feature_row[f"intensity_var_{resource}"] = variance
        result.append(feature_row)
    result.sort(key=lambda row: (row["stage"], row["combination_key"], row["repeat"], row["target_id"]))
    return result


def _expand_cm(rm_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rm_rows:
        for qos_ratio in QOS_RATIOS:
            expanded = dict(row)
            expanded["qos_ratio"] = qos_ratio
            expanded["qos_threshold"] = qos_ratio * float(row["solo_fps"])
            expanded["qos_satisfied"] = bool(float(row["mean_fps"]) >= expanded["qos_threshold"])
            result.append(expanded)
    return result


def _all_output_paths(output_dir: Path) -> dict[str, Path]:
    return {
        "base_samples": output_dir / "base_samples.parquet",
        "rm_samples": output_dir / "rm_samples.parquet",
        "cm_samples": output_dir / "cm_samples.parquet",
        "extra_rm_samples": output_dir / "extra_rm_samples.parquet",
        "extra_cm_samples": output_dir / "extra_cm_samples.parquet",
        "combination_manifest": output_dir / "combination_manifest.json",
        "split_manifest": output_dir / "split_manifest.json",
        "feature_manifest": output_dir / "feature_manifest.json",
        "dataset_summary": output_dir / "dataset-summary.json",
    }


def _build_manifests(
    *, repo_root: Path, profiles_file: Path, truth_file: Path, output_dir: Path,
    base_rows: list[dict[str, Any]], rm_rows: list[dict[str, Any]], extra_rm_rows: list[dict[str, Any]],
    cm_rows: list[dict[str, Any]], extra_cm_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    combinations: dict[str, dict[str, Any]] = {}
    for row in base_rows:
        entry = combinations.setdefault(
            str(row["combination_key"]),
            {"stage": row["stage"], "split": row["split"], "combination_size": row["combination_size"], "target_ids": set(), "repeats": set()},
        )
        entry["target_ids"].add(str(row["target_id"]))
        entry["repeats"].add(int(row["repeat"]))
    combination_manifest = {
        "schema_version": 1,
        "status": "passed",
        "combination_count": len(combinations),
        "combinations": [
            {
                **{key: value for key, value in entry.items() if key not in {"target_ids", "repeats"}},
                "combination_key": key,
                "target_count": len(entry["target_ids"]),
                "repeats": sorted(entry["repeats"]),
            }
            for key, entry in sorted(combinations.items())
        ],
        "checks": {
            "main_combination_count_60": sum(entry["stage"] == "colocation-main" for entry in combinations.values()) == 60,
            "extra_combination_count_12": sum(entry["stage"] == "colocation-extra-test" for entry in combinations.values()) == 12,
            "three_repeats_per_combination": all(entry["repeats"] == {1, 2, 3} for entry in combinations.values()),
        },
    }
    split_keys: dict[str, list[str]] = defaultdict(list)
    for key, entry in combinations.items():
        split_keys[str(entry["split"])].append(key)
    split_manifest = {
        "schema_version": 1,
        "status": "passed",
        "seed": 20260811,
        "keys": {split: sorted(keys) for split, keys in sorted(split_keys.items())},
        "row_counts": {
            "rm": {"train": sum(row["split"] == "train" for row in rm_rows), "validation": sum(row["split"] == "validation" for row in rm_rows), "test": sum(row["split"] == "test" for row in rm_rows), "extra_test": len(extra_rm_rows)},
            "cm": {"train": sum(row["split"] == "train" for row in cm_rows), "validation": sum(row["split"] == "validation" for row in cm_rows), "test": sum(row["split"] == "test" for row in cm_rows), "extra_test": len(extra_cm_rows)},
        },
        "checks": {
            "combination_split_is_stable": len(split_keys) > 0,
            "main_key_split_counts_36_12_12": [
                len(split_keys.get("train", [])),
                len(split_keys.get("validation", [])),
                len(split_keys.get("test", [])),
            ] == [36, 12, 12],
            "extra_key_count_12": len(split_keys.get("extra_test", [])) == 12,
            "rm_row_counts_279_96_81_144": [
                sum(row["split"] == "train" for row in rm_rows),
                sum(row["split"] == "validation" for row in rm_rows),
                sum(row["split"] == "test" for row in rm_rows),
                len(extra_rm_rows),
            ] == [279, 96, 81, 144],
            "cm_row_counts_837_288_243_432": [
                sum(row["split"] == "train" for row in cm_rows),
                sum(row["split"] == "validation" for row in cm_rows),
                sum(row["split"] == "test" for row in cm_rows),
                len(extra_cm_rows),
            ] == [837, 288, 243, 432],
        },
    }
    outputs = _all_output_paths(output_dir)
    feature_manifest = {
        "schema_version": 1,
        "status": "passed",
        "experiment_id": "formal-v1",
        "sources": {
            "profiles": {"path": _relative(repo_root, profiles_file), "sha256": _sha256(profiles_file), "rows": EXPECTED_PROFILE_ROWS},
            "truth": {"path": _relative(repo_root, truth_file), "sha256": _sha256(truth_file), "rows": len(base_rows)},
        },
        "resources": list(RESOURCES),
        "pressures": list(PRESSURES),
        "qos_ratios": list(QOS_RATIOS),
        "intensity_definition": "neighbor intensity is intensity_slowdown_mean at the highest requested pressure p=1.0; mean/variance use population variance across neighbors",
        "feature_columns": list(FEATURE_COLUMNS),
        "metadata_columns": list(METADATA_COLUMNS),
        "label_columns": list(LABEL_COLUMNS),
        "target_id_in_model_features": False,
        "row_counts": {"base": len(base_rows), "rm": len(rm_rows), "cm": len(cm_rows), "extra_rm": len(extra_rm_rows), "extra_cm": len(extra_cm_rows)},
        "artifacts": {name: _relative(repo_root, path) for name, path in outputs.items()},
    }
    return combination_manifest, split_manifest, feature_manifest


def build_dataset(
    *, repo_root: Path, profiles_file: Path, truth_file: Path, output_dir: Path
) -> dict[str, Any]:
    """独占构建 Step 9 的 base、RM、CM 表及三个 manifest。"""

    root = repo_root.resolve()
    profiles = _inside_repo(root, profiles_file)
    truth = _inside_repo(root, truth_file)
    out = _inside_repo(root, output_dir)
    outputs = _all_output_paths(out)
    existing = [path for path in outputs.values() if path.exists()]
    if existing:
        raise FileExistsError("Step 9 数据集产物已存在，拒绝覆盖: " + ", ".join(map(str, existing)))
    profile_rows = _read_parquet(profiles)
    truth_rows = _read_parquet(truth)
    profile_index = _validate_profiles(profile_rows, strict=True)
    _validate_truth(truth_rows, strict=True)
    base_rows = _build_feature_rows(truth_rows, profile_index)
    rm_rows = [row for row in base_rows if row["stage"] == "colocation-main"]
    extra_rm_rows = [row for row in base_rows if row["stage"] == "colocation-extra-test"]
    cm_rows = _expand_cm(rm_rows)
    extra_cm_rows = _expand_cm(extra_rm_rows)
    if (len(rm_rows), len(extra_rm_rows), len(cm_rows), len(extra_cm_rows)) != (
        EXPECTED_MAIN_RM_ROWS, EXPECTED_EXTRA_RM_ROWS, EXPECTED_MAIN_CM_ROWS, EXPECTED_EXTRA_CM_ROWS
    ):
        raise DatasetError("Step 9 RM/CM 样本数量不符合冻结设计")
    combination_manifest, split_manifest, feature_manifest = _build_manifests(
        repo_root=root, profiles_file=profiles, truth_file=truth, output_dir=out,
        base_rows=base_rows, rm_rows=rm_rows, extra_rm_rows=extra_rm_rows,
        cm_rows=cm_rows, extra_cm_rows=extra_cm_rows,
    )
    _write_parquet_exclusive(outputs["base_samples"], base_rows)
    _write_parquet_exclusive(outputs["rm_samples"], rm_rows)
    _write_parquet_exclusive(outputs["cm_samples"], cm_rows)
    _write_parquet_exclusive(outputs["extra_rm_samples"], extra_rm_rows)
    _write_parquet_exclusive(outputs["extra_cm_samples"], extra_cm_rows)
    _write_json_exclusive(outputs["combination_manifest"], combination_manifest)
    _write_json_exclusive(outputs["split_manifest"], split_manifest)
    _write_json_exclusive(outputs["feature_manifest"], feature_manifest)
    summary = {
        "schema_version": 1,
        "status": "passed",
        "experiment_id": "formal-v1",
        "row_counts": feature_manifest["row_counts"],
        "retention_above_one_count": sum(float(row["retention_ratio"]) > 1.0 for row in base_rows),
        "checks": {
            **combination_manifest["checks"],
            **split_manifest["checks"],
            "target_id_not_in_feature_columns": "target_id" not in FEATURE_COLUMNS,
            "all_feature_values_finite": all(math.isfinite(float(row[name])) for row in base_rows for name in FEATURE_COLUMNS),
        },
        "sources": feature_manifest["sources"],
        "artifacts": {
            name: {"path": _relative(root, path), "sha256": _sha256(path)}
            for name, path in outputs.items()
            if path.exists()
        },
    }
    _write_json_exclusive(outputs["dataset_summary"], summary)
    return summary


def audit_dataset(*, repo_root: Path, dataset_dir: Path, output_file: Path | None = None) -> dict[str, Any]:
    """独立重算特征并核对所有表、manifest、哈希和质量门。"""

    root = repo_root.resolve()
    out = _inside_repo(root, dataset_dir)
    outputs = _all_output_paths(out)
    required = [path for name, path in outputs.items() if name != "dataset_summary"] + [outputs["dataset_summary"]]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise DatasetError("缺少 Step 9 产物: " + ", ".join(map(str, missing)))
    feature_manifest = _read_json(outputs["feature_manifest"])
    sources = feature_manifest.get("sources", {})
    profiles = _inside_repo(root, root / str(sources.get("profiles", {}).get("path", "")))
    truth = _inside_repo(root, root / str(sources.get("truth", {}).get("path", "")))
    profile_rows = _read_parquet(profiles)
    truth_rows = _read_parquet(truth)
    profile_index = _validate_profiles(profile_rows, strict=True)
    _validate_truth(truth_rows, strict=True)
    expected_base = _build_feature_rows(truth_rows, profile_index)
    expected_rm = [row for row in expected_base if row["stage"] == "colocation-main"]
    expected_extra_rm = [row for row in expected_base if row["stage"] == "colocation-extra-test"]
    expected_cm = _expand_cm(expected_rm)
    expected_extra_cm = _expand_cm(expected_extra_rm)
    expected = {
        "base_samples": expected_base,
        "rm_samples": expected_rm,
        "cm_samples": expected_cm,
        "extra_rm_samples": expected_extra_rm,
        "extra_cm_samples": expected_extra_cm,
    }
    checks: dict[str, bool] = {}
    for name, rows in expected.items():
        checks[f"{name}_recomputed_exactly"] = _read_parquet(outputs[name]) == rows
        checks[f"{name}_sha256_present"] = bool(_sha256(outputs[name]))
    checks["profile_source_sha256"] = _sha256(profiles) == sources.get("profiles", {}).get("sha256")
    checks["truth_source_sha256"] = _sha256(truth) == sources.get("truth", {}).get("sha256")
    checks["target_id_not_in_feature_columns"] = "target_id" not in feature_manifest.get("feature_columns", [])
    checks["feature_columns_match_contract"] = tuple(feature_manifest.get("feature_columns", [])) == FEATURE_COLUMNS
    checks["row_counts_match_contract"] = feature_manifest.get("row_counts") == {"base": 600, "rm": 456, "cm": 1368, "extra_rm": 144, "extra_cm": 432}
    combination_manifest = _read_json(outputs["combination_manifest"])
    checks["combination_manifest_status_passed"] = combination_manifest.get("status") == "passed"
    checks["combination_manifest_checks_passed"] = all(combination_manifest.get("checks", {}).values())
    checks["combination_manifest_count_72"] = combination_manifest.get("combination_count") == 72
    split_manifest = _read_json(outputs["split_manifest"])
    checks["split_manifest_status_passed"] = split_manifest.get("status") == "passed"
    checks["split_manifest_checks_passed"] = all(split_manifest.get("checks", {}).values())
    checks["split_manifest_row_counts_match"] = split_manifest.get("row_counts") == {
        "rm": {"train": 279, "validation": 96, "test": 81, "extra_test": 144},
        "cm": {"train": 837, "validation": 288, "test": 243, "extra_test": 432},
    }
    checks["retention_above_one_preserved"] = sum(float(row["retention_ratio"]) > 1.0 for row in expected_base) > 0
    stored_summary = _read_json(outputs["dataset_summary"])
    checks["summary_status_passed"] = stored_summary.get("status") == "passed"
    checks["all_quality_checks"] = all(checks.values())
    result = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "check_count": len(checks),
        "passed_count": sum(checks.values()),
        "row_counts": {"base": len(expected_base), "rm": len(expected_rm), "cm": len(expected_cm), "extra_rm": len(expected_extra_rm), "extra_cm": len(expected_extra_cm)},
        "retention_above_one_count": sum(float(row["retention_ratio"]) > 1.0 for row in expected_base),
        "checks": checks,
        "artifacts": {name: {"path": _relative(root, path), "sha256": _sha256(path)} for name, path in outputs.items()},
    }
    if output_file is not None:
        output = _inside_repo(root, output_file)
        if output.exists():
            raise FileExistsError(f"拒绝覆盖 Step 9 验收 JSON: {output}")
        _write_json_exclusive(output, result)
    return result
