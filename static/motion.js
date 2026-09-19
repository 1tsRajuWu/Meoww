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

    var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var hoverable = !window.matchMedia("(max-width: 768px)").matches && !reduced;

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
        if (reduced || typeof IntersectionObserver !== "function") {
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
        }, { rootMargin: "0px 0px -60px 0px" });
        seen.observe(el);
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
       Number counter — count from 0 to the displayed value on scroll
       --------------------------------------------------------------------- */
    /* Each `.lp-stat dd` that contains a plain number (or a number with a comma
       separator) is animated once, the first time it enters the viewport.
       Non-numeric content (like a text label) is left alone. */
    function counterAnimations() {
        if (reduced || typeof IntersectionObserver !== "function") return;

        document.querySelectorAll(".lp-stat dd").forEach(function (dd) {
            var text = dd.textContent.trim();
            var raw = text.replace(/,/g, "");
            var target = parseInt(raw, 10);
            if (isNaN(target) || target <= 0) return;

            var hasComma = text.indexOf(",") !== -1;
            dd.textContent = "0";

            var seen = new IntersectionObserver(function (entries) {
                entries.forEach(function (entry) {
                    if (!entry.isIntersecting) return;
                    seen.unobserve(entry.target);
                    tween(1400, "easeOutExpo", function (p) {
                        var current = Math.round(target * p);
                        dd.textContent = hasComma
                            ? current.toLocaleString()
                            : String(current);
                    });
                });
            }, { rootMargin: "0px 0px -40px 0px" });
            seen.observe(dd);
        });
    }

    /* ---------------------------------------------------------------------
       3D perspective tilt — pointer-driven tilt on `[data-tilt]`
       --------------------------------------------------------------------- */
    /* The dashboard preview panels tilt toward the cursor with a smooth
       spring: X and Y rotation are derived from the pointer position relative
       to the element's centre. The tilt resets to neutral on pointer-leave. */
    function perspectiveTilt() {
        if (!hoverable) return;

        document.querySelectorAll("[data-tilt]").forEach(function (el) {
            var maxDeg = parseFloat(el.dataset.tilt) || 6;

            el.style.transition = "transform 0.4s var(--ease, cubic-bezier(0.22, 0.61, 0.36, 1))";
            el.style.transformStyle = "preserve-3d";
            el.parentElement.style.perspective = "800px";

            el.addEventListener("pointermove", function (e) {
                var rect = el.getBoundingClientRect();
                var cx = rect.left + rect.width / 2;
                var cy = rect.top + rect.height / 2;
                var px = (e.clientX - cx) / (rect.width / 2);
                var py = (e.clientY - cy) / (rect.height / 2);
                // Rotate Y by horizontal offset, rotate X by *negative* vertical offset
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
        if (reduced || typeof IntersectionObserver !== "function") {
            document.querySelectorAll(".lp-bars").forEach(function (bars) {
                bars.classList.add("is-visible");
            });
            return;
        }

        document.querySelectorAll(".lp-bars").forEach(function (bars) {
            var seen = new IntersectionObserver(function (entries) {
                entries.forEach(function (entry) {
                    if (!entry.isIntersecting) return;
                    seen.unobserve(entry.target);
                    bars.classList.add("is-visible");
                });
            }, { rootMargin: "0px 0px -40px 0px" });
            seen.observe(bars);
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

        if (reduced) {
            document.querySelectorAll(".clip-inner, .line-inner").forEach(function (el) {
                el.style.transform = "none";
                el.style.opacity = "1";
            });
        }

        document.addEventListener("meow:ready", function () {
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
        });

        loader();

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
