#!/usr/bin/env python3
"""
aria-comment-back-li.py -- LinkedIn Comment-Back Loop (L03).

THE SINGLE MOST IMPORTANT ENGAGEMENT LOOP ON LINKEDIN.

LinkedIn algo counts every comment (including your own replies) as a
separate engagement signal. A post with 10 comments where you replied
to all 10 = 20 engagement signals. This keeps the post alive in the feed.

Each cycle:
  1. Get own posts from last 24 hours
  2. Read comments on each post via CDP
  3. Classify each comment (substantive/agreement/question/disagreement/spam)
  4. Draft and post replies to unreplied comments
  5. Log everything

Runs every 30 minutes via launchd.
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
parse_ts      = shared.parse_ts
ts_age_hours  = shared.ts_age_hours
get_state     = shared.get_state
set_state     = shared.set_state
acquire_lock  = shared.acquire_lock
release_lock  = shared.release_lock
DRY_RUN       = shared.DRY_RUN
WORKSPACE     = shared.WORKSPACE
log           = shared.log

READ_COMMENTS_JS = SCRIPTS_DIR / "read_post_comments.js"
POST_COMMENT_JS = SCRIPTS_DIR / "post_linkedin_comment.js"
VOICE_PATH = WORKSPACE / "voice.json"
VOICE_RULES_PATH = WORKSPACE / "memory" / "voice-rules.md"

MAX_REPLIES_PER_CYCLE = 5


def log_cb(msg: str, level: str = "info"):
    log(msg, process="comment_back_li", level=level)


# ============================================================
# READ COMMENTS ON OWN POSTS
# ============================================================

def read_comments_cdp(post_url: str) -> tuple[str, list[dict]]:
    """Read comments on a post via CDP.
    Returns (post_text, comments_list)."""
    if not READ_COMMENTS_JS.exists():
        return "", []

    env = os.environ.copy()
    env["CDP_URL"] = shared.CDP_URL
    env["NODE_PATH"] = os.path.expanduser(
        "~/.openclaw/workspace/skills/x-twitter-poster/node_modules"
    )

    try:
        result = subprocess.run(
            ["node", str(READ_COMMENTS_JS), post_url],
            env=env, cwd=str(SCRIPTS_DIR),
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return data.get("post_text", ""), data.get("comments", [])
        else:
            err = result.stderr.strip()[:200]
            log_cb(f"read comments failed: {err}", level="error")
            return "", []
    except subprocess.TimeoutExpired:
        log_cb("read comments timed out", level="error")
        return "", []
    except Exception as e:
        log_cb(f"read comments error: {e}", level="error")
        return "", []


def post_reply_cdp(post_url: str, reply_text: str) -> tuple[bool, str]:
    """Post a reply comment via CDP."""
    if not POST_COMMENT_JS.exists():
        return False, "post_linkedin_comment.js not found"

    env = os.environ.copy()
    env["CDP_URL"] = shared.CDP_URL
    env["NODE_PATH"] = os.path.expanduser(
        "~/.openclaw/workspace/skills/x-twitter-poster/node_modules"
    )

    try:
        result = subprocess.run(
            ["node", str(POST_COMMENT_JS), post_url, reply_text],
            env=env, cwd=str(SCRIPTS_DIR),
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return data.get("success", False), data.get("message", "unknown")
        return False, result.stderr.strip()[:300]
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


# ============================================================
# CLASSIFY COMMENTS
# ============================================================

def classify_comment(comment_text: str) -> str:
    """Rule-based classification of comment type."""
    text = comment_text.lower().strip()
    word_count = len(text.split())

    # Spam detection
    spam_signals = ["check out my", "visit my", "follow me", "dm me",
                    "free trial", "discount", "promo", ".com/", "link in"]
    if any(s in text for s in spam_signals):
        return "spam"

    # Question detection
    if "?" in text and word_count >= 5:
        return "question"

    # Agreement (short, low-value)
    agreement_signals = ["great post", "love this", "so true", "agree", "well said",
                         "spot on", "nailed it", "this is gold", "resonates",
                         "thanks for sharing", "needed this"]
    if word_count <= 15 and any(s in text for s in agreement_signals):
        return "agreement"

    # Disagreement
    disagree_signals = ["disagree", "i'd push back", "not sure about",
                        "counterpoint", "on the other hand", "but what about",
                        "i see it differently", "not necessarily"]
    if any(s in text for s in disagree_signals):
        return "disagreement"

    # Substantive (3+ sentences or 50+ words)
    if word_count >= 30 or text.count(".") >= 3:
        return "substantive"

    if word_count >= 15:
        return "substantive"

    return "agreement"


# ============================================================
# DRAFT REPLIES
# ============================================================

def draft_reply(our_post_text: str, comment: dict, comment_type: str) -> str:
    """Draft a reply to a comment on our own post."""
    voice = json.loads(VOICE_PATH.read_text()) if VOICE_PATH.exists() else {}
    golden = "\n".join(f"  - {g['text']}" for g in voice.get("golden_tweets", [])[:3])

    commenter_name = comment.get("name", "someone")
    comment_text = comment.get("text", "")

    type_guidance = {
        "substantive": (
            "acknowledge their specific point (NOT generically). "
            "add a new angle or extend their thought. "
            "2-4 sentences. end with a question to keep the thread going. "
            "never say 'thanks for sharing', 'great insight'."
        ),
        "question": (
            "answer directly. add context they didn't ask for but would value. "
            "2-5 sentences."
        ),
        "agreement": (
            "acknowledge without sycophancy. ask them a deepening question. "
            "1-2 sentences."
        ),
        "disagreement": (
            "'that's a fair counterpoint. here's where i'd push back: [specific]'. "
            "never defensive, always curious. 2-4 sentences. "
            "end with 'what's your experience been?' or similar."
        ),
    }

    guidance = type_guidance.get(comment_type, type_guidance["substantive"])

    prompt = f"""you are replying to a comment on YOUR LinkedIn post.

your post:
"{our_post_text[:800]}"

{commenter_name} commented ({comment_type}):
"{comment_text[:500]}"

write a reply that:
{guidance}

your voice (same energy):
{golden}

RULES:
- no em dashes, no exclamation marks
- natural case
- 50-400 characters
- direct, warm but not sycophantic
- no "great point", "thanks for sharing", "appreciate you"
- if the thread can go deeper, make it go deeper

write exactly ONE reply. no preamble."""

    response = call_claude(prompt)
    if not response:
        return ""

    reply = response.strip()
    reply = re.sub(r'^```.*?\n', '', reply)
    reply = re.sub(r'\n```$', '', reply)
    reply = reply.strip('"').strip("'").strip()

    # Clean
    for ch in ["\u2014", "\u2013"]:
        reply = reply.replace(ch, ",")
    reply = reply.replace("!", ".")

    if len(reply) < 30 or len(reply) > 500:
        if len(reply) > 500:
            reply = reply[:500].rsplit(".", 1)[0] + "."
        else:
            return ""

    return reply


# ============================================================
# MAIN CYCLE
# ============================================================

def main():
    if not acquire_lock("comment_back_li"):
        print("comment-back already running, exiting")
        return

    try:
        db = get_db()
        init_db()
        li_db.init_linkedin_tables(db)

        log_cb("cycle start")

        # Get our recent posts (last 48h -- comment-back is valuable for longer)
        cutoff = (now_utc() - timedelta(hours=48)).isoformat()
        our_posts = db.execute(
            "SELECT id, content, post_url, posted_at FROM linkedin_posted "
            "WHERE posted_at > ? AND post_url IS NOT NULL "
            "ORDER BY posted_at DESC LIMIT 5",
            (cutoff,)
        ).fetchall()

        if not our_posts:
            log_cb("no recent posts to check")
            set_state(db, "comment_back_li.last_run", now_utc().isoformat())
            set_state(db, "comment_back_li.last_result", "no posts")
            db.close()
            return

        log_cb(f"checking {len(our_posts)} recent posts for new comments")
        replies_sent = 0

        for post in our_posts:
            if replies_sent >= MAX_REPLIES_PER_CYCLE:
                break

            post_url = post["post_url"]
            if not post_url or "linkedin.com" not in post_url:
                continue

            # Read comments via CDP
            post_text, comments = read_comments_cdp(post_url)
            if not comments:
                log_cb(f"no comments on post {post['id']}")
                continue

            log_cb(f"post {post['id']}: {len(comments)} comments found")

            for comment in comments:
                if replies_sent >= MAX_REPLIES_PER_CYCLE:
                    break

                comment_text = comment.get("text", "").strip()
                commenter_slug = comment.get("slug", "")
                commenter_name = comment.get("name", "")

                if not comment_text or len(comment_text) < 5:
                    continue

                # Check if we already know about this comment
                existing = db.execute(
                    "SELECT id, replied FROM li_comments_received "
                    "WHERE our_post_url=? AND comment_text=?",
                    (post_url, comment_text[:500])
                ).fetchone()

                if existing and existing["replied"]:
                    continue  # already replied

                # Classify
                comment_type = classify_comment(comment_text)

                # Store if new
                if not existing:
                    db.execute(
                        "INSERT INTO li_comments_received "
                        "(our_post_id, our_post_url, commenter_name, commenter_slug, "
                        "commenter_headline, comment_text, comment_type, replied, found_at) "
                        "VALUES (?,?,?,?,?,?,?,0,?)",
                        (post["id"], post_url, commenter_name, commenter_slug,
                         comment.get("headline", ""), comment_text[:1000],
                         comment_type, now_utc().isoformat())
                    )
                    db.commit()

                if comment_type == "spam":
                    log_cb(f"skipping spam from {commenter_name}")
                    continue

                # Skip agreement comments from small accounts (low ROI)
                if comment_type == "agreement":
                    # Still reply but with lower priority
                    pass

                # Draft reply
                reply_text = draft_reply(
                    post["content"], comment, comment_type
                )
                if not reply_text:
                    log_cb(f"draft failed for {commenter_name}'s {comment_type} comment")
                    continue

                log_cb(f"drafted reply to {commenter_name} ({comment_type}): {reply_text[:60]}...")

                if DRY_RUN:
                    log_cb(f"DRY RUN: would reply: {reply_text}")
                    continue

                # Anti-detection delay
                delay = random.randint(10, 30)
                time.sleep(delay)

                # Post reply
                # Note: posting a reply on the same post URL adds it to the main comment thread
                # LinkedIn doesn't have a "reply to specific comment" via simple posting
                # The comment will appear as a new comment on the post
                success, result_msg = post_reply_cdp(post_url, reply_text)

                if success:
                    # Mark as replied
                    rec_id = existing["id"] if existing else db.execute(
                        "SELECT id FROM li_comments_received "
                        "WHERE our_post_url=? AND comment_text=? "
                        "ORDER BY id DESC LIMIT 1",
                        (post_url, comment_text[:500])
                    ).fetchone()
                    if rec_id:
                        rid = rec_id if isinstance(rec_id, int) else rec_id["id"]
                        db.execute(
                            "UPDATE li_comments_received SET replied=1, "
                            "reply_text=?, reply_posted_at=? WHERE id=?",
                            (reply_text, now_utc().isoformat(), rid)
                        )
                    db.commit()

                    replies_sent += 1
                    log_cb(f"REPLIED to {commenter_name}: {reply_text[:60]}")
                else:
                    log_cb(f"reply FAILED: {result_msg}", level="error")

            # Delay between posts
            time.sleep(random.randint(5, 15))

        # Summary
        summary = f"comment-back: checked {len(our_posts)} posts, sent {replies_sent} replies"
        log_cb(summary)
        set_state(db, "comment_back_li.last_run", now_utc().isoformat())
        set_state(db, "comment_back_li.last_result", summary)

        if replies_sent > 0:
            send_telegram(
                f"<b>ARIA LinkedIn comment-back</b>\n\n"
                f"posts checked: {len(our_posts)}\n"
                f"replies sent: {replies_sent}"
            )

        db.close()

    except Exception as e:
        log_cb(f"cycle CRASHED: {e}\n{traceback.format_exc()}", level="error")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
