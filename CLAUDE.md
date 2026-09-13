# Splitter

Personal VelociDrone lap timer. Runs in a Proxmox LXC, connects to the game's
local websocket on the gaming PC, and is watched on a tablet while flying:
gate-by-gate splits against your PB live, every run logged, analysis pages
afterwards. Successor to the archived `velocidrone-lap-tracking` (Flask +
Socket.IO + manual session entry); built in Marshal's shape.

## Architecture (one asyncio process)

- FastAPI (uvicorn) serves the pages and a **native websocket** at `/ws/live`;
  its lifespan supervises one task, `game/bridge.py::GameBridge`, which holds
  the connection to the game (`velocidrone-ws` sibling library), pings every
  5 s, and reconnects with backoff. The bridge is the only module that talks
  to the game.
- `game/controller.py::RaceController` turns game events into DB rows and
  live messages. `core/` is pure and the most-tested part:
  `timing.py::RaceTracker` (snapshots → crossings/laps), `splits.py::Reference`
  (PB deltas by crossing index), `telemetry.py::TelemetryBuffer` (IMU samples
  → per-segment speed/distance, downsampling).
- `live/hub.py::LiveHub` fans messages out to browser sockets (per-client
  queues, oldest dropped when full). **Every page** opens one socket
  (`static/link.js`, loaded by `base.html`): it paints the two header
  indicators — *Splitter* (this socket: live / connecting / lost) and *Game*
  (the bridge state, shown as *unknown* whenever the socket is down so a stale
  page can never claim the game is connected) — and re-dispatches messages
  via `Splitter.link.on(type, fn)`; `live.js` subscribes instead of opening
  its own. A client gets a full `snapshot` on connect and can ask for another
  with a `"snapshot"` text frame (used on tab focus).
- Where the page runs is decided in `base.html` before anything renders:
  `html.desktop` when the Tauri shell's init script set `window.SPLITTER_DESKTOP`
  (`desktop/src-tauri/src/lib.rs`), `html.standalone` when installed as a PWA
  (display-mode media queries, re-checked on change, the `?source=pwa` start
  URL, or a sessionStorage flag carried across navigations). Both hide the
  browser-only controls (Install app, ⛶ fullscreen); desktop also skips the
  service worker. `Splitter.env` exposes the same answers to scripts.
- SQLite (aiosqlite + SQLAlchemy 2.0 async, WAL). `create_all` at startup plus
  automatic `ADD COLUMN` for additive changes (`db/engine.py::init_db`).
- Runtime knobs live in the `settings` table (`db/runtime_settings.py`),
  edited on the Settings page; env (`config.py`) only has `DATABASE_URL`,
  `HOST`, `PORT`.

## Race lifecycle (what the game sends, 1.17.13)

```
racestatus:start → armed
racetype         → race mode / format / laps (the only SP info about the run)
countdown 3..0   → 0 = GO: Race row created, PB reference loaded
FinishGate       → track-shape flag (has a distinct start/finish gate)
racedata …       → one crossing per new (lap, gate). The lap counter increments
                   at the start/finish gate: GO → first S/F crossing is the
                   **holeshot** (in the total, not a lap); lap n runs from the
                   first lap-n crossing to the first lap-(n+1) crossing;
                   finished:"True" ends the last lap. Verified against the
                   game's own lap times (core/timing.py docstring).
racestatus:"race finished" / "abort"
```

- If the single-player countdown is off there are no countdown frames: the
  first racedata after arming starts the race.
- An abort with zero crossings deletes the row; with crossings it is kept as
  `aborted`. A `start` while a race is running aborts the old one first.
- **Single player never names the track.** `session` (track, scenery, quad,
  laps) only fires when *this* machine creates a multiplayer room. Splitter
  therefore keeps a `SessionState` with a `source`: `game` (session event),
  `sticky` (carried over from the last run, persisted in settings as
  `last_*`), or `manual` (the "Track…" dialog on the live page, `POST
  /api/session`). Runs record `session_source`, and the Races page has bulk
  edit to fix mis-attributed runs. **Nemesis mode emits nothing extra**
  (verified 2026-09-12 with the Protocol page: the same frames as any SP race,
  no track name anywhere), so there is no automatic route in SP.
- The "Track…" dialog is a **picker** over the game's online track lists
  (`game/catalog.py::TrackCatalog`, `GET /api/tracks/search?q=&source=`),
  the same endpoints Marshal's event form uses via the sibling
  `velocidrone-api` library. Neither `get_official_tracks` nor
  `rated_tracks_list` needs credentials, so Splitter calls them anonymously.
  **A pick is required**: `POST /api/session` rejects a session without a
  `track_id`. Hosted-room `session` events (names only) are resolved to an id
  by exact name (`TrackCatalog.resolve`, official first); unresolved runs get
  `track_id` 0 and never become PBs. The same widget (`static/picker.js`) is on
  the race edit and bulk-edit forms to re-attribute runs.
- **Quads** come from the bundled game catalog (`core/quads.py`,
  `data/catalog.json` — the same snapshot Marshal ships: `models` +
  `sceneries` from the game's `settings.db`; refresh with
  `splitter extract-catalog <settings.db>`). The dialog offers class → model
  selects (`GET /api/quads`); the game's `quadType` string is matched to a
  model by exact name. Scene ids from the online lists get their names from
  the same catalog.
- PBs are per **`PBKey(track_id, quad_model_id, race_laps)`** (`db/repos.py`);
  `track_id` 0 is never a PB or a reference. `is_best` is re-flagged on finish,
  edit and delete (`repos.recalculate_best`). Names on races are display only.
- Which racedata entry is "me": the `player_name` setting, else the only
  pilot, else skip with a one-time notice.

## Telemetry (IMU)

Opt-in in the game (`web-socket-imu`, Betaflight FC only), 60 Hz, local drone
only, all JSON numbers, streams whenever flying (not just in races). Splitter
keeps every sample of a race in memory, aligns the game clock to the race clock
on the first frame after GO, computes max/avg speed and distance per gate and
per lap from the full stream, and stores a downsampled trace
(`telemetry_store_hz`, default 20) for the flight-path and speed charts on the
race page. Speed = |velocity| in m/s, shown as km/h. `imu` frames are logged
to the event log at most once per 5 s.

## Wire facts to remember

All race-event scalars are strings (`"3"`, `"69.711"`, `"True"`), `uid` in
racedata is a number, `imu` is all numbers, `spectatorChange` is a bare string;
server frames are binary; the game listens on its LAN IP only (never
loopback); newest client wins the feed. Full spec:
`../velocidrone-libraries/velocidrone-websocket/docs/ws-spec.md`.

## Pages

`/` live (tablet), `/races` + `/races/{id}` (laps, gates vs PB, flight path,
speed chart, edit/delete, bulk edit), `/tracks` + `/tracks/detail` (PB,
progression chart, gate consistency, theoretical best), `/settings`,
`/protocol` (raw frames). JSON: `/api/state`, `/api/session`,
`/api/connection`, `/api/races[/{id}]`, `/healthz`.

## Build & tooling

Hatch, mypy strict, ruff, pytest — same as Marshal. **Run tests/lint/typecheck
in a container, never on the host** (no uv/hatch/venv on the host):

```sh
docker compose --profile dev build test          # once, and after dependency changes
docker compose --profile dev run --rm test       # pytest
docker compose --profile dev run --rm test ruff check src tests
docker compose --profile dev run --rm test mypy src
```

The repo is self-contained: the websocket client is vendored under
`libs/velocidrone-ws` (copied from velocidrone-libraries; keep them in step by
hand). The online track client is the **private** `velocidrone-tracks` package
(velocidrone-libraries repo) and is an *optional* import (`game/catalog.py::
load_backend`); without it the picker is disabled and manual ids work. Never hit
a real game or the online API from tests (`app.state.catalog` takes a fake).

Exercise the whole pipeline without the game:
`splitter fake-game --port 60003 --loop [--no-session] [--speed 5]` (a
scripted server speaking the real wire shapes), then set the game address to
that host on the Settings page.

Deploy: `deploy/lxc/` (bundle = splitter + velocidrone-ws wheels, plus the
private tracks wheel when available, + installer + unit; `install.sh` is
idempotent and is the upgrade path). Docker: `docker compose up -d`, data in
`./data`, port 8100; release images go to ghcr.io/ryan-johnson2/splitter.

## Desktop / releases

`desktop/` holds the portable native app: `sidecar/build.py` freezes the server
with PyInstaller (`--tracks … --protect` Cython-compiles the private client in),
`src-tauri/` is a Tauri 2 shell that **embeds** that binary (`build.rs`), extracts
it into a portable `splitter-data/` folder beside the exe, spawns it and opens a
window at its `SPLITTER_READY` URL (`splitter/desktop_entry.py`). Config reads
`SPLITTER_DATA_DIR` for the SQLite location. `.github/workflows/release-builds.yml`
builds all of it on a `v*` tag, a `dev-*` tag, or on demand (publish=dev cuts a
`dev-<date>` tag): portable exe per OS, LXC bundle, GHCR image, GitHub Release
(prerelease for dev). Versions come from the build stamp (`scripts/stamp.py`:
clean v-tag → its version, else `<base>-dev-<hash>`; written to the gitignored
`splitter/_stamp.py` at build time and read by `version.py`). Secret `LIBRARIES_TOKEN` (read access to
velocidrone-libraries) is what makes the online picker part of a build. Do not
touch the user's running Docker container while working on release plumbing
unless asked.
