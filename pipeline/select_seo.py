"""
Picks the strongest title and finalizes description + tags for upload.

If research_and_script.py's Stage B sidecar file ("<stem>_marketing.json",
the SEO/thumbnail/hashtag/shorts/social package) exists next to the parsed
script, its richer title variations, full SEO description, and curated
hashtags/tags are used. If it doesn't exist (an older script, a Stage B
failure, or a hand-written markdown script that never went through the
Gemini pipeline at all), this falls back to the original heuristic-only
behavior exactly as before — nothing here requires the sidecar to exist.

Title scoring (used either way, to rank whichever candidate pool we have):
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
    """Original fallback description builder — used when there's no
    marketing sidecar to draw a richer description from."""
    desc = parsed.get("description", "").strip()
    tags_line = ", ".join(parsed.get("tags", [])[:8])
    disclaimer = "This video is for informational and educational purposes only and does not constitute personalized financial advice."
    parts = [p for p in [desc, disclaimer, f"Topics: {tags_line}" if tags_line else ""] if p]
    return "\n\n".join(parts)


def load_marketing_sidecar(parsed_path: Path):
    """Looks for <stem>_marketing.json next to the parsed script. Returns
    None (not an error) if it doesn't exist or fails to parse — a missing
    or broken sidecar should never block SEO selection, just mean we fall
    back to the original heuristic path."""
    sidecar_path = parsed_path.with_name(parsed_path.stem + "_marketing.json")
    if not sidecar_path.exists():
        return None
    try:
        return json.loads(sidecar_path.read_text())
    except Exception as e:
        print(f"WARNING: found {sidecar_path} but couldn't parse it ({e}) — falling back to heuristic SEO.")
        return None


def build_rich_description(marketing: dict, parsed: dict) -> str:
    desc_pkg = marketing.get("description_package") or {}
    full = (desc_pkg.get("full_description") or "").strip()
    if not full:
        return build_description(parsed)

    parts = [full]
    timestamps = desc_pkg.get("timestamps") or []
    if timestamps:
        ts_lines = "\n".join(f"{t.get('time', '')} - {t.get('label', '')}" for t in timestamps if t.get("time"))
        if ts_lines:
            parts.append(ts_lines)
    if desc_pkg.get("disclaimer"):
        parts.append(desc_pkg["disclaimer"])
    if desc_pkg.get("cta"):
        parts.append(desc_pkg["cta"])
    # Placeholders are left as visible markers (not hidden) so whoever
    # reviews the upload sees exactly where to drop in a real affiliate
    # link / newsletter link before or after publishing.
    if desc_pkg.get("affiliate_placeholder"):
        parts.append(desc_pkg["affiliate_placeholder"])
    if desc_pkg.get("newsletter_placeholder"):
        parts.append(desc_pkg["newsletter_placeholder"])
    curated_hashtags = marketing.get("curated_hashtags") or []
    if curated_hashtags:
        parts.append(" ".join(curated_hashtags))
    return "\n\n".join(p for p in parts if p)


def main():
    if len(sys.argv) < 2:
        print("Usage: python select_seo.py <parsed_script.json> [output.json]")
        sys.exit(1)

    parsed_path = Path(sys.argv[1])
    parsed = json.loads(parsed_path.read_text())
    marketing = load_marketing_sidecar(parsed_path)

    candidates = list(parsed.get("title_options", []) or [parsed["title"]])
    if marketing:
        titles_pkg = marketing.get("titles") or {}
        for key in ["seo_variations", "curiosity", "authority", "emotional", "breaking_news", "best_under_65_chars"]:
            val = titles_pkg.get(key)
            if isinstance(val, list):
                candidates.extend(v for v in val if v)
            elif isinstance(val, str) and val:
                candidates.append(val)
    # De-dupe while preserving order (a title appearing in both the script's
    # own title_options and the marketing package's variations shouldn't be
    # scored/listed twice).
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    scored = sorted((score_title(t) for t in candidates), key=lambda s: s["score"], reverse=True)

    if marketing:
        description = build_rich_description(marketing, parsed)
        tags_string = (marketing.get("tags_500_chars") or "").strip()
        tags = [t.strip() for t in tags_string.split(",") if t.strip()] if tags_string else parsed.get("tags", [])
    else:
        description = build_description(parsed)
        tags = parsed.get("tags", [])

    result = {
        "chosen_title": scored[0]["title"],
        "title_scores": scored,
        "description": description,
        "tags": tags,
        "used_marketing_sidecar": marketing is not None,
    }
    if marketing:
        result["hashtags_curated"] = marketing.get("curated_hashtags", [])
        result["shorts"] = marketing.get("shorts", {})
        result["social_posts"] = marketing.get("social_posts", {})

    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    output_text = json.dumps(result, indent=2)
    if out_path:
        Path(out_path).write_text(output_text)
        print(f"Wrote {out_path}")
    print(output_text)


if __name__ == "__main__":
    main()
