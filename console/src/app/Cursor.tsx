/** The custom cursor: supports 6 geometric styles + click ripple + ambient glow.
 *
 * Designed to preserve precision:
 * - Native pointer is preserved when system style is active.
 * - What a reader sees is always the chosen entity, and there is nothing under
 *   it. A dot pinned to the point and a ring trailing on a spring used to be
 *   drawn below the entity; both are gone. `styles.css` hid them in every style
 *   the picker offers (`cursor-style-sparkle` says "zero center dot, zero ring"
 *   and means it) and neither carried a size, so the pair was two 0x0 boxes
 *   positioned sixty times a second for nobody — and a second mark on the point
 *   is a second cursor, which is the complaint this layer exists to answer
 *   rather than to make. The spring they carried still runs: it is what the glow,
 *   the blob, the radar and the vortex trail on.
 * - Glow layer renders soft ambient light.
 * - Reduced motion shuts off custom layers cleanly.
 *
 * **Position goes in `translate`, shape goes in `transform`, and the split is
 * load-bearing.** This used to write `style.transform = translate3d(…)` while
 * `styles.css` animates `transform` for the twinkle, the bob and the slash —
 * and an animation outranks an inline declaration, so the animation did not
 * compose with the position, it *replaced* it. The swarm sat spinning in the
 * top-left corner of the viewport, the sword bobbed there, and every hover and
 * press rule the sheet writes about their `transform` was dead as well. Measured
 * on the served page: pointer at (600, 400), particles at (-9, -9). `translate`
 * and `transform` are separate properties that compose in that order, so the
 * script positions and the sheet shapes and neither can cancel the other.
 */

import { useEffect, useRef } from "react";
import { currentCursorStyle, type CursorStyleId } from "../lib/theme";

/** How hard the trailing layers are pulled toward the pointer, and how much
 *  they drag — the lead the glow, the blob, the radar and the vortex follow. */
const PULL = 0.2;
const DRAG = 0.72;
/** Below this a trailing layer is treated as settled. */
const REST = 0.12;

export function Cursor() {
  const shell = useRef<HTMLDivElement | null>(null);
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
    const glowNode = glow.current;
    const sparkleNode = sparkleCluster.current;
    const swordNode = swordEntity.current;
    const radarNode = radarHud.current;
    const blobNode = blobDrop.current;
    const vortexNode = vortexContainer.current;

    if (!shellNode || !glowNode || !sparkleNode || !swordNode || !radarNode || !blobNode || !vortexNode) return;

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
    // this console decided the opposite — the drawn layers are *companions* to
    // the pointer, not a replacement for it, so the real one stays (there is a test
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
      // One name per state. `cursor--link` used to be written beside `is-link`
      // as a second spelling of the same thing, and no selector anywhere
      // answered it — a class that reads as a rule somebody forgot, next to the
      // rule that was there.
      shellNode.className = `cursor meow-cursor cursor-style-${activeStyle}${
        hovering ? " is-link" : ""
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
      const angle = speed > 0.02 ? (Math.atan2(vy, vx) * 180) / Math.PI : 0;

      // 1. Sparkle swarm particles with orbital float
      for (const p of sparkles) {
        p.x += (targetX - p.x) * p.lag;
        p.y += (targetY - p.y) * p.lag;
        p.angle += 0.04;
        const fx = Math.cos(p.angle) * p.dist;
        const fy = Math.sin(p.angle) * p.dist;
        p.el.style.translate = `${(p.x + fx).toFixed(2)}px ${(p.y + fy).toFixed(2)}px`;
      }

      // 2. Cyber blade tip pinned directly to pointer
      swordNode.style.translate = `${targetX.toFixed(2)}px ${targetY.toFixed(2)}px`;

      // 3. Tactical Radar HUD
      radarNode.style.translate = `${x.toFixed(2)}px ${y.toFixed(2)}px`;

      // 4. Fluid velocity blob with dynamic squash & stretch
      const blobStretch = Math.min(0.65, speed * 0.05);
      blobNode.style.translate = `${x.toFixed(2)}px ${y.toFixed(2)}px`;
      blobNode.style.rotate = `${angle.toFixed(1)}deg`;
      blobNode.style.scale =
        `${((1 + blobStretch) * scale).toFixed(3)} ${((1 / (1 + blobStretch * 0.5)) * scale).toFixed(3)}`;

      // 5. Quantum Sacred Vortex
      vortexNode.style.translate = `${x.toFixed(2)}px ${y.toFixed(2)}px`;

      // Ambient glow
      glowNode.style.translate = `${x.toFixed(2)}px ${y.toFixed(2)}px`;
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
      // The swarm trails the pointer by design, so the loop is not finished
      // until the *particles* have caught up as well. Leaving them out was a
      // second way for the cursor to end up somewhere other than the pointer:
      // they converge with a lag of 0.14–0.34 per frame while the springs settle
      // in a handful, so a fast move stopped the loop with the swarm still
      // strung out behind it — and on the very first move it was strung out
      // from (-100, -100), which is the top-left corner of the viewport.
      const swarmSettled = sparkles.every(
        (p) => Math.abs(targetX - p.x) < REST && Math.abs(targetY - p.y) < REST
      );
      running = !settled || !swarmSettled;
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
        // The swarm starts where the pointer does, not where it was built.
        // `motion.js` has always done this; the console's copy of the loop did
        // not, so the first move released six particles from (-100, -100) and
        // they were still crossing the page when the ring had already settled.
        for (const p of sparkles) {
          p.x = targetX;
          p.y = targetY;
        }
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
      </div>
      <div className="meow-glow" ref={glow} aria-hidden="true" />
    </>
  );
}
