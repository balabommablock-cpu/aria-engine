#!/usr/bin/env python3
"""
Cortex Backtest — Replay historical ARIA data through cortex to validate learning.

Takes all historical posted+metrics data, creates predictions as if cortex had
been running from the start, measures them against real metrics, runs the
learning cycle, and reports what constraints would have been learned.

This is the cheapest test of whether cortex actually discovers real patterns.

Usage:
    python3 cortex_backtest.py                # full backtest
    python3 cortex_backtest.py --report       # report only (after a previous run)
    python3 cortex_backtest.py --days 30      # backtest last N days only
"""

import sqlite3
import json
import os
import sys
import math
import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List

IST = timezone(timedelta(hours=5, minutes=30))

WORKSPACE = Path(os.environ.get(
    "ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")
))
PROD_DB_PATH = WORKSPACE / "memory" / "aria.db"
BACKTEST_DB_PATH = WORKSPACE / "cortex" / "backtest.db"
SCHEMA_PATH = WORKSPACE / "cortex" / "aria-cortex-schema-v2.sql"

sys.path.insert(0, str(WORKSPACE / "scripts"))


def create_backtest_db() -> sqlite3.Connection:
    """Create a fresh backtest DB with cortex v2 schema."""
    if BACKTEST_DB_PATH.exists():
        BACKTEST_DB_PATH.unlink()

    db = sqlite3.connect(str(BACKTEST_DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    # Load cortex v2 schema
    if SCHEMA_PATH.exists():
        db.executescript(SCHEMA_PATH.read_text())
    else:
        print(f"ERROR: Schema not found at {SCHEMA_PATH}")
        sys.exit(1)

    # Create minimal ARIA tables needed for backtest
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posted (
            id TEXT PRIMARY KEY,
            text TEXT,
            territory TEXT,
            scores_json TEXT,
            image_type TEXT DEFAULT 'none',
            tweet_url TEXT,
            posted_at TEXT NOT NULL,
            self_reply_text TEXT,
            self_replied INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS metrics (
            post_id TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            impressions INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            replies INTEGER DEFAULT 0,
            retweets INTEGER DEFAULT 0,
            bookmarks INTEGER DEFAULT 0,
            PRIMARY KEY (post_id, scraped_at)
        );
        CREATE TABLE IF NOT EXISTS queue (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            territory TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            scores_json TEXT,
            image_type TEXT DEFAULT 'none',
            image_path TEXT,
            generated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            generator TEXT DEFAULT 'claude-opus',
            card_text TEXT DEFAULT ""
        );
        CREATE TABLE IF NOT EXISTS engagements (
            id INTEGER PRIMARY KEY,
            action TEXT NOT NULL,
            post_id TEXT,
            target_handle TEXT,
            target_tweet_url TEXT,
            text TEXT,
            performed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reply_drafts (
            id TEXT PRIMARY KEY,
            target_handle TEXT NOT NULL,
            target_tweet_url TEXT NOT NULL,
            target_tweet_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            score REAL DEFAULT 0,
            generated_at TEXT NOT NULL,
            posted_at TEXT
        );
    """)
    return db


def copy_historical_data(prod_db: sqlite3.Connection, bt_db: sqlite3.Connection,
                          days_back: int = None):
    """Copy historical data from production DB to backtest DB."""
    cutoff = ""
    if days_back:
        cutoff_dt = datetime.now(IST) - timedelta(days=days_back)
        cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Copy posted
    if cutoff:
        rows = prod_db.execute(
            "SELECT * FROM posted WHERE posted_at > ? ORDER BY posted_at ASC", (cutoff,)
        ).fetchall()
    else:
        rows = prod_db.execute("SELECT * FROM posted ORDER BY posted_at ASC").fetchall()

    post_count = 0
    for r in rows:
        try:
            bt_db.execute(
                """INSERT OR IGNORE INTO posted (id, text, territory, scores_json, image_type,
                   tweet_url, posted_at, self_reply_text, self_replied)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], r["text"], r["territory"], r["scores_json"],
                 r["image_type"], r["tweet_url"], r["posted_at"],
                 r["self_reply_text"], r["self_replied"])
            )
            post_count += 1
        except (sqlite3.IntegrityError, IndexError):
            pass

    # Copy metrics
    metric_count = 0
    for r in prod_db.execute("SELECT * FROM metrics").fetchall():
        try:
            bt_db.execute(
                """INSERT OR IGNORE INTO metrics (post_id, scraped_at, impressions, likes,
                   replies, retweets, bookmarks)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["post_id"], r["scraped_at"], r["impressions"], r["likes"],
                 r["replies"], r["retweets"], r["bookmarks"])
            )
            metric_count += 1
        except (sqlite3.IntegrityError, IndexError):
            pass

    # Copy queue
    queue_count = 0
    for r in prod_db.execute("SELECT * FROM queue").fetchall():
        try:
            bt_db.execute(
                """INSERT OR IGNORE INTO queue (id, text, territory, status, scores_json,
                   image_type, generated_at, expires_at, generator, card_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], r["text"], r["territory"], r["status"], r["scores_json"],
                 r["image_type"], r["generated_at"], r["expires_at"],
                 r["generator"], r["card_text"] or "")
            )
            queue_count += 1
        except (sqlite3.IntegrityError, IndexError, KeyError):
            pass

    # Copy reply_drafts
    reply_count = 0
    for r in prod_db.execute("SELECT * FROM reply_drafts").fetchall():
        try:
            bt_db.execute(
                """INSERT OR IGNORE INTO reply_drafts (id, target_handle, target_tweet_url,
                   target_tweet_text, reply_text, status, score, generated_at, posted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], r["target_handle"], r["target_tweet_url"],
                 r["target_tweet_text"], r["reply_text"], r["status"],
                 r["score"], r["generated_at"], r["posted_at"])
            )
            reply_count += 1
        except (sqlite3.IntegrityError, IndexError):
            pass

    # Copy engagements
    eng_count = 0
    for r in prod_db.execute("SELECT * FROM engagements").fetchall():
        try:
            bt_db.execute(
                """INSERT OR IGNORE INTO engagements (action, post_id, target_handle,
                   target_tweet_url, text, performed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["action"], r["post_id"], r["target_handle"],
                 r["target_tweet_url"], r["text"], r["performed_at"])
            )
            eng_count += 1
        except (sqlite3.IntegrityError, IndexError):
            pass

    bt_db.commit()
    print(f"Copied: {post_count} posts, {metric_count} metric snapshots, "
          f"{queue_count} queue items, {reply_count} reply drafts, {eng_count} engagements")
    return post_count


def create_retrospective_predictions(bt_db: sqlite3.Connection):
    """
    Create predictions for each historical post as if cortex had been running.
    Uses a naive baseline predictor (overall average) so we can measure
    how well cortex would have learned.
    """
    posts = bt_db.execute("""
        SELECT p.id, p.text, p.territory, p.posted_at, p.scores_json,
               p.image_type, p.tweet_url
        FROM posted p
        ORDER BY p.posted_at ASC
    """).fetchall()

    if not posts:
        print("No posts found for backtest")
        return 0

    # Get best metrics for each post
    post_metrics = {}
    for m in bt_db.execute("""
        SELECT post_id, MAX(impressions) as impressions,
               MAX(likes) as likes, MAX(retweets) as retweets,
               MAX(replies) as replies, MAX(bookmarks) as bookmarks
        FROM metrics GROUP BY post_id
    """).fetchall():
        post_metrics[m["post_id"]] = {
            "impressions": m["impressions"] or 0,
            "likes": m["likes"] or 0,
            "retweets": m["retweets"] or 0,
            "replies": m["replies"] or 0,
            "bookmarks": m["bookmarks"] or 0,
        }

    # Running stats for naive predictor
    seen_impressions = []
    seen_engagements = []
    pred_count = 0

    for post in posts:
        pid = post["id"]
        metrics = post_metrics.get(pid)

        if not metrics or metrics["impressions"] == 0:
            continue

        actual_imp = metrics["impressions"]
        actual_eng = metrics["likes"] + metrics["retweets"] + metrics["replies"]
        actual_rate = actual_eng / max(actual_imp, 1)

        # Naive prediction: running average of what we've seen so far
        if seen_impressions:
            predicted_imp = statistics.mean(seen_impressions)
            predicted_eng = statistics.mean(seen_engagements)
            predicted_var = statistics.variance(seen_impressions) if len(seen_impressions) > 1 else predicted_imp ** 2
        else:
            # Cold start: use a very rough prior
            predicted_imp = 10
            predicted_eng = 0.5
            predicted_var = 100

        predicted_rate = predicted_eng / max(predicted_imp, 1)

        # Parse posted_at
        posted_at = post["posted_at"]
        if posted_at and posted_at.endswith("Z"):
            posted_at = posted_at[:-1] + "+00:00"
        try:
            post_dt = datetime.fromisoformat(posted_at)
        except (ValueError, TypeError):
            post_dt = datetime.now(IST)

        hour_bucket = post_dt.hour
        day_of_week = post_dt.weekday()

        # Extract hook pattern from scores_json
        hook_pattern = "unknown"
        try:
            scores = json.loads(post["scores_json"] or "{}")
            if scores:
                hook_pattern = max(scores, key=lambda k: scores[k] if isinstance(scores[k], (int, float)) else 0)
        except (json.JSONDecodeError, TypeError):
            pass

        # Compute z-score
        pred_std = math.sqrt(max(predicted_var, 1))
        z_score = (actual_imp - predicted_imp) / max(pred_std, 1)
        error_imp = predicted_imp - actual_imp
        surprise = abs(math.log(max(actual_imp, 1) / max(predicted_imp, 1)))
        is_spike = 1 if (z_score > 2.0 or actual_imp > predicted_imp * 3) else 0

        features = {
            "territory": post["territory"] or "unknown",
            "hook_pattern": hook_pattern,
            "hour_bucket": hour_bucket,
            "day_of_week": day_of_week,
            "has_image": 1 if post["image_type"] and post["image_type"] != "none" else 0,
            "word_count": len((post["text"] or "").split()),
        }

        ts_str = post_dt.strftime("%Y-%m-%dT%H:%M:%S")
        measured_at = (post_dt + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")

        bt_db.execute(
            """INSERT INTO predictions
               (ts, action_type, action_ref, territory, hook_pattern, hour_bucket, day_of_week,
                predicted_impressions, predicted_engagements, predicted_engagement_rate,
                predicted_variance, confidence, reasoning, features_json,
                prediction_method, confounders_json,
                actual_impressions, actual_engagements, actual_engagement_rate,
                measured_at, error_impressions, error_engagements,
                surprise_score, z_score, lesson, is_spike)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts_str, "post_tweet", pid, post["territory"] or "unknown", hook_pattern,
             hour_bucket, day_of_week,
             predicted_imp, predicted_eng, predicted_rate,
             predicted_var, 0.3, "backtest: naive running average",
             json.dumps(features), "baseline", "{}",
             actual_imp, actual_eng, actual_rate,
             measured_at, error_imp, predicted_eng - actual_eng,
             surprise, z_score, f"backtest prediction #{pred_count + 1}", is_spike)
        )

        # Also create predictions for posted replies
        seen_impressions.append(actual_imp)
        seen_engagements.append(actual_eng)
        pred_count += 1

    # Add reply predictions
    replies = bt_db.execute("""
        SELECT r.id, r.target_handle, r.reply_text, r.posted_at, r.score
        FROM reply_drafts r
        WHERE r.status = 'posted' AND r.posted_at IS NOT NULL
        ORDER BY r.posted_at ASC
    """).fetchall()

    reply_pred_count = 0
    for reply in replies:
        posted_at = reply["posted_at"]
        if posted_at and posted_at.endswith("Z"):
            posted_at = posted_at[:-1] + "+00:00"
        try:
            reply_dt = datetime.fromisoformat(posted_at)
        except (ValueError, TypeError):
            reply_dt = datetime.now(IST)

        bt_db.execute(
            """INSERT INTO predictions
               (ts, action_type, action_ref, target_handle, territory, hook_pattern,
                hour_bucket, day_of_week,
                predicted_impressions, predicted_engagements, predicted_engagement_rate,
                predicted_variance, confidence, reasoning, features_json,
                prediction_method, confounders_json,
                actual_impressions, actual_engagements, actual_engagement_rate,
                measured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (reply_dt.strftime("%Y-%m-%dT%H:%M:%S"),
             "post_reply", reply["id"], reply["target_handle"],
             "", "unknown", reply_dt.hour, reply_dt.weekday(),
             0, 1, 0.02, 1, 0.2,
             "backtest: reply baseline (no metrics available)",
             json.dumps({"target_handle": reply["target_handle"]}),
             "baseline", "{}",
             0, 0, 0,
             (reply_dt + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S"))
        )
        reply_pred_count += 1

    bt_db.commit()
    print(f"Created {pred_count} tweet predictions + {reply_pred_count} reply predictions")
    return pred_count


def run_backtest_learning(bt_db: sqlite3.Connection):
    """Run cortex learning cycle on backtest data."""
    try:
        # Try importing the cortex module
        import importlib
        import importlib.util
        cortex_spec = importlib.util.spec_from_file_location(
            "aria_cortex", WORKSPACE / "scripts" / "aria-cortex.py"
        )
        cortex_mod = importlib.util.module_from_spec(cortex_spec)
        cortex_spec.loader.exec_module(cortex_mod)

        # Initialize knobs
        cortex_mod.init_knobs(bt_db)

        # Run learning components
        print("\nRunning cortex learning cycle on historical data...")

        # Spike detection
        spiker = cortex_mod.SpikeDetector(bt_db)
        new_spikes = spiker.detect_and_analyze()
        print(f"  Spikes detected: {new_spikes}")

        # Constraint learning
        memory = cortex_mod.AdaptiveMemory(bt_db)
        memory.learn_from_predictions()

        # Performance summary
        predictor = cortex_mod.PredictionEngine(bt_db)
        predictor.compute_performance_summary()

        # Spike replication scorecard
        spiker.compute_replication_scorecard()

        # Self-modifier analysis (propose but don't apply)
        modifier = cortex_mod.SelfModifier(bt_db)
        exp_id = modifier.propose_experiment()
        if exp_id:
            print(f"  Proposed experiment #{exp_id}")

        print("  Learning cycle complete")
        return True

    except Exception as e:
        print(f"  Import/run error: {e}")
        print("  Falling back to standalone learning...")
        return _standalone_learning(bt_db)


def _standalone_learning(bt_db: sqlite3.Connection) -> bool:
    """Standalone learning when cortex module can't be imported."""
    # Territory constraints
    territories = bt_db.execute(
        """SELECT territory, AVG(actual_engagement_rate) as avg_rate, COUNT(*) as n
           FROM predictions
           WHERE actual_impressions IS NOT NULL AND actual_impressions > 0 AND territory IS NOT NULL
           GROUP BY territory HAVING n >= 2"""
    ).fetchall()

    overall_rate = bt_db.execute(
        """SELECT AVG(actual_engagement_rate) as rate FROM predictions
           WHERE actual_impressions IS NOT NULL AND actual_impressions > 0"""
    ).fetchone()["rate"]

    for t in territories:
        if t["avg_rate"] and overall_rate and overall_rate > 0:
            ratio = t["avg_rate"] / overall_rate
            if ratio < 0.6 or ratio > 1.5:
                bt_db.execute(
                    """INSERT INTO learned_constraints
                       (ts, constraint_type, scope, target_field, target_value,
                        modifier, reason, observation_count, min_observations, active, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
                     "score_multiplier", "tweet", "territory", t["territory"],
                     max(0.3, min(1.8, ratio)),
                     f"backtest: {t['territory']} at {ratio:.0%} of avg (n={t['n']})",
                     t["n"], 2, 1, "statistical")
                )

    # Spike detection
    spikes = bt_db.execute(
        "SELECT * FROM predictions WHERE is_spike = 1"
    ).fetchall()
    for spike in spikes:
        bt_db.execute(
            """INSERT INTO spike_events
               (ts, prediction_id, actual_impressions, expected_impressions,
                spike_magnitude, z_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
             spike["id"], spike["actual_impressions"] or 0,
             spike["predicted_impressions"] or 1,
             (spike["actual_impressions"] or 0) / max(spike["predicted_impressions"] or 1, 1),
             spike["z_score"] or 0)
        )

    bt_db.commit()
    return True


def print_report(bt_db: sqlite3.Connection):
    """Print backtest results."""
    print("\n" + "=" * 60)
    print("CORTEX BACKTEST REPORT")
    print("=" * 60)

    # Data summary
    total_preds = bt_db.execute("SELECT COUNT(*) as n FROM predictions").fetchone()["n"]
    measured = bt_db.execute(
        "SELECT COUNT(*) as n FROM predictions WHERE actual_impressions IS NOT NULL AND actual_impressions > 0"
    ).fetchone()["n"]
    tweets = bt_db.execute(
        "SELECT COUNT(*) as n FROM predictions WHERE action_type = 'post_tweet' AND actual_impressions > 0"
    ).fetchone()["n"]
    replies = bt_db.execute(
        "SELECT COUNT(*) as n FROM predictions WHERE action_type = 'post_reply'"
    ).fetchone()["n"]

    print(f"\nData: {total_preds} predictions ({tweets} tweets, {replies} replies), {measured} with metrics")

    if measured < 3:
        print("\nINSUFFICIENT DATA for meaningful backtest.")
        print(f"Need at least 10 posts with metrics. Currently have {measured}.")
        print("Run this again after more data accumulates.")
        print("=" * 60)
        return

    # Prediction accuracy
    rows = bt_db.execute(
        """SELECT predicted_impressions, actual_impressions, z_score, surprise_score
           FROM predictions
           WHERE actual_impressions IS NOT NULL AND actual_impressions > 0
           ORDER BY ts ASC"""
    ).fetchall()

    predicted = [r["predicted_impressions"] for r in rows]
    actual = [r["actual_impressions"] for r in rows]
    abs_errors = [abs(p - a) for p, a in zip(predicted, actual)]
    within_50 = sum(1 for p, a in zip(predicted, actual) if abs(p - a) < 0.5 * max(p, a, 1))

    print(f"\nPrediction accuracy (naive baseline):")
    print(f"  MAE: {statistics.mean(abs_errors):.1f}")
    print(f"  Median error: {statistics.median(abs_errors):.1f}")
    print(f"  Within 50%: {within_50}/{len(rows)} ({within_50/len(rows):.0%})")

    if len(predicted) > 2:
        mx, my = statistics.mean(predicted), statistics.mean(actual)
        sx, sy = statistics.stdev(predicted), statistics.stdev(actual)
        if sx > 0 and sy > 0:
            cov = sum((x - mx) * (y - my) for x, y in zip(predicted, actual)) / (len(predicted) - 1)
            r = cov / (sx * sy)
            print(f"  Correlation (r): {r:.3f}")
        else:
            print(f"  Correlation: N/A (no variance)")
    else:
        print(f"  Correlation: N/A (need >2 data points)")

    # Territory breakdown
    territory_stats = bt_db.execute(
        """SELECT territory,
                  COUNT(*) as n,
                  AVG(actual_impressions) as avg_imp,
                  AVG(actual_engagement_rate) as avg_rate,
                  AVG(z_score) as avg_z
           FROM predictions
           WHERE actual_impressions IS NOT NULL AND actual_impressions > 0
           GROUP BY territory
           ORDER BY avg_imp DESC"""
    ).fetchall()

    if territory_stats:
        print(f"\nTerritory breakdown:")
        for t in territory_stats:
            print(f"  {t['territory']}: n={t['n']}, avg_imp={t['avg_imp']:.1f}, "
                  f"avg_rate={t['avg_rate']:.3f}, avg_z={t['avg_z']:.2f}")

    # Learned constraints
    constraints = bt_db.execute(
        "SELECT * FROM learned_constraints WHERE active = 1"
    ).fetchall()
    if constraints:
        print(f"\nLearned constraints ({len(constraints)}):")
        for c in constraints:
            direction = "boost" if c["modifier"] > 1.0 else "penalty"
            print(f"  {c['target_field']}={c['target_value']}: x{c['modifier']:.2f} "
                  f"({direction}, n={c['observation_count']})")
    else:
        print(f"\nNo constraints learned (need more data)")

    # Spikes
    spikes = bt_db.execute("SELECT COUNT(*) as n FROM spike_events").fetchone()["n"]
    print(f"\nSpikes detected: {spikes}")
    if spikes > 0:
        top_spikes = bt_db.execute(
            """SELECT se.spike_magnitude, se.z_score, p.territory, p.hook_pattern
               FROM spike_events se
               JOIN predictions p ON se.prediction_id = p.id
               ORDER BY se.spike_magnitude DESC LIMIT 5"""
        ).fetchall()
        for s in top_spikes:
            print(f"  {s['spike_magnitude']:.1f}x: {s['territory']} / {s['hook_pattern']} (z={s['z_score']:.1f})")

    # Performance summary
    perf = bt_db.execute(
        "SELECT * FROM prediction_performance ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if perf:
        print(f"\nPerformance summary:")
        print(f"  Trend: {perf['trend']}")
        try:
            print(f"  Correlation: {perf['correlation']:.3f}")
        except (IndexError, KeyError, TypeError):
            pass

    # Timing analysis
    hour_stats = bt_db.execute(
        """SELECT hour_bucket, COUNT(*) as n, AVG(actual_impressions) as avg_imp
           FROM predictions
           WHERE actual_impressions IS NOT NULL AND actual_impressions > 0
           GROUP BY hour_bucket ORDER BY hour_bucket"""
    ).fetchall()
    if hour_stats:
        print(f"\nHourly performance:")
        for h in hour_stats:
            bar = "#" * max(1, int(h["avg_imp"]))
            print(f"  {h['hour_bucket']:2d}h: {h['avg_imp']:6.1f} imp (n={h['n']}) {bar}")

    # Verdict
    print(f"\n{'=' * 60}")
    if measured < 10:
        print("VERDICT: Too little data for confident conclusions.")
        print(f"Have {measured} measured posts. Need 30+ for territory learning,")
        print("50+ for timing patterns, 100+ for compound constraints.")
    elif measured < 30:
        print("VERDICT: Early signal. Territory patterns may be emerging.")
        print("Constraints learned should be treated as tentative.")
    else:
        print("VERDICT: Sufficient data for baseline learning.")
        if constraints:
            print(f"Cortex would have learned {len(constraints)} actionable constraints.")
        if spikes:
            print(f"Spike analysis identified {spikes} outlier events for replication.")
    print("=" * 60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cortex Backtest")
    parser.add_argument("--report", action="store_true", help="Report only (use existing backtest.db)")
    parser.add_argument("--days", type=int, default=None, help="Backtest last N days only")
    args = parser.parse_args()

    if args.report:
        if not BACKTEST_DB_PATH.exists():
            print("No backtest.db found. Run without --report first.")
            sys.exit(1)
        bt_db = sqlite3.connect(str(BACKTEST_DB_PATH))
        bt_db.row_factory = sqlite3.Row
        print_report(bt_db)
        bt_db.close()
        return

    # Full backtest
    print("=== CORTEX BACKTEST ===\n")

    if not PROD_DB_PATH.exists():
        print(f"Production DB not found at {PROD_DB_PATH}")
        sys.exit(1)

    prod_db = sqlite3.connect(str(PROD_DB_PATH))
    prod_db.row_factory = sqlite3.Row

    bt_db = create_backtest_db()

    post_count = copy_historical_data(prod_db, bt_db, days_back=args.days)
    prod_db.close()

    if post_count == 0:
        print("No historical data to backtest")
        bt_db.close()
        return

    pred_count = create_retrospective_predictions(bt_db)

    if pred_count > 0:
        # Load knobs config
        knobs_path = WORKSPACE / "cortex" / "cortex-knobs.json"
        if knobs_path.exists():
            config = json.loads(knobs_path.read_text())
            for name, spec in config.get("knobs", {}).items():
                try:
                    bt_db.execute(
                        """INSERT OR IGNORE INTO knob_state
                           (knob_name, current_value, default_value, min_value, max_value,
                            description, last_modified, modified_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (name, spec["default"], spec["default"], spec["min"], spec["max"],
                         spec.get("description", ""),
                         datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"), "default")
                    )
                except sqlite3.IntegrityError:
                    pass
            bt_db.commit()

        run_backtest_learning(bt_db)

    print_report(bt_db)
    bt_db.close()
    print(f"\nBacktest DB saved to {BACKTEST_DB_PATH}")
    print("Re-run with --report to view results without regenerating.")


if __name__ == "__main__":
    main()
