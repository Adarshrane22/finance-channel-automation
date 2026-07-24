"""
Turns word-level timing data into an SRT caption file, grouping words into
readable on-screen chunks (~6-8 words / ~3 seconds per caption).

Works with two kinds of input:
  1. Real word timings from generate_voiceover.py's *_captions.json
     (produced by Edge-TTS's WordBoundary events) — accurate.
  2. An "estimated" mode (--estimate <narration_text> <audio_duration_s>)
     that spreads words evenly across a known audio duration — used only
     when real TTS timing isn't available (e.g. testing the video-assembly
     step with placeholder audio in a network-sandboxed environment).
     Estimated captions will drift out of sync with any *real* voiceover
     and should be regenerated from real word timings before publishing.
"""
import json
import sys
from pathlib import Path

WORDS_PER_CAPTION = 7


def srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def words_to_srt(word_events: list, out_path: str):
    lines = []
    idx = 1
    for i in range(0, len(word_events), WORDS_PER_CAPTION):
        chunk = word_events[i:i + WORDS_PER_CAPTION]
        start = chunk[0]["start_s"]
        end = chunk[-1]["start_s"] + chunk[-1]["duration_s"]
        text = " ".join(w["text"] for w in chunk)
        lines.append(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n")
        idx += 1
    Path(out_path).write_text("\n".join(lines))


def estimate_word_events(text: str, duration_s: float) -> list:
    words = text.split()
    if not words:
        return []
    per_word = duration_s / len(words)
    events = []
    t = 0.0
    for w in words:
        events.append({"text": w, "start_s": t, "duration_s": per_word})
        t += per_word
    return events


def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  From real timings: python build_captions.py <captions.json> <out.srt>")
        print("  Estimated:         python build_captions.py --estimate <narration.txt> <duration_s> <out.srt>")
        sys.exit(1)

    if sys.argv[1] == "--estimate":
        narration_text = Path(sys.argv[2]).read_text()
        duration_s = float(sys.argv[3])
        out_path = sys.argv[4]
        events = estimate_word_events(narration_text, duration_s)
        print("NOTE: using estimated (evenly-spaced) word timing, not real TTS timing.")
    else:
        events = json.loads(Path(sys.argv[1]).read_text())
        out_path = sys.argv[2]

    words_to_srt(events, out_path)
    print(f"Wrote {out_path} ({len(events)} words)")


if __name__ == "__main__":
    main()
