/** The custom cursor: one hairline reticle that trails the pointer on a spring.
 *
 * This replaced a paw sitting on the point. The paw was drawn *at* the pointer,
 * which is the one place a decoration must not be: the browser's own arrow is
 * already there and is the thing that decides what a click hits, so the two
 * overlapped into a small smear that read — correctly — as a rendering artifact.
 * Four rules hold this version together, and each one is the difference between a
 * flourish and a nuisance:
 *
 * * **The arrow stays the pointer.** `cursor: none` appears nowhere, so text
 *   selection, resize handles, the I-beam in a field and a disabled control's
 *   refusal all keep working. What this draws is an *instrument that follows*,
 *   a step behind, never a replacement for the point itself.
 * * **Nothing sits on the point.** The ring trails on a spring and stretches
 *   along its own direction of travel by how fast it is moving, so a fast sweep
 *   reads as weight and a settled cursor reads as a still circle. A drag across
 *   the page is the only time it deforms, which is exactly when it has
 *   information to carry.
 * * **It does not exist until the pointer moves.** The stylesheet hides it and
 *   the move handler is the only thing that reveals it, so a page that has just
 *   loaded — or one reached with the keyboard — has nothing parked on it.
 * * **It is off on touch and under `prefers-reduced-motion`**, where a trailing
 *   ring is either invisible or the exact motion the reader asked to be spared.
 *
 * The loop is not always on: it starts on a pointer move or a state change and
 * stops itself as soon as the ring has caught up, so an idle page costs nothing
 * and a pointer that never moves costs nothing either.
 */

import { useEffect, useRef } from "react";

/** How hard the ring is pulled toward the pointer, and how much it drags. */
const PULL = 0.2;
const DRAG = 0.72;
/** Per-pixel-of-travel stretch, and the ceiling on it. */
const STRETCH = 0.055;
const STRETCH_MAX = 0.26;
/** Below this the ring is treated as settled. */
const REST = 0.12;

export function Cursor() {
  const shell = useRef<HTMLDivElement | null>(null);
  const ring = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    const fine = window.matchMedia("(pointer: fine)").matches;
    const quiet = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!fine || quiet) return;

    const shellNode = shell.current;
    const ringNode = ring.current;
    if (!shellNode || !ringNode) return;

    let targetX = window.innerWidth / 2;
    let targetY = window.innerHeight / 2;
    let x = targetX;
    let y = targetY;
    let vx = 0;
    let vy = 0;
    let scale = 1;
    let scaleGoal = 1;
    let hovering = false;
    let pressed = false;
    let shown = false;
    let running = false;
    // `live` and the frame handle exist so unmount actually stops the loop: a
    // pending `requestAnimationFrame` outlives the effect that scheduled it, and
    // a loop that only checks `running` restarts itself on its last frame.
    let live = true;
    let handle = 0;

    const paint = () => {
      // A ring stretched along its own velocity: the angle of travel is the
      // rotation, and only the *speed* sets how far it is pulled out of shape.
      const speed = Math.hypot(vx, vy);
      const stretch = Math.min(STRETCH_MAX, speed * STRETCH);
      const angle = speed > 0.02 ? (Math.atan2(vy, vx) * 180) / Math.PI : 0;
      ringNode.style.transform =
        `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0) ` +
        `rotate(${angle.toFixed(1)}deg) ` +
        `scale(${(scale * (1 + stretch)).toFixed(3)}, ${(scale * (1 - stretch * 0.6)).toFixed(3)})`;
    };

    const frame = () => {
      if (!live) return;
      const goal = scaleGoal * (pressed ? 0.82 : 1);
      vx = (vx + (targetX - x) * PULL) * DRAG;
      vy = (vy + (targetY - y) * PULL) * DRAG;
      x += vx;
      y += vy;
      scale += (goal - scale) * 0.18;
      paint();
      const settled =
        Math.abs(targetX - x) < REST &&
        Math.abs(targetY - y) < REST &&
        Math.abs(goal - scale) < 0.002;
      running = !settled;
      if (running) handle = window.requestAnimationFrame(frame);
    };

    const wake = () => {
      if (running || !live) return;
      running = true;
      handle = window.requestAnimationFrame(frame);
    };

    const onMove = (event: PointerEvent) => {
      // A pointer outside the window has no position *on the page*. Some
      // environments park the mouse far off-screen (an automation harness, a
      // pointer that walked off a multi-monitor edge), and trusting that
      // coordinate sends the reticle flying across the viewport from nine
      // thousand pixels away — seen while driving this very page. Treat it as
      // the leave it is, and re-enter at the point rather than sliding in.
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
        // Arrive where the pointer is rather than sliding in from wherever the
        // element happens to be anchored.
        shown = true;
        x = targetX;
        y = targetY;
        vx = vy = 0;
        shellNode.style.opacity = "1";
      }
      // `event.target` is not always an Element: a move that lands on the
      // document (the scrollbar, a drag past the last node) reports `document`
      // or `window`, and `window.closest` is not a function — which threw a
      // TypeError out of this handler on every such move, found by driving the
      // page and reading its console rather than by reading this file.
      const target = event.target;
      const next =
        target instanceof Element &&
        Boolean(
          target.closest("a, button, [role='switch'], summary, label, input, select, textarea"),
        );
      if (next !== hovering) {
        hovering = next;
        shellNode.classList.toggle("cursor--link", hovering);
      }
      scaleGoal = hovering ? 1.45 : 1;
      wake();
    };

    const onDown = () => {
      pressed = true;
      wake();
    };
    const onUp = () => {
      pressed = false;
      wake();
    };
    const onLeave = () => {
      shown = false;
      shellNode.style.opacity = "0";
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerdown", onDown, { passive: true });
    window.addEventListener("pointerup", onUp, { passive: true });
    window.addEventListener("blur", onLeave);
    document.addEventListener("pointerleave", onLeave);
    paint();

    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("blur", onLeave);
      document.removeEventListener("pointerleave", onLeave);
      live = false;
      running = false;
      window.cancelAnimationFrame(handle);
    };
  }, []);

  return (
    <div className="cursor" ref={shell} aria-hidden="true">
      <span className="cursor__ring" ref={ring} />
    </div>
  );
}
