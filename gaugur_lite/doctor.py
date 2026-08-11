"""只读环境诊断；禁止创建目录、启动游戏或修改系统设置。"""

from __future__ import annotations

import csv
import importlib.metadata
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    config_sha256,
    discover_repo_root,
    load_local_config,
    resolved_output_paths,
)

PACKAGE_NAMES = (
    "pydantic",
    "typer",
    "PyYAML",
    "psutil",
    "pyxel",
    "torch",
    "nvidia-ml-py",
)


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _query_nvidia_smi(
    gpu_index: int,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
    executable_finder: Callable[[str], str | None],
) -> tuple[bool, dict[str, Any]]:
    executable = executable_finder("nvidia-smi")
    if executable is None:
        return False, {"error": "nvidia-smi not found"}

    command = [
        executable,
        "--query-gpu=index,name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = command_runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, {"error": f"nvidia-smi failed: {exc}"}
    if completed.returncode != 0:
        return False, {
            "returncode": completed.returncode,
            "stderr": completed.stderr.strip(),
        }

    rows = list(csv.reader(line for line in completed.stdout.splitlines() if line.strip()))
    for row in rows:
        if len(row) >= 4 and row[0].strip().isdigit() and int(row[0]) == gpu_index:
            return True, {
                "index": gpu_index,
                "name": row[1].strip(),
                "driver_version": row[2].strip(),
                "memory_total_mib": int(float(row[3].strip())),
            }
    return False, {"error": f"GPU index {gpu_index} not present", "gpu_count": len(rows)}


def build_doctor_report(
    config_path: str | Path,
    *,
    dry_run: bool = False,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    executable_finder: Callable[[str], str | None] = shutil.which,
    package_version_getter: Callable[[str], str] = importlib.metadata.version,
) -> dict[str, Any]:
    config_file = Path(config_path).resolve()
    config = load_local_config(config_file)
    repo_root = discover_repo_root(config_file)
    output_paths = resolved_output_paths(config, repo_root)

    checks: list[dict[str, Any]] = []
    actual_platform = platform.system().lower()
    checks.append(
        _check(
            "platform",
            actual_platform == config.host.platform,
            {"expected": config.host.platform, "actual": actual_platform},
        )
    )
    supported_python = (3, 11) <= sys.version_info[:2] < (3, 13)
    checks.append(
        _check(
            "python",
            supported_python,
            {"version": platform.python_version(), "supported": ">=3.11,<3.13"},
        )
    )

    package_versions: dict[str, str] = {}
    missing_packages: list[str] = []
    for package_name in PACKAGE_NAMES:
        try:
            package_versions[package_name] = package_version_getter(package_name)
        except importlib.metadata.PackageNotFoundError:
            missing_packages.append(package_name)
    checks.append(
        _check(
            "packages",
            not missing_packages,
            {"versions": package_versions, "missing": missing_packages},
        )
    )

    paths_within_repo = all(
        path == repo_root or repo_root in path.parents for path in output_paths.values()
    )
    checks.append(
        _check(
            "output_paths",
            paths_within_repo,
            {
                name: {"path": path.relative_to(repo_root).as_posix(), "exists": path.exists()}
                for name, path in output_paths.items()
            },
        )
    )

    nvidia_ok, nvidia_detail = _query_nvidia_smi(
        config.host.gpu_index,
        command_runner=command_runner,
        executable_finder=executable_finder,
    )
    checks.append(_check("nvidia_smi", nvidia_ok, nvidia_detail))

    return {
        "schema_version": 1,
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "read_only": True,
        "dry_run": dry_run,
        "config": {
            "path": _safe_relative(config_file, repo_root),
            "sha256": config_sha256(config),
            "host_id": config.host.id,
        },
        "checks": checks,
        "workload_processes_started": 0,
        "mutations_performed": [],
    }

