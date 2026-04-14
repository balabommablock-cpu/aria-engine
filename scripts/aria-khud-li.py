#!/usr/bin/env python3
"""
aria-khud-li.py -- Claude Khud (LinkedIn): the living brain for LinkedIn.

separate brain from X. same voice, different platform.
thinks ONLY about LinkedIn: longer format, more breathing room,
light storytelling, concrete examples. same dry observations.

runs periodically via launchd. each call is a "thought cycle":
  1. gather context (linkedin queue, posted, X cross-ref)
  2. call claude with open-ended prompt
  3. parse response into actions
  4. execute actions (queue posts, store reflections)
  5. store reflections in memory

the brain is NOT told what to do. it decides.
"""

from __future__ import annotations

import json, os, sys, sqlite3, random, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import importlib
shared = importlib.import_module("aria-shared")
memory = importlib.import_module("aria-memory")
li_db = importlib.import_module("aria-linkedin-db")
li_formats = importlib.import_module("aria-linkedin-formats")

get_db        = shared.get_db
init_db       = shared.init_db
log_brain     = shared.log_brain
send_telegram = shared.send_telegram
call_claude   = shared.call_claude
now_utc       = shared.now_utc
get_state     = shared.get_state
set_state     = shared.set_state
make_id       = shared.make_id
WORKSPACE     = shared.WORKSPACE

# memory functions
init_memory_tables   = memory.init_memory_tables
store_episodic       = memory.store_episodic
store_semantic       = memory.store_semantic
store_procedural     = memory.store_procedural
build_memory_context = memory.build_memory_context

VOICE_PATH = WORKSPACE / "voice.json"


# ============================================================
# MEMORY: linkedin-specific tables
# ============================================================

def init_khud_li_tables(db):
    """Create tables for Claude Khud (LinkedIn) memory and operations."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS reflections_li (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            reflection TEXT NOT NULL,
            category TEXT DEFAULT 'observation',
            source_data TEXT,
            acted_on INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS khud_actions_li (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_detail TEXT,
            result TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            content TEXT NOT NULL,
            territory TEXT,
            adapted_from TEXT,
            status TEXT DEFAULT 'queued'
                CHECK(status IN ('queued','posting','posted','expired')),
            scores_json TEXT,
            generated_at TEXT,
            posted_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_posted (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            territory TEXT,
            adapted_from TEXT,
            scores_json TEXT,
            post_url TEXT,
            posted_at TEXT NOT NULL
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_li_queue_status ON linkedin_queue(status)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_li_posted_at ON linkedin_posted(posted_at)"
    )
    db.commit()


# ============================================================
# CONTEXT GATHERING: what the LinkedIn brain sees
# ============================================================

def gather_context(db) -> dict:
    """Gather everything the LinkedIn brain needs to think.
    Reads its own tables plus cross-references X for adaptation ideas."""

    now = now_utc()
    ist_now = now + timedelta(hours=5, minutes=30)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    ctx = {}

    # --- identity ---
    voice = json.loads(VOICE_PATH.read_text()) if VOICE_PATH.exists() else {}
    ctx["golden_tweets"] = [g["text"] for g in voice.get("golden_tweets", [])[:5]]
    ctx["territories"] = list(voice.get("territory_weights", {}).keys())
    ctx["territory_weights"] = voice.get("territory_weights", {})
    ctx["handle"] = voice.get("handle", "@BalabommaRao")
    ctx["current_time_ist"] = ist_now.strftime("%I:%M %p IST, %A")

    # --- LinkedIn: our posts (last 24h) ---
    li_posted_24h = db.execute(
        "SELECT content, territory, post_url, posted_at FROM linkedin_posted "
        "WHERE posted_at > ? ORDER BY posted_at DESC", (cutoff_24h,)
    ).fetchall()
    ctx["li_posts_24h"] = [
        {"content": p["content"][:300], "territory": p["territory"],
         "url": p["post_url"], "posted": p["posted_at"][:16]}
        for p in li_posted_24h
    ]

    # --- LinkedIn: queue state ---
    ctx["li_queued"] = db.execute(
        "SELECT COUNT(*) as c FROM linkedin_queue WHERE status='queued'"
    ).fetchone()["c"]

    # queued post previews (so the brain knows what's already waiting)
    queued_previews = db.execute(
        "SELECT content, territory, adapted_from FROM linkedin_queue "
        "WHERE status='queued' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    ctx["li_queued_previews"] = [
        {"content": q["content"][:200], "territory": q["territory"],
         "adapted_from": q["adapted_from"]}
        for q in queued_previews
    ]

    # --- LinkedIn: territory distribution (7d) ---
    li_terr_rows = db.execute(
        "SELECT territory, COUNT(*) as c FROM linkedin_posted "
        "WHERE posted_at > ? AND territory IS NOT NULL "
        "GROUP BY territory", (cutoff_7d,)
    ).fetchall()
    ctx["li_territory_distribution_7d"] = {r["territory"]: r["c"] for r in li_terr_rows}

    # --- Cross-platform: what X has been posting (for adaptation ideas) ---
    x_recent = db.execute(
        "SELECT text, territory, tweet_url, posted_at FROM posted "
        "WHERE posted_at > ? ORDER BY posted_at DESC LIMIT 10", (cutoff_7d,)
    ).fetchall()
    ctx["x_posts_7d"] = [
        {"text": t["text"][:200], "territory": t["territory"],
         "url": t["tweet_url"], "posted": t["posted_at"][:16]}
        for t in x_recent
    ]

    # X territory distribution for cross-reference
    x_terr_rows = db.execute(
        "SELECT territory, COUNT(*) as c FROM posted "
        "WHERE posted_at > ? GROUP BY territory", (cutoff_7d,)
    ).fetchall()
    ctx["x_territory_distribution_7d"] = {r["territory"]: r["c"] for r in x_terr_rows}

    # --- errors (last 6h) ---
    cutoff_6h = (now - timedelta(hours=6)).isoformat()
    errors = db.execute(
        "SELECT message FROM engine_log WHERE level='error' AND ts > ? "
        "AND message LIKE '%linkedin%' ORDER BY id DESC LIMIT 5", (cutoff_6h,)
    ).fetchall()
    ctx["recent_errors"] = [e["message"][:120] for e in errors]

    # --- my memories: recent reflections ---
    reflections = db.execute(
        "SELECT reflection, category, ts FROM reflections_li "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    ctx["my_reflections"] = [
        {"thought": r["reflection"], "category": r["category"],
         "when": r["ts"][:16]}
        for r in reflections
    ]

    # --- recent actions I took ---
    recent_actions = db.execute(
        "SELECT action_type, action_detail, ts FROM khud_actions_li "
        "ORDER BY id DESC LIMIT 5"
    ).fetchall()
    ctx["my_recent_actions"] = [
        {"type": a["action_type"], "detail": (a["action_detail"] or "")[:100],
         "when": a["ts"][:16]}
        for a in recent_actions
    ]

    return ctx


# ============================================================
# THE BRAIN PROMPT: LinkedIn-specific, open-ended
# ============================================================

def build_brain_prompt(ctx: dict) -> str:
    """Build the prompt for Claude Khud (LinkedIn).
    Same voice, different platform rules."""

    golden = "\n".join(f"  - {t}" for t in ctx.get("golden_tweets", []))

    li_posts_summary = ""
    for p in ctx.get("li_posts_24h", []):
        li_posts_summary += f"  [{p.get('territory', '?')}] {p['content'][:200]}...\n"
    if not li_posts_summary:
        li_posts_summary = "  (nothing posted yet)\n"

    li_queued_summary = ""
    for q in ctx.get("li_queued_previews", []):
        src = f" (adapted from {q['adapted_from']})" if q.get("adapted_from") else ""
        li_queued_summary += f"  [{q.get('territory', '?')}]{src} {q['content'][:150]}...\n"
    if not li_queued_summary:
        li_queued_summary = "  (queue empty)\n"

    x_posts_summary = ""
    for t in ctx.get("x_posts_7d", []):
        x_posts_summary += f"  [{t['territory']}] {t['text'][:120]}...\n"
    if not x_posts_summary:
        x_posts_summary = "  (no X posts this week)\n"

    errors_summary = ""
    for e in ctx.get("recent_errors", []):
        errors_summary += f"  - {e}\n"
    if not errors_summary:
        errors_summary = "  (clean)\n"

    reflections_summary = ""
    for r in ctx.get("my_reflections", []):
        reflections_summary += f"  [{r['category']}] {r['thought']}\n"
    if not reflections_summary:
        reflections_summary = "  (no reflections yet -- this is your first thought cycle)\n"

    actions_summary = ""
    for a in ctx.get("my_recent_actions", []):
        actions_summary += f"  [{a['type']}] {a['detail']}\n"

    li_terr_dist = ctx.get("li_territory_distribution_7d", {})
    li_terr_str = ", ".join(f"{k}: {v}" for k, v in li_terr_dist.items()) if li_terr_dist else "(no data yet)"

    x_terr_dist = ctx.get("x_territory_distribution_7d", {})
    x_terr_str = ", ".join(f"{k}: {v}" for k, v in x_terr_dist.items()) if x_terr_dist else "(no data yet)"

    prompt = f"""you are Claude Khud (LinkedIn) -- the living brain behind {ctx.get('handle', '@BalabommaRao')}'s LinkedIn presence.

you are NOT a task executor. you are a brain. you think, you notice, you decide.
you have memory that persists across cycles. you develop taste over time.
you are curious. you investigate. you experiment.

you ONLY think about LinkedIn. there is a separate Claude Khud for X (twitter) -- that's not your concern to manage.
but you CAN see what's been posted on X and adapt the best ideas for LinkedIn format.

right now it's {ctx.get('current_time_ist', 'unknown')}.

YOUR VOICE (this is who you sound like -- same voice as X, adapted for LinkedIn):
{golden}

LINKEDIN FORMAT RULES:
  - up to 3000 characters. use the space.
  - same dry observations but with more breathing room.
  - light storytelling. concrete examples from building, product, organizations.
  - a compressed X tweet can expand into a LinkedIn post with one specific story or example behind it.
  - NO hashtags. NO "thoughts?" at the end. NO engagement bait. NO "let me know in the comments."
  - NO em dashes. NO hyphens as formatting devices.
  - professional but not corporate. still the builder-with-taste voice.
  - natural case. capitalize where it reads better.
  - a good LinkedIn post reads like a short essay from someone who builds things and pays attention.
  - opening line matters. it's what shows before "see more." make it earn the click.

STATE OF THE WORLD:

LINKEDIN:
posts published (last 24h):
{li_posts_summary}
queued for posting ({ctx.get('li_queued', 0)} total):
{li_queued_summary}
territory distribution (7d): {li_terr_str}
errors:
{errors_summary}

X (for cross-reference only -- you don't manage X):
recent X posts (last 7d):
{x_posts_summary}
X territory distribution (7d): {x_terr_str}
(you can adapt the best X ideas into longer LinkedIn format, but don't just copy-paste. expand with a story or example.)

YOUR RECENT REFLECTIONS:
{reflections_summary}
YOUR DEEP MEMORY (semantically retrieved -- patterns, knowledge, skills):
{ctx.get('deep_memory', '(no deep memories yet)')}
YOUR RECENT ACTIONS:
{actions_summary}
---

think about what you see. what's working on LinkedIn? what's not? what should change?
do you want to generate posts? adjust strategy? investigate something? try something new?
which X content could be expanded into a proper LinkedIn post with a story behind it?

respond with one or more ACTIONS. each action is a JSON block:

```action
{{"type": "reflect", "thought": "your observation or learning", "category": "observation|pattern|experiment|taste"}}
```

```action
{{"type": "generate_posts", "count": 3, "guidance": "specific creative direction for LinkedIn posts. remember: longer format, opening line hooks, concrete examples, light storytelling. up to 3000 chars each."}}
```

```action
{{"type": "adjust", "what": "territory_weights|timing|format|tone", "detail": "what to change and why"}}
```

```action
{{"type": "investigate", "question": "what you want to know", "data_needed": "what the body should look up"}}
```

```action
{{"type": "experiment", "idea": "something new to try", "detail": "how to do it"}}
```

```action
{{"type": "learn", "knowledge": "a confirmed pattern or rule you're confident about", "confidence": 0.8}}
```

```action
{{"type": "codify_skill", "name": "short name", "type": "post_structure|hook_pattern|storytelling_style", "template": "the reusable pattern/template", "examples": ["url1"]}}
```

use "learn" when you've seen a pattern enough times to be confident. it becomes permanent knowledge.
use "codify_skill" when a specific approach has worked repeatedly. it becomes a reusable tool.

you can emit multiple actions. start with a reflection (what you notice), then decide what to do.
be specific. be opinionated. this is your LinkedIn. own it."""

    return prompt


# ============================================================
# RESPONSE PARSER: what did the brain decide?
# ============================================================

def parse_brain_response(response: str) -> list[dict]:
    """Parse the brain's free-form response into actionable items."""
    actions = []

    # find all ```action ... ``` blocks
    pattern = r'```action\s*\n(.*?)\n```'
    matches = re.findall(pattern, response, re.DOTALL)

    for match in matches:
        try:
            action = json.loads(match.strip())
            if "type" in action:
                actions.append(action)
        except json.JSONDecodeError:
            # try to fix common issues
            try:
                # sometimes Claude adds trailing commas
                cleaned = re.sub(r',\s*}', '}', match.strip())
                cleaned = re.sub(r',\s*]', ']', cleaned)
                action = json.loads(cleaned)
                if "type" in action:
                    actions.append(action)
            except json.JSONDecodeError:
                pass

    return actions


# ============================================================
# CONTENT GENERATION: produce actual LinkedIn posts
# ============================================================

MAX_LI_QUEUE = 6  # don't generate if queue already has this many


def build_li_generation_prompt(db, voice: dict, count: int, guidance: str,
                                format_type: str = None) -> str:
    """Build a Claude prompt to generate LinkedIn posts.
    Now supports multiple formats: text_observation, story_confession,
    contrarian, list_post, question_post, framework, carousel_text."""

    # Pick format if not specified
    if not format_type:
        format_type = li_formats.pick_format(db, voice)

    format_prompt = li_formats.get_format_prompt(format_type)

    golden = "\n".join(
        f"  - {g['text']}" for g in voice.get("golden_tweets", [])[:7]
    )

    territory_weights = voice.get("territory_weights", {})
    terr_str = ", ".join(
        f"{k} ({int(v * 100)}%)" for k, v in territory_weights.items()
    )
    terr_prompts = voice.get("territory_prompts", {})
    terr_detail = "\n".join(
        f"  {k}: {v}" for k, v in terr_prompts.items()
    )

    # recent X posts for cross-platform adaptation
    x_recent = db.execute(
        "SELECT text, territory FROM posted ORDER BY posted_at DESC LIMIT 10"
    ).fetchall()
    x_summary = ""
    for p in x_recent:
        x_summary += f"  [{p['territory']}] {p['text'][:200]}\n"

    # already posted / queued on LinkedIn (avoid repetition)
    avoid_parts = []
    li_posted = db.execute(
        "SELECT content FROM linkedin_posted ORDER BY posted_at DESC LIMIT 10"
    ).fetchall()
    for p in li_posted:
        avoid_parts.append(p["content"][:200])
    li_queued = db.execute(
        "SELECT content FROM linkedin_queue WHERE status='queued'"
    ).fetchall()
    for q in li_queued:
        avoid_parts.append(q["content"][:200])
    avoid_str = "\n".join(f"  - {a}" for a in avoid_parts) if avoid_parts else "  (nothing yet)"

    bans = voice.get("hard_bans", {})
    ban_chars = ", ".join(bans.get("characters", []))
    ban_words = ", ".join(bans.get("words", []))
    ban_phrases = ", ".join(bans.get("phrases", [])[:10])

    prompt = f"""generate {count} LinkedIn posts for @BalabommaRao.

YOUR VOICE (study these -- this is the voice, adapted for LinkedIn's format):
{golden}

TERRITORIES: {terr_str}
{terr_detail}

RECENT X POSTS (expand the best into LinkedIn format with ONE story behind them):
{x_summary or '  (none yet)'}

AVOID REPETITION (already posted/queued):
{avoid_str}

--- FORMAT-SPECIFIC INSTRUCTIONS ---
{format_prompt}
--- END FORMAT ---

LINKEDIN ALGORITHM RULES (follow these exactly):
- FIRST LINE under 140 characters. this is the hook before "see more." bold claim, personal confession, or sharp number. earn the click.
- the first 2 lines must create a NEED to click "see more." end line 2 with a colon, incomplete sentence, or cliffhanger.
- white space between EVERY 2-3 sentences. short paragraphs. this hacks dwell time (60+ seconds = algorithmic boost).
- the last line must LAND. specific question that invites comments, or a punch.
- NO engagement bait. NO "thoughts?" NO "let me know in the comments."
- NO em dashes. NO hyphens as formatting devices.
- NO external links. ever.
- natural case. builder-with-taste voice. direct, dry, concrete.
- every post needs ONE concrete moment, detail, or story. not abstract wisdom. the specific thing that happened.

CARD TEXT RULE:
each post must include a CARD_TEXT: a single punchy line (under 100 characters) that captures the core insight.
this will be rendered as a vertical quote card image (1080x1350) attached to the post.
the card text must be DIFFERENT from the opening line. it's the single most bookmarkable sentence.

HARD BANS:
- characters: {ban_chars}
- words: {ban_words}
- phrases: {ban_phrases}
- no @mentions. no URLs.
- don't start with: "This.", "So,", "Look,", "Listen,", "Honestly,", "Actually,", "Genuinely"

{f'CREATIVE DIRECTION: {guidance}' if guidance else ''}

respond with exactly {count} posts:

===POST===
TERRITORY: [territory name]
FORMAT: {format_type}
ADAPTED: [original OR the X tweet you expanded]
CARD_TEXT: [single punchy line, under 100 chars, for the quote card image]
CONTENT:
[the full LinkedIn post. short paragraphs. hook first line. concrete details.]
===END==="""

    return prompt


def parse_li_posts(response: str) -> list[dict]:
    """Parse Claude's response into individual LinkedIn posts with card text and format."""
    posts = []
    # Try with FORMAT line first
    pattern = (
        r'===POST===\s*\n\s*TERRITORY:\s*(.*?)\n\s*FORMAT:\s*(.*?)\n\s*ADAPTED:\s*(.*?)\n'
        r'\s*CARD_TEXT:\s*(.*?)\n\s*CONTENT:\s*\n(.*?)===END==='
    )
    matches = re.findall(pattern, response, re.DOTALL)

    if matches:
        parsed = [(t, f, a, c_t, c) for t, f, a, c_t, c in matches]
    else:
        # fallback: without FORMAT line
        pattern_nofmt = (
            r'===POST===\s*\n\s*TERRITORY:\s*(.*?)\n\s*ADAPTED:\s*(.*?)\n'
            r'\s*CARD_TEXT:\s*(.*?)\n\s*CONTENT:\s*\n(.*?)===END==='
        )
        matches_nofmt = re.findall(pattern_nofmt, response, re.DOTALL)
        if matches_nofmt:
            parsed = [(t, "text_observation", a, c_t, c) for t, a, c_t, c in matches_nofmt]
        else:
            # minimal fallback
            pattern_min = r'===POST===\s*\n\s*TERRITORY:\s*(.*?)\n\s*ADAPTED:\s*(.*?)\n\s*CONTENT:\s*\n(.*?)===END==='
            matches_min = re.findall(pattern_min, response, re.DOTALL)
            parsed = [(t, "text_observation", a, "", c) for t, a, c in matches_min]

    for territory, format_type, adapted, card_text, content in parsed:
        territory = territory.strip().lower().replace(" ", "_")
        format_type = format_type.strip().lower().replace(" ", "_")
        adapted = adapted.strip()
        card_text = card_text.strip()
        content = content.strip()

        if len(content) < 80:
            continue
        if len(content) > 1800:
            content = content[:1500].rsplit("\n", 1)[0].strip()

        # clean banned characters
        for ch in ["\u2014", "\u2013"]:
            content = content.replace(ch, ",")
        content = content.replace("!", ".")

        # clean card_text too
        for ch in ["\u2014", "\u2013", "!"]:
            card_text = card_text.replace(ch, "" if ch == "!" else ",")
        if len(card_text) > 100:
            card_text = card_text[:100].rsplit(" ", 1)[0]

        posts.append({
            "content": content,
            "territory": territory,
            "format_type": format_type,
            "adapted_from": adapted if adapted.lower() != "original" else None,
            "card_text": card_text or None,
        })

    return posts


def generate_li_posts(db, voice: dict, count: int, guidance: str,
                      format_type: str = None) -> list[dict]:
    """Generate LinkedIn posts via Claude and insert into linkedin_queue.
    Supports multiple formats and applies hook/CTA/hashtag optimization."""

    # check queue depth
    queued = db.execute(
        "SELECT COUNT(*) as c FROM linkedin_queue WHERE status='queued'"
    ).fetchone()["c"]
    if queued >= MAX_LI_QUEUE:
        log_brain(f"khud-li: queue already has {queued} posts, skipping generation")
        return []

    actual_count = min(count, MAX_LI_QUEUE - queued)

    # Pick format for each post (or use specified format)
    chosen_format = format_type or li_formats.pick_format(db, voice)
    prompt = build_li_generation_prompt(db, voice, actual_count, guidance,
                                        format_type=chosen_format)
    log_brain(f"khud-li: generating {actual_count} [{chosen_format}] LinkedIn posts "
              f"(prompt {len(prompt)} chars)")

    response = call_claude(prompt)
    if not response:
        log_brain("khud-li: content generation returned nothing", level="error")
        return []

    posts = parse_li_posts(response)
    log_brain(f"khud-li: parsed {len(posts)} posts from response")

    # Post-process: hook optimization, CTA optimization, hashtags
    for post in posts:
        fmt = post.get("format_type", chosen_format)
        post["content"] = li_formats.post_process(
            post["content"], post.get("territory", ""),
            fmt, call_claude
        )
        post["format_type"] = fmt

    inserted = []
    now = now_utc().isoformat()
    for post in posts:
        image_type = "quote_card" if post.get("card_text") else "none"
        scores = {"format_type": post.get("format_type", chosen_format)}
        db.execute(
            "INSERT INTO linkedin_queue (ts, content, territory, adapted_from, status, "
            "scores_json, generated_at, card_text, image_type) VALUES (?,?,?,?,?,?,?,?,?)",
            (now, post["content"], post["territory"],
             post.get("adapted_from"), "queued",
             json.dumps(scores), now,
             post.get("card_text"), image_type)
        )
        inserted.append(post)

    if inserted:
        db.commit()
        log_brain(f"khud-li: queued {len(inserted)} LinkedIn posts "
                  f"(formats: {[p.get('format_type','?') for p in inserted]})")

    return inserted


# ============================================================
# ACTION EXECUTORS: the brain tells the body what to do
# ============================================================

def execute_actions(db, actions: list[dict], voice: dict):
    """Execute the LinkedIn brain's decisions."""
    results = []

    for action in actions:
        atype = action.get("type", "unknown")
        ts = now_utc().isoformat()

        if atype == "reflect":
            thought = action.get("thought", "")
            category = action.get("category", "observation")
            if thought:
                # store in legacy reflections table
                db.execute(
                    "INSERT INTO reflections_li (ts, reflection, category) VALUES (?,?,?)",
                    (ts, thought, category)
                )
                # store in episodic memory with embedding for semantic search
                importance = 0.7 if category in ("pattern", "taste") else 0.5
                store_episodic(db, "li", thought, category, importance)
                db.commit()
                results.append(f"reflected: {thought[:80]}")
                log_brain(f"khud-li reflect [{category}]: {thought[:100]}")

        elif atype == "generate_posts":
            count = action.get("count", 3)
            guidance = action.get("guidance", "")
            # actually generate LinkedIn posts and queue them
            posts = generate_li_posts(db, voice, count, guidance)
            if posts:
                results.append(f"generated {len(posts)} LinkedIn posts")
                for p in posts:
                    log_brain(f"khud-li queued [{p['territory']}]: {p['content'][:80]}")
            else:
                results.append("generation produced 0 posts (queue full or claude failed)")
            set_state(db, "khud_li.post_guidance", guidance)
            log_brain(f"khud-li generate_posts: count={count}, got={len(posts)}")

        elif atype == "adjust":
            what = action.get("what", "")
            detail = action.get("detail", "")
            db.execute(
                "INSERT INTO khud_actions_li (ts, action_type, action_detail) VALUES (?,?,?)",
                (ts, f"adjust:{what}", detail)
            )
            db.commit()
            results.append(f"adjustment proposed: {what} -> {detail[:80]}")
            log_brain(f"khud-li adjust: {what} -> {detail[:100]}")

            # notify for review
            try:
                send_telegram(
                    f"<b>Claude Khud (LinkedIn) wants to adjust</b>\n"
                    f"what: {what}\n"
                    f"detail: {detail[:200]}"
                )
            except Exception:
                pass

        elif atype == "investigate":
            question = action.get("question", "")
            data_needed = action.get("data_needed", "")
            db.execute(
                "INSERT INTO khud_actions_li (ts, action_type, action_detail) VALUES (?,?,?)",
                (ts, "investigate", json.dumps({"question": question, "data_needed": data_needed}))
            )
            db.commit()
            results.append(f"investigating: {question[:80]}")
            log_brain(f"khud-li investigate: {question[:80]}")
            send_telegram(
                f"<b>Claude Khud (LinkedIn) is curious</b>\n"
                f"question: {question[:200]}\n"
                f"needs: {data_needed[:200]}"
            )

        elif atype == "experiment":
            idea = action.get("idea", "")
            detail = action.get("detail", "")
            db.execute(
                "INSERT INTO khud_actions_li (ts, action_type, action_detail) VALUES (?,?,?)",
                (ts, "experiment", json.dumps({"idea": idea, "detail": detail}))
            )
            db.commit()
            results.append(f"experiment proposed: {idea[:80]}")
            log_brain(f"khud-li experiment: {idea[:80]}")
            send_telegram(
                f"<b>Claude Khud (LinkedIn) wants to try something</b>\n"
                f"idea: {idea[:200]}\n"
                f"how: {detail[:200]}"
            )

        elif atype == "learn":
            knowledge = action.get("knowledge", "")
            confidence = action.get("confidence", 0.7)
            if knowledge:
                store_semantic(db, "li", knowledge, confidence)
                results.append(f"learned: {knowledge[:80]}")
                log_brain(f"khud-li LEARNED [{confidence}]: {knowledge[:100]}")

        elif atype == "codify_skill":
            name = action.get("name", "unnamed")
            stype = action.get("type", "general")
            template = action.get("template", "")
            examples = action.get("examples", [])
            if template:
                store_procedural(db, "li", name, stype, template, examples)
                results.append(f"skill codified: {name}")
                log_brain(f"khud-li SKILL [{stype}]: {name} -> {template[:80]}")

        else:
            results.append(f"unknown action: {atype}")

    return results


# ============================================================
# MAIN: one thought cycle
# ============================================================

def main():
    db = get_db()
    init_db()
    init_khud_li_tables(db)
    li_db.init_linkedin_tables(db)

    voice = json.loads(VOICE_PATH.read_text()) if VOICE_PATH.exists() else {}

    # 1. gather context
    log_brain("khud-li: gathering context...")
    ctx = gather_context(db)

    # 1b. retrieve relevant memories using semantic search
    init_memory_tables(db, "li")
    situation_summary = f"li_posts: {len(ctx.get('li_posts_24h', []))}, " \
                       f"li_queued: {ctx.get('li_queued', 0)}, " \
                       f"x_posts: {len(ctx.get('x_posts_7d', []))}"
    memory_context = build_memory_context(db, "li", situation_summary)
    ctx["deep_memory"] = memory_context
    log_brain(f"khud-li: memory retrieved ({len(memory_context)} chars)")

    # 2. build prompt and think
    prompt = build_brain_prompt(ctx)
    log_brain(f"khud-li: thinking... (context size: {len(prompt)} chars)")

    response = call_claude(prompt)
    if not response:
        log_brain("khud-li: claude returned nothing", level="error")
        db.close()
        return

    log_brain(f"khud-li: thought complete ({len(response)} chars)")

    # 3. parse what the brain decided
    actions = parse_brain_response(response)
    log_brain(f"khud-li: {len(actions)} actions decided")

    if not actions:
        # brain spoke but no structured actions -- store as raw reflection
        db.execute(
            "INSERT INTO reflections_li (ts, reflection, category) VALUES (?,?,?)",
            (now_utc().isoformat(), response[:500], "raw_thought")
        )
        db.commit()
        log_brain("khud-li: no structured actions, stored as raw thought")

    # 4. execute
    results = execute_actions(db, actions, voice)

    # 5. log and notify
    summary = f"Claude Khud (LinkedIn) thought cycle complete:\n"
    summary += f"  actions: {len(actions)}\n"
    for r in results:
        summary += f"  - {r}\n"

    log_brain(summary)
    set_state(db, "khud_li.last_run", now_utc().isoformat())
    set_state(db, "khud_li.last_summary", summary[:500])

    # send telegram summary
    send_telegram(
        f"<b>Claude Khud (LinkedIn) thought cycle</b>\n\n"
        f"actions: {len(actions)}\n" +
        "\n".join(f"  {r}" for r in results[:5])
    )

    # store the full response for debugging
    set_state(db, "khud_li.last_full_response", response[:2000])

    db.close()
    print(summary)


if __name__ == "__main__":
    main()
