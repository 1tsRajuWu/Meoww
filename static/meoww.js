/* ==========================================================================
   meoww.js — the whole scripted surface of the new front door.

   Two jobs, and nothing else:

     1. **Run the command demo.** A visitor types a slash command; this works
        out which system owns it from the two registry tables the page ships as
        JSON (every real command, and which system gates each) and whether that
        system's switch is on *in the demo*, then answers with the bot's own
        refusal when it is off. Nothing here leaves the page: there is no
        request in this file, and the switches write to local state only — the
        page is not signed in to a server and does not pretend to be.
     2. **Open the narrow-screen nav.** One button, one attribute.

   Three rules this file obeys deliberately:

     * **No `getElementById`.** Every hook is a `data-mw-*` attribute read with
       `querySelector`, so the shell's own law — *every id a page's script looks
       up exists in that page's markup* — cannot be broken by a script that asks
       for no ids at all.
     * **No `IntersectionObserver`, no document listeners, no animation.** A
       reveal that fires as the reader moves is how a second entrance gets added
       to a page that is allowed exactly one, so `tests/test_meoww_site.py` fails
       if this file ever grows one. The page's one entrance is in the stylesheet,
       and a browser with no script here loses the demo and nothing else.
     * **Nothing a visitor types is ever written as markup.** The echo of the
       command and the refusal are built from `textContent` and `createElement`,
       so a command box on the front page is not a way of putting anything *into*
       the page.
   ========================================================================== */
(function () {
  "use strict";

  function all(root, selector) {
    return Array.prototype.slice.call(root.querySelectorAll(selector));
  }

  /* ------------------------------------------------------- the command demo */

  var tables = document.querySelector("[data-mw-sim]");
  var desk = document.querySelector("[data-mw-live]");

  if (tables && desk) {
    desk.hidden = false;

    var data = JSON.parse(tables.textContent);
    var box = desk.querySelector("[data-mw-sim-input]");
    var send = desk.querySelector("[data-mw-sim-send]");
    var answer = desk.querySelector("[data-mw-sim-answer]");
    var count = document.querySelector("[data-mw-live-count]");
    var toggles = all(desk, "[data-mw-switch]");

    /* Whether a system is on in the demo, read off the switch itself rather than
       kept in a second variable: the markup is where the state is stated (that
       is what makes it true with no script), so it is the only place to read it
       back from. */
    function isOn(feature) {
      for (var i = 0; i < toggles.length; i++) {
        if (toggles[i].getAttribute("data-mw-system") !== feature) continue;
        return toggles[i].getAttribute("aria-checked") === "true";
      }
      /* No switch is drawn for this system, so this page cannot show it off —
         which is the honest answer for the always-on half of the registry, and
         is only reachable for the other half if a gated system stops being drawn
         here at all. */
      return true;
    }

    function tally() {
      if (!count) return;
      var lit = 0;
      for (var i = 0; i < toggles.length; i++) {
        if (toggles[i].getAttribute("aria-checked") === "true") lit++;
      }
      count.textContent = lit + " of " + toggles.length + " on";
    }

    function element(tag, text) {
      var node = document.createElement(tag);
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function said(nodes, kind) {
      answer.textContent = "";
      /* The verdict rides on the region, as the three colours the switch beside
         it already uses. Cleared rather than left behind: a refusal followed by a
         name that is not a command must not keep the refusal's colour, which is
         the state every "sticky" attribute like this ends up in. */
      if (kind) answer.setAttribute("data-kind", kind);
      else answer.removeAttribute("data-kind");
      var line = element("p");
      line.className = "mw-sim__said";
      for (var i = 0; i < nodes.length; i++) line.appendChild(nodes[i]);
      answer.appendChild(line);
    }

    function aLink(href, label) {
      var node = element("a", label);
      node.setAttribute("href", href);
      return node;
    }

    function replyTo(raw) {
      var word = String(raw || "").trim().replace(/^\/+/, "").split(/\s+/)[0].toLowerCase();
      if (!word) {
        return said([
          element("span", "Type a command — "),
          element("code", "rank"),
          element("span", ", "),
          element("code", "market buy"),
          element("span", " or "),
          element("code", "balance"),
          element("span", "."),
        ]);
      }

      var typed = element("code", "/" + word);

      if (data.always.indexOf(word) !== -1) {
        return said(
          [typed, element("span", " runs whatever the switches say — it is not behind one.")],
          "on"
        );
      }

      if (!Object.prototype.hasOwnProperty.call(data.gates, word)) {
        return said([
          typed,
          element("span", " is not a command this bot has. The " + data.count +
            " it does have are listed on "),
          aLink("/systems", "Every system"),
          element("span", "."),
        ]);
      }

      var owners = data.gates[word];
      for (var i = 0; i < owners.length; i++) {
        if (isOn(owners[i])) continue;
        var parts = data.off.split("{name}");
        return said(
          [
            element("span", parts[0]),
            element("strong", data.names[owners[i]] || owners[i]),
            element("span", parts[1]),
            element("span", " " + data.hint),
          ],
          "off"
        );
      }

      return said(
        [
          typed,
          element("span", " would run — "),
          element("strong", data.names[owners[0]] || owners[0]),
          element("span", " is on in this server."),
        ],
        "on"
      );
    }

    function ask() {
      if (box) replyTo(box.value);
    }

    if (send) send.addEventListener("click", ask);

    if (box) {
      /* Enter submits, because a command box that needs a mouse is not a command
         box. There is no `<form>` anywhere in this band on purpose: a form with
         no action submits to the current page, and a demo must not be one failed
         listener away from navigating the reader out of it. */
      box.addEventListener("keydown", function (event) {
        if (event.key !== "Enter") return;
        event.preventDefault();
        ask();
      });
    }

    desk.addEventListener("click", function (event) {
      var button = event.target.closest("[data-mw-switch]");
      if (!button || !desk.contains(button)) return;
      button.setAttribute(
        "aria-checked",
        button.getAttribute("aria-checked") === "true" ? "false" : "true"
      );
      tally();
      /* Flipping a switch re-reads the command that is in the box, which is the
         whole point of putting the two panes side by side. An empty box keeps
         the invitation to type one rather than being answered with a refusal for
         a command nobody gave. */
      if (box && box.value.trim()) ask();
    });

    /* Space and Enter are what a `<button>` already answers to, so there is no
       key handler on the switches: a second one is a second implementation of
       the same rule, and the one the browser has is the correct one. */
    tally();
  }

  /* ------------------------------------------------------------------- nav */

  var toggle = document.querySelector("[data-mw-nav-toggle]");
  var nav = document.querySelector("[data-mw-nav]");
  if (toggle && nav) {
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("mw-nav--open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });

    /* A menu that stays open behind the reader is a menu they have to close
       twice, so following one of its links closes it. */
    nav.addEventListener("click", function (event) {
      if (!event.target.closest("a")) return;
      nav.classList.remove("mw-nav--open");
      toggle.setAttribute("aria-expanded", "false");
    });
  }
})();
