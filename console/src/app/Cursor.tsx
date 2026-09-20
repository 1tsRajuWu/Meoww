/** The custom cursor: one ring that lags behind the pointer on a spring.
 *
 * Three rules keep it from being a nuisance rather than a flourish, and all
 * three are the difference between a cursor someone chose and one they resent:
 *
 * * **It never replaces the pointer.** `cursor: none` is not used anywhere. The
 *   ring follows; the real arrow stays, so text selection, resize handles and
 *   the browser's own affordances all keep working.
 * * **It is off on touch and for `prefers-reduced-motion`.** A lagging ring on a
 *   device with no pointer is invisible at best, and for a reader who asked for
 *   less motion it is exactly the thing they asked to be spared.
 * * **It is one rAF loop reading a ref**, not a React state update per move —
 *   a component that re-renders sixty times a second to move a dot is how a
 *   flourish becomes a performance problem.
 */

import { useEffect, useRef } from "react";

const STIFFNESS = 0.18;
const DAMPING = 0.72;

export function Cursor() {
  const ring = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const fine = window.matchMedia("(pointer: fine)").matches;
    const quiet = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!fine || quiet) return;

    const node = ring.current;
    if (!node) return;

    let targetX = window.innerWidth / 2;
    let targetY = window.innerHeight / 2;
    let x = targetX;
    let y = targetY;
    let vx = 0;
    let vy = 0;
    let scale = 1;
    let targetScale = 1;
    let visible = false;
    let frame = 0;

    const onMove = (event: PointerEvent) => {
      targetX = event.clientX;
      targetY = event.clientY;
      if (!visible) {
        visible = true;
        node.style.opacity = "1";
      }
      const el = event.target as Element | null;
      // Grow over anything the operator can act on, so the ring reads as a
      // "this is clickable" hint rather than decoration.
      const interactive = el?.closest("a, button, [role='switch'], input, select, textarea");
      targetScale = interactive ? 1.9 : 1;
    };

    const onLeave = () => {
      visible = false;
      node.style.opacity = "0";
    };

    const tick = () => {
      // A spring toward the pointer: the lag is the whole effect, and it is what
      // makes the ring feel attached to a hand rather than to a coordinate.
      vx = (vx + (targetX - x) * STIFFNESS) * DAMPING;
      vy = (vy + (targetY - y) * STIFFNESS) * DAMPING;
      x += vx;
      y += vy;
      scale += (targetScale - scale) * 0.16;
      node.style.transform = `translate3d(${x - 18}px, ${y - 18}px, 0) scale(${scale.toFixed(3)})`;
      frame = window.requestAnimationFrame(tick);
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerdown", onMove, { passive: true });
    document.addEventListener("pointerleave", onLeave);
    frame = window.requestAnimationFrame(tick);

    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerdown", onMove);
      document.removeEventListener("pointerleave", onLeave);
      window.cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div className="cursor" ref={ring} aria-hidden="true">
      <span className="cursor__paw">🐾</span>
    </div>
  );
}
