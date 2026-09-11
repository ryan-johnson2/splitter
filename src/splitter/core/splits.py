"""Reference (personal best) splits and the deltas against them."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Reference:
    """The run a live race is compared against, indexed by crossing seq."""

    race_id: int
    total_ms: int
    cumulative_by_seq: list[int] = field(default_factory=list)
    gate_ms_by_seq: list[int] = field(default_factory=list)
    lap_ms_by_lap: dict[int, int] = field(default_factory=dict)
    gates_per_lap: int | None = None

    def split_at(self, seq: int, cumulative_ms: int) -> int | None:
        """Cumulative delta at the same crossing index (negative = ahead)."""
        if seq < len(self.cumulative_by_seq):
            return cumulative_ms - self.cumulative_by_seq[seq]
        return None

    def gate_delta(self, seq: int, gate_ms: int) -> int | None:
        """Segment delta at the same crossing index."""
        if seq < len(self.gate_ms_by_seq):
            return gate_ms - self.gate_ms_by_seq[seq]
        return None

    def lap_delta(self, lap: int, lap_ms: int) -> int | None:
        ref = self.lap_ms_by_lap.get(lap)
        return lap_ms - ref if ref is not None else None

    def total_delta(self, total_ms: int) -> int:
        return total_ms - self.total_ms


def build_reference(
    race_id: int,
    total_ms: int,
    crossings: list[tuple[int, int, int]],
    laps: list[tuple[int, int]],
    gates_per_lap: int | None,
) -> Reference:
    """Assemble a Reference from stored rows.

    ``crossings`` are ``(seq, cumulative_ms, gate_ms)`` in seq order;
    ``laps`` are ``(lap, lap_ms)``.
    """
    ordered = sorted(crossings)
    return Reference(
        race_id=race_id,
        total_ms=total_ms,
        cumulative_by_seq=[c[1] for c in ordered],
        gate_ms_by_seq=[c[2] for c in ordered],
        lap_ms_by_lap={lap: ms for lap, ms in laps},
        gates_per_lap=gates_per_lap,
    )


def theoretical_best(gate_ms_by_race: list[list[int]]) -> int | None:
    """Sum of the fastest segment at every crossing index across finished runs.

    Only runs with the same number of crossings are comparable; the modal
    length wins so one aborted or mis-scored run can't poison the result.
    """
    runs = [r for r in gate_ms_by_race if r]
    if not runs:
        return None
    lengths = [len(r) for r in runs]
    n = max(set(lengths), key=lengths.count)
    same = [r for r in runs if len(r) == n]
    return sum(min(r[i] for r in same) for i in range(n))
