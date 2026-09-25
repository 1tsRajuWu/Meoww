/* landing.js — the front door's own behaviour: the preview panel's tablist.
 *
 * Nothing in this file builds, hides or counts a list. The systems list it used
 * to filter is on `/systems`, rendered by the server from the bot's own registry
 * and filtered by `motion.js` — one renderer, so there is nothing here that can
 * disagree with it about how many systems exist, which is the failure this
 * project has shipped twice.
 */
(function () {
  "use strict";

  function toList(nodes) {
    return Array.prototype.slice.call(nodes);
  }

  /* -------------------------------------------------------------------------
     The showcase: a tablist over the dashboard's pages, and the preview
     controls inside each panel.

     It is a real tablist — roles, `aria-selected`, a roving tabindex and the
     arrow keys — because the rail is a control that changes what the panel
     shows, and a control a keyboard cannot operate is not one. Nothing here
     writes anywhere: the switches flip, the panel says it is sample data, and
     that is the whole interaction. A preview that reached a server would be a
     preview nobody should be allowed to click.
     ---------------------------------------------------------------------- */
  function showcase() {
    var root = document.querySelector("[data-showcase]");
    if (!root) return;

    var tabs = toList(root.querySelectorAll('[role="tab"]'));
    var panels = toList(root.querySelectorAll('[role="tabpanel"]'));
    if (!tabs.length) return;

    function select(index, focus) {
      tabs.forEach(function (tab, position) {
        var active = position === index;
        tab.setAttribute("aria-selected", active ? "true" : "false");
        tab.tabIndex = active ? 0 : -1;
        if (active && focus) tab.focus();
      });
      panels.forEach(function (panel, position) {
        panel.hidden = position !== index;
      });
    }

    tabs.forEach(function (tab, index) {
      tab.addEventListener("click", function () { select(index, false); });
      tab.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowDown" || event.key === "ArrowRight") next = (index + 1) % tabs.length;
        else if (event.key === "ArrowUp" || event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = tabs.length - 1;
        if (next === null) return;
        event.preventDefault();
        select(next, true);
      });
    });

    panels.forEach(function (panel) {
      toList(panel.querySelectorAll(".mock-switch")).forEach(function (control) {
        function flip() {
          var on = control.getAttribute("aria-checked") === "true";
          control.setAttribute("aria-checked", on ? "false" : "true");
        }
        control.addEventListener("click", flip);
        control.addEventListener("keydown", function (event) {
          if (event.key !== " " && event.key !== "Enter") return;
          event.preventDefault();
          flip();
        });
      });
    });

    select(0, false);
  }

  function boot() {
    showcase();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
