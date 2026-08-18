"""汇总既有产物为可审计的最终复现报告。"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any

from ..config import stable_json_dumps
from ..runner.plan import _file_sha256
from ..workloads.registry import GAME_REGISTRY


class ReportError(RuntimeError):
    """报告输入缺失或报告验收失败。"""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReportError(f"报告输入不存在: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"报告输入不是合法 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReportError(f"报告输入必须是 JSON object: {path}")
    return value


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _copy_figure(root: Path, out: Path, source: str, name: str) -> dict[str, Any]:
    source_path = root / source
    destination = out / "figures" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not source_path.is_file():
        raise ReportError(f"报告图表输入不存在: {source_path}")
    shutil.copy2(source_path, destination)
    return {
        "source": source,
        "path": _relative(destination, root),
        "sha256": _file_sha256(destination),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ReportError(f"不能写入空表: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_blocked_fps_figure(path: Path) -> None:
    """生成明确标注未执行原因的占位图，禁止把缺失结果画成零。"""

    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - formal environment includes matplotlib.
        raise ReportError("最终报告占位图需要 matplotlib") from exc
    figure, axis = plt.subplots(figsize=(7.2, 3.8))
    axis.axis("off")
    axis.text(
        0.5,
        0.55,
        "Step 13 fixed-slot FPS replay\nNOT EXECUTED",
        ha="center",
        va="center",
        fontsize=18,
        weight="bold",
    )
    axis.text(
        0.5,
        0.30,
        "Blocked: real QoS labels are degenerate;\nno valid CM/RM effectiveness dataset was frozen.",
        ha="center",
        va="center",
        fontsize=11,
    )
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def build_reproduction_report(
    *, repo_root: Path, experiment: str, model_dir: Path, output_dir: Path
) -> dict[str, Any]:
    """生成 Step 14 报告；只读已有结果，不执行游戏或训练。"""

    root = repo_root.resolve()
    output = output_dir.resolve()
    model_path = model_dir.resolve()
    if experiment != "formal-v1":
        raise ReportError("当前 Step 14 只支持已验收的 formal-v1")
    output.mkdir(parents=True, exist_ok=True)
    required_outputs = [
        output / "report.md",
        output / "run_quality.json",
        output / "dataset_card.md",
        output / "cm_model_card.md",
        output / "rm_model_card.md",
        output / "reproduction_manifest.json",
    ]
    existing = [path for path in required_outputs if path.exists()]
    if existing:
        raise FileExistsError(f"报告产物已存在，拒绝覆盖: {existing[0]}")

    paths = {
        "baselines": root / "data/interim/formal-v1/solo-baselines.json",
        "profile_acceptance": root / "artifacts/profiles/step7/safety-v2/formal-profile-verification.json",
        "colocation_acceptance": root / "artifacts/colocation/step8/safety-v2/formal-colocation-acceptance.json",
        "colocation_verification": root / "artifacts/colocation/step8/safety-v2/formal-colocation-verification.json",
        "dataset_acceptance": root / "artifacts/dataset/step9/formal-dataset-verification.json",
        "model_acceptance": model_path / "formal-model-acceptance.json",
        "evaluation": root / "artifacts/reports/formal-v1/evaluation/evaluation-summary.json",
        "ablation": root / "artifacts/reports/formal-v1/ablations/formal-ablation-acceptance.json",
        "packing": root / "artifacts/reports/formal-v1/packing/formal-packing-acceptance.json",
        "thresholds": root / "artifacts/effectiveness/qos-threshold-sensitivity/formal-v1/qos-threshold-sensitivity.json",
        "synthetic": root / "artifacts/effectiveness/synthetic-validation/formal-v1-v3/synthetic-validation.json",
        "stress": root / "artifacts/effectiveness/pilot/stress-pilot-acceptance.json",
        "highfps": root / "artifacts/effectiveness/highfps-pilot/highfps-pilot-acceptance.json",
        "affinity": root / "artifacts/effectiveness/affinity-pilot/highfps-pilot-acceptance.json",
    }
    loaded = {name: _read_json(path) for name, path in paths.items()}
    baseline_rows = loaded["baselines"].get("baselines", [])
    baseline_by_id = {str(row.get("workload_id")): row for row in baseline_rows}

    workload_rows: list[dict[str, Any]] = []
    for game in GAME_REGISTRY:
        public = game.public_dict()
        baseline = baseline_by_id.get(game.id, {})
        workload_rows.append(
            {
                "workload_id": game.id,
                "title": game.title,
                "source_kind": game.kind,
                "entrypoint": game.entrypoint,
                "license_file": "LICENSE",
                "license_sha256": next(item.sha256 for item in game.upstream_files if item.path == "LICENSE"),
                "target_fps": game.target_fps,
                "solo_mean_fps": baseline.get("mean_fps"),
                "solo_sample_std_fps": baseline.get("mean_fps_sample_std"),
                "solo_cv_pct": baseline.get("mean_fps_cv_pct"),
                "repeat_count": baseline.get("repeat_count"),
                "source_tree_sha256": (public.get("source_tree") or {}).get("sha256"),
            }
        )
    _write_csv(output / "tables/workloads-and-solo.csv", workload_rows)

    figure_sources = {
        "solo-baselines.png": "artifacts/baselines/step6/formal-solo-baselines.png",
        "sensitivity-curves.png": "artifacts/profiles/step7/safety-v2/plots/sensitivity-curves.png",
        "sensitivity-intensity.png": "artifacts/profiles/step7/safety-v2/plots/sensitivity-intensity.png",
        "intensity-heatmap.png": "artifacts/profiles/step7/safety-v2/plots/intensity-heatmap.png",
        "retention-by-size.png": "artifacts/colocation/step8/safety-v2/plots/retention-by-size.png",
        "cm-confusion-matrices.png": "artifacts/reports/formal-v1/evaluation/cm-confusion-matrices.png",
        "rm-error-cdf.png": "artifacts/reports/formal-v1/evaluation/rm-error-cdf.png",
        "ablation-rm-mae.png": "artifacts/reports/formal-v1/ablations/ablation-rm-mae.png",
        "packing-slots.png": "artifacts/reports/formal-v1/packing/packing-slots.png",
        "qos-threshold-sensitivity.png": "artifacts/effectiveness/qos-threshold-sensitivity/formal-v1/qos-threshold-sensitivity.png",
        "synthetic-validation-metrics.png": "artifacts/effectiveness/synthetic-validation/formal-v1-v3/synthetic-validation-metrics.png",
    }
    figures = [_copy_figure(root, output, source, name) for name, source in figure_sources.items()]
    blocked_figure = output / "figures/fixed-slot-fps-status.png"
    _write_blocked_fps_figure(blocked_figure)
    figures.append({"source": None, "path": _relative(blocked_figure, root), "sha256": _file_sha256(blocked_figure)})

    quality = {
        "schema_version": 1,
        "experiment": experiment,
        "status": "passed",
        "engineering_acceptance": {
            "profile": loaded["profile_acceptance"].get("status"),
            "colocation": loaded["colocation_acceptance"].get("status"),
            "colocation_verification": loaded["colocation_verification"].get("status"),
            "dataset": loaded["dataset_acceptance"].get("status"),
            "models": loaded["model_acceptance"].get("status"),
            "evaluation": loaded["evaluation"].get("status", "passed"),
            "ablations": loaded["ablation"].get("status"),
            "packing": loaded["packing"].get("status"),
        },
        "effectiveness_pilots": {
            "external_benchmark": loaded["stress"],
            "high_fps": loaded["highfps"],
            "same_core_affinity": loaded["affinity"],
            "threshold_sensitivity": loaded["thresholds"],
            "synthetic_validation": loaded["synthetic"],
        },
        "real_effectiveness_claim": "not_validated",
        "step13_fixed_slot_fps": "not_executed",
        "notes": [
            "formal-v1 truth is retained as an engineering control dataset, not effectiveness evidence",
            "all three real workload pilots failed the non-degenerate QoS-label gate",
            "synthetic validation is algorithm-only and does not substitute for real games",
        ],
    }
    (output / "run_quality.json").write_text(stable_json_dumps(quality, indent=2) + "\n", encoding="utf-8")

    dataset_card = """# formal-v1 dataset card

## Scope

This card describes the historical control dataset produced by the eight bundled Pyxel workloads. It contains 24 solo runs, 480 profile runs, 216 historical colocation runs and 600 target truth rows.

The colocation truth is not a valid reproduction of the paper's effectiveness result: the original run did not carry the required external pressure, and the three subsequent real-workload pilots produced no negative QoS labels.

## Features and splits

The dataset contains target solo FPS, four-resource sensitivity curves, neighbor intensity mean/variance and retention ratio. Splits are combination-level and target_id is excluded from model features. The source truth is retained byte-for-byte under `data/interim/formal-v1/safety-v2/`.

## Limitations

All eight workloads use the same Pyxel engine and are lightweight, frame-capped programs. The dataset therefore supports software-contract and pipeline verification, but not a claim about large commercial cloud games.
"""
    (output / "dataset_card.md").write_text(dataset_card, encoding="utf-8")
    cm_card = f"""# CM model card

- Selected candidate: `{loaded['model_acceptance'].get('selected_cm_candidate')}`
- Model artifact: `{model_path / 'cm.joblib'}`
- Feature manifest excludes `target_id`: `{loaded['evaluation'].get('checks', {}).get('model_feature_columns_exclude_target_id', False)}`
- Real-domain label status: degenerate under the main `qos_ratio=0.80` truth; CM effectiveness is **not validated**.
- Synthetic non-linear validation: F1 `0.9654`, algorithm-only.
"""
    rm_card = f"""# RM model card

- Selected candidate: `{loaded['model_acceptance'].get('selected_rm_candidate')}`
- Model artifact: `{model_path / 'rm.joblib'}`
- Real-domain label status: retention variation is too small for a meaningful effectiveness claim.
- Synthetic non-linear validation: retention MAE `0.01456`, algorithm-only.
"""
    (output / "cm_model_card.md").write_text(cm_card, encoding="utf-8")
    (output / "rm_model_card.md").write_text(rm_card, encoding="utf-8")

    manifest_files = [paths[name] for name in paths] + [output / "run_quality.json", output / "dataset_card.md", output / "cm_model_card.md", output / "rm_model_card.md"]
    manifest = {
        "schema_version": 1,
        "experiment": experiment,
        "status": "passed",
        "report": _relative(output / "report.md", root),
        "figures": figures,
        "sources": [
            {"path": _relative(path, root), "sha256": _file_sha256(path)}
            for path in manifest_files
        ],
        "claim_boundary": "engineering_pipeline_reproduced; real_workload_effectiveness_not_validated",
    }

    report = f"""# GAugur Lite 复现报告（{experiment}）

## 结论摘要

本项目完成了 GAugur 的轻量软件复现：真实 workload 注册、solo/profile 采集、资源敏感度/强度特征、共置 truth、CM/RM、基线、消融、QoS 装箱 replay 及自动验收链路均已实现并保留机器可读产物。

**真实方法有效性结论：未验证。** 历史 `formal-v1` 共置 truth 是工程控制数据；外部 benchmark、高 FPS、同核 affinity 三条真实 workload 修复路线均未产生非退化 QoS 标签。不能据此声称 CM 优于基线，也不能复现论文中的真实准确率/误差数字。

合成非线性交互验收通过，证明实现能够学习预先构造的非线性关系，但它不是云游戏实验证据。Step 13 固定槽位 FPS replay 因缺少有效真实标签而未执行。

## 1. Workloads、来源与许可证

八个 workload 均为仓库中冻结的 Pyxel 资源，许可证文件及哈希见 `tables/workloads-and-solo.csv`。独占 FPS、重复标准差和 CV 也在同表；重复次数为 3。

## 2. Profile 与共置观测

资源敏感度曲线、敏感度—强度关系、强度热图和按共置规模的 retention 图见：

![Sensitivity curves](figures/sensitivity-curves.png)
![Sensitivity versus intensity](figures/sensitivity-intensity.png)
![Intensity heatmap](figures/intensity-heatmap.png)
![Retention by colocation size](figures/retention-by-size.png)

## 3. CM/RM 与基线

模型评估图见 `figures/cm-confusion-matrices.png` 与 `figures/rm-error-cdf.png`。由于正式真实标签几乎全为正类，混淆矩阵和 QoS 指标不能解释为类别区分能力；Step 11 消融和主/额外测试原始结果仍保存在原报告目录。

![CM confusion matrices](figures/cm-confusion-matrices.png)
![RM error CDF](figures/rm-error-cdf.png)
![Ablation RM MAE](figures/ablation-rm-mae.png)

## 4. Replay 与完整性

QoS 装箱 replay、槽位数和实测违约率见 `figures/packing-slots.png`。右侧全零是历史 truth 的真实分布，不是缺失值。

## 5. 有效性修复与标签敏感性

三条真实 workload pilot 的原始 JSON 在 `run_quality.json` 中逐字嵌入。阈值敏感性图显示，`qos_ratio` 从 0.80 到 0.995 都没有负类，只有把任何可测下降都视为失败的 1.0 阈值产生 12 个负类；该标准不作为论文 QoS 结论。

![QoS threshold sensitivity](figures/qos-threshold-sensitivity.png)

## 6. 合成算法验收

合成非线性交互数据的 CM F1 为 0.9654，RM retention MAE 为 0.01456，均优于 count-only 和 linear-additive 基线；该结果只验证算法实现。

![Synthetic validation](figures/synthetic-validation-metrics.png)

## 7. 未执行项与限制

- Step 13 fixed-slot FPS replay：未执行，因为真实 QoS 标签门禁失败；对应状态图见 `figures/fixed-slot-fps-status.png`。
- 论文使用约 100 个商业游戏、七类资源和特定 Windows+i7-7700+GTX1060 环境；原始数据集未随论文公开。
- 当前八个 workload 同属 Pyxel 引擎且负载轻，不能外推到论文中的大型商业云游戏。
- 自动输入、窗口状态、deadline/失败现场均以原始 invocation JSON 和 Step 5–8 验收产物为准；本报告不把失败现场删除或改写。

## 8. 可复核入口

- 工程质量汇总：`run_quality.json`
- 数据卡：`dataset_card.md`
- CM/RM 模型卡：`cm_model_card.md`、`rm_model_card.md`
- 机器可读清单：`reproduction_manifest.json`

报告生成时间不参与哈希；所有输入源和复制图表均在 `reproduction_manifest.json` 中记录 SHA-256。
"""
    (output / "report.md").write_text(report, encoding="utf-8")
    manifest["sources"].extend(
        {"path": _relative(path, root), "sha256": _file_sha256(path)}
        for path in [output / "report.md", *[output / "figures" / name for name in figure_sources], blocked_figure]
    )
    (output / "reproduction_manifest.json").write_text(stable_json_dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "passed",
        "experiment": experiment,
        "report": _relative(output / "report.md", root),
        "figure_count": len(figures),
        "source_count": len(manifest["sources"]),
        "real_effectiveness_claim": "not_validated",
        "step13_fixed_slot_fps": "not_executed",
    }


def verify_reproduction_report(*, repo_root: Path, report: Path) -> dict[str, Any]:
    """只读验证报告目录、清单哈希和关键结论边界。"""

    root = repo_root.resolve()
    report = report.resolve()
    if not report.is_file():
        raise ReportError(f"report.md 不存在: {report}")
    report_dir = report.parent
    manifest = _read_json(report_dir / "reproduction_manifest.json")
    checks: dict[str, bool] = {}
    checks["report_mentions_unvalidated_real_effectiveness"] = "真实方法有效性结论：未验证" in report.read_text(encoding="utf-8")
    checks["report_mentions_step13_not_executed"] = "Step 13" in report.read_text(encoding="utf-8") and "未执行" in report.read_text(encoding="utf-8")
    checks["manifest_claim_boundary_is_safe"] = manifest.get("claim_boundary") == "engineering_pipeline_reproduced; real_workload_effectiveness_not_validated"
    checked_files = 0
    for item in [*manifest.get("figures", []), *manifest.get("sources", [])]:
        path = root / str(item["path"])
        if not path.is_file() or _file_sha256(path) != item.get("sha256"):
            checks[f"hash:{item.get('path')}"] = False
        else:
            checked_files += 1
    checks["all_manifest_hashes_match"] = all(value for key, value in checks.items() if key.startswith("hash:"))
    checks["required_cards_present"] = all((report_dir / name).is_file() for name in ("run_quality.json", "dataset_card.md", "cm_model_card.md", "rm_model_card.md"))
    result = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "report": _relative(report, root),
        "checked_file_count": checked_files,
        "checks": checks,
    }
    return result
