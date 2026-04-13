#!/usr/bin/env python3
"""
aria-quote-card.py -- generate quote card images for tweets.

usage:
  python3 scripts/aria-quote-card.py "tweet text here" output.png
  python3 scripts/aria-quote-card.py --from-queue  # render all queued cards
"""

from __future__ import annotations

import sys, os, textwrap, json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")))

FONTS_DIR = WORKSPACE / "assets" / "fonts"
IMAGES_DIR = WORKSPACE / "images"

# from voice.json quote_card_style
BG_COLOR     = "#FAF8F3"
TEXT_COLOR    = "#2C2B28"
ACCENT_COLOR = "#6B8F71"
W, H         = 1200, 675


def render_quote_card(text: str, output_path: str | Path,
                      style: str = "quote_card") -> Path:
    """Render a tweet as a quote card image. Returns the output path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if style == "terminal_screenshot":
        return _render_terminal(text, output_path)

    return _render_quote(text, output_path)


def _render_quote(text: str, output_path: Path) -> Path:
    """Warm cream background, sage accent bar, Lora serif."""
    img = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # accent bar on left
    draw.rectangle([60, 100, 66, H - 100], fill=ACCENT_COLOR)

    # load fonts
    try:
        font = ImageFont.truetype(str(FONTS_DIR / "Lora-Regular.ttf"), 38)
    except OSError:
        font = ImageFont.load_default()
    try:
        attr_font = ImageFont.truetype(str(FONTS_DIR / "DMSans-Regular.ttf"), 20)
    except OSError:
        attr_font = ImageFont.load_default()

    # word wrap -- target ~40 chars per line for readability
    lines = textwrap.wrap(text.lower(), width=40)

    # calculate vertical centering
    line_height = 54
    total_text_height = len(lines) * line_height
    y_start = max(100, (H - total_text_height) // 2 - 20)

    for line in lines:
        draw.text((90, y_start), line, font=font, fill=TEXT_COLOR)
        y_start += line_height

    # attribution
    draw.text((90, H - 70), "@BalabommaRao", font=attr_font, fill=ACCENT_COLOR)

    # subtle bottom accent line
    draw.rectangle([60, H - 40, W - 60, H - 38], fill=ACCENT_COLOR)

    img.save(str(output_path), quality=95)
    return output_path


def _render_terminal(text: str, output_path: Path) -> Path:
    """Dark terminal aesthetic for builder confessions."""
    bg = "#1E1E1E"
    text_color = "#6B8F71"   # sage green on dark
    dim_color = "#666666"

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # terminal chrome - three dots
    for i, color in enumerate(["#FF5F56", "#FFBD2E", "#27C93F"]):
        draw.ellipse([30 + i * 25, 25, 42 + i * 25, 37], fill=color)

    # title bar line
    draw.rectangle([20, 50, W - 20, 51], fill="#333333")

    # load font
    try:
        font = ImageFont.truetype(str(FONTS_DIR / "DMSans-Regular.ttf"), 28)
    except OSError:
        font = ImageFont.load_default()
    try:
        small_font = ImageFont.truetype(str(FONTS_DIR / "DMSans-Regular.ttf"), 16)
    except OSError:
        small_font = ImageFont.load_default()

    # prompt prefix
    draw.text((40, 75), "$ aria --post", font=small_font, fill=dim_color)

    # wrap and render tweet text
    lines = textwrap.wrap(text.lower(), width=50)
    y = 115
    for line in lines:
        draw.text((40, y), line, font=font, fill=text_color)
        y += 42

    # blinking cursor (static representation)
    draw.rectangle([40, y + 10, 52, y + 30], fill=text_color)

    # attribution bottom right
    draw.text((W - 200, H - 45), "@BalabommaRao", font=small_font, fill=dim_color)

    img.save(str(output_path), quality=95)
    return output_path


def render_for_queue_item(text: str, image_type: str, item_id: str) -> str | None:
    """Render a quote card for a queue item. Returns image path or None."""
    if image_type not in ("quote_card", "terminal_screenshot"):
        return None

    output_path = IMAGES_DIR / f"{item_id}.png"
    if output_path.exists():
        return str(output_path)

    render_quote_card(text, output_path, style=image_type)
    return str(output_path)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        text = sys.argv[1]
        output = sys.argv[2]
        style = sys.argv[3] if len(sys.argv) > 3 else "quote_card"
        path = render_quote_card(text, output, style=style)
        print(f"rendered: {path}")
    elif "--from-queue" in sys.argv:
        # render all queued items that need images
        import sqlite3
        db = sqlite3.connect(str(WORKSPACE / "memory" / "aria.db"))
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, text, image_type FROM queue "
            "WHERE status='queued' AND image_type IN ('quote_card', 'terminal_screenshot')"
        ).fetchall()
        for row in rows:
            path = render_for_queue_item(row["text"], row["image_type"], row["id"])
            if path:
                print(f"  {row['id']}: {path}")
        db.close()
    else:
        print("usage: aria-quote-card.py 'text' output.png [quote_card|terminal_screenshot]")
        print("       aria-quote-card.py --from-queue")
