# Finance Channel — Fully Automated Daily Pipeline (GitHub Actions)

This repo runs the entire cycle — research topics, fact-check, write scripts, generate voiceover, render video with retention-focused captions/callouts, generate thumbnails, pick SEO metadata, and upload to YouTube on a schedule — once a day, unattended, on GitHub's infrastructure. No Cowork session needs to be open for this to run; once it's set up, it runs itself.

This exists because the Cowork sandbox this was originally built in has locked-down network access (it can reach package registries but not Edge-TTS or the YouTube APIs), so anything requiring live internet calls has to run somewhere else. GitHub Actions runners have full internet access and a built-in free scheduler, which is why this landed here rather than a bespoke server.

## What runs each day

`pipeline/daily_cycle.py` is the entry point the workflow calls:

1. `research_and_script.py` — calls the Claude API (with its web search tool) to research current US finance topics, pick the 3 strongest, fact-check every material claim against 2+ sources, and write full scripts. This replaces the Cowork `finance-youtube-pipeline` skill with equivalent logic that can run from a script instead of inside a chat session.
2. For each script: voiceover (Edge-TTS) → captions → video render (karaoke captions, stat callouts, section tags, progress bar — same engine built and tested earlier) → 3 thumbnail variants → SEO title/description/tags.
3. Upload to YouTube as **private**, scheduled to auto-publish `PUBLISH_DELAY_HOURS` later (default 6). This is the safety margin in a fully unattended setup — it gives you a window to catch and pull anything that looks wrong before it goes public, without blocking the pipeline on you being available at upload time.
4. Everything (scripts, audio, video, thumbnails, SEO json, a `cycle_summary.json`) gets attached to the GitHub Actions run as a downloadable artifact, whether or not the upload succeeded — so nothing is ever silently lost, and you always have a paper trail to review even if you set the publish delay to something short.

One video failing doesn't stop the other two — see the comments at the top of `daily_cycle.py` for the reasoning.

## One-time setup

### 1. Create the GitHub repo

Push this folder to a new **private** repo (private matters — this pipeline handles API keys and, transitively, your channel's upload credentials).

```
cd finance-channel-automation
git init
git add .
git commit -m "Initial automated finance channel pipeline"
git branch -M main
git remote add origin https://github.com/<you>/<repo-name>.git
git push -u origin main
```

### 2. Get an Anthropic API key

console.anthropic.com → API Keys → Create Key. This is separate from any Claude subscription you use in chat — it's billed per-use via the API, and this pipeline's main cost driver (research + script writing runs a substantial prompt with web search, 3x/day).

### 3. Get YouTube credentials (if you haven't already)

Follow the earlier YouTube API setup guide (Google Cloud project → enable YouTube Data API → OAuth client credentials → `client_secret.json`). Then, **on your own machine** (not in GitHub Actions — this step needs a browser to log in interactively):

```
pip install google-auth-oauthlib google-api-python-client
python3 pipeline/authorize_youtube.py path/to/client_secret.json
```

This produces `pipeline/youtube_token.json`. Base64-encode both files:

```
base64 -w0 client_secret.json > client_secret.b64
base64 -w0 pipeline/youtube_token.json > youtube_token.b64
```

(On macOS, drop `-w0`: `base64 client_secret.json | tr -d '\n' > client_secret.b64`)

### 4. Add GitHub repo secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from step 2 |
| `YOUTUBE_CLIENT_SECRET_B64` | contents of `client_secret.b64` |
| `YOUTUBE_TOKEN_B64` | contents of `youtube_token.b64` |

Optional repo **variables** (Settings → Secrets and variables → Actions → Variables tab) to tweak behavior without editing code: `PUBLISH_DELAY_HOURS` (default 6), `TTS_VOICE` (default `en-US-GuyNeural`), `BRAND_HEX` (default `1F6FEB`).

### 5. Test it before trusting the schedule

Actions tab → "Daily Finance Channel Pipeline" → "Run workflow" → tick "skip_upload" for a dry run that renders everything but doesn't touch YouTube. Check the artifact it produces. Once that looks right, run it again without skip_upload to confirm a real upload lands correctly (as private/scheduled, so it won't actually go public unexpectedly — you can delete it from YouTube Studio afterward if it was just a test).

The workflow also runs automatically every day at 00:30 UTC (6:00 AM IST) once this is all set up — no further action needed after this point.

## Keeping the YouTube token alive

While your Google Cloud OAuth app is in "Testing" mode (the default, fine for personal use), the refresh token this relies on can expire after about 7 days of the app being unverified. If uploads start failing with an auth error, re-run `authorize_youtube.py` locally, re-encode the new `youtube_token.json`, and update the `YOUTUBE_TOKEN_B64` secret. This is a ~2 minute task; doing it weekly is far less effort than going through Google's app verification process for a personal channel. If this becomes annoying, that verification process is the permanent fix.

## Costs

- **Anthropic API**: the main variable cost — a research+script call with web search, 3x/day. Set a spend limit in the Anthropic console if you want a hard ceiling.
- **GitHub Actions**: free tier covers 2,000 minutes/month on private repos; a full run (3 videos, each rendering for several minutes given the retention-feature overlays — see the performance note in `pipeline/assemble_video.py`) could use a meaningful chunk of that. Monitor your first week of runs (Actions tab shows duration per run) and adjust `PUBLISH_DELAY_HOURS`/schedule or consider a self-hosted runner if you're approaching the limit.
- **Edge-TTS**: free.
- Everything else (thumbnails, video rendering, SEO scoring) is local compute, free.

## Reducing risk while you build trust in this running unattended

Recommended path for the first couple of weeks: keep `PUBLISH_DELAY_HOURS` generous (6-12) so you have a real window to check each day's output before it's public, and actually check the Actions run summary each morning (it lists what succeeded/failed and links to each uploaded video) rather than assuming silence means success. Once you've seen a couple weeks of consistently good output, shortening the delay or trusting it fully is a reasonable call to make — but that's a judgment call based on watching it work, not something to assume on day one.
