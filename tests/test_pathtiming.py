"""Crossings from the flight path (core/pathtiming.py): a synthetic circular
track whose gate positions and headings are known exactly."""

from __future__ import annotations

import math
from dataclasses import dataclass

from splitter.core import geometry, pathtiming
from splitter.core.geometry import GatePosition

R = 30.0  # circle radius, m
G = 6  # gates per lap
LAP_S = 9.0
HOLESHOT_S = 1.0


@dataclass(frozen=True)
class P:
    t_ms: int
    x: float
    y: float
    z: float


def angle_at(t_s: float) -> float:
    """The drone reaches the start/finish (angle 0) at HOLESHOT_S and laps every LAP_S."""
    return 2 * math.pi * (t_s - HOLESHOT_S) / LAP_S


def flight(seconds: float, hz: float = 60.0, y: float = 1.5) -> list[P]:
    out = []
    n = int(seconds * hz)
    for i in range(n + 1):
        t = i / hz
        a = angle_at(t)
        out.append(P(round(t * 1000), R * math.cos(a), y, R * math.sin(a)))
    return out


def crossing_times(laps: int) -> list[tuple[int, int]]:
    """(k, t_ms) as the game would report them: k=0 the first S/F, then 1..G per lap."""
    out = [(0, round(HOLESHOT_S * 1000))]
    for lap in range(laps):
        for k in range(1, G + 1):
            out.append((k, round((HOLESHOT_S + lap * LAP_S + k * LAP_S / G) * 1000)))
    return out


def model() -> dict[int, pathtiming.GatePlane]:
    trace = flight(HOLESHOT_S + 2 * LAP_S + 1)
    crossings = [(G if k == 0 else k, t) for k, t in crossing_times(2)]
    positions = geometry.gate_positions([(trace, crossings)])
    m = pathtiming.model_from_geometry(positions, G)
    assert m is not None
    return m


def test_gate_positions_carry_a_heading() -> None:
    trace = flight(HOLESHOT_S + LAP_S + 1)
    positions = geometry.gate_positions([(trace, [(G, round(HOLESHOT_S * 1000))])])
    p = positions[G]
    assert math.isclose(p.x, R, abs_tol=0.05) and math.isclose(p.z, 0, abs_tol=0.05)
    # At angle 0 the drone moves along +z (counter-clockwise).
    assert math.isclose(math.hypot(p.hx, p.hy, p.hz), 1.0, abs_tol=1e-6)
    assert p.hz > 0.99 and abs(p.hx) < 0.1


def test_model_falls_back_to_neighbour_headings_and_needs_every_gate() -> None:
    positions = {
        k: GatePosition(
            k, R * math.cos(2 * math.pi * k / G), 1.5, R * math.sin(2 * math.pi * k / G), 1
        )
        for k in range(1, G + 1)
    }
    m = pathtiming.model_from_geometry(positions, G)
    assert m is not None and set(m) == set(range(1, G + 1))
    plane = m[G]  # gate at angle 0: prev at -60°, next at +60° → heading +z
    assert plane.nz > 0.99
    del positions[3]
    assert pathtiming.model_from_geometry(positions, G) is None
    assert pathtiming.model_from_geometry({}, 0) is None


def test_reconstruct_finds_every_crossing_in_order_within_a_few_ms() -> None:
    laps = 3
    trace = flight(HOLESHOT_S + laps * LAP_S + 2, hz=20)  # a stored 20 Hz trace
    found = pathtiming.reconstruct(trace, model(), G, race_laps=laps)
    expected = crossing_times(laps)
    assert [c.k for c in found] == [k for k, _ in expected]
    assert max(abs(c.t_ms - t) for c, (_, t) in zip(found, expected, strict=True)) <= 10
    # Wire numbering: S/F = gate 1 of the lap it starts, segment k = gate k + 1,
    # the finish = gate G + 1 with finished set.
    assert (found[0].lap, found[0].gate, found[0].finished) == (1, 1, False)
    assert (found[1].lap, found[1].gate) == (1, 2)
    assert (found[G].lap, found[G].gate) == (2, 1)  # the S/F closing lap 1
    last = found[-1]
    assert last.finished and (last.lap, last.gate) == (laps, G + 1)
    # Nothing after the finish.
    assert len(found) == 1 + laps * G


def test_without_a_lap_count_the_run_never_finishes_and_the_detector_is_incremental() -> None:
    det = pathtiming.PathDetector(model(), G, race_laps=0)
    got = [c for s in flight(HOLESHOT_S + 2 * LAP_S + 1) if (c := det.feed(s)) is not None]
    assert len(got) == 1 + 2 * G and not any(c.finished for c in got)
    assert det.lap == 3 and det.next_k == 1


def test_a_pass_far_from_the_gate_does_not_count() -> None:
    # A path through the same planes but 10 m outside the circle: no gate within radius.
    trace = [
        P(p.t_ms, p.x * (R + 10) / R, p.y, p.z * (R + 10) / R) for p in flight(HOLESHOT_S + LAP_S)
    ]
    assert pathtiming.reconstruct(trace, model(), G, race_laps=1) == []
