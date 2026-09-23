/* landing.js — the front door's two filters.
 *
 * Everything it filters is already on the page: the systems list and the command
 * list are rendered by the server from the bot's own registry, and this file
 * only hides rows and rewrites the count. That is deliberate. A script that
 * *built* the list would be a second renderer, and the failure this project has
 * shipped twice is exactly two renderers disagreeing about how many commands
 * exist.
 *
 * Two rules the counters follow:
 *
 *   * a count is about what is *shown*, and it says so — "4 of 14", never "4";
 *   * a filter never leaves the page describing something it just hid. When a
 *     chip hides the system whose panel is open, the panel closes with it,
 *     because the button that could close it is gone.
 */
(function () {
  "use strict";

  function plural(count, one, many) {
    return count + " " + (count === 1 ? one : many);
  }

  function toList(nodes) {
    return Array.prototype.slice.call(nodes);
  }

  /* -------------------------------------------------------------------------
     The systems explorer
     ---------------------------------------------------------------------- */
  function explorer() {
    var root = document.querySelector("[data-explorer]");
    if (!root) return;

    var search = root.querySelector("[data-explorer-search]");
    var chips = toList(root.querySelectorAll("[data-explorer-kind]"));
    var rows = toList(root.querySelectorAll("[data-system]"));
    var status = root.querySelector("[data-explorer-status]");
    var empty = root.querySelector("[data-explorer-empty]");
    var kind = "";
    var term = "";

    function closePanel(row) {
      var panel = row.querySelector(".drawer");
      var trigger = row.querySelector("[data-drawer]");
      if (panel) panel.setAttribute("data-open", "false");
      if (trigger) trigger.setAttribute("aria-expanded", "false");
    }

    function apply() {
      var shown = 0;
      rows.forEach(function (row) {
        var matchesKind = !kind || row.getAttribute("data-kind") === kind;
        var haystack = (row.getAttribute("data-search") || "").toLowerCase();
        var matchesTerm = !term || haystack.indexOf(term) !== -1;
        var visible = matchesKind && matchesTerm;
        row.hidden = !visible;
        if (visible) shown += 1;
        /* A panel whose row was filtered away can no longer be closed: the
           trigger went with it. Close the panel as the row goes. */
        if (!visible) closePanel(row);
      });

      if (status) {
        if (!kind && !term) {
          status.textContent = "Showing all " + plural(rows.length, "system", "systems") + ".";
        } else {
          var why = kind ? "in " + kind.toLowerCase() : "matching “" + term + "”";
          status.textContent =
            "Showing " + shown + " of " + plural(rows.length, "system", "systems") + " " + why + ".";
        }
      }
      if (empty) empty.hidden = shown !== 0;
    }

    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        kind = chip.getAttribute("data-explorer-kind") || "";
        chips.forEach(function (other) {
          other.setAttribute("aria-pressed", other === chip ? "true" : "false");
        });
        apply();
      });
    });

    if (search) {
      search.addEventListener("input", function () {
        term = search.value.trim().toLowerCase();
        apply();
      });
    }

    apply();
  }

  /* -------------------------------------------------------------------------
     The command list
     ---------------------------------------------------------------------- */
  function commands() {
    var list = document.querySelector("[data-command-list]");
    if (!list) return;

    var field = document.querySelector("[data-command-search]");
    var status = document.querySelector("[data-command-status]");
    var rows = toList(list.querySelectorAll("[data-command]"));

    function apply() {
      var term = field ? field.value.trim().toLowerCase() : "";
      var shown = 0;
      rows.forEach(function (row) {
        var haystack = (row.getAttribute("data-search") || "").toLowerCase();
        var visible = !term || haystack.indexOf(term) !== -1;
        row.hidden = !visible;
        if (visible) shown += 1;
      });
      if (status) {
        status.textContent = term
          ? "Showing " + shown + " of " + plural(rows.length, "entry", "entries") +
            " matching “" + term + "”."
          : "Showing all " + plural(rows.length, "entry", "entries") + ".";
      }
    }

    if (!field) return;
    field.addEventListener("input", apply);
    apply();
  }

  function boot() {
    explorer();
    commands();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
