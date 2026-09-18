from splitter.core import trackcheck
from splitter.core.geometry import GatePosition


def ref(points: dict[int, tuple[float, float, float]]) -> dict[int, GatePosition]:
    return {k: GatePosition(k, x, y, z, 3) for k, (x, y, z) in points.items()}


SQUARE = {
    1: (0.0, 1.0, 0.0),
    2: (20.0, 1.0, 0.0),
    3: (20.0, 1.0, 20.0),
    4: (0.0, 1.0, 20.0),
    5: (-10.0, 1.0, 10.0),
}


def test_gate_count_mismatch_is_a_different_track_with_high_confidence() -> None:
    c = trackcheck.compare(7, {}, 5, ref(SQUARE))
    assert (c.verdict, c.confidence) == ("different", "high")
    assert "7 gates" in c.reason and "5" in c.reason


def test_same_gates_within_a_few_metres_is_the_same_track() -> None:
    run = {k: (x + 2.0, y, z - 1.5) for k, (x, y, z) in SQUARE.items()}
    c = trackcheck.compare(5, run, 5, ref(SQUARE))
    assert (c.verdict, c.confidence) == ("same", "high")
    assert c.gates_compared == 5 and c.mean_distance_m is not None and c.mean_distance_m < 3


def test_gates_far_away_is_a_different_track_with_medium_confidence() -> None:
    run = {k: (x + 40.0, y, z + 30.0) for k, (x, y, z) in SQUARE.items()}
    c = trackcheck.compare(5, run, 5, ref(SQUARE))
    assert (c.verdict, c.confidence) == ("different", "medium")
    assert "50 m" in c.reason


def test_too_few_gates_or_no_reference_is_unknown() -> None:
    assert trackcheck.compare(5, {1: SQUARE[1], 2: SQUARE[2]}, 5, ref(SQUARE)).verdict == "unknown"
    assert trackcheck.compare(5, {}, None, {}).verdict == "unknown"
    # Unknown gate count on either side does not trigger the count rule.
    assert trackcheck.compare(None, {}, 5, {}).verdict == "unknown"
