/* ===========================================================================
   Meow — the shared motion foundation for the four surfaces
   ===========================================================================
   `static/motion.js` owns the *declarative* primitives the pages opt into with
   `data-` attributes (reveals, clip-masked words, parallax, drawers, hover
   springs). This file owns the *coordinated* ones the interactive surfaces need
   and that a per-page script would otherwise each reinvent:

     MeowAnim.ready(fn)                    run once the document is parsed
     MeowAnim.spring(from,to,cfg,on,done)  one spring, on the page's single loop
     MeowAnim.tween(ms,easing,on,done)     one tween, on the same loop
     MeowAnim.reveal(el, opts)             fade-and-rise as it enters the viewport
     MeowAnim.stagger(host, step)          reveal a container's children in order
     MeowAnim.countUp(el, opts)            animate a rendered number, then restore it
     MeowAnim.panel(trigger, panel, opts)  open/close one panel, with motion
     MeowAnim.menu(root, opts)             a drop-down that opens, closes and drops
     MeowAnim.drop(trigger, panel, opts)   a menu/dialog positioned under its trigger
     MeowAnim.spotlight(root)              pointer-follow variables for a surface
     MeowAnim.tabs(root, opts)             coordinated tab/panel switching
     MeowAnim.observeInview(el, fn, opts)  call fn once, when el is on screen

   Four rules, and every one of them is load-bearing:

   1. **One rAF loop for the whole page.** Every spring and tween in every
      surface registers with the same ticker, so twenty animations cost one
      frame callback rather than twenty. A surface with two loops and a surface
      with one are indistinguishable until something animates in both.

   2. **Nothing here is required to read the page.** Elements start in their
      final state in the markup and this file moves them *out* before animating
      them back, so a blocked script, an old browser or a `noscript` shows the
      whole page.

   3. **`prefers-reduced-motion` wins, once, at the top.** Read once and
      honoured by every primitive: reveals are instant, springs jump to their
      target, the count-up restores the rendered figure, panels toggle without a
      transition, and the pointer spotlight is off entirely.

   4. **Only transform, opacity and `grid-template-rows` are animated.** No
      width/height/top/left is written frame by frame, so no animation here can
      cause a layout pass on every frame — which is the difference between a
      surface that feels smooth and one that feels like it is thinking.

   It is `'self'`-only: no library, no CDN, no inline code. Every surface that
   wants motion loads this file and nothing else for it.
   =========================================================================== */
(function () {
    "use strict";

    var reduceQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    var reduced = !!(reduceQuery && reduceQuery.matches);
    /* A visitor who changes their mind mid-session gets the new answer without a
       reload: every primitive reads `reduced` at the moment it starts. */
    if (reduceQuery && reduceQuery.addEventListener) {
        reduceQuery.addEventListener("change", function (event) {
            reduced = !!event.matches;
        });
    }

    var pointerFine = !!(
        window.matchMedia &&
        (window.matchMedia("(pointer: fine)").matches || window.matchMedia("(any-pointer: fine)").matches)
    );

    /* ---------------------------------------------------------------------
       One loop
       --------------------------------------------------------------------- */
    var tickers = new Set();
    var last = 0;

    function tick(now) {
        // Clamped: a backgrounded tab returns an enormous gap, and an unclamped
        // step turns a spring into an explosion instead of a finish.
        var dt = Math.min(0.064, (now - (last || now)) / 1000);
        last = now;
        tickers.forEach(function (fn) {
            if (fn(dt) === false) tickers.delete(fn);
        });
        if (tickers.size) {
            requestAnimationFrame(tick);
        } else {
            last = 0;
        }
    }

    function onLoop(fn) {
        tickers.add(fn);
        if (!last) requestAnimationFrame(tick);
        return function stop() {
            tickers.delete(fn);
        };
    }

    var EASINGS = {
        linear: function (t) { return t; },
        easeOutExpo: function (t) { return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); },
        easeOutQuart: function (t) { return 1 - Math.pow(1 - t, 4); },
        easeOutCubic: function (t) { return 1 - Math.pow(1 - t, 3); },
        easeInOutCubic: function (t) {
            return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        }
    };

    function spring(from, to, cfg, onValue, onDone) {
        if (reduced) {
            onValue(to);
            if (onDone) onDone();
            return function () {};
        }
        var x = from;
        var v = 0;
        var tension = (cfg && cfg.tension) || 190;
        var friction = (cfg && cfg.friction) || 26;
        return onLoop(function (dt) {
            v += (-tension * (x - to) - friction * v) * dt;
            x += v * dt;
            if (Math.abs(x - to) < 0.01 && Math.abs(v) < 0.01) {
                onValue(to);
                if (onDone) onDone();
                return false;
            }
            onValue(x);
            return true;
        });
    }

    function tween(duration, easing, onValue, done) {
        var fn = typeof easing === "function" ? easing : (EASINGS[easing] || EASINGS.easeOutExpo);
        if (reduced || duration <= 0) {
            onValue(1);
            if (done) done();
            return function () {};
        }
        var elapsed = 0;
        return onLoop(function (dt) {
            elapsed += dt * 1000;
            var t = Math.min(1, elapsed / duration);
            onValue(fn(t));
            if (t === 1) {
                if (done) done();
                return false;
            }
            return true;
        });
    }

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    /* ---------------------------------------------------------------------
       In-view: once, and only once
       --------------------------------------------------------------------- */
    function observeInview(el, fn, opts) {
        var options = opts || {};
        if (typeof IntersectionObserver !== "function") {
            fn();
            return function () {};
        }
        var seen = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (!entry.isIntersecting) return;
                seen.unobserve(entry.target);
                fn();
            });
        }, { rootMargin: options.rootMargin || "0px 0px -8% 0px", threshold: options.threshold || 0.06 });
        seen.observe(el);
        // Anything already on screen on first paint should arrive immediately
        // rather than waiting for a scroll that may never come.
        var rect = el.getBoundingClientRect();
        if (rect.top < window.innerHeight && rect.bottom > 0) {
            seen.unobserve(el);
            fn();
        }
        return function () { seen.unobserve(el); };
    }

    /* ---------------------------------------------------------------------
       Reveal / stagger
       ---------------------------------------------------------------------
       The element's *stylesheet* position is the finished one. This moves it
       out, animates it back, and then hands it back to the stylesheet — an
       inline `transform: none` left behind outranks every rule the sheet has
       for the rest of the page's life, which is how a reveal silently kills a
       hover spring on the same card.
       --------------------------------------------------------------------- */
    function reveal(el, opts) {
        if (!el) return;
        var options = opts || {};
        var fromY = options.y === undefined ? 22 : options.y;
        var fromScale = options.scale === undefined ? undefined : options.scale;
        var cfg = options.cfg || { tension: 190, friction: 26 };

        if (reduced || options.instant) {
            return;
        }

        el.style.willChange = "transform, opacity";
        el.style.opacity = "0";
        el.style.transform = "translateY(" + fromY + "px)" + (fromScale !== undefined ? " scale(" + fromScale + ")" : "");

        function release() {
            el.style.removeProperty("transform");
            el.style.removeProperty("opacity");
            el.style.removeProperty("will-change");
        }

        observeInview(el, function () {
            var delay = options.delay || 0;
            setTimeout(function () {
                spring(0, 1, cfg, function (p) {
                    var y = fromY * (1 - p);
                    var scale = fromScale === undefined ? undefined : fromScale + (1 - fromScale) * p;
                    el.style.transform = "translateY(" + y.toFixed(2) + "px)" +
                        (scale === undefined ? "" : " scale(" + scale.toFixed(4) + ")");
                    el.style.opacity = String(Math.min(1, p * 3));
                }, release);
            }, delay);
        });
    }

    function stagger(host, step) {
        if (!host) return;
        var children = host.children;
        var gap = step === undefined ? 70 : step;
        Array.prototype.forEach.call(children, function (child, index) {
            reveal(child, { delay: Math.min(index * gap, 640) });
        });
    }

    /* ---------------------------------------------------------------------
       Count-up
       ---------------------------------------------------------------------
       Captures what the server rendered, animates it, and puts the captured
       value back. Restoring matters: a live refresh writing into the same node
       while the count is running must not be overwritten by the count's last
       frame with a number the page no longer believes.
       --------------------------------------------------------------------- */
    function countUp(el, opts) {
        if (!el) return;
        var options = opts || {};
        var text = (el.textContent || "").trim();
        var value = Number(text.replace(/[^0-9.\-]/g, ""));
        if (!isFinite(value)) return;
        if (reduced || options.instant) return;
        var duration = options.duration || 1200;
        var decimals = (text.split(".")[1] || "").length;
        var suffix = text.replace(/[\d.,\s-]/g, "");
        observeInview(el, function () {
            tween(duration, "easeOutExpo", function (p) {
                var shown = value * p;
                el.textContent = shown.toLocaleString("en-US", {
                    minimumFractionDigits: decimals,
                    maximumFractionDigits: decimals,
                }) + suffix;
            }, function () {
                el.textContent = text;
            });
        });
    }

    /* ---------------------------------------------------------------------
       Panels — one open, one closed, animated by height
       ---------------------------------------------------------------------
       The `0fr -> 1fr` grid trick animates an unknown height without measuring,
       so a panel of any size opens at the same speed and nothing has to be kept
       in step. `visibility` is what keeps a closed panel's controls out of the
       tab order.
       --------------------------------------------------------------------- */
    function panel(trigger, target, opts) {
        if (!trigger || !target) return null;
        var options = opts || {};
        var startOpen = options.open === undefined
            ? trigger.getAttribute("aria-expanded") === "true"
            : !!options.open;

        var wrap = target.parentElement;
        if (!wrap || !wrap.hasAttribute("data-anim-wrap")) {
            wrap = document.createElement("div");
            wrap.setAttribute("data-anim-wrap", "");
            target.parentNode.insertBefore(wrap, target);
            wrap.appendChild(target);
        }
        wrap.classList.add("anim-panel");
        target.classList.add("anim-panel-body");

        var open = false;
        /* A panel can be *its own* trigger, and that is not a trick: a page with
           one panel and a dozen triggers — the systems explorer, where every
           card points at the same detail — has no single element to hand this
           primitive, so it hands it the panel. What that must not mean is a
           click inside the panel closing it (`target` is the trigger, so a
           click on a switch in the panel would bubble to it), an
           `aria-expanded` written onto a plain `div`, or Escape moving focus
           into one. Recorded once here rather than papered over at each call
           site. */
        var selfTriggered = trigger === target;

        function set(next, announce) {
            open = !!next;
            wrap.classList.toggle("is-open", open);
            target.classList.toggle("is-open", open);
            if (!selfTriggered) trigger.setAttribute("aria-expanded", open ? "true" : "false");
            target.setAttribute("aria-hidden", open ? "false" : "true");
            if (announce && options.onChange) options.onChange(open);
        }

        if (!selfTriggered) {
            trigger.addEventListener("click", function (event) {
                event.preventDefault();
                set(!open, true);
            });
        }
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && open) {
                set(false, true);
                if (!selfTriggered) trigger.focus();
            }
        });

        set(startOpen, false);
        return {
            set: set,
            isOpen: function () { return open; },
            toggle: function () { set(!open, true); },
        };
    }

    /* ---------------------------------------------------------------------
       Menus — open, close, drop
       ---------------------------------------------------------------------
       One root, one trigger, one panel. The panel is positioned under its
       trigger by CSS; this only owns the state, the class the stylesheet
       animates, `aria-expanded`/`aria-hidden`, Escape, and the click-away.
       `data-anim-state` carries "open"/"closing"/"closed" so a stylesheet can
       give the close a different curve than the open — the drop is not just the
       arrival played backwards.
       --------------------------------------------------------------------- */
    function menu(root, opts) {
        if (!root) return null;
        var options = opts || {};
        var trigger = options.trigger || root.querySelector("[data-anim-trigger]");
        var panelEl = options.panel || root.querySelector("[data-anim-menu]");
        if (!trigger || !panelEl) return null;

        var closeTimer = null;
        var open = false;

        function paint(state) {
            root.setAttribute("data-anim-state", state);
            trigger.setAttribute("aria-expanded", open ? "true" : "false");
            panelEl.setAttribute("aria-hidden", open ? "false" : "true");
        }

        function show() {
            if (open) return;
            if (closeTimer) {
                clearTimeout(closeTimer);
                closeTimer = null;
            }
            open = true;
            panelEl.hidden = false;
            paint("open");
            if (options.onOpen) options.onOpen();
        }

        function hide() {
            if (!open) return;
            open = false;
            paint("closing");
            if (reduced) {
                panelEl.hidden = true;
                paint("closed");
                return;
            }
            closeTimer = setTimeout(function () {
                panelEl.hidden = true;
                paint("closed");
                closeTimer = null;
            }, options.closeMs || 220);
        }

        function toggle() {
            if (open) hide();
            else show();
        }

        trigger.addEventListener("click", function (event) {
            event.stopPropagation();
            toggle();
        });
        panelEl.addEventListener("click", function (event) {
            event.stopPropagation();
        });
        document.addEventListener("click", function () { hide(); });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && open) {
                hide();
                trigger.focus();
            }
        });

        panelEl.hidden = true;
        paint("closed");
        return { show: show, hide: hide, toggle: toggle, isOpen: function () { return open; } };
    }

    /* ---------------------------------------------------------------------
       Drop — a dialog that grows out of the control that opened it
       --------------------------------------------------------------------- */
    function drop(trigger, target, opts) {
        var options = opts || {};
        if (!trigger || !target) return null;
        var open = false;

        function place() {
            var rect = trigger.getBoundingClientRect();
            var width = Math.min(options.width || 320, window.innerWidth - 24);
            var left = clamp(rect.left, 12, window.innerWidth - width - 12);
            target.style.width = width + "px";
            target.style.left = left + "px";
            var below = rect.bottom + 10;
            var spaceBelow = window.innerHeight - below;
            if (spaceBelow < 220 && rect.top > spaceBelow) {
                target.style.top = "auto";
                target.style.bottom = (window.innerHeight - rect.top + 10) + "px";
            } else {
                target.style.bottom = "auto";
                target.style.top = below + "px";
            }
        }

        function set(next) {
            open = !!next;
            target.hidden = !open;
            target.setAttribute("data-anim-state", open ? "open" : "closed");
            trigger.setAttribute("aria-expanded", open ? "true" : "false");
            if (open) {
                place();
                window.addEventListener("scroll", place, { passive: true });
                window.addEventListener("resize", place);
            } else {
                window.removeEventListener("scroll", place);
                window.removeEventListener("resize", place);
            }
        }

        trigger.addEventListener("click", function (event) {
            event.stopPropagation();
            set(!open);
        });
        document.addEventListener("click", function () { if (open) set(false); });
        target.addEventListener("click", function (event) { event.stopPropagation(); });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && open) {
                set(false);
                trigger.focus();
            }
        });
        set(false);
        return { set: set, isOpen: function () { return open; } };
    }

    /* ---------------------------------------------------------------------
       Spotlight — the pointer as a CSS variable, on a fine pointer only
       --------------------------------------------------------------------- */
    function spotlight(root) {
        if (!root || reduced || !pointerFine) return;
        var target = root;
        var frame = false;
        var px = 50;
        var py = 40;
        var cx = 50;
        var cy = 40;

        root.addEventListener("pointermove", function (event) {
            var rect = root.getBoundingClientRect();
            px = ((event.clientX - rect.left) / Math.max(1, rect.width)) * 100;
            py = ((event.clientY - rect.top) / Math.max(1, rect.height)) * 100;
            if (frame) return;
            frame = true;
            requestAnimationFrame(function () {
                frame = false;
                // Eased rather than snapped: the glow reads as light in a room
                // rather than as a cursor that is being tracked.
                cx += (px - cx) * 0.16;
                cy += (py - cy) * 0.16;
                target.style.setProperty("--pointer-x", cx.toFixed(2) + "%");
                target.style.setProperty("--pointer-y", cy.toFixed(2) + "%");
            });
        });
        root.addEventListener("pointerleave", function () {
            target.style.setProperty("--pointer-x", "50%");
            target.style.setProperty("--pointer-y", "40%");
        });
    }

    /* ---------------------------------------------------------------------
       Tabs — coordinated switching, with the panel's own motion
       --------------------------------------------------------------------- */
    function tabs(root, opts) {
        if (!root) return null;
        var options = opts || {};
        var tabSelector = options.tab || "[data-anim-tab]";
        var panelSelector = options.panel || "[data-anim-tab-panel]";
        var tabsList = Array.prototype.slice.call(root.querySelectorAll(tabSelector));
        var panels = Array.prototype.slice.call(root.querySelectorAll(panelSelector));
        if (!tabsList.length) return null;
        var current = 0;

        function select(index, focus) {
            current = clamp(index, 0, tabsList.length - 1);
            tabsList.forEach(function (tab, i) {
                var on = i === current;
                tab.setAttribute("aria-selected", on ? "true" : "false");
                tab.setAttribute("tabindex", on ? "0" : "-1");
                if (on && focus) tab.focus();
            });
            panels.forEach(function (p, i) {
                var on = i === current;
                if (on) {
                    p.hidden = false;
                    p.removeAttribute("aria-hidden");
                    if (!reduced && options.motion !== false) {
                        p.style.opacity = "0";
                        p.style.transform = "translateY(10px)";
                        tween(320, "easeOutCubic", function (v) {
                            p.style.opacity = String(v);
                            p.style.transform = "translateY(" + (10 * (1 - v)).toFixed(2) + "px)";
                        }, function () {
                            p.style.removeProperty("opacity");
                            p.style.removeProperty("transform");
                        });
                    }
                } else {
                    p.hidden = true;
                    p.setAttribute("aria-hidden", "true");
                }
            });
            if (options.onSelect) options.onSelect(current, tabsList[current]);
        }

        tabsList.forEach(function (tab, index) {
            tab.addEventListener("click", function () { select(index); });
            tab.addEventListener("keydown", function (event) {
                var key = event.key;
                var next = null;
                if (key === "ArrowRight" || key === "ArrowDown") next = (index + 1) % tabsList.length;
                if (key === "ArrowLeft" || key === "ArrowUp") next = (index - 1 + tabsList.length) % tabsList.length;
                if (key === "Home") next = 0;
                if (key === "End") next = tabsList.length - 1;
                if (next === null) return;
                event.preventDefault();
                select(next, true);
            });
        });

        select(0);
        return { select: select, current: function () { return current; } };
    }

    function ready(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    window.MeowAnim = {
        ready: ready,
        reduced: function () { return reduced; },
        finePointer: function () { return pointerFine; },
        onLoop: onLoop,
        spring: spring,
        tween: tween,
        easings: EASINGS,
        observeInview: observeInview,
        reveal: reveal,
        stagger: stagger,
        countUp: countUp,
        panel: panel,
        menu: menu,
        drop: drop,
        spotlight: spotlight,
        tabs: tabs,
        clamp: clamp,
    };
})();
