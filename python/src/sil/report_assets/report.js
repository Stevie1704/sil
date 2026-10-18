// SiL diagnostics report: optional conveniences. The page is complete
// without this script; it never parses the report JSON, so no number loses
// precision here. Sort keys are the exact JSON values as text.
(function () {
  "use strict";
  var root = document.documentElement;

  // --- theme -------------------------------------------------------------
  var themes = ["auto", "light", "dark"];
  var themeButton = document.getElementById("theme");
  function applyTheme(theme) {
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    themeButton.textContent = "Theme: " + theme;
  }
  var saved = "auto";
  try { saved = localStorage.getItem("sil-report-theme") || "auto"; }
  catch (e) { saved = "auto"; }
  if (themes.indexOf(saved) < 0) saved = "auto";
  applyTheme(saved);
  themeButton.hidden = false;
  themeButton.addEventListener("click", function () {
    var current = root.getAttribute("data-theme") || "auto";
    var next = themes[(themes.indexOf(current) + 1) % themes.length];
    applyTheme(next);
    try { localStorage.setItem("sil-report-theme", next); } catch (e) {}
  });

  // --- print -------------------------------------------------------------
  var printButton = document.getElementById("print");
  printButton.hidden = false;
  printButton.addEventListener("click", function () { window.print(); });
  var closed = [];
  window.addEventListener("beforeprint", function () {
    closed = Array.prototype.filter.call(
      document.querySelectorAll("details"), function (d) { return !d.open; });
    closed.forEach(function (d) { d.open = true; });
  });
  window.addEventListener("afterprint", function () {
    closed.forEach(function (d) { d.open = false; });
    closed = [];
  });

  // --- download: a Blob, because a long data: URL can be refused ---------
  var download = document.getElementById("download");
  try {
    var encoded = download.getAttribute("href").split(",", 2)[1];
    var binary = atob(encoded);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    download.href = URL.createObjectURL(
      new Blob([bytes], { type: "application/json" }));
  } catch (e) { /* the data: URL stays */ }

  // --- sorting -----------------------------------------------------------
  var integer = /^-?\d+$/;
  function compareKeys(a, b) {
    if (a === b) return 0;
    if (a === "") return 1;   // unavailable values sort last
    if (b === "") return -1;
    if (integer.test(a) && integer.test(b)) {
      var x = BigInt(a), y = BigInt(b);
      return x < y ? -1 : x > y ? 1 : 0;
    }
    var p = Number(a), q = Number(b);
    if (!isNaN(p) && !isNaN(q) && a.trim() !== "" && b.trim() !== "") {
      return p - q;
    }
    return a.localeCompare(b);
  }
  function sortKey(row, column) {
    var cell = row.cells[column];
    if (!cell) return "";
    var key = cell.getAttribute("data-sort");
    return key !== null ? key : cell.textContent.trim();
  }
  Array.prototype.forEach.call(
    document.querySelectorAll("table[data-sortable]"), function (table) {
      var headers = table.tHead.rows[0].cells;
      Array.prototype.forEach.call(headers, function (th, column) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "sort";
        while (th.firstChild) button.appendChild(th.firstChild);
        th.appendChild(button);
        th.setAttribute("aria-sort", "none");
        button.addEventListener("click", function () {
          var ascending = th.getAttribute("aria-sort") !== "ascending";
          Array.prototype.forEach.call(headers, function (other) {
            other.setAttribute("aria-sort", "none");
          });
          th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
          var body = table.tBodies[0];
          var rows = Array.prototype.slice.call(body.rows);
          rows.forEach(function (row, index) { row._order = index; });
          rows.sort(function (r, s) {
            var a = sortKey(r, column), b = sortKey(s, column);
            if (a === "" || b === "") return compareKeys(a, b);
            var order = compareKeys(a, b);
            return (ascending ? order : -order) || r._order - s._order;
          });
          rows.forEach(function (row) { body.appendChild(row); });
        });
      });
    });

  // --- filtering ---------------------------------------------------------
  Array.prototype.forEach.call(
    document.querySelectorAll("input[data-filter]"), function (input) {
      var table = document.getElementById(input.getAttribute("data-filter"));
      var count = document.getElementById(table.id + "-count");
      input.hidden = false;
      input.addEventListener("input", function () {
        var needle = input.value.trim().toLowerCase();
        var shown = 0, rows = table.tBodies[0].rows;
        Array.prototype.forEach.call(rows, function (row) {
          var match = row.textContent.toLowerCase().indexOf(needle) >= 0;
          row.hidden = !match;
          if (match) shown++;
        });
        count.textContent = needle ? shown + " of " + rows.length + " rows"
          : "";
      });
    });

  // --- Run selector (benchmark) ------------------------------------------
  var select = document.getElementById("run-select");
  if (select) {
    var details = document.querySelectorAll(".run-detail");
    var show = function (value) {
      Array.prototype.forEach.call(details, function (detail) {
        detail.hidden = value !== "all" &&
          detail.getAttribute("data-run") !== value;
      });
    };
    select.parentNode.parentNode.hidden = false;
    select.addEventListener("change", function () { show(select.value); });
    var fromHash = function () {
      var match = /^#run-(\d+)(?:-|$)/.exec(location.hash);
      if (match) {
        select.value = match[1];
        show(match[1]);
        var target = document.getElementById(location.hash.slice(1));
        if (target) target.scrollIntoView();
      }
    };
    window.addEventListener("hashchange", fromHash);
    if (/^#run-\d+/.test(location.hash)) fromHash();
    else {
      select.value = select.getAttribute("data-default") || "all";
      show(select.value);
    }
  }
})();
