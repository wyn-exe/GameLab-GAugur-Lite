from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gaugur_lite.metrics.writer import JsonlWriter
from gaugur_lite.workloads.pyxel_game import GameRunConfig, PyxelGameHarness
from gaugur_lite.workloads.registry import get_game


class FakePyxel:
    KEY_UP = 1
    KEY_DOWN = 2
    KEY_LEFT = 3
    KEY_RIGHT = 4
    KEY_SPACE = 5
    KEY_RETURN = 6
    KEY_R = 7
    KEY_Q = 8
    KEY_W = 9
    KEY_A = 10
    KEY_S = 11
    KEY_D = 12
    KEY_Z = 13
    KEY_N = 14
    KEY_X = 15
    KEY_M = 16
    MOUSE_BUTTON_LEFT = 17
    GAMEPAD1_BUTTON_DPAD_UP = 18
    GAMEPAD1_BUTTON_DPAD_DOWN = 19
    GAMEPAD1_BUTTON_DPAD_LEFT = 20
    GAMEPAD1_BUTTON_DPAD_RIGHT = 21
    GAMEPAD1_BUTTON_A = 22
    GAMEPAD1_BUTTON_B = 23
    GAMEPAD1_BUTTON_START = 24

    def __init__(self) -> None:
        self.stopped = False
        self.mouse_position = None
        self.seed = None

    def init(self, width: int, height: int, **kwargs: object) -> None:
        self.width = width
        self.height = height
        self.init_kwargs = kwargs

    def run(self, update: object, draw: object) -> None:
        while not self.stopped:
            update()
            draw()

    def flip(self) -> None:
        pass

    def load(self, filename: str, *args: object, **kwargs: object) -> None:
        self.loaded = filename

    def quit(self) -> None:
        self.stopped = True

    def btn(self, key: int) -> bool:
        return False

    def btnp(self, key: int, hold: int | None = None, repeat: int | None = None) -> bool:
        return False

    def btnr(self, key: int) -> bool:
        return False

    def play(self, *args: object, **kwargs: object) -> None:
        pass

    def playm(self, *args: object, **kwargs: object) -> None:
        pass

    def rseed(self, seed: int) -> None:
        self.seed = seed

    def set_mouse_pos(self, x: int, y: int) -> None:
        self.mouse_position = (x, y)


def test_harness_stops_at_exact_frame_count_and_writes_metrics(tmp_path: Path) -> None:
    pyxel = FakePyxel()
    game = get_game("pyxel_platformer")
    config = GameRunConfig(
        run_id="step3-smoke-pyxel_platformer-r01",
        duration_s=30,
        max_frames=5,
        headless=True,
        audio_mode="muted",
        metric_window_s=0.000001,
        batch_size=2,
    )
    class Callbacks(SimpleNamespace):
        updates = 0
        draws = 0

        def update(self) -> None:
            self.updates += 1

        def draw(self) -> None:
            self.draws += 1

    callbacks = Callbacks()
    metrics = tmp_path / "game_metrics.jsonl"
    with JsonlWriter(metrics, batch_size=2) as writer:
        harness = PyxelGameHarness(
            pyxel=pyxel,
            game=game,
            config=config,
            working_directory=tmp_path,
            output_directory=tmp_path,
            writer=writer,
        )
        with harness.installed():
            pyxel.init(128, 128, title=game.title)
            pyxel.run(callbacks.update, callbacks.draw)
        summary = harness.summary()

    rows = [json.loads(line) for line in metrics.read_text(encoding="utf-8").splitlines()]
    assert callbacks.updates == callbacks.draws == 5
    assert summary["status"] == "completed"
    assert summary["stop_reason"] == "max_frames_reached"
    assert summary["draw_count"] == 5
    assert sum(row["draw_count"] for row in rows) == 5
    assert pyxel.seed == game.seed
    assert pyxel.init_kwargs["display_scale"] == 2
    assert pyxel.init_kwargs["headless"] is True
    assert (tmp_path / "ready.json").is_file()
    assert (tmp_path / "heartbeat.json").is_file()
    assert (tmp_path / "stop.json").is_file()
