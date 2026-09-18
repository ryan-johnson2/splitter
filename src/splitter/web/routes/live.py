"""The live page and its websocket feed."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from splitter.web.templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def live_page(request: Request) -> Any:
    controller = request.app.state.controller
    return templates.TemplateResponse(
        request,
        "live.html",
        {
            "snapshot": controller.snapshot(),
            "settings": request.app.state.settings,
            "game_host_set": bool(request.app.state.settings.get("game_host")),
            "desktop": request.app.state.config.desktop,
            "show_getting_started": request.app.state.settings.get_bool("show_getting_started"),
            "game_ever_connected": request.app.state.settings.get_bool("game_ever_connected"),
        },
    )


@router.websocket("/ws/live")
async def live_feed(ws: WebSocket) -> None:
    hub = ws.app.state.hub
    controller = ws.app.state.controller
    await ws.accept()
    queue = hub.subscribe()
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": controller.snapshot()}))

        async def pump() -> None:
            while True:
                await ws.send_text(await queue.get())

        async def listen() -> None:
            # Browser → server is only used for keep-alive / resync requests.
            while True:
                text = await ws.receive_text()
                if text == "snapshot":
                    await ws.send_text(
                        json.dumps({"type": "snapshot", "data": controller.snapshot()})
                    )

        pump_task = asyncio.create_task(pump())
        listen_task = asyncio.create_task(listen())
        try:
            done, _ = await asyncio.wait(
                [pump_task, listen_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        finally:
            for task in (pump_task, listen_task):
                task.cancel()
            await asyncio.gather(pump_task, listen_task, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await ws.close()
