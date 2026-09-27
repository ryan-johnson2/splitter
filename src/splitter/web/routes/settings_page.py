"""Settings: the game PC address, player name, telemetry and logging options."""

from __future__ import annotations

import secrets
import socket
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from splitter.core import netinfo
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
            # The node name's placeholder: what the uploader sends when it is blank.
            "hostname": socket.gethostname(),
            "local_addresses": netinfo.local_ipv4_addresses() if state.config.desktop else [],
            "time_formats": TIME_FORMATS,
            "bridge": state.bridge.status(),
            "controller": state.controller,
            "sync": state.uploader.status() if hasattr(state, "uploader") else None,
            "acked_local": await _acked_local(state),
        },
    )


async def _acked_local(state: Any) -> int:
    from splitter.db import repos

    async with state.session_factory() as db:
        return await repos.acked_count(db)


@router.post("/ingest-token")
async def ingest_token(request: Request, action: str = Form("generate")) -> Any:
    """Turn receiving on (a fresh token), rotate it, or turn it off."""
    state = request.app.state
    value = secrets.token_urlsafe(32) if action == "generate" else ""
    async with state.session_factory() as db:
        await state.settings.set(db, "ingest_token", value)
    note = (
        "Receiving is on: give the token to the sending Splitter." if value else "Receiving is off."
    )
    return redirect_with_flash("/settings#receive", notice=note)


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
    node_name: str = Form(""),
    time_format: str = Form("seconds"),
    pace_yellow_s: float = Form(2.0),
    show_getting_started: str = Form("0"),
    upstream_url: str = Form(""),
    upstream_token: str = Form(""),
    keep_local_runs: str = Form("1"),
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
        "node_name": node_name.strip()[:40],
        "time_format": time_format if time_format in TIME_FORMATS else "seconds",
        "pace_yellow_s": f"{max(0.1, min(60.0, pace_yellow_s)):g}",
        "show_getting_started": "1" if show_getting_started == "1" else "0",
        "upstream_url": upstream_url.strip().rstrip("/"),
        "upstream_token": upstream_token.strip(),
        "keep_local_runs": "1" if keep_local_runs == "1" else "0",
    }
    host_changed = values["game_host"] != settings.get("game_host") or values[
        "game_port"
    ] != settings.get("game_port")
    sync_changed = any(
        values[k] != settings.get(k) for k in ("upstream_url", "upstream_token", "keep_local_runs")
    )
    async with state.session_factory() as db:
        await settings.set_many(db, values)
    if sync_changed and hasattr(state, "uploader"):
        await state.uploader.reconfigure()
        await state.controller.refresh_reference()
    state.controller.player_name = values["player_name"] or state.controller.player_name
    if host_changed or (values["auto_connect"] == "1" and not state.bridge.enabled):
        if values["game_host"]:
            state.bridge.configure(values["game_host"], int(values["game_port"]))
        else:
            state.bridge.disconnect()
    return redirect_with_flash("/settings", notice="Settings saved.")
