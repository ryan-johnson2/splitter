"""The web end of the relay (#14): the nodes currently mirroring their live
feed here, one ``LiveHub`` each for the browsers watching them, and the
commands sent back down.

``RelayNode.serve`` runs for the life of one node connection (the websocket
route wraps the socket in a channel; tests hand it a queue pair). Every
message from the node is fanned out to the browsers on ``/ws/live/<node>``
and folded into ``snapshot`` so a page opened mid-run renders from the latest
state; a fresh snapshot is asked for on every browser connect anyway.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Protocol

from splitter.live.hub import LiveHub
from splitter.util import utcnow

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 10.0


class Channel(Protocol):
    async def send(self, text: str) -> None: ...
    async def recv(self) -> str: ...


class RelayError(Exception):
    """A command the node refused or never answered; ``status`` is for the route."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class RelayNode:
    def __init__(self, node_id: str, name: str, channel: Channel) -> None:
        self.node_id = node_id
        self.name = name
        self.channel = channel
        self.hub = LiveHub()  # the browsers watching this node
        self.snapshot: dict[str, Any] = {}
        self.connected_at: datetime = utcnow()
        self.last_message_at: datetime | None = None
        self.received = 0
        self._next_id = 1
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}

    @property
    def label(self) -> str:
        return self.name or self.node_id[:8]

    def status(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "label": self.label,
            "connected": True,
            "connected_at": self.connected_at.replace(microsecond=0).isoformat() + "Z",
            "clients": self.hub.clients,
            "received": self.received,
        }

    # ── node → web ─────────────────────────────────────────────────

    async def serve(self) -> None:
        """Read the node's messages until the channel closes."""
        while True:
            text = await self.channel.recv()
            self.received += 1
            self.last_message_at = utcnow()
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.handle(msg)

    def handle(self, msg: dict[str, Any]) -> None:
        kind = str(msg.get("type", ""))
        data = msg.get("data")
        if kind == "reply":
            fut = self._waiting.pop(int(msg.get("id") or 0), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        if kind == "snapshot" and isinstance(data, dict):
            self.snapshot = dict(data)
            self.hub.broadcast("snapshot", self.snapshot)
            return
        self._fold(kind, data if isinstance(data, dict) else {})
        self.hub.broadcast(kind, data if isinstance(data, dict) else {})

    def _fold(self, kind: str, data: dict[str, Any]) -> None:
        """Keep ``snapshot`` roughly current between full snapshots."""
        s = self.snapshot
        if kind == "status":
            s["connection"] = data
        elif kind == "session":
            s["session"] = data
        elif kind == "reference":
            s["reference"] = data
        elif kind == "sync":
            s["sync"] = data
        elif kind == "capture":
            s["capture"] = bool(data.get("enabled", True))
        elif kind == "imu_warning":
            s["imu_missing"] = bool(data.get("missing"))
        elif kind == "armed":
            s["armed"] = True
            s["race"] = None
        elif kind == "race_started":
            s["armed"] = False
            s["race"] = {
                "id": data.get("id"),
                "crossings": [],
                "laps": [],
                "total_ms": 0,
                "lap_start_ms": 0,
                "finished": False,
            }
        elif kind == "crossing" and isinstance(s.get("race"), dict):
            race = s["race"]
            race["crossings"].append(data)
            race["total_ms"] = data.get("cumulative_ms", race.get("total_ms", 0))
            if data.get("lap_done"):
                race["laps"].append(data["lap_done"])
            if data.get("starts_lap"):
                race["lap_start_ms"] = data.get("cumulative_ms", 0)
        elif kind in ("race_finished", "race_aborted"):
            s["race"] = None
            s["armed"] = False
            if kind == "race_finished":
                s["last_result"] = data
        elif kind == "track_check":
            s["track_check"] = data

    # ── web → node ─────────────────────────────────────────────────

    async def command(self, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one command and wait for the node's reply (its result, or a
        ``RelayError`` with the status the node's own route would have given)."""
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[req_id] = fut
        try:
            await self.channel.send(
                json.dumps({"type": kind, "id": req_id, "data": data or {}}, separators=(",", ":"))
            )
            reply = await asyncio.wait_for(fut, timeout=COMMAND_TIMEOUT_S)
        except TimeoutError as e:
            raise RelayError(504, f"{self.label} did not answer in time") from e
        except Exception as e:
            raise RelayError(502, f"could not reach {self.label}: {e}") from e
        finally:
            self._waiting.pop(req_id, None)
        if not reply.get("ok"):
            raise RelayError(int(reply.get("status") or 502), str(reply.get("error") or "refused"))
        result = reply.get("data")
        return dict(result) if isinstance(result, dict) else {}

    def request_snapshot(self) -> None:
        """Ask for a fresh snapshot; it arrives as a normal ``snapshot`` message."""
        asyncio.get_running_loop().create_task(self._send_quiet({"type": "snapshot"}))

    async def _send_quiet(self, msg: dict[str, Any]) -> None:
        try:
            await self.channel.send(json.dumps(msg, separators=(",", ":")))
        except Exception as e:
            log.debug("relay send to %s failed: %s", self.label, e)

    def close(self) -> None:
        """The node went away: tell its browsers, fail anything still waiting."""
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_exception(RelayError(502, f"{self.label} disconnected"))
        self._waiting.clear()
        self.hub.broadcast("node", {"connected": False, "node_id": self.node_id, "name": self.name})


class RelayRegistry:
    """The nodes relaying right now, by node id."""

    def __init__(self, hub: LiveHub) -> None:
        self._nodes: dict[str, RelayNode] = {}
        self._hub = hub  # the web's own hub: pages hear when nodes come and go

    def __len__(self) -> int:
        return len(self._nodes)

    def get(self, node_id: str) -> RelayNode | None:
        return self._nodes.get(node_id)

    def all(self) -> list[RelayNode]:
        return sorted(self._nodes.values(), key=lambda n: n.label.lower())

    def add(self, node: RelayNode) -> RelayNode | None:
        """Register a node; returns the connection it replaced, if any (the
        node reconnected before the web noticed the old socket was gone)."""
        old = self._nodes.pop(node.node_id, None)
        self._nodes[node.node_id] = node
        if old is not None:
            old.close()
        self._hub.broadcast("nodes", {"connected": [n.status() for n in self.all()]})
        log.info("relay: %s connected (%d node%s)", node.label, len(self), "s"[: len(self) != 1])
        return old

    def remove(self, node: RelayNode) -> None:
        if self._nodes.get(node.node_id) is node:
            del self._nodes[node.node_id]
            node.close()
            self._hub.broadcast("nodes", {"connected": [n.status() for n in self.all()]})
            log.info("relay: %s disconnected", node.label)
