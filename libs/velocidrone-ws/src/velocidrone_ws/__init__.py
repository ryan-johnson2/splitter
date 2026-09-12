"""Python client library for VelociDrone's local WebSocket server."""

from velocidrone_ws.client import VelociDroneWS
from velocidrone_ws.models import (
    ActivateErrorEvent,
    CountdownEvent,
    Event,
    FinishGateEvent,
    ImuEvent,
    Pilot,
    PilotListEvent,
    PlayerEvent,
    PlayerRaceData,
    RaceDataEvent,
    RaceStatusEvent,
    RaceTypeEvent,
    SessionEvent,
    SpectatorChangeEvent,
)

__all__ = [
    "VelociDroneWS",
    "ActivateErrorEvent",
    "CountdownEvent",
    "Event",
    "FinishGateEvent",
    "ImuEvent",
    "Pilot",
    "PilotListEvent",
    "PlayerEvent",
    "PlayerRaceData",
    "RaceDataEvent",
    "RaceStatusEvent",
    "RaceTypeEvent",
    "SessionEvent",
    "SpectatorChangeEvent",
]
