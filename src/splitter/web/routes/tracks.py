"""Per-track analysis: PB, progression, gate consistency, theoretical best."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from splitter.core import quads
from splitter.core.splits import theoretical_best
from splitter.db import repos
from splitter.web.templating import templates

router = APIRouter(prefix="/tracks")


@router.get("", response_class=HTMLResponse)
async def tracks_page(request: Request) -> Any:
    async with request.app.state.session_factory() as db:
        summaries = await repos.track_summaries(db)
    return templates.TemplateResponse(request, "tracks.html", {"summaries": summaries})


@router.get("/detail", response_class=HTMLResponse)
async def track_page(
    request: Request, track_id: int = 0, quad_model: int = 0, laps: int = 0, track: str = ""
) -> Any:
    """One PB group. ``track`` (a name) only matters for id-less legacy runs."""
    key = repos.PBKey(track_id, quad_model, laps)
    async with request.app.state.session_factory() as db:
        races = await repos.races_for_key(db, key)
    if not key.valid:
        races = [r for r in races if r.track_name == track]
    track = races[-1].track_name if races else track
    quad = quads.model_name(quad_model) or (races[-1].quad_type if races else "")
    finished = [r for r in races if r.status == "finished" and r.total_time_ms is not None]
    best = min(finished, key=lambda r: r.total_time_ms or 0) if finished else None

    # Progression: total time per finished run, in order.
    progression = [
        {"id": r.id, "at": r.started_at.isoformat() + "Z", "ms": r.total_time_ms, "best": r.is_best}
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
            "quad": quad,
            "laps": laps,
            "identified": key.valid,
            "races": list(reversed(races)),
            "finished_count": len(finished),
            "best": best,
            "best_lap": best_lap,
            "theoretical": theo,
            "progression": progression,
            "gates": gates,
            "comparable": len(comparable),
        },
    )
