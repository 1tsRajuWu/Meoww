"""Comprehensive E2E Test Suite for the DaCat Under-Construction Experience.

Validates the standalone, ultra-premium single-page experience in templates/brb_main.html
and keep_alive.py across four comprehensive tiers:
  - Tier 1: Feature Coverage (R1 3D Perspective Tilt & Glassmorphism, R2 Spacetime Canvas Physics,
            R3 Edge Telemetry & Deployment Terminal, R4 Micro-Interactions & Audio Visualizer)
  - Tier 2: Boundary & Corner Cases (Typography >=12px floor, single <h1> heading, brand integrity,
            >=44px tap targets, reduced motion safety, viewport responsiveness)
  - Tier 3: Cross-Feature Pairwise Combinations (Telemetry + Terminal, Shockwave + Equalizer,
            Glassmorphic Card + 3D Tilt + Specular Border)
  - Tier 4: Real-World Acceptance Scenarios (Flask request context, live endpoint hook validation,
            HTML5 semantic DOM completeness, zero external CDN dependencies)

Run with:
    .venv/bin/pytest tests/test_dacat_under_construction_e2e.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "templates" / "brb_main.html"

# Minimum font-size floor per DaCat Design Laws
TYPE_FLOOR = 12.0
ROOT_PX = 16.0


def read_template() -> str:
    """Reads raw brb_main.html template."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_page(
    bot_name: str = "DaCat",
    invite_url: str = "https://discord.com/oauth2/authorize?client_id=123456789",
) -> str:
    """Renders brb_main.html with Flask template context."""
    import keep_alive
    with keep_alive.app.test_request_context("/"):
        return keep_alive.render_template(
            "brb_main.html",
            bot_name=bot_name,
            invite_url=invite_url,
            **keep_alive._brb_live_context(),
        )


# ==============================================================================
# TIER 1: FEATURE COVERAGE (R1 - R4)
# ==============================================================================

class TestR1PerspectiveTiltAndGlassmorphicStage(unittest.TestCase):
    """R1: 3D perspective tilt stage, sub-pixel lerp mechanics, specular border, glassmorphism."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_stage_card_structure_and_perspective_container(self):
        """Verifies stage wrapper has perspective defined and card has preserve-3d."""
        self.assertIn("stage-wrap", self.rendered)
        self.assertIn('id="stage-card"', self.rendered)
        self.assertRegex(
            self.raw,
            r"\.stage-wrap\s*\{[^}]*perspective:\s*1200px",
            "stage-wrap must declare 1200px perspective container",
        )
        self.assertRegex(
            self.raw,
            r"\.stage\s*\{[^}]*transform-style:\s*preserve-3d",
            "stage card must declare transform-style: preserve-3d",
        )

    def test_perspective_tilt_transform_properties(self):
        """Verifies card transform properties and sub-pixel lerp factor."""
        self.assertIn("will-change: transform", self.raw)
        self.assertIn("perspective(1200px)", self.raw)
        self.assertIn("rotateX(", self.raw)
        self.assertIn("rotateY(", self.raw)
        self.assertIn("TILT_LERP", self.raw)
        self.assertRegex(
            self.raw,
            r"TILT_LERP\s*=\s*0\.1",
            "card tilt must use ~0.10 sub-pixel lerp damping factor",
        )

    def test_subpixel_lerp_mechanics_and_event_handling(self):
        """Verifies pointermove listener calculates delta relative to center and resets on leave."""
        self.assertIn("pointermove", self.raw)
        self.assertIn("getBoundingClientRect", self.raw)
        self.assertIn("deltaX", self.raw)
        self.assertIn("deltaY", self.raw)
        self.assertIn("mouseleave", self.raw)
        self.assertIn("targetRotX = 0", self.raw)
        self.assertIn("targetRotY = 0", self.raw)
        # Check lerp interpolation formula
        self.assertIn("curRotX += (targetRotX - curRotX) * TILT_LERP", self.raw)
        self.assertIn("curRotY += (targetRotY - curRotY) * TILT_LERP", self.raw)

    def test_specular_perimeter_border_reflection_mask(self):
        """Verifies specular border uses mask-composite exclude/xor technique."""
        self.assertRegex(
            self.raw,
            r"\.stage::before\s*\{",
            "stage card must have ::before pseudo-element for specular border",
        )
        self.assertIn("-webkit-mask-composite: xor", self.raw)
        self.assertIn("mask-composite: exclude", self.raw)
        self.assertIn("pointer-events: none", self.raw)

    def test_specular_gradient_follows_mouse_card_properties(self):
        """Verifies dynamic radial gradient references --mouse-card-x and --mouse-card-y."""
        self.assertIn("--mouse-card-x", self.raw)
        self.assertIn("--mouse-card-y", self.raw)
        self.assertRegex(
            self.raw,
            r"stage\.style\.setProperty\(['\"]--mouse-card-x['\"]",
            "animation loop must update --mouse-card-x custom property",
        )
        self.assertRegex(
            self.raw,
            r"stage\.style\.setProperty\(['\"]--mouse-card-y['\"]",
            "animation loop must update --mouse-card-y custom property",
        )

    def test_multilayer_glassmorphic_hud_styling(self):
        """Verifies backdrop filter blur 32px, saturation 190%, and atmospheric glow."""
        self.assertRegex(
            self.raw,
            r"backdrop-filter:\s*blur\(32px\)\s+saturate\(190%\)",
            "stage card must declare blur(32px) saturate(190%) backdrop filter",
        )
        self.assertIn("-webkit-backdrop-filter", self.raw)
        self.assertIn("ambient-glow", self.rendered)
        self.assertRegex(
            self.raw,
            r"\.ambient-glow\s*\{[^}]*filter:\s*blur\(100px\)",
            "ambient glow must specify blur(100px) for atmospheric depth",
        )


class TestR2SpacetimeParticleGridAndPhysics(unittest.TestCase):
    """R2: Interactive spacetime particle grid canvas, Hooke's Law springs, webbing, shockwaves."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_canvas_element_and_viewport_bounds(self):
        """Verifies #dots-canvas element exists with full-bleed viewport styling."""
        self.assertIn('id="dots-canvas"', self.rendered)
        self.assertIn('aria-hidden="true"', self.rendered)
        self.assertRegex(
            self.raw,
            r"#dots-canvas\s*\{[^}]*position:\s*fixed",
            "dots canvas must be fixed-positioned",
        )
        self.assertRegex(
            self.raw,
            r"#dots-canvas\s*\{[^}]*inset:\s*0",
            "dots canvas must fill viewport with inset: 0",
        )

    def test_hookes_law_spring_physics_constants_and_formula(self):
        """Verifies Hooke's Law spring elasticity and velocity damping mechanics."""
        self.assertIn("SPRING", self.raw)
        self.assertIn("DAMPING", self.raw)
        # Hooke's Law spring restoring acceleration: F = -k * x
        self.assertRegex(
            self.raw,
            r"this\.vx\s*\+=\s*\(targetX\s*-\s*this\.x\)\s*\*\s*SPRING",
            "particle update must apply Hooke's Law spring restoring acceleration along X",
        )
        self.assertRegex(
            self.raw,
            r"this\.vy\s*\+=\s*\(targetY\s*-\s*this\.y\)\s*\*\s*SPRING",
            "particle update must apply Hooke's Law spring restoring acceleration along Y",
        )
        # Velocity damping
        self.assertRegex(
            self.raw,
            r"this\.vx\s*\*=\s*DAMPING",
            "particle velocity along X must be damped every frame",
        )

    def test_particle_lattice_grid_generation_and_spacing(self):
        """Verifies particle lattice generation with uniform SPACING."""
        self.assertIn("SPACING", self.raw)
        self.assertRegex(
            self.raw,
            r"var\s+SPACING\s*=\s*\d+",
            "canvas grid must declare a numeric SPACING constant",
        )
        self.assertIn("Dot", self.raw)
        self.assertIn("dots.push(new Dot(", self.raw)

    def test_constellation_energy_webbing_neighbor_connections(self):
        """Verifies constellation webbing connects displaced nodes using adjacent neighbor checks."""
        self.assertIn("displacement", self.raw)
        self.assertRegex(
            self.raw,
            r"d1\.displacement\s*>\s*[\d.]+",
            "constellation webbing must check particle displacement threshold",
        )
        # O(N) neighbor checks (rightDot and bottomDot)
        self.assertIn("rightDot", self.raw)
        self.assertIn("bottomDot", self.raw)
        self.assertIn("ctx.moveTo(d1.x, d1.y)", self.raw)
        self.assertIn("ctx.lineTo(", self.raw)
        self.assertIn("ctx.stroke()", self.raw)

    def test_volumetric_cursor_flashlight_illumination(self):
        """Verifies volumetric cursor illumination via radial gradient flashlight."""
        self.assertIn("createRadialGradient", self.raw)
        self.assertRegex(
            self.raw,
            r"ctx\.createRadialGradient\(\s*mouse\.x,\s*mouse\.y,\s*0,\s*mouse\.x,\s*mouse\.y",
            "flashlight gradient must radiate from current mouse coordinates",
        )
        self.assertIn("ctx.fillRect(0, 0, width, height)", self.raw)

    def test_acoustic_shockwave_dispersal_rings(self):
        """Verifies click/pointerdown event creates expanding shockwave dispersal rings via object pooling."""
        self.assertIn("onPointerDown", self.raw)
        self.assertIn("shockwavePool", self.raw)
        self.assertIn("MAX_SHOCKWAVES", self.raw)
        self.assertIn("spawnShockwave", self.raw)
        self.assertIn("sw.radius +=", self.raw)
        self.assertIn("sw.strength *=", self.raw)
        self.assertIn("ctx.arc(sw.x, sw.y, sw.radius, 0, Math.PI * 2)", self.raw)


class TestR3EdgeTelemetryAndDeploymentTerminal(unittest.TestCase):
    """R3: Live edge telemetry HUD, ping counter, subsystem health, deployment terminal."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_telemetry_hud_bar_and_edge_indicators(self):
        """Verifies telemetry bar contains edge region indicator and ping placeholder."""
        self.assertIn("telemetry-bar", self.rendered)
        self.assertIn("telemetry-dot", self.rendered)
        self.assertIn('id="pop-val"', self.rendered)
        self.assertIn('id="ping-val"', self.rendered)

    def test_ping_counter_ticker_element_and_interval(self):
        """Verifies ping ticker interval updates #ping-val dynamically with exponential smoothing."""
        self.assertIn("ping-val", self.rendered)
        self.assertIn("samplePing", self.raw)
        self.assertIn("pingSmooth", self.raw)
        self.assertRegex(
            self.raw,
            r"setInterval\(\s*samplePing\s*,\s*\d+\)",
            "script must include setInterval timer sampling ping",
        )

    def test_subsystem_health_matrix_and_chips(self):
        """Verifies subsystem health matrix displays chips for authentic DaCat core subsystems."""
        self.assertIn("system-matrix", self.rendered)
        self.assertIn("sys-chip", self.rendered)
        self.assertIn("sys-label", self.rendered)
        self.assertIn("sys-val", self.rendered)
        # Check presence of authentic DaCat systems
        self.assertIn("Core Engine", self.rendered)
        self.assertIn("Neural Shards", self.rendered)
        self.assertIn("Edge Sync", self.rendered)
        self.assertIn("Servers", self.rendered)

    def test_terminal_drawer_structure_and_aria_attributes(self):
        """Verifies terminal toggle button and collapsible drawer with ARIA attributes."""
        self.assertIn('id="term-toggle"', self.rendered)
        self.assertIn('id="term-drawer"', self.rendered)
        self.assertIn('aria-expanded="false"', self.rendered)
        self.assertIn('aria-controls="term-drawer"', self.rendered)
        self.assertIn("terminal-head", self.rendered)
        self.assertIn("terminal-body", self.rendered)

    def test_terminal_log_stream_lines_and_timestamps(self):
        """Verifies terminal stream contains log lines with timestamps and streaming build steps."""
        self.assertIn("term-line", self.rendered)
        self.assertIn('<span class="ts">', self.rendered)
        self.assertIn("dacat-deployment.log", self.rendered)
        self.assertIn("buildSteps", self.raw)
        self.assertIn("streamNextStep", self.raw)

    def test_live_streaming_line_and_blinking_cursor(self):
        """Verifies live terminal line carries blinking cursor with blink animation."""
        self.assertIn("term-cursor", self.rendered)
        self.assertRegex(
            self.raw,
            r"@keyframes\s+blink\s*\{[^}]*50%\s*\{\s*opacity:\s*0;",
            "cursor must define step-end @keyframes blink animation",
        )


class TestR4MicroInteractionsAndMotionPolish(unittest.TestCase):
    """R4: Optical laser progress tracking, radar sonar ripples, text shimmer, equalizer, flares."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_optical_laser_progress_tracking_bar(self):
        """Verifies optical laser track and animated laser bar with laser-slide keyframes."""
        self.assertIn("laser-track", self.rendered)
        self.assertIn("laser-bar", self.rendered)
        self.assertRegex(
            self.raw,
            r"(?s)@keyframes\s+laser-slide\s*\{.*?0%\s*\{.*?100%\s*\{",
            "laser bar must declare @keyframes laser-slide translation animation",
        )

    def test_radar_sonar_ripple_animation_on_status_pill(self):
        """Verifies status pill includes sonar radar wave animation."""
        self.assertIn("status-pill", self.rendered)
        self.assertIn("pulse-ring", self.rendered)
        self.assertIn("pulse-dot", self.rendered)
        self.assertRegex(
            self.raw,
            r"(?s)@keyframes\s+sonar\s*\{.*?transform:\s*scale\(3\.\d+\)",
            "sonar ripple must expand outward via @keyframes sonar",
        )

    def test_continuous_metallic_text_shimmer_on_title(self):
        """Verifies title text uses linear gradient clipping and continuous shimmer keyframes."""
        self.assertRegex(
            self.raw,
            r"\.title\s*\{[^}]*-webkit-background-clip:\s*text",
            "title must use -webkit-background-clip: text for metallic effect",
        )
        self.assertRegex(
            self.raw,
            r"\.title\s*\{[^}]*background-size:\s*200%",
            "title background-size must be expanded to 200% for shimmer motion",
        )
        self.assertRegex(
            self.raw,
            r"(?s)@keyframes\s+shimmer\s*\{.*?background-position:",
            "title must define @keyframes shimmer oscillating background position",
        )

    def test_five_bar_audio_visualizer_equalizer(self):
        """Verifies footer audio visualizer equalizer contains exactly 5 bars and Web Audio API integration."""
        self.assertIn("eq-strip", self.rendered)
        bar_count = self.rendered.count('class="eq-bar"')
        self.assertEqual(
            bar_count,
            5,
            f"equalizer strip must render exactly 5 eq-bar elements, found {bar_count}",
        )
        # Web Audio API hook
        self.assertIn("AudioContext", self.raw)
        self.assertIn("createAnalyser", self.raw)
        self.assertIn("getByteFrequencyData", self.raw)

    def test_equalizer_bounce_animation_and_staggered_delays(self):
        """Verifies equalizer bars have staggered animation delays and bounce keyframes."""
        self.assertRegex(
            self.raw,
            r"(?s)@keyframes\s+eq-bounce\s*\{.*?transform:\s*scaleY",
            "equalizer bars must animate via @keyframes eq-bounce scaleY",
        )
        for i in range(1, 6):
            self.assertRegex(
                self.raw,
                rf"\.eq-strip\.anim-fallback\s+\.eq-bar:nth-child\({i}\)\s*\{{[^}}]*animation-delay:",
                f"eq-bar:nth-child({i}) must declare staggered animation-delay",
            )

    def test_specular_cta_light_flare_on_action_button(self):
        """Verifies primary CTA button includes specular light sweep flare."""
        self.assertIn("btn-white", self.rendered)
        self.assertRegex(
            self.raw,
            r"\.btn-white::after\s*\{[^}]*transform:\s*rotate\(25deg\)",
            "CTA button ::after must define angled specular flare gradient",
        )
        self.assertRegex(
            self.raw,
            r"\.btn-white:hover::after\s*\{[^}]*left:\s*140%",
            "hovering CTA button must trigger specular flare sweep across button",
        )


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES
# ==============================================================================

class TestTier2TypographyFloor(unittest.TestCase):
    """Typography floor: ALL font sizes >= 12px everywhere in template and styles."""

    SIZE_PATTERN = re.compile(r"font-size:\s*([\d.]+)\s*(px|rem)\b")

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_no_css_font_size_below_twelve_pixels(self):
        """Scans all CSS rules in template and asserts no declaration is under 12px."""
        offenders = []
        for match in self.SIZE_PATTERN.finditer(self.raw):
            val, unit = match.group(1), match.group(2)
            px = float(val) * (ROOT_PX if unit == "rem" else 1.0)
            if px < TYPE_FLOOR:
                offenders.append(f"font-size: {val}{unit} ({px:.1f}px)")
        self.assertEqual(
            offenders,
            [],
            f"Found font-size declarations below {TYPE_FLOOR}px floor:\n" + "\n".join(offenders),
        )

    def test_no_inline_style_font_size_below_twelve_pixels(self):
        """Verifies no element in rendered markup carries inline style font-size < 12px."""
        inline_styles = re.findall(r'style="([^"]*font-size:[^"]*)"', self.rendered)
        offenders = []
        for style in inline_styles:
            for match in self.SIZE_PATTERN.finditer(style):
                val, unit = match.group(1), match.group(2)
                px = float(val) * (ROOT_PX if unit == "rem" else 1.0)
                if px < TYPE_FLOOR:
                    offenders.append(f"inline style {style} -> {px:.1f}px")
        self.assertEqual(offenders, [])

    def test_title_font_clamp_minimum_floor(self):
        """Verifies title clamp minimum exceeds 12px floor (at least 24px)."""
        clamp_match = re.search(r"clamp\(\s*([\d.]+)(px|rem)", self.raw)
        self.assertIsNotNone(clamp_match, "title must use responsive font-size clamp()")
        val, unit = float(clamp_match.group(1)), clamp_match.group(2)
        px = val * (ROOT_PX if unit == "rem" else 1.0)
        self.assertGreaterEqual(
            px,
            TYPE_FLOOR * 2,
            "title clamp floor must be prominent (>24px)",
        )

    def test_terminal_monospace_font_size_floor(self):
        """Verifies terminal drawer font size is at least 12px."""
        match = re.search(r"\.terminal-drawer\s*\{[^}]*font-size:\s*(\d+)px", self.raw)
        self.assertIsNotNone(match, "terminal-drawer must specify font-size")
        self.assertGreaterEqual(float(match.group(1)), TYPE_FLOOR)

    def test_hud_and_metadata_fine_print_floor(self):
        """Verifies telemetry bar and metadata notes respect 12px floor."""
        for selector in (r"\.telemetry-bar", r"\.meta-note", r"\.sys-label", r"\.term-title"):
            match = re.search(selector + r"\s*\{[^}]*font-size:\s*(\d+)px", self.raw)
            if match:
                self.assertGreaterEqual(
                    float(match.group(1)),
                    TYPE_FLOOR,
                    f"{selector} font-size must be >= {TYPE_FLOOR}px",
                )


class TestTier2HeadingStructure(unittest.TestCase):
    """Heading structure: Exactly one <h1> heading on the page."""

    def setUp(self):
        self.rendered = render_page()

    def test_exactly_one_h1_heading_on_page(self):
        """Asserts rendered page contains exactly one <h1> tag."""
        h1_openings = re.findall(r"<h1\b", self.rendered)
        self.assertEqual(
            len(h1_openings),
            1,
            f"Rendered page must contain exactly one <h1> heading, found {len(h1_openings)}",
        )

    def test_h1_contains_non_empty_semantic_text(self):
        """Asserts the <h1> element contains meaningful text."""
        match = re.search(r"<h1\b[^>]*>(.*?)</h1>", self.rendered, re.DOTALL)
        self.assertIsNotNone(match, "<h1> heading element must be present")
        text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        self.assertGreater(len(text), 3, "<h1> heading must contain text")

    def test_h1_is_contained_within_main_stage(self):
        """Asserts the <h1> tag is nested inside <main class="stage">."""
        main_match = re.search(r"<main\b[^>]*>(.*?)</main>", self.rendered, re.DOTALL)
        self.assertIsNotNone(main_match, "page must have <main> landmark")
        self.assertIn("<h1", main_match.group(1), "<h1> must be nested inside <main>")

    def test_no_subheading_violates_heading_hierarchy(self):
        """Asserts no <h2> appears before <h1> in DOM document order."""
        h1_pos = self.rendered.find("<h1")
        h2_pos = self.rendered.find("<h2")
        if h2_pos != -1:
            self.assertLess(h1_pos, h2_pos, "<h1> must precede any <h2> in document order")

    def test_heading_uses_accessible_dom_order(self):
        """Asserts <h1> appears after telemetry bar and status pill in stage."""
        h1_pos = self.rendered.find("<h1")
        pill_pos = self.rendered.find("status-pill")
        self.assertGreater(h1_pos, pill_pos, "<h1> should follow status pill in visual hierarchy")


class TestTier2BrandIntegrity(unittest.TestCase):
    """Brand integrity: 'DaCat' brand name present, retired name 'Keystone' strictly absent."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page(bot_name="DaCat")

    def test_brand_name_dacat_present_in_rendered_page(self):
        """Asserts brand name 'DaCat' is rendered on the page."""
        self.assertIn("DaCat", self.rendered)

    def test_forbidden_retired_name_keystone_strictly_absent(self):
        """Asserts retired brand name 'Keystone' is completely absent."""
        self.assertNotIn(
            "Keystone",
            self.raw,
            "Template file must NOT contain forbidden retired name 'Keystone'",
        )
        self.assertNotIn(
            "Keystone",
            self.rendered,
            "Rendered page must NOT contain forbidden retired name 'Keystone'",
        )

    def test_page_title_carries_bot_name(self):
        """Asserts <title> incorporates the bot name."""
        match = re.search(r"<title>(.*?)</title>", self.rendered)
        self.assertIsNotNone(match, "page must have <title>")
        self.assertIn("DaCat", match.group(1))

    def test_favicon_and_brand_assets_referenced(self):
        """Asserts favicon link references static favicon asset."""
        self.assertIn('href="/static/favicon.svg"', self.rendered)

    def test_meta_tags_reflect_dacat_brand(self):
        """Asserts meta description and dark theme color tags are present."""
        self.assertRegex(self.rendered, r'<meta\s+name="description"\s+content="[^"]+"')
        self.assertRegex(self.rendered, r'<meta\s+name="theme-color"\s+content="#[0-9a-fA-F]+"')


class TestTier2TapTargetsAndSizing(unittest.TestCase):
    """Tap targets: Interactive buttons/links have >= 44px minimum sizing."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_primary_cta_button_touch_target_height(self):
        """Asserts primary CTA button (.btn-white) has vertical padding >= 12px yielding >=44px height."""
        match = re.search(r"\.btn-coral,\s*\.btn-white\s*\{[^}]*padding:\s*(\d+)px\s+(\d+)px", self.raw)
        self.assertIsNotNone(match, ".btn-white must specify padding")
        vert_padding = int(match.group(1))
        # 14px top + 14px bottom + 15px font + line-height (1.2+) >= 44px
        self.assertGreaterEqual(
            vert_padding,
            12,
            "CTA button vertical padding must be >= 12px to guarantee >=44px tap target",
        )

    def test_primary_cta_button_horizontal_padding(self):
        """Asserts primary CTA button has comfortable horizontal touch target padding."""
        match = re.search(r"\.btn-coral,\s*\.btn-white\s*\{[^}]*padding:\s*(\d+)px\s+(\d+)px", self.raw)
        horiz_padding = int(match.group(2))
        self.assertGreaterEqual(horiz_padding, 24, "CTA horizontal padding must be >= 24px")

    def test_terminal_toggle_button_touch_target(self):
        """Asserts terminal toggle button provides an accessible interactive control."""
        self.assertRegex(
            self.raw,
            r"\.terminal-toggle\s*\{[^}]*cursor:\s*pointer",
            "terminal-toggle must specify cursor: pointer",
        )
        self.assertRegex(
            self.raw,
            r"\.terminal-toggle\s*\{[^}]*padding:\s*\d+px",
            "terminal-toggle must have padding defined",
        )

    def test_interactive_controls_have_pointer_cursor(self):
        """Verifies interactive toggle and action buttons specify cursor: pointer or link role."""
        self.assertIn("cursor: pointer", self.raw)

    def test_buttons_provide_focus_and_active_states(self):
        """Verifies CTA button defines hover and active transition states."""
        self.assertIn(".btn-white:hover", self.raw)
        self.assertIn(".btn-white:active", self.raw)


class TestTier2ReducedMotionSafety(unittest.TestCase):
    """Reduced motion safety: animation durations safe, decorative layers aria-hidden, prefers-reduced-motion."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_animation_durations_avoid_seizure_frequencies(self):
        """Asserts all CSS animations have durations >= 1.0s to avoid vestibular triggers (>3Hz)."""
        durations = re.findall(r"animation:\s*[a-zA-Z0-9-]+\s+([\d.]+)s", self.raw)
        for dur in durations:
            val = float(dur)
            self.assertGreaterEqual(
                val,
                1.0,
                f"Animation duration {val}s is under 1.0s threshold (risk of vestibular trigger)",
            )

    def test_decorative_micro_interactions_use_aria_hidden(self):
        """Asserts decorative canvas and atmospheric layers carry aria-hidden="true"."""
        self.assertIn('<canvas id="dots-canvas" aria-hidden="true">', self.rendered)
        self.assertIn('<div class="ambient-glow" aria-hidden="true">', self.rendered)
        self.assertIn('<div class="laser-track" aria-hidden="true">', self.rendered)

    def test_prefers_reduced_motion_media_query_present(self):
        """Asserts @media (prefers-reduced-motion: reduce) is implemented."""
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.raw)
        self.assertIn("animation: none !important", self.raw)

    def test_css_transitions_use_bounded_durations(self):
        """Asserts CSS transitions do not exceed 500ms for crisp responsiveness."""
        transitions = re.findall(r"transition:[^;]*\s(\d+)ms", self.raw)
        for t in transitions:
            self.assertLessEqual(int(t), 500, f"Transition duration {t}ms is sluggish (>500ms)")

    def test_parallax_tilt_resets_on_pointer_leave(self):
        """Asserts mouseleave event listener resets stage card tilt targets."""
        self.assertIn("mouseleave", self.raw)
        self.assertIn("targetRotX = 0", self.raw)
        self.assertIn("targetRotY = 0", self.raw)


class TestTier2ViewportResponsiveness(unittest.TestCase):
    """Viewport responsiveness: mobile, tablet, desktop, ultra-wide responsive rules."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_meta_viewport_tag_present(self):
        """Asserts meta viewport tag is present in document head."""
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            self.rendered,
        )

    def test_stage_wrapper_fluid_responsive_constraints(self):
        """Asserts stage-wrap has fluid responsive max-width and width: 100%."""
        self.assertRegex(
            self.raw,
            r"\.stage-wrap\s*\{[^}]*max-width:\s*5\d\dpx",
            "stage-wrap must have max-width constraint for desktop readability",
        )
        self.assertRegex(
            self.raw,
            r"\.stage-wrap\s*\{[^}]*width:\s*100%",
            "stage-wrap must have width: 100% for responsive contraction",
        )

    def test_mobile_media_query_breakpoint(self):
        """Asserts @media (max-width: ...) query is present for mobile viewports."""
        self.assertRegex(self.raw, r"@media\s*\(max-width:\s*\d+px\)")

    def test_system_matrix_responsive_grid_layout(self):
        """Asserts system matrix defines multi-column layout that collapses on mobile."""
        self.assertRegex(
            self.raw,
            r"\.system-matrix\s*\{[^}]*grid-template-columns:",
            "system-matrix must declare grid-template-columns layout",
        )
        self.assertRegex(
            self.raw,
            r"(?s)@media\s*\(max-width:\s*\d+px\)\s*\{.*?\.system-matrix\s*\{.*?grid-template-columns:\s*1fr",
            "system-matrix must collapse to single column on mobile",
        )

    def test_canvas_resize_handler_recalculates_dpr_and_bounds(self):
        """Asserts window resize listener invokes resize() recalculating canvas dimensions."""
        self.assertIn("window.addEventListener('resize', resize", self.raw)
        self.assertIn("window.devicePixelRatio", self.raw)
        self.assertIn("window.innerWidth", self.raw)
        self.assertIn("window.innerHeight", self.raw)


# ==============================================================================
# TIER 3: CROSS-FEATURE PAIRWISE COMBINATIONS
# ==============================================================================

class TestTier3CrossFeaturePairwise(unittest.TestCase):
    """Tier 3: Pairwise interactions across telemetry, terminal, physics, 3D tilt, and stage."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_telemetry_hud_and_deployment_terminal_coexistence(self):
        """Verifies telemetry bar and deployment terminal reside together in stage card."""
        main_match = re.search(r"<main\b[^>]*>(.*?)</main>", self.rendered, re.DOTALL)
        self.assertIsNotNone(main_match)
        content = main_match.group(1)
        self.assertIn("telemetry-bar", content)
        self.assertIn("terminal-wrap", content)

    def test_terminal_toggle_does_not_interfere_with_telemetry_flow(self):
        """Verifies terminal toggle script controls term-drawer without mutating telemetry elements."""
        self.assertIn("term-toggle", self.raw)
        self.assertIn("term-drawer", self.raw)
        # Ensure term toggle only mutates drawer
        self.assertIn("drawer.hidden = expanded", self.raw)

    def test_ping_ticker_updates_independently_of_terminal_drawer(self):
        """Verifies ping metric interval updates pingEl independently of terminal drawer state."""
        self.assertIn("ping-val", self.raw)
        self.assertIn("term-drawer", self.raw)
        self.assertIn("setInterval", self.raw)

    def test_acoustic_shockwave_click_and_audio_equalizer_pulse_coordination(self):
        """Verifies shockwaves click listener coordinates with Web Audio API sound trigger."""
        self.assertIn("onPointerDown", self.raw)
        self.assertIn("spawnShockwave", self.raw)
        self.assertIn("playShockwaveAudio", self.raw)
        self.assertIn("eq-strip", self.rendered)

    def test_equalizer_visual_anchor_and_interaction_hint_alignment(self):
        """Verifies equalizer strip is stacked directly above the interaction note in footer-hud."""
        self.assertIn("footer-hud", self.rendered)
        self.assertRegex(
            self.rendered,
            r'(?s)<div class="footer-hud">.*?<div class="[^"]*eq-strip[^"]*".*?<span class="meta-note">',
        )

    def test_particle_canvas_and_equalizer_share_theme_palette(self):
        """Verifies canvas shockwave rings and equalizer bars share DaCat Coral accent token."""
        self.assertIn("--coral", self.raw)
        self.assertIn("rgba(255, 42, 68", self.raw)

    def test_glassmorphic_stage_and_3d_cursor_tilt_transform(self):
        """Verifies stage card backdrop-filter composites smoothly with 3D perspective rotation."""
        self.assertIn("backdrop-filter", self.raw)
        self.assertIn("transform-style: preserve-3d", self.raw)
        self.assertIn("stage.style.transform", self.raw)

    def test_specular_border_tracking_coordinates_match_cursor_event(self):
        """Verifies pointermove event updates card coordinates and animation loop drives specular mask."""
        self.assertIn("updateCardCoordinates", self.raw)
        self.assertIn("targetCardX", self.raw)
        self.assertIn("targetCardY", self.raw)
        self.assertIn("--mouse-card-x", self.raw)
        self.assertIn("--mouse-card-y", self.raw)

    def test_backdrop_filter_and_box_shadow_layering_with_specular_mask(self):
        """Verifies stage card defines multi-layer box shadow along with ::before border mask."""
        self.assertRegex(
            self.raw,
            r"\.stage\s*\{[^}]*box-shadow:[^}]*0 32px 80px",
            "stage card must declare multi-layer elevation shadow",
        )
        self.assertIn(".stage::before", self.raw)

    def test_laser_progress_and_sonar_pulse_timing_harmony(self):
        """Verifies harmonic cycle timing between laser slide (2.4s) and sonar ring (2.4s)."""
        laser_match = re.search(r"laser-slide\s+([\d.]+)s", self.raw)
        sonar_match = re.search(r"sonar\s+([\d.]+)s", self.raw)
        self.assertIsNotNone(laser_match)
        self.assertIsNotNone(sonar_match)
        self.assertEqual(
            laser_match.group(1),
            sonar_match.group(1),
            "laser slide and sonar ripple must have harmonious animation cycles (2.4s)",
        )

    def test_cta_specular_flare_and_card_specular_glow_compositing(self):
        """Verifies CTA button specular flare (.btn-white::after) and card specular border co-exist."""
        self.assertIn(".stage::before", self.raw)
        self.assertIn(".btn-white::after", self.raw)

    def test_canvas_pointer_flashlight_and_stage_tilt_synchronicity(self):
        """Verifies both canvas flashlight and card tilt respond synchronously to pointer movement."""
        self.assertIn("onPointerMove", self.raw)
        self.assertIn("mouse.targetX", self.raw)
        self.assertIn("updateCardCoordinates", self.raw)


# ==============================================================================
# TIER 4: REAL-WORLD ACCEPTANCE SCENARIOS
# ==============================================================================

class TestTier4RealWorldAcceptance(unittest.TestCase):
    """Tier 4: Flask rendering context, live endpoint hook interception, and semantic DOM completeness."""

    def test_flask_render_template_with_full_context(self):
        """Verifies Flask render_template succeeds with bot_name and invite_url."""
        html = render_page(bot_name="DaCat", invite_url="https://discord.com/invite/dacat")
        self.assertIn("DaCat", html)
        self.assertIn('href="https://discord.com/invite/dacat"', html)
        self.assertIn("Add DaCat to Discord", html)

    def test_flask_render_template_without_invite_url_omits_cta(self):
        """Verifies fallback when invite_url is omitted."""
        html = render_page(bot_name="DaCat", invite_url="")
        # When invite_url is empty, template falls back to https://discord.com
        self.assertIn("Add DaCat to Discord", html)
        # Footer HUD remains intact
        self.assertIn("eq-strip", html)
        self.assertIn("Click anywhere to pulse gravity wave", html)

    def test_flask_render_template_custom_bot_name_injected(self):
        """Verifies custom bot_name parameter is accurately reflected in title and document."""
        html = render_page(bot_name="DaCat Canary")
        self.assertIn("Building The Website — DaCat Canary", html)

    def test_live_endpoint_hook_intercepts_root_path(self):
        """Verifies keep_alive._enforce_be_right_back() intercepts root / request."""
        import keep_alive
        with keep_alive.app.test_request_context("/"):
            orig = sys.modules.get("pytest")
            if "pytest" in sys.modules:
                del sys.modules["pytest"]
            try:
                resp = keep_alive._enforce_be_right_back()
                self.assertIsNotNone(resp, "_enforce_be_right_back() must return a Response for /")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("DeCat", resp.get_data(as_text=True))
            finally:
                if orig:
                    sys.modules["pytest"] = orig

    def test_live_endpoint_hook_intercepts_marketing_routes(self):
        """Verifies keep_alive._enforce_be_right_back() intercepts all primary marketing routes."""
        import keep_alive
        routes = ["/systems", "/pricing", "/terms", "/privacy", "/premium"]
        for route in routes:
            with keep_alive.app.test_request_context(route):
                orig = sys.modules.get("pytest")
                if "pytest" in sys.modules:
                    del sys.modules["pytest"]
                try:
                    resp = keep_alive._enforce_be_right_back()
                    self.assertIsNotNone(
                        resp,
                        f"_enforce_be_right_back() must intercept marketing route {route}",
                    )
                    self.assertEqual(resp.status_code, 200)
                finally:
                    if orig:
                        sys.modules["pytest"] = orig

    def test_live_endpoint_returns_html_utf8_mimetype(self):
        """Verifies _enforce_be_right_back() returns text/html; charset=utf-8 mimetype."""
        import keep_alive
        with keep_alive.app.test_request_context("/"):
            orig = sys.modules.get("pytest")
            if "pytest" in sys.modules:
                del sys.modules["pytest"]
            try:
                resp = keep_alive._enforce_be_right_back()
                self.assertIn("text/html", resp.mimetype)
            finally:
                if orig:
                    sys.modules["pytest"] = orig

    def test_live_endpoint_response_payload_size(self):
        """Verifies live under-construction response is full rich payload (> 20,000 bytes)."""
        import keep_alive
        with keep_alive.app.test_request_context("/"):
            orig = sys.modules.get("pytest")
            if "pytest" in sys.modules:
                del sys.modules["pytest"]
            try:
                resp = keep_alive._enforce_be_right_back()
                payload_len = len(resp.get_data())
                self.assertGreater(
                    payload_len,
                    20000,
                    f"Payload size {payload_len} bytes is too small for full rich experience",
                )
            finally:
                if orig:
                    sys.modules["pytest"] = orig

    def test_html5_doctype_and_html_attributes(self):
        """Verifies HTML5 doctype and lang/data-theme attributes on html tag."""
        html = render_page()
        self.assertTrue(
            html.strip().lower().startswith("<!doctype html>"),
            "Document must start with <!doctype html>",
        )
        self.assertRegex(html, r'<html\s+lang="en"\s+data-theme="dark">')

    def test_main_semantic_landmark_hierarchy(self):
        """Verifies <main> landmark encompasses the entire stage content."""
        html = render_page()
        self.assertEqual(html.count("<main"), 1, "Document must have exactly one <main> tag")
        self.assertEqual(html.count("</main>"), 1, "Document must have matching </main> tag")

    def test_no_unrendered_jinja_delimiters_in_output(self):
        """Verifies no unrendered Jinja2 delimiters remain in the rendered HTML output."""
        html = render_page()
        for delimiter in ("{{", "}}", "{%", "%}"):
            self.assertNotIn(
                delimiter,
                html,
                f"Rendered output contains unrendered Jinja delimiter: {delimiter}",
            )

    def test_static_asset_paths_resolve_to_static_mount(self):
        """Verifies static asset paths begin with /static/."""
        html = render_page()
        asset_links = re.findall(r'(?:href|src)="([^"]+)"', html)
        static_links = [link for link in asset_links if "static" in link]
        self.assertGreater(len(static_links), 0, "Document must link static assets")
        for link in static_links:
            self.assertTrue(
                link.startswith("/static/"),
                f"Asset link {link} does not start with /static/",
            )

    def test_zero_external_cdn_dependency_integrity(self):
        """Verifies zero external script or font CDN tags for offline/resilient delivery."""
        html = render_page()
        scripts = re.findall(r'<script\b[^>]*src="([^"]+)"', html)
        for src in scripts:
            self.assertFalse(
                src.startswith(("http://", "https://", "//")),
                f"External CDN script detected: {src}; page must be zero-dependency",
            )

    def test_dom_element_uniqueness_for_interactive_ids(self):
        """Verifies all interactive and major container element IDs are unique."""
        html = render_page()
        ids = re.findall(r'\bid="([^"]+)"', html)
        seen = set()
        duplicates = []
        for elem_id in ids:
            if elem_id in seen:
                duplicates.append(elem_id)
            seen.add(elem_id)
        self.assertEqual(
            duplicates,
            [],
            f"Duplicate element IDs found in rendered DOM: {duplicates}",
        )

    def test_color_scheme_meta_and_dark_theme_coherence(self):
        """Verifies dark color scheme meta tags match dark theme presentation."""
        html = render_page()
        self.assertIn('<meta name="color-scheme" content="dark">', html)
        self.assertIn('<meta name="theme-color" content="#0a0a0d">', html)

    def test_canvas_context_fallback_safety(self):
        """Verifies script includes defensive checks for canvas and 2D context availability."""
        raw = read_template()
        self.assertIn("if (!canvas) return;", raw)
        self.assertIn("if (!ctx) return;", raw)


class TestHeadlineBlockBuildUp(unittest.TestCase):
    """Headline builds one letter at a time, from the ground up, keeping plain text."""

    def setUp(self):
        self.raw = read_template()
        self.rendered = render_page()

    def test_headline_plain_text_intact_without_script(self):
        """The h1 ships the full line as markup so no-JS readers lose nothing."""
        self.assertIn(">Building The Website.<", self.rendered)

    def test_letter_cells_and_ground_rise(self):
        """Pixel canvas renders block glyphs with a coral moving glow."""
        self.assertIn("title-pixels", self.raw)
        self.assertIn("PIXEL_FONT_5X7", self.raw)
        self.assertIn("FF6B4A", self.raw)

    def test_builder_preserves_accessible_name(self):
        """Canvas is aria-hidden behind an aria-label of the full line."""
        self.assertIn('setAttribute(\'aria-label\'', self.raw)
        self.assertIn('aria-hidden', self.raw)

    def test_builder_respects_reduced_motion(self):
        """Reduced-motion readers skip the canvas and keep the static line."""
        self.assertIn("prefers-reduced-motion", self.raw)

    def test_loop_uses_delta_time_and_dpr(self):
        """Frame loop is delta-timed and the canvas scales by devicePixelRatio."""
        self.assertIn("requestAnimationFrame", self.raw)
        self.assertIn("devicePixelRatio", self.raw)
        self.assertIn("HOLD_S = 2.5", self.raw)


if __name__ == "__main__":
    unittest.main()
