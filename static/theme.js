/* theme.js — the theme switch, and the only script that runs before paint.
 *
 * It is loaded in the head with no `defer` because the alternative is a flash:
 * a visitor who chose the light theme would see the dark one for as long as it
 * takes a deferred script to run, which on a slow connection is a visible
 * flicker on every navigation.
 *
 * Three states rather than two — light, dark, and "whatever the system says" —
 * because the third one is what most people actually want and a two-way switch
 * silently takes it away from them. The choice is one small value in
 * localStorage; nothing here is sent anywhere.
 */
(function () {
  "use strict";

  var KEY = "meow-theme";
  var CHOICES = ["system", "light", "dark", "contrast"];
  var root = document.documentElement;

  /* The house palette is the light one, and it is what a first-time visitor
     gets — not their operating system's preference. A site whose look depends on
     which machine opened it is a site with no look; Dark and System are one click
     away in the theme menu, and System still follows the setting once chosen. */
  var DEFAULT = "light";

  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return CHOICES.indexOf(value) === -1 ? DEFAULT : value;
    } catch (error) {
      return DEFAULT;
    }
  }

  /* The sheet's own default is the light palette, so "light" is the absence of
     an attribute rather than another name for one. A page that states nothing
     is a page that renders before this runs. */
  function applies(choice) {
    if (choice === "system") {
      var light = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      return light ? "dark" : "light";
    }
    return choice;
  }

  function paint(choice) {
    var theme = applies(choice);
    if (theme === "light") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute(
        "content",
        theme === "dark" ? "#0d0f12" : theme === "contrast" ? "#000000" : "#f7f5f1"
      );
    }
    var label = document.querySelector("[data-theme-label]");
    if (label) {
      label.textContent =
        choice === "system" ? "System" :
        choice === "light" ? "Light" :
        choice === "dark" ? "Dark" : "High contrast";
    }
    document.querySelectorAll("[data-theme-set]").forEach(function (button) {
      var mine = button.getAttribute("data-theme-set") === choice;
      button.setAttribute("aria-pressed", mine ? "true" : "false");
    });
  }

  function choose(choice) {
    try {
      window.localStorage.setItem(KEY, choice);
    } catch (error) {
      /* A browser refusing storage is not a reason to refuse the theme. */
    }
    paint(choice);
  }

  paint(stored());
  root.setAttribute("data-theme-ready", "true");

  document.addEventListener("DOMContentLoaded", function () {
    var trigger = document.querySelector("[data-theme-toggle]");
    var menu = document.getElementById("theme-menu");
    if (trigger && menu) {
      trigger.addEventListener("click", function () {
        var open = menu.getAttribute("data-open") === "true";
        menu.setAttribute("data-open", open ? "false" : "true");
        trigger.setAttribute("aria-expanded", open ? "false" : "true");
      });
      document.addEventListener("click", function (event) {
        if (event.target.closest("#theme-menu") || event.target.closest("[data-theme-toggle]")) return;
        menu.setAttribute("data-open", "false");
        trigger.setAttribute("aria-expanded", "false");
      });
      document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        menu.setAttribute("data-open", "false");
        trigger.setAttribute("aria-expanded", "false");
      });
    }

    document.querySelectorAll("[data-theme-set]").forEach(function (button) {
      button.addEventListener("click", function () {
        choose(button.getAttribute("data-theme-set"));
        var panel = button.closest("#theme-menu");
        if (panel) {
          panel.setAttribute("data-open", "false");
          if (trigger) trigger.setAttribute("aria-expanded", "false");
        }
      });
    });

    paint(stored());
  });

  if (window.matchMedia) {
    var query = window.matchMedia("(prefers-color-scheme: dark)");
    var react = function () {
      if (stored() === "system") paint("system");
    };
    if (query.addEventListener) query.addEventListener("change", react);
    else if (query.addListener) query.addListener(react);
  }

  /* A visitor with JavaScript off never runs any of this: the sheet's own
     default is the light palette, and the menu's buttons still render. */
  window.MeowTheme = { choose: choose, current: stored };
})();
