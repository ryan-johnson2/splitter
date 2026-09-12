"""Wire-shaped event builders (1.17.13: everything stringified)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from velocidrone_ws import Event

from splitter.live.hub import LiveHub


def ev(kind: str, payload: Any) -> Event:
    return Event.from_json({kind: payload})


def session(track: str = "Practice Loop", quad: str = "Source One", laps: int = 3) -> Event:
    return ev(
        "session",
        {
            "playerName": "Ryan",
            "sessionName": "room",
            "sceneryTitle": "Basketball Stadium",
            "trackName": track,
            "raceLength": str(laps),
            "RaceMode": "THREE_LAP_SINGLE_CLASS",
            "quadType": quad,
            "quadSize": "5",
        },
    )


def status(action: str) -> Event:
    return ev("racestatus", {"raceAction": action})


def racetype(laps: int = 3) -> Event:
    return ev(
        "racetype",
        {"raceMode": "THREE_LAP_SINGLE_CLASS", "raceFormat": "NORMAL", "raceLaps": str(laps)},
    )


def countdown(n: int) -> Event:
    return ev("countdown", {"countValue": str(n)})


def racedata(
    lap: int, gate: int, t: float, finished: bool = False, name: str = "Ryan", **others: Any
) -> Event:
    players = {
        name: {
            "position": "1",
            "lap": str(lap),
            "gate": str(gate),
            "time": f"{t:.3f}",
            "finished": "True" if finished else "False",
            "colour": "00FFFF",
            "uid": 12345,
        }
    }
    for other, (olap, ogate, ot) in others.items():
        players[other] = {
            "position": "2",
            "lap": str(olap),
            "gate": str(ogate),
            "time": f"{ot:.3f}",
            "finished": "False",
            "colour": "FF0000",
            "uid": 67890,
        }
    return ev("racedata", players)


def imu(ts_ms: float, x: float, z: float, vx: float, vz: float) -> Event:
    return ev(
        "imu",
        {
            "roll": 0.0,
            "pitch": 0.0,
            "yaw": 0.0,
            "PositionX": x,
            "PositionY": 1.0,
            "PositionZ": z,
            "AttitudeX": 0.0,
            "AttitudeY": 0.0,
            "AttitudeZ": 0.0,
            "AttitudeW": 1.0,
            "SpeedX": vx,
            "SpeedY": 0.0,
            "SpeedZ": vz,
            "timestamp": ts_ms,
        },
    )


def drain(queue: asyncio.Queue[str]) -> list[dict[str, Any]]:
    out = []
    while not queue.empty():
        out.append(json.loads(queue.get_nowait()))
    return out


def subscribe(hub: LiveHub) -> asyncio.Queue[str]:
    return hub.subscribe()


# A 2-lap, 3-gate race with a distinct start/finish gate, in wire form:
# lap 1: gates 1..3, S/F crossing reported as (2,1); lap 2: gates 2..3 then finish (2,4).
def two_lap_race(base: float = 0.0, scale: float = 1.0) -> list[tuple[int, int, float, bool]]:
    """Two laps on a track with a start-only gate and 3 checkpoints per lap.

    Real wire shape: the start gate is ``lap 0``, the lap counter increments at
    the start/finish gate (ordinal 2), the finish arrives one ordinal past the
    per-lap count with ``finished`` set. Holeshot 2 s, laps 6 s each, total 14 s.
    """
    t = [1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0]
    t = [base + x * scale for x in t]
    return [
        (0, 1, t[0], False),  # start-only gate (holeshot)
        (1, 2, t[1], False),  # first start/finish crossing: lap 1 starts, holeshot ends
        (1, 3, t[2], False),
        (1, 4, t[3], False),
        (2, 2, t[4], False),  # start/finish: closes lap 1, starts lap 2
        (2, 3, t[5], False),
        (2, 4, t[6], False),
        (2, 5, t[7], True),  # finish
    ]
