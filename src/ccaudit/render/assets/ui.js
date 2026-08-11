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

  var table = document.querySelector("table[data-sortable]");
  var filter = document.getElementById("ui-filter");
  var filterCount = document.getElementById("ui-filter-count");

  // Filtering hides item rows only. Summary rows carry data-pinned — the conversation's own
  // cost, what the model wrote back, and the unattributed remainder — and stay visible whatever
  // is typed, because a part-to-whole view that can be filtered into looking tidy is a lie
  // (FR-040). The count below says how many rows are hidden, in words, not by colour alone.
  function applyFilter() {
    if (!table || !filter) return;
    var body = table.tBodies[0];
    if (!body) return;
    var needle = filter.value.trim().toLowerCase();
    var rows = Array.prototype.slice.call(body.rows);
    var items = 0;
    var shown = 0;
    rows.forEach(function (row) {
      if (row.dataset.pinned === "1") return;
      items += 1;
      var name = String(row.dataset.name || "").toLowerCase();
      var visible = needle === "" || name.indexOf(needle) !== -1;
      row.hidden = !visible;
      if (visible) shown += 1;
    });
    if (filterCount) {
      filterCount.textContent = needle === ""
        ? "showing all " + items + " item rows"
        : "showing " + shown + " of " + items + " item rows; the totals below still cover all "
          + items;
    }
  }

  if (filter) {
    filter.addEventListener("input", applyFilter);
    applyFilter();
  }

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