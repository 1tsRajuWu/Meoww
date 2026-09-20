/** The custom cursor: a paw that marks the point, and a ring that trails it.
 *
 * This replaced a first draft that was a 36px accent-tinted donut holding the
 * platform's own 🐾 glyph, and that **inflated to 1.9×** over anything clickable
 * — so it grew exactly when it was covering the label it pointed at, and macOS
 * draws that glyph as a pair of paws, which reads as a rendering artifact
 * rather than as a cursor. Four rules hold this version together, and each one
 * is the difference between a flourish and a nuisance:
 *
 * * **It never replaces the pointer.** `cursor: none` appears nowhere, so text
 *   selection, resize handles and the browser's own affordances keep working.
 * * **Two parts, two jobs.** The paw sits on the point; the ring follows on a
 *   spring, so decoration trails *behind* the label instead of sitting on it.
 *   Over anything clickable the ring tightens and takes a tint — it never
 *   balloons.
 * * **It does not exist until the pointer moves.** The stylesheet hides it and
 *   the move handler is the only thing that reveals it, so a page that has just
 *   loaded — or one reached with the keyboard — has nothing parked in the middle
 *   of it.
 * * **It is off on touch and for `prefers-reduced-motion`**, where a trailing
 *   ring is either invisible or the exact motion the reader asked to be spared.
 *
 * One rAF loop reading refs rather than a React state update per move: a
 * component that re-renders sixty times a second to move a dot is how a
 * flourish becomes a performance problem.
 */

import { useEffect, useRef } from "react";

/** How hard each part is pulled toward the pointer, and how much it drags. */
const RING_PULL = 0.16;
const RING_DRAG = 0.7;
const PAW_PULL = 0.46;
const PAW_DRAG = 0.6;

/** Drawn rather than typed: a glyph would be a different shape on every OS. */
const PAW = (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <ellipse cx="12" cy="15.6" rx="4.7" ry="4" />
    <ellipse cx="5.3" cy="10.7" rx="2.4" ry="2.9" />
    <ellipse cx="10" cy="6.1" rx="2.3" ry="3" />
    <ellipse cx="14.7" cy="6.3" rx="2.3" ry="3" />
    <ellipse cx="19.1" cy="11" rx="2.4" ry="2.9" />
  </svg>
);

export function Cursor() {
  const shell = useRef<HTMLDivElement | null>(null);
  const ring = useRef<HTMLSpanElement | null>(null);
  const paw = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    const fine = window.matchMedia("(pointer: fine)").matches;
    const quiet = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!fine || quiet) return;

    const shellNode = shell.current;
    const ringNode = ring.current;
    const pawNode = paw.current;
    if (!shellNode || !ringNode || !pawNode) return;

    let targetX = 0;
    let targetY = 0;
    let ringX = 0;
    let ringY = 0;
    let ringVX = 0;
    let ringVY = 0;
    let pawX = 0;
    let pawY = 0;
    let pawVX = 0;
    let pawVY = 0;
    let ringScale = 1;
    let pawScale = 1;
    let hovering = false;
    let pressed = false;
    let shown = false;
    let frame = 0;

    const onMove = (event: PointerEvent) => {
      // A pointer outside the window has no position *on the page*. Some
      // environments park the mouse far off-screen (an automation harness, a
      // pointer that walked off a multi-monitor edge), and trusting that
      // coordinate sends the companion flying across the viewport from
      // nine-thousand pixels away — seen while driving this very page. Treat it
      // as the leave it is, and re-enter at the point rather than sliding in.
      const outside =
        event.clientX < -4 ||
        event.clientY < -4 ||
        event.clientX > window.innerWidth + 4 ||
        event.clientY > window.innerHeight + 4;
      if (outside) {
        onLeave();
        return;
      }
      targetX = event.clientX;
      targetY = event.clientY;
      if (!shown) {
        // Arrive where the pointer is, rather than sliding in from the corner
        // the element is anchored to.
        shown = true;
        ringX = pawX = targetX;
        ringY = pawY = targetY;
        ringVX = ringVY = pawVX = pawVY = 0;
        shellNode.style.opacity = "1";
      }
      // `event.target` is not always an Element: a move that lands on the
      // document (the scrollbar, a drag past the last node) reports `document`
      // or `window`, and `window.closest` is not a function — which threw a
      // TypeError out of this handler every such move, found by driving the page
      // and reading its console rather than by reading this file.
      const target = event.target;
      hovering =
        target instanceof Element &&
        Boolean(
          target.closest("a, button, [role='switch'], summary, label, input, select, textarea"),
        );
      shellNode.classList.toggle("cursor--link", hovering);
    };

    const onDown = () => {
      pressed = true;
    };
    const onUp = () => {
      pressed = false;
    };
    const onLeave = () => {
      shown = false;
      shellNode.style.opacity = "0";
    };

    const tick = () => {
      ringVX = (ringVX + (targetX - ringX) * RING_PULL) * RING_DRAG;
      ringVY = (ringVY + (targetY - ringY) * RING_PULL) * RING_DRAG;
      ringX += ringVX;
      ringY += ringVY;
      pawVX = (pawVX + (targetX - pawX) * PAW_PULL) * PAW_DRAG;
      pawVY = (pawVY + (targetY - pawY) * PAW_PULL) * PAW_DRAG;
      pawX += pawVX;
      pawY += pawVY;

      // A press is the one *state* worth showing: the ring tightens under the
      // hand and comes back, which is what a physical control does.
      const ringGoal = (hovering ? 1.45 : 1) * (pressed ? 0.86 : 1);
      const pawGoal = pressed ? 0.8 : 1;
      ringScale += (ringGoal - ringScale) * 0.16;
      pawScale += (pawGoal - pawScale) * 0.22;

      ringNode.style.transform = `translate3d(${ringX.toFixed(2)}px, ${ringY.toFixed(2)}px, 0) scale(${ringScale.toFixed(3)})`;
      pawNode.style.transform = `translate3d(${pawX.toFixed(2)}px, ${pawY.toFixed(2)}px, 0) scale(${pawScale.toFixed(3)})`;
      frame = window.requestAnimationFrame(tick);
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerdown", onDown, { passive: true });
    window.addEventListener("pointerup", onUp, { passive: true });
    window.addEventListener("blur", onLeave);
    document.addEventListener("pointerleave", onLeave);
    frame = window.requestAnimationFrame(tick);

    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("blur", onLeave);
      document.removeEventListener("pointerleave", onLeave);
      window.cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div className="cursor" ref={shell} aria-hidden="true">
      <span className="cursor__ring" ref={ring} />
      <span className="cursor__paw" ref={paw}>
        {PAW}
      </span>
    </div>
  );
}
