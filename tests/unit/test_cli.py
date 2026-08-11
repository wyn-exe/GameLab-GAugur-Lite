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
    assert "--dry-run" in help_result.stdout
    assert version_result.exit_code == 0
    assert version_result.stdout.strip() == "0.1.0"


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

