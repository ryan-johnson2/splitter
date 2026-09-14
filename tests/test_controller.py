"""End-to-end through the controller: wire events in, DB rows and live messages out."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from splitter.db import repos
from splitter.db.models import EventLog, GateTime, Lap, Race, TelemetrySample
from splitter.game.controller import RaceController
from splitter.live.hub import LiveHub
from splitter.util import utcnow
from tests import helpers as h


async def fly(
    controller: RaceController, base: float = 0.0, scale: float = 1.0, imu: bool = False
) -> None:
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.racetype())
    for n in (3, 2, 1, 0):
        await controller.handle_event(h.countdown(n))
    await controller.handle_event(h.ev("FinishGate", {"StartFinishGate": "True"}))
    ts = 1_000_000.0
    for lap, gate, t, fin in h.two_lap_race(base, scale):
        if imu:
            # 60 Hz-ish samples up to this crossing, flying along x at 20 m/s.
            while ts - 1_000_000.0 < t * 1000:
                rel = (ts - 1_000_000.0) / 1000
                await controller.handle_event(h.imu(ts, 20 * rel, 0.0, 20.0, 0.0))
                ts += 1000 / 60
        await controller.handle_event(h.racedata(lap, gate, t, fin))
    await controller.handle_event(h.status("race finished"))


async def test_full_race_persists_and_broadcasts(
    controller: RaceController, hub: LiveHub, session_factory
) -> None:
    q = hub.subscribe()
    await controller.handle_event(h.session())
    await fly(controller)
    msgs = h.drain(q)
    kinds = [m["type"] for m in msgs]
    assert kinds[:3] == ["session", "reference", "armed"]
    assert "race_started" in kinds and kinds[-1] == "race_finished"
    assert kinds.count("crossing") == 8
    assert kinds.count("countdown") == 4

    result = msgs[-1]["data"]
    assert result["status"] == "finished" and result["total_ms"] == 14000
    assert result["total_laps"] == 2 and result["gates_per_lap"] == 3
    assert result["holeshot_ms"] == 2000
    assert result["is_best"] is True and result["pb_delta_ms"] is None  # first run: no reference

    async with session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.track_name == "Practice Loop" and race.quad_type == "Source One"
        assert race.track_id == 500 and race.scene_id == 29 and race.quad_model_id == 0
        assert race.race_laps == 3 and race.race_mode == "THREE_LAP_SINGLE_CLASS"
        assert race.session_source == "game" and race.player_name == "Ryan"
        assert race.start_finish_gate is True
        assert race.status == "finished" and race.total_time_ms == 14000 and race.is_best
        assert race.holeshot_ms == 2000
        gates = (await db.execute(select(GateTime).order_by(GateTime.seq))).scalars().all()
        assert [g.cumulative_ms for g in gates] == [
            1000,
            2000,
            4000,
            6000,
            8000,
            10000,
            12000,
            14000,
        ]
        assert gates[4].ends_lap == 1 and gates[7].ends_lap == 2
        assert [g.ends_lap for g in gates[:4]] == [None] * 4
        laps = (await db.execute(select(Lap).order_by(Lap.lap))).scalars().all()
        assert [(lap.lap, lap.lap_ms) for lap in laps] == [(1, 6000), (2, 6000)]
        assert (await db.execute(select(EventLog))).scalars().first() is not None
    assert not controller.race_active
    assert controller.session.source == "sticky"  # carried to the next run
    # Between runs the reference is the PB the next run will be measured against.
    assert result["next_reference"]["race_id"] == 1
    assert controller.snapshot()["reference"]["race_id"] == 1


async def test_second_run_gets_splits_against_pb(
    controller: RaceController, hub: LiveHub, session_factory
) -> None:
    await controller.handle_event(h.session())
    await fly(controller)
    q = hub.subscribe()
    await fly(controller, scale=0.9)  # 10% faster everywhere
    msgs = h.drain(q)
    started = next(m for m in msgs if m["type"] == "race_started")["data"]
    assert started["reference"]["race_id"] == 1 and started["reference"]["total_ms"] == 14000
    crossings = [m["data"] for m in msgs if m["type"] == "crossing"]
    assert crossings[0]["split_ms"] == -100 and crossings[0]["gate_delta_ms"] == -100
    assert crossings[4]["lap_done"]["delta_ms"] == -600
    result = msgs[-1]["data"]
    assert result["pb_delta_ms"] == -1400 and result["is_best"]
    async with session_factory() as db:
        first = await db.get(Race, 1)
        second = await db.get(Race, 2)
        assert first is not None and second is not None
        assert not first.is_best and second.is_best
        assert second.reference_race_id == 1
        gate = (
            await db.execute(select(GateTime).where(GateTime.race_id == 2, GateTime.seq == 7))
        ).scalar_one()
        assert gate.split_ms == -1400


async def test_abort_before_any_gate_drops_the_row(
    controller: RaceController, hub: LiveHub, session_factory
) -> None:
    await controller.handle_event(h.session())
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    assert controller.race_active
    q = hub.subscribe()
    await controller.handle_event(h.status("abort"))
    assert h.drain(q)[-1]["type"] == "race_aborted"
    async with session_factory() as db:
        assert (await db.execute(select(Race))).scalars().all() == []


async def test_abort_mid_race_keeps_partial_run(
    controller: RaceController, session_factory
) -> None:
    await controller.handle_event(h.session())
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    await controller.handle_event(h.racedata(1, 2, 2.0))
    await controller.handle_event(h.racedata(1, 3, 4.0))
    await controller.handle_event(h.status("abort"))
    async with session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.status == "aborted" and race.total_time_ms is None and not race.is_best
        assert await repos.gate_time_count(db, race.id) == 2


async def test_single_player_without_countdown_starts_on_first_racedata(
    controller: RaceController, hub: LiveHub
) -> None:
    await controller.set_manual_session("Manual Track", quad_type="Baby Ape", race_laps=2)
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.racetype(2))
    q = hub.subscribe()
    await controller.handle_event(h.racedata(1, 2, 1.5))
    kinds = [m["type"] for m in h.drain(q)]
    assert kinds == ["race_started", "crossing"]
    assert controller.race_active
    snap = controller.snapshot()
    assert snap["session"]["track_name"] == "Manual Track" and snap["session"]["source"] == "manual"
    assert snap["race"]["crossings"][0]["cumulative_ms"] == 1500


async def test_sticky_session_survives_restart(
    controller: RaceController, settings, session_factory, hub: LiveHub
) -> None:
    await controller.handle_event(h.session(track="Sticky Track"))
    fresh = RaceController(settings, session_factory, hub)
    fresh.load_sticky_session()
    assert fresh.session.track_name == "Sticky Track" and fresh.session.source == "sticky"
    assert fresh.session.race_laps == 3
    assert fresh.session.track_id == 501 and fresh.session.track_source == "official"


async def test_picks_me_among_several_pilots(
    controller: RaceController, settings, session_factory, hub: LiveHub
) -> None:
    async with session_factory() as db:
        await settings.set(db, "player_name", "Ryan")
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    await controller.handle_event(h.racedata(1, 2, 2.0, Other=(1, 3, 1.5)))
    await controller.handle_event(h.racedata(1, 3, 4.0, Other=(1, 4, 3.0)))
    assert controller.tracker is not None
    assert [c.cumulative_ms for c in controller.tracker.crossings] == [2000, 4000]


async def test_unknown_pilot_among_several_is_skipped(
    controller: RaceController, hub: LiveHub
) -> None:
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    q = hub.subscribe()
    await controller.handle_event(h.racedata(1, 2, 2.0, name="Someone", Other=(1, 3, 1.5)))
    kinds = [m["type"] for m in h.drain(q)]
    assert kinds == ["notice"]
    assert controller.tracker is not None and controller.tracker.crossings == []


async def test_telemetry_is_stored_and_summarised(
    controller: RaceController, hub: LiveHub, session_factory
) -> None:
    await controller.handle_event(h.session())
    q = hub.subscribe()
    await fly(controller, imu=True)
    msgs = h.drain(q)
    assert any(m["type"] == "telemetry" for m in msgs)
    result = msgs[-1]["data"]
    assert result["max_speed"] == 20.0 and result["telemetry_samples"] > 0
    crossing = next(m for m in msgs if m["type"] == "crossing")["data"]
    assert crossing["max_speed"] == 20.0 and crossing["distance_m"] is not None
    async with session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.max_speed == 20.0 and race.distance_m and 260 < race.distance_m < 290
        samples = (await db.execute(select(TelemetrySample))).scalars().all()
        # 14 s at 20 Hz storage.
        assert 260 <= len(samples) <= 290
        lap = (await db.execute(select(Lap).where(Lap.lap == 1))).scalar_one()
        assert lap.max_speed == 20.0
    assert controller.imu_frames > 800


async def test_imu_outside_a_race_is_not_stored(controller: RaceController) -> None:
    await controller.handle_event(h.imu(1.0, 0, 0, 1, 0))
    assert controller.imu_frames == 1 and controller.telemetry.samples == []


async def test_new_start_while_running_aborts_previous(
    controller: RaceController, session_factory
) -> None:
    await controller.handle_event(h.session())
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    await controller.handle_event(h.racedata(1, 2, 2.0))
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    async with session_factory() as db:
        races = (await db.execute(select(Race).order_by(Race.id))).scalars().all()
        assert [r.status for r in races] == ["aborted", "running"]


async def test_reference_follows_the_session(
    controller: RaceController, hub: LiveHub, settings, session_factory
) -> None:
    await controller.handle_event(h.session())
    assert controller.snapshot()["reference"] is None
    await fly(controller)
    assert controller.snapshot()["reference"]["race_id"] == 1
    # A different quad is a different PB group: no reference.
    q = hub.subscribe()
    await controller.set_manual_session(
        "Practice Loop", track_id=500, race_laps=3, quad_model_id=108
    )
    kinds = [m["type"] for m in h.drain(q)]
    assert kinds == ["session", "reference"] and controller.snapshot()["reference"] is None
    await controller.set_manual_session("Practice Loop", track_id=500, race_laps=3)
    assert controller.snapshot()["reference"]["race_id"] == 1
    # A restart reloads it from the sticky session.
    fresh = RaceController(settings, session_factory, hub)
    fresh.load_sticky_session()
    await fresh.refresh_reference()
    assert fresh.snapshot()["reference"]["race_id"] == 1


async def test_finish_without_crossings_is_not_a_pb(
    controller: RaceController, hub: LiveHub, session_factory
) -> None:
    """A 'race finished' with no gates seen must not become a 0.000 PB (bug seen 2026-09-12)."""
    await controller.handle_event(h.session())
    await fly(controller)  # a real PB: 14.000
    q = hub.subscribe()
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    await controller.handle_event(h.status("race finished"))  # no racedata at all
    kinds = [m["type"] for m in h.drain(q)]
    assert "race_aborted" in kinds and "race_finished" not in kinds
    async with session_factory() as db:
        rows = (await db.execute(select(Race).order_by(Race.id))).scalars().all()
        assert [(r.id, r.status, r.is_best) for r in rows] == [(1, "finished", True)]
    assert controller.snapshot()["reference"]["race_id"] == 1


async def test_zero_total_never_reference(session_factory) -> None:
    from splitter.db import repos

    async with session_factory() as db:
        db.add(
            Race(track_id=5, race_laps=3, status="finished", total_time_ms=0, started_at=utcnow())
        )
        db.add(
            Race(
                track_id=5, race_laps=3, status="finished", total_time_ms=9000, started_at=utcnow()
            )
        )
        await db.commit()
        best = await repos.recalculate_best(db, repos.PBKey(5, 0, 3))
        assert best == 2
        assert (await repos.get_best_race(db, repos.PBKey(5, 0, 3))).total_time_ms == 9000  # type: ignore[union-attr]


def _imu(ts_ms: float, x: float, vx: float, roll: float) -> h.Event:
    return h.ev(
        "imu",
        {
            "roll": roll,
            "pitch": 0.0,
            "yaw": 0.0,
            "PositionX": x,
            "PositionY": 1.0,
            "PositionZ": 0.0,
            "AttitudeX": 0.0,
            "AttitudeY": 0.0,
            "AttitudeZ": 0.0,
            "AttitudeW": 1.0,
            "SpeedX": vx,
            "SpeedY": 0.0,
            "SpeedZ": 0.0,
            "timestamp": ts_ms,
        },
    )


async def test_crashes_are_detected_and_attributed(
    controller: RaceController, hub: LiveHub, session_factory
) -> None:
    """A hard stop with a gyro spike mid-lap becomes one crash on the race row."""
    await controller.handle_event(h.session())
    q = hub.subscribe()
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.racetype())
    for n in (3, 2, 1, 0):
        await controller.handle_event(h.countdown(n))
    ts = 1_000_000.0
    for lap, gate, t, fin in h.two_lap_race():
        while ts - 1_000_000.0 < t * 1000:
            rel = (ts - 1_000_000.0) / 1000
            crashed = 4.5 <= rel < 5.0  # between the lap-1 gates at 4 s and 6 s
            speed = 2.0 if crashed else 20.0
            spike = 950.0 if 4.5 <= rel < 4.6 else 0.0  # the gyro spike that confirms it
            await controller.handle_event(_imu(ts, 20 * rel, speed, spike))
            ts += 1000 / 60
        await controller.handle_event(h.racedata(lap, gate, t, fin))
    await controller.handle_event(h.status("race finished"))
    result = h.drain(q)[-1]["data"]
    assert result["crashes"] == 1
    async with session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.crash_count == 1
        (crash,) = json.loads(race.crashes)
        assert crash["lap"] == 1 and crash["segment"] == 2 and 4400 <= crash["t_ms"] <= 4600
        assert crash["speed_before"] == 20.0 and crash["decel"] <= -120
        gate = (await db.execute(select(GateTime).where(GateTime.seq == 3))).scalar_one()
        assert gate.min_speed == 2.0 and gate.min_accel is not None and gate.min_accel <= -120


async def test_losing_the_game_mid_race_aborts_after_the_grace(
    controller: RaceController, hub: LiveHub, session_factory
) -> None:
    await controller.handle_event(h.session())
    await controller.handle_event(h.status("start"))
    await controller.handle_event(h.countdown(0))
    await controller.handle_event(h.racedata(0, 1, 1.0, False))
    await controller.handle_event(h.racedata(1, 2, 2.0, False))
    assert controller.race_active
    controller.game_loss_grace_s = 0.01
    q = hub.subscribe()
    # A blip that comes back inside the grace keeps the run.
    await controller.on_game_state(False)
    await controller.on_game_state(True)
    await asyncio.sleep(0.05)
    assert controller.race_active
    # A real loss ends it.
    await controller.on_game_state(False)
    await asyncio.sleep(0.05)
    assert not controller.race_active
    kinds = [m["type"] for m in h.drain(q)]
    assert "race_finished" in kinds and "notice" in kinds
    async with session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert race.status == "aborted"


async def test_manual_abort_without_a_race_is_a_no_op(controller: RaceController) -> None:
    assert await controller.abort_race("manual") is None
