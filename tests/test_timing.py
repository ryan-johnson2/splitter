from __future__ import annotations

from splitter.core.timing import RaceTracker
from tests.helpers import two_lap_race


def run(tracker: RaceTracker, script: list[tuple[int, int, float, bool]]) -> list:
    out = []
    for lap, gate, t, fin in script:
        c = tracker.update(lap, gate, round(t * 1000), fin)
        if c is not None:
            out.append(c)
    return out


def test_two_lap_race_crossings_and_laps() -> None:
    tr = RaceTracker()
    crossings = run(tr, two_lap_race())
    assert [c.seq for c in crossings] == list(range(8))
    assert [c.cumulative_ms for c in crossings] == [
        1000,
        2000,
        4000,
        6000,
        8000,
        10000,
        12000,
        14000,
    ]
    assert crossings[0].gate_ms == 1000 and crossings[1].gate_ms == 1000
    assert all(c.gate_ms == 2000 for c in crossings[2:])
    # Holeshot: GO → first start/finish crossing (the first crossing carrying lap 1).
    assert tr.holeshot_ms == 2000
    assert crossings[1].starts_lap == 1 and crossings[1].lap_done is None
    assert len(tr.laps) == 2
    lap1, lap2 = tr.laps
    assert (lap1.lap, lap1.lap_ms, lap1.cumulative_ms) == (1, 6000, 8000)
    assert (lap2.lap, lap2.lap_ms, lap2.cumulative_ms) == (2, 6000, 14000)
    assert lap1.gates == 3 and lap2.gates == 3
    assert tr.gates_per_lap == 3
    assert tr.finished and tr.total_ms == 14000 and tr.current_lap == 2
    # The start/finish crossing reported as (2, 2) closes lap 1 and starts lap 2.
    closer = crossings[4]
    assert closer.lap == 2 and closer.gate == 2
    assert closer.lap_done is lap1 and closer.starts_lap == 2
    assert crossings[-1].finished and crossings[-1].lap_done is lap2
    assert crossings[-1].starts_lap is None


def test_game_lap_times_reproduced() -> None:
    """The real capture of 2026-09-12: the game showed 47.005 / 46.180 / 47.988, total 144.359."""
    tr = RaceTracker()
    script = [(0, 1, 1.883, False), (1, 2, 3.186, False), (1, 3, 4.5, False), (1, 40, 49.0, False)]
    script += [(2, 2, 50.191, False), (2, 40, 95.0, False), (3, 2, 96.371, False)]
    script += [(3, 40, 143.0, False), (3, 41, 144.359, True)]
    run(tr, script)
    assert tr.holeshot_ms == 3186
    assert [lap.lap_ms for lap in tr.laps] == [47005, 46180, 47988]
    assert tr.total_ms == 144359 == 3186 + sum(lap.lap_ms for lap in tr.laps)


def test_lap_elapsed_resets_after_lap() -> None:
    tr = RaceTracker()
    crossings = run(tr, two_lap_race())
    assert crossings[0].lap_elapsed_ms == 1000  # holeshot in progress
    assert crossings[1].lap_elapsed_ms == 2000  # the S/F crossing that ends the holeshot
    assert crossings[2].lap_elapsed_ms == 2000  # first gate of lap 1
    assert crossings[4].lap_elapsed_ms == 6000  # closing crossing reports the full lap
    assert crossings[5].lap_elapsed_ms == 2000  # first gate of lap 2


def test_current_lap_during_holeshot() -> None:
    tr = RaceTracker()
    assert tr.current_lap == 0
    tr.update(0, 1, 1000, False)
    assert tr.current_lap == 0 and tr.holeshot_ms is None
    tr.update(1, 2, 2000, False)
    assert tr.current_lap == 1 and tr.holeshot_ms == 2000 and tr.lap_start_ms == 2000


def test_first_crossing_already_lap_one() -> None:
    """No start-only gate: the first crossing is the start/finish itself."""
    tr = RaceTracker()
    c = tr.update(1, 1, 1500, False)
    assert c is not None and c.starts_lap == 1 and tr.holeshot_ms == 1500 and tr.laps == []
    tr.update(1, 2, 3000, False)
    c = tr.update(2, 1, 5000, False)
    assert c is not None and c.lap_done is not None and c.lap_done.lap_ms == 3500


def test_duplicate_snapshots_are_ignored() -> None:
    tr = RaceTracker()
    assert tr.update(1, 2, 2000, False) is not None
    assert tr.update(1, 2, 2000, False) is None  # exact repeat
    assert tr.update(1, 2, 2100, False) is None  # same key, later clock: no new crossing
    assert len(tr.crossings) == 1
    assert tr.update(1, 3, 2100, False) is not None  # next gate


def test_out_of_order_time_is_ignored() -> None:
    tr = RaceTracker()
    tr.update(1, 2, 2000, False)
    assert tr.update(1, 3, 1500, False) is None
    assert tr.update(1, 4, 3000, False) is not None


def test_start_line_snapshot_ignored() -> None:
    tr = RaceTracker()
    assert tr.update(0, 1, 0, False) is None
    assert tr.crossings == []


def test_nothing_after_finish() -> None:
    tr = RaceTracker()
    run(tr, two_lap_race())
    assert tr.update(2, 5, 17000, True) is None
    assert tr.update(3, 1, 18000, False) is None


def test_finish_without_distinct_gate() -> None:
    """Tracks without a start/finish gate: finish arrives on a normal gate ordinal."""
    tr = RaceTracker()
    tr.update(1, 2, 1000, False)
    tr.update(1, 3, 2000, False)
    tr.update(2, 1, 3000, False)
    tr.update(2, 2, 4000, False)
    c = tr.update(2, 3, 5000, True)
    assert c is not None and c.finished and c.lap_done is not None
    assert [lap.lap_ms for lap in tr.laps] == [2000, 2000] and tr.holeshot_ms == 1000
