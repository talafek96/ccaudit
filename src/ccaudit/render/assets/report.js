// The report's only script, inlined. It adds two things and nothing else: sorting the
// leaderboard, and a theme override. Both are enhancements — the page is complete, readable,
// and fully reconciled with scripting disabled, because the reader may open it in something
// odd, or with scripts blocked by policy (FR-032, FR-038, FR-041).
//
// It fetches nothing, sends nothing, and never re-computes a figure. Every number on the page
// was rendered from the payload before the file was written; JavaScript only reorders rows.

(function () {
  "use strict";

  document.documentElement.classList.add("js");

  // Sorting: rows carry their measures as data attributes so the comparison is on the stored
  // integer, never on the rendered string — "$1,200" at two significant figures must not sort
  // below "$999.50".
  function sortable(table) {
    var body = table.tBodies[0];
    if (!body) return;
    var buttons = table.querySelectorAll(".sort-btn");

    function apply(key, descending) {
      var rows = Array.prototype.slice.call(body.rows);
      // Summary rows (the conversation's own cost, what the model wrote back, and the
      // unattributed remainder) are pinned to the bottom: they are not items and must not be
      // shuffled into a ranking of items.
      var items = rows.filter(function (row) { return row.dataset.pinned !== "1"; });
      var pinned = rows.filter(function (row) { return row.dataset.pinned === "1"; });
      items.sort(function (a, b) {
        var left = a.dataset[key];
        var right = b.dataset[key];
        var numeric = parseFloat(left);
        var other = parseFloat(right);
        var result;
        if (isNaN(numeric) || isNaN(other)) {
          result = String(left).localeCompare(String(right));
        } else {
          result = numeric - other;
        }
        if (result === 0) {
          // Ties break on the item's own name so the order is stable and reproducible.
          result = String(a.dataset.name).localeCompare(String(b.dataset.name));
        }
        return descending ? -result : result;
      });
      items.concat(pinned).forEach(function (row) { body.appendChild(row); });
      Array.prototype.forEach.call(buttons, function (button) {
        var active = button.dataset.sortKey === key;
        button.setAttribute("aria-pressed", active ? "true" : "false");
        button.textContent = active ? (descending ? "▼" : "▲") : "↕";
      });
    }

    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var key = button.dataset.sortKey;
        var descending = button.getAttribute("aria-pressed") !== "true"
          ? button.dataset.sortDefault !== "asc"
          : button.textContent !== "▲";
        apply(key, descending);
      });
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll("table[data-sortable]"), sortable);

  // Progressive reveal. The remainder line must shrink by exactly the cost of the rows just
  // revealed, or the table stops adding up mid-click. So this computes nothing: Python rendered
  // one label and one figure per expansion state, and the script only swaps between them.
  Array.prototype.forEach.call(document.querySelectorAll(".expand-btn"), function (button) {
    var row = button.closest("tr");
    var body = row.parentNode;
    var hidden = Array.prototype.filter.call(body.rows, function (candidate) {
      return candidate.dataset.overflow === "1";
    });
    var states = JSON.parse(button.dataset.expandStates);
    var step = parseInt(button.dataset.expandStep, 10);
    var shown = 0;

    button.addEventListener("click", function () {
      hidden.slice(shown, shown + step).forEach(function (candidate) {
        candidate.hidden = false;
        // Marked, not merely unhidden. Anything else that hides and shows rows — the UI's row
        // filter — has to tell a row the reader revealed from one that is still accounted for
        // by the truncation line, or it will show both and the table will double-count.
        candidate.dataset.revealed = "1";
      });
      shown = Math.min(shown + step, hidden.length);
      if (shown >= hidden.length) {
        // Everything is on the page; the line that stood in for the rest has nothing left to
        // account for, so it goes rather than sitting at $0.00.
        row.remove();
        return;
      }
      var state = states[shown / step];
      row.querySelector(".expand-label").textContent = state.label;
      row.querySelector("td.num").innerHTML = state.figure;
      row.dataset.total = state.micros;
      button.textContent = "Show " + Math.min(step, state.count) + " more";
    });
  });

  // Theme: the page already answers to the operating system's preference through
  // prefers-color-scheme. This only lets a reader override it for this document, which is what
  // a projector or a printed page tends to need.
  var toggle = document.querySelector(".theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var dark = root.getAttribute("data-theme") === "dark";
      if (!root.getAttribute("data-theme")) {
        dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      }
      root.setAttribute("data-theme", dark ? "light" : "dark");
      toggle.textContent = dark ? "Dark theme" : "Light theme";
    });
  }

  // Tooltip. The markup already carries a <title> on every mark, which is what makes the page
  // work with scripting disabled — but a native SVG tooltip waits about a second and then
  // appears wherever the platform decides, which for a dense chart is useless. This replaces it
  // with one that appears immediately, above the cursor, and follows it.
  //
  // It renders text that Python already produced. It computes nothing: there is no figure here
  // that is not already in the <title> it reads.
  var tip = document.createElement("div");
  tip.className = "tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);

  // Move each <title> onto its parent as data and remove the node. Two reasons: the browser
  // would otherwise draw its own tooltip on top of this one, and reading a data attribute is
  // cheaper than walking into the SVG on every mouseover. The <title> is only the fallback for
  // a reader with scripting off — and that reader never runs this line.
  Array.prototype.forEach.call(document.querySelectorAll("title"), function (title) {
    var holder = title.parentNode;
    if (!holder || holder.tagName === "head") return;
    holder.setAttribute("data-tip", title.textContent);
    holder.setAttribute("tabindex", "0");
    title.remove();
  });

  var GAP = 14;

  function place(event) {
    // Measured after the text is set, so a long path does not run off the right edge or get
    // clipped at the top of the viewport.
    var box = tip.getBoundingClientRect();
    var left = event.clientX - box.width / 2;
    var top = event.clientY - box.height - GAP;
    left = Math.max(6, Math.min(left, window.innerWidth - box.width - 6));
    if (top < 6) top = event.clientY + GAP;
    tip.style.left = Math.round(left) + "px";
    tip.style.top = Math.round(top) + "px";
  }

  function textFor(target) {
    if (!target || !target.closest) return "";
    var holder = target.closest("[data-tip]");
    return holder ? holder.getAttribute("data-tip") : "";
  }

  document.addEventListener("mouseover", function (event) {
    var text = textFor(event.target);
    if (!text) return;
    tip.textContent = text;
    tip.hidden = false;
    place(event);
  });

  document.addEventListener("mousemove", function (event) {
    if (!tip.hidden) place(event);
  });

  document.addEventListener("mouseout", function (event) {
    if (!textFor(event.target)) return;
    tip.hidden = true;
  });

  // A keyboard reader gets the same text: the elements that carry one are focusable, and the
  // tooltip follows focus rather than only the pointer.
  document.addEventListener("focusin", function (event) {
    var text = textFor(event.target);
    if (!text) return;
    var box = event.target.getBoundingClientRect();
    tip.textContent = text;
    tip.hidden = false;
    place({ clientX: box.left + box.width / 2, clientY: box.top });
  });

  document.addEventListener("focusout", function () { tip.hidden = true; });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") tip.hidden = true;
  });


  // Flame-graph zoom. Clicking a node makes it the full width and rescales everything under
  // it, which is how a flame graph is read: the shape you are looking at is always "100% of
  // the thing I clicked".
  //
  // This moves rectangles. It does not touch a figure — every cost, share and label was
  // rendered by Python and stays exactly as it was, which is what keeps a zoomed view and the
  // table underneath it incapable of disagreeing.
  Array.prototype.forEach.call(document.querySelectorAll("figure.chart svg"), function (svg) {
    var nodes = Array.prototype.slice.call(svg.querySelectorAll(".flame-node"));
    if (!nodes.length) return;
    var crumbs = svg.parentNode.querySelector("[data-flame-crumbs]");
    var width = svg.viewBox.baseVal.width;
    var rowHeight = 0;
    nodes.forEach(function (n) {
      var d = parseInt(n.dataset.depth, 10);
      var y = parseFloat(n.querySelector("rect").getAttribute("y"));
      if (d === 1 && !rowHeight) rowHeight = y;
    });
    // Where each node started, so a zoom is always computed from the original layout rather
    // than from the last one — repeated zooms cannot drift.
    nodes.forEach(function (n) {
      n._x0 = parseFloat(n.dataset.x0);
      n._x1 = parseFloat(n.dataset.x1);
      n._depth = parseInt(n.dataset.depth, 10);
      n._label = n.querySelector("text").textContent;
    });

    var CHARACTER = 7;
    var trail = [{name: "All", x0: 0, x1: 1, depth: 0}];

    function apply() {
      var focus = trail[trail.length - 1];
      var span = focus.x1 - focus.x0;
      nodes.forEach(function (n) {
        var inside = n._x0 >= focus.x0 - 1e-9 && n._x1 <= focus.x1 + 1e-9 && n._depth >= focus.depth;
        n.style.display = inside ? "" : "none";
        if (!inside) return;
        var x = (n._x0 - focus.x0) / span * width;
        var w = (n._x1 - n._x0) / span * width;
        var rect = n.querySelector("rect");
        var text = n.querySelector("text");
        rect.setAttribute("x", x.toFixed(1));
        rect.setAttribute("width", Math.max(0, w).toFixed(1));
        rect.setAttribute("y", ((n._depth - focus.depth) * rowHeight).toFixed(1));
        text.setAttribute("x", (x + 4).toFixed(1));
        text.setAttribute("y", ((n._depth - focus.depth) * rowHeight + rowHeight / 2).toFixed(1));
        // Re-fit the label to the node's new width. Text layout, not arithmetic on a figure.
        var fits = Math.max(0, Math.floor(w / CHARACTER));
        text.setAttribute("visibility", fits >= 3 ? "visible" : "hidden");
        text.textContent = n._label.length <= fits ? n._label : n._label.slice(0, Math.max(1, fits - 1)) + "\u2026";
      });
      if (!crumbs) return;
      crumbs.textContent = "";
      trail.forEach(function (step, index) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "flame-crumb";
        button.textContent = step.name;
        button.addEventListener("click", function () {
          trail = trail.slice(0, index + 1);
          apply();
        });
        crumbs.appendChild(button);
      });
    }

    function focusOn(node) {
      if (node._x1 - node._x0 <= 0) return;
      trail.push({name: node.dataset.name || "/", x0: node._x0, x1: node._x1, depth: node._depth});
      apply();
    }

    nodes.forEach(function (n) {
      n.style.cursor = "zoom-in";
      n.addEventListener("click", function () { focusOn(n); });
      n.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); focusOn(n); }
      });
    });
    apply();
  });

})();
