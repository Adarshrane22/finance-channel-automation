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

Env vars used: ANTHROPIC_API_KEY (research_and_script.py),
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


def run(cmd, **kwargs):
    print(f"$ {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, **kwargs)


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

    final_mp4 = video_dir / f"{stem}.mp4"
    run(["python3", str(PIPELINE_DIR / "assemble_video.py"), str(generated_mp3), str(generated_captions), str(parsed_json_path), str(final_mp4), brand_hex, "--broll-dir", str(video_dir / "broll")])

    run(["python3", str(PIPELINE_DIR / "generate_thumbnail.py"), str(parsed_json_path), str(video_dir), brand_hex])
    thumbnail_path = video_dir / f"{stem}_thumb_headline.jpg"

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

    return {"stem": stem, "video": str(final_mp4), "thumbnail": str(thumbnail_path), "video_id": video_id}


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
