#!/usr/bin/env python3
"""
aria-report-li.py -- LinkedIn Daily Digest + Weekly Report (L46 + L47).

Daily digest: 21:30 IST
Weekly report: Sunday 20:00 IST

Pass --daily or --weekly as argument.
"""

from __future__ import annotations

import json, os, sys, traceback
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
now_utc       = shared.now_utc
now_ist       = shared.now_ist
get_state     = shared.get_state
set_state     = shared.set_state
WORKSPACE     = shared.WORKSPACE
log           = shared.log


def log_rpt(msg: str, level: str = "info"):
    log(msg, process="report_li", level=level)


def daily_digest(db) -> str:
    """Generate the daily digest message (L46)."""
    now = now_utc()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    # Posts today
    posts_today = db.execute(
        "SELECT COUNT(*) as c FROM linkedin_posted WHERE posted_at > ?",
        (cutoff_24h,)
    ).fetchone()["c"]

    # Comments posted on others
    comments_posted = db.execute(
        "SELECT COUNT(*) as c FROM li_comments_posted WHERE posted_at > ?",
        (cutoff_24h,)
    ).fetchone()["c"]

    # Comments received on our posts
    comments_received = db.execute(
        "SELECT COUNT(*) as c FROM li_comments_received WHERE found_at > ?",
        (cutoff_24h,)
    ).fetchone()["c"]

    # Comment-backs sent
    comment_backs = db.execute(
        "SELECT COUNT(*) as c FROM li_comments_received "
        "WHERE replied=1 AND reply_posted_at > ?",
        (cutoff_24h,)
    ).fetchone()["c"]

    # Best post today (by comment count from metrics)
    best_post = db.execute(
        "SELECT lp.content, lpm.comments, lpm.likes FROM linkedin_posted lp "
        "LEFT JOIN li_post_metrics lpm ON lp.id = lpm.post_id "
        "WHERE lp.posted_at > ? "
        "ORDER BY lpm.comments DESC NULLS LAST LIMIT 1",
        (cutoff_24h,)
    ).fetchone()

    best_text = ""
    if best_post:
        best_text = f"\nbest post: {best_post['content'][:150]}..."
        if best_post["comments"]:
            best_text += f" ({best_post['comments']} comments)"

    # Follower count
    follower_row = db.execute(
        "SELECT count FROM li_followers ORDER BY checked_at DESC LIMIT 1"
    ).fetchone()
    followers = follower_row["count"] if follower_row else "?"

    # Queue status
    queued = db.execute(
        "SELECT COUNT(*) as c FROM linkedin_queue WHERE status='queued'"
    ).fetchone()["c"]

    # Comment opportunities
    opps = db.execute(
        "SELECT COUNT(*) as c FROM li_comment_opportunities "
        "WHERE status='new' AND found_at > ?",
        ((now - timedelta(hours=12)).isoformat(),)
    ).fetchone()["c"]

    msg = (
        f"<b>ARIA LinkedIn Daily Digest</b>\n"
        f"{now_ist().strftime('%A, %B %d %Y')}\n\n"
        f"posts today: {posts_today}\n"
        f"comments posted on others: {comments_posted}\n"
        f"comments received: {comments_received}\n"
        f"comment-backs sent: {comment_backs}\n"
        f"followers: {followers}\n"
        f"queue: {queued} posts\n"
        f"comment opportunities: {opps}"
        f"{best_text}"
    )
    return msg


def weekly_report(db) -> str:
    """Generate the weekly report message (L47)."""
    now = now_utc()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    # Follower growth
    follower_rows = db.execute(
        "SELECT count, checked_at FROM li_followers "
        "WHERE checked_at > ? ORDER BY checked_at ASC",
        (cutoff_7d,)
    ).fetchall()
    if len(follower_rows) >= 2:
        f_start = follower_rows[0]["count"]
        f_end = follower_rows[-1]["count"]
        follower_str = f"{f_start} -> {f_end} (+{f_end - f_start})"
    elif follower_rows:
        follower_str = f"{follower_rows[-1]['count']}"
    else:
        follower_str = "no data"

    # Total posts
    total_posts = db.execute(
        "SELECT COUNT(*) as c FROM linkedin_posted WHERE posted_at > ?",
        (cutoff_7d,)
    ).fetchone()["c"]

    # Posts by territory
    terr_rows = db.execute(
        "SELECT territory, COUNT(*) as c FROM linkedin_posted "
        "WHERE posted_at > ? GROUP BY territory",
        (cutoff_7d,)
    ).fetchall()
    terr_str = ", ".join(f"{r['territory']}: {r['c']}" for r in terr_rows)

    # Total comments received
    total_comments = db.execute(
        "SELECT COUNT(*) as c FROM li_comments_received WHERE found_at > ?",
        (cutoff_7d,)
    ).fetchone()["c"]

    # Substantive vs agreement breakdown
    substantive = db.execute(
        "SELECT COUNT(*) as c FROM li_comments_received "
        "WHERE found_at > ? AND comment_type='substantive'",
        (cutoff_7d,)
    ).fetchone()["c"]
    sub_pct = f"{substantive / total_comments * 100:.0f}%" if total_comments > 0 else "n/a"

    # Comments posted on others
    comments_out = db.execute(
        "SELECT COUNT(*) as c FROM li_comments_posted WHERE posted_at > ?",
        (cutoff_7d,)
    ).fetchone()["c"]

    # Avg comments per post
    avg_comments = total_comments / total_posts if total_posts > 0 else 0

    # Best post this week
    best = db.execute(
        "SELECT lp.content, lpm.comments, lpm.likes, lpm.impressions "
        "FROM linkedin_posted lp "
        "LEFT JOIN li_post_metrics lpm ON lp.id = lpm.post_id "
        "WHERE lp.posted_at > ? "
        "ORDER BY lpm.comments DESC NULLS LAST LIMIT 1",
        (cutoff_7d,)
    ).fetchone()
    best_str = ""
    if best:
        best_str = (f"\nbest post: {best['content'][:120]}..."
                    f"\n  comments: {best['comments'] or '?'}, "
                    f"likes: {best['likes'] or '?'}")

    # Comment target ROI (which accounts gave us the most visibility)
    top_target = db.execute(
        "SELECT creator_slug, COUNT(*) as c FROM li_comments_posted "
        "WHERE posted_at > ? GROUP BY creator_slug ORDER BY c DESC LIMIT 3",
        (cutoff_7d,)
    ).fetchall()
    top_str = ", ".join(f"@{r['creator_slug']}({r['c']})" for r in top_target)

    # Format breakdown
    format_rows = db.execute(
        "SELECT scores_json FROM linkedin_posted WHERE posted_at > ?",
        (cutoff_7d,)
    ).fetchall()
    format_counts = {}
    for r in format_rows:
        try:
            fmt = json.loads(r["scores_json"] or "{}").get("format_type", "text")
            format_counts[fmt] = format_counts.get(fmt, 0) + 1
        except Exception:
            format_counts["text"] = format_counts.get("text", 0) + 1
    format_str = ", ".join(f"{k}: {v}" for k, v in format_counts.items())

    msg = (
        f"<b>ARIA LinkedIn Weekly Report</b>\n"
        f"{now_ist().strftime('%A, %B %d %Y')}\n\n"
        f"follower growth: {follower_str}\n"
        f"total posts: {total_posts}\n"
        f"territories: {terr_str or 'none'}\n"
        f"formats: {format_str or 'none'}\n"
        f"comments received: {total_comments} (substantive: {sub_pct})\n"
        f"comments on others: {comments_out}\n"
        f"avg comments/post: {avg_comments:.1f}\n"
        f"top comment targets: {top_str or 'none'}"
        f"{best_str}\n\n"
        f"phase: {get_state(db, 'linkedin.phase', '1')}"
    )
    return msg


def main():
    report_type = sys.argv[1] if len(sys.argv) > 1 else "--daily"

    try:
        db = get_db()
        init_db()
        li_db.init_linkedin_tables(db)

        if report_type == "--weekly":
            msg = weekly_report(db)
            log_rpt("weekly report generated")
        else:
            msg = daily_digest(db)
            log_rpt("daily digest generated")

        send_telegram(msg)
        print(msg)

        set_state(db, f"report_li.last_{report_type.strip('-')}", now_utc().isoformat())
        db.close()

    except Exception as e:
        log_rpt(f"report CRASHED: {e}\n{traceback.format_exc()}", level="error")


if __name__ == "__main__":
    main()
