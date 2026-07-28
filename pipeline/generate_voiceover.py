"""
Generates a US-English voiceover from a parsed script's narration text,
using Edge-TTS (free, no API key needed, natural neural voices).

IMPORTANT — where this can run:
Edge-TTS talks to Microsoft's speech service over the open internet. It will
NOT work in network-sandboxed environments (like a locked-down cloud
container) that only allow a small allowlist of hosts. Run this on a machine
with normal internet access: your own laptop, a VPS, a GitHub Actions
runner, etc. If you see an SSLCertVerificationError or a connection error,
that's a network restriction, not a bug in this script.

Produces two files next to the input:
  <name>.mp3          - the voiceover audio
  <name>_captions.json - word-level timing data (from Edge-TTS's own
                          WordBoundary events, so captions are genuinely
                          synced to the audio, not estimated)

Recommended voices for a US finance channel (natural, clear, not overly
casual):
  en-US-GuyNeural       - male, confident/professional
  en-US-AriaNeural      - female, warm/professional
  en-US-ChristopherNeural - male, deeper/authoritative
  en-US-JennyNeural     - female, friendly/conversational
Run `edge-tts --list-voices` for the full catalog.
"""
import asyncio
import json
import sys
from pathlib import Path

import edge_tts

DEFAULT_VOICE = "en-US-GuyNeural"


async def synthesize(text: str, voice: str, out_mp3: str, out_captions: str):
    # boundary="WordBoundary" is required as of edge-tts 7.x — the library's
    # Communicate class now defaults to boundary="SentenceBoundary", which
    # silently produces zero WordBoundary events (audio still generates
    # fine, so this fails quietly rather than with an obvious error) unless
    # word-level timing is explicitly requested here.
    communicate = edge_tts.Communicate(text, voice, rate="+0%", boundary="WordBoundary")
    word_events = []
    with open(out_mp3, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_events.append({
                    "text": chunk["text"],
                    "start_s": chunk["offset"] / 10_000_000,  # 100-ns units -> seconds
                    "duration_s": chunk["duration"] / 10_000_000,
                })
    Path(out_captions).write_text(json.dumps(word_events, indent=2))
    return word_events


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_voiceover.py <parsed_script.json> [voice] [output_dir]")
        sys.exit(1)

    parsed_path = Path(sys.argv[1])
    voice = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VOICE
    out_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else parsed_path.parent

    data = json.loads(parsed_path.read_text())
    stem = parsed_path.stem
    out_mp3 = out_dir / f"{stem}.mp3"
    out_captions = out_dir / f"{stem}_captions.json"

    words = asyncio.run(synthesize(data["narration"], voice, str(out_mp3), str(out_captions)))
    if not words:
        # Edge-TTS occasionally drops WordBoundary events on a given call
        # (transient service issue) even though it produces valid audio.
        # A silent empty-captions file would only surface much later as a
        # confusing MoviePy error, so retry once immediately, then fail
        # loudly here if it happens twice in a row.
        print("WARNING: 0 word-boundary events on first attempt — retrying once before failing.")
        words = asyncio.run(synthesize(data["narration"], voice, str(out_mp3), str(out_captions)))
        if not words:
            raise RuntimeError(
                f"Edge-TTS returned audio but zero WordBoundary (caption) events, twice in a row, "
                f"for voice '{voice}'. This breaks karaoke captions downstream. Not a code bug in "
                f"this script — likely a transient Edge-TTS/Microsoft speech service issue. Re-run "
                f"the pipeline, or try a different --voice if it persists."
            )

    print(f"Voiceover: {out_mp3}")
    print(f"Word-level captions: {out_captions} ({len(words)} words)")


if __name__ == "__main__":
    main()
