"""Crash detection on synthetic 20 Hz traces shaped like the real ones."""

from dataclasses import dataclass

from splitter.core import crashes


@dataclass(frozen=True)
class S:
    t_ms: int
    x: float
    y: float
    z: float
    speed: float
    roll: float = 100.0
    pitch: float = 100.0
    yaw: float = 100.0


@dataclass(frozen=True)
class X:
    seq: int
    lap: int
    ends_lap: int | None
    cumulative_ms: int


def trace(profile: list[tuple[int, float, float]], step_ms: int = 50) -> list[S]:
    """``(until_ms, speed, gyro)`` pieces → samples every ``step_ms``."""
    out: list[S] = []
    t = 0
    for until, speed, gyro in profile:
        while t < until:
            out.append(S(t, t / 100.0, 1.0, 0.0, speed, gyro, 0.0, 0.0))
            t += step_ms
    return out


def test_a_hard_stop_with_a_gyro_spike_is_one_crash() -> None:
    samples = trace(
        [(2000, 20.0, 150.0), (2050, 3.0, 950.0), (3000, 1.0, 150.0), (6000, 20.0, 150.0)]
    )
    found = crashes.detect(samples)
    assert len(found) == 1
    c = found[0]
    assert c.t_ms == 1950 and c.speed_before == 20.0
    assert c.decel <= -120 and c.gyro >= 900


def test_a_stop_without_a_spike_is_confirmed_by_the_dwell() -> None:
    samples = trace(
        [(2000, 20.0, 150.0), (2050, 2.0, 150.0), (3000, 1.0, 150.0), (6000, 20.0, 150.0)]
    )
    assert len(crashes.detect(samples)) == 1


def test_braking_and_slow_bumps_are_not_crashes() -> None:
    # 20 → 14 m/s over 100 ms is -60 m/s^2: hard braking, still flying.
    braking = trace(
        [(2000, 20.0, 150.0), (2050, 17.0, 150.0), (2100, 14.0, 150.0), (4000, 14.0, 150.0)]
    )
    assert crashes.detect(braking) == []
    # A 7 m/s bump down to 1 m/s is below the flying-speed floor.
    bump = trace([(2000, 7.0, 150.0), (2050, 1.0, 900.0), (3000, 1.0, 150.0), (5000, 7.0, 150.0)])
    assert crashes.detect(bump) == []


def test_bounces_merge_into_the_first_crash() -> None:
    samples = trace(
        [
            (2000, 20.0, 150.0),
            (2050, 3.0, 950.0),
            (2600, 12.0, 150.0),
            (2650, 2.0, 900.0),
            (4000, 1.0, 150.0),
            (7000, 20.0, 150.0),
        ]
    )
    found = crashes.detect(samples)
    assert [c.t_ms for c in found] == [1950]


def test_two_separate_crashes_are_kept() -> None:
    samples = trace(
        [
            (2000, 20.0, 150.0),
            (2050, 3.0, 950.0),
            (3000, 1.0, 150.0),
            (6000, 20.0, 150.0),
            (6050, 2.0, 950.0),
            (8000, 1.0, 150.0),
        ]
    )
    assert [c.t_ms for c in crashes.detect(samples)] == [1950, 5950]


def test_attribution_uses_lap_relative_segments() -> None:
    # The helpers' two-lap race: start gate, then 3 segments per lap; S/F closes each lap.
    crossings = [
        X(0, 0, None, 1000),
        X(1, 1, None, 2000),
        X(2, 1, None, 4000),
        X(3, 1, None, 6000),
        X(4, 2, 1, 8000),
        X(5, 2, None, 10000),
        X(6, 2, None, 12000),
        X(7, 2, 2, 14000),
    ]
    at = [500, 2500, 5000, 9000, 13000]
    found = [crashes.Crash(t, 0, 0, 0, 20.0, -200.0, 900.0) for t in at]
    stamped = crashes.attribute(found, crossings)
    assert [(c.lap, c.segment) for c in stamped] == [(0, 0), (1, 1), (1, 2), (2, 1), (2, 3)]


def test_crash_round_trips_through_json_dict() -> None:
    c = crashes.Crash(1234, 1.234, 2.0, 3.456, 19.99, -211.7, 903.2, lap=2, segment=5)
    assert crashes.Crash.from_dict(c.to_dict()) == crashes.Crash(
        1234, 1.23, 2.0, 3.46, 19.99, -211.7, 903.2, lap=2, segment=5
    )
