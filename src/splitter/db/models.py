"""SQLAlchemy ORM models. All timestamps are naive UTC."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, LargeBinary, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Setting(Base):
    """Runtime settings (game PC address, player name, telemetry options, last session)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]


class Race(Base):
    """One run from GO to finish/abort."""

    __tablename__ = "races"
    __table_args__ = (
        Index("ix_races_track_quad", "track_name", "quad_type", "race_laps"),
        Index("ix_races_pb_key", "track_id", "quad_model_id", "race_laps"),
        Index("ix_races_uuid", "uuid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Identity minted where the run was captured (docs/sync-design.md). ``uuid``
    # is what other installs know the run by; ``node_id`` the capturing install;
    # ``seq`` its per-node upload counter (0 = pre-sync or not yet queued).
    uuid: Mapped[str] = mapped_column(default="")
    node_id: Mapped[str] = mapped_column(default="")
    seq: Mapped[int] = mapped_column(default=0)
    origin: Mapped[str] = mapped_column(default="capture")  # capture | import | ingest
    received_at: Mapped[datetime | None]  # when an import/ingest landed here; never for capture
    track_name: Mapped[str] = mapped_column(default="")
    scenery: Mapped[str] = mapped_column(default="")
    # Online track identity when the session came from the picker (else 0 / "").
    track_id: Mapped[int] = mapped_column(default=0)
    scene_id: Mapped[int] = mapped_column(default=0)
    track_source: Mapped[str] = mapped_column(default="")  # official | community
    quad_type: Mapped[str] = mapped_column(default="")
    quad_size: Mapped[str] = mapped_column(default="")
    # Catalog identity of the quad (0 when unknown). PBs are keyed by
    # (track_id, quad_model_id, race_laps); a race with track_id 0 never is one.
    quad_model_id: Mapped[int] = mapped_column(default=0)
    quad_class_id: Mapped[int] = mapped_column(default=0)
    race_mode: Mapped[str] = mapped_column(default="")  # e.g. THREE_LAP_SINGLE_CLASS
    race_format: Mapped[str] = mapped_column(default="")  # e.g. NORMAL
    race_laps: Mapped[int] = mapped_column(default=0)
    player_name: Mapped[str] = mapped_column(default="")
    # Where the track/quad came from: game (session event) | sticky (last used) | manual
    session_source: Mapped[str] = mapped_column(default="")
    start_finish_gate: Mapped[bool | None]
    status: Mapped[str] = mapped_column(default="running")  # running|finished|aborted
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    total_time_ms: Mapped[int | None]
    holeshot_ms: Mapped[int | None]  # GO → first start/finish crossing (in the total, not a lap)
    total_laps: Mapped[int] = mapped_column(default=0)
    gates_per_lap: Mapped[int | None]
    is_best: Mapped[bool] = mapped_column(default=False)
    reference_race_id: Mapped[int | None]  # the PB this run was compared against (local row)
    reference_uuid: Mapped[str] = mapped_column(default="")  # ...and its portable identity
    pb_delta_ms: Mapped[int | None]  # total vs reference at the time
    telemetry_samples: Mapped[int] = mapped_column(default=0)
    max_speed: Mapped[float | None]
    avg_speed: Mapped[float | None]
    distance_m: Mapped[float | None]
    notes: Mapped[str] = mapped_column(Text, default="")
    # Crashes found in the trace (core/crashes.py), JSON list of Crash dicts.
    crashes: Mapped[str] = mapped_column(Text, default="")
    crash_count: Mapped[int] = mapped_column(default=0)
    # Layout fingerprint (core/fingerprint.py), JSON, "" when the run had no trace.
    fingerprint: Mapped[str] = mapped_column(Text, default="")

    laps: Mapped[list[Lap]] = relationship(
        cascade="all, delete-orphan", order_by="Lap.lap", lazy="selectin"
    )
    gate_times: Mapped[list[GateTime]] = relationship(
        cascade="all, delete-orphan", order_by="GateTime.seq", lazy="selectin"
    )


class Lap(Base):
    __tablename__ = "laps"
    __table_args__ = (Index("ix_laps_race", "race_id", "lap"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id", ondelete="CASCADE"))
    lap: Mapped[int]
    lap_ms: Mapped[int]
    cumulative_ms: Mapped[int]
    gates: Mapped[int] = mapped_column(default=0)
    delta_ms: Mapped[int | None]  # vs the reference lap
    max_speed: Mapped[float | None]
    avg_speed: Mapped[float | None]
    distance_m: Mapped[float | None]
    min_speed: Mapped[float | None]
    min_accel: Mapped[float | None]  # hardest braking, m/s^2
    max_accel: Mapped[float | None]


class GateTime(Base):
    __tablename__ = "gate_times"
    __table_args__ = (Index("ix_gate_times_race", "race_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id", ondelete="CASCADE"))
    seq: Mapped[int]  # 0-based crossing index within the race
    lap: Mapped[int]  # as reported by the game
    gate: Mapped[int]  # as reported by the game
    cumulative_ms: Mapped[int]
    gate_ms: Mapped[int]
    lap_elapsed_ms: Mapped[int] = mapped_column(default=0)
    split_ms: Mapped[int | None]  # cumulative delta vs reference at this seq
    ends_lap: Mapped[int | None]  # the lap this crossing closed, if any
    max_speed: Mapped[float | None]
    avg_speed: Mapped[float | None]
    distance_m: Mapped[float | None]
    min_speed: Mapped[float | None]
    min_accel: Mapped[float | None]  # hardest braking, m/s^2
    max_accel: Mapped[float | None]


class TrackSection(Base):
    """One named group of consecutive gates on a track (see core/sections.py)."""

    __tablename__ = "track_sections"
    __table_args__ = (Index("ix_track_sections_track", "track_id", "ordinal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int]  # online track id; sections apply to every run on the track
    ordinal: Mapped[int]
    name: Mapped[str] = mapped_column(default="")
    first_gate: Mapped[int]  # lap-relative segment index, inclusive
    last_gate: Mapped[int]
    updated_at: Mapped[datetime]


class TelemetryBlob(Base):
    """The downsampled IMU trace of one race as one compressed columnar blob
    (``core/telemetry.py::encode_columns``). Replaces ``telemetry`` rows."""

    __tablename__ = "telemetry_blobs"

    race_id: Mapped[int] = mapped_column(
        ForeignKey("races.id", ondelete="CASCADE"), primary_key=True
    )
    hz: Mapped[float]
    samples: Mapped[int]
    columns: Mapped[str] = mapped_column(Text)  # JSON list of column names, in blob order
    encoding: Mapped[str]
    data: Mapped[bytes] = mapped_column(LargeBinary)


class TelemetrySample(Base):
    """Downsampled IMU trace, one row per sample. Legacy: converted to
    ``telemetry_blobs`` at startup (``repos.migrate_telemetry``) and kept only
    until ``splitter migrate-telemetry --drop``."""

    __tablename__ = "telemetry"
    __table_args__ = (Index("ix_telemetry_race", "race_id", "t_ms"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    race_id: Mapped[int] = mapped_column(ForeignKey("races.id", ondelete="CASCADE"))
    t_ms: Mapped[int]
    x: Mapped[float]
    y: Mapped[float]
    z: Mapped[float]
    vx: Mapped[float]
    vy: Mapped[float]
    vz: Mapped[float]
    speed: Mapped[float]
    roll: Mapped[float]
    pitch: Mapped[float]
    yaw: Mapped[float]
    qx: Mapped[float]
    qy: Mapped[float]
    qz: Mapped[float]
    qw: Mapped[float]


class EventLog(Base):
    """Raw game frames as received (imu sampled), for protocol debugging."""

    __tablename__ = "event_log"
    __table_args__ = (Index("ix_event_log_received", "received_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime]
    race_id: Mapped[int | None]
    event_type: Mapped[str]
    payload: Mapped[str] = mapped_column(Text, default="")
