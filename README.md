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

## Run it

```sh
docker compose up -d           # http://localhost:8100
```

Settings → enter the gaming PC's **LAN IP** (the game does not listen on
localhost). In the game enable *Options → Main Settings → Websocket
Communication* and, for telemetry, *Websocket IMU*.

Single player never sends the track name over the websocket, so on the live
page tap **Track…** once to say what you're flying; it sticks until you change
it or host a room (hosting sends a full session).

No game handy? `splitter fake-game --loop` serves a scripted one on port 60003.

Long-term hosting on Proxmox: see `deploy/lxc/README.md`.
