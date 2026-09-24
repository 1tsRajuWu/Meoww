/* Keystone Hub — behaviour.
 *
 * One file, no framework, no inline script anywhere (the hub is served under a
 * `script-src 'self'` policy). Every block below is guarded by whether its
 * elements are on *this* page, so the same file loads on all seven pages and
 * each one costs only what it uses.
 *
 * The rule this file keeps, and the reason it is worth stating: **a live read
 * never overwrites a rendered figure with a worse one.** The server paints the
 * hub from real data — the whole registry, the command tree, whatever tracking
 * exists — so a refresh may only *replace* a reading, never blank it. A failed
 * poll leaves the page exactly as it was built and says how old it is, which is
 * the honest answer to a free-tier host that has gone to sleep.
 */
"use strict";

(() => {
    const root = document.documentElement;

    /* ── theme ──────────────────────────────────────────────────────────── */

    const THEME_KEY = "meow-hub-theme";

    function preferredTheme() {
        const saved = localStorage.getItem(THEME_KEY);
        if (saved === "dark" || saved === "light") return saved;
        return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }

    function paintTheme(theme) {
        root.dataset.theme = theme;
        document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
            const next = theme === "dark" ? "light" : "dark";
            // The label names the theme you would *get*, not the one you are in:
            // a bare "Light" beside a sun icon reads as "you are in light mode"
            // on a page that is visibly dark, which is the opposite of helpful.
            button.setAttribute("aria-label", `Switch to the ${next} theme`);
            const label = button.querySelector("[data-theme-label]");
            if (label) label.textContent = next === "light" ? "Light mode" : "Dark mode";
        });
    }

    function initTheme() {
        paintTheme(preferredTheme());
        document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
            button.addEventListener("click", () => {
                const next = root.dataset.theme === "dark" ? "light" : "dark";
                localStorage.setItem(THEME_KEY, next);
                paintTheme(next);
            });
        });
        // Only follow the system while the visitor has not chosen.
        window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
            if (!localStorage.getItem(THEME_KEY)) paintTheme(preferredTheme());
        });
    }

    /* ── the rail's small-screen drawer ─────────────────────────────────── */

    function initRail() {
        const rail = document.getElementById("rail");
        const toggle = document.querySelector("[data-rail-toggle]");
        if (!rail || !toggle) return;

        const setOpen = (open) => {
            rail.classList.toggle("is-open", open);
            toggle.setAttribute("aria-expanded", open ? "true" : "false");
        };

        toggle.addEventListener("click", () => setOpen(!rail.classList.contains("is-open")));
        rail.addEventListener("click", (event) => {
            if (event.target.closest("a")) setOpen(false);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") setOpen(false);
        });
        // A drawer left open while the window grows would sit over the content.
        window.matchMedia("(min-width: 861px)").addEventListener("change", (event) => {
            if (event.matches) setOpen(false);
        });
    }

    /* ── reveal, and the reading line ───────────────────────────────────── */

    function initReveal() {
        const targets = document.querySelectorAll("[data-reveal]");
        if (!targets.length) return;
        if (!("IntersectionObserver" in window)) {
            targets.forEach((el) => el.classList.add("is-in"));
            return;
        }
        const observer = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                entry.target.classList.add("is-in");
                observer.unobserve(entry.target);
            });
        }, { rootMargin: "0px 0px -8% 0px" });
        targets.forEach((el) => observer.observe(el));
    }

    function initProgress() {
        const bar = document.querySelector(".scroll-progress");
        if (!bar) return;
        const paint = () => {
            const max = document.documentElement.scrollHeight - window.innerHeight;
            const ratio = max > 0 ? Math.min(1, window.scrollY / max) : 0;
            bar.style.transform = `scaleX(${ratio.toFixed(4)})`;
        };
        paint();
        window.addEventListener("scroll", paint, { passive: true });
        window.addEventListener("resize", paint);
    }

    /* ── toasts ─────────────────────────────────────────────────────────── */

    function toast(message) {
        let host = document.querySelector(".toasts");
        if (!host) {
            host = document.createElement("div");
            host.className = "toasts";
            host.setAttribute("role", "status");
            host.setAttribute("aria-live", "polite");
            document.body.appendChild(host);
        }
        const item = document.createElement("div");
        item.className = "toast";
        item.textContent = message;
        host.appendChild(item);
        setTimeout(() => item.remove(), 2600);
    }

    /* ── copyable endpoints ─────────────────────────────────────────────── */

    function initCopy() {
        document.querySelectorAll("[data-copy]").forEach((button) => {
            button.addEventListener("click", async () => {
                const value = button.getAttribute("data-copy") || "";
                try {
                    await navigator.clipboard.writeText(value);
                    toast("Copied");
                } catch (error) {
                    // Clipboard access needs a secure context and a user gesture;
                    // when it is refused the value is still on screen and
                    // selectable, so this says so rather than failing silently.
                    toast("Copy was refused — select the text instead");
                }
            });
        });
    }

    /* ── time, said the way a reader wants it ───────────────────────────── */

    function relative(seconds) {
        const value = Math.max(0, Math.round(seconds));
        if (value < 5) return "just now";
        if (value < 60) return `${value} seconds ago`;
        const minutes = Math.round(value / 60);
        if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
        const hours = Math.round(minutes / 60);
        if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
        const days = Math.round(hours / 24);
        return `${days} day${days === 1 ? "" : "s"} ago`;
    }

    function stampAge(host) {
        const at = Number(host.getAttribute("data-at") || 0);
        if (!at) return;
        const seconds = Date.now() / 1000 - at;
        host.textContent = `read ${relative(seconds)}`;
    }

    function initAges() {
        const hosts = document.querySelectorAll("[data-at]");
        if (!hosts.length) return;
        const paint = () => hosts.forEach(stampAge);
        paint();
        setInterval(paint, 30000);
    }

    /* ── meters ─────────────────────────────────────────────────────────── */

    function initMeters() {
        document.querySelectorAll("[data-meter]").forEach((meter) => {
            const bar = meter.querySelector("i");
            if (!bar) return;
            const value = Number(meter.getAttribute("data-value") || 0);
            const max = Number(meter.getAttribute("data-max") || 100);
            const ratio = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
            // CSSOM rather than a style attribute: the policy on this host has no
            // `unsafe-inline` for styles, and an attribute would be a silent
            // zero-width bar with a console error beside it.
            bar.style.width = `${(ratio * 100).toFixed(1)}%`;
        });
    }

    /* ── the count bars ─────────────────────────────────────────────────── */

    function paintBars() {
        const bars = Array.from(document.querySelectorAll("[data-bar-period]"));
        if (!bars.length) return;
        const values = bars.map((bar) => Number(bar.getAttribute("data-bar-value") || 0));
        // The tallest bar is the scale, recomputed from whatever the bars are
        // currently holding — so a live read that changes one window re-scales
        // the whole chart rather than overflowing it.
        const peak = Math.max(...values, 1);
        // Nothing was read at all: every bar would sit at its 1.5% floor, which
        // looks like a chart of one constant rather than like a chart of no
        // reading. The floor stays — a bar that vanishes is indistinguishable
        // from one that failed to draw — and the empty state says which it is.
        const empty = values.every((value) => !value);
        bars.forEach((bar, index) => {
            const ratio = Math.max(0, Math.min(1, values[index] / peak));
            const height = Math.max(ratio * 100, values[index] > 0 ? 3 : 1.5);
            bar.style.height = `${height.toFixed(1)}%`;
            bar.classList.toggle("is-empty", empty);
        });
    }

    /* ── the command palette ────────────────────────────────────────────── */

    function initPalette() {
        const palette = document.getElementById("palette");
        if (!palette) return;
        const input = palette.querySelector(".palette-input");
        const list = palette.querySelector(".palette-list");
        const trigger = document.querySelector("[data-palette-open]");
        const indexUrl = palette.getAttribute("data-index") || "static/hub-index.json";

        let entries = null;
        let rows = [];
        let active = 0;
        let load = null;
        let returnFocus = null;

        function loadIndex() {
            if (!load) {
                load = fetch(indexUrl, { headers: { Accept: "application/json" } })
                    .then((response) => (response.ok ? response.json() : { entries: [] }))
                    .then((data) => {
                        entries = Array.isArray(data && data.entries) ? data.entries : [];
                        return entries;
                    })
                    .catch(() => {
                        entries = [];
                        return entries;
                    });
            }
            return load;
        }

        function score(entry, needle) {
            const haystack = `${entry.label} ${entry.hint || ""} ${entry.text || ""}`.toLowerCase();
            if (!needle) return 1;
            const at = haystack.indexOf(needle);
            if (at < 0) return 0;
            // A label that starts with what was typed beats one that mentions it.
            return (entry.label || "").toLowerCase().startsWith(needle) ? 3 : 2;
        }

        function render(query) {
            const needle = query.trim().toLowerCase();
            rows = (entries || [])
                .map((entry) => ({ entry, rank: score(entry, needle) }))
                .filter((row) => row.rank > 0)
                .sort((a, b) => b.rank - a.rank)
                .slice(0, 40)
                .map((row) => row.entry);
            active = 0;
            list.textContent = "";
            if (!rows.length) {
                const empty = document.createElement("p");
                empty.className = "palette-empty";
                empty.textContent = "Nothing matches that.";
                list.appendChild(empty);
                return;
            }
            rows.forEach((entry, index) => {
                const item = entry.href ? document.createElement("a") : document.createElement("div");
                item.className = "palette-item" + (index === active ? " is-active" : "");
                if (entry.href) item.setAttribute("href", entry.href);
                const kind = document.createElement("span");
                kind.className = "palette-kind";
                kind.textContent = entry.kind || "";
                const label = document.createElement("b");
                label.textContent = entry.label;
                const hint = document.createElement("small");
                hint.textContent = entry.hint || "";
                item.append(kind, label, hint);
                item.addEventListener("mouseenter", () => {
                    active = index;
                    highlight();
                });
                if (entry.copy) {
                    item.addEventListener("click", async (event) => {
                        event.preventDefault();
                        try {
                            await navigator.clipboard.writeText(entry.copy);
                            toast(`Copied ${entry.copy}`);
                        } catch (error) {
                            toast("Copy was refused — select the text instead");
                        }
                        close();
                    });
                }
                list.appendChild(item);
            });
        }

        function highlight() {
            list.querySelectorAll(".palette-item").forEach((item, index) => {
                item.classList.toggle("is-active", index === active);
            });
            const current = list.querySelectorAll(".palette-item")[active];
            if (current && current.scrollIntoView) current.scrollIntoView({ block: "nearest" });
        }

        function open() {
            returnFocus = document.activeElement;
            palette.classList.add("is-open");
            // The page behind a modal must not scroll. Set on `documentElement`
            // rather than `body` so it also covers the case where the page is
            // scrolled and the body's own height is what is moving.
            root.dataset.locked = "1";
            input.value = "";
            render("");
            // Re-rendered from **whatever is in the box**, not from the empty
            // string: the index is a network request, and somebody who starts
            // typing before it lands would otherwise watch their query be wiped
            // the moment it arrives.
            loadIndex().then(() => render(input.value));
            input.focus();
        }

        function close() {
            palette.classList.remove("is-open");
            delete root.dataset.locked;
            if (returnFocus && returnFocus.focus) returnFocus.focus();
        }

        if (trigger) trigger.addEventListener("click", open);
        input.addEventListener("input", () => render(input.value));

        input.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown") {
                event.preventDefault();
                active = Math.min(rows.length - 1, active + 1);
                highlight();
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                active = Math.max(0, active - 1);
                highlight();
            } else if (event.key === "Enter") {
                const chosen = rows[active];
                if (!chosen) return;
                event.preventDefault();
                if (chosen.href) window.location.assign(chosen.href);
                else if (chosen.copy) {
                    navigator.clipboard.writeText(chosen.copy)
                        .then(() => toast(`Copied ${chosen.copy}`))
                        .catch(() => toast("Copy was refused — select the text instead"));
                    close();
                }
            }
        });

        palette.addEventListener("click", (event) => {
            if (event.target.classList.contains("palette-backdrop") || event.target.closest("[data-palette-close]")) close();
        });

        document.addEventListener("keydown", (event) => {
            const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((event.target.tagName || ""));
            if ((event.key === "k" || event.key === "K") && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                palette.classList.contains("is-open") ? close() : open();
            } else if (event.key === "/" && !typing && !palette.classList.contains("is-open")) {
                event.preventDefault();
                open();
            } else if (event.key === "Escape" && palette.classList.contains("is-open")) {
                close();
            }
        });
    }

    /* ── the live read ──────────────────────────────────────────────────── */

    function initLive() {
        const statusUrl = document.body && document.body.getAttribute("data-status-url");
        if (!statusUrl) return;

        const slots = document.querySelectorAll("[data-live]");
        const stamp = document.querySelector("[data-live-stamp]");
        const railLive = document.querySelector("[data-rail-live]");
        let failures = 0;

        /* What one slot should say, or `undefined` when this payload has nothing
           to say about it. Written as one lookup rather than as a chain of
           assignments, because every branch is a different endpoint field and a
           chain is how a slot silently keeps a build-time figure. */
        function valueFor(key, payload) {
            const figures = payload.figures || {};
            const text = figures.text || {};
            const config = payload.config || {};
            const period = payload.period || {};
            if (key === "connected") {
                return (payload.gateway || {}).connected ? "Live" : "Not connected";
            }
            if (key === "config-unset") return config.unset;
            if (key === "config-attention") return config.attention;
            if (key.startsWith("config-present-")) {
                const wanted = key.slice("config-present-".length);
                const row = (config.rows || []).find(
                    (item) => String(item.label || "").toLowerCase() === wanted,
                );
                return row && typeof row.missing === "number"
                    ? row.total - row.missing
                    : undefined;
            }
            if (key.startsWith("config-")) {
                const wanted = key.slice("config-".length);
                const row = (config.rows || []).find(
                    (item) => String(item.label || "").toLowerCase() === wanted,
                );
                return row ? row.missing : undefined;
            }
            if (key === "messages") return period.messages_text;
            if (key === "voice") return period.voice_text;
            if (key === "people") return period.people_text;
            if (key.startsWith("period-")) {
                const [, id, field] = key.split("-");
                const row = (figures.periods || []).find((item) => item.id === id);
                return row ? row[`${field}_text`] : undefined;
            }
            return text[key] !== undefined ? text[key] : figures[key];
        }

        function paint(payload) {
            slots.forEach((slot) => {
                const spelled = valueFor(slot.getAttribute("data-live"), payload);
                // Zero is a reading and gets painted; absent is not and does not.
                if (spelled === undefined || spelled === null || spelled === "") return;
                slot.textContent = spelled;
            });

            const periods = (payload.figures || {}).periods;
            if (Array.isArray(periods)) {
                document.querySelectorAll("[data-bar-period]").forEach((bar) => {
                    const row = periods.find(
                        (item) => item.id === bar.getAttribute("data-bar-period"),
                    );
                    if (row && typeof row.messages === "number") {
                        bar.setAttribute("data-bar-value", String(row.messages));
                    }
                });
                paintBars();
            }

            document.querySelectorAll("[data-live-state]").forEach((node) => {
                const key = node.getAttribute("data-live-state");
                const config = payload.config || {};
                let ok = null;
                if (key === "gateway") ok = Boolean(gateway.connected);
                // Only when the payload actually carries a number. An unread
                // configuration is not a clean one, and it is not a dirty one
                // either — so the dot keeps whatever the server painted rather
                // than being turned amber by an absence.
                if (key === "config" && typeof config.attention === "number") {
                    ok = config.attention === 0;
                }
                if (key === "site" && typeof payload.site_guard === "object") {
                    ok = (payload.site_guard || {}).status === "clean";
                }
                if (ok === null) return;
                node.classList.remove("is-ok", "is-warn", "is-bad", "is-idle");
                node.classList.add(ok ? "is-ok" : "is-warn");
            });

            if (railLive) {
                const live = Boolean(gateway.connected);
                const dot = railLive.querySelector(".dot");
                const textNode = railLive.querySelector("[data-rail-state]");
                if (dot) {
                    dot.classList.remove("is-ok", "is-bad", "is-idle", "is-live");
                    dot.classList.add(live ? "is-ok" : "is-bad");
                    if (live) dot.classList.add("is-live");
                }
                if (textNode) textNode.textContent = live ? "accepting commands" : "not connected";
            }
        }

        function report() {
            if (!stamp) return;
            if (failures === 0) {
                stamp.textContent = "read just now";
                stamp.classList.remove("is-stale");
            } else {
                // The page keeps the figures it was built with, and says so. A
                // host that has gone to sleep is a fact worth reading, not a
                // blank card.
                stamp.textContent = failures === 1
                    ? "live read did not answer — showing the figures this page was built with"
                    : `live read has not answered ${failures} times — showing the figures this page was built with`;
                stamp.classList.add("is-stale");
            }
        }

        async function read() {
            try {
                const response = await fetch(statusUrl, {
                    headers: { Accept: "application/json" },
                    cache: "no-store",
                });
                if (!response.ok) throw new Error(String(response.status));
                const payload = await response.json();
                if (!payload || typeof payload !== "object") throw new Error("empty payload");
                failures = 0;
                paint(payload);
                if (payload.at) {
                    document.querySelectorAll("[data-live-at]").forEach((node) => {
                        node.setAttribute("data-at", String(payload.at));
                        stampAge(node);
                    });
                }
            } catch (error) {
                failures += 1;
            }
            report();
        }

        /* The reading is fetched, so the interval is a cost rather than a
           convenience: every tick is a request this host answers and is billed
           for. A hidden tab asks for nothing — the page behind another window,
           the laptop that was closed, the session the browser restored last
           week — and it asks again the moment it is looked at, so nobody is
           shown a stale reading after coming back to it. */
        read();
        var timer = null;
        const start = () => {
            if (timer === null) timer = setInterval(() => {
                if (!document.hidden) read();
            }, 30000);
        };
        const stop = () => {
            if (timer !== null) {
                clearInterval(timer);
                timer = null;
            }
        };
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                stop();
            } else {
                read();
                start();
            }
        });
        if (!document.hidden) start();
    }

    /* ── go ─────────────────────────────────────────────────────────────── */

    function start() {
        // `initTheme` attaches the click handler and writes the button's label;
        // `paintTheme` above only sets the attribute. Leaving the first one out
        // of this list is how the theme toggle came to be dead on every page —
        // the button rendered, read correctly, and did nothing at all when
        // pressed, which is the kind of defect only *driving* the page finds.
        initTheme();
        initRail();
        initReveal();
        initProgress();
        initCopy();
        initMeters();
        paintBars();
        initAges();
        initPalette();
        initLive();
    }

    // The theme is applied as early as possible — before the body is parsed —
    // because the alternative is a page that paints one theme and jumps to the
    // other. It touches only `documentElement`, so it is safe here.
    paintTheme(preferredTheme());

    // Everything else needs the document. The hub loads this file with `defer`,
    // so the body is normally there already; the guard is here because the first
    // version of this block was not, and a single null `document.body` inside
    // `initLive` threw and took **every initialiser after it** down with it —
    // the reveal observer, the palette, the meters. A page that renders blank
    // above the fold looks exactly like a page whose stylesheet failed, so the
    // symptom gave no hint at all about the cause.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start, { once: true });
    } else {
        start();
    }
})();
