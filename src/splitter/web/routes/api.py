"""JSON endpoints used by the live page and by anything scripting Splitter."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from splitter.core import quads
from splitter.db import repos
from splitter.game.catalog import SearchResult
from splitter.sync import commands
from splitter.sync.commands import AbortIn, CaptureIn, SessionIn
from splitter.version import __version__

MAX_IMPORT_DOCS = 500

router = APIRouter(prefix="/api")


@router.get("/state")
async def state(request: Request) -> dict[str, Any]:
    return dict(request.app.state.controller.snapshot())


@router.post("/session")
async def set_session(request: Request, body: SessionIn) -> dict[str, Any]:
    state = request.app.state
    try:
        return await commands.set_session(state.controller, state.session_factory, body)
    except commands.CommandError as e:
        raise HTTPException(e.status, e.detail) from e


@router.post("/capture")
async def capture(request: Request, body: CaptureIn) -> dict[str, Any]:
    """Pause or resume recording; paused keeps the game link but ignores races."""
    return await commands.set_capture(request.app.state.controller, body)


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
    uploader = getattr(request.app.state, "uploader", None)
    if not getattr(catalog, "available", True) and uploader is not None and uploader.configured:
        # No private client here: the web has one, so search through it.
        try:
            return dict(await uploader.proxy_search(q, source, max(1, min(limit, 100))))
        except Exception as e:  # the picker shows the error; nothing else depends on it
            return {"tracks": [], "errors": {"upstream": f"{type(e).__name__}: {e}"}}
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


@router.post("/race/abort")
async def race_abort(request: Request, body: AbortIn | None = None) -> dict[str, Any]:
    """Manual abort: the timer got stuck (game closed mid-run) or the pilot gives up."""
    state = request.app.state
    return await commands.abort(state.controller, state.bridge, body or AbortIn())


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


@router.get("/races/{race_id}/export")
async def export_race(request: Request, race_id: int) -> JSONResponse:
    """The run as one document (``core/rundoc.py``); ``POST /api/import`` takes it anywhere."""
    async with request.app.state.session_factory() as db:
        r = await repos.get_race(db, race_id)
        if r is None:
            raise HTTPException(404)
        if r.status == "running":
            raise HTTPException(409, "the run is still going")
        doc = await repos.export_run(db, r, __version__)
    name = f"splitter-run-{r.started_at:%Y%m%d-%H%M%S}-{r.uuid[:8]}.json"
    return JSONResponse(doc, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/import")
async def import_races(request: Request) -> dict[str, Any]:
    """Import one run document or a list of them. Idempotent by uuid: a run
    already here is reported as ``exists`` and left alone."""
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(400, f"body is not JSON: {e}") from e
    docs = body if isinstance(body, list) else [body]
    if len(docs) > MAX_IMPORT_DOCS:
        raise HTTPException(413, f"at most {MAX_IMPORT_DOCS} documents per request")
    async with request.app.state.session_factory() as db:
        results = await repos.import_runs(
            db, docs, origin="import", default_node_id=request.app.state.settings.get("node_id")
        )
    uploader = getattr(request.app.state, "uploader", None)
    if uploader is not None:
        # Imported runs travel on like captured ones (a no-op without an upstream).
        for r in results:
            if r.status == "created" and r.race_id:
                await uploader.enqueue(r.race_id)
    await request.app.state.controller.refresh_reference()
    statuses = ("created", "exists", "rejected")
    counts = {s: sum(1 for r in results if r.status == s) for s in statuses}
    return {"results": [r.to_dict() for r in results], **counts}


def _race_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "uuid": r.uuid,
        "node_id": r.node_id,
        "origin": r.origin,
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
        "crash_count": r.crash_count,
        "notes": r.notes,
    }


# ── sync (node side) ─────────────────────────────────────────────


@router.get("/sync")
async def sync_status(request: Request) -> dict[str, Any]:
    uploader = request.app.state.uploader
    async with request.app.state.session_factory() as db:
        rows = await repos.outbox_rows(db)
    return dict(uploader.status()) | {
        "outbox": [
            {
                "uuid": r.race_uuid,
                "seq": r.seq,
                "queued_at": r.queued_at.isoformat() + "Z",
                "attempts": r.attempts,
                "acked_at": r.acked_at.isoformat() + "Z" if r.acked_at else None,
                "terminal": r.terminal,
                "last_error": r.last_error,
            }
            for r in rows
        ]
    }


@router.post("/sync/test")
async def sync_test(request: Request) -> dict[str, Any]:
    """Can this node reach the upstream with its token?"""
    return dict(await request.app.state.uploader.ping())


@router.post("/sync/flush")
async def sync_flush(request: Request) -> dict[str, Any]:
    """Push everything due now instead of waiting for the loop."""
    uploader = request.app.state.uploader
    if uploader.configured and not uploader.token_blocked:
        await uploader.process_once()
    return dict(uploader.status())


@router.post("/sync/purge")
async def sync_purge(request: Request) -> dict[str, Any]:
    """Delete the local copies of runs the web has acknowledged."""
    n = await request.app.state.uploader.purge()
    await request.app.state.uploader.refresh_counts()
    return {"deleted": n}


@router.post("/sync/retry")
async def sync_retry(request: Request) -> dict[str, Any]:
    """Put terminal rows back in the queue (after upgrading the web, say)."""
    from sqlalchemy import update

    from splitter.db.models import Outbox
    from splitter.util import utcnow

    async with request.app.state.session_factory() as db:
        result = await db.execute(
            update(Outbox)
            .where(Outbox.terminal != "")
            .values(terminal="", attempts=0, next_at=utcnow(), last_error="")
        )
        await db.commit()
    uploader = request.app.state.uploader
    uploader.last_error = ""
    await uploader.refresh_counts()
    uploader.wake()
    return {"requeued": int(getattr(result, "rowcount", 0) or 0)}
