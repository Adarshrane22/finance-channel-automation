"""
Stage 1-4 of the pipeline, running as real code instead of the Cowork
skill: research trending US finance topics, pick the 3 strongest for
today, fact-check each with real web search, and write full scripts —
using the Claude API directly (with its server-side web search tool) so
this can run unattended in GitHub Actions with no dependency on a Cowork
session being open.

This follows the same methodology as the finance-youtube-pipeline Cowork
skill (see that skill's SKILL.md for the full rationale) — it's
reimplemented here as an API call because a scheduled GitHub Actions job
has no access to Cowork's skill runtime, but needs the same research
discipline: real sources, two-source verification on material claims, and
the same script structure the rest of the pipeline expects.

Requires: ANTHROPIC_API_KEY environment variable.

Output: writes N JSON files (one per video, matching the schema
parse_script.py produces: title, title_options, narration, sections,
headline_stat, description, tags) directly into the given output
directory — no markdown intermediate, no separate parsing step needed,
since there's no human-authored markdown to parse here.

Usage:
  python research_and_script.py <output_dir> [num_videos] [topic_focus]
"""
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic
from json_repair import repair_json

DEFAULT_MODEL = "claude-sonnet-5"  # check platform.claude.com/docs/en/about-claude/models for the current recommended model — this list changes, and quality here matters since everything downstream depends on it. Override per-run via the ANTHROPIC_MODEL repo variable (e.g. "claude-opus-5" for higher quality at higher cost) without touching code.
MODEL = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

SYSTEM_PROMPT = """You are the research + scriptwriting stage of an automated USA finance YouTube channel's daily pipeline. You produce {n} complete, fact-checked video scripts per run.

Why this matters: this output goes straight into an unattended production pipeline (voiceover, video rendering, thumbnail, and — depending on configuration — automatic YouTube upload) with no human reading the script first. That means the discipline you'd normally apply during a human review step has to happen here, in this call, or it doesn't happen at all.

## Step 1: Find candidate topics
Use web search to build a shortlist of candidate topics from a spread of sources: recent US financial news (rate decisions, earnings, market moves, policy/tax changes), what's being discussed in personal-finance communities right now, and any seasonally relevant evergreen topics for today's date. Don't rely on your training data for current facts — search for them.

## Step 2: Select the {n} strongest ideas
Favor topics that are genuinely timely, have clear search intent (a normal person would type this into YouTube or Google), and have a specific angle rather than being generic. Prefer variety across the selections — don't pick multiple versions of the same story unless the news genuinely warrants it.

## Step 3: Fact-check each topic
Before writing any script, verify every material claim (a number, a date, a policy detail, an attribution) against at least two independent, credible sources using web search. This is the single highest-leverage step — a finance channel's credibility rests on not getting numbers wrong, and there's no downstream review step to catch an error here. If a claim can't be verified by two sources, either drop it or clearly hedge it in the script ("early reports suggest...") rather than stating it flatly.

## Step 4: Write each script
Hook (10-15 seconds, concrete and specific — a number, a question, or a stakes statement), 3-5 key points with real figures woven in naturally, a short call to action. Target 900-1300 spoken words unless told otherwise.

## Compliance (apply to every script)
No personalized directives ("you should buy X") — inform, don't instruct. Every hard number must trace to your two-source verification from Step 3. Attribute opinions/forecasts to whoever holds them rather than presenting them as fact. No absolute promises about outcomes. Include a brief informational-purposes-only framing naturally in the script or description.

## Output format — CRITICAL
Once your research, selection, and fact-checking is complete, call the `record_scripts` tool exactly once with all {n} finished scripts as its `videos` argument. Do not describe the scripts in plain text — the tool call is the only output that reaches the production pipeline. Every section's `text` should be plain narration prose: avoid nested double quotes inside it (write the Fed's dot plot rather than the Fed's "dot plot") since that text flows through several downstream automated steps.

Today's date is {today}. Topic focus for this run: {topic_focus}
"""

RECORD_SCRIPTS_TOOL = {
    "name": "record_scripts",
    "description": "Submit the finished, fact-checked video scripts. Call this exactly once, after research and fact-checking are complete, with every video included.",
    "input_schema": {
        "type": "object",
        "properties": {
            "videos": {
                "type": "array",
                "description": "One entry per finished video script.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "The working title."},
                        "title_options": {
                            "type": "array", "items": {"type": "string"},
                            "description": "3 alternative titles.",
                        },
                        "sections": {
                            "type": "array",
                            "description": "hook, 3-5 key_point_N sections, then cta, in that order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "text": {"type": "string", "description": "Plain narration prose for this section — avoid nested double quotes."},
                                    "stat": {"type": ["string", "null"], "description": "The single most prominent number/stat in this section, or null."},
                                },
                                "required": ["name", "text"],
                            },
                        },
                        "description": {"type": "string", "description": "2-4 sentences plus an informational-purposes disclaimer, suitable as the YouTube video description."},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "8-12 relevant tags."},
                        "citations": {
                            "type": "array",
                            "description": "Every material claim from the fact-checking step, for audit purposes.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "claim": {"type": "string"},
                                    "sources": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                    },
                    "required": ["title", "sections", "description", "tags"],
                },
            },
        },
        "required": ["videos"],
    },
}


def extract_json_array(text: str):
    """Fallback path only — used if the model ever answers in plain text
    instead of calling record_scripts (e.g. an older/different model via
    the ANTHROPIC_MODEL override that doesn't reliably use tools). Claude
    occasionally wraps JSON in stray prose or code fences, and — the actual
    bug hit in production — narration text can contain an unescaped nested
    quote (an em-dash-free "dot plot" style aside) that breaks strict JSON
    parsing partway through. json_repair fixes both classes of issue rather
    than failing the whole day's run over a formatting slip."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in model output. First 500 chars: {text[:500]}")
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        print(f"Strict JSON parse failed ({e}); retrying with json_repair...")
        return json.loads(repair_json(candidate))


def run(num_videos: int, topic_focus: str):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    system = SYSTEM_PROMPT.format(
        n=num_videos,
        today=date.today().isoformat(),
        topic_focus=topic_focus or "no specific focus — pick the strongest topics across the full range of US finance news",
    )

    # Research + fact-check + scripting with web search can comfortably run
    # past 10 minutes for num_videos > 1. The SDK requires streaming for
    # any call that might take that long (non-streaming requests get killed
    # by a client-side timeout ceiling) — .stream() handles that while still
    # giving us one complete assembled response via get_final_message().
    #
    # The final answer is a tool call (record_scripts), not free text. This
    # is the fix for a real production failure: when the scripts were
    # requested as a raw JSON blob in the response text, Claude would
    # occasionally include an unescaped nested quote inside narration prose
    # (e.g. the Fed's "dot plot"), which is perfectly natural English but
    # invalid inside a JSON string — and the whole day's run failed on a
    # JSONDecodeError. Tool calls are validated against a schema server-side
    # before they reach us, so this entire failure class goes away.
    # tool_choice "any" (rather than "auto") means the model must always
    # call *some* tool each turn — either web_search while researching, or
    # record_scripts once it's done — so it can't quietly fall back to a
    # free-text answer that would skip the schema validation.
    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        system=system,
        tools=[
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 20},
            RECORD_SCRIPTS_TOOL,
        ],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": f"Research, select, fact-check, and write {num_videos} finance video scripts for today."}],
    ) as stream:
        response = stream.get_final_message()

    tool_calls = [b for b in response.content if b.type == "tool_use" and b.name == "record_scripts"]
    if tool_calls:
        videos = tool_calls[-1].input.get("videos", [])
    else:
        # Fallback: some model configs may still answer in text despite
        # tool_choice="any" (or ANTHROPIC_MODEL points at one that doesn't
        # support this tool combo). Salvage it rather than failing outright.
        print("WARNING: model did not call record_scripts — falling back to text parsing.")
        text_blocks = [b.text for b in response.content if b.type == "text"]
        if not text_blocks:
            raise RuntimeError("Model response had neither a record_scripts tool call nor text content — check the raw response for an error or empty output.")
        videos = extract_json_array(text_blocks[-1])

    if len(videos) != num_videos:
        print(f"WARNING: requested {num_videos} videos, got {len(videos)}. Proceeding with what was returned.")

    return videos


def to_parsed_schema(video: dict) -> dict:
    """Matches the schema parse_script.py produces, so every downstream
    pipeline script (voiceover, video assembly, thumbnail, SEO) works
    identically whether the script came from a human-reviewed markdown
    file or straight from this API call."""
    narration = "\n\n".join(s["text"] for s in video["sections"])
    stats = [s["stat"] for s in video["sections"] if s.get("stat")]
    return {
        "source_file": "api-generated",
        "title": video["title"],
        "title_options": video.get("title_options", [video["title"]]),
        "narration": narration,
        "sections": video["sections"],
        "headline_stat": stats[0] if stats else None,
        "description": video.get("description", ""),
        "tags": video.get("tags", []),
        "word_count": len(narration.split()),
        "citations": video.get("citations", []),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return "-".join(slug.split("-")[:6])


def main():
    if len(sys.argv) < 2:
        print("Usage: python research_and_script.py <output_dir> [num_videos] [topic_focus]")
        sys.exit(1)

    out_dir = Path(sys.argv[1])
    num_videos = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    topic_focus = sys.argv[3] if len(sys.argv) > 3 else ""

    out_dir.mkdir(parents=True, exist_ok=True)

    videos = run(num_videos, topic_focus)

    today_str = date.today().isoformat()
    written = []
    for video in videos:
        parsed = to_parsed_schema(video)
        slug = slugify(parsed["title"])
        stem = f"{today_str}-{slug}"
        path = out_dir / f"{stem}.json"
        path.write_text(json.dumps(parsed, indent=2))
        written.append(str(path))
        print(f"Wrote {path}  ({parsed['word_count']} words, stat={parsed['headline_stat']})")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"date": today_str, "files": written}, indent=2))
    print(f"\nWrote manifest: {manifest_path}")


if __name__ == "__main__":
    main()
