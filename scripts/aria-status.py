#!/usr/bin/env python3
"""
aria-status.py -- incubator monitor. your eyes on the engine.

usage:
  python3 scripts/aria-status.py          # quick snapshot
  python3 scripts/aria-status.py --watch  # auto-refresh every 30s
  python3 scripts/aria-status.py --tail   # live log stream (ctrl+c to stop)
"""

from __future__ import annotations
import json, os, sys, sqlite3, subprocess, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")))
DB_PATH = WORKSPACE / "memory" / "aria.db"
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(IST)

def ago(ts_str):
    if not ts_str:
        return "never"
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        hours = delta.total_seconds() / 3600
        if hours < 1:
            mins = int(delta.total_seconds() / 60)
            return f"{mins}m ago"
        if hours < 24:
            return f"{hours:.1f}h ago"
        return f"{delta.days}d ago"
    except:
        return "?"

def health_icon(ok):
    return "OK" if ok else "!!"

def main():
    if "--tail" in sys.argv:
        do_tail()
        return

    watch = "--watch" in sys.argv
    while True:
        if watch:
            os.system("clear")
        show_status()
        if not watch:
            break
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            print("\nstopped.")
            break

def show_status():
    if not DB_PATH.exists():
        print("no database found. run migrate-to-sqlite.py first.")
        return

    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    ist_now = now_ist()

    print()
    print("=" * 60)
    print(f"  ARIA INCUBATOR  |  {ist_now.strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 60)

    # ---- processes ----
    brain_running = "com.aria.brain" in subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True).stdout
    hands_running = "com.aria.hands" in subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True).stdout

    brain_log = db.execute(
        "SELECT ts, message FROM engine_log WHERE process='brain' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    hands_log = db.execute(
        "SELECT ts, message FROM engine_log WHERE process='hands' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    brain_last = ago(brain_log['ts']) if brain_log else 'never'
    hands_last = ago(hands_log['ts']) if hands_log else 'never'

    print(f"\n  PROCESSES")
    print(f"  brain:  {'RUNNING' if brain_running else 'STOPPED':8s}  last: {brain_last}")
    print(f"  hands:  {'RUNNING' if hands_running else 'STOPPED':8s}  last: {hands_last}")

    # health check: warn if last activity > expected interval
    if brain_log and brain_log['ts']:
        try:
            ts = datetime.fromisoformat(brain_log['ts'].replace("Z", "+00:00"))
            brain_stale = (datetime.now(timezone.utc) - ts).total_seconds() > 2400  # >40min
        except:
            brain_stale = False
    else:
        brain_stale = True

    if hands_log and hands_log['ts']:
        try:
            ts = datetime.fromisoformat(hands_log['ts'].replace("Z", "+00:00"))
            hands_stale = (datetime.now(timezone.utc) - ts).total_seconds() > 900  # >15min
        except:
            hands_stale = False
    else:
        hands_stale = True

    if brain_stale and brain_running:
        print(f"  !! brain hasn't logged in >40min (expected every 30min)")
    if hands_stale and hands_running:
        print(f"  !! hands hasn't logged in >15min (expected every 10min)")

    # ---- posting window ----
    current_time = ist_now.strftime("%H:%M")
    windows = [
        ("morning", "07:30", "10:30"),
        ("midday", "11:30", "14:30"),
        ("afternoon", "16:00", "18:00"),
        ("evening", "19:30", "23:00"),
    ]
    in_window = False
    window_name = ""
    for name, start, end in windows:
        if start <= current_time <= end:
            in_window = True
            window_name = name
            break
    if in_window:
        print(f"\n  WINDOW: {window_name} (ACTIVE, tweets will post)")
    else:
        next_w = [(n, s) for n, s, e in windows if s > current_time]
        if next_w:
            print(f"\n  WINDOW: closed (next: {next_w[0][0]} at {next_w[0][1]} IST)")
        else:
            print(f"\n  WINDOW: closed (next: morning at 07:30 IST)")
        print(f"  note: replies post anytime, only original tweets respect windows")

    # ---- content pipeline ----
    queued = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='queued'").fetchone()["c"]
    posted_count = db.execute("SELECT COUNT(*) as c FROM posted").fetchone()["c"]
    ready_replies = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='ready'"
    ).fetchone()["c"]
    posted_replies = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='posted'"
    ).fetchone()["c"]
    failed_replies = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='failed'"
    ).fetchone()["c"]
    posting_replies = db.execute(
        "SELECT COUNT(*) as c FROM reply_drafts WHERE status='posting'"
    ).fetchone()["c"]

    print(f"\n  PIPELINE")
    print(f"  tweet queue:     {queued} ready to post")
    print(f"  reply drafts:    {ready_replies} ready, {posting_replies} posting")
    print(f"  tweets posted:   {posted_count}")
    print(f"  replies posted:  {posted_replies}")
    if failed_replies:
        print(f"  !! replies failed: {failed_replies}")

    # ---- last post ----
    last_post = db.execute(
        "SELECT text, posted_at, tweet_url FROM posted ORDER BY posted_at DESC LIMIT 1"
    ).fetchone()
    if last_post:
        print(f"\n  LAST TWEET ({ago(last_post['posted_at'])})")
        print(f"  \"{last_post['text'][:70]}\"")
        if last_post['tweet_url']:
            print(f"  {last_post['tweet_url']}")

    # ---- recent replies ----
    recent_replies = db.execute(
        "SELECT target_handle, reply_text, posted_at, target_tweet_url "
        "FROM reply_drafts WHERE status='posted' ORDER BY posted_at DESC LIMIT 3"
    ).fetchall()
    if recent_replies:
        print(f"\n  RECENT REPLIES")
        for r in recent_replies:
            url = r['target_tweet_url'] or ''
            print(f"  @{r['target_handle']} ({ago(r['posted_at'])})")
            print(f"    \"{r['reply_text'][:70]}\"")

    # ---- targets ----
    targets = db.execute("SELECT COUNT(*) as c FROM reply_targets").fetchone()["c"]
    replied_targets = db.execute(
        "SELECT COUNT(*) as c FROM reply_targets WHERE last_replied_at IS NOT NULL"
    ).fetchone()["c"]

    # show cooldown status for P1 targets
    p1_targets = db.execute(
        "SELECT handle, last_replied_at, reply_count FROM reply_targets "
        "WHERE priority=1 ORDER BY handle"
    ).fetchall()

    print(f"\n  TARGETS: {replied_targets}/{targets} engaged")
    if p1_targets:
        print(f"  priority 1:")
        for t in p1_targets:
            last = ago(t['last_replied_at']) if t['last_replied_at'] else 'never'
            count = t['reply_count'] or 0
            # check cooldown (4h)
            if t['last_replied_at']:
                try:
                    ts = datetime.fromisoformat(t['last_replied_at'].replace("Z", "+00:00"))
                    hours_since = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                    cd = "ready" if hours_since >= 4 else f"cooldown {4 - hours_since:.1f}h"
                except:
                    cd = "?"
            else:
                cd = "ready"
            print(f"    @{t['handle']:15s}  replies: {count}  last: {last:12s}  {cd}")

    # ---- errors in last 24h ----
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    errors = db.execute(
        "SELECT ts, process, message FROM engine_log "
        "WHERE level='error' AND ts > ? ORDER BY id DESC LIMIT 5",
        (cutoff,)
    ).fetchall()
    if errors:
        print(f"\n  ERRORS (last 24h)")
        for e in errors:
            print(f"  [{e['process']}] {ago(e['ts'])}: {e['message'][:70]}")

    # ---- metrics ----
    latest_metrics = db.execute(
        "SELECT SUM(impressions) as views, SUM(likes) as likes, "
        "SUM(replies) as replies, SUM(bookmarks) as bm "
        "FROM metrics"
    ).fetchone()
    if latest_metrics and latest_metrics["views"]:
        print(f"\n  METRICS (all time)")
        print(f"  views: {latest_metrics['views']}  likes: {latest_metrics['likes']}  "
              f"replies: {latest_metrics['replies']}  bookmarks: {latest_metrics['bm']}")

    # ---- signals ----
    signals = db.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
    fresh_signals = db.execute(
        "SELECT COUNT(*) as c FROM signals WHERE scraped_at > ?",
        ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),)
    ).fetchone()["c"]
    print(f"\n  SIGNALS: {signals} total, {fresh_signals} fresh (<2h)")

    # ---- what happens next ----
    print(f"\n  NEXT ACTIONS")
    if ready_replies > 0:
        next_reply = db.execute(
            "SELECT target_handle FROM reply_drafts WHERE status='ready' LIMIT 1"
        ).fetchone()
        if next_reply:
            print(f"  hands will reply to @{next_reply['target_handle']} (next 10min cycle)")
    if queued > 0 and in_window:
        print(f"  hands will post a tweet from queue (if gap met)")
    elif queued > 0 and not in_window:
        print(f"  {queued} tweets waiting for next posting window")
    if queued == 0:
        print(f"  brain will generate new tweets (next 30min cycle)")

    print()
    print("=" * 60)
    print(f"  commands: --watch (auto refresh) | --tail (live logs)")
    print("=" * 60)
    print()
    db.close()


def do_tail():
    """Live tail of both brain and hands logs, interleaved."""
    brain_log = WORKSPACE / "logs" / "brain-stdout.log"
    hands_log = WORKSPACE / "logs" / "hands-stdout.log"

    print("tailing brain + hands logs (ctrl+c to stop)")
    print("=" * 60)

    try:
        proc = subprocess.Popen(
            ["tail", "-f", "-n", "20", str(brain_log), str(hands_log)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for line in proc.stdout:
            # colorize
            if "SUCCESS" in line:
                print(f"\033[92m{line}\033[0m", end="")
            elif "FAILED" in line or "error" in line.lower():
                print(f"\033[91m{line}\033[0m", end="")
            elif "cycle starting" in line:
                print(f"\033[93m{line}\033[0m", end="")
            else:
                print(line, end="")
    except KeyboardInterrupt:
        proc.terminate()
        print("\nstopped.")


if __name__ == "__main__":
    main()
