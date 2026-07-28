"""
Uploads a finished video to YouTube as unlisted/private with a scheduled
publish time, using the metadata from select_seo.py — this is the human
review gate from the build plan turned into code: nothing here ever
uploads as immediately public, so you always get to watch the final cut
and check the metadata before it's live.

Requires:
  - youtube_token.json next to this script (run authorize_youtube.py once
    to create it)
  - Real internet access (same constraint as the voiceover step — this
    can't run inside the Cowork sandbox, which only allows package
    registries outbound. Run it on your own machine/server.)

Usage:
  python upload_video.py <video.mp4> <thumbnail.jpg> <seo.json> <publish_at_iso> [privacy]

  publish_at_iso: an ISO-8601 UTC timestamp, e.g. 2026-07-25T13:30:00Z
                   (YouTube publishes automatically at this time once
                   status is "private" + publishAt is set)
  privacy:        "private" (default, recommended) until you've watched
                   it — YouTube auto-flips it to "public" at publish_at
                   only if you set privacyStatus to "private" AND provide
                   publishAt. If you pass "unlisted" instead, YouTube
                   ignores publishAt and it stays unlisted until you
                   change it by hand — use that if you want a final manual
                   "go" button rather than a scheduled auto-publish.
"""
import json
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

TOKEN_PATH = Path(__file__).parent / "youtube_token.json"


def get_authenticated_service():
    if not TOKEN_PATH.exists():
        print(f"No token found at {TOKEN_PATH}. Run authorize_youtube.py first.")
        sys.exit(1)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload(video_path, thumbnail_path, seo_json_path, publish_at, privacy="private"):
    seo = json.loads(Path(seo_json_path).read_text())
    youtube = get_authenticated_service()

    body = {
        "snippet": {
            "title": seo["chosen_title"][:100],  # YouTube's title limit
            "description": seo["description"][:5000],
            "tags": seo.get("tags", [])[:500],
            "categoryId": "25",  # News & Politics; "22" (People & Blogs) or "27" (Education) are also reasonable for finance content
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    if privacy == "private" and publish_at:
        body["status"]["publishAt"] = publish_at

    print(f"Uploading {video_path} ...")
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  {int(status.progress() * 100)}% uploaded")

    video_id = response["id"]
    print(f"Uploaded. Video ID: {video_id}  URL: https://youtu.be/{video_id}")

    if thumbnail_path and Path(thumbnail_path).exists():
        # The video upload is the part that matters; a thumbnail failure
        # (most commonly: the channel hasn't completed YouTube's phone
        # verification, which gates the custom-thumbnail API) shouldn't
        # throw away an otherwise-successful upload. Log it clearly and
        # keep going — YouTube will use an auto-generated thumbnail until
        # this is set by hand or the channel gets verified.
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumbnail_path)).execute()
            print("Thumbnail set.")
        except Exception as e:
            print(f"WARNING: video uploaded successfully, but setting the custom thumbnail failed: {e}")
            print("This usually means the channel needs phone verification (YouTube Studio > Settings > Channel > Feature eligibility > Custom thumbnails). The video is live/scheduled with YouTube's auto-generated thumbnail for now.")

    return video_id


def main():
    if len(sys.argv) < 5:
        print("Usage: python upload_video.py <video.mp4> <thumbnail.jpg> <seo.json> <publish_at_iso> [privacy]")
        sys.exit(1)
    video_path, thumbnail_path, seo_json_path, publish_at = sys.argv[1:5]
    privacy = sys.argv[5] if len(sys.argv) > 5 else "private"
    upload(video_path, thumbnail_path, seo_json_path, publish_at, privacy)


if __name__ == "__main__":
    main()
