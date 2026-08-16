"""从 480 个正式 profile attempts 构建 GAugur 敏感度与干扰强度特征。"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .benchmarks.calibration import CALIBRATION_TIMING_SEMANTICS
from .config import config_sha256, stable_json_dumps
from .runner.plan import load_plan_rows, verify_plan
from .runner.runner import ParsedPlanRow, inspect_resume


RESOURCES = ("cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory")
PRESSURES = (0.0, 0.25, 0.5, 0.75, 1.0)
REPEATS = (1, 2, 3)
PROFILE_RUN_COUNT = 8 * len(RESOURCES) * len(PRESSURES) * len(REPEATS)
_AMENDMENT_ALLOWED_COLUMNS = {
    "execution_index",
    "max_gpu_temp_c",
    "config_sha256",
    "root_commit",
    "run_directory",
    "row_sha256",
}
_SHORT_AMENDMENT_ALLOWED_COLUMNS = _AMENDMENT_ALLOWED_COLUMNS | {
    "warmup_s",
    "duration_s",
    "cooldown_s",
}
_ORIGINAL_PROFILE_PROTOCOL = (20.0, 60.0, 20.0, 82.0)
_THERMAL_PROFILE_PROTOCOL = (20.0, 60.0, 20.0, 84.0)
_SHORT_PROFILE_PROTOCOL = (10.0, 30.0, 10.0, 84.0)
_SAFETY_V2_PROFILE_PROTOCOL = (10.0, 30.0, 10.0, 80.0)
_IDENTITY_PRESSURE_CAPS = {resource: 1.0 for resource in RESOURCES}


class ProfileError(RuntimeError):
    """profile 缺失、输入不兼容或质量门失败。"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_repo(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve()
    candidate = path.resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"路径必须位于仓库内且不能等于根目录: {path}")
    return candidate


def _relative(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_std(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _profile_plan_rows(plan_file: Path) -> list[ParsedPlanRow]:
    rows = [
        ParsedPlanRow.from_csv(raw)
        for raw in load_plan_rows(plan_file)
        if raw["stage"] == "profile"
    ]
    for row in rows:
        target = row.raw.get("target_id")
        neighbors = json.loads(row.raw.get("neighbor_ids", "[]"))
        if (
            row.mode != "pressure_profile"
            or len(row.workload_ids) != 1
            or target != row.workload_ids[0]
            or neighbors != []
            or row.resource not in RESOURCES
            or row.pressure_requested not in PRESSURES
            or row.repeat not in REPEATS
        ):
            raise ProfileError(f"profile 计划行结构非法: {row.run_id}")
    if len(rows) != PROFILE_RUN_COUNT:
        raise ProfileError(f"正式 profile 必须恰有 {PROFILE_RUN_COUNT} 行，实际 {len(rows)}")
    keys = [
        (row.workload_ids[0], row.resource, row.pressure_requested, row.repeat)
        for row in rows
    ]
    if len(set(keys)) != len(keys):
        raise ProfileError("profile 计划存在重复 workload/resource/pressure/repeat 单元")
    workloads = sorted({row.workload_ids[0] for row in rows})
    expected = {
        (workload, resource, pressure, repeat)
        for workload in workloads
        for resource in RESOURCES
        for pressure in PRESSURES
        for repeat in REPEATS
    }
    if len(workloads) != 8 or set(keys) != expected:
        raise ProfileError("profile 计划未完整覆盖 8×4×5×3 笛卡尔积")
    protocols = {
        (row.warmup_s, row.duration_s, row.cooldown_s, row.max_gpu_temp_c)
        for row in rows
    }
    accepted = {
        _ORIGINAL_PROFILE_PROTOCOL,
        _THERMAL_PROFILE_PROTOCOL,
        _SHORT_PROFILE_PROTOCOL,
        _SAFETY_V2_PROFILE_PROTOCOL,
    }
    if len(protocols) != 1 or not protocols.issubset(accepted):
        raise ProfileError(
            "profile 计划时序/温度协议必须全表唯一且为已审计版本: "
            f"{sorted(protocols)}"
        )
    return rows


def _load_solo_baselines(
    *, path: Path, expected_plan_sha256: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    payload = _read_json(path)
    if payload.get("status") != "passed" or payload.get("plan", {}).get("sha256") != expected_plan_sha256:
        raise ProfileError("solo baseline 状态或 plan SHA-256 不匹配")
    baselines = {
        str(item["workload_id"]): item for item in payload.get("baselines", [])
    }
    if len(baselines) != 8 or any(item.get("valid_for_retention") is not True for item in baselines.values()):
        raise ProfileError("必须提供 8 个通过质量门的 solo baseline")
    return baselines, payload


def _verify_profile_plan_amendment(
    *,
    repo_root: Path,
    profile_plan_file: Path,
    baseline_plan_file: Path,
    profile_plan_sha256: str,
    baseline_plan_sha256: str,
    mode: str,
    expected_profile_protocol: tuple[float, float, float, float],
    allowed_changed_columns: set[str],
    required_changed_columns: set[str],
    expected_manifest_stage: str,
    expected_manifest_rows: int,
) -> dict[str, Any]:
    """逐行证明 profile 修订只改变声明过的协议与身份字段。"""

    del repo_root
    profile_rows = [row for row in load_plan_rows(profile_plan_file) if row["stage"] == "profile"]
    baseline_rows = [row for row in load_plan_rows(baseline_plan_file) if row["stage"] == "profile"]
    if len(profile_rows) != PROFILE_RUN_COUNT or len(baseline_rows) != PROFILE_RUN_COUNT:
        raise ProfileError("温控修订前后都必须完整包含 480 个 profile 单元")
    profile_by_id = {row["run_id"]: row for row in profile_rows}
    baseline_by_id = {row["run_id"]: row for row in baseline_rows}
    if len(profile_by_id) != PROFILE_RUN_COUNT or set(profile_by_id) != set(baseline_by_id):
        raise ProfileError("温控修订前后的 profile run_id 集合不一致")

    changed_columns: set[str] = set()
    for run_id in sorted(profile_by_id):
        amended = profile_by_id[run_id]
        original = baseline_by_id[run_id]
        if set(amended) != set(original):
            raise ProfileError(f"温控修订前后的 CSV schema 不一致: {run_id}")
        differences = {
            column for column in amended if str(amended[column]) != str(original[column])
        }
        unexpected = differences - allowed_changed_columns
        if unexpected:
            raise ProfileError(
                f"profile 修订改变了未声明字段: {run_id}: {', '.join(sorted(unexpected))}"
            )
        changed_columns.update(differences)

    if not required_changed_columns.issubset(changed_columns):
        missing = sorted(required_changed_columns - changed_columns)
        raise ProfileError(f"profile 修订缺少必要差异字段: {', '.join(missing)}")
    original_protocols = {
        (
            float(row["warmup_s"]),
            float(row["duration_s"]),
            float(row["cooldown_s"]),
            float(row["max_gpu_temp_c"]),
        )
        for row in baseline_rows
    }
    amended_protocols = {
        (
            float(row["warmup_s"]),
            float(row["duration_s"]),
            float(row["cooldown_s"]),
            float(row["max_gpu_temp_c"]),
        )
        for row in profile_rows
    }
    if original_protocols != {_ORIGINAL_PROFILE_PROTOCOL} or amended_protocols != {
        expected_profile_protocol
    }:
        raise ProfileError(
            "profile 修订协议与审计版本不一致，实际 "
            f"{sorted(original_protocols)} -> {sorted(amended_protocols)}"
        )
    original_directories = {row["run_directory"] for row in baseline_rows}
    amended_directories = {row["run_directory"] for row in profile_rows}
    if original_directories & amended_directories:
        raise ProfileError("profile 修订必须使用与原计划完全分离的 raw 目录")

    profile_manifest = _read_json(
        profile_plan_file.with_name(f"{profile_plan_file.stem}-manifest.json")
    )
    baseline_manifest = _read_json(
        baseline_plan_file.with_name(f"{baseline_plan_file.stem}-manifest.json")
    )
    if (
        profile_manifest.get("root_dirty_at_generation") is not False
        or profile_manifest.get("selected_stage") != expected_manifest_stage
        or int(profile_manifest.get("row_count", 0)) != expected_manifest_rows
        or baseline_manifest.get("root_dirty_at_generation") is not False
    ):
        raise ProfileError("profile 修订计划与 baseline 父计划都必须来自干净提交")

    return {
        "mode": mode,
        "profile_plan_sha256": profile_plan_sha256,
        "baseline_plan_sha256": baseline_plan_sha256,
        "profile_row_count": PROFILE_RUN_COUNT,
        "baseline_protocol": {
            "warmup_s": _ORIGINAL_PROFILE_PROTOCOL[0],
            "duration_s": _ORIGINAL_PROFILE_PROTOCOL[1],
            "cooldown_s": _ORIGINAL_PROFILE_PROTOCOL[2],
            "max_gpu_temp_c": _ORIGINAL_PROFILE_PROTOCOL[3],
        },
        "profile_protocol": {
            "warmup_s": expected_profile_protocol[0],
            "duration_s": expected_profile_protocol[1],
            "cooldown_s": expected_profile_protocol[2],
            "max_gpu_temp_c": expected_profile_protocol[3],
        },
        "baseline_max_gpu_temp_c": _ORIGINAL_PROFILE_PROTOCOL[3],
        "profile_max_gpu_temp_c": expected_profile_protocol[3],
        "changed_columns": sorted(changed_columns),
        "allowed_changed_columns": sorted(allowed_changed_columns),
        "raw_directories_disjoint": True,
        "unmodified_fields_equal": True,
    }


def _verify_thermal_profile_amendment(
    *,
    repo_root: Path,
    profile_plan_file: Path,
    baseline_plan_file: Path,
    profile_plan_sha256: str,
    baseline_plan_sha256: str,
) -> dict[str, Any]:
    """证明旧 t84 计划只改变温控与非实验身份字段。"""

    result = _verify_profile_plan_amendment(
        repo_root=repo_root,
        profile_plan_file=profile_plan_file,
        baseline_plan_file=baseline_plan_file,
        profile_plan_sha256=profile_plan_sha256,
        baseline_plan_sha256=baseline_plan_sha256,
        mode="thermal_profile_amendment_v1",
        expected_profile_protocol=_THERMAL_PROFILE_PROTOCOL,
        allowed_changed_columns=_AMENDMENT_ALLOWED_COLUMNS,
        required_changed_columns={
            "max_gpu_temp_c",
            "config_sha256",
            "run_directory",
            "row_sha256",
        },
        expected_manifest_stage="profile",
        expected_manifest_rows=PROFILE_RUN_COUNT,
    )
    # 保持 v1 证据 schema 完全不变，使既有 thermal-amendment.json 可逐字重算。
    return {
        "mode": result["mode"],
        "profile_plan_sha256": result["profile_plan_sha256"],
        "baseline_plan_sha256": result["baseline_plan_sha256"],
        "profile_row_count": result["profile_row_count"],
        "baseline_max_gpu_temp_c": result["baseline_max_gpu_temp_c"],
        "profile_max_gpu_temp_c": result["profile_max_gpu_temp_c"],
        "changed_columns": result["changed_columns"],
        "allowed_changed_columns": result["allowed_changed_columns"],
        "raw_directories_disjoint": result["raw_directories_disjoint"],
        "semantic_fields_equal": True,
    }


def _verify_short_profile_amendment(
    *,
    repo_root: Path,
    profile_plan_file: Path,
    baseline_plan_file: Path,
    profile_plan_sha256: str,
    baseline_plan_sha256: str,
) -> dict[str, Any]:
    """证明 s30 计划仅采用声明的 10/30/10 时序与 t84 温度门。"""

    result = _verify_profile_plan_amendment(
        repo_root=repo_root,
        profile_plan_file=profile_plan_file,
        baseline_plan_file=baseline_plan_file,
        profile_plan_sha256=profile_plan_sha256,
        baseline_plan_sha256=baseline_plan_sha256,
        mode="short_profile_amendment_s30_v2",
        expected_profile_protocol=_SHORT_PROFILE_PROTOCOL,
        allowed_changed_columns=_SHORT_AMENDMENT_ALLOWED_COLUMNS,
        required_changed_columns={
            "warmup_s",
            "duration_s",
            "cooldown_s",
            "max_gpu_temp_c",
            "config_sha256",
            "run_directory",
            "row_sha256",
        },
        expected_manifest_stage="all",
        expected_manifest_rows=720,
    )
    result["semantic_fields_equal_except_timing"] = True
    return result


def _verify_safety_v2_profile_amendment(
    *,
    repo_root: Path,
    profile_plan_file: Path,
    baseline_plan_file: Path,
    profile_plan_sha256: str,
    baseline_plan_sha256: str,
) -> dict[str, Any]:
    """证明 safety-v2 只改变已声明的时序、温控、目录和实际作用压力。"""

    del repo_root
    current = [row for row in load_plan_rows(profile_plan_file) if row["stage"] == "profile"]
    baseline = [row for row in load_plan_rows(baseline_plan_file) if row["stage"] == "profile"]
    current_by_id = {row["run_id"]: row for row in current}
    baseline_by_id = {row["run_id"]: row for row in baseline}
    if (
        len(current_by_id) != PROFILE_RUN_COUNT
        or len(baseline_by_id) != PROFILE_RUN_COUNT
        or set(current_by_id) != set(baseline_by_id)
    ):
        raise ProfileError("safety-v2 与 solo 父计划的 480 个归一化实验单元不一致")

    ignored = {
        "schema_version",
        "execution_index",
        "pressure_applied",
        "warmup_s",
        "duration_s",
        "cooldown_s",
        "max_gpu_temp_c",
        "config_sha256",
        "root_commit",
        "run_directory",
        "row_sha256",
    }
    for run_id, amended in current_by_id.items():
        original = baseline_by_id[run_id]
        common = set(amended) & set(original)
        changed_semantic = {
            key for key in common - ignored if str(amended[key]) != str(original[key])
        }
        if changed_semantic:
            raise ProfileError(
                f"safety-v2 改变了未授权实验语义: {run_id}: {sorted(changed_semantic)}"
            )
        requested = float(amended["pressure_requested"])
        expected_applied = requested * (0.25 if amended["resource"] == "gpu_compute" else 1.0)
        if abs(float(amended.get("pressure_applied", -1)) - expected_applied) > 1e-10:
            raise ProfileError(f"safety-v2 实际压力映射非法: {run_id}")

    protocols = {
        (
            float(row["warmup_s"]),
            float(row["duration_s"]),
            float(row["cooldown_s"]),
            float(row["max_gpu_temp_c"]),
        )
        for row in current
    }
    if protocols != {_SAFETY_V2_PROFILE_PROTOCOL}:
        raise ProfileError(f"safety-v2 协议非法: {sorted(protocols)}")
    if {row["run_directory"] for row in current} & {
        row["run_directory"] for row in baseline
    }:
        raise ProfileError("safety-v2 必须使用隔离的 raw 根目录")
    manifest = _read_json(
        profile_plan_file.with_name(f"{profile_plan_file.stem}-manifest.json")
    )
    if (
        manifest.get("root_dirty_at_generation") is not False
        or manifest.get("selected_stage") != "all"
        or int(manifest.get("row_count", 0)) != 720
    ):
        raise ProfileError("safety-v2 计划必须由干净提交生成且完整包含 720 行")
    return {
        "mode": "safety_v2_capped_gpu_compute",
        "profile_plan_sha256": profile_plan_sha256,
        "baseline_plan_sha256": baseline_plan_sha256,
        "profile_row_count": PROFILE_RUN_COUNT,
        "baseline_protocol": {
            "warmup_s": 20.0,
            "duration_s": 60.0,
            "cooldown_s": 20.0,
            "max_gpu_temp_c": 82.0,
        },
        "profile_protocol": {
            "warmup_s": 10.0,
            "duration_s": 30.0,
            "cooldown_s": 10.0,
            "max_gpu_temp_c": 80.0,
        },
        "pressure_caps": {
            "cpu_compute": 1.0,
            "memory_bandwidth": 1.0,
            "gpu_compute": 0.25,
            "gpu_memory": 1.0,
        },
        "raw_directories_disjoint": True,
        "normalized_experiment_cells_equal": True,
    }


def _resolve_baseline_contract(
    *,
    repo_root: Path,
    profile_plan_file: Path,
    profile_plan_sha256: str,
    solo_baselines_file: Path,
    baseline_plan_file: Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    if baseline_plan_file is None:
        baselines, payload = _load_solo_baselines(
            path=solo_baselines_file, expected_plan_sha256=profile_plan_sha256
        )
        return baselines, payload, None

    baseline_verification = verify_plan(
        repo_root=repo_root, plan_file=baseline_plan_file
    )
    if baseline_verification["status"] != "passed":
        raise ProfileError("solo baseline 父计划未通过 verify")
    baseline_sha256 = str(baseline_verification["plan_sha256"])
    baselines, payload = _load_solo_baselines(
        path=solo_baselines_file, expected_plan_sha256=baseline_sha256
    )
    profile_rows = _profile_plan_rows(profile_plan_file)
    protocol = {
        (row.warmup_s, row.duration_s, row.cooldown_s, row.max_gpu_temp_c)
        for row in profile_rows
    }
    if protocol == {_THERMAL_PROFILE_PROTOCOL}:
        verify_amendment = _verify_thermal_profile_amendment
    elif protocol == {_SHORT_PROFILE_PROTOCOL}:
        verify_amendment = _verify_short_profile_amendment
    elif protocol == {_SAFETY_V2_PROFILE_PROTOCOL}:
        verify_amendment = _verify_safety_v2_profile_amendment
    else:
        raise ProfileError(f"baseline 复用不支持该 profile 协议: {sorted(protocol)}")
    amendment = verify_amendment(
        repo_root=repo_root,
        profile_plan_file=profile_plan_file,
        baseline_plan_file=baseline_plan_file,
        profile_plan_sha256=profile_plan_sha256,
        baseline_plan_sha256=baseline_sha256,
    )
    amendment["baseline_plan"] = _relative(repo_root, baseline_plan_file)
    return baselines, payload, amendment


def _load_standalone_benchmarks(
    *,
    path: Path,
    cv_threshold_pct: float,
    expected_pressure_caps: dict[str, float] | None = None,
    confirmation_path: Path | None = None,
) -> tuple[dict[tuple[str, float], dict[str, Any]], dict[str, Any]]:
    payload = _read_json(path)
    request = payload.get("request", {})
    pressure_caps = dict(expected_pressure_caps or _IDENTITY_PRESSURE_CAPS)
    expected_request = {
        "cpu_workers": 8,
        "gpu_index": 0,
        "gpu_matrix_size": 1024,
        "gpu_memory_max_mib": 1024,
        "memory_buffer_mib": 64,
        "levels": list(PRESSURES),
        "repeats": 3,
        "resources": list(RESOURCES),
    }
    if payload.get("status") != "passed" or payload.get("cell_count") != 60:
        raise ProfileError("Step 4 calibration 未通过或不是 60 个独立单元")
    for key, expected in expected_request.items():
        if request.get(key) != expected:
            raise ProfileError(f"calibration benchmark 参数不兼容: {key}")
    actual_caps = request.get("pressure_caps", _IDENTITY_PRESSURE_CAPS)
    if actual_caps != pressure_caps:
        raise ProfileError(
            f"calibration pressure_caps 不兼容: {actual_caps} != {pressure_caps}"
        )
    if (
        pressure_caps != _IDENTITY_PRESSURE_CAPS
        and request.get("timing_semantics") != CALIBRATION_TIMING_SEMANTICS
    ):
        raise ProfileError(
            "Safety-v2 calibration timing_semantics 不兼容: "
            f"{request.get('timing_semantics')} != {CALIBRATION_TIMING_SEMANTICS}"
        )
    grouped: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for run in payload.get("runs", []):
        resource = str(run.get("resource"))
        pressure = float(run.get("pressure_requested"))
        pressure_applied = float(run.get("pressure_applied", pressure))
        repeat = int(run.get("repeat", 0))
        worker = run.get("worker", {})
        if (
            resource not in RESOURCES
            or pressure not in PRESSURES
            or repeat not in REPEATS
            or worker.get("status") != "completed"
            or worker.get("resource") != resource
            or abs(float(worker.get("pressure_requested", -1)) - pressure_applied) > 1e-10
            or float(worker.get("elapsed_s", 0)) <= 0
        ):
            raise ProfileError(f"calibration worker 状态非法: {run.get('run_key')}")
        operations = int(worker.get("operations", -1))
        if (pressure == 0 and operations != 0) or (pressure > 0 and operations <= 0):
            raise ProfileError(f"calibration operations 非法: {run.get('run_key')}")
        grouped[(resource, pressure)].append(run)
    if len(grouped) != len(RESOURCES) * len(PRESSURES) or any(len(items) != 3 for items in grouped.values()):
        raise ProfileError("calibration 未完整覆盖 4×5×3")

    result: dict[tuple[str, float], dict[str, Any]] = {}
    unstable: dict[tuple[str, float], dict[str, Any]] = {}
    calibration_sha = _file_sha256(path)
    for (resource, pressure), items in sorted(grouped.items()):
        if pressure == 0:
            throughputs: list[float] = []
            mean = None
            std = None
            cv = None
        else:
            throughputs = [
                int(item["worker"]["operations"]) / float(item["worker"]["elapsed_s"])
                for item in items
            ]
            mean = statistics.fmean(throughputs)
            std = _sample_std(throughputs)
            cv = std / mean * 100
        observed = [float(item["observed_pressure"]) for item in items]
        result[(resource, pressure)] = {
            "standalone_id": config_sha256(
                {
                    "calibration_sha256": calibration_sha,
                    "resource": resource,
                    "pressure_requested": pressure,
                    "run_keys": [item["run_key"] for item in items],
                    "throughputs": throughputs,
                }
            ),
            "resource": resource,
            "pressure_requested": pressure,
            "pressure_applied": pressure * pressure_caps[resource],
            "run_keys": [item["run_key"] for item in items],
            "throughputs_ops_per_s": throughputs,
            "throughput_mean_ops_per_s": mean,
            "throughput_sample_std_ops_per_s": std,
            "throughput_cv_pct": cv,
            "observed_pressure_mean": statistics.fmean(observed),
        }
        if cv is not None and (not math.isfinite(cv) or cv > cv_threshold_pct):
            unstable[(resource, pressure)] = result[(resource, pressure)]

    confirmation_payload: dict[str, Any] | None = None
    if unstable:
        first_key = sorted(unstable)[0]
        first_cv = unstable[first_key]["throughput_cv_pct"]
        if confirmation_path is None:
            raise ProfileError(
                f"独立 benchmark 吞吐 CV 超限: {first_key[0]}/{first_key[1]}={first_cv:.3f}%"
            )
        confirmation_payload = _read_json(confirmation_path)
        rule = confirmation_payload.get("selection_rule", {})
        if (
            confirmation_payload.get("status") != "passed"
            or confirmation_payload.get("base_calibration_sha256") != calibration_sha
            or confirmation_payload.get("environment_sha256") != payload.get("environment_sha256")
            or confirmation_payload.get("timing_semantics") != CALIBRATION_TIMING_SEMANTICS
            or float(rule.get("cv_threshold_pct", -1)) != cv_threshold_pct
            or int(rule.get("additional_repeats", -1)) != 2
            or int(rule.get("combined_repeat_count", -1)) != 5
        ):
            raise ProfileError("calibration confirmation 合同、环境或 base hash 不兼容")
        expected_keys = set(unstable)
        combined_rows = confirmation_payload.get("combined_cells", [])
        actual_keys = {
            (str(item.get("resource")), float(item.get("pressure_requested", -1)))
            for item in combined_rows
        }
        if actual_keys != expected_keys:
            raise ProfileError("calibration confirmation 未且仅未覆盖 base 失败集合")
        extra_grouped: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
        for run in confirmation_payload.get("runs", []):
            key = (str(run.get("resource")), float(run.get("pressure_requested", -1)))
            worker = run.get("worker", {})
            if (
                key not in expected_keys
                or int(run.get("repeat", 0)) not in (4, 5)
                or worker.get("status") != "completed"
                or worker.get("resource") != key[0]
                or abs(
                    float(worker.get("pressure_requested", -1))
                    - float(result[key]["pressure_applied"])
                )
                > 1e-10
                or float(worker.get("elapsed_s", 0)) <= 0
                or int(worker.get("operations", 0)) <= 0
            ):
                raise ProfileError("calibration confirmation worker 状态非法")
            extra_grouped[key].append(run)
        if set(extra_grouped) != expected_keys or any(
            {int(run["repeat"]) for run in runs} != {4, 5}
            for runs in extra_grouped.values()
        ):
            raise ProfileError("calibration confirmation 追加重复不完整")
        stored_combined = {
            (str(item["resource"]), float(item["pressure_requested"])): item
            for item in combined_rows
        }
        confirmation_sha = _file_sha256(confirmation_path)
        for key in sorted(expected_keys):
            base_cell = result[key]
            extra_runs = sorted(extra_grouped[key], key=lambda run: int(run["repeat"]))
            extra_values = [
                int(run["worker"]["operations"]) / float(run["worker"]["elapsed_s"])
                for run in extra_runs
            ]
            combined = [*base_cell["throughputs_ops_per_s"], *extra_values]
            mean = statistics.fmean(combined)
            std = _sample_std(combined)
            cv = std / mean * 100
            stored = stored_combined[key]
            if (
                stored.get("status") != "passed"
                or stored.get("combined_throughputs_ops_per_s") != combined
                or abs(float(stored.get("combined_throughput_cv_pct", -1)) - cv) > 1e-10
                or not math.isfinite(cv)
                or cv > cv_threshold_pct
            ):
                raise ProfileError(
                    f"追加确认后独立 benchmark 吞吐 CV 仍超限: {key[0]}/{key[1]}={cv:.3f}%"
                )
            base_cell.update(
                {
                    "standalone_id": config_sha256(
                        {
                            "calibration_sha256": calibration_sha,
                            "confirmation_sha256": confirmation_sha,
                            "resource": key[0],
                            "pressure_requested": key[1],
                            "run_keys": [
                                *base_cell["run_keys"],
                                *[run["run_key"] for run in extra_runs],
                            ],
                            "throughputs": combined,
                        }
                    ),
                    "run_keys": [
                        *base_cell["run_keys"],
                        *[run["run_key"] for run in extra_runs],
                    ],
                    "throughputs_ops_per_s": combined,
                    "throughput_mean_ops_per_s": mean,
                    "throughput_sample_std_ops_per_s": std,
                    "throughput_cv_pct": cv,
                    "confirmation_sha256": confirmation_sha,
                    "confirmation_additional_repeats": 2,
                }
            )
    elif confirmation_path is not None:
        raise ProfileError("base calibration 已通过，不允许附加非必要 confirmation")

    returned_payload = dict(payload)
    returned_payload["denominator_confirmation"] = (
        {
            "sha256": _file_sha256(confirmation_path),
            "selected_cell_count": len(unstable),
        }
        if confirmation_payload is not None and confirmation_path is not None
        else None
    )
    return result, returned_payload


def _plan_pressure_caps(rows: list[ParsedPlanRow]) -> dict[str, float]:
    caps: dict[str, float] = {}
    for resource in RESOURCES:
        resource_rows = [
            row
            for row in rows
            if row.resource == resource and row.pressure_requested == 1.0
        ]
        applied = {row.pressure_applied for row in resource_rows}
        if len(applied) != 1 or None in applied:
            raise ProfileError(f"计划无法确定唯一实际压力上限: {resource}")
        caps[resource] = float(next(iter(applied)))
    for row in rows:
        assert row.resource is not None and row.pressure_requested is not None
        expected = row.pressure_requested * caps[row.resource]
        if row.pressure_applied is None or abs(row.pressure_applied - expected) > 1e-10:
            raise ProfileError(f"计划实际压力映射不一致: {row.run_id}")
    return caps


def audit_profile_inputs(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    calibration_file: Path,
    calibration_confirmation_file: Path | None = None,
    baseline_plan_file: Path | None = None,
    benchmark_cv_threshold_pct: float = 5.0,
) -> dict[str, Any]:
    """不读取 profile attempt，只复核不可变计划与两类分母。"""

    verification = verify_plan(repo_root=repo_root, plan_file=plan_file)
    if verification["status"] != "passed":
        raise ProfileError("正式计划未通过 verify")
    rows = _profile_plan_rows(plan_file)
    baselines, _, amendment = _resolve_baseline_contract(
        repo_root=repo_root,
        profile_plan_file=plan_file,
        profile_plan_sha256=verification["plan_sha256"],
        solo_baselines_file=solo_baselines_file,
        baseline_plan_file=baseline_plan_file,
    )
    pressure_caps = _plan_pressure_caps(rows)
    standalone, calibration = _load_standalone_benchmarks(
        path=calibration_file,
        cv_threshold_pct=benchmark_cv_threshold_pct,
        expected_pressure_caps=pressure_caps,
        confirmation_path=calibration_confirmation_file,
    )
    nonzero_cvs = [
        float(item["throughput_cv_pct"])
        for item in standalone.values()
        if item["throughput_cv_pct"] is not None
    ]
    return {
        "schema_version": 1,
        "status": "passed",
        "profile_plan_rows": len(rows),
        "workload_count": len(baselines),
        "resource_count": len(RESOURCES),
        "pressure_levels": list(PRESSURES),
        "pressure_caps": pressure_caps,
        "repeats": list(REPEATS),
        "plan_sha256": verification["plan_sha256"],
        "baseline_contract": amendment["mode"] if amendment else "same_plan",
        "profile_amendment": amendment,
        "gpu_temperature_max_c": max(row.max_gpu_temp_c for row in rows),
        "solo_baselines_sha256": _file_sha256(solo_baselines_file),
        "calibration_sha256": _file_sha256(calibration_file),
        "calibration_environment_sha256": calibration.get("environment_sha256"),
        "calibration_confirmation": (
            {
                "path": _relative(repo_root, calibration_confirmation_file),
                **calibration["denominator_confirmation"],
            }
            if calibration.get("denominator_confirmation") is not None
            and calibration_confirmation_file is not None
            else None
        ),
        "standalone_nonzero_cell_count": len(nonzero_cvs),
        "standalone_throughput_cv_max_pct": max(nonzero_cvs),
        "standalone_throughput_cv_threshold_pct": benchmark_cv_threshold_pct,
    }


def _hardware_signal(resource: str, system_metrics: Path) -> tuple[str, float | None]:
    field = {
        "cpu_compute": "cpu_util_pct",
        "memory_bandwidth": "cpu_util_pct",
        "gpu_compute": "gpu_util_pct",
        "gpu_memory": "gpu_mem_used_bytes",
    }[resource]
    values = []
    for line in system_metrics.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        value = json.loads(line).get(field)
        if value is not None:
            values.append(float(value))
    return field, statistics.fmean(values) if values else None


def _collect_profile_record(
    *, repo_root: Path, row: ParsedPlanRow, plan_sha256: str
) -> dict[str, Any]:
    decision = inspect_resume(repo_root=repo_root, row=row)
    if decision.get("action") != "skip":
        raise ProfileError(
            f"profile run 尚无完整有效 attempt: {row.run_id} ({decision.get('reason')})"
        )
    attempt_dir = _inside_repo(repo_root, repo_root / str(decision["directory"]))
    manifest = _read_json(attempt_dir / "manifest.json")
    summary = _read_json(attempt_dir / "summary.json")
    if manifest.get("plan_sha256") != plan_sha256:
        raise ProfileError(f"attempt plan SHA-256 不匹配: {row.run_id}")
    provenance = manifest.get("execution_provenance", {})
    if not provenance.get("source_tree_sha256"):
        raise ProfileError(f"attempt 缺少 execution provenance: {row.run_id}")
    workload_id = row.workload_ids[0]
    if (
        summary.get("status") != "completed"
        or summary.get("valid") is not True
        or summary.get("stage") != "profile"
        or summary.get("mode") != "pressure_profile"
        or summary.get("workload_ids") != [workload_id]
        or summary.get("resource") != row.resource
        or float(summary.get("pressure_requested", -1)) != row.pressure_requested
        or float(summary.get("pressure_applied", row.pressure_requested))
        != row.pressure_applied
    ):
        raise ProfileError(f"profile summary 状态/身份非法: {row.run_id}")
    workloads = summary.get("workloads", [])
    benchmark = summary.get("benchmark", {})
    if len(workloads) != 1 or workloads[0].get("workload_id") != workload_id:
        raise ProfileError(f"profile workload summary 非唯一: {row.run_id}")
    if (
        benchmark.get("status") != "completed"
        or benchmark.get("barrier_used") is not True
        or benchmark.get("resource") != row.resource
        or float(benchmark.get("pressure_requested", -1)) != row.pressure_applied
    ):
        raise ProfileError(f"profile benchmark 状态/身份非法: {row.run_id}")
    fps = workloads[0].get("game_fps", {})
    if any(fps.get(key) is None or float(fps[key]) <= 0 for key in ("mean", "p05", "min")):
        raise ProfileError(f"profile FPS 缺失或非正: {row.run_id}")
    elapsed = float(benchmark.get("elapsed_s", 0))
    operations = int(benchmark.get("operations", -1))
    if elapsed <= 0 or (row.pressure_applied == 0 and operations != 0) or (
        row.pressure_applied > 0 and operations <= 0
    ):
        raise ProfileError(f"profile benchmark 吞吐字段非法: {row.run_id}")
    if row.resource == "gpu_memory":
        capacity = int(benchmark.get("capacity_bytes", 0))
        observed = int(benchmark.get("allocated_bytes", 0)) / capacity if capacity else 0.0
    else:
        observed = float(benchmark.get("active_fraction", -1))
    signal_name, signal_mean = _hardware_signal(
        str(row.resource), attempt_dir / "system_metrics.jsonl"
    )
    return {
        "schema_version": 1,
        "experiment_id": row.experiment_id,
        "workload_id": workload_id,
        "resource": row.resource,
        "pressure_requested": row.pressure_requested,
        "pressure_applied": row.pressure_applied,
        "pressure_observed": observed,
        "repeat": row.repeat,
        "run_id": row.run_id,
        "row_sha256": row.row_sha256,
        "attempt": int(decision["attempt"]),
        "attempt_directory": _relative(repo_root, attempt_dir),
        "summary_sha256": _file_sha256(attempt_dir / "summary.json"),
        "execution_root_commit": provenance.get("root_commit"),
        "execution_root_dirty": provenance.get("root_dirty_at_execution"),
        "execution_source_tree_sha256": provenance["source_tree_sha256"],
        "mean_fps": float(fps["mean"]),
        "p05_fps": float(fps["p05"]),
        "min_fps": float(fps["min"]),
        "measurement_coverage_ratio": float(workloads[0]["measurement_coverage_ratio"]),
        "system_coverage_ratio": float(summary["system_coverage_ratio"]),
        "workload_overlap_ratio": float(summary["workload_overlap_ratio"]),
        "gpu_temp_c_max": summary.get("gpu_temp_c_max"),
        "missed_deadline_count": int(workloads[0]["missed_deadline_count"]),
        "benchmark_operations": operations,
        "benchmark_elapsed_s": elapsed,
        "benchmark_active_fraction": float(benchmark.get("active_fraction", 0)),
        "benchmark_throughput_colocated_ops_per_s": operations / elapsed if operations else None,
        "hardware_signal_name": signal_name,
        "hardware_signal_mean": signal_mean,
    }


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average_rank = (cursor + 1 + end) / 2
        for index in order[cursor:end]:
            ranks[index] = average_rank
        cursor = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float | None:
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale == 0 or right_scale == 0:
        return None
    return numerator / (left_scale * right_scale)


def _aggregate(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str, float], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["workload_id"], record["resource"], record["pressure_requested"])].append(record)
    aggregates = []
    for (workload, resource, pressure), items in sorted(grouped.items()):
        items.sort(key=lambda item: item["repeat"])
        sensitivity = [float(item["sensitivity_mean_fps"]) for item in items]
        sensitivity_p05 = [float(item["sensitivity_p05_fps"]) for item in items]
        observed = [float(item["pressure_observed"]) for item in items]
        slowdowns = [
            float(item["intensity_slowdown"])
            for item in items
            if item["intensity_slowdown"] is not None
        ]
        retentions = [
            float(item["benchmark_throughput_retention"])
            for item in items
            if item["benchmark_throughput_retention"] is not None
        ]
        signals = [float(item["hardware_signal_mean"]) for item in items if item["hardware_signal_mean"] is not None]
        colocated = [
            float(item["benchmark_throughput_colocated_ops_per_s"])
            for item in items
            if item["benchmark_throughput_colocated_ops_per_s"] is not None
        ]
        aggregates.append(
            {
                "schema_version": 1,
                "experiment_id": items[0]["experiment_id"],
                "workload_id": workload,
                "resource": resource,
                "pressure_requested": pressure,
                "pressure_applied": items[0].get("pressure_applied", pressure),
                "pressure_observed_mean": statistics.fmean(observed),
                "pressure_observed_sample_std": _sample_std(observed),
                "repeat_count": len(items),
                "repeats": [item["repeat"] for item in items],
                "run_ids": [item["run_id"] for item in items],
                "solo_baseline_id": items[0]["solo_baseline_id"],
                "solo_mean_fps": items[0]["solo_mean_fps"],
                "sensitivity_mean": statistics.fmean(sensitivity),
                "sensitivity_sample_std": _sample_std(sensitivity),
                "sensitivity_p05_mean": statistics.fmean(sensitivity_p05),
                "standalone_benchmark_id": items[0]["standalone_benchmark_id"],
                "benchmark_throughput_solo_ops_per_s": items[0]["benchmark_throughput_solo_ops_per_s"],
                "benchmark_throughput_colocated_mean_ops_per_s": statistics.fmean(colocated) if colocated else None,
                "benchmark_throughput_colocated_sample_std_ops_per_s": _sample_std(colocated) if colocated else None,
                "benchmark_throughput_retention_mean": statistics.fmean(retentions) if retentions else None,
                "benchmark_throughput_retention_sample_std": _sample_std(retentions) if retentions else None,
                "intensity_slowdown_mean": statistics.fmean(slowdowns) if slowdowns else None,
                "intensity_slowdown_sample_std": _sample_std(slowdowns) if slowdowns else None,
                "hardware_signal_name": items[0]["hardware_signal_name"],
                "hardware_signal_mean": statistics.fmean(signals) if signals else None,
            }
        )

    by_curve: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in aggregates:
        by_curve[(item["workload_id"], item["resource"])].append(item)
    curves = []
    for (workload, resource), points in sorted(by_curve.items()):
        points.sort(key=lambda item: item["pressure_requested"])
        first = float(points[0]["sensitivity_mean"])
        last = float(points[-1]["sensitivity_mean"])
        deviations = [
            abs(float(point["sensitivity_mean"]) - (first + (last - first) * float(point["pressure_requested"])))
            for point in points[1:-1]
        ]
        intensities = [float(point["intensity_slowdown_mean"]) for point in points if point["intensity_slowdown_mean"] is not None]
        curves.append(
            {
                "workload_id": workload,
                "resource": resource,
                "pressures": [point["pressure_requested"] for point in points],
                "pressure_observed_means": [point["pressure_observed_mean"] for point in points],
                "sensitivity_means": [point["sensitivity_mean"] for point in points],
                "sensitivity_sample_stds": [point["sensitivity_sample_std"] for point in points],
                "sensitivity_drop_at_max_pressure": 1.0 - last,
                "max_abs_nonlinear_deviation": max(deviations),
                "intensity_slowdown": statistics.fmean(intensities),
            }
        )
    sensitivity_drop = [float(item["sensitivity_drop_at_max_pressure"]) for item in curves]
    intensity = [float(item["intensity_slowdown"]) for item in curves]
    analysis = {
        "curve_count": len(curves),
        "nonlinear_deviation_threshold": 0.02,
        "max_abs_nonlinear_deviation": max(float(item["max_abs_nonlinear_deviation"]) for item in curves),
        "curves_above_nonlinear_threshold": sum(float(item["max_abs_nonlinear_deviation"]) >= 0.02 for item in curves),
        "pearson_sensitivity_drop_vs_intensity": _pearson(sensitivity_drop, intensity),
        "spearman_sensitivity_drop_vs_intensity": _pearson(_rank(sensitivity_drop), _rank(intensity)),
        "interpretation": "相关系数仅描述 32 个 workload-resource 点的实测关系，不预设不相关结论。",
    }
    return aggregates, curves, analysis


def compute_profiles(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    calibration_file: Path,
    calibration_confirmation_file: Path | None = None,
    baseline_plan_file: Path | None = None,
    benchmark_cv_threshold_pct: float = 5.0,
    pressure_zero_tolerance: float = 0.05,
    observed_pressure_tolerance: float = 0.05,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """只读重算 480 行 run、160 行聚合 profile 和 32 条曲线。"""

    audit = audit_profile_inputs(
        repo_root=repo_root,
        plan_file=plan_file,
        solo_baselines_file=solo_baselines_file,
        calibration_file=calibration_file,
        calibration_confirmation_file=calibration_confirmation_file,
        baseline_plan_file=baseline_plan_file,
        benchmark_cv_threshold_pct=benchmark_cv_threshold_pct,
    )
    rows = _profile_plan_rows(plan_file)
    baselines, baseline_payload, amendment = _resolve_baseline_contract(
        repo_root=repo_root,
        profile_plan_file=plan_file,
        profile_plan_sha256=audit["plan_sha256"],
        solo_baselines_file=solo_baselines_file,
        baseline_plan_file=baseline_plan_file,
    )
    pressure_caps = _plan_pressure_caps(rows)
    standalone, calibration_payload = _load_standalone_benchmarks(
        path=calibration_file,
        cv_threshold_pct=benchmark_cv_threshold_pct,
        expected_pressure_caps=pressure_caps,
        confirmation_path=calibration_confirmation_file,
    )
    records = [
        _collect_profile_record(
            repo_root=repo_root, row=row, plan_sha256=audit["plan_sha256"]
        )
        for row in rows
    ]
    for record in records:
        baseline = baselines[record["workload_id"]]
        standalone_cell = standalone[(record["resource"], record["pressure_requested"])]
        colocated = record["benchmark_throughput_colocated_ops_per_s"]
        solo_throughput = standalone_cell["throughput_mean_ops_per_s"]
        record.update(
            {
                "solo_baseline_id": baseline["baseline_id"],
                "solo_mean_fps": float(baseline["mean_fps"]),
                "solo_p05_fps": float(baseline["p05_fps"]),
                "sensitivity_mean_fps": record["mean_fps"] / float(baseline["mean_fps"]),
                "sensitivity_p05_fps": record["p05_fps"] / float(baseline["p05_fps"]),
                "standalone_benchmark_id": standalone_cell["standalone_id"],
                "benchmark_throughput_solo_ops_per_s": solo_throughput,
                "benchmark_throughput_solo_cv_pct": standalone_cell["throughput_cv_pct"],
                "benchmark_throughput_retention": colocated / solo_throughput if colocated else None,
                "intensity_slowdown": solo_throughput / colocated if colocated else None,
            }
        )
    records.sort(key=lambda item: (item["workload_id"], item["resource"], item["pressure_requested"], item["repeat"]))
    aggregates, curves, analysis = _aggregate(records)
    zero = [item for item in aggregates if item["pressure_requested"] == 0]
    max_zero_deviation = max(abs(float(item["sensitivity_mean"]) - 1.0) for item in zero)
    max_observed_error = max(
        abs(
            float(item["pressure_observed"])
            - float(item.get("pressure_applied", item["pressure_requested"]))
        )
        for item in records
    )
    source_hashes = sorted({str(item["execution_source_tree_sha256"]) for item in records})
    root_commits = sorted({str(item["execution_root_commit"]) for item in records})
    plan_manifest = _read_json(plan_file.with_name(f"{plan_file.stem}-manifest.json"))
    checks = {
        "plan_verified": True,
        "plan_generated_from_clean_commit": plan_manifest.get("root_dirty_at_generation") is False,
        "profile_run_count_480": len(records) == PROFILE_RUN_COUNT,
        "aggregate_cell_count_160": len(aggregates) == 8 * 4 * 5,
        "curve_count_32": len(curves) == 8 * 4,
        "three_repeats_per_cell": set(Counter((item["workload_id"], item["resource"], item["pressure_requested"]) for item in records).values()) == {3},
        "single_profile_source_tree": len(source_hashes) == 1,
        "single_profile_root_commit": len(root_commits) == 1,
        "pressure_zero_retention_near_one": max_zero_deviation <= pressure_zero_tolerance,
        "applied_observed_pressure_close": max_observed_error <= observed_pressure_tolerance,
        "coverage": all(float(item["measurement_coverage_ratio"]) >= 0.95 and float(item["system_coverage_ratio"]) >= 0.95 and float(item["workload_overlap_ratio"]) >= 0.95 for item in records),
        "temperature": all(
            item["gpu_temp_c_max"] is None
            or float(item["gpu_temp_c_max"]) <= float(audit["gpu_temperature_max_c"])
            for item in records
        ),
        "standalone_benchmark_stable": audit["standalone_throughput_cv_max_pct"] <= benchmark_cv_threshold_pct,
        "sensitivity_and_intensity_both_present": all(item["sensitivity_mean"] is not None and (item["pressure_requested"] == 0 or item["intensity_slowdown_mean"] is not None) for item in aggregates),
        "nonlinearity_and_correlation_analyzed": analysis["curve_count"] == 32,
    }
    result = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "experiment_id": rows[0].experiment_id,
        "definition": {
            "sensitivity": "profile mean FPS / solo baseline mean FPS",
            "benchmark_throughput": "worker operations / worker formal-window elapsed seconds",
            "benchmark_throughput_retention": "colocated throughput / standalone throughput",
            "intensity_slowdown": "standalone throughput / colocated throughput; equal to fixed-work completion-time slowdown",
            "workload_resource_intensity": "arithmetic mean of intensity slowdown over four nonzero pressure levels after three-repeat aggregation",
        },
        "quality_thresholds": {
            "minimum_repeats": 3,
            "measurement_coverage_min": 0.95,
            "system_coverage_min": 0.95,
            "workload_overlap_min": 0.95,
            "benchmark_cv_max_pct": benchmark_cv_threshold_pct,
            "pressure_zero_retention_abs_deviation_max": pressure_zero_tolerance,
            "applied_observed_pressure_abs_error_max": observed_pressure_tolerance,
            "gpu_temperature_max_c": audit["gpu_temperature_max_c"],
        },
        "inputs": {
            "plan": _relative(repo_root, plan_file),
            "plan_sha256": audit["plan_sha256"],
            "baseline_contract": audit["baseline_contract"],
            "baseline_plan": amendment.get("baseline_plan") if amendment else None,
            "baseline_plan_sha256": (
                amendment.get("baseline_plan_sha256") if amendment else audit["plan_sha256"]
            ),
            "profile_amendment": amendment,
            "solo_baselines": _relative(repo_root, solo_baselines_file),
            "solo_baselines_sha256": _file_sha256(solo_baselines_file),
            "calibration": _relative(repo_root, calibration_file),
            "calibration_sha256": _file_sha256(calibration_file),
            "calibration_environment_sha256": calibration_payload.get("environment_sha256"),
            "calibration_confirmation": audit.get("calibration_confirmation"),
            "solo_execution_source_tree_sha256s": baseline_payload.get("execution", {}).get("source_tree_sha256s", []),
        },
        "execution": {
            "profile_root_commits": root_commits,
            "profile_source_tree_sha256s": source_hashes,
            "profile_dirty_values": sorted({str(item["execution_root_dirty"]) for item in records}),
        },
        "run_count": len(records),
        "aggregate_cell_count": len(aggregates),
        "curve_count": len(curves),
        "max_pressure_zero_retention_abs_deviation": max_zero_deviation,
        "max_applied_observed_pressure_abs_error": max_observed_error,
        "standalone_throughput_cv_max_pct": audit["standalone_throughput_cv_max_pct"],
        "curves": curves,
        "analysis": analysis,
        "checks": checks,
    }
    if result["status"] != "passed":
        failed = [name for name, passed in checks.items() if not passed]
        raise ProfileError("profile 质量门失败: " + ", ".join(failed))
    return result, records, aggregates


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(stable_json_dumps(value, indent=2) + "\n")


def _write_jsonl_exclusive(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(stable_json_dumps(row) + "\n")


def _write_parquet_exclusive(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - 正式环境已锁定 pyarrow。
        raise RuntimeError("profile parquet 需要 pyarrow") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"拒绝覆盖 parquet: {path}")
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path, compression="zstd")


def _write_plots(plot_dir: Path, aggregates: list[dict[str, Any]], curves: list[dict[str, Any]]) -> dict[str, Path]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("profile plot 需要 matplotlib/numpy") from exc
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "sensitivity_curves": plot_dir / "sensitivity-curves.png",
        "intensity_heatmap": plot_dir / "intensity-heatmap.png",
        "sensitivity_intensity": plot_dir / "sensitivity-intensity.png",
    }
    if any(path.exists() for path in paths.values()):
        raise FileExistsError("profile 图表已存在，拒绝覆盖")

    workloads = sorted({item["workload_id"] for item in curves})
    figure, axes = plt.subplots(4, 2, figsize=(12, 15), constrained_layout=True)
    for axis, workload in zip(axes.flat, workloads, strict=True):
        for curve in [item for item in curves if item["workload_id"] == workload]:
            axis.errorbar(curve["pressures"], curve["sensitivity_means"], yerr=curve["sensitivity_sample_stds"], marker="o", capsize=3, label=curve["resource"])
        axis.axhline(1.0, color="black", linewidth=0.8, linestyle="--")
        axis.set_title(workload)
        axis.set_xlabel("requested pressure")
        axis.set_ylabel("FPS retention / sensitivity")
        axis.grid(alpha=0.25)
        axis.legend(fontsize=8)
    with paths["sensitivity_curves"].open("xb") as stream:
        figure.savefig(stream, format="png", dpi=160)
    plt.close(figure)

    matrix = np.array([[next(item["intensity_slowdown"] for item in curves if item["workload_id"] == workload and item["resource"] == resource) for resource in RESOURCES] for workload in workloads])
    figure, axis = plt.subplots(figsize=(10, 6), constrained_layout=True)
    image = axis.imshow(matrix, aspect="auto", cmap="magma")
    axis.set_xticks(range(len(RESOURCES)), RESOURCES, rotation=25, ha="right")
    axis.set_yticks(range(len(workloads)), workloads)
    axis.set_title("Benchmark completion-time slowdown (intensity)")
    for row_index in range(matrix.shape[0]):
        for column_index in range(matrix.shape[1]):
            axis.text(column_index, row_index, f"{matrix[row_index, column_index]:.3f}", ha="center", va="center", color="white" if matrix[row_index, column_index] > matrix.mean() else "black", fontsize=8)
    figure.colorbar(image, ax=axis, label="slowdown ratio")
    with paths["intensity_heatmap"].open("xb") as stream:
        figure.savefig(stream, format="png", dpi=160)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(9, 6), constrained_layout=True)
    for resource in RESOURCES:
        selected = [item for item in curves if item["resource"] == resource]
        axis.scatter([item["sensitivity_drop_at_max_pressure"] for item in selected], [item["intensity_slowdown"] for item in selected], label=resource)
        for item in selected:
            axis.annotate(item["workload_id"], (item["sensitivity_drop_at_max_pressure"], item["intensity_slowdown"]), fontsize=6, alpha=0.75)
    axis.set_xlabel("sensitivity severity: 1 - S(1.0)")
    axis.set_ylabel("intensity slowdown")
    axis.set_title("Sensitivity vs. interference intensity")
    axis.grid(alpha=0.25)
    axis.legend()
    with paths["sensitivity_intensity"].open("xb") as stream:
        figure.savefig(stream, format="png", dpi=160)
    plt.close(figure)
    return paths


def build_profiles(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    calibration_file: Path,
    calibration_confirmation_file: Path | None = None,
    baseline_plan_file: Path | None = None,
    output_file: Path,
    runs_output_file: Path,
    summary_file: Path,
    plot_dir: Path,
) -> dict[str, Any]:
    """独占写出 480 run JSONL、160-row Parquet、summary 和三张图。"""

    output = _inside_repo(repo_root, output_file)
    runs = _inside_repo(repo_root, runs_output_file)
    summary_path = _inside_repo(repo_root, summary_file)
    plots = _inside_repo(repo_root, plot_dir)
    expected = [output, runs, summary_path, plots / "sensitivity-curves.png", plots / "intensity-heatmap.png", plots / "sensitivity-intensity.png"]
    existing = [path for path in expected if path.exists()]
    if existing:
        raise FileExistsError("profile 产物已存在，拒绝覆盖: " + ", ".join(map(str, existing)))
    result, records, aggregates = compute_profiles(
        repo_root=repo_root,
        plan_file=plan_file,
        solo_baselines_file=solo_baselines_file,
        calibration_file=calibration_file,
        calibration_confirmation_file=calibration_confirmation_file,
        baseline_plan_file=baseline_plan_file,
    )
    _write_jsonl_exclusive(runs, records)
    _write_parquet_exclusive(output, aggregates)
    plot_paths = _write_plots(plots, aggregates, result["curves"])
    result["artifacts"] = {
        "profile_runs": _relative(repo_root, runs),
        "profile_runs_sha256": _file_sha256(runs),
        "profiles_parquet": _relative(repo_root, output),
        "profiles_parquet_sha256": _file_sha256(output),
        **{
            name: _relative(repo_root, path)
            for name, path in plot_paths.items()
        },
        **{
            f"{name}_sha256": _file_sha256(path)
            for name, path in plot_paths.items()
        },
    }
    _write_json_exclusive(summary_path, result)
    return result


def verify_profiles(
    *,
    repo_root: Path,
    plan_file: Path,
    solo_baselines_file: Path,
    calibration_file: Path,
    calibration_confirmation_file: Path | None = None,
    baseline_plan_file: Path | None = None,
    profiles_file: Path,
    runs_file: Path,
    summary_file: Path,
    plot_dir: Path,
) -> dict[str, Any]:
    """从原始 attempts 重算并核对 profile 表、JSONL、图表及其哈希。"""

    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("profile verify 需要 pyarrow") from exc
    stored = _read_json(summary_file)
    thresholds = stored["quality_thresholds"]
    recomputed, records, aggregates = compute_profiles(
        repo_root=repo_root,
        plan_file=plan_file,
        solo_baselines_file=solo_baselines_file,
        calibration_file=calibration_file,
        calibration_confirmation_file=calibration_confirmation_file,
        baseline_plan_file=baseline_plan_file,
        benchmark_cv_threshold_pct=float(thresholds["benchmark_cv_max_pct"]),
        pressure_zero_tolerance=float(thresholds["pressure_zero_retention_abs_deviation_max"]),
        observed_pressure_tolerance=float(
            thresholds.get(
                "applied_observed_pressure_abs_error_max",
                thresholds.get("observed_pressure_abs_error_max", 0.05),
            )
        ),
    )
    stored_core = dict(stored)
    artifacts = stored_core.pop("artifacts", {})
    disk_records = [json.loads(line) for line in runs_file.read_text(encoding="utf-8").splitlines() if line]
    disk_aggregates = pq.read_table(profiles_file).to_pylist()
    plots = {
        "sensitivity_curves": plot_dir / "sensitivity-curves.png",
        "intensity_heatmap": plot_dir / "intensity-heatmap.png",
        "sensitivity_intensity": plot_dir / "sensitivity-intensity.png",
    }
    checks = [
        {"name": "summary_recomputed_exactly", "passed": stored_core == recomputed, "actual": config_sha256(stored_core), "expected": config_sha256(recomputed)},
        {"name": "run_records_recomputed_exactly", "passed": disk_records == records, "actual": len(disk_records), "expected": len(records)},
        {"name": "parquet_rows_recomputed_exactly", "passed": disk_aggregates == aggregates, "actual": len(disk_aggregates), "expected": len(aggregates)},
        {"name": "runs_sha256", "passed": _file_sha256(runs_file) == artifacts.get("profile_runs_sha256"), "actual": _file_sha256(runs_file), "expected": artifacts.get("profile_runs_sha256")},
        {"name": "parquet_sha256", "passed": _file_sha256(profiles_file) == artifacts.get("profiles_parquet_sha256"), "actual": _file_sha256(profiles_file), "expected": artifacts.get("profiles_parquet_sha256")},
    ]
    for name, path in plots.items():
        checks.append({"name": f"{name}_sha256", "passed": path.is_file() and _file_sha256(path) == artifacts.get(f"{name}_sha256"), "actual": _file_sha256(path) if path.is_file() else None, "expected": artifacts.get(f"{name}_sha256")})
        checks.append({"name": f"{name}_png_signature", "passed": path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "actual": path.suffix.lower(), "expected": "valid PNG"})
    checks.append({"name": "all_quality_checks", "passed": stored.get("status") == "passed" and all(stored.get("checks", {}).values()), "actual": stored.get("checks"), "expected": True})
    return {
        "schema_version": 1,
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "check_count": len(checks),
        "passed_count": sum(bool(item["passed"]) for item in checks),
        "plan_sha256": recomputed["inputs"]["plan_sha256"],
        "summary_sha256": _file_sha256(summary_file),
        "checks": checks,
    }
