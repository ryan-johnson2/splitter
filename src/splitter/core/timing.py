"""Gate-crossing state machine.

Feed it the (lap, gate, cumulative time, finished) tuple from every
``racedata`` snapshot for one pilot and it yields one :class:`Crossing` per
new checkpoint, tagging the crossing that closes a lap.

VelociDrone numbering, verified against the game's own lap times on
2026-09-12 (USADT Champs Trial 01, two 3-lap runs):

- ``gate`` is 1-based, counts every checkpoint and resets each lap. Before
  the start/finish gate there may be start-only gates, reported with
  ``lap`` 0 (e.g. ``lap 0 gate 1``).
- The lap counter increments **at the start/finish gate**. The first crossing
  reporting ``lap`` *n* is the start of lap *n* — so lap 1 begins at the first
  start/finish crossing, and everything before it (GO → that crossing) is the
  **holeshot**, which the game counts in the total but not in any lap.
- Lap *n* ends on the first crossing reporting lap *n+1* (the same
  start/finish crossing that starts the next lap). On the final lap the
  counter cannot roll over: the finish crossing arrives with ``finished`` set
  and a gate ordinal one past the per-lap count.

Wire example (3 laps, 39 checkpoints per lap): ``0/1 1.883`` (start gate),
``1/2 3.186`` (S/F: holeshot 3.186), ``1/3 …`` … ``2/2 50.191`` (lap 1 =
47.005), … ``3/2 96.371`` (lap 2 = 46.180), … ``3/41 144.359 finished``
(lap 3 = 47.988) — the game shows exactly those laps and total.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LapDone:
    lap: int
    lap_ms: int
    cumulative_ms: int
    first_seq: int  # first crossing after the one that started the lap
    last_seq: int  # the crossing that closed it (a start/finish or finish crossing)

    @property
    def gates(self) -> int:
        """Checkpoints flown in the lap, start/finish included."""
        return self.last_seq - self.first_seq + 1


@dataclass(frozen=True)
class Crossing:
    seq: int  # 0-based index within the race; the key for split comparison
    lap: int  # as reported by the game (0 = before the first start/finish crossing)
    gate: int  # as reported by the game
    cumulative_ms: int  # race clock at the crossing
    gate_ms: int  # since the previous crossing (since GO for seq 0)
    lap_elapsed_ms: int  # since the start of the lap this crossing belongs to
    finished: bool
    lap_done: LapDone | None = None  # set when this crossing closed a lap
    starts_lap: int | None = None  # set when this crossing is a start/finish crossing


@dataclass
class RaceTracker:
    """Turns racedata snapshots for one pilot into crossings and laps."""

    crossings: list[Crossing] = field(default_factory=list)
    laps: list[LapDone] = field(default_factory=list)
    holeshot_ms: int | None = None  # GO → first start/finish crossing
    _last_key: tuple[int, int] | None = None
    _last_cum: int = 0
    _last_finished: bool = False
    _current_lap: int = 0  # 0 until the first start/finish crossing
    _lap_start_ms: int = 0
    _lap_first_seq: int = 0

    @property
    def finished(self) -> bool:
        return self._last_finished

    @property
    def total_ms(self) -> int:
        return self._last_cum

    @property
    def current_lap(self) -> int:
        """Lap in progress; 0 during the holeshot."""
        return self._current_lap

    @property
    def lap_start_ms(self) -> int:
        return self._lap_start_ms

    def update(self, lap: int, gate: int, cumulative_ms: int, finished: bool) -> Crossing | None:
        """Consume one snapshot; return the crossing it represents, if any."""
        if self._last_finished:
            return None
        key = (lap, gate)
        is_new_key = key != self._last_key
        became_finished = finished and not self._last_finished
        if not is_new_key and not became_finished:
            return None
        if self.crossings and cumulative_ms <= self._last_cum:
            # Out-of-order snapshot: the race clock did not advance, so it cannot
            # be a real crossing. Keep the old key so a corrected one still counts.
            return None
        if cumulative_ms <= 0:
            # A start-line "crossing" at t=0 carries no timing information.
            self._last_key = key
            return None

        seq = len(self.crossings)
        gate_ms = cumulative_ms - self._last_cum
        lap_done: LapDone | None = None
        starts_lap: int | None = None

        if lap > self._current_lap:
            # Start/finish crossing: closes the lap in progress (if any) and
            # starts lap ``lap``. The very first one ends the holeshot instead.
            if self._current_lap > 0:
                lap_done = self._close_lap(seq, cumulative_ms)
            else:
                self.holeshot_ms = cumulative_ms
            starts_lap = lap
        elif finished:
            lap_done = self._close_lap(seq, cumulative_ms)

        # A crossing that closed a lap belongs to that lap for elapsed-time purposes.
        lap_elapsed = (
            lap_done.lap_ms if lap_done is not None else cumulative_ms - self._lap_start_ms
        )
        crossing = Crossing(
            seq=seq,
            lap=lap,
            gate=gate,
            cumulative_ms=cumulative_ms,
            gate_ms=gate_ms,
            lap_elapsed_ms=lap_elapsed,
            finished=finished,
            lap_done=lap_done,
            starts_lap=starts_lap,
        )
        self.crossings.append(crossing)
        if lap_done is not None:
            self.laps.append(lap_done)
        if starts_lap is not None:
            self._current_lap = starts_lap
            self._lap_start_ms = cumulative_ms
            self._lap_first_seq = seq + 1
        self._last_key = key
        self._last_cum = cumulative_ms
        self._last_finished = finished
        return crossing

    def _close_lap(self, last_seq: int, cumulative_ms: int) -> LapDone:
        return LapDone(
            lap=len(self.laps) + 1,
            lap_ms=cumulative_ms - self._lap_start_ms,
            cumulative_ms=cumulative_ms,
            first_seq=self._lap_first_seq,
            last_seq=last_seq,
        )

    @property
    def gates_per_lap(self) -> int | None:
        """Checkpoints in the first completed lap (the track's per-lap count)."""
        return self.laps[0].gates if self.laps else None
