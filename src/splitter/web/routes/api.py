"""JSON endpoints used by the live page and by anything scripting Splitter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from splitter.db import repos

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


@router.post("/session")
async def set_session(request: Request, body: SessionIn) -> dict[str, Any]:
    if not body.track_name.strip():
        raise HTTPException(400, "track_name is required")
    controller = request.app.state.controller
    await controller.set_manual_session(
        body.track_name,
        body.scenery,
        body.quad_type,
        body.quad_size,
        body.race_laps,
        body.race_mode,
    )
    return dict(controller.session.to_dict())


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
        "quad_type": r.quad_type,
        "quad_size": r.quad_size,
        "race_mode": r.race_mode,
        "race_format": r.race_format,
        "race_laps": r.race_laps,
        "player_name": r.player_name,
        "session_source": r.session_source,
        "status": r.status,
        "started_at": r.started_at.isoformat() + "Z",
        "ended_at": r.ended_at.isoformat() + "Z" if r.ended_at else None,
        "total_time_ms": r.total_time_ms,
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
