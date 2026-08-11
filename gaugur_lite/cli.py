"""GAugur-Lite 统一命令行入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from . import __version__
from .config import ConfigError, stable_json_dumps
from .doctor import build_doctor_report


@dataclass(frozen=True)
class CliState:
    dry_run: bool = False


app = typer.Typer(
    name="gaugur-lite",
    help="GAugur 轻量复现实验 CLI（Windows + Conda）。",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="只输出计划或诊断，不执行可变操作。",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="显示版本并退出。",
    ),
) -> None:
    """保存全局执行策略，供后续实验子命令统一复用。"""

    del version
    ctx.obj = CliState(dry_run=dry_run)


@app.command()
def doctor(
    ctx: typer.Context,
    config: Path = typer.Option(
        Path("configs/local.example.yaml"),
        "--config",
        help="主机 YAML 配置。",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="可放在子命令后的等价 dry-run 开关。",
    ),
) -> None:
    """只读检查配置、依赖和 GPU；不会启动 workload。"""

    state = ctx.ensure_object(CliState)
    effective_dry_run = state.dry_run or dry_run
    try:
        report = build_doctor_report(config, dry_run=effective_dry_run)
    except ConfigError as exc:
        typer.echo(f"CONFIG_ERROR: {exc}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo(stable_json_dumps(report, indent=2))
    if report["status"] != "passed":
        raise typer.Exit(code=1)

