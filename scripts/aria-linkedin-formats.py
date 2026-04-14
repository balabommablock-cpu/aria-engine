#!/usr/bin/env python3
"""
aria-linkedin-formats.py -- LinkedIn post format system (L12-L22, L48).

All format-specific prompts, weights, scheduling, hook/CTA optimization.
Called by aria-khud-li.py during generation.
"""

from __future__ import annotations

import json, random, re
from datetime import datetime, timezone, timedelta

# ============================================================
# FORMAT DEFINITIONS
# ============================================================

FORMATS = {
    "text_observation": {
        "weight": 0.25,
        "min_per_week": 3,
        "max_per_week": 7,
        "prompt_suffix": """FORMAT: Standard text observation.
800-1300 chars. 3-5 short paragraphs. opening line hooks before "see more".
one concrete detail or moment. end with a specific question or punch.""",
    },
    "story_confession": {
        "weight": 0.20,
        "min_per_week": 1,
        "max_per_week": 3,
        "prompt_suffix": """FORMAT: Story/Confession post.
STRUCTURE:
- Hook: "Last year, I [did something unexpected]." or "I used to believe [common belief]. Then [event] happened."
- Body: specific details, one turning point, NOT "lessons learned" format
- Vulnerability without self-pity. The insight emerges from the story.
- Close: "has anyone else experienced this?" or end on the insight.

This is LinkedIn's HIGHEST-COMMENT format. People respond with their own stories.
Builder confessions work especially well: "i spent 36 hours automating something i'd never done manually."
Organization observations disguised as stories: "in my third product meeting this week, i noticed..."
1000-1500 chars. Make it personal and specific.""",
    },
    "contrarian": {
        "weight": 0.12,
        "min_per_week": 0,
        "max_per_week": 2,
        "prompt_suffix": """FORMAT: Contrarian/Thoughtful Disagreement.
STRUCTURE:
- Hook: "[Widely held belief] is wrong." or "I stopped [common practice] and here's what happened."
- Body: state the consensus clearly, present counter-evidence (MUST be specific), acknowledge the strongest counterargument
- Close: "where do you stand on this?" (invites debate)

LinkedIn hot takes must be "thoughtful disagreement with evidence" not aggressive contrarianism.
No personal attacks, no inflammatory language. Evidence required.
800-1300 chars.""",
    },
    "list_post": {
        "weight": 0.12,
        "min_per_week": 1,
        "max_per_week": 2,
        "prompt_suffix": """FORMAT: List Post.
STRUCTURE:
- Hook: "[N] [things] that [benefit/insight]:"
- Body: 5-7 items, each with name + 1 sentence why it matters
- At least 2 items should be non-obvious/surprising
- Close: "what would you add to this list?"

LinkedIn's most-shared format. Each item must be specific, not generic.
NO affiliate links or disguised promotions.
800-1300 chars.""",
    },
    "question_post": {
        "weight": 0.12,
        "min_per_week": 1,
        "max_per_week": 2,
        "prompt_suffix": """FORMAT: Question Post.
STRUCTURE:
Option A: Direct question with 2-3 sentences of context.
Option B: "either/or" with your own answer. "X or Y? I'll go first: [answer + why]."
Option C: Setup observation (2-3 sentences) then a specific question.

The question must be answerable from personal experience (not trivia).
Must be specific enough that answers are interesting.
"what do you think?" is TOO GENERIC. Always be specific.
Include your own answer (seeds the comment section).
500-800 chars. Shorter than other formats.""",
    },
    "framework": {
        "weight": 0.08,
        "min_per_week": 0,
        "max_per_week": 1,
        "prompt_suffix": """FORMAT: Framework Post.
STRUCTURE:
- Hook: "After [X years/projects], I use one framework for [thing]:"
- Name the framework (ideally original or a twist on a known one)
- 3-5 clear steps or dimensions, each with 1 sentence + 1 example
- Close: "save this for next time you face [situation]" or "what frameworks do you use?"

LinkedIn's most-SAVED format. Think 2x2 matrices, step-by-step models.
800-1300 chars.""",
    },
    "carousel_text": {
        "weight": 0.11,
        "min_per_week": 1,
        "max_per_week": 3,
        "prompt_suffix": """FORMAT: Carousel/Slide Deck (text version).
Generate the COMPANION TEXT for a carousel post (the actual slides will be generated separately).

STRUCTURE:
- Hook: bold claim or question that works even without opening the carousel
- 2-3 sentences summarizing the carousel's value
- "Swipe through for the full breakdown."
- Close: CTA question

ALSO generate SLIDE_TEXTS: 8-12 slide texts, each 20-50 words:
  Slide 1: Title slide (bold claim)
  Slides 2-9: One point per slide, each standalone valuable
  Slide 10: Summary/key takeaway
  Slide 11: CTA + handle

IMPORTANT: Include a SLIDES: section after CONTENT: with one line per slide.
Carousels get 2-3x reach on LinkedIn. Each swipe = engagement signal.""",
    },
}


# ============================================================
# FORMAT SELECTION
# ============================================================

def pick_format(db, voice: dict) -> str:
    """Pick the best format for the next post based on weights and recent usage."""
    # Count recent format usage (last 7 days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    # Try to read format_type from linkedin_queue and linkedin_posted
    recent_formats = {}
    try:
        rows = db.execute(
            "SELECT scores_json FROM linkedin_posted WHERE posted_at > ?",
            (cutoff,)
        ).fetchall()
        for r in rows:
            try:
                scores = json.loads(r["scores_json"] or "{}")
                fmt = scores.get("format_type", "text_observation")
                recent_formats[fmt] = recent_formats.get(fmt, 0) + 1
            except (json.JSONDecodeError, KeyError):
                recent_formats["text_observation"] = recent_formats.get("text_observation", 0) + 1
    except Exception:
        pass

    # Calculate adjusted weights
    adjusted = {}
    for fmt_name, fmt_config in FORMATS.items():
        base_weight = fmt_config["weight"]
        used = recent_formats.get(fmt_name, 0)
        max_week = fmt_config["max_per_week"]

        if used >= max_week:
            adjusted[fmt_name] = 0
        else:
            # Boost under-represented formats
            min_week = fmt_config["min_per_week"]
            if used < min_week:
                adjusted[fmt_name] = base_weight * 2
            else:
                adjusted[fmt_name] = base_weight

    # Weighted random selection
    if not adjusted or sum(adjusted.values()) == 0:
        return "text_observation"

    total = sum(adjusted.values())
    r = random.random() * total
    cumulative = 0
    for fmt_name, weight in adjusted.items():
        cumulative += weight
        if r <= cumulative:
            return fmt_name

    return "text_observation"


# ============================================================
# FORMAT-SPECIFIC PROMPT BUILDING
# ============================================================

def get_format_prompt(format_type: str) -> str:
    """Get the format-specific prompt suffix."""
    fmt = FORMATS.get(format_type, FORMATS["text_observation"])
    return fmt["prompt_suffix"]


# ============================================================
# HOOK OPTIMIZER (L21 + L48)
# ============================================================

def optimize_hook(post_text: str, call_llm_fn) -> str:
    """Optimize the first 2 lines for 'see more' clicks.
    Returns the optimized post, or original if already good."""

    lines = post_text.strip().split("\n")
    # Get first non-empty lines
    visible_lines = []
    for line in lines:
        if line.strip():
            visible_lines.append(line.strip())
            if len(visible_lines) >= 2:
                break

    if not visible_lines:
        return post_text

    hook = " ".join(visible_lines)

    # Rule-based checks
    hook_score = 0

    # Specificity: contains a number or specific detail
    if any(c.isdigit() for c in hook):
        hook_score += 3
    # Curiosity gap: ends with colon, incomplete thought
    if hook.rstrip().endswith(":") or hook.rstrip().endswith(","):
        hook_score += 2
    # Personal admission
    if hook.lower().startswith("i ") or "i " in hook.lower()[:20]:
        hook_score += 2
    # Counterintuitive
    contra_signals = ["wrong", "isn't", "doesn't", "never", "stopped", "quit", "myth"]
    if any(s in hook.lower() for s in contra_signals):
        hook_score += 2
    # Length check (under 200 chars for preview)
    if len(hook) <= 200:
        hook_score += 1

    if hook_score >= 7:
        return post_text  # Hook is already good

    # LLM optimization if score is low
    prompt = f"""rewrite ONLY the first 2 lines of this LinkedIn post.
the reader must be unable to scroll past without clicking "see more."

current first lines:
"{hook}"

rest of post (keep unchanged):
"{post_text[len(hook):][:300]}"

proven hook patterns:
- start with a specific number or timeframe
- start with a counterintuitive statement
- start with a personal admission
- start with "I stopped [common thing]"
- end line 2 with a colon or incomplete thought (cliffhanger)

do NOT use: "I'm excited to..." / "Thrilled to..." / "Hot take:" / "Unpopular opinion:"
keep it under 200 characters.

write ONLY the replacement first 2 lines. nothing else."""

    new_hook = call_llm_fn(prompt)
    if new_hook and len(new_hook.strip()) < 250:
        # Replace hook in original text
        rest = post_text[len("\n".join(visible_lines)):].lstrip("\n")
        return new_hook.strip() + "\n\n" + rest
    return post_text


# ============================================================
# CTA OPTIMIZER (L22)
# ============================================================

def optimize_cta(post_text: str, call_llm_fn) -> str:
    """Optimize the closing line for comment generation.
    Returns the optimized post, or original if already good."""

    lines = [l for l in post_text.strip().split("\n") if l.strip()]
    if not lines:
        return post_text

    last_line = lines[-1].strip()

    # Classify CTA
    has_question = "?" in last_line
    generic_questions = ["thoughts?", "what do you think?", "agree?", "agree or disagree?",
                         "what say you?", "right?"]
    is_generic = any(g in last_line.lower() for g in generic_questions)
    is_engagement_bait = any(b in last_line.lower() for b in
                             ["like if", "share if", "tag someone", "let me know in the comments"])

    if has_question and not is_generic and not is_engagement_bait:
        return post_text  # CTA is already good

    # LLM optimization
    prompt = f"""rewrite the closing line of this LinkedIn post to invite specific comments.

current closing:
"{last_line}"

post context (what the post is about):
"{post_text[:300]}"

the question must be:
- answerable from personal experience
- specific enough that answers are interesting to read
- NOT "what do you think?" or "agree?"

good examples:
- "what's one process at your company that everyone follows but nobody believes in?"
- "when was the last time you made a decision purely based on taste, not data?"
- "what's the tool you use most that you'd be embarrassed to admit?"

bad: "thoughts?" / "agree or disagree?" / "like and share if you agree"

write ONLY the replacement closing line. nothing else."""

    new_cta = call_llm_fn(prompt)
    if new_cta and len(new_cta.strip()) < 300:
        lines[-1] = new_cta.strip()
        return "\n\n".join(l for l in "\n".join(lines).split("\n\n") if l.strip())
    return post_text


# ============================================================
# HASHTAG STRATEGY (L49)
# ============================================================

HASHTAG_POOLS = {
    "broad": ["#AI", "#ProductManagement", "#Leadership", "#Innovation", "#Technology"],
    "medium": ["#BuildInPublic", "#AIProducts", "#ProductThinking", "#TechLeadership",
               "#StartupLife", "#FounderLife", "#AgenticAI"],
    "niche": ["#FinTechIndia", "#AIIndia", "#ProductBuilding", "#SoloFounder",
              "#BuilderMindset", "#DesignThinking"],
}

HASHTAG_BANS = {"#motivation", "#success", "#hustle", "#grind", "#mondaymotivation",
                "#thoughtleader", "#networking"}


def pick_hashtags(territory: str, count: int = 4) -> list[str]:
    """Pick 3-5 hashtags: 1 broad, 2 medium, 1 niche."""
    tags = []
    tags.append(random.choice(HASHTAG_POOLS["broad"]))
    tags.extend(random.sample(HASHTAG_POOLS["medium"], min(2, len(HASHTAG_POOLS["medium"]))))
    tags.append(random.choice(HASHTAG_POOLS["niche"]))
    # Dedupe and limit
    seen = set()
    result = []
    for t in tags:
        if t.lower() not in seen and t.lower() not in HASHTAG_BANS:
            result.append(t)
            seen.add(t.lower())
    return result[:count]


# ============================================================
# POST-PROCESSING PIPELINE
# ============================================================

def post_process(post_text: str, territory: str, format_type: str, call_llm_fn) -> str:
    """Apply hook optimization, CTA optimization, and hashtags."""
    # 1. Hook optimization (L21 + L48)
    text = optimize_hook(post_text, call_llm_fn)

    # 2. CTA optimization (L22)
    if format_type != "question_post":  # Questions already have CTAs
        text = optimize_cta(text, call_llm_fn)

    # 3. Hashtags (L49) -- appended at end
    hashtags = pick_hashtags(territory)
    if hashtags:
        text = text.rstrip() + "\n\n" + " ".join(hashtags)

    return text
