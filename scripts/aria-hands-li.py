#!/usr/bin/env python3
"""
aria-hands-li.py -- Hands process for ARIA LinkedIn.

Runs every 20 min via launchd. ONE action per cycle:
  1. Post from linkedin_queue if gap is met
  2. Expire stale queued items

No posting windows (24/7 as requested).
No replies yet (future work).
"""

from __future__ import annotations

import json, os, sys, random, time, subprocess, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import importlib
shared = importlib.import_module("aria-shared")

get_db          = shared.get_db
init_db         = shared.init_db
send_telegram   = shared.send_telegram
now_utc         = shared.now_utc
now_ist         = shared.now_ist
parse_ts        = shared.parse_ts
ts_age_hours    = shared.ts_age_hours
get_state       = shared.get_state
set_state       = shared.set_state
acquire_lock    = shared.acquire_lock
release_lock    = shared.release_lock
DRY_RUN         = shared.DRY_RUN
CDP_URL         = shared.CDP_URL
WORKSPACE       = shared.WORKSPACE

POST_LINKEDIN_JS = SCRIPTS_DIR / "post_linkedin.js"

# import LinkedIn quote card renderer
li_card = importlib.import_module("aria-quote-card-li")

# Config
MIN_GAP_HOURS = 4       # minimum hours between LinkedIn posts
QUEUE_EXPIRY_HOURS = 48  # queued posts expire after this
DAILY_CAP = 4           # max posts per 24h

# LinkedIn optimal posting windows (IST)
# Research: Tue-Thu 8:30-10 AM primary, 12-2 PM secondary
# We allow all days but prefer these windows
LI_POSTING_WINDOWS = [
    ("08:00", "10:30"),  # morning prime
    ("12:00", "14:00"),  # lunch window
    ("17:00", "19:00"),  # evening wind-down
    ("21:00", "23:00"),  # night builders
]


def log_hands_li(msg: str, level: str = "info"):
    shared.log(msg, process="hands_li", level=level)


# ============================================================
# INIT: ensure linkedin tables exist
# ============================================================

def init_li_tables(db):
    """Create LinkedIn-specific tables if not exist."""
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
# CDP: post to LinkedIn
# ============================================================

def do_post_linkedin(content: str, image_path: str = "") -> tuple[bool, str]:
    """Post to LinkedIn via node post_linkedin.js.
    Returns (success, url_or_error)."""

    if not POST_LINKEDIN_JS.exists():
        return False, "post_linkedin.js not found"

    env = os.environ.copy()
    env["CDP_URL"] = CDP_URL
    # playwright lives in the x-twitter-poster skill's node_modules
    env["NODE_PATH"] = os.path.expanduser(
        "~/.openclaw/workspace/skills/x-twitter-poster/node_modules"
    )

    cmd = ["node", str(POST_LINKEDIN_JS), content]
    if image_path and os.path.isfile(image_path):
        cmd.append(image_path)

    try:
        result = subprocess.run(
            cmd, env=env, cwd=str(POST_LINKEDIN_JS.parent),
            capture_output=True, text=True, timeout=240
        )
        if result.returncode == 0:
            # try to extract URL from output
            post_url = None
            for line in result.stdout.split("\n"):
                if "linkedin.com" in line and "feed/update" in line:
                    post_url = line.strip()
                    break
            # also check JSON result line
            for line in result.stdout.split("\n"):
                if line.strip().startswith('{"') or line.strip().startswith("result:"):
                    try:
                        j = json.loads(line.strip().replace("result: ", ""))
                        if j.get("url"):
                            post_url = j["url"]
                    except Exception:
                        pass
            return True, post_url or "posted (url not captured)"
        else:
            err = result.stderr.strip()[:500] or result.stdout.strip()[:500] or "unknown error"
            return False, err
    except subprocess.TimeoutExpired:
        return False, "timeout (240s)"
    except Exception as e:
        return False, str(e)


# ============================================================
# ACTIONS
# ============================================================

def expire_stale(db):
    """Expire queued posts older than QUEUE_EXPIRY_HOURS."""
    cutoff = (now_utc() - timedelta(hours=QUEUE_EXPIRY_HOURS)).isoformat()
    expired = db.execute(
        "UPDATE linkedin_queue SET status='expired' "
        "WHERE status='queued' AND ts < ?", (cutoff,)
    ).rowcount
    if expired:
        db.commit()
        log_hands_li(f"expired {expired} stale queued posts")
    return expired


def posts_today(db) -> int:
    """Count LinkedIn posts in the last 24h."""
    cutoff = (now_utc() - timedelta(hours=24)).isoformat()
    row = db.execute(
        "SELECT COUNT(*) as c FROM linkedin_posted WHERE posted_at > ?",
        (cutoff,)
    ).fetchone()
    return row["c"]


def gap_since_last(db) -> float:
    """Hours since last LinkedIn post. Returns 999 if never posted."""
    row = db.execute(
        "SELECT posted_at FROM linkedin_posted ORDER BY posted_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 999
    return ts_age_hours(row["posted_at"])


def in_li_posting_window() -> tuple[bool, str]:
    """Check if current IST time is in a LinkedIn posting window."""
    ist = shared.now_ist()
    current = ist.strftime("%H:%M")
    for start, end in LI_POSTING_WINDOWS:
        if start <= current <= end:
            return True, f"{start}-{end}"
    # find next window
    upcoming = [s for s, e in LI_POSTING_WINDOWS if s > current]
    if upcoming:
        return False, f"next window: {upcoming[0]}"
    return False, "past all windows today"


def try_post(db) -> str:
    """Attempt to post the next queued LinkedIn post.
    Returns status message."""

    # check daily cap
    today_count = posts_today(db)
    if today_count >= DAILY_CAP:
        return f"daily cap hit ({today_count}/{DAILY_CAP})"

    # check gap
    gap = gap_since_last(db)
    if gap < MIN_GAP_HOURS:
        return f"too soon ({gap:.1f}h < {MIN_GAP_HOURS}h)"

    # grab next queued post (oldest first)
    post = db.execute(
        "SELECT * FROM linkedin_queue WHERE status='queued' "
        "ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not post:
        return "queue empty"

    post_id = post["id"]
    content = post["content"]

    if DRY_RUN:
        log_hands_li(f"DRY RUN: would post [{post['territory']}]: {content[:80]}")
        return f"dry run: {content[:60]}"

    # render quote card image if we have card_text
    image_path = ""
    try:
        card_text = post["card_text"]
    except (IndexError, KeyError):
        card_text = None
    if card_text:
        try:
            territory = post["territory"] or ""
            rendered = li_card.render_for_li_queue(
                card_text, post_id, territory
            )
            if rendered:
                image_path = rendered
                log_hands_li(f"rendered card: {image_path}")
        except Exception as e:
            log_hands_li(f"card render failed: {e}", level="error")

    # mark as posting
    db.execute(
        "UPDATE linkedin_queue SET status='posting' WHERE id=?", (post_id,)
    )
    db.commit()

    # anti-detection delay
    delay = random.randint(15, 45)
    log_hands_li(f"posting in {delay}s: {content[:80]}")
    time.sleep(delay)

    # post via CDP
    success, result = do_post_linkedin(content, image_path)

    if success:
        now = now_utc().isoformat()
        db.execute(
            "INSERT INTO linkedin_posted "
            "(content, territory, adapted_from, scores_json, post_url, posted_at) "
            "VALUES (?,?,?,?,?,?)",
            (content, post["territory"], post["adapted_from"],
             post["scores_json"], result, now)
        )
        db.execute(
            "UPDATE linkedin_queue SET status='posted', posted_at=? WHERE id=?",
            (now, post_id)
        )
        db.commit()

        send_telegram(
            f"<b>ARIA LinkedIn posted</b>\n\n"
            f"territory: {post['territory']}\n"
            f"{content[:300]}...\n\n"
            f"url: {result}"
        )
        log_hands_li(f"posted [{post['territory']}]: {content[:80]}")
        return f"posted: {content[:60]}"
    else:
        # revert to queued
        db.execute(
            "UPDATE linkedin_queue SET status='queued' WHERE id=?", (post_id,)
        )
        db.commit()
        log_hands_li(f"post FAILED: {result}", level="error")
        send_telegram(
            f"<b>ARIA LinkedIn post FAILED</b>\n\n{result[:300]}"
        )
        return f"failed: {result[:60]}"


# ============================================================
# MAIN
# ============================================================

def main():
    # single-instance lock
    if not acquire_lock("hands_li"):
        print("hands-li already running, exiting")
        return

    try:
        db = get_db()
        init_db()
        init_li_tables(db)

        log_hands_li("cycle start")

        # phase 1: expire stale
        expire_stale(db)

        # phase 2: try to post
        result = try_post(db)
        log_hands_li(f"cycle result: {result}")

        # update state
        set_state(db, "hands_li.last_run", now_utc().isoformat())
        set_state(db, "hands_li.last_result", result[:200])

        db.close()
    except Exception as e:
        log_hands_li(f"cycle CRASHED: {e}\n{traceback.format_exc()}", level="error")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
