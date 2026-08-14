"""只在 Pyxel 输入 API 层注入的确定性游戏控制器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ControlState:
    held: frozenset[str] = frozenset()
    mouse_position: tuple[int, int] | None = None


class Controller(Protocol):
    name: str

    def decide(self, frame: int, game: Any) -> ControlState: ...


class JumpController:
    name = "jump_v1"

    def decide(self, frame: int, game: Any) -> ControlState:
        del frame
        player_center = float(getattr(game, "player_x", 72)) + 8
        floors = [item for item in getattr(game, "floor", ()) if item[2] and item[0] >= -20]
        if not floors:
            return ControlState()
        target = min(floors, key=lambda item: abs(item[0] - player_center))
        target_center = float(target[0]) + 20
        if target_center < player_center - 3:
            return ControlState(frozenset({"KEY_LEFT"}))
        if target_center > player_center + 3:
            return ControlState(frozenset({"KEY_RIGHT"}))
        return ControlState()


class BubblesController:
    name = "bubbles_v1"

    def decide(self, frame: int, game: Any) -> ControlState:
        bubbles = list(getattr(game, "bubbles", ()))
        if not bubbles:
            return ControlState(mouse_position=(128, 128))
        # 最大气泡位置稳定且更容易命中；每 20 帧产生一次离散点击。
        bubble = max(bubbles, key=lambda item: (float(item.r), -float(item.x), -float(item.y)))
        mouse = (round(float(bubble.x)), round(float(bubble.y)))
        held = frozenset({"MOUSE_BUTTON_LEFT"}) if frame % 20 == 0 else frozenset()
        return ControlState(held=held, mouse_position=mouse)


class SnakeController:
    name = "snake_cycle_v1"
    _MOVES = (
        ("KEY_UP", (0, -1)),
        ("KEY_LEFT", (-1, 0)),
        ("KEY_DOWN", (0, 1)),
        ("KEY_RIGHT", (1, 0)),
    )

    def decide(self, frame: int, game: Any) -> ControlState:
        del frame
        if bool(getattr(game, "death", False)):
            return ControlState(frozenset({"KEY_R"}))
        snake = list(getattr(game, "snake", ()))
        apple = getattr(game, "apple", None)
        if not snake or apple is None:
            return ControlState(frozenset({"KEY_RIGHT"}))
        head = snake[0]
        current = tuple(getattr(game, "direction", (1, 0)))
        blocked = set(snake[:-1])
        candidates: list[tuple[int, int, str]] = []
        for priority, (key, (dx, dy)) in enumerate(self._MOVES):
            if (dx, dy) == (-current[0], -current[1]):
                continue
            next_pos = (head[0] + dx, head[1] + dy)
            inside = 0 <= next_pos[0] < 40 and 6 <= next_pos[1] < 50
            if inside and next_pos not in blocked:
                distance = abs(next_pos[0] - apple[0]) + abs(next_pos[1] - apple[1])
                candidates.append((distance, priority, key))
        key = min(candidates)[2] if candidates else "KEY_R"
        return ControlState(frozenset({key}))


class ShooterController:
    name = "shooter_patrol_v1"

    def decide(self, frame: int, game: Any) -> ControlState:
        scene = int(getattr(game, "scene", 0))
        if scene in {0, 2}:
            return ControlState(frozenset({"KEY_RETURN"}))
        phase = (frame // 75) % 2
        held = {"KEY_RIGHT" if phase == 0 else "KEY_LEFT"}
        if frame % 4 == 0:
            held.add("KEY_SPACE")
        return ControlState(frozenset(held))


class PlatformerController:
    name = "platformer_right_jump_v1"

    def decide(self, frame: int, game: Any) -> ControlState:
        del game
        held = {"KEY_RIGHT"}
        if frame % 18 == 0:
            held.add("KEY_SPACE")
        return ControlState(frozenset(held))


class DaylightController:
    name = "daylight_patrol_v1"

    def decide(self, frame: int, game: Any) -> ControlState:
        inner = getattr(game, "game", None)
        if inner is None or int(getattr(inner, "state", 0)) == 0:
            return ControlState(frozenset({"KEY_RETURN"}))
        directions = ("KEY_RIGHT", "KEY_DOWN", "KEY_LEFT", "KEY_UP")
        held = {directions[(frame // 25) % len(directions)], "KEY_Z"}
        if frame % 40 in {20, 21}:
            held.add("KEY_X")
        return ControlState(frozenset(held))


class MegaWingController:
    name = "mega_wing_patrol_v1"

    def decide(self, frame: int, game: Any) -> ControlState:
        scene = int(getattr(game, "scene", 0))
        if scene == 0:
            return ControlState(frozenset({"KEY_RETURN"}))
        if scene == 2:
            return ControlState()
        horizontal = "KEY_RIGHT" if (frame // 90) % 2 == 0 else "KEY_LEFT"
        vertical = "KEY_UP" if (frame // 45) % 2 == 0 else "KEY_DOWN"
        return ControlState(frozenset({horizontal, vertical, "KEY_SPACE"}))


class SpaceRescueController:
    name = "space_rescue_pulse_v1"

    def decide(self, frame: int, game: Any) -> ControlState:
        if bool(getattr(game, "is_title", True)):
            return ControlState(frozenset({"KEY_RETURN"}))
        held = frozenset({"KEY_SPACE"}) if frame % 40 < 18 else frozenset()
        return ControlState(held)


_CONTROLLERS: dict[str, type[Controller]] = {
    item.name: item
    for item in (
        JumpController,
        BubblesController,
        SnakeController,
        ShooterController,
        PlatformerController,
        DaylightController,
        MegaWingController,
        SpaceRescueController,
    )
}


def create_controller(name: str) -> Controller:
    try:
        return _CONTROLLERS[name]()
    except KeyError as exc:
        raise ValueError(f"未注册 controller: {name}") from exc


def controller_names() -> tuple[str, ...]:
    return tuple(sorted(_CONTROLLERS))
