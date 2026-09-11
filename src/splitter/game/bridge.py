"""Connection to the game's websocket: connect, keep alive, reconnect, dispatch.

One asyncio task, supervised by the app. ``configure()`` changes the target
and forces a reconnect; ``disconnect()`` parks it until the next
``configure``/``reconnect``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum

from velocidrone_ws import Event, VelociDroneWS

from splitter.util import utcnow

log = logging.getLogger(__name__)

PING_INTERVAL = 5.0  # the game drops idle connections after 40 s
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0
SLOW_AFTER_ATTEMPTS = 30  # then retry once a minute instead of hammering
SLOW_BACKOFF = 60.0


class BridgeState(StrEnum):
    IDLE = "idle"  # nothing configured / disconnected on purpose
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


EventHandler = Callable[[Event], Awaitable[None]]
StateHandler = Callable[["GameBridge"], Awaitable[None]]


class GameBridge:
    def __init__(self, on_event: EventHandler, on_state: StateHandler | None = None) -> None:
        self._on_event = on_event
        self._on_state = on_state
        self.host = ""
        self.port = 60003
        self.state = BridgeState.IDLE
        self.attempts = 0
        self.last_error = ""
        self.connected_since: datetime | None = None
        self.last_event_at: datetime | None = None
        self.events_received = 0
        self._enabled = False
        self._wake = asyncio.Event()
        self._client: VelociDroneWS | None = None
        self._generation = 0

    # ── control ──────────────────────────────────────────────────────

    def configure(self, host: str, port: int, enabled: bool = True) -> None:
        """Point at a (new) game address; drops any current connection."""
        self.host = host.strip()
        self.port = port
        self._enabled = enabled and bool(self.host)
        self.attempts = 0
        self._bump()

    def reconnect(self) -> None:
        self._enabled = bool(self.host)
        self.attempts = 0
        self._bump()

    def disconnect(self) -> None:
        self._enabled = False
        self._bump()

    def _bump(self) -> None:
        self._generation += 1
        self._wake.set()

    @property
    def connected(self) -> bool:
        return self.state == BridgeState.CONNECTED

    @property
    def enabled(self) -> bool:
        return self._enabled

    def status(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "connected_since": self.connected_since.isoformat() + "Z"
            if self.connected_since
            else None,
            "last_event_at": self.last_event_at.isoformat() + "Z" if self.last_event_at else None,
            "events_received": self.events_received,
        }

    async def _set_state(self, state: BridgeState) -> None:
        if state == self.state:
            return
        self.state = state
        if self._on_state is not None:
            await self._on_state(self)

    # ── main loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run forever; the supervisor restarts us if something escapes."""
        while True:
            if not self._enabled:
                await self._set_state(BridgeState.IDLE)
                await self._wait_for_wake()
                continue
            generation = self._generation
            await self._set_state(
                BridgeState.RECONNECTING if self.attempts else BridgeState.CONNECTING
            )
            client = VelociDroneWS(
                host=self.host, port=self.port, open_timeout=5, ping_interval=None
            )
            try:
                await client.connect()
            except Exception as exc:
                self.attempts += 1
                self.last_error = str(exc)
                log.info(
                    "game connect failed (attempt %d) to %s:%d: %s",
                    self.attempts,
                    self.host,
                    self.port,
                    exc,
                )
                await self._set_state(BridgeState.RECONNECTING)
                await self._sleep_or_wake(self._backoff())
                continue

            self._client = client
            self.attempts = 0
            self.last_error = ""
            self.connected_since = utcnow()
            await self._set_state(BridgeState.CONNECTED)
            log.info("connected to game at %s", client.url)
            try:
                await self._serve(client, generation)
            finally:
                self._client = None
                self.connected_since = None
                with contextlib.suppress(Exception):
                    await client.close()
            if self._generation == generation and self._enabled:
                # Connection dropped on its own — go straight back in.
                self.attempts += 1
                await self._set_state(BridgeState.RECONNECTING)
                await self._sleep_or_wake(INITIAL_BACKOFF)

    async def _serve(self, client: VelociDroneWS, generation: int) -> None:
        reader = asyncio.create_task(self._read(client), name="game-reader")
        pinger = asyncio.create_task(self._ping(client), name="game-pinger")
        waker = asyncio.create_task(self._wait_for_wake(), name="game-waker")
        try:
            done, _ = await asyncio.wait(
                [reader, pinger, waker], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not waker and task.exception() is not None:
                    self.last_error = str(task.exception())
                    log.warning("game connection error: %s", task.exception())
            if reader in done and reader.exception() is None and self._generation == generation:
                self.last_error = "connection closed by game"
        finally:
            for task in (reader, pinger, waker):
                task.cancel()
            await asyncio.gather(reader, pinger, waker, return_exceptions=True)

    async def _read(self, client: VelociDroneWS) -> None:
        async for event in client.events():
            self.events_received += 1
            self.last_event_at = utcnow()
            try:
                await self._on_event(event)
            except Exception:
                log.exception("event handler failed for %s", event.type)

    async def _ping(self, client: VelociDroneWS) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await client.ping()

    def _backoff(self) -> float:
        if self.attempts >= SLOW_AFTER_ATTEMPTS:
            return SLOW_BACKOFF
        return float(min(INITIAL_BACKOFF * (2 ** max(self.attempts - 1, 0)), MAX_BACKOFF))

    async def _wait_for_wake(self) -> None:
        await self._wake.wait()
        self._wake.clear()

    async def _sleep_or_wake(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wait_for_wake(), timeout=seconds)
