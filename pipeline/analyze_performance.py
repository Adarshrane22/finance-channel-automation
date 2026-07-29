"""
Stage 9: pulls performance data for recently published videos and
summarizes what's working, so that summary can be fed back into future
runs of the finance-youtube-pipeline skill (paste the output into a
follow-up prompt like "here's what worked last week, keep this in mind
when picking today's topics").

Also closes the thumbnail-style feedback loop automatically (added
alongside the thumbnail-selection upgrade in generate_thumbnail.py):
joins docs/thumbnail_style_log.json (which daily_cycle.py appends to
after every successful upload — video_id -> which of the 3 thumbnail
styles was used) against real CTR from the Analytics API, and writes
docs/thumbnail_style_weights.json so future runs of generate_thumbnail.py
automatically favor whichever style is actually earning more clicks for
this channel. This happens every time this script runs — no separate
flag needed — and never overwrites the weights file with nothing if
analytics data isn't available yet (e.g. the scope hasn't been granted,
or there's no history yet), so it's safe to run early in a channel's
life.

Needs the same youtube_token.json as upload_video.py, but only the
readonly scope is actually used here. Needs real internet access — same
constraint as the other YouTube-API and TTS scripts.

Usage:
  python analyze_performance.py [days_back]

Pulls the channel's videos published in the last N days (default 14),
their current view/like/comment counts (Data API), and CTR/average-view-
duration/retention where available (Analytics API — requires the
`youtubepartner`-adjacent `yt-analytics.readonly` scope; add it to SCOPES
in authorize_youtube.py and re-run that script if you haven't already).
The thumbnail-weight update below uses its own, wider date range (back to
the earliest logged video, capped at 90 days) since a style needs enough
sample videos across time to trust, independent of whatever days_back was
passed for the human-readable summary.
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_PATH = Path(__file__).parent / "youtube_token.json"
DOCS_DIR = Path(__file__).parent.parent / "docs"
STYLE_LOG_PATH = DOCS_DIR / "thumbnail_style_log.json"
STYLE_WEIGHTS_PATH = DOCS_DIR / "thumbnail_style_weights.json"
MIN_SAMPLES_PER_STYLE = 3  # below this, we don't trust the average CTR enough to deviate from a neutral 1.0 weight
WEIGHT_FLOOR, WEIGHT_CEIL = 0.5, 2.0  # keeps one early hot streak from making a style near-exclusive


def get_service(api, version, creds):
    return build(api, version, credentials=creds)


def load_creds():
    if not TOKEN_PATH.exists():
        print(f"No token found at {TOKEN_PATH}. Run authorize_youtube.py first.")
        sys.exit(1)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def get_recent_videos(youtube, days_back):
    channels = youtube.channels().list(part="contentDetails", mine=True).execute()
    uploads_playlist = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    videos = []
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet", playlistId=uploads_playlist, maxResults=50, pageToken=page_token,
        ).execute()
        for item in resp["items"]:
            published = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00"))
            if published >= cutoff:
                videos.append({
                    "video_id": item["snippet"]["resourceId"]["videoId"],
                    "title": item["snippet"]["title"],
                    "published_at": item["snippet"]["publishedAt"],
                })
        page_token = resp.get("nextPageToken")
        if not page_token or (resp["items"] and datetime.fromisoformat(resp["items"][-1]["snippet"]["publishedAt"].replace("Z", "+00:00")) < cutoff):
            break
    return videos


def enrich_with_stats(youtube, videos):
    if not videos:
        return videos
    ids = [v["video_id"] for v in videos]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        resp = youtube.videos().list(part="statistics", id=",".join(chunk)).execute()
        stats_by_id = {item["id"]: item["statistics"] for item in resp["items"]}
        for v in videos:
            if v["video_id"] in stats_by_id:
                s = stats_by_id[v["video_id"]]
                v["views"] = int(s.get("viewCount", 0))
                v["likes"] = int(s.get("likeCount", 0))
                v["comments"] = int(s.get("commentCount", 0))
    return videos


def get_analytics(creds, video_ids, days_back):
    """CTR / avg view duration / retention via the YouTube Analytics API.
    Requires the yt-analytics.readonly scope on the token."""
    try:
        yta = build("youtubeAnalytics", "v2", credentials=creds)
    except Exception as e:
        print(f"Could not build Analytics API client: {e}")
        return {}

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    try:
        resp = yta.reports().query(
            ids="channel==MINE",
            startDate=str(start), endDate=str(end),
            metrics="averageViewDuration,averageViewPercentage,impressions,impressionClickThroughRate",
            dimensions="video",
            filters=f"video=={','.join(video_ids)}" if video_ids else None,
        ).execute()
    except Exception as e:
        print(f"Analytics query failed (you may need to re-run authorize_youtube.py with the analytics scope added): {e}")
        return {}

    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    out = {}
    for row in resp.get("rows", []):
        row_dict = dict(zip(headers, row))
        out[row_dict.get("video")] = row_dict
    return out


def summarize(videos, analytics):
    lines = ["# Recent video performance summary\n"]
    for v in sorted(videos, key=lambda x: x.get("views", 0), reverse=True):
        a = analytics.get(v["video_id"], {})
        lines.append(f"## {v['title']}")
        lines.append(f"Published: {v['published_at']}")
        lines.append(f"Views: {v.get('views', 'n/a')} | Likes: {v.get('likes', 'n/a')} | Comments: {v.get('comments', 'n/a')}")
        if a:
            lines.append(
                f"CTR: {a.get('impressionClickThroughRate', 'n/a')} | "
                f"Avg view duration: {a.get('averageViewDuration', 'n/a')}s | "
                f"Avg % viewed: {a.get('averageViewPercentage', 'n/a')}%"
            )
        lines.append("")
    return "\n".join(lines)


def load_style_log():
    if not STYLE_LOG_PATH.exists():
        return []
    try:
        return json.loads(STYLE_LOG_PATH.read_text())
    except Exception as e:
        print(f"WARNING: found {STYLE_LOG_PATH} but couldn't parse it ({e}) — skipping thumbnail weight update.")
        return []


def update_thumbnail_style_weights(creds):
    """Joins the thumbnail style log against real CTR and writes updated
    per-style weights for generate_thumbnail.py to read next run. Returns
    the weights dict written (or None if there wasn't enough to update
    anything) — never raises, since this is a best-effort learning step
    that must never block the rest of the pipeline."""
    log = load_style_log()
    if not log:
        print("No thumbnail style log yet (docs/thumbnail_style_log.json) — nothing to learn from yet.")
        return None

    video_ids = [e["video_id"] for e in log if e.get("video_id")]
    if not video_ids:
        return None

    uploaded_dates = [datetime.fromisoformat(e["uploaded_at"]) for e in log if e.get("uploaded_at")]
    earliest = min(uploaded_dates) if uploaded_dates else datetime.now(timezone.utc)
    days_back = min(90, max(1, (datetime.now(timezone.utc) - earliest).days + 1))

    analytics = get_analytics(creds, video_ids, days_back)
    if not analytics:
        print("No analytics data available yet (check that yt-analytics.readonly is in authorize_youtube.py's SCOPES and you've re-run it) — leaving thumbnail weights unchanged.")
        return None

    ctr_by_style = defaultdict(list)
    for entry in log:
        row = analytics.get(entry.get("video_id"))
        ctr = row.get("impressionClickThroughRate") if row else None
        if ctr is not None:
            ctr_by_style[entry["style"]].append(float(ctr))

    all_ctrs = [v for values in ctr_by_style.values() for v in values]
    if not all_ctrs:
        print("No CTR data yet for any logged video (impressions may still be accumulating) — leaving thumbnail weights unchanged.")
        return None
    overall_mean = sum(all_ctrs) / len(all_ctrs)

    weights = {}
    for style, values in ctr_by_style.items():
        avg_ctr = sum(values) / len(values)
        if len(values) < MIN_SAMPLES_PER_STYLE or overall_mean <= 0:
            weights[style] = {
                "weight": 1.0, "samples": len(values), "avg_ctr": round(avg_ctr, 5),
                "note": f"fewer than {MIN_SAMPLES_PER_STYLE} samples so far — using a neutral weight until there's enough data",
            }
            continue
        raw_weight = avg_ctr / overall_mean
        weights[style] = {
            "weight": round(max(WEIGHT_FLOOR, min(WEIGHT_CEIL, raw_weight)), 3),
            "samples": len(values),
            "avg_ctr": round(avg_ctr, 5),
        }

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    STYLE_WEIGHTS_PATH.write_text(json.dumps(weights, indent=2))
    print(f"Updated {STYLE_WEIGHTS_PATH} from {len(log)} logged videos:")
    print(json.dumps(weights, indent=2))
    return weights


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    creds = load_creds()
    youtube = get_service("youtube", "v3", creds)

    videos = get_recent_videos(youtube, days_back)
    videos = enrich_with_stats(youtube, videos)
    analytics = get_analytics(creds, [v["video_id"] for v in videos], days_back)

    summary = summarize(videos, analytics)
    print(summary)

    out_path = Path("output") / "performance_summary.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(summary)
    print(f"\nWrote {out_path}")

    try:
        update_thumbnail_style_weights(creds)
    except Exception as e:
        # Best-effort: the human-readable summary above is the primary
        # purpose of this script and already succeeded, so a failure here
        # (a transient API error, an unexpected response shape) should be
        # visible but not treated as this script failing.
        print(f"WARNING: thumbnail style weight update failed, leaving the existing weights file (if any) untouched: {e}")


if __name__ == "__main__":
    main()
