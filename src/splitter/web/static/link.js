/* The page's link to the Splitter server: one websocket to /ws/live, opened on
   every page, owning the two header indicators — "Splitter" (this socket: live /
   connecting / lost) and "Game" (the server's connection to VelociDrone). The
   game indicator only ever shows a value that arrived over the socket; while
   the socket is down it says "unknown", so a stale page can never claim the
   game is connected. Pages that want the feed subscribe with Splitter.link.on. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var listeners = {}, ws = null, opened = false, game = null;
  var link = { state: "connecting", game: null, sync: null, ws: null };

  function paint(dotId, labelId, cls, text) {
    var dot = $(dotId), label = $(labelId);
    if (dot) dot.className = "conn " + cls;
    if (label) label.textContent = text;
  }
  function renderLink(state) {
    link.state = state;
    paint("link-dot", "link-label", state === "live" ? "on" : state === "connecting" ? "busy" : "", state);
  }
  function renderGame(c) {
    game = link.game = c || null;
    if (!c) { paint("game-dot", "game-label", "unk", "unknown"); return; }
    var busy = c.state === "connecting" || c.state === "reconnecting";
    paint("game-dot", "game-label", c.connected ? "on" : busy ? "busy" : "",
      c.connected ? "connected" : c.state === "idle" || !c.state ? "offline" : c.state + (c.attempts ? " (" + c.attempts + ")" : ""));
    emit("game", c);
  }
  // "IMU": shown only while runs are arriving without telemetry (the game's
  // Websocket IMU option is off); telemetry, fingerprints and crashes need it.
  function renderImu(missing) {
    link.imuMissing = !!missing;
    var b = $("ind-imu");
    if (b) b.hidden = !missing;
  }
  // "Web": the upstream this node sends runs to (only rendered when one is set).
  function renderSync(s) {
    link.sync = s || null;
    if (!$("web-dot")) return;
    if (!s) { paint("web-dot", "web-label", "unk", "unknown"); return; }
    if (!s.upstream) paint("web-dot", "web-label", "unk", "off");
    else if (s.token_blocked) paint("web-dot", "web-label", "", "blocked");
    // What the web itself said last: these come before the queue counts, because a
    // queue of zero says nothing about the web (nothing was ever sent).
    else if (s.web === "receiving_off") paint("web-dot", "web-label", "", "not receiving");
    else if (s.web === "bad_token") paint("web-dot", "web-label", "", "bad token");
    else if (s.web === "old_web") paint("web-dot", "web-label", "", "web too old");
    else if (s.web === "untrusted_cert") paint("web-dot", "web-label", "", "certificate not trusted");
    else if (s.web === "unreachable") paint("web-dot", "web-label", "", "unreachable");
    else if (s.terminal) paint("web-dot", "web-label", "", s.terminal + " refused");
    else if (s.pending) paint("web-dot", "web-label", s.last_error ? "" : "busy", s.pending + " pending");
    else if (s.web !== "ok") paint("web-dot", "web-label", "busy", "checking…");
    else paint("web-dot", "web-label", "on", "up to date");
  }
  function emit(type, data) {
    (listeners[type] || []).forEach(function (fn) { try { fn(data); } catch (e) { console.error("link listener failed", type, e); } });
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    // A relayed view (window.SPLITTER_LIVE_WS = /ws/live/<node>) rides the same code.
    ws = link.ws = new WebSocket(proto + location.host + (window.SPLITTER_LIVE_WS || "/ws/live"));
    renderLink("connecting");
    ws.onopen = function () { opened = true; renderLink("live"); emit("open"); };
    ws.onmessage = function (ev) {
      var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "snapshot") { renderGame(msg.data && msg.data.connection); renderSync(msg.data && msg.data.sync); renderImu(msg.data && msg.data.imu_missing); }
      else if (msg.type === "imu_warning") renderImu(msg.data && msg.data.missing);
      else if (msg.type === "status") renderGame(msg.data);
      else if (msg.type === "sync") renderSync(msg.data);
      else if (msg.type === "node") { link.node = msg.data; if (msg.data && msg.data.connected === false) renderGame(null); }
      emit(msg.type, msg.data);
    };
    ws.onclose = function () { renderLink(opened ? "lost" : "connecting"); renderGame(null); renderSync(null); emit("close"); setTimeout(connect, 1500); };
    ws.onerror = function () { ws.close(); };
  }
  // Server-rendered game state is only trusted for a moment: if the socket has
  // not opened soon after load, show the game as unknown rather than stale.
  setTimeout(function () { if (!opened) renderGame(null); }, 5000);
  setInterval(function () { if (ws && ws.readyState === 1) ws.send("ping"); }, 20000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden && ws && ws.readyState === 1) ws.send("snapshot"); });

  // ── help popovers ─────────────────────────────────────────────
  // Anything with data-help="<key>" opens a short "what to do" note on click
  // (hover-only titles are useless on a tablet). Texts adapt to the live state.
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  var HELP = {
    link: function () {
      if (link.state === "live") return { title: "Splitter link: live", body: "This page is talking to Splitter. Nothing to do." };
      return { title: "Splitter link: " + link.state, body: "This page cannot reach Splitter itself." +
        "<ul><li>Desktop app: is the Splitter window still open? Closing it stops the timer.</li>" +
        "<li>Phone or tablet: same Wi-Fi as the PC, and the address in the browser must be the PC's, e.g. <code>http://192.168.1.23:8100/</code>.</li>" +
        "<li>It reconnects by itself; if the dot stays red, reload the page.</li></ul>" };
    },
    game: function () {
      var g = link.game, addr = window.SPLITTER_GAME_ADDR || "";
      if (g && g.connected) return { title: "Game: connected", body: "Splitter is receiving VelociDrone's feed. Fly." };
      if (link.state !== "live") return { title: "Game: unknown", body: "This page has lost Splitter, so the game state is unknown. Fix the Splitter link first." };
      var head = addr ? "Splitter is trying to reach VelociDrone at <code>" + esc(addr) + "</code>" : "No game address is set yet";
      var err = g && g.last_error ? '<p class="muted small">Last error: ' + esc(g.last_error) + "</p>" : "";
      return { title: "Game: " + ((g && g.state) || "offline"), body: head + ".<ul>" +
        (addr ? "" : '<li>Open <a href="/settings">Settings</a> and enter the game PC\'s address.</li>') +
        "<li>Is VelociDrone running?</li>" +
        "<li>In the game: <i>Options → Main Settings</i>, set <i>Websocket Communication</i> and <i>Websocket IMU Data</i> to Yes, then <b>restart the game</b> — it only reads them at startup.</li>" +
        "<li>The address must be the game PC's <b>LAN address</b>, never localhost, even when Splitter runs on the same PC. <a href=\"/settings\">Settings</a> lists this PC's addresses in the desktop app.</li>" +
        "<li>Same network, and nothing blocking port 60003 between them.</li>" +
        "<li>Only one tool can listen to the game at a time — close other overlays or timers.</li></ul>" + err };
    },
    track: function () { return { title: "Track…", body: "Single player never tells Splitter which track you are on, so pick it here before you fly. Splitter remembers it until you change it. Runs without a track are saved but cannot be personal bests." }; },
    capture: function () { return { title: "Capture paused", body: "Splitter stays connected to the game but records nothing: no runs, no PBs, no telemetry. Use it for free flying or practice you do not want in the log. Tap <b>Resume capture</b> when you want runs recorded again." }; },
    abort: function () { return { title: "Abort", body: "Ends the current run as aborted in Splitter and tells the game to abort too. Use it if the timer keeps running after you quit or crashed out of a race." }; },
    imu: function () { return { title: "No IMU data", body: "Runs are being recorded, but the game is not sending IMU (position and speed) data, so there is no telemetry, no flight path, no crash detection, and other Splitters cannot recognise the track from the run.<ul><li>In VelociDrone: <i>Options → Main Settings → Websocket IMU Data</i> → Yes, then <b>restart the game</b>.</li><li>IMU is only sent for Betaflight-type quads.</li><li>Splitter: Settings → Telemetry → <i>Record telemetry</i> must be on (it is, or this would not show).</li></ul>This goes away by itself once frames arrive." }; },
    sync: function () {
      var s = link.sync;
      if (!s) return { title: "Web: unknown", body: "This page has lost Splitter, so nothing is known about the other Splitter either." };
      var where = s.url ? ' at <code>' + esc(s.url) + '</code>' : "";
      if (!s.upstream) return { title: "Web: off", body: "This Splitter is not sending its runs anywhere. Set it up under <a href=\"/settings#send\">Settings</a>." };
      if (s.token_blocked) return { title: "Web: blocked", body: "The address" + where + " is plain <code>http://</code> to a public address, which would expose the token. Use <code>https://</code>, or a LAN address." };
      if (s.web === "receiving_off") return { title: "Web: not receiving", body: "The other Splitter" + where + " has <i>Receive runs</i> turned off, so nothing can be sent. Turn it on in its Settings; runs recorded meanwhile wait here." };
      if (s.web === "bad_token") return { title: "Web: bad token", body: "The other Splitter" + where + " refused the token. Copy it again from its Settings → <i>Receive runs</i> into <a href=\"/settings#send\">Settings</a> here." };
      if (s.web === "untrusted_cert") return { title: "Web: certificate not trusted", body: "This machine does not trust the root that signed the other Splitter's certificate" + where + ". Splitter runs as a service, so the root must be in the <b>machine's</b> Trusted Root store (not just your user's): on Windows, as administrator, <code>certutil -addstore -f Root root.crt</code>. The other Splitter serves its root at <code>http://&lt;its host&gt;/splitter-ca.crt</code>. Or use its plain <code>http://</code> LAN address." + err };
      if (s.web === "old_web") return { title: "Web: too old", body: "The other Splitter" + where + " runs an older version that cannot read this node's runs. Upgrade it." + err };
      if (s.web === "unreachable") return { title: "Web: unreachable", body: "The other Splitter" + where + " did not answer. Runs wait here and go as soon as it can be reached." + err };
      var err = s.last_error ? '<p class="muted small">Last error: ' + esc(s.last_error) + "</p>" : "";
      if (s.terminal) return { title: "Web: runs refused", body: s.terminal + " run" + (s.terminal === 1 ? "" : "s") + " will not be sent" + where + ": the other Splitter refused them (older version, or the run was deleted there). See <a href=\"/settings#send\">Settings</a>." + err };
      if (s.pending) return { title: "Web: " + s.pending + " pending", body: "Runs waiting to be sent" + where + ". They go as soon as it can be reached; nothing is lost meanwhile." + err };
      if (s.web !== "ok") return { title: "Web: checking", body: "Asking the other Splitter" + where + " how it is. This takes a moment." };
      return { title: "Web: up to date", body: "Every run has been sent" + where + "." + (s.keep_local ? "" : " Local copies are deleted once sent.") };
    },
    noid: function () { return { title: "No online track id", body: "This run's track has no online id, so it cannot be a personal best and has no reference. Pick the track with <b>Track…</b>, or fix it later on the Races page." }; }
  };
  var pop = null;
  function closeHelp() { if (pop) { pop.remove(); pop = null; } }
  function openHelp(anchor, key) {
    closeHelp();
    var make = HELP[key]; if (!make) return;
    var h = make();
    pop = document.createElement("div"); pop.className = "popover"; pop.setAttribute("role", "dialog");
    pop.innerHTML = '<div class="popover-head"><b>' + h.title + '</b><button type="button" class="popover-close" aria-label="close">×</button></div><div class="popover-body">' + h.body + "</div>";
    document.body.appendChild(pop);
    var r = anchor.getBoundingClientRect(), w = Math.min(360, window.innerWidth - 24);
    pop.style.width = w + "px";
    pop.style.left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12)) + window.scrollX + "px";
    pop.style.top = (r.bottom + 8 + window.scrollY) + "px";
    pop.querySelector(".popover-close").onclick = closeHelp;
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest ? e.target.closest("[data-help]") : null;
    if (t) { e.preventDefault(); openHelp(t, t.getAttribute("data-help")); return; }
    if (pop && !pop.contains(e.target)) closeHelp();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeHelp(); });
  link.help = openHelp;

  link.on = function (type, fn) { (listeners[type] = listeners[type] || []).push(fn); return link; };
  link.send = function (text) { if (ws && ws.readyState === 1) ws.send(text); };
  link.connected = function () { return !!(game && game.connected); };
  window.Splitter = window.Splitter || {};
  window.Splitter.link = link;
  connect();
})();
