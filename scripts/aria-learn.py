#!/usr/bin/env python3
"""
aria-learn.py v2.1 -- ARIA Voice Engine: Continuous Learning

Now triggered by aria-metrics.py after every metrics check.
Updates weights based on X algo score (not just impressions).
Algo score = likes*1 + retweets*20 + replies*13.5 + bookmarks*10

Usage:
    python3 aria-learn.py              # full learn cycle
    python3 aria-learn.py --dry-run    # show what would change
"""

import json, os, sys, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
from urllib import request as urllib_request

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE", os.path.expanduser("~/.openclaw/agents/aria/workspace")))
VOICE_PATH = WORKSPACE / "voice.json"
POSTED_PATH = WORKSPACE / "memory" / "posted.jsonl"
LOG_PATH = WORKSPACE / "logs" / "learn.log"

BLEND_FACTOR = 0.3
MIN_POSTS_TO_LEARN = 5
GOLDEN_PROMOTION_THRESHOLD = 2.0
MAX_GOLDEN = 20

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8794849679:AAGUiW5aIKeGzChVeSOMzpMFeqw0g-gGqII")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7353580848")


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_voice():
    with open(VOICE_PATH) as f:
        return json.load(f)

def save_voice(voice):
    voice["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(VOICE_PATH, "w") as f:
        json.dump(voice, f, indent=2)

def load_posted():
    if not POSTED_PATH.exists():
        return []
    posts = []
    with open(POSTED_PATH) as f:
        for line in f:
            try: posts.append(json.loads(line.strip()))
            except: pass
    return posts


def compute_algo_score(metrics):
    """X's actual scoring formula from open-sourced code."""
    return (
        metrics.get("likes", 0) * 1 +
        metrics.get("retweets", 0) * 20 +
        metrics.get("replies", 0) * 13.5 +
        metrics.get("bookmarks", 0) * 10
    )


def compute_territory_performance(posts):
    """Group by territory, compute avg algo score (not just impressions)."""
    stats = defaultdict(lambda: {"algo_scores": [], "impressions": [], "profile_visits": [], "count": 0})

    for post in posts:
        territory = post.get("territory")
        metrics = post.get("metrics", {})
        if not territory or metrics.get("impressions") is None:
            continue

        algo_score = metrics.get("algo_score") or compute_algo_score(metrics)
        s = stats[territory]
        s["count"] += 1
        s["algo_scores"].append(algo_score)
        s["impressions"].append(metrics.get("impressions", 0))
        s["profile_visits"].append(metrics.get("profile_visits", 0) or 0)

    result = {}
    for t, s in stats.items():
        if s["count"] == 0:
            continue
        result[t] = {
            "count": s["count"],
            "avg_algo_score": sum(s["algo_scores"]) / len(s["algo_scores"]),
            "avg_impressions": sum(s["impressions"]) / len(s["impressions"]),
            "avg_profile_visits": sum(s["profile_visits"]) / len(s["profile_visits"]) if s["profile_visits"] else 0,
            "total_algo_score": sum(s["algo_scores"])
        }
    return result


def update_weights(voice, perf, dry_run=False):
    """Update territory weights using algo score as the primary signal."""
    current = voice["territory_weights"]

    # Use algo score (weighted engagement), not raw impressions
    total_score = sum(t["avg_algo_score"] * t["count"] for t in perf.values())
    if total_score == 0:
        log("no algo score data. skipping.")
        return False

    new_weights = {}
    for territory in current:
        old_w = current[territory]
        if territory in perf:
            contribution = (perf[territory]["avg_algo_score"] * perf[territory]["count"]) / total_score
            new_w = old_w * (1 - BLEND_FACTOR) + contribution * BLEND_FACTOR
        else:
            new_w = old_w * 0.95

        # Enforce variety bounds
        variety = voice.get("variety", {})
        min_w = variety.get("min_territory_pct_weekly", 0.15)
        max_w = variety.get("max_territory_pct_weekly", 0.40)
        new_weights[territory] = round(max(min(new_w, max_w), min_w), 4)

    # Normalize
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}

    log("weight update:")
    changed = False
    for t in sorted(current.keys()):
        old = current[t]
        new = new_weights.get(t, old)
        p = perf.get(t, {})
        delta = new - old
        if abs(delta) > 0.001:
            changed = True
        log(f"  {t}: {old:.3f} -> {new:.3f} ({'+'if delta>0 else ''}{delta:.3f}) "
            f"[posts={p.get('count',0)}, avg_algo={p.get('avg_algo_score',0):.0f}]")

    if not dry_run and changed:
        voice["territory_weights"] = new_weights
    return changed


def evolve_golden_set(voice, posts, perf, dry_run=False):
    """Promote top-performing tweets to golden set."""
    candidates = []
    for post in posts:
        t = post.get("territory")
        m = post.get("metrics", {})
        if not t or m.get("impressions") is None:
            continue
        if t not in perf:
            continue

        algo_score = m.get("algo_score") or compute_algo_score(m)
        avg = perf[t]["avg_algo_score"]
        if avg == 0:
            continue

        ratio = algo_score / avg
        if ratio >= GOLDEN_PROMOTION_THRESHOLD:
            existing_texts = {g["text"] for g in voice["golden_tweets"]}
            if post["text"] not in existing_texts:
                candidates.append({
                    "text": post["text"],
                    "territory": t,
                    "ratio": ratio,
                    "algo_score": algo_score
                })

    candidates.sort(key=lambda c: c["ratio"], reverse=True)

    if candidates:
        top = candidates[0]
        log(f"golden candidate: [{top['ratio']:.1f}x avg] \"{top['text'][:60]}...\"")
        if not dry_run and len(voice["golden_tweets"]) < MAX_GOLDEN:
            voice["golden_tweets"].append({
                "id": f"g{len(voice['golden_tweets'])+1:02d}",
                "text": top["text"],
                "territory": top["territory"],
                "style": "earned",
                "promoted_at": datetime.now(timezone.utc).isoformat(),
                "performance_ratio": top["ratio"]
            })
            log(f"  PROMOTED to golden set")
            return top
    return None


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode()
        req = urllib_request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib_request.urlopen(req, timeout=10): pass
    except: pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log("=" * 60)
    log("ARIA v2.1 learn starting")

    voice = load_voice()
    posts = load_posted()
    log(f"posted tweets: {len(posts)}")

    perf = compute_territory_performance(posts)
    total_with_metrics = sum(t["count"] for t in perf.values())
    log(f"posts with metrics: {total_with_metrics}")

    if total_with_metrics < MIN_POSTS_TO_LEARN:
        log(f"need {MIN_POSTS_TO_LEARN} posts with metrics. have {total_with_metrics}.")
        return

    for t, p in sorted(perf.items()):
        log(f"  {t}: {p['count']} posts, avg_algo={p['avg_algo_score']:.0f}, avg_imp={p['avg_impressions']:.0f}")

    changed = update_weights(voice, perf, dry_run=args.dry_run)
    promoted = evolve_golden_set(voice, posts, perf, dry_run=args.dry_run)

    if (changed or promoted) and not args.dry_run:
        save_voice(voice)
        log("voice.json updated")

    # Summary
    summary_lines = ["<b>ARIA learn cycle</b>\n"]
    for t in sorted(perf.keys()):
        p = perf[t]
        w = voice["territory_weights"].get(t, 0)
        summary_lines.append(f"{t}: {p['count']} posts, algo={p['avg_algo_score']:.0f}, weight={w:.2f}")
    if promoted:
        summary_lines.append(f"\nnew golden: \"{promoted['text'][:60]}...\"")
    summary_lines.append(f"\ngolden set: {len(voice['golden_tweets'])} tweets")

    summary = "\n".join(summary_lines)
    log(summary)
    if not args.dry_run:
        send_telegram(summary)

    log("learn complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
