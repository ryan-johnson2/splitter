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

  link.on = function (type, fn) { (listeners[type] = listeners[type] || []).push(fn); return link; };
  link.send = function (text) { if (ws && ws.readyState === 1) ws.send(text); };
  link.connected = function () { return !!(game && game.connected); };
  window.Splitter = window.Splitter || {};
  window.Splitter.link = link;
  connect();
})();
