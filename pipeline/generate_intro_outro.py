"""
Procedurally-generated, fully original intro and outro clips for The Rate
Report. Everything here — background, animated ascending-line-chart motif,
text, and even the short audio chime — is rendered from code, not a single
external logo/music/footage asset. That's a deliberate compliance choice
(nothing to license, nothing that could ever trigger a copyright claim)
as much as a practical one (nothing to keep in sync by hand across videos).

Visually it reuses the exact same gradient background, font, and accent
color as the rest of assemble_video.py, so the branded open/close reads as
one continuous system rather than a mismatched intro bolted onto a
differently-styled video.

Usage (standalone preview — writes two short mp4s so you can see them
without rendering a full video):
  python generate_intro_outro.py <output_dir> [brand_hex]

Normally you don't call this directly — assemble_video.py imports
build_intro_clip/build_outro_clip and concatenates them onto every video
automatically (see its --no-intro-outro flag to disable per-run).
"""
import sys
from pathlib import Path

import numpy as np
from moviepy import AudioClip, AudioFileClip, CompositeAudioClip, TextClip, VideoClip, vfx
from PIL import Image, ImageDraw

from moviepy import CompositeVideoClip

from assemble_video import ACCENT_YELLOW, FONT_BOLD, HEIGHT, WIDTH, build_background, hex_to_rgb


def _text_clip(text, font_size, color, width_frac=0.85, stroke_color=None, stroke_width=0):
    """Thin wrapper around TextClip that works around a real MoviePy/Pillow
    quirk confirmed by direct testing: method="caption" with size=(w, None)
    auto-computes a height from font metrics that's too short specifically
    for ALL-CAPS text in this font (Poppins Bold) — it visibly clips the
    top of every capital letter, even though the exact same call with
    normal mixed-case text (e.g. assemble_video.py's build_title_card)
    renders perfectly fine. Explicitly forcing a generous height instead of
    letting it auto-size fixes it. Isolated with a save_frame() test before
    this fix was applied; see intro/outro render inspection in this
    session's history if you need to reproduce it."""
    height = int(font_size * (1.7 if stroke_width else 1.5))
    return TextClip(
        text=text, font=FONT_BOLD, font_size=font_size, color=color,
        stroke_color=stroke_color, stroke_width=stroke_width,
        size=(int(WIDTH * width_frac), height), method="caption", text_align="center",
    )

CHANNEL_NAME = "THE RATE REPORT"
TAGLINE = "Daily US Finance News, Explained"
OUTRO_LINE_1 = "SUBSCRIBE"
OUTRO_LINE_2 = "New finance videos daily"
DISCLAIMER = "The Rate Report — informational purposes only, not financial advice."

INTRO_DURATION = 4.0
OUTRO_DURATION = 6.0
AUDIO_FPS = 44100


# ----------------------------------------------------------------------------
# Synthesized audio (pure sine-wave chimes — original, nothing licensed)
# ----------------------------------------------------------------------------

def _chime(duration, notes, note_len, gap, fps=AUDIO_FPS):
    """Shared synthesis core for both chimes: N short sine-wave notes in
    sequence, each with its own tiny attack/decay envelope so they don't
    click at the edges. Returns a stereo AudioClip."""
    def frame_function(t):
        t_arr = np.atleast_1d(np.asarray(t, dtype=np.float64))
        wave = np.zeros_like(t_arr)
        for i, f in enumerate(notes):
            start = i * gap
            local = t_arr - start
            active = (local >= 0) & (local < note_len)
            local_clamped = np.clip(local, 0, None)
            envelope = np.clip(local_clamped / 0.015, 0, 1) * np.clip((note_len - local_clamped) / 0.12, 0, 1)
            wave += np.where(active, 0.22 * np.sin(2 * np.pi * f * local_clamped) * envelope, 0.0)
        stereo = np.stack([wave, wave], axis=-1)
        return stereo if np.asarray(t).ndim > 0 else stereo[0]

    return AudioClip(frame_function, duration=duration, fps=fps)


def synth_intro_chime(duration=INTRO_DURATION):
    """Three-note ascending chime — 'things are looking up'."""
    return _chime(duration, notes=(440.0, 554.37, 659.25), note_len=0.32, gap=0.16)


def synth_outro_chime(duration=OUTRO_DURATION):
    """Two-note descending resolving chime — a clean 'that's a wrap' cue."""
    return _chime(duration, notes=(659.25, 523.25), note_len=0.45, gap=0.22)


# ----------------------------------------------------------------------------
# Animated ascending line-chart motif (shared visual signature)
# ----------------------------------------------------------------------------

def _chart_points(n=40):
    """A deterministic gentle uptrend with some noise — always the same
    shape run to run (no RNG dependency to accidentally vary output), reads
    unambiguously as 'a market going up' without depicting any real data."""
    xs = np.linspace(0, 1, n)
    trend = xs ** 1.3
    wiggle = 0.05 * np.sin(xs * 14) + 0.03 * np.sin(xs * 27 + 1.3)
    ys = trend + wiggle
    ys = (ys - ys.min()) / (ys.max() - ys.min())
    return xs, ys


def _draw_chart(progress: float, chart_x0, chart_y0, chart_w, chart_h, xs, ys):
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    if progress <= 0:
        return img
    draw = ImageDraw.Draw(img)
    n_show = max(2, int(len(xs) * progress))
    px = chart_x0 + xs[:n_show] * chart_w
    py = chart_y0 + chart_h - ys[:n_show] * chart_h
    pts = list(zip(px.tolist(), py.tolist()))
    fill_pts = pts + [(pts[-1][0], chart_y0 + chart_h), (pts[0][0], chart_y0 + chart_h)]
    draw.polygon(fill_pts, fill=(*ACCENT_YELLOW, 45))
    draw.line(pts, fill=(*ACCENT_YELLOW, 255), width=6, joint="curve")
    r = 9
    draw.ellipse([pts[-1][0] - r, pts[-1][1] - r, pts[-1][0] + r, pts[-1][1] + r], fill=(*ACCENT_YELLOW, 255))
    return img


# ----------------------------------------------------------------------------
# Intro
# ----------------------------------------------------------------------------

def build_intro_clip(brand_hex="1F6FEB", channel_name=CHANNEL_NAME, tagline=TAGLINE, duration=INTRO_DURATION, with_audio=True):
    brand_rgb = hex_to_rgb(brand_hex)
    bg = build_background(duration, brand_rgb)

    xs, ys = _chart_points()
    chart_w, chart_h = int(WIDTH * 0.62), int(HEIGHT * 0.28)
    chart_x0, chart_y0 = (WIDTH - chart_w) // 2, int(HEIGHT * 0.62)
    draw_finish_frac = 0.7  # the chart finishes drawing itself 70% through the intro

    def chart_frame(t):
        progress = np.clip(t / (duration * draw_finish_frac), 0, 1)
        return np.array(_draw_chart(progress, chart_x0, chart_y0, chart_w, chart_h, xs, ys))

    chart_clip = VideoClip(chart_frame, duration=duration, has_constant_size=True)
    chart_clip = chart_clip.with_mask(
        VideoClip(lambda t: np.array(_draw_chart(np.clip(t / (duration * draw_finish_frac), 0, 1),
                                                   chart_x0, chart_y0, chart_w, chart_h, xs, ys))[:, :, 3] / 255.0,
                  duration=duration, is_mask=True)
    )

    name_clip = (
        _text_clip(channel_name, 88, "white", width_frac=0.85, stroke_color="black", stroke_width=3)
        .with_start(0.1).with_duration(max(0.1, duration - 0.1))
        .with_position(("center", int(HEIGHT * 0.32)))
        .with_effects([vfx.CrossFadeIn(0.5)])
    )
    tagline_clip = (
        _text_clip(tagline, 36, "#FFD100", width_frac=0.7)
        .with_start(0.6).with_duration(max(0.1, duration - 0.6))
        .with_position(("center", int(HEIGHT * 0.46)))
        .with_effects([vfx.CrossFadeIn(0.5)])
    )

    layers = [bg, chart_clip, name_clip, tagline_clip]
    final = CompositeVideoClip(layers, size=(WIDTH, HEIGHT)).with_duration(duration)
    if with_audio:
        final = final.with_audio(synth_intro_chime(duration))
    return final


# ----------------------------------------------------------------------------
# Outro
# ----------------------------------------------------------------------------

def build_outro_clip(brand_hex="1F6FEB", channel_name=CHANNEL_NAME, duration=OUTRO_DURATION, with_audio=True):
    brand_rgb = hex_to_rgb(brand_hex)
    bg = build_background(duration, brand_rgb)

    # A simple animated "ring fill" behind a bell shape, sweeping a full
    # circle over the clip's duration — a lightweight, fully-original stand-
    # in for a subscribe-bell animation, no icon asset needed.
    ring_cx, ring_cy, ring_r = WIDTH // 2, int(HEIGHT * 0.22), 80

    def ring_frame(t):
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        sweep_deg = 360 * min(t / (duration * 0.6), 1.0)
        draw.arc([ring_cx - ring_r, ring_cy - ring_r, ring_cx + ring_r, ring_cy + ring_r],
                  start=-90, end=-90 + sweep_deg, fill=(*ACCENT_YELLOW, 255), width=10)
        # simple bell glyph: a trapezoid body + circle clapper, in white
        bw = 46
        draw.polygon([
            (ring_cx - bw * 0.5, ring_cy + 30), (ring_cx + bw * 0.5, ring_cy + 30),
            (ring_cx + bw * 0.85, ring_cy - 35), (ring_cx - bw * 0.85, ring_cy - 35),
        ], fill=(255, 255, 255, 255))
        draw.ellipse([ring_cx - 12, ring_cy + 28, ring_cx + 12, ring_cy + 52], fill=(255, 255, 255, 255))
        draw.rectangle([ring_cx - 6, ring_cy - 50, ring_cx + 6, ring_cy - 35], fill=(255, 255, 255, 255))
        return img

    ring_clip = VideoClip(lambda t: np.array(ring_frame(t))[:, :, :3], duration=duration, has_constant_size=True)
    ring_clip = ring_clip.with_mask(VideoClip(lambda t: np.array(ring_frame(t))[:, :, 3] / 255.0, duration=duration, is_mask=True))

    subscribe_clip = (
        _text_clip(OUTRO_LINE_1, 110, "white", width_frac=0.9, stroke_color="black", stroke_width=4)
        .with_start(0.3).with_duration(max(0.1, duration - 0.3))
        .with_position(("center", int(HEIGHT * 0.38)))
        .with_effects([vfx.CrossFadeIn(0.4)])
    )
    subline_clip = (
        _text_clip(OUTRO_LINE_2, 42, "#FFD100", width_frac=0.8)
        .with_start(0.7).with_duration(max(0.1, duration - 0.7))
        .with_position(("center", int(HEIGHT * 0.58)))
        .with_effects([vfx.CrossFadeIn(0.4)])
    )
    name_clip = (
        _text_clip(channel_name, 44, "white", width_frac=0.8)
        .with_start(1.0).with_duration(max(0.1, duration - 1.0))
        .with_position(("center", int(HEIGHT * 0.70)))
        .with_effects([vfx.CrossFadeIn(0.4)])
    )
    disclaimer_clip = (
        _text_clip(DISCLAIMER, 24, "white", width_frac=0.9)
        .with_start(1.2).with_duration(max(0.1, duration - 1.2))
        .with_position(("center", HEIGHT - 80))
        .with_effects([vfx.CrossFadeIn(0.4)])
    )

    layers = [bg, ring_clip, subscribe_clip, subline_clip, name_clip, disclaimer_clip]
    final = CompositeVideoClip(layers, size=(WIDTH, HEIGHT)).with_duration(duration)
    if with_audio:
        final = final.with_audio(synth_outro_chime(duration))
    return final


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("intro_outro_preview")
    brand_hex = sys.argv[2] if len(sys.argv) > 2 else "1F6FEB"
    out_dir.mkdir(parents=True, exist_ok=True)

    intro = build_intro_clip(brand_hex=brand_hex)
    intro.write_videofile(str(out_dir / "intro.mp4"), fps=30, codec="libx264", audio_codec="aac", threads=4, preset="medium")

    outro = build_outro_clip(brand_hex=brand_hex)
    outro.write_videofile(str(out_dir / "outro.mp4"), fps=30, codec="libx264", audio_codec="aac", threads=4, preset="medium")

    print(f"Wrote {out_dir / 'intro.mp4'} and {out_dir / 'outro.mp4'}")


if __name__ == "__main__":
    main()
