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

  var table = document.querySelector("table[data-sortable]");
  var filter = document.getElementById("ui-filter");
  var filterCount = document.getElementById("ui-filter-count");

  var tagsPanel = document.getElementById("ui-tags-panel");
  var tagsBox = document.getElementById("ui-tags");
  var tagsCount = document.getElementById("ui-tags-count");
  var selectedTags = new Set();

  function rowTags(row) {
    var raw = String(row.dataset.tags || "").toLowerCase().trim();
    return raw === "" ? [] : raw.split(" ");
  }

  function itemRows() {
    if (!table || !table.tBodies[0]) return [];
    return Array.prototype.filter.call(table.tBodies[0].rows, function (row) {
      return row.dataset.pinned !== "1";
    });
  }

  // A row the filter is allowed to show: everything except the ones still standing behind the
  // "N other items" line, which the truncation figure is accounting for.
  function eligibleRows() {
    return itemRows().filter(function (row) {
      return row.dataset.overflow !== "1" || row.dataset.revealed === "1";
    });
  }

  // Every tag on the page, with how many rows carry it. Counted over the rows the filter can
  // actually reveal — a panel offering "docs (8)" that shows nothing when ticked, because six
  // of those eight are still behind the truncation line, is a control that lies about itself.
  // Rebuilt after "show more", when the answer changes.
  function buildTagPanel() {
    if (!tagsPanel || !tagsBox) return;
    var counts = new Map();
    eligibleRows().forEach(function (row) {
      rowTags(row).forEach(function (tag) {
        counts.set(tag, (counts.get(tag) || 0) + 1);
      });
    });
    tagsBox.textContent = "";
    if (!counts.size) {
      tagsPanel.hidden = true;
      return;
    }
    tagsPanel.hidden = false;
    Array.from(counts.keys()).sort().forEach(function (tag) {
      var label = document.createElement("label");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.value = tag;
      box.checked = selectedTags.has(tag);
      box.addEventListener("change", function () { toggleTag(tag); });
      var text = document.createElement("span");
      text.textContent = tag + " (" + counts.get(tag) + ")";
      label.appendChild(box);
      label.appendChild(text);
      tagsBox.appendChild(label);
    });
  }

  function toggleTag(tag) {
    if (!tag) return;
    if (selectedTags.has(tag)) selectedTags.delete(tag);
    else selectedTags.add(tag);
    applyFilter();
  }

  // One state, two controls: a tag ticked in the panel and a tag clicked on a row are the same
  // fact, so both are redrawn from the same set rather than each tracking its own.
  function syncTagControls() {
    if (tagsBox) {
      Array.prototype.forEach.call(tagsBox.querySelectorAll("input"), function (box) {
        box.checked = selectedTags.has(box.value);
      });
    }
    // Not aria-pressed: these are spans, not buttons, and a state attribute the CSS can see
    // is what is actually needed — a selected tag has to *look* selected wherever it appears.
    Array.prototype.forEach.call(document.querySelectorAll(".flag[data-tag]"), function (flag) {
      if (selectedTags.has(flag.getAttribute("data-tag"))) flag.setAttribute("data-selected", "1");
      else flag.removeAttribute("data-selected");
    });
    if (tagsCount) {
      tagsCount.textContent = selectedTags.size === 0
        ? "no tag filter"
        : selectedTags.size + " selected";
    }
  }

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
      // Rows past the truncation point are hidden on purpose, and the "N other items" line is
      // still accounting for them. Unhiding one here — which an empty filter did, on load, to
      // all of them — leaves the table showing the rows *and* the line that stands in for
      // them, so the visible column sums to more than the session total. That is a breakdown
      // that does not add up, which is the one defect this project treats as a show-stopper.
      if (row.dataset.overflow === "1" && row.dataset.revealed !== "1") return;
      items += 1;
      var name = String(row.dataset.name || "").toLowerCase();
      var tags = rowTags(row);
      // Two independent filters, both of which must pass. The text narrows by name; the tag
      // set narrows by kind. An empty tag set means "no tag filter" rather than "no rows" —
      // ticking nothing is how a reader says they have not chosen, never that they want an
      // empty table.
      var matchesText = needle === "" || name.indexOf(needle) !== -1;
      var matchesTags = selectedTags.size === 0 || tags.some(function (tag) {
        return selectedTags.has(tag);
      });
      var visible = matchesText && matchesTags;
      row.hidden = !visible;
      if (visible) shown += 1;
    });
    if (filterCount) {
      filterCount.textContent = shown === items
        ? "showing all " + items + " item rows"
        : "showing " + shown + " of " + items + " item rows; the totals below still cover all "
          + items;
    }
    syncTagControls();
  }

  buildTagPanel();

  var tagsAll = document.getElementById("ui-tags-all");
  var tagsNone = document.getElementById("ui-tags-none");
  if (tagsAll) {
    tagsAll.addEventListener("click", function () {
      eligibleRows().forEach(function (row) {
        rowTags(row).forEach(function (t) { selectedTags.add(t); });
      });
      applyFilter();
    });
  }
  if (tagsNone) {
    tagsNone.addEventListener("click", function () { selectedTags.clear(); applyFilter(); });
  }

  if (filter) {
    filter.addEventListener("input", applyFilter);
  }
  applyFilter();

  // Clicking a tag toggles it. The tag is already the thing the reader is pointing at when they
  // wonder "how much of my bill is this?", so making them find a control elsewhere is a step
  // that exists for no reason. It is the same toggle the Tags panel drives, not a second
  // mechanism — one filter state, reachable from either end.
  document.addEventListener("click", function (event) {
    var tag = event.target.closest ? event.target.closest(".flag[data-tag]") : null;
    if (!tag) return;
    // Filtering removes rows from the flow, so the clicked tag would otherwise slide up the
    // page under the reader's cursor — and scrolling to the *filter box* instead, as this did,
    // threw them to the top of the document every time. Neither is acceptable: the thing you
    // clicked stays where you clicked it, and the page moves under it.
    var anchor = tag.getBoundingClientRect().top;
    toggleTag(tag.getAttribute("data-tag"));
    var drift = tag.getBoundingClientRect().top - anchor;
    if (drift) window.scrollBy(0, drift);
  });

  // Rows revealed by "show more" arrive unhidden, which used to undo an active filter: a
  // reader who had narrowed to one tag got every kind of row back. The reveal is in report.js
  // and knows nothing about filtering, so it announces itself and the filter is re-applied.
  document.addEventListener("ccaudit:rows-revealed", function () {
    buildTagPanel();
    applyFilter();
  });

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