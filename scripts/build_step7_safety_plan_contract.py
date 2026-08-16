"""逐行证明 Safety-v2 保留 720 个归一化实验单元并只做声明过的修改。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gaugur_lite.config import discover_repo_root, stable_json_dumps
from gaugur_lite.runner.plan import load_plan_rows, verify_plan

DESIGN_COLUMNS = (
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
    "sample_interval_s",
    "host_id",
    "gpu_index",
    "display_index",
    "window_layout",
    "require_visible_windows",
    "game_entrypoints",
    "game_sha256s",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build(repo_root: Path, baseline: Path, safety: Path) -> dict[str, Any]:
    baseline_verification = verify_plan(repo_root=repo_root, plan_file=baseline)
    safety_verification = verify_plan(repo_root=repo_root, plan_file=safety)
    if baseline_verification["status"] != "passed" or safety_verification["status"] != "passed":
        raise RuntimeError("父计划或 Safety-v2 计划未通过哈希验证")

    old_rows = load_plan_rows(baseline)
    new_rows = load_plan_rows(safety)
    old_by_id = {row["run_id"]: row for row in old_rows}
    new_by_id = {row["run_id"]: row for row in new_rows}
    if len(old_by_id) != 720 or len(new_by_id) != 720 or set(old_by_id) != set(new_by_id):
        raise RuntimeError("新旧计划必须具有同一组 720 个唯一 run_id")

    mismatches: list[dict[str, str]] = []
    mapping_counts: Counter[str] = Counter()
    for run_id in sorted(old_by_id):
        old = old_by_id[run_id]
        new = new_by_id[run_id]
        for column in DESIGN_COLUMNS:
            if old[column] != new[column]:
                mismatches.append(
                    {
                        "run_id": run_id,
                        "column": column,
                        "baseline": old[column],
                        "safety_v2": new[column],
                    }
                )
        requested = new["pressure_requested"]
        applied = new.get("pressure_applied", "")
        if new["stage"] != "profile":
            if requested or applied:
                raise RuntimeError(f"非 profile 行不得声明压力: {run_id}")
            continue
        expected = float(requested) * (0.25 if new["resource"] == "gpu_compute" else 1.0)
        if abs(float(applied) - expected) > 1e-10:
            raise RuntimeError(f"实际压力映射不合法: {run_id}")
        mapping_counts[f"{new['resource']}:{requested}->{applied}"] += 1
    if mismatches:
        raise RuntimeError(f"归一化实验字段发生变化: {mismatches[:3]}")

    new_protocols = {
        (
            float(row["warmup_s"]),
            float(row["duration_s"]),
            float(row["cooldown_s"]),
            float(row["max_gpu_temp_c"]),
        )
        for row in new_rows
    }
    if new_protocols != {(10.0, 30.0, 10.0, 80.0)}:
        raise RuntimeError(f"Safety-v2 协议不唯一: {sorted(new_protocols)}")
    if {row["run_directory"] for row in old_rows} & {
        row["run_directory"] for row in new_rows
    }:
        raise RuntimeError("Safety-v2 raw 目录与父计划重叠")
    manifest = _read_json(safety.with_name(f"{safety.stem}-manifest.json"))
    if (
        manifest.get("schema_version") != 2
        or manifest.get("root_dirty_at_generation") is not False
        or manifest.get("row_count") != 720
        or manifest.get("pressure_caps", {}).get("gpu_compute") != 0.25
    ):
        raise RuntimeError("Safety-v2 manifest 的 schema/clean/cap 合同不成立")

    return {
        "schema_version": 1,
        "status": "passed",
        "mode": "safety_v2_normalized_design_contract",
        "baseline_plan": baseline.relative_to(repo_root).as_posix(),
        "baseline_plan_sha256": baseline_verification["plan_sha256"],
        "safety_plan": safety.relative_to(repo_root).as_posix(),
        "safety_plan_sha256": safety_verification["plan_sha256"],
        "safety_plan_file_sha256": _sha256(safety),
        "generation_commit": manifest["root_commit"],
        "root_dirty_at_generation": False,
        "row_count": 720,
        "run_id_sets_equal": True,
        "design_columns_equal": True,
        "design_columns_checked": list(DESIGN_COLUMNS),
        "stage_counts": dict(sorted(Counter(row["stage"] for row in new_rows).items())),
        "profile_row_count": sum(row["stage"] == "profile" for row in new_rows),
        "protocol": {
            "warmup_s": 10.0,
            "duration_s": 30.0,
            "cooldown_s": 10.0,
            "max_gpu_temp_c": 80.0,
            "adaptive_cooldown_target_c": 70.0,
            "batch_start_gpu_temp_max_c": 50.0,
        },
        "pressure_caps": manifest["pressure_caps"],
        "pressure_mapping_cell_counts": dict(sorted(mapping_counts.items())),
        "raw_directories_disjoint": True,
        "legacy_plan_schema": 1,
        "safety_plan_schema": 2,
        "allowed_non_design_changes": [
            "schema_version",
            "pressure_applied",
            "warmup_s",
            "duration_s",
            "cooldown_s",
            "max_gpu_temp_c",
            "config_sha256",
            "root_commit",
            "run_directory",
            "row_sha256",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=Path("artifacts/plans/formal-v1.csv"))
    parser.add_argument(
        "--safety", type=Path, default=Path("artifacts/plans/formal-v1-safety-v2-s30.csv")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/plans/formal-v1-safety-v2-s30-contract.json"),
    )
    args = parser.parse_args()
    root = discover_repo_root(Path.cwd())
    baseline = (root / args.baseline).resolve() if not args.baseline.is_absolute() else args.baseline
    safety = (root / args.safety).resolve() if not args.safety.is_absolute() else args.safety
    output = (root / args.output).resolve() if not args.output.is_absolute() else args.output
    payload = build(root, baseline, safety)
    text = stable_json_dumps(payload, indent=2) + "\n"
    if output.exists():
        if output.read_text(encoding="utf-8") != text:
            raise FileExistsError(f"既有合同不同，拒绝覆盖: {output}")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")
    print(stable_json_dumps(payload, indent=2))


if __name__ == "__main__":
    main()
