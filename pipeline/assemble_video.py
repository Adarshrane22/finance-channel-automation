"""
Assembles the final video with retention-focused visual techniques layered
on top of the base gradient/audio: karaoke-style word-highlight captions,
animated stat callouts, section pop-in labels, and a progress bar. These
are the specific things short-form/explainer channels lean on to keep
viewers watching past the first 15-30 seconds, where most drop-off
happens:

  - Karaoke captions (current word highlighted) hold attention better than
    static caption blocks — the eye has something to track in sync with
    the voice, which is why most high-retention finance/explainer content
    uses this style rather than plain subtitles.
  - Stat callouts are a deliberate pattern interrupt: a brief, punchy
    full-screen number pop whenever the script hits a standout figure,
    breaking up what would otherwise be a visually static video and
    re-grabbing attention right when the most important information lands.
  - Section pop-in labels ("Point 2 of 5") give viewers a sense of
    progress through the video, which correlates with people sticking
    around to see the rest — similar to a progress bar, just spoken in
    the language of the content structure.
  - The progress bar is the same idea made constant/ambient rather than
    momentary.

None of this needs external assets — everything is rendered from the
script's own text and structure, so it works the same way for every video
without hand-authoring per-episode.

Usage:
  python assemble_video.py <audio.mp3> <word_events.json> <parsed_script.json> <output.mp4> [brand_hex] [--draft]

  word_events.json: either the *_captions.json from generate_voiceover.py
  (real per-word timing) or an estimated one from build_captions.py
  --estimate (see that script) — either works, real timing is just more
  accurate.

  --draft: renders at 15fps with ffmpeg's "ultrafast" preset instead of
  30fps/"medium". A full-length render at full quality can take a while
  (compositing this many overlay layers per frame — captions, callouts,
  progress bar — isn't free); draft mode is for quickly checking that
  timing/layout looks right before committing to the slower final pass.
  Resolution stays at 1920x1080 either way since the karaoke/stat-callout
  layout is tuned to that canvas.
"""
import re
import sys
from pathlib import Path
import json

from moviepy import (
    AudioFileClip, CompositeVideoClip, ImageClip, VideoClip, VideoFileClip, vfx,
)
from PIL import Image, ImageDraw, ImageFont
import numpy as np

WIDTH, HEIGHT = 1920, 1080
FONT_BOLD = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"
ACCENT_YELLOW = (255, 209, 0)
CAPTION_FONT_SIZE = 60
WORDS_PER_LINE = 7


def hex_to_rgb(hex_color: str):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def build_background(duration: float, brand_rgb) -> VideoClip:
    """Precomputes the static diagonal gradient once (this used to be
    recomputed from scratch, pixel by pixel, on every single output
    frame — for a multi-minute video at 30fps that was the single biggest
    render-time cost in the whole pipeline). The gentle phase drift is
    applied as a cheap per-frame remap of the precomputed base instead."""
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


def build_progress_bar(duration: float) -> VideoClip:
    bar_h = 10

    def make_frame(t):
        frame = np.zeros((bar_h, WIDTH, 3), dtype=np.uint8)
        filled = int(WIDTH * min(t / duration, 1.0))
        frame[:, :filled] = ACCENT_YELLOW
        frame[:, filled:] = (255, 255, 255)
        return frame

    return VideoClip(make_frame, duration=duration).with_position((0, HEIGHT - bar_h)).with_opacity(0.85)


def group_words(word_events, words_per_line=WORDS_PER_LINE, section_boundaries=None):
    """Split the word stream into caption-line-sized chunks. If
    section_boundaries (word indices where a new script section starts)
    is given, a chunk never crosses one — otherwise the last word of one
    key point and the first word of the next can end up sharing a caption
    line, which reads oddly since they're not actually the same thought."""
    if not section_boundaries:
        return [word_events[i:i + words_per_line] for i in range(0, len(word_events), words_per_line)]

    boundaries = sorted(set([0, *section_boundaries, len(word_events)]))
    groups = []
    for b_start, b_end in zip(boundaries, boundaries[1:]):
        segment = word_events[b_start:b_end]
        groups.extend(segment[i:i + words_per_line] for i in range(0, len(segment), words_per_line))
    return groups


def render_caption_line(words, active_idx, font):
    """Renders one caption line as an RGBA image, with the currently-
    spoken word in accent yellow and the rest in white — this is what
    creates the karaoke effect frame by frame."""
    tmp_img = Image.new("RGBA", (WIDTH, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tmp_img)

    texts = [w["text"] for w in words]
    widths = [draw.textbbox((0, 0), t + " ", font=font)[2] for t in texts]
    total_w = sum(widths)
    start_x = (WIDTH - total_w) // 2
    y = 40

    x = start_x
    for i, (t, w) in enumerate(zip(texts, widths)):
        color = ACCENT_YELLOW if i == active_idx else (255, 255, 255)
        for dx, dy in [(-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2), (-2, 2), (2, -2)]:
            draw.text((x + dx, y + dy), t, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), t, font=font, fill=(*color, 255))
        x += w

    bbox = tmp_img.getbbox()
    if bbox:
        tmp_img = tmp_img.crop((0, bbox[1], WIDTH, bbox[3]))
    return np.array(tmp_img)


def build_karaoke_captions(word_events, video_duration: float, section_boundaries=None):
    """One VideoClip per caption LINE (not per word) — each line's
    make_frame picks the active word on the fly from the current time, and
    a small in-process cache avoids re-rendering the same frame twice in a
    row. Keeping the clip count down to ~1 per line (rather than 1 per
    word) matters a lot for render speed: MoviePy's compositor re-checks
    every clip's active window on every output frame, so a few hundred
    word-level clips for a full-length script made rendering impractically
    slow. Line-level clips cut that count by ~7x (WORDS_PER_LINE) while
    producing the identical visual karaoke effect."""
    font = ImageFont.truetype(FONT_BOLD, CAPTION_FONT_SIZE)
    clips = []
    for line_words in group_words(word_events, section_boundaries=section_boundaries):
        if not line_words:
            continue
        line_start = line_words[0]["start_s"]
        line_end = min(line_words[-1]["start_s"] + line_words[-1]["duration_s"], video_duration)
        if line_end <= line_start:
            continue

        cache = {}

        def make_frame(t, _line_words=line_words, _line_start=line_start):
            local_t = t + _line_start
            active_idx = 0
            for i, w in enumerate(_line_words):
                if w["start_s"] <= local_t < w["start_s"] + w["duration_s"]:
                    active_idx = i
                    break
                if local_t >= w["start_s"]:
                    active_idx = i
            if active_idx not in cache:
                cache[active_idx] = render_caption_line(_line_words, active_idx, font)[:, :, :3]
            return cache[active_idx]

        def make_mask(t, _line_words=line_words, _line_start=line_start):
            local_t = t + _line_start
            active_idx = 0
            for i, w in enumerate(_line_words):
                if w["start_s"] <= local_t < w["start_s"] + w["duration_s"]:
                    active_idx = i
                    break
                if local_t >= w["start_s"]:
                    active_idx = i
            key = f"mask_{active_idx}"
            if key not in cache:
                cache[key] = render_caption_line(_line_words, active_idx, font)[:, :, 3] / 255.0
            return cache[key]

        clip = (
            VideoClip(make_frame, duration=line_end - line_start)
            .with_start(line_start)
            .with_position(("center", HEIGHT - 260))
        )
        mask_clip = VideoClip(make_mask, duration=line_end - line_start, is_mask=True)
        clip = clip.with_mask(mask_clip)
        clips.append(clip)
    return clips


def compute_section_times(sections, word_events):
    """Maps each script section (hook, key_point_1, ...) onto a time range
    by counting off that many words from the synced word-timing stream.
    Approximate (TTS word-splitting doesn't always match text.split()
    exactly) but close enough for pop-in timing, which doesn't need
    frame-perfect precision."""
    times = []
    idx = 0
    for s in sections:
        n = max(1, len(s["text"].split()))
        chunk = word_events[idx:idx + n]
        if not chunk:
            break
        start = chunk[0]["start_s"]
        end = chunk[-1]["start_s"] + chunk[-1]["duration_s"]
        times.append({**s, "start_s": start, "end_s": end})
        idx += n
    return times


def build_broll_layer(section_times, broll_dir: Path, credits: list, video_duration: float):
    """Real stock footage (fetched by fetch_broll.py) behind the captions/
    callouts, for whichever sections it found a match for — sections
    without one just keep the gradient background showing through
    underneath, since this list is laid on top of `background`, not
    instead of it. Each clip is muted (the voiceover is the only audio),
    dimmed with a translucent black overlay so white captions stay
    readable over busy footage, and looped or trimmed to exactly fill
    that section's time range."""
    if not broll_dir or not broll_dir.exists() or not credits:
        return []

    credit_by_section = {c["section_index"]: c for c in credits}
    clips = []
    for idx, s in enumerate(section_times):
        credit = credit_by_section.get(idx)
        if not credit:
            continue
        clip_path = broll_dir.parent / credit["file"]
        if not clip_path.exists():
            continue

        start, end = s["start_s"], min(s["end_s"], video_duration)
        seg_duration = end - start
        if seg_duration <= 0:
            continue

        try:
            raw = VideoFileClip(str(clip_path)).without_audio()
        except Exception as e:
            print(f"  WARNING: could not load broll clip {clip_path}: {e}")
            continue

        # Loop short clips, trim long ones, so the segment exactly fills
        # this section's slot in the timeline.
        if raw.duration < seg_duration:
            raw = raw.with_effects([vfx.Loop(duration=seg_duration)])
        else:
            raw = raw.subclipped(0, seg_duration)

        # Fill-crop to the full 1920x1080 canvas regardless of the source
        # clip's native aspect ratio (Pexels footage varies).
        raw = raw.with_effects([vfx.Resize(height=HEIGHT)])
        if raw.w < WIDTH:
            raw = raw.with_effects([vfx.Resize(width=WIDTH)])
        raw = raw.with_effects([vfx.Crop(x_center=raw.w / 2, y_center=raw.h / 2, width=WIDTH, height=HEIGHT)])

        dim = ImageClip(np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)).with_duration(seg_duration).with_opacity(0.35)
        segment = (
            CompositeVideoClip([raw, dim], size=(WIDTH, HEIGHT))
            .with_start(start)
            .with_duration(seg_duration)
            .with_effects([vfx.CrossFadeIn(0.3)])
        )
        clips.append(segment)
    return clips


def render_stat_callout(stat: str) -> np.ndarray:
    """Just the number, deliberately — no label text. Earlier versions
    also printed a text summary of the section under the number, but that
    duplicated the karaoke captions running at the same time right below
    it and made the frame look cluttered/redundant. The number alone,
    positioned in the upper-middle of the frame, reads as a clean pattern
    interrupt without competing with the caption line."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    stat_font = ImageFont.truetype(FONT_BOLD, 240)

    box_top, box_bottom = int(HEIGHT * 0.22), int(HEIGHT * 0.52)
    draw.rectangle([0, box_top, WIDTH, box_bottom], fill=(0, 0, 0, 150))

    bbox = draw.textbbox((0, 0), stat, font=stat_font)
    x = (WIDTH - (bbox[2] - bbox[0])) // 2
    y = box_top + (box_bottom - box_top - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), stat, font=stat_font, fill=(*ACCENT_YELLOW, 255))

    return np.array(img)


def build_stat_callouts(section_times, video_duration, hold_s=2.0, skip_before_s=0.0):
    """skip_before_s pushes any callout that would otherwise start while
    the title card is still on screen back to right after it clears,
    rather than dropping it — the hook's stat is often the single most
    important number in the script, so delaying beats losing it."""
    clips = []
    for s in section_times:
        if not s.get("stat"):
            continue
        start = max(s["start_s"], skip_before_s)
        end = min(start + hold_s, video_duration)
        if end <= start:
            continue
        frame = render_stat_callout(s["stat"])
        clip = (
            ImageClip(frame)
            .with_start(start)
            .with_duration(end - start)
            .with_effects([vfx.CrossFadeIn(0.2), vfx.CrossFadeOut(0.3)])
        )
        clips.append(clip)
    return clips


def render_section_tag(text: str) -> np.ndarray:
    img = Image.new("RGBA", (700, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_BOLD, 42)
    draw.rounded_rectangle([0, 0, 699, 90], radius=18, fill=(0, 0, 0, 160))
    draw.text((30, 22), text.upper(), font=font, fill=(*ACCENT_YELLOW, 255))
    return np.array(img)


def build_section_tags(section_times, video_duration, hold_s=1.8):
    clips = []
    key_point_sections = [s for s in section_times if s["name"].startswith("key_point")]
    total = len(key_point_sections)
    for i, s in enumerate(key_point_sections, start=1):
        start = s["start_s"]
        end = min(start + hold_s, video_duration)
        if end <= start:
            continue
        frame = render_section_tag(f"Point {i} of {total}")
        clip = (
            ImageClip(frame)
            .with_start(start)
            .with_duration(end - start)
            .with_position((60, 60))
            .with_effects([vfx.CrossFadeIn(0.15), vfx.CrossFadeOut(0.3)])
        )
        clips.append(clip)
    return clips


TITLE_CARD_DURATION = 2.5


def build_title_card(title: str, duration: float = TITLE_CARD_DURATION):
    """A compact banner in the upper third, not a full-screen takeover —
    it needs to coexist with captions and stat callouts that can start
    firing within the same first couple of seconds (the hook is often
    where the most important stat in the whole script lives), so it stays
    out of the center/bottom two-thirds of the frame entirely."""
    from moviepy import TextClip
    return (
        TextClip(
            text=title, font=FONT_BOLD, font_size=52, color="white",
            size=(int(WIDTH * 0.7), None), method="caption", text_align="center",
        )
        .with_start(0)
        .with_duration(duration)
        .with_position(("center", 70))
        .with_effects([vfx.CrossFadeIn(0.3), vfx.CrossFadeOut(0.4)])
    )


def main():
    if len(sys.argv) < 5:
        print("Usage: python assemble_video.py <audio.mp3> <word_events.json> <parsed_script.json> <output.mp4> [brand_hex]")
        sys.exit(1)

    audio_path, word_events_path, parsed_json_path, out_path = sys.argv[1:5]
    rest = sys.argv[5:]
    draft = "--draft" in rest
    rest = [a for a in rest if a != "--draft"]

    broll_dir = None
    if "--broll-dir" in rest:
        i = rest.index("--broll-dir")
        broll_dir = Path(rest[i + 1])
        rest = rest[:i] + rest[i + 2:]

    brand_hex = rest[0] if rest else "1F6FEB"
    brand_rgb = hex_to_rgb(brand_hex)

    audio = AudioFileClip(audio_path)
    duration = audio.duration

    word_events = json.loads(Path(word_events_path).read_text())
    parsed = json.loads(Path(parsed_json_path).read_text())
    sections = parsed.get("sections", [])
    section_times = compute_section_times(sections, word_events) if sections else []

    section_boundaries = []
    idx = 0
    for s in sections:
        section_boundaries.append(idx)
        idx += max(1, len(s["text"].split()))

    background = build_background(duration, brand_rgb)

    broll_clips = []
    if broll_dir:
        credits_path = broll_dir.parent / "broll_credits.json"
        broll_credits = json.loads(credits_path.read_text()) if credits_path.exists() else []
        broll_clips = build_broll_layer(section_times, broll_dir, broll_credits, duration)

    title_card = build_title_card(parsed["title"])
    caption_clips = build_karaoke_captions(word_events, duration, section_boundaries=section_boundaries)
    stat_clips = build_stat_callouts(section_times, duration, skip_before_s=TITLE_CARD_DURATION - 0.5)
    tag_clips = build_section_tags(section_times, duration)
    progress_bar = build_progress_bar(duration)

    layers = [background, *broll_clips, title_card, *caption_clips, *stat_clips, *tag_clips, progress_bar]
    final = CompositeVideoClip(layers, size=(WIDTH, HEIGHT))
    final = final.with_audio(audio).with_duration(duration)

    final.write_videofile(
        out_path, fps=15 if draft else 30, codec="libx264", audio_codec="aac",
        threads=4, preset="ultrafast" if draft else "medium",
    )


if __name__ == "__main__":
    main()
