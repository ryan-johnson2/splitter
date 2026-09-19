/* Live page: rides the shared link (link.js): a snapshot on connect, then incremental messages. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var state = { race: null, reference: null, session: null, lastCrossing: null, phase: "idle", speed: 0, connected: false, lastResult: null };
  var timer = null;

  var fmt = Splitter.fmt;  // honours the Settings "Time format"
  function fmtDelta(ms) { if (ms == null) return "--"; return (ms < 0 ? "-" : "+") + (Math.abs(ms) / 1000).toFixed(3); }
  function deltaClass(ms) { return ms == null ? "" : ms < 0 ? "ahead" : ms > 0 ? "behind" : ""; }
  function setDelta(el, ms) { el.textContent = fmtDelta(ms); el.className = el.className.replace(/\b(ahead|behind)\b/g, "").trim() + " " + deltaClass(ms); }
  function kmh(mps) { return mps == null ? "--" : Math.round(mps * 3.6); }

  function toast(msg, level) {
    var t = document.createElement("div"); t.className = "toast " + (level || ""); t.textContent = msg;
    $("toasts").appendChild(t); setTimeout(function () { t.remove(); }, 6000);
  }

  // ── rendering ─────────────────────────────────────────────────
  function goHold() { return !!(state.goUntil && Date.now() < state.goUntil); }

  function setPhase(phase) {
    state.phase = phase;
    var pill = $("phase");
    var map = { idle: ["dim", "idle"], armed: ["warn", "starting"], countdown: ["warn", "countdown"], racing: ["ok", "racing"], finished: ["accent", "finished"], aborted: ["bad", "aborted"], offline: ["bad", "game offline"] };
    var m = map[phase] || ["dim", phase];
    pill.className = "pill " + m[0]; pill.textContent = m[1];
    // #3: in focus mode the race card owns the screen while a run is in progress.
    document.documentElement.classList.toggle("racing", phase === "racing" || phase === "countdown");
    $("btn-abort").hidden = !(phase === "racing" || phase === "countdown" || phase === "armed");
    renderGettingStarted();
  }

  // #2: colour the race clock by pace vs the PB at the last gate crossing.
  function paintPace(splitMs) {
    var el = $("race-time"), yellow = (window.SPLITTER_PACE_YELLOW_S || 2) * 1000;
    el.classList.remove("pace-ahead", "pace-close", "pace-behind");
    if (splitMs == null || !state.reference) return;
    el.classList.add(splitMs < 0 ? "pace-ahead" : splitMs <= yellow ? "pace-close" : "pace-behind");
  }

  function renderSession(s) {
    state.session = s;
    var line = s && s.known ? "<b>" + esc(s.track_name) + "</b>" + (s.scenery ? " · " + esc(s.scenery) : "") + (s.quad_type ? " · " + esc(s.quad_type) : "") + (s.race_laps ? " · " + s.race_laps + " laps" : "") + (s.source ? ' <span class="muted small">(' + s.source + ")</span>" : "") + (s.identified ? "" : ' <span class="pill bad" data-help="noid" title="no online track id — tap for help">no id</span>')
      : '<span class="muted">no track set — tap “Track…”</span>';
    $("session-line").innerHTML = line;
    renderGettingStarted();
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  // The header indicators are painted by link.js; this only tracks the phase.
  function renderConnection(c) {
    state.connected = !!(c && c.connected);
    if (!state.connected && state.phase === "idle") setPhase("offline");
    if (state.connected && state.phase === "offline") setPhase("idle");
    renderGettingStarted();
  }

  // First-run checklist: shown until the address is set, the game has connected
  // once and a track is picked; never while a run is on.
  function renderGettingStarted() {
    var card = $("getting-started"); if (!card) return;
    var steps = { "gs-host": !!window.SPLITTER_GAME_HOST_SET, "gs-game": state.connected || !!window.SPLITTER_EVER_CONNECTED, "gs-track": !!(state.session && state.session.known) || !!window.SPLITTER_TRACK_EVER_SET };
    var all = true;
    Object.keys(steps).forEach(function (id) { var li = $(id); if (li) li.classList.toggle("done", steps[id]); if (!steps[id]) all = false; });
    if (state.connected) { window.SPLITTER_GAME_HOST_SET = true; window.SPLITTER_EVER_CONNECTED = true; }
    if (state.session && state.session.known) window.SPLITTER_TRACK_EVER_SET = true;
    var busy = state.phase === "racing" || state.phase === "countdown" || state.phase === "armed";
    card.hidden = all || busy;
  }

  function renderReference(ref) {
    state.reference = ref;
    var s = state.session || {};
    $("ref-line").textContent = ref ? "vs PB " + fmt(ref.total_ms) + " (#" + ref.race_id + ")"
      : !s.known ? "" : !s.identified ? "No online track id — runs here won't count as PBs" : "No PB yet for this track, quad and lap count — a finished run sets it";
  }

  function resetRaceView() {
    if (!goHold()) { $("race-time").textContent = "0.000"; $("race-time").classList.remove("counting"); }
    $("lap-time").textContent = "0.000"; paintPace(null);
    $("split").innerHTML = "&nbsp;"; $("split").className = "big-delta";
    $("lap-no").textContent = "HS"; $("gate-no").textContent = "–"; $("last-gate").textContent = "–";
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
    $("lap-no").textContent = c.lap ? c.lap : "HS";  // lap 0 = holeshot, before the first start/finish crossing
    $("gate-no").textContent = c.gate;
    $("last-gate").textContent = fmt(c.gate_ms);
    setDelta($("split"), c.split_ms);
    paintPace(c.split_ms);
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
      if (!goHold()) $("race-time").textContent = fmt(elapsed);
      $("lap-time").textContent = fmt(Math.max(0, elapsed - lapStart));
    }, 47);
    state.clockT0 = t0;
  }
  function stopClock() { if (timer) { clearInterval(timer); timer = null; } }
  function resync(cumMs) { state.clockT0 = Date.now() - cumMs; startClockFrom(state.clockT0); }
  function startClockFrom(t0) { stopClock(); timer = setInterval(function () {
    if (!state.race || state.race.finished) return;
    var elapsed = Date.now() - t0; var lapStart = state.race.lap_start_ms || 0;
    if (!goHold()) $("race-time").textContent = fmt(elapsed); $("lap-time").textContent = fmt(Math.max(0, elapsed - lapStart));
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
    if (r.holeshot_ms != null) { var hs = document.createElement("div"); hs.className = "kv"; hs.innerHTML = "Holeshot<b>" + fmt(r.holeshot_ms) + "</b>"; laps.appendChild(hs); }
    (r.laps || []).forEach(function (l) {
      var div = document.createElement("div"); div.className = "kv";
      div.innerHTML = "Lap " + l.lap + "<b>" + fmt(l.lap_ms) + (l.delta_ms != null ? ' <span class="small ' + deltaClass(l.delta_ms) + '">' + fmtDelta(l.delta_ms) + "</span>" : "") + "</b>";
      laps.appendChild(div);
    });
    if (r.max_speed != null) { var sp = document.createElement("div"); sp.className = "kv"; sp.innerHTML = "Top speed<b>" + kmh(r.max_speed) + " km/h</b>"; laps.appendChild(sp); }
  }

  function applySnapshot(snap) {
    state.capture = snap.capture !== false; renderCapture();
    if (snap.track_check && snap.track_check.verdict === "different") showTrackCheck(snap.track_check);
    renderConnection(snap.connection);
    renderSession(snap.session);
    renderReference(snap.reference);
    if (snap.race) { renderRace(snap.race); }
    else { resetRaceView(); setPhase(snap.armed ? "armed" : (state.connected ? "idle" : "offline")); renderResult(snap.last_result); if (snap.last_result && $("gate-rows").children.length === 0) $("gate-rows").innerHTML = '<tr><td colspan="6" class="empty">Waiting for a race…</td></tr>'; }
  }

  // ── messages ──────────────────────────────────────────────────
  var handlers = {
    snapshot: function (snap) { try { applySnapshot(snap); } catch (e) { console.error("snapshot render failed", e); } },
    status: renderConnection,
    session: function (s) { renderSession(s); renderReference(state.reference); },
    reference: renderReference,
    player: function () {},
    armed: function (d) { renderSession(d.session); setPhase("armed"); resetRaceView(); $("gate-rows").innerHTML = ""; },
    // The countdown lives in the main clock so nothing else on the page moves:
    // 3, 2, 1 in the clock's place, GO, then the clock starts from 0.000.
    countdown: function (d) {
      var el = $("race-time");
      if (d.count === 0) {
        // GO holds the clock for a moment (state.goUntil); the reset from race_started
        // and the clock ticks leave it alone until then.
        el.textContent = "GO"; el.classList.add("counting"); state.goUntil = Date.now() + 700; setPhase("racing");
        setTimeout(function () { state.goUntil = 0; el.classList.remove("counting"); if (!state.race) el.textContent = "0.000"; }, 700);
      } else { resetRaceView(); el.textContent = String(d.count); el.classList.add("counting"); setPhase("countdown"); }
    },
    race_started: function (d) { renderSession(d.session); renderReference(d.reference); state.race = { id: d.id, crossings: [], laps: [], total_ms: 0, lap_start_ms: 0, finished: false }; resetRaceView(); setPhase("racing"); startClock(state.race); },
    crossing: function (c) {
      if (!state.race) { state.race = { id: 0, crossings: [], laps: [], total_ms: 0, lap_start_ms: 0, finished: false }; setPhase("racing"); }
      state.race.total_ms = c.cumulative_ms;
      if (c.lap_done) state.race.laps.push(c.lap_done);
      if (c.starts_lap) { state.race.lap_start_ms = c.cumulative_ms; state.race.current_lap = c.starts_lap; if (!state.race.holeshot_ms && c.starts_lap === 1) state.race.holeshot_ms = c.cumulative_ms; }
      state.race.crossings.push(c);
      applyCrossing(c);
      resync(c.cumulative_ms);
      $("race-time").textContent = fmt(c.cumulative_ms);
    },
    telemetry: function (t) { state.speed = t.speed; $("speed").textContent = kmh(t.speed); },
    race_finished: function (r) {
      stopClock();
      if (state.race) { state.race.finished = true; }
      $("race-time").textContent = r.aborted ? $("race-time").textContent : fmt(r.total_ms);
      setPhase(r.aborted ? "aborted" : "finished");
      renderResult(r);
      if (r.next_reference !== undefined) renderReference(r.next_reference);
      if (r.is_best) toast("New PB on " + r.track_name + ": " + fmt(r.total_ms), "ok");
      state.race = null;
    },
    race_aborted: function () { stopClock(); state.race = null; setPhase("idle"); },
    capture: function (d) { state.capture = !!d.enabled; renderCapture(); toast(state.capture ? "Capture resumed" : "Capture paused — runs are not recorded", state.capture ? "ok" : "warn"); },
    track_check: showTrackCheck,
    notice: function (n) { toast(n.message, n.level); }
  };

  function connect() {
    var link = Splitter.link;
    Object.keys(handlers).forEach(function (type) { link.on(type, handlers[type]); });
    // Link down: the game state is unknown, so do not claim idle-and-connected.
    link.on("close", function () { renderConnection(null); });
  }

  // ── capture on/off ────────────────────────────────────────────
  function renderCapture() {
    var on = state.capture !== false;
    $("btn-capture").innerHTML = on ? '<span class="ico">⏸</span> Pause capture' : '<span class="ico">▶</span> Resume capture';
    $("btn-capture").classList.toggle("primary", !on);
    $("capture-pill").hidden = on;
  }
  $("btn-capture").onclick = function () {
    var next = state.capture === false;
    if (!next && state.race) { if (!confirm("Pause capture now? The current run will be aborted.")) return; }
    fetch("/api/capture", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: next }) })
      .then(function (r) { if (!r.ok) throw new Error("failed"); }).catch(function (e) { toast(e.message, "bad"); });
  };

  // ── "did you change tracks?" ──────────────────────────────────
  // After lap 1 the server compares the run with the session's track. Three
  // outcomes: switched (it recognised another track: notice only), unset (it
  // could not tell: modal, with any close matches as one-tap picks), or a
  // geometry doubt (modal, track kept).
  var tcDlg = $("track-check-dialog"), pendingApply = 0, lastCheck = null;
  function trackBody(t, applyTo) {
    var s = (lastCheck && lastCheck.track) || state.session || {};
    return { track_name: t.track_name, scenery: t.scenery || "", quad_type: s.quad_type || "", quad_size: s.quad_size || "", race_laps: s.race_laps || 0, track_id: t.track_id, scene_id: t.scene_id || 0, track_source: t.track_source || "", quad_model_id: s.quad_model_id || 0, quad_class_id: s.quad_class_id || 0, apply_to_race_id: applyTo || 0 };
  }
  function postSession(body, okMsg, errMsg) {
    return fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { if (!r.ok) throw new Error(errMsg); toast(okMsg, "ok"); })
      .catch(function (e) { toast(e.message, "bad"); });
  }
  function showTrackCheck(d) {
    if (!d || d.verdict !== "different" || (lastCheck && lastCheck.race_id === d.race_id)) return;
    lastCheck = d;
    var name = (d.track && d.track.track_name) || "the selected track";
    if (d.switched) {
      var sw = d.switched, dist = sw.mean_distance_m != null ? " (" + sw.gates_compared + " gates within " + Math.round(sw.mean_distance_m) + " m)" : "";
      $("track-switched-text").textContent = "Track switched to " + sw.track_name + (sw.scenery ? " · " + sw.scenery : "") + " — this run's gates match it" + dist + ", not " + name + ". The run and its PB check now count for " + sw.track_name + ".";
      $("track-switched").hidden = false;
      toast("Track switched to " + sw.track_name, "ok");
      return;
    }
    $("track-check-text").textContent = "This run (#" + d.race_id + ") does not look like " + name + ": " + d.reason + "." +
      (d.unset ? " The track has been unset and this run will not count as a PB until you pick one." : " It is still recorded against " + name + ".");
    var list = $("track-check-candidates").querySelector(".candidate-list"); list.innerHTML = "";
    (d.candidates || []).forEach(function (c) {
      var b = document.createElement("button"); b.type = "button"; b.className = "btn";
      b.textContent = c.track_name + (c.scenery ? " · " + c.scenery : "") + (c.mean_distance_m != null ? " — gates within " + Math.round(c.mean_distance_m) + " m" : "");
      b.onclick = function () { tcDlg.close(); postSession(trackBody(c, d.race_id), "Track set: " + c.track_name, "could not set the track"); };
      list.appendChild(b);
    });
    $("track-check-candidates").hidden = !(d.candidates && d.candidates.length);
    if (!tcDlg.open) tcDlg.showModal();
  }
  $("track-check-pick").onclick = function () { tcDlg.close(); pendingApply = lastCheck ? lastCheck.race_id : 0; $("btn-session").onclick(); };
  $("track-check-same").onclick = function () {
    tcDlg.close();
    if (!lastCheck || !lastCheck.unset || !lastCheck.track) return;
    var t = lastCheck.track;
    postSession(trackBody(t, lastCheck.race_id), "Track kept: " + t.track_name, "could not restore the track");
  };
  $("track-switched-dismiss").onclick = function () { $("track-switched").hidden = true; };
  $("track-switched-pick").onclick = function () { $("track-switched").hidden = true; pendingApply = lastCheck ? lastCheck.race_id : 0; $("btn-session").onclick(); };

  // ── session dialog ────────────────────────────────────────────
  var dlg = $("session-dialog");
  var trackPicker = Splitter.trackPicker($("track-picker"), { recent: [] });
  var quadPicker = Splitter.quadPicker($("quad-picker"));

  $("btn-session").onclick = function () {
    var s = state.session || {};
    trackPicker.set({ track_name: s.track_name || "", scenery: s.scenery || "", track_id: s.track_id || 0, scene_id: s.scene_id || 0, track_source: s.track_source || "" });
    $("f-size").value = s.quad_size || ""; $("f-laps").value = s.race_laps || "";
    quadPicker.set(s.quad_model_id || 0, s.quad_class_id || 0);
    fetch("/api/races?limit=200").then(function (r) { return r.json(); }).then(function (rows) {
      var seen = {}, recent = [];
      rows.forEach(function (r) {
        if (r.track_id && !seen[r.track_id]) { seen[r.track_id] = 1; recent.push({ track_name: r.track_name, scenery: r.scenery, track_id: r.track_id, scene_id: r.scene_id, track_source: r.track_source }); }
      });
      trackPicker.setRecent(recent); trackPicker.showRecent();
    }).catch(function () {});
    $("apply-last-wrap").hidden = !pendingApply; $("apply-last-id").textContent = "#" + pendingApply; $("apply-last").checked = true;
    dlg.showModal();
  };
  $("session-cancel").onclick = function () { dlg.close(); pendingApply = 0; };
  $("session-form").onsubmit = function (ev) {
    ev.preventDefault();
    var fd = new FormData(ev.target);
    var body = { track_name: fd.get("track_name"), scenery: fd.get("scenery"), quad_type: fd.get("quad_type"), quad_size: fd.get("quad_size"), race_laps: parseInt(fd.get("race_laps") || "0", 10) || 0,
                 track_id: parseInt(fd.get("track_id") || "0", 10) || 0, scene_id: parseInt(fd.get("scene_id") || "0", 10) || 0, track_source: fd.get("track_source") || "",
                 quad_model_id: parseInt(fd.get("quad_model_id") || "0", 10) || 0, quad_class_id: parseInt(fd.get("quad_class_id") || "0", 10) || 0 };
    if (!body.track_id) { toast("Pick the track from the search — PBs need its online id", "warn"); return; }
    if (pendingApply && $("apply-last").checked) body.apply_to_race_id = pendingApply;
    fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || "save failed"); }); return r.json(); })
      .then(function (j) { dlg.close(); toast(j.applied ? "Track set and applied to run #" + pendingApply : "Track set", "ok"); pendingApply = 0; })
      .catch(function (e) { toast(e.message, "bad"); });
  };
  // Fullscreen (tablet): the Fullscreen API works over plain http on the LAN,
  // unlike "install as app", which needs https.
  // Two modes: "fullscreen" keeps the header/menus; "focus" is fullscreen
  // with the header hidden so the race card gets the whole screen.
  var root = document.documentElement, fsBtn = $("btn-fullscreen"), focusBtn = $("btn-focus");
  if (!root.requestFullscreen) { fsBtn.hidden = true; focusBtn.hidden = true; }
  function enterFullscreen() {
    return root.requestFullscreen({ navigationUI: "hide" }).catch(function () { root.classList.remove("focus"); toast("Fullscreen refused by the browser", "warn"); });
  }
  function paintFsButtons() {
    var fs = !!document.fullscreenElement, focus = root.classList.contains("focus");
    fsBtn.innerHTML = fs && !focus ? '<span class="ico">🡼</span> Exit fullscreen' : '<span class="ico">⛶</span> Fullscreen'; fsBtn.title = fs ? "leave fullscreen" : "fullscreen, menus visible";
    focusBtn.classList.toggle("primary", focus); focusBtn.innerHTML = focus ? '<span class="ico">☰</span> Show menus' : '<span class="ico">◉</span> Focus'; focusBtn.title = focus ? "show menus" : "focus: fullscreen, race only";
  }
  // Installed as an app or inside the native shell there is no browser chrome
  // to hide, so focus is just the layout (and the plain-fullscreen button is gone).
  var standalone = root.classList.contains("standalone") || root.classList.contains("desktop");
  fsBtn.onclick = function () {
    if (document.fullscreenElement) { document.exitFullscreen(); return; }
    root.classList.remove("focus"); enterFullscreen();
  };
  focusBtn.onclick = function () {
    if (root.classList.contains("focus")) { root.classList.remove("focus"); paintFsButtons(); return; }
    root.classList.add("focus");
    // Installed as an app there is no browser chrome to hide, so focus is just the layout.
    if (document.fullscreenElement || standalone) paintFsButtons(); else enterFullscreen();
  };
  document.addEventListener("fullscreenchange", function () { if (!document.fullscreenElement && !standalone) root.classList.remove("focus"); paintFsButtons(); });

  $("btn-abort").onclick = function () {
    if (!confirm("Abort this run? It is kept as aborted, and the game is told to abort too.")) return;
    fetch("/api/race/abort", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ in_game: true }) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || "failed"); return j; }); })
      .then(function (j) { toast(j.aborted ? (j.in_game ? "Aborted here and in the game" : "Aborted (game not connected)") : "No run to abort"); })
      .catch(function (e) { toast(e.message, "bad"); });
  };

  $("btn-reconnect").onclick = function () {
    fetch("/api/connection", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "connect" }) })
      .then(function (r) { if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || "failed"); }); toast("Reconnecting…"); })
      .catch(function (e) { toast(e.message + " — set the game PC address in Settings", "warn"); });
  };

  // Rendering must never keep the live socket from opening.
  try { applySnapshot(window.SPLITTER_SNAPSHOT || {}); } catch (e) { console.error("initial render failed", e); }
  connect();
})();
