"""Windows 实验 Runner：精确子进程、barrier、遥测、窗口检查与安全 resume。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, IO

import psutil

from ..config import config_sha256, stable_json_dumps
from ..metrics.system_sampler import SystemSampler
from ..metrics.writer import JsonlWriter, write_json_atomic
from ..workloads.registry import get_game
from ..workloads.window_probe import capture_window
from .plan import load_plan_rows, verify_plan
from .window_layout import arrange_windows_grid, rectangles_overlap, wait_for_windows


class RunInvalidError(RuntimeError):
    """实验完成不了质量门，但不表示 Runner 自身损坏。"""


@dataclass(frozen=True)
class ParsedPlanRow:
    raw: dict[str, str]
    execution_index: int
    run_id: str
    experiment_id: str
    stage: str
    split: str
    mode: str
    workload_ids: tuple[str, ...]
    resource: str | None
    pressure_requested: float | None
    repeat: int
    warmup_s: float
    duration_s: float
    sample_interval_s: float
    cooldown_s: float
    gpu_index: int
    display_index: int
    window_layout: str
    require_visible_windows: bool
    max_gpu_temp_c: float
    config_sha256: str
    run_directory: str
    row_sha256: str

    @classmethod
    def from_csv(cls, row: dict[str, str]) -> "ParsedPlanRow":
        workloads = tuple(json.loads(row["workload_ids"]))
        if not workloads or len(set(workloads)) != len(workloads):
            raise ValueError("plan workload_ids 必须非空且唯一")
        pressure = float(row["pressure_requested"]) if row["pressure_requested"] else None
        return cls(
            raw=dict(row),
            execution_index=int(row["execution_index"]),
            run_id=row["run_id"],
            experiment_id=row["experiment_id"],
            stage=row["stage"],
            split=row.get("split", "not_applicable"),
            mode=row["mode"],
            workload_ids=workloads,
            resource=row["resource"] or None,
            pressure_requested=pressure,
            repeat=int(row["repeat"]),
            warmup_s=float(row["warmup_s"]),
            duration_s=float(row["duration_s"]),
            sample_interval_s=float(row["sample_interval_s"]),
            cooldown_s=float(row["cooldown_s"]),
            gpu_index=int(row["gpu_index"]),
            display_index=int(row["display_index"]),
            window_layout=row["window_layout"],
            require_visible_windows=row["require_visible_windows"].lower() == "true",
            max_gpu_temp_c=float(row["max_gpu_temp_c"]),
            config_sha256=row["config_sha256"],
            run_directory=row["run_directory"],
            row_sha256=row["row_sha256"],
        )


@dataclass
class ManagedChild:
    name: str
    kind: str
    process: subprocess.Popen[str]
    command: tuple[str, ...]
    create_time: float
    output_directory: Path
    stdout: IO[str]
    stderr: IO[str]

    def close_logs(self) -> None:
        if not self.stdout.closed:
            self.stdout.close()
        if not self.stderr.closed:
            self.stderr.close()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside_repo(repo_root: Path, relative: str | Path) -> Path:
    root = repo_root.resolve()
    candidate = (root / relative).resolve() if not Path(relative).is_absolute() else Path(relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"Runner 路径必须位于仓库内且不能等于根目录: {relative}")
    return candidate


def _safe_error(exc: BaseException, repo_root: Path) -> str:
    return str(exc).replace(str(repo_root), "<repo>")[:1000]


def _phase_event(phase: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": phase,
        "wall_time_ns": time.time_ns(),
        "monotonic_time_ns": time.perf_counter_ns(),
        "detail": detail or {},
    }


def _write_status(
    path: Path,
    *,
    row: ParsedPlanRow,
    attempt: int,
    phase: str,
    status: str,
    started_wall_time_ns: int,
    children: list[ManagedChild],
    valid: bool | None = None,
    reason: str | None = None,
) -> None:
    write_json_atomic(
        path,
        {
            "schema_version": 1,
            "run_id": row.run_id,
            "attempt": attempt,
            "status": status,
            "phase": phase,
            "valid": valid,
            "reason": reason,
            "started_wall_time_ns": started_wall_time_ns,
            "updated_wall_time_ns": time.time_ns(),
            "child_pids": [child.process.pid for child in children],
        },
    )


def _load_index(run_root: Path, row: ParsedPlanRow) -> dict[str, Any]:
    path = run_root / "index.json"
    if not path.is_file():
        return {
            "schema_version": 1,
            "run_id": row.run_id,
            "config_sha256": row.config_sha256,
            "row_sha256": row.row_sha256,
            "attempts": [],
        }
    index = json.loads(path.read_text(encoding="utf-8"))
    if index.get("run_id") != row.run_id:
        raise RuntimeError("已有 index.json 的 run_id 不匹配")
    if index.get("config_sha256") != row.config_sha256 or index.get("row_sha256") != row.row_sha256:
        raise RuntimeError("已有 run 目录与当前配置/row hash 不一致，拒绝复用")
    if not isinstance(index.get("attempts"), list):
        raise RuntimeError("已有 index.json attempts 非法")
    return index


def _validate_completed_attempt(
    *, repo_root: Path, run_root: Path, row: ParsedPlanRow, entry: dict[str, Any]
) -> tuple[bool, str]:
    if entry.get("status") != "completed" or entry.get("valid") is not True:
        return False, "attempt_not_completed_valid"
    relative = entry.get("directory")
    if not isinstance(relative, str):
        return False, "attempt_directory_missing"
    attempt_dir = _inside_repo(repo_root, relative)
    if attempt_dir.parent.parent != run_root.resolve():
        return False, "attempt_directory_mismatch"
    status_path = attempt_dir / "status.json"
    summary_path = attempt_dir / "summary.json"
    manifest_path = attempt_dir / "manifest.json"
    if not all(path.is_file() for path in (status_path, summary_path, manifest_path)):
        return False, "required_file_missing"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if status.get("status") != "completed" or status.get("valid") is not True:
        return False, "status_not_completed_valid"
    if summary.get("status") != "completed" or summary.get("valid") is not True:
        return False, "summary_not_completed_valid"
    if manifest.get("row_sha256") != row.row_sha256:
        return False, "manifest_row_hash_mismatch"
    for relative_path, expected_hash in summary.get("artifact_sha256", {}).items():
        artifact = attempt_dir / relative_path
        if not artifact.is_file() or _file_sha256(artifact) != expected_hash:
            return False, f"artifact_hash_mismatch:{relative_path}"
    if float(summary.get("system_coverage_ratio", 0.0)) < 0.95:
        return False, "system_coverage_below_0_95"
    if float(summary.get("workload_overlap_ratio", 0.0)) < 0.95:
        return False, "workload_overlap_below_0_95"
    return True, "completed_valid"


def inspect_resume(*, repo_root: Path, row: ParsedPlanRow) -> dict[str, Any]:
    run_root = _inside_repo(repo_root, row.run_directory)
    if not run_root.exists():
        return {"action": "run", "reason": "run_directory_absent", "attempt": 1}
    index = _load_index(run_root, row)
    for entry in reversed(index["attempts"]):
        valid, reason = _validate_completed_attempt(
            repo_root=repo_root, run_root=run_root, row=row, entry=entry
        )
        if valid:
            return {
                "action": "skip",
                "reason": reason,
                "attempt": int(entry["attempt"]),
                "directory": entry["directory"],
            }
    next_attempt = max((int(item.get("attempt", 0)) for item in index["attempts"]), default=0) + 1
    return {"action": "run", "reason": "no_valid_completed_attempt", "attempt": next_attempt}


def _spawn_child(
    *,
    repo_root: Path,
    run_id: str,
    name: str,
    kind: str,
    command: list[str],
    output_directory: Path,
) -> ManagedChild:
    if run_id not in " ".join(command):
        raise ValueError("受管子进程命令必须携带当前 run_id")
    stdout = (output_directory / "stdout.log").open("x", encoding="utf-8", newline="\n")
    stderr = (output_directory / "stderr.log").open("x", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            text=True,
            shell=False,
            creationflags=flags,
        )
        create_time = psutil.Process(process.pid).create_time()
        return ManagedChild(
            name=name,
            kind=kind,
            process=process,
            command=tuple(command),
            create_time=create_time,
            output_directory=output_directory,
            stdout=stdout,
            stderr=stderr,
        )
    except BaseException:
        stdout.close()
        stderr.close()
        raise


def _terminate_owned_children(children: list[ManagedChild]) -> list[dict[str, Any]]:
    """仅终止 PID+create_time 已核对的本次 Popen 树；不按进程名全局清理。"""

    actions: list[dict[str, Any]] = []
    verified: list[psutil.Process] = []
    for child in children:
        if child.process.poll() is not None:
            actions.append({"name": child.name, "pid": child.process.pid, "action": "already_exited"})
            continue
        try:
            root = psutil.Process(child.process.pid)
            if abs(root.create_time() - child.create_time) > 0.01:
                actions.append({"name": child.name, "pid": child.process.pid, "action": "pid_reused_not_touched"})
                continue
            descendants = root.children(recursive=True)
            verified.extend(descendants)
            verified.append(root)
            actions.append(
                {
                    "name": child.name,
                    "pid": child.process.pid,
                    "action": "terminate_verified_tree",
                    "descendant_pids": [item.pid for item in descendants],
                }
            )
        except psutil.NoSuchProcess:
            actions.append({"name": child.name, "pid": child.process.pid, "action": "already_exited"})
    unique = {process.pid: process for process in verified}
    for process in reversed(list(unique.values())):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(unique.values()), timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)
    return actions


def _wait_ready(children: list[ManagedChild], *, timeout_s: float) -> dict[str, dict[str, Any]]:
    deadline = time.perf_counter() + timeout_s
    ready: dict[str, dict[str, Any]] = {}
    while time.perf_counter() < deadline:
        for child in children:
            if child.name in ready:
                continue
            if child.process.poll() is not None:
                raise RuntimeError(
                    f"{child.name} 在 ready 前退出，exit_code={child.process.returncode}"
                )
            path = child.output_directory / "ready.json"
            if path.is_file():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("status") != "ready" or int(payload.get("pid", 0)) != child.process.pid:
                    raise RuntimeError(f"{child.name} ready.json 与受管 PID 不一致")
                ready[child.name] = payload
        if len(ready) == len(children):
            return ready
        time.sleep(0.05)
    missing = sorted(child.name for child in children if child.name not in ready)
    raise TimeoutError(f"ready barrier 超时: {', '.join(missing)}")


def _check_heartbeats(children: list[ManagedChild], *, timeout_s: float) -> None:
    now_ns = time.time_ns()
    for child in children:
        if child.kind != "workload":
            continue
        heartbeat = child.output_directory / "heartbeat.json"
        if not heartbeat.is_file():
            raise RunInvalidError(f"{child.name} heartbeat 缺失")
        age_s = (now_ns - heartbeat.stat().st_mtime_ns) / 1_000_000_000
        if age_s > timeout_s:
            raise RunInvalidError(f"{child.name} heartbeat 超时 {age_s:.2f}s")


def _check_windows(
    titles: tuple[str, ...], expected_pids: dict[str, int]
) -> dict[str, Any]:
    observations = [capture_window(title) for title in titles]
    overlaps = []
    for first in range(len(observations)):
        for second in range(first + 1, len(observations)):
            if rectangles_overlap(observations[first], observations[second]):
                overlaps.append([titles[first], titles[second]])
    healthy = all(
        item.get("found") is True
        and int(item.get("process_pid") or 0) == int(expected_pids[title])
        and item.get("visible") is True
        and item.get("minimized") is False
        for title, item in zip(titles, observations, strict=True)
    ) and not overlaps
    return {"healthy": healthy, "observations": observations, "overlaps": overlaps}


def _sample_temperature(sampler: SystemSampler, sequence: int, limit_c: float) -> Any:
    event = sampler.sample(sequence)
    temperature = event.gpu_temp_c
    if temperature is not None and temperature > limit_c:
        raise RunInvalidError(f"gpu_temperature_exceeded:{temperature:.1f}>{limit_c:.1f}")
    return event


def _wait_until(
    *,
    deadline: float,
    children: list[ManagedChild],
    sampler: SystemSampler,
    temperature_limit_c: float,
    heartbeat_timeout_s: float,
) -> None:
    sequence = 0
    next_check = time.perf_counter()
    while time.perf_counter() < deadline:
        for child in children:
            if child.process.poll() is not None:
                raise RunInvalidError(f"{child.name} 在统一测量窗口前退出")
        now = time.perf_counter()
        if now >= next_check:
            _check_heartbeats(children, timeout_s=heartbeat_timeout_s)
            _sample_temperature(sampler, sequence, temperature_limit_c)
            sequence += 1
            next_check += 0.5
        time.sleep(min(0.05, max(0.0, deadline - time.perf_counter())))


def _cooldown(*, row: ParsedPlanRow, run_id: str, output: Path) -> dict[str, Any]:
    started = time.perf_counter()
    rows = []
    if row.cooldown_s <= 0:
        return {"elapsed_s": 0.0, "samples": 0, "gpu_temp_c_last": None}
    with JsonlWriter(output / "cooldown.jsonl", batch_size=1) as writer, SystemSampler(
        run_id=run_id,
        gpu_index=row.gpu_index,
        process_pid=os.getpid(),
    ) as sampler:
        sequence = 0
        while True:
            event = sampler.sample(sequence)
            writer.write(event)
            rows.append(event)
            sequence += 1
            remaining = row.cooldown_s - (time.perf_counter() - started)
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
    return {
        "elapsed_s": time.perf_counter() - started,
        "samples": len(rows),
        "gpu_temp_c_last": rows[-1].gpu_temp_c if rows else None,
    }


def _artifact_hashes(attempt_dir: Path, workload_ids: tuple[str, ...], profile: bool) -> dict[str, str]:
    relative_paths = [
        "manifest.json",
        "barrier.json",
        "system_metrics.jsonl",
        "window_observations.jsonl",
        "layout.json",
        "cooldown.jsonl",
    ]
    for workload_id in workload_ids:
        relative_paths.extend(
            [
                f"workloads/{workload_id}/game_metrics.jsonl",
                f"workloads/{workload_id}/ready.json",
                f"workloads/{workload_id}/heartbeat.json",
                f"workloads/{workload_id}/measurement-start.json",
                f"workloads/{workload_id}/stop.json",
                f"workloads/{workload_id}/summary.json",
                f"workloads/{workload_id}/status.json",
                f"workloads/{workload_id}/stdout.log",
                f"workloads/{workload_id}/stderr.log",
            ]
        )
    if profile:
        relative_paths.extend(
            [
                "benchmark/ready.json",
                "benchmark/status.json",
                "benchmark/stdout.log",
                "benchmark/stderr.log",
            ]
        )
    return {
        relative: _file_sha256(attempt_dir / relative)
        for relative in relative_paths
        if (attempt_dir / relative).is_file()
    }


def run_one(
    *,
    repo_root: Path,
    row: ParsedPlanRow,
    plan_sha256: str,
    resume: bool,
    headless: bool = False,
    startup_timeout_s: float = 30.0,
) -> dict[str, Any]:
    run_root = _inside_repo(repo_root, row.run_directory)
    if run_root.exists() and not resume:
        raise FileExistsError(f"run 目录已存在；使用 --resume 安全复核: {run_root}")
    run_root.mkdir(parents=True, exist_ok=True)
    index = _load_index(run_root, row)
    decision = inspect_resume(repo_root=repo_root, row=row)
    if resume and decision["action"] == "skip":
        return {"run_id": row.run_id, "status": "skipped", **decision}
    attempt_number = int(decision["attempt"])
    attempts_root = run_root / "attempts"
    attempts_root.mkdir(exist_ok=True)
    attempt_dir = attempts_root / f"a{attempt_number:03d}"
    attempt_dir.mkdir(exist_ok=False)
    relative_attempt = attempt_dir.relative_to(repo_root).as_posix()
    index["attempts"].append(
        {
            "attempt": attempt_number,
            "directory": relative_attempt,
            "status": "running",
            "valid": None,
            "started_wall_time_ns": time.time_ns(),
        }
    )
    write_json_atomic(run_root / "index.json", index)

    started_wall_ns = time.time_ns()
    status_path = attempt_dir / "status.json"
    barrier_path = attempt_dir / "barrier.json"
    children: list[ManagedChild] = []
    lifecycle: JsonlWriter | None = None
    final_status = "failed"
    final_valid = False
    final_reason: str | None = None
    summary: dict[str, Any] | None = None
    cleanup_actions: list[dict[str, Any]] = []
    try:
        write_json_atomic(
            attempt_dir / "manifest.json",
            {
                "schema_version": 1,
                "run_id": row.run_id,
                "attempt": attempt_number,
                "plan_sha256": plan_sha256,
                "config_sha256": row.config_sha256,
                "row_sha256": row.row_sha256,
                "plan_row": row.raw,
            },
        )
        lifecycle = JsonlWriter(attempt_dir / "lifecycle.jsonl", batch_size=1)
        lifecycle.__enter__()

        def phase(name: str, detail: dict[str, Any] | None = None) -> None:
            assert lifecycle is not None
            lifecycle.write(_phase_event(name, detail=detail))
            _write_status(
                status_path,
                row=row,
                attempt=attempt_number,
                phase=name,
                status="running",
                started_wall_time_ns=started_wall_ns,
                children=children,
            )

        phase("PREPARING")
        workloads_root = attempt_dir / "workloads"
        workloads_root.mkdir()
        for workload_id in row.workload_ids:
            child_dir = workloads_root / workload_id
            child_dir.mkdir()
            command = [
                sys.executable,
                "-m",
                "gaugur_lite",
                "workload",
                "_execute",
                "--workload-id",
                workload_id,
                "--run-id",
                row.run_id,
                "--duration",
                str(row.duration_s),
                "--warmup",
                str(row.warmup_s),
                "--max-frames",
                "0",
                "--repeat",
                str(row.repeat),
                "--metric-window",
                str(row.sample_interval_s),
                "--barrier-file",
                str(barrier_path),
                "--barrier-timeout",
                str(startup_timeout_s + row.warmup_s),
                "--output-directory",
                str(child_dir),
            ]
            if headless:
                command.append("--headless")
            children.append(
                _spawn_child(
                    repo_root=repo_root,
                    run_id=row.run_id,
                    name=workload_id,
                    kind="workload",
                    command=command,
                    output_directory=child_dir,
                )
            )
        if row.mode == "pressure_profile":
            if row.resource is None or row.pressure_requested is None:
                raise ValueError("pressure_profile 计划缺少 resource/pressure")
            benchmark_dir = attempt_dir / "benchmark"
            benchmark_dir.mkdir()
            command = [
                sys.executable,
                "-m",
                "gaugur_lite",
                "benchmark",
                "_worker",
                "--resource",
                row.resource,
                "--pressure",
                str(row.pressure_requested),
                "--runtime-s",
                str(row.duration_s),
                "--warmup-s",
                str(row.warmup_s),
                "--barrier-file",
                str(barrier_path),
                "--barrier-timeout-s",
                str(startup_timeout_s + row.warmup_s),
                "--gpu-index",
                str(row.gpu_index),
                "--cpu-workers",
                "8",
                "--memory-buffer-mib",
                "64",
                "--gpu-matrix-size",
                "1024",
                "--gpu-memory-max-mib",
                "1024",
                "--ready-file",
                str(benchmark_dir / "ready.json"),
                "--status-file",
                str(benchmark_dir / "status.json"),
            ]
            children.append(
                _spawn_child(
                    repo_root=repo_root,
                    run_id=row.run_id,
                    name=f"benchmark:{row.resource}",
                    kind="benchmark",
                    command=command,
                    output_directory=benchmark_dir,
                )
            )

        phase("STARTING", {"child_pids": [child.process.pid for child in children]})
        ready = _wait_ready(children, timeout_s=startup_timeout_s)
        phase("READY", {"ready_names": sorted(ready)})
        titles = tuple(str(ready[item]["title"]) for item in row.workload_ids)
        expected_pids = {
            title: int(ready[workload_id]["pid"])
            for workload_id, title in zip(row.workload_ids, titles, strict=True)
        }
        if row.require_visible_windows and headless:
            raise ValueError("计划要求可见窗口，正式 Runner 不允许 headless")
        if not headless:
            # 此处仅复核句柄和 PID，不发送可能等待 GUI 线程处理的窗口消息。
            wait_for_windows(
                titles=titles,
                expected_pids=expected_pids,
                timeout_s=min(2.0, startup_timeout_s),
            )

        now_ns = time.perf_counter_ns()
        measurement_start_ns = now_ns + int(row.warmup_s * 1_000_000_000)
        measurement_end_ns = measurement_start_ns + int(row.duration_s * 1_000_000_000)
        write_json_atomic(
            barrier_path,
            {
                "schema_version": 1,
                "status": "released",
                "run_id": row.run_id,
                "released_wall_time_ns": time.time_ns(),
                "released_monotonic_time_ns": now_ns,
                "measurement_start_monotonic_ns": measurement_start_ns,
                "measurement_end_monotonic_ns": measurement_end_ns,
            },
        )

        system_rows: list[Any] = []
        window_rows: list[dict[str, Any]] = []
        phase("WARMUP")
        if not headless:
            # barrier 已释放，Pyxel 正在 warmup 中泵送窗口消息；异步排布不会互相死等。
            layout = arrange_windows_grid(
                titles=titles,
                expected_pids=expected_pids,
                display_index=row.display_index,
                layout=row.window_layout,
                timeout_s=min(1.0, max(0.1, row.warmup_s * 0.5)),
            )
            if layout["status"] != "passed":
                raise RunInvalidError("window_layout_failed")
            if time.perf_counter_ns() >= measurement_start_ns:
                raise RunInvalidError("window_layout_exceeded_warmup")
        else:
            layout = {
                "schema_version": 1,
                "status": "skipped",
                "headless": True,
                "external_occlusion_checked": False,
            }
        write_json_atomic(attempt_dir / "layout.json", layout)

        with SystemSampler(
            run_id=row.run_id,
            gpu_index=row.gpu_index,
            process_pid=os.getpid(),
        ) as sampler:
            _wait_until(
                deadline=measurement_start_ns / 1_000_000_000,
                children=children,
                sampler=sampler,
                temperature_limit_c=row.max_gpu_temp_c,
                heartbeat_timeout_s=max(3.0, row.sample_interval_s * 3),
            )
            phase("MEASURING")
            with JsonlWriter(attempt_dir / "system_metrics.jsonl", batch_size=5) as system_writer, JsonlWriter(
                attempt_dir / "window_observations.jsonl", batch_size=1
            ) as window_writer:
                next_sample_ns = measurement_start_ns
                sequence = 0
                while next_sample_ns <= measurement_end_ns:
                    remaining_s = (next_sample_ns - time.perf_counter_ns()) / 1_000_000_000
                    if remaining_s > 0:
                        time.sleep(remaining_s)
                    if time.perf_counter_ns() < measurement_end_ns:
                        for child in children:
                            if child.process.poll() is not None:
                                raise RunInvalidError(
                                    f"{child.name} 在正式测量结束前退出"
                                )
                    _check_heartbeats(
                        children, timeout_s=max(3.0, row.sample_interval_s * 3)
                    )
                    event = _sample_temperature(
                        sampler, sequence, row.max_gpu_temp_c
                    )
                    system_writer.write(event)
                    system_rows.append(event)
                    if not headless:
                        observation = _check_windows(titles, expected_pids)
                        observation.update(
                            {
                                "schema_version": 1,
                                "run_id": row.run_id,
                                "sequence": sequence,
                                "wall_time_ns": time.time_ns(),
                                "monotonic_time_ns": time.perf_counter_ns(),
                            }
                        )
                        window_writer.write(observation)
                        window_rows.append(observation)
                        if not observation["healthy"]:
                            raise RunInvalidError("window_health_failed")
                    sequence += 1
                    next_sample_ns = measurement_start_ns + int(
                        sequence * row.sample_interval_s * 1_000_000_000
                    )

        phase("STOPPING")
        early_or_failed = []
        for child in children:
            try:
                exit_code = child.process.wait(timeout=max(5.0, row.sample_interval_s * 3))
            except subprocess.TimeoutExpired:
                early_or_failed.append(f"{child.name}:did_not_exit")
                continue
            if exit_code != 0:
                early_or_failed.append(f"{child.name}:exit_{exit_code}")
        if early_or_failed:
            raise RunInvalidError("child_exit_failed:" + ",".join(early_or_failed))

        workload_summaries = []
        starts = []
        ends = []
        for workload_id in row.workload_ids:
            child_dir = workloads_root / workload_id
            status = json.loads((child_dir / "status.json").read_text(encoding="utf-8"))
            workload_summary = json.loads(
                (child_dir / "summary.json").read_text(encoding="utf-8")
            )
            if status.get("status") != "completed" or workload_summary.get("status") != "completed":
                raise RunInvalidError(f"{workload_id} summary/status 未通过")
            if workload_summary.get("barrier_used") is not True:
                raise RunInvalidError(f"{workload_id} 未使用统一 barrier")
            if float(workload_summary.get("measurement_coverage_ratio", 0.0)) < 0.95:
                raise RunInvalidError(f"{workload_id} 测量覆盖率低于 0.95")
            starts.append(int(workload_summary["measurement_started_monotonic_ns"]))
            ends.append(int(workload_summary["measurement_finished_monotonic_ns"]))
            workload_summaries.append(workload_summary)
        overlap_s = max(0.0, (min(ends) - max(starts)) / 1_000_000_000)
        overlap_ratio = min(1.0, overlap_s / row.duration_s)
        if overlap_ratio < 0.95:
            raise RunInvalidError(f"workload_overlap_below_0_95:{overlap_ratio:.4f}")

        if len(system_rows) < 2:
            raise RunInvalidError("system_metrics 样本不足")
        system_coverage_s = (
            system_rows[-1].monotonic_time_ns - system_rows[0].monotonic_time_ns
        ) / 1_000_000_000
        system_coverage_ratio = min(1.0, system_coverage_s / row.duration_s)
        if system_coverage_ratio < 0.95:
            raise RunInvalidError(f"system_coverage_below_0_95:{system_coverage_ratio:.4f}")
        gpu_temperatures = [
            float(event.gpu_temp_c) for event in system_rows if event.gpu_temp_c is not None
        ]
        benchmark_summary = None
        if row.mode == "pressure_profile":
            benchmark_summary = json.loads(
                (attempt_dir / "benchmark" / "status.json").read_text(encoding="utf-8")
            )
            if benchmark_summary.get("status") != "completed" or benchmark_summary.get("barrier_used") is not True:
                raise RunInvalidError("benchmark status/barrier 未通过")

        phase("COOLDOWN")
        cooldown = _cooldown(row=row, run_id=row.run_id, output=attempt_dir)
        artifact_sha256 = _artifact_hashes(
            attempt_dir,
            row.workload_ids,
            profile=row.mode == "pressure_profile",
        )
        summary = {
            "schema_version": 1,
            "run_id": row.run_id,
            "attempt": attempt_number,
            "status": "completed",
            "valid": True,
            "mode": row.mode,
            "stage": row.stage,
            "split": row.split,
            "workload_ids": list(row.workload_ids),
            "resource": row.resource,
            "pressure_requested": row.pressure_requested,
            "measurement_start_monotonic_ns": measurement_start_ns,
            "measurement_end_monotonic_ns": measurement_end_ns,
            "system_sample_count": len(system_rows),
            "system_coverage_s": system_coverage_s,
            "system_coverage_ratio": system_coverage_ratio,
            "workload_overlap_s": overlap_s,
            "workload_overlap_ratio": overlap_ratio,
            "gpu_temp_c_max": max(gpu_temperatures) if gpu_temperatures else None,
            "gpu_temp_limit_c": row.max_gpu_temp_c,
            "window_sample_count": len(window_rows),
            "windows_pairwise_nonoverlap": all(item["healthy"] for item in window_rows),
            "external_occlusion_checked": False,
            "workloads": workload_summaries,
            "benchmark": benchmark_summary,
            "cooldown": cooldown,
            "cleanup": {"global_kill_used": False, "actions": []},
            "artifact_sha256": artifact_sha256,
        }
        write_json_atomic(attempt_dir / "summary.json", summary)
        final_status = "completed"
        final_valid = True
        phase("COMPLETED")
        _write_status(
            status_path,
            row=row,
            attempt=attempt_number,
            phase="COMPLETED",
            status="completed",
            started_wall_time_ns=started_wall_ns,
            children=children,
            valid=True,
        )
        return {
            "run_id": row.run_id,
            "status": "completed",
            "valid": True,
            "attempt": attempt_number,
            "directory": relative_attempt,
            "summary": summary,
        }
    except BaseException as exc:
        final_status = "invalid" if isinstance(exc, RunInvalidError) else "failed"
        final_valid = False
        final_reason = f"{type(exc).__name__}:{_safe_error(exc, repo_root)}"
        cleanup_actions = _terminate_owned_children(children)
        failure_summary = {
            "schema_version": 1,
            "run_id": row.run_id,
            "attempt": attempt_number,
            "status": final_status,
            "valid": False,
            "reason": final_reason,
            "cleanup": {"global_kill_used": False, "actions": cleanup_actions},
        }
        write_json_atomic(attempt_dir / "failure.json", failure_summary)
        _write_status(
            status_path,
            row=row,
            attempt=attempt_number,
            phase=final_status.upper(),
            status=final_status,
            started_wall_time_ns=started_wall_ns,
            children=children,
            valid=False,
            reason=final_reason,
        )
        return {
            "run_id": row.run_id,
            "status": final_status,
            "valid": False,
            "attempt": attempt_number,
            "directory": relative_attempt,
            "reason": final_reason,
        }
    finally:
        if any(child.process.poll() is None for child in children):
            cleanup_actions.extend(_terminate_owned_children(children))
        for child in children:
            child.close_logs()
        if lifecycle is not None:
            lifecycle.__exit__(None, None, None)
        index = _load_index(run_root, row)
        for entry in index["attempts"]:
            if int(entry.get("attempt", 0)) == attempt_number:
                entry.update(
                    {
                        "status": final_status,
                        "valid": final_valid,
                        "reason": final_reason,
                        "finished_wall_time_ns": time.time_ns(),
                    }
                )
                break
        write_json_atomic(run_root / "index.json", index)


def run_plan(
    *,
    repo_root: Path,
    plan_file: Path,
    resume: bool,
    max_runs: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    verification = verify_plan(repo_root=repo_root, plan_file=plan_file)
    if verification["status"] != "passed":
        raise ValueError("计划 verify 未通过，拒绝执行")
    rows = [ParsedPlanRow.from_csv(item) for item in load_plan_rows(plan_file)]
    if max_runs is not None:
        if max_runs < 1:
            raise ValueError("max_runs 必须 >= 1")
        rows = rows[:max_runs]
    decisions = [inspect_resume(repo_root=repo_root, row=row) for row in rows]
    if dry_run:
        return {
            "schema_version": 1,
            "status": "planned",
            "dry_run": True,
            "plan_sha256": verification["plan_sha256"],
            "selected_runs": len(rows),
            "would_run": sum(item["action"] == "run" for item in decisions),
            "would_skip": sum(item["action"] == "skip" for item in decisions),
            "decisions": [
                {"run_id": row.run_id, **decision}
                for row, decision in zip(rows, decisions, strict=True)
            ],
        }
    started = time.perf_counter()
    results = []
    for row in rows:
        results.append(
            run_one(
                repo_root=repo_root,
                row=row,
                plan_sha256=verification["plan_sha256"],
                resume=resume,
            )
        )
    completed = sum(item["status"] == "completed" for item in results)
    skipped = sum(item["status"] == "skipped" for item in results)
    failed = len(results) - completed - skipped
    return {
        "schema_version": 1,
        "status": "passed" if failed == 0 else "failed",
        "plan": plan_file.resolve().relative_to(repo_root.resolve()).as_posix(),
        "plan_sha256": verification["plan_sha256"],
        "selected_runs": len(rows),
        "completed": completed,
        "skipped": skipped,
        "failed_or_invalid": failed,
        "elapsed_s": time.perf_counter() - started,
        "global_kill_used": False,
        "results": results,
    }
