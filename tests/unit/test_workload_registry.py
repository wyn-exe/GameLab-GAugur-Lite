from __future__ import annotations

from pathlib import Path

from gaugur_lite.workloads.registry import (
    GAME_REGISTRY,
    get_game,
    sha256_tree,
    verify_upstream,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_eight_registered_games_match_upstream_and_catalog() -> None:
    result = verify_upstream(REPO_ROOT)

    assert len(GAME_REGISTRY) == 8
    assert len({game.id for game in GAME_REGISTRY}) == 8
    assert len({game.controller for game in GAME_REGISTRY}) == 8
    assert result["status"] == "passed"
    assert result["manifest_exactly_covered"] is True
    assert all(item["passed"] for item in result["source_tree_checks"])
    assert get_game("daylight").target_fps == 10


def test_tree_hash_detects_path_and_content(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("one", encoding="utf-8")
    first = sha256_tree(tree)
    (tree / "a.txt").write_text("two", encoding="utf-8")
    second = sha256_tree(tree)
    (tree / "b.txt").write_text("two", encoding="utf-8")
    third = sha256_tree(tree)

    assert first != second
    assert second != third
    assert third[0] == 2
