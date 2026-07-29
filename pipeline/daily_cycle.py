"""
The full unattended daily cycle: research + script → voiceover → video →
thumbnail → SEO → upload, for every video produced by research_and_script.py.
This is what the GitHub Actions workflow calls once a day.

Design choices worth knowing about:

- One video failing doesn't kill the run. If video 2 of 3 fails at the
  voiceover step (a flaky TTS connection, say), videos 1 and 3 still
  complete and upload. Failures are collected and reported at the end
  (and in the GitHub Actions job summary) rather than raising immediately
  — an unattended job that dies on the first hiccup defeats the point of
  automating it.
- Upload always goes out as privacyStatus=private with a delayed
  publishAt (PUBLISH_DELAY_HOURS, default 6) rather than immediately
  public. There's no human in the loop in this mode, so this delay is the
  only safety margin left — it gives you a window to catch and unpublish
  something (via the YouTube Studio app, or by re-running
  upload_video.py's underlying API call to update the video) before it
  goes live, without blocking the pipeline on your availability.
- Every artifact (script JSON, audio, video, thumbnails, SEO json) is
  kept in the output directory and uploaded as a GitHub Actions artifact
  regardless of whether the YouTube upload succeeds, so a failed upload
  never means lost work.

Usage:
  python daily_cycle.py [output_dir] [num_videos] [topic_focus]

Env vars used: GEMINI_API_KEY (research_and_script.py),
PUBLISH_DELAY_HOURS (default 6), TTS_VOICE (default en-US-GuyNeural),
BRAND_HEX (default 1F6FEB), SKIP_UPLOAD (set to "1" to render everything
but skip the actual YouTube upload — useful for a dry run).
"""
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

PIPELINE_DIR = Path(__file__).parent
DOCS_DIR = PIPELINE_DIR.parent / "docs"


def run(cmd, **kwargs):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


SHORTS_SCRIPT_PRIORITY = ["thirty_second_script", "sixty_second_script", "fifteen_second_script"]
DISCLAIMER = "Informational purposes only, not financial advice."


def probe_has_streams(video_path: Path) -> bool:
    """Lightweight sanity check for a rendered Short — confirms it has
    both a video and an audio stream and a positive duration. This is
    deliberately NOT the full quality_check.py gate: that script assumes
    a 1920x1080 landscape canvas, a matching set of 3 thumbnail variants,
    and a full SEO json shape, none of which a vertical Short produces.
    Retrofitting quality_check.py for two different video shapes was more
    risk to the already-working long-form gate than it was worth for a
    first version of Shorts support — this is a real, if lighter,
    automated check rather than no check at all."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-show_entries", "format=duration", "-of", "json", str(video_path)],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(result.stdout)
        codec_types = {s.get("codec_type") for s in info.get("streams", [])}
        duration = float(info.get("format", {}).get("duration", 0))
        return "video" in codec_types and "audio" in codec_types and duration > 0
    except Exception as e:
        print(f"WARNING: could not probe {video_path}: {e}")
        return False


def pick_shorts_script(marketing: dict):
    """Prefers the 30-second script (a strong sweet spot for a hook-driven
    finance short — long enough for a real point, short enough to hold
    attention), falling back to 60s then 15s if that one's missing.
    Returns (duration_label, text) or (None, None) if the marketing
    sidecar has no shorts content at all."""
    shorts = (marketing or {}).get("shorts") or {}
    for key in SHORTS_SCRIPT_PRIORITY:
        text = (shorts.get(key) or "").strip()
        if text:
            return key.replace("_script", ""), text
    return None, None


def build_short_title(video_title: str, max_len: int = 90) -> str:
    """Keeps ' #Shorts' in the title — alongside the vertical aspect
    ratio and sub-3-minute duration, this is one of the signals YouTube
    uses to route a video into the Shorts shelf/feed rather than treating
    it as a regular upload, which matters a lot for the reach this is
    meant to add."""
    suffix = " #Shorts"
    base = video_title[: max_len - len(suffix)].rstrip()
    return f"{base}{suffix}"


def process_short_for_video(parsed: dict, marketing: dict, video_dir: Path, stem: str, voice: str, brand_hex: str, video_id, skip_upload: bool):
    """Builds and (unless skip_upload) uploads a vertical YouTube Short
    from one of the 30/60/15-second scripts research_and_script.py's
    Stage B marketing package already wrote — no new research/writing
    cost, this is a second, shorter edit of content that's already
    fact-checked. Returns a small info dict, or None if there's no
    shorts script to work from (an older/failed marketing sidecar) —
    a missing Short should never fail the main video."""
    duration_label, narration = pick_shorts_script(marketing)
    if not narration:
        print(f"No shorts script available for {stem} (marketing sidecar missing or empty shorts block) — skipping Short.")
        return None

    short_stem = f"{stem}_short"
    short_json_path = video_dir / f"{short_stem}.json"
    short_json_path.write_text(json.dumps({"narration": narration, "title": parsed["title"]}))

    run(["python3", str(PIPELINE_DIR / "generate_voiceover.py"), str(short_json_path), voice, str(video_dir)])
    short_mp3 = video_dir / f"{short_stem}.mp3"
    short_captions = video_dir / f"{short_stem}_captions.json"

    short_mp4 = video_dir / f"{short_stem}.mp4"
    run(["python3", str(PIPELINE_DIR / "assemble_short.py"), str(short_mp3), str(short_captions), str(short_mp4), brand_hex])

    if not probe_has_streams(short_mp4):
        print(f"WARNING: {short_mp4} failed the basic video/audio stream check — skipping upload for this Short.")
        return {"stem": short_stem, "video": str(short_mp4), "video_id": None, "duration_variant": duration_label, "skipped_reason": "failed stream check"}

    curated_hashtags = (marketing.get("curated_hashtags") or [])[:8]  # fewer than the main video — Shorts descriptions are read even less than long-form ones
    desc_parts = [f"{parsed['title']}.", DISCLAIMER]
    if video_id:
        desc_parts.append(f"Full video: https://youtu.be/{video_id}")
    if curated_hashtags:
        desc_parts.append(" ".join(curated_hashtags))
    short_seo = {
        "chosen_title": build_short_title(parsed["title"]),
        "description": "\n\n".join(desc_parts),
        "tags": parsed.get("tags", [])[:10],
    }
    short_seo_path = video_dir / f"{short_stem}_seo.json"
    short_seo_path.write_text(json.dumps(short_seo, indent=2))

    short_video_id = None
    if not skip_upload:
        delay_hours = float(os.environ.get("PUBLISH_DELAY_HOURS", "6"))
        publish_at = (datetime.now(timezone.utc) + timedelta(hours=delay_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = subprocess.run(
            # No thumbnail arg (empty string) — Shorts don't support custom
            # thumbnails the same way long-form videos do; YouTube picks a
            # frame automatically. upload_video.py already handles an
            # empty/nonexistent thumbnail path gracefully.
            ["python3", str(PIPELINE_DIR / "upload_video.py"), str(short_mp4), "", str(short_seo_path), publish_at],
            capture_output=True, text=True,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            print(f"WARNING: Short upload failed for {short_stem} — main video upload already succeeded, so this is logged, not raised.")
        else:
            for line in result.stdout.splitlines():
                if "Video ID:" in line:
                    short_video_id = line.split("Video ID:")[1].split()[0]

    return {
        "stem": short_stem,
        "video": str(short_mp4),
        "video_id": short_video_id,
        "duration_variant": duration_label,
    }


def append_thumbnail_style_log(entry: dict):
    """Records which thumbnail style actually got uploaded for a video,
    keyed by video_id, so analyze_performance.py can later join it
    against real YouTube Analytics CTR and feed a learned preference back
    into generate_thumbnail.py's select_primary_variant. Lives in docs/
    so the same GitHub Actions step that already commits
    docs/dashboard_data.json back to the repo picks this up too — see
    daily-pipeline.yml's "Publish dashboard + thumbnail performance data"
    step. Best-effort: a logging failure here should never fail an
    otherwise-successful upload."""
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = DOCS_DIR / "thumbnail_style_log.json"
        log = []
        if log_path.exists():
            try:
                log = json.loads(log_path.read_text())
            except Exception:
                log = []
        log.append(entry)
        log_path.write_text(json.dumps(log, indent=2))
    except Exception as e:
        print(f"WARNING: could not append to thumbnail style log: {e}")


def process_one_video(parsed_json_path: Path, out_dir: Path, voice: str, brand_hex: str, skip_upload: bool):
    stem = parsed_json_path.stem
    video_dir = out_dir / stem
    video_dir.mkdir(parents=True, exist_ok=True)

    mp3_path = video_dir / f"{stem}.mp3"
    run(["python3", str(PIPELINE_DIR / "generate_voiceover.py"), str(parsed_json_path), voice, str(video_dir)])
    # generate_voiceover.py names outputs after the input file's stem
    generated_mp3 = video_dir / f"{parsed_json_path.stem}.mp3"
    generated_captions = video_dir / f"{parsed_json_path.stem}_captions.json"

    srt_path = video_dir / f"{stem}.srt"
    run(["python3", str(PIPELINE_DIR / "build_captions.py"), str(generated_captions), str(srt_path)])

    # Free stock B-roll (Pexels) — optional visual enhancement. If
    # PEXELS_API_KEY isn't set, or Pexels has nothing usable for a given
    # section, that section just keeps assemble_video.py's gradient
    # background, so this never blocks the run.
    run(["python3", str(PIPELINE_DIR / "fetch_broll.py"), str(parsed_json_path), str(video_dir)])

    # Thumbnails are generated BEFORE the video is assembled (reordered
    # from the original research->voice->captions->broll->video->thumbnail
    # sequence) specifically so assemble_video.py can flash the finished
    # thumbnail graphic into the video's opening seconds — see
    # generate_intro_outro.build_thumbnail_flash_clip for why that helps
    # retention (viewers see the same image that got them to click).
    run(["python3", str(PIPELINE_DIR / "generate_thumbnail.py"), str(parsed_json_path), str(video_dir), brand_hex])

    # generate_thumbnail.py now picks the single best-scoring variant
    # itself (heuristics + learned per-style CTR weight, see its
    # select_primary_variant) rather than this script always hardcoding
    # "_thumb_headline.jpg". Fall back to the headline variant only if
    # the chosen-variant sidecar is somehow missing (e.g. an older
    # generate_thumbnail.py), so this never hard-fails a video.
    chosen_path = video_dir / f"{stem}_thumb_chosen.json"
    thumbnail_style = "headline_forward"
    if chosen_path.exists():
        try:
            chosen = json.loads(chosen_path.read_text())
            thumbnail_path = Path(chosen["path"])
            thumbnail_style = chosen["style"]
            print(f"Selected thumbnail style for {stem}: {thumbnail_style} ({chosen.get('reason')})")
        except Exception as e:
            print(f"WARNING: couldn't read {chosen_path} ({e}) — falling back to the headline variant.")
            thumbnail_path = video_dir / f"{stem}_thumb_headline.jpg"
    else:
        thumbnail_path = video_dir / f"{stem}_thumb_headline.jpg"

    final_mp4 = video_dir / f"{stem}.mp4"
    run(["python3", str(PIPELINE_DIR / "assemble_video.py"), str(generated_mp3), str(generated_captions), str(parsed_json_path), str(final_mp4), brand_hex,
         "--broll-dir", str(video_dir / "broll"), "--thumbnail", str(thumbnail_path)])

    seo_json = video_dir / f"{stem}_seo.json"
    run(["python3", str(PIPELINE_DIR / "select_seo.py"), str(parsed_json_path), str(seo_json)])

    # Quality + copyright gate: this is the one automated check standing
    # in for a human watching the video before it uploads. A failure here
    # means "don't upload this," full stop — better to skip a day's video
    # than publish something silently broken (dead audio, a frozen
    # render) or something using an asset nobody signed off on.
    qc_result = subprocess.run(
        ["python3", str(PIPELINE_DIR / "quality_check.py"), str(video_dir), stem, voice],
        capture_output=True, text=True,
    )
    print(qc_result.stdout)
    if qc_result.returncode != 0:
        print(qc_result.stderr, file=sys.stderr)
        raise RuntimeError(f"Quality/copyright check failed for {stem} — upload skipped. See {stem}_quality_report.json for details.")

    video_id = None
    if not skip_upload:
        delay_hours = float(os.environ.get("PUBLISH_DELAY_HOURS", "6"))
        publish_at = (datetime.now(timezone.utc) + timedelta(hours=delay_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = subprocess.run(
            ["python3", str(PIPELINE_DIR / "upload_video.py"), str(final_mp4), str(thumbnail_path), str(seo_json), publish_at],
            capture_output=True, text=True,
        )
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            raise RuntimeError(f"Upload failed for {stem}: {result.stderr[-500:]}")
        for line in result.stdout.splitlines():
            if "Video ID:" in line:
                video_id = line.split("Video ID:")[1].split()[0]

        if video_id:
            title = stem
            try:
                title = json.loads(seo_json.read_text()).get("chosen_title", stem)
            except Exception:
                pass
            append_thumbnail_style_log({
                "stem": stem,
                "video_id": video_id,
                "style": thumbnail_style,
                "title": title,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            })

    # Fully-automated YouTube Short, built from the same fact-checked
    # content as the long-form video — see process_short_for_video's
    # docstring. Runs after the main video's upload (even in skip_upload
    # dry runs, it still renders for QA, just doesn't upload) so it can
    # link back to the full video once a video_id exists. A Short failing
    # here is logged, never raised — the main video for the day already
    # succeeded and that's what matters most.
    short_info = None
    marketing_path = parsed_json_path.with_name(parsed_json_path.stem + "_marketing.json")
    if marketing_path.exists():
        try:
            parsed = json.loads(parsed_json_path.read_text())
            marketing = json.loads(marketing_path.read_text())
            short_info = process_short_for_video(parsed, marketing, video_dir, stem, voice, brand_hex, video_id, skip_upload)
        except Exception as e:
            print(f"WARNING: Short generation failed for {stem}, continuing without it: {e}")
    else:
        print(f"No marketing sidecar for {stem} — skipping Short (only the main video's title/tags exist, no shorts script to build from).")

    return {
        "stem": stem, "video": str(final_mp4), "thumbnail": str(thumbnail_path),
        "thumbnail_style": thumbnail_style, "video_id": video_id, "short": short_info,
    }


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output") / datetime.now().strftime("%Y-%m-%d")
    num_videos = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    topic_focus = sys.argv[3] if len(sys.argv) > 3 else ""
    voice = os.environ.get("TTS_VOICE", "en-US-GuyNeural")
    brand_hex = os.environ.get("BRAND_HEX", "1F6FEB")
    skip_upload = os.environ.get("SKIP_UPLOAD") == "1"

    out_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = out_dir / "scripts"

    run(["python3", str(PIPELINE_DIR / "research_and_script.py"), str(scripts_dir), str(num_videos), topic_focus])

    manifest = json.loads((scripts_dir / "manifest.json").read_text())
    results, failures = [], []

    for file_path in manifest["files"]:
        parsed_json_path = Path(file_path)
        print(f"\n{'=' * 60}\nProcessing: {parsed_json_path.name}\n{'=' * 60}")
        try:
            result = process_one_video(parsed_json_path, out_dir, voice, brand_hex, skip_upload)
            results.append(result)
        except Exception as e:
            print(f"FAILED: {parsed_json_path.name}: {e}")
            traceback.print_exc()
            failures.append({"file": str(parsed_json_path), "error": str(e)})

    summary = {
        "date": manifest["date"],
        "requested": num_videos,
        "succeeded": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
    summary_path = out_dir / "cycle_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    print(f"\n{'=' * 60}\nCYCLE COMPLETE: {len(results)}/{num_videos} succeeded\n{'=' * 60}")
    for r in results:
        print(f"  OK   {r['stem']}  video_id={r['video_id']}")
    for f in failures:
        print(f"  FAIL {f['file']}: {f['error']}")

    # Refresh the live dashboard's data file with real YouTube Analytics
    # numbers. Deliberately non-fatal: a stale dashboard is far better than
    # letting an optional reporting step fail an otherwise-successful
    # video run. Skipped entirely on a skip_upload dry run (no token
    # restored in that mode — see the workflow's "Restore YouTube
    # credentials" step condition).
    if not skip_upload:
        dash_result = subprocess.run(
            ["python3", str(PIPELINE_DIR / "export_dashboard_data.py")],
            capture_output=True, text=True,
        )
        print(dash_result.stdout)
        if dash_result.returncode != 0:
            print(f"WARNING: dashboard data export failed (non-fatal): {dash_result.stderr[-500:]}", file=sys.stderr)

    # Surface a readable summary in the GitHub Actions job UI, not just logs
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(f"# Daily cycle: {summary['succeeded']}/{summary['requested']} succeeded\n\n")
            for r in results:
                yt_link = f"https://youtu.be/{r['video_id']}" if r["video_id"] else "(upload skipped)"
                f.write(f"- ✅ **{r['stem']}** — {yt_link}\n")
            for fl in failures:
                f.write(f"- ❌ **{Path(fl['file']).stem}** — {fl['error']}\n")

    if failures and not results:
        sys.exit(1)  # total failure — fail the job so GitHub notifies you; partial failure exits 0 so artifacts still upload


if __name__ == "__main__":
    main()
