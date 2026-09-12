/* Shared time formatting; honours the Settings page "Time format" (window.SPLITTER_TIME_FORMAT). */
(function () {
  "use strict";
  function fmt(ms, style) {
    if (ms == null || ms < 0) return "--";
    style = style || window.SPLITTER_TIME_FORMAT || "seconds";
    var secondsOnly = (ms / 1000).toFixed(3);
    if (style === "seconds") return secondsOnly;
    var m = Math.floor(ms / 60000), s = (ms % 60000) / 1000;
    var mmss = m ? m + ":" + s.toFixed(3).padStart(6, "0") : secondsOnly;
    return style === "both" && m ? mmss + " (" + secondsOnly + ")" : mmss;
  }
  window.Splitter = window.Splitter || {};
  window.Splitter.fmt = fmt;
})();
