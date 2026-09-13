from dataclasses import dataclass

import pytest

from splitter.core import geometry


@dataclass(frozen=True)
class P:
    t_ms: int
    x: float
    y: float
    z: float


def test_position_interpolates_between_samples() -> None:
    trace = [P(0, 0, 1, 0), P(100, 10, 1, 0), P(200, 10, 1, 10)]
    assert geometry.position_at(trace, 50) == (5, 1, 0)
    assert geometry.position_at(trace, 100) == (10, 1, 0)
    assert geometry.position_at(trace, 150) == (10, 1, 5)


def test_position_none_outside_trace_or_across_a_gap() -> None:
    trace = [P(0, 0, 0, 0), P(100, 10, 0, 0), P(2000, 20, 0, 0)]
    assert geometry.position_at([], 10) is None
    assert geometry.position_at(trace, -1) is None
    assert geometry.position_at(trace, 2001) is None
    assert geometry.position_at(trace, 1000) is None  # 100 → 2000 is a hole in the feed


def test_gate_positions_average_across_runs() -> None:
    run_a = ([P(0, 0, 0, 0), P(400, 4, 0, 0), P(800, 8, 0, 0), P(1000, 10, 0, 0)], [(1, 200)])
    run_b = ([P(0, 0, 0, 0), P(400, 4, 0, 4), P(1000, 10, 0, 10)], [(1, 200), (2, 1000)])
    pos = geometry.gate_positions([run_a, run_b])
    assert pos[1].samples == 2 and pos[1].x == 2 and pos[1].z == 1
    assert pos[2].samples == 1 and pos[2].x == 10


def test_square_track_turns_ninety_degrees_at_every_gate() -> None:
    corners = {1: (0, 0), 2: (10, 0), 3: (10, 10), 4: (0, 10)}
    pos = {k: geometry.GatePosition(k, x, 1.0, z, 1) for k, (x, z) in corners.items()}
    geom = geometry.gate_geometry(pos, 4)
    assert [g.k for g in geom] == [1, 2, 3, 4]
    assert all(g.spacing_m == pytest.approx(10) for g in geom)
    assert all(g.heading_change_deg == pytest.approx(90) for g in geom)


def test_missing_gate_positions_leave_none() -> None:
    pos = {1: geometry.GatePosition(1, 0, 0, 0, 1), 2: geometry.GatePosition(2, 5, 0, 0, 1)}
    geom = geometry.gate_geometry(pos, 3)
    assert geom[2].spacing_m is None and geom[2].heading_change_deg is None
    assert (
        geom[1].spacing_m == 5 and geom[1].heading_change_deg is None
    )  # no gate 3 to turn towards
