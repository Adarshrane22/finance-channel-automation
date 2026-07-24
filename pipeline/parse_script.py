"""
Parses a script markdown file (produced by the finance-youtube-pipeline skill)
into the pieces the production pipeline needs: the spoken narration text
(hook + key points + CTA, stripped of markdown/headers), plus metadata
(title, suggested titles, description, tags).

This has no external dependencies and needs no network access, so it runs
anywhere.
"""
import re
import sys
import json
from pathlib import Path


def parse_script(md_path: str) -> dict:
    text = Path(md_path).read_text()

    def section(name, next_names):
        # Grab everything between "## {name}" and the next "## " heading, or a "---" divider (or end of file)
        pattern = rf"## {re.escape(name)}\s*\n(.*?)(?=\n## |\n---|\Z)"
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else ""

    title_match = re.search(r"^# (.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Untitled"

    hook = section("Hook (first 10-15 seconds)", [])
    key_points_raw = section("Key points (3-5)", [])
    cta = section("Call to action", [])
    titles_raw = section("Suggested title options (3)", [])
    description = section("Suggested description", [])
    tags_raw = section("Suggested tags", [])

    def clean(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"\*|`", "", s)
        s = re.sub(r"[ \t]+", " ", s)
        return s.strip()

    # Split key points into individual numbered items so downstream stages
    # (video pacing, stat callouts, section pop-ins) can align to them
    # instead of treating the whole block as one blob.
    key_point_items = re.findall(r"^\d+\.\s*(.+?)(?=^\d+\.\s|\Z)", key_points_raw, re.MULTILINE | re.DOTALL)
    key_point_items = [clean(item) for item in key_point_items if clean(item)]

    def find_standout_stat(text: str):
        """Grab the most prominent number in a chunk of text — percentages
        and dollar figures first (most attention-grabbing), then plain
        numbers/ratios — for use as a visual callout. Returns None if
        nothing number-like is present."""
        for pattern in [r"\$[\d,]+(?:\.\d+)?[BMK]?", r"\d+(?:\.\d+)?%", r"\d+-in-\d+", r"\b\d[\d,]*(?:\.\d+)?\b"]:
            m = re.search(pattern, text)
            if m:
                return m.group(0)
        return None

    sections = [{"name": "hook", "text": clean(hook), "stat": find_standout_stat(hook)}]
    for i, item in enumerate(key_point_items, start=1):
        sections.append({"name": f"key_point_{i}", "text": item, "stat": find_standout_stat(item)})
    sections.append({"name": "cta", "text": clean(cta), "stat": None})
    sections = [s for s in sections if s["text"]]

    narration_clean = "\n\n".join(s["text"] for s in sections)
    narration_clean = re.sub(r"\n{3,}", "\n\n", narration_clean).strip()

    title_options = [
        re.sub(r"^\d+\.\s*", "", line).strip()
        for line in titles_raw.splitlines() if line.strip()
    ]

    # The single strongest stat across the whole script — used as the
    # thumbnail's headline number when one stands out.
    all_stats = [s["stat"] for s in sections if s["stat"]]
    headline_stat = all_stats[0] if all_stats else None

    return {
        "source_file": str(md_path),
        "title": title,
        "title_options": title_options,
        "narration": narration_clean,
        "sections": sections,
        "headline_stat": headline_stat,
        "description": description.strip(),
        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()],
        "word_count": len(narration_clean.split()),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python parse_script.py <script.md> [output.json]")
        sys.exit(1)
    result = parse_script(sys.argv[1])
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    if out_path:
        Path(out_path).write_text(json.dumps(result, indent=2))
        print(f"Wrote {out_path}")
    else:
        print(json.dumps(result, indent=2))
