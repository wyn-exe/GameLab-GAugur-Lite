"""单游戏子进程 launcher、心跳 watchdog 与 Step 3 汇总验收。"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..metrics.writer import write_json_atomic
from .registry import GAME_REGISTRY, GameDefinition, verify_upstream


def _inside_repo(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"输出目录必须位于仓库内且不能等于仓库根目录: {path}")
    return resolved


def _terminate_exact_child(process: subprocess.Popen[str]) -> str:
    process.terminate()
    try:
        process.wait(timeout=5)
        return "terminated"
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        return "killed"


def launch_smoke(
    *,
    repo_root: Path,
    game: GameDefinition,
    duration_s: float,
    max_frames: int,
    repeat: int,
    output_directory: Path,
    headless: bool,
    startup_timeout_s: float = 20.0,
    heartbeat_timeout_s: float = 10.0,
) -> dict[str, Any]:
    """只管理本次明确 PID；不用 taskkill，也不触碰其他 Python 进程。"""

    output = _inside_repo(repo_root, output_directory)
    if output.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖: {output_directory}")
    before = verify_upstream(repo_root)
    if before["status"] != "passed":
        raise RuntimeError("启动前上游完整性校验失败")
    output.mkdir(parents=True, exist_ok=False)

    run_id = f"step3-smoke-{game.id}-r{repeat:02d}"
    command = [
        sys.executable,
        "-m",
        "gaugur_lite",
        "workload",
        "_execute",
        "--workload-id",
        game.id,
        "--duration",
        str(duration_s),
        "--max-frames",
        str(max_frames),
        "--repeat",
        str(repeat),
        "--output-directory",
        str(output),
    ]
    if headless:
        command.append("--headless")

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started_wall_ns = time.time_ns()
    started = time.perf_counter()
    watchdog_reason: str | None = None
    termination: str | None = None
    ready_seen = False
    last_heartbeat_mtime: int | None = None
    last_heartbeat_seen = started

    with (output / "stdout.log").open("x", encoding="utf-8", newline="\n") as stdout, (
        output / "stderr.log"
    ).open("x", encoding="utf-8", newline="\n") as stderr:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            shell=False,
            creationflags=creationflags,
        )
        while process.poll() is None:
            now = time.perf_counter()
            ready_seen = ready_seen or (output / "ready.json").is_file()
            heartbeat = output / "heartbeat.json"
            if heartbeat.is_file():
                mtime = heartbeat.stat().st_mtime_ns
                if mtime != last_heartbeat_mtime:
                    last_heartbeat_mtime = mtime
                    last_heartbeat_seen = now
            if not ready_seen and now - started > startup_timeout_s:
                watchdog_reason = "startup_timeout"
            elif ready_seen and now - last_heartbeat_seen > heartbeat_timeout_s:
                watchdog_reason = "heartbeat_timeout"
            elif now - started > duration_s + startup_timeout_s + 15.0:
                watchdog_reason = "total_timeout"
            if watchdog_reason is not None:
                termination = _terminate_exact_child(process)
                break
            time.sleep(0.2)
        exit_code = int(process.wait())

    # 极短 run 可能在父进程第一次轮询前已完成，结束后以落盘文件为准复核 ready。
    ready_seen = ready_seen or (output / "ready.json").is_file()

    after = verify_upstream(repo_root)
    summary_path = output / "summary.json"
    summary = (
        json.loads(summary_path.read_text(encoding="utf-8"))
        if summary_path.is_file()
        else None
    )
    passed = (
        exit_code == 0
        and watchdog_reason is None
        and ready_seen
        and after["status"] == "passed"
        and summary is not None
        and summary.get("status") == "completed"
    )
    launcher = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "completed" if passed else "failed",
        "workload_id": game.id,
        "repeat": repeat,
        "headless": headless,
        "duration_requested_s": duration_s,
        "max_frames": max_frames,
        "pid": process.pid,
        "started_wall_time_ns": started_wall_ns,
        "finished_wall_time_ns": time.time_ns(),
        "launcher_elapsed_s": time.perf_counter() - started,
        "ready_seen": ready_seen,
        "exit_code": exit_code,
        "watchdog_reason": watchdog_reason,
        "termination": termination,
        "upstream_before": before["status"],
        "upstream_after": after["status"],
        "upstream_unchanged": before == after,
        "output_files": {
            "stdout": "stdout.log",
            "stderr": "stderr.log",
            "launcher": "launcher.json",
            "summary": "summary.json" if summary is not None else None,
        },
    }
    write_json_atomic(output / "launcher.json", launcher)
    return {"launcher": launcher, "summary": summary}


def _coefficient_of_variation(values: list[float]) -> float:
    mean = statistics.fmean(values)
    return statistics.stdev(values) / mean * 100 if len(values) >= 2 and mean else 0.0


def build_step3_acceptance(
    *,
    repo_root: Path,
    input_root: Path,
    expected_repeats: int,
) -> dict[str, Any]:
    root = _inside_repo(repo_root, input_root)
    game_results: list[dict[str, Any]] = []
    global_failures: list[str] = []

    for game in GAME_REGISTRY:
        summaries = sorted((root / game.id).glob("r*/summary.json"))
        runs: list[dict[str, Any]] = []
        for summary_path in summaries:
            run_dir = summary_path.parent
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            launcher = json.loads((run_dir / "launcher.json").read_text(encoding="utf-8"))
            status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            metric_lines = (run_dir / "game_metrics.jsonl").read_text(encoding="utf-8").splitlines()
            metrics = [json.loads(line) for line in metric_lines]
            windows_healthy = all(
                row["window"].get("found") is True
                and row["window"].get("visible") is True
                and row["window"].get("minimized") is False
                for row in metrics
            )
            run_passed = all(
                (
                    summary.get("status") == "completed",
                    summary.get("headless") is False,
                    summary.get("draw_count") == game.target_fps * 30,
                    summary.get("max_frames") == game.target_fps * 30,
                    launcher.get("status") == "completed",
                    launcher.get("upstream_unchanged") is True,
                    status.get("status") == "completed",
                    status.get("samples_written") == len(metrics),
                    summary.get("metric_rows") == len(metrics),
                    windows_healthy,
                )
            )
            runs.append(
                {
                    "run_directory": run_dir.relative_to(repo_root).as_posix(),
                    "passed": run_passed,
                    "elapsed_s": summary.get("elapsed_s"),
                    "draw_count": summary.get("draw_count"),
                    "metric_rows": len(metrics),
                    "mean_fps": summary.get("game_fps", {}).get("mean"),
                    "p05_fps": summary.get("game_fps", {}).get("p05"),
                    "missed_deadline_count": summary.get("missed_deadline_count"),
                    "controller_trace_sha256": summary.get("controller_trace_sha256"),
                    "windows_healthy": windows_healthy,
                }
            )

        means = [float(item["mean_fps"]) for item in runs if item["mean_fps"] is not None]
        traces = {item["controller_trace_sha256"] for item in runs}
        cv_pct = _coefficient_of_variation(means) if len(means) == expected_repeats else None
        checks = {
            "repeat_count": len(runs) == expected_repeats,
            "all_runs_passed": bool(runs) and all(item["passed"] for item in runs),
            "controller_trace_identical": len(traces) == 1,
            "fps_cv_below_5pct": cv_pct is not None and cv_pct < 5.0,
        }
        passed = all(checks.values())
        if not passed:
            global_failures.append(game.id)
        game_results.append(
            {
                "workload_id": game.id,
                "target_fps": game.target_fps,
                "expected_frames": game.target_fps * 30,
                "repeat_count": len(runs),
                "mean_fps_across_repeats": statistics.fmean(means) if means else None,
                "fps_cv_pct": cv_pct,
                "controller_trace_sha256": next(iter(traces)) if len(traces) == 1 else None,
                "checks": checks,
                "status": "passed" if passed else "failed",
                "runs": runs,
            }
        )

    upstream = verify_upstream(repo_root)
    passed = not global_failures and upstream["status"] == "passed"
    return {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "expected_game_count": 8,
        "actual_game_count": len(game_results),
        "expected_repeats_per_game": expected_repeats,
        "expected_total_runs": 8 * expected_repeats,
        "actual_total_runs": sum(item["repeat_count"] for item in game_results),
        "fps_cv_threshold_pct": 5.0,
        "upstream_status": upstream["status"],
        "failed_workloads": global_failures,
        "games": game_results,
    }
