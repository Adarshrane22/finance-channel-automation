"""
Pre-upload gate: technical quality checks (does the rendered video/audio
actually look and sound right) plus a copyright/licensing check (was
everything used here something we're actually allowed to use).

Why this exists: in the fully-unattended GitHub Actions cycle there's no
human watching the video before it uploads. The 6-hour publish delay
(see daily_cycle.py) is a safety margin for a human to catch problems,
but it only works if someone's actually looking — this check catches the
kind of failure a human would catch instantly (silent audio, a frozen/
blank render, a corrupt thumbnail) automatically, so a broken video
doesn't sit there quietly waiting for its publish time even if nobody
happens to check that day.

This is NOT a substitute for human spot-checks, and it can't verify
content accuracy (that's what the fact-checking in research_and_script.py
is for) — it verifies the render came out technically sound and that
nothing unlicensed snuck into the asset pipeline.

Exits 0 (pass) or 1 (fail) and writes <stem>_quality_report.json with the
full breakdown either way, so a failure is diagnosable from the GitHub
Actions artifact without re-running anything.

Usage:
  python quality_check.py <video_dir> <stem> <voice_used> [manifest_path]
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

EXPECTED_RESOLUTION = (1920, 1080)
EXPECTED_THUMBNAIL_SIZE = (1280, 720)
MIN_DURATION_S = 20  # a real script should never render this short — catches truncated renders
MAX_DURATION_S = 1800  # sanity ceiling — catches runaway renders
SILENCE_MEAN_DB_THRESHOLD = -50.0
YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
YOUTUBE_TAGS_MAX_CHARS = 500

# Filenames the pipeline itself is known to produce for a given stem —
# anything else found in the video directory is an unaccounted-for asset
# and needs a manifest entry (see the copyright gate below).
def known_filenames(stem: str):
    return {
        f"{stem}.mp3", f"{stem}_captions.json", f"{stem}.srt", f"{stem}.mp4",
        f"{stem}_thumb_stat.jpg", f"{stem}_thumb_headline.jpg", f"{stem}_thumb_directional.jpg",
        f"{stem}_seo.json", f"{stem}_quality_report.json", "parsed.json", f"{stem}.json",
    }


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, passed, detail):
        self.checks.append({"check": name, "passed": bool(passed), "detail": detail})
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}: {detail}")

    @property
    def all_passed(self):
        return all(c["passed"] for c in self.checks)


def ffprobe_json(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    return json.loads(result.stdout) if result.stdout else {}


def check_av_streams(video_path: Path, report: Report):
    if not video_path.exists() or video_path.stat().st_size == 0:
        report.add("video_file_exists", False, f"{video_path} missing or empty")
        return None

    probe = ffprobe_json(video_path)
    streams = probe.get("streams", [])
    video_streams = [s for s in streams if s["codec_type"] == "video"]
    audio_streams = [s for s in streams if s["codec_type"] == "audio"]

    report.add("has_video_stream", len(video_streams) > 0, f"{len(video_streams)} video stream(s)")
    report.add("has_audio_stream", len(audio_streams) > 0, f"{len(audio_streams)} audio stream(s)")

    if video_streams:
        vs = video_streams[0]
        resolution = (int(vs.get("width", 0)), int(vs.get("height", 0)))
        report.add("resolution", resolution == EXPECTED_RESOLUTION, f"{resolution} (expected {EXPECTED_RESOLUTION})")

    duration = float(probe.get("format", {}).get("duration", 0))
    report.add(
        "duration_sane",
        MIN_DURATION_S <= duration <= MAX_DURATION_S,
        f"{duration:.1f}s (expected {MIN_DURATION_S}-{MAX_DURATION_S}s)",
    )
    return duration


def check_audio_not_silent(video_path: Path, report: Report):
    result = subprocess.run(
        ["ffmpeg", "-i", str(video_path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    mean_db = None
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            try:
                mean_db = float(line.split("mean_volume:")[1].strip().split(" ")[0])
            except (ValueError, IndexError):
                pass
    if mean_db is None:
        report.add("audio_not_silent", False, "could not measure volume — ffmpeg volumedetect returned nothing usable")
    else:
        report.add("audio_not_silent", mean_db > SILENCE_MEAN_DB_THRESHOLD, f"mean volume {mean_db} dB (fails below {SILENCE_MEAN_DB_THRESHOLD} dB)")


def check_video_not_blank_or_frozen(video_path: Path, duration: float, report: Report, tmp_dir: Path):
    if not duration:
        report.add("video_has_visible_content", False, "skipped — no duration available")
        return
    tmp_dir.mkdir(parents=True, exist_ok=True)
    sample_times = [duration * f for f in (0.2, 0.4, 0.6, 0.8)]
    frames = []
    for i, t in enumerate(sample_times):
        frame_path = tmp_dir / f"qc_frame_{i}.png"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", str(video_path), "-frames:v", "1", "-update", "1", str(frame_path)],
            capture_output=True,
        )
        if frame_path.exists():
            frames.append(np.array(Image.open(frame_path).convert("RGB")))

    if len(frames) < 2:
        report.add("video_has_visible_content", False, f"only extracted {len(frames)} sample frame(s), expected {len(sample_times)}")
        return

    stds = [float(f.std()) for f in frames]
    not_blank = all(s > 5.0 for s in stds)  # a real frame (gradient + text) has much more variance than a flat color
    report.add("frames_not_blank", not_blank, f"per-frame pixel std: {[round(s, 1) for s in stds]} (fails if any <= 5.0)")

    diffs = [float(np.abs(frames[i].astype(int) - frames[i - 1].astype(int)).mean()) for i in range(1, len(frames))]
    not_frozen = any(d > 1.0 for d in diffs)  # captions/callouts should change the frame meaningfully over time
    report.add("frames_not_frozen", not_frozen, f"mean frame-to-frame diff: {[round(d, 2) for d in diffs]} (fails if all <= 1.0)")


def check_thumbnails(video_dir: Path, stem: str, report: Report):
    variants = ["stat", "headline", "directional"]
    found_any = False
    for variant in variants:
        path = video_dir / f"{stem}_thumb_{variant}.jpg"
        if not path.exists():
            continue
        found_any = True
        try:
            img = Image.open(path)
            size_ok = img.size == EXPECTED_THUMBNAIL_SIZE
            report.add(f"thumbnail_{variant}_dimensions", size_ok, f"{img.size} (expected {EXPECTED_THUMBNAIL_SIZE})")
        except Exception as e:
            report.add(f"thumbnail_{variant}_readable", False, f"could not open image: {e}")
    report.add("at_least_one_thumbnail", found_any, "found a usable thumbnail variant" if found_any else "no thumbnail files found")


def check_seo_metadata(video_dir: Path, stem: str, report: Report):
    seo_path = video_dir / f"{stem}_seo.json"
    if not seo_path.exists():
        report.add("seo_file_exists", False, f"{seo_path} missing")
        return
    seo = json.loads(seo_path.read_text())
    title = seo.get("chosen_title", "")
    description = seo.get("description", "")
    tags = seo.get("tags", [])
    tags_len = len(", ".join(tags))

    report.add("title_present", bool(title.strip()), f"'{title[:60]}...'" if title else "empty")
    report.add("title_length", len(title) <= YOUTUBE_TITLE_MAX, f"{len(title)} chars (YouTube max {YOUTUBE_TITLE_MAX})")
    report.add("description_length", len(description) <= YOUTUBE_DESCRIPTION_MAX, f"{len(description)} chars (YouTube max {YOUTUBE_DESCRIPTION_MAX})")
    report.add("tags_length", tags_len <= YOUTUBE_TAGS_MAX_CHARS, f"{tags_len} chars combined (YouTube max {YOUTUBE_TAGS_MAX_CHARS})")


def check_copyright_and_licensing(video_dir: Path, stem: str, voice_used: str, manifest: dict, report: Report):
    approved_voices = manifest.get("approved_tts_voices", [])
    report.add("tts_voice_approved", voice_used in approved_voices, f"'{voice_used}' {'in' if voice_used in approved_voices else 'NOT in'} approved voice list")

    known = known_filenames(stem)
    known.update({"manifest.json", "cycle_summary.json"})
    licensed_asset_files = {a.get("file") for a in manifest.get("broll_and_music_assets", [])}

    unaccounted = []
    for path in video_dir.iterdir():
        if path.is_dir():
            continue
        if path.name in known or path.name.startswith("qc_frame") or path.name.startswith(stem):
            # anything prefixed with the video's own stem is pipeline-generated for this video
            if path.name in known or path.name.startswith(f"{stem}_thumb_") or path.name.startswith("qc_frame"):
                continue
        if path.name in licensed_asset_files:
            continue
        unaccounted.append(path.name)

    report.add(
        "no_unlicensed_assets",
        len(unaccounted) == 0,
        "no unaccounted media files found" if not unaccounted else f"found files not in the pipeline's known outputs or asset_manifest.json: {unaccounted}",
    )


def main():
    if len(sys.argv) < 4:
        print("Usage: python quality_check.py <video_dir> <stem> <voice_used> [manifest_path]")
        sys.exit(1)

    video_dir = Path(sys.argv[1])
    stem = sys.argv[2]
    voice_used = sys.argv[3]
    manifest_path = Path(sys.argv[4]) if len(sys.argv) > 4 else Path(__file__).parent / "asset_manifest.json"

    manifest = json.loads(manifest_path.read_text())
    video_path = video_dir / f"{stem}.mp4"

    print(f"Quality-checking {stem} ...")
    report = Report()

    duration = check_av_streams(video_path, report)
    if duration:
        check_audio_not_silent(video_path, report)
        check_video_not_blank_or_frozen(video_path, duration, report, video_dir / "_qc_tmp")
    check_thumbnails(video_dir, stem, report)
    check_seo_metadata(video_dir, stem, report)
    check_copyright_and_licensing(video_dir, stem, voice_used, manifest, report)

    result = {"stem": stem, "passed": report.all_passed, "checks": report.checks}
    report_path = video_dir / f"{stem}_quality_report.json"
    report_path.write_text(json.dumps(result, indent=2))

    tmp_dir = video_dir / "_qc_tmp"
    if tmp_dir.exists():
        for f in tmp_dir.iterdir():
            f.unlink()
        tmp_dir.rmdir()

    print(f"\n{'PASSED' if result['passed'] else 'FAILED'}: {stem}  (report: {report_path})")
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
