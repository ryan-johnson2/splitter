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
    assert crossings[0].gate_ms == 2000 and crossings[0].cumulative_ms == 2000
    assert all(c.gate_ms == 2000 for c in crossings)
    assert len(tr.laps) == 2
    lap1, lap2 = tr.laps
    assert (lap1.lap, lap1.lap_ms, lap1.cumulative_ms) == (1, 8000, 8000)
    assert (lap2.lap, lap2.lap_ms, lap2.cumulative_ms) == (2, 8000, 16000)
    assert lap1.gates == 4 and lap2.gates == 4
    assert tr.gates_per_lap == 4
    assert tr.finished and tr.total_ms == 16000
    # The crossing that closed lap 1 is reported by the game as (2, 1).
    closer = crossings[3]
    assert closer.lap == 2 and closer.gate == 1 and closer.lap_done is lap1
    assert crossings[-1].finished and crossings[-1].lap_done is lap2


def test_lap_elapsed_resets_after_lap() -> None:
    tr = RaceTracker()
    crossings = run(tr, two_lap_race())
    assert crossings[2].lap_elapsed_ms == 6000
    assert crossings[3].lap_elapsed_ms == 8000  # closing crossing reports the full lap
    assert crossings[4].lap_elapsed_ms == 2000  # first gate of lap 2


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
    assert tr.update(1, 1, 0, False) is None
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
    assert [lap.lap_ms for lap in tr.laps] == [3000, 2000]
    assert tr.total_ms == 5000


def test_finished_flag_on_same_key_counts_once() -> None:
    tr = RaceTracker()
    tr.update(1, 2, 1000, False)
    c = tr.update(1, 3, 2000, True)
    assert c is not None and c.finished
    assert tr.update(1, 3, 2000, True) is None
