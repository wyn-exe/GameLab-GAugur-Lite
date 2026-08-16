"""运行或 dry-run Safety-v2 校准分母的确定性追加确认。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gaugur_lite.benchmarks.calibration import (
    plan_calibration_confirmation,
    run_calibration_confirmation,
)
from gaugur_lite.config import discover_repo_root, stable_json_dumps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = discover_repo_root(Path.cwd())
    base = root / "artifacts/calibration/step7-safety-v2/formal-calibration-warmup-v1.json"
    output = root / "artifacts/calibration/step7-safety-v2/formal-calibration-confirmation-v1.json"
    metrics = root / "artifacts/calibration/step7-safety-v2/formal-calibration-confirmation-v1-metrics.jsonl"
    status = root / "artifacts/calibration/step7-safety-v2/formal-calibration-confirmation-v1-status.json"
    workers = root / "artifacts/calibration/step7-safety-v2/formal-calibration-confirmation-v1-workers"
    kwargs = {
        "repo_root": root,
        "calibration_file": base,
        "output_file": output,
        "metrics_file": metrics,
        "status_file": status,
        "workers_root": workers,
        "cv_threshold_pct": 5.0,
        "eligibility_ceiling_pct": 10.0,
        "additional_repeats": 2,
    }
    if args.dry_run:
        result = plan_calibration_confirmation(**kwargs)
    else:
        result = run_calibration_confirmation(
            config_path=root / "configs/local.safety-v2-s30.yaml",
            **kwargs,
        )
    print(stable_json_dumps(result, indent=2))
    return 0 if args.dry_run or result["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
