// SiL profile report: filters of a CPU profile (#318). The page is complete
// without this script. It reads the inert JSON block #profile-data, builds
// every element with textContent (never markup from data), and builds the
// caller tree with the bounds and order of sil.profile_html_parts, so an
// empty filter gives the tree that Python wrote. Virtual times are decimal
// strings compared as BigInt, so they stay exact.
(function () {
  "use strict";
  var block = document.getElementById("profile-data");
  var form = document.getElementById("explorer");
  if (!block || !form) return;
  var data;
  try { data = JSON.parse(block.textContent); } catch (e) { return; }
  function byId(id) { return document.getElementById(id); }
  var fClass = byId("f-class"), fTask = byId("f-task"),
    fFrom = byId("f-vt-from"), fTo = byId("f-vt-to"),
    fProcess = byId("f-process"), fThread = byId("f-thread");
  var selection = byId("selection"), treeBox = byId("cpu-tree"),
    tables = byId("selection-tables");
  var limits = data.limits;
  var selectedInterval = null, flameZoom = [], currentTree = null;
  form.hidden = false;

  // --- formatting, as sil.diagnostics_format --------------------------------
  function duration(ns) {
    var units = [["s", 1e9], ["ms", 1e6], ["us", 1e3]];
    for (var i = 0; i < units.length; i++) {
      if (Math.abs(ns) >= units[i][1]) {
        return (ns / units[i][1]).toFixed(3) + " " + units[i][0];
      }
    }
    return ns.toFixed(0) + " ns";
  }
  function share(part, whole) {
    return whole ? (100 * part / whole).toFixed(1) + "%" : "-";
  }
  function node(tagName, className, text) {
    var element = document.createElement(tagName);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === null || child === undefined) continue;
      parent.appendChild(typeof child === "string" ?
        document.createTextNode(child) : child);
    }
    return parent;
  }
  function basename(path) { return path.split("/").pop(); }
  function label(key) {
    var fn = data.functions[key], parts = document.createDocumentFragment();
    add(parts, fn[0] !== null ? node("span", "fn", fn[0]) :
      node("span", "na", "[unknown function]"), " ");
    if (fn[1] !== null) {
      var module = node("code", "module", basename(fn[1]));
      module.title = fn[1];
      add(parts, module);
    } else add(parts, node("span", "na", "[unknown module]"));
    return parts;
  }

  // --- the filter ------------------------------------------------------------
  function exactValue(input) {
    if (!input) return null;
    var text = input.value.trim();
    if (text === "") return null;
    if (!/^\d+$/.test(text)) return undefined;
    return BigInt(text);
  }
  function activationTest() {
    var task = fTask ? fTask.value : "";
    var from = exactValue(fFrom), to = exactValue(fTo);
    if (from === undefined || to === undefined) return undefined;
    if (task === "" && from === null && to === null) return null;
    return function (index) {
      var a = data.activations[index];
      if (task !== "" && a[0] !== +task) return false;
      var virtual = BigInt(a[1]);
      if (from !== null && virtual < from) return false;
      if (to !== null && virtual >= to) return false;  // [from, to)
      return true;
    };
  }
  function sampleTest(byActivation) {
    var cls = fClass ? fClass.value : "";
    var process = fProcess ? fProcess.value : "";
    var thread = fThread ? fThread.value : "";
    return function (row) {  // [path, thread, class, ref, samples, weight]
      if (selectedInterval) {
        var kind = selectedInterval.kind;
        if (kind === "activation") {
          if ((row[2] !== "execution_context" && row[2] !== "window_associated") ||
              row[3] !== selectedInterval.ref) return false;
        } else if (row[2] !== kind || row[3] !== selectedInterval.ref) return false;
      }
      if (cls !== "" && row[2] !== cls) return false;
      if (thread !== "" && row[1] !== thread) return false;
      if (process !== "" && data.threads[row[1]] !== process) return false;
      if (byActivation) {
        if (row[2] !== "execution_context" && row[2] !== "window_associated") {
          return false;
        }
        return byActivation(row[3]);
      }
      return true;
    };
  }

  // --- the tree, as sil.profile_html_parts.build_tree / render_tree ----------
  function newNode() {
    return { count: 0, weight: 0, selfCount: 0, selfWeight: 0,
             children: new Map() };
  }
  function buildTree(rows) {
    var root = newNode();
    rows.forEach(function (row) {
      var current = root, count = row[4], weight = row[5];
      current.count += count; current.weight += weight;
      data.paths[row[0]].forEach(function (key) {
        var next = current.children.get(key);
        if (!next) { next = newNode(); current.children.set(key, next); }
        next.count += count; next.weight += weight;
        current = next;
      });
      current.selfCount += count; current.selfWeight += weight;
    });
    return root;
  }
  function renderTree(root) {
    if (root.count === 0) {
      return node("p", "note", "No sample matches the filters.");
    }
    var budget = limits.max_nodes;
    var floor = Math.max(1, root.weight * limits.min_share);
    function numbers(n) {
      var values = node("span", "tree-values");
      add(values, node("span", "num", duration(n.weight)),
        " " + share(n.weight, root.weight), " · " + n.count + " samples",
        n.selfCount ? " · self " + duration(n.selfWeight) : null);
      return values;
    }
    function other(text, count, weight) {
      return add(node("li", "tree-other"), node("span", "na", text), " ",
        node("span", "num", duration(weight)),
        " " + share(weight, root.weight) + " · " + count + " samples");
    }
    function children(n, depth) {
      var ordered = Array.from(n.children.entries()).sort(function (a, b) {
        return b[1].weight - a[1].weight || a[0] - b[0];
      });
      var shown = [], hidden = [];
      ordered.forEach(function (entry) {
        if (shown.length < limits.max_children && entry[1].weight >= floor &&
            depth < limits.max_depth && budget > 0) {
          budget -= 1;
          shown.push(entry);
        } else hidden.push(entry[1]);
      });
      var items = shown.map(function (entry) {
        return item(entry[0], entry[1], depth + 1);
      });
      if (hidden.length) {
        var text = depth >= limits.max_depth ?
          hidden.length + " deeper callees not shown" :
          hidden.length + " other callees";
        items.push(other(text,
          hidden.reduce(function (s, c) { return s + c.count; }, 0),
          hidden.reduce(function (s, c) { return s + c.weight; }, 0)));
      }
      return items;
    }
    function item(key, n, depth) {
      var head = document.createDocumentFragment();
      add(head, label(key), " ", numbers(n));
      var below = children(n, depth);
      if (!below.length) return add(node("li"), add(node("div", "tree-leaf"),
        head));
      var details = node("details");
      details.open = depth <= 2;
      add(details, add(node("summary"), head),
        add.apply(null, [node("ul")].concat(below)));
      return add(node("li"), details);
    }
    var top = node("details");
    top.open = true;
    add(top, add(node("summary"), node("strong", null, "All"), " ",
      numbers(root)), add.apply(null, [node("ul")].concat(children(root, 0))));
    var list = node("ul", "tree");
    list.setAttribute("role", "list");
    return add(list, add(node("li"), top));
  }

  // --- the hotspot tables of the selection -----------------------------------
  function ranking(rows) {
    var self = new Map(), inclusive = new Map();
    function bump(map, key, row) {
      var entry = map.get(key) || [0, 0];
      entry[0] += row[4]; entry[1] += row[5];
      map.set(key, entry);
    }
    rows.forEach(function (row) {
      var path = data.paths[row[0]];
      if (path.length) bump(self, path[path.length - 1], row);
      new Set(path).forEach(function (key) { bump(inclusive, key, row); });
    });
    function top(map) {
      return Array.from(map.entries()).sort(function (a, b) {
        return b[1][1] - a[1][1] || a[0] - b[0];
      }).slice(0, limits.rows);
    }
    return { self: top(self), inclusive: top(inclusive) };
  }
  function hotspotTable(caption, entries, count, weight, footer) {
    var table = node("table");
    add(table, node("caption", null, caption));
    var head = add(node("thead"), node("tr"));
    ["#", "Function and module", "Samples", "Weight", "Share of selection"]
      .forEach(function (text, index) {
        var th = node("th", index === 1 ? null : "numeric", text);
        th.scope = "col";
        head.firstChild.appendChild(th);
      });
    var body = node("tbody");
    entries.forEach(function (entry, index) {
      add(body, add(node("tr"), node("td", "numeric", String(index + 1)),
        add(node("td"), label(entry[0])),
        node("td", "numeric", String(entry[1][0])),
        node("td", "numeric", duration(entry[1][1])),
        node("td", "numeric", share(entry[1][1], weight))));
    });
    add(table, head, body);
    if (footer) {
      var foot = node("tfoot");
      footer.forEach(function (row) {
        add(foot, add(node("tr"), node("td", "numeric", ""),
          node("td", null, row[0]), node("td", "numeric", String(row[1])),
          node("td", "numeric", duration(row[2])),
          node("td", "numeric", share(row[2], weight))));
      });
      add(table, foot);
    }
    var wrap = node("div", "table-wrap");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", caption);
    return add(node("div", "table-block"), add(wrap, table));
  }
  function renderTables(rows, count, weight) {
    var ranked = ranking(rows);
    var listed = ranked.self.reduce(function (s, e) {
      return [s[0] + e[1][0], s[1] + e[1][1]];
    }, [0, 0]);
    tables.textContent = "";
    if (!count) return;
    add(tables, node("h3", null, "Hotspots of the selection"),
      hotspotTable("Selection by self weight, top " + limits.rows,
        ranked.self, count, weight, [
          ["Other leaf frames (not listed)", count - listed[0],
           weight - listed[1]],
          ["All selected samples", count, weight]]),
      hotspotTable("Selection by inclusive weight, top " + limits.rows +
        " (inclusive values overlap and do not add up)", ranked.inclusive,
        count, weight, null));
  }

  // --- flame graph: widths partition weight, including self and remainders ---
  function svgNode(name, attributes, text) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attributes || {}).forEach(function (key) {
      el.setAttribute(key, String(attributes[key]));
    });
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function functionName(key) {
    var fn = data.functions[key];
    return (fn[0] || "[unknown function]") + " (" +
      (fn[1] || "[unknown module]") + ")";
  }
  function renderFlame() {
    var container = byId("cpu-flame");
    if (!container || !currentTree) return;
    var root = currentTree, zoomName = "";
    for (var key of flameZoom) {
      if (!root.children.has(key)) { flameZoom = []; root = currentTree; break; }
      root = root.children.get(key); zoomName = functionName(key);
    }
    byId("flame-total").textContent = "Total: " + currentTree.count + " samples / " +
      currentTree.weight + " ns sampled weight. Denominator of the selection." +
      (flameZoom.length ? " Zoom: " + zoomName + ", " + root.count + " samples / " +
       root.weight + " ns; widths use zoom weight." : " Widths use this denominator.");
    var svg = svgNode("svg", {id: "cpu-flame-svg", class: "flame-graph", role: "group",
      "aria-label": "Flame graph: width is sample weight", "data-weight": root.weight});
    var budget = limits.max_nodes, deepest = 0;
    var query = byId("flame-search").value.trim().toLowerCase(), matches = 0;
    function box(name, count, weight, x, depth, path) {
      deepest = Math.max(deepest, depth);
      var width = root.weight ? 1000 * weight / root.weight : 0;
      var title = name + ": " + count + " samples, " + weight + " ns (" + share(weight, root.weight) + ")";
      var matching = query && name.toLowerCase().includes(query);
      if (matching) matches++;
      var g = svgNode("g", {tabindex: "0", role: path === null ? "img" : "button",
        "aria-label": title, "data-name": name, "data-weight": weight,
        class: "flame-frame" + (matching ? " search-match" : "")});
      if (path !== null) g.setAttribute("data-flame-path", path.join(","));
      var characters = Math.max(0, Math.floor(width / 7) - 2);
      var clip = "cpu-flame-clip-" + svg.children.length;
      add(g, add(svgNode("defs"), add(svgNode("clipPath", {id:clip}),
        svgNode("rect", {x:x+2,y:depth*28,width:Math.max(0,width-4),height:26}))),
        svgNode("title", {}, title), svgNode("rect", {x: x, y: depth * 28,
        width: width, height: 26, class: "flame-rect"}),
        svgNode("text", {x: x+3, y: depth*28+18, class: "flame-label", "clip-path":"url(#"+clip+")"},
          characters < 2 ? "" : name.length <= characters ? name :
            name.slice(0, Math.max(0, characters-1)) + "…"));
      svg.appendChild(g);
    }
    function walk(n, x, depth, path) {
      var shown = [], hidden = [];
      Array.from(n.children.entries()).sort(function (a,b) {
        return b[1].weight-a[1].weight || a[0]-b[0];
      }).forEach(function (entry) {
        if (budget > 0 && depth < limits.max_depth && shown.length < limits.max_children &&
            entry[1].weight >= Math.max(1, root.weight*limits.min_share)) {
          budget--; shown.push(entry);
        } else hidden.push(entry[1]);
      });
      shown.forEach(function (entry) {
        var p = path.concat(entry[0]);
        box(functionName(entry[0]), entry[1].count, entry[1].weight, x, depth+1, p);
        walk(entry[1], x, depth+1, p);
        x += 1000*entry[1].weight/root.weight;
      });
      if (hidden.length) {
        var weight = hidden.reduce(function(s,n) { return s+n.weight; },0);
        box(hidden.length + " other / deeper callees not shown",
          hidden.reduce(function(s,n) { return s+n.count; },0), weight, x, depth+1, null);
        x += 1000*weight/root.weight;
      }
      if (n.selfCount) box("[self / stack ends here; possibly cut off]",
        n.selfCount, n.selfWeight, x, depth+1, null);
    }
    if (root.weight) {
      box(flameZoom.length ? zoomName : "All samples", root.count, root.weight, 0,0, flameZoom);
      walk(root,0,0,flameZoom);
    }
    svg.setAttribute("viewBox", "0 0 1000 " + ((deepest+1)*28));
    container.replaceChildren(root.weight ? svg : node("p", "note",
      "No sampled weight to draw. No sample is not evidence of no CPU use."));
    byId("flame-search-status").textContent = query ? matches +
      " matching visible frames; bounded remainders may contain more matches." : "";
  }
  if (byId("flame-controls")) {
    byId("flame-controls").hidden = false;
    byId("flame-search").addEventListener("input", renderFlame);
    byId("flame-reset").addEventListener("click", function () {
      flameZoom = []; byId("flame-search").value = ""; renderFlame();
    });
  }
  function activateGraphic(event) {
    if (event.type === "keydown" && event.key !== "Enter" && event.key !== " ") return;
    var frame = event.target.closest("[data-flame-path]");
    if (frame && frame.closest("#cpu-flame")) {
      event.preventDefault();
      flameZoom = frame.dataset.flamePath === "" ? [] : frame.dataset.flamePath.split(",").map(Number);
      renderFlame();
      var focused = byId("cpu-flame").querySelector("[data-flame-path]");
      if (focused) focused.focus();
    }
    var interval = event.target.closest("[data-interval-kind]");
    if (interval) {
      event.preventDefault(); form.reset();
      var kind = interval.dataset.intervalKind;
      selectedInterval = {kind: kind, ref: kind === "lifecycle" ?
        interval.dataset.intervalPhase : +interval.dataset.intervalRef};
      flameZoom = []; update();
    }
  }
  document.addEventListener("click", activateGraphic);
  document.addEventListener("keydown", activateGraphic);
  document.addEventListener("focus", function(event) {
    var frame = event.target.closest(".flame-frame");
    if (frame && byId("flame-focus")) byId("flame-focus").textContent = frame.getAttribute("aria-label");
  }, true);
  document.querySelectorAll("a[href^='#task-detail-']").forEach(function (link) {
    link.addEventListener("click", function () {
      var target = document.querySelector(link.getAttribute("href"));
      if (target) target.open = true;
    });
  });
  document.querySelectorAll("[data-task-filter]").forEach(function (link) {
    link.addEventListener("click", function () {
      form.reset(); selectedInterval = null; flameZoom = [];
      fTask.value = link.dataset.taskFilter; fClass.value = link.dataset.classFilter;
      update();
    });
  });

  // --- update ----------------------------------------------------------------
  function update() {
    var byActivation = data.activations ? activationTest() : null;
    if (byActivation === undefined) {
      selection.textContent = "A Virtual time must be a whole number of " +
        "nanoseconds.";
      return;
    }
    var test = sampleTest(byActivation);
    var rows = data.rows.filter(test);
    var count = 0, weight = 0;
    rows.forEach(function (row) { count += row[4]; weight += row[5]; });
    selection.textContent = count + " of " + data.total[0] + " samples, " +
      duration(weight) + " of " + duration(data.total[1]) +
      " sampled weight (" + share(weight, data.total[1]) +
      ")" + (count ? "" : ": no sample matches the filters");
    if (selectedInterval) selection.textContent += "; interval " + selectedInterval.kind +
      " " + selectedInterval.ref;
    if (data.activations) selection.textContent += "; " +
      ["execution_context", "window_associated", "lifecycle", "replay", "unattributed"].map(function (cls) {
        var group = rows.filter(function(r) { return r[2] === cls; });
        return cls + ": " + group.reduce(function(s,r) { return s+r[4]; },0) +
          " samples / " + group.reduce(function(s,r) { return s+r[5]; },0) + " ns";
      }).join("; ");
    if (byId("timeline-selection")) byId("timeline-selection").textContent =
      "Selected: " + selection.textContent + ". Geometry and density show the saved capture context.";
    var intervalTotals = new Map();
    rows.forEach(function(r) {
      var kind = r[2] === "execution_context" || r[2] === "window_associated" ? "activation" : r[2];
      var key = kind + ":" + r[3], sum = intervalTotals.get(key) || [0,0];
      sum[0] += r[4]; sum[1] += r[5]; intervalTotals.set(key,sum);
    });
    document.querySelectorAll(".timeline-interval").forEach(function (interval) {
      var kind = interval.dataset.intervalKind, ref = kind === "lifecycle" ?
        interval.dataset.intervalPhase : +interval.dataset.intervalRef;
      var sum = intervalTotals.get(kind + ":" + ref) || [0,0];
      interval.dataset.selectedSamples = sum[0];
      interval.dataset.selectedWeight = sum[1];
      var saved = interval.querySelector("title").textContent;
      interval.setAttribute("aria-label", saved + " Selected: " + interval.dataset.selectedSamples +
        " samples / " + interval.dataset.selectedWeight + " ns.");
    });
    treeBox.textContent = "";
    currentTree = buildTree(rows);
    treeBox.appendChild(renderTree(currentTree));
    renderFlame();
    renderTables(rows, count, weight);
    var table = byId("activation-table");
    if (table) {
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        var index = /^activation-(\d+)$/.exec(row.id);
        if (index) row.hidden = !!byActivation && !byActivation(+index[1]);
      });
    }
  }
  function filterChanged() { selectedInterval = null; flameZoom = []; update(); }
  form.addEventListener("input", filterChanged);
  form.addEventListener("change", filterChanged);
  form.addEventListener("submit", function (event) { event.preventDefault(); });
  form.addEventListener("reset", function () { selectedInterval = null; flameZoom = []; setTimeout(update, 0); });
  update();
})();
