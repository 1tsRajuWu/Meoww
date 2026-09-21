/* Meow — the theme, and the control that chooses it.
 *
 * Two jobs, and they run at two different times on purpose.
 *
 * 1. **Apply, immediately.** This file is loaded in `<head>` *without* `defer`,
 *    so it runs before the first paint. That is the whole reason it is not
 *    folded into `static/motion.js`: a deferred script runs after the browser
 *    has already painted the default palette, so a visitor who chose Sakura
 *    would watch the page arrive indigo and then turn rose, on every navigation.
 *    The cost is one small same-origin request, cached immutably behind the
 *    `?v=` bust the templates already use, and the page's *content* does not
 *    depend on it — if it never loads, or throws, or `localStorage` is refused
 *    (private mode, blocked cookies, a `file://` open), every page still renders
 *    in the default theme. Nothing here is allowed to break a page.
 *
 * 2. **Wire the picker**, on `DOMContentLoaded`, because the buttons live at the
 *    foot of the document and this file runs before the body exists at all.
 *
 * There is no third job. Nothing here reads the network, writes a cookie, or
 * looks at anything the visitor did not choose themselves.
 */
(function () {
    "use strict";

    /* The four, in the order the menu lists them. `midnight` is `:root` in the
     * stylesheet, so it is the answer for a visitor who has chosen nothing and
     * for one whose stored choice has since been retired. */
    var THEMES = ["midnight", "sakura", "forest", "ember"];
    var DEFAULT = "midnight";
    /* `STORAGE_KEY`, not `KEY`: a short name holding a string is what a secret
     * scanner reports as a hardcoded credential, and a false alarm in a scan is
     * one more thing to read past before the real one. It names the
     * `localStorage` entry holding the reader's theme choice. */
    var STORAGE_KEY = "meow-theme";

    /* Enough to label the trigger. The swatch colours themselves are in the
     * stylesheet, next to the tokens they belong to — a colour written down
     * twice is the defect this whole project keeps finding, and a private copy
     * in here would be no better for being small.
     *
     * The same reasoning retired `theme-color`. A theme moves the *accent*; the
     * page background is a per-page constant the templates already declare
     * (`#08080a` on the landing page, `#0b0e13` on the three document pages,
     * `#090c10` on the two consoles). Writing it from here meant every page
     * advertised one value that matched none of them. */
    var LABELS = { midnight: "Midnight", sakura: "Sakura", forest: "Forest", ember: "Ember" };

    function stored() {
        try {
            var value = window.localStorage.getItem(STORAGE_KEY);
            return THEMES.indexOf(value) === -1 ? DEFAULT : value;
        } catch (error) {
            /* A browser that refuses storage is not an error worth surfacing:
             * the theme is a preference, and the default is a fine one. */
            return DEFAULT;
        }
    }

    function save(name) {
        try {
            window.localStorage.setItem(STORAGE_KEY, name);
        } catch (error) {
            /* Chosen for this page, then. It still applies until the next load. */
        }
    }

    function apply(name) {
        var root = document.documentElement;
        if (name === DEFAULT) {
            /* Left off rather than set, so the attribute means "chosen
             * something else" and a page that has never been themed has no
             * attribute for a stylesheet or a test to be surprised by. */
            root.removeAttribute("data-theme");
        } else {
            root.setAttribute("data-theme", name);
        }
        if (document.body) {
            updateThemedAssets(name);
        }
        return name;
    }

    function updateThemedAssets(name) {
        var themeName = (name && THEMES.indexOf(name) !== -1) ? name : DEFAULT;
        var suffix = themeName === DEFAULT ? "" : "-" + themeName;

        var logos = document.querySelectorAll("img[src*='logo']");
        for (var i = 0; i < logos.length; i++) {
            var img = logos[i];
            var src = img.getAttribute("src") || "";
            var updated = src.replace(/((?:\/static\/|static\/)logo)(?:-[a-z]+)?(\.svg)/, "$1" + suffix + "$2");
            if (updated !== src) {
                img.setAttribute("src", updated);
            }
        }

        var favicons = document.querySelectorAll("link[rel*='icon']");
        for (var j = 0; j < favicons.length; j++) {
            var link = favicons[j];
            var href = link.getAttribute("href") || "";
            var updatedHref = href.replace(/((?:\/static\/|static\/)favicon)(?:-[a-z]+)?(\.svg)/, "$1" + suffix + "$2");
            if (updatedHref !== href) {
                link.setAttribute("href", updatedHref);
            }
        }
    }

    if (typeof MutationObserver !== "undefined") {
        new MutationObserver(function (mutations) {
            for (var k = 0; k < mutations.length; k++) {
                if (mutations[k].attributeName === "data-theme") {
                    var current = document.documentElement.getAttribute("data-theme") || DEFAULT;
                    updateThemedAssets(current);
                    break;
                }
            }
        }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }

    /* ---- 1. before paint -------------------------------------------------- */

    apply(stored());
    (function () {
        try {
            var c = window.localStorage.getItem("meow-cursor-style");
            if (c === "ring") c = "halo";
            var valid = ["halo", "reticle", "nova", "bubble", "stealth", "system"];
            var choice = (valid.indexOf(c) !== -1) ? c : "halo";
            document.documentElement.setAttribute("data-cursor-style", choice);
        } catch (e) {}
    })();

    /* ---- 2. the picker ---------------------------------------------------- */

    function wire() {
        var pickers = document.querySelectorAll("[data-theme-picker]");
        updateThemedAssets(stored());
        if (!pickers.length) {
            return;
        }

        function paint(name) {
            updateThemedAssets(name);
            Array.prototype.forEach.call(pickers, function (picker) {
                var trigger = picker.querySelector("[data-theme-open]");
                var label = picker.querySelector("[data-theme-name]");
                if (label) {
                    label.textContent = LABELS[name] || LABELS[DEFAULT];
                }
                if (trigger) {
                    trigger.setAttribute("aria-label", "Theme: " + (LABELS[name] || LABELS[DEFAULT]));
                }
                Array.prototype.forEach.call(
                    picker.querySelectorAll("[data-theme-value]"),
                    function (option) {
                        option.setAttribute(
                            "aria-pressed",
                            option.getAttribute("data-theme-value") === name ? "true" : "false",
                        );
                    }
                );
                var activeCursor = "halo";
                try {
                    var c = window.localStorage.getItem("meow-cursor-style");
                    if (c === "ring") c = "halo";
                    if (["halo", "reticle", "nova", "bubble", "stealth", "system"].indexOf(c) !== -1) activeCursor = c;
                } catch (error) {}
                Array.prototype.forEach.call(
                    picker.querySelectorAll("[data-cursor-value]"),
                    function (option) {
                        option.setAttribute(
                            "aria-pressed",
                            option.getAttribute("data-cursor-value") === activeCursor ? "true" : "false",
                        );
                    }
                );
            });
        }

        function close(picker) {
            var menu = picker.querySelector("[data-theme-menu]");
            var trigger = picker.querySelector("[data-theme-open]");
            if (menu) {
                menu.hidden = true;
            }
            if (trigger) {
                trigger.setAttribute("aria-expanded", "false");
            }
        }

        function closeEverywhere(except) {
            Array.prototype.forEach.call(pickers, function (picker) {
                if (picker !== except) {
                    close(picker);
                }
            });
        }

        Array.prototype.forEach.call(pickers, function (picker) {
            var trigger = picker.querySelector("[data-theme-open]");
            var menu = picker.querySelector("[data-theme-menu]");
            if (!trigger || !menu) {
                return;
            }

            trigger.addEventListener("click", function (event) {
                event.stopPropagation();
                var opening = menu.hidden;
                closeEverywhere(picker);
                menu.hidden = !opening;
                trigger.setAttribute("aria-expanded", opening ? "true" : "false");
            });

            Array.prototype.forEach.call(picker.querySelectorAll("[data-theme-value]"), function (option) {
                option.addEventListener("click", function () {
                    var name = apply(option.getAttribute("data-theme-value"));
                    save(name);
                    paint(name);
                    close(picker);
                    trigger.focus();
                });
            });

            Array.prototype.forEach.call(picker.querySelectorAll("[data-cursor-value]"), function (option) {
                option.addEventListener("click", function (event) {
                    event.stopPropagation();
                    var chosen = option.getAttribute("data-cursor-value");
                    try {
                        window.localStorage.setItem("meow-cursor-style", chosen);
                    } catch (error) {}
                    document.documentElement.setAttribute("data-cursor-style", chosen);
                    if (window.setCursorStyle) {
                        window.setCursorStyle(chosen);
                    }
                    Array.prototype.forEach.call(document.querySelectorAll("[data-cursor-value]"), function (btn) {
                        btn.setAttribute("aria-pressed", btn.getAttribute("data-cursor-value") === chosen ? "true" : "false");
                    });
                    close(picker);
                    trigger.focus();
                });
            });

            /* A menu that only closes on a second click of its own button is a
             * menu that traps a visitor who has moved on. */
            document.addEventListener("click", function () {
                close(picker);
            });
            menu.addEventListener("click", function (event) {
                event.stopPropagation();
            });
            document.addEventListener("keydown", function (event) {
                if (event.key === "Escape") {
                    close(picker);
                }
            });
        });

        paint(stored());
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", wire, { once: true });
    } else {
        wire();
    }
})();
