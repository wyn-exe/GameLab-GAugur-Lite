"""真实 Pyxel workload 的注册、控制、运行与验收。"""

from .registry import GAME_REGISTRY, GameDefinition, get_game

__all__ = ["GAME_REGISTRY", "GameDefinition", "get_game"]
