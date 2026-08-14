"""GAugur-Lite 统一命令行入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from . import __version__
from .config import ConfigError, stable_json_dumps
from .doctor import build_doctor_report
from .metrics.telemetry import format_result, run_overhead, run_probe


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
telemetry_app = typer.Typer(
    name="telemetry",
    help="结构化系统遥测与采样器开销量化。",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(telemetry_app, name="telemetry")


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


def _effective_dry_run(ctx: typer.Context, local_dry_run: bool) -> bool:
    state = ctx.ensure_object(CliState)
    return state.dry_run or local_dry_run


@telemetry_app.command("probe")
def telemetry_probe(
    ctx: typer.Context,
    duration: float = typer.Option(60.0, "--duration", min=0.1, help="采样总时长（秒）。"),
    interval: float = typer.Option(1.0, "--interval", min=0.05, help="目标采样间隔（秒）。"),
    gpu_index: int = typer.Option(0, "--gpu-index", min=0, help="NVML GPU 编号。"),
    output_directory: Path = typer.Option(
        Path("artifacts/telemetry/step2/probe"),
        "--output-directory",
        help="结构化产物目录。",
    ),
    batch_size: int = typer.Option(10, "--batch-size", min=1, help="JSONL 批量 flush 行数。"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅输出计划，不创建文件或初始化 NVML。"),
) -> None:
    """采集 CPU、内存、进程与 GPU/NVML 系统指标。"""

    if interval > duration:
        raise typer.BadParameter("--interval 不得大于 --duration", param_hint="--interval")
    plan = {
        "command": "telemetry probe",
        "dry_run": _effective_dry_run(ctx, dry_run),
        "duration_s": duration,
        "interval_s": interval,
        "gpu_index": gpu_index,
        "batch_size": batch_size,
        "output_directory": output_directory.as_posix(),
        "mutations_planned": [
            "system_metrics.jsonl",
            "status.json",
            "summary.json",
        ],
    }
    if plan["dry_run"]:
        typer.echo(stable_json_dumps(plan, indent=2))
        return
    try:
        result = run_probe(
            duration_s=duration,
            interval_s=interval,
            gpu_index=gpu_index,
            output_directory=output_directory,
            batch_size=batch_size,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"TELEMETRY_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(format_result(result))


@telemetry_app.command("overhead")
def telemetry_overhead(
    ctx: typer.Context,
    duration: float = typer.Option(120.0, "--duration", min=1.0, help="全部配对阶段总时长（秒）。"),
    interval: float = typer.Option(1.0, "--interval", min=0.05, help="有采样阶段的间隔（秒）。"),
    gpu_index: int = typer.Option(0, "--gpu-index", min=0, help="NVML GPU 编号。"),
    repeats: int = typer.Option(4, "--repeats", min=2, max=20, help="配对重复数。"),
    work_iterations: int = typer.Option(
        20_000,
        "--work-iterations",
        min=100,
        help="每个 proxy frame 的确定性计算量。",
    ),
    output: Path = typer.Option(
        Path("artifacts/telemetry/step2/overhead.json"),
        "--output",
        help="开销结果 JSON。",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅输出计划，不运行 proxy 或初始化 NVML。"),
) -> None:
    """以配对合成帧循环量化采样器开销；结果不是实际 game_fps。"""

    plan = {
        "command": "telemetry overhead",
        "dry_run": _effective_dry_run(ctx, dry_run),
        "duration_s": duration,
        "phase_duration_s": duration / (repeats * 2),
        "interval_s": interval,
        "gpu_index": gpu_index,
        "repeats": repeats,
        "work_iterations": work_iterations,
        "output": output.as_posix(),
        "benchmark_kind": "synthetic_frame_loop_proxy",
        "mutations_planned": [
            output.name,
            f"{output.stem}-metrics.jsonl",
            f"{output.stem}-status.json",
        ],
    }
    if plan["dry_run"]:
        typer.echo(stable_json_dumps(plan, indent=2))
        return
    try:
        result = run_overhead(
            duration_s=duration,
            interval_s=interval,
            gpu_index=gpu_index,
            output_file=output,
            repeats=repeats,
            work_iterations=work_iterations,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"TELEMETRY_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(format_result(result))
    if result["status"] != "passed":
        raise typer.Exit(code=3)
