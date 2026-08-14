"""GAugur-Lite 统一命令行入口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from . import __version__
from .config import ConfigError, discover_repo_root, stable_json_dumps
from .doctor import build_doctor_report
from .metrics.telemetry import format_result, run_overhead, run_probe
from .metrics.writer import write_json_atomic
from .workloads.launcher import build_step3_acceptance, launch_smoke
from .workloads.pyxel_game import GameRunConfig, execute_game_child
from .workloads.registry import GAME_REGISTRY, get_game, verify_upstream


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
workload_app = typer.Typer(
    name="workload",
    help="八个真实 Pyxel 游戏的校验、运行与验收。",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(telemetry_app, name="telemetry")
app.add_typer(workload_app, name="workload")


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


@workload_app.command("list")
def workload_list() -> None:
    """列出注册表中的八个正式 workload。"""

    typer.echo(
        stable_json_dumps(
            {
                "schema_version": 1,
                "count": len(GAME_REGISTRY),
                "workloads": [game.public_dict() for game in GAME_REGISTRY],
            },
            indent=2,
        )
    )


@workload_app.command("verify-upstream")
def workload_verify_upstream(
    root: Path = typer.Option(
        Path("games/pyxel"),
        "--root",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Pyxel 上游副本目录。",
    ),
    output: Path | None = typer.Option(None, "--output", help="可选的独占验收 JSON 文件。"),
) -> None:
    """校验原始 manifest、运行入口、资源、app bundle 与解包源码树。"""

    try:
        repo_root = root.resolve().parents[1]
        result = verify_upstream(repo_root, root.resolve())
        if output is not None:
            if output.exists():
                raise FileExistsError(f"输出文件已存在，拒绝覆盖: {output}")
            write_json_atomic(output, result)
    except (ConfigError, FileExistsError, OSError, ValueError) as exc:
        typer.echo(f"WORKLOAD_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(stable_json_dumps(result, indent=2))
    if result["status"] != "passed":
        raise typer.Exit(code=3)


@workload_app.command("smoke")
def workload_smoke(
    ctx: typer.Context,
    workload_id: str = typer.Argument(..., help="注册表 workload ID。"),
    duration: float = typer.Option(30.0, "--duration", min=1.0, help="计划测量秒数。"),
    max_frames: int = typer.Option(0, "--max-frames", min=0, help="大于 0 时按固定 draw 数停止。"),
    repeat: int = typer.Option(1, "--repeat", min=1, max=100, help="重复编号。"),
    output_directory: Path | None = typer.Option(None, "--output-directory", help="独占创建的 run 目录。"),
    headless: bool = typer.Option(False, "--headless", help="仅供自动测试；正式验收禁止使用。"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只输出子进程与产物计划。"),
) -> None:
    """在受 watchdog 管理的独立子进程中运行一个真实游戏。"""

    try:
        game = get_game(workload_id)
        repo_root = discover_repo_root(Path.cwd())
        output = output_directory or (
            repo_root / "artifacts" / "workloads" / "step3" / "smoke" / game.id / f"r{repeat:02d}"
        )
        resolved_output = output.resolve()
        try:
            displayed_output = resolved_output.relative_to(repo_root).as_posix()
        except ValueError:
            # dry-run 允许展示仓库外路径；真实运行仍由 launcher 拒绝越界写入。
            displayed_output = resolved_output.as_posix()
        plan = {
            "command": "workload smoke",
            "dry_run": _effective_dry_run(ctx, dry_run),
            "workload_id": game.id,
            "duration_s": duration,
            "max_frames": max_frames,
            "repeat": repeat,
            "headless": headless,
            "output_directory": displayed_output,
            "child_processes_planned": 1,
            "mutations_planned": [
                "game_metrics.jsonl",
                "ready.json",
                "heartbeat.json",
                "stop.json",
                "status.json",
                "summary.json",
                "launcher.json",
                "stdout.log",
                "stderr.log",
            ],
        }
        if plan["dry_run"]:
            typer.echo(stable_json_dumps(plan, indent=2))
            return
        result = launch_smoke(
            repo_root=repo_root,
            game=game,
            duration_s=duration,
            max_frames=max_frames,
            repeat=repeat,
            output_directory=output,
            headless=headless,
        )
    except (ConfigError, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"WORKLOAD_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(stable_json_dumps(result, indent=2))
    if result["launcher"]["status"] != "completed":
        raise typer.Exit(code=3)


@workload_app.command("accept")
def workload_accept(
    input_root: Path = typer.Option(
        Path("artifacts/workloads/step3/formal"),
        "--input-root",
        help="布局为 <workload>/rXX/summary.json 的正式 run 根目录。",
    ),
    expected_repeats: int = typer.Option(3, "--expected-repeats", min=2, max=20),
    output: Path = typer.Option(
        Path("artifacts/workloads/step3/acceptance.json"),
        "--output",
        help="独占创建的汇总验收 JSON。",
    ),
) -> None:
    """核对 8×3 可见窗口 run、轨迹一致性和 FPS 变异系数。"""

    try:
        if output.exists():
            raise FileExistsError(f"输出文件已存在，拒绝覆盖: {output}")
        repo_root = discover_repo_root(Path.cwd())
        result = build_step3_acceptance(
            repo_root=repo_root,
            input_root=input_root,
            expected_repeats=expected_repeats,
        )
        write_json_atomic(output, result)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"WORKLOAD_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(stable_json_dumps(result, indent=2))
    if result["status"] != "passed":
        raise typer.Exit(code=3)


@workload_app.command("_execute", hidden=True)
def workload_execute_child(
    workload_id: str = typer.Option(..., "--workload-id"),
    duration: float = typer.Option(..., "--duration", min=1.0),
    max_frames: int = typer.Option(..., "--max-frames", min=0),
    repeat: int = typer.Option(..., "--repeat", min=1),
    output_directory: Path = typer.Option(..., "--output-directory"),
    headless: bool = typer.Option(False, "--headless"),
) -> None:
    """launcher 专用子进程入口。"""

    game = get_game(workload_id)
    repo_root = discover_repo_root(Path.cwd())
    resolved_output = output_directory.resolve()
    if repo_root not in resolved_output.parents:
        raise typer.BadParameter("子进程输出目录必须位于仓库内", param_hint="--output-directory")
    run_id = f"step3-smoke-{game.id}-r{repeat:02d}"
    try:
        result = execute_game_child(
            repo_root=repo_root,
            game=game,
            config=GameRunConfig(
                run_id=run_id,
                duration_s=duration,
                max_frames=max_frames,
                headless=headless,
                audio_mode="muted",
            ),
            output_directory=resolved_output,
        )
    except BaseException as exc:
        typer.echo(f"CHILD_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if result["status"] != "completed":
        raise typer.Exit(code=4)
