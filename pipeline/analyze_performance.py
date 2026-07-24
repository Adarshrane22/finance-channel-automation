"""
Stage 9: pulls performance data for recently published videos and
summarizes what's working, so that summary can be fed back into future
runs of the finance-youtube-pipeline skill (paste the output into a
follow-up prompt like "here's what worked last week, keep this in mind
when picking today's topics").

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
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_PATH = Path(__file__).parent / "youtube_token.json"


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


if __name__ == "__main__":
    main()
