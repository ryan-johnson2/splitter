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
- **A run cannot outlive the game link.** Closing the game mid-run sends no
  abort, so the controller aborts the race itself when the bridge has been
  disconnected for `GAME_LOSS_GRACE_S` (10 s; a blip that reconnects sooner
  keeps the run). The live page has an **Abort** button while a run is on:
  `POST /api/race/abort` ends it here and, when connected, sends the game's
  `abortrace` command (works in single player). Startup still marks any race
  left `running` as aborted.
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

## Sections, crashes, geometry (0.4.0, issue #5)

- **Lap-relative segments**: `core/sections.py::lap_segments` turns a run's
  crossings into `{lap: [segment 1..G]}` — lap-0 crossings and the first lap-1
  crossing (the S/F that ends the holeshot) are not segments; a crossing with
  `ends_lap` set is the lap's last segment (G = `gates_per_lap`). Nothing else
  needs the wire ordinals; labels ("G3"… "S/F") come from `segment_labels`.
- **Sections** (`track_sections` table, keyed by online `track_id` only) cover
  `1..G` contiguously. First view of a track page seeds them
  (`web/analysis.py::track_analysis` → `sections.suggest`: the largest section is
  split at its best-scoring boundary — heading change at the previous gate,
  speed-regime change, spacing jump — until 8 sections / 3-gate minimum; thirds
  when there is nothing to go on). Edited on the track page (tap a gate to split,
  tap a section's first gate to merge; `POST /tracks/{id}/sections`, `…/reset`).
- **Gate positions** (`core/geometry.py`): the drone's interpolated position at
  each crossing time, averaged over the PB + 7 most recent traced runs; gives the
  top-down map on the track page, spacing and heading change per gate.
- **Crashes** (`core/crashes.py`): events in the trace of every run — the pilot
  often flies on, so abort ≠ crash and the game sends nothing. A single-step decel
  ≤ −120 m/s² or ≥ 70 % speed loss in 100 ms (at ≥ 8 m/s, ≤ −80 m/s² over the
  window), confirmed by a gyro spike above the run's 95th percentile (floor 300)
  or a ≥ 300 ms dwell below 3 m/s; bounces within 1.5 s merge. Measured on real
  runs: crashes −130…−320 m/s², gyro 700–1170; braking −60…−85, gyro p99 ≈ 570.
  Detected at race end on the full 60 Hz buffer (finished runs: only up to the
  finish), stamped with `(lap, segment)` and stored as JSON on `races.crashes`
  (+ `crash_count`); `splitter backfill-crashes [--all]` scans stored 20 Hz
  traces for older runs. Per-segment `min_speed` / `min_accel` / `max_accel`
  are recorded on gate_times and laps at crossing time (not recoverable later).
- **Pages**: track page = sections table ranked by *on the table* (PB per-lap
  section time − best-ever), spread (IQR), min speed, crashes, verdict (`line`
  when the PB is ≥ max(150 ms, 6 %) off the best; `mistakes` when ≥ 15 % of laps
  blow up ≥ 1.5× median; else `solid`), trend (last 3 runs vs the 3 before), map,
  editor; gate consistency collapsed at the bottom. Race page = this run per
  section and lap vs the same lap of the PB, crash marks (✕ on the path and speed
  charts), gate crossings collapsed at the bottom. Live page untouched (by design).

## Helping non-technical users (0.5.0)

- **Desktop = same PC as the game.** `desktop_entry.py` sets `SPLITTER_DESKTOP=1`
  → `Config.desktop`. On first run with no `game_host`, startup pre-fills it
  with `core/netinfo.py::default_route_ipv4()` (UDP-connect trick, nothing is
  sent); the Settings page lists every local IPv4 as one-tap chips
  (`local_ipv4_addresses()`, default route first) for PCs with several
  adapters. Server/LXC builds never guess (the game is elsewhere).
- **Getting-started checklist** on the live page (`#getting-started`): address
  set / game connected / track picked, ticked live from the link, hidden once
  all done or while a run is on.
- **Help popovers** (`link.js`, `HELP` map): anything with `data-help="key"`
  opens a click popover (hover titles don't work on tablets). Keys: `link`,
  `game` (adapts to the bridge state, shows the configured address and last
  error), `track`, `abort`, `noid`. The header indicators are buttons. Add a
  key there rather than sprinkling text into pages.
- Track page: per-section trend chart (`analysis.trends`, y = mean per-lap
  section time minus the section's best-ever; legend isolates one section).
  Race page flight path labels one gate per number, every 5th on long tracks.

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
