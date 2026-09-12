/* Shared track + quad pickers. Used by the live "Track…" dialog, the race edit
   form and the races bulk-edit form. Plain DOM, no dependencies.

   Splitter.trackPicker(root, opts)  root = element containing:
       .tp-search (input), .tp-source (select), .tp-results (div), .tp-picked (div),
       [name=track_name], [name=scenery], [name=track_id], [name=scene_id], [name=track_source]
     opts.recent: array of {track_name, scenery, track_id, scene_id, track_source} shown when the box is empty
     opts.onPick(t): optional
   Splitter.quadPicker(root)  root = element containing:
       .qp-class (select), .qp-model (select), [name=quad_type], [name=quad_model_id], [name=quad_class_id]
*/
(function () {
  "use strict";
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function q(root, sel) { return root.querySelector(sel); }
  function named(root, name) { return root.querySelector("[name=" + name + "]"); }

  function trackPicker(root, opts) {
    opts = opts || {};
    var search = q(root, ".tp-search"), source = q(root, ".tp-source"), results = q(root, ".tp-results"), picked = q(root, ".tp-picked");
    var fName = named(root, "track_name"), fScenery = named(root, "scenery"), fId = named(root, "track_id"), fScene = named(root, "scene_id"), fSource = named(root, "track_source");
    var timer = null, seq = 0;

    function renderPicked() {
      var id = parseInt(fId.value, 10) || 0;
      if (!picked) return;
      picked.innerHTML = id
        ? "Picked <b>" + esc(fName.value) + "</b> <span class='pill dim'>" + esc(fSource.value) + " #" + id + "</span>" + (fScenery && fScenery.value ? " <span class='muted small'>" + esc(fScenery.value) + "</span>" : "")
        : "<span class='muted'>" + esc(opts.emptyText || "No track picked yet — search above.") + "</span>";
    }
    function set(t) {
      fName.value = t.track_name || t.name || "";
      if (fScenery && t.scenery) fScenery.value = t.scenery;
      fId.value = t.track_id || 0; fScene.value = t.scene_id || 0; fSource.value = t.track_source || t.source || "";
      renderPicked();
      if (opts.onPick) opts.onPick(t);
    }
    function row(t, tag) {
      var b = document.createElement("button"); b.type = "button"; b.className = "picker-row";
      b.innerHTML = "<span class='name'>" + esc(t.track_name || t.name) + "</span><span class='meta'>"
        + (tag ? "<span class='pill dim'>" + esc(tag) + "</span> " : "") + esc(t.scenery || "")
        + (t.kind ? " · " + esc(t.kind) : "") + (t.author ? " · " + esc(t.author) : "")
        + (t.track_id ? " <span class='muted'>#" + t.track_id + "</span>" : "") + "</span>";
      b.onclick = function () { set(t); results.innerHTML = ""; search.value = ""; };
      return b;
    }
    function head(text, cls) { var d = document.createElement("div"); d.className = "picker-head " + (cls || "muted small"); d.textContent = text; return d; }
    function render(res, query) {
      results.innerHTML = "";
      if (!query) {
        var recent = opts.recent || [];
        if (recent.length) { results.appendChild(head("Recent")); recent.slice(0, 8).forEach(function (t) { results.appendChild(row(t, t.track_source || "")); }); }
        return;
      }
      if (res.available === false) { results.appendChild(head("This build has no online track lists — enter the track id by hand below.", "muted small")); return; }
      (res.tracks || []).forEach(function (t) { results.appendChild(row(t, t.source)); });
      var errs = Object.keys(res.errors || {});
      if (errs.length) results.appendChild(head(errs.map(function (k) { return k + ": " + res.errors[k]; }).join(" · "), "flash error small"));
      if (!(res.tracks || []).length && !errs.length) results.appendChild(head("No tracks match."));
    }
    function run() {
      var query = search.value.trim(), src = source ? source.value : "", mine = ++seq;
      if (!query) { render({}, ""); return; }
      results.innerHTML = ""; results.appendChild(head("searching…"));
      fetch("/api/tracks/search?q=" + encodeURIComponent(query) + "&source=" + encodeURIComponent(src))
        .then(function (r) { return r.json(); })
        .then(function (res) { if (mine === seq) render(res, query); })
        .catch(function (e) { if (mine === seq) render({ tracks: [], errors: { search: e.message } }, query); });
    }
    // Manual entry: the online id is the PB key, so it can always be typed in
    // (from the game's track browser / velocidrone.co.uk) when search is
    // unavailable or the track is not listed.
    var manual = document.createElement("details"); manual.className = "picker-manual";
    manual.innerHTML = "<summary class='muted small'>Enter a track id by hand</summary>"
      + "<div class='inline-form'><input type='number' min='1' placeholder='online id' class='pm-id' style='width:8rem'>"
      + "<input placeholder='track name' class='pm-name'><select class='pm-source'><option value='community'>community</option><option value='official'>official</option></select>"
      + "<button type='button' class='sm'>Use</button></div>";
    picked.parentNode.insertBefore(manual, picked.nextSibling);
    manual.querySelector("button").onclick = function () {
      var id = parseInt(manual.querySelector(".pm-id").value, 10) || 0, name = manual.querySelector(".pm-name").value.trim();
      if (!id || !name) { manual.querySelector(id ? ".pm-name" : ".pm-id").focus(); return; }
      set({ track_name: name, track_id: id, scene_id: 0, track_source: manual.querySelector(".pm-source").value });
      manual.open = false;
    };
    search.oninput = function () { clearTimeout(timer); timer = setTimeout(run, 350); };
    search.onkeydown = function (ev) { if (ev.key === "Enter") { ev.preventDefault(); clearTimeout(timer); run(); } };
    if (source) source.onchange = run;
    renderPicked();
    return { set: set, clear: function () { fId.value = 0; fScene.value = 0; fSource.value = ""; renderPicked(); }, showRecent: function () { search.value = ""; render({}, ""); }, setRecent: function (r) { opts.recent = r; } };
  }

  var quadCatalog = null;
  function loadQuads() {
    if (quadCatalog) return Promise.resolve(quadCatalog);
    return fetch("/api/quads").then(function (r) { return r.json(); }).then(function (c) { quadCatalog = c; return c; });
  }

  function quadPicker(root) {
    var cls = q(root, ".qp-class"), model = q(root, ".qp-model");
    var fType = named(root, "quad_type"), fModel = named(root, "quad_model_id"), fClass = named(root, "quad_class_id");
    var catalog = null;
    function fillModels(classId, selectedModel) {
      model.innerHTML = "<option value='0'>" + (classId ? "— pick a quad —" : "— any / unknown —") + "</option>";
      if (!catalog) return;
      catalog.classes.forEach(function (c) {
        if (classId && c.class_id !== classId) return;
        c.models.forEach(function (m) {
          var o = document.createElement("option"); o.value = m.model_id; o.textContent = classId ? m.name : m.name + " (" + m.class_name + ")";
          if (m.model_id === selectedModel) o.selected = true;
          model.appendChild(o);
        });
      });
    }
    function sync() {
      var mid = parseInt(model.value, 10) || 0, picked = null;
      if (catalog) catalog.classes.forEach(function (c) { c.models.forEach(function (m) { if (m.model_id === mid) picked = { m: m, c: c }; }); });
      fModel.value = mid;
      fClass.value = picked ? picked.c.class_id : (parseInt(cls.value, 10) || 0);
      if (fType && picked) fType.value = picked.m.name;
      if (fType && !picked && fType.dataset.auto === "1") fType.value = "";
    }
    cls.onchange = function () { fillModels(parseInt(cls.value, 10) || 0, 0); sync(); };
    model.onchange = sync;
    function set(modelId, classId) {
      var ready = loadQuads().then(function (c) {
        catalog = c;
        cls.innerHTML = "<option value='0'>All classes</option>";
        c.classes.forEach(function (k) { var o = document.createElement("option"); o.value = k.class_id; o.textContent = k.class_name; cls.appendChild(o); });
        if (modelId && !classId) c.classes.forEach(function (k) { k.models.forEach(function (m) { if (m.model_id === modelId) classId = k.class_id; }); });
        cls.value = classId || 0;
        fillModels(classId || 0, modelId || 0);
        if (modelId) model.value = modelId;
        sync();
      });
      return ready;
    }
    return { set: set };
  }

  window.Splitter = window.Splitter || {};
  window.Splitter.trackPicker = trackPicker;
  window.Splitter.quadPicker = quadPicker;
})();
