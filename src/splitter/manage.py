"""The ``splitter`` CLI: run the server, inspect data, replay a fake race."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from splitter.config import Config
from splitter.core.timeparse import format_delta, format_ms
from splitter.version import __version__


def _session_factory(cfg: Config) -> Any:
    from splitter.db.engine import create_engine, create_session_factory

    return create_session_factory(create_engine(cfg.database_url))


async def _open(cfg: Config) -> Any:
    """Session factory over a prepared database (schema, node id, backfills)."""
    from splitter.db.prepare import prepare

    sf = _session_factory(cfg)
    await prepare(sf)
    return sf


async def _cmd_races(cfg: Config, args: argparse.Namespace) -> None:
    from splitter.db import repos

    sf = await _open(cfg)
    async with sf() as db:
        rows = await repos.list_races(
            db, repos.RaceFilters(track=args.track or "", limit=args.limit)
        )
    for r in rows:
        flag = " PB" if r.is_best else ""
        time_s = format_ms(r.total_time_ms) if r.status == "finished" else r.status
        delta = format_delta(r.pb_delta_ms)
        print(
            f"#{r.id:<5} {r.started_at:%Y-%m-%d %H:%M}  {r.track_name or '?':<30} "
            f"{r.quad_type or '?':<16} {r.total_laps}L  {time_s:>10} {delta:>8}{flag}"
        )


async def _cmd_settings(cfg: Config, args: argparse.Namespace) -> None:
    from splitter.db.runtime_settings import RuntimeSettings

    sf = await _open(cfg)
    settings = RuntimeSettings()
    async with sf() as db:
        await settings.load(db)
        if args.key and args.value is not None:
            await settings.set(db, args.key, args.value)
            print(f"{args.key} = {args.value}")
            return
    for key, value in sorted(settings.all().items()):
        if not args.key or key == args.key:
            print(f"{key} = {value}")


async def _cmd_backfill_crashes(cfg: Config, args: argparse.Namespace) -> None:
    """Run crash detection over the stored traces of existing runs."""
    import json

    from sqlalchemy import select

    from splitter.core import crashes as crash_detect
    from splitter.db import repos
    from splitter.db.models import Race

    sf = await _open(cfg)
    async with sf() as db:
        stmt = select(Race).where(Race.telemetry_samples > 0).order_by(Race.id)
        if not args.all:
            stmt = stmt.where(Race.crashes == "")
        races = list((await db.execute(stmt)).scalars().all())
        runs = crashes = 0
        for race in races:
            samples = await repos.telemetry_for_race(db, race.id)
            found = crash_detect.detect(samples)
            if race.status == "finished" and race.total_time_ms:
                found = [c for c in found if c.t_ms <= race.total_time_ms + 500]
            found = crash_detect.attribute(found, race.gate_times)
            race.crashes = json.dumps([c.to_dict() for c in found])
            race.crash_count = len(found)
            runs += 1
            crashes += len(found)
            if args.verbose and found:
                where = ", ".join(f"L{c.lap} seg {c.segment} @ {c.t_ms / 1000:.1f}s" for c in found)
                print(f"#{race.id:<5} {race.status:<8} {len(found)} crash(es): {where}")
        await db.commit()
    print(f"scanned {runs} runs, found {crashes} crashes")


async def _cmd_export(cfg: Config, args: argparse.Namespace) -> None:
    """Write one document per run into a directory."""
    import json
    from datetime import datetime
    from pathlib import Path

    from sqlalchemy import select

    from splitter.db import repos
    from splitter.db.models import Race

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sf = await _open(cfg)
    async with sf() as db:
        stmt = select(Race).where(Race.status != "running").order_by(Race.id)
        if args.since:
            stmt = stmt.where(Race.started_at >= datetime.fromisoformat(args.since))
        elif not args.all:
            raise SystemExit("say --all or --since YYYY-MM-DD")
        n = 0
        for race in (await db.execute(stmt)).scalars():
            doc = await repos.export_run(db, race, __version__)
            name = f"splitter-run-{race.started_at:%Y%m%d-%H%M%S}-{race.uuid[:8]}.json"
            (out / name).write_text(json.dumps(doc, separators=(",", ":")))
            n += 1
    print(f"wrote {n} runs to {out}")


async def _cmd_import(cfg: Config, args: argparse.Namespace) -> None:
    import json
    from pathlib import Path

    from splitter.db import repos

    docs = []
    for name in args.files:
        loaded = json.loads(Path(name).read_text())
        docs.extend(loaded if isinstance(loaded, list) else [loaded])
    from splitter.db.prepare import prepare

    sf = _session_factory(cfg)
    settings = await prepare(sf)
    async with sf() as db:
        results = await repos.import_runs(
            db, docs, origin="import", default_node_id=settings.get("node_id")
        )
    for r in results:
        line = f"{r.uuid[:8]:<8} {r.status:<8}"
        print(f"{line} #{r.race_id}" if r.race_id else f"{line} {r.error}")
    statuses = ("created", "exists", "rejected")
    counts = {s: sum(1 for r in results if r.status == s) for s in statuses}
    print(", ".join(f"{v} {k}" for k, v in counts.items()))


async def _cmd_migrate_telemetry(cfg: Config, args: argparse.Namespace) -> None:
    """Convert legacy telemetry rows to blobs (startup does this too); --drop the table."""
    from sqlalchemy import text

    from splitter.db import repos

    sf = await _open(cfg)  # the conversion itself happens in prepare()
    engine = sf.kw["bind"]
    async with sf() as db:
        left = await repos.legacy_telemetry_rows(db)
    print(f"{left} legacy telemetry rows left")
    if args.drop:
        if left:
            raise SystemExit("legacy rows remain; not dropping")
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS telemetry"))
        print("dropped the telemetry table (it is recreated empty at next start)")


async def _cmd_retime(cfg: Config, args: argparse.Namespace) -> None:
    """Time runs that have a trace but no gate data from their flight path."""
    from splitter.db import repos

    sf = await _open(cfg)
    async with sf() as db:
        races = await repos.retime_candidates(db)
        if args.ids:
            races = [r for r in races if r.id in set(args.ids)]
        done = 0
        for race in races:
            n = await repos.retime_from_path(db, race)
            done += n is not None
            print(
                f"#{race.id:<5} {race.track_name[:30]:<30} "
                + (f"{n} crossings, {race.total_laps} laps, {race.status}" if n else "not possible")
            )
        print(f"{done} of {len(races)} run(s) timed from the flight path")


async def _cmd_backfill_fingerprints(cfg: Config, args: argparse.Namespace) -> None:
    """Fingerprint runs that were recorded before fingerprints existed, from their traces."""
    import json

    from sqlalchemy import select

    from splitter.core import fingerprint as fingerprinting
    from splitter.db import repos
    from splitter.db.models import Race

    sf = await _open(cfg)
    async with sf() as db:
        stmt = select(Race).where(Race.telemetry_samples > 0).order_by(Race.id)
        if not args.all:
            stmt = stmt.where(Race.fingerprint == "")
        races = list((await db.execute(stmt)).scalars().all())
        done = 0
        for race in races:
            samples = await repos.telemetry_for_race(db, race.id)
            fp = fingerprinting.compute(
                race.gate_times,
                race.gates_per_lap,
                samples,
                [(lap.lap, lap.lap_ms) for lap in race.laps],
                [c.lap for c in repos.crashes_of(race)],
            )
            race.fingerprint = json.dumps(fp.to_dict()) if fp else ""
            done += bool(fp)
            if args.verbose:
                where = f"lap {fp.lap}, {fp.located}/{fp.gates_per_lap} gates" if fp else "none"
                print(f"#{race.id:<5} {race.track_name[:30]:<30} {where}")
        await db.commit()
        # The registry learns from every attributed, fingerprinted run — not only from
        # the ones attributed after the fingerprint code existed. Without this the LXC
        # knew three layouts while holding 60 fingerprinted runs on seven tracks, and a
        # pushed run on a well-known track sat in the review queue (2026-09-27).
        learned = 0
        stmt = select(Race).where((Race.fingerprint != "") & (Race.track_id > 0)).order_by(Race.id)
        for race in (await db.execute(stmt)).scalars().all():
            if await repos.learn_fingerprint(db, race) is not None:
                learned += 1
        identified = await repos.identify_unidentified(db) if args.identify else 0
        if args.identify:
            identified += await repos.infer_sticky_quads_all(db)
    print(
        f"scanned {len(races)} runs, fingerprinted {done}, "
        f"{learned} new layout(s) registered"
        + (f", {identified} queued run(s) identified" if args.identify else "")
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="splitter", description="Splitter — VelociDrone lap timer"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="print the version")
    sub.add_parser("serve", help="run the web app (same as python -m splitter)")
    p = sub.add_parser("races", help="list recent races")
    p.add_argument("--track", default="")
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("settings", help="show or set runtime settings")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p = sub.add_parser(
        "backfill-crashes", help="detect crashes in the stored traces of existing runs"
    )
    p.add_argument("--all", action="store_true", help="re-scan runs already analysed")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("export", help="write every run as a document (JSON) into a directory")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--all", action="store_true")
    p.add_argument("--since", default="", help="runs started on or after this date (ISO)")
    p = sub.add_parser("import", help="import run documents (files or lists of them)")
    p.add_argument("files", nargs="+")
    p = sub.add_parser(
        "migrate-telemetry", help="convert legacy telemetry rows to blobs; --drop the old table"
    )
    p.add_argument("--drop", action="store_true")
    p = sub.add_parser(
        "retime", help="time runs that have a trace but no gate data from their flight path"
    )
    p.add_argument("ids", nargs="*", type=int, help="only these run ids (default: every candidate)")
    p = sub.add_parser(
        "backfill-fingerprints", help="fingerprint older runs from their stored traces"
    )
    p.add_argument("--all", action="store_true", help="recompute runs that already have one")
    p.add_argument(
        "--identify",
        action="store_true",
        help="afterwards, re-run identification over the runs with no track",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser(
        "fingerprint-stats",
        help="how well fingerprints separate tracks in this database (threshold spike)",
    )
    p = sub.add_parser(
        "service",
        help="install/remove/start/stop this install as a system service (--dry-run to see)",
        add_help=False,
    )
    p.add_argument("service_args", nargs=argparse.REMAINDER)
    p = sub.add_parser(
        "extract-catalog",
        help="regenerate the bundled quad/scene catalog from the game's settings.db",
    )
    p.add_argument("settings_db")
    p.add_argument("--out", default="", help="write here instead of the package data dir")
    p = sub.add_parser("fake-game", help="serve a scripted fake VelociDrone websocket for testing")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=60003)
    p.add_argument("--laps", type=int, default=3)
    p.add_argument("--gates", type=int, default=8)
    p.add_argument("--speed", type=float, default=1.0, help="time multiplier (2 = twice as fast)")
    p.add_argument("--loop", action="store_true", help="keep running races until stopped")
    p.add_argument("--no-imu", action="store_true")
    p.add_argument(
        "--no-session", action="store_true", help="single-player style: no session event"
    )
    args = parser.parse_args(argv)

    cfg = Config()
    if args.cmd == "version":
        print(__version__)
    elif args.cmd == "serve":
        from splitter.__main__ import main as serve

        serve()
    elif args.cmd == "races":
        asyncio.run(_cmd_races(cfg, args))
    elif args.cmd == "settings":
        asyncio.run(_cmd_settings(cfg, args))
    elif args.cmd == "backfill-crashes":
        asyncio.run(_cmd_backfill_crashes(cfg, args))
    elif args.cmd == "export":
        asyncio.run(_cmd_export(cfg, args))
    elif args.cmd == "import":
        asyncio.run(_cmd_import(cfg, args))
    elif args.cmd == "migrate-telemetry":
        asyncio.run(_cmd_migrate_telemetry(cfg, args))
    elif args.cmd == "retime":
        asyncio.run(_cmd_retime(cfg, args))
    elif args.cmd == "backfill-fingerprints":
        asyncio.run(_cmd_backfill_fingerprints(cfg, args))
    elif args.cmd == "service":
        from splitter.service import main as service_main

        sys.exit(service_main(args.service_args))
    elif args.cmd == "fingerprint-stats":
        from splitter.devtools.fingerprint_stats import run_stats

        asyncio.run(run_stats(_open(cfg)))
    elif args.cmd == "extract-catalog":
        from pathlib import Path

        from splitter.core.quads import BUNDLED_CATALOG, read_game_db, write_bundled_catalog

        cat = read_game_db(Path(args.settings_db))
        out = Path(args.out) if args.out else BUNDLED_CATALOG
        write_bundled_catalog(cat, out)
        print(f"wrote {len(cat.models)} models and {len(cat.scenes)} scenes to {out}")
    elif args.cmd == "fake-game":
        from splitter.devtools.fake_game import run_fake_game

        try:
            asyncio.run(
                run_fake_game(
                    host=args.host,
                    port=args.port,
                    laps=args.laps,
                    gates=args.gates,
                    speed=args.speed,
                    loop=args.loop,
                    imu=not args.no_imu,
                    session=not args.no_session,
                )
            )
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
