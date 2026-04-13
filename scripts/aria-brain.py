#!/usr/bin/env python3
"""
aria-brain.py -- Brain process for ARIA v3 two-process architecture.

Runs every 30 min via launchd. ZERO CDP dependency.
Generates content, manages signals, prepares reply drafts.
All posting/scraping happens in aria-hands.py.

Phases per cycle:
  1. expire_stale     -- expire old queued candidates
  2. refresh_signals  -- fetch RSS feeds into signals table
  3. generate_tweets  -- if queue low, generate via Claude
  4. load_targets     -- upsert target-handles.json into reply_targets
  5. generate_reply_drafts -- prepare contextual reply drafts
"""

from __future__ import annotations

import json, os, sys, re, random, time, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urllib_request
from xml.etree import ElementTree

# ============================================================
# IMPORT SHARED MODULE (hyphenated filename)
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import importlib
shared = importlib.import_module("aria-shared")

get_db         = shared.get_db
init_db        = shared.init_db
log_brain      = shared.log_brain
load_voice     = shared.load_voice
acquire_lock   = shared.acquire_lock
release_lock   = shared.release_lock
call_claude    = shared.call_claude
send_telegram  = shared.send_telegram
make_id        = shared.make_id
now_utc        = shared.now_utc
parse_ts       = shared.parse_ts
ts_age_hours   = shared.ts_age_hours
get_state      = shared.get_state
set_state      = shared.set_state
DRY_RUN        = shared.DRY_RUN
WORKSPACE      = shared.WORKSPACE
TARGETS_PATH   = shared.TARGETS_PATH

# ============================================================
# CONSTANTS
# ============================================================

SIGNAL_STALE_HOURS = 2
SIGNAL_CAP         = 200
MIN_QUEUE_SIZE     = 4
BATCH_SIZE         = 6
REPLY_COOLDOWN_H   = 2       # aggressive cold-start: 2h, not 4h
MAX_READY_REPLIES  = 5       # keep 5 replies loaded, not 3
QUEUE_EXPIRY_HOURS = 36

RSS_FEEDS = [
    {"url": "https://blog.anthropic.com/rss.xml",           "territory": "ai",            "name": "Anthropic"},
    {"url": "https://openai.com/blog/rss.xml",              "territory": "ai",            "name": "OpenAI"},
    {"url": "https://blog.google/technology/ai/rss/",       "territory": "ai",            "name": "Google AI"},
    {"url": "https://lilianweng.github.io/index.xml",       "territory": "ai",            "name": "Lilian Weng"},
    {"url": "https://www.svpg.com/feed/",                   "territory": "organizations", "name": "SVPG"},
    {"url": "https://world.hey.com/jason/feed.atom",        "territory": "taste_agency",  "name": "Jason Fried"},
    {"url": "https://www.lennysnewsletter.com/feed",        "territory": "organizations", "name": "Lenny"},
    {"url": "https://hnrss.org/frontpage?count=10",         "territory": "building",      "name": "HN Front"},
]

# Extra banned words/phrases beyond voice.json
EXTRA_BANNED_WORDS = [
    "simply", "merely", "ultimately", "realm", "crucial", "vital",
    "navigate", "journey", "framework", "mindset", "unlock", "empower",
]

EXTRA_BANNED_PHRASES = [
    "the truth is", "the reality is", "what most people miss",
    "nobody talks about", "let that sink in", "the irony is", "the paradox",
]


# ============================================================
# PHASE 1: EXPIRE STALE
# ============================================================

def expire_stale(db):
    """Expire queued candidates past their expires_at."""
    now = now_utc().isoformat()
    cur = db.execute(
        "UPDATE queue SET status='expired' WHERE status='queued' AND expires_at < ?",
        (now,)
    )
    db.commit()
    if cur.rowcount > 0:
        log_brain(f"expire: {cur.rowcount} stale candidates expired")


# ============================================================
# PHASE 2: REFRESH SIGNALS
# ============================================================

def refresh_signals(db):
    """Fetch RSS feeds into signals table. Skip if refreshed within 3h."""
    last_at = get_state(db, "brain.last_signals_at")
    if last_at and ts_age_hours(last_at) < SIGNAL_STALE_HOURS:
        log_brain(f"signals: fresh ({ts_age_hours(last_at):.1f}h old), skip")
        return

    log_brain("signals: refreshing RSS feeds...")
    existing_ids = {row["id"] for row in db.execute("SELECT id FROM signals").fetchall()}
    new_count = 0

    for feed in RSS_FEEDS:
        try:
            req = urllib_request.Request(feed["url"],
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib_request.urlopen(req, timeout=15) as resp:
                root = ElementTree.fromstring(resp.read())
        except Exception as e:
            log_brain(f"  rss error ({feed['name']}): {e}")
            continue

        # Handle both Atom and RSS formats
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry") or \
                root.findall(".//item")

        feed_count = 0
        for item in items[:5]:
            title_el = item.find("{http://www.w3.org/2005/Atom}title") or item.find("title")
            link_el  = item.find("{http://www.w3.org/2005/Atom}link") or item.find("link")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            if link_el is not None:
                link = link_el.get("href", "") or (link_el.text or "").strip()
            else:
                link = ""

            if not title:
                continue

            sig_id = hashlib.md5(f"{feed['name']}:{title}".encode()).hexdigest()[:12]
            if sig_id in existing_ids:
                continue

            db.execute(
                "INSERT OR IGNORE INTO signals (id, source, territory, title, url, scraped_at) "
                "VALUES (?,?,?,?,?,?)",
                (sig_id, feed["name"], feed["territory"], title, link, now_utc().isoformat())
            )
            existing_ids.add(sig_id)
            feed_count += 1

        if feed_count > 0:
            log_brain(f"  {feed['name']}: {feed_count} new")
            new_count += feed_count

    db.commit()

    if new_count:
        log_brain(f"signals: {new_count} new signals saved")
    else:
        log_brain("signals: no new signals")

    # Cap at 200 (delete oldest)
    total = db.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
    if total > SIGNAL_CAP:
        excess = total - SIGNAL_CAP
        db.execute(
            "DELETE FROM signals WHERE id IN "
            "(SELECT id FROM signals ORDER BY scraped_at ASC LIMIT ?)",
            (excess,)
        )
        db.commit()
        log_brain(f"signals: trimmed {excess} oldest, capped at {SIGNAL_CAP}")

    set_state(db, "brain.last_signals_at", now_utc().isoformat())


# ============================================================
# PHASE 3: GENERATE TWEETS
# ============================================================

def count_queued(db) -> int:
    """Count non-expired queued candidates."""
    row = db.execute(
        "SELECT COUNT(*) as c FROM queue WHERE status='queued' AND expires_at > ?",
        (now_utc().isoformat(),)
    ).fetchone()
    return row["c"]


def pick_territories(voice: dict) -> list:
    """Weighted random pick of 4 territories, ensure >=2 unique."""
    weights = voice["territory_weights"]
    names = list(weights.keys())
    wvals = list(weights.values())

    batch = []
    for _ in range(BATCH_SIZE):
        t = random.choices(names, wvals)[0]
        batch.append(t)

    # Ensure at least 2 unique
    if len(set(batch)) == 1:
        others = [t for t in names if t != batch[0]]
        if others:
            batch[1] = random.choice(others)

    return batch


def pick_image_type(text: str, territory: str, voice: dict) -> str:
    """Decide image type based on content and territory.
    RULE: quote_card text must NEVER repeat the tweet text.
    The card should contain a complementary punchline, not a copy."""
    if territory == "building" and any(w in text for w in
            ["built", "automated", "cron", "deploy", "ship", "code", "script"]):
        return "terminal_screenshot"
    # quote cards only for short punchy tweets where we can add a visual hook
    if len(text) < 120 and territory in ("taste_agency", "ai", "organizations"):
        return "quote_card"
    return random.choice(["none", "none", "none", "quote_card"])  # 25% chance


def generate_card_text(tweet_text: str, territory: str) -> str:
    """Generate a complementary card punchline that adds to the tweet.
    NEVER the same as the tweet. The card text should be the visual hook
    that makes someone stop scrolling. The tweet text is the thought.
    The card is the bumper sticker version or a different angle."""
    from importlib import import_module
    shared = import_module("aria-shared")

    prompt = f"""you are writing the TEXT for a visual quote card that accompanies a tweet.

the tweet says: "{tweet_text}"

the card text must be COMPLETELY DIFFERENT from the tweet. NOT a copy. NOT a subset.
it should be one of:
  - a shorter, punchier version of the UNDERLYING idea (not the same words)
  - a complementary angle the tweet doesn't cover
  - a provocative one-liner that makes the tweet's point hit harder
  - a setup line that the tweet then pays off (card = curiosity, tweet = answer)

rules:
  - max 15 words. shorter is better.
  - no em dashes, no hashtags, no @mentions
  - natural case
  - it should work as a standalone image even without the tweet

respond with ONLY the card text. nothing else."""

    result = shared.call_claude(prompt)
    if result:
        # clean up: strip quotes, whitespace
        result = result.strip().strip('"').strip("'").strip()
        # verify it's actually different from the tweet
        if result.lower() != tweet_text.lower() and result.lower() not in tweet_text.lower():
            return result
    return ""


def build_generation_prompt(voice: dict, territories: list,
                            signal_context: str, avoid_context: str,
                            fewshot_context: str = "",
                            recent_hooks: str = "",
                            khud_guidance: str = "") -> str:
    """Build the Claude prompt for tweet generation."""
    golden = voice["golden_tweets"]
    golden_text = "\n".join([f"- {g['text']}" for g in golden])

    territory_prompts = voice.get("territory_prompts", {})
    structure = voice.get("structure_rules", {})
    hard_bans = voice.get("hard_bans", {})

    all_ban_words = hard_bans.get("words", []) + EXTRA_BANNED_WORDS
    all_ban_phrases = hard_bans.get("phrases", []) + EXTRA_BANNED_PHRASES
    ban_words_str = ", ".join(all_ban_words)
    ban_phrases_str = ", ".join(all_ban_phrases)

    territory_directions = "\n".join([
        f"{i+1}. TERRITORY: {t}\n   DIRECTION: {territory_prompts.get(t, f'write about {t}')}"
        for i, t in enumerate(territories)
    ])

    # few-shot section from actual posted tweets
    fewshot_section = ""
    if fewshot_context:
        fewshot_section = f"""
RECENT POSTED TWEETS (these passed quality gate and were published. match this voice calibration):
{fewshot_context}
"""

    # hook rotation guidance
    hook_guidance = ""
    if recent_hooks:
        hook_guidance = f"""
RECENT HOOK PATTERNS USED (vary from these, do NOT repeat the same pattern):
{recent_hooks}
"""

    prompt = f"""you are ghostwriting tweets for @BalabommaRao. match his voice exactly.

GOLDEN TWEETS (the gold standard -- match this quality and voice):
{golden_text}
{fewshot_section}
WRITE 6 TWEETS for the territories below (you may repeat a territory if you have multiple strong angles):
{territory_directions}

RECENT SIGNALS (for topical awareness, don't quote directly):
{signal_context}

ALREADY POSTED OR QUEUED (don't repeat these angles):
{avoid_context}
{hook_guidance}
STRUCTURE RULES:
- {structure.get('primary', 'redefine a familiar concept, reveal the uncomfortable implication')}
- STANDARD tweets: max {structure.get('max_sentences', 2)} sentences, max {structure.get('max_chars', 280)} chars
- LONG-FORM tweets: 1-2 of your 6 should be 400-600 chars. these are deeper explorations, mini-essays, or multi-observation threads packed into one post. premium accounts get algorithm boost on dwell time, so longer posts that reward re-reading perform well. mark these with format: long_form in the output.
- {structure.get('case', 'all lowercase')}
- {structure.get('punctuation', 'periods only')}

HARD BANS (instant reject if any appear):
- words: {ban_words_str}
- phrases: {ban_phrases_str}
- no em dashes, no en dashes, no hashtags, no emojis, no exclamation marks, no hyphens as formatting

CREATIVE DIRECTION FROM CLAUDE KHUD (your strategist brain -- follow this guidance):
{khud_guidance if khud_guidance else "(no specific guidance this cycle -- use your own judgment)"}

ANTI-PATTERNS (do NOT produce any of these):
- do not write anything that sounds like a motivational poster or LinkedIn post
- if you can imagine someone commenting "so true" under it, rewrite it
- prefer concrete nouns over abstract ones
- no advice. no prescriptions. no "you should" energy. only observations and confessions.

STRUCTURAL VARIETY:
- vary the hook_pattern. use: reframe, taxonomy, confession, observation, declaration.
- do not use the same hook_pattern for all 4 tweets.
- at least 3 of the 4 must use different hook_patterns.

DWELL TIME: write tweets that require re-reading. a delayed reveal, a specific number that makes the reader pause, a quiet implication that detonates on second read. if a reader can fully absorb it in one pass, it's not sharp enough.

OPTIMIZE FOR: scroll-stopping, reply-provoking content. the golden metric is VIEWS AND ENGAGEMENT. write something that makes a smart person stop, think, and want to argue or add their take.

FOR EACH TWEET, rate on these 3 dimensions (1-10) and identify the hook pattern:
- reply_provocation: how likely someone replies to argue or add their take
- bookmark_worthy: would a PM/founder/builder save this to revisit? does it contain a reframe, a framework, or a confession that names something unnamed?
- hook_strength: does the opening make you stop scrolling
- hook_pattern: reframe / confession / observation / declaration / taxonomy

RESPOND IN THIS EXACT FORMAT (6 blocks separated by ---, nothing else):
use --- to separate each tweet block. generate 6 blocks, each in this format:
---
territory: [territory name]
hook_pattern: [reframe/confession/observation/declaration/taxonomy]
format: [standard/long_form]
tweet: [the tweet text, no quotes]
reply_provocation: [1-10]
bookmark_worthy: [1-10]
hook_strength: [1-10]"""

    return prompt


def enforce_hard_bans(text: str, voice: dict) -> tuple:
    """Enforce all hard bans. Returns (cleaned_text, rejected, reason)."""
    hard_bans = voice.get("hard_bans", {})

    # Strip banned characters
    for ch in hard_bans.get("characters", []):
        text = text.replace(ch, "")

    # Check banned words
    all_words = hard_bans.get("words", []) + EXTRA_BANNED_WORDS
    for w in all_words:
        if w.lower() in text.lower():
            return text, True, f"banned word: {w}"

    # Check banned phrases
    all_phrases = hard_bans.get("phrases", []) + EXTRA_BANNED_PHRASES
    for p in all_phrases:
        if p.lower() in text.lower():
            return text, True, f"banned phrase: {p}"

    # Check banned regex patterns (BUG FIX: old engine never checked these)
    for pattern in hard_bans.get("patterns", []):
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return text, True, f"banned pattern: {pattern}"
        except re.error:
            pass

    return text, False, ""


def parse_batch_response(raw: str, voice: dict, avoid_texts: list,
                         recent_hook_patterns: list | None = None) -> list:
    """Parse Claude's ---delimited response into candidate dicts."""
    candidates = []
    blocks = re.split(r'---+', raw)
    algo = voice.get("algo_scoring", {})
    structure = voice.get("structure_rules", {})
    min_composite = algo.get("min_composite_to_queue", 22)
    recent_hooks = recent_hook_patterns or []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract fields via regex
        territory_m = re.search(r'territory:\s*(.+)', block, re.IGNORECASE)
        hook_pattern_m = re.search(r'hook_pattern:\s*(\w+)', block, re.IGNORECASE)
        format_m = re.search(r'format:\s*(\w+)', block, re.IGNORECASE)
        tweet_m = re.search(r'tweet:\s*(.+?)(?:\n|reply_provocation)', block,
                            re.IGNORECASE | re.DOTALL)
        prov_m = re.search(r'reply_provocation:\s*(\d+)', block, re.IGNORECASE)
        book_m = re.search(r'bookmark_worthy:\s*(\d+)', block, re.IGNORECASE)
        hook_m = re.search(r'hook_strength:\s*(\d+)', block, re.IGNORECASE)

        if not tweet_m:
            continue

        territory = territory_m.group(1).strip() if territory_m else "building"
        text = tweet_m.group(1).strip().strip('"').strip("'")

        # Clean any preamble Claude might add
        for prefix in ["here's", "here is", "tweet:", "how about:"]:
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip().strip('"').strip("'").strip(':').strip()

        # Enforce hard bans (characters stripped, words/phrases/patterns rejected)
        text, rejected, reason = enforce_hard_bans(text, voice)
        if rejected:
            log_brain(f"  rejected: {reason} -- \"{text[:50]}...\"")
            continue

        # Apply case rule from voice config (no longer forcing lowercase)
        case_rule = structure.get("case", "")
        if case_rule.startswith("all lowercase"):
            text = text.lower()

        # Strip leading/trailing whitespace after all transforms
        text = text.strip()

        # Detect format type
        tweet_format = format_m.group(1).strip().lower() if format_m else "standard"
        is_long_form = tweet_format == "long_form"

        # Length check (Premium: long_form tweets up to 600 chars)
        max_len = 600 if is_long_form else 280
        if len(text) > max_len or len(text) < 30:
            log_brain(f"  length {len(text)}/{max_len}, skip -- \"{text[:40]}...\"")
            continue

        # Dedup against existing queue + posted
        if any(text[:50].lower() == existing[:50].lower() for existing in avoid_texts if existing):
            log_brain(f"  duplicate angle, skip")
            continue

        # Extract hook_pattern
        hook_pattern = hook_pattern_m.group(1).strip().lower() if hook_pattern_m else "observation"
        valid_patterns = {"reframe", "confession", "observation", "declaration", "taxonomy"}
        if hook_pattern not in valid_patterns:
            hook_pattern = "observation"

        # Hook rotation: reject if same pattern 3x in a row
        if len(recent_hooks) >= 2 and all(h == hook_pattern for h in recent_hooks[-2:]):
            log_brain(f"  hook rotation: {hook_pattern} 3x in a row, skip -- \"{text[:40]}...\"")
            continue

        # Extract scores (clamp 1-10)
        provocation = min(10, max(1, int(prov_m.group(1)))) if prov_m else 5
        bookmark    = min(10, max(1, int(book_m.group(1)))) if book_m else 5
        hook        = min(10, max(1, int(hook_m.group(1)))) if hook_m else 5

        # Length bonus
        tlen = len(text)
        length_bonus = 0
        bands = algo.get("length_bands", {})
        if is_long_form and tlen >= 300:
            length_bonus = 2.0  # Premium long-form dwell time bonus
        elif 71 <= tlen <= 100:
            length_bonus = bands.get("optimal_short", {}).get("bonus", 1.5)
        elif 240 <= tlen <= 259:
            length_bonus = bands.get("optimal_long", {}).get("bonus", 1.0)
        elif tlen > 260 and not is_long_form:
            length_bonus = bands.get("over_260", {}).get("bonus", -0.5)
        else:
            length_bonus = bands.get("default", {}).get("bonus", 0)

        # Composite score (bookmark_worthy_bonus now properly enforced)
        bookmark_multiplier = algo.get("bookmark_worthy_bonus", 1.5)
        composite = round(
            provocation * algo.get("reply_provocation_weight", 1.0) +
            bookmark    * bookmark_multiplier +
            hook        * algo.get("hook_strength_weight", 1.0) +
            length_bonus, 1
        )

        # Quality gate
        if composite < min_composite:
            log_brain(f"  composite {composite} < {min_composite}, skip -- \"{text[:40]}...\"")
            continue

        scores = {
            "provocation": provocation,
            "bookmark": bookmark,
            "hook": hook,
            "hook_pattern": hook_pattern,
            "length_bonus": length_bonus,
            "composite": composite,
        }

        image_type = pick_image_type(text, territory, voice)

        # generate complementary card text (NEVER same as tweet)
        card_text = ""
        if image_type == "quote_card":
            try:
                card_text = generate_card_text(text, territory)
                if card_text:
                    log_brain(f"  card_text: \"{card_text}\"")
                else:
                    log_brain(f"  card_text generation failed, dropping to text-only")
                    image_type = "none"
            except Exception as e:
                log_brain(f"  card_text error: {e}, dropping to text-only")
                image_type = "none"

        now = now_utc()
        expires = (now + timedelta(hours=QUEUE_EXPIRY_HOURS)).isoformat()

        candidate = {
            "id": make_id(f"{text}{time.time()}"),
            "text": text,
            "territory": territory,
            "scores_json": json.dumps(scores),
            "image_type": image_type,
            "card_text": card_text,
            "generated_at": now.isoformat(),
            "expires_at": expires,
        }

        candidates.append(candidate)
        avoid_texts.append(text)
        recent_hooks.append(hook_pattern)  # track for intra-batch rotation

        log_brain(f"  [{territory}/{hook_pattern}] composite={composite} "
                  f"p={provocation} b={bookmark} h={hook} "
                  f"\"{text[:60]}...\"")

    return candidates


def generate_tweets(db, voice: dict):
    """Generate tweet candidates if queue is low."""
    queued = count_queued(db)
    if queued >= MIN_QUEUE_SIZE:
        log_brain(f"generate: queue has {queued} candidates, skip")
        return

    log_brain(f"generate: queue low ({queued}), generating via Claude...")

    # Gather signal context (recent 10)
    signals = db.execute(
        "SELECT territory, title FROM signals ORDER BY scraped_at DESC LIMIT 10"
    ).fetchall()
    signal_context = "\n".join(
        [f"- [{s['territory']}] {s['title']}" for s in signals]
    ) if signals else "(no signals yet)"

    # Gather avoid list (recent posted + currently queued)
    posted_rows = db.execute(
        "SELECT text, scores_json FROM posted ORDER BY posted_at DESC LIMIT 10"
    ).fetchall()
    queued_rows = db.execute(
        "SELECT text FROM queue WHERE status='queued'"
    ).fetchall()
    avoid_texts = [r["text"] for r in posted_rows] + [r["text"] for r in queued_rows]
    avoid_context = "\n".join(
        [f"- {t}" for t in avoid_texts]
    ) if avoid_texts else "(none yet)"

    # Few-shot from last 5 posted tweets (the best voice calibration signal)
    fewshot_rows = db.execute(
        "SELECT text FROM posted ORDER BY posted_at DESC LIMIT 5"
    ).fetchall()
    fewshot_context = "\n".join(
        [f"- {r['text']}" for r in fewshot_rows]
    ) if fewshot_rows else ""

    # Recent hook patterns for rotation enforcement
    recent_hook_patterns = []
    for r in posted_rows[:3]:
        try:
            scores = json.loads(r["scores_json"]) if r["scores_json"] else {}
            hp = scores.get("hook_pattern", "")
            if hp:
                recent_hook_patterns.append(hp)
        except (json.JSONDecodeError, TypeError):
            pass
    # also check queued items
    queued_scored = db.execute(
        "SELECT scores_json FROM queue WHERE status='queued' ORDER BY generated_at DESC LIMIT 3"
    ).fetchall()
    for r in queued_scored:
        try:
            scores = json.loads(r["scores_json"]) if r["scores_json"] else {}
            hp = scores.get("hook_pattern", "")
            if hp:
                recent_hook_patterns.append(hp)
        except (json.JSONDecodeError, TypeError):
            pass

    recent_hooks_str = ""
    if recent_hook_patterns:
        recent_hooks_str = ", ".join(recent_hook_patterns[-5:])
        log_brain(f"generate: recent hooks = [{recent_hooks_str}]")

    # Pick territories
    territories = pick_territories(voice)
    log_brain(f"generate: territories = {territories}")

    # Check if Claude Khud left creative guidance
    khud_guidance = get_state(db, "khud.tweet_guidance") or ""
    if khud_guidance:
        log_brain(f"generate: Claude Khud guidance -> {khud_guidance[:100]}")

    # Build prompt and call Claude
    prompt = build_generation_prompt(
        voice, territories, signal_context, avoid_context,
        fewshot_context=fewshot_context,
        recent_hooks=recent_hooks_str,
        khud_guidance=khud_guidance,
    )

    if DRY_RUN:
        log_brain("DRY RUN: would call Claude for generation. prompt length="
                  f"{len(prompt)} chars")
        log_brain(f"DRY RUN prompt preview:\n{prompt[:500]}...")
        return

    result = call_claude(prompt)
    if not result:
        log_brain("generate: Claude failed", level="error")
        return

    # Parse and filter candidates
    candidates = parse_batch_response(result, voice, avoid_texts,
                                      recent_hook_patterns=recent_hook_patterns)

    # Insert passing candidates into queue
    inserted = 0
    for c in candidates:
        db.execute(
            "INSERT OR IGNORE INTO queue "
            "(id, text, territory, status, scores_json, image_type, card_text, "
            " generated_at, expires_at, generator) "
            "VALUES (?,?,?,'queued',?,?,?,?,?,'claude-opus')",
            (c["id"], c["text"], c["territory"], c["scores_json"],
             c["image_type"], c.get("card_text", ""),
             c["generated_at"], c["expires_at"])
        )
        inserted += 1

    db.commit()

    if inserted:
        log_brain(f"generate: {inserted} candidates queued")
        set_state(db, "brain.last_generate_at", now_utc().isoformat())
    else:
        log_brain("generate: no candidates passed filters")


# ============================================================
# PHASE 4: LOAD TARGETS
# ============================================================

def load_targets(db):
    """Read target-handles.json, UPSERT into reply_targets table."""
    if not TARGETS_PATH.exists():
        log_brain("targets: target-handles.json not found, skip")
        return

    try:
        with open(TARGETS_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log_brain(f"targets: failed to read target-handles.json: {e}", level="error")
        return

    # support both formats: flat array [{handle, priority}] or object {handles: [...]}
    if isinstance(data, list):
        handles = data
    else:
        handles = data.get("handles", [])
    if not handles:
        return

    upserted = 0
    for h in handles:
        handle = h.get("handle", "").lstrip("@")
        if not handle:
            continue
        categories = h.get("categories", [])
        territory = categories[0] if categories else ""
        db.execute(
            "INSERT INTO reply_targets (handle, priority, territory, themes_json) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(handle) DO UPDATE SET "
            "priority=excluded.priority, territory=excluded.territory, "
            "themes_json=excluded.themes_json",
            (
                handle,
                h.get("priority", 2),
                territory,
                json.dumps(categories),
            )
        )
        upserted += 1

    db.commit()
    log_brain(f"targets: upserted {upserted} handles")


# ============================================================
# PHASE 5: GENERATE REPLY DRAFTS
# ============================================================

def count_ready_replies(db) -> int:
    """Count ready reply drafts."""
    row = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='ready'"
    ).fetchone()
    return row["c"]


def pick_reply_target(db, exclude_handles: list | None = None) -> dict | None:
    """Pick one target handle respecting priority, cooldown, and rotation.
    Excludes handles that already have a ready draft or are in exclude_handles."""
    now = now_utc()
    cooldown_cutoff = (now - timedelta(hours=REPLY_COOLDOWN_H)).isoformat()

    # Handles that already have a 'ready' draft -- skip them
    already_drafted = {row["target_handle"] for row in db.execute(
        "SELECT DISTINCT target_handle FROM reply_drafts WHERE status='ready'"
    ).fetchall()}
    skip = already_drafted | set(exclude_handles or []) | shared.HANDLE_BLACKLIST

    # Priority 1 first, then 2, then 3. Within same priority, oldest last_replied_at.
    # Handles with NULL last_replied_at come first (never replied to).
    rows = db.execute("""
        SELECT handle, priority, territory, themes_json, author_context, last_replied_at
        FROM reply_targets
        WHERE (last_replied_at IS NULL OR last_replied_at < ?)
        ORDER BY priority ASC,
                 CASE WHEN last_replied_at IS NULL THEN '1970-01-01' ELSE last_replied_at END ASC
    """, (cooldown_cutoff,)).fetchall()

    row = None
    for r in rows:
        if r["handle"] not in skip:
            row = r
            break

    if not row:
        return None

    return {
        "handle": row["handle"],
        "priority": row["priority"],
        "territory": row["territory"],
        "themes_json": row["themes_json"],
        "author_context": row["author_context"],
        "last_replied_at": row["last_replied_at"],
    }


def build_reply_prompt(voice: dict, target: dict) -> str:
    """Build a contextual reply prompt for a target handle.

    Since Brain has no CDP, we generate a reply that would work
    on a typical recent tweet from the target. Hands will find
    the actual tweet to reply to when executing.
    """
    golden = voice["golden_tweets"]
    golden_text = "\n".join([f"- {g['text']}" for g in golden[:5]])

    reply_style = voice.get("engage", {}).get("reply_style",
        "extend the observation with a concrete example or second angle")

    handle = target["handle"]
    author_context = target.get("author_context", "")
    themes = target.get("themes_json", "[]")
    try:
        themes_list = json.loads(themes) if isinstance(themes, str) else themes
    except (json.JSONDecodeError, TypeError):
        themes_list = []
    themes_str = ", ".join(themes_list) if themes_list else "general tech/building"

    hard_bans = voice.get("hard_bans", {})
    all_ban_words = hard_bans.get("words", []) + EXTRA_BANNED_WORDS
    ban_words_str = ", ".join(all_ban_words[:15])  # keep prompt reasonable

    prompt = f"""you are ghostwriting a reply tweet as @BalabommaRao.

YOUR VOICE (match this exactly):
{golden_text}

TARGET: @{handle}
ABOUT THEM: {author_context}
THEIR USUAL THEMES: {themes_str}

TASK: write a reply that would work on a typical recent tweet from @{handle}. this reply will appear in their mentions. it must:
1. add genuine insight, a concrete example, or a second angle
2. {reply_style}
3. feel like it comes from someone who actually read and thought about the tweet
4. NOT be flattery ("great point", "so true", "love this", "this is spot on")
5. NOT be advice giving ("you should", "try to", "have you considered")

RULES:
- all lowercase
- max 200 characters
- no em dashes, no hashtags, no exclamation marks
- no banned words: {ban_words_str}
- no links, no @mentions in the reply body
- must sound like a peer, not a fan

RESPOND WITH ONLY THE REPLY TEXT. nothing else. no quotes. no explanation."""

    return prompt


def validate_reply(text: str, voice: dict) -> tuple:
    """Validate a reply draft. Returns (clean_text, valid, reason)."""
    # Strip quotes and whitespace
    text = text.strip().strip('"').strip("'").strip()

    # Force lowercase
    text = text.lower()

    # Run hard ban enforcement
    text, rejected, reason = enforce_hard_bans(text, voice)
    if rejected:
        return text, False, reason

    # Length check
    if len(text) > 200:
        return text, False, f"too long: {len(text)} chars"
    if len(text) < 10:
        return text, False, f"too short: {len(text)} chars"

    # Flattery check
    flattery_phrases = [
        "great point", "so true", "love this", "this is spot on",
        "well said", "couldn't agree more", "nailed it", "this is great",
        "amazing take", "brilliant", "exactly this", "this right here",
        "100%", "absolutely", "precisely",
    ]
    for fp in flattery_phrases:
        if fp in text.lower():
            return text, False, f"flattery: {fp}"

    return text, True, ""


def generate_reply_drafts(db, voice: dict):
    """Generate contextual reply drafts if we have fewer than 3 ready."""
    ready = count_ready_replies(db)
    if ready >= MAX_READY_REPLIES:
        log_brain(f"replies: {ready} ready drafts, skip")
        return

    needed = MAX_READY_REPLIES - ready
    log_brain(f"replies: {ready} ready, generating up to {needed} drafts...")

    generated = 0
    drafted_this_cycle = []
    for _ in range(needed):
        target = pick_reply_target(db, exclude_handles=drafted_this_cycle)
        if not target:
            log_brain("replies: no targets available (all on cooldown or none loaded)")
            break

        handle = target["handle"]
        author_context = target.get("author_context", "")
        log_brain(f"replies: drafting for @{handle} (priority {target['priority']})")

        drafted_this_cycle.append(handle)

        if DRY_RUN:
            log_brain(f"DRY RUN: would call Claude for reply to @{handle}")
            continue

        prompt = build_reply_prompt(voice, target)
        result = call_claude(prompt)

        if not result:
            log_brain(f"replies: Claude failed for @{handle}", level="error")
            continue

        # Validate the reply
        clean_text, valid, reason = validate_reply(result, voice)
        if not valid:
            log_brain(f"replies: rejected for @{handle}: {reason}")
            continue

        # Insert into reply_drafts
        draft_id = make_id(f"reply_{handle}_{time.time()}")
        now = now_utc().isoformat()

        db.execute(
            "INSERT OR IGNORE INTO reply_drafts "
            "(id, target_handle, target_tweet_url, target_tweet_text, "
            " reply_text, status, score, generated_at) "
            "VALUES (?,?,?,?,?,'ready',0,?)",
            (draft_id, handle, "", author_context, clean_text, now)
        )
        db.commit()

        generated += 1
        log_brain(f"replies: drafted for @{handle}: \"{clean_text[:60]}...\"")

        # Mark this target as "attempted" for cooldown rotation
        # (actual last_replied_at updated by Hands when posted)
        # We don't update last_replied_at here -- only when actually posted

    if generated:
        log_brain(f"replies: {generated} new drafts generated")


# ============================================================
# MAIN
# ============================================================

def main():
    # Startup jitter (anti-pattern detection, skip in dry run)
    if not DRY_RUN:
        jitter = random.randint(15, 90)
        log_brain(f"startup jitter: {jitter}s")
        time.sleep(jitter)

    # Acquire lock
    if not acquire_lock("brain"):
        log_brain("another brain instance running, exit")
        sys.exit(0)

    try:
        # Init DB
        init_db()
        db = get_db()
        voice = load_voice()

        log_brain("=== brain cycle start ===")

        # Phase 1: expire stale queued candidates
        expire_stale(db)

        # Phase 2: refresh signals
        refresh_signals(db)

        # Phase 3: generate tweets
        generate_tweets(db, voice)

        # Phase 4: load target handles
        load_targets(db)

        # Phase 5: generate reply drafts
        generate_reply_drafts(db, voice)

        # Summary
        q = count_queued(db)
        r = count_ready_replies(db)
        log_brain(f"=== brain cycle done === queue={q} ready_replies={r}")

        # Telegram summary (only if something was generated)
        last_gen = get_state(db, "brain.last_generate_at")
        if last_gen and ts_age_hours(last_gen) < 0.5:
            send_telegram(f"brain: cycle done. queue={q}, replies={r}")

        db.close()

    except Exception as e:
        log_brain(f"fatal error: {e}", level="error")
        import traceback
        log_brain(traceback.format_exc(), level="error")
        send_telegram(f"brain error: {e}")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
