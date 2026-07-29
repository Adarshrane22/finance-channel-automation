"""
Assembles a vertical (1080x1920, 9:16) YouTube Short from one of the
30/60/15-second scripts research_and_script.py's Stage B marketing
package already generates for every long-form video — so this adds
almost no new research/scripting cost, it's a second, shorter edit of
content that's already fact-checked and already exists.

Why a separate file rather than reusing assemble_video.py: that file's
WIDTH/HEIGHT/caption layout/intro-outro are all built around a 1920x1080
landscape canvas and multi-minute runtime. Rather than retrofit those
module-level constants to support two aspect ratios (real risk of
breaking the already-working long-form pipeline for a marginal amount of
shared code), this is a small, self-contained sibling with its own
vertical-appropriate constants: bigger/bolder captions positioned in the
safe middle zone (avoiding the top/bottom ~15-18% that YouTube's own
Shorts player UI — profile, like/comment/share buttons, description —
overlays on top of the video), no branded intro/outro (there's no
runtime budget for a 4s+6s bookend on a 15-60s video), just a lightweight
watermark and an end-card follow prompt.

Usage:
  python assemble_short.py <audio.mp3> <word_events.json> <output.mp4> [brand_hex] [--draft]

word_events.json is the same format generate_voiceover.py already
produces (a list of {text, start_s, duration_s}) — this script doesn't
care whether the audio came from a full script or a short one.
"""
import sys
from pathlib import Path

from moviepy import AudioFileClip, CompositeVideoClip, VideoClip
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import json

WIDTH, HEIGHT = 1080, 1920
FONT_BOLD = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
ACCENT_YELLOW = (255, 209, 0)
CAPTION_FONT_SIZE = 92
WORDS_PER_LINE = 3
CHANNEL_WATERMARK = "THE RATE REPORT"
FOLLOW_TEXT = "Follow for daily market updates"

# YouTube's own Shorts player UI (profile pic, like/comment/share rail,
# caption/description) sits in these bands — keep captions and other
# overlays out of them so nothing gets covered.
SAFE_TOP = int(HEIGHT * 0.16)
SAFE_BOTTOM = int(HEIGHT * 0.78)


def hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def build_background(duration: float, brand_rgb) -> VideoClip:
    """Same precomputed-gradient-plus-cheap-per-frame-remap technique as
    assemble_video.py's build_background, just at vertical dimensions —
    duplicated rather than imported so this file has zero dependency on
    assemble_video.py's module-level WIDTH/HEIGHT globals."""
    dark = tuple(max(0, c - 60) for c in brand_rgb)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    base_phase = ((xx + yy) / (WIDTH + HEIGHT)).astype(np.float32)
    dark_arr = np.array(dark, dtype=np.float32)
    brand_arr = np.array(brand_rgb, dtype=np.float32)

    def make_frame(t):
        phase = np.clip(base_phase + 0.02 * np.sin(t / 8), 0, 1)
        frame = dark_arr[None, None, :] + (brand_arr - dark_arr)[None, None, :] * phase[:, :, None]
        return frame.astype(np.uint8)

    return VideoClip(make_frame, duration=duration)


def group_words(word_events, words_per_line=WORDS_PER_LINE):
    return [word_events[i:i + words_per_line] for i in range(0, len(word_events), words_per_line)]


def render_caption_line(words, active_idx, font):
    """Same karaoke-style render as assemble_video.py's
    render_caption_line, sized for the vertical canvas and a bigger font
    — Shorts captions need to read at a glance on a phone screen, so
    they're bolder and fewer words per line than the long-form captions."""
    tmp_img = Image.new("RGBA", (WIDTH, 400), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tmp_img)

    texts = [w["text"] for w in words]
    widths = [draw.textbbox((0, 0), t + " ", font=font)[2] for t in texts]
    total_w = sum(widths)
    start_x = max(20, (WIDTH - total_w) // 2)
    y = 60

    x = start_x
    for i, (t, w) in enumerate(zip(texts, widths)):
        color = ACCENT_YELLOW if i == active_idx else (255, 255, 255)
        for dx, dy in [(-4, 0), (4, 0), (0, -4), (0, 4), (-3, -3), (3, 3), (-3, 3), (3, -3)]:
            draw.text((x + dx, y + dy), t, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), t, font=font, fill=(*color, 255))
        x += w

    bbox = tmp_img.getbbox()
    if bbox:
        tmp_img = tmp_img.crop((0, bbox[1], WIDTH, bbox[3]))
    return np.array(tmp_img)


def build_karaoke_captions(word_events, video_duration: float):
    font = ImageFont.truetype(FONT_BOLD, CAPTION_FONT_SIZE)
    clips = []
    caption_y = (SAFE_TOP + SAFE_BOTTOM) // 2 - 100  # vertically centered in the UI-safe middle band

    for line_words in group_words(word_events):
        if not line_words:
            continue
        line_start = line_words[0]["start_s"]
        line_end = min(line_words[-1]["start_s"] + line_words[-1]["duration_s"], video_duration)
        if line_end <= line_start:
            continue

        # See assemble_video.py's build_karaoke_captions for why `cache`
        # must be a default argument, not a closed-over free variable —
        # same late-binding trap applies here.
        cache = {}

        def make_frame(t, _line_words=line_words, _line_start=line_start, _cache=cache):
            local_t = t + _line_start
            active_idx = 0
            for i, w in enumerate(_line_words):
                if w["start_s"] <= local_t < w["start_s"] + w["duration_s"]:
                    active_idx = i
                    break
                if local_t >= w["start_s"]:
                    active_idx = i
            if active_idx not in _cache:
                _cache[active_idx] = render_caption_line(_line_words, active_idx, font)[:, :, :3]
            return _cache[active_idx]

        def make_mask(t, _line_words=line_words, _line_start=line_start, _cache=cache):
            local_t = t + _line_start
            active_idx = 0
            for i, w in enumerate(_line_words):
                if w["start_s"] <= local_t < w["start_s"] + w["duration_s"]:
                    active_idx = i
                    break
                if local_t >= w["start_s"]:
                    active_idx = i
            key = f"mask_{active_idx}"
            if key not in _cache:
                _cache[key] = render_caption_line(_line_words, active_idx, font)[:, :, 3] / 255.0
            return _cache[key]

        clip = (
            VideoClip(make_frame, duration=line_end - line_start)
            .with_start(line_start)
            .with_position(("center", caption_y))
        )
        mask_clip = VideoClip(make_mask, duration=line_end - line_start, is_mask=True)
        clip = clip.with_mask(mask_clip)
        clips.append(clip)
    return clips


def build_watermark(duration: float) -> VideoClip:
    """Small, unobtrusive channel name near the top-safe-zone boundary —
    branding without competing with the captions for attention."""
    font = ImageFont.truetype(FONT_BOLD, 40)
    img = Image.new("RGBA", (WIDTH, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), CHANNEL_WATERMARK, font=font)
    x = (WIDTH - (bbox[2] - bbox[0])) // 2
    for dx, dy in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
        draw.text((x + dx, 20 + dy), CHANNEL_WATERMARK, font=font, fill=(0, 0, 0, 200))
    draw.text((x, 20), CHANNEL_WATERMARK, font=font, fill=(255, 255, 255, 230))
    arr = np.array(img)

    clip = VideoClip(lambda t: arr[:, :, :3], duration=duration).with_position(("center", SAFE_TOP - 90))
    mask = VideoClip(lambda t: arr[:, :, 3] / 255.0, duration=duration, is_mask=True)
    return clip.with_mask(mask)


def build_follow_endcard(total_duration: float, hold_s: float = 2.2) -> VideoClip:
    """A brief 'Follow for daily market updates' prompt over the last
    couple seconds — Shorts' own UI already has a prominent
    subscribe/follow button, so this is a nudge rather than a full
    branded outro (there's no time budget for one on a 15-60s video)."""
    start = max(0.0, total_duration - hold_s)
    dur = total_duration - start
    if dur <= 0:
        return None

    font = ImageFont.truetype(FONT_BOLD, 64)
    img = Image.new("RGBA", (WIDTH, 200), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), FOLLOW_TEXT, font=font)
    tw = bbox[2] - bbox[0]
    # Wrap onto two lines if the phrase is too wide for the vertical canvas.
    if tw > WIDTH - 80:
        words = FOLLOW_TEXT.split()
        mid = len(words) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
    else:
        lines = [FOLLOW_TEXT]

    y = 20
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (WIDTH - (bbox[2] - bbox[0])) // 2
        for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3)]:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 220))
        draw.text((x, y), line, font=font, fill=(*ACCENT_YELLOW, 255))
        y += 76
    arr = np.array(img)

    clip = (
        VideoClip(lambda t: arr[:, :, :3], duration=dur)
        .with_start(start)
        .with_position(("center", SAFE_BOTTOM - 40))
    )
    mask = VideoClip(lambda t: arr[:, :, 3] / 255.0, duration=dur, is_mask=True)
    return clip.with_mask(mask)


def assemble(audio_path: str, word_events_path: str, out_path: str, brand_hex: str = "1F6FEB", draft: bool = False):
    brand_rgb = hex_to_rgb(brand_hex)
    audio = AudioFileClip(audio_path)
    duration = audio.duration

    word_events = json.loads(Path(word_events_path).read_text())

    background = build_background(duration, brand_rgb)
    caption_clips = build_karaoke_captions(word_events, duration)
    watermark = build_watermark(duration)
    endcard = build_follow_endcard(duration)

    layers = [background, watermark, *caption_clips]
    if endcard:
        layers.append(endcard)

    final = CompositeVideoClip(layers, size=(WIDTH, HEIGHT))
    final = final.with_audio(audio).with_duration(duration)

    final.write_videofile(
        out_path, fps=15 if draft else 30, codec="libx264", audio_codec="aac",
        threads=4, preset="ultrafast" if draft else "medium",
    )


def main():
    if len(sys.argv) < 4:
        print("Usage: python assemble_short.py <audio.mp3> <word_events.json> <output.mp4> [brand_hex] [--draft]")
        sys.exit(1)
    audio_path, word_events_path, out_path = sys.argv[1:4]
    rest = sys.argv[4:]
    draft = "--draft" in rest
    rest = [a for a in rest if a != "--draft"]
    brand_hex = rest[0] if rest else "1F6FEB"

    assemble(audio_path, word_events_path, out_path, brand_hex, draft)


if __name__ == "__main__":
    main()
