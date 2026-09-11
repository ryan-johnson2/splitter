"""Gate-crossing state machine.

Feed it the (lap, gate, cumulative time, finished) tuple from every
``racedata`` snapshot for one pilot and it yields one :class:`Crossing` per
new checkpoint, tagging the crossing that closes a lap.

VelociDrone numbering (1.17.13): ``gate`` is 1-based, counts every gate on
the track and resets each lap. Crossing the start/finish gate at the end of
lap *n* is reported as lap *n+1*, gate 1 — so a lap ends on the first
crossing that carries a higher lap number. On the final lap the counter
cannot roll over, so the finish crossing arrives with ``finished`` set (and a
gate one past the per-lap count on tracks with a distinct start/finish gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LapDone:
    lap: int
    lap_ms: int
    cumulative_ms: int
    first_seq: int
    last_seq: int

    @property
    def gates(self) -> int:
        return self.last_seq - self.first_seq + 1


@dataclass(frozen=True)
class Crossing:
    seq: int  # 0-based index within the race; the key for split comparison
    lap: int  # as reported by the game
    gate: int  # as reported by the game
    cumulative_ms: int  # race clock at the crossing
    gate_ms: int  # since the previous crossing (since GO for seq 0)
    lap_elapsed_ms: int  # since the start of the lap this crossing belongs to
    finished: bool
    lap_done: LapDone | None = None  # set when this crossing closed a lap


@dataclass
class RaceTracker:
    """Turns racedata snapshots for one pilot into crossings and laps."""

    crossings: list[Crossing] = field(default_factory=list)
    laps: list[LapDone] = field(default_factory=list)
    _last_key: tuple[int, int] | None = None
    _last_cum: int = 0
    _last_finished: bool = False
    _current_lap: int = 1
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
            # The start-line "crossing" at t=0 carries no timing information.
            self._last_key = key
            return None

        seq = len(self.crossings)
        gate_ms = cumulative_ms - self._last_cum
        lap_done: LapDone | None = None

        if lap > self._current_lap and self.crossings:
            lap_done = self._close_lap(seq, cumulative_ms)
            self._current_lap = lap
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
        )
        self.crossings.append(crossing)
        if lap_done is not None:
            self.laps.append(lap_done)
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
        """Crossings in the first completed lap (the track's gate count)."""
        return self.laps[0].gates if self.laps else None
