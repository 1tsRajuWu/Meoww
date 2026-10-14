/* site.js — the marketing site's one script, and the last thing every page of
   it loads.

   One file, one rAF loop, no dependencies (the CSP is `script-src 'self'`), and
   it only ever *adds* to a page: every element it touches is fully readable
   without it. The contract with the stylesheet is that the resting state is the
   final state — this file is what bookmarks something as "not yet revealed"
   (`site.css` styles `.is-pending`, which `meow.css` already defines), and it
   only writes that bookmark after checking `prefers-reduced-motion`.

   What it does, in the order the page needs it:

     1. header state + reading progress   (every page)
     2. the narrow-screen menu            (every page)
     3. scroll reveals + count-ups        (every page)
     4. the live figures resolver         (a built page asking its host)
     5. pointer tilt / parallax / magnet  (fine pointers, motion allowed)
     6. the activity card's window switch (/systems)
 */
(function () {
  'use strict';

  var root = document.documentElement;
  var motionQuery = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : null;
  var finePointers = window.matchMedia
    ? window.matchMedia('(pointer: fine)').matches
    : false;

  function motionAllowed() {
    return !(motionQuery && motionQuery.matches);
  }

  function toNumber(value, fallback) {
    var n = parseFloat(value);
    return isFinite(n) ? n : fallback;
  }

  /* ------------------------------------------------------------------------
     1 & 5. The shared loop
     ------------------------------------------------------------------------
     Scroll, pointer and count work all land in one rAF tick rather than one
     listener per effect — the same shape the market page and the old motion
     layer used, because a page that schedules three loops for one frame is a
     page that janks on the device least able to afford it.
     ---------------------------------------------------------------------- */

  var tilters = [];
  var parallaxers = [];
  var magnetics = [];
  var pointer = { x: -1, y: -1 };
  var ticking = false;

  function schedule() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(tick);
  }

  function tick() {
    ticking = false;
    var vh = window.innerHeight || 800;

    /* Header state + reading progress. */
    var top = document.querySelector('[data-site-top]');
    var y = window.pageYOffset || root.scrollTop || 0;
    if (top) {
      var scrolled = y > 8;
      if ((top.getAttribute('data-scrolled') === 'true') !== scrolled) {
        top.setAttribute('data-scrolled', scrolled ? 'true' : 'false');
      }
    }
    var doc = document.documentElement;
    var max = (doc.scrollHeight - vh) || 1;
    root.style.setProperty('--site-p', Math.min(1, Math.max(0, y / max)).toFixed(3));

    /* The pointer's glow, read by `.site-bg__glow` through the two tokens
       `meow.css` already declares for exactly this. */
    if (pointer.x >= 0) {
      root.style.setProperty('--meow-x', pointer.x + 'px');
      root.style.setProperty('--meow-y', pointer.y + 'px');
    }

    /* Parallax: a small translate away from the resting place, weakest at the
       edges of the viewport so nothing ever pops in a frame late. At rest the
       inline transform is *removed* rather than zeroed — an inline value
       outranks every rule for the life of the page, which is how a reveal
       quietly takes `.btn:active` and every hover state with it. */
    for (var i = 0; i < parallaxers.length; i++) {
      var item = parallaxers[i];
      var rect = item.el.getBoundingClientRect();
      if (rect.bottom < -80 || rect.top > vh + 80) continue;
      var centre = rect.top + rect.height / 2 - vh / 2;
      var offset = -centre * item.y;
      if (Math.abs(offset) < 0.5) {
        item.el.style.removeProperty("transform");
      } else {
        item.el.style.transform = "translate3d(0," + offset.toFixed(1) + "px,0)";
      }
    }

    /* Tilt and magnet, lerped toward the pointer so the card trails it rather
       than snapping to it. The magnet releases its element the same way the
       parallax does. */
    for (i = 0; i < tilters.length; i++) {
      var t = tilters[i];
      t.x += (t.tx - t.x) * 0.14;
      t.y += (t.ty - t.y) * 0.14;
      t.el.style.setProperty("--site-tx", (-t.y).toFixed(2) + "deg");
      t.el.style.setProperty("--site-ty", t.x.toFixed(2) + "deg");
      if (Math.abs(t.tx - t.x) > 0.02 || Math.abs(t.ty - t.y) > 0.02) {
        ticking = true;
      }
    }

    for (i = 0; i < magnetics.length; i++) {
      var m = magnetics[i];
      m.x += (m.tx - m.x) * 0.2;
      m.y += (m.ty - m.y) * 0.2;
      if (m.tx === 0 && m.ty === 0 && Math.abs(m.x) < 0.05 && Math.abs(m.y) < 0.05) {
        m.x = 0;
        m.y = 0;
        m.el.style.removeProperty("transform");
        continue;
      }
      m.el.style.transform = "translate3d(" + m.x.toFixed(1) + "px," + m.y.toFixed(1) + "px,0)";
      ticking = true;
    }

    if (ticking) window.requestAnimationFrame(tick);
  }

  /* ------------------------------------------------------------------------
     2. The narrow-screen menu
     ---------------------------------------------------------------------- */

  function menu() {
    var top = document.querySelector('[data-site-top]');
    var toggle = document.querySelector('[data-menu-toggle]');
    if (!top || !toggle) return;

    function set(open) {
      top.setAttribute('data-menu-open', open ? 'true' : 'false');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    toggle.addEventListener('click', function () {
      set(top.getAttribute('data-menu-open') !== 'true');
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') set(false);
    });

    top.addEventListener('click', function (event) {
      var link = event.target.closest ? event.target.closest('.site-nav a') : null;
      if (link) set(false);
    });
  }

  /* ------------------------------------------------------------------------
     3. Reveals and count-ups
     ------------------------------------------------------------------------
     `[data-reveal]` is visible in the stylesheet; this marks it pending only
     once it knows it can un-mark it (an observer exists) and that motion was
     not asked against. The stagger is written as the `--site-rd` token rather
     than a class per index, so the stylesheet keeps one transition rule.
     ---------------------------------------------------------------------- */

  function reveals() {
    var nodes = Array.prototype.slice.call(document.querySelectorAll('[data-reveal]'));
    if (!nodes.length) return;

    var counters = Array.prototype.slice.call(document.querySelectorAll('[data-count]'));

    if (!motionAllowed() || !('IntersectionObserver' in window)) {
      counters.forEach(animateCount);
      return;
    }

    var seen = {};
    nodes.forEach(function (node) {
      var key = node.parentNode;
      var index = seen[key] ? seen[key].count : 0;
      seen[key] = { count: index + 1 };
      node.style.setProperty('--site-rd', Math.min(index, 6) * 70 + 'ms');
      node.classList.add('is-pending');
    });

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.remove('is-pending');
        observer.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });

    nodes.forEach(function (node) { observer.observe(node); });

    if (counters.length) {
      var countObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          animateCount(entry.target);
          countObserver.unobserve(entry.target);
        });
      }, { threshold: 0.4 });
      counters.forEach(function (node) { countObserver.observe(node); });
    }
  }

  /* A figure counts up from zero once, and only when its value is a whole
     number. Anything else — an em dash, a spelled duration — is left exactly
     as the server wrote it: a formatter reimplemented here would be a second
     author for a number the deployment already spells. */
  function animateCount(el) {
    if (!motionAllowed()) return;
    var raw = el.getAttribute('data-count');
    if (!raw) return;
    var match = String(raw).match(/^(\d{1,3}(?:,\d{3})+|\d+)$/);
    if (!match) return;

    var grouped = match[1].indexOf(',') !== -1;
    var target = parseInt(match[1].replace(/,/g, ''), 10);
    if (!isFinite(target) || target <= 0) return;

    var started = null;
    var duration = 900;

    function frame(ts) {
      if (started === null) started = ts;
      var progress = Math.min(1, (ts - started) / duration);
      var eased = 1 - Math.pow(1 - progress, 3);
      var value = Math.round(target * eased);
      el.textContent = grouped ? value.toLocaleString('en-US') : String(value);
      if (progress < 1) {
        window.requestAnimationFrame(frame);
      } else {
        el.textContent = grouped ? target.toLocaleString('en-US') : String(target);
      }
    }

    window.requestAnimationFrame(frame);
  }

  /* ------------------------------------------------------------------------
     4. The built copy asking its host for the readings it does not have
     ------------------------------------------------------------------------
     A page built without a process carries an em dash where a count belongs
     and a `data-figures-url` on `<main>`; this fills every `[data-figure]`
     from one request. A failure is silent on purpose — the dashes stay, and
     nothing about a static page should look broken because a host is asleep.
     ---------------------------------------------------------------------- */

  function figures() {
    var main = document.querySelector('main[data-figures-url]');
    if (!main || !window.fetch) return;

    window.fetch(main.getAttribute('data-figures-url'), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    }).then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (payload) {
      if (!payload) return;
      var text = payload.text || {};
      var periods = {};
      (payload.periods || []).forEach(function (period) { periods[period.id] = period; });

      document.querySelectorAll('[data-figure]').forEach(function (el) {
        var parts = el.getAttribute('data-figure').split(':');
        var value = '';
        if (parts[0] === 'activity' && parts.length === 3 && periods[parts[1]]) {
          value = periods[parts[1]][parts[2]] || '';
        } else {
          value = text[parts[0]] || '';
        }
        if (!value) return;
        el.textContent = value;
        /* The fresh reading outranks the count-up: leaving the attribute on
           would let the animation overwrite it on the next intersection, which
           is the two-writers defect the figures module was built to end. */
        el.removeAttribute('data-count');
      });
    }).catch(function () {
      /* No reading is not an error here. */
    });
  }

  /* ------------------------------------------------------------------------
     5. Pointer work — registered, not started, until the loop runs
     ---------------------------------------------------------------------- */

  function pointerWork() {
    if (!finePointers || !motionAllowed()) return;

    Array.prototype.forEach.call(document.querySelectorAll('[data-tilt]'), function (el) {
      var item = { el: el, x: 0, y: 0, tx: 0, ty: 0 };
      tilters.push(item);

      el.addEventListener('pointermove', function (event) {
        var rect = el.getBoundingClientRect();
        item.tx = ((event.clientX - rect.left) / rect.width - 0.5) * 7;
        item.ty = ((event.clientY - rect.top) / rect.height - 0.5) * 7;
        schedule();
      });

      el.addEventListener('pointerleave', function () {
        item.tx = 0;
        item.ty = 0;
        schedule();
      });
    });

    Array.prototype.forEach.call(document.querySelectorAll('[data-parallax]'), function (el) {
      var raw = String(el.getAttribute('data-parallax') || '').split(',');
      var y = toNumber(raw.length > 1 ? raw[1] : raw[0], 8) / 100;
      parallaxers.push({ el: el, y: y });
    });

    Array.prototype.forEach.call(document.querySelectorAll('[data-magnetic]'), function (el) {
      var item = { el: el, x: 0, y: 0, tx: 0, ty: 0 };
      magnetics.push(item);

      el.addEventListener('pointermove', function (event) {
        var rect = el.getBoundingClientRect();
        item.tx = ((event.clientX - (rect.left + rect.width / 2)) / rect.width) * 8;
        item.ty = ((event.clientY - (rect.top + rect.height / 2)) / rect.height) * 8;
        schedule();
      });

      el.addEventListener('pointerleave', function () {
        item.tx = 0;
        item.ty = 0;
        schedule();
      });
    });

    document.addEventListener('pointermove', function (event) {
      pointer.x = event.clientX;
      pointer.y = event.clientY;
      schedule();
    }, { passive: true });

    if (parallaxers.length) schedule();
  }

  /* ------------------------------------------------------------------------
     6. The activity card's window switch
     ------------------------------------------------------------------------
     Every window is already in the markup — the server rendered all of them —
     so switching is a redraw rather than a request, and with no script the
     first window is simply the one the page shows.
     ---------------------------------------------------------------------- */

  function activityWindows() {
    var buttons = document.querySelectorAll('[data-window]');
    if (!buttons.length) return;

    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener('click', function () {
        var wanted = button.getAttribute('data-window');
        Array.prototype.forEach.call(buttons, function (other) {
          other.setAttribute('aria-pressed', other === button ? 'true' : 'false');
        });
        Array.prototype.forEach.call(document.querySelectorAll('[data-window-panel]'), function (panel) {
          panel.hidden = panel.getAttribute('data-window-panel') !== wanted;
        });
      });
    });
  }

  /* ------------------------------------------------------------------------
     Boot
     ---------------------------------------------------------------------- */

  function boot() {
    menu();
    reveals();
    figures();
    pointerWork();
    activityWindows();

    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule, { passive: true });
    schedule();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
