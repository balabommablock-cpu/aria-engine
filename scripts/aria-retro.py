#!/usr/bin/env python3
"""
aria-retro.py -- deep retrospective analysis every 6 hours.

analyzes:
  - reply quality and voice consistency
  - which content patterns get engagement
  - target effectiveness
  - pipeline bottlenecks
  - improvement recommendations

generates a structured analysis saved to memory/retro/ and sent to telegram.
designed to be consumed by the scheduled Claude improvement agent.
"""

from __future__ import annotations

import json, os, sys, sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import importlib
shared = importlib.import_module("aria-shared")

get_db       = shared.get_db
init_db      = shared.init_db
send_telegram = shared.send_telegram
call_gemma   = shared.call_gemma
now_utc      = shared.now_utc
get_state    = shared.get_state
set_state    = shared.set_state
WORKSPACE    = shared.WORKSPACE

RETRO_DIR = WORKSPACE / "memory" / "retro"


def main():
    db = get_db()
    init_db()
    now = now_utc()
    cutoff_6h = (now - timedelta(hours=6)).isoformat()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    retro = {
        "timestamp": now.isoformat(),
        "period": "6h",
    }

    # ---- posted content analysis ----
    posts = db.execute(
        "SELECT * FROM posted ORDER BY posted_at DESC LIMIT 20"
    ).fetchall()

    retro["total_posts"] = len(posts)

    # territory distribution
    terr_dist = Counter()
    hook_dist = Counter()
    image_dist = Counter()
    composite_scores = []

    for p in posts:
        terr_dist[p["territory"] or "unknown"] += 1
        image_dist[p["image_type"] or "none"] += 1
        try:
            scores = json.loads(p["scores_json"]) if p["scores_json"] else {}
            hook_dist[scores.get("hook_pattern", "unknown")] += 1
            if "composite" in scores:
                composite_scores.append(scores["composite"])
        except (json.JSONDecodeError, TypeError):
            pass

    retro["territory_distribution"] = dict(terr_dist)
    retro["hook_pattern_distribution"] = dict(hook_dist)
    retro["image_type_distribution"] = dict(image_dist)
    retro["avg_composite_score"] = round(sum(composite_scores) / len(composite_scores), 1) if composite_scores else 0

    # ---- reply analysis ----
    all_replies = db.execute(
        "SELECT * FROM reply_drafts WHERE status='posted' ORDER BY posted_at DESC"
    ).fetchall()
    failed_replies = db.execute(
        "SELECT * FROM reply_drafts WHERE status='failed'"
    ).fetchall()

    retro["total_replies_posted"] = len(all_replies)
    retro["total_replies_failed"] = len(failed_replies)

    # reply text analysis: check for repetitive patterns
    reply_openers = Counter()
    for r in all_replies:
        text = r["reply_text"] or ""
        first_word = text.split()[0] if text.split() else ""
        reply_openers[first_word] += 1

    retro["reply_opener_distribution"] = dict(reply_openers.most_common(5))

    # check for contextual vs fallback ratio from logs
    ctx_logs = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE message LIKE '%using contextual reply%' AND ts > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    fb_logs = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE message LIKE '%using brain draft%' AND ts > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    retro["contextual_reply_rate"] = f"{ctx_logs}/{ctx_logs + fb_logs}" if (ctx_logs + fb_logs) > 0 else "n/a"

    # ---- target effectiveness ----
    targets = db.execute(
        "SELECT handle, priority, reply_count, last_replied_at FROM reply_targets ORDER BY reply_count DESC"
    ).fetchall()

    target_analysis = []
    for t in targets:
        rc = t["reply_count"] or 0
        if rc > 0:
            target_analysis.append({
                "handle": t["handle"],
                "priority": t["priority"],
                "reply_count": rc,
                "last_replied_at": t["last_replied_at"],
            })
    retro["active_targets"] = target_analysis

    # ---- error analysis ----
    errors = db.execute(
        "SELECT ts, process, message FROM engine_log WHERE level='error' AND ts > ? ORDER BY id DESC",
        (cutoff_24h,)
    ).fetchall()

    error_categories = Counter()
    for e in errors:
        msg = e["message"]
        if "could not find tweet" in msg:
            error_categories["tweet_not_found"] += 1
        elif "FAILED" in msg:
            error_categories["action_failed"] += 1
        elif "timeout" in msg.lower():
            error_categories["timeout"] += 1
        else:
            error_categories["other"] += 1

    retro["error_categories_24h"] = dict(error_categories)
    retro["total_errors_24h"] = len(errors)

    # ---- pipeline throughput ----
    brain_cycles = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE process='brain' AND message LIKE '%brain cycle done%' AND ts > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    hands_cycles = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' AND message LIKE '%hands cycle done%' AND ts > ?",
        (cutoff_24h,)
    ).fetchone()["c"]
    retro["brain_cycles_24h"] = brain_cycles
    retro["hands_cycles_24h"] = hands_cycles

    # ---- queue health ----
    queued = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='queued'").fetchone()["c"]
    expired = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='expired'").fetchone()["c"]
    retro["queue_active"] = queued
    retro["queue_expired"] = expired

    # ---- recommendations ----
    recommendations = []

    if retro["total_replies_posted"] < 4 and (ctx_logs + fb_logs) > 0:
        recommendations.append("reply volume low. consider reducing cooldown or adding more P1 targets.")

    dominant_hook = hook_dist.most_common(1)[0] if hook_dist else None
    if dominant_hook and dominant_hook[1] >= 3 and len(posts) >= 5:
        recommendations.append(f"hook pattern '{dominant_hook[0]}' overrepresented ({dominant_hook[1]}/{len(posts)}). force variety.")

    dominant_opener = reply_openers.most_common(1)[0] if reply_openers else None
    if dominant_opener and dominant_opener[1] >= 3:
        recommendations.append(f"reply opener '{dominant_opener[0]}' repeated {dominant_opener[1]}x. add to anti-pattern list.")

    if retro["total_errors_24h"] > 10:
        recommendations.append("high error rate. investigate top error category.")

    if queued == 0:
        recommendations.append("queue empty. brain may need shorter interval or larger batch size.")

    terr_pcts = {k: v / len(posts) for k, v in terr_dist.items()} if posts else {}
    for terr, pct in terr_pcts.items():
        if pct > 0.5:
            recommendations.append(f"territory '{terr}' at {pct:.0%}, too dominant. rebalance weights.")
        if pct < 0.1 and terr != "unknown":
            recommendations.append(f"territory '{terr}' at {pct:.0%}, underrepresented.")

    retro["recommendations"] = recommendations

    # ---- save retro ----
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"retro_{now.strftime('%Y%m%d_%H%M')}.json"
    retro_path = RETRO_DIR / filename
    with open(retro_path, "w") as f:
        json.dump(retro, f, indent=2)

    set_state(db, "retro.last_run", now.isoformat())
    set_state(db, "retro.last_file", str(retro_path))

    # ---- telegram report ----
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    msg = f"<b>retro {ist_now.strftime('%H:%M IST')}</b>\n\n"
    msg += f"posts: {retro['total_posts']} | replies: {retro['total_replies_posted']} | errors: {retro['total_errors_24h']}\n"
    msg += f"contextual rate: {retro['contextual_reply_rate']}\n"
    msg += f"avg composite: {retro['avg_composite_score']}\n"
    msg += f"queue: {queued}\n"

    if retro["hook_pattern_distribution"]:
        msg += f"hooks: {', '.join(f'{k}:{v}' for k,v in retro['hook_pattern_distribution'].items())}\n"

    if recommendations:
        msg += "\n<b>recommendations:</b>\n"
        for r in recommendations:
            msg += f"  {r}\n"

    if target_analysis:
        msg += "\n<b>targets:</b>\n"
        for t in target_analysis[:5]:
            msg += f"  @{t['handle']} p{t['priority']} ({t['reply_count']} replies)\n"

    send_telegram(msg)

    # print for log
    print(json.dumps(retro, indent=2))
    db.close()


if __name__ == "__main__":
    main()
