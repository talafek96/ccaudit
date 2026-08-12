// The interactive shell's only script. It is deliberately incapable of producing a figure.
//
// Everything on the page was rendered from the payload by the same Python renderer the shareable
// report uses. This script reorders nothing that report.js does not already reorder, and computes
// no money, no share, and no total: it hides rows, folds drill-downs, and hides sections. That is
// what keeps FR-074 true — the browser cannot show something the terminal cannot, because the
// browser derives nothing.
//
// It fetches nothing. Selection changes are ordinary form navigation to the local server, which
// re-renders the page server-side, so there is exactly one renderer and no client-side state to
// drift out of agreement with it.

(function () {
  "use strict";

  document.documentElement.classList.add("js");
  // Only the exploring shell has a filter for a tag click to drive; the shareable report is
  // a document, and a control that does nothing there would be worse than no control.
  document.documentElement.classList.add("ui-shell");


  // Drill-downs are <details> elements rendered server-side; these two buttons only open and
  // close them all at once.
  function foldAll(open) {
    Array.prototype.forEach.call(document.querySelectorAll("details.drill"), function (details) {
      details.open = open;
    });
  }

  var expand = document.getElementById("ui-expand");
  var collapse = document.getElementById("ui-collapse");
  if (expand) expand.addEventListener("click", function () { foldAll(true); });
  if (collapse) collapse.addEventListener("click", function () { foldAll(false); });

  // View switching: each <h2> in the report starts a section, and each section gets a checkbox.
  // Only the sections are hidden — the header above the first <h2> carries the cost-basis
  // sentence and the total, and is never hideable.
  var views = document.getElementById("ui-views");
  var main = document.querySelector("main");
  if (views && main) {
    var sections = [];
    var current = null;
    Array.prototype.forEach.call(main.children, function (node) {
      if (node.tagName === "H2") {
        current = { title: node.textContent, nodes: [node] };
        sections.push(current);
      } else if (current) {
        current.nodes.push(node);
      }
    });

    sections.forEach(function (section, index) {
      var label = document.createElement("label");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.checked = true;
      box.id = "ui-view-" + index;
      box.addEventListener("change", function () {
        section.nodes.forEach(function (node) { node.hidden = !box.checked; });
      });
      label.setAttribute("for", box.id);
      label.appendChild(box);
      label.appendChild(document.createTextNode(" " + section.title));
      views.appendChild(label);
    });
    if (sections.length === 0) views.hidden = true;
  }

  // Changing the grouping is a whole new question for the server to answer, so it submits.
  // The session checkboxes do not: picking several is one decision, applied once.
  var grouping = document.querySelector(".ui-form select[name='by']");
  if (grouping) {
    grouping.addEventListener("change", function () {
      var form = grouping.form;
      if (form) form.submit();
    });
  }

  // Session selection. Ticking a box changes which sessions are aggregated, and the figures
  // for a new selection are computed *by the server*, in Python, by the same code the terminal
  // uses. This handler therefore does exactly one thing: submit the form. It does not add up
  // per-session figures in the browser, which would be a second implementation of the
  // arithmetic and could disagree with the first.
  var form = document.querySelector("form.ui-form");
  var boxes = Array.prototype.slice.call(
    document.querySelectorAll('.ui-sessions input[name="session"]')
  );
  var selectedCount = document.getElementById("ui-selected");

  function describeSelection() {
    if (!selectedCount) return;
    var chosen = boxes.filter(function (box) { return box.checked; }).length;
    selectedCount.textContent = chosen === boxes.length
      ? "all " + boxes.length + " sessions selected"
      : chosen + " of " + boxes.length + " sessions selected";
  }

  function setAll(checked) {
    boxes.forEach(function (box) { box.checked = checked; });
    describeSelection();
  }

  if (form && boxes.length) {
    describeSelection();
    boxes.forEach(function (box) {
      box.addEventListener("change", function () {
        describeSelection();
        // Unticking the last box would ask the server for an empty selection, which is not a
        // question it can answer. The box springs back and says so rather than navigating to
        // an error page.
        if (!boxes.some(function (other) { return other.checked; })) {
          box.checked = true;
          describeSelection();
          if (selectedCount) {
            selectedCount.textContent = "at least one session has to stay selected";
          }
          return;
        }
        form.submit();
      });
    });
    var all = document.getElementById("ui-all");
    var none = document.getElementById("ui-none");
    if (all) all.addEventListener("click", function () { setAll(true); form.submit(); });
    if (none) {
      // "Select none" leaves the first one ticked for the same reason as above: the smallest
      // answerable selection is one session, not zero.
      none.addEventListener("click", function () {
        setAll(false);
        if (boxes.length) boxes[0].checked = true;
        describeSelection();
        form.submit();
      });
    }
  }

})();
/* The session picker's ranked columns.
 *
 * Cheap facts are already in the table. The rest need each session analysed, which is too slow
 * to hold the page open for, so they are fetched one session at a time and written in as they
 * land. A cell stays empty until its answer arrives: blank means "not known yet", and a zero
 * would mean "none" — only one of those is true while the work is still running.
 */
(function () {
  var table = document.getElementById("ui-session-table");
  if (!table || !table.tBodies[0]) return;
  var body = table.tBodies[0];
  var rows = Array.prototype.slice.call(body.rows);
  var status = document.getElementById("ui-facts-status");
  var pending = rows.length;

  function money(micros) {
    var dollars = micros / 1000000;
    if (dollars >= 100) return "$" + Math.round(dollars);
    if (dollars >= 1) return "$" + dollars.toFixed(1);
    return "$" + dollars.toFixed(2);
  }

  function announce() {
    if (!status) return;
    status.textContent = pending > 0 ? "measuring " + pending + " more…" : "";
  }

  function fill(row, facts) {
    Array.prototype.forEach.call(row.querySelectorAll("td[data-metric]"), function (cell) {
      var key = cell.getAttribute("data-metric");
      if (!(key in facts)) return;
      var value = facts[key];
      cell.setAttribute("data-value", String(value));
      cell.textContent = key === "cost_micros" ? money(value) : value.toLocaleString();
    });
  }

  // Sequential on purpose. These are CPU-bound analyses on the machine the reader is using;
  // firing twenty-six at once makes every one of them slower and the first answer later.
  function next(index) {
    if (index >= rows.length) { announce(); return; }
    var row = rows[index];
    fetch("/facts?session=" + encodeURIComponent(row.getAttribute("data-session")))
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (facts) { if (facts) fill(row, facts); })
      .catch(function () { /* a session that will not analyse keeps its blank cells */ })
      .then(function () { pending -= 1; announce(); next(index + 1); });
  }

  // Sorting is over the rows already on screen: unmeasured rows sort last whichever way the
  // column is pointing, because "not known" is not a small number.
  var descending = {};
  Array.prototype.forEach.call(table.querySelectorAll("th[data-metric]"), function (header) {
    header.classList.add("ui-sortable");
    header.addEventListener("click", function () {
      var key = header.getAttribute("data-metric");
      var down = !descending[key];
      descending[key] = down;
      var sorted = Array.prototype.slice.call(body.rows).sort(function (a, b) {
        if (key === "name") {
          var left = a.getAttribute("data-name") || "";
          var right = b.getAttribute("data-name") || "";
          return down ? right.localeCompare(left) : left.localeCompare(right);
        }
        var av = a.querySelector('td[data-metric="' + key + '"]');
        var bv = b.querySelector('td[data-metric="' + key + '"]');
        var an = av && av.hasAttribute("data-value") ? Number(av.getAttribute("data-value")) : null;
        var bn = bv && bv.hasAttribute("data-value") ? Number(bv.getAttribute("data-value")) : null;
        if (an === null && bn === null) return 0;
        if (an === null) return 1;
        if (bn === null) return -1;
        return down ? bn - an : an - bn;
      });
      sorted.forEach(function (row) { body.appendChild(row); });
      Array.prototype.forEach.call(table.querySelectorAll("th[data-metric]"), function (other) {
        other.removeAttribute("aria-sort");
      });
      header.setAttribute("aria-sort", down ? "descending" : "ascending");
    });
  });

  announce();
  next(0);
})();
