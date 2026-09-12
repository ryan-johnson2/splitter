/* Races page: selection helpers and the bulk-edit preview (#1).
   - header checkbox selects everything in that table; ⇡ on a row selects it and
     every newer run in the same table; shift-click selects a range
   - the bulk section opens itself when something is selected and shows what
     Apply would change before it is pressed */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var pickers = null, lastClicked = null;

  function boxes() { return Array.prototype.slice.call(document.querySelectorAll("input[name=race_ids]")); }
  function selected() { return boxes().filter(function (b) { return b.checked; }); }
  function tableOf(el) { return el.closest("table"); }

  function refresh() {
    var sel = selected(), n = sel.length, details = $("bulk");
    $("bulk-count").textContent = n ? n + " selected" : "none selected";
    $("bulk-count").className = "pill " + (n ? "accent" : "dim");
    $("bulk-delete").disabled = !n;
    if (n && !details.open) details.open = true;
    document.querySelectorAll("input.select-all").forEach(function (all) {
      var mine = Array.prototype.slice.call(tableOf(all).querySelectorAll("input[name=race_ids]"));
      var on = mine.filter(function (b) { return b.checked; }).length;
      all.checked = mine.length > 0 && on === mine.length; all.indeterminate = on > 0 && on < mine.length;
    });
    // Preview of what Apply would do.
    var form = $("bulk-form");
    var trackId = parseInt(form.querySelector("[name=track_id]").value, 10) || 0, trackName = form.querySelector("[name=track_name]").value;
    var quadModel = parseInt(form.querySelector("[name=quad_model_id]").value, 10) || 0, quadName = form.querySelector("[name=quad_type]").value;
    var parts = [], changing = 0, groups = {};
    if (!n) { $("bulk-preview").textContent = "Select runs and pick a track or quad to see what would change."; $("bulk-apply").disabled = true; return; }
    if (!trackId && !quadModel) { $("bulk-preview").textContent = n + " selected — pick a track and/or a quad above to re-attribute them, or delete them."; $("bulk-apply").disabled = true; return; }
    sel.forEach(function (b) {
      var tChange = trackId && parseInt(b.dataset.trackId, 10) !== trackId, qChange = quadModel && parseInt(b.dataset.quadModel, 10) !== quadModel;
      if (tChange || qChange) changing++;
      groups[b.dataset.trackId + "/" + b.dataset.quadModel + "/" + b.dataset.laps] = 1;
      if (b.dataset.best === "1" && (tChange || qChange)) groups.__pbMoves = (groups.__pbMoves || 0) + 1;
    });
    var pbMoves = groups.__pbMoves || 0; delete groups.__pbMoves;
    if (trackId) parts.push("track → " + trackName + " (#" + trackId + ")");
    if (quadModel) parts.push("quad → " + quadName);
    var text = n + " selected: " + parts.join(", ") + ". " + changing + " run" + (changing === 1 ? "" : "s") + " actually change" + (changing === 1 ? "s" : "") + "; " + Object.keys(groups).length + " PB group" + (Object.keys(groups).length === 1 ? "" : "s") + " recalculated";
    if (pbMoves) text += "; " + pbMoves + " current PB" + (pbMoves === 1 ? "" : "s") + " will be re-decided";
    $("bulk-preview").textContent = text + ".";
    $("bulk-apply").disabled = changing === 0;
  }

  function init(p) {
    pickers = p;
    document.addEventListener("change", function (ev) {
      var t = ev.target;
      if (t.classList && t.classList.contains("select-all")) {
        tableOf(t).querySelectorAll("input[name=race_ids]").forEach(function (b) { b.checked = t.checked; });
      }
      if (t.name === "race_ids" || t.classList.contains("select-all") || t.closest("#bulk")) refresh();
    });
    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (t.classList && t.classList.contains("since")) {
        var row = t.closest("tr"), rows = Array.prototype.slice.call(tableOf(t).querySelectorAll("tr[data-id]"));
        rows.slice(0, rows.indexOf(row) + 1).forEach(function (r) { r.querySelector("input[name=race_ids]").checked = true; });  // rows are newest first
        refresh(); return;
      }
      if (t.name === "race_ids") {
        if (ev.shiftKey && lastClicked && tableOf(lastClicked) === tableOf(t)) {
          var all = Array.prototype.slice.call(tableOf(t).querySelectorAll("input[name=race_ids]"));
          var a = all.indexOf(lastClicked), b = all.indexOf(t), lo = Math.min(a, b), hi = Math.max(a, b);
          all.slice(lo, hi + 1).forEach(function (x) { x.checked = t.checked; });
        }
        lastClicked = t; refresh();
      }
    });
    $("bulk-clear").onclick = function () { boxes().forEach(function (b) { b.checked = false; }); refresh(); };
    refresh();
  }
  init.refresh = refresh;
  window.Splitter = window.Splitter || {};
  window.Splitter.racesPage = init;
  Object.defineProperty(init, "quad", { get: function () { return pickers && pickers.quad; } });
})();
