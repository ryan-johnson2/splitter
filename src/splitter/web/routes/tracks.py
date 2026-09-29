"""Per-track analysis: PB, progression, gate consistency, theoretical best."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from splitter.core import quads
from splitter.core import sections as sec
from splitter.core.splits import theoretical_best
from splitter.db import repos
from splitter.db.models import Race
from splitter.web import analysis
from splitter.web.templating import templates

router = APIRouter(prefix="/tracks")


@router.get("", response_class=HTMLResponse)
async def tracks_page(request: Request) -> Any:
    async with request.app.state.session_factory() as db:
        groups = repos.group_tracks(await repos.track_summaries(db))
    return templates.TemplateResponse(request, "tracks.html", {"groups": groups})


@dataclass
class QuadPB:
    """One PB key of a track (quad and race length) for the "PB by quad" table."""

    quad_model_id: int
    quad_type: str
    race_laps: int
    runs: int
    finished: int
    best: Race | None
    best_lap_ms: int | None


def pb_groups(races: list[Race]) -> list[QuadPB]:
    """Per (quad model, laps): the PB run, counts and best lap; unidentified
    tracks (id 0) have no PBs, so ``best`` stays None there."""
    order: list[tuple[int, int]] = []
    members: dict[tuple[int, int], list[Race]] = {}
    for r in races:
        k = (r.quad_model_id, r.race_laps)
        if k not in members:
            order.append(k)
            members[k] = []
        members[k].append(r)
    out: list[QuadPB] = []
    for k in order:
        runs = members[k]
        finished = [r for r in runs if r.status == "finished" and (r.total_time_ms or 0) > 0]
        best = min(finished, key=lambda r: (r.total_time_ms or 0, r.id)) if finished else None
        laps = [lap.lap_ms for r in finished for lap in r.laps]
        out.append(
            QuadPB(
                quad_model_id=k[0],
                quad_type=quads.model_name(k[0]) or runs[-1].quad_type,
                race_laps=k[1],
                runs=len(runs),
                finished=len(finished),
                best=best if runs[-1].track_id > 0 else None,
                best_lap_ms=min(laps) if laps else None,
            )
        )
    out.sort(key=lambda p: (p.quad_type.lower(), p.race_laps))
    return out


def default_laps(races: list[Race]) -> int:
    """The race length to open a track on: the most flown (finished runs
    first), so totals on the page compare like with like."""
    finished = [r.race_laps for r in races if r.status == "finished"] or [
        r.race_laps for r in races
    ]
    return max(set(finished), key=finished.count) if finished else 0


@router.get("/detail", response_class=HTMLResponse)
async def track_page(
    request: Request,
    track_id: int = 0,
    quad_model: int | None = None,
    laps: int | None = None,
    track: str = "",
) -> Any:
    """One track, every quad it was flown with. PBs are still kept per quad
    and race length (the "PB by quad" table); ``quad_model`` narrows the
    analysis to one quad and ``laps`` to one race length (default: the most
    flown). ``track`` (a name) only matters for id-less legacy runs."""
    identified = track_id > 0
    async with request.app.state.session_factory() as db:
        everything = await repos.races_for_track(db, track_id, track)
        if laps is None:
            laps = default_laps(everything)
        races = [
            r
            for r in everything
            if r.race_laps == laps and (quad_model is None or r.quad_model_id == quad_model)
        ]
        finished = [r for r in races if r.status == "finished" and r.total_time_ms is not None]
        best = min(finished, key=lambda r: r.total_time_ms or 0) if finished else None
        sections = await analysis.track_analysis(db, track_id, races, best) if identified else None
        layouts = await repos.fingerprints_for_track(db, track_id) if identified else []
    pbs = pb_groups(everything)
    no_quad = [r for r in everything if not repos.has_quad(r)]
    track = everything[-1].track_name if everything else track
    quad_names = {p.quad_model_id: p.quad_type for p in pbs}
    quad = quad_names.get(quad_model, "") if quad_model is not None else ""
    quad_options = sorted({(p.quad_model_id, p.quad_type) for p in pbs}, key=lambda q: q[1].lower())
    laps_options = sorted({p.race_laps for p in pbs})
    # Each run against the PB of its own quad and length (they differ when
    # the page shows every quad).
    pb_of: dict[tuple[int, int], int] = {
        (p.quad_model_id, p.race_laps): p.best.total_time_ms or 0 for p in pbs if p.best
    }
    vs_pb = {
        r.id: (r.total_time_ms or 0) - pb_of[(r.quad_model_id, r.race_laps)]
        for r in races
        if r.status == "finished" and r.total_time_ms and (r.quad_model_id, r.race_laps) in pb_of
    }

    # Progression: total time per finished run, in order.
    progression = [
        {
            "id": r.id,
            "at": r.started_at.isoformat() + "Z",
            "ms": r.total_time_ms,
            "best": r.is_best,
            "quad": quad_names.get(r.quad_model_id) or r.quad_type,
        }
        for r in finished
    ]
    # Gate consistency: per crossing index, best / median / this-PB segment time.
    gate_ms_by_race = [[g.gate_ms for g in r.gate_times] for r in finished]
    theo = theoretical_best(gate_ms_by_race)
    n = 0
    if gate_ms_by_race:
        lengths = [len(g) for g in gate_ms_by_race if g]
        n = max(set(lengths), key=lengths.count) if lengths else 0
    comparable = [g for g in gate_ms_by_race if len(g) == n]
    gates: list[dict[str, Any]] = []
    best_gates = [g.gate_ms for g in best.gate_times] if best else []
    labels = [(g.lap, g.gate) for g in best.gate_times] if best else []
    for i in range(n):
        column = sorted(g[i] for g in comparable)
        median = column[len(column) // 2]
        gates.append(
            {
                "seq": i,
                "label": f"L{labels[i][0]} G{labels[i][1]}" if i < len(labels) else f"#{i + 1}",
                "best": column[0],
                "median": median,
                "worst": column[-1],
                "pb": best_gates[i] if i < len(best_gates) else None,
                "spread": column[-1] - column[0],
            }
        )
    lap_bests: dict[int, int] = {}
    for r in finished:
        for lap in r.laps:
            if lap.lap not in lap_bests or lap.lap_ms < lap_bests[lap.lap]:
                lap_bests[lap.lap] = lap.lap_ms
    best_lap = min(lap_bests.values()) if lap_bests else None
    return templates.TemplateResponse(
        request,
        "track_detail.html",
        {
            "track": track,
            "track_id": track_id,
            "track_query": track if not identified else "",
            "quad": quad,
            "quad_model": quad_model,
            "quad_options": quad_options,
            "laps": laps,
            "laps_options": laps_options,
            "pbs": pbs,
            "no_quad": no_quad,
            "here": str(request.url.path) + ("?" + request.url.query if request.url.query else ""),
            "identified": identified,
            "races": list(reversed(races)),
            "all_runs": len(everything),
            "vs_pb": vs_pb,
            "finished_count": len(finished),
            "best": best,
            "best_quad": (quad_names.get(best.quad_model_id) or best.quad_type) if best else "",
            "best_lap": best_lap,
            "theoretical": theo,
            "progression": progression,
            "gates": gates,
            "comparable": len(comparable),
            "analysis": sections,
            "section_stats": sorted(sections.stats, key=lambda s: -(s.on_table_ms or -1))
            if sections
            else [],
            "layouts": layouts,
        },
    )


@router.post("/{track_id}/layouts/{fingerprint_id}/forget")
async def layout_forget(request: Request, track_id: int, fingerprint_id: int) -> Any:
    """Drop one registry row (a wrong attribution taught it); runs are untouched."""
    from splitter.web.templating import redirect_with_flash

    async with request.app.state.session_factory() as db:
        ok = await repos.forget_fingerprint(db, fingerprint_id)
    if not ok:
        raise HTTPException(404)
    return redirect_with_flash(
        f"/tracks/detail?track_id={track_id}#layouts", notice="Layout forgotten."
    )


@router.post("/{track_id}/sections")
async def sections_save(request: Request, track_id: int) -> Any:
    """Replace a track's sections: ``{"count": N, "sections": [{name, first, last}, …]}``."""
    body = await request.json()
    try:
        count = int(body.get("count", 0))
        items = [
            sec.Section(i, str(s.get("name", "")), int(s["first"]), int(s["last"]))
            for i, s in enumerate(body.get("sections", []), start=1)
        ]
        valid = sec.validate(items, count)
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    if track_id <= 0:
        raise HTTPException(400, "sections need an online track id")
    async with request.app.state.session_factory() as db:
        await repos.save_sections(db, track_id, valid)
    return {"ok": True, "sections": [s.to_dict() for s in valid]}


@router.post("/{track_id}/sections/reset")
async def sections_reset(request: Request, track_id: int) -> Any:
    """Drop the layout; the next view of the track re-suggests it from the data."""
    async with request.app.state.session_factory() as db:
        await repos.save_sections(db, track_id, [])
    return {"ok": True}
