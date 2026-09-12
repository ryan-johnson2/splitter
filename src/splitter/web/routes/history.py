"""Race history: list, detail (laps, gates vs PB, flight path), edit, delete."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from splitter.core import quads
from splitter.db import repos
from splitter.web.templating import redirect_with_flash, templates

router = APIRouter(prefix="/races")


@router.get("", response_class=HTMLResponse)
async def races_page(request: Request, track: str = "", quad: str = "", status: str = "") -> Any:
    async with request.app.state.session_factory() as db:
        rows = await repos.list_races(
            db, repos.RaceFilters(track=track, quad=quad, status=status, limit=200)
        )
        tracks = await repos.distinct_tracks(db)
        quads = await repos.distinct_quads(db)
    return templates.TemplateResponse(
        request,
        "races.html",
        {
            "races": rows,
            "tracks": tracks,
            "quads": quads,
            "filter": {"track": track, "quad": quad, "status": status},
        },
    )


@router.get("/{race_id}", response_class=HTMLResponse)
async def race_page(request: Request, race_id: int) -> Any:
    async with request.app.state.session_factory() as db:
        race = await repos.get_race(db, race_id)
        if race is None:
            raise HTTPException(404)
        best = await repos.get_best_race(db, repos.race_key(race))
        reference = repos.reference_from_race(best) if best and best.id != race.id else None
        samples = await repos.telemetry_for_race(db, race_id)
    gate_rows = []
    for g in race.gate_times:
        gate_rows.append(
            {
                "g": g,
                "vs_best": reference.split_at(g.seq, g.cumulative_ms) if reference else None,
                "seg_vs_best": reference.gate_delta(g.seq, g.gate_ms) if reference else None,
            }
        )
    lap_rows = [
        {"lap": lap, "vs_best": reference.lap_delta(lap.lap, lap.lap_ms) if reference else None}
        for lap in race.laps
    ]
    telemetry = [
        [s.t_ms, round(s.x, 2), round(s.y, 2), round(s.z, 2), round(s.speed, 2)] for s in samples
    ]
    gate_marks = [[g.cumulative_ms, g.lap, g.gate] for g in race.gate_times]
    return templates.TemplateResponse(
        request,
        "race_detail.html",
        {
            "race": race,
            "best": best,
            "reference": reference,
            "gate_rows": gate_rows,
            "lap_rows": lap_rows,
            "telemetry": telemetry,
            "gate_marks": gate_marks,
        },
    )


def _identity_fields(
    track_id: int, scene_id: int, track_source: str, quad_model_id: int, quad_class_id: int
) -> dict[str, Any]:
    """Picker ids to apply; ids are only touched when a pick was made (> 0)."""
    out: dict[str, Any] = {}
    if track_id > 0:
        out.update(track_id=track_id, scene_id=max(0, scene_id), track_source=track_source.strip())
    if quad_model_id > 0:
        model = quads.catalog().model(quad_model_id)
        out.update(
            quad_model_id=quad_model_id,
            quad_class_id=quad_class_id or (model.component_group_id if model else 0),
        )
        if model:
            out["quad_type"] = model.name
    return out


@router.post("/{race_id}/edit")
async def race_edit(
    request: Request,
    race_id: int,
    track_name: str = Form(""),
    scenery: str = Form(""),
    quad_type: str = Form(""),
    quad_size: str = Form(""),
    race_laps: int = Form(0),
    notes: str = Form(""),
    track_id: int = Form(0),
    scene_id: int = Form(0),
    track_source: str = Form(""),
    quad_model_id: int = Form(0),
    quad_class_id: int = Form(0),
) -> Any:
    fields: dict[str, Any] = {
        "track_name": track_name.strip(),
        "scenery": scenery.strip(),
        "quad_type": quad_type.strip(),
        "quad_size": quad_size.strip(),
        "race_laps": race_laps,
        "notes": notes.strip(),
    }
    fields.update(_identity_fields(track_id, scene_id, track_source, quad_model_id, quad_class_id))
    async with request.app.state.session_factory() as db:
        race = await repos.update_race(db, race_id, **fields)
    if race is None:
        raise HTTPException(404)
    return redirect_with_flash(f"/races/{race_id}", notice="Race updated.")


@router.post("/{race_id}/delete")
async def race_delete(request: Request, race_id: int) -> Any:
    async with request.app.state.session_factory() as db:
        ok = await repos.delete_race(db, race_id)
    if not ok:
        raise HTTPException(404)
    return redirect_with_flash("/races", notice=f"Race #{race_id} deleted.")


@router.post("/bulk")
async def races_bulk(
    request: Request,
    race_ids: list[int] = Form([]),  # noqa: B008
    action: str = Form(""),
    track_name: str = Form(""),
    quad_type: str = Form(""),
    scenery: str = Form(""),
    track_id: int = Form(0),
    scene_id: int = Form(0),
    track_source: str = Form(""),
    quad_model_id: int = Form(0),
    quad_class_id: int = Form(0),
) -> Any:
    if not race_ids:
        return redirect_with_flash("/races", error="Nothing selected.")
    async with request.app.state.session_factory() as db:
        if action == "delete":
            for rid in race_ids:
                await repos.delete_race(db, rid)
            return redirect_with_flash("/races", notice=f"Deleted {len(race_ids)} races.")
        fields: dict[str, Any] = {}
        if track_name.strip():
            fields["track_name"] = track_name.strip()
        if quad_type.strip():
            fields["quad_type"] = quad_type.strip()
        if scenery.strip():
            fields["scenery"] = scenery.strip()
        fields.update(
            _identity_fields(track_id, scene_id, track_source, quad_model_id, quad_class_id)
        )
        if not fields:
            return redirect_with_flash("/races", error="Nothing to change.")
        for rid in race_ids:
            await repos.update_race(db, rid, **fields)
    return redirect_with_flash("/races", notice=f"Updated {len(race_ids)} races.")
