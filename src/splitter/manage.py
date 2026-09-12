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


async def _cmd_races(cfg: Config, args: argparse.Namespace) -> None:
    from splitter.db import repos

    sf = _session_factory(cfg)
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
    from splitter.db.engine import init_db
    from splitter.db.runtime_settings import RuntimeSettings

    sf = _session_factory(cfg)
    await init_db(sf.kw["bind"])
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
