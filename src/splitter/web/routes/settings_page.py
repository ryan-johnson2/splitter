"""Settings: the game PC address, player name, telemetry and logging options."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from splitter.core.timeparse import TIME_FORMATS
from splitter.web.templating import redirect_with_flash, templates

router = APIRouter(prefix="/settings")


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request) -> Any:
    state = request.app.state
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "values": state.settings.all(),
            "time_formats": TIME_FORMATS,
            "bridge": state.bridge.status(),
            "controller": state.controller,
        },
    )


@router.post("")
async def settings_save(
    request: Request,
    game_host: str = Form(""),
    game_port: int = Form(60003),
    player_name: str = Form(""),
    auto_connect: str = Form("0"),
    telemetry_enabled: str = Form("0"),
    telemetry_store_hz: int = Form(20),
    event_log_enabled: str = Form("0"),
    event_log_keep: int = Form(5000),
    brand_name: str = Form("Splitter"),
    time_format: str = Form("seconds"),
) -> Any:
    state = request.app.state
    settings = state.settings
    values = {
        "game_host": game_host.strip(),
        "game_port": str(game_port),
        "player_name": player_name.strip(),
        "auto_connect": "1" if auto_connect == "1" else "0",
        "telemetry_enabled": "1" if telemetry_enabled == "1" else "0",
        "telemetry_store_hz": str(max(1, min(60, telemetry_store_hz))),
        "event_log_enabled": "1" if event_log_enabled == "1" else "0",
        "event_log_keep": str(max(100, event_log_keep)),
        "brand_name": brand_name.strip() or "Splitter",
        "time_format": time_format if time_format in TIME_FORMATS else "seconds",
    }
    host_changed = values["game_host"] != settings.get("game_host") or values[
        "game_port"
    ] != settings.get("game_port")
    async with state.session_factory() as db:
        await settings.set_many(db, values)
    state.controller.player_name = values["player_name"] or state.controller.player_name
    if host_changed or (values["auto_connect"] == "1" and not state.bridge.enabled):
        if values["game_host"]:
            state.bridge.configure(values["game_host"], int(values["game_port"]))
        else:
            state.bridge.disconnect()
    return redirect_with_flash("/settings", notice="Settings saved.")
