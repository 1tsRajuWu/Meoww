/* ==========================================================================
   spine.js — which waypoint the reader is at.

   The rail is a table of contents drawn vertically. A table of contents that
   cannot say *where you are* is a row of decorative circles, so this file
   answers that one question, and it is deliberately tiny because both of the
   alternatives were worse:

     * **CSS cannot answer it.** A scroll timeline can drive one line's length
       from the page's scroll position, and this sheet did exactly that — but
       "which of these five sections is the reader in?" is a question about
       *each* section, and the only way to ask it in CSS is to give the
       stylesheet one timeline name per section. That is a second copy of every
       page's section ids in a file that cannot see the list the page actually
       declares, and the whole point of `templates/meoww/_spine.html` is that the
       page's own list is the *only* author of those ids. So the browser is
       asked instead, and the answer is read out of the rail itself.
     * **No second entrance.** Nothing here animates, reveals, or adds an
       element. It flips one class on one mark and sets one custom property the
       sheet already knows how to read. `meoww.js`'s rules (no observer, no
       reveal, one entrance) exist to stop a *page* arriving in pieces; a
       position read is not an arrival, and `tests/test_meoww_site.py` pins the
       difference rather than trusting this comment.

   The sections come from the rail's own marks: each is `href="#id"`, and the
   band it points at carries that id. This file names no section, so a page that
   adds a sixth band gets a sixth waypoint that works without this file changing
   — and a page that draws no rail (the dashboard, the policies) returns here
   without doing anything at all.
   ========================================================================== */
(function () {
  "use strict";

  var nav = document.querySelector(".mw-spine");
  if (!nav) return;

  /* The marks that point somewhere. A mark whose section is missing is dropped
     rather than counted: it is a link that scrolls nowhere, which the rail's own
     law refuses to ship, and this file must not be the thing that hides it. */
  var at = [];
  Array.prototype.forEach.call(nav.querySelectorAll(".mw-spine__mark"), function (mark) {
    var id = (mark.getAttribute("href") || "").slice(1);
    var section = id ? document.getElementById(id) : null;
    if (section) at.push({ mark: mark, section: section });
  });

  /* One waypoint is not a position: with nothing to be between, the rail would
     claim a reading it cannot know. */
  if (at.length < 2) return;

  var here = -1;

  /* Where the reading line is drawn, as a fraction of the window's height — and
     the one number in this file that took a measurement to choose.

     It is not 0.5, and the difference is the whole of a defect this file used to
     have. The line was the middle of the window and the answer was the boundary
     *nearest* it; because the sections are of very different heights, the
     nearest boundary from the middle of a tall window is regularly the one
     *below* the section the reader is in. Measured on the front door at 1440×900
     the rail said "01" from the top of the page until the reader was two
     sections down, and "05" while they were reading section three.

     Sections are a sequence, so "which one am I in" has an answer that cannot
     skip: the last boundary the line has crossed. 0.38 puts that line in the
     upper third, which is where a heading arrives as the section the reader has
     just entered, rather than the middle of the glass — and it is a fraction of
     the window, so it holds on a laptop and on a tall monitor alike. */
  var READ_AT = 0.38;

  function place() {
    var line = window.innerHeight * READ_AT;
    var best = 0;
    at.forEach(function (entry, index) {
      if (entry.section.getBoundingClientRect().top <= line) best = index;
    });

    /* The last section is usually shorter than the window, so its heading can
       never reach the line: at the foot of the page it is what you are reading by
       definition, and a rail that refused to say so would claim the reader is
       still in the section above. */
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
      best = at.length - 1;
    }

    if (best === here) return;
    if (here > -1) at[here].mark.classList.remove("is-here");
    at[best].mark.classList.add("is-here");
    here = best;

    /* Where the line stops. Measured off the dot rather than computed from its
       index, because the two differ by half a dot: `0` to `1` across the rail's
       own box would leave the fill short of the dot it is naming. A hidden rail
       (`display: none` below the sheet's width) has no box to measure, so nothing
       is claimed there and the sheet's own resting state stands. */
    if (!nav.offsetHeight) return;
    var centre = at[best].mark.offsetTop + at[best].mark.offsetHeight / 2;
    nav.style.setProperty("--mw-at", (centre / nav.offsetHeight).toFixed(4));
  }

  /* Coalesced onto a frame: a scroll event fires far more often than the screen
     is painted, and this reads five rectangles. `requestAnimationFrame` is the
     throttle, not an animation — nothing here moves except the two things the
     sheet has already said may. */
  var queued = false;
  function schedule() {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(function () {
      queued = false;
      place();
    });
  }

  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  place();
})();
