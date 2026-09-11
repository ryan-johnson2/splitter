from __future__ import annotations

from splitter.core.splits import build_reference, theoretical_best


def test_reference_deltas() -> None:
    ref = build_reference(
        race_id=7,
        total_ms=16000,
        crossings=[(i, (i + 1) * 2000, 2000) for i in range(8)],
        laps=[(1, 8000), (2, 8000)],
        gates_per_lap=4,
    )
    assert ref.split_at(0, 1900) == -100
    assert ref.split_at(7, 16500) == 500
    assert ref.split_at(8, 1) is None
    assert ref.gate_delta(3, 2500) == 500
    assert ref.lap_delta(1, 7900) == -100
    assert ref.lap_delta(3, 1) is None
    assert ref.total_delta(15000) == -1000


def test_build_reference_sorts_by_seq() -> None:
    ref = build_reference(1, 100, [(1, 60, 30), (0, 30, 30)], [], None)
    assert ref.cumulative_by_seq == [30, 60]


def test_theoretical_best_uses_modal_length() -> None:
    runs = [[10, 20, 30], [12, 18, 33], [5, 5]]  # the 2-gate run is an outlier
    assert theoretical_best(runs) == 10 + 18 + 30
    assert theoretical_best([]) is None
    assert theoretical_best([[]]) is None
