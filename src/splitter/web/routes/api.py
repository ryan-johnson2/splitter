"""JSON endpoints used by the live page and by anything scripting Splitter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from splitter.core import quads
from splitter.db import repos
from splitter.game.catalog import SOURCES, SearchResult

router = APIRouter(prefix="/api")


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    return dict(request.app.state.controller.snapshot())


class SessionIn(BaseModel):
    track_name: str
    scenery: str = ""
    quad_type: str = ""
    quad_size: str = ""
    race_laps: int = 0
    race_mode: str = ""
    # From the track picker; 0 / "" when the name was typed by hand.
    track_id: int = 0
    scene_id: int = 0
    track_source: str = ""
    # From the quad picker (catalog model id); 0 = unknown quad.
    quad_model_id: int = 0
    quad_class_id: int = 0


@router.post("/session")
async def set_session(request: Request, body: SessionIn) -> dict[str, Any]:
    if not body.track_name.strip():
        raise HTTPException(400, "track_name is required")
    if body.track_id <= 0:
        # PBs are keyed by online track id, so a typed name is not enough.
        raise HTTPException(400, "pick the track from the search so it has an online id")
    controller = request.app.state.controller
    await controller.set_manual_session(
        body.track_name,
        body.scenery,
        body.quad_type,
        body.quad_size,
        body.race_laps,
        body.race_mode,
        track_id=body.track_id,
        scene_id=body.scene_id,
        track_source=body.track_source if body.track_source in SOURCES else "",
        quad_model_id=max(0, body.quad_model_id),
        quad_class_id=max(0, body.quad_class_id),
    )
    return dict(controller.session.to_dict())


@router.get("/quads")
async def quad_catalog() -> dict[str, Any]:
    """Quad models grouped by class, from the bundled game catalog."""
    cat = quads.catalog()
    return {"classes": cat.classes(), "generated_at": cat.generated_at, "source": cat.source}


@router.get("/tracks/search")
async def track_search(
    request: Request, q: str = "", source: str = "", limit: int = 30
) -> dict[str, Any]:
    """Search the game's online track lists (official + community) for the picker.

    ``source`` is ``official``, ``community`` or empty for both. A source that
    fails is reported under ``errors`` while the other's results still come back.
    """
    catalog = request.app.state.catalog
    result: SearchResult = await catalog.search(q, source, limit=max(1, min(limit, 100)))
    return result.to_dict()


class ConnectionIn(BaseModel):
    action: str  # connect | disconnect | reconnect
    host: str | None = None
    port: int | None = None


@router.post("/connection")
async def connection(request: Request, body: ConnectionIn) -> dict[str, Any]:
    bridge = request.app.state.bridge
    settings = request.app.state.settings
    if body.action == "connect":
        host = (body.host if body.host is not None else settings.get("game_host")).strip()
        port = body.port or settings.get_int("game_port")
        if not host:
            raise HTTPException(400, "no game host configured")
        if host != settings.get("game_host") or port != settings.get_int("game_port"):
            async with request.app.state.session_factory() as db:
                await settings.set_many(db, {"game_host": host, "game_port": str(port)})
        bridge.configure(host, port)
    elif body.action == "disconnect":
        bridge.disconnect()
    elif body.action == "reconnect":
        bridge.reconnect()
    else:
        raise HTTPException(400, "unknown action")
    return dict(bridge.status())


class AbortIn(BaseModel):
    in_game: bool = True  # also send the game's abortrace command when connected


@router.post("/race/abort")
async def race_abort(request: Request, body: AbortIn | None = None) -> dict[str, Any]:
    """Manual abort: the timer got stuck (game closed mid-run) or the pilot gives up."""
    controller = request.app.state.controller
    bridge = request.app.state.bridge
    sent = False
    if (body is None or body.in_game) and controller.race_active:
        sent = await bridge.abort_race()
    race_id = await controller.abort_race("manual")
    return {"aborted": race_id is not None, "race_id": race_id, "in_game": sent}


@router.get("/races")
async def races(request: Request, track: str = "", quad: str = "", limit: int = 100) -> list[Any]:
    async with request.app.state.session_factory() as db:
        rows = await repos.list_races(db, repos.RaceFilters(track=track, quad=quad, limit=limit))
        return [_race_dict(r) for r in rows]


@router.get("/races/{race_id}")
async def race(request: Request, race_id: int) -> dict[str, Any]:
    async with request.app.state.session_factory() as db:
        r = await repos.get_race(db, race_id)
        if r is None:
            raise HTTPException(404)
        d = _race_dict(r)
        d["laps"] = [
            {
                "lap": lap.lap,
                "lap_ms": lap.lap_ms,
                "cumulative_ms": lap.cumulative_ms,
                "gates": lap.gates,
                "delta_ms": lap.delta_ms,
                "max_speed": lap.max_speed,
                "avg_speed": lap.avg_speed,
            }
            for lap in r.laps
        ]
        d["gate_times"] = [
            {
                "seq": g.seq,
                "lap": g.lap,
                "gate": g.gate,
                "cumulative_ms": g.cumulative_ms,
                "gate_ms": g.gate_ms,
                "split_ms": g.split_ms,
                "ends_lap": g.ends_lap,
                "max_speed": g.max_speed,
                "avg_speed": g.avg_speed,
            }
            for g in r.gate_times
        ]
        samples = await repos.telemetry_for_race(db, race_id)
        d["telemetry"] = [[s.t_ms, s.x, s.y, s.z, round(s.speed, 2)] for s in samples]
        return d


@router.delete("/races/{race_id}")
async def delete_race(request: Request, race_id: int) -> dict[str, Any]:
    async with request.app.state.session_factory() as db:
        if not await repos.delete_race(db, race_id):
            raise HTTPException(404)
    return {"ok": True}


def _race_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "track_name": r.track_name,
        "scenery": r.scenery,
        "track_id": r.track_id,
        "scene_id": r.scene_id,
        "track_source": r.track_source,
        "quad_type": r.quad_type,
        "quad_size": r.quad_size,
        "quad_model_id": r.quad_model_id,
        "quad_class_id": r.quad_class_id,
        "quad_class": quads.class_name(r.quad_class_id),
        "race_mode": r.race_mode,
        "race_format": r.race_format,
        "race_laps": r.race_laps,
        "player_name": r.player_name,
        "session_source": r.session_source,
        "status": r.status,
        "started_at": r.started_at.isoformat() + "Z",
        "ended_at": r.ended_at.isoformat() + "Z" if r.ended_at else None,
        "total_time_ms": r.total_time_ms,
        "holeshot_ms": r.holeshot_ms,
        "total_laps": r.total_laps,
        "gates_per_lap": r.gates_per_lap,
        "is_best": r.is_best,
        "pb_delta_ms": r.pb_delta_ms,
        "max_speed": r.max_speed,
        "avg_speed": r.avg_speed,
        "distance_m": r.distance_m,
        "telemetry_samples": r.telemetry_samples,
        "notes": r.notes,
    }
