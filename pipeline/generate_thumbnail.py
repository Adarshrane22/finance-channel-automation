"""
Generates 3 high-CTR thumbnail variants per video (1280x720), fully
offline with PIL — no stock photo / image-gen API needed.

Why 3 variants and why these particular styles: no single thumbnail
formula wins consistently, and the fastest way to find out what actually
works for *this* channel and *this* audience is to A/B test rather than
guess. Each variant leans on a different, well-established pattern for
finance/explainer content:

  - "stat_forward": the number IS the thumbnail (huge digit, small
    context label). Works when the script has a genuinely surprising
    number — leads with the thing that made the topic newsworthy.
  - "headline_forward": bold multi-line headline claim, the classic
    approach — works best when the *claim* is more surprising than any
    single number (e.g. "the Fed might raise rates").
  - "directional": headline + a big up/down arrow icon — useful for any
    "X is rising/falling" story, gives an instant visual read before the
    viewer even processes the text.

Upload all 3 to YouTube's thumbnail A/B test feature (if available on the
channel) or just eyeball them during human review and pick the strongest.

Usage:
  python generate_thumbnail.py <parsed_script.json> <output_dir> [brand_hex]

Writes <output_dir>/<stem>_thumb_stat.jpg, _thumb_headline.jpg, _thumb_directional.jpg
"""
import re
import sys
import textwrap
from pathlib import Path
import json
import random

from PIL import Image, ImageDraw, ImageFont, ImageFilter

WIDTH, HEIGHT = 1280, 720
FONT_BOLD = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
FONT_BLACK = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
ACCENT_YELLOW = (255, 209, 0)
ACCENT_RED = (255, 59, 48)
ACCENT_GREEN = (52, 199, 89)


def hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def base_canvas(brand_rgb, style="diagonal"):
    """A gradient + subtle vignette + light noise texture, so it reads as
    a designed background rather than a flat color fill."""
    dark = tuple(max(0, c - 90) for c in brand_rgb)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    px = img.load()
    for y in range(HEIGHT):
        for x in range(0, WIDTH, 4):  # step by 4 and fill blocks: much faster, imperceptible at this scale
            t = (x + y) / (WIDTH + HEIGHT)
            color = tuple(int(dark[i] + (brand_rgb[i] - dark[i]) * t) for i in range(3))
            for dx in range(4):
                if x + dx < WIDTH:
                    px[x + dx, y] = color
    # vignette: darken corners slightly for depth
    vignette = Image.new("L", (WIDTH, HEIGHT), 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse([-WIDTH * 0.25, -HEIGHT * 0.3, WIDTH * 1.25, HEIGHT * 1.3], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(120))
    dark_layer = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    img = Image.composite(img, dark_layer, vignette)
    return img


def draw_text_with_outline(draw, xy, text, font, fill, outline_fill=(0, 0, 0), outline_w=4):
    x, y = xy
    for dx in range(-outline_w, outline_w + 1, max(1, outline_w)):
        for dy in range(-outline_w, outline_w + 1, max(1, outline_w)):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=outline_fill)
    draw.text((x, y), text, font=font, fill=fill)


def fit_wrapped(draw, text, font_path, max_width, max_font_size, min_font_size, max_lines=3):
    for size in range(max_font_size, min_font_size - 1, -4):
        font = ImageFont.truetype(font_path, size)
        for wrap_width in range(8, 40):
            lines = textwrap.wrap(text, width=wrap_width)
            if len(lines) > max_lines:
                continue
            widths = [draw.textbbox((0, 0), line, font=font)[2] for line in lines]
            if max(widths) <= max_width:
                return font, lines
    return ImageFont.truetype(font_path, min_font_size), textwrap.wrap(text, width=25)


def shorten(title: str, max_words=7) -> str:
    words = title.split()
    return title if len(words) <= max_words else " ".join(words[:max_words])


def draw_arrow(draw, cx, cy, size, up: bool, color):
    """A bold, simple directional arrow icon drawn from polygons — no
    external icon assets needed."""
    half = size / 2
    if up:
        pts = [(cx, cy - half), (cx + half, cy), (cx + half * 0.35, cy),
               (cx + half * 0.35, cy + half), (cx - half * 0.35, cy + half),
               (cx - half * 0.35, cy), (cx - half, cy)]
    else:
        pts = [(cx, cy + half), (cx + half, cy), (cx + half * 0.35, cy),
               (cx + half * 0.35, cy - half), (cx - half * 0.35, cy - half),
               (cx - half * 0.35, cy), (cx - half, cy)]
    draw.polygon(pts, fill=color, outline=(0, 0, 0), width=4)


def variant_stat_forward(data, brand_rgb, out_path):
    img = base_canvas(brand_rgb)
    draw = ImageDraw.Draw(img)
    stat = data.get("headline_stat") or "?"
    label = shorten(data["title"], 5).upper()

    stat_font = ImageFont.truetype(FONT_BLACK, 300 if len(stat) <= 5 else 220)
    bbox = draw.textbbox((0, 0), stat, font=stat_font)
    x = (WIDTH - (bbox[2] - bbox[0])) // 2
    draw_text_with_outline(draw, (x, 60), stat, stat_font, ACCENT_YELLOW, outline_w=6)

    label_font, lines = fit_wrapped(draw, label, FONT_BOLD, WIDTH - 140, 64, 40, max_lines=2)
    y = HEIGHT - 60 - len(lines) * (label_font.size + 10)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=label_font)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw_text_with_outline(draw, (x, y), line, label_font, (255, 255, 255), outline_w=4)
        y += label_font.size + 10

    img.save(out_path, quality=93)


def variant_headline_forward(data, brand_rgb, out_path):
    img = base_canvas(brand_rgb)
    draw = ImageDraw.Draw(img)
    headline = shorten(data["title"], 8).upper()

    font, lines = fit_wrapped(draw, headline, FONT_BOLD, WIDTH - 120, 108, 50)
    line_height = font.size + 16
    total_h = line_height * len(lines)
    y = (HEIGHT - total_h) // 2
    stat = data.get("headline_stat")
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        # Highlight a number token within the line in accent yellow, since
        # concrete figures are what earn the click.
        if stat and stat in line:
            before, after = line.split(stat, 1)
            bx = x
            draw_text_with_outline(draw, (bx, y), before, font, (255, 255, 255))
            bx += draw.textbbox((0, 0), before, font=font)[2]
            draw_text_with_outline(draw, (bx, y), stat, font, ACCENT_YELLOW)
            bx += draw.textbbox((0, 0), stat, font=font)[2]
            draw_text_with_outline(draw, (bx, y), after, font, (255, 255, 255))
        else:
            draw_text_with_outline(draw, (x, y), line, font, (255, 255, 255))
        y += line_height

    img.save(out_path, quality=93)


def variant_directional(data, brand_rgb, out_path):
    img = base_canvas(brand_rgb)
    draw = ImageDraw.Draw(img)
    headline = shorten(data["title"], 6).upper()
    title_lower = data["title"].lower()
    up = not any(w in title_lower for w in ["cut", "fall", "drop", "crash", "down", "lower", "decline"])
    arrow_color = ACCENT_GREEN if up else ACCENT_RED

    draw_arrow(draw, WIDTH - 220, HEIGHT // 2, 260, up, arrow_color)

    font, lines = fit_wrapped(draw, headline, FONT_BOLD, WIDTH - 480, 90, 44, max_lines=3)
    line_height = font.size + 14
    total_h = line_height * len(lines)
    y = (HEIGHT - total_h) // 2
    for line in lines:
        draw_text_with_outline(draw, (60, y), line, font, (255, 255, 255), outline_w=4)
        y += line_height

    img.save(out_path, quality=93)


def generate_all(parsed_json_path: str, out_dir: str, brand_hex="1F6FEB"):
    data = json.loads(Path(parsed_json_path).read_text())
    brand_rgb = hex_to_rgb(brand_hex)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(parsed_json_path).stem

    paths = {}
    if data.get("headline_stat"):
        p = out_dir / f"{stem}_thumb_stat.jpg"
        variant_stat_forward(data, brand_rgb, p)
        paths["stat_forward"] = str(p)

    p = out_dir / f"{stem}_thumb_headline.jpg"
    variant_headline_forward(data, brand_rgb, p)
    paths["headline_forward"] = str(p)

    p = out_dir / f"{stem}_thumb_directional.jpg"
    variant_directional(data, brand_rgb, p)
    paths["directional"] = str(p)

    return paths


def main():
    if len(sys.argv) < 3:
        print("Usage: python generate_thumbnail.py <parsed_script.json> <output_dir> [brand_hex]")
        sys.exit(1)
    parsed_path, out_dir = sys.argv[1], sys.argv[2]
    brand_hex = sys.argv[3] if len(sys.argv) > 3 else "1F6FEB"
    paths = generate_all(parsed_path, out_dir, brand_hex)
    for name, path in paths.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
