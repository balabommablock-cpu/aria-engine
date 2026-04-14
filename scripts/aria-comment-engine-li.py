#!/usr/bin/env python3
"""
aria-comment-engine-li.py -- LinkedIn Comment Engine (L01 + L02).

THE most important loop for LinkedIn growth. Period.
Commenting on the right posts = 80% of the growth strategy.

Each cycle:
  1. Pick 2-3 target accounts to scan (round-robin rotation)
  2. Scan their recent activity via CDP
  3. Score comment opportunities
  4. Draft a comment via Claude for the best opportunity
  5. Post the comment via CDP
  6. Log everything

Runs every 2 hours via launchd.
"""

from __future__ import annotations

import json, os, sys, subprocess, random, re, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import importlib
shared = importlib.import_module("aria-shared")
li_db = importlib.import_module("aria-linkedin-db")

get_db        = shared.get_db
init_db       = shared.init_db
send_telegram = shared.send_telegram
call_claude   = shared.call_claude
now_utc       = shared.now_utc
now_ist       = shared.now_ist
parse_ts      = shared.parse_ts
ts_age_hours  = shared.ts_age_hours
get_state     = shared.get_state
set_state     = shared.set_state
acquire_lock  = shared.acquire_lock
release_lock  = shared.release_lock
DRY_RUN       = shared.DRY_RUN
WORKSPACE     = shared.WORKSPACE
log           = shared.log

TARGETS_LI_PATH = WORKSPACE / "memory" / "target-accounts-li.json"
VOICE_PATH = WORKSPACE / "voice.json"
VOICE_RULES_PATH = WORKSPACE / "memory" / "voice-rules.md"

SCAN_PROFILE_JS = SCRIPTS_DIR / "scan_linkedin_profile.js"
POST_COMMENT_JS = SCRIPTS_DIR / "post_linkedin_comment.js"

# Config
PROFILES_PER_SCAN = 3
MAX_POST_AGE_HOURS = 6
MAX_COMMENTS_PER_DAY = 10
MIN_GAP_MINUTES = 15
COMMENT_MIN_CHARS = 150
COMMENT_MAX_CHARS = 800


def log_ce(msg: str, level: str = "info"):
    log(msg, process="comment_engine_li", level=level)


# ============================================================
# TARGETS
# ============================================================

def load_targets() -> dict:
    if not TARGETS_LI_PATH.exists():
        log_ce("target-accounts-li.json not found", level="error")
        return {}
    return json.loads(TARGETS_LI_PATH.read_text())


def get_scan_candidates(db, targets: dict) -> list[dict]:
    """Pick profiles to scan this cycle using round-robin rotation."""
    all_accounts = []
    for tier_key in ["tier_1_creators", "tier_2_creators", "tier_3_peers"]:
        tier_label = tier_key.replace("_creators", "").replace("_peers", "")
        for acct in targets.get(tier_key, []):
            acct["tier"] = tier_label
            all_accounts.append(acct)

    if not all_accounts:
        return []

    # Ensure scanner state rows exist
    for acct in all_accounts:
        db.execute(
            "INSERT OR IGNORE INTO li_scanner_state (slug, tier, last_scanned_at, scan_count) "
            "VALUES (?, ?, NULL, 0)",
            (acct["slug"], acct["tier"])
        )
    db.commit()

    # Pick the least-recently-scanned profiles
    rows = db.execute(
        "SELECT slug, tier, last_scanned_at FROM li_scanner_state "
        "ORDER BY last_scanned_at ASC NULLS FIRST, scan_count ASC "
        f"LIMIT {PROFILES_PER_SCAN}"
    ).fetchall()

    candidates = []
    slug_to_acct = {a["slug"]: a for a in all_accounts}
    for row in rows:
        if row["slug"] in slug_to_acct:
            candidates.append(slug_to_acct[row["slug"]])
    return candidates


# ============================================================
# SCANNING: find posts on target profiles
# ============================================================

def scan_profile(slug: str) -> list[dict]:
    """Scan a LinkedIn profile's recent activity via CDP."""
    if not SCAN_PROFILE_JS.exists():
        log_ce(f"scan_linkedin_profile.js not found", level="error")
        return []

    env = os.environ.copy()
    env["CDP_URL"] = shared.CDP_URL
    env["NODE_PATH"] = os.path.expanduser(
        "~/.openclaw/workspace/skills/x-twitter-poster/node_modules"
    )

    try:
        result = subprocess.run(
            ["node", str(SCAN_PROFILE_JS), slug],
            env=env, cwd=str(SCRIPTS_DIR),
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return data.get("posts", [])
        else:
            err = result.stderr.strip()[:200]
            log_ce(f"scan {slug} failed: {err}", level="error")
            return []
    except subprocess.TimeoutExpired:
        log_ce(f"scan {slug} timed out", level="error")
        return []
    except Exception as e:
        log_ce(f"scan {slug} error: {e}", level="error")
        return []


def estimate_post_age(time_label: str) -> float:
    """Estimate post age in hours from LinkedIn's relative time label."""
    if not time_label:
        return 999
    t = time_label.lower().strip()
    # "2h" "3h ago" "5m" "1d" etc.
    m = re.search(r'(\d+)\s*m(?:in)?', t)
    if m:
        return int(m.group(1)) / 60
    m = re.search(r'(\d+)\s*h(?:our|r)?', t)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*d(?:ay)?', t)
    if m:
        return int(m.group(1)) * 24
    m = re.search(r'(\d+)\s*w(?:eek)?', t)
    if m:
        return int(m.group(1)) * 168
    if "just now" in t or "now" in t:
        return 0.1
    return 999


# ============================================================
# SCORING: rate comment opportunities
# ============================================================

def score_opportunity(post: dict, acct: dict, db) -> int:
    """Score a comment opportunity per L01 spec."""
    score = 0
    territory = acct.get("territory", "")
    tier = acct.get("tier", "tier_3")

    # Topic match
    voice = json.loads(VOICE_PATH.read_text()) if VOICE_PATH.exists() else {}
    our_territories = set(voice.get("territory_weights", {}).keys())
    post_topics = set(acct.get("topics", []))
    if territory in our_territories or post_topics & our_territories:
        score += 4

    # Low competition (< 30 comments)
    comments = post.get("comments", 0)
    if comments < 30:
        score += 3
    elif comments < 100:
        score += 1

    # Fresh (< 2 hours old)
    age = estimate_post_age(post.get("time_label", ""))
    if age < 2:
        score += 3
    elif age < 4:
        score += 2
    elif age < MAX_POST_AGE_HOURS:
        score += 1
    else:
        score -= 5  # too old

    # Has enough text to comment on meaningfully
    text_len = post.get("text_length", len(post.get("text", "")))
    if text_len > 200:
        score += 2
    elif text_len > 100:
        score += 1

    # Haven't already commented on this exact post
    existing = db.execute(
        "SELECT COUNT(*) as c FROM li_comment_opportunities WHERE post_url=? AND status='posted'",
        (post.get("url", ""),)
    ).fetchone()["c"]
    if existing > 0:
        score -= 10

    # Haven't commented on this creator's last post
    recent_comment = db.execute(
        "SELECT COUNT(*) as c FROM li_comments_posted "
        "WHERE creator_slug=? AND posted_at > ?",
        (acct["slug"], (now_utc() - timedelta(hours=12)).isoformat())
    ).fetchone()["c"]
    if recent_comment > 0:
        score -= 3

    return score


# ============================================================
# DRAFTING: generate comment via Claude
# ============================================================

def draft_comment(post_text: str, acct: dict, existing_themes: list[str]) -> tuple[str, dict]:
    """Draft a comment using Claude. Returns (comment_text, scores)."""
    voice = json.loads(VOICE_PATH.read_text()) if VOICE_PATH.exists() else {}
    voice_rules = ""
    if VOICE_RULES_PATH.exists():
        voice_rules = VOICE_RULES_PATH.read_text()[:1000]

    golden = "\n".join(
        f"  - {g['text']}" for g in voice.get("golden_tweets", [])[:5]
    )
    themes_str = "\n".join(f"  - {t}" for t in existing_themes[:5]) if existing_themes else "  (no existing comments seen)"

    prompt = f"""you are commenting on this LinkedIn post by {acct.get('name', 'someone')}:

"{post_text[:1500]}"

existing comments cover these angles:
{themes_str}

write ONE comment that:
1. adds a NEW angle not covered by existing comments
2. includes a specific example, data point, or personal experience
3. is 3-8 sentences long (LinkedIn rewards longer, thoughtful comments)
4. ends with either a question back to the author OR a mini-insight
   that makes OTHER readers want to engage with YOUR comment
5. does NOT start with "Great post.", "Love this.", "So true.",
   "Couldn't agree more", or any sycophantic opener
6. does NOT use emojis in the first line
7. does NOT self-promote ("I wrote about this", "check my profile")
8. does NOT use external links or hashtags

the comment should be good enough that someone reading the comments
section would click on YOUR profile. that's the whole game.

your voice (study these):
{golden}

voice rules:
{voice_rules[:500]}

HARD RULES:
- no em dashes
- no exclamation marks
- natural case
- 150-800 characters
- at least one specific detail (name a tool, cite a number, describe a real scenario)
- no sycophantic opener (first 5 words must NOT be agreement/praise)

write exactly ONE comment. nothing else. no preamble, no explanation."""

    response = call_claude(prompt)
    if not response:
        return "", {}

    # Clean the response
    comment = response.strip()
    # Remove any markdown formatting
    comment = re.sub(r'^```.*?\n', '', comment)
    comment = re.sub(r'\n```$', '', comment)
    comment = comment.strip('"').strip("'").strip()

    # Apply hard bans
    for ch in ["\u2014", "\u2013"]:
        comment = comment.replace(ch, ",")
    comment = comment.replace("!", ".")

    # Check sycophantic opener
    first_words = comment.split()[:5]
    syc_openers = {"great", "love", "amazing", "wonderful", "brilliant", "excellent",
                   "fantastic", "incredible", "awesome", "agree", "true", "exactly"}
    if first_words and first_words[0].lower().rstrip(".,") in syc_openers:
        comment = " ".join(first_words[1:] + comment.split()[5:])

    # Length check
    if len(comment) < COMMENT_MIN_CHARS or len(comment) > COMMENT_MAX_CHARS:
        if len(comment) > COMMENT_MAX_CHARS:
            comment = comment[:COMMENT_MAX_CHARS].rsplit(".", 1)[0] + "."
        elif len(comment) < COMMENT_MIN_CHARS:
            return "", {}

    scores = {
        "length": len(comment),
        "has_specific_detail": 1 if any(c.isdigit() for c in comment) else 0,
        "has_question": 1 if "?" in comment else 0,
    }

    return comment, scores


# ============================================================
# POSTING: post comment via CDP
# ============================================================

def post_comment_cdp(post_url: str, comment_text: str) -> tuple[bool, str]:
    """Post a comment via the CDP script."""
    if not POST_COMMENT_JS.exists():
        return False, "post_linkedin_comment.js not found"

    env = os.environ.copy()
    env["CDP_URL"] = shared.CDP_URL
    env["NODE_PATH"] = os.path.expanduser(
        "~/.openclaw/workspace/skills/x-twitter-poster/node_modules"
    )

    try:
        result = subprocess.run(
            ["node", str(POST_COMMENT_JS), post_url, comment_text],
            env=env, cwd=str(SCRIPTS_DIR),
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return data.get("success", False), data.get("message", "unknown")
        else:
            err = result.stderr.strip()[:300] or result.stdout.strip()[:300]
            return False, err
    except subprocess.TimeoutExpired:
        return False, "timeout (180s)"
    except Exception as e:
        return False, str(e)


# ============================================================
# PACING CHECKS
# ============================================================

def comments_today(db) -> int:
    cutoff = (now_utc() - timedelta(hours=24)).isoformat()
    return db.execute(
        "SELECT COUNT(*) as c FROM li_comments_posted WHERE posted_at > ?",
        (cutoff,)
    ).fetchone()["c"]


def minutes_since_last_comment(db) -> float:
    row = db.execute(
        "SELECT posted_at FROM li_comments_posted ORDER BY posted_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return 999
    age = ts_age_hours(row["posted_at"])
    return age * 60


# ============================================================
# MAIN CYCLE
# ============================================================

def main():
    if not acquire_lock("comment_engine_li"):
        print("comment engine already running, exiting")
        return

    try:
        db = get_db()
        init_db()
        li_db.init_linkedin_tables(db)

        log_ce("cycle start")

        targets = load_targets()
        if not targets:
            log_ce("no targets configured, exiting")
            return

        # Check daily cap
        today_count = comments_today(db)
        if today_count >= MAX_COMMENTS_PER_DAY:
            log_ce(f"daily cap hit ({today_count}/{MAX_COMMENTS_PER_DAY})")
            return

        # Check gap
        gap = minutes_since_last_comment(db)
        if gap < MIN_GAP_MINUTES:
            log_ce(f"too soon since last comment ({gap:.0f}m < {MIN_GAP_MINUTES}m)")
            return

        # Phase 1: SCAN for opportunities
        candidates = get_scan_candidates(db, targets)
        log_ce(f"scanning {len(candidates)} profiles: {[c['slug'] for c in candidates]}")

        all_opportunities = []
        for acct in candidates:
            posts = scan_profile(acct["slug"])
            ts = now_utc().isoformat()

            # Update scanner state
            db.execute(
                "UPDATE li_scanner_state SET last_scanned_at=?, scan_count=scan_count+1 WHERE slug=?",
                (ts, acct["slug"])
            )

            for post in posts:
                age = estimate_post_age(post.get("time_label", ""))
                if age > MAX_POST_AGE_HOURS:
                    continue
                if not post.get("url"):
                    continue

                score = score_opportunity(post, acct, db)
                min_score = targets.get("scanning", {}).get(
                    f"min_score_{acct['tier']}", 5
                )

                if score >= min_score:
                    opp = {
                        "creator_name": acct["name"],
                        "creator_slug": acct["slug"],
                        "post_url": post["url"],
                        "post_text": post.get("text", ""),
                        "post_engagement_json": json.dumps({
                            "likes": post.get("likes", 0),
                            "comments": post.get("comments", 0),
                            "shares": post.get("shares", 0),
                        }),
                        "post_age_hours": age,
                        "score": score,
                        "tier": acct["tier"],
                        "territory": acct.get("territory", ""),
                    }
                    all_opportunities.append(opp)

                    # Upsert into DB
                    db.execute(
                        "INSERT OR IGNORE INTO li_comment_opportunities "
                        "(creator_name, creator_slug, post_url, post_text, "
                        "post_engagement_json, post_age_hours, score, status, "
                        "tier, territory, found_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (opp["creator_name"], opp["creator_slug"], opp["post_url"],
                         opp["post_text"][:2000], opp["post_engagement_json"],
                         opp["post_age_hours"], opp["score"], "new",
                         opp["tier"], opp["territory"], ts)
                    )

            db.commit()
            log_ce(f"scanned {acct['slug']}: {len(posts)} posts found")
            # Small delay between profile scans
            time.sleep(random.randint(3, 8))

        # Also consider existing undrafted opportunities
        existing = db.execute(
            "SELECT * FROM li_comment_opportunities "
            "WHERE status='new' AND found_at > ? "
            "ORDER BY score DESC LIMIT 5",
            ((now_utc() - timedelta(hours=MAX_POST_AGE_HOURS)).isoformat(),)
        ).fetchall()

        log_ce(f"total opportunities: {len(all_opportunities)} new + {len(existing)} existing")

        # Phase 2: Pick best opportunity and DRAFT + POST comment
        # Combine new + existing, sort by score
        best = None
        best_score = -999

        for opp in existing:
            if opp["score"] > best_score:
                best_score = opp["score"]
                best = dict(opp)

        if not best:
            log_ce("no viable comment opportunities this cycle")
            set_state(db, "comment_engine_li.last_run", now_utc().isoformat())
            set_state(db, "comment_engine_li.last_result", "no opportunities")
            db.close()
            return

        log_ce(f"best opportunity: {best['creator_slug']} (score={best['score']})")

        # Draft comment
        comment_text, scores = draft_comment(
            best["post_text"],
            {"name": best["creator_name"], "slug": best["creator_slug"],
             "topics": [], "territory": best.get("territory", "")},
            []  # existing_themes -- could be populated from read_post_comments
        )

        if not comment_text:
            log_ce("comment draft failed or too short", level="error")
            db.execute(
                "UPDATE li_comment_opportunities SET status='skipped' WHERE id=?",
                (best["id"],)
            )
            db.commit()
            set_state(db, "comment_engine_li.last_run", now_utc().isoformat())
            set_state(db, "comment_engine_li.last_result", "draft failed")
            db.close()
            return

        log_ce(f"drafted comment ({len(comment_text)} chars): {comment_text[:80]}...")

        # Update opportunity with draft
        db.execute(
            "UPDATE li_comment_opportunities SET status='drafted', draft_text=?, "
            "draft_scores_json=?, drafted_at=? WHERE id=?",
            (comment_text, json.dumps(scores), now_utc().isoformat(), best["id"])
        )
        db.commit()

        if DRY_RUN:
            log_ce(f"DRY RUN: would comment on {best['post_url']}")
            log_ce(f"DRY RUN: {comment_text}")
            set_state(db, "comment_engine_li.last_run", now_utc().isoformat())
            set_state(db, "comment_engine_li.last_result", f"dry run: {comment_text[:60]}")
            db.close()
            return

        # Anti-detection delay
        delay = random.randint(20, 60)
        log_ce(f"posting comment in {delay}s...")
        time.sleep(delay)

        # Post comment
        success, result_msg = post_comment_cdp(best["post_url"], comment_text)

        if success:
            ts = now_utc().isoformat()
            db.execute(
                "UPDATE li_comment_opportunities SET status='posted', posted_at=? WHERE id=?",
                (ts, best["id"])
            )
            db.execute(
                "INSERT INTO li_comments_posted "
                "(opportunity_id, creator_name, creator_slug, post_url, "
                "comment_text, scores_json, territory, tier, posted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (best["id"], best["creator_name"], best["creator_slug"],
                 best["post_url"], comment_text, json.dumps(scores),
                 best.get("territory", ""), best.get("tier", ""), ts)
            )
            db.commit()

            log_ce(f"POSTED comment on {best['creator_slug']}: {comment_text[:80]}")
            send_telegram(
                f"<b>ARIA LinkedIn comment posted</b>\n\n"
                f"on: {best['creator_name']} ({best['creator_slug']})\n"
                f"comment: {comment_text[:300]}\n\n"
                f"score: {best['score']}, tier: {best.get('tier','?')}"
            )
        else:
            log_ce(f"comment post FAILED: {result_msg}", level="error")
            db.execute(
                "UPDATE li_comment_opportunities SET status='new' WHERE id=?",
                (best["id"],)
            )
            db.commit()
            send_telegram(
                f"<b>ARIA LinkedIn comment FAILED</b>\n\n"
                f"target: {best['creator_slug']}\n"
                f"error: {result_msg[:200]}"
            )

        # Final state update
        set_state(db, "comment_engine_li.last_run", now_utc().isoformat())
        set_state(db, "comment_engine_li.last_result",
                  f"{'posted' if success else 'failed'}: {best['creator_slug']}")

        db.close()
        log_ce("cycle complete")

    except Exception as e:
        log_ce(f"cycle CRASHED: {e}\n{traceback.format_exc()}", level="error")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
