"""Run identity and the run document (sync phase 1): codec, fingerprint,
export/import round trip, legacy telemetry migration, identity backfill."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import select

from splitter.core import fingerprint as fingerprinting
from splitter.core import rundoc
from splitter.core.telemetry import COLUMNS, ENCODING, Sample, decode_columns, encode_columns
from splitter.db import repos
from splitter.db.models import GateTime, Lap, Race, TelemetryBlob, TelemetrySample
from splitter.devtools.fingerprint_stats import summarise
from splitter.game.controller import RaceController
from tests import helpers as h
from tests.test_controller import fly


def _samples(n: int = 50) -> list[Sample]:
    return [
        Sample(
            t_ms=i * 50,
            x=i * 1.5,
            y=1.0,
            z=-i * 0.25,
            vx=30.0,
            vy=0.0,
            vz=-5.0,
            speed=30.41,
            roll=i * 0.1,
            pitch=-0.2,
            yaw=300.5,
            qx=0.0,
            qy=0.1,
            qz=0.2,
            qw=0.97,
        )
        for i in range(n)
    ]


def test_columnar_codec_round_trips_to_float32() -> None:
    samples = _samples()
    blob = encode_columns(samples)
    back = decode_columns(blob, len(samples), ENCODING)
    assert len(back) == len(samples)
    for a, b in zip(samples, back, strict=True):
        assert a.t_ms == b.t_ms
        for name in COLUMNS[1:]:
            assert getattr(a, name) == pytest.approx(getattr(b, name), abs=1e-4)
    assert encode_columns([]) == b"" and decode_columns(b"", 0) == []
    with pytest.raises(ValueError):
        decode_columns(blob, len(samples) + 1)
    with pytest.raises(ValueError):
        decode_columns(blob, len(samples), "zstd")


# A 2-lap, 3-gate run in lap-segment terms: crossings as (seq, lap, ends_lap, cumulative_ms).
class _C:
    def __init__(self, seq: int, lap: int, ends_lap: int | None, cumulative_ms: int) -> None:
        self.seq, self.lap, self.ends_lap, self.cumulative_ms = seq, lap, ends_lap, cumulative_ms


def _run_crossings() -> list[_C]:
    return [
        _C(0, 0, None, 1000),
        _C(1, 1, None, 2000),
        _C(2, 1, None, 4000),
        _C(3, 1, None, 6000),
        _C(4, 2, 1, 8000),
        _C(5, 2, None, 10000),
        _C(6, 2, None, 12000),
        _C(7, 2, 2, 14000),
    ]


def test_fingerprint_prefers_the_fastest_clean_lap() -> None:
    samples = _samples(300)  # 15 s at 20 Hz, x = 30 m/s * t
    laps = [(1, 6000), (2, 6000)]
    fp = fingerprinting.compute(_run_crossings(), 3, samples, laps)
    assert fp is not None and fp.lap == 1 and fp.gates_per_lap == 3 and fp.located == 3
    assert fp.gates[0] is not None and fp.gates[0][0] == pytest.approx(120.0, abs=0.01)
    # Lap 1 crashed: lap 2 is the clean one.
    fp2 = fingerprinting.compute(_run_crossings(), 3, samples, laps, crashed_laps=[1])
    assert fp2 is not None and fp2.lap == 2 and fp2.gates[0][0] == pytest.approx(300.0, abs=0.01)  # type: ignore[index]
    # Every lap crashed: the first complete lap.
    fp3 = fingerprinting.compute(_run_crossings(), 3, samples, laps, crashed_laps=[1, 2])
    assert fp3 is not None and fp3.lap == 1
    # No trace / no gate count / no complete lap → nothing.
    assert fingerprinting.compute(_run_crossings(), 3, [], laps) is None
    assert fingerprinting.compute(_run_crossings(), None, samples, laps) is None
    assert fingerprinting.compute(_run_crossings()[:3], 3, samples, []) is None
    d = fp.to_dict()
    assert fingerprinting.Fingerprint.from_dict(d) == fingerprinting.Fingerprint(
        3,
        1,
        tuple(tuple(g) for g in d["gates"]),  # type: ignore[misc]
    )
    assert fingerprinting.Fingerprint.from_dict(None) is None


def test_fingerprint_stats_separates_tracks() -> None:
    def fp(offset: float) -> fingerprinting.Fingerprint:
        return fingerprinting.Fingerprint(
            4, 1, tuple((offset + k * 10.0, 1.0, k * 2.0) for k in range(4))
        )

    runs = [(1, fp(0)), (1, fp(1.5)), (2, fp(40)), (2, fp(41))]
    s = summarise(runs)
    assert s["same_pairs"] == 2 and s["cross_pairs"] == 4
    assert s["same_over_threshold"] == 0 and s["cross_under_threshold"] == 0
    assert s["same"]["max"] < 2 and s["cross"]["min"] > 38


async def test_export_import_round_trip(controller: RaceController, session_factory) -> None:
    await controller.handle_event(h.session())
    await fly(controller, imu=True)
    async with session_factory() as db:
        race = (await db.execute(select(Race))).scalar_one()
        assert len(race.uuid) == 32 and race.origin == "capture" and race.received_at is None
        assert race.fingerprint and race.telemetry_samples > 0
        assert await db.get(TelemetryBlob, race.id) is not None
        assert not (await db.execute(select(TelemetrySample))).scalars().all()
        doc = await repos.export_run(db, race, "test")
        trace_before = await repos.telemetry_for_race(db, race.id)

    assert doc["doc_version"] == rundoc.DOC_VERSION and doc["uuid"] == race.uuid
    assert "id" not in doc["race"] and "is_best" not in doc["race"]
    assert doc["race"]["total_time_ms"] == 14000 and doc["race"]["started_at"].endswith("Z")
    assert len(doc["laps"]) == 2 and len(doc["gate_times"]) == 8
    assert doc["fingerprint"]["gates_per_lap"] == 3 and doc["telemetry"]["encoding"] == ENCODING
    # Survives JSON.
    doc = json.loads(json.dumps(doc))

    # Into a second, empty database.
    import tempfile

    from splitter.db.engine import create_engine, create_session_factory, init_db

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp}/other.db")
        await init_db(engine)
        sf2 = create_session_factory(engine)
        async with sf2() as db:
            results = await repos.import_runs(db, [doc])
            assert [r.status for r in results] == ["created"]
            again = await repos.import_runs(db, [doc, doc])
            assert [r.status for r in again] == ["exists", "exists"]
            r2 = (await db.execute(select(Race))).scalar_one()
            assert r2.uuid == race.uuid and r2.origin == "import" and r2.received_at is not None
            assert r2.is_best and r2.total_time_ms == 14000 and r2.node_id == race.node_id
            assert r2.fingerprint == race.fingerprint and r2.crash_count == race.crash_count
            assert [(lap.lap, lap.lap_ms) for lap in r2.laps] == [(1, 6000), (2, 6000)]
            assert [g.cumulative_ms for g in r2.gate_times] == [
                g.cumulative_ms for g in race.gate_times
            ]
            trace_after = await repos.telemetry_for_race(db, r2.id)
            assert [s.t_ms for s in trace_after] == [s.t_ms for s in trace_before]
            assert isinstance(r2.started_at, datetime)
        await engine.dispose()


async def test_import_keeps_a_reference_it_cannot_resolve(
    controller: RaceController, session_factory
) -> None:
    await controller.handle_event(h.session())
    await fly(controller)
    await fly(controller, scale=0.9)
    async with session_factory() as db:
        first, second = (await db.execute(select(Race).order_by(Race.id))).scalars().all()
        assert second.reference_race_id == first.id and second.reference_uuid == first.uuid
        doc_second = await repos.export_run(db, second, "test")
        doc_first = await repos.export_run(db, first, "test")

    import tempfile

    from splitter.db.engine import create_engine, create_session_factory, init_db

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp}/other.db")
        await init_db(engine)
        sf2 = create_session_factory(engine)
        async with sf2() as db:
            (r,) = await repos.import_runs(db, [doc_second])
            imported = await db.get(Race, r.race_id)
            assert imported is not None
            assert imported.reference_uuid == first.uuid and imported.reference_race_id is None
            assert imported.pb_delta_ms == -1400  # a historical fact, kept
            assert imported.is_best
            # The reference arrives later (slower): it joins up and is not the PB.
            (r1,) = await repos.import_runs(db, [doc_first])
            await db.refresh(imported)
            assert imported.reference_race_id == r1.race_id
            ref = await db.get(Race, r1.race_id)
            assert ref is not None and not ref.is_best and imported.is_best
        await engine.dispose()


def test_parse_rejects_bad_documents() -> None:
    good = {
        "doc_version": 1,
        "uuid": "a" * 32,
        "race": {"started_at": "2026-09-26T10:00:00Z", "status": "finished"},
        "gate_times": [{"seq": 0, "cumulative_ms": 1000}],
    }
    parsed = rundoc.parse(good)
    assert parsed.uuid == "a" * 32 and parsed.race["status"] == "finished"
    bad = [
        ({}, "doc_version"),
        (good | {"doc_version": 99}, "newer"),
        (good | {"uuid": "nope"}, "uuid"),
        (good | {"race": {"started_at": "2026-09-26T10:00:00Z", "status": "running"}}, "status"),
        (good | {"gate_times": []}, "crossings"),
        (good | {"gate_times": [{"seq": 0, "cumulative_ms": 1}] * 2}, "duplicate"),
        (good | {"telemetry": {"samples": 3, "encoding": ENCODING, "data": "!!"}}, "telemetry"),
        ("text", "object"),
    ]
    for doc, word in bad:
        with pytest.raises(rundoc.DocumentError, match=word):
            rundoc.parse(doc)


async def test_legacy_rows_migrate_to_a_blob(session_factory) -> None:
    async with session_factory() as db:
        race = Race(started_at=datetime(2026, 1, 1), status="finished", total_time_ms=1)
        db.add(race)
        await db.flush()
        for s in _samples(30):
            db.add(
                TelemetrySample(
                    race_id=race.id,
                    **{name: getattr(s, name) for name in COLUMNS},
                )
            )
        race.telemetry_samples = 30
        await db.commit()
        rid = race.id
        before = await repos.telemetry_for_race(db, rid)
        assert len(before) == 30 and await repos.legacy_telemetry_rows(db) == 30
        assert await repos.migrate_telemetry(db) == 1
        assert await repos.migrate_telemetry(db) == 0
        assert await repos.legacy_telemetry_rows(db) == 0
        blob = await db.get(TelemetryBlob, rid)
        assert blob is not None and blob.samples == 30 and blob.hz == pytest.approx(20.7, abs=0.2)
        after = await repos.telemetry_for_race(db, rid)
        assert [s.t_ms for s in after] == [s.t_ms for s in before]
        assert after[3].x == pytest.approx(before[3].x)
        assert await repos.races_with_telemetry(db, [rid, 999]) == [rid]


async def test_backfill_gives_old_rows_an_identity(session_factory) -> None:
    async with session_factory() as db:
        a = Race(started_at=datetime(2026, 1, 1), status="finished", total_time_ms=1)
        db.add(a)
        await db.flush()
        b = Race(
            started_at=datetime(2026, 1, 2),
            status="finished",
            total_time_ms=2,
            reference_race_id=a.id,
        )
        db.add(b)
        await db.commit()
        assert a.uuid == "" and b.reference_uuid == ""
        assert await repos.backfill_identity(db, "node-x") == 2
        assert await repos.backfill_identity(db, "node-x") == 0
        await db.refresh(a)
        await db.refresh(b)
        assert len(a.uuid) == 32 and a.uuid != b.uuid
        assert a.node_id == "node-x" and b.reference_uuid == a.uuid
        # Children still attach by row id.
        db.add(Lap(race_id=a.id, lap=1, lap_ms=1, cumulative_ms=1))
        db.add(GateTime(race_id=a.id, seq=0, lap=0, gate=1, cumulative_ms=1, gate_ms=1))
        await db.commit()
