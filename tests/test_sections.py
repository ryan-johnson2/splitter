from dataclasses import dataclass

import pytest

from splitter.core import sections as sec
from splitter.core.geometry import GateGeometry


@dataclass(frozen=True)
class G:
    seq: int
    lap: int
    gate: int
    ends_lap: int | None
    cumulative_ms: int
    gate_ms: int
    avg_speed: float | None = None
    min_speed: float | None = None
    min_accel: float | None = None


def two_laps(scale: float = 1.0, race_id: int = 1) -> sec.RunSegments:
    """The helpers' two-lap race shape: start gate, 3 segments per lap, S/F closes laps."""
    ms = [int(v * scale) for v in (1000, 2000, 4000, 6000, 8000, 10000, 12000, 14000)]
    xs = [
        G(0, 0, 1, None, ms[0], ms[0]),
        G(1, 1, 2, None, ms[1], ms[1] - ms[0]),
        G(2, 1, 3, None, ms[2], ms[2] - ms[1], 20.0, 15.0, -30.0),
        G(3, 1, 4, None, ms[3], ms[3] - ms[2], 22.0, 18.0, -20.0),
        G(4, 2, 2, 1, ms[4], ms[4] - ms[3], 25.0, 21.0, -10.0),
        G(5, 2, 3, None, ms[5], ms[5] - ms[4], 20.0, 14.0, -35.0),
        G(6, 2, 4, None, ms[6], ms[6] - ms[5], 22.0, 18.0, -20.0),
        G(7, 2, 5, 2, ms[7], ms[7] - ms[6], 25.0, 21.0, -10.0),
    ]
    return sec.RunSegments(race_id, True, sec.lap_segments(xs))


def test_lap_segments_skip_the_holeshot_and_close_on_the_finish_crossing() -> None:
    run = two_laps()
    assert sorted(run.laps) == [1, 2]
    assert [g.gate for g in run.laps[1]] == [3, 4, 2]
    assert [g.gate for g in run.laps[2]] == [3, 4, 5]
    assert sec.segment_labels(run.laps[1]) == {1: "G3", 2: "G4", 3: "S/F"}


def test_lap_segments_keep_a_trailing_partial_lap() -> None:
    xs = [G(0, 0, 1, None, 1000, 1000), G(1, 1, 2, None, 2000, 1000), G(2, 1, 3, None, 4000, 2000)]
    assert [len(v) for v in sec.lap_segments(xs).values()] == [1]


def test_validate_normalises_and_rejects_gaps() -> None:
    ok = sec.validate([sec.Section(9, "  ", 1, 2), sec.Section(3, "The S", 3, 5)], 5)
    assert [(s.ordinal, s.name, s.first, s.last) for s in ok] == [
        (1, "Section 1", 1, 2),
        (2, "The S", 3, 5),
    ]
    with pytest.raises(ValueError):
        sec.validate([sec.Section(1, "a", 1, 2), sec.Section(2, "b", 4, 5)], 5)
    with pytest.raises(ValueError):
        sec.validate([sec.Section(1, "a", 1, 5)], 6)
    with pytest.raises(ValueError):
        sec.validate([], 3)


def test_suggest_splits_where_the_path_turns() -> None:
    turns = {3: 90.0, 6: 95.0, 9: 88.0}
    geom = [GateGeometry(k, 10.0, turns.get(k, 5.0)) for k in range(1, 13)]
    got = sec.suggest(12, geom)
    assert [(s.first, s.last) for s in got] == [(1, 3), (4, 6), (7, 9), (10, 12)]
    assert got[0].name == "Section 1" and got[-1].ordinal == 4


def test_suggest_uses_speed_regimes_and_falls_back_to_thirds() -> None:
    speeds: list[float | None] = [25.0] * 4 + [10.0] * 4 + [25.0] * 4
    got = sec.suggest(12, None, speeds)
    assert [(s.first, s.last) for s in got] == [(1, 4), (5, 8), (9, 12)]
    assert [(s.first, s.last) for s in sec.suggest(12)] == [(1, 4), (5, 8), (9, 12)]
    assert [(s.first, s.last) for s in sec.suggest(4)] == [(1, 4)]


def test_suggest_keeps_sections_at_least_min_gates_long() -> None:
    geom = [GateGeometry(k, 10.0, 120.0) for k in range(1, 10)]  # a turn at every gate
    got = sec.suggest(9, geom, min_gates=3, max_sections=8)
    assert all(s.size >= 3 for s in got) and got[-1].last == 9


def test_stats_measure_every_complete_lap_against_the_pb() -> None:
    pb, slow, faster = two_laps(1.0, 1), two_laps(1.2, 2), two_laps(0.9, 3)
    sections = [sec.Section(1, "Opening", 1, 2), sec.Section(2, "Home", 3, 3)]
    stats = sec.stats(sections, [pb, slow, faster], 3, pb_race_id=1, crashes_by_segment={2: 4})
    opening, home = stats
    assert opening.instances == 6
    assert opening.best_ms == 3600 and opening.pb_ms == 4000 and opening.on_table_ms == 400
    assert opening.crashes == 4 and home.crashes == 0
    assert (
        opening.pb_min_speed == 14.5  # mean over the PB's laps (15 and 14)
        and opening.pb_exit_speed == 22.0
        and opening.pb_min_accel == -32.5
    )
    assert home.best_ms == 1800 and home.on_table_ms == 200
    assert opening.verdict == "line"
    assert [t[0] for t in opening.trend] == [1, 2, 3]


def test_verdicts() -> None:
    tight = [1000, 1010, 1020, 1030, 1040]
    assert sec._verdict(tight, pb=1010) == "solid"
    assert sec._verdict(tight, pb=1300) == "line"
    blown = [1000, 1000, 1000, 1000, 1000, 1000, 1600, 1700]
    assert sec._verdict(blown, pb=1000) == "mistakes"
    assert sec._verdict([1000, 1000], pb=1000) == "-"
