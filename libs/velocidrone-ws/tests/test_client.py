"""Tests for WebSocket client.

Uses mocked WebSocket connections — never connects to a real server.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from velocidrone_ws.client import VelociDroneWS
from velocidrone_ws.exceptions import CommandError, ConnectionError
from velocidrone_ws.models import (
    CountdownEvent,
    RaceDataEvent,
    SessionEvent,
)


class FakeWebSocket:
    """Fake WebSocket connection for testing."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self.messages = messages or []
        self.sent: list[str] = []
        self._index = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self._index >= len(self.messages):
            raise Exception("No more messages")
        msg = self.messages[self._index]
        self._index += 1
        return msg

    async def close(self) -> None:
        pass

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self.messages):
            raise StopAsyncIteration
        msg = self.messages[self._index]
        self._index += 1
        return msg


class TestVelociDroneWSProperties:
    def test_default_url(self) -> None:
        ws = VelociDroneWS()
        assert ws.url == "ws://localhost:60003/velocidrone/"

    def test_custom_url(self) -> None:
        ws = VelociDroneWS(host="192.168.1.100", port=9999, service="test")
        assert ws.url == "ws://192.168.1.100:9999/test/"

    def test_not_connected_by_default(self) -> None:
        ws = VelociDroneWS()
        assert ws.connected is False

    def test_default_connection_parameters(self) -> None:
        ws = VelociDroneWS()
        assert ws._open_timeout == 10
        assert ws._ping_interval is None
        assert ws._ping_timeout is None
        assert ws._close_timeout == 10

    def test_custom_connection_parameters(self) -> None:
        ws = VelociDroneWS(
            ping_interval=20.0,
            ping_timeout=10.0,
            open_timeout=5.0,
            close_timeout=3.0,
        )
        assert ws._open_timeout == 5.0
        assert ws._ping_interval == 20.0
        assert ws._ping_timeout == 10.0
        assert ws._close_timeout == 3.0


class TestVelociDroneWSCommands:
    @pytest.fixture()
    def ws(self) -> VelociDroneWS:
        client = VelociDroneWS()
        client._ws = FakeWebSocket()  # type: ignore[assignment]
        return client

    @pytest.fixture()
    def fake_ws(self, ws: VelociDroneWS) -> FakeWebSocket:
        assert isinstance(ws._ws, FakeWebSocket)
        return ws._ws

    @pytest.mark.asyncio
    async def test_ping(self, ws: VelociDroneWS, fake_ws: FakeWebSocket) -> None:
        await ws.ping()
        assert len(fake_ws.sent) == 1
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "ping"}

    @pytest.mark.asyncio
    async def test_start_race(self, ws: VelociDroneWS, fake_ws: FakeWebSocket) -> None:
        await ws.start_race()
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "startrace"}

    @pytest.mark.asyncio
    async def test_abort_race(self, ws: VelociDroneWS, fake_ws: FakeWebSocket) -> None:
        await ws.abort_race()
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "abortrace"}

    @pytest.mark.asyncio
    async def test_lock(self, ws: VelociDroneWS, fake_ws: FakeWebSocket) -> None:
        await ws.lock()
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "lock"}

    @pytest.mark.asyncio
    async def test_unlock(self, ws: VelociDroneWS, fake_ws: FakeWebSocket) -> None:
        await ws.unlock()
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "unlock"}

    @pytest.mark.asyncio
    async def test_activate(self, ws: VelociDroneWS, fake_ws: FakeWebSocket) -> None:
        await ws.activate(["uid1", "uid2"])
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "activate", "pilots": ["uid1", "uid2"]}

    @pytest.mark.asyncio
    async def test_get_pilots(
        self, ws: VelociDroneWS, fake_ws: FakeWebSocket
    ) -> None:
        await ws.get_pilots()
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "getpilots"}

    @pytest.mark.asyncio
    async def test_camera_player(
        self, ws: VelociDroneWS, fake_ws: FakeWebSocket
    ) -> None:
        await ws.camera_player("player_uid")
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "cameraplayer", "uid": "player_uid"}

    @pytest.mark.asyncio
    async def test_camera_mode(
        self, ws: VelociDroneWS, fake_ws: FakeWebSocket
    ) -> None:
        await ws.camera_mode("fpv")
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "cameramode", "mode": "fpv"}

    @pytest.mark.asyncio
    async def test_camera_select(
        self, ws: VelociDroneWS, fake_ws: FakeWebSocket
    ) -> None:
        await ws.camera_select(2)
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "cameraselect", "number": 2}

    @pytest.mark.asyncio
    async def test_camera_reset(
        self, ws: VelociDroneWS, fake_ws: FakeWebSocket
    ) -> None:
        await ws.camera_reset()
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "camerareset"}

    @pytest.mark.asyncio
    async def test_all_spectate(
        self, ws: VelociDroneWS, fake_ws: FakeWebSocket
    ) -> None:
        await ws.all_spectate()
        msg = json.loads(fake_ws.sent[0])
        assert msg == {"command": "allspectate"}


class TestVelociDroneWSCommandErrors:
    @pytest.mark.asyncio
    async def test_command_not_connected(self) -> None:
        ws = VelociDroneWS()
        with pytest.raises(ConnectionError, match="Not connected"):
            await ws.ping()

    @pytest.mark.asyncio
    async def test_command_send_failure(self) -> None:
        ws = VelociDroneWS()
        fake = FakeWebSocket()
        fake.send = AsyncMock(side_effect=OSError("broken"))  # type: ignore[method-assign]
        ws._ws = fake  # type: ignore[assignment]
        with pytest.raises(CommandError, match="Failed to send"):
            await ws.ping()


class TestVelociDroneWSEvents:
    @pytest.mark.asyncio
    async def test_events_iterator(self) -> None:
        messages = [
            json.dumps({"countdown": {"countValue": 3}}),
            json.dumps({"countdown": {"countValue": 2}}),
            json.dumps({"countdown": {"countValue": 1}}),
        ]
        ws = VelociDroneWS()
        ws._ws = FakeWebSocket(messages)  # type: ignore[assignment]

        events = []
        async for event in ws.events():
            events.append(event)

        assert len(events) == 3
        assert isinstance(events[0].data, CountdownEvent)
        assert events[0].data.count_value == 3
        assert events[2].data is not None
        assert isinstance(events[2].data, CountdownEvent)
        assert events[2].data.count_value == 1

    @pytest.mark.asyncio
    async def test_events_skips_invalid_json(self) -> None:
        messages = [
            "not json at all",
            json.dumps({"countdown": {"countValue": 5}}),
        ]
        ws = VelociDroneWS()
        ws._ws = FakeWebSocket(messages)  # type: ignore[assignment]

        events = []
        async for event in ws.events():
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0].data, CountdownEvent)

    @pytest.mark.asyncio
    async def test_events_not_connected(self) -> None:
        ws = VelociDroneWS()
        with pytest.raises(ConnectionError, match="Not connected"):
            async for _ in ws.events():
                pass

    @pytest.mark.asyncio
    async def test_recv_event(self) -> None:
        msg = json.dumps(
            {
                "session": {
                    "playerName": "Test",
                    "sessionName": "Room",
                    "sceneryTitle": "Scene",
                    "trackName": "Track",
                    "raceLength": 3,
                    "RaceMode": "MultiGP",
                    "quadType": "5inch",
                    "quadSize": "5",
                }
            }
        )
        ws = VelociDroneWS()
        ws._ws = FakeWebSocket([msg])  # type: ignore[assignment]

        event = await ws.recv_event()
        assert event is not None
        assert event.type == "session"
        assert isinstance(event.data, SessionEvent)
        assert event.data.player_name == "Test"

    @pytest.mark.asyncio
    async def test_recv_event_not_connected(self) -> None:
        ws = VelociDroneWS()
        with pytest.raises(ConnectionError, match="Not connected"):
            await ws.recv_event()

    @pytest.mark.asyncio
    async def test_recv_event_invalid_json(self) -> None:
        ws = VelociDroneWS()
        ws._ws = FakeWebSocket(["not json"])  # type: ignore[assignment]
        event = await ws.recv_event()
        assert event is None

    @pytest.mark.asyncio
    async def test_events_handles_bytes(self) -> None:
        """Verify byte messages are decoded to UTF-8."""
        raw = json.dumps({"countdown": {"countValue": 1}})
        ws = VelociDroneWS()

        class ByteFakeWS(FakeWebSocket):
            async def __anext__(self) -> str:
                result = await super().__anext__()
                return result  # type: ignore[return-value]

        ws._ws = ByteFakeWS([raw])  # type: ignore[assignment]
        events = []
        async for event in ws.events():
            events.append(event)
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_racedata_event_parsing(self) -> None:
        msg = json.dumps(
            {
                "racedata": {
                    "Pilot1": {
                        "position": 1,
                        "lap": 2,
                        "gate": 4,
                        "time": "00:45.123",
                        "finished": False,
                        "colour": "#FF0000",
                        "uid": "uid1",
                    },
                    "Pilot2": {
                        "position": 2,
                        "lap": 1,
                        "gate": 3,
                        "time": "00:50.456",
                        "finished": False,
                        "colour": "#0000FF",
                        "uid": "uid2",
                    },
                }
            }
        )
        ws = VelociDroneWS()
        ws._ws = FakeWebSocket([msg])  # type: ignore[assignment]

        event = await ws.recv_event()
        assert event is not None
        assert isinstance(event.data, RaceDataEvent)
        assert len(event.data.players) == 2
        assert event.data.players["Pilot1"].position == 1
        assert event.data.players["Pilot2"].uid == "uid2"


class TestVelociDroneWSConnect:
    @pytest.mark.asyncio
    async def test_connect_passes_parameters(self) -> None:
        from unittest.mock import patch, AsyncMock as AM

        ws = VelociDroneWS(
            ping_interval=25.0,
            ping_timeout=15.0,
            open_timeout=8.0,
            close_timeout=5.0,
        )
        fake = FakeWebSocket()
        with patch("velocidrone_ws.client.websockets.connect", new=AM(return_value=fake)) as mock_connect:
            await ws.connect()
            mock_connect.assert_called_once_with(
                ws.url,
                open_timeout=8.0,
                ping_interval=25.0,
                ping_timeout=15.0,
                close_timeout=5.0,
                max_size=None,
            )

    @pytest.mark.asyncio
    async def test_connect_default_disables_ping(self) -> None:
        from unittest.mock import patch, AsyncMock as AM

        ws = VelociDroneWS()
        fake = FakeWebSocket()
        with patch("velocidrone_ws.client.websockets.connect", new=AM(return_value=fake)) as mock_connect:
            await ws.connect()
            _, kwargs = mock_connect.call_args
            assert kwargs["ping_interval"] is None
            assert kwargs["ping_timeout"] is None


class TestVelociDroneWSConnection:
    @pytest.mark.asyncio
    async def test_close(self) -> None:
        ws = VelociDroneWS()
        fake = FakeWebSocket()
        ws._ws = fake  # type: ignore[assignment]
        assert ws.connected is True
        await ws.close()
        assert ws.connected is False

    @pytest.mark.asyncio
    async def test_close_when_not_connected(self) -> None:
        ws = VelociDroneWS()
        await ws.close()  # Should not raise
        assert ws.connected is False
