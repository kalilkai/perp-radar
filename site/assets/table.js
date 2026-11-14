(function () {
  "use strict";

  var table = document.getElementById("market");
  if (!table) return;

  var tbody = table.querySelector("tbody");
  var status = document.getElementById("status");
  var empty = document.getElementById("empty");
  var search = document.getElementById("q");
  var filter = "all";
  var sortKey = "amt";
  var sortDir = -1;

  function allRows() {
    return Array.prototype.slice.call(tbody.querySelectorAll("tr"));
  }

  function labelOf(name) {
    var button = document.querySelector('.filters button[data-filter="' + name + '"]');
    if (!button) return name;
    return button.getAttribute("data-label") || name;
  }

  function apply() {
    var query = (search.value || "").trim().toUpperCase();
    var shown = 0;
    allRows().forEach(function (tr) {
      var tags = (tr.getAttribute("data-tags") || "").split(/\s+/);
      var ok = filter === "all" || tags.indexOf(filter) !== -1;
      if (query) {
        var blob = (
          (tr.getAttribute("data-base") || "") +
          " " +
          (tr.getAttribute("data-pair") || "")
        ).toUpperCase();
        if (blob.indexOf(query) === -1) ok = false;
      }
      tr.hidden = !ok;
      if (ok) shown += 1;
    });
    if (status) {
      var name = filter === "all" ? "全部" : "「" + labelOf(filter) + "」";
      status.textContent = "目前顯示" + name + "：" + shown + " 個。";
    }
    if (empty) empty.hidden = shown !== 0;
  }

  document.querySelectorAll(".filters button").forEach(function (button) {
    button.addEventListener("click", function () {
      filter = button.getAttribute("data-filter") || "all";
      document.querySelectorAll(".filters button").forEach(function (other) {
        other.setAttribute("aria-pressed", other === button ? "true" : "false");
      });
      apply();
    });
  });

  table.querySelectorAll("thead button").forEach(function (button) {
    button.addEventListener("click", function () {
      var key = button.getAttribute("data-sort");
      if (!key) return;
      if (sortKey === key) {
        sortDir = -sortDir;
      } else {
        sortKey = key;
        sortDir = key === "base" ? 1 : -1;
      }
      var list = allRows();
      list.sort(function (a, b) {
        if (key === "base") {
          var cmp = (a.getAttribute("data-base") || "").localeCompare(
            b.getAttribute("data-base") || ""
          );
          return cmp * sortDir;
        }
        var rawA = a.getAttribute("data-" + key);
        var rawB = b.getAttribute("data-" + key);
        var va = rawA === "" || rawA === null ? null : parseFloat(rawA);
        var vb = rawB === "" || rawB === null ? null : parseFloat(rawB);
        if (va !== null && isNaN(va)) va = null;
        if (vb !== null && isNaN(vb)) vb = null;
        if (va === null && vb === null) return 0;
        if (va === null) return 1;
        if (vb === null) return -1;
        if (va === vb) {
          return (a.getAttribute("data-base") || "").localeCompare(
            b.getAttribute("data-base") || ""
          );
        }
        return (va - vb) * sortDir;
      });
      list.forEach(function (tr) {
        tbody.appendChild(tr);
      });
      table.querySelectorAll("thead th").forEach(function (th) {
        th.removeAttribute("aria-sort");
      });
      button.parentElement.setAttribute(
        "aria-sort",
        sortDir < 0 ? "descending" : "ascending"
      );
      apply();
    });
  });

  if (search) {
    search.addEventListener("input", apply);
  }
  apply();
})();
