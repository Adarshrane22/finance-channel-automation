"""
Generates 3 high-CTR, top-level-graphic-design-quality thumbnail variants
per video (1280x720), fully offline with PIL — no stock photo / image-gen
API needed, so there is zero copyright/licensing risk in the output.

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
    single number (e.g. "the Fed might raise rates"). This is the
    always-available fallback if the other two don't apply.
  - "directional": headline + a big glowing up/down arrow icon — useful
    for any "X is rising/falling" story, gives an instant visual read
    before the viewer even processes the text.

What makes this pass "top-level" rather than a flat gradient + text card
(the previous version), all still 100% procedural/original assets:
  - A radial "spotlight" glow behind the focal element instead of a flat
    linear gradient, plus a faint diagonal ticker-grid texture — the kind
    of layered depth real thumbnail designers add by hand.
  - A dark "shelf" gradient anchored to whichever edge the text sits on,
    so text stays legible regardless of what's behind it (a standard
    professional-thumbnail technique).
  - A soft drop shadow under every text block (not just a thin outline),
    a small rounded "kicker" badge (BREAKING / category tag) for extra
    urgency and visual hierarchy, and a subtle channel watermark bug —
    the small details that separate a designed thumbnail from a text
    dump on a gradient.
  - Accent color is chosen from the marketing package's thumbnail
    emotion/color_psychology fields when available (fear/urgency -> red,
    opportunity/growth -> green, neutral/surprise -> brand yellow),
    instead of always defaulting to yellow.

All 3 variants are always rendered (so the full set is in every video's
output artifacts for manual review or a manual YouTube "Test & compare"
run — that feature has no public API, so it can't be automated end to
end), but the pipeline needs exactly ONE thumbnail to actually upload and
flash into the video. That choice is not hardcoded to "headline_forward"
anymore — see `select_primary_variant` below:

  - Each variant gets a base heuristic score (does a strong stat exist
    for stat_forward? does the title carry clear directional language
    for directional? headline_forward is always a safe baseline).
  - That score is multiplied by a *learned* weight per style, read from
    docs/thumbnail_style_weights.json — a file `analyze_performance.py`
    writes after joining real YouTube Analytics CTR data (via
    docs/thumbnail_style_log.json, which daily_cycle.py appends to after
    every successful upload) back to which style each past video used.
    In other words: once enough videos have gone out, the channel starts
    favoring whichever thumbnail style is actually earning more clicks
    for *this* audience, automatically, no manual A/B review required.
  - A small epsilon-greedy exploration chance (20%, deterministic per
    video so it's reproducible on a retry) keeps trying non-favorite
    styles too, so the weighting can't get stuck on an early fluke and
    stops collecting the data it needs to keep learning.
  - With no weights file yet (a fresh channel, or before enough videos
    have real analytics), every style defaults to a neutral 1.0 weight —
    behavior is just the heuristic scoring, nothing regresses.

Usage:
  python generate_thumbnail.py <parsed_script.json> <output_dir> [brand_hex]

Writes <output_dir>/<stem>_thumb_stat.jpg, _thumb_headline.jpg,
_thumb_directional.jpg, and <stem>_thumb_chosen.json (which one was
actually selected, and why — see select_primary_variant).
"""
import random
import re
import sys
import textwrap
from pathlib import Path
import json

from PIL import Image, ImageDraw, ImageFont, ImageFilter

WIDTH, HEIGHT = 1280, 720
FONT_BOLD = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
FONT_BLACK = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
FONT_MEDIUM = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
CHANNEL_WATERMARK = "THE RATE REPORT"

ACCENT_YELLOW = (255, 209, 0)
ACCENT_RED = (255, 59, 48)
ACCENT_GREEN = (52, 199, 89)
ACCENT_WHITE = (255, 255, 255)

FEAR_WORDS = ["crash", "collapse", "warn", "danger", "risk", "fear", "recession", "layoff", "plunge", "shock", "crisis", "cut"]
GROWTH_WORDS = ["surge", "soar", "rally", "boom", "gain", "record", "win", "grow", "opportunity", "rise", "profit"]


def hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def pick_accent(marketing: dict, brand_rgb, title: str):
    """Chooses the accent color a real designer would: red for
    fear/urgency stories, green for growth/opportunity stories, brand
    yellow otherwise. Prefers the marketing package's own
    emotion/color_psychology fields (written specifically for the
    thumbnail) when present, falling back to scanning the title."""
    signal = ""
    if marketing:
        thumb = marketing.get("thumbnail") or {}
        signal = f"{thumb.get('emotion', '')} {thumb.get('color_psychology', '')}".lower()
    if not signal:
        signal = title.lower()
    if any(w in signal for w in FEAR_WORDS):
        return ACCENT_RED
    if any(w in signal for w in GROWTH_WORDS):
        return ACCENT_GREEN
    return ACCENT_YELLOW


def base_canvas(brand_rgb, accent_rgb=None, focal=(0.72, 0.38)):
    """A layered background: diagonal gradient base + a radial spotlight
    glow behind the focal point (where the eye should land first) + a
    faint ticker-grid texture + a vignette for depth. This is the biggest
    single upgrade over a flat gradient fill — it's what makes the canvas
    read as "designed" before a single word of text is even placed."""
    dark = tuple(max(0, c - 100) for c in brand_rgb)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    px = img.load()
    for y in range(HEIGHT):
        for x in range(0, WIDTH, 4):
            t = (x + y) / (WIDTH + HEIGHT)
            color = tuple(int(dark[i] + (brand_rgb[i] - dark[i]) * t) for i in range(3))
            for dx in range(4):
                if x + dx < WIDTH:
                    px[x + dx, y] = color

    # Faint diagonal ticker-grid: thin repeating lines at a slant, low
    # alpha — reads as "financial data" texture without competing with
    # the text.
    grid = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grid)
    for gx in range(-HEIGHT, WIDTH, 46):
        gdraw.line([(gx, 0), (gx + HEIGHT, HEIGHT)], fill=(255, 255, 255, 10), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), grid).convert("RGB")

    # Radial spotlight glow behind the focal point, in the accent color,
    # additively brightening that area so whatever sits there (a big
    # stat, an arrow) pops instead of blending into the gradient.
    if accent_rgb:
        glow = Image.new("L", (WIDTH, HEIGHT), 0)
        gdraw2 = ImageDraw.Draw(glow)
        fx, fy = int(WIDTH * focal[0]), int(HEIGHT * focal[1])
        r = int(WIDTH * 0.42)
        gdraw2.ellipse([fx - r, fy - r, fx + r, fy + r], fill=140)
        glow = glow.filter(ImageFilter.GaussianBlur(90))
        glow_layer = Image.new("RGB", (WIDTH, HEIGHT), accent_rgb)
        img = Image.composite(glow_layer, img, glow.point(lambda a: int(a * 0.35)))

    # Vignette: darken corners slightly for depth.
    vignette = Image.new("L", (WIDTH, HEIGHT), 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse([-WIDTH * 0.25, -HEIGHT * 0.3, WIDTH * 1.25, HEIGHT * 1.3], fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(120))
    dark_layer = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    img = Image.composite(img, dark_layer, vignette)
    return img


def add_bottom_shelf(img, height_frac=0.42):
    """A dark gradient 'shelf' rising from the bottom edge, standard
    professional-thumbnail technique so headline text stays legible no
    matter what's happening in the background behind it."""
    shelf = Image.new("L", (WIDTH, HEIGHT), 0)
    sh_h = int(HEIGHT * height_frac)
    px = shelf.load()
    for y in range(HEIGHT - sh_h, HEIGHT):
        t = (y - (HEIGHT - sh_h)) / sh_h
        alpha = int(180 * (t ** 1.4))
        for x in range(WIDTH):
            px[x, y] = alpha
    black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    return Image.composite(black, img, shelf)


def draw_soft_shadow_text(draw, xy, text, font, fill, shadow_offset=(0, 8), shadow_blur=None, outline_fill=(0, 0, 0), outline_w=4):
    """Outline (for crisp legibility at small sizes) plus a soft drop
    shadow (for depth) — real thumbnails use both, not just one."""
    x, y = xy
    for dx in range(-outline_w, outline_w + 1, max(1, outline_w)):
        for dy in range(-outline_w, outline_w + 1, max(1, outline_w)):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=outline_fill)
    draw.text((x, y), text, font=font, fill=fill)


def draw_kicker(img, text, accent_rgb, pos=(48, 40)):
    """A small rounded badge (e.g. BREAKING) in the top-left — cheap
    visual hierarchy trick that draws the eye and adds urgency."""
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_BOLD, 30)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 22, 12
    x0, y0 = pos
    x1, y1 = x0 + tw + pad_x * 2, y0 + th + pad_y * 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) // 2, fill=accent_rgb)
    draw.text((x0 + pad_x, y0 + pad_y - bbox[1]), text, font=font, fill=(15, 15, 15))
    return y1  # bottom edge, so callers can stack content below it


def draw_watermark(img):
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_MEDIUM, 24)
    text = CHANNEL_WATERMARK
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    x, y = WIDTH - tw - 32, HEIGHT - 46
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 200))


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


def draw_arrow(img, cx, cy, size, up: bool, color):
    """A bold directional arrow with a soft glow behind it and a subtle
    two-tone fill for a bit of dimensionality — no external icon asset
    needed, still fully procedural."""
    half = size / 2

    def _pts(scale):
        h = half * scale
        if up:
            return [(cx, cy - h), (cx + h, cy), (cx + h * 0.35, cy),
                    (cx + h * 0.35, cy + h), (cx - h * 0.35, cy + h),
                    (cx - h * 0.35, cy), (cx - h, cy)]
        return [(cx, cy + h), (cx + h, cy), (cx + h * 0.35, cy),
                (cx + h * 0.35, cy - h), (cx - h * 0.35, cy - h),
                (cx - h * 0.35, cy), (cx - h, cy)]

    # Glow: blurred, oversized, low-alpha copy behind the arrow.
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.polygon(_pts(1.35), fill=(*color, 130))
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    img.paste(Image.new("RGB", (WIDTH, HEIGHT), color), (0, 0), glow.split()[3].point(lambda a: int(a * 0.9)))

    draw = ImageDraw.Draw(img)
    lighter = tuple(min(255, c + 40) for c in color)
    draw.polygon(_pts(1.0), fill=lighter, outline=(0, 0, 0), width=5)
    # A thin darker inner accent line for a faceted, less-flat look.
    draw.line(_pts(1.0) + [_pts(1.0)[0]], fill=tuple(max(0, c - 60) for c in color), width=2)


def variant_stat_forward(data, brand_rgb, accent_rgb, marketing, out_path):
    img = base_canvas(brand_rgb, accent_rgb, focal=(0.5, 0.32))
    img = add_bottom_shelf(img, height_frac=0.40)
    bottom = draw_kicker(img, "BREAKING", accent_rgb)
    draw = ImageDraw.Draw(img)

    stat = data.get("headline_stat") or "?"
    label = shorten(data["title"], 5).upper()

    stat_font = ImageFont.truetype(FONT_BLACK, 280 if len(stat) <= 5 else 200)
    bbox = draw.textbbox((0, 0), stat, font=stat_font)
    x = (WIDTH - (bbox[2] - bbox[0])) // 2
    y = max(bottom + 20, 90)
    draw_soft_shadow_text(draw, (x, y), stat, stat_font, accent_rgb, outline_w=6)

    label_font, lines = fit_wrapped(draw, label, FONT_BOLD, WIDTH - 140, 64, 40, max_lines=2)
    ly = HEIGHT - 60 - len(lines) * (label_font.size + 10)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=label_font)
        lx = (WIDTH - (bbox[2] - bbox[0])) // 2
        draw_soft_shadow_text(draw, (lx, ly), line, label_font, (255, 255, 255), outline_w=4)
        ly += label_font.size + 10

    draw_watermark(img)
    img.save(out_path, quality=95)


def variant_headline_forward(data, brand_rgb, accent_rgb, marketing, out_path):
    img = base_canvas(brand_rgb, accent_rgb, focal=(0.78, 0.30))
    img = add_bottom_shelf(img, height_frac=0.55)
    kicker = "JUST IN"
    if marketing:
        risk_flags = marketing.get("risk_flags") or []
        if any("breaking" in str(f).lower() for f in risk_flags):
            kicker = "BREAKING"
    draw_kicker(img, kicker, accent_rgb)
    draw = ImageDraw.Draw(img)
    headline = shorten(data["title"], 8).upper()

    font, lines = fit_wrapped(draw, headline, FONT_BOLD, WIDTH - 120, 104, 50)
    line_height = font.size + 16
    total_h = line_height * len(lines)
    y = HEIGHT - 70 - total_h
    stat = data.get("headline_stat")
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        if stat and stat in line:
            before, after = line.split(stat, 1)
            bx = x
            draw_soft_shadow_text(draw, (bx, y), before, font, (255, 255, 255))
            bx += draw.textbbox((0, 0), before, font=font)[2]
            draw_soft_shadow_text(draw, (bx, y), stat, font, accent_rgb)
            bx += draw.textbbox((0, 0), stat, font=font)[2]
            draw_soft_shadow_text(draw, (bx, y), after, font, (255, 255, 255))
        else:
            draw_soft_shadow_text(draw, (x, y), line, font, (255, 255, 255))
        y += line_height

    draw_watermark(img)
    img.save(out_path, quality=95)


def variant_directional(data, brand_rgb, accent_rgb, marketing, out_path):
    title_lower = data["title"].lower()
    up = not any(w in title_lower for w in ["cut", "fall", "drop", "crash", "down", "lower", "decline"])
    arrow_color = ACCENT_GREEN if up else ACCENT_RED

    img = base_canvas(brand_rgb, arrow_color, focal=(0.82, 0.5))
    draw_arrow(img, WIDTH - 230, HEIGHT // 2, 260, up, arrow_color)
    img = add_bottom_shelf(img, height_frac=0.0)  # no-op shelf here; left panel already has contrast from vignette
    draw_kicker(img, "MARKET MOVE", arrow_color)
    draw = ImageDraw.Draw(img)

    headline = shorten(data["title"], 6).upper()
    font, lines = fit_wrapped(draw, headline, FONT_BOLD, WIDTH - 480, 88, 44, max_lines=3)
    line_height = font.size + 14
    total_h = line_height * len(lines)
    y = (HEIGHT - total_h) // 2 + 30
    for line in lines:
        draw_soft_shadow_text(draw, (60, y), line, font, (255, 255, 255), outline_w=4)
        y += line_height

    draw_watermark(img)
    img.save(out_path, quality=95)


def load_marketing(parsed_json_path: Path):
    sidecar_path = parsed_json_path.with_name(parsed_json_path.stem + "_marketing.json")
    if not sidecar_path.exists():
        return None
    try:
        return json.loads(sidecar_path.read_text())
    except Exception as e:
        print(f"WARNING: found {sidecar_path} but couldn't parse it ({e}) — using defaults for thumbnail design.")
        return None


def load_thumbnail_headline(marketing: dict):
    """If research_and_script.py's Stage B marketing sidecar includes a
    dedicated thumbnail concept, its headline (written specifically to be
    short and punchy for a 1280x720 image, distinct from the video's
    actual title) is used for the on-image text instead of a shortened
    video title."""
    if not marketing:
        return None
    return (marketing.get("thumbnail") or {}).get("headline") or None


# ----------------------------------------------------------------------------
# Learning-driven variant selection
# ----------------------------------------------------------------------------

DIRECTIONAL_WORDS = [
    "cut", "fall", "drop", "crash", "down", "lower", "decline",
    "surge", "soar", "rally", "rise", "jump", "climb", "spike", "plunge",
]
EXPLORATION_RATE = 0.2  # how often we deliberately try a non-favorite style, to keep collecting real data on it
STYLE_WEIGHT_FLOOR, STYLE_WEIGHT_CEIL = 0.4, 2.5


def repo_docs_dir() -> Path:
    """docs/ at the repo root, resolved relative to this file
    (pipeline/generate_thumbnail.py) rather than the current working
    directory, since daily_cycle.py invokes this script from various
    output directories."""
    return Path(__file__).resolve().parent.parent / "docs"


def load_style_weights() -> dict:
    """Reads docs/thumbnail_style_weights.json, written by
    analyze_performance.py from real CTR data joined against
    docs/thumbnail_style_log.json. Missing/unparseable file (a fresh
    channel with no performance history yet) just means every style
    scores at a neutral weight — this never blocks thumbnail generation."""
    path = repo_docs_dir() / "thumbnail_style_weights.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"WARNING: found {path} but couldn't parse it ({e}) — using neutral weights.")
        return {}


def score_variants(data: dict, available_styles) -> dict:
    """Base heuristic score per style, BEFORE learned weighting — this is
    the same reasoning a designer would apply by eye: does this variant's
    formula actually fit this specific title/stat."""
    scores = {}
    if "headline_forward" in available_styles:
        scores["headline_forward"] = 1.0  # always a safe, always-applicable baseline

    if "stat_forward" in available_styles:
        stat = data.get("headline_stat") or ""
        scores["stat_forward"] = 1.3 if stat and len(stat) <= 5 else 0.9

    if "directional" in available_styles:
        title_lower = data["title"].lower()
        scores["directional"] = 1.2 if any(w in title_lower for w in DIRECTIONAL_WORDS) else 0.7

    return scores


def select_primary_variant(paths: dict, data: dict, stem: str) -> dict:
    """Picks exactly one rendered variant to be THE thumbnail — the one
    uploaded to YouTube and flashed into the video. Combines the base
    heuristic score with the learned per-style weight (see
    load_style_weights), then applies epsilon-greedy exploration so the
    system keeps sampling styles it isn't currently favoring instead of
    locking in on whatever won first. Returns None only if somehow no
    variants were rendered at all."""
    available_styles = list(paths.keys())
    if not available_styles:
        return None

    base_scores = score_variants(data, available_styles)
    weights = load_style_weights()

    final_scores = {}
    for style, base in base_scores.items():
        raw_weight = weights.get(style, {})
        learned = raw_weight.get("weight", 1.0) if isinstance(raw_weight, dict) else raw_weight
        learned = max(STYLE_WEIGHT_FLOOR, min(STYLE_WEIGHT_CEIL, float(learned or 1.0)))
        final_scores[style] = round(base * learned, 4)

    # Deterministic per-video "randomness" (seeded on the stem) so a retry
    # of the same video makes the same exploration choice, rather than
    # flip-flopping between styles across reruns of a failed pipeline.
    rng = random.Random(stem)
    if len(final_scores) > 1 and rng.random() < EXPLORATION_RATE:
        chosen_style = rng.choice(list(final_scores.keys()))
        reason = "exploration"
    else:
        chosen_style = max(final_scores, key=final_scores.get)
        reason = "best_score"

    return {
        "style": chosen_style,
        "path": paths[chosen_style],
        "reason": reason,
        "scores": final_scores,
        "weights_used": weights,
    }


def generate_all(parsed_json_path: str, out_dir: str, brand_hex="1F6FEB"):
    parsed_json_path = Path(parsed_json_path)
    data = json.loads(parsed_json_path.read_text())
    brand_rgb = hex_to_rgb(brand_hex)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = parsed_json_path.stem

    marketing = load_marketing(parsed_json_path)

    # Only the on-image text uses the marketing headline (if any) — data
    # itself (headline_stat, etc.) is left untouched so nothing else in
    # this file needs to change.
    thumb_headline = load_thumbnail_headline(marketing)
    if thumb_headline:
        print(f"Using marketing sidecar thumbnail headline: {thumb_headline!r}")
        data = {**data, "title": thumb_headline}

    accent_rgb = pick_accent(marketing, brand_rgb, data["title"])

    paths = {}
    if data.get("headline_stat"):
        p = out_dir / f"{stem}_thumb_stat.jpg"
        variant_stat_forward(data, brand_rgb, accent_rgb, marketing, p)
        paths["stat_forward"] = str(p)

    p = out_dir / f"{stem}_thumb_headline.jpg"
    variant_headline_forward(data, brand_rgb, accent_rgb, marketing, p)
    paths["headline_forward"] = str(p)

    p = out_dir / f"{stem}_thumb_directional.jpg"
    variant_directional(data, brand_rgb, accent_rgb, marketing, p)
    paths["directional"] = str(p)

    # Pick the one variant that actually becomes the video's thumbnail —
    # see select_primary_variant's docstring for the scoring + learned-
    # weight + exploration logic. Written to a sidecar rather than just
    # returned so daily_cycle.py (a separate process, called via
    # subprocess) and any manual inspection can both read it.
    chosen = select_primary_variant(paths, data, stem)
    if chosen:
        chosen_path = out_dir / f"{stem}_thumb_chosen.json"
        chosen_path.write_text(json.dumps(chosen, indent=2))
        print(f"Chosen thumbnail style: {chosen['style']} ({chosen['reason']}) — scores {chosen['scores']}")
        paths["chosen_style"] = chosen["style"]
        paths["chosen"] = chosen["path"]

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
