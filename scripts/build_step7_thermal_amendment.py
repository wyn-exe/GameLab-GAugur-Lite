"""构建/复核 Step 7 的 82°C -> 84°C 温控修订证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from gaugur_lite.config import discover_repo_root, stable_json_dumps
from gaugur_lite.profiles import _verify_thermal_profile_amendment
from gaugur_lite.runner.plan import load_plan_rows, verify_plan


TRIGGER_RUN_ID = "formal-v1__profile__pyxel_snake__gpu_compute__p100__r03"
TRIGGER_REASON = "RunInvalidError:gpu_temperature_exceeded:83.0>82.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def build_payload(root: Path) -> dict[str, Any]:
    original_plan = root / "artifacts/plans/formal-v1.csv"
    amended_plan = root / "artifacts/plans/formal-v1-profile-t84.csv"
    solo_runs = root / "data/interim/formal-v1/solo-runs.jsonl"
    device_query = root / "artifacts/profiles/step7/thermal-device-query.txt"
    original_verification = verify_plan(repo_root=root, plan_file=original_plan)
    amended_verification = verify_plan(repo_root=root, plan_file=amended_plan)
    if original_verification["status"] != "passed" or amended_verification["status"] != "passed":
        raise RuntimeError("温控修订前后的计划必须都通过 verify")
    compatibility = _verify_thermal_profile_amendment(
        repo_root=root,
        profile_plan_file=amended_plan,
        baseline_plan_file=original_plan,
        profile_plan_sha256=amended_verification["plan_sha256"],
        baseline_plan_sha256=original_verification["plan_sha256"],
    )

    pilot_valid = []
    pilot_invalid = []
    trigger_index: dict[str, Any] | None = None
    for row in load_plan_rows(original_plan):
        if row["stage"] != "profile":
            continue
        index_path = root / row["run_directory"] / "index.json"
        if not index_path.is_file():
            continue
        index = _json(index_path)
        for attempt in index.get("attempts", []):
            record = {"run_id": row["run_id"], **attempt}
            if attempt.get("status") == "completed" and attempt.get("valid") is True:
                pilot_valid.append(record)
            else:
                pilot_invalid.append(record)
        if row["run_id"] == TRIGGER_RUN_ID:
            trigger_index = index
    if len({item["run_id"] for item in pilot_valid}) != 23:
        raise RuntimeError("中止的 82°C pilot 必须恰有 23 个已完成单元")
    if trigger_index is None:
        raise RuntimeError("缺少触发温控修订的 run index")
    trigger_attempts = trigger_index.get("attempts", [])
    if len(trigger_attempts) != 4 or any(
        item.get("status") != "invalid"
        or item.get("valid") is not False
        or item.get("reason") != TRIGGER_REASON
        for item in trigger_attempts
    ):
        raise RuntimeError("触发单元必须保留四次同因 83°C>82°C 的 invalid attempt")

    solo_records = [
        json.loads(line)
        for line in solo_runs.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if len(solo_records) != 24:
        raise RuntimeError("必须保留 24 个 Step 6 solo run")
    solo_max_temp = max(float(item["gpu_temp_c_max"]) for item in solo_records)
    if solo_max_temp > 82:
        raise RuntimeError("solo baseline 自身超过原 82°C 门，禁止复用")

    query_text = device_query.read_text(encoding="utf-8-sig")
    target_match = re.search(r"GPU Target Temperature\s*:\s*(-?\d+)\s*C", query_text)
    target_temp = int(target_match.group(1)) if target_match else None
    if target_temp != 87:
        raise RuntimeError(f"设备目标温度证据应为 87°C，实际 {target_temp!r}")

    return {
        "schema_version": 1,
        "status": "passed",
        "amendment_id": "step7-thermal-profile-t84-v1",
        "scope": "profile_only",
        "decision": {
            "original_limit_c": 82,
            "amended_limit_c": 84,
            "abort_above_c": 84,
            "device_target_temperature_c": target_temp,
            "margin_below_device_target_c": target_temp - 84,
            "cooldown_target_c": 74,
            "reuse_solo_baselines": True,
            "reuse_original_profile_attempts": False,
            "reason": "四次独立冷启动均在同一 gpu_compute/p100 单元以 83°C>82°C 终止；继续重试不能解决协议冲突。",
        },
        "plans": {
            "original": {
                "path": _rel(root, original_plan),
                "sha256": original_verification["plan_sha256"],
            },
            "amended_profile": {
                "path": _rel(root, amended_plan),
                "sha256": amended_verification["plan_sha256"],
            },
            "compatibility": compatibility,
        },
        "pilot_82c": {
            "completed_run_count": len({item["run_id"] for item in pilot_valid}),
            "valid_attempt_count": len(pilot_valid),
            "invalid_attempt_count": len(pilot_invalid),
            "included_in_final_profiles": False,
            "trigger_run_id": TRIGGER_RUN_ID,
            "trigger_attempt_count": len(trigger_attempts),
            "trigger_attempts": trigger_attempts,
        },
        "solo_baseline_reuse": {
            "run_count": len(solo_records),
            "max_observed_gpu_temp_c": solo_max_temp,
            "original_limit_c": 82,
            "temperature_headroom_c": 82 - solo_max_temp,
            "solo_runs": _rel(root, solo_runs),
            "solo_runs_sha256": _sha256(solo_runs),
        },
        "device_temperature_query": {
            "path": _rel(root, device_query),
            "sha256": _sha256(device_query),
            "note": "笔记本驱动的部分 T.Limit 字段异常；仅使用可解析的 GPU Target Temperature。",
        },
        "checks": {
            "four_same_reason_trigger_attempts": True,
            "original_profile_attempts_excluded": True,
            "amended_raw_directories_disjoint": compatibility["raw_directories_disjoint"],
            "semantic_profile_fields_equal": compatibility["semantic_fields_equal"],
            "solo_baselines_below_original_limit": solo_max_temp <= 82,
            "amended_limit_below_device_target": 84 < target_temp,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = discover_repo_root(Path.cwd())
    output = args.output.resolve()
    if root.resolve() not in output.parents:
        raise ValueError("输出必须位于仓库内")
    payload = build_payload(root)
    rendered = stable_json_dumps(payload, indent=2) + "\n"
    if output.exists():
        if output.read_text(encoding="utf-8") != rendered:
            raise RuntimeError("既有温控修订证据与当前原始数据重算结果不一致")
        mode = "verified"
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
        mode = "created"
    print(
        stable_json_dumps(
            {
                "status": "passed",
                "mode": mode,
                "output": _rel(root, output),
                "amended_plan_sha256": payload["plans"]["amended_profile"]["sha256"],
                "pilot_completed": payload["pilot_82c"]["completed_run_count"],
                "trigger_attempts": payload["pilot_82c"]["trigger_attempt_count"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
