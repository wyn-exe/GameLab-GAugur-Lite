"""八个正式 Pyxel 游戏的不可变注册表与上游完整性校验。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import load_workload_catalog


@dataclass(frozen=True)
class ProtectedFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class SourceTree:
    path: str
    file_count: int
    sha256: str


@dataclass(frozen=True)
class GameDefinition:
    id: str
    title: str
    kind: str
    entrypoint: str
    working_directory: str
    controller: str
    seed: int
    display_scale: int
    target_fps: int
    upstream_files: tuple[ProtectedFile, ...]
    source_tree: SourceTree | None = None

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["nominal_frames_30s"] = self.target_fps * 30
        return value


LICENSE = ProtectedFile(
    "LICENSE", "1A0FFBE3ACA378F955ADFA93CBC913F3DC907B6ABD87C221E66FA6120361AF88"
)

GAME_REGISTRY: tuple[GameDefinition, ...] = (
    GameDefinition(
        id="pyxel_jump",
        title="Pyxel Jump",
        kind="jump",
        entrypoint="games/pyxel/02_jump_game.py",
        working_directory="games/pyxel",
        controller="jump_v1",
        seed=1001,
        display_scale=2,
        target_fps=30,
        upstream_files=(
            ProtectedFile("02_jump_game.py", "4743A825F1C9CAE7E049AC52115F4ECF83350DB546C00745A54AF187D7BC2E46"),
            ProtectedFile("assets/jump_game.pyxres", "AA97A53590C4443283E261821B55ADBAA6D9190A1ADC972C580A2BD78E268367"),
            LICENSE,
        ),
    ),
    GameDefinition(
        id="pyxel_bubbles",
        title="Pyxel Bubbles",
        kind="click",
        entrypoint="games/pyxel/06_click_game.py",
        working_directory="games/pyxel",
        controller="bubbles_v1",
        seed=1002,
        display_scale=2,
        target_fps=30,
        upstream_files=(
            ProtectedFile("06_click_game.py", "54C101E90BDAED839AC4C4284494530D5DF37A5819A35F1E41EEBB0B3DA5D900"),
            LICENSE,
        ),
    ),
    GameDefinition(
        id="pyxel_snake",
        title="Snake!",
        kind="snake",
        entrypoint="games/pyxel/07_snake.py",
        working_directory="games/pyxel",
        controller="snake_cycle_v1",
        seed=1003,
        display_scale=2,
        target_fps=20,
        upstream_files=(
            ProtectedFile("07_snake.py", "BC533FEF92D1D358E570F69D979919FE5C1CD8C4DA49841104B49A1E661A5AC3"),
            LICENSE,
        ),
    ),
    GameDefinition(
        id="pyxel_shooter",
        title="Pyxel Shooter",
        kind="shooter",
        entrypoint="games/pyxel/09_shooter.py",
        working_directory="games/pyxel",
        controller="shooter_patrol_v1",
        seed=1004,
        display_scale=2,
        target_fps=30,
        upstream_files=(
            ProtectedFile("09_shooter.py", "0F53E4156907EAC41080A8F289E22C89365B3239517C754CF59A51AF201D5563"),
            LICENSE,
        ),
    ),
    GameDefinition(
        id="pyxel_platformer",
        title="Pyxel Platformer",
        kind="platformer",
        entrypoint="games/pyxel/10_platformer.py",
        working_directory="games/pyxel",
        controller="platformer_right_jump_v1",
        seed=1005,
        display_scale=2,
        target_fps=30,
        upstream_files=(
            ProtectedFile("10_platformer.py", "1B2C75B49DC9AB0254FB4B724682A4787868814A38D2F08920720DC5B6007AC5"),
            ProtectedFile("assets/platformer.pyxres", "0812E282065FBA8BBFB3357C7912CAF3B9040AF5343A02B1E333E6F3E5CDAD35"),
            LICENSE,
        ),
    ),
    GameDefinition(
        id="daylight",
        title="30 Seconds of Daylight",
        kind="roguelike",
        entrypoint="games/pyxel/apps-src/30SecondsOfDaylight/src/main.py",
        working_directory="games/pyxel/apps-src/30SecondsOfDaylight/src",
        controller="daylight_patrol_v1",
        seed=1006,
        display_scale=2,
        target_fps=10,
        upstream_files=(
            ProtectedFile("apps/30sec_of_daylight.pyxapp", "CD04D0138891D9695602DCF2B409068B9623E309B6E885B73EE1EB463BC2F95A"),
            LICENSE,
        ),
        source_tree=SourceTree(
            "apps-src/30SecondsOfDaylight",
            24,
            "D14F328B31C7B6805F2CA494ED6BB0DD3CE1A8842A7B616A52B82D2336441CB1",
        ),
    ),
    GameDefinition(
        id="mega_wing",
        title="Mega Wing",
        kind="bullet_hell",
        entrypoint="games/pyxel/apps-src/mega_wing/mega_wing.py",
        working_directory="games/pyxel/apps-src/mega_wing",
        controller="mega_wing_patrol_v1",
        seed=1007,
        display_scale=2,
        target_fps=30,
        upstream_files=(
            ProtectedFile("apps/mega_wing.pyxapp", "CB6CAE3B8DF4526CAE6B23100247296BD78E6C8D8AC49939648B28FC939F3324"),
            LICENSE,
        ),
        source_tree=SourceTree(
            "apps-src/mega_wing",
            3,
            "48AB0F60B00A5487CC2E309BD116B64ECD9DFA2FB564B17B61828C90B9CB9BD0",
        ),
    ),
    GameDefinition(
        id="space_rescue",
        title="Space Rescue",
        kind="one_key",
        entrypoint="games/pyxel/apps-src/space_rescue/space_rescue.py",
        working_directory="games/pyxel/apps-src/space_rescue",
        controller="space_rescue_pulse_v1",
        seed=1008,
        display_scale=2,
        target_fps=30,
        upstream_files=(
            ProtectedFile("apps/space_rescue.pyxapp", "250ABABACC8F8B50A0E12C30E966B81E6AEA1D8559E065A9A734BAF59BA82BE9"),
            LICENSE,
        ),
        source_tree=SourceTree(
            "apps-src/space_rescue",
            3,
            "E40326B9A5DD418C958328D388026A2AE45119FE0E1F9DCA106408E582C649AC",
        ),
    ),
)

_BY_ID = {game.id: game for game in GAME_REGISTRY}


def get_game(workload_id: str) -> GameDefinition:
    try:
        return _BY_ID[workload_id]
    except KeyError as exc:
        choices = ", ".join(sorted(_BY_ID))
        raise ValueError(f"未知 workload {workload_id!r}；可选值: {choices}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sha256_tree(path: Path) -> tuple[int, str]:
    """路径、长度和内容共同参与哈希，可检测增删、改名和内容变化。"""

    files = sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"
    )
    digest = hashlib.sha256()
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        data = item.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return len(files), digest.hexdigest().upper()


def _read_manifest(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        parts = raw_line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"SHA256SUMS.txt 第 {line_number} 行格式错误")
        relative = parts[1].strip().lstrip("*").replace("\\", "/")
        if relative in result:
            raise ValueError(f"SHA256SUMS.txt 重复路径: {relative}")
        result[relative] = parts[0].upper()
    return result


def verify_upstream(repo_root: Path, pyxel_root: Path | None = None) -> dict[str, Any]:
    root = (pyxel_root or repo_root / "games" / "pyxel").resolve()
    manifest = _read_manifest(root / "SHA256SUMS.txt")
    file_checks: list[dict[str, Any]] = []
    tree_checks: list[dict[str, Any]] = []

    expected_manifest: dict[str, str] = {}
    for game in GAME_REGISTRY:
        for protected in game.upstream_files:
            expected_manifest[protected.path] = protected.sha256
            target = root / protected.path
            actual = sha256_file(target) if target.is_file() else None
            manifest_digest = manifest.get(protected.path)
            file_checks.append(
                {
                    "workload_id": game.id,
                    "path": protected.path,
                    "expected_sha256": protected.sha256,
                    "manifest_sha256": manifest_digest,
                    "actual_sha256": actual,
                    "passed": actual == protected.sha256 == manifest_digest,
                }
            )
        if game.source_tree is not None:
            target = root / game.source_tree.path
            actual_count, actual_digest = sha256_tree(target) if target.is_dir() else (0, None)
            tree_checks.append(
                {
                    "workload_id": game.id,
                    "path": game.source_tree.path,
                    "expected_file_count": game.source_tree.file_count,
                    "actual_file_count": actual_count,
                    "expected_sha256": game.source_tree.sha256,
                    "actual_sha256": actual_digest,
                    "passed": (
                        actual_count == game.source_tree.file_count
                        and actual_digest == game.source_tree.sha256
                    ),
                }
            )

    catalog = load_workload_catalog(repo_root / "configs" / "workloads.yaml")
    catalog_by_id = {item.id: item for item in catalog.workloads}
    catalog_checks: list[dict[str, Any]] = []
    for game in GAME_REGISTRY:
        configured = catalog_by_id.get(game.id)
        passed = configured is not None and all(
            (
                configured.entrypoint == game.entrypoint,
                configured.working_directory == game.working_directory,
                configured.controller == game.controller,
                configured.seed == game.seed,
                configured.display_scale == game.display_scale,
                (repo_root / game.entrypoint).is_file(),
                (repo_root / game.working_directory).is_dir(),
            )
        )
        catalog_checks.append({"workload_id": game.id, "passed": passed})

    manifest_coverage = manifest == expected_manifest
    passed = (
        len(GAME_REGISTRY) == 8
        and len(catalog_by_id) == 8
        and manifest_coverage
        and all(item["passed"] for item in (*file_checks, *tree_checks, *catalog_checks))
    )
    return {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "game_count": len(GAME_REGISTRY),
        "manifest_entry_count": len(manifest),
        "manifest_exactly_covered": manifest_coverage,
        "file_checks": file_checks,
        "source_tree_checks": tree_checks,
        "catalog_checks": catalog_checks,
    }
