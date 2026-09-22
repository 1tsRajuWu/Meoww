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
  const sparkleCluster = useRef<HTMLDivElement | null>(null);
  const swordEntity = useRef<HTMLDivElement | null>(null);
  const radarHud = useRef<HTMLDivElement | null>(null);
  const blobDrop = useRef<HTMLDivElement | null>(null);
  const vortexContainer = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const fine = window.matchMedia("(pointer: fine)").matches;
    const quiet = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!fine || quiet) return;

    const shellNode = shell.current;
    const ringNode = ring.current;
    const dotNode = dot.current;
    const glowNode = glow.current;
    const sparkleNode = sparkleCluster.current;
    const swordNode = swordEntity.current;
    const radarNode = radarHud.current;
    const blobNode = blobDrop.current;
    const vortexNode = vortexContainer.current;

    if (!shellNode || !ringNode || !dotNode || !glowNode || !sparkleNode || !swordNode || !radarNode || !blobNode || !vortexNode) return;

    const particleEls = sparkleNode.querySelectorAll<HTMLSpanElement>(".sparkle-particle");
    const sparkles = Array.from(particleEls).map((el, i) => ({
      el,
      x: -100,
      y: -100,
      lag: 0.14 + i * 0.04,
      angle: (i * Math.PI * 2) / 6,
      dist: 12 + (i % 3) * 8,
    }));

    let targetX = -100;
    let targetY = -100;
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

    // No `has-meow-cursor` on the document element. That flag exists so a sheet
    // can put `cursor: none` on the page while a drawn cursor is in charge, and
    // this console decided the opposite — the ring is a *companion* to the
    // pointer, not a replacement for it, so the real one stays (there is a test
    // that pins exactly that, and `styles.css` says so where the cursor's rules
    // live). Setting the flag here was a promise to a rule this bundle's sheet
    // has never had, and a class no selector answers is not a harmless one: it
    // reads as "the pointer is hidden somewhere else".
    const updateClasses = () => {
      if (activeStyle === "system") {
        shellNode.style.display = "none";
        glowNode.style.display = "none";
      } else {
        shellNode.style.display = "";
        glowNode.style.display = "";
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

      // 1. Sparkle swarm particles with orbital float
      for (const p of sparkles) {
        p.x += (targetX - p.x) * p.lag;
        p.y += (targetY - p.y) * p.lag;
        p.angle += 0.04;
        const fx = Math.cos(p.angle) * p.dist;
        const fy = Math.sin(p.angle) * p.dist;
        p.el.style.transform = `translate3d(${(p.x + fx).toFixed(2)}px, ${(p.y + fy).toFixed(2)}px, 0)`;
      }

      // 2. Cyber blade tip pinned directly to pointer
      swordNode.style.transform = `translate3d(${targetX.toFixed(2)}px, ${targetY.toFixed(2)}px, 0)`;

      // 3. Tactical Radar HUD
      radarNode.style.transform = `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0)`;

      // 4. Fluid velocity blob with dynamic squash & stretch
      const blobStretch = Math.min(0.65, speed * 0.05);
      blobNode.style.transform =
        `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0) ` +
        `rotate(${angle.toFixed(1)}deg) ` +
        `scale(${((1 + blobStretch) * scale).toFixed(3)}, ${((1 / (1 + blobStretch * 0.5)) * scale).toFixed(3)})`;

      // 5. Quantum Sacred Vortex
      vortexNode.style.transform = `translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, 0)`;

      // Trailing spring ring (fallback)
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
        <div className="sparkle-cluster" ref={sparkleCluster}>
          <span className="sparkle-particle" />
          <span className="sparkle-particle" />
          <span className="sparkle-particle" />
          <span className="sparkle-particle" />
          <span className="sparkle-particle" />
          <span className="sparkle-particle" />
        </div>
        <div className="sword-entity" ref={swordEntity}>
          <span className="sword-blade" />
          <span className="sword-hilt" />
        </div>
        <div className="radar-hud" ref={radarHud}>
          <span className="radar-sweep-beam" />
          <span className="radar-center-pip" />
          <span className="radar-bracket tl" />
          <span className="radar-bracket tr" />
          <span className="radar-bracket bl" />
          <span className="radar-bracket br" />
        </div>
        <div className="blob-drop" ref={blobDrop}>
          <span className="blob-shine" />
        </div>
        <div className="vortex-container" ref={vortexContainer}>
          <span className="vortex-inner-rune" />
          <span className="vortex-outer-ring">
            <span className="vortex-tachyon t1" />
            <span className="vortex-tachyon t2" />
            <span className="vortex-tachyon t3" />
          </span>
        </div>
        <span className="cursor__ring meow-cursor-ring" ref={ring} />
        <span className="meow-cursor-dot" ref={dot} />
      </div>
      <div className="meow-glow" ref={glow} aria-hidden="true" />
    </>
  );
}
