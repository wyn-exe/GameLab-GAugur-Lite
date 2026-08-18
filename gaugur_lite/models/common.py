"""模型训练共享的数据读取、切分和持久化工具。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import stable_json_dumps
from ..features.dataset import FEATURE_COLUMNS


class ModelError(RuntimeError):
    """模型数据、切分或训练质量门失败。"""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelError(f"无法读取模型 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ModelError(f"模型 JSON 顶层必须为对象: {path}")
    return payload


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"拒绝覆盖模型 JSON: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json_dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_feature_manifest(dataset_dir: Path) -> dict[str, Any]:
    manifest = read_json(dataset_dir / "feature_manifest.json")
    columns = tuple(manifest.get("feature_columns", ()))
    if columns != FEATURE_COLUMNS or manifest.get("target_id_in_model_features") is not False:
        raise ModelError("feature manifest 与 Step 9 特征契约不一致")
    return manifest


def load_dataset_tables(dataset_dir: Path) -> dict[str, pd.DataFrame]:
    """读取 Step 9 五张表；训练只使用 manifest 列出的特征。"""

    manifest = load_feature_manifest(dataset_dir)
    del manifest
    names = ("rm", "cm", "extra_rm", "extra_cm")
    paths = {
        "rm": dataset_dir / "rm_samples.parquet",
        "cm": dataset_dir / "cm_samples.parquet",
        "extra_rm": dataset_dir / "extra_rm_samples.parquet",
        "extra_cm": dataset_dir / "extra_cm_samples.parquet",
    }
    missing = [path for path in paths.values() if not path.is_file()]
    if missing:
        raise ModelError("缺少 Step 9 数据表: " + ", ".join(map(str, missing)))
    try:
        tables = {name: pd.read_parquet(paths[name]) for name in names}
    except (OSError, ValueError, ImportError) as exc:
        raise ModelError("无法读取 Step 9 parquet 数据表") from exc
    return tables


def validate_split_contract(tables: dict[str, pd.DataFrame], *, strict: bool = True) -> dict[str, Any]:
    """验证组合级 split 不交叉，且主/额外样本数保持冻结设计。"""

    required = {"rm", "cm", "extra_rm", "extra_cm"}
    if set(tables) != required:
        raise ModelError(f"模型表集合不完整: {sorted(tables)}")
    key_splits: dict[str, set[str]] = {}
    for name, table in tables.items():
        if "combination_key" not in table or "split" not in table:
            raise ModelError(f"{name} 缺少 combination_key/split")
        for key, split in zip(table["combination_key"].astype(str), table["split"].astype(str), strict=True):
            key_splits.setdefault(key, set()).add(split)
    unstable = {key: sorted(splits) for key, splits in key_splits.items() if len(splits) != 1}
    if unstable:
        raise ModelError(f"组合跨 split 泄漏: {unstable}")
    main_keys = set(tables["rm"]["combination_key"].astype(str))
    main_cm_keys = set(tables["cm"]["combination_key"].astype(str))
    extra_keys = set(tables["extra_rm"]["combination_key"].astype(str))
    extra_cm_keys = set(tables["extra_cm"]["combination_key"].astype(str))
    if main_keys != main_cm_keys:
        raise ModelError("RM 与 CM 主数据 combination_key 不一致")
    if extra_keys != extra_cm_keys:
        raise ModelError("RM 与 CM extra_test combination_key 不一致")
    if main_keys & extra_keys:
        raise ModelError("主数据与 extra_test combination_key 交叉")
    counts = {
        "rm": {split: int((tables["rm"]["split"] == split).sum()) for split in ("train", "validation", "test")},
        "cm": {split: int((tables["cm"]["split"] == split).sum()) for split in ("train", "validation", "test")},
        "extra_rm": int(len(tables["extra_rm"])),
        "extra_cm": int(len(tables["extra_cm"])),
    }
    if strict and counts != {
        "rm": {"train": 279, "validation": 96, "test": 81},
        "cm": {"train": 837, "validation": 288, "test": 243},
        "extra_rm": 144,
        "extra_cm": 432,
    }:
        raise ModelError(f"模型表 split 行数不符: {counts}")
    return {"counts": counts, "main_key_count": len(main_keys), "extra_key_count": len(extra_keys), "key_splits": key_splits}


def model_feature_frame(table: pd.DataFrame, feature_columns: tuple[str, ...] = FEATURE_COLUMNS) -> pd.DataFrame:
    missing = [column for column in feature_columns if column not in table.columns]
    if missing:
        raise ModelError(f"模型表缺少特征列: {missing}")
    if "target_id" in feature_columns:
        raise ModelError("target_id 禁止进入模型特征")
    return table.loc[:, list(feature_columns)].astype(float)


def prediction_sha256(values: Any) -> str:
    import numpy as np

    array = np.asarray(values)
    return hashlib.sha256(array.tobytes()).hexdigest()
