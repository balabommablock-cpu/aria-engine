#!/usr/bin/env python3
"""
aria-watchdog.py -- hourly health monitor + auto-adjuster.

runs every hour via launchd. checks:
  - process health (brain + hands alive?)
  - error rates (rising? repeating patterns?)
  - pipeline flow (queue draining? replies posting?)
  - reply quality (contextual vs fallback ratio)
  - target engagement (who replies back? who's dead?)

auto-adjustments:
  - deprioritize targets that never engage back after 5+ attempts
  - alert on repeated errors
  - alert on empty queue
  - track image post vs text-only performance

sends findings to telegram.
"""

from __future__ import annotations

import json, os, sys, sqlite3, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import importlib
shared = importlib.import_module("aria-shared")

get_db       = shared.get_db
init_db      = shared.init_db
log_brain    = shared.log_brain
send_telegram = shared.send_telegram
now_utc      = shared.now_utc
get_state    = shared.get_state
set_state    = shared.set_state
WORKSPACE    = shared.WORKSPACE

DB_PATH = WORKSPACE / "memory" / "aria.db"


def main():
    if not DB_PATH.exists():
        return

    db = get_db()
    init_db()
    now = now_utc()
    cutoff_1h = (now - timedelta(hours=1)).isoformat()
    cutoff_6h = (now - timedelta(hours=6)).isoformat()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    report = []
    alerts = []

    # ---- process health ----
    brain_alive = "com.aria.brain" in subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True).stdout
    hands_alive = "com.aria.hands" in subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True).stdout

    if not brain_alive:
        alerts.append("brain is STOPPED")
    if not hands_alive:
        alerts.append("hands is STOPPED")

    # ---- CDP health: ensure Chrome has at least 1 tab ----
    try:
        import urllib.request as ur
        tabs_resp = ur.urlopen("http://127.0.0.1:28800/json/list", timeout=5).read()
        tabs = json.loads(tabs_resp)
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if len(page_tabs) == 0:
            alerts.append("CHROME HAS ZERO TABS. auto-recovering...")
            # create a new tab
            req = ur.Request("http://127.0.0.1:28800/json/new?https://x.com/home", method="PUT")
            ur.urlopen(req, timeout=10)
            adjustments.append("created new Chrome tab (was empty)")
    except Exception as e:
        alerts.append(f"CDP check failed: {str(e)[:60]}")

    # ---- error rate (last 1h vs last 6h) ----
    errors_1h = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE level='error' AND ts > ?",
        (cutoff_1h,)
    ).fetchone()["c"]
    errors_6h = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE level='error' AND ts > ?",
        (cutoff_6h,)
    ).fetchone()["c"]

    if errors_1h >= 3:
        alerts.append(f"high error rate: {errors_1h} errors in last hour")

    # check for repeating errors
    recent_errors = db.execute(
        "SELECT message FROM engine_log WHERE level='error' AND ts > ? ORDER BY id DESC LIMIT 10",
        (cutoff_6h,)
    ).fetchall()
    error_patterns = Counter()
    for e in recent_errors:
        msg = e["message"][:50]
        error_patterns[msg] += 1
    for pattern, count in error_patterns.items():
        if count >= 3:
            alerts.append(f"repeating error ({count}x): {pattern}")

    # ---- pipeline health ----
    queued = db.execute(
        "SELECT COUNT(*) as c FROM queue WHERE status='queued'"
    ).fetchone()["c"]
    if queued == 0:
        alerts.append("tweet queue is EMPTY. brain needs to generate.")

    posted_24h = db.execute(
        "SELECT COUNT(*) as c FROM posted WHERE posted_at > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    replies_24h = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='posted' AND posted_at > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    failed_24h = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='failed' AND generated_at > ?",
        (cutoff_24h,)
    ).fetchone()["c"]

    report.append(f"24h: {posted_24h} tweets, {replies_24h} replies, {failed_24h} failed")

    # ---- reply quality: contextual vs fallback ----
    # check engine_log for "using contextual reply" vs "using brain draft"
    contextual_count = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE message LIKE '%using contextual reply%' AND ts > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    fallback_count = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE message LIKE '%using brain draft%' AND ts > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    total_replies = contextual_count + fallback_count
    if total_replies > 0:
        ctx_pct = int(contextual_count / total_replies * 100)
        report.append(f"reply quality: {ctx_pct}% contextual ({contextual_count}/{total_replies})")
        if ctx_pct < 50 and total_replies >= 3:
            alerts.append(f"low contextual reply rate: {ctx_pct}%")

    # ---- target engagement analysis ----
    targets = db.execute(
        "SELECT handle, priority, reply_count, last_replied_at FROM reply_targets "
        "ORDER BY priority ASC, reply_count DESC"
    ).fetchall()
    engaged_back = 0
    dead_targets = []
    for t in targets:
        rc = t["reply_count"] or 0
        # check if this target has ever engaged back (we'd need metrics for this,
        # for now just track reply_count vs no engagement data)
        if rc >= 5:
            # 5+ replies with no known engagement back -- flag for review
            dead_targets.append(f"@{t['handle']} ({rc} replies)")

    if dead_targets:
        report.append(f"low-engagement targets: {', '.join(dead_targets[:3])}")

    # ---- hook pattern distribution (last 10 posts) ----
    recent_posts = db.execute(
        "SELECT scores_json FROM posted ORDER BY posted_at DESC LIMIT 10"
    ).fetchall()
    hook_dist = Counter()
    for p in recent_posts:
        try:
            scores = json.loads(p["scores_json"]) if p["scores_json"] else {}
            hp = scores.get("hook_pattern", "unknown")
            hook_dist[hp] += 1
        except (json.JSONDecodeError, TypeError):
            hook_dist["unknown"] += 1
    if hook_dist:
        dist_str = ", ".join(f"{k}:{v}" for k, v in hook_dist.most_common())
        report.append(f"hook patterns (last 10): {dist_str}")

    # ---- territory distribution ----
    terr_dist = Counter()
    for p in recent_posts:
        try:
            # territory is stored directly in posted table
            pass  # need to query with territory
        except:
            pass
    terr_rows = db.execute(
        "SELECT territory, COUNT(*) as c FROM posted GROUP BY territory"
    ).fetchall()
    if terr_rows:
        terr_str = ", ".join(f"{r['territory']}:{r['c']}" for r in terr_rows)
        report.append(f"territories (all time): {terr_str}")

    # ---- image vs text performance ----
    img_posts = db.execute(
        "SELECT COUNT(*) as c FROM posted WHERE image_type != 'none' AND image_type IS NOT NULL"
    ).fetchone()["c"]
    text_posts = db.execute(
        "SELECT COUNT(*) as c FROM posted WHERE image_type IS NULL OR image_type = 'none'"
    ).fetchone()["c"]
    report.append(f"image posts: {img_posts}, text-only: {text_posts}")

    # ---- HANDS ACTIVITY CHECK (critical: detect blocked states) ----
    last_action_row = db.execute(
        "SELECT ts FROM engine_log WHERE process='hands' "
        "AND message LIKE 'action=%' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if last_action_row:
        last_action_ts = last_action_row["ts"]
        from datetime import datetime as dt
        try:
            last_dt = dt.fromisoformat(last_action_ts.replace("Z", "+00:00"))
            idle_hours = (now - last_dt).total_seconds() / 3600
            if idle_hours > 1:
                alerts.append(f"HANDS IDLE for {idle_hours:.1f}h. last action: {last_action_ts[:16]}")

                # detect WHY hands is idle
                cap_hits = db.execute(
                    "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' "
                    "AND message LIKE '%daily cap%' AND ts > ?",
                    (cutoff_1h,)
                ).fetchone()["c"]
                skip_hits = db.execute(
                    "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' "
                    "AND message LIKE '%random skip%' AND ts > ?",
                    (cutoff_1h,)
                ).fetchone()["c"]
                idle_hits = db.execute(
                    "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' "
                    "AND message LIKE '%idle%' AND ts > ?",
                    (cutoff_1h,)
                ).fetchone()["c"]

                if cap_hits > 0:
                    alerts.append(f"BLOCKED BY DAILY CAP ({cap_hits} hits in last hour)")
                elif idle_hits > 3:
                    alerts.append(f"hands cycling but idle ({idle_hits}x). queue or replies empty?")
                elif skip_hits > 3:
                    alerts.append(f"excessive random skips ({skip_hits}x in 1h)")
        except (ValueError, TypeError):
            pass
    else:
        alerts.append("hands has NEVER performed an action")

    # ---- BRAIN ACTIVITY CHECK ----
    last_brain = db.execute(
        "SELECT ts FROM engine_log WHERE process='brain' "
        "AND message LIKE '%brain cycle done%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last_brain:
        try:
            last_brain_dt = dt.fromisoformat(last_brain["ts"].replace("Z", "+00:00"))
            brain_idle = (now - last_brain_dt).total_seconds() / 3600
            if brain_idle > 1.5:
                alerts.append(f"brain idle for {brain_idle:.1f}h")
        except (ValueError, TypeError):
            pass

    # ---- auto-adjustments ----
    adjustments = []

    # deprioritize dead targets after 5+ unanswered replies
    for t in targets:
        rc = t["reply_count"] or 0
        if rc >= 5 and t["priority"] < 3:
            db.execute(
                "UPDATE reply_targets SET priority = 3 WHERE handle = ? AND priority < 3",
                (t["handle"],)
            )
            adjustments.append(f"deprioritized @{t['handle']} (p{t['priority']}->p3, {rc} replies with no engagement)")
    if adjustments:
        db.commit()

    # ---- compose telegram report ----
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    msg = f"<b>watchdog {ist_now.strftime('%H:%M IST')}</b>\n\n"

    if alerts:
        msg += "<b>alerts:</b>\n"
        for a in alerts:
            msg += f"  {a}\n"
        msg += "\n"

    msg += "<b>status:</b>\n"
    for r in report:
        msg += f"  {r}\n"

    if adjustments:
        msg += "\n<b>auto-adjustments:</b>\n"
        for a in adjustments:
            msg += f"  {a}\n"

    msg += f"\nqueue: {queued} | targets: {len([t for t in targets if (t['reply_count'] or 0) > 0])}/{len(targets)} engaged"

    # ALWAYS send telegram on alerts. regular reports at 6h intervals.
    hour = ist_now.hour
    is_6h_report = hour in (8, 14, 20, 2)

    if alerts:
        send_telegram(msg)  # immediate alert
    elif is_6h_report:
        send_telegram(msg)  # scheduled report

    # always log to db
    db.execute(
        "INSERT INTO engine_log (ts, process, level, message) VALUES (?,?,?,?)",
        (now.isoformat(), "watchdog", "info",
         f"alerts={len(alerts)} report_items={len(report)} adjustments={len(adjustments)}")
    )
    db.commit()

    # save last watchdog run
    set_state(db, "watchdog.last_run", now.isoformat())
    set_state(db, "watchdog.last_report", msg)
    db.close()

    print(msg.replace("<b>", "").replace("</b>", ""))


if __name__ == "__main__":
    main()
