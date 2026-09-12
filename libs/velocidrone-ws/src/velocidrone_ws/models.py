"""Typed models for VelociDrone WebSocket protocol messages.

VelociDrone runs a local WebSocket server on port 60003. Events are sent
from the game to connected clients as one-key JSON objects
(``{"<event>": <payload>}``); commands go the other way as JSON with a
``command`` field.

Wire facts that shape these parsers (see ``docs/ws-spec.md``, 1.17.13):

- Every race-event scalar is a **string** (``"3"``, ``"69.711"``,
  ``"True"``), except ``racedata.<player>.uid`` (a JSON number) and the
  whole ``imu`` payload (all numbers). Parsers therefore accept either.
- Booleans arrive capitalised (``"True"`` / ``"False"``).
- ``spectatorChange`` is the one event whose payload is a bare string.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


def _parse_bool(val: Any) -> bool:
    """Parse a boolean that may arrive as ``"True"``/``"False"`` or a real bool."""
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() == "true"


def _parse_int(val: Any, default: int = 0) -> int:
    """Parse an int that may arrive as a string, a number, or be missing."""
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _parse_float(val: Any, default: float = 0.0) -> float:
    """Parse a float that may arrive as a string (``"69.711"``) or a number.

    Tolerates a comma decimal separator: the game formats numbers with the
    current culture, so IMU floats can arrive as ``1,5`` on some locales.
    """
    if val is None or val == "":
        return default
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(",", "."))
    except ValueError:
        return default


# ── Enums ─────────────────────────────────────────────────────────────


class RaceMode(IntEnum):
    """Race mode ids as used by the game's online API (not the websocket)."""

    SINGLE_LAP = 3
    THREE_LAP = 6
    TEN_LAP = 9
    TIME_TRIAL = 10
    COMBAT = 19
    TEAM_RACE = 20


class CameraMode(str):
    """Camera mode values for the ``cameramode`` command."""

    FPV = "fpv"
    SPECTATE = "spectate"


# ── Server → Client Events ───────────────────────────────────────────


@dataclass
class SessionEvent:
    """Room created by the local player (multiplayer only).

    Fires only when *this* machine creates a multiplayer room — never when
    joining someone else's room and never in single player. It is the only
    event that names the track, scenery and quad.
    """

    player_name: str
    session_name: str
    scenery_title: str
    track_name: str
    race_length: int
    race_mode: str
    quad_type: str
    quad_size: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionEvent:
        return cls(
            player_name=str(data.get("playerName", "")),
            session_name=str(data.get("sessionName", "")),
            scenery_title=str(data.get("sceneryTitle", "")),
            track_name=str(data.get("trackName", "")),
            race_length=_parse_int(data.get("raceLength")),
            race_mode=str(data.get("RaceMode", "")),
            quad_type=str(data.get("quadType", "")),
            quad_size=str(data.get("quadSize", "")),
        )


@dataclass
class CountdownEvent:
    """Pre-race countdown. Multiplayer counts 5→0, single player 3→0; ``0`` is GO.

    Nothing is emitted in single player if the countdown setting is off.
    """

    count_value: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CountdownEvent:
        return cls(count_value=_parse_int(data.get("countValue")))

    @property
    def is_go(self) -> bool:
        return self.count_value == 0


@dataclass
class FinishGateEvent:
    """Track-shape flag sent once right after ``countdown: 0``.

    ``start_finish_gate`` says whether the track has a distinct start/finish
    gate. It is *not* a per-crossing event.
    """

    start_finish_gate: bool

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FinishGateEvent:
        return cls(start_finish_gate=_parse_bool(data.get("StartFinishGate", False)))


@dataclass
class PlayerEvent:
    """Local player state. ``race_manager`` is the room-host flag."""

    player_name: str
    player_colour: str
    player_flying: bool
    race_manager: bool

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PlayerEvent:
        return cls(
            player_name=str(data.get("PlayerName", "")),
            player_colour=str(data.get("playerColour", "")),
            player_flying=_parse_bool(data.get("playerFlying", False)),
            race_manager=_parse_bool(data.get("raceManager", False)),
        )


@dataclass
class RaceTypeEvent:
    """Race format, sent right after ``racestatus: start`` on every race."""

    race_mode: str
    race_format: str
    race_laps: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RaceTypeEvent:
        return cls(
            race_mode=str(data.get("raceMode", "")),
            race_format=str(data.get("raceFormat", "")),
            race_laps=_parse_int(data.get("raceLaps")),
        )


@dataclass
class SpectatorChangeEvent:
    """Camera subject changed. The wire payload is a bare string (the player name)."""

    player_name: str

    @classmethod
    def from_json(cls, data: Any) -> SpectatorChangeEvent:
        return cls(player_name=str(data) if data is not None else "")


RACE_ACTION_START = "start"
RACE_ACTION_ABORT = "abort"
RACE_ACTION_FINISHED = "race finished"


@dataclass
class RaceStatusEvent:
    """Race lifecycle transition: ``start``, ``abort`` or ``race finished``."""

    race_action: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RaceStatusEvent:
        return cls(race_action=str(data.get("raceAction", "")))

    @property
    def action(self) -> str:
        return self.race_action.strip().lower()

    @property
    def is_start(self) -> bool:
        return self.action == RACE_ACTION_START

    @property
    def is_abort(self) -> bool:
        return self.action == RACE_ACTION_ABORT

    @property
    def is_finished(self) -> bool:
        return self.action == RACE_ACTION_FINISHED


@dataclass
class PlayerRaceData:
    """One pilot's entry in a ``racedata`` snapshot.

    ``gate`` is 1-based, counts every gate on the track and resets each lap;
    ``time`` is cumulative race seconds with three decimals (``"69.711"``).
    On the final crossing ``finished`` is true and ``gate`` may be one past
    the per-lap gate count — treat any finished crossing as the race end.
    """

    position: int
    lap: int
    gate: int
    time: str
    finished: bool
    colour: str
    uid: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PlayerRaceData:
        uid = data.get("uid", "")
        return cls(
            position=_parse_int(data.get("position")),
            lap=_parse_int(data.get("lap")),
            gate=_parse_int(data.get("gate")),
            time=str(data.get("time", "")),
            finished=_parse_bool(data.get("finished", False)),
            colour=str(data.get("colour", "")),
            uid="" if uid is None else str(uid),
        )

    @property
    def time_seconds(self) -> float:
        """``time`` as a float (0.0 if unparseable)."""
        return _parse_float(self.time)

    @property
    def time_ms(self) -> int:
        """``time`` in integer milliseconds."""
        return round(self.time_seconds * 1000)


@dataclass
class RaceDataEvent:
    """Whole-field position snapshot, keyed by player name.

    Sent on every checkpoint crossing by any pilot (coalesced to at most
    10/s) and always carries every active pilot's latest state.
    """

    players: dict[str, PlayerRaceData] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RaceDataEvent:
        players: dict[str, PlayerRaceData] = {}
        for name, pdata in data.items():
            if isinstance(pdata, dict):
                players[name] = PlayerRaceData.from_json(pdata)
        return cls(players=players)


@dataclass
class Pilot:
    """A pilot entry in the ``pilotlist`` roster (uid is a string here)."""

    name: str
    uid: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Pilot:
        return cls(name=str(data.get("name", "")), uid=str(data.get("uid", "")))


@dataclass
class PilotListEvent:
    """Reply to the ``getpilots`` command."""

    pilots: list[Pilot] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: list[dict[str, Any]]) -> PilotListEvent:
        return cls(pilots=[Pilot.from_json(p) for p in data if isinstance(p, dict)])


@dataclass
class ActivateErrorEvent:
    """One frame per uid an ``activate`` command asked for that is not in the room."""

    uid_not_found: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ActivateErrorEvent:
        return cls(uid_not_found=str(data.get("UIDNotFound", "")))


@dataclass
class ImuEvent:
    """Local drone attitude/telemetry sample (1.17+, opt-in, Betaflight FC only).

    Streams at 60 Hz whenever the local drone is flying — not only during
    races. ``roll``/``pitch``/``yaw`` are gyro rates, ``position`` the world
    position, ``attitude`` a quaternion, ``speed`` the velocity vector, and
    ``timestamp`` Unity ``Time.time * 1000`` (ms since the game started).
    All values are JSON numbers on the wire.
    """

    roll: float
    pitch: float
    yaw: float
    position: tuple[float, float, float]
    attitude: tuple[float, float, float, float]
    speed: tuple[float, float, float]
    timestamp: float

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ImuEvent:
        f = _parse_float
        return cls(
            roll=f(data.get("roll")),
            pitch=f(data.get("pitch")),
            yaw=f(data.get("yaw")),
            position=(f(data.get("PositionX")), f(data.get("PositionY")), f(data.get("PositionZ"))),
            attitude=(
                f(data.get("AttitudeX")),
                f(data.get("AttitudeY")),
                f(data.get("AttitudeZ")),
                f(data.get("AttitudeW"), 1.0),
            ),
            speed=(f(data.get("SpeedX")), f(data.get("SpeedY")), f(data.get("SpeedZ"))),
            timestamp=f(data.get("timestamp")),
        )

    @property
    def speed_magnitude(self) -> float:
        """Ground-truth speed in game units per second (metres/s in Unity)."""
        x, y, z = self.speed
        return math.sqrt(x * x + y * y + z * z)


# ── Generic Event Wrapper ────────────────────────────────────────────

EventData = (
    SessionEvent
    | CountdownEvent
    | FinishGateEvent
    | PlayerEvent
    | RaceTypeEvent
    | SpectatorChangeEvent
    | RaceStatusEvent
    | RaceDataEvent
    | PilotListEvent
    | ActivateErrorEvent
    | ImuEvent
)

# Event key → parser. Keys are matched exactly (the game is case-inconsistent).
_EVENT_PARSERS: dict[str, type[Any]] = {
    "session": SessionEvent,
    "countdown": CountdownEvent,
    "FinishGate": FinishGateEvent,
    "player": PlayerEvent,
    "racetype": RaceTypeEvent,
    "spectatorChange": SpectatorChangeEvent,
    "racestatus": RaceStatusEvent,
    "racedata": RaceDataEvent,
    "pilotlist": PilotListEvent,
    "ActivateError": ActivateErrorEvent,
    "imu": ImuEvent,
}

# Payload shapes that are not JSON objects.
_LIST_PAYLOADS = {"pilotlist"}
_SCALAR_PAYLOADS = {"spectatorChange"}


@dataclass
class Event:
    """A parsed WebSocket event from VelociDrone.

    Attributes:
        type: The event key (``"session"``, ``"racedata"``, ``"imu"`` ...).
        data: The typed payload, or ``None`` for unknown / malformed events.
        raw: The original one-key JSON dict.
    """

    type: str
    data: EventData | None
    raw: dict[str, Any]

    @classmethod
    def from_json(cls, msg: dict[str, Any]) -> Event:
        """Parse a raw one-key message into a typed Event.

        Unknown keys yield ``data=None`` so consumers can skip future frames
        without dying. A known key with a payload of the wrong shape is also
        returned untyped rather than raising.
        """
        for key, parser_cls in _EVENT_PARSERS.items():
            if key not in msg:
                continue
            payload = msg[key]
            if key in _LIST_PAYLOADS:
                ok = isinstance(payload, list)
            elif key in _SCALAR_PAYLOADS:
                ok = not isinstance(payload, (dict, list))
            else:
                ok = isinstance(payload, dict)
            if not ok:
                return cls(type=key, data=None, raw=msg)
            return cls(type=key, data=parser_cls.from_json(payload), raw=msg)
        event_type = next(iter(msg), "unknown")
        return cls(type=event_type, data=None, raw=msg)
