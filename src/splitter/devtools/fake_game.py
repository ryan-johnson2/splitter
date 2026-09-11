"""A scripted stand-in for VelociDrone's websocket server.

Speaks the 1.17.13 wire shapes (one-key objects, stringified scalars,
``"True"``/``"False"``, uid as a number, imu as numbers, binary frames) so
the whole pipeline can be exercised without the game:

    splitter fake-game --port 60003 --loop
    # then point Settings → Game PC at 127.0.0.1

The race script: session (unless --no-session) → racestatus start →
racetype → countdown → FinishGate → gate crossings with jitter → finish.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve

log = logging.getLogger(__name__)

TRACKS = [
    ("Basketball Stadium", "Practice Loop"),
    ("Football Stadium", "Grand Prix"),
]
QUADS = [("TBS Source One", "5"), ("Five33 Switchback", "5")]


class FakeGame:
    def __init__(
        self,
        laps: int = 3,
        gates: int = 8,
        speed: float = 1.0,
        imu: bool = True,
        session: bool = True,
        player: str = "Pilot",
        seed: int | None = None,
    ) -> None:
        self.laps = laps
        self.gates = gates
        self.speed = speed
        self.imu = imu
        self.session = session
        self.player = player
        self.rng = random.Random(seed)
        self.conn: ServerConnection | None = None
        self.commands: list[dict[str, Any]] = []
        self.game_ms = 0.0  # fake Unity Time.time * 1000
        self.race_no = 0

    async def send(self, obj: dict[str, Any]) -> None:
        if self.conn is None:
            return
        try:
            await self.conn.send(json.dumps(obj, separators=(",", ":")).encode())
        except websockets.ConnectionClosed:
            self.conn = None

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)
        self.game_ms += seconds * 1000

    async def handler(self, conn: ServerConnection) -> None:
        self.conn = conn  # newest connection wins, like the real thing
        log.info("client connected: %s", conn.remote_address)
        try:
            async for raw in conn:
                text = raw.decode() if isinstance(raw, bytes) else raw
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "command" in msg:
                    self.commands.append(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.conn is conn:
                self.conn = None

    async def run_race(self) -> None:
        self.race_no += 1
        scenery, track = TRACKS[(self.race_no - 1) % len(TRACKS)]
        quad, size = QUADS[(self.race_no - 1) % len(QUADS)]
        if self.session:
            await self.send(
                {
                    "session": {
                        "playerName": self.player,
                        "sessionName": "practice",
                        "sceneryTitle": scenery,
                        "trackName": track,
                        "raceLength": str(self.laps),
                        "RaceMode": "THREE_LAP_SINGLE_CLASS",
                        "quadType": quad,
                        "quadSize": size,
                    }
                }
            )
            await self.sleep(0.5)
        await self.send({"racestatus": {"raceAction": "start"}})
        await self.send(
            {
                "racetype": {
                    "raceMode": "THREE_LAP_SINGLE_CLASS",
                    "raceFormat": "NORMAL",
                    "raceLaps": str(self.laps),
                }
            }
        )
        for n in (3, 2, 1):
            await self.send({"countdown": {"countValue": str(n)}})
            await self.sleep(1.0)
        await self.send({"countdown": {"countValue": "0"}})
        await self.send({"FinishGate": {"StartFinishGate": "True"}})

        race_ms = 0.0
        imu_task = asyncio.create_task(self._imu_loop()) if self.imu else None
        try:
            await self._racedata(1, 1, 0.0, False)  # start-line snapshot at t=0
            for lap in range(1, self.laps + 1):
                for gate in range(1, self.gates + 1):
                    seg = self.rng.uniform(1.6, 2.6)
                    await self.sleep(seg)
                    race_ms += seg * 1000
                    is_last = lap == self.laps and gate == self.gates
                    if gate == self.gates and not is_last:
                        await self._racedata(lap + 1, 1, race_ms / 1000, False)
                    elif is_last:
                        await self._racedata(lap, gate + 1, race_ms / 1000, True)
                    else:
                        await self._racedata(lap, gate + 1, race_ms / 1000, False)
        finally:
            if imu_task:
                imu_task.cancel()
                await asyncio.gather(imu_task, return_exceptions=True)
        await self.sleep(0.3)
        await self.send({"racestatus": {"raceAction": "race finished"}})
        log.info("fake race %d done: %s / %s in %.3fs", self.race_no, track, quad, race_ms / 1000)

    async def _racedata(self, lap: int, gate: int, t: float, finished: bool) -> None:
        await self.send(
            {
                "racedata": {
                    self.player: {
                        "position": "1",
                        "lap": str(lap),
                        "gate": str(gate),
                        "time": f"{t:.3f}",
                        "finished": "True" if finished else "False",
                        "colour": "00FFFF",
                        "uid": 12345,
                    }
                }
            }
        )

    async def _imu_loop(self) -> None:
        t = 0.0
        while True:
            # Fly a lazy circle at ~25 m/s so the path looks like a track.
            angle = t * 0.8
            r = 30.0
            x, z = r * math.cos(angle), r * math.sin(angle)
            vx, vz = -r * 0.8 * math.sin(angle), r * 0.8 * math.cos(angle)
            await self.send(
                {
                    "imu": {
                        "roll": self.rng.uniform(-200, 200),
                        "pitch": self.rng.uniform(-100, 100),
                        "yaw": self.rng.uniform(-50, 50),
                        "PositionX": x,
                        "PositionY": 2.0 + math.sin(t * 2) * 0.5,
                        "PositionZ": z,
                        "AttitudeX": 0.0,
                        "AttitudeY": math.sin(angle / 2),
                        "AttitudeZ": 0.0,
                        "AttitudeW": math.cos(angle / 2),
                        "SpeedX": vx,
                        "SpeedY": 0.0,
                        "SpeedZ": vz,
                        "timestamp": self.game_ms + (t * 1000) % 1,
                    }
                }
            )
            await asyncio.sleep(1 / 60 / self.speed)
            t += 1 / 60
            self.game_ms += 1000 / 60


async def run_fake_game(
    host: str = "127.0.0.1",
    port: int = 60003,
    laps: int = 3,
    gates: int = 8,
    speed: float = 1.0,
    loop: bool = False,
    imu: bool = True,
    session: bool = True,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    game = FakeGame(laps=laps, gates=gates, speed=speed, imu=imu, session=session)
    async with serve(game.handler, host, port):
        log.info("fake VelociDrone listening on ws://%s:%d/velocidrone/", host, port)
        while True:
            while game.conn is None:
                await asyncio.sleep(0.2)
            await asyncio.sleep(2 / speed)
            await game.run_race()
            if not loop:
                await asyncio.sleep(1)
                return
            await asyncio.sleep(5 / speed)
