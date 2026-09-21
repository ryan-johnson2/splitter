# Design note: capture nodes, a web server, and sync between them

Status: proposal (2026-09-21). Tracked as #10 (umbrella) and #11–#15 on GitHub.

## Why

Today Splitter is one process on one machine: capture, storage and analysis
in a single SQLite file next to the game (desktop) or in an LXC on the LAN.
That means results live wherever the capture happened, the local database
grows with every traced run, and a pilot who only wants their runs recorded
still has to run the full app with its pages.

The idea: split it into a **node** that captures and a **web** that stores
and analyses. A node pushes finished runs up; the web is where every result
lives and where every analysis page is. A node can be the full app (live
page on a tablet, as now) or a headless background service with no UI at all.
The web can be self-hosted (the existing LXC / Docker deployment) or, later,
a hosted service.

## The seam

The split falls on a boundary the code already has:

| stays on the node                                   | moves to the web                                   |
|-----------------------------------------------------|----------------------------------------------------|
| `game/bridge.py` (the only thing that talks to the game) | Races, Tracks, Track detail, Race detail pages   |
| `game/controller.py`, all of `core/`                | `web/analysis.py` (sections, geometry, trends)     |
| live hub and live page (`/ws/live`, `/`)            | edit, delete, bulk re-attribution                  |
| crash detection (needs the full 60 Hz buffer)       | PB flags (`recalculate_best`), progression         |
| per-segment min/max speed and accel (crossing time) | the online track picker (`velocidrone-tracks`)     |
| capture pause, abort, getting-started card          | Settings that are about the account, not the link  |

Two consequences worth calling out:

- **The live page stays local.** Splits over the internet would be fast
  enough, but flying must not depend on the internet being up. A node keeps
  `/ws/live` and the tablet page; it does not keep the analysis pages.
- **The private tracks client moves server-side.** The desktop build no
  longer needs the Cython-protected private wheel; the picker on the node's
  live page proxies through the web (or, see below, the web *is* where the
  track is picked).

## Direction of data

**Push, never pull, for results.** The gaming PC is behind NAT and often off,
so the web can never reach a node. A node uploads each finished or aborted-
with-crossings run as one document and forgets it once the web acknowledges.

**Pull only for what the live page needs at GO**: the reference bundle for
the run's `PBKey` (PB crossings for live splits, gate geometry for the
track-change check). The upload response carries the updated bundle for that
key, so a node that is uploading stays fresh without polling; an explicit
`GET /api/reference?...` covers cold starts and keys the node has never
uploaded to.

What a node stores after this: settings, an **outbox** of unacknowledged
runs, and a **reference cache**. That, not a smaller telemetry table, is what
keeps the local database small. A headless node stores even less: it has no
live page, so it needs no references at all (see "Headless and the map").

## Identity and ordering

Everything about ordering follows from two rules.

1. **Every run has an identity minted where it was captured.** A UUID (or
   ULID, sortable by time) assigned at GO, plus a `node_id` and a per-node
   `seq` counter. Upload is `PUT /api/ingest/runs/{uuid}` and idempotent: a
   retry, a duplicate, or a laptop catching up after a week are no-ops or
   plain inserts. `reference_race_id` references the UUID, never a row id.
2. **The web never derives anything from arrival order.** `is_best` is
   re-flagged per key from stored rows after every ingest (exactly what
   `recalculate_best` does today). Progression, trends and "last 3 vs the 3
   before" sort by `started_at` (node clock; `received_at` is kept separately
   so skew is visible but never used for ordering). Sections and gate
   geometry recompute from whatever runs exist.

Two things are **historical facts and must not be recomputed** when an older
run arrives late and turns out to have been the true PB: `reference_race_id`
and `pb_delta_ms` say what the run was compared against *at the time*. A run
from a headless node has neither (no reference at GO); the web fills them in
against the PB as of ingest and marks them `derived`.

The per-node `seq` lets the web say "3 runs still pending from the laptop"
instead of showing a silent gap, and lets a node detect a lost outbox.

Edits (track, quad, laps, notes) happen only on the web, after the run has
been acknowledged, so there is no two-way merge. The node's "Track…" dialog
sets the *session*, which is pre-race state, not a run edit. Deletion is a
web action; a node's abort-with-zero-crossings never uploads anything.

## The run document

One JSON document per run, which is also the export/import format and the
first thing to build (it makes "results anywhere" work by hand before any
networking exists):

```
{
  "uuid": "...", "node_id": "...", "seq": 412, "splitter_version": "0.6.0",
  "race": { ...every Race column except id/is_best... },
  "laps": [ ... ], "gate_times": [ ... ],
  "crashes": [ ... ],
  "session": { "source": "manual|game|sticky|matched|none", "track_id": 0, ... },
  "fingerprint": { "gates_per_lap": 9, "lap1_gates": [[x,y,z], ...] },
  "telemetry": { "hz": 20, "columns": [...], "data": "<base64 zstd float32>" }
}
```

Telemetry as one compressed columnar blob instead of one row of 16 floats per
sample: the `telemetry` table is the bulk of the current database (roughly
150 KB per traced run before index overhead) and the same blob is the upload
payload. The race page reads it through `repos.telemetry_for_race`, which
keeps its signature and decodes the blob.

## Headless and the map

A background service with no UI cannot show the "Track…" dialog, and single
player never names the track (see CLAUDE.md, "Single player never names the
track"). Three mechanisms, in order of how much they carry:

1. **Server-side identification by fingerprint.** The track-change check
   already turns lap 1 into a fingerprint (`gates_per_lap` + lap-1 gate
   positions from the IMU) and ranks it against every known track with a
   traced run of that gate count (`trackcheck.rank`, `trackcheck.decide`).
   Move that ranking to ingest on the web, over *all* the tracks the web
   knows. Every run carries its fingerprint; a clear winner is attributed
   (`session_source` `matched`), anything else lands **unidentified**
   (`track_id` 0, never a PB) in a review queue.
2. **The web learns from one attribution.** The first run on a new track
   cannot be matched. The pilot attributes it once on the Races page (bulk
   edit exists) and from then on every run with that fingerprint matches.
   On a hosted web with many users the fingerprint set is shared, so a new
   user's runs on any track anyone has flown identify on arrival.
3. **Node-side sticky as an opt-in.** Carrying the last track over is a
   guess that silently mis-attributes PBs; on a headless node it is off by
   default and the review queue is the truth. Hosted multiplayer rooms still
   name the track (`session` event) and are attributed at capture.

Limits to state plainly:

- Fingerprinting needs telemetry (IMU opt-in in the game, Betaflight FC
  only). Without it a run carries only its gate count and lands in the
  review queue grouped by gate count and time, which is still a quick bulk
  edit.
- **Twins** (the same layout in day and night scenes) share a fingerprint.
  Geometry cannot resolve the scene. Attribute to the twin the pilot used
  last, flag it as ambiguous in the queue.
- A headless node has no live splits, by definition. Anyone who wants splits
  runs the full node with the live page; the difference is a flag, not a
  different program.

### Optional: a relay

A node can hold an *outbound* websocket to the web and mirror its live-hub
messages up it, and accept `session` and `abort` commands back down. That
gives the web a live view and lets the pilot pick the track from the web
before a headless run. It is the same `LiveHub` fan-out with one more client,
and the same `POST /api/session` handler on the node. Nice to have; not
needed for identification, and it must never be required for a run to
record.

## Hosting

Storage is not the issue: a heavy user is tens of MB a year with columnar
telemetry. Accounts, abuse and being on the hook for uptime are. So:

- The **web** is the existing LXC / Docker deployment with a single-user
  ingest token in settings. Self-hosting stays the first-class, AGPL path.
- Multi-user is a layer on that same image (accounts, per-user ingest
  tokens, `user_id` on every table, per-user PB keys), and hosting is running
  that image somewhere with a domain. Shared fingerprints across users are
  the one cross-user feature and are anonymous geometry, not runs.
- A node is configured with `upstream_url` + token; the same node talks to a
  self-hosted or a hosted web without knowing which.

## Order of work

1. **Run identity and the run document.** UUID / node_id / seq on `races`,
   columnar telemetry blob, `GET /api/races/{id}/export`, `POST /api/import`
   (idempotent). Ships on the current single-process app; results can be
   moved by hand.
2. **Push.** `upstream_url` + token settings, outbox with retries, ingest
   endpoint, reference bundle in the upload response and on `GET`. The
   current app is both node and web; a second instance can be the web.
3. **Headless node.** `splitter node`: bridge + controller + outbox, no
   templates, one status line. Server-side fingerprint identification and the
   review queue on the web.
4. **Relay** (optional), then **multi-user**, then hosting if still wanted.
