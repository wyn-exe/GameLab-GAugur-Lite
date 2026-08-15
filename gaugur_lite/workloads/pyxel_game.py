"""不修改上游源码的 Pyxel 生命周期包装、输入注入与 FPS 采集。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import runpy
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from ..config import stable_json_dumps
from ..metrics.summarize import describe, percentile
from ..metrics.writer import JsonlWriter, write_json_atomic
from .controllers import ControlState, create_controller
from .registry import GameDefinition, verify_upstream
from .window_probe import capture_window

_INPUT_NAMES = (
    "KEY_UP",
    "KEY_DOWN",
    "KEY_LEFT",
    "KEY_RIGHT",
    "KEY_SPACE",
    "KEY_RETURN",
    "KEY_R",
    "KEY_Q",
    "KEY_W",
    "KEY_A",
    "KEY_S",
    "KEY_D",
    "KEY_Z",
    "KEY_N",
    "KEY_X",
    "KEY_M",
    "MOUSE_BUTTON_LEFT",
    "GAMEPAD1_BUTTON_DPAD_UP",
    "GAMEPAD1_BUTTON_DPAD_DOWN",
    "GAMEPAD1_BUTTON_DPAD_LEFT",
    "GAMEPAD1_BUTTON_DPAD_RIGHT",
    "GAMEPAD1_BUTTON_A",
    "GAMEPAD1_BUTTON_B",
    "GAMEPAD1_BUTTON_START",
)


@dataclass(frozen=True)
class GameRunConfig:
    run_id: str
    duration_s: float
    max_frames: int
    headless: bool
    audio_mode: str
    metric_window_s: float = 1.0
    batch_size: int = 5
    warmup_s: float = 0.0
    barrier_file: Path | None = None
    barrier_timeout_s: float = 30.0


class _StopPyxelLoop(Exception):
    """只在包装后的 update/draw 与 pyxel.run 之间传播的计划停止信号。"""


class InputInjector:
    """把 controller 的符号输入映射到 Pyxel btn/btnp/btnr。"""

    def __init__(self, pyxel: Any, controller_name: str) -> None:
        self.pyxel = pyxel
        self.controller = create_controller(controller_name)
        self.state = ControlState()
        self.previous = ControlState()
        self.trace = hashlib.sha256()
        self.code_by_name = {
            name: getattr(pyxel, name) for name in _INPUT_NAMES if hasattr(pyxel, name)
        }
        self.controlled_codes = frozenset(self.code_by_name.values())
        self.held_codes: frozenset[int] = frozenset()
        self.pressed_codes: frozenset[int] = frozenset()
        self.released_codes: frozenset[int] = frozenset()

    def prepare(self, frame: int, game: Any) -> None:
        self.previous = self.state
        self.state = self.controller.decide(frame, game)
        unknown = self.state.held.difference(self.code_by_name)
        if unknown:
            raise RuntimeError(f"controller 产生未知输入: {sorted(unknown)}")
        current = frozenset(self.code_by_name[name] for name in self.state.held)
        previous = frozenset(self.code_by_name[name] for name in self.previous.held)
        self.held_codes = current
        self.pressed_codes = current.difference(previous)
        self.released_codes = previous.difference(current)
        if self.state.mouse_position is not None:
            x, y = self.state.mouse_position
            self.pyxel.set_mouse_pos(int(x), int(y))
        trace_row = {
            "frame": frame,
            "held": sorted(self.state.held),
            "mouse_position": self.state.mouse_position,
        }
        self.trace.update((stable_json_dumps(trace_row) + "\n").encode("utf-8"))

    def btn(self, key: int, original: Callable[..., bool]) -> bool:
        return key in self.held_codes if key in self.controlled_codes else bool(original(key))

    def btnp(
        self,
        key: int,
        hold: int | None,
        repeat: int | None,
        original: Callable[..., bool],
    ) -> bool:
        if key in self.controlled_codes:
            return key in self.pressed_codes
        return bool(original(key, hold, repeat))

    def btnr(self, key: int, original: Callable[..., bool]) -> bool:
        return key in self.released_codes if key in self.controlled_codes else bool(original(key))

    @property
    def trace_sha256(self) -> str:
        return self.trace.hexdigest()


class PyxelGameHarness:
    def __init__(
        self,
        *,
        pyxel: Any,
        game: GameDefinition,
        config: GameRunConfig,
        working_directory: Path,
        output_directory: Path,
        writer: JsonlWriter,
    ) -> None:
        self.pyxel = pyxel
        self.game = game
        self.config = config
        self.working_directory = working_directory.resolve()
        self.output_directory = output_directory
        self.writer = writer
        self.injector = InputInjector(pyxel, game.controller)
        self.title = game.title
        self.target_fps = game.target_fps
        self.logical_width: int | None = None
        self.logical_height: int | None = None
        self.started_wall_time_ns: int | None = None
        self.started_monotonic_ns: int | None = None
        self.finished_monotonic_ns: int | None = None
        self.measurement_started_monotonic_ns: int | None = None
        self.measurement_finished_monotonic_ns: int | None = None
        self.planned_measurement_start_monotonic_ns: int | None = None
        self.planned_measurement_end_monotonic_ns: int | None = None
        self.barrier_wait_s = 0.0
        self.phase = "measurement" if config.barrier_file is None else "waiting"
        self.update_count = 0
        self.draw_count = 0
        self.stop_reason: str | None = None
        self.game_object: Any = None
        self.events: list[dict[str, Any]] = []
        self.update_ms: list[float] = []
        self.draw_ms: list[float] = []
        self.draw_intervals_ms: list[float] = []
        self.deadline_misses = 0
        self.measurement_update_ms: list[float] = []
        self.measurement_draw_ms: list[float] = []
        self.measurement_draw_intervals_ms: list[float] = []
        self.measurement_deadline_misses = 0
        self.window_started_ns: int | None = None
        self.window_update_start = 0
        self.window_draw_start = 0
        self.window_update_ms: list[float] = []
        self.window_draw_ms: list[float] = []
        self.window_intervals_ms: list[float] = []
        self.window_deadline_misses = 0
        self.last_draw_ns: int | None = None
        self._originals: dict[str, Any] = {}
        self._run_called = False

    @contextmanager
    def installed(self) -> Iterator[None]:
        names = ("init", "load", "run", "btn", "btnp", "btnr", "quit", "play", "playm")
        self._originals = {name: getattr(self.pyxel, name) for name in names}
        self.pyxel.init = self._init
        self.pyxel.load = self._load
        self.pyxel.run = self._run
        self.pyxel.btn = lambda key: self.injector.btn(key, self._originals["btn"])
        self.pyxel.btnp = lambda key, hold=None, repeat=None: self.injector.btnp(
            key, hold, repeat, self._originals["btnp"]
        )
        self.pyxel.btnr = lambda key: self.injector.btnr(key, self._originals["btnr"])
        self.pyxel.quit = self._game_quit
        if self.config.audio_mode == "muted":
            self.pyxel.play = lambda *args, **kwargs: None
            self.pyxel.playm = lambda *args, **kwargs: None
        try:
            yield
        finally:
            for name, value in self._originals.items():
                setattr(self.pyxel, name, value)

    def _load(self, filename: str, *args: Any, **kwargs: Any) -> Any:
        # Pyxel 内核可能保留进程启动目录；在 Python 层固定为当前游戏工作目录。
        path = Path(filename)
        resolved = path if path.is_absolute() else (self.working_directory / path).resolve()
        return self._originals["load"](str(resolved), *args, **kwargs)

    def _init(self, width: int, height: int, *args: Any, **kwargs: Any) -> Any:
        title = kwargs.get("title") or (args[0] if args else None) or self.game.title
        fps = kwargs.get("fps")
        if fps is None and len(args) >= 2:
            fps = args[1]
        kwargs["display_scale"] = self.game.display_scale
        if self.config.headless:
            kwargs["headless"] = True
        result = self._originals["init"](width, height, *args, **kwargs)
        # Pyxel 会按 init 调用栈改变 cwd；恢复到注册表规定的游戏目录。
        os.chdir(self.working_directory)
        self.logical_width = int(width)
        self.logical_height = int(height)
        self.title = str(title)
        self.target_fps = int(fps or 30)
        self.pyxel.rseed(self.game.seed)
        return result

    def _run(self, update: Callable[[], None], draw: Callable[[], None]) -> Any:
        if self._run_called:
            raise RuntimeError("一个 workload 只能调用一次 pyxel.run")
        self._run_called = True
        self.game_object = getattr(update, "__self__", None)
        self.started_wall_time_ns = time.time_ns()
        self.started_monotonic_ns = time.perf_counter_ns()
        self._write_ready()
        self._await_barrier()
        self.window_started_ns = time.perf_counter_ns()
        self.phase = (
            "warmup"
            if self.planned_measurement_start_monotonic_ns is not None
            and self.window_started_ns < self.planned_measurement_start_monotonic_ns
            else "measurement"
        )
        if self.phase == "measurement":
            self._start_measurement(self.window_started_ns)
        try:
            while self.stop_reason is None:
                try:
                    self._maybe_start_measurement(time.perf_counter_ns())
                    self._update(update)
                    self._draw(draw)
                except _StopPyxelLoop:
                    break
                self.pyxel.flip()
            return None
        finally:
            self.finished_monotonic_ns = time.perf_counter_ns()
            if self.stop_reason is None:
                self.stop_reason = "window_closed"
            self._flush_window(self.finished_monotonic_ns, force=True)
            self._write_stop()

    def _update(self, callback: Callable[[], None]) -> None:
        self.injector.prepare(self.update_count, self.game_object)
        started = time.perf_counter_ns()
        callback()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        self.update_count += 1
        self.update_ms.append(elapsed_ms)
        self.window_update_ms.append(elapsed_ms)
        if self.phase == "measurement":
            self.measurement_update_ms.append(elapsed_ms)

    def _draw(self, callback: Callable[[], None]) -> None:
        started = time.perf_counter_ns()
        callback()
        ended = time.perf_counter_ns()
        elapsed_ms = (ended - started) / 1_000_000
        self.draw_count += 1
        self.draw_ms.append(elapsed_ms)
        self.window_draw_ms.append(elapsed_ms)
        if self.phase == "measurement":
            self.measurement_draw_ms.append(elapsed_ms)

        if self.last_draw_ns is not None:
            interval_ms = (ended - self.last_draw_ns) / 1_000_000
            self.draw_intervals_ms.append(interval_ms)
            self.window_intervals_ms.append(interval_ms)
            if self.phase == "measurement":
                self.measurement_draw_intervals_ms.append(interval_ms)
            deadline_ms = 1000.0 / self.target_fps
            if interval_ms > deadline_ms * 1.5:
                missed = max(1, int(interval_ms // deadline_ms) - 1)
                self.deadline_misses += missed
                self.window_deadline_misses += missed
                if self.phase == "measurement":
                    self.measurement_deadline_misses += missed
        self.last_draw_ns = ended
        self._flush_window(ended)

        if self.config.max_frames > 0 and self.draw_count >= self.config.max_frames:
            self.stop_reason = "max_frames_reached"
        elif (
            self.config.max_frames == 0
            and self.planned_measurement_end_monotonic_ns is not None
            and ended >= self.planned_measurement_end_monotonic_ns
        ):
            self.measurement_finished_monotonic_ns = ended
            self.stop_reason = "duration_reached"

    def _game_quit(self) -> Any:
        if self.stop_reason is None:
            self.stop_reason = "game_requested_quit"
        raise _StopPyxelLoop

    def elapsed_s(self, now_ns: int | None = None) -> float:
        if self.started_monotonic_ns is None:
            return 0.0
        end = now_ns or self.finished_monotonic_ns or time.perf_counter_ns()
        return (end - self.started_monotonic_ns) / 1_000_000_000

    def _await_barrier(self) -> None:
        """ready 后等待父进程统一释放；所有进程共享 Windows monotonic clock。"""

        if self.config.barrier_file is None:
            now_ns = time.perf_counter_ns()
            self.planned_measurement_start_monotonic_ns = now_ns + int(
                self.config.warmup_s * 1_000_000_000
            )
            self.planned_measurement_end_monotonic_ns = (
                self.planned_measurement_start_monotonic_ns
                + int(self.config.duration_s * 1_000_000_000)
            )
            return
        started = time.perf_counter()
        deadline = started + self.config.barrier_timeout_s
        last_heartbeat = started
        while time.perf_counter() < deadline:
            if self.config.barrier_file.is_file():
                payload = json.loads(self.config.barrier_file.read_text(encoding="utf-8"))
                if payload.get("status") != "released":
                    raise RuntimeError("runner barrier 状态不是 released")
                if payload.get("run_id") != self.config.run_id:
                    raise RuntimeError("runner barrier 的 run_id 不匹配")
                self.planned_measurement_start_monotonic_ns = int(
                    payload["measurement_start_monotonic_ns"]
                )
                self.planned_measurement_end_monotonic_ns = int(
                    payload["measurement_end_monotonic_ns"]
                )
                self.barrier_wait_s = time.perf_counter() - started
                return
            now = time.perf_counter()
            if now - last_heartbeat >= 1.0:
                self._write_heartbeat()
                last_heartbeat = now
            time.sleep(0.02)
        raise TimeoutError("等待 runner barrier 超时")

    def _start_measurement(self, now_ns: int) -> None:
        if self.measurement_started_monotonic_ns is not None:
            return
        if self.phase == "warmup":
            self._flush_window(now_ns, force=True)
        self.phase = "measurement"
        self.measurement_started_monotonic_ns = now_ns
        self.window_started_ns = now_ns
        self.window_update_start = self.update_count
        self.window_draw_start = self.draw_count
        self.window_update_ms = []
        self.window_draw_ms = []
        self.window_intervals_ms = []
        self.window_deadline_misses = 0
        self.last_draw_ns = None
        write_json_atomic(
            self.output_directory / "measurement-start.json",
            {
                "schema_version": 1,
                "run_id": self.config.run_id,
                "status": "measurement",
                "pid": os.getpid(),
                "wall_time_ns": time.time_ns(),
                "monotonic_time_ns": now_ns,
                "planned_monotonic_time_ns": self.planned_measurement_start_monotonic_ns,
            },
        )

    def _maybe_start_measurement(self, now_ns: int) -> None:
        if (
            self.measurement_started_monotonic_ns is None
            and self.planned_measurement_start_monotonic_ns is not None
            and now_ns >= self.planned_measurement_start_monotonic_ns
        ):
            self._start_measurement(now_ns)

    def _flush_window(self, now_ns: int, *, force: bool = False) -> None:
        if self.window_started_ns is None:
            return
        duration_s = (now_ns - self.window_started_ns) / 1_000_000_000
        draws = self.draw_count - self.window_draw_start
        if draws <= 0 or (not force and duration_s < self.config.metric_window_s):
            return
        event = {
            "schema_version": 1,
            "run_id": self.config.run_id,
            "source": "workload",
            "workload_id": self.game.id,
            "wall_time_ns": time.time_ns(),
            "monotonic_time_ns": now_ns,
            "sequence": len(self.events),
            "phase": self.phase,
            "elapsed_s": self.elapsed_s(now_ns),
            "window_duration_s": duration_s,
            "update_count": self.update_count - self.window_update_start,
            "draw_count": draws,
            "game_fps": draws / duration_s,
            "target_fps": self.target_fps,
            "update_time_ms": describe(self.window_update_ms),
            "draw_time_ms": describe(self.window_draw_ms),
            "draw_interval_ms": describe(self.window_intervals_ms),
            "missed_deadline_count": self.window_deadline_misses,
            "controller_frame_end": self.update_count,
            "window": (
                {"headless": True, "found": False, "title": self.title}
                if self.config.headless
                else {"headless": False, **capture_window(self.title)}
            ),
        }
        self.writer.write(event)
        self.events.append(event)
        self.window_started_ns = now_ns
        self.window_update_start = self.update_count
        self.window_draw_start = self.draw_count
        self.window_update_ms = []
        self.window_draw_ms = []
        self.window_intervals_ms = []
        self.window_deadline_misses = 0
        self._write_heartbeat()

    def _write_ready(self) -> None:
        write_json_atomic(
            self.output_directory / "ready.json",
            {
                "schema_version": 1,
                "run_id": self.config.run_id,
                "status": "ready",
                "pid": os.getpid(),
                "wall_time_ns": self.started_wall_time_ns,
                "title": self.title,
                "target_fps": self.target_fps,
                "headless": self.config.headless,
                "barrier_required": self.config.barrier_file is not None,
            },
        )
        self._write_heartbeat()

    def _write_heartbeat(self) -> None:
        write_json_atomic(
            self.output_directory / "heartbeat.json",
            {
                "schema_version": 1,
                "run_id": self.config.run_id,
                "status": "running",
                "pid": os.getpid(),
                "wall_time_ns": time.time_ns(),
                "elapsed_s": self.elapsed_s(),
                "update_count": self.update_count,
                "draw_count": self.draw_count,
                "metric_rows": self.writer.count,
            },
        )

    def _write_stop(self) -> None:
        write_json_atomic(
            self.output_directory / "stop.json",
            {
                "schema_version": 1,
                "run_id": self.config.run_id,
                "status": "stopped",
                "pid": os.getpid(),
                "wall_time_ns": time.time_ns(),
                "elapsed_s": self.elapsed_s(),
                "stop_reason": self.stop_reason,
                "update_count": self.update_count,
                "draw_count": self.draw_count,
                "metric_rows": self.writer.count,
            },
        )

    def summary(self) -> dict[str, Any]:
        measurement_events = [event for event in self.events if event.get("phase") == "measurement"]
        fps_values = [
            float(event["game_fps"])
            for event in measurement_events
            if float(event["window_duration_s"]) >= self.config.metric_window_s * 0.8
        ]
        window_rows = [event["window"] for event in measurement_events]
        visible_rows = [row for row in window_rows if not row.get("headless", False)]
        planned_stop = self.stop_reason in {"duration_reached", "max_frames_reached"}
        count_consistent = (
            sum(int(event["draw_count"]) for event in self.events) == self.draw_count
            and sum(int(event["update_count"]) for event in self.events) == self.update_count
        )
        window_ok = self.config.headless or (
            bool(visible_rows)
            and all(row.get("found") is True for row in visible_rows)
            and all(row.get("visible") is True for row in visible_rows)
            and all(row.get("minimized") is False for row in visible_rows)
        )
        expected_frames_ok = self.config.max_frames == 0 or self.draw_count == self.config.max_frames
        measurement_coverage_s = sum(
            float(event["window_duration_s"]) for event in measurement_events
        )
        measurement_coverage_ratio = min(1.0, measurement_coverage_s / self.config.duration_s)
        coverage_ok = self.config.max_frames > 0 or measurement_coverage_ratio >= 0.95
        checks = [
            {"name": "planned_stop", "passed": planned_stop, "actual": self.stop_reason},
            {"name": "draw_count_positive", "passed": self.draw_count > 0, "actual": self.draw_count},
            {"name": "metric_count_positive", "passed": bool(self.events), "actual": len(self.events)},
            {"name": "counts_consistent", "passed": count_consistent, "actual": count_consistent},
            {"name": "expected_frames", "passed": expected_frames_ok, "actual": self.draw_count, "threshold": self.config.max_frames or None},
            {"name": "target_fps_matches_registry", "passed": self.target_fps == self.game.target_fps, "actual": self.target_fps, "threshold": self.game.target_fps},
            {"name": "window_healthy", "passed": window_ok, "actual": window_ok, "skipped": self.config.headless},
            {"name": "measurement_coverage", "passed": coverage_ok, "actual": measurement_coverage_ratio, "threshold": 0.95, "skipped": self.config.max_frames > 0},
        ]
        passed = all(bool(item["passed"]) for item in checks)
        return {
            "schema_version": 1,
            "run_id": self.config.run_id,
            "status": "completed" if passed else "invalid",
            "workload_id": self.game.id,
            "title": self.game.title,
            "entrypoint": self.game.entrypoint,
            "controller": self.game.controller,
            "controller_trace_sha256": self.injector.trace_sha256,
            "seed": self.game.seed,
            "audio_mode": self.config.audio_mode,
            "headless": self.config.headless,
            "display_scale": self.game.display_scale,
            "logical_width": self.logical_width,
            "logical_height": self.logical_height,
            "target_fps": self.target_fps,
            "duration_requested_s": self.config.duration_s,
            "warmup_requested_s": self.config.warmup_s,
            "barrier_used": self.config.barrier_file is not None,
            "barrier_wait_s": self.barrier_wait_s,
            "planned_measurement_start_monotonic_ns": self.planned_measurement_start_monotonic_ns,
            "planned_measurement_end_monotonic_ns": self.planned_measurement_end_monotonic_ns,
            "measurement_started_monotonic_ns": self.measurement_started_monotonic_ns,
            "measurement_finished_monotonic_ns": self.measurement_finished_monotonic_ns,
            "measurement_coverage_s": measurement_coverage_s,
            "measurement_coverage_ratio": measurement_coverage_ratio,
            "max_frames": self.config.max_frames,
            "elapsed_s": self.elapsed_s(),
            "stop_reason": self.stop_reason,
            "update_count": self.update_count,
            "draw_count": self.draw_count,
            "metric_rows": len(self.events),
            "measurement_metric_rows": len(measurement_events),
            "game_fps": {
                **describe(fps_values),
                "p05": percentile(fps_values, 0.05),
                "windows_used": len(fps_values),
            },
            "update_time_ms": describe(self.measurement_update_ms),
            "draw_time_ms": describe(self.measurement_draw_ms),
            "draw_interval_ms": describe(self.measurement_draw_intervals_ms),
            "missed_deadline_count": self.measurement_deadline_misses,
            "window_observations": len(visible_rows),
            "quality_gate": {
                "status": "passed" if passed else "failed",
                "checks": checks,
                "failed_checks": [item["name"] for item in checks if not item["passed"]],
            },
            "output_files": {
                "metrics": "game_metrics.jsonl",
                "ready": "ready.json",
                "heartbeat": "heartbeat.json",
                "stop": "stop.json",
                "status": "status.json",
                "summary": "summary.json",
            },
        }


@contextmanager
def _entry_environment(working_directory: Path) -> Iterator[None]:
    old_cwd = Path.cwd()
    old_path = list(sys.path)
    os.chdir(working_directory)
    sys.path.insert(0, str(working_directory))
    try:
        yield
    finally:
        sys.path[:] = old_path
        os.chdir(old_cwd)


def _safe_error(exc: BaseException, repo_root: Path) -> str:
    message = str(exc).replace(str(repo_root), "<repo>")
    message = re.sub(r"[A-Za-z]:\\[^\r\n]+", "<path>", message)
    return message[:500]


def execute_game_child(
    *,
    repo_root: Path,
    game: GameDefinition,
    config: GameRunConfig,
    output_directory: Path,
) -> dict[str, Any]:
    """在 launcher 子进程内执行一个上游入口，并总是落盘最终状态。"""

    import pyxel

    upstream = verify_upstream(repo_root)
    if upstream["status"] != "passed":
        raise RuntimeError("上游完整性校验失败，拒绝启动游戏")
    status_path = output_directory / "status.json"
    started_wall_ns = time.time_ns()
    writer: JsonlWriter | None = None
    write_json_atomic(
        status_path,
        {
            "schema_version": 1,
            "run_id": config.run_id,
            "status": "running",
            "started_wall_time_ns": started_wall_ns,
            "updated_wall_time_ns": started_wall_ns,
            "finished_wall_time_ns": None,
            "samples_written": 0,
            "error_type": None,
            "error_message": None,
            "summary_file": None,
        },
    )
    try:
        random.seed(game.seed)
        with JsonlWriter(output_directory / "game_metrics.jsonl", batch_size=config.batch_size) as writer:
            harness = PyxelGameHarness(
                pyxel=pyxel,
                game=game,
                config=config,
                working_directory=repo_root / game.working_directory,
                output_directory=output_directory,
                writer=writer,
            )
            with harness.installed(), _entry_environment(repo_root / game.working_directory):
                runpy.run_path(str(repo_root / game.entrypoint), run_name="__main__")
            if not harness._run_called:
                raise RuntimeError("上游入口没有调用 pyxel.run")
            summary = harness.summary()
            samples = writer.count
        write_json_atomic(output_directory / "summary.json", summary)
        finished = time.time_ns()
        write_json_atomic(
            status_path,
            {
                "schema_version": 1,
                "run_id": config.run_id,
                "status": summary["status"],
                "started_wall_time_ns": started_wall_ns,
                "updated_wall_time_ns": finished,
                "finished_wall_time_ns": finished,
                "samples_written": samples,
                "error_type": None,
                "error_message": None,
                "summary_file": "summary.json",
            },
        )
        return summary
    except BaseException as exc:
        finished = time.time_ns()
        write_json_atomic(
            status_path,
            {
                "schema_version": 1,
                "run_id": config.run_id,
                "status": "failed",
                "started_wall_time_ns": started_wall_ns,
                "updated_wall_time_ns": finished,
                "finished_wall_time_ns": finished,
                "samples_written": writer.count if writer is not None else 0,
                "error_type": type(exc).__name__,
                "error_message": _safe_error(exc, repo_root),
                "summary_file": None,
            },
        )
        raise
