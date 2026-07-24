"""
Picks the strongest title from the script's suggested options and finalizes
description + tags for upload, using simple, explainable heuristics rather
than another model call (cheap, deterministic, and easy to override by
hand during the human-review step).

Scoring a title on:
  - Length: YouTube truncates titles in search/suggested around ~60-70
    characters, and very short titles waste the space. Sweet spot ~40-70.
  - Specificity: contains a number, a year, or a named entity (Fed, Tesla,
    a dollar figure) tends to outperform vague titles, on the theory that
    concrete claims earn more trust/clicks than generic ones.
  - Curiosity/stakes language: presence of words like "why", "what",
    "actually", a question mark, or a contrast word ("but", "not") signals
    a hook rather than a flat statement.

This is a heuristic, not a guarantee — treat the score as a tiebreaker
between options that are all already reasonable, not gospel. During human
review, feel free to just pick a different one from the list if it reads
better to you.

Usage:
  python select_seo.py <parsed_script.json> [output.json]
"""
import json
import re
import sys
from pathlib import Path

CURIOSITY_WORDS = ["why", "what", "actually", "real", "here's", "but", "not", "before", "after"]


def score_title(title: str) -> dict:
    length = len(title)
    length_score = 0
    if 40 <= length <= 70:
        length_score = 3
    elif 30 <= length <= 80:
        length_score = 2
    else:
        length_score = 0

    has_number = bool(re.search(r"\d", title))
    has_curiosity = any(w in title.lower() for w in CURIOSITY_WORDS) or "?" in title

    score = length_score + (2 if has_number else 0) + (2 if has_curiosity else 0)
    return {
        "title": title,
        "length": length,
        "has_number": has_number,
        "has_curiosity_language": has_curiosity,
        "score": score,
    }


def build_description(parsed: dict) -> str:
    desc = parsed.get("description", "").strip()
    tags_line = ", ".join(parsed.get("tags", [])[:8])
    disclaimer = "This video is for informational and educational purposes only and does not constitute personalized financial advice."
    parts = [p for p in [desc, disclaimer, f"Topics: {tags_line}" if tags_line else ""] if p]
    return "\n\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print("Usage: python select_seo.py <parsed_script.json> [output.json]")
        sys.exit(1)

    parsed_path = Path(sys.argv[1])
    parsed = json.loads(parsed_path.read_text())

    candidates = parsed.get("title_options", []) or [parsed["title"]]
    scored = sorted((score_title(t) for t in candidates), key=lambda s: s["score"], reverse=True)

    result = {
        "chosen_title": scored[0]["title"],
        "title_scores": scored,
        "description": build_description(parsed),
        "tags": parsed.get("tags", []),
    }

    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    output_text = json.dumps(result, indent=2)
    if out_path:
        Path(out_path).write_text(output_text)
        print(f"Wrote {out_path}")
    print(output_text)


if __name__ == "__main__":
    main()
