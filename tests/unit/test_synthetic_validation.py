from __future__ import annotations

from gaugur_lite.synthetic_validation import build_synthetic_interaction_table


def test_synthetic_interaction_table_has_non_degenerate_labels_and_groups() -> None:
    table = build_synthetic_interaction_table(seed=20260818, repeats=1)

    assert len(table) == 900
    assert table["qos_satisfied"].nunique() == 2
    assert table["combination_key"].nunique() < len(table)
    assert table["retention_ratio"].between(0.20, 1.10).all()
