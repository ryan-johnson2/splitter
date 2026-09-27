"""The node end of the relay (#14): mirror the live feed to the web, take its
commands.

Opt-in (``relay_enabled``), and never needed for a run to record. One
supervised task next to the uploader: with the switch on and an upstream
configured it holds an outbound websocket to ``<upstream>/ws/relay`` with the
ingest token, sends a snapshot on connect, then every message the local
``LiveHub`` fans out (one more subscriber). Down the same socket the web
sends the commands its Live page allows — ``session``, ``capture``,
``abort``, ``snapshot`` — each answered with a ``reply`` carrying the same
``id``. Reconnects with backoff like the game bridge; a 44xx close from the
web (bad token, receiving off) is remembered as ``refused`` and retried
slowly.

The transport is injectable (``connect_factory``) so tests join a node and a
web in one process without sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

import truststore
import websockets
from websockets.asyncio.client import connect as ws_connect

from splitter.db.runtime_settings import RuntimeSettings
from splitter.live.hub import LiveHub
from splitter.sync import commands
from splitter.util import utcnow
from splitter.version import __version__

if TYPE_CHECKING:
    from splitter.game.bridge import GameBridge
    from splitter.game.controller import RaceController
    from splitter.sync.uploader import Uploader

log = logging.getLogger(__name__)

INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 60.0
REFUSED_BACKOFF = 120.0  # the web said no (token, receiving): nothing to hurry
PING_INTERVAL = 20.0
STATES = ("off", "connecting", "connected", "reconnecting", "refused")


class Channel(Protocol):
    """What the relay needs from a connection: text in, text out, close."""

    async def send(self, text: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


class ChannelClosed(Exception):
    """The far end closed; ``code`` is the websocket close code when known."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        super().__init__(reason or f"closed ({code})")
        self.code = code
        self.reason = reason


ConnectFactory = Callable[[str, dict[str, str]], Awaitable[Channel]]


class _WsChannel:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def send(self, text: str) -> None:
        try:
            await self._conn.send(text)
        except websockets.ConnectionClosed as e:
            raise ChannelClosed(_close_code(e), _close_reason(e)) from e

    async def recv(self) -> str:
        try:
            msg = await self._conn.recv()
        except websockets.ConnectionClosed as e:
            raise ChannelClosed(_close_code(e), _close_reason(e)) from e
        return msg if isinstance(msg, str) else msg.decode()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._conn.close()


def _close_code(e: websockets.ConnectionClosed) -> int | None:
    return e.rcvd.code if e.rcvd is not None else None


def _close_reason(e: websockets.ConnectionClosed) -> str:
    return e.rcvd.reason if e.rcvd is not None else ""


async def _default_connect(url: str, headers: dict[str, str]) -> Channel:
    ctx = None
    if url.startswith("wss://"):
        # Same trust as the uploader: the machine's own store, not certifi's bundle.
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    conn = await ws_connect(
        url,
        additional_headers=headers,
        ssl=ctx,
        open_timeout=10,
        ping_interval=PING_INTERVAL,
        max_size=4 * 1024 * 1024,
    )
    return _WsChannel(conn)


def relay_url(upstream_url: str) -> str:
    """``https://web`` → ``wss://web/ws/relay`` (``http`` → ``ws``)."""
    base = upstream_url.strip().rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/ws/relay"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/ws/relay"
    return base + "/ws/relay"


class Relay:
    def __init__(
        self,
        settings: RuntimeSettings,
        session_factory: Any,
        hub: LiveHub,
        uploader: Uploader,
        connect_factory: ConnectFactory | None = None,
    ) -> None:
        self._settings = settings
        self._sf = session_factory
        self._hub = hub
        self._uploader = uploader
        self._connect = connect_factory or _default_connect
        self.controller: RaceController | None = None
        self.bridge: GameBridge | None = None
        self.state = "off"
        self.attempts = 0
        self.last_error = ""
        self.connected_since: datetime | None = None
        self.sent = 0
        self.received = 0
        self._wake = asyncio.Event()
        self._generation = 0
        self._channel: Channel | None = None

    # ── configuration / status ─────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """On, with somewhere to relay to. A web never relays (it has no live feed)."""
        return (
            self._settings.get_bool("relay_enabled")
            and not self._settings.get_bool("web_mode")
            and self._uploader.configured
            and not self._uploader.token_blocked
        )

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "switch": self._settings.get_bool("relay_enabled"),
            "state": self.state,
            "url": relay_url(self._uploader.url) if self._uploader.url else "",
            "attempts": self.attempts,
            "last_error": self.last_error,
            "connected_since": (
                self.connected_since.replace(microsecond=0).isoformat() + "Z"
                if self.connected_since
                else None
            ),
            "sent": self.sent,
            "received": self.received,
        }

    def reconfigure(self) -> None:
        """Settings changed: drop the connection and start over (or stop)."""
        self.attempts = 0
        self.last_error = ""
        self._generation += 1
        self._wake.set()

    def _headers(self) -> dict[str, str]:
        name = self._settings.get("node_name").strip() or socket.gethostname()
        return {
            "Authorization": f"Bearer {self._settings.get('upstream_token').strip()}",
            "X-Splitter-Node": name[:80],
            "X-Splitter-Node-Id": self._settings.get("node_id"),
            "User-Agent": f"splitter/{__version__}",
        }

    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._hub.broadcast("relay", self.status())

    # ── the loop ───────────────────────────────────────────────────

    async def run(self) -> None:
        while True:
            if not self.enabled:
                self._set_state("off")
                await self._wait_for_wake()
                continue
            generation = self._generation
            self._set_state("reconnecting" if self.attempts else "connecting")
            url = relay_url(self._uploader.url)
            try:
                channel = await self._connect(url, self._headers())
            except Exception as e:
                self.attempts += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.info("relay connect to %s failed (attempt %d): %s", url, self.attempts, e)
                self._set_state("reconnecting")
                await self._sleep_or_wake(self._backoff())
                continue
            self._channel = channel
            self.attempts = 0
            self.last_error = ""
            self.connected_since = utcnow()
            self._set_state("connected")
            log.info("relay connected to %s", url)
            refused = False
            try:
                await self._serve(channel, generation)
            except ChannelClosed as e:
                self.last_error = e.reason or f"closed ({e.code})"
                refused = e.code is not None and 4400 <= e.code < 4500
                log.info("relay closed by the web: %s", self.last_error)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("relay connection error: %s", self.last_error)
            finally:
                self._channel = None
                self.connected_since = None
                await channel.close()
            if self._generation != generation:
                continue  # settings changed: straight back round
            if refused:
                # Bad token or receiving off: the uploader's indicator says why.
                self._set_state("refused")
                await self._sleep_or_wake(REFUSED_BACKOFF)
            else:
                self.attempts += 1
                self._set_state("reconnecting")
                await self._sleep_or_wake(INITIAL_BACKOFF)

    async def _serve(self, channel: Channel, generation: int) -> None:
        if self.controller is None:
            raise RuntimeError("relay has no controller")
        await channel.send(_dumps({"type": "snapshot", "data": self.controller.snapshot()}))
        self.sent += 1
        queue = self._hub.subscribe()
        pump = asyncio.create_task(self._pump(channel, queue), name="relay-pump")
        listen = asyncio.create_task(self._listen(channel), name="relay-listen")
        waker = asyncio.create_task(self._wait_for_wake(), name="relay-waker")
        try:
            done, _ = await asyncio.wait([pump, listen, waker], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not waker and task.exception() is not None:
                    raise task.exception()  # type: ignore[misc]
            if listen in done and listen.exception() is None:
                raise ChannelClosed(None, "connection closed by the web")
        finally:
            self._hub.unsubscribe(queue)
            for task in (pump, listen, waker):
                task.cancel()
            await asyncio.gather(pump, listen, waker, return_exceptions=True)

    async def _pump(self, channel: Channel, queue: asyncio.Queue[str]) -> None:
        while True:
            await channel.send(await queue.get())
            self.sent += 1

    async def _listen(self, channel: Channel) -> None:
        while True:
            text = await channel.recv()
            self.received += 1
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            await self._handle(channel, msg)

    async def _handle(self, channel: Channel, msg: dict[str, Any]) -> None:
        kind = str(msg.get("type", ""))
        req_id = msg.get("id")
        raw = msg.get("data")
        data: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        assert self.controller is not None
        if kind == "snapshot":
            await channel.send(
                _dumps({"type": "snapshot", "id": req_id, "data": self.controller.snapshot()})
            )
            self.sent += 1
            return
        if kind not in commands.COMMANDS:
            return
        if self.bridge is None:
            reply: dict[str, Any] = {"ok": False, "status": 503, "error": "no game bridge"}
        else:
            try:
                result = await commands.run(kind, data, self.controller, self.bridge, self._sf)
                reply = {"ok": True, "data": result}
            except commands.CommandError as e:
                reply = {"ok": False, "status": e.status, "error": e.detail}
            except Exception as e:  # a bug must not take the relay down
                log.exception("relayed %s command failed", kind)
                reply = {"ok": False, "status": 500, "error": f"{type(e).__name__}: {e}"}
        log.info("relayed %s command: %s", kind, "ok" if reply["ok"] else reply["error"])
        await channel.send(_dumps({"type": "reply", "id": req_id, "command": kind} | reply))
        self.sent += 1

    def _backoff(self) -> float:
        return float(min(INITIAL_BACKOFF * (2 ** max(self.attempts - 1, 0)), MAX_BACKOFF))

    async def _wait_for_wake(self) -> None:
        await self._wake.wait()
        self._wake.clear()

    async def _sleep_or_wake(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wait_for_wake(), timeout=seconds)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
