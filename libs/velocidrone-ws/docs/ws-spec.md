# VelociDrone WebSocket Protocol Specification

**Game version: VelociDrone 1.17.13** — see [Version identification](#version-identification).
Spec revision: 2026-08-29 (full rewrite against the 1.17.13 binary).

Reverse-engineered from `Assembly-CSharp.dll` using `dnre-mcp` (ILSpy-based decompilation).
This is the authoritative, binary-derived description of VelociDrone's undocumented local
WebSocket interface: the race-event feed and the remote-control command surface.

## Provenance & confidence

Every claim in this document is tagged with its source:

- **[BINARY]** — read directly from the decompiled 1.17.13 game code (the strongest class:
  it is what the shipped code *does*).
- **[LIVE]** — confirmed against a running sim (velocistream `devel`'s controller ran the
  full seat → start → ingest → results loop over this protocol).
- **[CAPTURE]** — observed in the dargust/VDSplitViewer `messages.log` wire capture
  (2025-01-23, a pre-1.17 build; used only for historical/legacy shapes).
- **[SCENE]** — a value that lives in Unity *scene/prefab serialization*, not in the DLL.
  The Managed folder alone cannot prove it; noted wherever it matters.

Analysis inputs:

| Artifact | Value |
|---|---|
| Binary | `Assembly-CSharp.dll`, 26,760,704 bytes, file date 2026-04-28 |
| sha256 | `bf66b32070606b2a442b890b466bea8f48fd57f129791037444835794694bfa7` |
| Source drop | `~/development/fpv/gridfpv/velocidrone-game/Managed/` (full Managed folder) |
| Tooling | `dnre-mcp` decompiler over the loaded assembly (4,531 types) |

The game code is obfuscated two ways, and reading it requires knowing both: identifiers are
random 11-char strings (`nooongbkkcb` = JSON object class, etc.), and the obfuscator emits
**decoy clones** of real methods stuffed with garbage string literals (e.g. a fake
`raceStatusUpdate("ESC3: ")` next to the real `raceStatusUpdate("start")`). Decoys are
identified by nonsense arguments/behavior and are called out below where they previously
poisoned this spec.

## Version identification

The binary carries the display-version strings **`1.17.13`** (twice) and `1.17.1`; no other
`1.x.y` game-version strings exist in it. Triangulation:

- DLL file date **2026-04-28** — eleven days after the vendor's 2026-04-17 news entry
  "Add IMU data to websocket", and the IMU feature **is present** in this binary
  (`SocketManager.sendIMUData`, the `web-socket-imu` setting) [BINARY].
- The 2026-01-20 `ping` command (vendor news) is present: `"ping"` is an accepted no-op in
  the command dispatcher [BINARY].
- `Authorize.simVersionLevel = "1.16.0."` also appears — do **not** mistake it for the game
  version; it is a stale internal prefix used for `user_sim_version` fields in HTTPS auth
  calls (with the scene count appended), unrelated to the display version [BINARY].

Final confirmation is one glance at the main-menu version label on the install this Managed
folder was copied from; everything below is stated against 1.17.13.

## Overview

VelociDrone runs a **local WebSocket server** inside the game for overlays, timing tools and
race control. External clients connect to it; the game pushes JSON race events and accepts
JSON commands.

- **Server**: the game is the server; tools are clients [BINARY]
- **Port**: `60003` (code default; also the value every known consumer uses) [BINARY][LIVE]
- **Path**: `/velocidrone/` on current builds [LIVE]; see [Service path](#service-path)
- **Binding**: the machine's LAN IP — **not** loopback; `ws://127.0.0.1:…` does not connect [LIVE]
- **Protocol**: WebSocket (RFC 6455), JSON text frames, one object per frame [BINARY]
- **Enabling**: Options → Main Settings → Websocket Communication → Yes
  (`use-web-socket` sim_state in the settings DB; read once at startup) [BINARY]
- **IMU feed**: separately gated by the `web-socket-imu` sim_state (Options toggle) [BINARY]

### The single-connection reality

`UnityWSServer` is configured for `_maxConnections = 100`, **but `SocketManager` tracks
exactly one connection**: on every new client, `myConnection` is overwritten and
`connectionCount` set to 1; every event send goes to `myConnection` only [BINARY]:

```csharp
public void OnWSSNewConnection(WSConnection connection, UnityWSServer server)
{
    myConnection = connection;
    connectionCount = 1;
}
public void sendMessage(string message)
{
    if (websocketOn && connectionCount == 1) { myConnection.SendData(message); }
}
```

Consequences for consumers:

- **The newest connection wins.** An older client stays connected at the socket level but
  silently stops receiving events. Run ONE consumer per game instance, and treat "connected
  but silent" as possibly-usurped.
- When any tracked connection closes, `connectionCount` drops to 0 and events stop until a
  client (re)connects. `newRoomCreated` attempts a server `Connect()` if no client is
  tracked [BINARY].

### Lifecycle

1. Player enables the websocket setting; `SocketManager.Start()` reads `use-web-socket` and
   `web-socket-imu`, sets up `UnityWSServer`, and connects (listens) if enabled [BINARY].
2. A client connects to `ws://<lan-ip>:60003/velocidrone/`.
3. The game pushes events (below); the client may send commands.
4. Toggling the setting off calls `Disconnect()`; the server also disconnects on quit [BINARY].

## Service path

Two path strings exist, and both are real:

- The `UnityWSServer` **code default** is `_service = "ws"` (→ `ws://<ip>:60003/ws/`), and
  the in-game developer test client (`WS_test.GetDefaultURL`) builds exactly that URL —
  including `[bracketed]` IPv6 hosts [BINARY].
- The **shipped configuration** is `_service = "velocidrone"`: `_service` is a public
  serialized MonoBehaviour field, so the scene/prefab value overrides the code default
  [SCENE], and `/velocidrone/` is the endpoint that actually connects on current builds
  [LIVE] and the one all working community consumers use.

To pin the shipped value statically we would need the Unity scene data (`globalgamemanagers`
/ `levelN` / `data.unity3d`), which a Managed-only drop does not include. Until then:
**connect to `/velocidrone/`**; treat `/ws/` as the legacy/code-default path.

## Transport details

The server stack is the eToile "SocketsUnderControl" asset (`Server:
SocketsUnderControl.WSServer` in its responses): a hand-rolled RFC-6455-ish implementation
over raw TCP. It deviates from the RFC in ways a faithful client (or mock) must know. All
of this section is [BINARY], decompiled from `WSServer`, `WSServer.WSClient`,
`WSServer.HandShake` and the TCP wrapper.

Defaults from `UnityWSServer`:

```csharp
public int    _port = 60003;
public string _service = "ws";          // overridden by scene config, see above
public int    _maxConnections = 100;
public float  _keepAliveTimeout = 40f;  // seconds
```

### Binding

With the default empty local-IP, the TCP layer binds to the machine's **specific primary
LAN IPv4 address** — discovered by "connecting" a UDP socket to `8.8.8.8:65530` and reading
the local endpoint — not `IPAddress.Any` and not loopback; if a default IPv6 address exists
a secondary listener binds to it too. Listen backlog 10. This is *why*
`ws://127.0.0.1:60003` never connects. On a machine with no default route (offline), the
probe fails and the server has no usable v4 bind address.

### Connection acceptance

The TCP/WS layer accepts up to `_maxConnections` (100) concurrent clients — each gets a
full handshake; client #101 is accepted and immediately shut down. The **one-served-client
behavior lives above this**, in `SocketManager` (see [The single-connection
reality](#the-single-connection-reality)): extra clients handshake fine and then receive
nothing.

### Handshake (server side)

The client's request must arrive within **3 seconds** of the TCP connect, or the connection
is aborted. Parsing is line-and-space split with quirks:

- The GET target is taken as the second space-separated token of the `GET` line and
  stripped of **all** leading/trailing `/`; it must then equal the configured service
  string **exactly** (ordinal, case-sensitive). `/velocidrone`, `/velocidrone/`,
  `//velocidrone//` all match `velocidrone`; `/Velocidrone` does not, and a query string
  (`/velocidrone?x=1`) breaks the match.
- Only the **first** space-separated token after each header name is read. Consequence:
  `Connection: keep-alive, Upgrade` **fails** (the parser sees `keep-alive,` only); send
  `Connection: Upgrade` (standard clients do).
- Required: `Upgrade: websocket` (exact, lowercase), `Sec-WebSocket-Version: 13` (exact),
  a non-empty `Sec-WebSocket-Key`, a non-empty service. Subprotocols and extensions are
  never parsed or echoed.
- Accept key: standard RFC 6455 — `base64(SHA1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))`.

Success response, verbatim (header order fixed):

```
HTTP/1.1 101 Switching Protocols\r\n
Server: SocketsUnderControl.WSServer\r\n
Connection: Upgrade\r\n
Upgrade: websocket\r\n
Sec-WebSocket-Accept: <base64>\r\n
\r\n
```

(The `HandShake` class itself builds the response *without* the final blank line; the TCP
send wrapper holds a `\r\n\r\n` terminator during the handshake phase and appends it, then
the terminator is cleared and framing runs raw — verified through the whole send path.)

Failure (wrong path, bad headers): `HTTP/1.1 400 Bad Request` (+ `Server:` and
`Sec-WebSocket-Version: 13` headers), then the connection is aborted.

### Framing — the big deviations

1. **Every server→client message is a BINARY frame (opcode 0x2), FIN set, unmasked — even
   though the payload is UTF-8 JSON text.** A client that only processes text frames
   receives *nothing* from this server. (This is a live bug in gridfpv's
   `velocidrone/transport.rs`, which matches `Message::Text` only.)
2. Client→server data frames **must be masked** (RFC-conformant); an unmasked data frame
   draws an error and an immediate abort. Opcodes 0x0/0x1/0x2 are all accepted and treated
   identically as data — commands may be sent as text or binary frames.
3. Fragmentation is supported (payloads accumulate until FIN). No maximum payload size is
   enforced.
4. **Close (0x8)**: the server tears down the TCP connection (linger 0 ≈ RST) without ever
   sending a close frame, in either direction. Do not wait for a close reply.
5. **Ping (0x9)**: the reply is *not* an RFC pong — it is a normal binary **message** with
   the 2-byte payload `{0x8A, 0x00}`. Clients of this stack filter exactly that byte pair.
   Worse, the ping handler assumes a zero-length ping (it consumes exactly 6 bytes:
   2 header + 4 mask); **a ping carrying a payload desynchronizes the server's frame
   parser**. Don't send RFC pings at all; if you must, send them empty.
6. Pong (0xA) is consumed (same zero-payload assumption) and ignored.
7. An **empty masked data frame is echoed back** as an empty frame and never reaches the
   JSON/command layer.

Length encoding is standard (7-bit / 126+2BE / 127+8BE).

### Keep-alive & timeouts

The server enforces a **sliding idle timeout** (default 40 s): *any* inbound frame re-arms
it; expiry with no inbound traffic closes the connection (again: raw teardown, no close
frame). The server never sends heartbeats of its own.

Two keep-alives work on 1.17.13:

- `{"command":"ping"}` — the vendor-blessed form (2026-01-20 changelog); an explicit no-op
  `case` in the command dispatcher. **Use this.**
- An empty data frame — handled (echoed) at frame level, silently. This is what several
  older community tools send. It works, but the ping command is the documented path.

What actually caused the pre-2026 "log error → stutter" problem is any **non-empty,
non-command** frame (e.g. the literal `"heartbeat"` some tools sent): it reaches the JSON
layer, fails to parse or lacks `command`, and is logged every time. Avoid junk frames;
recommended keep-alive cadence 5 s.

## JSON serialization

The game uses an obfuscated bundled JSON library (`nooongbkkcb` = object, `faapjegofob` =
array, `pghcdipngog` = value, `ebhdgmnpagh` = kind enum). What matters on the wire:

- **Race-event scalars are JSON strings.** Every emitter stringifies at the call site
  (`value.ToString()` into the string overload of the value factory), so `lap`, `gate`,
  `position`, `countValue`, `raceLength`, `raceLaps`, booleans, times — all arrive quoted
  [BINARY]. C# `bool.ToString()` produces **`"True"` / `"False"`** (capitalized).
- **Two exceptions carry real JSON numbers**:
  - `racedata.<player>.uid` — the struct field is an `int` passed *without* `.ToString()`,
    so it hits the double overload and serializes as a bare number [BINARY]. (In
    `pilotlist` and `ActivateError` the uid IS a string — different call sites stringify.)
  - the entire `imu` payload — floats passed directly [BINARY].
- Key order is insertion order as listed per event below; output is compact JSON
  (no whitespace); duplicate keys last-wins; `true`/`false`/`null` lowercase unquoted
  (only reachable via the number/bool factory paths — the race events stringify their
  booleans first, hence `"True"`/`"False"`).

Two serializer bugs worth knowing [BINARY]:

- **Numbers serialize with `Double.ToString()` under the CURRENT CULTURE.** On a
  comma-decimal locale (German, French, …) a fractional number emits `1,5` — malformed
  JSON. In practice this can only bite the number-typed fields: `imu` values and
  `racedata.uid` (uid is integral, so it survives any locale; IMU floats do not). The
  *parser* side is invariant-culture, so inbound commands are locale-safe.
- **Strings are not escaped at all** — a value (or key) is emitted as `"` + raw + `"`.
  A player name containing `"` or `\` produces malformed JSON frames. (The parser *does*
  decode `\uXXXX` on input, but none of the standard short escapes.)

Parser tolerances (for commands you send): top-level must be an object; whitespace ok;
keys must be quoted; **trailing commas are accepted**; malformed input returns null and is
logged (never throws).

## Server → Client events

All events are one-key objects: `{"<event>": <payload>}`. Ordering below is the natural
race-lifecycle order.

### `session` — room created

**Trigger**: the local player **creates a multiplayer room** (`PhotonNetwork.CreateRoom`
succeeds in the create-room flow; `SocketManager.newRoomCreated` is called immediately
after) [BINARY]. It does **not** fire when joining someone else's room, and never in single
player — a consumer must not depend on it unless it is running on the room creator's
machine.

```json
{"session": {
  "playerName": "…", "sessionName": "…", "sceneryTitle": "…", "trackName": "…",
  "raceLength": "3", "RaceMode": "…", "quadType": "…", "quadSize": "…"
}}
```

All values strings (raceLength stringified) [BINARY]. Field order as shown.

### `racetype` — race format

**Trigger**: sent by `RaceManager.startRace()` immediately **after** the
`racestatus: "start"` frame, on every race start [BINARY].

```json
{"racetype": {"raceMode": "THREE_LAP_SINGLE_CLASS", "raceFormat": "NORMAL", "raceLaps": "3"}}
```

`raceMode` and `raceFormat` are the enum `ToString()` values of the current event mode and
race format; `raceLaps` is the stringified lap count [BINARY].

### `countdown` — start countdown

**Trigger**: the pre-race countdown in `RaceManager`'s update loop. **Multiplayer counts
5 → 4 → 3 → 2 → 1 → 0** at 1-second steps; single player starts the timer 3 s in, so it
emits **3 → 2 → 1 → 0** (matching the [CAPTURE] values). If the single-player countdown
setting is disabled, **no countdown frames are emitted at all** [BINARY]. `0` is GO.

```json
{"countdown": {"countValue": "3"}}
```

### `FinishGate` — track-shape flag (NOT a crossing event)

**Trigger**: emitted once, immediately after `countdown: "0"`, carrying the track's
`startFinishGateActive` flag [BINARY]. It describes whether the track has a distinct
start/finish gate — lap-derivation logic branches on it. It is **not** per-crossing data.

```json
{"FinishGate": {"StartFinishGate": "True"}}
```

Note the capitalized `"True"`/`"False"` (C# `bool.ToString()`).

### `racestatus` — lifecycle transitions

```json
{"racestatus": {"raceAction": "start"}}
```

The **complete** `raceAction` vocabulary in 1.17.13, with emitters [BINARY]:

| Literal | Emitter |
|---|---|
| `"start"` | `RaceManager.startRace()` (host, on race start) |
| `"abort"` | `RaceManager.raceAbort()` (host, on abort) |
| `"race finished"` | `PlayerPositions` (two sites) when the race completes |

**Correction to the previous spec revision:** `"started"`/`"aborted"` do **not** exist in
this binary (no such lowercase literals anywhere in it). velocistream's observed
`start` / `abort` / `race finished` is exactly right; parse those three. (S5/ZippyOVD's
`"started"/"aborted"/"finished"/"reset"` matches nothing in 1.17.13 either.)

### `racedata` — the position/telemetry snapshot

**The primary event for timing.** A dictionary keyed by player name, one entry per active
pilot, sent as a **whole-field snapshot**.

```json
{"racedata": {
  "Dacus": {"position": "1", "lap": "2", "gate": "5", "time": "31.245",
             "finished": "False", "colour": "00FFFF", "uid": 12345},
  "Ace":   {"position": "2", "lap": "2", "gate": "4", "time": "32.101",
             "finished": "False", "colour": "FF0000", "uid": 67890}
}}
```

**Cadence & trigger** [BINARY]: every checkpoint crossing by any pilot replaces that
pilot's entry in the host's checkpoint list and re-ranks the field; the ranked snapshot is
RPC'd to all clients, where a **dirty flag + 0.1 s coroutine** forwards it to the websocket.
So: at most **10 snapshots/second**, each triggered by ≥1 actual crossing, each containing
**every** active pilot's latest state (not just the pilot who crossed).

Field-by-field [BINARY]:

| Field | Wire type | Meaning |
|---|---|---|
| *(map key)* | string | Player name (the only per-entry identity in legacy builds) |
| `position` | string int | Live rank, `"1"` = leader. Sort: packed progress desc, then `time` asc |
| `lap` | string int | `calculatedposition / 1000` — the lap counter |
| `gate` | string int | `calculatedposition % 1000` — **1-based** checkpoint ordinal within the lap (`checkpointIndex + 1`), counting **every** track gate. Resets each lap |
| `time` | string decimal | Cumulative race time in **seconds**, formatted `F3` (exactly 3 decimals, e.g. `"69.711"`) |
| `finished` | `"True"`/`"False"` | Capitalized C# bool |
| `colour` | string | Uppercase RGB hex, **no `#`** (e.g. `"00FFFF"`) |
| `uid` | **JSON number** | VelociDrone account id (int). Present in 1.17.13; absent in the 2025-01 capture (older build) |

The packing `lap * 1000 + (checkpoint + 1)` means `gate` can never exceed 999; the ranking
compares the packed int directly, so a higher lap always outranks any gate index.

Emission requires the snapshot's leader slot to be populated (`position != 0`) — nothing is
sent for an empty field [BINARY].

**Single-player quirk**: when the (only) pilot's entry arrives with `finished == true`, the
game force-finishes the local session (`setAllfinished(true)`) [BINARY].

**Finish semantics**: the final crossing of the last lap arrives with `finished:"True"`;
`lap` does *not* increment past the race length, and on tracks whose start/finish gate is
counted the final crossing appears as one gate ordinal beyond the per-lap gate count
(`gate = gates_per_lap + 1` observed in [CAPTURE]). Treat any `finished:"True"` crossing as
the race-ending lap gate regardless of gate index.

### `spectatorChange` — camera-subject change (bare string!)

```json
{"spectatorChange": "Dacus"}
```

**Trigger**: `DroneControlNetwork.setCameraPlayerName` — fired whenever the local client's
camera switches to display a different player's name (i.e., *whose view/name the local
machine is showing*), with the raw (un-colour-tagged) player name as a **bare string
payload** [BINARY].

**Bug in the game, worth knowing**: the emitter (`SocketManager.playerNameUpdate`) builds a
second object with a `spectatorStatus` key that references *itself* and is **never sent** —
the `spectatorStatus` event documented in the previous spec revision is dead code and never
appears on the wire [BINARY]:

```csharp
nooongbkkcb2.akefdlahcfd("spectatorChange", pghcdipngog.dffliogmgno(name));
nooongbkkcb3.akefdlahcfd("spectatorStatus", pghcdipngog.dffliogmgno(nooongbkkcb3)); // self-ref, discarded
sendMessage(nooongbkkcb2.ToString());
```

### `player` — player state

```json
{"player": {"PlayerName": "…", "playerColour": "…", "playerFlying": "True", "raceManager": "False"}}
```

All values strings; note the inconsistent `PlayerName` casing [BINARY].
`raceManager` is the room-host flag — the readback for host detection.

<!-- PLAYER-TRIGGER: call-site sweep pending -->

### `pilotlist` — roster reply

**Trigger**: reply to the `getpilots` command (host-gated; see below) [BINARY].

```json
{"pilotlist": [{"name": "Ace", "uid": "12345"}, {"name": "Bee", "uid": "67890"}]}
```

Built from the Photon player list; `uid` here is a **string** (`CustomProperties["uid"].ToString()`)
— unlike the number in `racedata` [BINARY].

### `ActivateError` — seating readback

**Trigger**: after an `activate` command, **one frame per requested uid that is not in the
room** (a loop over the requested uids emits `NotifyNotActivated(uid)` for each missing
one) [BINARY]. This settles the old "once or per-uid?" question: **per uid**, in request
order.

```json
{"ActivateError": {"UIDNotFound": "99999"}}
```

`UIDNotFound` is the stringified int uid [BINARY]. Absence of any `ActivateError` within a
bounded window after `activate` is the success signal; `getpilots` → `pilotlist` is the
positive confirm.

### `imu` — drone attitude feed (1.17.x, opt-in)

**Trigger**: only when the `web-socket-imu` setting is on **and** the local drone uses the
**Betaflight** flight controller model; emitted from a **60 Hz** coroutine while flying,
for the **local player's own drone only** [BINARY].

```json
{"imu": {
  "roll": 1.23, "pitch": -0.5, "yaw": 0.01,
  "PositionX": 1.0, "PositionY": 2.0, "PositionZ": 3.0,
  "AttitudeX": 0.0, "AttitudeY": 0.0, "AttitudeZ": 0.0, "AttitudeW": 1.0,
  "SpeedX": 0.0, "SpeedY": 0.0, "SpeedZ": 0.0,
  "timestamp": 123456.78
}}
```

**All values are JSON numbers** (the only event family that is). `roll/pitch/yaw` are the
gyro rates (`gyroADC`), `Position*` the world position, `Attitude*` a quaternion, `Speed*`
the velocity vector, `timestamp` = Unity `Time.time * 1000` (ms since game start, float)
[BINARY]. Consumers not interested in IMU must tolerate this frame at 60 Hz without choking.

## Client → Server commands

Wire form: `{"command": "<name>", …}`. Dispatch details [BINARY]:

- The `command` value is **lowercased before matching** (`ToLower()`), so `"StartRace"`
  works; the additional field *keys* (`pilots`, `mode`, `number`, `uid`) are **not**
  case-normalized.
- A frame without a `command` key, or non-JSON input, is logged (warning/error) and ignored.
- An unknown command is logged and ignored.
- **Failed authorization is silent** — the command is simply dropped (no error frame).

### Authorization matrix (exact, from the dispatcher + handlers)

Two distinct checks exist, and the previous spec conflated them:

| Command | Dispatcher gate (game mode) | Handler gate |
|---|---|---|
| `ping` | none | — (no-op) |
| `startrace` | **none** | `PhotonNetwork.IsMasterClient` |
| `abortrace` | **none** | `PhotonNetwork.IsMasterClient` |
| `activate` | `MULTI_PLAYER_HOST` | `IsMasterClient` |
| `lock` / `unlock` | `MULTI_PLAYER_HOST` | `IsMasterClient` |
| `allspectate` | `MULTI_PLAYER_HOST` | `IsMasterClient` |
| `getpilots` | `MULTI_PLAYER_HOST` | none (replies `pilotlist`) |
| `cameraplayer` / `cameramode` / `cameraselect` / `camerareset` | not `SINGLE_PLAYER` | none |

Practical reading: `startrace`/`abortrace` are **not** mode-gated, so they also work in
single player (where the local client is master) — the camera commands are the multiplayer-
only set, and the roster/room commands require being the multiplayer host.

### Command reference

- `{"command":"ping"}` — keep-alive no-op. Any client.
- `{"command":"startrace"}` — starts the race via the same path as the host's start button
  (join waiting players, reset, start; triggers `racestatus:"start"` + `racetype` +
  countdown). Master client only.
- `{"command":"abortrace"}` — aborts (internally `startNewRace(neogllonlbg: true)` →
  `raceAbort()`); triggers `racestatus:"abort"`. Master client only.
- `{"command":"activate","pilots":[12345,"67890"]}` — seat exactly these uids: listed
  pilots → flying, everyone else → spectate (auto-enable is switched off first). Elements
  may be JSON numbers **or** numeric strings — both are parsed (`int.TryParse` for
  strings; non-numeric strings are logged and skipped) [BINARY]. Readback:
  `ActivateError` per missing uid.
- `{"command":"allspectate"}` — everyone to spectator.
- `{"command":"getpilots"}` — request roster → `pilotlist` reply.
- `{"command":"lock"}` / `{"command":"unlock"}` — close/open the Photon room to joins.
- `{"command":"cameraplayer","uid":12345}` — focus camera on that player (uid may be
  number or numeric string; unknown uid is ignored).
- `{"command":"cameramode","mode":"fpv"|"spectate"}` — case-insensitive mode value;
  anything else logs a warning.
- `{"command":"cameraselect","number":2}` — numbered camera (number or numeric string).
- `{"command":"camerareset"}` — reset camera to self.

## Consumer checklist (wire traps)

0. **All server frames are BINARY (opcode 0x2), not text.** Decode the payload as UTF-8
   JSON regardless of frame type. A text-frames-only client hears nothing, with no error
   anywhere — the highest-severity trap in this document.
1. **Everything is a quoted string except `racedata.uid` and the whole `imu` payload.**
   Parse scalars string-or-number tolerantly; you will meet both across builds.
2. Booleans are `"True"`/`"False"` (capitalized) on this build; older tools' fixtures with
   `"true"` are wrong for 1.17.13.
3. `racedata.time` is cumulative **seconds** with exactly 3 decimals (`F3`), not `mm:ss.fff`.
4. `colour` has no `#` prefix.
5. `uid` is a number in `racedata`, a string in `pilotlist`/`ActivateError`.
6. `gate` is 1-based, counts every track gate, resets per lap; lap changes when the packed
   counter crosses a ×1000 boundary. A `finished:"True"` crossing ends the pilot's race
   whatever the gate index says.
7. One-connection semantics: the newest client silently takes over the feed.
8. Send `{"command":"ping"}` every ~5 s; the sliding idle timeout is 40 s. An empty frame
   also works (frame-level echo, silent), but any non-empty non-command frame spams the
   game's log (the historical stutter bug). Never send an RFC ping with a payload — it
   desyncs the server's frame parser. Don't wait for close frames; the server never sends
   any (raw TCP teardown).
9. No `session` frame unless *this* machine created the room; no countdown frames in SP if
   the countdown is disabled.
10. `spectatorChange`'s payload is a **bare string**, the only non-object payload.
11. Expect unknown future one-key frames; skip them without dying (and without logging
    per-frame at 60 Hz — `imu` will hurt you).
12. Failed writes are silent. Prove every write landed: `startrace` → expect
    `racestatus`/`countdown`; `activate` → absence of `ActivateError` + optional
    `getpilots` cross-check.

## Changes vs the previous spec revision (early-2026 dnSpyEx pass)

| Topic | Was | Is (1.17.13) |
|---|---|---|
| `raceAction` values | `"started"`, `"aborted"` | **`"start"`, `"abort"`, `"race finished"`** (old values don't exist in the binary; likely a decoy-clone misread) |
| Countdown | `3..0` | **`5..0` multiplayer**, `3..0` single player, nothing if SP countdown off |
| `spectatorStatus` event | documented | **dead code — never on the wire**; the real event is bare-string `spectatorChange` |
| `racedata` scalar types | ints/floats/bool | **all strings** (F3 time, `"True"`/`"False"`) except `uid` = number |
| `racedata.time` | `"01:23.456"` | cumulative seconds, `"F3"` |
| `uid` in `racedata` | string | **JSON number** |
| `imu` event | absent | new in 1.17.x (60 Hz, numbers, Betaflight-only, opt-in) |
| `ActivateError` | fires once | **fires per missing uid** |
| `FinishGate` | "sent when a player crosses the gate" | track-shape flag sent once after countdown 0 |
| `session` trigger | "new room or session" | **only when the local player creates the room** |
| Access control | one host gate | two-level: mode gate (dispatcher) + IsMasterClient (handler); `startrace`/`abortrace` have **no mode gate** |
| Max connections | 100 | 100 accepted, **1 served** (newest wins) |
| Service path | `ws` | `ws` is only the code default; shipped scene config is `velocidrone` [LIVE] |
| Keep-alive | "send `ping`" | confirmed: `"ping"` no-op case exists; 40 s **sliding** idle timeout; empty-frame keep-alive is also silent at frame level |
| Frame type | "JSON text frames" | **JSON in BINARY frames** (opcode 0x2) — server never sends text frames |
| Handshake GUID | `258EAFA5-…-5AB5DC11650A` (garbled) | the standard `258EAFA5-E914-47DA-95CA-C5AB0DC85B11` |
| Close handling | "Close frames — connection teardown" | no close frame is ever sent; raw TCP teardown (linger 0) |
| Ping/Pong | "opcodes 0x9/0xA keep-alive" | 0x9 answered with an app-level 2-byte binary *message* `{0x8A,0x00}`, zero-payload assumption desyncs on payload pings |
| Number/string serialization | — | numbers are CurrentCulture-formatted (locale bug); strings are entirely unescaped |

## Mocking

GridFPV's `vd-mock` (gridfpv repo: `crates/testkit` + `cargo xtask vd-mock`) implements
this spec server-side for CI and bench use — wire-exact frames (string scalars, F3 times,
capitalized booleans, uid-as-number, key order), the command sink with the authorization
matrix, per-uid `ActivateError`, and the newest-connection-wins behavior.
