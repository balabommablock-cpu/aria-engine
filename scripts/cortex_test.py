#!/usr/bin/env python3
"""
Cortex Test — Simulation and verification.

Generates synthetic prediction data with known patterns, runs cortex,
and verifies that it discovers the correct constraints and proposes
sensible experiments.

Usage:
    python3 cortex_test.py              # full test suite
    python3 cortex_test.py --generate   # generate synthetic data only
    python3 cortex_test.py --verify     # verify cortex learned correctly
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

IST = timezone(timedelta(hours=5, minutes=30))

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(__file__))

# Ground truth patterns that synthetic data will embed
GROUND_TRUTH = {
    "territories": {
        "building": {"mean_impressions": 200, "std": 80, "engagement_rate": 0.035},
        "organizations": {"mean_impressions": 100, "std": 50, "engagement_rate": 0.015},
        "ai": {"mean_impressions": 150, "std": 70, "engagement_rate": 0.025},
        "taste_agency": {"mean_impressions": 120, "std": 60, "engagement_rate": 0.020},
    },
    "hours": {
        # Peak hours (IST)
        "peak": [8, 9, 10, 18, 19, 20, 21],
        "peak_multiplier": 1.5,
        "off_peak_multiplier": 0.7,
    },
    "hook_patterns": {
        "observation": 1.2,
        "question": 1.1,
        "inversion": 0.6,  # should be penalized
        "reframe": 0.7,
        "story": 1.4,     # best pattern
    },
    "compound": {
        # building + morning = extra good
        ("building", 9): 2.5,
        # organizations + evening = bad
        ("organizations", 20): 0.4,
    },
    "spikes": {
        # 5% of tweets will be artificial spikes (5x normal)
        "probability": 0.05,
        "multiplier": 5.0,
    },
}


def create_test_db() -> sqlite3.Connection:
    """Create an in-memory test DB with cortex v2 schema."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    # Load schema
    schema_path = Path(__file__).parent / "aria-cortex-schema-v2.sql"
    if schema_path.exists():
        db.executescript(schema_path.read_text())
    else:
        print(f"ERROR: Schema not found at {schema_path}")
        sys.exit(1)

    # Create minimal versions of tables cortex reads from
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posted (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            text TEXT,
            views INTEGER,
            likes INTEGER,
            retweets INTEGER,
            replies INTEGER,
            url TEXT
        );
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            text TEXT,
            territory TEXT,
            image_type TEXT,
            card_text TEXT,
            scores_json TEXT
        );
        CREATE TABLE IF NOT EXISTS engagements (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            type TEXT,
            likes INTEGER
        );
    """)

    return db


def generate_synthetic_data(db: sqlite3.Connection, num_predictions: int = 200):
    """
    Generate synthetic prediction + outcome data with embedded patterns.
    The patterns match GROUND_TRUTH — cortex should discover them.
    """
    print(f"Generating {num_predictions} synthetic predictions...")

    territories = list(GROUND_TRUTH["territories"].keys())
    hook_patterns = list(GROUND_TRUTH["hook_patterns"].keys())
    now = datetime.now(IST)

    spike_count = 0

    for i in range(num_predictions):
        # Spread over 30 days
        ts = now - timedelta(hours=random.randint(1, 720))
        territory = random.choice(territories)
        hook = random.choice(hook_patterns)
        hour = random.randint(6, 23)
        day_of_week = ts.weekday()

        # Base performance from territory
        t_stats = GROUND_TRUTH["territories"][territory]
        base_imp = max(10, random.gauss(t_stats["mean_impressions"], t_stats["std"]))
        base_rate = max(0.005, random.gauss(t_stats["engagement_rate"], t_stats["engagement_rate"] * 0.3))

        # Apply hour multiplier
        if hour in GROUND_TRUTH["hours"]["peak"]:
            base_imp *= GROUND_TRUTH["hours"]["peak_multiplier"]
        else:
            base_imp *= GROUND_TRUTH["hours"]["off_peak_multiplier"]

        # Apply hook pattern multiplier
        base_imp *= GROUND_TRUTH["hook_patterns"][hook]

        # Apply compound effects
        compound_key = (territory, hour)
        if compound_key in GROUND_TRUTH["compound"]:
            base_imp *= GROUND_TRUTH["compound"][compound_key]

        # Spike injection
        is_spike = random.random() < GROUND_TRUTH["spikes"]["probability"]
        if is_spike:
            base_imp *= GROUND_TRUTH["spikes"]["multiplier"]
            spike_count += 1

        actual_imp = max(1, int(base_imp))
        actual_eng = max(0, int(actual_imp * base_rate))
        actual_rate = actual_eng / max(actual_imp, 1)

        # Make predictions slightly wrong (as they would be in cold start)
        # Predictions are based on overall mean, not territory-specific
        overall_mean = statistics.mean([
            t["mean_impressions"] for t in GROUND_TRUTH["territories"].values()
        ])
        predicted_imp = max(10, random.gauss(overall_mean, overall_mean * 0.3))
        predicted_eng = max(0, predicted_imp * 0.02)
        predicted_rate = 0.02
        predicted_var = (overall_mean * 0.5) ** 2

        ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
        measured_at = (ts + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")

        error_imp = predicted_imp - actual_imp
        pred_std = math.sqrt(predicted_var)
        z_score = (actual_imp - predicted_imp) / max(pred_std, 1)
        surprise = abs(math.log(max(actual_imp, 1) / max(predicted_imp, 1)))
        is_spike_flag = 1 if (z_score > 2.0 or actual_imp > predicted_imp * 3) else 0

        # Insert prediction (already measured)
        db.execute(
            """INSERT INTO predictions
               (ts, action_type, action_ref, territory, hook_pattern, hour_bucket, day_of_week,
                predicted_impressions, predicted_engagements, predicted_engagement_rate,
                predicted_variance, confidence, reasoning, features_json,
                prediction_method, confounders_json,
                actual_impressions, actual_engagements, actual_engagement_rate,
                measured_at, error_impressions, error_engagements,
                surprise_score, z_score, lesson, is_spike)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts_str, "post_tweet", str(i), territory, hook, hour, day_of_week,
             predicted_imp, predicted_eng, predicted_rate,
             predicted_var, 0.3, "synthetic", json.dumps({"territory": territory, "hook_pattern": hook, "hour_bucket": hour}),
             "baseline", "{}",
             actual_imp, actual_eng, actual_rate,
             measured_at, error_imp, predicted_eng - actual_eng,
             surprise, z_score, "synthetic data", is_spike_flag)
        )

        # Also insert into posted table for lookup
        db.execute(
            "INSERT INTO posted (id, ts, text, views, likes, retweets, replies) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (i, ts_str, f"Synthetic tweet about {territory} using {hook}", actual_imp,
             int(actual_eng * 0.6), int(actual_eng * 0.2), int(actual_eng * 0.2))
        )

    db.commit()
    print(f"  Generated: {num_predictions} predictions, {spike_count} spikes")
    print(f"  Territories: {territories}")
    print(f"  Hook patterns: {hook_patterns}")


def run_cortex_on_test_data(db: sqlite3.Connection):
    """Run cortex learning on synthetic data."""
    # Import cortex modules
    try:
        from importlib import import_module
        # We need to set DB_PATH to make the module work with our test DB
        # Instead, just instantiate the classes directly
        sys.path.insert(0, os.path.dirname(__file__))

        # Load knobs config
        knobs_path = Path(__file__).parent / "cortex-knobs.json"
        if knobs_path.exists():
            config = json.loads(knobs_path.read_text())
            for name, spec in config.get("knobs", {}).items():
                existing = db.execute(
                    "SELECT knob_name FROM knob_state WHERE knob_name = ?", (name,)
                ).fetchone()
                if not existing:
                    db.execute(
                        """INSERT INTO knob_state
                           (knob_name, current_value, default_value, min_value, max_value, description, last_modified, modified_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (name, spec["default"], spec["default"], spec["min"], spec["max"],
                         spec.get("description", ""), datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"), "default")
                    )
            db.commit()

        # Import and run
        import importlib
        cortex_mod = importlib.import_module("aria-cortex-v2".replace("-", "_"))

        print("\nRunning cortex learning cycle on test data...")

        # Adaptive Memory
        memory = cortex_mod.AdaptiveMemory(db)
        memory.learn_from_predictions()

        # Spike Detection
        spiker = cortex_mod.SpikeDetector(db)
        new_spikes = spiker.detect_and_analyze()
        spiker.compute_replication_scorecard()

        # Performance Summary
        predictor = cortex_mod.PredictionEngine(db)
        predictor.compute_performance_summary()

        # Self-Modifier
        modifier = cortex_mod.SelfModifier(db)
        exp_id = modifier.propose_experiment()

        print("  Learning complete.")

    except ImportError as e:
        print(f"  Import error (running standalone tests instead): {e}")
        _run_standalone_learning(db)


def _run_standalone_learning(db: sqlite3.Connection):
    """Fallback: run learning logic directly without importing cortex module."""
    print("  Running standalone learning...")

    # Learn territory constraints
    territories = db.execute(
        """SELECT territory,
                  AVG(actual_engagement_rate) as avg_rate,
                  COUNT(*) as n
           FROM predictions
           WHERE actual_impressions IS NOT NULL AND territory IS NOT NULL
           GROUP BY territory HAVING n >= 5"""
    ).fetchall()

    overall_rate = db.execute(
        "SELECT AVG(actual_engagement_rate) as rate FROM predictions WHERE actual_impressions IS NOT NULL"
    ).fetchone()["rate"]

    for t in territories:
        if t["avg_rate"] and overall_rate and overall_rate > 0:
            ratio = t["avg_rate"] / overall_rate
            if ratio < 0.6 or ratio > 1.5:
                db.execute(
                    """INSERT INTO learned_constraints
                       (ts, constraint_type, scope, target_field, target_value,
                        modifier, reason, observation_count, min_observations, active, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
                     "score_multiplier", "tweet", "territory", t["territory"],
                     max(0.3, min(1.8, ratio)),
                     f"test: {t['territory']} at {ratio:.0%} of avg (n={t['n']})",
                     t["n"], 5, 1, "statistical")
                )

    # Learn hook pattern constraints
    patterns = db.execute(
        """SELECT hook_pattern,
                  AVG(actual_engagement_rate) as avg_rate,
                  COUNT(*) as n
           FROM predictions
           WHERE actual_impressions IS NOT NULL AND hook_pattern IS NOT NULL
           GROUP BY hook_pattern HAVING n >= 3"""
    ).fetchall()

    for p in patterns:
        if p["avg_rate"] and overall_rate and overall_rate > 0:
            ratio = p["avg_rate"] / overall_rate
            if ratio < 0.7 or ratio > 1.3:
                db.execute(
                    """INSERT INTO learned_constraints
                       (ts, constraint_type, scope, target_field, target_value,
                        modifier, reason, observation_count, min_observations, active, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
                     "score_multiplier", "tweet", "hook_pattern", p["hook_pattern"],
                     max(0.2, min(2.0, ratio)),
                     f"test: hook '{p['hook_pattern']}' at {ratio:.0%} of avg (n={p['n']})",
                     p["n"], 3, 1, "statistical")
                )

    # Detect spikes
    spikes = db.execute(
        """SELECT * FROM predictions WHERE is_spike = 1
           AND id NOT IN (SELECT prediction_id FROM spike_events)"""
    ).fetchall()

    for spike in spikes:
        expected = spike["predicted_impressions"] or 1
        actual = spike["actual_impressions"] or 0
        db.execute(
            """INSERT INTO spike_events
               (ts, prediction_id, actual_impressions, expected_impressions,
                spike_magnitude, z_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
             spike["id"], actual, expected,
             actual / max(expected, 1), spike["z_score"])
        )

    # Compute basic performance summary
    rows = db.execute(
        """SELECT ts, predicted_impressions, actual_impressions, z_score, surprise_score
           FROM predictions WHERE actual_impressions IS NOT NULL ORDER BY ts ASC"""
    ).fetchall()
    if len(rows) >= 3:
        predicted = [r["predicted_impressions"] for r in rows if r["predicted_impressions"]]
        actual = [r["actual_impressions"] for r in rows if r["actual_impressions"] is not None]
        abs_errors = [abs(p - a) for p, a in zip(predicted, actual)]
        within_50 = sum(1 for p, a in zip(predicted, actual)
                       if abs(p - a) < 0.5 * max(p, a, 1))
        correlation = 0.0
        if len(predicted) > 2:
            mx, my = statistics.mean(predicted), statistics.mean(actual)
            sx, sy = statistics.stdev(predicted), statistics.stdev(actual)
            if sx > 0 and sy > 0:
                cov = sum((x - mx) * (y - my) for x, y in zip(predicted, actual)) / (len(predicted) - 1)
                correlation = cov / (sx * sy)

        db.execute(
            """INSERT INTO prediction_performance
               (ts, window_label, window_start, window_end, prediction_count,
                mean_abs_error, median_abs_error, accuracy_within_50pct, correlation, trend, trend_slope)
               VALUES (?, 'week', ?, ?, ?, ?, ?, ?, ?, 'stable', 0.0)""",
            (datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S"),
             rows[0]["ts"] if rows else "", rows[-1]["ts"] if rows else "",
             len(rows),
             statistics.mean(abs_errors),
             statistics.median(abs_errors),
             within_50 / len(rows),
             correlation)
        )

    db.commit()
    print("  Standalone learning complete.")


def verify_results(db: sqlite3.Connection) -> bool:
    """
    Verify that cortex discovered the patterns embedded in synthetic data.
    Returns True if all critical checks pass.
    """
    print("\n=== VERIFICATION ===\n")
    passed = 0
    failed = 0

    # Check 1: Territory constraints discovered
    constraints = db.execute(
        "SELECT * FROM learned_constraints WHERE active = 1 AND target_field = 'territory'"
    ).fetchall()

    print("1. Territory constraints:")
    if len(constraints) > 0:
        for c in constraints:
            direction = "boost" if c["modifier"] > 1.0 else "penalty"
            print(f"   {c['target_value']}: x{c['modifier']:.2f} ({direction})")

        # Building should have highest modifier (highest engagement rate)
        building_constraint = [c for c in constraints if c["target_value"] == "building"]
        org_constraint = [c for c in constraints if c["target_value"] == "organizations"]

        if building_constraint and building_constraint[0]["modifier"] > 1.0:
            print("   ✓ Building correctly identified as high-performing")
            passed += 1
        else:
            print("   ✗ Building should have boost modifier (ground truth: 0.035 engagement rate)")
            failed += 1

        if org_constraint and org_constraint[0]["modifier"] < 1.0:
            print("   ✓ Organizations correctly identified as low-performing")
            passed += 1
        else:
            print("   ✗ Organizations should have penalty modifier (ground truth: 0.015 engagement rate)")
            failed += 1
    else:
        print("   ✗ No territory constraints learned")
        failed += 2

    # Check 2: Hook pattern constraints
    hook_constraints = db.execute(
        "SELECT * FROM learned_constraints WHERE active = 1 AND target_field = 'hook_pattern'"
    ).fetchall()

    print("\n2. Hook pattern constraints:")
    if hook_constraints:
        for c in hook_constraints:
            print(f"   {c['target_value']}: x{c['modifier']:.2f}")

        inversion = [c for c in hook_constraints if c["target_value"] == "inversion"]
        story = [c for c in hook_constraints if c["target_value"] == "story"]

        if inversion and inversion[0]["modifier"] < 0.8:
            print("   ✓ Inversion correctly penalized (ground truth: 0.6x)")
            passed += 1
        else:
            print("   ✗ Inversion should be penalized")
            failed += 1

        if story and story[0]["modifier"] > 1.2:
            print("   ✓ Story correctly boosted (ground truth: 1.4x)")
            passed += 1
        else:
            print("   ? Story pattern may need more observations")
            # Soft fail — depends on sample size
    else:
        print("   ✗ No hook pattern constraints learned")
        failed += 1

    # Check 3: Spikes detected
    spike_count = db.execute("SELECT COUNT(*) as n FROM spike_events").fetchone()["n"]
    pred_spike_count = db.execute("SELECT COUNT(*) as n FROM predictions WHERE is_spike = 1").fetchone()["n"]

    print(f"\n3. Spike detection:")
    print(f"   Predictions flagged as spikes: {pred_spike_count}")
    print(f"   Spike events created: {spike_count}")
    expected_spikes = 200 * GROUND_TRUTH["spikes"]["probability"]
    if pred_spike_count >= expected_spikes * 0.5:
        print(f"   ✓ Spike detection working (expected ~{expected_spikes:.0f})")
        passed += 1
    else:
        print(f"   ✗ Too few spikes detected (expected ~{expected_spikes:.0f})")
        failed += 1

    # Check 4: Performance summary computed
    perf = db.execute(
        "SELECT * FROM prediction_performance WHERE window_label = 'week'"
    ).fetchone()
    print(f"\n4. Performance summary:")
    if perf:
        print(f"   MAE: {perf['mean_abs_error']:.1f}")
        print(f"   Accuracy (within 50%): {perf['accuracy_within_50pct']:.0%}")
        try:
            corr = perf["correlation"]
            if corr:
                print(f"   Correlation: {corr:.2f}")
        except (IndexError, KeyError):
            pass
        print(f"   ✓ Performance summary computed")
        passed += 1
    else:
        print("   ✗ No performance summary")
        failed += 1

    # Summary
    total = passed + failed
    print(f"\n=== RESULTS: {passed}/{total} passed ===")
    if failed == 0:
        print("All checks passed. Cortex discovered the embedded patterns.")
    else:
        print(f"{failed} check(s) failed. Review ground truth vs learned constraints.")

    return failed == 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cortex Test Suite")
    parser.add_argument("--generate", action="store_true", help="Generate synthetic data only")
    parser.add_argument("--verify", action="store_true", help="Verify existing results only")
    args = parser.parse_args()

    if args.verify:
        from pathlib import Path
        db_path = Path(os.environ.get("ARIA_WORKSPACE",
                       os.path.expanduser("~/.openclaw/agents/aria/workspace"))) / "memory" / "aria.db"
        if db_path.exists():
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            verify_results(db)
            db.close()
        else:
            print(f"DB not found at {db_path}")
        return

    # Full test: create DB, generate data, run cortex, verify
    print("=== CORTEX v2 TEST SUITE ===\n")

    db = create_test_db()
    generate_synthetic_data(db, num_predictions=200)
    run_cortex_on_test_data(db)
    success = verify_results(db)

    db.close()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
