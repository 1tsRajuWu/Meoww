/** The custom cursor: supports 6 geometric styles + click ripple + ambient glow.
 *
 * Designed to preserve precision:
 * - Native pointer is preserved when system style is active.
 * - Dot is pinned to pointer tip with 0ms lag.
 * - Ring trails on a responsive physical spring.
 * - Glow layer renders soft ambient light.
 * - Reduced motion shuts off custom layers cleanly.
 */

import { useEffect, useRef } from "react";
import { currentCursorStyle, type CursorStyleId } from "../lib/theme";

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
  const dot = useRef<HTMLSpanElement | null>(null);
  const glow = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const fine = window.matchMedia("(pointer: fine)").matches;
    const quiet = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!fine || quiet) return;

    const shellNode = shell.current;
    const ringNode = ring.current;
    const dotNode = dot.current;
    const glowNode = glow.current;
    if (!shellNode || !ringNode || !dotNode || !glowNode) return;

    let targetX = window.innerWidth / 2;
    let targetY = window.innerHeight / 2;
    let x = targetX;
    let y = targetY;
    let vx = 0;
    let vy = 0;
    let scale = 1;
    let scaleGoal = 1;
    let hovering = false;
    let textHovering = false;
    let pressed = false;
    let shown = false;
    let running = false;
    let live = true;
    let handle = 0;
    let activeStyle: CursorStyleId = currentCursorStyle();

    const updateClasses = () => {
      if (activeStyle === "system") {
        shellNode.style.display = "none";
        glowNode.style.display = "none";
        document.documentElement.classList.remove("has-meow-cursor");
      } else {
        shellNode.style.display = "";
        glowNode.style.display = "";
        document.documentElement.classList.add("has-meow-cursor");
      }
      shellNode.className = `cursor meow-cursor cursor-style-${activeStyle}${
        hovering ? " cursor--link is-link" : ""
      }${textHovering ? " is-text" : ""}${pressed ? " is-down" : ""}${
        shown ? " is-on" : ""
      }`;
    };

    updateClasses();

    const observer = new MutationObserver(() => {
      const nextStyle = currentCursorStyle();
      if (nextStyle !== activeStyle) {
        activeStyle = nextStyle;
        updateClasses();
        wake();
      }
    });
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-cursor-style"],
    });

    const paint = () => {
      const speed = Math.hypot(vx, vy);
      const stretch = Math.min(STRETCH_MAX, speed * STRETCH);
      const angle = speed > 0.02 ? (Math.atan2(vy, vx) * 180) / Math.PI : 0;

      // Pin dot directly to pointer coordinates
      dotNode.style.transform = `translate3d(${targetX.toFixed(2)}px, ${targetY.toFixed(2)}px, 0)`;

      // Trailing spring ring
      ringNode.style.transform =
        `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0) ` +
        `rotate(${angle.toFixed(1)}deg) ` +
        `scale(${(scale * (1 + stretch)).toFixed(3)}, ${(scale * (1 - stretch * 0.6)).toFixed(3)})`;

      // Ambient glow
      glowNode.style.transform = `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0)`;
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
        shown = true;
        x = targetX;
        y = targetY;
        vx = vy = 0;
        shellNode.style.opacity = "1";
        glowNode.classList.add("is-on");
        updateClasses();
      }
      const target = event.target;
      const isElement = target instanceof Element;
      const nextHover =
        isElement &&
        Boolean(
          target.closest(
            "a, button, [role='button'], [role='switch'], summary, label, select, .swatch, .btn"
          )
        );
      const nextText =
        isElement &&
        Boolean(target.closest("input, textarea, [contenteditable='true']"));

      if (nextHover !== hovering || nextText !== textHovering) {
        hovering = nextHover;
        textHovering = nextText;
        updateClasses();
      }
      scaleGoal = hovering ? 1.45 : textHovering ? 0.85 : 1;
      wake();
    };

    const onDown = (event: PointerEvent) => {
      pressed = true;
      updateClasses();

      if (activeStyle !== "system") {
        const ripple = document.createElement("span");
        ripple.className = "meow-click-fx";
        ripple.style.left = `${event.clientX}px`;
        ripple.style.top = `${event.clientY}px`;
        document.body.appendChild(ripple);
        window.setTimeout(() => ripple.remove(), 700);
      }

      wake();
    };

    const onUp = () => {
      pressed = false;
      updateClasses();
      wake();
    };

    const onLeave = () => {
      shown = false;
      shellNode.style.opacity = "0";
      glowNode.classList.remove("is-on");
      updateClasses();
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerdown", onDown, { passive: true });
    window.addEventListener("pointerup", onUp, { passive: true });
    window.addEventListener("blur", onLeave);
    document.addEventListener("pointerleave", onLeave);
    paint();

    return () => {
      observer.disconnect();
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
    <>
      <div className="cursor meow-cursor" ref={shell} aria-hidden="true">
        <span className="cursor__ring meow-cursor-ring" ref={ring} />
        <span className="meow-cursor-dot" ref={dot} />
      </div>
      <div className="meow-glow" ref={glow} aria-hidden="true" />
    </>
  );
}
