/* The figures cards: what this deployment is doing, drawn from `/api/figures`.
 *
 * Two surfaces render one of these — the landing page's pair of preview cards
 * and the fleet console's *what the bot is doing* panel — and they must not
 * grow two behaviours. The whole script is here, once, and each card is a
 * `[data-figures-url]` section holding: a baked JSON payload, one or more
 * `[data-figure="control:<key>"]` / `[data-figure="activity:<key>"]` values,
 * optional `[data-period]` window buttons, an optional `[data-figures-refresh]`
 * with a `[data-figures-stamp]`, and (on the landing page) the optional
 * `[data-bar]` chart.
 *
 * Four decisions worth stating:
 *
 * * **The rendered payload is the fallback, not the network.** The published
 *   copy is a file on a CDN, so it carries the figures of the day it was built;
 *   the first thing this does is ask the live host for the current ones. If that
 *   answer never comes — a sleeping host, a blocked request, a reader offline —
 *   the card keeps the figures it was rendered with, and the stamp keeps saying
 *   what it said rather than claiming a time it did not read at.
 * * **Every number is text, never markup.** Values are assigned with
 *   `textContent`. This data is counts today; a card that wrote its payload as
 *   HTML would be one server name away from an injection on the front page.
 * * **An absent figure is an em dash, not a zero.** `/api/figures` reports "not
 *   read" separately from "none" on purpose, and printing `0` for a sleeping
 *   host would undo that in the last step.
 * * **The stamp is the reader's clock.** "read at 08:31" is local time — a
 *   visitor in Tokyo should not have to convert the operator's.
 */
(() => {
    "use strict";

    function wire(section) {
        const baked = section.querySelector("[data-figures-data]");
        let payload;
        try {
            payload = JSON.parse(baked ? baked.textContent : "null");
        } catch (error) {
            return; // the server-rendered figures stand; they are already true
        }
        if (!payload) return;

        const readAgain = section.querySelector("[data-figures-refresh]");
        const stamp = section.querySelector("[data-figures-stamp]");
        const bars = Array.from(section.querySelectorAll("[data-bar]"));
        const readout = section.querySelector("[data-bar-readout]");
        const tabs = Array.from(section.querySelectorAll("[data-period]"));
        const asked = section.dataset.figuresUrl;

        function windows() {
            return Array.isArray(payload.periods) ? payload.periods : [];
        }

        function opened() {
            return windows().find((entry) => entry.id === "week") || windows()[0] || null;
        }

        let window_ = opened();

        function setFigure(key, value) {
            const node = section.querySelector('[data-figure="' + key + '"]');
            if (node) node.textContent = value;
        }

        function paint() {
            const text = payload.text || {};
            // An em dash where there is no reading to show: absent is not zero,
            // and `/api/figures` goes to the trouble of saying so. Both halves of
            // the card follow it — the activity windows used to fall back to
            // `"0"`, which printed **0 messages** under a card whose own payload
            // was saying it had not read anything, and that is the exact claim
            // the module's docstring says it exists to avoid making.
            ["servers", "members", "systems", "commands"].forEach((key) => {
                setFigure("control:" + key, text[key] || "\u2014");
            });
            if (!window_) return;
            ["messages_text", "voice_text", "people_text", "channels_text"].forEach((key) => {
                setFigure("activity:" + key, window_[key] || "\u2014");
            });
        }

        // The bars answer the question a chart is actually asked — "which of
        // these is the big one?" — so a press names the system, its command
        // count and its share of the list.
        function selectBar(bar) {
            const already = bar.getAttribute("aria-pressed") === "true";
            bars.forEach((other) => {
                const on = other === bar && !already;
                other.setAttribute("aria-pressed", String(on));
                other.classList.toggle("is-selected", on);
            });
            if (!readout) return;
            if (already) {
                readout.textContent = "Pick a bar to see which system it is.";
                return;
            }
            const commands = Number(bar.dataset.barCommands) || 0;
            const total = bars.reduce((sum, one) => sum + (Number(one.dataset.barCommands) || 0), 0) || 1;
            readout.textContent =
                bar.dataset.barName + " — " +
                commands + (commands === 1 ? " command" : " commands") + ", " +
                Math.round((commands * 100) / total) + "% of the " + total + " across " +
                bars.length + " systems.";
        }

        bars.forEach((bar) => {
            bar.addEventListener("click", () => selectBar(bar));
        });

        // Switching windows is a redraw, not a request: every window is already
        // in the payload, so the card cannot stall on a slow host by being read.
        function selectWindow(id) {
            const found = windows().find((entry) => entry.id === id);
            if (!found) return;
            window_ = found;
            tabs.forEach((tab) => {
                const on = tab.dataset.period === id;
                tab.setAttribute("aria-pressed", String(on));
                tab.classList.toggle("is-selected", on);
            });
            paint();
        }

        function markWindow() {
            tabs.forEach((tab) => {
                const on = tab.dataset.period === (window_ && window_.id);
                tab.setAttribute("aria-pressed", String(on));
                tab.classList.toggle("is-selected", on);
            });
        }

        tabs.forEach((tab) => {
            tab.addEventListener("click", () => selectWindow(tab.dataset.period));
        });
        markWindow();

        function showReadTime(at) {
            if (!stamp) return;
            const when = Number(at);
            if (!when) return;
            stamp.title = "";
            stamp.textContent = "read at " + new Date(when * 1000).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
            });
        }

        async function readNow() {
            if (!asked) return;
            if (readAgain) readAgain.disabled = true;
            try {
                const response = await fetch(asked, {
                    headers: { Accept: "application/json" },
                    credentials: "same-origin",
                });
                if (!response.ok) throw new Error(String(response.status));
                const fresh = await response.json();
                if (!fresh || typeof fresh.text !== "object") throw new Error("unexpected shape");
                const chosen = window_ ? window_.id : "";
                payload = fresh;
                window_ = windows().find((entry) => entry.id === chosen) || opened();
                paint();
                markWindow();
                showReadTime(fresh.at);
            } catch (error) {
                // Say nothing on the page and everything in the tooltip: a failed
                // re-read is not something a reader can act on, but the stamp must
                // not claim a time it did not read at.
                if (stamp) {
                    stamp.title = "the live read did not answer — these are the figures the page was built with";
                }
            } finally {
                if (readAgain) readAgain.disabled = false;
            }
        }

        if (readAgain) readAgain.addEventListener("click", readNow);
        paint();
        readNow();
        // Two minutes: the endpoint is cached for one, so this is the fastest ask
        // that is never a wasted one, and only while somebody is looking.
        window.setInterval(() => {
            if (!document.hidden) readNow();
        }, 120000);
    }

    // Every card on the page, not the first: the console and the landing page both
    // use this script, and a page with two of them is the next thing somebody
    // will try.
    document.querySelectorAll("[data-figures-url]").forEach(wire);
})();
