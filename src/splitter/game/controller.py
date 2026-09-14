"""Glue between the game feed, the timing core, the database and the live hub.

Lifecycle of one run, driven purely by game events:

    racestatus:start  → armed (a race is about to begin)
    countdown:0       → GO: create the Race row, load the PB reference
    racedata          → crossings → GateTime rows (+ Lap rows) + live "crossing" messages
    finished crossing / racestatus:"race finished" → finalize, re-flag the PB
    racestatus:abort  → mark aborted (or drop the row if nothing was recorded)

If single player has the countdown turned off, no countdown frames arrive;
the first racedata after arming starts the race instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from velocidrone_ws import (
    CountdownEvent,
    Event,
    FinishGateEvent,
    ImuEvent,
    PlayerEvent,
    PlayerRaceData,
    RaceDataEvent,
    RaceStatusEvent,
    RaceTypeEvent,
    SessionEvent,
)

from splitter.core import crashes as crash_detect
from splitter.core import quads
from splitter.core.splits import Reference
from splitter.core.telemetry import SegmentStats, TelemetryBuffer
from splitter.core.timing import Crossing, RaceTracker
from splitter.db import repos
from splitter.db.models import GateTime, Lap, Race, TelemetrySample
from splitter.db.runtime_settings import RuntimeSettings
from splitter.game.catalog import TrackRef
from splitter.game.session import SOURCE_GAME, SOURCE_MANUAL, SOURCE_STICKY, SessionState
from splitter.live.hub import LiveHub
from splitter.util import utcnow

log = logging.getLogger(__name__)

GAME_LOSS_GRACE_S = 10.0  # seconds without the game before a running race is aborted
IMU_LOG_EVERY_S = 5.0  # how often an imu frame lands in the event log
LIVE_TELEMETRY_HZ = 4.0  # live speed/position messages to the browser


@dataclass(frozen=True)
class _CrossingRef:
    """What crash attribution needs to know about a crossing."""

    seq: int
    lap: int
    ends_lap: int | None
    cumulative_ms: int


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _stats_dict(stats: SegmentStats | None) -> dict[str, float | None]:
    if stats is None:
        return {
            "max_speed": None,
            "avg_speed": None,
            "distance_m": None,
            "min_speed": None,
            "min_accel": None,
            "max_accel": None,
        }
    return {
        "max_speed": round(stats.max_speed, 2),
        "avg_speed": round(stats.avg_speed, 2),
        "distance_m": round(stats.distance_m, 1),
        "min_speed": round(stats.min_speed, 2),
        "min_accel": round(stats.min_accel, 1) if stats.min_accel is not None else None,
        "max_accel": round(stats.max_accel, 1) if stats.max_accel is not None else None,
    }


class RaceController:
    def __init__(
        self,
        settings: RuntimeSettings,
        session_factory: async_sessionmaker[AsyncSession],
        hub: LiveHub,
        status_provider: Callable[[], dict[str, object]] | None = None,
        resolve_track: Callable[[str], Awaitable[TrackRef | None]] | None = None,
    ) -> None:
        self._settings = settings
        self._sf = session_factory
        self._hub = hub
        self._status_provider = status_provider or (lambda: {})
        # Gives a game-named track (hosted room) its online id; None = never resolve.
        self._resolve_track = resolve_track

        self.session = SessionState()
        self.tracker: RaceTracker | None = None
        self.reference: Reference | None = None
        self.telemetry = TelemetryBuffer()
        self.game_loss_grace_s = GAME_LOSS_GRACE_S
        self._loss_task: asyncio.Task[None] | None = None
        self.race_id: int | None = None
        self.race_started_at: datetime | None = None
        self.armed = False
        self.armed_at: datetime | None = None
        self.countdown: int | None = None
        self.start_finish_gate: bool | None = None
        self.last_result: dict[str, Any] | None = None
        self.player_name = ""
        self.imu_frames = 0
        self.imu_last_at: datetime | None = None
        self._go_monotonic = 0.0
        self._imu_last_logged = 0.0
        self._imu_last_broadcast = 0.0
        self._warned_no_pilot = False

    # ── startup ──────────────────────────────────────────────────────

    def load_sticky_session(self) -> None:
        """Seed the session from the last run so single player has something to go on."""
        s = self._settings
        if s.get("last_track_name"):
            self.session = SessionState(
                track_name=s.get("last_track_name"),
                scenery=s.get("last_scenery"),
                track_id=s.get_int("last_track_id"),
                scene_id=s.get_int("last_scene_id"),
                track_source=s.get("last_track_source"),
                quad_type=s.get("last_quad_type"),
                quad_size=s.get("last_quad_size"),
                quad_model_id=s.get_int("last_quad_model_id"),
                quad_class_id=s.get_int("last_quad_class_id"),
                race_mode=s.get("last_race_mode"),
                race_laps=s.get_int("last_race_laps"),
                source=SOURCE_STICKY,
            )
        self.player_name = s.get("player_name")

    # ── public state ─────────────────────────────────────────────────

    @property
    def race_active(self) -> bool:
        return self.race_id is not None

    def snapshot(self) -> dict[str, Any]:
        """Everything a browser needs to render the live page from scratch."""
        race: dict[str, Any] | None = None
        if self.race_id is not None and self.tracker is not None:
            race = {
                "id": self.race_id,
                "started_at": _iso(self.race_started_at),
                "crossings": [self._crossing_dict(c) for c in self.tracker.crossings],
                "laps": [self._lap_dict(lap) for lap in self.tracker.laps],
                "total_ms": self.tracker.total_ms,
                "holeshot_ms": self.tracker.holeshot_ms,
                "current_lap": self.tracker.current_lap,
                "lap_start_ms": self.tracker.lap_start_ms,
                "finished": self.tracker.finished,
            }
        return {
            "connection": self._status_provider(),
            "session": self.session.to_dict(),
            "player_name": self.player_name,
            "armed": self.armed,
            "countdown": self.countdown,
            "race": race,
            "reference": self._reference_dict(),
            "last_result": self.last_result,
            "imu": {"frames": self.imu_frames, "last_at": _iso(self.imu_last_at)},
            "clients": self._hub.clients,
        }

    async def set_manual_session(
        self,
        track_name: str,
        scenery: str = "",
        quad_type: str = "",
        quad_size: str = "",
        race_laps: int = 0,
        race_mode: str = "",
        track_id: int = 0,
        scene_id: int = 0,
        track_source: str = "",
        quad_model_id: int = 0,
        quad_class_id: int = 0,
    ) -> None:
        if quad_model_id and not quad_type.strip():
            quad_type = quads.model_name(quad_model_id)
        if quad_model_id and not quad_class_id:
            model = quads.catalog().model(quad_model_id)
            quad_class_id = model.component_group_id if model else 0
        self.session = SessionState(
            track_name=track_name.strip(),
            scenery=scenery.strip(),
            track_id=track_id,
            scene_id=scene_id,
            track_source=track_source.strip(),
            quad_type=quad_type.strip(),
            quad_size=quad_size.strip(),
            quad_model_id=quad_model_id,
            quad_class_id=quad_class_id,
            race_mode=race_mode.strip() or self.session.race_mode,
            race_format=self.session.race_format,
            race_laps=race_laps or self.session.race_laps,
            player_name=self.session.player_name,
            source=SOURCE_MANUAL,
        )
        await self._persist_session()
        await self.refresh_reference()
        self._hub.broadcast("session", self.session.to_dict())
        self._hub.broadcast("reference", self._reference_dict())

    async def refresh_reference(self) -> None:
        """Load the PB the *next* run would be compared against (idle only).

        Keeps the live page honest between runs: the reference line shows the
        current PB for the session's track/quad/laps, or that there is none.
        """
        if self.race_active:
            return
        s = self.session
        async with self._sf() as db:
            self.reference = await repos.load_reference(
                db, repos.PBKey(s.track_id, s.quad_model_id, s.race_laps)
            )

    async def set_player_name(self, name: str) -> None:
        self.player_name = name.strip()
        async with self._sf() as db:
            await self._settings.set(db, "player_name", self.player_name)
        self._hub.broadcast("session", self.session.to_dict())

    def broadcast_status(self) -> None:
        self._hub.broadcast("status", self._status_provider())

    async def abort_race(self, reason: str) -> int | None:
        """End the current run as aborted (a stuck timer, or the pilot's say-so).

        Returns the race id that was aborted, or None when nothing was running.
        """
        race_id = self.race_id
        if race_id is None and not self.armed:
            return None
        log.info("aborting race %s: %s", race_id, reason)
        await self._finish_race(aborted=True)
        self._hub.broadcast("notice", {"message": f"Race aborted: {reason}", "level": "warn"})
        return race_id

    async def on_game_state(self, connected: bool) -> None:
        """Game link changed. A run cannot survive losing the game: after a short
        grace (a blip may reconnect) the race is aborted instead of ticking forever."""
        self.broadcast_status()
        if connected:
            if self._loss_task is not None:
                self._loss_task.cancel()
                self._loss_task = None
            return
        if self.race_id is None or self._loss_task is not None:
            return
        race_id = self.race_id

        async def _abort_after_grace() -> None:
            try:
                await asyncio.sleep(self.game_loss_grace_s)
            except asyncio.CancelledError:
                return
            self._loss_task = None
            if self.race_id == race_id:
                await self.abort_race("game connection lost")

        self._loss_task = asyncio.create_task(_abort_after_grace())

    # ── event entry point ────────────────────────────────────────────

    async def handle_event(self, event: Event) -> None:
        data = event.data
        if isinstance(data, ImuEvent):
            await self._on_imu(data)
            return
        await self._log_event(event)
        if isinstance(data, SessionEvent):
            await self._on_session(data)
        elif isinstance(data, RaceTypeEvent):
            await self._on_race_type(data)
        elif isinstance(data, RaceStatusEvent):
            await self._on_race_status(data)
        elif isinstance(data, CountdownEvent):
            await self._on_countdown(data)
        elif isinstance(data, FinishGateEvent):
            await self._on_finish_gate(data)
        elif isinstance(data, RaceDataEvent):
            await self._on_race_data(data)
        elif isinstance(data, PlayerEvent):
            await self._on_player(data)
        else:
            log.debug("unhandled game event %s: %s", event.type, event.raw)

    # ── handlers ─────────────────────────────────────────────────────

    async def _on_session(self, data: SessionEvent) -> None:
        # The game names the track but has no ids; keep the picker's ids when
        # it is the same track we already had.
        same = data.track_name.strip() == self.session.track_name.strip()
        prev = self.session
        track_id, scene_id, track_source = (
            (prev.track_id, prev.scene_id, prev.track_source) if same else (0, 0, "")
        )
        if not track_id and self._resolve_track is not None:
            try:
                ref = await self._resolve_track(data.track_name)
            except Exception:  # the online lookup must never break the race feed
                log.exception("track lookup failed for %r", data.track_name)
                ref = None
            if ref is not None:
                track_id, scene_id, track_source = ref.track_id, ref.scene_id, ref.source
                log.info("resolved %r to %s #%d", data.track_name, ref.source, ref.track_id)
            else:
                log.warning("no online track id for %r — PBs off for this run", data.track_name)
        model = quads.catalog().model_by_name(data.quad_type)
        self.session = SessionState(
            track_name=data.track_name,
            scenery=data.scenery_title,
            track_id=track_id,
            scene_id=scene_id,
            track_source=track_source,
            quad_type=data.quad_type,
            quad_size=data.quad_size,
            quad_model_id=model.model_id if model else 0,
            quad_class_id=model.component_group_id if model else 0,
            race_mode=data.race_mode,
            race_laps=data.race_length,
            player_name=data.player_name,
            session_name=data.session_name,
            source=SOURCE_GAME,
        )
        if data.player_name and not self.player_name:
            self.player_name = data.player_name
        await self._persist_session()
        await self.refresh_reference()
        log.info("session from game: %s / %s (%s)", data.track_name, data.quad_type, data.race_mode)
        self._hub.broadcast("session", self.session.to_dict())
        self._hub.broadcast("reference", self._reference_dict())

    async def _on_race_type(self, data: RaceTypeEvent) -> None:
        self.session.race_mode = data.race_mode or self.session.race_mode
        self.session.race_format = data.race_format
        laps_changed = bool(data.race_laps) and data.race_laps != self.session.race_laps
        if data.race_laps:
            self.session.race_laps = data.race_laps
        self.session.updated_at = utcnow()
        self._hub.broadcast("session", self.session.to_dict())
        if laps_changed:
            await self.refresh_reference()
            self._hub.broadcast("reference", self._reference_dict())

    async def _on_player(self, data: PlayerEvent) -> None:
        if data.player_name and not self.player_name:
            self.player_name = data.player_name
        self._hub.broadcast(
            "player",
            {
                "player_name": data.player_name,
                "flying": data.player_flying,
                "race_manager": data.race_manager,
                "colour": data.player_colour,
            },
        )

    async def _on_race_status(self, data: RaceStatusEvent) -> None:
        if data.is_start:
            if self.race_active:
                log.info("new race started while one was running — aborting the old one")
                await self._finish_race(aborted=True)
            self.armed = True
            self.armed_at = utcnow()
            self.countdown = None
            self._hub.broadcast("armed", {"session": self.session.to_dict()})
        elif data.is_abort:
            self.armed = False
            await self._finish_race(aborted=True)
        elif data.is_finished:
            self.armed = False
            if self.race_active:
                await self._finish_race(aborted=False)
        else:
            log.info("unknown raceAction %r", data.race_action)

    async def _on_countdown(self, data: CountdownEvent) -> None:
        self.countdown = data.count_value
        self._hub.broadcast("countdown", {"count": data.count_value})
        if data.is_go:
            await self._start_race()

    async def _on_finish_gate(self, data: FinishGateEvent) -> None:
        self.start_finish_gate = data.start_finish_gate
        if self.race_id is not None:
            async with self._sf() as db:
                race = await db.get(Race, self.race_id)
                if race is not None:
                    race.start_finish_gate = data.start_finish_gate
                    await db.commit()

    async def _on_race_data(self, data: RaceDataEvent) -> None:
        me = self._pick_me(data)
        if me is None:
            return
        if not self.race_active:
            if not self.armed:
                return
            # Countdown disabled in single player: the first snapshot is the start.
            await self._start_race()
        if self.tracker is None:
            return
        crossing = self.tracker.update(me.lap, me.gate, me.time_ms, me.finished)
        if crossing is not None:
            await self._on_crossing(crossing)

    def _pick_me(self, data: RaceDataEvent) -> PlayerRaceData | None:
        players = data.players
        if not players:
            return None
        name = self._settings.get("player_name") or self.player_name
        if name and name in players:
            return players[name]
        if len(players) == 1:
            only = next(iter(players))
            if not self.player_name:
                self.player_name = only
            return players[only]
        if not self._warned_no_pilot:
            self._warned_no_pilot = True
            log.warning(
                "racedata has %d pilots and none is %r — set your player name in Settings",
                len(players),
                name,
            )
            self._hub.broadcast(
                "notice",
                {
                    "level": "warn",
                    "message": "Several pilots in the race and none matches your player name.",
                },
            )
        return None

    async def _on_imu(self, data: ImuEvent) -> None:
        self.imu_frames += 1
        self.imu_last_at = utcnow()
        now = time.monotonic()
        if now - self._imu_last_logged >= IMU_LOG_EVERY_S and self._settings.get_bool(
            "event_log_enabled"
        ):
            self._imu_last_logged = now
            async with self._sf() as db:
                await repos.add_event_log(db, "imu", data.__dict__, self.race_id)
                await db.commit()
        if not self.race_active or not self._settings.get_bool("telemetry_enabled"):
            return
        fallback_ms = int((now - self._go_monotonic) * 1000)
        t_ms = self.telemetry.race_ms_for(data.timestamp, fallback_ms)
        sample = self.telemetry.add(
            t_ms, data.position, data.speed, (data.roll, data.pitch, data.yaw), data.attitude
        )
        if now - self._imu_last_broadcast >= 1.0 / LIVE_TELEMETRY_HZ:
            self._imu_last_broadcast = now
            self._hub.broadcast(
                "telemetry",
                {
                    "t_ms": sample.t_ms,
                    "speed": round(sample.speed, 2),
                    "x": round(sample.x, 2),
                    "y": round(sample.y, 2),
                    "z": round(sample.z, 2),
                },
            )

    # ── race lifecycle ───────────────────────────────────────────────

    async def _start_race(self) -> None:
        if self.race_active:
            return
        self.armed = False
        self.countdown = 0
        self._warned_no_pilot = False
        self.tracker = RaceTracker()
        self.telemetry.clear()
        self._go_monotonic = time.monotonic()
        self.race_started_at = utcnow()
        s = self.session
        async with self._sf() as db:
            self.reference = await repos.load_reference(
                db, repos.PBKey(s.track_id, s.quad_model_id, s.race_laps)
            )
            race = Race(
                track_name=s.track_name,
                scenery=s.scenery,
                track_id=s.track_id,
                scene_id=s.scene_id,
                track_source=s.track_source,
                quad_type=s.quad_type,
                quad_size=s.quad_size,
                quad_model_id=s.quad_model_id,
                quad_class_id=s.quad_class_id,
                race_mode=s.race_mode,
                race_format=s.race_format,
                race_laps=s.race_laps,
                player_name=self.player_name,
                session_source=s.source,
                start_finish_gate=self.start_finish_gate,
                status="running",
                started_at=self.race_started_at,
                reference_race_id=self.reference.race_id if self.reference else None,
            )
            db.add(race)
            await db.commit()
            self.race_id = race.id
        log.info(
            "race %d started: %s / %s / %d laps (reference: %s)",
            self.race_id,
            s.track_name or "unknown track",
            s.quad_type or "unknown quad",
            s.race_laps,
            self.reference.race_id if self.reference else "none",
        )
        self._hub.broadcast(
            "race_started",
            {
                "id": self.race_id,
                "started_at": _iso(self.race_started_at),
                "session": s.to_dict(),
                "reference": self._reference_dict(),
            },
        )

    async def _on_crossing(self, crossing: Crossing) -> None:
        if self.race_id is None or self.tracker is None:
            return
        ref = self.reference
        prev_cum = crossing.cumulative_ms - crossing.gate_ms
        gate_stats = self.telemetry.segment(prev_cum, crossing.cumulative_ms)
        split_ms = ref.split_at(crossing.seq, crossing.cumulative_ms) if ref else None
        lap_payload: dict[str, Any] | None = None
        async with self._sf() as db:
            db.add(
                GateTime(
                    race_id=self.race_id,
                    seq=crossing.seq,
                    lap=crossing.lap,
                    gate=crossing.gate,
                    cumulative_ms=crossing.cumulative_ms,
                    gate_ms=crossing.gate_ms,
                    lap_elapsed_ms=crossing.lap_elapsed_ms,
                    split_ms=split_ms,
                    ends_lap=crossing.lap_done.lap if crossing.lap_done else None,
                    **_stats_dict(gate_stats),
                )
            )
            if crossing.lap_done is not None:
                done = crossing.lap_done
                lap_stats = self.telemetry.segment(
                    done.cumulative_ms - done.lap_ms, done.cumulative_ms
                )
                delta = ref.lap_delta(done.lap, done.lap_ms) if ref else None
                db.add(
                    Lap(
                        race_id=self.race_id,
                        lap=done.lap,
                        lap_ms=done.lap_ms,
                        cumulative_ms=done.cumulative_ms,
                        gates=done.gates,
                        delta_ms=delta,
                        **_stats_dict(lap_stats),
                    )
                )
                lap_payload = self._lap_dict(done) | _stats_dict(lap_stats)
            await db.commit()
        payload = self._crossing_dict(crossing) | _stats_dict(gate_stats)
        payload["lap_done"] = lap_payload
        self._hub.broadcast("crossing", payload)
        if crossing.finished:
            await self._finish_race(aborted=False)

    async def _finish_race(self, aborted: bool) -> None:
        race_id, tracker = self.race_id, self.tracker
        if race_id is None or tracker is None:
            self.armed = False
            return
        ref = self.reference
        async with self._sf() as db:
            race = await db.get(Race, race_id)
            if race is None:
                self._reset_race()
                return
            if not aborted and (not tracker.crossings or tracker.total_ms <= 0):
                # "race finished" without a single crossing (e.g. the game sent it
                # for a run Splitter never saw gates for). A finished race with a
                # zero total would become an unbeatable PB — treat it as an abort.
                log.warning("race %d finished with no crossings — treating as aborted", race_id)
                aborted = True
            if aborted and not tracker.crossings:
                await db.delete(race)
                await db.commit()
                log.info("race %d aborted before any gate — dropped", race_id)
                self._reset_race()
                await self.refresh_reference()
                self._hub.broadcast("reference", self._reference_dict())
                self._hub.broadcast("race_aborted", {"id": race_id, "kept": False})
                return
            race.status = "aborted" if aborted else "finished"
            race.ended_at = utcnow()
            race.total_laps = len(tracker.laps)
            race.gates_per_lap = tracker.gates_per_lap
            race.holeshot_ms = tracker.holeshot_ms
            if not aborted:
                race.total_time_ms = tracker.total_ms
                if ref is not None:
                    race.pb_delta_ms = ref.total_delta(tracker.total_ms)
            whole = self.telemetry.segment(-1, 10**12)
            if whole is not None:
                race.max_speed = round(whole.max_speed, 2)
                race.avg_speed = round(whole.avg_speed, 2)
                race.distance_m = round(whole.distance_m, 1)
            stored = self._store_telemetry(db, race_id)
            race.telemetry_samples = stored
            # Crashes are events in the trace, whether or not the run went on.
            found = crash_detect.detect(self.telemetry.samples)
            if not aborted:
                found = [c for c in found if c.t_ms <= tracker.total_ms + 500]
            found = crash_detect.attribute(
                found,
                [
                    _CrossingRef(
                        c.seq, c.lap, c.lap_done.lap if c.lap_done else None, c.cumulative_ms
                    )
                    for c in tracker.crossings
                ],
            )
            race.crashes = json.dumps([c.to_dict() for c in found])
            race.crash_count = len(found)
            await db.commit()
            is_best = False
            if not aborted:
                best_id = await repos.recalculate_best(db, repos.race_key(race))
                is_best = best_id == race.id
            result = {
                "id": race.id,
                "status": race.status,
                "aborted": aborted,
                "track_name": race.track_name,
                "track_id": race.track_id,
                "quad_type": race.quad_type,
                "race_laps": race.race_laps,
                "total_ms": race.total_time_ms,
                "holeshot_ms": race.holeshot_ms,
                "total_laps": race.total_laps,
                "gates_per_lap": race.gates_per_lap,
                "laps": [self._lap_dict(lap) for lap in tracker.laps],
                "crossings": len(tracker.crossings),
                "pb_delta_ms": race.pb_delta_ms,
                "is_best": is_best,
                "reference": self._reference_dict(),
                "max_speed": race.max_speed,
                "avg_speed": race.avg_speed,
                "distance_m": race.distance_m,
                "telemetry_samples": stored,
                "crashes": race.crash_count,
                "ended_at": _iso(race.ended_at),
            }
        log.info(
            "race %d %s: %s ms, %d laps, %d crossings%s",
            race_id,
            "aborted" if aborted else "finished",
            result["total_ms"],
            result["total_laps"],
            result["crossings"],
            " — NEW PB" if is_best else "",
        )
        self.last_result = result
        self._reset_race()
        await self.refresh_reference()  # the PB the next run is measured against
        result["next_reference"] = self._reference_dict()
        self._hub.broadcast("race_finished", result)

    def _store_telemetry(self, db: AsyncSession, race_id: int) -> int:
        if not self.telemetry.samples:
            return 0
        hz = self._settings.get_float("telemetry_store_hz")
        rows = self.telemetry.downsampled(hz)
        db.add_all(
            TelemetrySample(
                race_id=race_id,
                t_ms=s.t_ms,
                x=s.x,
                y=s.y,
                z=s.z,
                vx=s.vx,
                vy=s.vy,
                vz=s.vz,
                speed=s.speed,
                roll=s.roll,
                pitch=s.pitch,
                yaw=s.yaw,
                qx=s.qx,
                qy=s.qy,
                qz=s.qz,
                qw=s.qw,
            )
            for s in rows
        )
        return len(rows)

    def _reset_race(self) -> None:
        self.race_id = None
        self.tracker = None
        self.reference = None
        self.armed = False
        self.countdown = None
        self.telemetry.clear()
        if self.session.source == SOURCE_GAME:
            # The next run may well be the same room; keep it but mark it as a carry-over.
            self.session.source = SOURCE_STICKY

    # ── helpers ──────────────────────────────────────────────────────

    async def _persist_session(self) -> None:
        s = self.session
        async with self._sf() as db:
            await self._settings.set_many(
                db,
                {
                    "last_track_name": s.track_name,
                    "last_scenery": s.scenery,
                    "last_track_id": str(s.track_id),
                    "last_scene_id": str(s.scene_id),
                    "last_track_source": s.track_source,
                    "last_quad_type": s.quad_type,
                    "last_quad_size": s.quad_size,
                    "last_quad_model_id": str(s.quad_model_id),
                    "last_quad_class_id": str(s.quad_class_id),
                    "last_race_mode": s.race_mode,
                    "last_race_laps": str(s.race_laps),
                },
            )

    async def _log_event(self, event: Event) -> None:
        if not self._settings.get_bool("event_log_enabled"):
            return
        async with self._sf() as db:
            await repos.add_event_log(db, event.type, event.raw, self.race_id)
            await db.commit()

    def _reference_dict(self) -> dict[str, Any] | None:
        ref = self.reference
        if ref is None:
            return None
        return {
            "race_id": ref.race_id,
            "total_ms": ref.total_ms,
            "gates_per_lap": ref.gates_per_lap,
            "crossings": len(ref.cumulative_by_seq),
            "laps": ref.lap_ms_by_lap,
        }

    def _crossing_dict(self, c: Crossing) -> dict[str, Any]:
        ref = self.reference
        split = ref.split_at(c.seq, c.cumulative_ms) if ref else None
        return {
            "seq": c.seq,
            "lap": c.lap,
            "gate": c.gate,
            "cumulative_ms": c.cumulative_ms,
            "gate_ms": c.gate_ms,
            "lap_elapsed_ms": c.lap_elapsed_ms,
            "split_ms": split,
            "gate_delta_ms": ref.gate_delta(c.seq, c.gate_ms) if ref else None,
            "finished": c.finished,
            "ends_lap": c.lap_done.lap if c.lap_done else None,
            "starts_lap": c.starts_lap,
        }

    def _lap_dict(self, lap: Any) -> dict[str, Any]:
        ref = self.reference
        return {
            "lap": lap.lap,
            "lap_ms": lap.lap_ms,
            "cumulative_ms": lap.cumulative_ms,
            "gates": lap.gates,
            "delta_ms": ref.lap_delta(lap.lap, lap.lap_ms) if ref else None,
        }
