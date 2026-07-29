"""
Stage 1-4 of the pipeline, running as real code instead of the Cowork
skill: research trending US finance topics, pick the 3 strongest for
today, fact-check each with real web search, and write full scripts —
using the Google Gemini API directly (with its built-in Google Search
grounding tool) so this can run unattended in GitHub Actions with no
dependency on a Cowork session being open.

This follows the same methodology as the finance-youtube-pipeline Cowork
skill (see that skill's SKILL.md for the full rationale) — it's
reimplemented here as an API call because a scheduled GitHub Actions job
has no access to Cowork's skill runtime, but needs the same research
discipline: real sources, two-source verification on material claims, and
the same script structure the rest of the pipeline expects.

Why Gemini instead of Claude: gemini-2.5-flash's Google Search grounding
is free of charge up to 500 requests/day (see
ai.google.dev/gemini-api/docs/pricing) — a real, no-card-required free
tier, unlike Anthropic's web_search tool which bills per search plus full
token cost of every result. This run uses one request/day, so 500 RPD is
enormous headroom. Trade-off worth knowing: Gemini Flash's research/
fact-checking judgment is generally a notch below Claude Sonnet on this
kind of nuanced, compliance-sensitive writing task — worth spot-checking
the first several days of output more closely than you might with Claude.

Requires: GEMINI_API_KEY environment variable (free key, no card —
generate one at aistudio.google.com/apikey).

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

from google import genai
from google.genai import types
from json_repair import repair_json

DEFAULT_MODEL = "gemini-2.5-flash"  # free-tier Google Search grounding (500 RPD) — check ai.google.dev/gemini-api/docs/pricing before changing, other models may not have free grounding at all. Override per-run via the GEMINI_MODEL repo variable without touching code.
MODEL = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL

SYSTEM_PROMPT = """You are the research + scriptwriting stage of an automated USA finance YouTube channel's daily pipeline. You produce {n} complete, fact-checked video scripts per run.

Why this matters: this output goes straight into an unattended production pipeline (voiceover, video rendering, thumbnail, and — depending on configuration — automatic YouTube upload) with no human reading the script first. That means the discipline you'd normally apply during a human review step has to happen here, in this call, or it doesn't happen at all.

## Step 1: Find candidate topics
Use Google Search to build a shortlist of candidate topics from a spread of sources: recent US financial news (rate decisions, earnings, market moves, policy/tax changes), what's being discussed in personal-finance communities right now, and any seasonally relevant evergreen topics for today's date. Don't rely on your training data for current facts — search for them.

## Step 2: Select the {n} strongest ideas
Favor topics that are genuinely timely, have clear search intent (a normal person would type this into YouTube or Google), and have a specific angle rather than being generic. Prefer variety across the selections — don't pick multiple versions of the same story unless the news genuinely warrants it.

## Step 3: Fact-check each topic
Before writing any script, verify every material claim (a number, a date, a policy detail, an attribution) against at least two independent, credible sources using Google Search. This is the single highest-leverage step — a finance channel's credibility rests on not getting numbers wrong, and there's no downstream review step to catch an error here. If a claim can't be verified by two sources, either drop it or clearly hedge it in the script ("early reports suggest...") rather than stating it flatly.

## Step 4: Write each script
Hook (10-15 seconds, concrete and specific — a number, a question, or a stakes statement), 3-5 key points with real figures woven in naturally, a short call to action. Target 900-1300 spoken words unless told otherwise.

## Compliance (apply to every script)
No personalized directives ("you should buy X") — inform, don't instruct. Every hard number must trace to your two-source verification from Step 3. Attribute opinions/forecasts to whoever holds them rather than presenting them as fact. No absolute promises about outcomes. Include a brief informational-purposes-only framing naturally in the script or description.

## Output format — CRITICAL
Once your research, selection, and fact-checking is complete, call the `record_scripts` function exactly once with all {n} finished scripts as its `videos` argument. Do not describe the scripts in plain text — the function call is the only output that reaches the production pipeline. Every section's `text` should be plain narration prose: avoid nested double quotes inside it (write the Fed's dot plot rather than the Fed's "dot plot") since that text flows through several downstream automated steps.

Today's date is {today}. Topic focus for this run: {topic_focus}
"""

# Gemini's function-calling accepts a raw JSON Schema via
# parameters_json_schema, so this is the same shape as the Anthropic tool
# this replaced — one schema, two providers, easy to keep in sync if you
# ever want to switch back or run both.
RECORD_SCRIPTS_SCHEMA = {
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
                                "stat": {"type": "string", "description": "The single most prominent number/stat in this section, if any (e.g. '3.7%' or '$1,200'). Omit if none."},
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
}


def extract_json_array(text: str):
    """Fallback path only — used if the model ever answers in plain text
    instead of calling record_scripts. Handles stray prose/code fences and
    runs json_repair over the result so a natural nested quote inside
    narration prose (e.g. the Fed's "dot plot") can't crash the whole run
    the way it once did on strict json.loads."""
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
    client = genai.Client()  # reads GEMINI_API_KEY (or GOOGLE_API_KEY) from env

    system = SYSTEM_PROMPT.format(
        n=num_videos,
        today=date.today().isoformat(),
        topic_focus=topic_focus or "no specific focus — pick the strongest topics across the full range of US finance news",
    )

    record_scripts_fn = types.FunctionDeclaration(
        name="record_scripts",
        description="Submit the finished, fact-checked video scripts. Call this exactly once, after research and fact-checking are complete, with every video included.",
        parametersJsonSchema=RECORD_SCRIPTS_SCHEMA,
    )

    config = types.GenerateContentConfig(
        system_instruction=system,
        # Google Search grounding + a custom function declaration in the
        # same request: the model runs searches on its own (server-side,
        # like Claude's web_search tool) and then, once it's satisfied,
        # calls record_scripts with the finished array. tool_config is left
        # at AUTO rather than forced — Gemini's forced-function-calling
        # (ANY mode) is not reliably supported alongside google_search
        # grounding, so this leans on the system prompt instruction instead
        # and falls back to text parsing below if the model ever answers in
        # plain text anyway.
        tools=[
            types.Tool(google_search=types.GoogleSearch()),
            types.Tool(function_declarations=[record_scripts_fn]),
        ],
        max_output_tokens=16000,
    )

    response = client.models.generate_content(
        model=MODEL,
        contents=f"Research, select, fact-check, and write {num_videos} finance video scripts for today.",
        config=config,
    )

    videos = None
    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            fc = getattr(part, "function_call", None)
            if fc and fc.name == "record_scripts":
                args = fc.args if isinstance(fc.args, dict) else dict(fc.args)
                videos = args.get("videos", [])
                break
        if videos is not None:
            break

    if videos is None:
        # Fallback: the model answered in plain text instead of calling
        # record_scripts. Salvage it rather than failing the whole run.
        print("WARNING: model did not call record_scripts — falling back to text parsing.")
        text = getattr(response, "text", None)
        if not text:
            raise RuntimeError("Model response had neither a record_scripts function call nor text content — check the raw response for an error or empty output.")
        videos = extract_json_array(text)

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
