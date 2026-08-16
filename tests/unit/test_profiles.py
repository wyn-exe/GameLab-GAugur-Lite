from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gaugur_lite import profiles
from gaugur_lite.profiles import (
    ProfileError,
    _aggregate,
    _load_standalone_benchmarks,
    _verify_safety_v2_profile_amendment,
    _verify_short_profile_amendment,
    _verify_thermal_profile_amendment,
    build_profiles,
    compute_profiles,
    verify_profiles,
)


def _amendment_rows(
    *,
    limit: int,
    directory_prefix: str,
    config: str,
    warmup: int = 20,
    duration: int = 60,
    cooldown: int = 20,
    gpu_compute_cap: float | None = None,
) -> list[dict[str, str]]:
    rows = []
    resources = ("cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory")
    pressures = (0.0, 0.25, 0.5, 0.75, 1.0)
    for index in range(480):
        resource = resources[(index // 5) % 4]
        pressure = pressures[index % 5]
        row = {
                "schema_version": "2" if gpu_compute_cap is not None else "1",
                "execution_index": str(index + 1),
                "run_id": f"formal-v1__profile__cell_{index:03d}",
                "experiment_id": "formal-v1",
                "stage": "profile",
                "resource": resource,
                "pressure_requested": str(pressure),
                "warmup_s": str(warmup),
                "duration_s": str(duration),
                "sample_interval_s": "1",
                "cooldown_s": str(cooldown),
                "max_gpu_temp_c": str(limit),
                "config_sha256": config * 64,
                "root_commit": config * 40,
                "run_directory": f"{directory_prefix}/{index:03d}",
                "row_sha256": f"{index + (1 if limit == 82 else 1000):064x}",
            }
        if gpu_compute_cap is not None:
            cap = gpu_compute_cap if resource == "gpu_compute" else 1.0
            row["pressure_applied"] = str(pressure * cap)
        rows.append(row)
    return rows


def test_thermal_amendment_accepts_only_identity_fields_and_t84(
    tmp_path: Path, monkeypatch: object
) -> None:
    original = tmp_path / "formal-v1.csv"
    amended = tmp_path / "formal-v1-profile-t84.csv"
    original.write_text("placeholder\n", encoding="utf-8")
    amended.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-v1-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False, "selected_stage": "all", "row_count": 720}),
        encoding="utf-8",
    )
    (tmp_path / "formal-v1-profile-t84-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False, "selected_stage": "profile", "row_count": 480}),
        encoding="utf-8",
    )
    rows = {
        original: _amendment_rows(limit=82, directory_prefix="data/raw/formal-v1", config="a"),
        amended: _amendment_rows(limit=84, directory_prefix="data/raw/step7-t84/formal-v1", config="b"),
    }
    monkeypatch.setattr(profiles, "load_plan_rows", lambda path: rows[path])

    result = _verify_thermal_profile_amendment(
        repo_root=tmp_path,
        profile_plan_file=amended,
        baseline_plan_file=original,
        profile_plan_sha256="c" * 64,
        baseline_plan_sha256="d" * 64,
    )

    assert result["semantic_fields_equal"] is True
    assert result["raw_directories_disjoint"] is True
    assert result["profile_max_gpu_temp_c"] == 84


def test_thermal_amendment_rejects_semantic_measurement_change(
    tmp_path: Path, monkeypatch: object
) -> None:
    original = tmp_path / "formal-v1.csv"
    amended = tmp_path / "formal-v1-profile-t84.csv"
    original.write_text("placeholder\n", encoding="utf-8")
    amended.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-v1-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False}), encoding="utf-8"
    )
    (tmp_path / "formal-v1-profile-t84-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False, "selected_stage": "profile", "row_count": 480}),
        encoding="utf-8",
    )
    original_rows = _amendment_rows(limit=82, directory_prefix="data/raw/formal-v1", config="a")
    amended_rows = _amendment_rows(
        limit=84, directory_prefix="data/raw/step7-t84/formal-v1", config="b"
    )
    amended_rows[0]["duration_s"] = "59"
    rows = {original: original_rows, amended: amended_rows}
    monkeypatch.setattr(profiles, "load_plan_rows", lambda path: rows[path])

    with pytest.raises(ProfileError, match="未声明字段"):
        _verify_thermal_profile_amendment(
            repo_root=tmp_path,
            profile_plan_file=amended,
            baseline_plan_file=original,
            profile_plan_sha256="c" * 64,
            baseline_plan_sha256="d" * 64,
        )


def test_short_amendment_accepts_exact_10_30_10_protocol(
    tmp_path: Path, monkeypatch: object
) -> None:
    original = tmp_path / "formal-v1.csv"
    amended = tmp_path / "formal-v1-profile-s30.csv"
    original.write_text("placeholder\n", encoding="utf-8")
    amended.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-v1-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False, "selected_stage": "all", "row_count": 720}),
        encoding="utf-8",
    )
    (tmp_path / "formal-v1-profile-s30-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False, "selected_stage": "all", "row_count": 720}),
        encoding="utf-8",
    )
    rows = {
        original: _amendment_rows(
            limit=82, directory_prefix="data/raw/formal-v1", config="a"
        ),
        amended: _amendment_rows(
            limit=84,
            directory_prefix="data/raw/remaining-s30/formal-v1",
            config="b",
            warmup=10,
            duration=30,
            cooldown=10,
        ),
    }
    monkeypatch.setattr(profiles, "load_plan_rows", lambda path: rows[path])

    result = _verify_short_profile_amendment(
        repo_root=tmp_path,
        profile_plan_file=amended,
        baseline_plan_file=original,
        profile_plan_sha256="c" * 64,
        baseline_plan_sha256="d" * 64,
    )

    assert result["mode"] == "short_profile_amendment_s30_v2"
    assert result["profile_protocol"] == {
        "warmup_s": 10.0,
        "duration_s": 30.0,
        "cooldown_s": 10.0,
        "max_gpu_temp_c": 84.0,
    }
    assert result["semantic_fields_equal_except_timing"] is True


def test_short_amendment_rejects_any_unlisted_change(
    tmp_path: Path, monkeypatch: object
) -> None:
    original = tmp_path / "formal-v1.csv"
    amended = tmp_path / "formal-v1-profile-s30.csv"
    original.write_text("placeholder\n", encoding="utf-8")
    amended.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-v1-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False}), encoding="utf-8"
    )
    (tmp_path / "formal-v1-profile-s30-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False, "selected_stage": "all", "row_count": 720}),
        encoding="utf-8",
    )
    original_rows = _amendment_rows(
        limit=82, directory_prefix="data/raw/formal-v1", config="a"
    )
    amended_rows = _amendment_rows(
        limit=84,
        directory_prefix="data/raw/remaining-s30/formal-v1",
        config="b",
        warmup=10,
        duration=30,
        cooldown=10,
    )
    amended_rows[0]["sample_interval_s"] = "2"
    rows = {original: original_rows, amended: amended_rows}
    monkeypatch.setattr(profiles, "load_plan_rows", lambda path: rows[path])

    with pytest.raises(ProfileError, match="未声明字段"):
        _verify_short_profile_amendment(
            repo_root=tmp_path,
            profile_plan_file=amended,
            baseline_plan_file=original,
            profile_plan_sha256="c" * 64,
            baseline_plan_sha256="d" * 64,
        )


def test_safety_v2_amendment_caps_only_gpu_compute(
    tmp_path: Path, monkeypatch: object
) -> None:
    original = tmp_path / "formal-v1.csv"
    amended = tmp_path / "formal-v1-safety-v2-s30.csv"
    original.write_text("placeholder\n", encoding="utf-8")
    amended.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-v1-safety-v2-s30-manifest.json").write_text(
        json.dumps(
            {"root_dirty_at_generation": False, "selected_stage": "all", "row_count": 720}
        ),
        encoding="utf-8",
    )
    rows = {
        original: _amendment_rows(
            limit=82, directory_prefix="data/raw/formal-v1", config="a"
        ),
        amended: _amendment_rows(
            limit=80,
            directory_prefix="data/raw/safety-v2-s30/formal-v1",
            config="b",
            warmup=10,
            duration=30,
            cooldown=10,
            gpu_compute_cap=0.25,
        ),
    }
    monkeypatch.setattr(profiles, "load_plan_rows", lambda path: rows[path])

    result = _verify_safety_v2_profile_amendment(
        repo_root=tmp_path,
        profile_plan_file=amended,
        baseline_plan_file=original,
        profile_plan_sha256="c" * 64,
        baseline_plan_sha256="d" * 64,
    )

    assert result["mode"] == "safety_v2_capped_gpu_compute"
    assert result["pressure_caps"]["gpu_compute"] == 0.25
    assert result["profile_protocol"]["max_gpu_temp_c"] == 80.0


def _calibration_payload(
    *, unstable: bool = False, gpu_compute_cap: float = 1.0
) -> dict[str, object]:
    runs = []
    for resource in ("cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory"):
        for pressure in (0.0, 0.25, 0.5, 0.75, 1.0):
            for repeat in (1, 2, 3):
                cap = gpu_compute_cap if resource == "gpu_compute" else 1.0
                applied = pressure * cap
                operations = 0 if applied == 0 else int(1_000_000 * applied)
                if unstable and resource == "cpu_compute" and pressure == 0.25 and repeat == 3:
                    operations *= 2
                runs.append(
                    {
                        "resource": resource,
                        "pressure_requested": pressure,
                        "pressure_applied": applied,
                        "repeat": repeat,
                        "run_key": f"{resource}-{pressure}-{repeat}",
                        "observed_pressure": applied,
                        "worker": {
                            "status": "completed",
                            "resource": resource,
                            "pressure_requested": applied,
                            "elapsed_s": 2.0,
                            "operations": operations,
                        },
                    }
                )
    return {
        "status": "passed",
        "cell_count": 60,
        "request": {
            "cpu_workers": 8,
            "gpu_index": 0,
            "gpu_matrix_size": 1024,
            "gpu_memory_max_mib": 1024,
            "memory_buffer_mib": 64,
            "levels": [0.0, 0.25, 0.5, 0.75, 1.0],
            "repeats": 3,
            "resources": ["cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory"],
            "pressure_caps": {
                "cpu_compute": 1.0,
                "memory_bandwidth": 1.0,
                "gpu_compute": gpu_compute_cap,
                "gpu_memory": 1.0,
            },
        },
        "runs": runs,
    }


def test_standalone_benchmark_uses_operations_per_elapsed_second(tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(_calibration_payload()), encoding="utf-8")

    cells, _ = _load_standalone_benchmarks(path=path, cv_threshold_pct=5.0)

    assert cells[("cpu_compute", 0.5)]["throughput_mean_ops_per_s"] == 250_000
    assert cells[("cpu_compute", 0.5)]["throughput_cv_pct"] == 0
    assert cells[("gpu_memory", 0.0)]["throughput_mean_ops_per_s"] is None


def test_standalone_benchmark_rejects_unstable_denominator(tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(_calibration_payload(unstable=True)), encoding="utf-8")

    with pytest.raises(ProfileError, match="吞吐 CV 超限"):
        _load_standalone_benchmarks(path=path, cv_threshold_pct=5.0)


def test_standalone_benchmark_requires_matching_safety_cap(tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(_calibration_payload(gpu_compute_cap=0.25)), encoding="utf-8"
    )
    caps = {
        "cpu_compute": 1.0,
        "memory_bandwidth": 1.0,
        "gpu_compute": 0.25,
        "gpu_memory": 1.0,
    }

    cells, _ = _load_standalone_benchmarks(
        path=path, cv_threshold_pct=5.0, expected_pressure_caps=caps
    )

    assert cells[("gpu_compute", 1.0)]["pressure_applied"] == 0.25
    with pytest.raises(ProfileError, match="pressure_caps 不兼容"):
        _load_standalone_benchmarks(path=path, cv_threshold_pct=5.0)


def test_aggregate_keeps_sensitivity_and_slowdown_as_distinct_metrics() -> None:
    rows = []
    for pressure in (0.0, 0.25, 0.5, 0.75, 1.0):
        for repeat in (1, 2, 3):
            slowdown = None if pressure == 0 else 1.0 + pressure
            rows.append(
                {
                    "schema_version": 1,
                    "experiment_id": "formal-v1",
                    "workload_id": "game",
                    "resource": "cpu_compute",
                    "pressure_requested": pressure,
                    "pressure_observed": pressure,
                    "repeat": repeat,
                    "run_id": f"run-{pressure}-{repeat}",
                    "sensitivity_mean_fps": 1.0 - pressure * pressure * 0.2,
                    "sensitivity_p05_fps": 0.99 - pressure * pressure * 0.2,
                    "intensity_slowdown": slowdown,
                    "benchmark_throughput_retention": None if slowdown is None else 1 / slowdown,
                    "benchmark_throughput_colocated_ops_per_s": None if slowdown is None else 100 / slowdown,
                    "benchmark_throughput_solo_ops_per_s": None if slowdown is None else 100,
                    "solo_baseline_id": "baseline",
                    "solo_mean_fps": 30.0,
                    "standalone_benchmark_id": "benchmark" if pressure else None,
                    "hardware_signal_name": "cpu_util_pct",
                    "hardware_signal_mean": 25.0,
                }
            )

    aggregates, curves, analysis = _aggregate(rows)

    assert len(aggregates) == 5
    assert len(curves) == 1
    assert curves[0]["intensity_slowdown"] == pytest.approx(1.625)
    assert curves[0]["max_abs_nonlinear_deviation"] == pytest.approx(0.05)
    assert aggregates[-1]["sensitivity_mean"] == pytest.approx(0.8)
    assert aggregates[-1]["intensity_slowdown_mean"] == pytest.approx(2.0)
    assert analysis["curve_count"] == 1


def test_compute_profiles_builds_full_formal_cartesian_product(
    tmp_path: Path, monkeypatch: object
) -> None:
    workloads = [f"game_{index}" for index in range(8)]
    resources = ("cpu_compute", "memory_bandwidth", "gpu_compute", "gpu_memory")
    pressures = (0.0, 0.25, 0.5, 0.75, 1.0)
    rows = []
    execution_index = 1
    for workload in workloads:
        for resource in resources:
            for pressure in pressures:
                for repeat in (1, 2, 3):
                    rows.append(
                        {
                            "execution_index": str(execution_index),
                            "run_id": f"formal-v1__profile__{workload}__{resource}__{pressure}__r{repeat}",
                            "experiment_id": "formal-v1",
                            "stage": "profile",
                            "split": "not_applicable",
                            "mode": "pressure_profile",
                            "workload_ids": json.dumps([workload]),
                            "target_id": workload,
                            "neighbor_ids": "[]",
                            "resource": resource,
                            "pressure_requested": str(pressure),
                            "repeat": str(repeat),
                            "warmup_s": "20",
                            "duration_s": "60",
                            "sample_interval_s": "1",
                            "cooldown_s": "20",
                            "gpu_index": "0",
                            "display_index": "0",
                            "window_layout": "grid_2x2",
                            "require_visible_windows": "true",
                            "max_gpu_temp_c": "82",
                            "config_sha256": "a" * 64,
                            "run_directory": f"data/raw/formal-v1/{execution_index}",
                            "row_sha256": f"{execution_index:064x}"[-64:],
                        }
                    )
                    execution_index += 1
    plan = tmp_path / "formal-v1.csv"
    plan.write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "formal-v1-manifest.json").write_text(
        json.dumps({"root_dirty_at_generation": False}), encoding="utf-8"
    )
    solo = tmp_path / "solo.json"
    solo.write_text(
        json.dumps(
            {
                "status": "passed",
                "plan": {"sha256": "b" * 64},
                "execution": {"source_tree_sha256s": ["s" * 64]},
                "baselines": [
                    {
                        "workload_id": workload,
                        "baseline_id": f"baseline-{workload}",
                        "valid_for_retention": True,
                        "mean_fps": 30.0,
                        "p05_fps": 29.0,
                    }
                    for workload in workloads
                ],
            }
        ),
        encoding="utf-8",
    )
    calibration = tmp_path / "calibration.json"
    calibration.write_text(json.dumps(_calibration_payload()), encoding="utf-8")
    monkeypatch.setattr(profiles, "load_plan_rows", lambda _: rows)
    monkeypatch.setattr(
        profiles,
        "verify_plan",
        lambda **_: {"status": "passed", "plan_sha256": "b" * 64},
    )

    def fake_collect(*, row: object, **_: object) -> dict[str, object]:
        pressure = float(row.pressure_requested)
        standalone = 500_000 * pressure
        return {
            "schema_version": 1,
            "experiment_id": "formal-v1",
            "workload_id": row.workload_ids[0],
            "resource": row.resource,
            "pressure_requested": pressure,
            "pressure_observed": pressure,
            "repeat": row.repeat,
            "run_id": row.run_id,
            "row_sha256": row.row_sha256,
            "attempt": 1,
            "attempt_directory": f"data/raw/{row.run_id}/attempts/a001",
            "summary_sha256": "d" * 64,
            "execution_root_commit": "1" * 40,
            "execution_root_dirty": True,
            "execution_source_tree_sha256": "e" * 64,
            "mean_fps": 30 * (1 - 0.1 * pressure**2),
            "p05_fps": 29 * (1 - 0.1 * pressure**2),
            "min_fps": 28.0,
            "measurement_coverage_ratio": 1.0,
            "system_coverage_ratio": 1.0,
            "workload_overlap_ratio": 1.0,
            "gpu_temp_c_max": 70.0,
            "missed_deadline_count": 0,
            "benchmark_operations": int(standalone * 60 / 1.1),
            "benchmark_elapsed_s": 60.0,
            "benchmark_active_fraction": pressure,
            "benchmark_throughput_colocated_ops_per_s": None if pressure == 0 else standalone / 1.1,
            "hardware_signal_name": "cpu_util_pct",
            "hardware_signal_mean": 20.0,
        }

    monkeypatch.setattr(profiles, "_collect_profile_record", fake_collect)

    result, records, aggregates = compute_profiles(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=solo,
        calibration_file=calibration,
    )

    assert result["status"] == "passed"
    assert len(records) == 480
    assert len(aggregates) == 160
    assert len(result["curves"]) == 32
    assert result["analysis"]["max_abs_nonlinear_deviation"] == pytest.approx(0.025)
    assert all(item["intensity_slowdown"] == pytest.approx(1.1) for item in result["curves"])

    # 用同一套完整合成结果走真实 Parquet/JSONL/PNG 写入与独立复核路径。
    core = copy.deepcopy(result)
    monkeypatch.setattr(
        profiles,
        "compute_profiles",
        lambda **_: (copy.deepcopy(core), copy.deepcopy(records), copy.deepcopy(aggregates)),
    )
    parquet = tmp_path / "output/profiles.parquet"
    runs = tmp_path / "output/profile-runs.jsonl"
    summary = tmp_path / "output/profile-summary.json"
    plots = tmp_path / "output/plots"
    built = build_profiles(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=solo,
        calibration_file=calibration,
        output_file=parquet,
        runs_output_file=runs,
        summary_file=summary,
        plot_dir=plots,
    )
    verified = verify_profiles(
        repo_root=tmp_path,
        plan_file=plan,
        solo_baselines_file=solo,
        calibration_file=calibration,
        profiles_file=parquet,
        runs_file=runs,
        summary_file=summary,
        plot_dir=plots,
    )

    assert built["artifacts"]["profiles_parquet_sha256"]
    assert verified["status"] == "passed"
    assert verified["passed_count"] == verified["check_count"] == 12
