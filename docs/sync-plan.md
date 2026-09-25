# Implementation plan: capture nodes, web server, and sync

Status: plan (2026-09-25), branch `feature/cloud-sync`. The design is
`docs/sync-design.md`; this is how it gets built, phase by phase, against the
code as it stands at 0.5.2. Each phase ships on its own, keeps the current
single-process app working, and is tracked by one GitHub issue.

| phase | issue | ships as | outcome                                                          |
|-------|-------|----------|------------------------------------------------------------------|
| 1     | #11   | 0.6.0    | every run has an identity and a document; move results by hand   |
| 2     | #12   | 0.7.0    | a node pushes runs to an upstream web; local DB stays small      |
| 3     | #13   | 0.8.0    | headless node from logon; the web identifies tracks on arrival   |
| 4     | #16   | 0.9.0    | admin portal labels the map                                      |
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
- **The app stays one package and one binary.** Node, web, headless and
  admin are what a given install has configured and which routers are
  mounted, never separate programs. `create_app` grows keyword flags.
- **No two-way sync, ever.** Edits and deletes happen on the web after the
  run is acknowledged. A node never re-uploads an acknowledged run.

## Phase 1: run identity and the run document (#11)

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
  `_check_track` uses and store it (also on aborts with crossings). Set
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
  `splitter import FILE…`, `splitter migrate-telemetry [--drop]` (rows →
  blobs, then optionally drop the table), `splitter backfill-fingerprints`
  (from stored traces, like `backfill-crashes`).
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

## Phase 2: push to an upstream web (#12)

### Node side

- Settings: `upstream_url`, `upstream_token`, `keep_local_runs` (`0` once an
  upstream is set), `node_name`. Settings page section *Upstream* with a
  *Test connection* button (`GET <upstream>/api/ingest/ping` with the token).
- Table `outbox`: `race_uuid` PK, `queued_at`, `attempts`, `next_at`,
  `last_error`, `acked_at` nullable. A row is added in `_finish_race` for
  every finished or aborted-with-crossings run. Survives restarts.
- `sync/uploader.py::Uploader`: supervised task next to the bridge (same
  `_supervise`). Loop: pick due rows oldest first, build the document, `PUT`
  it, on 2xx mark acked, store the returned reference bundle in the cache,
  and delete the local run unless `keep_local_runs`. Backoff 5 s → 5 min.
  Wakes immediately on a new outbox row (an `asyncio.Event`).
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
  `repos.import_run` with `origin=ingest` and `received_at=now`, per-key
  `recalculate_best`, response `{status, reference: bundle}`.
  `GET /api/ingest/ping` for the test button. `GET /api/reference` for cold
  starts (also served on a node from its own DB, so the app is symmetric).
- The reference bundle = `Reference` crossings for the key + `track_geometry`
  for the track, in `core/splits.py` shape so the node can load it straight
  into `Reference`.
- Table `nodes`: `node_id` PK, `name`, `last_seen_at`, `max_seq`. Ingest
  updates it; the Races page shows "n runs pending from *name*" when
  `max_seq` minus the count of runs with that node_id is positive, and lists
  the gaps in a tooltip.

### Tests

- `test_sync.py`: node app and web app in one process, uploader given an
  `ASGITransport` client. A finished run appears on the web within one loop
  iteration; a 500 from the web keeps it queued with backoff; a duplicate
  `PUT` is a no-op; after ack the node has no race row when
  `keep_local_runs` is off; the reference cache holds the bundle from the
  response and `refresh_reference` uses it with the DB empty.
- Auth: wrong token → 401, nothing written.

### Done when

The LXC is the web, the desktop app is a node, and a run flown on the PC
shows up on the LXC's Races page in seconds with the PC keeping only its
outbox and reference cache.

## Phase 3: headless node from logon, identification on the web (#13)

### Node

- `create_app(headless=True)`: no templates, no static mount, no page
  routers; keeps `/healthz`, `/api/state`, `/api/session`, `/api/capture`,
  `/api/race/abort` and `/ws/live` (the relay in phase 5 needs them, and a
  tablet can still hit the JSON). `splitter node` and `splitter-sidecar
  --headless` both call it. Log one line per run: "run 412 finished 71.2 s,
  uploaded".
- Sticky session off by default in headless mode (`sticky_session` setting,
  default `0` there, `1` in the full app).
- `splitter service install | remove | status`: Windows → `schtasks` at
  logon running `splitter-sidecar --headless --data-dir …`; Linux → systemd
  user unit; macOS → launchd agent. Writes `<data_dir>/node.pid` and
  `node.port` while running.
- Desktop shell: on launch, if `node.pid` is alive, open the window at that
  port instead of spawning a sidecar (`lib.rs`). Settings page toggle *Run in
  the background at logon* calls install/remove through a new
  `POST /api/service`.
- Runs from a headless node upload with `reference_uuid` null and
  `pb_delta_ms` null; the web fills both against the PB as of ingest and
  sets `delta_source = derived` (new column, display only).

### Web

- Table `track_fingerprints`: `id`, `track_id`, `scene_id`, `gates_per_lap`,
  `gates` JSON, `source` (`learned` | `labelled`), `owner` (node_id or user
  id for learned, "" for labelled), `origin_race_uuid`, `created_at`.
- `core/trackcheck.py`: `rank` and `decide` already take candidates as plain
  geometry; `web/identify.py` loads the registry for a gate count (cached,
  invalidated on write) and runs them at ingest. Clear winner → `track_id`,
  `session_source = matched`. Otherwise `track_id` 0 and the run is
  *unidentified*.
- Learning: `update_race` and bulk edit, when they set a `track_id` on a run
  with a fingerprint, insert a `learned` row for that track if no row within
  threshold exists. A *Re-run identification* button on the review queue
  re-ranks every unidentified run.
- Review queue: `/races?unidentified=1` grouped by node, gate count and day,
  with the existing bulk-edit picker. Twins: when `decide` finds two winners
  that are the same layout under different scenes, attribute to the
  `(track_id, scene_id)` the same node flew most recently and set
  `track_check_note = ambiguous` for the queue to show.
- No fingerprint (no IMU): queued by gate count and day only, never
  auto-attributed.

### Tests

- Headless app serves JSON and no HTML; `service` command renders the right
  unit/task text per platform (dry-run flag, no registration in tests).
- Identification: registry with two tracks, a run whose fingerprint matches
  one is attributed at ingest; an unmatched run is unidentified; attributing
  it teaches the registry and the next identical run matches; twins are
  flagged ambiguous.

### Done when

A PC with only the background node installed records every run, and the
web attributes them without anyone touching the PC, after the pilot has
labelled each track once.

## Phase 4: the admin portal (#16)

- Role: `is_admin` on the single-user web is implicitly true; multi-user
  (phase 6) adds the column. Routes under `/admin`, hidden from the nav for
  non-admins, 403 otherwise.
- `web/clusters.py` (pure, tested): group unidentified fingerprints into
  clusters with the trackcheck thresholds (greedy: each fingerprint joins the
  first cluster whose centroid it is within 12 m of over ≥ 4 gates, same
  gate count; centroid = mean gate positions). Deterministic order by first
  seen.
- `/admin/fingerprints`: one card per cluster: map (reuse the track page's
  top-down SVG), gate count, runs, distinct nodes, first/last seen, and the
  picker. Actions: *Label* (track + optional scene) → writes a `labelled`
  registry row from the centroid and re-runs identification for the
  cluster's runs; *Add twin* → a second `(track_id, scene_id)` on the same
  row; *Not a track* → a `labelled` row with `track_id = -1` so the runs stop
  appearing and stay unranked; *Merge* / *Split* by moving fingerprints
  between clusters.
- `/admin/tracks`: every labelled track with its fingerprint count, learned
  vs labelled, and a *promote* action that turns a learned row into a global
  label (this is how one pilot's attributions become everyone's on a hosted
  web).
- Registry rows are anonymous geometry; the portal never shows a run's
  telemetry, only gate centroids.

### Tests

- Clustering: two runs of the same track cluster; a run 20 m off does not;
  gate count separates otherwise identical layouts.
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

Unchanged. `user_id` on `races`, `track_sections`, `nodes`, `outbox`-free
tables; per-user ingest tokens; `PBKey` gains `user_id`; the fingerprint
registry is the one global table. Hosting is a decision for after this
works self-hosted.

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
- **Data safety.** `keep_local_runs` off deletes only after a 2xx ack that
  echoes the uuid. `splitter export --all` is documented as the backup
  before `migrate-telemetry --drop`.
- **Out of scope for this branch.** Accounts, hosting, billing, the relay.
  The branch lands phase 1 and is cut per phase after that.

## First steps on this branch

1. `core/rundoc.py` and the columnar telemetry codec, with tests.
2. Schema columns and the backfill in `init_db`.
3. Controller: uuid/seq/fingerprint at GO and finish; blob at finish.
4. `repos.import_run`, export/import endpoints, CLI commands.
5. `migrate-telemetry`, docs, 0.6.0.
