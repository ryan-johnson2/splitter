# velocidrone-ws User Guide

A Python client library for VelociDrone's local WebSocket server. Provides async, typed access to real-time race events and game commands — useful for building race overlays, OBS integrations, and race management tools.

## Installation

```bash
pip install velocidrone-ws
```

Or install from the built wheel:

```bash
pip install dist/velocidrone_ws-0.1.0-py3-none-any.whl
```

### Dependencies

- `websockets` — Async WebSocket client

## How It Works

VelociDrone runs a **local WebSocket server** on your machine when the "use-web-socket" option is enabled in the simulator settings. External tools connect to this server to receive real-time race data and optionally send commands (start races, change cameras, etc.).

This library is an async **client** that connects to VelociDrone's server.

### Enabling the WebSocket Server

In VelociDrone:
1. Open Settings
2. Enable the "Web Socket" option (this sets `use-web-socket` in the sim states)
3. The server starts on port **60003**

## Quick Start

```python
import asyncio
from velocidrone_ws import VelociDroneWS

async def main():
    async with VelociDroneWS() as ws:
        async for event in ws.events():
            print(f"[{event.type}] {event.data}")

asyncio.run(main())
```

## Client Reference

### Creating a Connection

```python
from velocidrone_ws import VelociDroneWS

# Connect to localhost (same machine as VelociDrone)
async with VelociDroneWS() as ws:
    ...

# Connect to another machine on the network
async with VelociDroneWS(host="192.168.1.100") as ws:
    ...

# Custom port and service path
async with VelociDroneWS(host="192.168.1.100", port=60003, service="ws") as ws:
    ...

# Manual lifecycle
ws = VelociDroneWS()
await ws.connect()
try:
    ...
finally:
    await ws.close()
```

### Receiving Events

VelociDrone sends real-time events during gameplay. Use the async iterator to process them:

```python
async with VelociDroneWS() as ws:
    async for event in ws.events():
        print(f"Event type: {event.type}")
        print(f"Parsed data: {event.data}")
        print(f"Raw JSON: {event.raw}")
```

Or receive one event at a time:

```python
event = await ws.recv_event()
if event:
    print(event.type, event.data)
```

### Event Types

All events are returned as `Event` objects with a `type` string and typed `data` field.

#### `session` — Session Created

Sent when a new room/session is created.

```python
from velocidrone_ws import SessionEvent

if isinstance(event.data, SessionEvent):
    print(f"Player: {event.data.player_name}")
    print(f"Track: {event.data.track_name}")
    print(f"Scene: {event.data.scenery_title}")
    print(f"Mode: {event.data.race_mode}")
    print(f"Quad: {event.data.quad_type} ({event.data.quad_size})")
```

Fields: `player_name`, `session_name`, `scenery_title`, `track_name`, `race_length`, `race_mode`, `quad_type`, `quad_size`.

#### `countdown` — Race Countdown

Sent during the pre-race countdown (3, 2, 1, GO).

```python
from velocidrone_ws import CountdownEvent

if isinstance(event.data, CountdownEvent):
    print(f"Countdown: {event.data.count_value}")
```

Fields: `count_value`.

#### `FinishGate` — Gate Crossing

Sent when a player crosses the start/finish gate.

```python
from velocidrone_ws import FinishGateEvent

if isinstance(event.data, FinishGateEvent):
    print(f"Start/Finish: {event.data.start_finish_gate}")
```

Fields: `start_finish_gate`.

#### `player` — Player State

Sent with player state updates.

```python
from velocidrone_ws import PlayerEvent

if isinstance(event.data, PlayerEvent):
    print(f"{event.data.player_name} (colour: {event.data.player_colour})")
    print(f"Flying: {event.data.player_flying}, Host: {event.data.race_manager}")
```

Fields: `player_name`, `player_colour`, `player_flying`, `race_manager`.

#### `racetype` — Race Type Info

Sent when race type/format information is broadcast.

```python
from velocidrone_ws import RaceTypeEvent

if isinstance(event.data, RaceTypeEvent):
    print(f"Mode: {event.data.race_mode}, Format: {event.data.race_format}")
    print(f"Laps: {event.data.race_laps}")
```

Fields: `race_mode`, `race_format`, `race_laps`.

#### `spectatorStatus` — Spectator Mode Changed

```python
from velocidrone_ws import SpectatorStatusEvent

if isinstance(event.data, SpectatorStatusEvent):
    print(f"Spectator change: {event.data.spectator_change}")
```

Fields: `spectator_change`.

#### `racestatus` — Race Status Changed

Sent when the race state changes (started, aborted, etc.).

```python
from velocidrone_ws import RaceStatusEvent

if isinstance(event.data, RaceStatusEvent):
    print(f"Race action: {event.data.race_action}")
```

Fields: `race_action`.

#### `racedata` — Real-Time Race Data

Sent with per-player race position data during a race. This is the primary event for building race overlays.

```python
from velocidrone_ws import RaceDataEvent, PlayerRaceData

if isinstance(event.data, RaceDataEvent):
    for name, player in event.data.players.items():
        print(f"{name}: P{player.position} Lap {player.lap} "
              f"Gate {player.gate} Time {player.time} "
              f"Finished: {player.finished}")
```

`RaceDataEvent.players` is a `dict[str, PlayerRaceData]` keyed by player name.

`PlayerRaceData` fields: `position`, `lap`, `gate`, `time`, `finished`, `colour`, `uid`.

#### `pilotlist` — Pilot List

Sent in response to a `get_pilots()` command.

```python
from velocidrone_ws import PilotListEvent

if isinstance(event.data, PilotListEvent):
    for pilot in event.data.pilots:
        print(f"{pilot.name} (UID: {pilot.uid})")
```

Fields: `pilots` (list of `Pilot` with `name`, `uid`).

#### `ActivateError` — Activation Failed

Sent when a pilot activation fails because the UID was not found.

```python
from velocidrone_ws import ActivateErrorEvent

if isinstance(event.data, ActivateErrorEvent):
    print(f"UID not found: {event.data.uid_not_found}")
```

Fields: `uid_not_found`.

### Sending Commands

Commands let you control VelociDrone remotely. Most commands require being the room host.

```python
# Keep-alive
await ws.ping()

# Race control (host only)
await ws.start_race()
await ws.abort_race()

# Room control (host only)
await ws.lock()
await ws.unlock()

# Pilot activation (host only)
await ws.activate(["uid1", "uid2"])
await ws.get_pilots()     # Response comes as a pilotlist event
await ws.all_spectate()

# Camera control (multiplayer only)
await ws.camera_player("uid")        # Focus on a player
await ws.camera_mode("fpv")          # "fpv" or "spectate"
await ws.camera_select(2)            # Select camera number
await ws.camera_reset()              # Reset camera
```

### Error Handling

```python
from velocidrone_ws.exceptions import (
    VelociDroneWSError,   # base exception
    ConnectionError,      # connection failed
    CommandError,         # command send failed
)

try:
    async with VelociDroneWS() as ws:
        await ws.ping()
except ConnectionError as e:
    print(f"Connection failed: {e}")
except CommandError as e:
    print(f"Command failed: {e}")
```

## Example: Race Overlay

```python
import asyncio
from velocidrone_ws import (
    VelociDroneWS,
    SessionEvent,
    CountdownEvent,
    RaceDataEvent,
    RaceStatusEvent,
)

async def overlay():
    async with VelociDroneWS() as ws:
        async for event in ws.events():
            if isinstance(event.data, SessionEvent):
                print(f"\n=== {event.data.track_name} ===")
                print(f"Scene: {event.data.scenery_title}")
                print(f"Mode: {event.data.race_mode}")

            elif isinstance(event.data, CountdownEvent):
                v = event.data.count_value
                print(f"{'GO!' if v == 0 else v}...")

            elif isinstance(event.data, RaceStatusEvent):
                print(f"Race {event.data.race_action}")

            elif isinstance(event.data, RaceDataEvent):
                for name, p in sorted(
                    event.data.players.items(),
                    key=lambda x: x[1].position,
                ):
                    status = "FINISHED" if p.finished else f"Lap {p.lap}"
                    print(f"  P{p.position} {name}: {p.time} ({status})")

asyncio.run(overlay())
```

## Development

```bash
# Run tests (uses mocked WebSocket — never connects to a real server)
hatch run test

# Lint
hatch run lint

# Type check (mypy strict)
hatch run typecheck

# Build wheel
hatch build
```
