"""GAugur-Lite 统一命令行入口。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import typer

from . import __version__
from .benchmarks.calibration import (
    CalibrationRequest,
    format_calibration_result,
    run_calibration,
    verify_calibration,
)
from .benchmarks.engine import BENCHMARK_RESOURCES, BenchmarkWorkerConfig, run_benchmark_worker, worker_result_text
from .config import (
    ConfigError,
    discover_repo_root,
    load_experiment_config,
    load_local_config,
    stable_json_dumps,
)
from .doctor import build_doctor_report
from .metrics.telemetry import format_result, run_overhead, run_probe
from .metrics.writer import write_json_atomic
from .runner.plan import PLAN_STAGES, build_plan, verify_plan
from .runner.runner import run_plan
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
benchmark_app = typer.Typer(
    name="benchmark",
    help="四类资源压力 benchmark 的独占校准与只读复核。",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(telemetry_app, name="telemetry")
app.add_typer(workload_app, name="workload")
app.add_typer(benchmark_app, name="benchmark")


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


def _parse_csv_strings(value: str, *, option_name: str) -> tuple[str, ...]:
    parsed = tuple(part.strip() for part in value.split(",") if part.strip())
    if not parsed:
        raise ValueError(f"{option_name} 不能为空")
    return parsed


def _parse_csv_floats(value: str, *, option_name: str) -> tuple[float, ...]:
    try:
        return tuple(float(part) for part in _parse_csv_strings(value, option_name=option_name))
    except ValueError as exc:
        raise ValueError(f"{option_name} 必须是逗号分隔的浮点数") from exc


def _calibration_paths(
    output: Path,
    metrics_output: Path | None,
    status_output: Path | None,
    workers_directory: Path | None,
    plot: Path | None,
) -> tuple[Path, Path, Path, Path, Path | None]:
    stem = output.with_suffix("")
    metrics = metrics_output or output.with_name(f"{stem.name}-metrics.jsonl")
    status = status_output or output.with_name(f"{stem.name}-status.json")
    workers = workers_directory or output.with_name(f"{stem.name}-workers")
    return output, metrics, status, workers, plot


@app.command("plan")
def experiment_plan(
    ctx: typer.Context,
    experiment: Path = typer.Option(
        ...,
        "--experiment",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="实验 YAML。",
    ),
    stage: str = typer.Option("all", "--stage", help="solo/profile/colocation-main/colocation-extra-test/all。"),
    output: Path = typer.Option(..., "--out", help="独占创建的不可变 CSV。"),
    config: Path = typer.Option(
        Path("configs/local.example.yaml"),
        "--config",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="主机 YAML。",
    ),
    workloads: Path = typer.Option(
        Path("configs/workloads.yaml"),
        "--workloads",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="workload catalog。",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="只校验并显示规模，不创建文件。"),
) -> None:
    """确定性展开实验为 CSV，并生成配置/组合 sidecar。"""

    try:
        if stage not in PLAN_STAGES:
            raise ValueError(f"--stage 必须是 {', '.join(PLAN_STAGES)}")
        repo_root = discover_repo_root(Path.cwd())
        if _effective_dry_run(ctx, dry_run):
            local = load_local_config(config)
            spec = load_experiment_config(experiment)
            counts = {
                "solo": len(spec.workload_ids) * spec.repeats,
                "profile": len(spec.workload_ids)
                * len(spec.resources)
                * len(spec.pressure_levels)
                * spec.repeats,
                "colocation-main": (
                    spec.main_combinations.pairs.expected_count
                    + spec.main_combinations.triples.expected_count
                )
                * spec.repeats,
                "colocation-extra-test": spec.extra_test.expected_count * spec.repeats,
            }
            selected_count = sum(counts.values()) if stage == "all" else counts[stage]
            typer.echo(
                stable_json_dumps(
                    {
                        "command": "plan",
                        "dry_run": True,
                        "experiment_id": spec.name,
                        "stage": stage,
                        "selected_row_count": selected_count,
                        "all_stage_counts": counts,
                        "random_seed": local.measurement.random_seed,
                        "output": output.as_posix(),
                        "mutations_planned": [
                            output.name,
                            f"{output.stem}-manifest.json",
                            f"{output.stem}-combinations.json",
                        ],
                    },
                    indent=2,
                )
            )
            return
        result = build_plan(
            repo_root=repo_root,
            local_config_path=config,
            experiment_path=experiment,
            workload_catalog_path=workloads,
            stage=stage,  # type: ignore[arg-type]
            output_file=output,
        )
    except (ConfigError, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"PLAN_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(stable_json_dumps(result["manifest"], indent=2))


@app.command("plan-verify")
def experiment_plan_verify(
    plan: Path = typer.Option(
        ...,
        "--plan",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
    ),
    output: Path | None = typer.Option(None, "--output", help="可选的独占校验 JSON。"),
) -> None:
    """只读复核计划 CSV、组合 manifest 和逐行哈希。"""

    try:
        repo_root = discover_repo_root(Path.cwd())
        if output is not None and output.exists():
            raise FileExistsError(f"验证输出已存在，拒绝覆盖: {output}")
        result = verify_plan(repo_root=repo_root, plan_file=plan)
        if output is not None:
            write_json_atomic(output, result)
    except (FileExistsError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"PLAN_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(stable_json_dumps(result, indent=2))
    if result["status"] != "passed":
        raise typer.Exit(code=3)


@app.command("run")
def experiment_run(
    ctx: typer.Context,
    plan: Path = typer.Option(
        ...,
        "--plan",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="由 plan 命令生成且可通过哈希复核的 CSV。",
    ),
    resume: bool = typer.Option(False, "--resume", help="只跳过完整、有效且哈希一致的 attempt。"),
    max_runs: int | None = typer.Option(None, "--max-runs", min=1, help="仅调试/分批执行前 N 个计划行。"),
    report: Path | None = typer.Option(None, "--report", help="可选的独占运行报告 JSON。"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只复核计划和 resume 决策。"),
) -> None:
    """顺序执行不可变计划；每行失败被隔离到自己的 attempt。"""

    try:
        if report is not None and report.exists():
            raise FileExistsError(f"运行报告已存在，拒绝覆盖: {report}")
        repo_root = discover_repo_root(Path.cwd())
        result = run_plan(
            repo_root=repo_root,
            plan_file=plan,
            resume=resume,
            max_runs=max_runs,
            dry_run=_effective_dry_run(ctx, dry_run),
        )
        if report is not None and not result.get("dry_run"):
            write_json_atomic(report, result)
    except (ConfigError, FileExistsError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"RUNNER_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(stable_json_dumps(result, indent=2))
    if result["status"] == "failed":
        raise typer.Exit(code=3)


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


@benchmark_app.command("calibrate")
def benchmark_calibrate(
    ctx: typer.Context,
    config: Path = typer.Option(
        Path("configs/local.example.yaml"),
        "--config",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="主机 YAML 配置。",
    ),
    resources: str = typer.Option(
        ",".join(BENCHMARK_RESOURCES),
        "--resources",
        help="逗号分隔的 cpu_compute,memory_bandwidth,gpu_compute,gpu_memory。",
    ),
    levels: str = typer.Option("0,0.25,0.5,0.75,1.0", "--levels", help="严格递增压力档。"),
    repeats: int = typer.Option(3, "--repeats", min=2, max=20),
    warmup_s: float = typer.Option(1.0, "--warmup-s", min=0.0),
    duration_s: float = typer.Option(6.0, "--duration-s", min=0.1),
    sample_interval_s: float = typer.Option(1.0, "--sample-interval-s", min=0.05),
    gpu_index: int = typer.Option(0, "--gpu-index", min=0),
    cpu_workers: int = typer.Option(8, "--cpu-workers", min=1, max=64),
    memory_buffer_mib: int = typer.Option(64, "--memory-buffer-mib", min=8, max=4096),
    gpu_matrix_size: int = typer.Option(1024, "--gpu-matrix-size", min=128, max=4096),
    gpu_memory_max_mib: int = typer.Option(1024, "--gpu-memory-max-mib", min=64, max=12288),
    output: Path = typer.Option(
        Path("artifacts/calibration/step4/calibration.json"),
        "--output",
        help="独占创建的校准汇总 JSON。",
    ),
    metrics_output: Path | None = typer.Option(None, "--metrics-output", help="原始 JSONL 路径。"),
    status_output: Path | None = typer.Option(None, "--status-output", help="校准状态 JSON 路径。"),
    workers_directory: Path | None = typer.Option(None, "--workers-directory", help="worker 日志目录。"),
    plot: Path | None = typer.Option(None, "--plot", help="可选的请求/作用压力曲线 PNG。"),
    dry_run: bool = typer.Option(False, "--dry-run", help="仅输出 4×5×repeat 计划，不启动 worker。"),
) -> None:
    """顺序校准四类 benchmark；真实资源加载由独立 worker 承担。"""

    try:
        repo_root = discover_repo_root(Path.cwd())
        parsed_resources = _parse_csv_strings(resources, option_name="--resources")
        invalid_resources = [item for item in parsed_resources if item not in BENCHMARK_RESOURCES]
        if invalid_resources:
            raise ValueError(f"--resources 包含未知项: {', '.join(invalid_resources)}")
        output_path, metrics_path, status_path, workers_path, plot_path = _calibration_paths(
            output,
            metrics_output,
            status_output,
            workers_directory,
            plot,
        )
        request = CalibrationRequest(
            config_path=config,
            resources=parsed_resources,  # type: ignore[arg-type]
            levels=_parse_csv_floats(levels, option_name="--levels"),
            repeats=repeats,
            warmup_s=warmup_s,
            duration_s=duration_s,
            sample_interval_s=sample_interval_s,
            gpu_index=gpu_index,
            cpu_workers=cpu_workers,
            memory_buffer_mib=memory_buffer_mib,
            gpu_matrix_size=gpu_matrix_size,
            gpu_memory_max_mib=gpu_memory_max_mib,
            output_file=output_path,
            metrics_file=metrics_path,
            status_file=status_path,
            workers_root=workers_path,
            plot_file=plot_path,
        )
        request.validate(repo_root)
        if _effective_dry_run(ctx, dry_run):
            typer.echo(stable_json_dumps(request.public_plan(repo_root), indent=2))
            return
        result = run_calibration(repo_root=repo_root, request=request)
    except (ConfigError, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"BENCHMARK_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(format_calibration_result(result))
    if result["status"] != "passed":
        raise typer.Exit(code=3)


@benchmark_app.command("verify")
def benchmark_verify(
    calibration: Path = typer.Option(
        ...,
        "--calibration",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="由 benchmark calibrate 生成的 JSON。",
    ),
    output: Path | None = typer.Option(None, "--output", help="可选的独占验证 JSON。"),
) -> None:
    """只读复核校准摘要、每资源质量门和原始 JSONL 哈希。"""

    try:
        repo_root = discover_repo_root(Path.cwd())
        if output is not None and output.exists():
            raise FileExistsError(f"验证输出已存在，拒绝覆盖: {output}")
        result = verify_calibration(repo_root=repo_root, calibration_file=calibration)
        if output is not None:
            write_json_atomic(output, result)
    except (ConfigError, FileExistsError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"BENCHMARK_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(format_calibration_result(result))
    if result["status"] != "passed":
        raise typer.Exit(code=3)


@benchmark_app.command("_worker", hidden=True)
def benchmark_worker(
    resource: str = typer.Option(..., "--resource"),
    pressure: float = typer.Option(..., "--pressure", min=0.0, max=1.0),
    runtime_s: float = typer.Option(..., "--runtime-s", min=0.1),
    warmup_s: float = typer.Option(0.0, "--warmup-s", min=0.0),
    barrier_file: Path | None = typer.Option(None, "--barrier-file"),
    barrier_timeout_s: float = typer.Option(30.0, "--barrier-timeout-s", min=0.1),
    gpu_index: int = typer.Option(0, "--gpu-index", min=0),
    cpu_workers: int = typer.Option(..., "--cpu-workers", min=1, max=64),
    memory_buffer_mib: int = typer.Option(..., "--memory-buffer-mib", min=8, max=4096),
    gpu_matrix_size: int = typer.Option(..., "--gpu-matrix-size", min=128, max=4096),
    gpu_memory_max_mib: int = typer.Option(..., "--gpu-memory-max-mib", min=64, max=12288),
    ready_file: Path = typer.Option(..., "--ready-file"),
    status_file: Path = typer.Option(..., "--status-file"),
) -> None:
    """仅供校准父进程调用的隐藏 worker 入口。"""

    try:
        result = run_benchmark_worker(
            config=BenchmarkWorkerConfig(
                resource=resource,  # type: ignore[arg-type]
                pressure=pressure,
                runtime_s=runtime_s,
                warmup_s=warmup_s,
                barrier_file=barrier_file,
                barrier_timeout_s=barrier_timeout_s,
                gpu_index=gpu_index,
                cpu_workers=cpu_workers,
                memory_buffer_mib=memory_buffer_mib,
                gpu_matrix_size=gpu_matrix_size,
                gpu_memory_max_mib=gpu_memory_max_mib,
            ),
            ready_file=ready_file,
            status_file=status_file,
        )
    except BaseException as exc:
        typer.echo(f"BENCHMARK_WORKER_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(worker_result_text(result))


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
    run_id: str | None = typer.Option(None, "--run-id"),
    duration: float = typer.Option(..., "--duration", min=1.0),
    warmup: float = typer.Option(0.0, "--warmup", min=0.0),
    max_frames: int = typer.Option(..., "--max-frames", min=0),
    repeat: int = typer.Option(..., "--repeat", min=1),
    output_directory: Path = typer.Option(..., "--output-directory"),
    barrier_file: Path | None = typer.Option(None, "--barrier-file"),
    barrier_timeout: float = typer.Option(30.0, "--barrier-timeout", min=0.1),
    metric_window: float = typer.Option(1.0, "--metric-window", min=0.1),
    headless: bool = typer.Option(False, "--headless"),
) -> None:
    """launcher 专用子进程入口。"""

    game = get_game(workload_id)
    repo_root = discover_repo_root(Path.cwd())
    resolved_output = output_directory.resolve()
    if repo_root not in resolved_output.parents:
        raise typer.BadParameter("子进程输出目录必须位于仓库内", param_hint="--output-directory")
    effective_run_id = run_id or f"step3-smoke-{game.id}-r{repeat:02d}"
    resolved_barrier = barrier_file.resolve() if barrier_file is not None else None
    if resolved_barrier is not None and repo_root not in resolved_barrier.parents:
        raise typer.BadParameter("barrier 文件必须位于仓库内", param_hint="--barrier-file")
    try:
        result = execute_game_child(
            repo_root=repo_root,
            game=game,
            config=GameRunConfig(
                run_id=effective_run_id,
                duration_s=duration,
                max_frames=max_frames,
                headless=headless,
                audio_mode="muted",
                warmup_s=warmup,
                barrier_file=resolved_barrier,
                barrier_timeout_s=barrier_timeout,
                metric_window_s=metric_window,
            ),
            output_directory=resolved_output,
        )
    except BaseException as exc:
        typer.echo(f"CHILD_ERROR: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if result["status"] != "completed":
        raise typer.Exit(code=4)
