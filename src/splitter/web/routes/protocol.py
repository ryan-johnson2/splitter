"""Raw game frame log — what the game actually sent, for protocol debugging."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from splitter.db import repos
from splitter.web.templating import redirect_with_flash, templates

router = APIRouter(prefix="/protocol")


@router.get("", response_class=HTMLResponse)
async def protocol_page(request: Request, type: str = "", race: int | None = None) -> Any:
    async with request.app.state.session_factory() as db:
        events = await repos.recent_events(db, limit=300, event_type=type, race_id=race)
        types = await repos.event_types(db)
    return templates.TemplateResponse(
        request,
        "protocol.html",
        {"events": events, "types": types, "filter": {"type": type, "race": race}},
    )


@router.post("/clear")
async def protocol_clear(request: Request) -> Any:
    async with request.app.state.session_factory() as db:
        await repos.prune_event_log(db, 0)
    return redirect_with_flash("/protocol", notice="Event log cleared.")
