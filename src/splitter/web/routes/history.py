"""Race history: list, detail (laps, gates vs PB, flight path), edit, delete."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from splitter.core import quads
from splitter.db import repos
from splitter.web import analysis, clusters
from splitter.web.templating import redirect_with_flash, templates

router = APIRouter(prefix="/races")


@router.get("", response_class=HTMLResponse)
async def races_page(
    request: Request, track: str = "", quad: str = "", status: str = "", unidentified: int = 0
) -> Any:
    async with request.app.state.session_factory() as db:
        rows = await repos.list_races(
            db,
            repos.RaceFilters(
                track=track, quad=quad, status=status, unidentified=bool(unidentified), limit=200
            ),
        )
        tracks = await repos.distinct_tracks(db)
        quads = await repos.distinct_quads(db)
        nodes = await repos.nodes_status(db)
        unidentified_count = await repos.unidentified_count(db)
        retime_count = await repos.retime_candidate_count(db)
    return templates.TemplateResponse(
        request,
        "races.html",
        {
            "races": rows,
            "tracks": tracks,
            "quads": quads,
            "nodes": nodes,
            "unidentified_count": unidentified_count,
            "retime_count": retime_count,
            "filter": {
                "track": track,
                "quad": quad,
                "status": status,
                "unidentified": bool(unidentified),
            },
        },
    )


@router.get("/layouts", response_class=HTMLResponse)
async def layouts_page(request: Request) -> Any:
    """Unidentified runs grouped by layout: label a cluster once."""
    async with request.app.state.session_factory() as db:
        runs = await repos.unidentified_runs(db)
        nodes = {n.node.node_id: n.node.name for n in await repos.nodes_status(db)}
    members = []
    without = 0
    for r in runs:
        fp = repos.fingerprint_of(r)
        if fp is None:
            without += 1
            continue
        members.append(clusters.Member(r.id, r.started_at, r.node_id, fp))
    found = clusters.cluster(members)
    cards = []
    for i, c in enumerate(found, start=1):
        cen = c.centroid
        cards.append(
            {
                "n": i,
                "gates_per_lap": c.gates_per_lap,
                "runs": len(c.members),
                "race_ids": [m.race_id for m in c.members],
                "nodes": sorted(nodes.get(x, x[:8]) or x[:8] for x in c.nodes),
                "first_seen": c.first_seen,
                "last_seen": c.last_seen,
                "points": [[k, round(p[0], 1), round(p[2], 1)] for k, p in cen.positions.items()],
            }
        )
    return templates.TemplateResponse(
        request,
        "layouts.html",
        {"cards": cards, "without_fingerprint": without, "queued": len(runs)},
    )


@router.post("/layouts/label")
async def layouts_label(
    request: Request,
    race_ids: list[int] = Form([]),  # noqa: B008
    action: str = Form("label"),
    track_name: str = Form(""),
    scenery: str = Form(""),
    track_id: int = Form(0),
    scene_id: int = Form(0),
    track_source: str = Form(""),
) -> Any:
    """Label a cluster (its runs, by id) as a track, or as not a track."""
    if not race_ids:
        return redirect_with_flash("/races/layouts", error="Nothing to label.")
    if action == "label" and track_id <= 0:
        return redirect_with_flash("/races/layouts", error="Pick the track from the search first.")
    async with request.app.state.session_factory() as db:
        members = []
        for rid in race_ids:
            race = await repos.get_race(db, rid)
            fp = repos.fingerprint_of(race) if race else None
            if race is not None and fp is not None:
                members.append(clusters.Member(race.id, race.started_at, race.node_id, fp))
        if not members:
            return redirect_with_flash("/races/layouts", error="Those runs have no fingerprint.")
        group = clusters.Cluster(members[0].fingerprint.gates_per_lap)
        for m in members:
            group.add(m)
        if action == "ignore":
            await repos.label_layout(db, group.centroid, race_ids, track_id=repos.NOT_A_TRACK)
            note = f"{len(race_ids)} run{'s' if len(race_ids) != 1 else ''} marked as not a track."
        else:
            await repos.label_layout(
                db,
                group.centroid,
                race_ids,
                track_id=track_id,
                scene_id=max(0, scene_id),
                track_name=track_name.strip(),
                scenery=scenery.strip(),
                track_source=track_source.strip(),
            )
            more = await repos.identify_unidentified(db)
            await repos.infer_sticky_quads_all(db)
            note = f"Labelled {_ids(race_ids)} as {track_name.strip() or '#' + str(track_id)}"
            note += f"; {more} more recognised." if more else "."
    await request.app.state.controller.refresh_reference()
    return redirect_with_flash("/races/layouts", notice=note)


@router.post("/identify")
async def races_identify(request: Request) -> Any:
    """Re-run layout identification over every run with no track (the review
    queue), after the registry has learned something new."""
    async with request.app.state.session_factory() as db:
        n = await repos.identify_unidentified(db)
        q = await repos.infer_sticky_quads_all(db)
    await request.app.state.controller.refresh_reference()
    note = f"Identified {n} run{'s' if n != 1 else ''}." if n else "Nothing new recognised."
    if q:
        note += f" {q} run{'s' if q != 1 else ''} got the quad of the run before."
    return redirect_with_flash("/races?unidentified=1", notice=note)


@router.post("/retime")
async def races_retime(request: Request) -> Any:
    """Time every run that has a trace but no gate data from its flight path."""
    done = found = 0
    async with request.app.state.session_factory() as db:
        for race in await repos.retime_candidates(db):
            found += 1
            if await repos.retime_from_path(db, race):
                done += 1
    await request.app.state.controller.refresh_reference()
    if not found:
        return redirect_with_flash("/races", notice="No run needs timing from its flight path.")
    note = f"Timed {done} of {found} run{'s' if found != 1 else ''} from the flight path."
    if done < found:
        note += " The rest have no known gates on their track yet, or never reached one."
    return redirect_with_flash("/races", notice=note)


@router.post("/{race_id}/retime")
async def race_retime(request: Request, race_id: int) -> Any:
    async with request.app.state.session_factory() as db:
        race = await repos.get_race(db, race_id)
        if race is None:
            raise HTTPException(404)
        n = await repos.retime_from_path(db, race)
    await request.app.state.controller.refresh_reference()
    if n is None:
        return redirect_with_flash(
            f"/races/{race_id}",
            error="Could not time this run from its path: it needs a trace, a track whose "
            "gates are known from other runs, and no gate data of its own.",
        )
    return redirect_with_flash(
        f"/races/{race_id}", notice=f"Timed from the flight path: {n} gate crossings."
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
        sections = await repos.sections_for_track(db, race.track_id) if race.track_id else []
    count = race.gates_per_lap or 0
    section_rows = analysis.race_sections(race, best, sections, count) if sections and count else []
    lap_numbers = sorted(sec_lap_numbers(race))
    crashes = repos.crashes_of(race)
    crash_marks = [
        [c.t_ms, round(c.x, 2), round(c.y, 2), round(c.z, 2), c.lap, c.segment] for c in crashes
    ]
    labels = analysis.section_labels_for(race, best, count)
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
            "section_rows": section_rows,
            "lap_numbers": lap_numbers,
            "crashes": crashes,
            "crash_marks": crash_marks,
            "labels": labels,
        },
    )


def sec_lap_numbers(race: Any) -> list[int]:
    from splitter.core.sections import lap_segments

    return list(lap_segments(race.gate_times))


def _ids(ids: list[int], limit: int = 12) -> str:
    """``run #5`` / ``3 runs (#5, #6, #7)`` / ``20 runs (#1, … +8 more)`` for flashes."""
    if len(ids) == 1:
        return f"run #{ids[0]}"
    shown = ", ".join(f"#{i}" for i in ids[:limit])
    more = f", +{len(ids) - limit} more" if len(ids) > limit else ""
    return f"{len(ids)} runs ({shown}{more})"


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
    next: str = Form(""),
) -> Any:
    """Bulk edit or delete. ``next`` (a local path) is where to land afterwards:
    the track page's fix-up cards post here and want to come back."""
    back = next if next.startswith("/") and not next.startswith("//") else "/races"
    if not race_ids:
        return redirect_with_flash(back, error="Nothing selected.")
    async with request.app.state.session_factory() as db:
        if action == "delete":
            for rid in race_ids:
                await repos.delete_race(db, rid)
            return redirect_with_flash(back, notice=f"Deleted {_ids(race_ids)}.")
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
            return redirect_with_flash(back, error="Nothing to change.")
        for rid in race_ids:
            await repos.update_race(db, rid, **fields)
    what = []
    if "track_id" in fields or "track_name" in fields:
        what.append(f"track → {fields.get('track_name') or '#' + str(fields.get('track_id'))}")
    if "quad_model_id" in fields or "quad_type" in fields:
        what.append(f"quad → {fields.get('quad_type') or '#' + str(fields.get('quad_model_id'))}")
    if "scenery" in fields:
        what.append(f"scenery → {fields['scenery']}")
    return redirect_with_flash(
        back, notice=f"Updated {_ids(race_ids)}: {', '.join(what)}. PB flags recalculated."
    )
