#!/usr/bin/env python3
"""
aria-quote-card-li.py -- vertical quote card images for LinkedIn posts.

LinkedIn algorithm favors vertical images (1080x1350, 4:5 ratio).
Original visual content gets 3x reach and 6x save rate vs decorative images.

Two styles:
  quote_card  -- clean cream bg, sage accent, Lora serif
  terminal    -- dark terminal aesthetic for builder confessions
"""

from __future__ import annotations

import sys, os, textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")))

FONTS_DIR = WORKSPACE / "assets" / "fonts"
IMAGES_DIR = WORKSPACE / "images" / "linkedin"

# LinkedIn optimal: vertical 4:5 ratio
W, H = 1080, 1350

# Palette
BG_COLOR     = "#FAF8F3"
TEXT_COLOR    = "#2C2B28"
ACCENT_COLOR = "#6B8F71"
DARK_BG      = "#1A1A1A"
DARK_TEXT     = "#E8E4DC"
DARK_ACCENT   = "#6B8F71"
DIM_COLOR    = "#666666"


def render_linkedin_card(text: str, output_path: str | Path,
                         style: str = "quote_card") -> Path:
    """Render a quote card for LinkedIn. Vertical 1080x1350."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if style == "terminal":
        return _render_terminal(text, output_path)
    return _render_quote(text, output_path)


def _load_font(name: str, size: int):
    try:
        return ImageFont.truetype(str(FONTS_DIR / name), size)
    except OSError:
        return ImageFont.load_default()


def _render_quote(text: str, output_path: Path) -> Path:
    """Clean, bold quote card. Cream background, large text, sage accent."""
    img = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # accent bar on left
    draw.rectangle([80, 200, 88, H - 200], fill=ACCENT_COLOR)

    # load fonts
    font = _load_font("Lora-Regular.ttf", 52)
    attr_font = _load_font("DMSans-Regular.ttf", 26)

    # wrap text -- shorter lines for impact
    lines = textwrap.wrap(text, width=28)

    # vertical centering
    line_height = 72
    total_h = len(lines) * line_height
    y_start = max(200, (H - total_h) // 2 - 40)

    for line in lines:
        draw.text((120, y_start), line, font=font, fill=TEXT_COLOR)
        y_start += line_height

    # attribution
    draw.text((120, H - 140), "@BalabommaRao", font=attr_font, fill=ACCENT_COLOR)

    # bottom accent line
    draw.rectangle([80, H - 90, W - 80, H - 87], fill=ACCENT_COLOR)

    img.save(str(output_path), quality=95)
    return output_path


def _render_terminal(text: str, output_path: Path) -> Path:
    """Dark terminal card for builder/tech content."""
    img = Image.new("RGB", (W, H), DARK_BG)
    draw = ImageDraw.Draw(img)

    # terminal chrome
    for i, color in enumerate(["#FF5F56", "#FFBD2E", "#27C93F"]):
        draw.ellipse([50 + i * 30, 50, 66 + i * 30, 66], fill=color)
    draw.rectangle([40, 85, W - 40, 86], fill="#333333")

    # load fonts
    font = _load_font("DMSans-Regular.ttf", 44)
    small_font = _load_font("DMSans-Regular.ttf", 22)

    # prompt
    draw.text((60, 115), "$ aria --insight", font=small_font, fill=DIM_COLOR)

    # text
    lines = textwrap.wrap(text, width=32)
    line_height = 62
    total_h = len(lines) * line_height
    y_start = max(180, (H - total_h) // 2 - 30)

    for line in lines:
        draw.text((60, y_start), line, font=font, fill=DARK_ACCENT)
        y_start += line_height

    # cursor
    draw.rectangle([60, y_start + 15, 76, y_start + 40], fill=DARK_ACCENT)

    # attribution
    draw.text((W - 250, H - 70), "@BalabommaRao", font=small_font, fill=DIM_COLOR)

    img.save(str(output_path), quality=95)
    return output_path


def render_for_li_queue(card_text: str, item_id: int | str,
                        territory: str = "") -> str | None:
    """Render a quote card for a LinkedIn queue item. Returns image path."""
    if not card_text:
        return None

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    output_path = IMAGES_DIR / f"li_{item_id}.png"
    if output_path.exists():
        return str(output_path)

    # use terminal style for building/ai, quote for rest
    style = "terminal" if territory in ("building", "ai") else "quote_card"
    render_linkedin_card(card_text, output_path, style=style)
    return str(output_path)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        text = sys.argv[1]
        output = sys.argv[2]
        style = sys.argv[3] if len(sys.argv) > 3 else "quote_card"
        path = render_linkedin_card(text, output, style=style)
        print(f"rendered: {path}")
    else:
        print("usage: aria-quote-card-li.py 'text' output.png [quote_card|terminal]")
