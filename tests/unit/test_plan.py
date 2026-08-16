from __future__ import annotations

import json
from pathlib import Path

from gaugur_lite.runner.plan import build_plan, load_plan_rows, select_balanced_triples, verify_plan


WORKLOADS = (
    "pyxel_jump",
    "pyxel_bubbles",
    "pyxel_snake",
    "pyxel_shooter",
    "pyxel_platformer",
    "daylight",
    "mega_wing",
    "space_rescue",
)


def _mini_repo(root: Path) -> tuple[Path, Path, Path]:
    (root / "README.md").write_text("# test\n", encoding="utf-8")
    games = root / "games"
    configs = root / "configs"
    games.mkdir()
    configs.mkdir()
    workload_rows = []
    for index in range(4):
        workload_id = f"game_{index}"
        (games / f"{workload_id}.py").write_text(f"# {workload_id}\n", encoding="utf-8")
        workload_rows.append(
            f'''  - id: "{workload_id}"
    driver: "pyxel_game"
    entrypoint: "games/{workload_id}.py"
    working_directory: "games"
    controller: "controller_{index}"
    seed: {index + 1}
    display_scale: 2'''
        )
    workload_path = configs / "workloads.yaml"
    workload_path.write_text(
        "schema_version: 1\nworkloads:\n"
        + "\n".join(workload_rows)
        + "\ndefaults:\n  audio_mode: muted\n  input_mode: deterministic_engine_api\n  preserve_game_logic: true\n",
        encoding="utf-8",
    )
    local_path = configs / "local.yaml"
    local_path.write_text(
        """schema_version: 1
host:
  id: test-host
  platform: windows
  gpu_index: 0
  display_index: 0
  dpi_awareness: per_monitor_v2
  window_layout: grid_2x2
  require_visible_windows: true
  cpu_affinity: null
  cooldown_s: 0
  max_gpu_temp_c: 82
measurement:
  warmup_s: 1
  duration_s: 2
  sample_interval_s: 1
  repeats: 1
  qos_ratios: [0.8]
  random_seed: 123
paths:
  raw: data/raw
  interim: data/interim
  processed: data/processed
  artifacts: artifacts
""",
        encoding="utf-8",
    )
    experiment_path = configs / "experiment.yaml"
    experiment_path.write_text(
        """schema_version: 1
name: test-exp
workload_ids: [game_0, game_1, game_2, game_3]
resources: [cpu_compute]
pressure_levels: [0.0]
repeats: 1
randomize_order: true
main_combinations:
  pairs: {mode: all, expected_count: 6}
  triples: {mode: all, expected_count: 4, seed: 123}
extra_test: {size: 4, mode: all, expected_count: 1, trainable: false}
split:
  group_by: combination_key
  seed: 123
  train_groups: 6
  validation_groups: 2
  test_groups: 2
""",
        encoding="utf-8",
    )
    return local_path, experiment_path, workload_path


def test_balanced_subset_has_exact_formal_constraints() -> None:
    selected, metadata = select_balanced_triples(WORKLOADS, seed=20260811)

    assert len(selected) == 32
    assert len(set(selected)) == 32
    assert set(metadata["workload_occurrences"].values()) == {12}
    assert metadata["objective"]["pair_cooccurrence_max"] == max(
        metadata["pair_cooccurrence"].values()
    )


def test_build_and_verify_plan_with_all_physical_stages(tmp_path: Path) -> None:
    local, experiment, workloads = _mini_repo(tmp_path)
    output = tmp_path / "artifacts" / "all.csv"

    result = build_plan(
        repo_root=tmp_path,
        local_config_path=local,
        experiment_path=experiment,
        workload_catalog_path=workloads,
        stage="all",
        output_file=output,
    )
    verified = verify_plan(repo_root=tmp_path, plan_file=output)
    rows = load_plan_rows(output)

    assert result["manifest"]["row_count"] == 19
    assert result["manifest"]["stage_counts"] == {
        "colocation-extra-test": 1,
        "colocation-main": 10,
        "profile": 4,
        "solo": 4,
    }
    assert len(rows) == len({row["run_id"] for row in rows}) == 19
    assert {row["schema_version"] for row in rows} == {"2"}
    assert all(
        row["pressure_applied"] == row["pressure_requested"]
        for row in rows
        if row["stage"] == "profile"
    )
    assert verified["status"] == "passed"
    assert result["combinations"]["checks"]["main_extra_disjoint"] is True

    output.write_text(output.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert verify_plan(repo_root=tmp_path, plan_file=output)["status"] == "failed"


def test_plan_sidecars_record_row_and_combination_hashes(tmp_path: Path) -> None:
    local, experiment, workloads = _mini_repo(tmp_path)
    output = tmp_path / "artifacts" / "extra.csv"
    build_plan(
        repo_root=tmp_path,
        local_config_path=local,
        experiment_path=experiment,
        workload_catalog_path=workloads,
        stage="colocation-extra-test",
        output_file=output,
    )
    manifest = json.loads(
        output.with_name("extra-manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["row_count"] == 1
    assert len(manifest["plan_sha256"]) == 64
    assert len(manifest["combination_manifest_sha256"]) == 64


def test_plan_records_normalized_and_capped_applied_pressure(tmp_path: Path) -> None:
    local, experiment, workloads = _mini_repo(tmp_path)
    local.write_text(
        local.read_text(encoding="utf-8").replace(
            "  random_seed: 123\n",
            "  random_seed: 123\n"
            "  pressure_caps:\n"
            "    cpu_compute: 0.25\n"
            "    memory_bandwidth: 1.0\n"
            "    gpu_compute: 1.0\n"
            "    gpu_memory: 1.0\n",
        ),
        encoding="utf-8",
    )
    experiment.write_text(
        experiment.read_text(encoding="utf-8").replace(
            "pressure_levels: [0.0]", "pressure_levels: [1.0]"
        ),
        encoding="utf-8",
    )
    output = tmp_path / "artifacts" / "profile.csv"

    build_plan(
        repo_root=tmp_path,
        local_config_path=local,
        experiment_path=experiment,
        workload_catalog_path=workloads,
        stage="profile",
        output_file=output,
    )
    rows = load_plan_rows(output)

    assert {row["pressure_requested"] for row in rows} == {"1"}
    assert {row["pressure_applied"] for row in rows} == {"0.25"}
