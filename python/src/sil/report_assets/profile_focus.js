// Bottom-up caller analysis of retained normalized rows. Data is always text.
(function () {
  "use strict";
  var block = document.getElementById("profile-data"), controls = document.getElementById("caller-controls");
  if (!block || !controls) return;
  var data;
  try { data = JSON.parse(block.textContent).focus; } catch (e) { return; }
  if (!data || !data.interactive) return;
  function byId(id) { return document.getElementById(id); }
  function node(tag, text) {
    var element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function add(parent) {
    for (var i = 1; i < arguments.length; i++) parent.appendChild(arguments[i]);
    return parent;
  }
  var functions = byId("caller-function"), search = byId("caller-search"),
    metric = byId("caller-metric"), mode = byId("caller-mode"), offset = 0,
    selected = null, rows = data.rows, limits = data.limits;
  function name(key) {
    var fn = data.functions[key];
    return (fn[0] || "[unknown function]") + " (" + (fn[1] || "[unknown module]") + ")";
  }
  function value(row) {
    if (data.kind === "cpu") return row[5];
    return row[4 + data.metrics.indexOf(metric.value)];
  }
  function share(part, total) { return total ? (100 * part / total).toFixed(1) + "%" : "-"; }
  function newNode() { return {count:0, weight:0, own:0, ownWeight:0, children:new Map()}; }
  function candidates() {
    var query = search.value.trim().toLowerCase();
    return data.functions.map(function (_, key) { return key; }).filter(function (key) {
      return name(key).toLowerCase().includes(query);
    });
  }
  function options() {
    var matches = candidates();
    offset = Math.min(offset, Math.max(0, Math.floor((matches.length - 1) / limits.options) * limits.options));
    functions.textContent = "";
    matches.slice(offset, offset + limits.options).forEach(function (key) {
      var option = node("option", name(key)); option.value = key;
      option.selected = key === selected; functions.appendChild(option);
    });
    if (selected === null || !matches.slice(offset,offset + limits.options).includes(selected)) {
      selected = functions.value === "" ? null : +functions.value;
    }
    byId("caller-page").textContent = (matches.length ? offset + 1 : 0) + "–" +
      Math.min(offset + limits.options, matches.length) + " of " + matches.length + " matching functions";
    byId("caller-prev").disabled = offset === 0;
    byId("caller-next").disabled = offset + limits.options >= matches.length;
  }
  function render() {
    var root = newNode(), enclosing = 0, end = data.functions.length, deep = end + 1;
    rows.forEach(function (row) {
      enclosing += value(row);
      if (selected === null) return;
      var path = data.paths[row[0]], anchor = path.lastIndexOf(selected);
      if (anchor < 0 || (mode.value === "leaf" && path[path.length-1] !== selected)) return;
      var callers = path.slice(0, anchor).reverse();
      if (callers.length > limits.max_depth) callers = callers.slice(0,limits.max_depth).concat(deep);
      else callers.push(end);
      var current = root;
      current.count += row[4]; current.weight += value(row);
      callers.forEach(function (key) {
        if (!current.children.has(key)) current.children.set(key,newNode());
        current = current.children.get(key); current.count += row[4]; current.weight += value(row);
      });
      current.own += row[4]; current.ownWeight += value(row);
    });
    var unit = data.kind === "cpu" ? "ns" : metric.value;
    var countUnit = data.kind === "cpu" ? "samples" : "allocations";
    byId("caller-summary").textContent = "Selected function: " + (selected === null ? "none" : name(selected)) + ". " +
      root.count + " " + countUnit + " / " + root.weight + " " + unit + ". " + share(root.weight,enclosing) +
      " of the enclosing selection (" + enclosing + " " + unit + "). " +
      (mode.value === "leaf" ? "Leaf-site selection." : "Inclusive selection; leafmost occurrence anchor.");
    var budget = limits.max_nodes, floor = Math.max(1,root.weight*limits.min_share);
    function numbers(n) { return n.weight + " " + unit + " · " + share(n.weight,root.weight) + " · " + n.count + " " + countUnit; }
    function children(n,depth) {
      var list = node("ul"), hidden = [], shown = 0;
      Array.from(n.children.entries()).sort(function(a,b) { return b[1].weight-a[1].weight || a[0]-b[0]; }).forEach(function(entry) {
        var key = entry[0], child = entry[1];
        if (budget <= 0 || depth >= limits.max_depth || shown >= limits.max_children || child.weight < floor) {
          hidden.push(child); return;
        }
        budget--; shown++;
        var text = key === end ? "[stack ends here; caller unavailable or truncated]" :
          key === deep ? "[deeper callers omitted]" : name(key) +
          (key === selected ? " [recursive ancestor; ambiguous caller relationship]" : "");
        var details = node("details"); details.open = depth < 2;
        add(details,node("summary",text + " · " + numbers(child)));
        if (child.children.size) details.appendChild(children(child,depth+1));
        list.appendChild(add(node("li"), details));
      });
      if (hidden.length) {
        var other = {weight:0,count:0};
        hidden.forEach(function(n) { other.weight += n.weight; other.count += n.count; });
        var omitted = node("li",hidden.length + " other/deeper caller paths · " + numbers(other));
        omitted.className = "tree-other"; list.appendChild(omitted);
      }
      return list;
    }
    var tree = byId("caller-tree"); tree.textContent = "";
    if (!root.count) tree.appendChild(node("p","No retained row matches this function and selection. This is not evidence of no work."));
    else {
      var all = node("details"); all.open = true;
      add(all,node("summary","All selected · " + numbers(root)),children(root,0));
      var list = add(node("ul"),add(node("li"),all)); list.className = "tree";
      tree.appendChild(list);
    }
  }
  controls.hidden = false;
  // Prefer the largest inclusive contributor, using leaf weight to break ties.
  var totals = new Map(), own = new Map();
  rows.forEach(function(row) {
    var path = data.paths[row[0]];
    own.set(path[path.length-1], (own.get(path[path.length-1]) || 0) + value(row));
    new Set(path).forEach(function(key) { totals.set(key,(totals.get(key)||0)+value(row)); });
  });
  if (totals.size) selected = Array.from(totals.keys()).sort(function(a,b) {
    return totals.get(b)-totals.get(a) || (own.get(b)||0)-(own.get(a)||0) || a-b;
  })[0];
  if (selected !== null) offset = Math.floor(selected/limits.options)*limits.options;
  options(); render();
  controls.addEventListener("submit",function(event) { event.preventDefault(); });
  search.addEventListener("input",function() { offset=0; options(); render(); });
  functions.addEventListener("change",function() { selected=functions.value === "" ? null : +functions.value; render(); });
  metric.addEventListener("change",render); mode.addEventListener("change",render);
  byId("caller-prev").addEventListener("click",function() { offset-=limits.options; options(); render(); });
  byId("caller-next").addEventListener("click",function() { offset+=limits.options; options(); render(); });
  document.addEventListener("sil-profile-selection",function(event) { rows=event.detail.rows; render(); });
})();
