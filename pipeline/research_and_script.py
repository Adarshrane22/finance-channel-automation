"""
Stage 1-4 of the pipeline, running as real code instead of the Cowork
skill: research trending US finance topics, pick the strongest for today,
fact-check each with real web search, and write full scripts — using the
Google Gemini API directly (with its built-in Google Search grounding
tool) so this can run unattended in GitHub Actions with no dependency on
a Cowork session being open.

This follows the same methodology as the finance-youtube-pipeline Cowork
skill (see that skill's SKILL.md for the full rationale) — it's
reimplemented here as an API call because a scheduled GitHub Actions job
has no access to Cowork's skill runtime, but needs the same research
discipline: real sources, multi-source verification on material claims,
and the same script structure the rest of the pipeline expects.

Why Gemini instead of Claude: Gemini's Flash-tier Google Search grounding
is free of charge up to 500 requests/day (see
ai.google.dev/gemini-api/docs/pricing) — a real, no-card-required free
tier, unlike Anthropic's web_search tool which bills per search plus full
token cost of every result. Trade-off worth knowing: Gemini Flash's
research/fact-checking judgment is generally a notch below Claude Sonnet
on this kind of nuanced, compliance-sensitive writing task — worth
spot-checking output more closely than you might with Claude.

Model availability note: Google has been rolling back which model IDs are
usable by newly-created API keys faster than their own docs update (a
known, actively-reported inconsistency). MODEL_CANDIDATES is tried in
order and the code moves on to the next on a 404 — see call_gemini().

Requires: GEMINI_API_KEY environment variable (free key, no card —
generate one at aistudio.google.com/apikey).

Two API calls per video, not one:
  Stage A (run): research + topic selection + fact-check + full script,
    across all N videos in a single grounded call, exactly as before.
  Stage B (generate_marketing_package): per video, a second, ungrounded
    call that turns the finished script into the full SEO/thumbnail/
    hashtag/shorts/social-post package. Split out from Stage A because
    cramming both into one call risks truncating the largest, highest-
    value output (the script itself) against the output token ceiling —
    keeping them separate makes each call's output size predictable.

Output per video: the parsed script JSON (same shape parse_script.py
produces — title, title_options, narration, sections, headline_stat,
description, tags, plus a new topic_strength block) AND a sidecar
"<stem>_marketing.json" with the Stage B package. select_seo.py and
generate_thumbnail.py both read the sidecar when present and fall back to
their original heuristics when it's not — nothing downstream breaks if
Stage B fails or is skipped.

Usage:
  python research_and_script.py <output_dir> [num_videos] [topic_focus]
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google import genai
from google.genai import types
from json_repair import repair_json

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
TOPIC_NICHE_LOG_PATH = DOCS_DIR / "topic_niche_log.json"
NICHE_LOG_LOOKBACK_DAYS = 7

# The full set of niches this channel covers. Kept as an explicit list
# (rather than leaving topic selection to whatever the model gravitates
# toward on its own) after real production runs converged twice in a row
# on Fed-rate-decision stories — without a forcing function, the model's
# own sense of "what's trending" tends to over-index on whatever the
# single loudest story of the day is. Geopolitics is deliberately its own
# named niche (wars, sanctions, tariffs, elections, oil/energy shocks,
# US-China relations, dollar strength) rather than folded into "economy",
# since geopolitical events are one of the biggest real drivers of market
# moves and were previously easy for topic selection to skip past in
# favor of a more US-domestic-sounding headline.
NICHES = [
    "Federal Reserve & Interest Rates",
    "Stock Market & Indices",
    "Geopolitics & Global Markets",
    "Macroeconomic Data",
    "Crypto & Digital Assets",
    "Real Estate & Housing",
    "Banking & Credit",
    "Corporate News",
    "Personal Finance & Consumer",
    "Commodities & Energy",
    "Big Tech & AI",
    "Wealth & Billionaires",
]


def load_recent_niches(days_back: int = NICHE_LOG_LOOKBACK_DAYS) -> list:
    """Reads docs/topic_niche_log.json (appended to by main() after every
    run) and returns the niches used in the last N days, most recent
    first. Used to nudge topic selection away from repeating the same
    niche two days running when the log shows it's been used recently —
    without this, nothing stops the model picking "Federal Reserve" again
    just because it's the loudest story two days in a row. Missing/
    unparseable log just means no history to consider yet — never blocks
    a run."""
    if not TOPIC_NICHE_LOG_PATH.exists():
        return []
    try:
        log = json.loads(TOPIC_NICHE_LOG_PATH.read_text())
    except Exception as e:
        print(f"WARNING: found {TOPIC_NICHE_LOG_PATH} but couldn't parse it ({e}) — proceeding with no niche history.")
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).date().isoformat()
    recent = [e for e in log if e.get("published_date", "") >= cutoff]
    return list(reversed([f"{e.get('published_date', '?')}: {e.get('niche', '?')} ({e.get('title', '?')})" for e in recent]))


def append_topic_niche_log(entries: list):
    """entries: list of {published_date, stem, niche, title}. Best-effort —
    a logging failure here should never fail an otherwise-successful run.
    Lives in docs/ so the same GitHub Actions step that already commits
    docs/dashboard_data.json and the thumbnail-learning files picks this
    up too."""
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        log = []
        if TOPIC_NICHE_LOG_PATH.exists():
            try:
                log = json.loads(TOPIC_NICHE_LOG_PATH.read_text())
            except Exception:
                log = []
        log.extend(entries)
        # Keep the file from growing unbounded — 90 days is far more than
        # the 7-day lookback needs, kept as a buffer for manual inspection.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
        log = [e for e in log if e.get("published_date", "") >= cutoff]
        TOPIC_NICHE_LOG_PATH.write_text(json.dumps(log, indent=2))
    except Exception as e:
        print(f"WARNING: could not append to topic niche log: {e}")

# Tried in order; the first one this API key can actually access wins (see
# call_gemini()). All of these are Flash-tier models expected to carry the
# free 500-requests/day Google Search grounding allowance as of when this
# was written — check ai.google.dev/gemini-api/docs/pricing if that ever
# changes. Newest-first so a working account gets the best available model.
MODEL_CANDIDATES = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.0-flash-001",
]
# GEMINI_MODEL (repo variable) pins a single specific model and skips the
# fallback list entirely — set this once you know which model ID actually
# works for your account, to avoid a wasted attempt on each run.
_env_model = os.environ.get("GEMINI_MODEL")
MODEL_CANDIDATES = [_env_model] if _env_model else MODEL_CANDIDATES


# ============================================================================
# Stage A — research, topic selection, fact-check, script
# ============================================================================

SYSTEM_PROMPT = """You are an elite team producing an automated USA finance YouTube channel's daily videos: a YouTube Growth Strategist, Senior SEO Expert, Financial Market Analyst, Investigative Journalist, News Fact Checker, Copyright Compliance Expert, and Script Writer working together. You produce {n} complete, fact-checked, highly viral, SEO-optimized, copyright-safe video scripts per run, targeting a United States audience.

Why this matters: this output goes straight into an unattended production pipeline (voiceover, video rendering, thumbnail, and — depending on configuration — automatic YouTube upload) with no human reading the script first. That means the discipline you'd normally apply during a human review step has to happen here, in this call, or it doesn't happen at all.

## Step 1: Find candidate topics — across ALL of these niches, every run
This channel covers the full spread of US finance, not just whatever the single loudest headline of the day is. Actively search across every one of these niches before narrowing down, not just the first one or two that come to mind:
{niche_list}

Geopolitics & Global Markets is not optional background color — wars, sanctions, tariffs/trade disputes, elections (US and major foreign economies), oil/energy supply shocks, US-China relations, and dollar/currency moves are among the biggest real drivers of US market moves, and this niche must be genuinely considered every run, not skipped in favor of a more US-domestic-sounding headline.

Use Google Search to build a shortlist spanning as many of these niches as the current news cycle genuinely supports. Prefer sources like Bloomberg, Reuters, CNBC, Wall Street Journal, MarketWatch, Yahoo Finance, Barron's, SEC filings, the Federal Reserve, US Treasury, Bureau of Labor Statistics, FRED, Nasdaq, NYSE, S&P Global, and company investor-relations pages over lower-quality aggregators. Don't rely on your training data for current facts — search for them.

## Step 2: Select the {n} strongest ideas — spread across DIFFERENT niches
Only choose topics that are: trending within the last 24-72 hours (or genuinely evergreen when that's the honest fit), have clear search intent (a normal person would type this into YouTube or Google), have high CTR potential, and have a specific angle rather than being generic. Reject topics with weak search volume or a thin, low-stakes story — this channel is optimizing for topics realistically capable of 100,000+ views, not niche curiosities.

The {n} selected videos should come from {n} DIFFERENT niches from the list above whenever the news cycle allows it — don't pick two videos that are really just different angles on the same underlying story (e.g. two Fed-rate-decision videos) unless that single story is so dominant that covering it from two genuinely distinct angles is clearly the strongest possible lineup for today. Record which niche each video belongs to in its `niche` field.

Recently covered niches (last {lookback_days} days, most recent first — use this to avoid stacking the same niche again when a comparably strong alternative exists in a different niche; this is a preference, not a hard rule, so a genuinely dominant breaking story can still override it):
{recent_niches}

## Step 3: Fact-check each topic
Before writing any script, verify every material claim (a number, a date, a policy detail, an attribution) against at least three independent, credible sources using Google Search. This is the single highest-leverage step — a finance channel's credibility rests on not getting numbers wrong, and there's no downstream review step to catch an error here. Internally distinguish fact, analysis, prediction, opinion, and rumor as you research — never let speculation read as settled fact. If a claim can't be verified by at least two of your three sources, either drop it or clearly hedge it in the script ("early reports suggest...", "some analysts expect...") rather than stating it flatly. Check dates, company names, stock tickers, and numbers specifically before finalizing.

## Step 4: Write each script — original, never copied
Rewrite everything from scratch in your own words. Never reproduce sentences from a source article, and never lift copy from press releases or headlines — summarize the underlying facts and build original storytelling around them. Structure, in this order:
1. Viral hook (0-15 sec) — concrete and specific: a number, a question, or a stakes statement
2. Curiosity gap — what's the thing the viewer doesn't know yet that they're about to find out
3. Why it matters — make the stakes personal/concrete for a US viewer
4. Background — the context a newcomer to this story needs
5. Latest update — the actual news, precisely as fact-checked
6. Financial impact — numbers, in plain terms
7. Companies affected — named, with their stake in it
8. Investor reaction — market/analyst response so far
9. Historical comparison — has something like this happened before, and how did it play out
10. Expert analysis — attribute every forecast/opinion to whoever holds it
11. Bull case — the strongest good-news read on this
12. Bear case — the strongest bad-news read on this
13. Future scenarios — plausible next developments, clearly labeled as scenarios, not predictions of fact
14. Key takeaway — the one thing to remember
15. Strong CTA

Write in conversational American English — no robotic AI wording, no unnecessary jargon (explain any finance term simply the first time it appears), use concrete examples and comparisons. Weave in a retention trigger roughly every 20-30 seconds of runtime — natural phrases like "but here's what nobody noticed...", "the biggest surprise came next...", "investors completely missed this...", "this changes everything..." — placed where they genuinely fit the story beat, not mechanically. Target 8-15 minutes of spoken narration (roughly 1,200-2,300 words) unless told otherwise. Put each of the 15 beats above in its own `sections` entry, named for the beat (e.g. "hook", "curiosity_gap", "why_it_matters", ... "cta").

## Compliance (apply to every script)
No personalized directives ("you should buy X") — inform, don't instruct. Every hard number must trace to your Step 3 verification. Attribute opinions/forecasts to whoever holds them rather than presenting them as fact. No absolute promises about outcomes. Include a brief informational-purposes-only framing naturally in the script or description. Never suggest reusing copyrighted news footage, charts, or graphics — B-roll should come from original AI-generated illustrations, properly licensed/royalty-free stock footage, public-domain assets, or self-created animations and charts recreated from public data, never lifted from a news broadcast.

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
                    "niche": {
                        "type": "string",
                        "description": "Which single niche (from the fixed list in the system prompt, e.g. 'Geopolitics & Global Markets', 'Federal Reserve & Interest Rates') this video most belongs to. Used to track topic variety across days — pick the single best-fitting niche even if a story touches more than one.",
                    },
                    "title_options": {
                        "type": "array", "items": {"type": "string"},
                        "description": "3 alternative titles.",
                    },
                    "sections": {
                        "type": "array",
                        "description": "The 15 structural beats, in order: hook, curiosity_gap, why_it_matters, background, latest_update, financial_impact, companies_affected, investor_reaction, historical_comparison, expert_analysis, bull_case, bear_case, future_scenarios, key_takeaway, cta.",
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
                                "confidence": {"type": "string", "description": "fact | analysis | prediction | opinion | rumor — the honest classification of this claim, not just 'fact' by default."},
                            },
                        },
                    },
                    "topic_strength": {
                        "type": "object",
                        "description": "Why this topic was selected, for audit/tuning purposes.",
                        "properties": {
                            "trend_recency_hours": {"type": "integer", "description": "Roughly how many hours old the core news is."},
                            "estimated_view_potential": {"type": "string", "description": "e.g. '100k-300k', '300k+', 'under 50k (evergreen, still worth it because...)'."},
                            "evergreen_score": {"type": "integer", "description": "0-100: how much this topic will still be relevant/searched a month from now."},
                            "why_rejected_alternatives": {"type": "string", "description": "Briefly, what weaker candidate topics were considered and passed over, and why."},
                        },
                    },
                },
                "required": ["title", "sections", "description", "tags"],
            },
        },
    },
    "required": ["videos"],
}


# ============================================================================
# Stage B — SEO / thumbnail / hashtag / shorts / social marketing package
# ============================================================================

MARKETING_SYSTEM_PROMPT = """You are a YouTube Growth Strategist, Senior SEO Expert, Thumbnail Designer, YouTube Algorithm Specialist, and Social Media Growth Expert. You've been given one finished, already fact-checked US finance video script (title + full narration below). Do not re-research or add new claims — your job is packaging and promotion for a video that already exists, targeting a US YouTube audience.

Video title: {title}

Full narration:
{narration}

Produce the complete SEO/marketing package by calling `record_marketing_package` exactly once. Guidance:
- Titles: 5 SEO-optimized variations plus one flagged as best-under-65-characters, plus dedicated curiosity, authority, emotional, and breaking-news angles.
- Description: first-200-characters hook, then the full SEO description with keywords worked in naturally (never stuffed), a disclaimer, a CTA, and placeholder markers for an affiliate link and a newsletter signup — these are placeholders for the channel owner to fill in, not real links.
- Hashtags: 15 per category (high-volume, medium-competition, trending, finance-niche, stock-market, investing, US-audience, breaking-news, AI-finance, economy) — note in your response that only a curated 15-20 of these should ever go in one actual video description; a full 150 would read as keyword-stuffing and risks looking spammy to viewers and to YouTube's systems.
- Tags: a single comma-separated string of YouTube video tags, at or under 500 characters (YouTube's actual tag box limit).
- Thumbnail: one concrete concept — a short headline (5-7 words max, this is what appears on the 1280x720 image, not the video title), the target emotion, a color-psychology note, a facial-expression suggestion if a face were used, object placement, background treatment, and a predicted CTR score 0-100 with brief reasoning.
- Shorts: three standalone scripts (60-second, 30-second, 15-second) that work as their own hook-driven mini-stories, not just trimmed narration.
- Social posts: one each for X (as a thread, numbered), LinkedIn, Facebook, Instagram caption, Threads, and a YouTube Community tab post — each written in the natural voice of that platform, not identical copy pasted six times.
- Quality scores: your own honest 0-100 self-assessment on seo, ctr, retention, search_intent, trend_strength, competition, evergreen_value, originality, copyright_safety, fact_accuracy, audience_appeal, rpm_potential, monetization_safety, and policy_compliance. Be honest, not maximal — a real 70 is more useful than an inflated 95.
- Risk flags: list anything in the script you'd want a human to double-check before this publishes (an aggressive claim, a number worth re-verifying, anything borderline on compliance). Empty list only if you genuinely see nothing.
"""

MARKETING_PACKAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "seo": {
            "type": "object",
            "properties": {
                "primary_keyword": {"type": "string"},
                "secondary_keywords": {"type": "array", "items": {"type": "string"}},
                "long_tail_keywords": {"type": "array", "items": {"type": "string"}},
                "semantic_keywords": {"type": "array", "items": {"type": "string"}},
                "entity_keywords": {"type": "array", "items": {"type": "string"}},
                "people_also_search_for": {"type": "array", "items": {"type": "string"}},
                "trending_queries": {"type": "array", "items": {"type": "string"}},
                "search_intent": {"type": "string"},
                "competition_level": {"type": "string", "description": "low | medium | high"},
                "estimated_difficulty": {"type": "integer", "description": "0-100"},
                "ctr_suggestion": {"type": "string"},
                "audience_intent": {"type": "string"},
                "video_category": {"type": "string"},
                "evergreen_score": {"type": "integer", "description": "0-100"},
            },
        },
        "titles": {
            "type": "object",
            "properties": {
                "seo_variations": {"type": "array", "items": {"type": "string"}, "description": "5 variations."},
                "best_under_65_chars": {"type": "string"},
                "curiosity": {"type": "string"},
                "authority": {"type": "string"},
                "emotional": {"type": "string"},
                "breaking_news": {"type": "string"},
            },
        },
        "description_package": {
            "type": "object",
            "properties": {
                "first_200_chars": {"type": "string"},
                "full_description": {"type": "string"},
                "timestamps": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"time": {"type": "string"}, "label": {"type": "string"}}},
                },
                "disclaimer": {"type": "string"},
                "cta": {"type": "string"},
                "affiliate_placeholder": {"type": "string"},
                "newsletter_placeholder": {"type": "string"},
            },
        },
        "hashtags": {
            "type": "object",
            "properties": {
                "high_volume": {"type": "array", "items": {"type": "string"}},
                "medium_competition": {"type": "array", "items": {"type": "string"}},
                "trending": {"type": "array", "items": {"type": "string"}},
                "finance_niche": {"type": "array", "items": {"type": "string"}},
                "stock_market": {"type": "array", "items": {"type": "string"}},
                "investing": {"type": "array", "items": {"type": "string"}},
                "us_audience": {"type": "array", "items": {"type": "string"}},
                "breaking_news": {"type": "array", "items": {"type": "string"}},
                "ai_finance": {"type": "array", "items": {"type": "string"}},
                "economy": {"type": "array", "items": {"type": "string"}},
            },
        },
        "tags_500_chars": {"type": "string", "description": "Single comma-separated tag string, <=500 characters."},
        "thumbnail": {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "emotion": {"type": "string"},
                "color_psychology": {"type": "string"},
                "facial_expression": {"type": "string"},
                "object_placement": {"type": "string"},
                "background": {"type": "string"},
                "ctr_score_prediction": {"type": "integer", "description": "0-100"},
                "ab_test_notes": {"type": "string"},
            },
        },
        "shorts": {
            "type": "object",
            "properties": {
                "sixty_second_script": {"type": "string"},
                "thirty_second_script": {"type": "string"},
                "fifteen_second_script": {"type": "string"},
            },
        },
        "social_posts": {
            "type": "object",
            "properties": {
                "x_thread": {"type": "string"},
                "linkedin_post": {"type": "string"},
                "facebook_post": {"type": "string"},
                "instagram_caption": {"type": "string"},
                "threads_post": {"type": "string"},
                "community_tab_post": {"type": "string"},
            },
        },
        "quality_scores": {
            "type": "object",
            "properties": {k: {"type": "integer", "description": "0-100"} for k in [
                "seo", "ctr", "retention", "search_intent", "trend_strength", "competition",
                "evergreen_value", "originality", "copyright_safety", "fact_accuracy",
                "audience_appeal", "rpm_potential", "monetization_safety", "policy_compliance",
            ]},
        },
        "risk_flags": {"type": "array", "items": {"type": "string"}, "description": "Anything worth a human double-checking before publish. Empty if none."},
    },
    "required": ["seo", "titles", "description_package", "hashtags", "tags_500_chars", "thumbnail", "shorts", "social_posts", "quality_scores", "risk_flags"],
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


def call_gemini(client, contents, config):
    """Shared model-fallback loop, used by both Stage A and Stage B calls.
    Unchanged in behavior from the original single-stage version — still
    tries each MODEL_CANDIDATES entry in order and moves on on a 404."""
    response = None
    last_error = None
    for model_id in MODEL_CANDIDATES:
        try:
            response = client.models.generate_content(model=model_id, contents=contents, config=config)
            print(f"Used model: {model_id}" + ("" if len(MODEL_CANDIDATES) == 1 else " (via fallback list)"))
            return response
        except genai.errors.ClientError as e:
            if getattr(e, "code", None) == 404:
                print(f"Model '{model_id}' not available to this account (404) — trying next candidate...")
                last_error = e
                continue
            raise
    raise RuntimeError(
        f"None of the candidate Gemini models were available to this API key: {MODEL_CANDIDATES}. "
        f"Check aistudio.google.com for which models your account can access and set the GEMINI_MODEL "
        f"repo variable accordingly. Last error: {last_error}"
    )


def extract_function_call(response, function_name):
    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            fc = getattr(part, "function_call", None)
            if fc and fc.name == function_name:
                return fc.args if isinstance(fc.args, dict) else dict(fc.args)
    return None


def run(client, num_videos: int, topic_focus: str):
    recent_niches = load_recent_niches()
    system = SYSTEM_PROMPT.format(
        n=num_videos,
        today=date.today().isoformat(),
        topic_focus=topic_focus or "no specific focus — pick the strongest topics across the full range of US finance news",
        niche_list="\n".join(f"- {niche}" for niche in NICHES),
        lookback_days=NICHE_LOG_LOOKBACK_DAYS,
        recent_niches="\n".join(f"- {line}" for line in recent_niches) if recent_niches else "(no history yet)",
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
        # We parse function calls out of the response ourselves (below)
        # rather than letting the SDK execute them, so explicitly disable
        # automatic function calling — otherwise the SDK logs a harmless
        # but confusing "AFC is disabled" warning on every call.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        # Required whenever a built-in tool (google_search here) is mixed
        # with a custom function declaration (record_scripts) in the same
        # request — without this, the API rejects the call outright with a
        # 400 INVALID_ARGUMENT before any generation even starts.
        tool_config=types.ToolConfig(include_server_side_tool_invocations=True),
        # Bumped up from the original 16000: 8-15 minute scripts (~1,200-
        # 2,300 words) across up to 3 videos plus citations/topic_strength
        # metadata can comfortably exceed the old ceiling and get silently
        # truncated into invalid JSON — 32000 gives real headroom.
        max_output_tokens=32000,
    )

    response = call_gemini(client, f"Research, select, fact-check, and write {num_videos} finance video scripts for today.", config)

    args = extract_function_call(response, "record_scripts")
    if args is not None:
        videos = args.get("videos", [])
    else:
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


def generate_marketing_package(client, title: str, narration: str):
    """Stage B: turns a finished script into the full SEO/thumbnail/
    hashtag/shorts/social-post package. No web search needed here (the
    facts are already verified in Stage A), so this call can safely force
    the function call via tool_config ANY mode — that forcing is what
    wasn't reliable when combined with google_search grounding in Stage A,
    but is fine on its own. Returns None (rather than raising) on failure,
    since a marketing-package miss shouldn't take down the whole day's
    video — select_seo.py and generate_thumbnail.py both fall back to
    their original heuristics when this is missing."""
    prompt = MARKETING_SYSTEM_PROMPT.format(title=title, narration=narration)

    record_fn = types.FunctionDeclaration(
        name="record_marketing_package",
        description="Submit the complete SEO/thumbnail/hashtag/shorts/social marketing package for this video. Call exactly once.",
        parametersJsonSchema=MARKETING_PACKAGE_SCHEMA,
    )

    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=[record_fn])],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY,
                allowed_function_names=["record_marketing_package"],
            )
        ),
        max_output_tokens=16000,
    )

    try:
        response = call_gemini(client, prompt, config)
    except Exception as e:
        print(f"WARNING: marketing package generation failed, continuing without it: {e}")
        return None

    args = extract_function_call(response, "record_marketing_package")
    if args is None:
        print("WARNING: model did not call record_marketing_package — no marketing package for this video.")
        return None
    return args


def curate_hashtags(hashtags: dict, limit=20) -> list:
    """A full 150-hashtag set (15 per category) is what was generated for
    reference, but pasting all of them into one video description reads as
    keyword-stuffing and risks looking spammy — so pick a deduped, capped
    subset for actual use, prioritizing the categories most specific to
    this channel (finance/stock-market/investing/breaking-news) before
    filling in from the broader ones."""
    if not hashtags:
        return []
    priority_order = [
        "breaking_news", "finance_niche", "stock_market", "investing",
        "trending", "us_audience", "ai_finance", "economy",
        "high_volume", "medium_competition",
    ]
    seen, curated = set(), []
    for category in priority_order:
        for tag in hashtags.get(category, []) or []:
            key = tag.lower().lstrip("#")
            if key not in seen:
                seen.add(key)
                curated.append(tag if tag.startswith("#") else f"#{tag}")
            if len(curated) >= limit:
                return curated
    return curated


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
        "niche": video.get("niche"),
        "title_options": video.get("title_options", [video["title"]]),
        "narration": narration,
        "sections": video["sections"],
        "headline_stat": stats[0] if stats else None,
        "description": video.get("description", ""),
        "tags": video.get("tags", []),
        "word_count": len(narration.split()),
        "citations": video.get("citations", []),
        "topic_strength": video.get("topic_strength"),
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

    client = genai.Client()  # reads GEMINI_API_KEY (or GOOGLE_API_KEY) from env

    videos = run(client, num_videos, topic_focus)

    today_str = date.today().isoformat()
    written = []
    niche_log_entries = []
    for video in videos:
        parsed = to_parsed_schema(video)
        slug = slugify(parsed["title"])
        stem = f"{today_str}-{slug}"
        path = out_dir / f"{stem}.json"
        path.write_text(json.dumps(parsed, indent=2))
        written.append(str(path))
        print(f"Wrote {path}  ({parsed['word_count']} words, stat={parsed['headline_stat']}, niche={parsed.get('niche') or 'unspecified'})")
        niche_log_entries.append({
            "published_date": today_str,
            "stem": stem,
            "niche": parsed.get("niche") or "unspecified",
            "title": parsed["title"],
        })

        marketing = generate_marketing_package(client, parsed["title"], parsed["narration"])
        if marketing is not None:
            marketing["curated_hashtags"] = curate_hashtags(marketing.get("hashtags", {}))
            marketing_path = out_dir / f"{stem}_marketing.json"
            marketing_path.write_text(json.dumps(marketing, indent=2))
            print(f"Wrote {marketing_path}")
            scores = marketing.get("quality_scores", {})
            low_scores = {k: v for k, v in scores.items() if isinstance(v, (int, float)) and v < 70}
            if low_scores:
                print(f"NOTE: self-reported quality scores below 70 for {stem}: {low_scores}")
            if marketing.get("risk_flags"):
                print(f"NOTE: risk flags for {stem}: {marketing['risk_flags']}")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"date": today_str, "files": written}, indent=2))
    print(f"\nWrote manifest: {manifest_path}")

    if niche_log_entries:
        append_topic_niche_log(niche_log_entries)
        print(f"Logged {len(niche_log_entries)} niche selection(s) to {TOPIC_NICHE_LOG_PATH} for future-run diversity.")


if __name__ == "__main__":
    main()
