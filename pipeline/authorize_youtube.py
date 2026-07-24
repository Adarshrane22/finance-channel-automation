"""
One-time (well, roughly weekly, while your OAuth app is in Testing mode —
see the setup guide) authorization step. Run this once interactively on
your own machine to produce a reusable token file, which upload_video.py
then uses without prompting you again.

Needs: `client_secret.json` from the Google Cloud setup guide, sitting in
this same directory (or pass its path as an argument).

Usage:
  python authorize_youtube.py [path/to/client_secret.json]

This opens a browser window for you to log in and approve access. On a
headless server without a browser, google-auth-oauthlib falls back to a
console flow (it'll print a URL to open elsewhere and paste back a code).
"""
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]

TOKEN_PATH = Path(__file__).parent / "youtube_token.json"


def main():
    client_secret_path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "client_secret.json")
    if not Path(client_secret_path).exists():
        print(f"Can't find {client_secret_path}. Put your client_secret.json there, or pass its path as an argument.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.write_text(creds.to_json())
    print(f"Authorized. Token saved to {TOKEN_PATH}")
    print("Keep this file private — it grants upload access to your channel.")
    print("If your OAuth app is still in 'Testing' mode in Google Cloud, re-run this script roughly every 7 days when the token expires.")


if __name__ == "__main__":
    main()
