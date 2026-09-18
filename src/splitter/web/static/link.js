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
  var link = { state: "connecting", game: null, ws: null };

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
  function emit(type, data) {
    (listeners[type] || []).forEach(function (fn) { try { fn(data); } catch (e) { console.error("link listener failed", type, e); } });
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = link.ws = new WebSocket(proto + location.host + "/ws/live");
    renderLink("connecting");
    ws.onopen = function () { opened = true; renderLink("live"); emit("open"); };
    ws.onmessage = function (ev) {
      var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "snapshot") renderGame(msg.data && msg.data.connection);
      else if (msg.type === "status") renderGame(msg.data);
      emit(msg.type, msg.data);
    };
    ws.onclose = function () { renderLink(opened ? "lost" : "connecting"); renderGame(null); emit("close"); setTimeout(connect, 1500); };
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
