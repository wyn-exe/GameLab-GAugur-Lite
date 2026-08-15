from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from gaugur_lite import cli

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_help_and_version() -> None:
    help_result = runner.invoke(cli.app, ["--help"])
    version_result = runner.invoke(cli.app, ["--version"])

    assert help_result.exit_code == 0
    assert "doctor" in help_result.stdout
    assert "benchmark" in help_result.stdout
    assert "workload" in help_result.stdout
    assert "--dry-run" in help_result.stdout
    assert version_result.exit_code == 0
    assert version_result.stdout.strip() == "0.1.0"


def test_benchmark_calibrate_dry_run_does_not_create_artifacts(
    tmp_path: Path, monkeypatch: object
) -> None:
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    (tmp_path / "games").mkdir()
    output = tmp_path / "artifacts" / "calibration.json"
    config = REPO_ROOT / "configs" / "local.example.yaml"
    monkeypatch.setattr(cli, "discover_repo_root", lambda _: tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "benchmark",
            "calibrate",
            "--config",
            str(config),
            "--resources",
            "cpu_compute,gpu_memory",
            "--levels",
            "0,0.5,1",
            "--repeats",
            "2",
            "--output",
            str(output),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert '"cell_count": 12' in result.stdout
    assert not output.exists()


def test_doctor_accepts_dry_run_before_or_after_command(monkeypatch: object) -> None:
    observed: list[bool] = []

    def fake_report(config: Path, *, dry_run: bool) -> dict[str, object]:
        assert config.name == "local.example.yaml"
        observed.append(dry_run)
        return {
            "schema_version": 1,
            "status": "passed",
            "read_only": True,
            "dry_run": dry_run,
            "workload_processes_started": 0,
            "mutations_performed": [],
            "checks": [],
        }

    monkeypatch.setattr(cli, "build_doctor_report", fake_report)
    config = str(REPO_ROOT / "configs/local.example.yaml")

    before = runner.invoke(cli.app, ["--dry-run", "doctor", "--config", config])
    after = runner.invoke(cli.app, ["doctor", "--config", config, "--dry-run"])

    assert before.exit_code == 0
    assert after.exit_code == 0
    assert observed == [True, True]
    assert '"workload_processes_started": 0' in before.stdout


def test_telemetry_dry_run_does_not_create_output(tmp_path: Path) -> None:
    output = tmp_path / "probe"
    result = runner.invoke(
        cli.app,
        [
            "telemetry",
            "probe",
            "--duration",
            "1",
            "--interval",
            "0.5",
            "--output-directory",
            str(output),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert '"dry_run": true' in result.stdout
    assert not output.exists()


def test_global_dry_run_reaches_overhead_command(tmp_path: Path) -> None:
    output = tmp_path / "overhead.json"
    result = runner.invoke(
        cli.app,
        [
            "--dry-run",
            "telemetry",
            "overhead",
            "--duration",
            "2",
            "--repeats",
            "2",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert '"benchmark_kind": "synthetic_frame_loop_proxy"' in result.stdout
    assert not output.exists()


def test_workload_list_and_smoke_dry_run_do_not_start_child(tmp_path: Path) -> None:
    listed = runner.invoke(cli.app, ["workload", "list"])
    output = tmp_path / "must-not-exist"
    planned = runner.invoke(
        cli.app,
        [
            "workload",
            "smoke",
            "pyxel_jump",
            "--duration",
            "1",
            "--output-directory",
            str(output),
            "--dry-run",
        ],
    )

    assert listed.exit_code == 0
    assert '"count": 8' in listed.stdout
    assert planned.exit_code == 0
    assert '"child_processes_planned": 1' in planned.stdout
    assert not output.exists()
