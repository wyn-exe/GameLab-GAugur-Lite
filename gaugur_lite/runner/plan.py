"""把实验 YAML 展开为不可变 CSV，并生成组合与哈希 manifest。"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import random
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Literal

from ..config import (
    config_sha256,
    load_experiment_config,
    load_local_config,
    load_workload_catalog,
    stable_json_dumps,
)
from ..metrics.writer import write_json_atomic
from ..schema import ExperimentSpec, RunMode, RunSpec, make_combination_key

PlanStage = Literal["solo", "profile", "colocation-main", "colocation-extra-test", "all"]
PLAN_STAGES: tuple[PlanStage, ...] = (
    "solo",
    "profile",
    "colocation-main",
    "colocation-extra-test",
    "all",
)

LEGACY_PLAN_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "execution_index",
    "run_id",
    "experiment_id",
    "stage",
    "split",
    "mode",
    "workload_ids",
    "target_id",
    "neighbor_ids",
    "combination_key",
    "colocation_id",
    "resource",
    "pressure_requested",
    "repeat",
    "seed",
    "warmup_s",
    "duration_s",
    "sample_interval_s",
    "cooldown_s",
    "host_id",
    "gpu_index",
    "display_index",
    "window_layout",
    "require_visible_windows",
    "max_gpu_temp_c",
    "config_sha256",
    "root_commit",
    "run_directory",
    "game_entrypoints",
    "game_sha256s",
    "row_sha256",
)

# v2 显式区分建模使用的归一化压力与 benchmark 实际执行压力。
PLAN_COLUMNS: tuple[str, ...] = (
    *LEGACY_PLAN_COLUMNS[: LEGACY_PLAN_COLUMNS.index("pressure_requested") + 1],
    "pressure_applied",
    *LEGACY_PLAN_COLUMNS[LEGACY_PLAN_COLUMNS.index("pressure_requested") + 1 :],
)

_FORMAL_EXTRA_QUADS: tuple[tuple[str, ...], ...] = (
    ("pyxel_jump", "pyxel_snake", "pyxel_platformer", "mega_wing"),
    ("pyxel_bubbles", "pyxel_shooter", "daylight", "space_rescue"),
    ("pyxel_jump", "pyxel_bubbles", "pyxel_platformer", "daylight"),
    ("pyxel_snake", "pyxel_shooter", "mega_wing", "space_rescue"),
    ("pyxel_jump", "pyxel_shooter", "pyxel_platformer", "space_rescue"),
    ("pyxel_bubbles", "pyxel_snake", "daylight", "mega_wing"),
    ("pyxel_jump", "pyxel_bubbles", "pyxel_snake", "pyxel_shooter"),
    ("pyxel_platformer", "daylight", "mega_wing", "space_rescue"),
    ("pyxel_jump", "pyxel_snake", "daylight", "space_rescue"),
    ("pyxel_bubbles", "pyxel_shooter", "pyxel_platformer", "mega_wing"),
    ("pyxel_jump", "pyxel_bubbles", "mega_wing", "space_rescue"),
    ("pyxel_snake", "pyxel_shooter", "pyxel_platformer", "daylight"),
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _repo_relative(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _combination_key(items: Iterable[str]) -> str:
    return make_combination_key(tuple(items))


def _pair_counts(combinations: Iterable[tuple[str, ...]]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for combination in combinations:
        counts.update(itertools.combinations(sorted(combination), 2))
    return counts


def _workload_counts(combinations: Iterable[tuple[str, ...]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for combination in combinations:
        counts.update(combination)
    return counts


def select_balanced_triples(workload_ids: tuple[str, ...], *, seed: int) -> tuple[
    tuple[tuple[str, ...], ...], dict[str, Any]
]:
    """用确定性 MILP 实现 README 中的 balanced_subset_v1 约束与目标。"""

    if len(workload_ids) != 8:
        raise ValueError("balanced_subset_v1 需要恰好 8 个 workload")
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
    except ImportError as exc:  # pragma: no cover - requirements-windows.txt 已固定依赖。
        raise RuntimeError("balanced_subset_v1 需要 scipy.optimize.milp") from exc

    workloads = tuple(sorted(workload_ids))
    candidates = tuple(
        sorted(
            itertools.combinations(workloads, 3),
            key=lambda item: hashlib.sha256(
                f"{seed}:{_combination_key(item)}".encode("utf-8")
            ).hexdigest(),
        )
    )
    pairs = tuple(itertools.combinations(workloads, 2))
    n = len(candidates)

    # 第一阶段只最小化 pair 共现最大值 M。
    objective = np.zeros(n + 1, dtype=float)
    objective[-1] = 1.0
    rows: list[list[float]] = []
    lower: list[float] = []
    upper: list[float] = []
    for workload in workloads:
        rows.append([float(workload in triple) for triple in candidates] + [0.0])
        lower.append(12.0)
        upper.append(12.0)
    rows.append([1.0] * n + [0.0])
    lower.append(32.0)
    upper.append(32.0)
    for pair in pairs:
        rows.append([float(set(pair).issubset(triple)) for triple in candidates] + [-1.0])
        lower.append(-math.inf)
        upper.append(0.0)
    first = milp(
        objective,
        integrality=np.ones(n + 1),
        bounds=Bounds(np.zeros(n + 1), np.array([1.0] * n + [6.0])),
        constraints=LinearConstraint(np.asarray(rows), np.asarray(lower), np.asarray(upper)),
        options={"presolve": True},
    )
    if not first.success or first.x is None:
        raise RuntimeError(f"balanced_subset_v1 第一阶段无解: {first.message}")
    pair_max = int(round(float(first.x[-1])))

    # 第二阶段用 one-hot pair count 精确最小化相对均值 24/7 的平方偏差。
    pair_levels = tuple(range(7))
    y_count = len(pairs) * len(pair_levels)
    variable_count = n + y_count
    objective2 = np.zeros(variable_count, dtype=float)
    for pair_index in range(len(pairs)):
        for level in pair_levels:
            y_index = n + pair_index * len(pair_levels) + level
            objective2[y_index] = float((7 * level - 24) ** 2)
    # 平方偏差是整数；极小的候选 rank 只负责稳定打破等价最优解。
    for rank in range(n):
        objective2[rank] = (rank + 1) * 1e-8

    rows2: list[list[float]] = []
    lower2: list[float] = []
    upper2: list[float] = []
    for workload in workloads:
        rows2.append([float(workload in triple) for triple in candidates] + [0.0] * y_count)
        lower2.append(12.0)
        upper2.append(12.0)
    rows2.append([1.0] * n + [0.0] * y_count)
    lower2.append(32.0)
    upper2.append(32.0)
    for pair_index, pair in enumerate(pairs):
        pair_row = [float(set(pair).issubset(triple)) for triple in candidates] + [0.0] * y_count
        rows2.append(pair_row)
        lower2.append(-math.inf)
        upper2.append(float(pair_max))

        one_hot = [0.0] * variable_count
        count_match = [float(set(pair).issubset(triple)) for triple in candidates] + [0.0] * y_count
        for level in pair_levels:
            y_index = n + pair_index * len(pair_levels) + level
            one_hot[y_index] = 1.0
            count_match[y_index] = -float(level)
        rows2.append(one_hot)
        lower2.append(1.0)
        upper2.append(1.0)
        rows2.append(count_match)
        lower2.append(0.0)
        upper2.append(0.0)

    second = milp(
        objective2,
        integrality=np.ones(variable_count),
        bounds=Bounds(np.zeros(variable_count), np.ones(variable_count)),
        constraints=LinearConstraint(
            np.asarray(rows2), np.asarray(lower2), np.asarray(upper2)
        ),
        options={"presolve": True},
    )
    if not second.success or second.x is None:
        raise RuntimeError(f"balanced_subset_v1 第二阶段无解: {second.message}")
    selected = tuple(candidates[index] for index in range(n) if second.x[index] >= 0.5)
    workload_counts = _workload_counts(selected)
    selected_pair_counts = _pair_counts(selected)
    if len(selected) != 32 or any(workload_counts[item] != 12 for item in workloads):
        raise RuntimeError("balanced_subset_v1 后置校验失败")
    actual_pair_max = max(selected_pair_counts.values())
    if actual_pair_max != pair_max:
        raise RuntimeError("balanced_subset_v1 pair 最大值与求解结果不一致")
    deviation_numerator = sum((7 * selected_pair_counts[pair] - 24) ** 2 for pair in pairs)
    metadata = {
        "algorithm": "balanced_subset_v1",
        "seed": seed,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "workload_occurrences": dict(sorted(workload_counts.items())),
        "pair_cooccurrence": {
            "+".join(pair): selected_pair_counts[pair] for pair in pairs
        },
        "objective": {
            "pair_cooccurrence_max": actual_pair_max,
            "pair_squared_deviation_numerator": deviation_numerator,
            "pair_squared_deviation": deviation_numerator / (49 * len(pairs)),
        },
    }
    return tuple(sorted(selected, key=_combination_key)), metadata


def _main_combinations(experiment: ExperimentSpec) -> tuple[
    tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...], dict[str, Any]
]:
    workloads = tuple(sorted(experiment.workload_ids))
    pairs = (
        tuple(itertools.combinations(workloads, 2))
        if experiment.main_combinations.pairs.mode == "all"
        else ()
    )
    triple_spec = experiment.main_combinations.triples
    if triple_spec.mode == "all":
        triples = tuple(itertools.combinations(workloads, 3))
        triple_meta: dict[str, Any] = {"algorithm": "all", "selected_count": len(triples)}
    elif triple_spec.mode == "none":
        triples = ()
        triple_meta = {"algorithm": "none", "selected_count": 0}
    else:
        assert triple_spec.seed is not None
        triples, triple_meta = select_balanced_triples(workloads, seed=triple_spec.seed)
    if len(pairs) != experiment.main_combinations.pairs.expected_count:
        raise RuntimeError("pair 组合数与配置不一致")
    if len(triples) != triple_spec.expected_count:
        raise RuntimeError("triple 组合数与配置不一致")
    return pairs, triples, triple_meta


def _extra_combinations(experiment: ExperimentSpec) -> tuple[tuple[str, ...], ...]:
    workloads = tuple(sorted(experiment.workload_ids))
    extra = experiment.extra_test
    if extra.mode == "none":
        combinations: tuple[tuple[str, ...], ...] = ()
    elif extra.mode == "all":
        combinations = tuple(itertools.combinations(workloads, extra.size))
    else:
        allowed = set(workloads)
        combinations = tuple(tuple(sorted(item)) for item in _FORMAL_EXTRA_QUADS)
        if any(set(item) - allowed for item in combinations):
            raise ValueError("balanced_binary_design_v1 的固定 workload 与配置不一致")
    if len(combinations) != extra.expected_count:
        raise RuntimeError("extra_test 组合数与配置不一致")
    return tuple(sorted(combinations, key=_combination_key))


def _git_state(repo_root: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
                shell=False,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def _stable_seed(base_seed: int, run_id: str) -> int:
    digest = hashlib.sha256(f"{base_seed}:{run_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _split_main_combinations(
    combinations: tuple[tuple[str, ...], ...], experiment: ExperimentSpec
) -> dict[str, str]:
    ordered = sorted(
        (_combination_key(item) for item in combinations),
        key=lambda key: hashlib.sha256(
            f"{experiment.split.seed}:{key}".encode("utf-8")
        ).hexdigest(),
    )
    train_end = experiment.split.train_groups
    validation_end = train_end + experiment.split.validation_groups
    mapping = {}
    for index, key in enumerate(ordered):
        if index < train_end:
            mapping[key] = "train"
        elif index < validation_end:
            mapping[key] = "validation"
        else:
            mapping[key] = "test"
    if len(mapping) != len(combinations):
        raise RuntimeError("主组合 split 分配不完整")
    return mapping


def _json_cell(value: Any) -> str:
    return stable_json_dumps(value)


def _build_row(
    *,
    repo_root: Path,
    local: Any,
    experiment: ExperimentSpec,
    catalog_by_id: dict[str, Any],
    config_hash: str,
    root_commit: str | None,
    stage: str,
    split: str,
    mode: RunMode,
    workload_ids: tuple[str, ...],
    target_id: str,
    repeat: int,
    resource: str | None = None,
    pressure: float | None = None,
) -> dict[str, Any]:
    canonical_workloads = tuple(sorted(workload_ids))
    neighbors = tuple(item for item in canonical_workloads if item != target_id)
    combination_key = _combination_key(canonical_workloads) if len(canonical_workloads) > 1 else None
    target = catalog_by_id[target_id]
    pressure_applied = (
        None
        if pressure is None or resource is None
        else pressure * float(local.measurement.pressure_caps[resource])
    )
    run = RunSpec(
        experiment_id=experiment.name,
        mode=mode,
        target_id=target_id,
        neighbor_ids=neighbors,
        combination_key=combination_key,
        game_entrypoint=target.entrypoint,
        game_sha256=_file_sha256(repo_root / target.entrypoint),
        controller=target.controller,
        resource=resource,
        pressure_requested=pressure,
        pressure_applied=pressure_applied,
        repeat=repeat,
        seed=0,
        warmup_s=local.measurement.warmup_s,
        duration_s=local.measurement.duration_s,
        host_id=local.host.id,
        config_sha256=config_hash,
        root_commit=root_commit,
    )
    assert run.run_id is not None
    seed = _stable_seed(local.measurement.random_seed, run.run_id)
    entrypoints = {item: catalog_by_id[item].entrypoint for item in canonical_workloads}
    hashes = {
        item: _file_sha256(repo_root / catalog_by_id[item].entrypoint)
        for item in canonical_workloads
    }
    return {
        "schema_version": 2,
        "execution_index": 0,
        "run_id": run.run_id,
        "experiment_id": experiment.name,
        "stage": stage,
        "split": split,
        "mode": mode.value,
        "workload_ids": _json_cell(canonical_workloads),
        "target_id": target_id,
        "neighbor_ids": _json_cell(neighbors),
        "combination_key": run.combination_key or "",
        "colocation_id": run.colocation_id or "",
        "resource": resource or "",
        "pressure_requested": "" if pressure is None else format(pressure, ".10g"),
        "pressure_applied": (
            "" if pressure_applied is None else format(pressure_applied, ".10g")
        ),
        "repeat": repeat,
        "seed": seed,
        "warmup_s": format(local.measurement.warmup_s, ".10g"),
        "duration_s": format(local.measurement.duration_s, ".10g"),
        "sample_interval_s": format(local.measurement.sample_interval_s, ".10g"),
        "cooldown_s": format(local.host.cooldown_s, ".10g"),
        "host_id": local.host.id,
        "gpu_index": local.host.gpu_index,
        "display_index": local.host.display_index,
        "window_layout": local.host.window_layout,
        "require_visible_windows": str(local.host.require_visible_windows).lower(),
        "max_gpu_temp_c": format(local.host.max_gpu_temp_c, ".10g"),
        "config_sha256": config_hash,
        "root_commit": root_commit or "",
        "run_directory": (
            Path(local.paths.raw) / experiment.name / run.run_id
        ).as_posix(),
        "game_entrypoints": _json_cell(entrypoints),
        "game_sha256s": _json_cell(hashes),
        "row_sha256": "",
    }


def _select_rows_for_stage(rows: list[dict[str, Any]], stage: PlanStage) -> list[dict[str, Any]]:
    if stage == "all":
        return rows
    return [row for row in rows if row["stage"] == stage]


def _plan_sidecars(output_file: Path) -> tuple[Path, Path]:
    return (
        output_file.with_name(f"{output_file.stem}-manifest.json"),
        output_file.with_name(f"{output_file.stem}-combinations.json"),
    )


def build_plan(
    *,
    repo_root: Path,
    local_config_path: Path,
    experiment_path: Path,
    workload_catalog_path: Path,
    stage: PlanStage,
    output_file: Path,
) -> dict[str, Any]:
    """完整校验后独占创建 CSV 和两个 sidecar；不会覆盖旧计划。"""

    if stage not in PLAN_STAGES:
        raise ValueError(f"未知 stage: {stage}")
    root = repo_root.resolve()
    output = output_file.resolve()
    if root not in output.parents:
        raise ValueError("计划输出必须位于仓库内")
    plan_manifest_path, combination_manifest_path = _plan_sidecars(output)
    existing = [path for path in (output, plan_manifest_path, combination_manifest_path) if path.exists()]
    if existing:
        raise FileExistsError(f"计划产物已存在，拒绝覆盖: {', '.join(map(str, existing))}")

    local = load_local_config(local_config_path)
    experiment = load_experiment_config(experiment_path)
    catalog = load_workload_catalog(workload_catalog_path)
    catalog_by_id = {item.id: item for item in catalog.workloads}
    unknown = sorted(set(experiment.workload_ids) - set(catalog_by_id))
    if unknown:
        raise ValueError(f"实验配置包含未注册 workload: {', '.join(unknown)}")
    config_bundle = {
        "local": local.model_dump(mode="json"),
        "experiment": experiment.model_dump(mode="json"),
        "workloads": catalog.model_dump(mode="json"),
    }
    config_hash = config_sha256(config_bundle)
    root_commit, root_dirty = _git_state(root)
    pairs, triples, triple_meta = _main_combinations(experiment)
    extras = _extra_combinations(experiment)
    all_main = tuple(sorted((*pairs, *triples), key=_combination_key))
    split_by_key = _split_main_combinations(all_main, experiment)

    rows: list[dict[str, Any]] = []
    for workload_id in experiment.workload_ids:
        for repeat in range(1, experiment.repeats + 1):
            rows.append(
                _build_row(
                    repo_root=root,
                    local=local,
                    experiment=experiment,
                    catalog_by_id=catalog_by_id,
                    config_hash=config_hash,
                    root_commit=root_commit,
                    stage="solo",
                    split="not_applicable",
                    mode=RunMode.SOLO,
                    workload_ids=(workload_id,),
                    target_id=workload_id,
                    repeat=repeat,
                )
            )
    for workload_id, resource, pressure, repeat in itertools.product(
        experiment.workload_ids,
        experiment.resources,
        experiment.pressure_levels,
        range(1, experiment.repeats + 1),
    ):
        rows.append(
            _build_row(
                repo_root=root,
                local=local,
                experiment=experiment,
                catalog_by_id=catalog_by_id,
                config_hash=config_hash,
                root_commit=root_commit,
                stage="profile",
                split="not_applicable",
                mode=RunMode.PRESSURE_PROFILE,
                workload_ids=(workload_id,),
                target_id=workload_id,
                repeat=repeat,
                resource=resource,
                pressure=pressure,
            )
        )
    for combination in (*pairs, *triples):
        canonical = tuple(sorted(combination))
        for repeat in range(1, experiment.repeats + 1):
            rows.append(
                _build_row(
                    repo_root=root,
                    local=local,
                    experiment=experiment,
                    catalog_by_id=catalog_by_id,
                    config_hash=config_hash,
                    root_commit=root_commit,
                    stage="colocation-main",
                    split=split_by_key[_combination_key(canonical)],
                    mode=RunMode.COLOCATION,
                    workload_ids=canonical,
                    target_id=canonical[0],
                    repeat=repeat,
                )
            )
    for combination in extras:
        canonical = tuple(sorted(combination))
        for repeat in range(1, experiment.repeats + 1):
            rows.append(
                _build_row(
                    repo_root=root,
                    local=local,
                    experiment=experiment,
                    catalog_by_id=catalog_by_id,
                    config_hash=config_hash,
                    root_commit=root_commit,
                    stage="colocation-extra-test",
                    split="extra_test",
                    mode=RunMode.EXTRA_TEST,
                    workload_ids=canonical,
                    target_id=canonical[0],
                    repeat=repeat,
                )
            )

    selected = _select_rows_for_stage(rows, stage)
    if experiment.randomize_order:
        random.Random(local.measurement.random_seed).shuffle(selected)
    for execution_index, row in enumerate(selected, start=1):
        row["execution_index"] = execution_index
        # CSV 读取后所有 cell 都是字符串；写入前先规范化，确保 row hash 可复算。
        for key in PLAN_COLUMNS:
            if key != "row_sha256":
                row[key] = str(row[key])
        row["row_sha256"] = config_sha256({key: value for key, value in row.items() if key != "row_sha256"})
    run_ids = [str(row["run_id"]) for row in selected]
    if len(run_ids) != len(set(run_ids)):
        raise RuntimeError("计划出现重复 run_id")

    combination_manifest = {
        "schema_version": 2,
        "experiment_id": experiment.name,
        "config_sha256": config_hash,
        "main": {
            "pair_count": len(pairs),
            "triple_count": len(triples),
            "total_count": len(all_main),
            "pairs": [_combination_key(item) for item in pairs],
            "triples": [_combination_key(item) for item in triples],
            "triple_selection": triple_meta,
            "split": {
                "algorithm": "sha256_group_v1",
                "seed": experiment.split.seed,
                "counts": dict(sorted(Counter(split_by_key.values()).items())),
                "assignments": dict(sorted(split_by_key.items())),
            },
        },
        "extra_test": {
            "algorithm": experiment.extra_test.mode,
            "count": len(extras),
            "combinations": [_combination_key(item) for item in extras],
            "workload_occurrences": dict(sorted(_workload_counts(extras).items())),
            "pair_cooccurrence": {
                "+".join(pair): count
                for pair, count in sorted(_pair_counts(extras).items())
            },
            "trainable": False,
        },
        "checks": {
            "main_keys_unique": len({_combination_key(item) for item in all_main}) == len(all_main),
            "extra_keys_unique": len({_combination_key(item) for item in extras}) == len(extras),
            "main_extra_disjoint": not (
                {_combination_key(item) for item in all_main}
                & {_combination_key(item) for item in extras}
            ),
            "split_complete": len(split_by_key) == len(all_main)
            and set(split_by_key) == {_combination_key(item) for item in all_main},
        },
    }
    if not all(combination_manifest["checks"].values()):
        raise RuntimeError("组合 manifest 后置检查失败")

    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(combination_manifest_path, combination_manifest)
    with output.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PLAN_COLUMNS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(selected)
    plan_hash = _file_sha256(output)
    combination_hash = _file_sha256(combination_manifest_path)
    stage_counts = dict(sorted(Counter(str(row["stage"]) for row in selected).items()))
    manifest = {
        "schema_version": 2,
        "status": "completed",
        "experiment_id": experiment.name,
        "selected_stage": stage,
        "row_count": len(selected),
        "stage_counts": stage_counts,
        "randomized": experiment.randomize_order,
        "random_seed": local.measurement.random_seed,
        "pressure_caps": dict(local.measurement.pressure_caps),
        "config_sha256": config_hash,
        "root_commit": root_commit,
        "root_dirty_at_generation": root_dirty,
        "plan_file": _repo_relative(root, output),
        "plan_sha256": plan_hash,
        "combination_manifest": _repo_relative(root, combination_manifest_path),
        "combination_manifest_sha256": combination_hash,
        "inputs": {
            "local_config": _repo_relative(root, local_config_path),
            "experiment": _repo_relative(root, experiment_path),
            "workloads": _repo_relative(root, workload_catalog_path),
        },
    }
    write_json_atomic(plan_manifest_path, manifest)
    return {"manifest": manifest, "combinations": combination_manifest}


def load_plan_rows(plan_file: Path) -> list[dict[str, str]]:
    with plan_file.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = tuple(reader.fieldnames or ())
        if columns not in {LEGACY_PLAN_COLUMNS, PLAN_COLUMNS}:
            raise ValueError("计划 CSV header 与 schema 不一致")
        return list(reader)


def verify_plan(*, repo_root: Path, plan_file: Path) -> dict[str, Any]:
    """只读复核 CSV、sidecar 哈希、row hash、索引和 run_id 唯一性。"""

    root = repo_root.resolve()
    plan = plan_file.resolve()
    plan_manifest_path, combination_manifest_path = _plan_sidecars(plan)
    manifest = json.loads(plan_manifest_path.read_text(encoding="utf-8"))
    combinations = json.loads(combination_manifest_path.read_text(encoding="utf-8"))
    rows = load_plan_rows(plan)
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, actual: Any, expected: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "actual": actual, "expected": expected})

    actual_plan_hash = _file_sha256(plan)
    actual_combination_hash = _file_sha256(combination_manifest_path)
    add("plan_sha256", actual_plan_hash == manifest.get("plan_sha256"), actual_plan_hash, manifest.get("plan_sha256"))
    add(
        "combination_manifest_sha256",
        actual_combination_hash == manifest.get("combination_manifest_sha256"),
        actual_combination_hash,
        manifest.get("combination_manifest_sha256"),
    )
    add("row_count", len(rows) == manifest.get("row_count"), len(rows), manifest.get("row_count"))
    indices = [int(row["execution_index"]) for row in rows]
    add("execution_indices", indices == list(range(1, len(rows) + 1)), indices[:5], "1..row_count")
    run_ids = [row["run_id"] for row in rows]
    add("unique_run_ids", len(run_ids) == len(set(run_ids)), len(set(run_ids)), len(run_ids))
    bad_hashes = []
    for row in rows:
        expected = config_sha256({key: value for key, value in row.items() if key != "row_sha256"})
        if row["row_sha256"] != expected:
            bad_hashes.append(row["run_id"])
    add("row_sha256", not bad_hashes, bad_hashes, [])
    add(
        "combination_checks",
        all(combinations.get("checks", {}).values()),
        combinations.get("checks"),
        True,
    )
    result = {
        "schema_version": 1,
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "plan": _repo_relative(root, plan),
        "plan_sha256": actual_plan_hash,
        "row_count": len(rows),
        "checks": checks,
    }
    return result
