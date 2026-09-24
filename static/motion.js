/* motion.js — the site's motion layer, and the last script every page loads.
 *
 * One requestAnimationFrame loop drives everything that has to be smooth: the
 * pointer's own layer, the counters that animate a figure into place, and the
 * elements that fade up as they scroll in. A second loop would be a second
 * clock, and two clocks is how a page ends up with a glow that arrives before
 * the thing it is glowing.
 *
 * The rule that shapes this file: **the position is the script's and the shape
 * is the sheet's, by property.** The layer's position is written as
 * `style.translate` and never as `style.transform`, because CSS animations
 * outrank inline declarations — an animation of `transform` would silently
 * replace a position written as `transform`, and the layer would sit in the
 * corner of the viewport while the pointer moved around it.
 *
 * Everything here is optional. With this file blocked, every page still reads:
 * the sheets state the resting values, the markup says what is closed, and
 * nothing below is required to make a control work.
 */
(function () {
  "use strict";

  var reduced = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false, addEventListener: function () {} };
  var doc = document;

  /* --------------------------------------------------------------------------
   * The menu: one attribute on <html>, and the sheet animates the rest.
   * ----------------------------------------------------------------------- */
  function wireMenu() {
    var trigger = doc.querySelector("[data-menu-toggle]");
    var nav = doc.getElementById("site-nav");
    if (!trigger || !nav) return;

    function set(open) {
      doc.documentElement.setAttribute("data-menu-open", open ? "true" : "false");
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function isOpen() {
      return doc.documentElement.getAttribute("data-menu-open") === "true";
    }

    set(false);
    trigger.addEventListener("click", function () {
      set(!isOpen());
    });
    nav.addEventListener("click", function (event) {
      if (event.target.closest("a")) set(false);
    });
    doc.addEventListener("keydown", function (event) {
      if (event.key === "Escape") set(false);
    });
    /* A tap anywhere else closes it.
       Escape, a link and the button itself were the three ways out, which is
       every way except the one a thumb uses: on a phone the panel covers the
       content a visitor was reading, and the only escape was to find the same
       small button again and press it twice. The theme popover next door has
       always closed on an outside click; this is the same rule, in the same
       shape, so the two menus of the same header behave the same way.         */
    doc.addEventListener("click", function (event) {
      if (!isOpen()) return;
      if (event.target.closest("#site-nav") || event.target.closest("[data-menu-toggle]")) return;
      set(false);
    });
  }

  /* --------------------------------------------------------------------------
   * Drawers — a question that opens in place.
   *
   * One panel per trigger, and the panel is found rather than assumed: a page
   * may put several drawers side by side, and a primitive that adopted the
   * *first* one it saw would leave every trigger but the first unable to close
   * the panel it opened.
   * ----------------------------------------------------------------------- */
  function wireDrawers() {
    doc.querySelectorAll("[data-drawer]").forEach(function (trigger) {
      var id = trigger.getAttribute("aria-controls");
      var panel = id ? doc.getElementById(id) : null;
      if (!panel) return;

      trigger.setAttribute("aria-expanded", panel.getAttribute("data-open") === "true");

      trigger.addEventListener("click", function () {
        var open = panel.getAttribute("data-open") === "true";
        panel.setAttribute("data-open", open ? "false" : "true");
        trigger.setAttribute("aria-expanded", open ? "false" : "true");
      });
    });
  }

  /* --------------------------------------------------------------------------
   * Reveals: fade up once, then stop being watched.
   * ----------------------------------------------------------------------- */
  function wireReveals() {
    var targets = Array.prototype.slice.call(doc.querySelectorAll("[data-reveal]"));
    if (!targets.length) return;
    if (reduced.matches || !("IntersectionObserver" in window)) return;

    targets.forEach(function (node) {
      if (node.hasAttribute("data-reveal-now")) return;
      node.classList.add("is-pending");
    });

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.remove("is-pending");
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.12 }
    );

    targets.forEach(function (node) {
      observer.observe(node);
    });
  }

  /* --------------------------------------------------------------------------
   * Counters: a figure is written as text, and the animation restores what it
   * captured, so a live refresh that lands mid-count is not overwritten by the
   * last frame of a decoration.
   * ----------------------------------------------------------------------- */
  function wireCounters() {
    var targets = Array.prototype.slice.call(doc.querySelectorAll("[data-count]"));
    if (!targets.length || reduced.matches || !("IntersectionObserver" in window)) return;

    function run(node) {
      var final = node.getAttribute("data-count") || node.textContent || "";
      var digits = final.match(/[0-9][0-9.,]*/);
      if (!digits) return;
      var target = parseFloat(digits[0].replace(/,/g, ""));
      if (!isFinite(target)) return;
      /* What the figure is *written as* is captured and restored, so a live
         refresh landing mid-count is not overwritten by the last frame. */
      var prefix = final.slice(0, digits.index);
      var suffix = final.slice(digits.index + digits[0].length);
      var start = null;

      function step(stamp) {
        if (start === null) start = stamp;
        var progress = Math.min(1, (stamp - start) / 900);
        var eased = 1 - Math.pow(1 - progress, 3);
        node.textContent = prefix + Math.round(target * eased).toLocaleString() + suffix;
        if (progress < 1) requestAnimationFrame(step);
        else node.textContent = final;
      }
      requestAnimationFrame(step);
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        run(entry.target);
      });
    }, { threshold: 0.4 });

    targets.forEach(function (node) {
      observer.observe(node);
    });
  }

  /* --------------------------------------------------------------------------
   * The pointer layer.
   * ----------------------------------------------------------------------- */
  function wirePointer() {
    if (reduced.matches) return;
    if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;

    var layer = doc.createElement("div");
    layer.className = "meow-layer";
    layer.setAttribute("aria-hidden", "true");
    layer.innerHTML =
      '<div class="meow-glow"></div>' +
      '<div class="vortex-container"></div>' +
      '<div class="radar-hud"></div>' +
      '<div class="sword-entity"></div>' +
      '<div class="blob-drop"></div>';
    doc.body.appendChild(layer);

    var glow = layer.querySelector(".meow-glow");
    var vortex = layer.querySelector(".vortex-container");
    var radar = layer.querySelector(".radar-hud");
    var sword = layer.querySelector(".sword-entity");
    var blob = layer.querySelector(".blob-drop");

    var COUNT = 7;
    var sparkleParticles = [];
    for (var index = 0; index < COUNT; index += 1) {
      var particle = doc.createElement("span");
      particle.className = "sparkle-particle";
      layer.appendChild(particle);
      sparkleParticles.push({ node: particle, x: 0, y: 0 });
    }

    /* Where the springs are heading, and where the swarm is. The swarm is a
       trailing layer: it eases in at a fixed lag, so a loop that stops when the
       springs settle leaves a fast flick strung out behind the pointer. */
    var targetX = window.innerWidth / 2;
    var targetY = window.innerHeight / 2;
    var x = targetX;
    var y = targetY;
    var running = false;
    var started = false;

    function wake() {
      if (running) return;
      running = true;
      requestAnimationFrame(frame);
    }

    /* The loop. It stops when both the springs and the swarm have arrived, and
       it is woken by the pointer rather than left spinning. */
    function frame() {
      x += (targetX - x) * 0.22;
      y += (targetY - y) * 0.22;

      var settled = Math.abs(targetX - x) < 0.4 && Math.abs(targetY - y) < 0.4;
      if (settled) {
        x = targetX;
        y = targetY;
      }

      layer.style.translate = x + "px " + y + "px";

      var swarmSettled = true;
      for (var si = 0; si < sparkleParticles.length; si += 1) {
        var part = sparkleParticles[si];
        var lag = 0.14 + si * 0.03;
        part.x += (x - part.x) * lag;
        part.y += (y - part.y) * lag;
        if (Math.abs(x - part.x) > 0.6 || Math.abs(y - part.y) > 0.6) swarmSettled = false;
        part.node.style.translate = (part.x - x) + "px " + (part.y - y) + "px";
      }

      if (!settled || !swarmSettled) requestAnimationFrame(frame);
      else running = false;
    }

    doc.addEventListener(
      "pointermove",
      function (event) {
        targetX = event.clientX;
        targetY = event.clientY;
        if (!started) {
          /* The first move releases the layer where the pointer *is*, rather than
             from the corner the springs were built in. */
          started = true;
          x = targetX;
          y = targetY;
          layer.classList.add("is-on");
          for (var si = 0; si < sparkleParticles.length; si += 1) {
            sparkleParticles[si].x = targetX;
            sparkleParticles[si].y = targetY;
          }
        }
        layer.classList.toggle("is-down", event.buttons !== 0);
        var interactive = event.target.closest("a, button, input, select, textarea, [role='button']");
        var typing = event.target.closest("input, textarea, [contenteditable='true']");
        layer.classList.toggle("is-link", Boolean(interactive) && !typing);
        layer.classList.toggle("is-text", Boolean(typing));
        if (glow && vortex && radar && sword && blob) {
          /* Named here rather than inferred: the sheet is the only thing that
             decides what each entity looks like, and a script that stopped
             naming one would leave a law watching an element nobody carries. */
          glow.style.opacity = "";
        }
        wake();
      },
      { passive: true }
    );

    doc.addEventListener("pointerdown", function () {
      layer.classList.add("is-down");
    });
    doc.addEventListener("pointerup", function () {
      layer.classList.remove("is-down");
    });
  }

  /* --------------------------------------------------------------------------
   * Copy-to-clipboard: the only control here the page cannot provide itself.
   * ----------------------------------------------------------------------- */
  function wireCopy() {
    doc.querySelectorAll("[data-copy]").forEach(function (button) {
      /* The resting label is read once, when the page loads, and never from the
         button again. It used to be captured per click, which made a second
         click read *"Copied"* as the label to restore — so the button said
         "Copied" for the rest of the page's life, about nothing, from a single
         impatient double press. The pending timer is cleared for the same
         reason: two clicks, one label, one timer.                           */
      var resting = button.textContent;
      var pending = null;

      button.addEventListener("click", function () {
        var text = button.getAttribute("data-copy") || "";
        var label = button.getAttribute("data-copy-label") || "Copied";
        function done() {
          button.textContent = label;
          if (pending) window.clearTimeout(pending);
          pending = window.setTimeout(function () {
            button.textContent = resting;
            pending = null;
          }, 1600);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, done);
        } else {
          done();
        }
      });
    });
  }

  /* --------------------------------------------------------------------------
   * The command reference (/systems): one collapsed group per system, and a
   * search that reaches across all of them.
   *
   * The groups themselves are native `<details>` — they need no script to open,
   * so everything this file adds is enhancement: un-hide the tools (they ship
   * `hidden`, because a search box whose file did not load is a control that
   * does nothing), filter rows inside every group, hide the groups that emptied,
   * open the ones that matched while a term is typed, and land the deep link
   * the front door's “+N more →” sends (`#cmd-<id>`).
   * ----------------------------------------------------------------------- */
  function wireCommandExplorer() {
    var root = doc.querySelector("[data-command-explorer]");
    if (!root) return;

    var tools = root.querySelector("[data-command-tools]");
    var field = root.querySelector("[data-command-search]");
    var status = root.querySelector("[data-command-status]");
    var empty = root.querySelector("[data-command-empty]");
    var expand = root.querySelector("[data-command-expand]");
    var groups = Array.prototype.slice.call(root.querySelectorAll("[data-command-group]"));
    var total = root.querySelectorAll("[data-command]").length;

    if (tools) tools.hidden = false;

    function apply() {
      var term = field ? field.value.trim().toLowerCase() : "";
      var found = 0;

      groups.forEach(function (group) {
        var shownInGroup = 0;
        Array.prototype.slice.call(group.querySelectorAll("[data-command]")).forEach(function (row) {
          var haystack = (row.getAttribute("data-search") || "").toLowerCase();
          var hit = !term || haystack.indexOf(term) !== -1;
          row.hidden = !hit;
          if (hit) { shownInGroup += 1; found += 1; }
        });
        group.hidden = shownInGroup === 0;
        /* A search says *find this*: the groups holding matches open so the
           answer is on screen, and they stay open when the box is cleared —
           closing what the reader just found is a filter that argues. */
        if (term && shownInGroup > 0) group.open = true;
      });

      if (status) {
        status.hidden = !term;
        status.textContent = term
          ? found + " of " + total + " match “" + term + "”."
          : "";
      }
      if (empty) empty.hidden = !(term && found === 0);
      if (expand) {
        expand.hidden = Boolean(term);
        if (!term) {
          var allOpen = groups.every(function (group) { return group.hidden || group.open; });
          expand.textContent = allOpen ? "Collapse all" : "Expand all";
          expand.setAttribute("aria-expanded", allOpen ? "true" : "false");
        }
      }
    }

    if (field) field.addEventListener("input", apply);

    if (expand) {
      expand.addEventListener("click", function () {
        var allOpen = groups.every(function (group) { return group.hidden || group.open; });
        groups.forEach(function (group) { if (!group.hidden) group.open = !allOpen; });
        apply();
      });
    }

    /* The front door links here as `/systems#cmd-economy`. A closed <details>
       still scrolls to its summary, but the reader would arrive at a group that
       looks empty — so the hash names the group and it opens before they land. */
    if (window.location.hash) {
      try {
        var target = doc.querySelector(window.location.hash);
        if (target && target.tagName === "DETAILS") target.open = true;
      } catch (invalid) { /* a hash that is not a selector is not ours to open */ }
    }

    apply();
  }

  /* --------------------------------------------------------------------------
   * The station table (/radio): three hundred rows, two dozen of them on screen,
   * and a question in front of the rest.
   *
   * The page it replaces was one forty-eight-row list — 7,476px, two thirds of
   * the page — and a visitor looking for their own language had to read it. The
   * shape here is the command reference's, one page over: the rows are all in
   * the document, the search and the two menus narrow them, and the count says
   * what is being looked at rather than leaving the reader to guess.
   *
   * Three rules, each of them the same rule as /systems:
   *
   *   * a search is an instruction to look at *everything* — asking lifts the
   *     cap, and clearing the boxes puts it back exactly as it was;
   *   * nothing the reader asked for is hidden — the *Show all* control leaves
   *     the screen while a question is on, because it answers a question nobody
   *     is asking any more;
   *   * and a count is about what is *shown*, and says so — "12 of 308", never
   *     "12".
   * ----------------------------------------------------------------------- */
  function wireStationExplorer() {
    var root = doc.querySelector("[data-station-explorer]");
    if (!root) return;

    var tools = root.querySelector("[data-station-tools]");
    var field = root.querySelector("[data-station-search]");
    var language = root.querySelector("[data-station-language]");
    var country = root.querySelector("[data-station-country]");
    var status = root.querySelector("[data-station-status]");
    var note = root.querySelector("[data-station-cap]");
    var empty = root.querySelector("[data-station-empty]");
    var more = root.querySelector("[data-station-more]");
    var list = root.querySelector("[data-station-list]");
    var rows = Array.prototype.slice.call(root.querySelectorAll("[data-station]"));
    if (!rows.length || (!field && !language && !country)) return;

    var total = rows.length;
    /* The cap is counted, never written: the template marks the rows beyond it,
       and the control's label is what those rows are worth, so the sheet, the
       sentence under the list and the button cannot disagree about the number. */
    var beyond = rows.filter(function (row) {
      return row.hasAttribute("data-beyond");
    }).length;
    var preview = total - beyond;
    var opened = false;

    if (tools) tools.hidden = false;
    if (more) more.hidden = false;

    function question() {
      return Boolean(
        (field && field.value.trim()) ||
        (language && language.value) ||
        (country && country.value)
      );
    }

    function apply() {
      var term = field ? field.value.trim().toLowerCase() : "";
      var wanted = language ? language.value : "";
      var placed = country ? country.value : "";
      var asked = question();
      var found = 0;

      rows.forEach(function (row) {
        var hit =
          (!term || (row.getAttribute("data-search") || "").indexOf(term) !== -1) &&
          (!wanted || row.getAttribute("data-language") === wanted) &&
          (!placed || row.getAttribute("data-country") === placed);
        row.hidden = !hit;
        if (hit) found += 1;
      });

      /* The cap, in one place: the sheet hides `[data-beyond]` while the list is
         `data-capped`, and this writes both — never the rows themselves. */
      if (list) list.setAttribute("data-capped", (asked || opened) ? "false" : "true");
      if (note) note.hidden = asked || opened;

      if (status) {
        status.hidden = !asked;
        if (asked) {
          /* The ask, in the reader's words: a term is *matching*, and the two
             menus are *where* — joined into one “in …” so a search that uses
             both reads "in Finnish · Finland" rather than "in Finnish, in
             Finland", which is a sentence about a language and then repeats
             itself about a country. */
          var why = [];
          if (term) why.push("matching “" + term + "”");
          var where = [wanted, placed].filter(Boolean).join(" · ");
          if (where) why.push("in " + where);
          status.textContent = found + " of " + total + " " + why.join(", ") + ".";
        }
      }

      if (empty) empty.hidden = !(asked && found === 0);

      if (more) {
        more.hidden = asked;
        more.textContent = opened ? "Show the first " + preview : "Show all " + total;
        more.setAttribute("aria-expanded", opened ? "true" : "false");
      }
    }

    [field, language, country].forEach(function (control) {
      if (!control) return;
      control.addEventListener("input", apply);
      control.addEventListener("change", apply);
    });

    if (more) {
      more.addEventListener("click", function () {
        opened = !opened;
        apply();
      });
    }

    apply();
  }

  function boot() {
    wireMenu();
    wireDrawers();
    wireReveals();
    wireCounters();
    wireCopy();
    wireCommandExplorer();
    wireStationExplorer();
    wirePointer();
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot);
  else boot();

  window.Meow = { wake: function () {} };
})();
