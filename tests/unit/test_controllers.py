from __future__ import annotations

from collections import deque
from types import SimpleNamespace

from gaugur_lite.workloads.controllers import controller_names, create_controller
from gaugur_lite.workloads.registry import GAME_REGISTRY


def test_every_registry_controller_exists_and_is_deterministic() -> None:
    assert set(controller_names()) == {game.controller for game in GAME_REGISTRY}

    first = create_controller("platformer_right_jump_v1")
    second = create_controller("platformer_right_jump_v1")
    first_trace = [first.decide(frame, object()) for frame in range(100)]
    second_trace = [second.decide(frame, object()) for frame in range(100)]

    assert first_trace == second_trace
    assert first_trace[0].held == frozenset({"KEY_RIGHT", "KEY_SPACE"})


def test_state_aware_controllers_start_and_restart_games() -> None:
    snake = SimpleNamespace(
        death=True,
        snake=deque([(5, 10)]),
        apple=(10, 10),
        direction=(1, 0),
    )
    shooter = SimpleNamespace(scene=0)
    daylight = SimpleNamespace(game=SimpleNamespace(state=0))
    space_rescue = SimpleNamespace(is_title=True)

    assert create_controller("snake_cycle_v1").decide(0, snake).held == {"KEY_R"}
    assert create_controller("shooter_patrol_v1").decide(0, shooter).held == {"KEY_RETURN"}
    assert create_controller("daylight_patrol_v1").decide(0, daylight).held == {"KEY_RETURN"}
    assert create_controller("space_rescue_pulse_v1").decide(0, space_rescue).held == {"KEY_RETURN"}


def test_bubbles_controller_targets_largest_bubble() -> None:
    game = SimpleNamespace(
        bubbles=[
            SimpleNamespace(x=10.4, y=20.6, r=3.0),
            SimpleNamespace(x=80.2, y=90.8, r=8.0),
        ]
    )
    state = create_controller("bubbles_v1").decide(20, game)

    assert state.mouse_position == (80, 91)
    assert state.held == {"MOUSE_BUTTON_LEFT"}
