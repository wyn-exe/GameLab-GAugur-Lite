"""实验配置、运行清单和指标事件的严格数据契约。"""

from __future__ import annotations

import math
import re
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_GENERATED_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_+.-]{0,255}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class StrictModel(BaseModel):
    """禁止未知字段并冻结模型，避免运行中静默修改配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def validate_identifier(value: str, *, field_name: str = "identifier") -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} 必须匹配 {_ID_PATTERN.pattern!r}，当前值为 {value!r}"
        )
    return value


def validate_generated_identifier(value: str, *, field_name: str) -> str:
    if not _GENERATED_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} 含非法字符或超过 256 字符: {value!r}")
    return value


def validate_repo_relative_path(value: str, *, field_name: str) -> str:
    """接受 Windows 分隔符，但禁止绝对路径和 `..` 逃逸仓库。"""

    normalized = value.strip().replace("\\", "/")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise ValueError(f"{field_name} 必须是仓库内相对路径: {value!r}")
    return posix_path.as_posix()


def make_combination_key(workload_ids: list[str] | tuple[str, ...]) -> str:
    canonical = tuple(sorted(workload_ids))
    if len(canonical) < 2:
        raise ValueError("combination_key 至少需要两个 workload")
    if len(set(canonical)) != len(canonical):
        raise ValueError("combination_key 不允许重复 workload")
    for workload_id in canonical:
        validate_identifier(workload_id, field_name="workload_id")
    return "+".join(canonical)


def make_colocation_id(combination_key: str, repeat: int) -> str:
    if repeat < 1:
        raise ValueError("repeat 必须 >= 1")
    validate_generated_identifier(combination_key, field_name="combination_key")
    return f"{combination_key}__r{repeat:02d}"


def make_pressure_token(pressure: float) -> str:
    if not math.isfinite(pressure) or not 0.0 <= pressure <= 1.0:
        raise ValueError("pressure 必须位于 [0, 1]")
    return f"p{round(pressure * 100):03d}"


class RunStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INVALID = "invalid"
    CANCELLED = "cancelled"


class RunMode(str, Enum):
    SOLO = "solo"
    PRESSURE_PROFILE = "pressure_profile"
    INTENSITY_PROFILE = "intensity_profile"
    COLOCATION = "colocation"
    EXTRA_TEST = "extra_test"


def make_run_id(
    *,
    experiment_id: str,
    mode: RunMode,
    target_id: str,
    repeat: int,
    resource: str | None = None,
    pressure_requested: float | None = None,
    combination_key: str | None = None,
) -> str:
    validate_identifier(experiment_id, field_name="experiment_id")
    validate_identifier(target_id, field_name="target_id")
    if repeat < 1:
        raise ValueError("repeat 必须 >= 1")

    mode_token = {
        RunMode.PRESSURE_PROFILE: "profile",
        RunMode.INTENSITY_PROFILE: "intensity",
        RunMode.EXTRA_TEST: "extra",
    }.get(mode, mode.value)
    subject = combination_key if mode in {RunMode.COLOCATION, RunMode.EXTRA_TEST} else target_id
    if subject is None:
        raise ValueError(f"{mode.value} 模式需要 combination_key")
    validate_generated_identifier(subject, field_name="run subject")

    parts = [experiment_id, mode_token, subject]
    if resource is not None:
        parts.append(validate_identifier(resource, field_name="resource"))
    if pressure_requested is not None:
        parts.append(make_pressure_token(pressure_requested))
    parts.append(f"r{repeat:02d}")
    return validate_generated_identifier("__".join(parts), field_name="run_id")


class HostSpec(StrictModel):
    id: str
    platform: Literal["windows"] = "windows"
    gpu_index: int = Field(default=0, ge=0)
    display_index: int = Field(default=0, ge=0)
    dpi_awareness: Literal["unaware", "system", "per_monitor_v2"] = "per_monitor_v2"
    window_layout: str = "grid_2x2"
    require_visible_windows: bool = True
    cpu_affinity: tuple[int, ...] | None = None
    cooldown_s: float = Field(default=20.0, ge=0, le=3600)
    max_gpu_temp_c: float = Field(default=82.0, ge=30, le=110)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value, field_name="host.id")

    @field_validator("window_layout")
    @classmethod
    def validate_window_layout(cls, value: str) -> str:
        return validate_identifier(value, field_name="window_layout")

    @field_validator("cpu_affinity")
    @classmethod
    def validate_cpu_affinity(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if value is None:
            return None
        if not value or any(cpu < 0 for cpu in value) or len(set(value)) != len(value):
            raise ValueError("cpu_affinity 必须是非空、非负且不重复的 CPU 编号")
        return tuple(sorted(value))


class WorkloadSpec(StrictModel):
    id: str
    driver: Literal["pyxel_game"] = "pyxel_game"
    entrypoint: str
    working_directory: str
    controller: str
    seed: int = Field(ge=0)
    display_scale: int = Field(default=2, ge=1, le=8)

    @field_validator("id", "controller")
    @classmethod
    def validate_ids(cls, value: str, info: Any) -> str:
        return validate_identifier(value, field_name=info.field_name)

    @field_validator("entrypoint", "working_directory")
    @classmethod
    def validate_paths(cls, value: str, info: Any) -> str:
        return validate_repo_relative_path(value, field_name=info.field_name)


class WorkloadDefaults(StrictModel):
    audio_mode: Literal["muted", "enabled"] = "muted"
    input_mode: Literal["deterministic_engine_api"] = "deterministic_engine_api"
    preserve_game_logic: bool = True


class WorkloadCatalog(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    workloads: tuple[WorkloadSpec, ...]
    defaults: WorkloadDefaults = Field(default_factory=WorkloadDefaults)

    @model_validator(mode="after")
    def validate_unique_workloads(self) -> "WorkloadCatalog":
        ids = [workload.id for workload in self.workloads]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("workloads 必须非空且 id 不重复")
        return self


class MeasurementSpec(StrictModel):
    warmup_s: float = Field(default=20.0, ge=0, le=3600)
    duration_s: float = Field(default=60.0, gt=0, le=86400)
    sample_interval_s: float = Field(default=1.0, gt=0, le=3600)
    repeats: int = Field(default=3, ge=1, le=100)
    qos_ratios: tuple[float, ...] = (0.70, 0.80, 0.90)
    random_seed: int = Field(default=20260811, ge=0)

    @field_validator("qos_ratios")
    @classmethod
    def validate_qos_ratios(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not value or any(not 0 < ratio <= 1 for ratio in value):
            raise ValueError("qos_ratios 必须非空且每项位于 (0, 1]")
        if len(set(value)) != len(value):
            raise ValueError("qos_ratios 不允许重复")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_sample_interval(self) -> "MeasurementSpec":
        if self.sample_interval_s > self.duration_s:
            raise ValueError("sample_interval_s 不得大于 duration_s")
        return self


class PathsSpec(StrictModel):
    raw: str = "data/raw"
    interim: str = "data/interim"
    processed: str = "data/processed"
    artifacts: str = "artifacts"

    @field_validator("raw", "interim", "processed", "artifacts")
    @classmethod
    def validate_paths(cls, value: str, info: Any) -> str:
        return validate_repo_relative_path(value, field_name=f"paths.{info.field_name}")


class LocalConfig(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    host: HostSpec
    measurement: MeasurementSpec
    paths: PathsSpec


class PairCombinationSpec(StrictModel):
    mode: Literal["all", "none"] = "all"
    expected_count: int = Field(ge=0, le=10_000)


class TripleCombinationSpec(StrictModel):
    mode: Literal["all", "none", "balanced_subset_v1"] = "balanced_subset_v1"
    expected_count: int = Field(ge=0, le=10_000)
    seed: int | None = Field(default=None, ge=0)


class MainCombinationSpec(StrictModel):
    pairs: PairCombinationSpec
    triples: TripleCombinationSpec


class ExtraTestSpec(StrictModel):
    size: int = Field(default=4, ge=2, le=8)
    mode: Literal["all", "none", "balanced_binary_design_v1"]
    expected_count: int = Field(ge=0, le=10_000)
    trainable: Literal[False] = False


class SplitSpec(StrictModel):
    group_by: Literal["combination_key"] = "combination_key"
    seed: int = Field(ge=0)
    train_groups: int = Field(ge=0)
    validation_groups: int = Field(ge=0)
    test_groups: int = Field(ge=0)


class ExperimentSpec(StrictModel):
    """Step 5 计划生成所需的完整实验配置。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    name: str
    workload_ids: tuple[str, ...]
    resources: tuple[
        Literal["cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory"], ...
    ]
    pressure_levels: tuple[float, ...]
    repeats: int = Field(ge=1, le=100)
    randomize_order: bool = True
    main_combinations: MainCombinationSpec
    extra_test: ExtraTestSpec
    split: SplitSpec

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_identifier(value, field_name="experiment.name")

    @field_validator("workload_ids")
    @classmethod
    def validate_workload_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) < 2 or len(value) > 8:
            raise ValueError("workload_ids 数量必须位于 [2, 8]")
        for workload_id in value:
            validate_identifier(workload_id, field_name="workload_ids")
        if len(set(value)) != len(value):
            raise ValueError("workload_ids 不允许重复")
        return value

    @field_validator("resources")
    @classmethod
    def validate_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("resources 必须非空且不重复")
        return value

    @field_validator("pressure_levels")
    @classmethod
    def validate_pressure_levels(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if (
            not value
            or any(not math.isfinite(level) or not 0.0 <= level <= 1.0 for level in value)
            or tuple(sorted(value)) != value
            or len(set(value)) != len(value)
        ):
            raise ValueError("pressure_levels 必须是 [0, 1] 内严格递增且不重复的有限数")
        return value

    @model_validator(mode="after")
    def validate_combination_counts(self) -> "ExperimentSpec":
        workload_count = len(self.workload_ids)
        expected_pairs = math.comb(workload_count, 2)
        pair_count = self.main_combinations.pairs.expected_count
        pair_mode = self.main_combinations.pairs.mode
        if pair_count != (expected_pairs if pair_mode == "all" else 0):
            raise ValueError("pairs.expected_count 与 mode/workload 数量不一致")

        triple = self.main_combinations.triples
        if triple.mode == "all":
            expected_triples = math.comb(workload_count, 3)
        elif triple.mode == "none":
            expected_triples = 0
        else:
            if workload_count != 8 or triple.expected_count != 32 or triple.seed is None:
                raise ValueError("balanced_subset_v1 固定要求 8 个 workload、32 个组合和 seed")
            expected_triples = 32
        if triple.expected_count != expected_triples:
            raise ValueError("triples.expected_count 与 mode/workload 数量不一致")

        extra = self.extra_test
        if extra.size > workload_count:
            raise ValueError("extra_test.size 不得超过 workload 数量")
        if extra.mode == "all":
            expected_extra = math.comb(workload_count, extra.size)
        elif extra.mode == "none":
            expected_extra = 0
        else:
            if workload_count != 8 or extra.size != 4 or extra.expected_count != 12:
                raise ValueError("balanced_binary_design_v1 固定要求 8 个 workload、四元、12 个组合")
            expected_extra = 12
        if extra.expected_count != expected_extra:
            raise ValueError("extra_test.expected_count 与 mode/workload 数量不一致")

        main_group_count = pair_count + triple.expected_count
        split_group_count = (
            self.split.train_groups + self.split.validation_groups + self.split.test_groups
        )
        if split_group_count != main_group_count:
            raise ValueError("split 的 group 数之和必须等于主组合数")
        return self


class RunSpec(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str | None = None
    experiment_id: str
    mode: RunMode
    target_id: str
    neighbor_ids: tuple[str, ...] = ()
    combination_key: str | None = None
    colocation_id: str | None = None
    game_entrypoint: str | None = None
    game_sha256: str | None = None
    controller: str | None = None
    resource: str | None = None
    pressure_requested: float | None = Field(default=None, ge=0, le=1)
    repeat: int = Field(default=1, ge=1, le=999)
    seed: int = Field(default=0, ge=0)
    warmup_s: float = Field(default=20.0, ge=0, le=3600)
    duration_s: float = Field(default=60.0, gt=0, le=86400)
    host_id: str
    config_sha256: str | None = None
    root_commit: str | None = None
    status: RunStatus = RunStatus.PLANNED

    @field_validator("experiment_id", "target_id", "host_id", "controller", "resource")
    @classmethod
    def validate_optional_ids(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return validate_identifier(value, field_name=info.field_name)

    @field_validator("neighbor_ids")
    @classmethod
    def validate_neighbors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for workload_id in value:
            validate_identifier(workload_id, field_name="neighbor_ids")
        if len(value) != len(set(value)):
            raise ValueError("neighbor_ids 不允许重复")
        return tuple(sorted(value))

    @field_validator("game_entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str | None) -> str | None:
        return (
            validate_repo_relative_path(value, field_name="game_entrypoint")
            if value is not None
            else None
        )

    @field_validator("game_sha256", "config_sha256")
    @classmethod
    def validate_hashes(cls, value: str | None, info: Any) -> str | None:
        if value is not None and not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(f"{info.field_name} 必须是 64 位十六进制 SHA-256")
        return value.lower() if value is not None else None

    @field_validator("root_commit")
    @classmethod
    def validate_commit(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
            raise ValueError("root_commit 必须是 7 到 64 位十六进制 Git commit")
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def canonicalize_ids(self) -> "RunSpec":
        if self.target_id in self.neighbor_ids:
            raise ValueError("target_id 不能同时出现在 neighbor_ids")

        expected_combination = (
            make_combination_key((self.target_id, *self.neighbor_ids))
            if self.neighbor_ids
            else None
        )
        if self.combination_key is not None and self.combination_key != expected_combination:
            raise ValueError("combination_key 与 target_id/neighbor_ids 不一致")
        if self.colocation_id is not None and expected_combination is None:
            raise ValueError("没有 neighbor_ids 时不得设置 colocation_id")

        combination = self.combination_key or expected_combination
        colocation = (
            make_colocation_id(combination, self.repeat) if combination is not None else None
        )
        if self.colocation_id is not None and self.colocation_id != colocation:
            raise ValueError("colocation_id 与 combination_key/repeat 不一致")

        generated_run_id = make_run_id(
            experiment_id=self.experiment_id,
            mode=self.mode,
            target_id=self.target_id,
            repeat=self.repeat,
            resource=self.resource,
            pressure_requested=self.pressure_requested,
            combination_key=combination,
        )
        if self.run_id is not None and self.run_id != generated_run_id:
            raise ValueError("run_id 与规范化运行字段不一致")

        object.__setattr__(self, "combination_key", combination)
        object.__setattr__(self, "colocation_id", colocation)
        object.__setattr__(self, "run_id", generated_run_id)
        return self


def _ensure_json_safe(value: Any, path: str = "values") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} 不允许 NaN 或 Infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_json_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} 的对象键必须为字符串")
            _ensure_json_safe(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} 包含不可 JSON 序列化的类型: {type(value).__name__}")


class MetricEvent(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    source: str
    wall_time_ns: int = Field(gt=0)
    monotonic_time_ns: int = Field(ge=0)
    sequence: int = Field(default=0, ge=0)
    values: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        return validate_generated_identifier(value, field_name="run_id")

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return validate_identifier(value, field_name="source")

    @field_validator("values")
    @classmethod
    def validate_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        _ensure_json_safe(value)
        return value


class SystemMetricEvent(StrictModel):
    """Step 2 系统遥测的扁平 JSONL 契约。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    source: Literal["system"] = "system"
    wall_time_ns: int = Field(gt=0)
    monotonic_time_ns: int = Field(ge=0)
    sequence: int = Field(ge=0)
    process_pid: int = Field(ge=0)
    cpu_util_pct: float = Field(ge=0, le=100)
    cpu_freq_mhz: float | None = Field(default=None, ge=0)
    ram_used_bytes: int = Field(ge=0)
    ram_available_bytes: int = Field(ge=0)
    # 单进程可能使用多个逻辑核，psutil 因而允许返回超过 100 的百分比。
    process_cpu_util_pct: float = Field(ge=0)
    process_rss_bytes: int = Field(ge=0)
    gpu_util_pct: float | None = Field(default=None, ge=0, le=100)
    gpu_mem_util_pct: float | None = Field(default=None, ge=0, le=100)
    gpu_mem_used_bytes: int | None = Field(default=None, ge=0)
    gpu_clock_mhz: float | None = Field(default=None, ge=0)
    gpu_power_w: float | None = Field(default=None, ge=0)
    gpu_temp_c: float | None = Field(default=None, ge=0, le=150)

    @field_validator("run_id")
    @classmethod
    def validate_system_run_id(cls, value: str) -> str:
        return validate_generated_identifier(value, field_name="run_id")


class TelemetryStatus(StrictModel):
    """机器可读 status.json；错误时不包含 traceback 或本机绝对路径。"""

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str
    status: RunStatus
    started_wall_time_ns: int = Field(gt=0)
    updated_wall_time_ns: int = Field(gt=0)
    finished_wall_time_ns: int | None = Field(default=None, gt=0)
    samples_written: int = Field(default=0, ge=0)
    error_type: str | None = None
    error_message: str | None = None
    summary_file: str | None = None

    @field_validator("run_id")
    @classmethod
    def validate_status_run_id(cls, value: str) -> str:
        return validate_generated_identifier(value, field_name="run_id")

    @field_validator("summary_file")
    @classmethod
    def validate_summary_file(cls, value: str | None) -> str | None:
        return (
            validate_repo_relative_path(value, field_name="summary_file")
            if value is not None
            else None
        )

    @model_validator(mode="after")
    def validate_status_fields(self) -> "TelemetryStatus":
        if self.updated_wall_time_ns < self.started_wall_time_ns:
            raise ValueError("updated_wall_time_ns 不得早于 started_wall_time_ns")
        if (
            self.finished_wall_time_ns is not None
            and self.finished_wall_time_ns < self.started_wall_time_ns
        ):
            raise ValueError("finished_wall_time_ns 不得早于 started_wall_time_ns")
        if self.status is RunStatus.FAILED and not self.error_type:
            raise ValueError("failed 状态必须包含 error_type")
        if self.status is not RunStatus.FAILED and (self.error_type or self.error_message):
            raise ValueError("只有 failed 状态可以包含错误字段")
        return self
