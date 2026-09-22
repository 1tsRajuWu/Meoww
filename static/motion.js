/* ===========================================================================
   Meow — motion for the landing page
   ===========================================================================
   The page somebody lands on has one job the dashboard does not: it has to
   arrive. Four primitives, all declared in the markup, all driven from here:

     data-loader                 the curtain; everything gated on `is-ready`
     data-text-reveal="words"    split into clip-masked words, slide up, stagger
     data-lines                  animate the `.line-inner` children, staggered
     data-reveal                 fade-and-rise the first time it is on screen
     data-reveal-delay="120"     ...after a delay, so a grid arrives in order
     data-parallax="0,12"        translateY between two percentages of scroll
     data-parallax-x="-3,3"      the same, on X, for the oversized ghost words
     data-hover="y:-8"           a spring on pointer-enter, back on leave

   Three rules, and they are the reason this file exists rather than a library:

   1. **It is 'self' only.** The CSP this project serves allows no third-party
      script, and it is not widened for an animation: everything here is a
      hundred lines of rAF. A smooth-scroll library was also deliberately not
      used — hijacking the wheel on a page that people scroll *past* to reach a
      control panel breaks zoom, anchor links and the browser's own scroll
      restoration, which is a poor trade for a nicer flick.

   2. **Nothing is required to read the page.** Every gated element starts in
      its final position in the CSS and is moved *out* by this script, so a
      blocked script, a `noscript`, or an old browser shows the whole page
      rather than a blank one. The loader is the single exception, and it is
      dismissed by CSS animation as well as by this file.

   3. **prefers-reduced-motion wins.** The loader is skipped, parallax is off,
      reveals are instant. Read once and honoured throughout, because a page
      that animates anyway is worse than a page with no motion at all.
   =========================================================================== */
(function () {
    "use strict";

    // Universal cross-browser animation support (Edge, Chrome, Safari, Firefox)
    /* Read once, and *assigned* — which it was not. `reduced` was declared
       false and read in four places (the loader's timings, the parallax guard
       and the pointer layer), so every one of those branches was dead code and
       the rule below was only true in the stylesheet: a person who had asked
       their system for less motion still got parallax and a cursor chasing the
       pointer. Found while adding the pointer layer, which is gated on it. */
    var prefersReduced = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    var reduced = prefersReduced;
    var hoverable = true;
    // A mouse or a trackpad: anything that can hover and be followed. A phone
    // reports `coarse`, and a device with neither query support (or no
    // pointer) reports nothing — so the test is positive, never "not coarse".
    var fine = !!(
        (window.matchMedia && (window.matchMedia("(pointer: fine)").matches || window.matchMedia("(any-pointer: fine)").matches)) ||
        (typeof window !== "undefined" && !('ontouchstart' in window))
    );

    /* ---------------------------------------------------------------------
       One rAF loop for every spring on the page
       --------------------------------------------------------------------- */
    var tickers = new Set();
    var last = 0;

    function tick(now) {
        // Clamped because a backgrounded tab returns a huge gap, and an
        // unclamped step makes a spring explode instead of finishing.
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

    function start(fn) {
        tickers.add(fn);
        if (!last) requestAnimationFrame(tick);
    }

    /* A value pulled toward a target by tension and released by friction —
       the same model the react-spring constants in the spec are written in.
       Integrated per step, and settled when both position and velocity are
       within a pixel, so a finished spring stops the loop rather than
       leaving a transform micro-updating for the life of the page. */
    function spring(from, to, cfg, onValue, onDone) {
        var x = from;
        var v = 0;
        var tension = (cfg && cfg.tension) || 200;
        var friction = (cfg && cfg.friction) || 26;
        start(function (dt) {
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

    var EASINGS = {
        easeOutExpo: function (t) { return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); },
        easeOutQuart: function (t) { return 1 - Math.pow(1 - t, 4); },
        easeInOutCubic: function (t) {
            return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        }
    };

    function tween(duration, easing, onValue, done) {
        var fn = EASINGS[easing] || EASINGS.easeOutExpo;
        var elapsed = 0;
        if (duration <= 0) {
            onValue(1);
            if (done) done();
            return;
        }
        start(function (dt) {
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

    /* ---------------------------------------------------------------------
       Clip-masked text
       --------------------------------------------------------------------- */
    /* Words are split here rather than written twice in the template: the
       heading in the markup is the heading a reader (and a test) sees, and the
       masking is scaffolding this script adds and can be re-run. Only elements
       with no child elements are split — a heading with a link or an <em> in it
       keeps its own markup, because re-writing mixed content from textContent
       would silently drop the elements inside it. */
    function clipWords(el) {
        if (el.childElementCount) return [];
        var words = el.textContent.trim().split(/\s+/);
        if (!words.length) return [];
        el.textContent = "";
        return words.map(function (word, i) {
            var clip = document.createElement("span");
            clip.className = "clip";
            var inner = document.createElement("span");
            inner.className = "clip-inner";
            inner.textContent = word;
            clip.appendChild(inner);
            el.appendChild(clip);
            if (i < words.length - 1) el.appendChild(document.createTextNode(" "));
            return inner;
        });
    }

    function playWords(el, opts) {
        var stagger = opts.stagger || 140;
        var duration = opts.duration || 1100;
        var inners = el.querySelectorAll(".clip-inner");
        if (!inners.length) return;
        Array.prototype.forEach.call(inners, function (inner, i) {
            inner.style.transform = "translateY(115%)";
            inner.style.opacity = "0";
            setTimeout(function () {
                tween(duration, "easeOutExpo", function (p) {
                    inner.style.transform = "translateY(" + (115 * (1 - p)).toFixed(2) + "%)";
                    inner.style.opacity = String(Math.min(1, p * 4));
                }, function () {
                    inner.style.removeProperty("transform");
                    inner.style.removeProperty("opacity");
                });
            }, i * stagger);
        });
    }

    /* Lines are separate elements in the markup (`.line > .line-inner`), so a
       two-line tagline is two pieces of real text rather than one string this
       script has to guess how to break. */
    function playLines(el, opts) {
        var stagger = opts.stagger || 120;
        var duration = opts.duration || 950;
        var base = opts.baseDelay || 0;
        var inners = el.querySelectorAll(".line-inner");
        Array.prototype.forEach.call(inners, function (inner, i) {
            inner.style.transform = "translateY(115%)";
            inner.style.opacity = "0";
            setTimeout(function () {
                tween(duration, "easeOutExpo", function (p) {
                    inner.style.transform = "translateY(" + (115 * (1 - p)).toFixed(2) + "%)";
                    inner.style.opacity = String(Math.min(1, p * 4));
                }, function () {
                    inner.style.removeProperty("transform");
                    inner.style.removeProperty("opacity");
                });
            }, base + i * stagger);
        });
    }

    /* ---------------------------------------------------------------------
       In-view rise, once
       --------------------------------------------------------------------- */
    function inview(el, opts) {
        var delay = opts.delay || 0;
        var from = opts.from;   // e.g. {y: 28, scale: 0.96}
        var cfg = opts.cfg || { tension: 200, friction: 26 };
        el.style.willChange = "transform, opacity";
        el.dataset.revealed = "0";

        function apply(y, scale, opacity) {
            var bits = [];
            if (y) bits.push("translateY(" + y.toFixed(2) + "px)");
            if (scale !== undefined) bits.push("scale(" + scale.toFixed(4) + ")");
            el.style.transform = bits.join(" ");
            el.style.opacity = String(opacity);
        }

        /* One spring drives the whole reveal — 0 to 1 — and every property is a
           function of that progress. Deriving progress from the y value instead
           is the version that divides by zero the first time a reveal wants a
           scale and no movement. */
        /* Once the reveal has finished, the element is handed back to its
           stylesheet.

           An inline style outranks every CSS rule for the rest of the page's
           life, so leaving `opacity: 1; transform: none` in place is not
           "finished" — it is a permanent override of rules this file has never
           seen. On the dashboard that silently disabled `.module-card.busy`'s
           dimming and the hover springs on anything that was also revealed: a
           reveal that removes behaviours it never touched, and invisible in
           both files, because each one is correct on its own. */
        function release() {
            el.style.removeProperty("transform");
            el.style.removeProperty("opacity");
            el.style.removeProperty("will-change");
        }

        function play() {
            setTimeout(function () {
                spring(0, 1, cfg, function (p) {
                    var y = (from.y || 0) * (1 - p);
                    var scale = from.scale !== undefined
                        ? from.scale + (1 - from.scale) * p
                        : undefined;
                    apply(y, scale, Math.min(1, p * 3));
                }, release);
                el.dataset.revealed = "1";
            }, delay);
        }

        apply(from.y || 0, from.scale, 0);

        // Same hand-back for the paths that never animate — writing "none"/"1"
        // inline here would be the identical permanent override, from the path
        // that exists precisely so the page can be still.
        if (typeof IntersectionObserver !== "function") {
            release();
            el.dataset.revealed = "1";
            return;
        }
        var seen = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (!entry.isIntersecting) return;
                seen.unobserve(entry.target);
                play();
            });
        }, { rootMargin: "60px 0px 60px 0px", threshold: 0.05 });
        seen.observe(el);

        var rect = el.getBoundingClientRect();
        if (rect.top < window.innerHeight && rect.bottom > 0) {
            play();
        }
    }

    /* ---------------------------------------------------------------------
       Drawers — a panel that opens and closes, with motion
       --------------------------------------------------------------------- */
    /* `data-drawer="<id>"` on the trigger, and that id on the panel it opens.
       The panel is animated by *height* (the `grid-template-rows: 0fr → 1fr`
       trick rather than a `max-height` guess), so a panel of any size opens at
       the same speed and nothing has to be measured or kept in step.

       Three things this does that a class toggle would not: `aria-expanded` on
       the trigger and `aria-hidden` on the closed panel stay in step with what
       is on screen; the panel is `visibility: hidden` when closed, so its
       controls are not tabbable behind a lid; and Escape closes it and puts
       focus back on the button that opened it. Under `prefers-reduced-motion`
       the stylesheet's global override makes all of that instant — the code here
       does not need a second branch for it. */
    function drawers() {
        document.querySelectorAll("[data-drawer]").forEach(function (trigger) {
            var panel = document.getElementById(trigger.dataset.drawer || "");
            if (!panel) return;

            // The wrapper is created rather than required in the markup: assuming
            // it exists means either a layout the author did not ask for (a
            // `display: grid` on somebody's card) or a silent half-working panel.
            var wrap = panel.parentElement;
            if (!wrap || !wrap.hasAttribute("data-drawer-wrap")) {
                wrap = document.createElement("div");
                wrap.setAttribute("data-drawer-wrap", "");
                panel.parentNode.insertBefore(wrap, panel);
                wrap.appendChild(panel);
            }
            wrap.classList.add("drawer-wrap");
            panel.classList.add("drawer");

            var open = false;
            function set(next) {
                open = next;
                wrap.classList.toggle("is-open", open);
                panel.classList.toggle("is-open", open);
                trigger.setAttribute("aria-expanded", open ? "true" : "false");
                panel.setAttribute("aria-hidden", open ? "false" : "true");
            }

            set(panel.dataset.drawerOpen === "1");

            trigger.addEventListener("click", function (event) {
                event.preventDefault();
                set(!open);
            });
            document.addEventListener("keydown", function (event) {
                if (event.key !== "Escape" || !open) return;
                set(false);
                trigger.focus();
            });
        });
    }

    /* ---------------------------------------------------------------------
       Scroll progress → a value
       --------------------------------------------------------------------- */
    /* 0 when the element's top is at the bottom of the viewport, 1 when its
       bottom has reached the top — the mapping the spec's parallax is written
       against, and the one that keeps a hero plate moving for exactly as long
       as the hero is on screen. */
    function progressOf(el) {
        var rect = el.getBoundingClientRect();
        var vh = window.innerHeight || 1;
        var span = vh + rect.height;
        if (span <= 0) return 0;
        return Math.min(1, Math.max(0, (vh - rect.top) / span));
    }

    function lerp(from, to, p) {
        return from + (to - from) * p;
    }

    function parsePair(value) {
        var parts = String(value || "").split(",");
        var a = parseFloat(parts[0]);
        var b = parseFloat(parts[1]);
        return [isNaN(a) ? 0 : a, isNaN(b) ? 0 : b];
    }

    /* Every parallax on the page shares one scroll listener and one rAF, and
       writes nothing when the value has not changed — a scroll handler that
       touches the DOM on every event is how a "smooth" page ends up janky. */
    function parallax() {
        var moves = [];
        document.querySelectorAll("[data-parallax]").forEach(function (el) {
            moves.push({ el: el, pair: parsePair(el.dataset.parallax), prop: "y" });
        });
        document.querySelectorAll("[data-parallax-x]").forEach(function (el) {
            moves.push({ el: el, pair: parsePair(el.dataset.parallaxX), prop: "x" });
        });
        if (!moves.length || reduced) return;

        var queued = false;
        function apply() {
            queued = false;
            moves.forEach(function (move) {
                var p = progressOf(move.el);
                var value = lerp(move.pair[0], move.pair[1], p);
                var next = move.prop === "y"
                    ? "translate3d(0," + value.toFixed(3) + "%,0)"
                    : "translate3d(" + value.toFixed(3) + "%,0,0)";
                if (move.last === next) return;
                move.last = next;
                move.el.style.transform = next;
            });
        }
        window.addEventListener("scroll", function () {
            if (queued) return;
            queued = true;
            requestAnimationFrame(apply);
        }, { passive: true });
        window.addEventListener("resize", apply, { passive: true });
        apply();
    }

    /* ---------------------------------------------------------------------
       Hover springs — pointer devices only
       --------------------------------------------------------------------- */
    function hoverSprings() {
        if (!hoverable) return;
        document.querySelectorAll("[data-hover]").forEach(function (el) {
            var target = {};
            String(el.dataset.hover).split(/[;,]/).forEach(function (pair) {
                var bits = pair.split(":");
                if (bits.length !== 2) return;
                var value = parseFloat(bits[1]);
                if (!isNaN(value)) target[bits[0].trim()] = value;
            });
            var keys = Object.keys(target);
            if (!keys.length) return;

            // The resting value is remembered per property and the spring starts
            // from *there*: starting every animation from the resting position
            // makes the leave snap rather than settle, which is the difference
            // between a spring and a class toggle.
            var state = { x: 0, y: 0, scale: 1 };
            var cfg = { tension: 300, friction: 22 };

            function apply() {
                el.style.transform =
                    "translate3d(" + state.x.toFixed(2) + "px," + state.y.toFixed(2) + "px,0) " +
                    "scale(" + state.scale.toFixed(4) + ")";
            }

            function move(active) {
                keys.forEach(function (key) {
                    var rest = key === "scale" ? 1 : 0;
                    spring(state[key], active ? target[key] : rest, cfg, function (value) {
                        state[key] = value;
                        apply();
                    });
                });
            }

            el.addEventListener("pointerenter", function () { move(true); });
            el.addEventListener("pointerleave", function () { move(false); });
        });
    }

    /* ---------------------------------------------------------------------
       The pointer: a cat that follows, and a light that drifts after it
       --------------------------------------------------------------------- */
    /* Two enhancements, both opt-in by capability and both purely additive.

       * **The cursor.** The chosen entity and nothing else: one of the six
         shapes below, each trailing on its own spring so a move reads as having
         weight. What follows the pointer is the *style*, and the only mark on
         the point itself is the one the style draws. A dot pinned to the point
         and a ring chasing it used to be built here, under the entity — both are
         gone, and the reason is the same one this file keeps relearning: no
         sheet ever showed them (`meow.css` hides both in every style, and
         `cursor-style-sparkle` says "zero center dot, zero ring" and means it),
         neither had a width or a height, so the two nodes were 0x0 boxes
         positioned every frame, and a second mark on the point is a second
         cursor rather than an improvement. The native cursor is hidden only
         **after** this layer exists and only on a device with a fine pointer —
         `cursor: none` with a script that then fails is a page with no pointer at
         all, which is a far worse trade than never having a custom one.

         **Position goes in `translate`, shape goes in `transform`, and the
         split is load-bearing.** This used to write `style.transform =
         translate3d(…)` while `meow.css` animates `transform` for the twinkle,
         the bob and the slash — and an animation outranks an inline
         declaration, so the animation did not compose with the position, it
         *replaced* it. The default sparkle swarm sat spinning in the top-left
         corner of the viewport, the blade bobbed there, and every hover and
         press rule the sheet writes about their `transform` was dead too.
         Measured on the served dashboard: the pointer at (700, 500), the
         particles at (-10, -10) — on a page whose own rule is `cursor: none`,
         so the visitor had no cursor on it at all. `translate` and `transform`
         are separate properties that compose in that order, so this script
         positions and the sheet shapes and neither can cancel the other.
       * **The glow.** One large, dim accent light drifting behind the content.
         It carries no information, so it is the first thing removed when motion
         is not wanted.

       The loop is *not* always on, and that is deliberate. A rAF loop that runs
       for the life of every page is the exact cost this project already paid
       once and removed (see `tests/test_shared_system.py`, which pins the
       absence of the old particle canvas): 45 shapes redrawn every frame behind
       content that never moved. This one starts on a pointer move and stops
       itself as soon as every layer has caught up, so an idle page costs nothing
       and a page whose pointer never moves costs nothing either. */
    var CURSOR_KEY = "meow-cursor-style";
    var CURSOR_COLOR_KEY = "meow-cursor-color";
    var CURSOR_STYLES = ["sparkle", "sword", "radar", "blob", "vortex", "system"];
    var CURSOR_COLORS = ["theme", "cyan", "magenta", "lime", "gold", "gradient-sunset", "gradient-plasma", "gradient-aurora", "gradient-cosmic"];
    var DEFAULT_CURSOR = "sparkle";
    var DEFAULT_CURSOR_COLOR = "theme";
    var activeCursorStyleHandler = null;
    var activeCursorColorHandler = null;

    function normalizeCursorStyle(val) {
        if (!val) return DEFAULT_CURSOR;
        if (val === "crosshair" || val === "ring" || val === "halo" || val === "reticle") return "radar";
        if (val === "blade" || val === "stealth") return "sword";
        if (val === "diamond") return "sparkle";
        if (val === "orbit" || val === "nova") return "vortex";
        if (val === "comet" || val === "bubble") return "blob";
        return CURSOR_STYLES.indexOf(val) === -1 ? DEFAULT_CURSOR : val;
    }

    window.setCursorStyle = function (style) {
        var s = normalizeCursorStyle(style);
        if (activeCursorStyleHandler) {
            activeCursorStyleHandler(s);
        } else {
            try { window.localStorage.setItem(CURSOR_KEY, s); } catch (e) {}
            document.documentElement.setAttribute("data-cursor-style", s);
            var buttons = document.querySelectorAll("[data-cursor-value]");
            Array.prototype.forEach.call(buttons, function (btn) {
                btn.setAttribute("aria-pressed", btn.getAttribute("data-cursor-value") === s ? "true" : "false");
            });
        }
    };
    window.cycleCursorStyle = function () {
        var current = normalizeCursorStyle(document.documentElement.getAttribute("data-cursor-style") || DEFAULT_CURSOR);
        var idx = CURSOR_STYLES.indexOf(current);
        var next = CURSOR_STYLES[(idx + 1) % CURSOR_STYLES.length];
        window.setCursorStyle(next);
        return next;
    };

    window.setCursorColor = function (color) {
        var c = CURSOR_COLORS.indexOf(color) === -1 ? DEFAULT_CURSOR_COLOR : color;
        if (activeCursorColorHandler) {
            activeCursorColorHandler(c);
        } else {
            try { window.localStorage.setItem(CURSOR_COLOR_KEY, c); } catch (e) {}
            document.documentElement.setAttribute("data-cursor-color", c);
            var buttons = document.querySelectorAll("[data-cursor-color-value]");
            Array.prototype.forEach.call(buttons, function (btn) {
                btn.setAttribute("aria-pressed", btn.getAttribute("data-cursor-color-value") === c ? "true" : "false");
            });
        }
    };
    window.cycleCursorColor = function () {
        var current = document.documentElement.getAttribute("data-cursor-color") || DEFAULT_CURSOR_COLOR;
        var idx = CURSOR_COLORS.indexOf(current);
        var next = CURSOR_COLORS[(idx + 1) % CURSOR_COLORS.length];
        window.setCursorColor(next);
        return next;
    };

    function pointerLayer() {
        if (reduced || !fine || !document.body) return;
        if (document.querySelector(".meow-cursor")) return;

        function getStoredCursor() {
            try {
                var val = window.localStorage.getItem(CURSOR_KEY);
                return normalizeCursorStyle(val);
            } catch (e) {
                return DEFAULT_CURSOR;
            }
        }
        function getStoredCursorColor() {
            try {
                var col = window.localStorage.getItem(CURSOR_COLOR_KEY);
                return CURSOR_COLORS.indexOf(col) === -1 ? DEFAULT_CURSOR_COLOR : col;
            } catch (e) {
                return DEFAULT_CURSOR_COLOR;
            }
        }

        var currentCursor = getStoredCursor();
        var currentCursorColor = getStoredCursorColor();

        var host = document.createElement("div");
        host.className = "meow-cursor cursor-style-" + currentCursor;
        host.setAttribute("aria-hidden", "true");

        // 1. Magic Sparkle Swarm (Cluster of 6 twinkling 4-point micro-stars)
        var sparkleCluster = document.createElement("div");
        sparkleCluster.className = "sparkle-cluster";
        var sparkleParticles = [];
        for (var i = 0; i < 6; i++) {
            var spEl = document.createElement("span");
            spEl.className = "sparkle-particle";
            sparkleCluster.appendChild(spEl);
            sparkleParticles.push({
                el: spEl,
                x: -100,
                y: -100,
                lag: 0.14 + (i * 0.04),
                angle: (i * Math.PI * 2) / 6,
                dist: 12 + (i % 3) * 8
            });
        }
        host.appendChild(sparkleCluster);

        // 2. Cyber Pixel Blade (16-bit retro energy dagger)
        var swordEntity = document.createElement("div");
        swordEntity.className = "sword-entity";
        var swordBlade = document.createElement("span");
        swordBlade.className = "sword-blade";
        var swordHilt = document.createElement("span");
        swordHilt.className = "sword-hilt";
        swordEntity.appendChild(swordBlade);
        swordEntity.appendChild(swordHilt);
        host.appendChild(swordEntity);

        // 3. Tactical Radar HUD (Continuous sweep beam + 4 corner target brackets)
        var radarHud = document.createElement("div");
        radarHud.className = "radar-hud";
        var radarBeam = document.createElement("span");
        radarBeam.className = "radar-sweep-beam";
        var radarPip = document.createElement("span");
        radarPip.className = "radar-center-pip";
        var bracketTL = document.createElement("span"); bracketTL.className = "radar-bracket tl";
        var bracketTR = document.createElement("span"); bracketTR.className = "radar-bracket tr";
        var bracketBL = document.createElement("span"); bracketBL.className = "radar-bracket bl";
        var bracketBR = document.createElement("span"); bracketBR.className = "radar-bracket br";
        radarHud.appendChild(radarBeam);
        radarHud.appendChild(radarPip);
        radarHud.appendChild(bracketTL);
        radarHud.appendChild(bracketTR);
        radarHud.appendChild(bracketBL);
        radarHud.appendChild(bracketBR);
        host.appendChild(radarHud);

        // 4. Fluid Velocity Blob (Liquid squash & stretch mercury droplet)
        var blobDrop = document.createElement("div");
        blobDrop.className = "blob-drop";
        var blobShine = document.createElement("span");
        blobShine.className = "blob-shine";
        blobDrop.appendChild(blobShine);
        host.appendChild(blobDrop);

        // 5. Quantum Sacred Vortex (Dual counter-rotating rune rings)
        var vortexContainer = document.createElement("div");
        vortexContainer.className = "vortex-container";
        var vortexInner = document.createElement("span");
        vortexInner.className = "vortex-inner-rune";
        var vortexOuter = document.createElement("span");
        vortexOuter.className = "vortex-outer-ring";
        var tachyon1 = document.createElement("span"); tachyon1.className = "vortex-tachyon t1";
        var tachyon2 = document.createElement("span"); tachyon2.className = "vortex-tachyon t2";
        var tachyon3 = document.createElement("span"); tachyon3.className = "vortex-tachyon t3";
        vortexOuter.appendChild(tachyon1);
        vortexOuter.appendChild(tachyon2);
        vortexOuter.appendChild(tachyon3);
        vortexContainer.appendChild(vortexInner);
        vortexContainer.appendChild(vortexOuter);
        host.appendChild(vortexContainer);

        var glow = document.createElement("div");
        glow.className = "meow-glow";
        glow.setAttribute("aria-hidden", "true");

        document.body.appendChild(host);
        document.body.appendChild(glow);
        if (currentCursor !== "system") {
            document.documentElement.classList.add("has-meow-cursor");
        } else {
            host.style.display = "none";
            glow.style.display = "none";
        }
        document.documentElement.setAttribute("data-cursor-style", currentCursor);
        document.documentElement.setAttribute("data-cursor-color", currentCursorColor);

        function updateCursorButtons(style) {
            var buttons = document.querySelectorAll("[data-cursor-value]");
            Array.prototype.forEach.call(buttons, function (btn) {
                var val = btn.getAttribute("data-cursor-value");
                btn.setAttribute("aria-pressed", val === style ? "true" : "false");
            });
        }

        function updateCursorColorButtons(color) {
            var buttons = document.querySelectorAll("[data-cursor-color-value]");
            Array.prototype.forEach.call(buttons, function (btn) {
                var val = btn.getAttribute("data-cursor-color-value");
                btn.setAttribute("aria-pressed", val === color ? "true" : "false");
            });
        }

        function applyCursorStyle(style) {
            style = normalizeCursorStyle(style);
            currentCursor = style;
            try { window.localStorage.setItem(CURSOR_KEY, style); } catch (e) {}
            document.documentElement.setAttribute("data-cursor-style", style);

            CURSOR_STYLES.forEach(function (s) {
                host.classList.remove("cursor-style-" + s);
            });
            host.classList.add("cursor-style-" + style);

            if (style === "system") {
                document.documentElement.classList.remove("has-meow-cursor");
                host.style.display = "none";
                glow.style.display = "none";
            } else {
                document.documentElement.classList.add("has-meow-cursor");
                host.style.display = "";
                glow.style.display = "";
            }
            updateCursorButtons(style);
        }

        function applyCursorColor(color) {
            if (CURSOR_COLORS.indexOf(color) === -1) color = DEFAULT_CURSOR_COLOR;
            currentCursorColor = color;
            try { window.localStorage.setItem(CURSOR_COLOR_KEY, color); } catch (e) {}
            document.documentElement.setAttribute("data-cursor-color", color);
            updateCursorColorButtons(color);
        }

        activeCursorStyleHandler = applyCursorStyle;
        activeCursorColorHandler = applyCursorColor;

        window.cycleCursorStyle = function () {
            var idx = CURSOR_STYLES.indexOf(currentCursor);
            var next = CURSOR_STYLES[(idx + 1) % CURSOR_STYLES.length];
            applyCursorStyle(next);
            return next;
        };
        window.cycleCursorColor = function () {
            var idx = CURSOR_COLORS.indexOf(currentCursorColor);
            var next = CURSOR_COLORS[(idx + 1) % CURSOR_COLORS.length];
            applyCursorColor(next);
            return next;
        };

        window.addEventListener("keydown", function (event) {
            if (event.altKey && !event.shiftKey && (event.key === "c" || event.key === "C")) {
                window.cycleCursorStyle();
            } else if (event.altKey && event.shiftKey && (event.key === "c" || event.key === "C")) {
                window.cycleCursorColor();
            }
        });

        function wireCursorButtons() {
            var buttons = document.querySelectorAll("[data-cursor-value]");
            Array.prototype.forEach.call(buttons, function (btn) {
                btn.addEventListener("click", function () {
                    var chosen = btn.getAttribute("data-cursor-value");
                    if (chosen) {
                        applyCursorStyle(chosen);
                    }
                });
            });
            var colorButtons = document.querySelectorAll("[data-cursor-color-value]");
            Array.prototype.forEach.call(colorButtons, function (btn) {
                btn.addEventListener("click", function () {
                    var chosenColor = btn.getAttribute("data-cursor-color-value");
                    if (chosenColor) {
                        applyCursorColor(chosenColor);
                    }
                });
            });
        }
        wireCursorButtons();

        document.addEventListener("click", function (event) {
            var btn = event.target && event.target.closest ? event.target.closest("[data-cursor-value]") : null;
            if (btn) {
                var chosen = btn.getAttribute("data-cursor-value");
                if (chosen) applyCursorStyle(chosen);
            }
            var colorBtn = event.target && event.target.closest ? event.target.closest("[data-cursor-color-value]") : null;
            if (colorBtn) {
                var chosenColor = colorBtn.getAttribute("data-cursor-color-value");
                if (chosenColor) applyCursorColor(chosenColor);
            }
        }, true);

        updateCursorButtons(currentCursor);
        updateCursorColorButtons(currentCursorColor);

        var x = -100;
        var y = -100;
        var glowX = -100;
        var glowY = -100;
        var blobX = -100;
        var blobY = -100;
        var prevBlobX = -100;
        var prevBlobY = -100;
        var radarX = -100;
        var radarY = -100;
        var vortexX = -100;
        var vortexY = -100;
        var scale = 1;
        var scaleTo = 1;
        var running = false;

        function frame() {
            glowX += (x - glowX) * 0.05;
            glowY += (y - glowY) * 0.05;
            scale += (scaleTo - scale) * 0.16;

            // 1. Sparkle Swarm physics
            for (var si = 0; si < sparkleParticles.length; si++) {
                var sp = sparkleParticles[si];
                sp.x += (x - sp.x) * sp.lag;
                sp.y += (y - sp.y) * sp.lag;
                sp.angle += 0.04;
                var fx = Math.cos(sp.angle) * sp.dist;
                var fy = Math.sin(sp.angle) * sp.dist;
                sp.el.style.translate = (sp.x + fx).toFixed(2) + "px " + (sp.y + fy).toFixed(2) + "px";
            }

            // 2. Cyber Blade (Tip pinned directly to pointer)
            swordEntity.style.translate = x.toFixed(2) + "px " + y.toFixed(2) + "px";

            // 3. Tactical Radar HUD
            radarX += (x - radarX) * 0.25;
            radarY += (y - radarY) * 0.25;
            radarHud.style.translate = radarX.toFixed(2) + "px " + radarY.toFixed(2) + "px";

            // 4. Fluid Velocity Blob (Squash & stretch deformation)
            blobX += (x - blobX) * 0.22;
            blobY += (y - blobY) * 0.22;
            var bvx = blobX - prevBlobX;
            var bvy = blobY - prevBlobY;
            prevBlobX = blobX;
            prevBlobY = blobY;
            var bspeed = Math.hypot(bvx, bvy);
            var bangle = bspeed > 0.05 ? (Math.atan2(bvy, bvx) * 180 / Math.PI) : 0;
            var bstretch = Math.min(0.65, bspeed * 0.05);
            blobDrop.style.translate = blobX.toFixed(2) + "px " + blobY.toFixed(2) + "px";
            blobDrop.style.rotate = bangle.toFixed(1) + "deg";
            blobDrop.style.scale =
                ((1 + bstretch) * scale).toFixed(3) + " " + ((1 / (1 + bstretch * 0.5)) * scale).toFixed(3);

            // 5. Quantum Sacred Vortex
            vortexX += (x - vortexX) * 0.20;
            vortexY += (y - vortexY) * 0.20;
            vortexContainer.style.translate = vortexX.toFixed(2) + "px " + vortexY.toFixed(2) + "px";

            glow.style.translate = glowX.toFixed(2) + "px " + glowY.toFixed(2) + "px";

            // The swarm trails the pointer by design (each particle eases in at
            // its own lag), so the loop is not finished until the particles have
            // caught up as well. Leaving them out of this test stopped the loop
            // the moment the springs settled — a handful of frames — and left
            // the six of them strung out along the path of a fast flick.
            var swarmSettled = true;
            for (var sj = 0; sj < sparkleParticles.length; sj++) {
                if (Math.abs(x - sparkleParticles[sj].x) > 0.3 || Math.abs(y - sparkleParticles[sj].y) > 0.3) {
                    swarmSettled = false;
                }
            }
            running = !swarmSettled ||
                Math.abs(x - blobX) > 0.3 || Math.abs(y - blobY) > 0.3 ||
                Math.abs(x - radarX) > 0.3 || Math.abs(y - radarY) > 0.3 ||
                Math.abs(x - vortexX) > 0.3 || Math.abs(y - vortexY) > 0.3 ||
                Math.abs(scaleTo - scale) > 0.002;
            if (running) requestAnimationFrame(frame);
        }

        function wake() {
            if (running || currentCursor === "system") return;
            running = true;
            requestAnimationFrame(frame);
        }

        window.addEventListener("pointermove", function (event) {
            // Touch reports itself as a pointer too, and on touch there is no
            // hover to enhance: the first move both reveals and deactivates the
            // layer, so a phone never carries it.
            if (event.pointerType === "touch") { hide(); return; }
            if (currentCursor === "system") return;
            var first = !host.classList.contains("is-on");
            host.classList.add("is-on");
            glow.classList.add("is-on");
            x = event.clientX;
            y = event.clientY;            if (first) {
                glowX = x;
                glowY = y;
                blobX = x;
                blobY = y;
                prevBlobX = x;
                prevBlobY = y;
                radarX = x;
                radarY = y;
                vortexX = x;
                vortexY = y;
                for (var si = 0; si < sparkleParticles.length; si++) {
                    sparkleParticles[si].x = x;
                    sparkleParticles[si].y = y;
                }
            }
            wake();
        }, { passive: true });

        function hide() {
            host.classList.remove("is-on");
            glow.classList.remove("is-on");
        }
        document.addEventListener("pointerleave", hide);
        window.addEventListener("blur", hide);

        // What is under the pointer decides the ring's state, and it is read
        // from the event's own target rather than from `elementFromPoint`: one
        // is the browser answering, the other is us measuring.
        var LINK = "a, button, [role='button'], label, select, summary, input[type='checkbox'], input[type='radio'], .btn, .chip, [data-hover]";
        var TEXT = "input[type='text'], input[type='password'], input[type='search'], input[type='email'], textarea";
        document.addEventListener("pointerover", function (event) {
            if (currentCursor === "system") return;
            var node = event.target;
            if (!node || !node.closest) return;
            var text = !!node.closest(TEXT);
            var link = !text && !!node.closest(LINK);
            host.classList.toggle("is-text", text);
            host.classList.toggle("is-link", link);
            scaleTo = link ? 1.35 : (text ? 0.85 : 1);
            wake();
        }, { passive: true });

        // Tactile click compression and ripple burst at point of contact
        var ripples = 0;
        document.addEventListener("pointerdown", function (event) {
            if (event.pointerType === "touch" || currentCursor === "system") return;
            host.classList.add("is-down");
            scaleTo = 0.82;
            wake();
            if (ripples >= 8) return;
            var ripple = document.createElement("span");
            ripple.className = "meow-click-fx";
            ripple.style.left = event.clientX + "px";
            ripple.style.top = event.clientY + "px";
            ripples += 1;
            ripple.addEventListener("animationend", function () {
                ripples -= 1;
                ripple.remove();
            });
            document.body.appendChild(ripple);
        }, { passive: true });

        document.addEventListener("pointerup", function () {
            if (currentCursor === "system") return;
            host.classList.remove("is-down");
            scaleTo = host.classList.contains("is-link") ? 1.35 : 1;
            wake();
        }, { passive: true });
    }

    /* ---------------------------------------------------------------------
       Number counter — count from 0 to the displayed value on scroll
       --------------------------------------------------------------------- */
    /* Each `.lp-stat dd` that contains a plain number (or a number with a comma
       separator) is animated once, the first time it enters the viewport.
       Non-numeric content (like a text label) is left alone. */
    function counterAnimations() {
        document.querySelectorAll(".lp-stat dd").forEach(function (dd) {
            var text = dd.textContent.trim();
            var raw = text.replace(/,/g, "");
            var target = parseInt(raw, 10);
            if (isNaN(target) || target <= 0) return;

            var hasComma = text.indexOf(",") !== -1;
            var animated = false;

            function runCount() {
                if (animated) return;
                animated = true;
                dd.textContent = "0";
                tween(1200, "easeOutExpo", function (p) {
                    var current = Math.round(target * p);
                    dd.textContent = hasComma
                        ? current.toLocaleString()
                        : String(current);
                }, function () {
                    dd.textContent = text;
                });
            }

            if (typeof IntersectionObserver !== "function") {
                runCount();
                return;
            }

            var seen = new IntersectionObserver(function (entries) {
                entries.forEach(function (entry) {
                    if (!entry.isIntersecting) return;
                    seen.unobserve(entry.target);
                    runCount();
                });
            }, { rootMargin: "60px 0px 60px 0px", threshold: 0.05 });
            seen.observe(dd);

            var rect = dd.getBoundingClientRect();
            if (rect.top < window.innerHeight && rect.bottom > 0) {
                runCount();
            }
        });
    }

    /* ---------------------------------------------------------------------
       3D perspective tilt — pointer-driven tilt on `[data-tilt]`
       --------------------------------------------------------------------- */
    /* The dashboard preview panels tilt toward the cursor with a smooth
       spring: X and Y rotation are derived from the pointer position relative
       to the element's centre. The tilt resets to neutral on pointer-leave. */
    function perspectiveTilt() {
        document.querySelectorAll("[data-tilt]").forEach(function (el) {
            var maxDeg = parseFloat(el.dataset.tilt) || 6;

            el.style.transition = "transform 0.35s cubic-bezier(0.22, 0.61, 0.36, 1)";
            el.style.transformStyle = "preserve-3d";
            if (el.parentElement) {
                el.parentElement.style.perspective = "800px";
            }

            el.addEventListener("pointermove", function (e) {
                var rect = el.getBoundingClientRect();
                var cx = rect.left + rect.width / 2;
                var cy = rect.top + rect.height / 2;
                var px = (e.clientX - cx) / (rect.width / 2);
                var py = (e.clientY - cy) / (rect.height / 2);
                var ry = px * maxDeg;
                var rx = -py * maxDeg;
                el.style.transform = "rotateX(" + rx.toFixed(2) + "deg) rotateY(" + ry.toFixed(2) + "deg)";
            });

            el.addEventListener("pointerleave", function () {
                el.style.transform = "rotateX(0deg) rotateY(0deg)";
            });
        });
    }

    /* ---------------------------------------------------------------------
       Chart bar animation — animate heights when scrolled into view
       --------------------------------------------------------------------- */
    function chartBarAnimation() {
        document.querySelectorAll(".lp-bars").forEach(function (bars) {
            function trigger() {
                bars.classList.add("is-visible");
            }

            if (typeof IntersectionObserver !== "function") {
                trigger();
                return;
            }

            var seen = new IntersectionObserver(function (entries) {
                entries.forEach(function (entry) {
                    if (!entry.isIntersecting) return;
                    seen.unobserve(entry.target);
                    trigger();
                });
            }, { rootMargin: "60px 0px 60px 0px", threshold: 0.05 });
            seen.observe(bars);

            var rect = bars.getBoundingClientRect();
            if (rect.top < window.innerHeight && rect.bottom > 0) {
                trigger();
            }
        });
    }

    /* ---------------------------------------------------------------------
       Fit a display line to its container
       --------------------------------------------------------------------- */
    /* A headline that must not wrap cannot be sized from the viewport: how much
       room a sentence needs depends on the font and the sentence, and the first
       version of this page clipped "OWN YOUR SERVER" to "OWN YOUR SERV" at
       12.5vw because a `vw` guess that fits one line of copy does not fit
       another. Measured instead — after layout and again once the fonts have
       loaded, since a fallback face has different metrics — and only ever
       shrunk, so the declared size stays the ceiling. */
    function fitLines() {
        document.querySelectorAll("[data-fit]").forEach(function (el) {
            var ceiling = parseFloat(el.dataset.fit) || 12.5;
            var box = el.parentElement || el;

            function apply() {
                el.style.fontSize = ceiling + "vw";
                // `clientWidth` includes padding, and the first version of this
                // measured against it: the headline fitted its *box* and still
                // crossed the band's padding, where `overflow: hidden` cut the
                // last letter off. The content box is the width that matters.
                var style = window.getComputedStyle(box);
                var available = box.clientWidth
                    - (parseFloat(style.paddingLeft) || 0)
                    - (parseFloat(style.paddingRight) || 0);
                var needed = el.scrollWidth;
                if (needed > available && needed > 0) {
                    // 0.995 leaves the rounding in the browser's hands rather
                    // than ours: sitting exactly on the boundary re-triggers
                    // the shrink on the next resize for no reason.
                    el.style.fontSize = (ceiling * available / needed * 0.995) + "vw";
                }
            }

            apply();
            window.addEventListener("resize", apply, { passive: true });
            if (document.fonts && document.fonts.ready) document.fonts.ready.then(apply);
        });
    }

    /* ---------------------------------------------------------------------
       The loader
       --------------------------------------------------------------------- */
    var MIN_VISIBLE_MS = 200;
    var MAX_VISIBLE_MS = 400;
    var EXIT_MS = 250;

    function loader() {
        var curtain = document.querySelector("[data-loader]");
        if (!curtain) {
            document.documentElement.classList.add("is-ready");
            document.dispatchEvent(new CustomEvent("meow:ready"));
            return;
        }

        var minVisible = reduced ? 50 : MIN_VISIBLE_MS;
        var exit = reduced ? 0 : EXIT_MS;
        var fill = curtain.querySelector(".loader-fill");
        var done = false;

        if (fill) {
            tween(reduced ? 0 : minVisible, "easeInOutCubic", function (p) {
                fill.style.transform = "scaleX(" + p.toFixed(4) + ")";
            });
        }

        function reveal() {
            if (done) return;
            done = true;
            document.documentElement.classList.add("is-ready");
            document.dispatchEvent(new CustomEvent("meow:ready"));
            document.body.style.removeProperty("overflow");
            curtain.style.pointerEvents = "none";
            if (!exit) {
                curtain.remove();
                return;
            }
            tween(exit, "easeInOutCubic", function (p) {
                curtain.style.transform = "translateY(" + (-105 * p).toFixed(2) + "%)";
            }, function () {
                curtain.remove();
            });
        }

        var timer = setTimeout(reveal, minVisible);
        setTimeout(reveal, MAX_VISIBLE_MS);
        if (document.readyState === "complete") {
            reveal();
        } else {
            window.addEventListener("load", function () {
                reveal();
            });
        }
    }

    /* ---------------------------------------------------------------------
       Wiring
       --------------------------------------------------------------------- */
    function ready(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    ready(function () {
        // Text reveals are prepared now and *played* when the curtain lifts, so
        // the most important words on the page are not spent behind it.
        var wordHeads = [];
        document.querySelectorAll('[data-text-reveal="words"]').forEach(function (el) {
            clipWords(el);
            wordHeads.push(el);
        });
        var lineHeads = [];
        document.querySelectorAll("[data-lines]").forEach(function (el) {
            lineHeads.push(el);
        });

        var entranceTriggered = false;
        function triggerEntrance() {
            if (entranceTriggered) return;
            entranceTriggered = true;
            wordHeads.forEach(function (el) {
                playWords(el, {
                    stagger: parseInt(el.dataset.stagger || "140", 10),
                    duration: parseInt(el.dataset.duration || "1100", 10)
                });
            });
            lineHeads.forEach(function (el) {
                playLines(el, {
                    stagger: parseInt(el.dataset.stagger || "110", 10),
                    duration: parseInt(el.dataset.duration || "950", 10),
                    baseDelay: parseInt(el.dataset.baseDelay || "350", 10)
                });
            });
        }

        if (document.documentElement.classList.contains("is-ready")) {
            triggerEntrance();
        } else {
            document.addEventListener("meow:ready", triggerEntrance, { once: true });
        }
        setTimeout(triggerEntrance, 450);

        loader();

        /* `data-stagger="70"` stamps a delay on each revealing child, so a grid
           arrives in order without a number written into every card. Stamped
           before the loop below reads it, and never over an explicit delay. */
        document.querySelectorAll("[data-stagger]").forEach(function (group) {
            var step = parseInt(group.dataset.stagger || "60", 10);
            group.querySelectorAll("[data-reveal]").forEach(function (kid, i) {
                if (!kid.dataset.revealDelay) kid.dataset.revealDelay = String(i * step);
            });
        });

        document.querySelectorAll("[data-reveal]").forEach(function (el) {
            var from = { y: parseFloat(el.dataset.revealY || "28") };
            if (el.dataset.revealScale) from.scale = parseFloat(el.dataset.revealScale);
            inview(el, {
                delay: parseInt(el.dataset.revealDelay || "0", 10),
                from: from,
                cfg: { tension: 190, friction: 26 }
            });
        });

        fitLines();
        parallax();
        hoverSprings();
        drawers();
        pointerLayer();
        counterAnimations();
        perspectiveTilt();
        chartBarAnimation();

        // The menu: focus goes into it, escape closes it, and the page behind
        // is not scrollable while it is open.
        var menu = document.querySelector("[data-menu]");
        var openers = document.querySelectorAll("[data-menu-open]");
        if (menu) {
            var lastOpener = null;
            var grab = 0;

            /* Focus has to wait until the overlay is actually *visible*, not
               just until the class lands. `.lp-menu` transitions `visibility`
               over 260ms so that a closing menu stays on screen while it fades,
               and a browser silently ignores `focus()` on an element that is
               still hidden — which is how the first version opened the menu and
               left the caret out on the page behind it, one frame earlier doing
               exactly the same nothing.

               Rather than guess which frame the transition flips on (or how
               long it lasts, which is a number in the stylesheet this file
               would then be duplicating), the attempt is simply repeated until
               it takes. It stops immediately when it does, so the normal case
               costs two frames, and reduced motion — where the duration is
               flattened to nothing — takes on the first. */
            function grabFocus() {
                // The menu's own links first: the brand in the panel's header is
                // also focusable and comes first in the DOM, so landing there
                // meant that pressing Enter on an freshly opened menu navigated
                // straight home — the opposite of what opening a menu is for.
                var first = menu.querySelector("nav a")
                    || menu.querySelector("[data-menu-close]")
                    || menu.querySelector("a, button");
                if (!first) return;
                var mine = ++grab;
                (function attempt(n) {
                    // A newer open/close owns the caret now: drop this one rather
                    // than fight it for focus.
                    if (mine !== grab) return;
                    first.focus();
                    if (document.activeElement === first) return;
                    if (n >= 60) return;   // ~1s, then give up rather than spin
                    requestAnimationFrame(function () { attempt(n + 1); });
                })(0);
            }

            function setMenu(open) {
                menu.classList.toggle("is-open", open);
                menu.setAttribute("aria-hidden", String(!open));
                document.body.classList.toggle("menu-open", open);
                openers.forEach(function (btn) {
                    btn.setAttribute("aria-expanded", String(open));
                });
                if (open) {
                    grabFocus();
                } else {
                    // Closing invalidates any focus attempt still in flight, so
                    // a delayed one cannot pull the caret back into a closed
                    // overlay after the opener has taken it back.
                    grab++;
                    if (lastOpener) lastOpener.focus();
                }
            }
            openers.forEach(function (btn) {
                btn.addEventListener("click", function () {
                    lastOpener = btn;
                    setMenu(true);
                });
            });
            menu.querySelectorAll("[data-menu-close]").forEach(function (btn) {
                btn.addEventListener("click", function () { setMenu(false); });
            });
            menu.querySelectorAll("a[href^='#'], a[href^='/']").forEach(function (link) {
                link.addEventListener("click", function () { setMenu(false); });
            });
            document.addEventListener("keydown", function (event) {
                // Only when it is actually open: an unguarded close would take
                // the caret back to the burger button every time somebody
                // pressed Escape anywhere on the page.
                if (event.key === "Escape" && menu.classList.contains("is-open")) {
                    setMenu(false);
                }
            });
        }
    });
})();
