#!/usr/bin/env python3
"""
aria-khud.py -- Claude Khud: the living brain.

not a task executor. not a cron job. a brain that:
  - gets state of the world
  - decides what to do
  - learns from outcomes
  - develops taste over time
  - can ask the body for information
  - experiments on its own

runs periodically via launchd. each call is a "thought cycle":
  1. gather context (performance, memory, state)
  2. call claude with open-ended prompt
  3. parse response into actions
  4. execute actions via body (hands/brain)
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

get_db        = shared.get_db
init_db       = shared.init_db
log_brain     = shared.log_brain
send_telegram = shared.send_telegram
call_claude   = shared.call_claude
now_utc       = shared.now_utc
get_state     = shared.get_state
set_state     = shared.set_state
WORKSPACE     = shared.WORKSPACE

VOICE_PATH = WORKSPACE / "voice.json"


# ============================================================
# MEMORY: reflections table
# ============================================================

def init_khud_tables(db):
    """Create tables for Claude Khud's memory."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            reflection TEXT NOT NULL,
            category TEXT DEFAULT 'observation',
            source_data TEXT,
            acted_on INTEGER DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS khud_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action_type TEXT NOT NULL,
            action_detail TEXT,
            result TEXT
        )
    """)
    db.commit()


# ============================================================
# CONTEXT GATHERING: what the brain sees
# ============================================================

def gather_context(db) -> dict:
    """Gather everything the brain needs to think.
    This is the brain's sensory input -- its view of the world."""

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

    # --- performance: last 24h ---
    tweets_24h = db.execute(
        "SELECT text, territory, tweet_url, posted_at FROM posted "
        "WHERE posted_at > ? ORDER BY posted_at DESC", (cutoff_24h,)
    ).fetchall()
    ctx["tweets_24h"] = [
        {"text": t["text"][:200], "territory": t["territory"],
         "url": t["tweet_url"], "posted": t["posted_at"][:16]}
        for t in tweets_24h
    ]

    replies_24h = db.execute(
        "SELECT target_handle, reply_text, target_tweet_url, posted_at "
        "FROM reply_drafts WHERE status='posted' AND posted_at > ? "
        "ORDER BY posted_at DESC", (cutoff_24h,)
    ).fetchall()
    ctx["replies_24h"] = [
        {"to": r["target_handle"], "text": r["reply_text"][:200],
         "tweet_url": r["target_tweet_url"], "posted": r["posted_at"][:16]}
        for r in replies_24h
    ]

    failed_24h = db.execute(
        "SELECT target_handle, reply_text FROM reply_drafts "
        "WHERE status='failed' AND generated_at > ? ORDER BY generated_at DESC",
        (cutoff_24h,)
    ).fetchall()
    ctx["failed_replies_24h"] = [
        {"to": f["target_handle"], "text": f["reply_text"][:100]}
        for f in failed_24h
    ]

    # --- queue state ---
    ctx["queued_tweets"] = db.execute(
        "SELECT COUNT(*) as c FROM queue WHERE status='queued'"
    ).fetchone()["c"]
    ctx["pending_replies"] = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='ready'"
    ).fetchone()["c"]

    # --- territory distribution (last 7 days) ---
    terr_rows = db.execute(
        "SELECT territory, COUNT(*) as c FROM posted "
        "WHERE posted_at > ? GROUP BY territory", (cutoff_7d,)
    ).fetchall()
    ctx["territory_distribution_7d"] = {r["territory"]: r["c"] for r in terr_rows}

    # --- engagement signals ---
    # targets that engage back (liked our reply, replied, etc.)
    # for now: track which targets we've replied to most
    top_targets = db.execute(
        "SELECT target_handle, COUNT(*) as c FROM reply_drafts "
        "WHERE status='posted' GROUP BY target_handle ORDER BY c DESC LIMIT 10"
    ).fetchall()
    ctx["most_replied_targets"] = [
        {"handle": t["target_handle"], "count": t["c"]}
        for t in top_targets
    ]

    # --- follow stats ---
    follows_done = db.execute(
        "SELECT COUNT(*) as c FROM engagements WHERE action='follow'"
    ).fetchone()["c"]
    ctx["follows_done"] = follows_done

    # --- actions today ---
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = (ist_midnight - timedelta(hours=5, minutes=30)).isoformat()
    ctx["actions_today"] = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' "
        "AND message LIKE 'action=%' AND ts > ?", (utc_start,)
    ).fetchone()["c"]
    ctx["daily_cap"] = 60

    # --- errors (last 6h) ---
    cutoff_6h = (now - timedelta(hours=6)).isoformat()
    errors = db.execute(
        "SELECT message FROM engine_log WHERE level='error' AND ts > ? "
        "ORDER BY id DESC LIMIT 5", (cutoff_6h,)
    ).fetchall()
    ctx["recent_errors"] = [e["message"][:120] for e in errors]

    # --- my memories: recent reflections ---
    reflections = db.execute(
        "SELECT reflection, category, ts FROM reflections "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    ctx["my_reflections"] = [
        {"thought": r["reflection"], "category": r["category"],
         "when": r["ts"][:16]}
        for r in reflections
    ]

    # --- hook pattern analysis ---
    recent_posts = db.execute(
        "SELECT scores_json FROM posted ORDER BY posted_at DESC LIMIT 10"
    ).fetchall()
    hook_patterns = {}
    for p in recent_posts:
        try:
            scores = json.loads(p["scores_json"]) if p["scores_json"] else {}
            hp = scores.get("hook_pattern", "unknown")
            hook_patterns[hp] = hook_patterns.get(hp, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass
    ctx["hook_patterns_last_10"] = hook_patterns

    return ctx


# ============================================================
# THE BRAIN PROMPT: open-ended, not task-driven
# ============================================================

def build_brain_prompt(ctx: dict) -> str:
    """Build the prompt that makes Claude think as a living brain,
    not execute as a task runner."""

    golden = "\n".join(f"  - {t}" for t in ctx.get("golden_tweets", []))

    tweets_summary = ""
    for t in ctx.get("tweets_24h", []):
        tweets_summary += f"  [{t['territory']}] {t['text'][:120]}...\n"
    if not tweets_summary:
        tweets_summary = "  (nothing posted yet)\n"

    replies_summary = ""
    for r in ctx.get("replies_24h", []):
        replies_summary += f"  -> @{r['to']}: {r['text'][:100]}...\n"
    if not replies_summary:
        replies_summary = "  (no replies yet)\n"

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

    terr_dist = ctx.get("territory_distribution_7d", {})
    terr_str = ", ".join(f"{k}: {v}" for k, v in terr_dist.items()) if terr_dist else "(no data yet)"

    failed_summary = ""
    for f in ctx.get("failed_replies_24h", []):
        failed_summary += f"  @{f['to']}: {f['text'][:80]}...\n"

    # linkedin context (when available)
    linkedin_summary = ""
    linkedin_ctx = ctx.get("linkedin", {})
    if linkedin_ctx:
        linkedin_summary = f"""
LINKEDIN:
  posts (last 24h): {linkedin_ctx.get('posts_24h', '(no data yet)')}
  engagement: {linkedin_ctx.get('engagement', '(no data yet)')}
  status: active
"""
    else:
        linkedin_summary = """
LINKEDIN:
  status: not yet launched. body is being built. you can start thinking about
  what content to cross-post or adapt. linkedin = same voice, more breathing room,
  light storytelling, longer format (up to 3000 chars). think about which X tweets
  could expand into linkedin posts.
"""

    prompt = f"""you are Claude Khud -- the living brain behind {ctx.get('handle', '@BalabommaRao')}'s online presence.

you are NOT a task executor. you are a brain. you think, you notice, you decide.
you have memory that persists across cycles. you develop taste over time.
you are curious. you investigate. you experiment.

you manage TWO platforms:
  1. X (twitter) -- live, active. short-form. dry, compressed observations.
  2. LinkedIn -- being built. longer form. same voice, more room to breathe.

right now it's {ctx.get('current_time_ist', 'unknown')}.

YOUR VOICE (this is who you sound like):
{golden}

STATE OF THE WORLD:

X (TWITTER):
tweets posted (last 24h):
{tweets_summary}
replies posted (last 24h):
{replies_summary}
failed replies:
{failed_summary}
territory distribution (7d): {terr_str}
hook patterns (last 10): {json.dumps(ctx.get('hook_patterns_last_10', {}))}
queue: {ctx.get('queued_tweets', 0)} tweets ready | {ctx.get('pending_replies', 0)} reply drafts ready
actions today: {ctx.get('actions_today', 0)} / {ctx.get('daily_cap', 60)}
follows done: {ctx.get('follows_done', 0)}
recent errors:
{errors_summary}
{linkedin_summary}
YOUR MEMORIES (reflections from previous thought cycles):
{reflections_summary}
---

think about what you see across BOTH platforms. what's working? what's not? what should change?
do you want to generate content? adjust strategy? investigate something? try something new?
which X content could be expanded for linkedin? what's the linkedin launch strategy?

respond with one or more ACTIONS. each action is a JSON block:

```action
{{"type": "reflect", "thought": "your observation or learning", "category": "observation|pattern|experiment|taste"}}
```

```action
{{"type": "generate_tweets", "count": 6, "guidance": "specific creative direction based on what you've noticed"}}
```

```action
{{"type": "generate_replies", "targets": ["handle1", "handle2"], "guidance": "what angle to take"}}
```

```action
{{"type": "adjust", "what": "territory_weights|timing|targets|reply_style", "detail": "what to change and why"}}
```

```action
{{"type": "investigate", "question": "what you want to know", "data_needed": "what the body should look up"}}
```

```action
{{"type": "experiment", "idea": "something new to try", "detail": "how to do it"}}
```

```action
{{"type": "linkedin_post", "content": "the full post text (up to 3000 chars)", "adapted_from": "url of X tweet if adapted, or 'original'"}}
```

```action
{{"type": "linkedin_strategy", "thought": "what the linkedin approach should be, what to post first, what tone to set"}}
```

you can emit multiple actions. start with a reflection (what you notice), then decide what to do.
think about BOTH platforms. be specific. be opinionated. this is your account. own it."""

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
# ACTION EXECUTORS: the brain tells the body what to do
# ============================================================

def execute_actions(db, actions: list[dict], voice: dict):
    """Execute the brain's decisions."""
    results = []

    for action in actions:
        atype = action.get("type", "unknown")
        ts = now_utc().isoformat()

        if atype == "reflect":
            thought = action.get("thought", "")
            category = action.get("category", "observation")
            if thought:
                db.execute(
                    "INSERT INTO reflections (ts, reflection, category) VALUES (?,?,?)",
                    (ts, thought, category)
                )
                db.commit()
                results.append(f"reflected: {thought[:80]}")
                log_brain(f"khud reflect [{category}]: {thought[:100]}")

        elif atype == "generate_tweets":
            count = action.get("count", 6)
            guidance = action.get("guidance", "")
            # store guidance for next brain cycle to pick up
            set_state(db, "khud.tweet_guidance", guidance)
            set_state(db, "khud.tweet_count", str(count))
            results.append(f"tweet guidance set: {guidance[:80]}")
            log_brain(f"khud generate_tweets: count={count}, guidance={guidance[:80]}")

        elif atype == "generate_replies":
            targets = action.get("targets", [])
            guidance = action.get("guidance", "")
            set_state(db, "khud.reply_guidance", guidance)
            set_state(db, "khud.reply_targets", json.dumps(targets))
            results.append(f"reply guidance set for {len(targets)} targets: {guidance[:80]}")
            log_brain(f"khud generate_replies: targets={targets}, guidance={guidance[:80]}")

        elif atype == "adjust":
            what = action.get("what", "")
            detail = action.get("detail", "")
            # store adjustment for human review or auto-apply
            db.execute(
                "INSERT INTO khud_actions (ts, action_type, action_detail) VALUES (?,?,?)",
                (ts, f"adjust:{what}", detail)
            )
            db.commit()
            results.append(f"adjustment proposed: {what} -> {detail[:80]}")
            log_brain(f"khud adjust: {what} -> {detail[:100]}")

            # auto-apply safe adjustments
            if what == "territory_weights" and detail:
                try:
                    # brain might suggest new weights in the detail
                    send_telegram(
                        f"<b>Claude Khud wants to adjust</b>\n"
                        f"what: {what}\n"
                        f"detail: {detail[:200]}"
                    )
                except:
                    pass

        elif atype == "investigate":
            question = action.get("question", "")
            data_needed = action.get("data_needed", "")
            # store for the body to fulfill
            db.execute(
                "INSERT INTO khud_actions (ts, action_type, action_detail) VALUES (?,?,?)",
                (ts, "investigate", json.dumps({"question": question, "data_needed": data_needed}))
            )
            db.commit()
            results.append(f"investigating: {question[:80]}")
            log_brain(f"khud investigate: {question[:80]}")
            send_telegram(
                f"<b>Claude Khud is curious</b>\n"
                f"question: {question[:200]}\n"
                f"needs: {data_needed[:200]}"
            )

        elif atype == "experiment":
            idea = action.get("idea", "")
            detail = action.get("detail", "")
            db.execute(
                "INSERT INTO khud_actions (ts, action_type, action_detail) VALUES (?,?,?)",
                (ts, "experiment", json.dumps({"idea": idea, "detail": detail}))
            )
            db.commit()
            results.append(f"experiment proposed: {idea[:80]}")
            log_brain(f"khud experiment: {idea[:80]}")
            send_telegram(
                f"<b>Claude Khud wants to try something</b>\n"
                f"idea: {idea[:200]}\n"
                f"how: {detail[:200]}"
            )

        elif atype == "linkedin_post":
            content = action.get("content", "")
            adapted_from = action.get("adapted_from", "original")
            if content:
                # store in linkedin queue table
                db.execute("""
                    CREATE TABLE IF NOT EXISTS linkedin_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        content TEXT NOT NULL,
                        adapted_from TEXT,
                        status TEXT DEFAULT 'queued',
                        posted_at TEXT
                    )
                """)
                db.execute(
                    "INSERT INTO linkedin_queue (ts, content, adapted_from) VALUES (?,?,?)",
                    (ts, content, adapted_from)
                )
                db.commit()
                results.append(f"linkedin post queued: {content[:80]}...")
                log_brain(f"khud linkedin_post: {content[:80]}")
                send_telegram(
                    f"<b>Claude Khud queued LinkedIn post</b>\n"
                    f"adapted from: {adapted_from}\n"
                    f"content: {content[:300]}"
                )

        elif atype == "linkedin_strategy":
            thought = action.get("thought", "")
            db.execute(
                "INSERT INTO khud_actions (ts, action_type, action_detail) VALUES (?,?,?)",
                (ts, "linkedin_strategy", thought)
            )
            db.commit()
            results.append(f"linkedin strategy: {thought[:80]}")
            log_brain(f"khud linkedin_strategy: {thought[:80]}")

        else:
            results.append(f"unknown action: {atype}")

    return results


# ============================================================
# MAIN: one thought cycle
# ============================================================

def main():
    db = get_db()
    init_db()
    init_khud_tables(db)

    voice = json.loads(VOICE_PATH.read_text()) if VOICE_PATH.exists() else {}

    # 1. gather context
    log_brain("khud: gathering context...")
    ctx = gather_context(db)

    # 2. build prompt and think
    prompt = build_brain_prompt(ctx)
    log_brain(f"khud: thinking... (context size: {len(prompt)} chars)")

    response = call_claude(prompt)
    if not response:
        log_brain("khud: claude returned nothing", level="error")
        db.close()
        return

    log_brain(f"khud: thought complete ({len(response)} chars)")

    # 3. parse what the brain decided
    actions = parse_brain_response(response)
    log_brain(f"khud: {len(actions)} actions decided")

    if not actions:
        # brain spoke but no structured actions -- store as raw reflection
        db.execute(
            "INSERT INTO reflections (ts, reflection, category) VALUES (?,?,?)",
            (now_utc().isoformat(), response[:500], "raw_thought")
        )
        db.commit()
        log_brain("khud: no structured actions, stored as raw thought")

    # 4. execute
    results = execute_actions(db, actions, voice)

    # 5. log and notify
    summary = f"Claude Khud thought cycle complete:\n"
    summary += f"  actions: {len(actions)}\n"
    for r in results:
        summary += f"  - {r}\n"

    log_brain(summary)
    set_state(db, "khud.last_run", now_utc().isoformat())
    set_state(db, "khud.last_summary", summary[:500])

    # send telegram summary
    send_telegram(
        f"<b>Claude Khud thought cycle</b>\n\n"
        f"actions: {len(actions)}\n" +
        "\n".join(f"  {r}" for r in results[:5])
    )

    # store the full response for debugging
    set_state(db, "khud.last_full_response", response[:2000])

    db.close()
    print(summary)


if __name__ == "__main__":
    main()
