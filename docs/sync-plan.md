# Implementation plan: capture nodes, web server, and sync

Status: plan (2026-09-25, revised 2026-09-26; phases 1, 2 and 4 and the web side
of 3 built 2026-09-26), branch `feature/cloud-sync`. The design is
`docs/sync-design.md`; this is how it gets built, phase by phase, against the
code as it stands at 0.5.2. Each phase ships on its own, keeps the current
single-process app working, and is tracked by one GitHub issue.

| phase | issue | ships as | outcome                                                          |
|-------|-------|----------|------------------------------------------------------------------|
| 1     | #11   | 0.6.0    | every run has an identity and a document; move results by hand   |
| 2     | #12   | 0.7.0    | a node pushes runs to an upstream web; local DB stays small      |
| 3     | #13   | 0.8.0    | node as a service via an installer; the web identifies tracks    |
| 4     | #16   | 0.9.0    | label clusters of unknown layouts from the review queue          |
| 5     | #14   | later    | relay: live view and session pick from the web (optional)        |
| 6     | #15   | later    | multi-user, then decide about hosting                            |

## Decisions taken up front

- **Run id = `uuid4().hex`.** Stdlib, no dependency. Nothing orders by the
  id (progression and trends sort by `started_at`), so ULID buys nothing.
- **Node id = `uuid4().hex`, minted once** into the `node_id` setting the
  first time the app starts after the upgrade. Shown on the Settings page so
  the web can name the node ("3 runs pending from *gaming-pc*"); a
  `node_name` setting gives it a friendly label.
- **Telemetry blob = float32 little-endian columns, zlib.** Stdlib again.
  The blob row carries `encoding` so a switch to zstd or delta coding later
  is a new encoding string, not a migration.
- **`httpx` becomes a runtime dependency** (it is already a dev one). The
  uploader takes an `httpx.AsyncClient` so tests inject one bound to the web
  app through `ASGITransport` and never touch the network.
- **Ingest auth = `Authorization: Bearer <token>`.** One token in the web's
  settings for now; per-user tokens are phase 6.
- **The app stays one package, one binary, one process.** Node and web are
  what a given install has configured, never separate programs, and there
  is no headless build: the node is a service that also serves the pages,
  and the desktop window is a viewer of it.
- **Local runs are kept by default.** `keep_local_runs` defaults to on even
  with an upstream, so the web is never the only copy and the finish-line
  PB banner keeps working from local rows. Phase 1's blob already makes a
  heavy year tens of MB.
- **`seq` is assigned at outbox enqueue, not at GO.** Aborts with zero
  crossings delete the row and never upload; a counter at GO would leave a
  phantom gap for every false start. The uuid is still minted at GO.
- **Deleted runs stay deleted.** The web keeps a `run_tombstones` table of
  deleted uuids; ingest and import refuse them.
- **No two-way sync, ever.** Edits and deletes happen on the web after the
  run is acknowledged. A node never re-uploads an acknowledged run.

## Phase 1: run identity and the run document (#11) — built

As planned, with two changes found while building: `db/prepare.py` is the
one place startup *and* the CLI prepare a database (a run exported from the
command line must carry the same uuid the app gave it), and the telemetry
migration lives there rather than in the app alone.

Everything else depends on this, and it is useful alone: results can be
exported from the LXC and imported on a laptop, or vice versa.

### Schema (all additive, handled by `init_db`)

`races` gains:

| column           | type          | notes                                                   |
|------------------|---------------|---------------------------------------------------------|
| `uuid`           | TEXT, unique  | minted at GO; backfilled for existing rows at startup   |
| `node_id`        | TEXT          | the capturing node; backfill = this node                |
| `seq`            | INTEGER       | per-node counter at GO; backfill = 0 ("pre-sync")       |
| `reference_uuid` | TEXT nullable | UUID of `reference_race_id`; backfilled by join         |
| `received_at`    | DATETIME null | set on import/ingest, never on local capture            |
| `fingerprint`    | TEXT          | JSON `{gates_per_lap, lap1_gates: [[x,y,z]…]}` or ""    |
| `origin`         | TEXT          | `capture` / `import` / `ingest`; display only           |

New table `telemetry_blobs`:

| column      | type                        |
|-------------|-----------------------------|
| `race_id`   | INTEGER PK, FK races CASCADE |
| `hz`        | REAL                        |
| `samples`   | INTEGER                     |
| `columns`   | TEXT (JSON list of names)   |
| `encoding`  | TEXT, `f32le+zlib`          |
| `data`      | BLOB                        |

The old `telemetry` table stays until the operator runs the migration; reads
try the blob first and fall back to rows. `init_db` also backfills uuids,
node_id and reference_uuid in one pass (idempotent: only rows with an empty
uuid).

### Code

- `core/rundoc.py` (pure, tested): `build(race, laps, gate_times, blob) ->
  dict` and `parse(doc) -> RunDoc` dataclass with validation (`version`,
  required keys, lap/gate counts agree, blob decodes). `DOC_VERSION = 1`.
- `core/telemetry.py`: `encode_columns(samples, hz) -> (bytes, meta)` and
  `decode_columns(bytes, meta) -> list[Sample]`; the same functions serve the
  blob table and the document.
- `game/controller.py::_start_race`: mint `uuid`, read/increment `node_seq`
  setting, stamp `node_id`. `_finish_race`: write the blob instead of rows;
  compute the fingerprint from the IMU buffer with the same geometry helper
  `_check_track` uses and store it (also on aborts with crossings). Take the
  gate positions from the **fastest lap with no crash**, lap 1 as the
  fallback, so a lap-1 crash does not put gates in odd places. Set
  `reference_uuid` alongside `reference_race_id`.
- `db/repos.py`: `telemetry_for_race` keeps its signature and decodes the
  blob (falls back to rows). New `get_race_by_uuid`, `import_run(doc) ->
  (race, created: bool)` which inserts race + laps + gate_times + blob in one
  transaction, resolves `reference_uuid` to a local `reference_race_id` when
  the referenced run exists, and returns without change when the uuid exists.
- `web/routes/api.py`: `GET /api/races/{id}/export` (the document, filename
  `<started_at>-<uuid8>.json`), `POST /api/import` accepting one document or a
  list, returning `[{uuid, status: created|exists|rejected, error?}]` and
  running `recalculate_best` once per touched `PBKey`.
- `manage.py`: `splitter export [--since DATE] [--all] -o DIR`,
  `splitter import FILE…`, `splitter backfill-fingerprints` (from stored
  traces, like `backfill-crashes`).
- **Telemetry migration runs itself**: at startup, after `init_db`, rows are
  converted to blobs in batches of 50 races and the rows deleted per race
  in the same transaction, so an interrupted startup resumes. Only
  `splitter migrate-telemetry --drop` (drop the empty table) is manual, and
  the docs say to `splitter export --all` first.
- **Threshold spike** (before phase 3 relies on it): a script over the
  existing database computing same-track and cross-track fingerprint
  distances, to confirm 12 m over ≥ 4 gates separates layouts across many
  runs and to pick the clustering threshold for phase 4.
- Race page: an *Export* link; Races page: an *Import* button (file input).

### Tests

- `test_rundoc.py`: round trip build → parse → build is identical; blob round
  trip preserves values to float32; malformed documents are rejected with a
  reason.
- `test_web.py`: export then import into a second app instance yields the
  same race, laps, gates and telemetry; importing twice is a no-op; importing
  a faster run re-flags `is_best`; importing a run whose reference is absent
  keeps `reference_uuid` and leaves `reference_race_id` null.
- `test_controller.py`: a run gets uuid, node_id and increasing seq; the
  fingerprint is stored at finish; telemetry lands as a blob.
- Migration test: rows → blob → `telemetry_for_race` returns the same samples.

### Done when

A run captured on the desktop app can be exported, imported into the LXC,
and appears there as a PB with its flight path, with no network code yet.

## Phase 2: push to an upstream web (#12) — built

As planned, plus: runs recorded or imported before the upstream was set are
queued when it is (`enqueue_all`); an imported document with no node id is
stamped with the importing install's; ingest is serialised on the web and
uploader passes on the node (two concurrent pushes of one run must not both
insert); Races/Tracks stay in the nav while local copies are kept, and a
*Web ↗* link is added. The picker proxy is in but untested against a real
private client.

### Node side

- Settings: `upstream_url`, `upstream_token`, `keep_local_runs` (default
  `1`), `node_name`. Settings page section *Upstream* with a *Test
  connection* button (`GET <upstream>/api/ingest/ping` with the token) and a
  *Remove local copies* button (`POST /api/sync/purge`: deletes every run
  with an acked outbox row, shows the count first). The node refuses to send
  the token over plain `http://` unless the host is a private address.
- Table `outbox`: `race_uuid` PK, `seq`, `queued_at`, `attempts`, `next_at`,
  `last_error`, `acked_at` nullable. A row is added in `_finish_race` for
  every finished or aborted-with-crossings run, and that is where `seq` is
  taken from the `node_seq` setting. Survives restarts.
- `sync/uploader.py::Uploader`: supervised task next to the bridge (same
  `_supervise`). Loop: pick due rows oldest first, build the document, `PUT`
  it, on 2xx mark acked, store the returned reference bundle in the cache,
  and delete the local run only when `keep_local_runs` is off. Backoff 5 s →
  5 min. Wakes immediately on a new outbox row (an `asyncio.Event`). A 409
  `unsupported document version` or 410 `deleted` is terminal: the row is
  marked with the reason, never retried, and the Settings page and live
  header show it.
- Table `reference_cache`: `pb_key` (text `track:quad:laps`) PK, `payload`
  JSON, `updated_at`. `controller.refresh_reference` order: cache, then
  upstream `GET /api/reference?…` when connected, then local DB.
- Snapshot carries `sync: {upstream: bool, pending: n, last_ack_at,
  last_error}`; the live page header gets a third indicator *Web* (help key
  `sync`), and the getting-started card learns nothing new (an upstream is
  optional).
- When an upstream is set the nav hides Races and Tracks and shows one link
  to the web; `/api/tracks/search` proxies to the upstream so the private
  tracks client is only needed there.

### Web side

- Setting `ingest_token` (generated on first view of the Settings page,
  shown once, regenerate button).
- `PUT /api/ingest/runs/{uuid}` in `web/routes/ingest.py`: bearer check,
  body cap (8 MB) before parsing, tombstone check (410), `DOC_VERSION`
  check (409 with the supported version), `repos.import_run` with
  `origin=ingest` and `received_at=now`, per-key `recalculate_best`,
  response `{status, reference: bundle}`. `GET /api/ingest/ping` returns
  `{ok, version, doc_version}` for the test button. `GET /api/reference`
  for cold starts (also served on a node from its own DB, so the app is
  symmetric).
- Table `run_tombstones`: `uuid` PK, `deleted_at`. Written by
  `repos.delete_race`; checked by ingest and `POST /api/import`.
- The reference bundle = `Reference` crossings for the key + `track_geometry`
  for the track, in `core/splits.py` shape so the node can load it straight
  into `Reference`.
- Table `nodes`: `node_id` PK, `name`, `last_seen_at`, `max_seq`,
  `imu_seen_at`. Ingest updates it; the Races page shows "n runs pending
  from *name*" when `max_seq` minus the count of runs with that node_id is
  positive, and lists the gaps in a tooltip. `imu_seen_at` comes from the
  document (`telemetry.samples > 0`).

### Tests

- `test_sync.py`: node app and web app in one process, uploader given an
  `ASGITransport` client. A finished run appears on the web within one loop
  iteration; a 500 from the web keeps it queued with backoff; a duplicate
  `PUT` is a no-op; after ack the node has no race row when
  `keep_local_runs` is off and still has it when on; the reference cache
  holds the bundle from the response and `refresh_reference` uses it with
  the DB empty; a purge deletes only acked runs.
- Auth: wrong token → 401, nothing written. Tombstoned uuid → 410 and the
  outbox row is terminal. Newer `DOC_VERSION` → 409 and terminal.
- Seq: an abort with no crossings between two uploads leaves no gap.

### Done when

The LXC is the web, the desktop app is a node, and a run flown on the PC
shows up on the LXC's Races page in seconds with the PC keeping only its
outbox and reference cache.

## Phase 3: the node as a service, identification on the web (#13) — web side built

Built 2026-09-26: the registry, identification at ingest, learning from every
attributed run (including a node's own pick at ingest and race end), derived
deltas, the review queue with re-run, twins flagged, the IMU warning in the
header, `splitter service` with dry-run for systemd / launchd / `sc`, the
sidecar's Windows service mode (pywin32, untested), and `node.port` /
`node.pid`. Still to do: the NSIS installer with service hooks, the shell
attaching to a running service, data-dir migration from a portable install,
the OS notification. Those need a Windows machine to finish and verify.

### Node

- **No headless build.** The service runs `create_app` as it is; the pages
  cost nothing. `splitter node` is dropped from the plan.
- `splitter service install | remove | status | start | stop`
  (`splitter/service.py`): Windows → `sc create` with the service binary at
  its installed path, auto start, and a DACL that lets interactive users
  start and stop it; Linux → a systemd unit; macOS → a launchd daemon.
  Renders the unit/task text with a `--dry-run` flag so tests cover it
  without registering anything. Windows service entry: `pywin32`
  `ServiceFramework` wrapper around `desktop_entry.main`, or the NSSM-style
  approach if pywin32 fights PyInstaller; decide with a spike.
- **Data dir** for the service is `%ProgramData%\Splitter` (Windows),
  `/var/lib/splitter` (Linux, as the LXC already does). `SPLITTER_DATA_DIR`
  stays the override. On first start with an empty database the service
  copies `splitter-data/splitter.db` from a portable install next to a
  known path (the installer passes it) and logs that it did.
- **Installer**: Tauri's NSIS bundle, per-machine, with install hooks that
  run `service install` and `service start`, and upgrade hooks that stop
  before replacing and start after. Uninstall runs `service remove` and
  leaves ProgramData unless the user ticks *remove my data*. The portable
  exe keeps building for two more releases and is listed second.
- **Window as viewer**: `lib.rs` reads `<data dir>/node.port` (written by
  the service on bind) and opens the webview there. If the file is missing
  or the port does not answer, the window shows a *Service not running*
  page with a *Start* button (`sc start`, allowed by the DACL) and a link to
  the log. The window no longer spawns a sidecar when a service is
  installed; a portable install still does, unchanged.
- **IMU warning**: the controller sets `imu_missing` when a run reaches its
  first crossing with no `imu` frame received since GO, and clears it when
  one arrives. Snapshot carries it; the live header shows a warning with
  help key `imu` (how to enable `web-socket-imu` in the game); the service
  raises one OS notification per session (`plyer` or Tauri's notification
  API through the window when open). Never blocks recording.
- Sticky session stays on; with an upstream the web can re-attribute, and
  the review queue shows `session_source` so sticky guesses are visible.
- Runs upload with `reference_uuid` null and `pb_delta_ms` null when the
  node had no reference at GO; the web fills both against the PB as of
  ingest and sets `delta_source = derived` (new column, display only).

### Web

- Table `track_fingerprints`: `id`, `track_id`, `scene_id`, `gates_per_lap`,
  `gates` JSON, `source` (`learned` | `labelled`), `owner` (node_id or user
  id for learned, "" for labelled), `origin_race_uuid`, `created_at`.
- `core/trackcheck.py`: `rank` and `decide` already take candidates as plain
  geometry; `web/identify.py` loads the registry for a gate count (cached,
  invalidated on write) and runs them at ingest, with the thresholds the
  phase 1 spike confirmed. Clear winner → `track_id`,
  `session_source = matched`. Otherwise `track_id` 0 and the run is
  *unidentified*.
- Learning: `update_race` and bulk edit, when they set a `track_id` on a run
  with a fingerprint, insert a `learned` row for that track if no row within
  threshold exists. A *Re-run identification* button on the review queue
  re-ranks every unidentified run.
- Review queue: `/races?unidentified=1` grouped by node, gate count and day,
  with the existing bulk-edit picker, and a "no IMU" note on nodes whose
  `imu_seen_at` is stale. Twins: when `decide` finds two winners that are
  the same layout under different scenes, attribute to the
  `(track_id, scene_id)` the same node flew most recently and set
  `track_check_note = ambiguous` for the queue to show.
- No fingerprint (no IMU): queued by gate count and day only, never
  auto-attributed.

### Tests

- `service --dry-run` renders the right unit/service definition per
  platform; `node.port` is written on bind and removed on exit.
- IMU warning flips on at the first crossing without frames and off on the
  first frame; one notification per session.
- Identification: registry with two tracks, a run whose fingerprint matches
  one is attributed at ingest; an unmatched run is unidentified; attributing
  it teaches the registry and the next identical run matches; twins are
  flagged ambiguous.

### Done when

A PC with the installer run once records every run from boot with nobody
touching it, the window opens on the running service, and the web
attributes the runs after the pilot has labelled each track once.

## Phase 4: label clusters of unknown layouts (#16) — built

Built 2026-09-26: clustering, the by-layout view with a map per cluster and
the picker, *Label* and *Not a track*, the track page's known-layouts box
with *Forget*. Not built: *Add twin*, *Merge* / *Split* (label each twin's
cluster to its own track instead; a mis-clustered run is fixed run by run).

Trimmed from a separate admin portal to actions on the review queue: on a
single-user web the two would be the same page. The admin role and
`/admin` pages move to phase 6 with multi-user.

- `web/clusters.py` (pure, tested): group unidentified fingerprints into
  clusters with the thresholds from the phase 1 spike (greedy by centroid:
  each fingerprint joins the first cluster whose centroid it is within
  threshold of, same gate count; centroid = mean gate positions).
  Deterministic order by first seen.
- The review queue gains a *By layout* view: one card per cluster with the
  top-down map (reuse the track page's SVG), gate count, runs, distinct
  nodes, first/last seen, and the picker. Actions: *Label* (track +
  optional scene) writes a `labelled` registry row from the centroid and
  re-runs identification for the cluster's runs; *Add twin* adds a second
  `(track_id, scene_id)` to the same row; *Not a track* (`track_id = -1`)
  hides the cluster and leaves its runs at `track_id` 0 without re-queuing
  them; *Merge* / *Split* move fingerprints between clusters.
- Track page: a *Layouts* box listing the track's registry rows, learned
  and labelled, with *forget* for a wrong one.
- Registry rows are anonymous geometry; the view shows gate centroids only,
  never a run's telemetry. That is what lets the same rows be shared across
  users in phase 6.

### Tests

- Clustering: two runs of the same track cluster; a run beyond threshold
  does not; gate count separates otherwise identical layouts.
- Labelling a cluster attributes all its runs and the next arrival; *Not a
  track* removes the cluster and keeps the runs at `track_id` 0 without
  re-queuing them.

### Done when

On a fresh web, the first ten runs across three tracks land in three
clusters, and three labels put every one of them on the right track page.

## Phase 5: relay (#14, optional)

Unchanged from the design note. Node opens an outbound websocket to
`<upstream>/ws/relay` with the token, mirrors `LiveHub` messages, accepts
`session` and `abort` frames and routes them to the existing handlers. Web
gets `/live/<node>` and a *Pick the track for gaming-pc* action. Never
required for a run to record.

## Phase 6: multi-user (#15)

`user_id` on `races`, `track_sections`, `nodes` and the web-side tables;
per-user ingest tokens; `PBKey` gains `user_id`; `is_admin` and the
`/admin` pages (the cluster view for every user's unidentified runs, and
*promote* to turn one pilot's `learned` row into a global label); the
fingerprint registry is the one global table. Hosting is a decision for
after this works self-hosted.

## Cross-cutting

- **Versions.** Phase 1 = 0.6.0, and `splitter_version` is in every document
  so the web can refuse documents newer than it understands (`DOC_VERSION`
  is what is checked; the app version is for the log).
- **Docs.** CLAUDE.md gets an *Architecture: node and web* paragraph per
  phase; `deploy/lxc/README.md` and `desktop/README.md` gain the upstream
  and service setup; `docs/beta-guide` gets "record on the PC, look on the
  web".
- **Tests never reach the network.** The uploader and the relay take
  clients; `app.state.catalog` keeps taking a fake.
- **Data safety.** Deletion of a local run, whether by `keep_local_runs`
  off or the purge button, happens only after a 2xx ack that echoes the
  uuid. `splitter export --all` is documented as the backup before
  `migrate-telemetry --drop`.
- **Signing.** An unsigned installer trips SmartScreen the way an unsigned
  exe trips Defender; the options in `docs/code-signing.md` are unchanged
  and phase 3 is when it starts to matter.
- **Out of scope for this branch.** Accounts, hosting, billing, the relay.
  The branch lands phase 1 and is cut per phase after that.
- **Later: https from Splitter itself (#21).** A node on `http://<lan-ip>:8100`
  cannot be installed as a PWA from a phone (secure context). Settings would
  grow a TLS block: off, a custom certificate and key handed to uvicorn, then
  ACME against Let's Encrypt or an internal CA. Documented in the issue, not
  built; the LXC keeps Caddy in front.

## Next session on the gaming PC (Windows)

What is left needs a Windows machine with the game. In order:

1. **Build the branch** (`feature/cloud-sync`): `python desktop/sidecar/build.py`,
   then `npx -y @tauri-apps/cli@2 build --no-bundle` in `desktop/src-tauri`,
   or push a `dev-*` tag and take the CI artifacts.
2. ~~DONE 2026-09-27 (dev-2026-09-27c against the LXC on 0.8.0-dev): push, match, label, recognise all passed.~~ **Node against the LXC.** On the LXC (upgrade it with the bundle from the
   same build): Settings → *Receive runs* → turn on, copy the token. On the
   PC's portable build: Settings → *Send runs to another Splitter* → the LXC
   address and token → *Test connection*. Fly a run: it should appear on the
   LXC's Races page within seconds with "Receiving from <PC name>", and the
   PC's header *Web* indicator should read *sent*. Then clear the track on
   the PC (or pick nothing) and fly the same track: the LXC should attribute
   it (`matched`). Fly a new track: it should land under *no track yet*,
   label it once on `/races/layouts`, fly it again, check it is recognised.
3. ~~DONE 2026-09-27 on the LXC (62 fingerprinted runs, 5 tracks): same-track median 0.9 m, p95 1.2 m, max 1.6 m against 12 m; no comparable cross-track pairs yet (different gate counts).~~ **Threshold check** on the LXC's real data: `splitter fingerprint-stats`
   (after `splitter backfill-fingerprints`). Same-track pairs should sit well
   under 12 m and cross-track pairs well over; if not, adjust
   `trackcheck.DIFFERENT_M` / `CLOSE_M` before trusting identification.
4. ~~DONE 2026-09-27 on recon-xps (Windows 11, PyInstaller 6 / Python 3.12): pywin32 is in the bundle; `sc start` → RUNNING in 4 s (no SCM timeout), `node.port` written, `/healthz` answers, `sc stop` → STOPPED and the marker is removed. The one real failure was uvicorn's logging config under a console-less process (`sys.stdout` is None) — fixed in `desktop_entry._run_as_windows_service` (file log in the data dir, `log_config=None`). The DACL is verified too: on 2026-09-27 Ryan stopped and started the service from a plain, non-admin prompt on the gaming PC. **Then the gaming PC failed** (event 7009, 1053: 30 s SCM timeout, `service.log` never written). Not slowness — `--help` there takes 0.6 s — but the **CI-built sidecar has no pywin32** (the laptop build had it installed by hand; the build never adds it), so `--service` exited with "service mode needs pywin32" before ever registering, and a process that quits before connecting looks to the SCM exactly like one that never answers. Rather than bundle pywin32, the service binary is now the **Tauri shell** (`splitter-desktop.exe --service`, `lib.rs::service_host`, `windows-service` crate): it reports Running at once and supervises the sidecar, restarting it on exit. Note: `service install` is a subcommand of the `splitter` CLI (`splitter service install --exe <sidecar.exe>`), not of the sidecar, which only takes `--service`.~~ **Windows service spike.** In an elevated prompt:
   `splitter-sidecar.exe service install --dry-run` to review, then without
   `--dry-run`. Watch for the SCM 30 s timeout ("did not respond") — that is
   the pywin32-under-PyInstaller question. If `sc start Splitter` works and
   `node.port` appears in `%ProgramData%\Splitter`, service mode is fine;
   if not, switch `windows_commands` to a WinSW wrapper. Check `sc stop`
   works as a plain user (the DACL). Note: PyInstaller must bundle pywin32
   (`pip install pywin32` before `build.py`; add `servicemanager`,
   `win32serviceutil` as hidden imports if the build drops them).
5. ~~DONE 2026-09-27 (recon-xps): NSIS bundle on (`perMachine`, `nsis/hooks.nsh`); the hooks call the shell's headless `--install-service` / `--remove-service`, which unpack the embedded sidecar into `C:\ProgramData\Splitter\bin\splitter-sidecar-<ver>` and run the sidecar's `service install|remove`. Silent install → service AUTO_START RUNNING, `/healthz` 200, Add/Remove entry; silent uninstall → service gone, program gone, data kept. The sidecar stays *embedded* (no resources folder needed: the hook unpacks it to a stable path itself).~~ **Installer.** `tauri.conf.json`: enable the NSIS bundle, `installMode`
   `perMachine`, an `installerHooks` NSI with `NSIS_HOOK_POSTINSTALL` running
   `service install`, `NSIS_HOOK_PREUNINSTALL` running `service remove`, and
   pre/post-update stop/start. Move the sidecar from *embedded archive* to a
   bundled `resources` folder at a stable path for the installer build.
6. ~~DONE 2026-09-27 (`lib.rs::running_node`): the shell probes `node.port` in the service data dir and its own, attaches when `/healthz` answers, else spawns as before. The *Service not running* page with a Start button is not built — a stopped service just means the portable sidecar starts.~~ **Shell attach.** `lib.rs`: before unpacking, read `<data>/node.port`; if
   `http://127.0.0.1:<port>/healthz` answers, open the window there and skip
   the sidecar; else the *Service not running* page with a *Start* button
   (`sc start Splitter`).
7. *(Reframed 2026-09-27: the installer cannot know where a portable copy kept its data. Use the Races page's Export / Import between the portable folder and the installed service instead; a one-click "import from a portable Splitter folder" can come later if it is missed.)* **Data-dir migration.** First service start with an empty database copies
   `splitter-data/splitter.db` from the portable install if the installer
   recorded its path.
8. **Missing-IMU OS notification** from the service or the shell (header
   warning already exists).

Everything above is tracked in #13; the branch is otherwise ready to merge
after the node-against-LXC check in step 2 passes.

## First steps on this branch

1. `core/rundoc.py` and the columnar telemetry codec, with tests.
2. Schema columns and the backfill in `init_db`.
3. Controller: uuid at GO, fingerprint and blob at finish (seq waits for
   the outbox in phase 2).
4. `repos.import_run`, export/import endpoints, CLI commands.
5. Startup telemetry migration, the threshold spike script, docs, 0.6.0.
