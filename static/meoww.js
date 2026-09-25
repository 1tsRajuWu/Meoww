/* ==========================================================================
   meoww.js — the whole scripted surface of the new front door.

   Three jobs, and nothing else:

     1. **Reveal the live toggle grid.** The grid ships `hidden` and is
        un-hidden here, because a switch that does nothing is worse than no
        switch: a reader who has no script is shown the *drawing* of the
        dashboard in the hero — which states its own state in markup — and is
        never handed a control that cannot answer.
     2. **Flip a demo switch.** Local state only. Nothing here posts anywhere,
        because the page this grid copies is the one behind a sign-in, and its
        switches are forms that a server answers.
     3. **Open the narrow-screen nav.** One button, one attribute.

   Two rules this file obeys deliberately:

     * **No `getElementById`.** Every hook is a `data-mw-*` attribute read with
       `querySelector`, so the shell's own law — *every id a page's script looks
       up exists in that page's markup* — cannot be broken by a script that asks
       for no ids at all.
     * **No `IntersectionObserver`, no scroll listeners, and no animation.** A
       reveal that fires as you scroll is how a second entrance gets added to a
       page that is allowed exactly one, so `tests/test_meoww_site.py` fails if
       this file ever grows one. The page's one entrance is in the stylesheet,
       and a browser with no script here loses nothing but the grid.
   ========================================================================== */
(function () {
  "use strict";

  function switches(root) {
    return Array.prototype.slice.call(root.querySelectorAll("[data-mw-switch]"));
  }

  /* ---------------------------------------------------------------- 1 and 2 */

  var live = document.querySelector("[data-mw-live]");
  if (live) {
    live.hidden = false;

    var rows = switches(live);
    var count = document.querySelector("[data-mw-live-count]");

    function say(button) {
      var on = button.getAttribute("aria-checked") === "true";
      button.setAttribute("aria-checked", on ? "false" : "true");
    }

    function tally() {
      if (!count) return;
      var on = rows.filter(function (button) {
        return button.getAttribute("aria-checked") === "true";
      }).length;
      count.textContent = on + " of " + rows.length + " on";
    }

    live.addEventListener("click", function (event) {
      var button = event.target.closest("[data-mw-switch]");
      if (!button || !live.contains(button)) return;
      say(button);
      tally();
    });

    /* Space and Enter are what a `<button>` already answers to, so there is no
       key handler here on purpose: a second one is a second implementation of
       the same rule, and the one the browser has is the correct one. */
    tally();
  }

  /* --------------------------------------------------------------------- 3 */

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
