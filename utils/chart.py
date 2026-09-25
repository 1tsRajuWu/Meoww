"""Market charts, drawn with the Pillow the bot already ships.

A chart is the one place a price becomes a *shape*, so the two things that must
never happen here are drawing a plausible-looking series the data does not
contain and drawing a line that hides how little data there is.

Hence the rules:

* **The renderer draws exactly what it is given.** It receives candles (true
  open/high/low/close, from a real exchange) or closes alone, and switches between
  candlesticks and a line based on which it got — it never invents the missing
  wicks. Too many candles to draw as bodies (over ``MAX_CANDLES``) falls back to a
  line rather than drawing an illegible smear.
* **A flat series is flat.** An unchanged price has no range to scale against, so
  the plot is padded around it instead of dividing by zero or pretending to a
  movement that is not there.
* **The member's own entry is part of the picture.** Their average cost is drawn
  across the chart when they hold the instrument, which is the honest way to show
  profit or loss: not a number beside a chart, but a line above or below the price.
"""
from __future__ import annotations

import datetime
import io

from PIL import Image, ImageDraw

from utils.card import font
from utils.market import Candle

#: Palette — dark, and deliberately the same greens and reds as the cards.
BACKGROUND = (17, 21, 28, 255)
PANEL = (24, 30, 40, 255)
GRID = (44, 52, 66, 255)
TEXT = (232, 238, 246, 255)
MUTED = (138, 150, 168, 255)
UP = (38, 166, 91, 255)
DOWN = (224, 49, 74, 255)
ENTRY = (240, 196, 65, 255)
ACCENT = (88, 101, 242, 255)

#: Above this many points, bodies are unreadable and a line is drawn instead.
MAX_CANDLES = 120
#: Sparkline glyphs, low to high.
BLOCKS = "▁▂▃▄▅▆▇█"


def is_line_series(candles: list[Candle]) -> bool:
    """Whether this series should be drawn as a line rather than candles."""
    if len(candles) > MAX_CANDLES:
        return True
    return all(candle.flat for candle in candles)


def short_number(value: float) -> str:
    """Coins or dollars, abbreviated the way a chart axis has room for."""
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000_000:
        return f"{sign}{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{sign}{value / 1_000:.1f}k"
    if value >= 1:
        return f"{sign}{value:,.0f}"
    return f"{sign}{value:.4f}".rstrip("0").rstrip(".")


def sparkline(values: list[float]) -> str:
    """A one-line shape of a series, for places an image cannot go.

    Returns an empty string for anything with no shape to show — fewer than two
    distinct points is a fact, not a trend.
    """
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    if high <= low:
        return BLOCKS[0] * len(values)
    span = high - low
    return "".join(BLOCKS[min(len(BLOCKS) - 1, int((value - low) / span * (len(BLOCKS) - 1)))] for value in values)


def render_market_chart(
    *,
    symbol: str,
    name: str = "",
    candles: list[Candle],
    price_coins: float | None = None,
    change_pct: float | None = None,
    entry_coins: float | None = None,
    source: str = "",
    interval_label: str = "",
    age_seconds: float | None = None,
    width: int = 1000,
    height: int = 430,
) -> io.BytesIO:
    """Draw a price chart and return it as a PNG in memory.

    ``entry_coins`` is the member's average cost for this instrument; when it is
    given (and inside the plotted range) it is drawn as a labelled line, so the
    chart shows profit or loss as geometry rather than as a number beside it.
    """
    image = Image.new("RGB", (width, height), BACKGROUND[:3])
    draw = ImageDraw.Draw(image)

    title_font = font("Roboto-Bold.ttf", 30)
    price_font = font("Roboto-Bold.ttf", 26)
    label_font = font("Roboto-Regular.ttf", 16)
    small_font = font("Roboto-Regular.ttf", 14)

    # -- header ---------------------------------------------------------- #
    draw.text((26, 20), f"{symbol} · {name}" if name else symbol, fill=TEXT[:3], font=title_font)
    if price_coins is not None:
        price_text = f"{short_number(price_coins)} coins"
        box = draw.textbbox((0, 0), price_text, font=price_font)
        draw.text((width - 26 - (box[2] - box[0]), 22), price_text, fill=TEXT[:3], font=price_font)
    if change_pct is not None:
        change_text = f"{change_pct:+.2f}%"
        colour = UP[:3] if change_pct >= 0 else DOWN[:3]
        box = draw.textbbox((0, 0), change_text, font=label_font)
        draw.text((width - 26 - (box[2] - box[0]), 58), change_text, fill=colour, font=label_font)

    plot_left, plot_top = 26, 96
    plot_right, plot_bottom = width - 110, height - 52
    if plot_right <= plot_left or plot_bottom <= plot_top:
        plot_right, plot_bottom = width - 26, height - 26
    draw.rectangle((plot_left, plot_top, plot_right, plot_bottom), fill=PANEL[:3])

    if not candles:
        draw.text(
            (plot_left + 20, plot_top + 20),
            "No price history to draw yet.",
            fill=MUTED[:3],
            font=label_font,
        )
    else:
        lows = [candle.low for candle in candles]
        highs = [candle.high for candle in candles]
        low, high = min(lows), max(highs)
        if entry_coins and entry_coins > 0:
            # Include the member's entry price so their line is on the chart
            # rather than off the top or bottom of it.
            low, high = min(low, entry_coins), max(high, entry_coins)
        if high <= low:
            # A flat market: pad around it instead of dividing by zero.
            pad = low * 0.01 if low else 1.0
            low, high = low - pad, high + pad
        span = high - low

        def y_of(value: float) -> float:
            return plot_bottom - (value - low) / span * (plot_bottom - plot_top)

        # -- gridlines and axis labels ---------------------------------- #
        for step in range(5):
            value = low + span * step / 4
            y = y_of(value)
            draw.line((plot_left, y, plot_right, y), fill=GRID[:3], width=1)
            draw.text(
                (plot_right + 8, y - 8), short_number(value), fill=MUTED[:3], font=small_font
            )

        step_x = (plot_right - plot_left) / max(1, len(candles))
        line_mode = is_line_series(candles)

        if line_mode:
            points = [
                (plot_left + step_x * (index + 0.5), y_of(candle.close))
                for index, candle in enumerate(candles)
            ]
            if len(points) > 1:
                # A soft fill under the line, so the shape reads at a glance.
                draw.polygon(
                    [(points[0][0], plot_bottom), *points, (points[-1][0], plot_bottom)],
                    fill=(30, 42, 60),
                )
                draw.line(points, fill=ACCENT[:3], width=3, joint="curve")
            for x, y in points[-1:]:
                draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=ACCENT[:3])
        else:
            body_width = max(1, int(step_x * 0.6))
            for index, candle in enumerate(candles):
                centre = plot_left + step_x * (index + 0.5)
                rising = candle.close >= candle.open
                colour = UP[:3] if rising else DOWN[:3]
                draw.line((centre, y_of(candle.high), centre, y_of(candle.low)), fill=colour, width=1)
                top, bottom = y_of(candle.open), y_of(candle.close)
                if top == bottom:
                    bottom = top + 1
                draw.rectangle(
                    (centre - body_width / 2, min(top, bottom), centre + body_width / 2, max(top, bottom)),
                    fill=colour,
                )

        # -- the member's average cost ---------------------------------- #
        if entry_coins and entry_coins > 0:
            entry_y = y_of(entry_coins)
            for x in range(plot_left, plot_right, 12):
                draw.line((x, entry_y, x + 6, entry_y), fill=ENTRY[:3], width=2)
            draw.text(
                (plot_left + 6, entry_y - 18),
                f"your avg cost {short_number(entry_coins)}",
                fill=ENTRY[:3],
                font=small_font,
            )

        # -- last price marker ------------------------------------------ #
        last_y = y_of(candles[-1].close)
        for x in range(plot_left, plot_right, 12):
            draw.line((x, last_y, x + 6, last_y), fill=MUTED[:3], width=1)

        # -- time axis --------------------------------------------------- #
        stamps = (0, len(candles) // 2, len(candles) - 1)
        for index in sorted(set(stamps)):
            stamp = candles[index].ts
            label = _clock(stamp)
            x = plot_left + step_x * (index + 0.5)
            box = draw.textbbox((0, 0), label, font=small_font)
            draw.text(
                (min(max(plot_left, x - (box[2] - box[0]) / 2), plot_right - (box[2] - box[0])), plot_bottom + 8),
                label,
                fill=MUTED[:3],
                font=small_font,
            )

    # -- footer ---------------------------------------------------------- #
    bits = [bit for bit in (source, interval_label) if bit]
    if candles:
        bits.append(f"{len(candles)} points")
    if age_seconds is not None:
        bits.append(f"price {_age(age_seconds)} old")
    footer = " • ".join(bits) or "no source"
    draw.text((26, height - 30), footer[:110], fill=MUTED[:3], font=small_font)
    badge = "DaCat Markets"
    box = draw.textbbox((0, 0), badge, font=small_font)
    draw.text((width - 26 - (box[2] - box[0]), height - 30), badge, fill=MUTED[:3], font=small_font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _clock(ts: float) -> str:
    """A short UTC timestamp for the time axis."""
    try:
        moment = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    if moment.hour == 0 and moment.minute == 0:
        return moment.strftime("%d %b")
    return moment.strftime("%H:%M")


def _age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"
