"""Async WebSocket client for VelociDrone's local WebSocket server.

VelociDrone runs a WebSocket server on ``ws://<lan-ip>:60003/velocidrone/``
for race overlays, timing tools and race control. This client connects to
it and provides typed access to events and commands.

Wire traps handled here (see ``docs/ws-spec.md``):

- The game binds its **LAN** address, not loopback — ``localhost`` only
  works if the game resolves it to the same LAN IP, so pass the machine's
  LAN IP explicitly.
- Every server frame is a *binary* frame carrying UTF-8 JSON; ``events()``
  decodes bytes and text alike.
- The server drops idle connections after 40 s; send ``ping()`` (the
  ``{"command":"ping"}`` no-op) every ~5 s. RFC ping frames are disabled by
  default because a ping with a payload desynchronises the game's parser.
- The server never sends a close frame; expect raw TCP teardown.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from velocidrone_ws.exceptions import CommandError, ConnectionError
from velocidrone_ws.models import Event

DEFAULT_PORT = 60003
DEFAULT_SERVICE = "velocidrone"  # shipped path; "ws" is only the legacy code default


class VelociDroneWS:
    """Async client for VelociDrone's local WebSocket server.

    Args:
        host: The IP address or hostname of the machine running VelociDrone.
            Defaults to ``"localhost"``.
        port: WebSocket server port. Defaults to ``60003``.
        service: WebSocket service path. Defaults to ``"velocidrone"``.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = DEFAULT_PORT,
        service: str = DEFAULT_SERVICE,
        *,
        open_timeout: float = 10,
        ping_interval: float | None = None,
        ping_timeout: float | None = None,
        close_timeout: float | None = 10,
    ) -> None:
        self._host = host
        self._port = port
        self._service = service
        self._open_timeout = open_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._close_timeout = close_timeout
        self._ws: ClientConnection | None = None

    @property
    def url(self) -> str:
        """The WebSocket server URL."""
        return f"ws://{self._host}:{self._port}/{self._service}/"

    @property
    def connected(self) -> bool:
        """Whether the client is currently connected."""
        return self._ws is not None

    async def connect(self) -> None:
        """Connect to the VelociDrone WebSocket server.

        Raises:
            ConnectionError: If the connection fails.
        """
        if self._ws is not None:
            return
        try:
            self._ws = await websockets.connect(
                self.url,
                open_timeout=self._open_timeout,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
                close_timeout=self._close_timeout,
                max_size=None,  # the game enforces no payload cap; neither do we
            )
        except Exception as exc:
            raise ConnectionError(
                f"Failed to connect to {self.url}: {exc}"
            ) from exc

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> VelociDroneWS:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ── Sending Commands ──────────────────────────────────────────────

    async def _send_command(self, command: str, **kwargs: Any) -> None:
        """Send a JSON command to the server.

        Args:
            command: The command name.
            **kwargs: Additional fields to include in the JSON message.

        Raises:
            ConnectionError: If not connected.
            CommandError: If sending fails.
        """
        if self._ws is None:
            raise ConnectionError("Not connected")
        msg: dict[str, Any] = {"command": command, **kwargs}
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as exc:
            raise CommandError(f"Failed to send '{command}': {exc}") from exc

    async def ping(self) -> None:
        """Send a ping command (no-op, keeps connection alive)."""
        await self._send_command("ping")

    async def start_race(self) -> None:
        """Start the race (host only)."""
        await self._send_command("startrace")

    async def abort_race(self) -> None:
        """Abort the current race (host only)."""
        await self._send_command("abortrace")

    async def lock(self) -> None:
        """Lock the room (host only)."""
        await self._send_command("lock")

    async def unlock(self) -> None:
        """Unlock the room (host only)."""
        await self._send_command("unlock")

    async def activate(self, pilot_uids: list[str | int]) -> None:
        """Seat exactly these pilots by UID (host only); everyone else spectates.

        Args:
            pilot_uids: Pilot UIDs, as numbers or numeric strings.
        """
        await self._send_command("activate", pilots=pilot_uids)

    async def get_pilots(self) -> None:
        """Request the pilot list (host only).

        The server responds with a ``pilotlist`` event.
        """
        await self._send_command("getpilots")

    async def camera_player(self, uid: str) -> None:
        """Switch camera to a specific player (multiplayer only).

        Args:
            uid: The player's UID.
        """
        await self._send_command("cameraplayer", uid=uid)

    async def camera_mode(self, mode: str) -> None:
        """Set the camera mode (multiplayer only).

        Args:
            mode: ``"fpv"`` or ``"spectate"``.
        """
        await self._send_command("cameramode", mode=mode)

    async def camera_select(self, number: int) -> None:
        """Select a numbered camera (multiplayer only).

        Args:
            number: Camera number to select.
        """
        await self._send_command("cameraselect", number=number)

    async def camera_reset(self) -> None:
        """Reset the camera view (multiplayer only)."""
        await self._send_command("camerareset")

    async def all_spectate(self) -> None:
        """Force all players into spectator mode (host only)."""
        await self._send_command("allspectate")

    # ── Receiving Events ──────────────────────────────────────────────

    async def events(self) -> AsyncIterator[Event]:
        """Async iterator that yields parsed events from the server.

        Yields:
            Parsed ``Event`` objects with typed ``data`` fields.

        Raises:
            ConnectionError: If not connected.
        """
        if self._ws is None:
            raise ConnectionError("Not connected")
        try:
            async for raw_msg in self._ws:
                if isinstance(raw_msg, bytes):
                    raw_msg = raw_msg.decode("utf-8")
                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    yield Event.from_json(data)
        except websockets.exceptions.ConnectionClosed:
            self._ws = None

    async def recv_event(self) -> Event | None:
        """Receive a single event from the server.

        Returns:
            A parsed ``Event``, or ``None`` if the connection is closed.

        Raises:
            ConnectionError: If not connected.
        """
        if self._ws is None:
            raise ConnectionError("Not connected")
        try:
            raw_msg = await self._ws.recv()
            if isinstance(raw_msg, bytes):
                raw_msg = raw_msg.decode("utf-8")
            data = json.loads(raw_msg)
            if isinstance(data, dict):
                return Event.from_json(data)
            return None
        except websockets.exceptions.ConnectionClosed:
            self._ws = None
            return None
        except json.JSONDecodeError:
            return None
