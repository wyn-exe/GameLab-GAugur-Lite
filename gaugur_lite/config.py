"""YAML 加载、仓库路径解析、稳定序列化和配置哈希。"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .schema import LocalConfig, WorkloadCatalog

ModelT = TypeVar("ModelT", bound=BaseModel)


class ConfigError(ValueError):
    """配置读取或校验失败。"""


class UniqueKeyLoader(yaml.SafeLoader):
    """拒绝 YAML 重复键，防止静默覆盖实验参数。"""


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(f"YAML 存在重复键: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def stable_json_dumps(value: Any, *, indent: int | None = None) -> str:
    """生成跨键顺序稳定、禁止 NaN 的 UTF-8 JSON 文本。"""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)

    def default(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", exclude_none=False)
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, Path):
            return item.as_posix()
        raise TypeError(f"不支持稳定序列化的类型: {type(item).__name__}")

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else None,
        indent=indent,
        allow_nan=False,
        default=default,
    )


def config_sha256(value: Any) -> str:
    return hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if config_path.suffix.lower() not in {".yaml", ".yml"}:
        raise ConfigError(f"配置文件必须使用 .yaml 或 .yml: {config_path}")
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在: {config_path}")
    try:
        raw = yaml.load(config_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取 YAML {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"YAML 顶层必须是对象: {config_path}")
    return raw


def load_model(path: str | Path, model_type: type[ModelT]) -> ModelT:
    try:
        return model_type.model_validate(load_yaml_mapping(path))
    except ValidationError as exc:
        raise ConfigError(f"配置 schema 校验失败 ({path}):\n{exc}") from exc


def load_local_config(path: str | Path) -> LocalConfig:
    return load_model(path, LocalConfig)


def load_workload_catalog(path: str | Path) -> WorkloadCatalog:
    return load_model(path, WorkloadCatalog)


def discover_repo_root(start: str | Path) -> Path:
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / "README.md").is_file() and (candidate / "games").is_dir():
            return candidate
    raise ConfigError(f"无法从 {start!s} 向上找到仓库根目录")


def resolve_repo_path(repo_root: str | Path, relative_path: str) -> Path:
    root = Path(repo_root).resolve()
    resolved = (root / relative_path).resolve()
    if resolved != root and root not in resolved.parents:
        raise ConfigError(f"路径逃逸仓库根目录: {relative_path!r}")
    return resolved


def resolved_output_paths(config: LocalConfig, repo_root: str | Path) -> dict[str, Path]:
    return {
        name: resolve_repo_path(repo_root, value)
        for name, value in config.paths.model_dump().items()
    }

