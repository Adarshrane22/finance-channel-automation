"""
Pulls real YouTube performance data (views, likes, CTR, watch time) for the
channel's recent videos and writes it as JSON into docs/dashboard_data.json
— the data file the hosted dashboard (docs/index.html, served via GitHub
Pages) reads to render live charts.

This is the "keep the dashboard live" half of the loop: daily_cycle.py
calls this after upload, and the workflow commits the updated JSON back to
the repo, so the hosted page always reflects the latest data without you
doing anything manually.

Builds on the same YouTube Data + Analytics API calls as
analyze_performance.py (which writes a human-readable markdown summary);
this script writes structured JSON instead, and — importantly — appends
to a running history array rather than overwriting it, so the dashboard
can chart trends over time rather than just a single snapshot.

Needs the same youtube_token.json as upload_video.py / analyze_performance.py.
Fails gracefully: if the Analytics API call fails (a common transient
issue, or the token temporarily lacking the analytics scope), it still
writes what it has from the Data API rather than crashing the whole run —
a stale-but-present dashboard beats a pipeline failure over an
optional reporting step.

Usage:
  python export_dashboard_data.py [days_back] [output_path]
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_PATH = Path(__file__).parent / "youtube_token.json"
DEFAULT_OUTPUT = Path(__file__).parent.parent / "docs" / "dashboard_data.json"


def load_creds():
    if not TOKEN_PATH.exists():
        print(f"No token found at {TOKEN_PATH}. Skipping dashboard export.")
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def get_recent_videos(youtube, days_back):
    channels = youtube.channels().list(part="contentDetails,statistics", mine=True).execute()
    channel = channels["items"][0]
    uploads_playlist = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    channel_stats = channel.get("statistics", {})

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
                sn = item["snippet"]
                videos.append({
                    "video_id": sn["resourceId"]["videoId"],
                    "title": sn["title"],
                    "published_at": sn["publishedAt"],
                    "thumbnail_url": (sn.get("thumbnails", {}).get("medium") or sn.get("thumbnails", {}).get("default") or {}).get("url", ""),
                })
        page_token = resp.get("nextPageToken")
        if not page_token or (resp["items"] and datetime.fromisoformat(resp["items"][-1]["snippet"]["publishedAt"].replace("Z", "+00:00")) < cutoff):
            break
    return videos, channel_stats


def enrich_with_stats(youtube, videos):
    if not videos:
        return videos
    ids = [v["video_id"] for v in videos]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        resp = youtube.videos().list(part="statistics,status", id=",".join(chunk)).execute()
        by_id = {item["id"]: item for item in resp["items"]}
        for v in videos:
            item = by_id.get(v["video_id"])
            if not item:
                continue
            s = item.get("statistics", {})
            v["views"] = int(s.get("viewCount", 0))
            v["likes"] = int(s.get("likeCount", 0))
            v["comments"] = int(s.get("commentCount", 0))
            v["privacy_status"] = item.get("status", {}).get("privacyStatus", "unknown")
    return videos


def get_analytics(creds, video_ids, days_back):
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
        print(f"Analytics query failed (non-fatal — dashboard will show Data API stats only): {e}")
        return {}

    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    out = {}
    for row in resp.get("rows", []):
        out[dict(zip(headers, row)).get("video")] = dict(zip(headers, row))
    return out


def build_snapshot(videos, analytics, channel_stats):
    enriched = []
    total_views = total_likes = total_comments = 0
    ctr_values, watch_values = [], []

    for v in sorted(videos, key=lambda x: x.get("published_at", ""), reverse=True):
        a = analytics.get(v["video_id"], {})
        ctr = a.get("impressionClickThroughRate")
        watch_s = a.get("averageViewDuration")
        pct_viewed = a.get("averageViewPercentage")
        row = {
            **v,
            "ctr_pct": round(ctr, 2) if isinstance(ctr, (int, float)) else None,
            "avg_view_duration_s": round(watch_s, 1) if isinstance(watch_s, (int, float)) else None,
            "avg_pct_viewed": round(pct_viewed, 1) if isinstance(pct_viewed, (int, float)) else None,
            "url": f"https://youtu.be/{v['video_id']}",
        }
        enriched.append(row)
        total_views += v.get("views", 0)
        total_likes += v.get("likes", 0)
        total_comments += v.get("comments", 0)
        if row["ctr_pct"] is not None:
            ctr_values.append(row["ctr_pct"])
        if row["avg_view_duration_s"] is not None:
            watch_values.append(row["avg_view_duration_s"])

    return {
        "videos": enriched,
        "recent_totals": {
            "views": total_views,
            "likes": total_likes,
            "comments": total_comments,
            "video_count": len(enriched),
            "avg_ctr_pct": round(sum(ctr_values) / len(ctr_values), 2) if ctr_values else None,
            "avg_watch_time_s": round(sum(watch_values) / len(watch_values), 1) if watch_values else None,
        },
        "channel_totals": {
            "subscriber_count": int(channel_stats.get("subscriberCount", 0)) if not channel_stats.get("hiddenSubscriberCount") else None,
            "view_count": int(channel_stats.get("viewCount", 0)),
            "video_count": int(channel_stats.get("videoCount", 0)),
        },
    }


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT

    creds = load_creds()
    if creds is None:
        # No token (e.g. a skip_upload dry run) — leave any existing
        # dashboard data file untouched rather than erroring.
        return

    youtube = build("youtube", "v3", credentials=creds)
    videos, channel_stats = get_recent_videos(youtube, days_back)
    videos = enrich_with_stats(youtube, videos)
    analytics = get_analytics(creds, [v["video_id"] for v in videos], days_back)
    snapshot = build_snapshot(videos, analytics, channel_stats)

    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text())
        except Exception:
            existing = {}

    history = existing.get("history", [])
    # One entry per day: replace today's entry if this is a re-run on the
    # same day, otherwise append — so history charts a clean daily trend
    # instead of accumulating multiple points per day.
    history = [h for h in history if h.get("date") != today]
    history.append({
        "date": today,
        "total_views": snapshot["recent_totals"]["views"],
        "avg_ctr_pct": snapshot["recent_totals"]["avg_ctr_pct"],
        "avg_watch_time_s": snapshot["recent_totals"]["avg_watch_time_s"],
        "channel_view_count": snapshot["channel_totals"]["view_count"],
        "channel_video_count": snapshot["channel_totals"]["video_count"],
    })
    history = sorted(history, key=lambda h: h["date"])[-90:]  # keep last 90 days

    data = {
        "last_updated": now_iso,
        **snapshot,
        "history": history,
    }
    output_path.write_text(json.dumps(data, indent=2))
    print(f"Wrote dashboard data: {output_path} ({len(snapshot['videos'])} videos, {len(history)} history points)")


if __name__ == "__main__":
    main()
