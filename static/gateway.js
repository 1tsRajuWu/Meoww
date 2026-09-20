/* The gateway badge, kept honest after the page has loaded.
 *
 * The badge in the topbar is server-rendered, so it is correct at the moment
 * the response was written and says nothing about any later moment. An operator
 * who leaves a console open — which is the whole point of a console — was
 * reading a status from whenever they opened the tab. That is the complaint this
 * answers: the status was *there*, unchanged, long after it had stopped being
 * true.
 *
 * What it reads is ``/health``'s ``gateway`` field, which is
 * ``bot.is_ready()`` — the live answer — and deliberately **not** ``bot_ready``,
 * which is "this process has logged in at least once". Both consoles used the
 * latch, and a latch is why the badge read "Live" through a reconnect: the flag
 * is set on the first successful login and never cleared, so it is a fact about
 * the past wearing the word for the present.
 *
 * Three states, because a Discord bot reconnecting is not the same claim as one
 * that has never been up, and neither is "Live". Reconnecting is ordinary and
 * routine; calling it offline would send somebody looking for an outage that is
 * not happening.
 *
 * One file for both consoles. The two copies this replaces were the same
 * mistake written twice, and the second copy was the one that had gone stale.
 */
(function () {
    "use strict";

    var POLL_EVERY_MS = 30000;

    var STATES = {
        connected: { label: "Live", css: "badge-online", pulse: true },
        reconnecting: { label: "Reconnecting", css: "badge-warn", pulse: false },
        never_connected: { label: "Bot offline", css: "badge-offline", pulse: false }
    };

    var CLASSES = ["badge-online", "badge-warn", "badge-offline"];

    function badge() {
        return document.getElementById("gateway-badge");
    }

    /* Repaint the badge from one ``/health`` payload.
     *
     * Unrecognised values fall back to the offline state rather than leaving the
     * previous one on screen: a page that cannot read the answer has not learned
     * that the bot is fine, and a badge stuck on Live is the defect. */
    function paint(data) {
        var el = badge();
        if (!el || !data) return;
        var state = STATES[data.gateway] || STATES.never_connected;

        CLASSES.forEach(function (name) { el.classList.toggle(name, state.css === name); });

        var label = document.getElementById("gateway-badge-label");
        if (label && label.textContent !== state.label) label.textContent = state.label;

        var pulse = el.querySelector(".badge-pulse");
        if (state.pulse && !pulse) {
            pulse = document.createElement("span");
            pulse.className = "badge-pulse";
            pulse.setAttribute("aria-hidden", "true");
            el.insertBefore(pulse, el.firstChild);
        } else if (!state.pulse && pulse) {
            pulse.remove();
        }
    }
    window.MeowGateway = { paint: paint };

    /* A page that already polls ``/health`` for its own panels — the fleet
       console does, for the deployment reading — sets this before loading this
       file. The badge then rides that poll instead of starting a second request
       to the same address every thirty seconds. */
    if (window.MEOW_GATEWAY_FED) return;

    async function read() {
        try {
            var response = await fetch("/health", { headers: { "Accept": "application/json" } });
            if (!response.ok) return;
            paint(await response.json());
        } catch (error) {
            /* A failed poll is not worth a banner. The badge keeps the reading
               it has, and the next tick may succeed — and if the process is
               genuinely gone the page itself stops being served. */
        }
    }

    var timer = null;
    function start() {
        if (timer) return;
        timer = setInterval(function () {
            if (document.visibilityState === "visible") read();
        }, POLL_EVERY_MS);
    }
    function stop() {
        if (timer) { clearInterval(timer); timer = null; }
    }

    /* A tab coming back into view is the one moment a stale badge is most likely
       to be read, so it is refreshed before the interval resumes rather than
       after it. While hidden, nothing is polled at all. */
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") { read(); start(); } else { stop(); }
    });

    read();
    start();
})();
