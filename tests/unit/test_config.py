from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from gaugur_lite.config import (
    ConfigError,
    config_sha256,
    discover_repo_root,
    load_local_config,
    load_workload_catalog,
    load_yaml_mapping,
    resolve_repo_path,
    stable_json_dumps,
)
from gaugur_lite.doctor import PACKAGE_NAMES, build_doctor_report
from gaugur_lite.schema import LocalConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_local_config_and_hash_are_stable() -> None:
    config = load_local_config(REPO_ROOT / "configs/local.example.yaml")

    assert config.host.id == "windows-rtx4060"
    assert config.measurement.repeats == 3
    assert config_sha256(config) == config_sha256(config)
    assert stable_json_dumps({"b": 2, "a": 1}) == stable_json_dumps({"a": 1, "b": 2})


def test_yaml_duplicate_key_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "duplicate.yaml"
    config_file.write_text("schema_version: 1\nschema_version: 2\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="重复键"):
        load_yaml_mapping(config_file)


def test_invalid_repeats_and_output_path_are_rejected() -> None:
    raw = load_yaml_mapping(REPO_ROOT / "configs/local.example.yaml")
    raw["measurement"]["repeats"] = 0
    with pytest.raises(ValidationError, match="repeats"):
        LocalConfig.model_validate(raw)

    raw = load_yaml_mapping(REPO_ROOT / "configs/local.example.yaml")
    raw["paths"]["raw"] = "../outside"
    with pytest.raises(ValidationError, match="仓库内相对路径"):
        LocalConfig.model_validate(raw)


def test_repo_path_resolution_prevents_escape() -> None:
    assert discover_repo_root(REPO_ROOT / "configs") == REPO_ROOT
    assert resolve_repo_path(REPO_ROOT, "data/raw") == REPO_ROOT / "data/raw"
    with pytest.raises(ConfigError, match="逃逸"):
        resolve_repo_path(REPO_ROOT, "../outside")


def test_all_eight_workload_paths_exist() -> None:
    catalog = load_workload_catalog(REPO_ROOT / "configs/workloads.yaml")

    assert len(catalog.workloads) == 8
    assert len({workload.id for workload in catalog.workloads}) == 8
    for workload in catalog.workloads:
        assert (REPO_ROOT / workload.entrypoint).is_file()
        assert (REPO_ROOT / workload.working_directory).is_dir()


def test_doctor_is_read_only_and_does_not_start_workload() -> None:
    commands: list[list[str]] = []

    def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="0, NVIDIA GeForce RTX 4060 Laptop GPU, 560.94, 8188\n",
            stderr="",
        )

    versions = {name: "test-version" for name in PACKAGE_NAMES}
    report = build_doctor_report(
        REPO_ROOT / "configs/local.example.yaml",
        dry_run=True,
        command_runner=fake_runner,
        executable_finder=lambda _: "nvidia-smi",
        package_version_getter=versions.__getitem__,
    )

    assert report["status"] == "passed"
    assert report["read_only"] is True
    assert report["dry_run"] is True
    assert report["workload_processes_started"] == 0
    assert report["mutations_performed"] == []
    assert len(commands) == 1
    assert commands[0][0] == "nvidia-smi"
    assert not any("games" in argument for argument in commands[0])

