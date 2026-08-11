from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from gaugur_lite.config import config_sha256, stable_json_dumps
from gaugur_lite.schema import (
    HostSpec,
    MetricEvent,
    RunMode,
    RunSpec,
    RunStatus,
    WorkloadSpec,
    make_colocation_id,
    make_combination_key,
    make_pressure_token,
)


def test_combination_and_colocation_ids_are_canonical() -> None:
    key = make_combination_key(("pyxel_snake", "mega_wing", "daylight"))

    assert key == "daylight+mega_wing+pyxel_snake"
    assert make_colocation_id(key, 2) == "daylight+mega_wing+pyxel_snake__r02"
    assert make_pressure_token(0.5) == "p050"
    with pytest.raises(ValueError, match="重复"):
        make_combination_key(("daylight", "daylight"))


def test_run_spec_generates_stable_profile_id() -> None:
    run = RunSpec(
        experiment_id="formal-v1",
        mode=RunMode.PRESSURE_PROFILE,
        target_id="pyxel_shooter",
        game_entrypoint="games/pyxel/09_shooter.py",
        controller="shooter_patrol_v1",
        resource="gpu_compute",
        pressure_requested=0.5,
        repeat=1,
        seed=20260811,
        host_id="windows-rtx4060",
    )

    assert run.run_id == "formal-v1__profile__pyxel_shooter__gpu_compute__p050__r01"
    assert run.combination_key is None
    assert run.status is RunStatus.PLANNED


def test_colocation_run_is_order_independent() -> None:
    left = RunSpec(
        experiment_id="formal-v1",
        mode=RunMode.COLOCATION,
        target_id="pyxel_snake",
        neighbor_ids=("mega_wing", "daylight"),
        repeat=3,
        host_id="windows-rtx4060",
    )
    right = RunSpec(
        experiment_id="formal-v1",
        mode=RunMode.COLOCATION,
        target_id="pyxel_snake",
        neighbor_ids=("daylight", "mega_wing"),
        repeat=3,
        host_id="windows-rtx4060",
    )

    assert left == right
    assert left.combination_key == "daylight+mega_wing+pyxel_snake"
    assert left.colocation_id == "daylight+mega_wing+pyxel_snake__r03"
    assert left.run_id == "formal-v1__colocation__daylight+mega_wing+pyxel_snake__r03"
    assert config_sha256(left) == config_sha256(right)


@pytest.mark.parametrize(
    ("field", "value"),
    (("pressure_requested", 1.01), ("repeat", 0), ("duration_s", 0)),
)
def test_run_spec_rejects_invalid_ranges(field: str, value: float | int) -> None:
    data = {
        "experiment_id": "formal-v1",
        "mode": "pressure_profile",
        "target_id": "pyxel_jump",
        "resource": "cpu_compute",
        "pressure_requested": 0.25,
        "repeat": 1,
        "duration_s": 60,
        "host_id": "windows-rtx4060",
    }
    data[field] = value

    with pytest.raises(ValidationError, match=field):
        RunSpec.model_validate(data)


def test_run_spec_rejects_inconsistent_explicit_id() -> None:
    with pytest.raises(ValidationError, match="run_id"):
        RunSpec(
            run_id="formal-v1__wrong__r01",
            experiment_id="formal-v1",
            mode=RunMode.SOLO,
            target_id="pyxel_jump",
            repeat=1,
            host_id="windows-rtx4060",
        )


def test_windows_paths_and_host_affinity_are_validated() -> None:
    with pytest.raises(ValidationError, match="仓库内相对路径"):
        WorkloadSpec(
            id="escape_game",
            entrypoint="..\\outside.py",
            working_directory="games\\pyxel",
            controller="escape_v1",
            seed=1,
        )
    with pytest.raises(ValidationError, match="cpu_affinity"):
        HostSpec(id="windows-host", cpu_affinity=(0, 0))


def test_metric_event_rejects_non_json_finite_values() -> None:
    valid = MetricEvent(
        run_id="formal-v1__solo__pyxel_jump__r01",
        source="workload",
        wall_time_ns=1,
        monotonic_time_ns=2,
        values={"fps": 30.0, "healthy": True},
    )
    assert '"fps":30.0' in stable_json_dumps(valid)

    with pytest.raises(ValidationError, match="NaN"):
        MetricEvent(
            run_id="formal-v1__solo__pyxel_jump__r01",
            source="workload",
            wall_time_ns=1,
            monotonic_time_ns=2,
            values={"fps": math.nan},
        )

