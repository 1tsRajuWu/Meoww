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
  var CHOICES = ["system", "light", "contrast"];
  var root = document.documentElement;

  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return CHOICES.indexOf(value) === -1 ? "system" : value;
    } catch (error) {
      return "system";
    }
  }

  function applies(choice) {
    if (choice === "system") {
      var light = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
      return light ? "light" : "dark";
    }
    return choice;
  }

  function paint(choice) {
    var theme = applies(choice);
    if (theme === "dark") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute(
        "content",
        theme === "dark" ? "#0b0f14" : theme === "contrast" ? "#000000" : "#f6f7f9"
      );
    }
    var label = document.querySelector("[data-theme-label]");
    if (label) {
      label.textContent =
        choice === "system" ? "System" : choice === "light" ? "Light" : "High contrast";
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
    var query = window.matchMedia("(prefers-color-scheme: light)");
    var react = function () {
      if (stored() === "system") paint("system");
    };
    if (query.addEventListener) query.addEventListener("change", react);
    else if (query.addListener) query.addListener(react);
  }

  window.MeowTheme = { choose: choose, current: stored };
})();
