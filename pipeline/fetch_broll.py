"""
Fetches free, licensed stock B-roll video clips from Pexels to replace the
plain gradient background with real footage relevant to each script
section — a meaningful visual quality step up at zero added cost.

Requires: PEXELS_API_KEY environment variable (free, instant signup at
pexels.com/api — no waiting/approval, generous free tier: 200 req/hour,
20,000/month, plenty for 3 videos/day).

Usage:
  python fetch_broll.py <parsed_script.json> <output_dir>

Writes one .mp4 per matched section into <output_dir>/broll/ (named
broll_<section_index>_<pexels_id>.mp4), plus <output_dir>/broll_credits.json
logging the source/license/photographer for every clip actually used.
quality_check.py's copyright gate reads that credits file to allow these
into a video's output folder — since B-roll here is fetched dynamically
per video (not a fixed local asset), it can't be pre-listed in the static
global pipeline/asset_manifest.json the way a hand-picked music track
would be; broll_credits.json is the equivalent per-video paper trail.

Pexels License (https://www.pexels.com/license/): free to use for any
purpose, no attribution legally required. The credits file logs the
photographer anyway as good practice and an easy audit trail.

This is a graceful-degradation step, not a hard requirement: if
PEXELS_API_KEY isn't set, or a given section's search returns nothing
usable, that section just keeps assemble_video.py's gradient background
instead of failing the run — B-roll is an enhancement layered on top of
a pipeline that already works without it.

Also supports a second, vertical mode for YouTube Shorts:

  python fetch_broll.py --vertical <short.json> <output_dir>

<short.json> is the same small {"narration": ..., "title": ...} file
daily_cycle.py already writes for a Short before generating its
voiceover. Unlike the landscape mode (one clip per script section),
a Short is one continuous narration, so this fetches a single portrait
(9:16) clip sized off the whole narration's keywords and writes it to
<output_dir>/broll_vertical.mp4 plus a matching
<output_dir>/broll_vertical_credits.json — same graceful-degradation
rule: no key or no usable result just means assemble_short.py falls
back to its gradient background, never a failed run.
"""
import json
import os
import re
import sys
from pathlib import Path

import requests

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
# Portrait clips run smaller on Pexels than landscape — 1080x1920 "large"
# hits are rarer, so the vertical search accepts anything reasonably
# HD-ish and lets assemble_short.py's fill-crop handle the rest, the same
# way it already handles varying source aspect ratios.
VERTICAL_MIN_WIDTH = 720
VERTICAL_MIN_HEIGHT = 1280

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "this", "that", "it", "its", "as", "by", "at",
    "be", "has", "have", "had", "will", "would", "could", "should", "from",
    "about", "into", "over", "after", "before", "than", "then", "so", "not",
    "no", "yes", "you", "your", "we", "our", "they", "their", "what", "which",
}


def keywords_from_text(text: str, fallback: str = "finance business") -> str:
    """Shared keyword extraction: pull the first few distinct, meaningful
    words out of a chunk of text — Pexels' search is forgiving of a
    short, generic-ish query, and this avoids an extra API call to have a
    model generate a proper search phrase for every section/short."""
    words = re.findall(r"[A-Za-z]{4,}", (text or "").lower())
    words = [w for w in words if w not in STOPWORDS]
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
        if len(seen) >= 3:
            break
    return " ".join(seen) if seen else fallback


def keywords_for_section(section: dict, title: str) -> str:
    """Crude but effective: pull the first few distinct, meaningful words
    out of the section's own text (falling back to the title) rather than
    anything more elaborate — Pexels' search is forgiving of a short,
    generic-ish query, and this avoids an extra API call to have a model
    generate a proper search phrase for every section of every video."""
    return keywords_from_text(section.get("text", "") or title)


def search_and_pick(query: str, api_key: str, orientation: str = "landscape",
                     min_width: int = MIN_WIDTH, min_height: int = MIN_HEIGHT):
    try:
        resp = requests.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": api_key},
            params={"query": query, "orientation": orientation, "size": "large", "per_page": 5},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  WARNING: Pexels search failed for '{query}': {e}")
        return None

    for video in data.get("videos", []):
        files = sorted(
            (
                f for f in video.get("video_files", [])
                if f.get("width", 0) >= min_width
                and f.get("height", 0) >= min_height
                and f.get("file_type") == "video/mp4"
            ),
            key=lambda f: f["width"],
        )
        if files:
            return {
                "pexels_id": video["id"],
                "url": files[0]["link"],
                "photographer": video.get("user", {}).get("name", "unknown"),
                "photographer_url": video.get("user", {}).get("url", ""),
                "pexels_page_url": video.get("url", ""),
            }
    return None


def download(url: str, out_path: Path):
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)


def fetch_vertical(short_json_path: Path, out_dir: Path):
    """Fetches a single portrait clip for a whole Short's narration and
    writes broll_vertical.mp4 + broll_vertical_credits.json into out_dir.
    Mirrors the landscape flow's graceful-degradation behavior: any miss
    (no key, no search match, download failure) just leaves those files
    absent/empty and assemble_short.py keeps its gradient background."""
    out_dir.mkdir(parents=True, exist_ok=True)
    credits_path = out_dir / "broll_vertical_credits.json"

    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        print("PEXELS_API_KEY not set — Short will use the gradient background only.")
        credits_path.write_text(json.dumps([]))
        return

    short = json.loads(short_json_path.read_text())
    query = keywords_from_text(short.get("narration") or short.get("title"))
    print(f"Short B-roll: searching Pexels (portrait) for '{query}'")

    pick = search_and_pick(query, api_key, orientation="portrait",
                            min_width=VERTICAL_MIN_WIDTH, min_height=VERTICAL_MIN_HEIGHT)
    if not pick:
        print("  no usable portrait result — Short keeps the gradient background")
        credits_path.write_text(json.dumps([]))
        return

    out_path = out_dir / "broll_vertical.mp4"
    try:
        download(pick["url"], out_path)
    except Exception as e:
        print(f"  WARNING: download failed for vertical B-roll: {e}")
        credits_path.write_text(json.dumps([]))
        return

    print(f"  downloaded broll_vertical.mp4 (credit: {pick['photographer']})")
    credits_path.write_text(json.dumps([{
        "file": "broll_vertical.mp4",
        "source": "Pexels",
        "license": "Pexels License - free to use, no attribution legally required (https://www.pexels.com/license/)",
        "photographer": pick["photographer"],
        "photographer_url": pick["photographer_url"],
        "pexels_page_url": pick["pexels_page_url"],
    }], indent=2))


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--vertical":
        if len(sys.argv) < 4:
            print("Usage: python fetch_broll.py --vertical <short.json> <output_dir>")
            sys.exit(1)
        fetch_vertical(Path(sys.argv[2]), Path(sys.argv[3]))
        return

    if len(sys.argv) < 3:
        print("Usage: python fetch_broll.py <parsed_script.json> <output_dir>")
        print("       python fetch_broll.py --vertical <short.json> <output_dir>")
        sys.exit(1)

    parsed_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    broll_dir = out_dir / "broll"

    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        print("PEXELS_API_KEY not set — skipping B-roll; video will use the gradient background only.")
        (out_dir / "broll_credits.json").write_text(json.dumps([]))
        return

    parsed = json.loads(parsed_path.read_text())
    sections = parsed.get("sections", [])
    title = parsed.get("title", "")

    broll_dir.mkdir(parents=True, exist_ok=True)
    credits = []

    for i, section in enumerate(sections):
        query = keywords_for_section(section, title)
        print(f"Section {i} ({section.get('name')}): searching Pexels for '{query}'")
        pick = search_and_pick(query, api_key)
        if not pick:
            print("  no usable result — this section keeps the gradient background")
            continue

        filename = f"broll_{i}_{pick['pexels_id']}.mp4"
        out_path = broll_dir / filename
        try:
            download(pick["url"], out_path)
        except Exception as e:
            print(f"  WARNING: download failed for section {i}: {e}")
            continue

        print(f"  downloaded {filename} (credit: {pick['photographer']})")
        credits.append({
            "file": f"broll/{filename}",
            "section_index": i,
            "section_name": section.get("name"),
            "source": "Pexels",
            "license": "Pexels License - free to use, no attribution legally required (https://www.pexels.com/license/)",
            "photographer": pick["photographer"],
            "photographer_url": pick["photographer_url"],
            "pexels_page_url": pick["pexels_page_url"],
        })

    (out_dir / "broll_credits.json").write_text(json.dumps(credits, indent=2))
    print(f"\nFetched {len(credits)}/{len(sections)} B-roll clips. Credits: {out_dir / 'broll_credits.json'}")


if __name__ == "__main__":
    main()
