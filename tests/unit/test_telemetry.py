from __future__ import annotations

from pathlib import Path

import pytest

from gaugur_lite.metrics.telemetry import expected_sample_count, run_overhead


def test_expected_sample_count() -> None:
    assert expected_sample_count(60, 1) == 60
    assert expected_sample_count(1.01, 0.5) == 3


def test_overhead_rejects_invalid_parameters(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repeats"):
        run_overhead(
            duration_s=1,
            interval_s=1,
            gpu_index=0,
            output_file=tmp_path / "overhead.json",
            repeats=1,
        )


def test_overhead_refuses_existing_output_before_gpu_init(tmp_path: Path) -> None:
    output = tmp_path / "overhead.json"
    output.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        run_overhead(
            duration_s=2,
            interval_s=1,
            gpu_index=0,
            output_file=output,
            repeats=2,
        )
