# Splitter

Personal VelociDrone lap timer. Point it at the gaming PC, open it on a tablet,
fly. It waits for the race to start, logs every gate crossing with the split
against your personal best, records the IMU telemetry, and keeps everything
for analysis later.

- **Live**: race clock, current lap, split vs PB at every gate, last lap and
  its delta, live speed, rolling gate log, result card with per-lap deltas.
- **Races**: every run with track, quad, time, delta vs PB; per-race laps,
  gates (segment and cumulative vs the current PB), top-down flight path
  coloured by speed, speed-over-time with gate ticks; edit or bulk-fix runs.
- **Tracks**: PB, best lap, progression chart, gate consistency (best / median /
  worst / PB-run segment per gate) and the theoretical best.
- **Protocol**: the raw frames the game sent, for figuring out what a game
  mode actually emits.

## Get it

**Desktop, no install:** grab the standalone executable for your OS from the
[latest release](../../releases/latest) and run it. It keeps its data in a
`splitter-data` folder next to the file, opens its own window, and is also
reachable from a tablet on your LAN on port 8100. Details: `desktop/README.md`.

**Self-hosted:**

```sh
docker run -d -p 8100:8100 -v splitter-data:/data ghcr.io/ryan-johnson2/splitter:latest
# or, from a checkout:
docker compose up -d           # http://localhost:8100
```

Long-term hosting on Proxmox or any systemd box: the `splitter-lxc-<version>.tar.gz`
release asset, see `deploy/lxc/README.md`.

## Use it

Settings → enter the gaming PC's **LAN IP** (the game does not listen on
localhost). In the game enable *Options → Main Settings → Websocket
Communication* and, for telemetry, *Websocket IMU*.

Single player never sends the track name over the websocket, so on the live
page tap **Track…** once to say what you're flying; it sticks until you change
it or host a room (hosting sends a full session).

No game handy? `splitter fake-game --loop` serves a scripted one on port 60003.

Long-term hosting on Proxmox: see `deploy/lxc/README.md`.

## What is not in this repo

The **online track picker** (search of VelociDrone's official and community
track lists, which gives each run the track id its personal bests are keyed by)
talks to the game's web API through a small private client package,
`velocidrone-tracks`. It is not published here. The release binaries and images
include it (compiled); a build from this source tree runs without it, and the
Track dialog then takes the track id typed by hand. Everything else — timing,
telemetry, analysis, the websocket client under `libs/velocidrone-ws` — is here.

## Developing

Tests, lint and types run in a container (nothing is installed on the host):

```sh
docker compose --profile dev build test
docker compose --profile dev run --rm test                 # pytest
docker compose --profile dev run --rm test ruff check src tests
docker compose --profile dev run --rm test mypy src
```

Or natively: `pip install -e libs/velocidrone-ws -e . pytest pytest-asyncio httpx ruff mypy`.
