from __future__ import annotations

import json
from pathlib import Path

from gaugur_lite.workloads import launcher
from gaugur_lite.workloads.registry import GAME_REGISTRY


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_step3_acceptance_requires_all_eight_three_times(
    tmp_path: Path, monkeypatch: object
) -> None:
    repo = tmp_path / "repo"
    root = repo / "artifacts" / "workloads" / "step3" / "formal"
    root.mkdir(parents=True)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        launcher, "verify_upstream", lambda _: {"status": "passed"}
    )
    metric = {
        "window": {"found": True, "visible": True, "minimized": False},
    }
    for game in GAME_REGISTRY:
        for repeat in range(1, 4):
            run = root / game.id / f"r{repeat:02d}"
            expected_frames = game.target_fps * 30
            _write_json(
                run / "summary.json",
                {
                    "status": "completed",
                    "headless": False,
                    "draw_count": expected_frames,
                    "max_frames": expected_frames,
                    "metric_rows": 1,
                    "elapsed_s": 30,
                    "game_fps": {"mean": float(game.target_fps), "p05": float(game.target_fps)},
                    "missed_deadline_count": 0,
                    "controller_trace_sha256": game.id,
                },
            )
            _write_json(run / "launcher.json", {"status": "completed", "upstream_unchanged": True})
            _write_json(run / "status.json", {"status": "completed", "samples_written": 1})
            (run / "game_metrics.jsonl").write_text(json.dumps(metric) + "\n", encoding="utf-8")

    result = launcher.build_step3_acceptance(
        repo_root=repo,
        input_root=root,
        expected_repeats=3,
    )

    assert result["status"] == "passed"
    assert result["actual_total_runs"] == 24
    assert all(game["fps_cv_pct"] == 0 for game in result["games"])
