"""IMU sample buffering, per-segment statistics and storage downsampling.

The game streams ``imu`` at 60 Hz for the local drone whenever it is flying.
Splitter keeps every sample of a race in memory (a few thousand rows),
derives per-gate speed/distance from the full-rate data, and stores a
downsampled trace for the flight-path and speed views.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise


@dataclass(frozen=True)
class Sample:
    t_ms: int  # race clock
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    speed: float  # |v| in m/s
    roll: float
    pitch: float
    yaw: float
    qx: float
    qy: float
    qz: float
    qw: float


@dataclass(frozen=True)
class SegmentStats:
    samples: int
    max_speed: float
    avg_speed: float
    min_speed: float
    distance_m: float
    min_accel: float | None = None  # hardest braking in the window, m/s^2 (negative)
    max_accel: float | None = None  # hardest acceleration, m/s^2


class TelemetryBuffer:
    """Accumulates samples for one race."""

    def __init__(self) -> None:
        self.samples: list[Sample] = []
        self._offset_ms: float | None = None  # game timestamp minus race clock

    def clear(self) -> None:
        self.samples.clear()
        self._offset_ms = None

    def align(self, game_timestamp_ms: float, race_ms: int) -> None:
        """Pin the game's IMU clock to the race clock (done once, on the first sample after GO)."""
        if self._offset_ms is None:
            self._offset_ms = game_timestamp_ms - race_ms

    def race_ms_for(self, game_timestamp_ms: float, fallback_race_ms: int) -> int:
        if self._offset_ms is None:
            self.align(game_timestamp_ms, fallback_race_ms)
        assert self._offset_ms is not None
        return round(game_timestamp_ms - self._offset_ms)

    def add(
        self,
        t_ms: int,
        position: tuple[float, float, float],
        speed: tuple[float, float, float],
        rates: tuple[float, float, float],
        attitude: tuple[float, float, float, float],
    ) -> Sample:
        vx, vy, vz = speed
        sample = Sample(
            t_ms=t_ms,
            x=position[0],
            y=position[1],
            z=position[2],
            vx=vx,
            vy=vy,
            vz=vz,
            speed=math.sqrt(vx * vx + vy * vy + vz * vz),
            roll=rates[0],
            pitch=rates[1],
            yaw=rates[2],
            qx=attitude[0],
            qy=attitude[1],
            qz=attitude[2],
            qw=attitude[3],
        )
        self.samples.append(sample)
        return sample

    def segment(self, from_ms: int, to_ms: int) -> SegmentStats | None:
        """Stats over samples with ``from_ms < t <= to_ms``."""
        window = [s for s in self.samples if from_ms < s.t_ms <= to_ms]
        return segment_stats(window)

    def downsampled(self, hz: float) -> list[Sample]:
        """Keep at most ``hz`` samples per second (the first in each bucket)."""
        if hz <= 0 or not self.samples:
            return list(self.samples)
        bucket_ms = 1000.0 / hz
        out: list[Sample] = []
        last_bucket = -1
        for s in self.samples:
            bucket = int(s.t_ms // bucket_ms)
            if bucket != last_bucket:
                out.append(s)
                last_bucket = bucket
        return out


def segment_stats(window: list[Sample]) -> SegmentStats | None:
    if not window:
        return None
    speeds = [s.speed for s in window]
    distance = 0.0
    accels: list[float] = []
    for a, b in pairwise(window):
        distance += math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
        dt = (b.t_ms - a.t_ms) / 1000.0
        if 0 < dt <= 0.5:
            accels.append((b.speed - a.speed) / dt)
    return SegmentStats(
        samples=len(window),
        max_speed=max(speeds),
        avg_speed=sum(speeds) / len(speeds),
        min_speed=min(speeds),
        distance_m=distance,
        min_accel=min(accels) if accels else None,
        max_accel=max(accels) if accels else None,
    )


# ── columnar storage ──────────────────────────────────────────────
#
# A trace is stored (and shipped in the run document) as one compressed blob:
# every column of every sample, column-major, ``t_ms`` as int32 and the rest as
# float32, little-endian, zlib. Roughly a fifth of the size of a row per sample
# before SQLite's own overhead, and the same bytes on disk and on the wire.

COLUMNS: tuple[str, ...] = (
    "t_ms",
    "x",
    "y",
    "z",
    "vx",
    "vy",
    "vz",
    "speed",
    "roll",
    "pitch",
    "yaw",
    "qx",
    "qy",
    "qz",
    "qw",
)
ENCODING = "cols1+zlib"


def encode_columns(samples: Sequence[Sample]) -> bytes:
    """Pack samples into the ``cols1+zlib`` blob (empty bytes for no samples)."""
    import zlib
    from array import array

    if not samples:
        return b""
    parts: list[bytes] = []
    for name in COLUMNS:
        values = [getattr(s, name) for s in samples]
        arr = array("i", [int(v) for v in values]) if name == "t_ms" else array("f", values)
        if sys.byteorder == "big":
            arr.byteswap()
        parts.append(arr.tobytes())
    return zlib.compress(b"".join(parts), 6)


def decode_columns(data: bytes, count: int, encoding: str = ENCODING) -> list[Sample]:
    """Unpack a blob written by :func:`encode_columns`; raises ``ValueError`` on
    a size mismatch or an encoding this build does not know."""
    import zlib
    from array import array

    if encoding != ENCODING:
        raise ValueError(f"unknown telemetry encoding {encoding!r}")
    if count <= 0 or not data:
        return []
    raw = zlib.decompress(data)
    width = 4  # int32 and float32 are both four bytes
    if len(raw) != width * count * len(COLUMNS):
        raise ValueError("telemetry blob does not match its sample count")
    columns: list[list[float]] = []
    for i, name in enumerate(COLUMNS):
        chunk = raw[i * width * count : (i + 1) * width * count]
        arr = array("i" if name == "t_ms" else "f")
        arr.frombytes(chunk)
        if sys.byteorder == "big":
            arr.byteswap()
        columns.append(list(arr))
    return [
        Sample(
            t_ms=int(columns[0][j]),
            x=columns[1][j],
            y=columns[2][j],
            z=columns[3][j],
            vx=columns[4][j],
            vy=columns[5][j],
            vz=columns[6][j],
            speed=columns[7][j],
            roll=columns[8][j],
            pitch=columns[9][j],
            yaw=columns[10][j],
            qx=columns[11][j],
            qy=columns[12][j],
            qz=columns[13][j],
            qw=columns[14][j],
        )
        for j in range(count)
    ]
