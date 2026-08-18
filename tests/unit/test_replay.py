from __future__ import annotations

import pytest

from gaugur_lite.replay import ReplayError, _load_requests, _pack_requests, _truth_group_feasible


class _FakeEvaluator:
    def __init__(self, predicted: dict[tuple[str, ...], bool], actual: dict[tuple[str, ...], tuple[bool, int, int]]) -> None:
        self.predicted = predicted
        self.actual_values = actual

    def predicted_feasible(self, group: list[str], strategy: str) -> bool | None:
        del strategy
        return self.predicted.get(tuple(group))

    def actual(self, group: list[str]) -> tuple[bool, int, int]:
        if len(group) == 1:
            return True, 0, 1
        return self.actual_values.get(tuple(group), (False, len(group), len(group)))


def _requests(*workloads: str) -> list[dict[str, object]]:
    return [
        {"request_id": f"req-{index}", "workload_id": workload, "qos_ratio": 0.8}
        for index, workload in enumerate(workloads, start=1)
    ]


def test_pack_requests_prefers_largest_predicted_feasible_group_and_falls_back_to_singleton() -> None:
    evaluator = _FakeEvaluator(
        predicted={("a", "b"): True, ("a", "b", "c"): False, ("c", "d"): True},
        actual={("a", "b"): (True, 0, 6), ("c",): (True, 0, 1)},
    )
    result = _pack_requests(_requests("a", "b", "c"), evaluator=evaluator, strategy="fake", max_group_size=4)

    assert result["slot_count"] == 2
    assert result["slots"][0]["workload_ids"] == ["a", "b"]
    assert result["slots"][1]["workload_ids"] == ["c"]
    assert result["actual_qos_violation_rate"] == 0.0


def test_pack_requests_reports_measured_truth_violation_rate() -> None:
    evaluator = _FakeEvaluator(
        predicted={("a", "b"): True},
        actual={("a", "b"): (False, 1, 2)},
    )
    result = _pack_requests(_requests("a", "b"), evaluator=evaluator, strategy="fake", max_group_size=4)

    assert result["slot_count"] == 1
    assert result["actual_qos_violation_count"] == 1
    assert result["actual_qos_observation_count"] == 2
    assert result["actual_qos_violation_rate"] == 0.5


def test_truth_group_feasible_uses_each_target_qos_and_all_repeats() -> None:
    truth = {
        ("a+b", "a"): [
            {"mean_fps": 80.0, "solo_mean_fps": 100.0, "repeat": 1},
            {"mean_fps": 79.0, "solo_mean_fps": 100.0, "repeat": 2},
        ],
        ("a+b", "b"): [
            {"mean_fps": 90.0, "solo_mean_fps": 100.0, "repeat": 1},
            {"mean_fps": 90.0, "solo_mean_fps": 100.0, "repeat": 2},
        ],
    }

    feasible, violations, observations = _truth_group_feasible(
        ["a", "b"], truth=truth, qos_by_workload={"a": 0.8, "b": 0.9}
    )
    assert (feasible, violations, observations) == (False, 1, 4)


def test_load_requests_rejects_duplicate_workloads() -> None:
    spec = {"qos_ratio": 0.8, "requests": [{"request_id": "1", "workload_id": "a"}, {"request_id": "2", "workload_id": "a"}]}
    with pytest.raises(ReplayError):
        _load_requests_from_mapping(spec)


def _load_requests_from_mapping(spec: dict[str, object]) -> None:
    # 通过临时 YAML 走真实解析路径，避免绕过 request schema 校验。
    import tempfile
    from pathlib import Path

    import yaml

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "requests.yaml"
        path.write_text(yaml.safe_dump(spec), encoding="utf-8")
        _load_requests(path, {"a", "b"}, None)
