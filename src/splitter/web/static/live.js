/* Live page: one websocket, a snapshot on connect, then incremental messages. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var state = { race: null, reference: null, session: null, lastCrossing: null, phase: "idle", speed: 0, ws: null, connected: false, lastResult: null };
  var timer = null;

  function fmt(ms) {
    if (ms == null || ms < 0) return "--";
    var m = Math.floor(ms / 60000), s = (ms % 60000) / 1000;
    return m ? m + ":" + s.toFixed(3).padStart(6, "0") : s.toFixed(3);
  }
  function fmtDelta(ms) { if (ms == null) return "--"; return (ms < 0 ? "-" : "+") + (Math.abs(ms) / 1000).toFixed(3); }
  function deltaClass(ms) { return ms == null ? "" : ms < 0 ? "ahead" : ms > 0 ? "behind" : ""; }
  function setDelta(el, ms) { el.textContent = fmtDelta(ms); el.className = el.className.replace(/\b(ahead|behind)\b/g, "").trim() + " " + deltaClass(ms); }
  function kmh(mps) { return mps == null ? "--" : Math.round(mps * 3.6); }

  function toast(msg, level) {
    var t = document.createElement("div"); t.className = "toast " + (level || ""); t.textContent = msg;
    $("toasts").appendChild(t); setTimeout(function () { t.remove(); }, 6000);
  }

  // ── rendering ─────────────────────────────────────────────────
  function setPhase(phase) {
    state.phase = phase;
    var pill = $("phase");
    var map = { idle: ["dim", "idle"], armed: ["warn", "starting"], countdown: ["warn", "countdown"], racing: ["ok", "racing"], finished: ["accent", "finished"], aborted: ["bad", "aborted"], offline: ["bad", "game offline"] };
    var m = map[phase] || ["dim", phase];
    pill.className = "pill " + m[0]; pill.textContent = m[1];
    $("view-countdown").hidden = phase !== "countdown";
  }

  function renderSession(s) {
    state.session = s;
    var line = s && s.known ? "<b>" + esc(s.track_name) + "</b>" + (s.scenery ? " · " + esc(s.scenery) : "") + (s.quad_type ? " · " + esc(s.quad_type) : "") + (s.race_laps ? " · " + s.race_laps + " laps" : "") + (s.source ? ' <span class="muted small">(' + s.source + ")</span>" : "")
      : '<span class="muted">no track set — tap “Track…”</span>';
    $("session-line").innerHTML = line;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function renderConnection(c) {
    state.connected = !!(c && c.connected);
    var dot = $("conn-dot"), label = $("conn-label");
    dot.className = "conn " + (c.connected ? "on" : (c.state === "connecting" || c.state === "reconnecting") ? "busy" : "");
    label.textContent = c.connected ? "game connected" : c.state === "idle" ? "game offline" : c.state + (c.attempts ? " (" + c.attempts + ")" : "");
    if (!c.connected && state.phase === "idle") setPhase("offline");
    if (c.connected && state.phase === "offline") setPhase("idle");
  }

  function renderReference(ref) {
    state.reference = ref;
    $("ref-line").textContent = ref ? "vs PB " + fmt(ref.total_ms) + " (#" + ref.race_id + ")" : (state.session && state.session.known ? "No PB yet on this track/quad — this run sets it" : "");
  }

  function resetRaceView() {
    $("race-time").textContent = "0.000"; $("lap-time").textContent = "0.000";
    $("split").innerHTML = "&nbsp;"; $("split").className = "big-delta";
    $("lap-no").textContent = "1"; $("gate-no").textContent = "–"; $("last-gate").textContent = "–";
    $("last-lap").textContent = "–"; $("last-lap-delta").textContent = "–"; $("last-lap-delta").className = "";
    $("gate-rows").innerHTML = "";
    $("result-card").hidden = true;
  }

  function addGateRow(c) {
    var tr = document.createElement("tr");
    if (c.ends_lap) tr.className = "lap-end";
    tr.innerHTML = "<td>" + c.lap + "</td><td>" + c.gate + (c.finished ? " 🏁" : "") + "</td><td class=num>" + fmt(c.gate_ms) + "</td><td class='num " + deltaClass(c.split_ms) + "'>" + fmtDelta(c.split_ms) + "</td><td class=num>" + fmt(c.cumulative_ms) + "</td><td class=num>" + kmh(c.max_speed) + "</td>";
    var body = $("gate-rows");
    var empty = body.querySelector(".empty"); if (empty) empty.parentNode.remove();
    body.insertBefore(tr, body.firstChild);
  }

  function applyCrossing(c) {
    state.lastCrossing = c;
    $("lap-no").textContent = c.ends_lap && !c.finished ? c.ends_lap + 1 : c.lap;
    $("gate-no").textContent = c.gate;
    $("last-gate").textContent = fmt(c.gate_ms);
    setDelta($("split"), c.split_ms);
    if (c.lap_done) {
      $("last-lap").textContent = fmt(c.lap_done.lap_ms);
      setDelta($("last-lap-delta"), c.lap_done.delta_ms);
    }
    addGateRow(c);
  }

  function renderRace(race) {
    state.race = race;
    resetRaceView();
    if (!race) return;
    race.crossings.forEach(applyCrossing);
    if (race.finished) { setPhase("finished"); } else { setPhase("racing"); startClock(race); }
  }

  function startClock(race) {
    stopClock();
    var t0 = Date.now() - (race.total_ms || 0);
    // Between crossings the clock free-runs from the last known race time.
    timer = setInterval(function () {
      if (!state.race || state.race.finished) return;
      var elapsed = Date.now() - t0;
      var lapStart = state.race.lap_start_ms || 0;
      $("race-time").textContent = fmt(elapsed);
      $("lap-time").textContent = fmt(Math.max(0, elapsed - lapStart));
    }, 47);
    state.clockT0 = t0;
  }
  function stopClock() { if (timer) { clearInterval(timer); timer = null; } }
  function resync(cumMs) { state.clockT0 = Date.now() - cumMs; startClockFrom(state.clockT0); }
  function startClockFrom(t0) { stopClock(); timer = setInterval(function () {
    if (!state.race || state.race.finished) return;
    var elapsed = Date.now() - t0; var lapStart = state.race.lap_start_ms || 0;
    $("race-time").textContent = fmt(elapsed); $("lap-time").textContent = fmt(Math.max(0, elapsed - lapStart));
  }, 47); }

  function renderResult(r) {
    state.lastResult = r;
    var card = $("result-card");
    if (!r) { card.hidden = true; return; }
    card.hidden = false;
    card.className = "card result-card" + (r.is_best ? " pb" : "");
    $("result-title").textContent = r.aborted ? "Aborted" : r.is_best ? "New personal best!" : "Result";
    $("result-total").textContent = r.aborted ? fmt(null) : fmt(r.total_ms);
    var d = $("result-delta");
    if (r.pb_delta_ms != null && !r.aborted) { setDelta(d, r.pb_delta_ms); d.textContent += " vs PB"; } else { d.innerHTML = "&nbsp;"; d.className = "big-delta"; }
    $("result-link").href = "/races/" + r.id;
    var laps = $("result-laps"); laps.innerHTML = "";
    (r.laps || []).forEach(function (l) {
      var div = document.createElement("div"); div.className = "kv";
      div.innerHTML = "Lap " + l.lap + "<b>" + fmt(l.lap_ms) + (l.delta_ms != null ? ' <span class="small ' + deltaClass(l.delta_ms) + '">' + fmtDelta(l.delta_ms) + "</span>" : "") + "</b>";
      laps.appendChild(div);
    });
    if (r.max_speed != null) { var sp = document.createElement("div"); sp.className = "kv"; sp.innerHTML = "Top speed<b>" + kmh(r.max_speed) + " km/h</b>"; laps.appendChild(sp); }
  }

  function applySnapshot(snap) {
    renderConnection(snap.connection);
    renderSession(snap.session);
    renderReference(snap.reference);
    if (snap.race) { renderRace(snap.race); }
    else { resetRaceView(); setPhase(snap.armed ? "armed" : (state.connected ? "idle" : "offline")); renderResult(snap.last_result); if (snap.last_result && $("gate-rows").children.length === 0) $("gate-rows").innerHTML = '<tr><td colspan="6" class="empty">Waiting for a race…</td></tr>'; }
  }

  // ── messages ──────────────────────────────────────────────────
  var handlers = {
    snapshot: applySnapshot,
    status: renderConnection,
    session: renderSession,
    player: function () {},
    armed: function (d) { renderSession(d.session); setPhase("armed"); resetRaceView(); $("gate-rows").innerHTML = ""; },
    countdown: function (d) { $("countdown").textContent = d.count === 0 ? "GO" : d.count; setPhase(d.count === 0 ? "racing" : "countdown"); },
    race_started: function (d) { renderSession(d.session); renderReference(d.reference); state.race = { id: d.id, crossings: [], laps: [], total_ms: 0, lap_start_ms: 0, finished: false }; resetRaceView(); setPhase("racing"); startClock(state.race); },
    crossing: function (c) {
      if (!state.race) { state.race = { id: 0, crossings: [], laps: [], total_ms: 0, lap_start_ms: 0, finished: false }; setPhase("racing"); }
      state.race.total_ms = c.cumulative_ms;
      if (c.lap_done) { state.race.laps.push(c.lap_done); state.race.lap_start_ms = c.cumulative_ms; }
      state.race.crossings.push(c);
      applyCrossing(c);
      resync(c.cumulative_ms);
      $("race-time").textContent = fmt(c.cumulative_ms);
    },
    telemetry: function (t) { state.speed = t.speed; $("speed").textContent = kmh(t.speed); $("speed-bar").style.width = Math.min(100, t.speed * 3.6 / 200 * 100) + "%"; },
    race_finished: function (r) {
      stopClock();
      if (state.race) { state.race.finished = true; }
      $("race-time").textContent = r.aborted ? $("race-time").textContent : fmt(r.total_ms);
      setPhase(r.aborted ? "aborted" : "finished");
      renderResult(r);
      if (r.is_best) toast("New PB on " + r.track_name + ": " + fmt(r.total_ms), "ok");
      state.race = null;
    },
    race_aborted: function () { stopClock(); state.race = null; setPhase("idle"); },
    notice: function (n) { toast(n.message, n.level); }
  };

  function connect() {
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    var ws = new WebSocket(proto + location.host + "/ws/live");
    state.ws = ws;
    ws.onopen = function () { $("ws-state").textContent = "live"; };
    ws.onmessage = function (ev) {
      var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      var h = handlers[msg.type]; if (h) h(msg.data);
    };
    ws.onclose = function () { $("ws-state").textContent = "reconnecting"; setTimeout(connect, 1500); };
    ws.onerror = function () { ws.close(); };
  }
  setInterval(function () { if (state.ws && state.ws.readyState === 1) state.ws.send("ping"); }, 20000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden && state.ws && state.ws.readyState === 1) state.ws.send("snapshot"); });

  // ── session dialog ────────────────────────────────────────────
  var dlg = $("session-dialog");
  $("btn-session").onclick = function () {
    var s = state.session || {};
    $("f-track").value = s.track_name || ""; $("f-scenery").value = s.scenery || ""; $("f-quad").value = s.quad_type || ""; $("f-size").value = s.quad_size || ""; $("f-laps").value = s.race_laps || "";
    fetch("/api/races?limit=200").then(function (r) { return r.json(); }).then(function (rows) {
      var tracks = {}, quads = {};
      rows.forEach(function (r) { if (r.track_name) tracks[r.track_name] = r.scenery; if (r.quad_type) quads[r.quad_type] = r.quad_size; });
      $("track-list").innerHTML = Object.keys(tracks).map(function (t) { return "<option value=\"" + esc(t) + "\">"; }).join("");
      $("quad-list").innerHTML = Object.keys(quads).map(function (q) { return "<option value=\"" + esc(q) + "\">"; }).join("");
    }).catch(function () {});
    dlg.showModal();
  };
  $("session-cancel").onclick = function () { dlg.close(); };
  $("session-form").onsubmit = function (ev) {
    ev.preventDefault();
    var fd = new FormData(ev.target);
    var body = { track_name: fd.get("track_name"), scenery: fd.get("scenery"), quad_type: fd.get("quad_type"), quad_size: fd.get("quad_size"), race_laps: parseInt(fd.get("race_laps") || "0", 10) || 0 };
    fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { if (!r.ok) throw new Error("save failed"); return r.json(); })
      .then(function () { dlg.close(); toast("Track set", "ok"); })
      .catch(function (e) { toast(e.message, "bad"); });
  };
  $("btn-reconnect").onclick = function () {
    fetch("/api/connection", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "connect" }) })
      .then(function (r) { if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || "failed"); }); toast("Reconnecting…"); })
      .catch(function (e) { toast(e.message + " — set the game PC address in Settings", "warn"); });
  };

  applySnapshot(window.SPLITTER_SNAPSHOT || {});
  connect();
})();
