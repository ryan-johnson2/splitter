"""The web side of the relay (#14): the node's socket in, the browsers'
sockets out, the Live page per node, and the commands back down."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from splitter.db import repos
from splitter.live.relay import RelayError, RelayNode
from splitter.sync.commands import AbortIn, CaptureIn, SessionIn
from splitter.web.templating import templates

router = APIRouter()

# Close codes the node reads as "the web said no": it then retries slowly and
# the uploader's own indicator explains which (same token, same switch).
CLOSE_RECEIVING_OFF = 4403
CLOSE_BAD_TOKEN = 4401
CLOSE_NO_ID = 4400


class _WsChannel:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send(self, text: str) -> None:
        await self._ws.send_text(text)

    async def recv(self) -> str:
        return await self._ws.receive_text()


def _token_ok(ws: WebSocket) -> int:
    """0 when the bearer matches the ingest token, else the close code to send."""
    token = ws.app.state.settings.get("ingest_token").strip()
    if not token:
        return CLOSE_RECEIVING_OFF
    scheme, _, presented = ws.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), token):
        return CLOSE_BAD_TOKEN
    return 0


@router.websocket("/ws/relay")
async def relay_in(ws: WebSocket) -> None:
    """A node's outbound socket: its live feed up, our commands down."""
    await ws.accept()
    code = _token_ok(ws)
    if code:
        await ws.close(
            code=code,
            reason="receiving runs is off here" if code == CLOSE_RECEIVING_OFF else "bad token",
        )
        return
    node_id = ws.headers.get("x-splitter-node-id", "").strip()
    name = ws.headers.get("x-splitter-node", "").strip()[:80]
    if not node_id:
        await ws.close(code=CLOSE_NO_ID, reason="no node id")
        return
    state = ws.app.state
    node = RelayNode(node_id, name, _WsChannel(ws))
    state.relays.add(node)
    async with state.session_factory() as db:
        await repos.note_node(db, node_id=node_id, name=name, seq=0, imu_seen=False)
    try:
        await node.serve()
    except WebSocketDisconnect:
        pass
    finally:
        state.relays.remove(node)
        with contextlib.suppress(Exception):
            await ws.close()


@router.websocket("/ws/live/{node_id}")
async def relay_out(ws: WebSocket, node_id: str) -> None:
    """A browser watching one node: the node's feed, re-sent."""
    await ws.accept()
    node = ws.app.state.relays.get(node_id)
    if node is None:
        # Not relaying now: say so and hang up; the page retries by itself.
        await ws.send_text(
            json.dumps({"type": "node", "data": {"connected": False, "node_id": node_id}})
        )
        with contextlib.suppress(Exception):
            await ws.close()
        return
    queue = node.hub.subscribe()
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "data": node.snapshot}))
        node.request_snapshot()

        async def pump() -> None:
            while True:
                await ws.send_text(await queue.get())

        async def listen() -> None:
            while True:
                text = await ws.receive_text()
                if text == "snapshot":
                    node.request_snapshot()

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
        node.hub.unsubscribe(queue)
        with contextlib.suppress(Exception):
            await ws.close()


# ── pages ──────────────────────────────────────────────────────


@router.get("/live/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request) -> Any:
    state = request.app.state
    async with state.session_factory() as db:
        known = await repos.nodes_status(db)
    live = {n.node_id: n for n in state.relays.all()}
    return templates.TemplateResponse(
        request,
        "live_nodes.html",
        {
            "live": state.relays.all(),
            "known": [k for k in known if k.node.node_id not in live],
            "receiving": bool(state.settings.get("ingest_token").strip()),
        },
    )


@router.get("/live/{node_id}", response_class=HTMLResponse)
async def node_live_page(request: Request, node_id: str) -> Any:
    state = request.app.state
    node = state.relays.get(node_id)
    if node is None:
        async with state.session_factory() as db:
            known = {k.node.node_id: k.node for k in await repos.nodes_status(db)}
        if node_id not in known:
            raise HTTPException(404, "no such node")
        return RedirectResponse(
            f"/live/nodes?error={known[node_id].name or node_id[:8]}+is+not+relaying+right+now",
            status_code=303,
        )
    return templates.TemplateResponse(
        request,
        "live.html",
        {
            "snapshot": node.snapshot,
            "settings": state.settings,
            "relay_node": node.status(),
            "game_host_set": True,
            "desktop": False,
            "show_getting_started": False,
            "game_ever_connected": True,
            "track_ever_set": True,
        },
    )


# ── commands ───────────────────────────────────────────────────


def _node(request: Request, node_id: str) -> RelayNode:
    node: RelayNode | None = request.app.state.relays.get(node_id)
    if node is None:
        raise HTTPException(404, "that node is not relaying right now")
    return node


async def _relay(node: RelayNode, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    try:
        return await node.command(kind, data)
    except RelayError as e:
        raise HTTPException(e.status, e.detail) from e


@router.post("/api/relay/{node_id}/session")
async def relay_session(request: Request, node_id: str, body: SessionIn) -> dict[str, Any]:
    return await _relay(_node(request, node_id), "session", body.model_dump())


@router.post("/api/relay/{node_id}/capture")
async def relay_capture(request: Request, node_id: str, body: CaptureIn) -> dict[str, Any]:
    return await _relay(_node(request, node_id), "capture", body.model_dump())


@router.post("/api/relay/{node_id}/race/abort")
async def relay_abort(
    request: Request, node_id: str, body: AbortIn | None = None
) -> dict[str, Any]:
    return await _relay(_node(request, node_id), "abort", (body or AbortIn()).model_dump())


@router.get("/api/relay")
async def relay_nodes(request: Request) -> dict[str, Any]:
    return {"connected": [n.status() for n in request.app.state.relays.all()]}
