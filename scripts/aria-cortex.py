#!/usr/bin/env python3
"""
ARIA Cortex v2 — The Intelligence Layer (Rebuilt)

Six systems:
  1. PredictionEngine  — regression-based, variance-aware, confounder-tracked
  2. SpikeDetector     — identifies outliers, triggers Claude analysis, tracks replication
  3. Mythos            — world model of narrative beliefs, identity guard, Claude-powered
  4. SelfModifier      — statistically rigorous experiments (Welch's t-test)
  5. AdaptiveMemory    — compound constraints, spike-derived, mythos-linked
  6. CognitiveContinuity — dynamic goals, strategy tracking, identity drift detection

Key changes from v1:
  - Predictions track confounding variables (Claude session state, follower count, parent tweet velocity)
  - Baselines use recency-weighted regression, not simple averages
  - Confidence is variance-aware, not just count-based
  - Experiments use Welch's t-test with effect size, not fixed thresholds
  - Spike detection + replication tracking = the real success metric
  - Mythos layer: Claude analyzes WHY things work, stores beliefs, guards identity
  - Constraints can be compound (territory + hour, not just territory)
  - Cognitive state has dynamic goals and strategy pivoting

Usage:
    python3 aria-cortex.py              # full cycle
    python3 aria-cortex.py --predict    # prediction + measurement only
    python3 aria-cortex.py --modify     # self-modification only
    python3 aria-cortex.py --mythos     # mythos update only (calls Claude)
    python3 aria-cortex.py --spikes     # spike analysis only
    python3 aria-cortex.py --status     # print current cortex state
    python3 aria-cortex.py --seed-mythos # seed initial world model beliefs
"""

import sqlite3
import json
import os
import sys
import math
import random
import logging
import argparse
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

# Optional: cortex_claude for spike analysis via Anthropic API
try:
    from cortex_claude import CortexClaude
except ImportError:
    CortexClaude = None

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

WORKSPACE = Path(os.environ.get(
    "ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")
))
DB_PATH = WORKSPACE / "memory" / "aria.db"
KNOBS_PATH = WORKSPACE / "cortex" / "cortex-knobs.json"
SCHEMA_PATH = WORKSPACE / "cortex" / "aria-cortex-schema-v2.sql"

IST = timezone(timedelta(hours=5, minutes=30))

# Claude session degradation thresholds
CLAUDE_SESSION_SOFT_LIMIT = 150_000   # tokens: quality starts to drift
CLAUDE_SESSION_HARD_LIMIT = 500_000   # tokens: quality materially degraded
CLAUDE_SESSION_CLIFF = 1_000_000      # tokens: significantly degraded

# Spike detection
SPIKE_THRESHOLD_ZSCORE = 2.0          # z-scores above mean to count as spike
SPIKE_THRESHOLD_MULTIPLE = 3.0        # or 3x expected impressions
MIN_PREDICTIONS_FOR_STATS = 10        # need this many before computing variance

# Statistical testing
MIN_SAMPLE_SIZE_EXPERIMENT = 15       # minimum n per arm for experiment conclusion
P_VALUE_THRESHOLD = 0.10              # significance level (relaxed for small samples)
MIN_EFFECT_SIZE = 0.3                 # Cohen's d minimum to call meaningful

LOG_FORMAT = "%(asctime)s [cortex/%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")  # 10s, up from 5s
    return db


def init_schema(db: sqlite3.Connection):
    check = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='world_model'"
    ).fetchone()
    if check:
        return
    if SCHEMA_PATH.exists():
        db.executescript(SCHEMA_PATH.read_text())
        log.info("Cortex v2 schema initialized")
    else:
        log.warning("Schema not found at %s", SCHEMA_PATH)


def init_knobs(db: sqlite3.Connection):
    if not KNOBS_PATH.exists():
        return
    config = json.loads(KNOBS_PATH.read_text())
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
                 spec.get("description", ""), now_iso(), "default")
            )
    db.commit()


def cortex_log_event(db: sqlite3.Connection, component: str, event_type: str,
                     message: str, data: dict = None):
    db.execute(
        "INSERT INTO cortex_log (ts, component, event_type, message, data_json) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), component, event_type, message, json.dumps(data) if data else None)
    )
    db.commit()


# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S")

def now_ist() -> datetime:
    return datetime.now(IST)

def parse_ts(ts_str: str) -> datetime:
    if ts_str and ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return datetime.now(IST)

def hours_ago(hours: float) -> str:
    return (now_ist() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")

def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b != 0 else default

def welch_t_test(mean1: float, std1: float, n1: int,
                 mean2: float, std2: float, n2: int) -> Tuple[float, float]:
    """
    Welch's t-test for unequal variances.
    Returns (t_statistic, approximate_p_value).
    Using approximation since we don't have scipy.
    """
    if n1 < 2 or n2 < 2:
        return 0.0, 1.0

    se1 = (std1 ** 2) / n1
    se2 = (std2 ** 2) / n2
    se_total = math.sqrt(se1 + se2)

    if se_total == 0:
        return 0.0, 1.0

    t_stat = (mean2 - mean1) / se_total

    # Welch-Satterthwaite degrees of freedom
    num = (se1 + se2) ** 2
    den = (se1 ** 2) / (n1 - 1) + (se2 ** 2) / (n2 - 1)
    df = num / den if den > 0 else 1

    # Approximate p-value using normal approximation (good for df > 30)
    # For smaller df, this underestimates p-value (conservative direction is fine)
    z = abs(t_stat)
    # Abramowitz and Stegun approximation for normal CDF
    p = 0.5 * math.erfc(z / math.sqrt(2)) * 2  # two-tailed
    return t_stat, p

def cohens_d(mean1: float, std1: float, n1: int,
             mean2: float, std2: float, n2: int) -> float:
    """Cohen's d effect size with pooled standard deviation."""
    pooled_std = math.sqrt(
        ((n1 - 1) * std1 ** 2 + (n2 - 1) * std2 ** 2) / (n1 + n2 - 2)
    ) if (n1 + n2 - 2) > 0 else 1.0
    return safe_div(mean2 - mean1, pooled_std)

def weighted_mean(values: List[float], half_life: int = 10) -> float:
    """
    Exponentially weighted mean. Most recent values count more.
    half_life = how many observations until weight halves.
    Values are ordered oldest-first.
    """
    if not values:
        return 0.0
    n = len(values)
    weights = [math.exp(-0.693 * (n - 1 - i) / max(half_life, 1)) for i in range(n)]
    total_weight = sum(weights)
    if total_weight == 0:
        return sum(values) / n
    return sum(v * w for v, w in zip(values, weights)) / total_weight

def weighted_std(values: List[float], half_life: int = 10) -> float:
    """Exponentially weighted standard deviation."""
    if len(values) < 2:
        return 0.0
    n = len(values)
    weights = [math.exp(-0.693 * (n - 1 - i) / max(half_life, 1)) for i in range(n)]
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0
    wmean = sum(v * w for v, w in zip(values, weights)) / total_weight
    wvar = sum(w * (v - wmean) ** 2 for v, w in zip(values, weights)) / total_weight
    return math.sqrt(max(wvar, 0))


# ---------------------------------------------------------------------------
# CONFOUNDER TRACKING
# ---------------------------------------------------------------------------

class ConfounderTracker:
    """
    Captures external variables that affect outcomes but aren't content features.
    Called at prediction time to snapshot the state of the world.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.log = logging.getLogger("confounders")

    def snapshot(self, prediction_id: int, context: dict = None) -> dict:
        """
        Take a confounder snapshot. `context` can be passed from brain.py/hands.py
        with live data (session tokens, follower count, etc.)
        Whatever isn't passed, we estimate from DB.
        """
        ctx = context or {}

        # Claude session quality proxies
        session_tokens = ctx.get("session_token_count", self._estimate_session_tokens())
        candidates_this_session = ctx.get("candidates_generated_this_session", 0)
        session_age = ctx.get("session_age_minutes", 0)
        generation_model = ctx.get("generation_model", "unknown")

        # Account state (from most recent confounder snapshot or passed in)
        follower_count = ctx.get("follower_count", self._last_follower_count())
        following_count = ctx.get("following_count", 0)

        # Posting context
        last_post_ts = self.db.execute(
            "SELECT MAX(ts) as last FROM predictions WHERE action_type = 'post_tweet'"
        ).fetchone()
        minutes_since_last = 0
        if last_post_ts and last_post_ts["last"]:
            delta = now_ist() - parse_ts(last_post_ts["last"])
            minutes_since_last = delta.total_seconds() / 60

        posts_24h = self.db.execute(
            "SELECT COUNT(*) as n FROM predictions WHERE action_type = 'post_tweet' AND ts > ?",
            (hours_ago(24),)
        ).fetchone()["n"]

        posts_7d = self.db.execute(
            "SELECT COUNT(*) as n FROM predictions WHERE action_type = 'post_tweet' AND ts > ?",
            (hours_ago(168),)
        ).fetchone()["n"]

        avg_eng_7d = self.db.execute(
            """SELECT AVG(actual_engagement_rate) as rate FROM predictions
               WHERE actual_engagement_rate IS NOT NULL AND ts > ?""",
            (hours_ago(168),)
        ).fetchone()["rate"] or 0

        # Reply context
        parent_age = ctx.get("parent_tweet_age_minutes")
        parent_impressions = ctx.get("parent_tweet_impressions")
        parent_velocity = ctx.get("parent_tweet_velocity")
        parent_followers = ctx.get("parent_author_follower_count")

        # External
        now = now_ist()
        is_weekend = 1 if now.weekday() >= 5 else 0
        trending_overlap = ctx.get("trending_topics_overlap", 0)

        snapshot = {
            "session_token_count": session_tokens,
            "candidates_generated_this_session": candidates_this_session,
            "session_age_minutes": session_age,
            "generation_model": generation_model,
            "follower_count": follower_count,
            "following_count": following_count,
            "minutes_since_last_post": minutes_since_last,
            "posts_last_24h": posts_24h,
            "posts_last_7d": posts_7d,
            "avg_engagement_last_7d": avg_eng_7d,
            "parent_tweet_age_minutes": parent_age,
            "parent_tweet_impressions": parent_impressions,
            "parent_tweet_velocity": parent_velocity,
            "parent_author_follower_count": parent_followers,
            "is_weekend": is_weekend,
            "trending_topics_overlap": trending_overlap,
        }

        # Store
        self.db.execute(
            """INSERT INTO confounder_snapshots
               (ts, prediction_id, session_token_count, candidates_generated_this_session,
                session_age_minutes, generation_model, follower_count, following_count,
                account_age_days, minutes_since_last_post, posts_last_24h, posts_last_7d,
                avg_engagement_last_7d, parent_tweet_age_minutes, parent_tweet_impressions,
                parent_tweet_velocity, parent_author_follower_count,
                is_weekend, is_indian_holiday, trending_topics_overlap)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), prediction_id, session_tokens, candidates_this_session,
             session_age, generation_model, follower_count, following_count,
             0,  # account_age_days — fill from context
             minutes_since_last, posts_24h, posts_7d, avg_eng_7d,
             parent_age, parent_impressions, parent_velocity, parent_followers,
             is_weekend, 0, trending_overlap)
        )
        self.db.commit()
        return snapshot

    def claude_quality_penalty(self, context: dict = None) -> float:
        """
        Returns a multiplier (0.0 to 1.0) representing Claude's expected generation quality
        based on session state. 1.0 = fresh session, 0.5 = significantly degraded.
        """
        ctx = context or {}
        tokens = ctx.get("session_token_count", 0)

        if tokens < CLAUDE_SESSION_SOFT_LIMIT:
            return 1.0
        elif tokens < CLAUDE_SESSION_HARD_LIMIT:
            # Linear degradation from 1.0 to 0.85
            progress = (tokens - CLAUDE_SESSION_SOFT_LIMIT) / (CLAUDE_SESSION_HARD_LIMIT - CLAUDE_SESSION_SOFT_LIMIT)
            return 1.0 - (0.15 * progress)
        elif tokens < CLAUDE_SESSION_CLIFF:
            # Steeper degradation from 0.85 to 0.6
            progress = (tokens - CLAUDE_SESSION_HARD_LIMIT) / (CLAUDE_SESSION_CLIFF - CLAUDE_SESSION_HARD_LIMIT)
            return 0.85 - (0.25 * progress)
        else:
            return 0.5  # heavily degraded

    def _estimate_session_tokens(self) -> int:
        """Estimate current session tokens from recent activity."""
        # If brain.py doesn't pass this, we estimate from predictions in last 2 hours
        recent = self.db.execute(
            "SELECT COUNT(*) as n FROM predictions WHERE ts > ?",
            (hours_ago(2),)
        ).fetchone()["n"]
        # Rough estimate: each prediction cycle uses ~5K tokens
        return recent * 5000

    def _last_follower_count(self) -> int:
        row = self.db.execute(
            "SELECT follower_count FROM confounder_snapshots WHERE follower_count > 0 ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return row["follower_count"] if row else 0


# ---------------------------------------------------------------------------
# 1. PREDICTION ENGINE (v2)
# ---------------------------------------------------------------------------

class PredictionEngine:
    """
    v2: Recency-weighted regression with variance tracking.
    Predictions include expected variance (uncertainty).
    Z-scores replace raw surprise scores.
    Confounders are factored in.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.log = logging.getLogger("predictor")
        self.confounders = ConfounderTracker(db)

    def predict_tweet(self, tweet_row: dict, context: dict = None) -> int:
        """
        Generate a prediction with confounder tracking and variance estimate.
        `context` should include session_token_count, follower_count, etc.
        """
        features = self._extract_tweet_features(tweet_row)
        baseline = self._get_weighted_baseline("post_tweet", features)

        predicted_imp = baseline["impressions"]
        predicted_eng = baseline["engagements"]
        predicted_rate = baseline["engagement_rate"]
        predicted_var = baseline["variance"]

        # Apply Claude quality penalty
        quality_mult = self.confounders.claude_quality_penalty(context)
        if quality_mult < 1.0:
            predicted_imp *= quality_mult
            predicted_eng *= quality_mult
            self.log.info("Claude quality penalty: x%.2f (session tokens: %s)",
                         quality_mult, (context or {}).get("session_token_count", "unknown"))

        # Apply follower growth adjustment
        follower_mult = self._follower_growth_adjustment(context)
        predicted_imp *= follower_mult

        # Apply learned constraints
        modifiers = self._get_active_modifiers("tweet", features)
        for m in modifiers:
            predicted_imp *= m["modifier"]
            predicted_eng *= m["modifier"]

        # Apply content spacing penalty
        spacing_mult = self._content_spacing_penalty(features.get("territory", ""))
        predicted_imp *= spacing_mult

        confidence = self._calculate_confidence("post_tweet", features, baseline)

        method = "regression" if baseline["sample_size"] >= MIN_PREDICTIONS_FOR_STATS else "baseline"

        reasoning_parts = [
            f"weighted baseline from {baseline['sample_size']} tweets (method: {method})"
        ]
        if quality_mult < 1.0:
            reasoning_parts.append(f"claude quality penalty x{quality_mult:.2f}")
        if follower_mult != 1.0:
            reasoning_parts.append(f"follower growth adj x{follower_mult:.2f}")
        if spacing_mult < 1.0:
            reasoning_parts.append(f"content spacing penalty x{spacing_mult:.2f}")
        for m in modifiers:
            reasoning_parts.append(f"{m['reason']} (x{m['modifier']:.2f})")

        confounders_dict = {
            "session_token_count": (context or {}).get("session_token_count", 0),
            "follower_count": (context or {}).get("follower_count", 0),
            "quality_multiplier": quality_mult,
            "follower_multiplier": follower_mult,
            "spacing_multiplier": spacing_mult,
        }

        cur = self.db.execute(
            """INSERT INTO predictions
               (ts, action_type, action_ref, territory, hook_pattern, hour_bucket, day_of_week,
                predicted_impressions, predicted_engagements, predicted_engagement_rate,
                predicted_variance, confidence, reasoning, features_json,
                prediction_method, confounders_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), "post_tweet", str(tweet_row.get("id", "")),
             features.get("territory", "unknown"),
             features.get("hook_pattern", "unknown"),
             features.get("hour_bucket", now_ist().hour),
             features.get("day_of_week", now_ist().weekday()),
             predicted_imp, predicted_eng, predicted_rate,
             predicted_var, confidence, "; ".join(reasoning_parts),
             json.dumps(features), method, json.dumps(confounders_dict))
        )
        self.db.commit()
        pred_id = cur.lastrowid

        # Snapshot confounders
        self.confounders.snapshot(pred_id, context)

        self.log.info(
            "Predicted tweet %s: ~%.0f imp (±%.0f), ~%.1f eng (conf %.2f, method %s)",
            tweet_row.get("id", "?"), predicted_imp, math.sqrt(max(predicted_var, 0)),
            predicted_eng, confidence, method
        )
        cortex_log_event(self.db, "predictor", "prediction_made",
                         f"Tweet {tweet_row.get('id')}: {predicted_imp:.0f}±{math.sqrt(max(predicted_var, 0)):.0f} imp",
                         {"prediction_id": pred_id, "method": method})

        return pred_id

    def predict_reply(self, reply_row: dict, context: dict = None) -> int:
        """Generate prediction for a reply with confounder tracking."""
        features = self._extract_reply_features(reply_row)
        baseline = self._get_weighted_baseline("post_reply", features)

        predicted_eng = baseline["engagements"]
        predicted_var = baseline["variance"]

        # Claude quality
        quality_mult = self.confounders.claude_quality_penalty(context)
        predicted_eng *= quality_mult

        # Parent tweet velocity is the dominant factor for reply visibility
        parent_velocity = (context or {}).get("parent_tweet_velocity", 0)
        parent_age = (context or {}).get("parent_tweet_age_minutes", 999)
        if parent_velocity > 0 and parent_age < 60:
            # Early reply on a fast-moving tweet: big boost
            velocity_mult = min(3.0, 1.0 + math.log1p(parent_velocity) * 0.3)
            predicted_eng *= velocity_mult
        elif parent_age > 360:
            # Late reply: diminished returns
            predicted_eng *= 0.4

        modifiers = self._get_active_modifiers("reply", features)
        for m in modifiers:
            predicted_eng *= m["modifier"]

        confidence = self._calculate_confidence("post_reply", features, baseline)

        confounders_dict = {
            "parent_velocity": parent_velocity,
            "parent_age_minutes": parent_age,
            "quality_multiplier": quality_mult,
        }

        cur = self.db.execute(
            """INSERT INTO predictions
               (ts, action_type, action_ref, target_handle, territory, hook_pattern,
                hour_bucket, day_of_week,
                predicted_impressions, predicted_engagements, predicted_engagement_rate,
                predicted_variance, confidence, reasoning, features_json,
                prediction_method, confounders_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), "post_reply", str(reply_row.get("id", "")),
             features.get("target_handle", ""),
             features.get("territory", ""),
             features.get("hook_pattern", ""),
             now_ist().hour, now_ist().weekday(),
             baseline["impressions"], predicted_eng, baseline["engagement_rate"],
             predicted_var, confidence,
             f"weighted baseline from {baseline['sample_size']} replies",
             json.dumps(features), "baseline", json.dumps(confounders_dict))
        )
        self.db.commit()
        pred_id = cur.lastrowid
        self.confounders.snapshot(pred_id, context)
        return pred_id

    def measure_outcomes(self) -> int:
        """Measure predictions with z-score computation and spike detection."""
        unmeasured = self.db.execute(
            """SELECT p.* FROM predictions p
               WHERE p.actual_impressions IS NULL AND p.ts < ?""",
            (hours_ago(6),)
        ).fetchall()

        measured_count = 0
        for pred in unmeasured:
            actual = self._lookup_actual(pred)
            if actual is None:
                continue

            actual_imp = actual.get("impressions", 0)
            actual_eng = actual.get("engagements", 0)
            actual_rate = safe_div(actual_eng, max(actual_imp, 1))

            pred_imp = pred["predicted_impressions"] or 1
            pred_eng = pred["predicted_engagements"] or 0.1
            pred_var = pred["predicted_variance"] or (pred_imp * 0.5) ** 2  # fallback variance

            error_imp = pred_imp - actual_imp
            error_eng = pred_eng - actual_eng

            # Z-score: how many stddevs away from prediction, accounting for expected variance
            pred_std = math.sqrt(max(pred_var, 1))
            z_score = safe_div(actual_imp - pred_imp, pred_std)

            # Surprise: normalized but using log ratio to handle extremes better
            if pred_imp > 0 and actual_imp > 0:
                surprise = abs(math.log(actual_imp / pred_imp))
            else:
                surprise = abs(error_imp) / max(pred_imp, actual_imp, 1)

            # Is this a spike?
            is_spike = 1 if (z_score > SPIKE_THRESHOLD_ZSCORE or
                            actual_imp > pred_imp * SPIKE_THRESHOLD_MULTIPLE) else 0

            lesson = self._generate_lesson(pred, actual, z_score, surprise)

            self.db.execute(
                """UPDATE predictions SET
                   actual_impressions = ?, actual_engagements = ?, actual_engagement_rate = ?,
                   measured_at = ?, error_impressions = ?, error_engagements = ?,
                   surprise_score = ?, z_score = ?, lesson = ?, is_spike = ?
                   WHERE id = ?""",
                (actual_imp, actual_eng, actual_rate, now_iso(),
                 error_imp, error_eng, surprise, z_score, lesson, is_spike, pred["id"])
            )
            measured_count += 1

            if abs(z_score) > 2.0:
                self.log.info(
                    "HIGH Z-SCORE (%.2f): predicted %.0f±%.0f, got %.0f. %s",
                    z_score, pred_imp, pred_std, actual_imp, lesson
                )

            if is_spike:
                self.log.info("SPIKE DETECTED: prediction #%d, %.1fx expected", pred["id"],
                             safe_div(actual_imp, pred_imp, 1))

        self.db.commit()
        if measured_count:
            self.log.info("Measured %d predictions", measured_count)
        return measured_count

    def compute_performance_summary(self):
        """Roll up with proper statistics: correlation, regression trend."""
        for window_label, hours_back in [("day", 24), ("week", 168), ("all_time", 8760)]:
            rows = self.db.execute(
                """SELECT predicted_impressions, actual_impressions, z_score, surprise_score
                   FROM predictions
                   WHERE actual_impressions IS NOT NULL AND ts > ?
                   ORDER BY ts ASC""",
                (hours_ago(hours_back),)
            ).fetchall()

            if len(rows) < 3:
                continue

            predicted = [r["predicted_impressions"] for r in rows if r["predicted_impressions"]]
            actual = [r["actual_impressions"] for r in rows if r["actual_impressions"] is not None]
            z_scores = [r["z_score"] for r in rows if r["z_score"] is not None]
            surprises = [r["surprise_score"] for r in rows if r["surprise_score"] is not None]

            if not predicted or not actual or len(predicted) != len(actual):
                continue

            abs_errors = [abs(p - a) for p, a in zip(predicted, actual)]
            within_50 = sum(1 for p, a in zip(predicted, actual)
                          if abs(p - a) < 0.5 * max(p, a, 1))

            mean_abs = statistics.mean(abs_errors) if abs_errors else 0
            median_abs = statistics.median(abs_errors) if abs_errors else 0
            mean_surprise = statistics.mean(surprises) if surprises else 0
            mean_z = statistics.mean([abs(z) for z in z_scores]) if z_scores else 0
            accuracy_50 = safe_div(within_50, len(rows))

            pred_std = statistics.stdev(predicted) if len(predicted) > 1 else 0
            actual_std = statistics.stdev(actual) if len(actual) > 1 else 0

            # Pearson correlation between predicted and actual
            correlation = self._pearson_r(predicted, actual)

            # Trend: linear regression of absolute errors over index
            trend, slope = self._compute_trend(abs_errors)

            self.db.execute(
                """INSERT INTO prediction_performance
                   (ts, window_label, window_start, window_end, action_type,
                    prediction_count, mean_abs_error, median_abs_error, mean_surprise,
                    accuracy_within_50pct, mean_z_score,
                    prediction_stddev, actual_stddev, correlation,
                    trend, trend_slope, trend_method)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), window_label, hours_ago(hours_back), now_iso(), None,
                 len(rows), mean_abs, median_abs, mean_surprise, accuracy_50, mean_z,
                 pred_std, actual_std, correlation,
                 trend, slope, "linear_regression")
            )

        self.db.commit()
        self.log.info("Performance summary updated")

    # --- internal helpers ---

    def _extract_tweet_features(self, row: dict) -> dict:
        scores = {}
        if row.get("scores_json"):
            try:
                scores = json.loads(row["scores_json"]) if isinstance(row["scores_json"], str) else row["scores_json"]
            except (json.JSONDecodeError, TypeError):
                pass

        text = row.get("text", "")
        return {
            "territory": row.get("territory", "unknown"),
            "hook_pattern": scores.get("hook_pattern", "unknown"),
            "has_image": bool(row.get("image_type")),
            "has_card": bool(row.get("card_text")),
            "char_count": len(text),
            "word_count": len(text.split()),
            "hour_bucket": now_ist().hour,
            "day_of_week": now_ist().weekday(),
            "quality_score": scores.get("overall", scores.get("total", 5)),
        }

    def _extract_reply_features(self, row: dict) -> dict:
        return {
            "target_handle": row.get("target_handle", ""),
            "territory": row.get("territory", ""),
            "hook_pattern": row.get("hook_pattern", ""),
            "char_count": len(row.get("reply_text", "")),
            "hour_bucket": now_ist().hour,
            "day_of_week": now_ist().weekday(),
        }

    def _get_weighted_baseline(self, action_type: str, features: dict) -> dict:
        """
        Recency-weighted baseline with variance estimation.
        Uses exponential decay: recent tweets count more than old ones.
        """
        territory = features.get("territory", "unknown")

        # Territory-specific first
        rows = self.db.execute(
            """SELECT actual_impressions, actual_engagements, actual_engagement_rate
               FROM predictions
               WHERE action_type = ? AND territory = ? AND actual_impressions IS NOT NULL
               ORDER BY ts ASC LIMIT 30""",
            (action_type, territory)
        ).fetchall()

        if len(rows) >= 5:
            return self._compute_weighted_stats(rows)

        # Fall back to all data for this action type
        rows = self.db.execute(
            """SELECT actual_impressions, actual_engagements, actual_engagement_rate
               FROM predictions
               WHERE action_type = ? AND actual_impressions IS NOT NULL
               ORDER BY ts ASC LIMIT 50""",
            (action_type,)
        ).fetchall()

        if rows:
            return self._compute_weighted_stats(rows)

        # Cold start priors
        if action_type == "post_tweet":
            return {"impressions": 100, "engagements": 2, "engagement_rate": 0.02,
                    "variance": 2500, "sample_size": 0, "std": 50}
        else:
            return {"impressions": 50, "engagements": 1, "engagement_rate": 0.02,
                    "variance": 625, "sample_size": 0, "std": 25}

    def _compute_weighted_stats(self, rows) -> dict:
        """Compute recency-weighted stats from prediction history."""
        impressions = [r["actual_impressions"] or 0 for r in rows]
        engagements = [r["actual_engagements"] or 0 for r in rows]
        rates = [r["actual_engagement_rate"] or 0 for r in rows]

        imp_mean = weighted_mean(impressions)
        imp_std = weighted_std(impressions) if len(impressions) >= 2 else imp_mean * 0.5
        eng_mean = weighted_mean(engagements)
        rate_mean = weighted_mean(rates)

        return {
            "impressions": imp_mean,
            "engagements": eng_mean,
            "engagement_rate": rate_mean,
            "variance": imp_std ** 2,
            "std": imp_std,
            "sample_size": len(rows),
        }

    def _follower_growth_adjustment(self, context: dict = None) -> float:
        """
        Adjust predictions for follower growth.
        If follower count has grown since the baseline period, scale up.
        """
        current_followers = (context or {}).get("follower_count", 0)
        if current_followers == 0:
            return 1.0

        # Get follower count from ~7 days ago
        old_snapshot = self.db.execute(
            """SELECT follower_count FROM confounder_snapshots
               WHERE follower_count > 0 AND ts < ?
               ORDER BY ts DESC LIMIT 1""",
            (hours_ago(168),)
        ).fetchone()

        if not old_snapshot or old_snapshot["follower_count"] == 0:
            return 1.0

        growth = current_followers / old_snapshot["follower_count"]
        # Cap the adjustment at 1.5x, and don't adjust for small changes
        if growth > 1.05:
            return min(1.5, growth)
        elif growth < 0.95:
            return max(0.7, growth)
        return 1.0

    def _content_spacing_penalty(self, territory: str) -> float:
        """
        Penalize if we've recently posted in the same territory.
        Audience fatigue is real.
        """
        if not territory or territory == "unknown":
            return 1.0

        recent_same = self.db.execute(
            """SELECT COUNT(*) as n FROM predictions
               WHERE action_type = 'post_tweet' AND territory = ? AND ts > ?""",
            (territory, hours_ago(12))
        ).fetchone()["n"]

        if recent_same == 0:
            return 1.0
        elif recent_same == 1:
            return 0.85
        elif recent_same == 2:
            return 0.7
        else:
            return 0.5  # 3+ same territory tweets in 12 hours = heavy penalty

    def _get_active_modifiers(self, scope: str, features: dict) -> list:
        """Get learned constraints, including compound constraints."""
        constraints = self.db.execute(
            "SELECT * FROM learned_constraints WHERE active = 1 AND scope = ?",
            (scope,)
        ).fetchall()

        applicable = []
        for c in constraints:
            field_value = str(features.get(c["target_field"], ""))
            if field_value != str(c["target_value"]):
                continue

            # Check compound constraint
            if c["is_compound"] and c["target_field_2"]:
                field_value_2 = str(features.get(c["target_field_2"], ""))
                if field_value_2 != str(c["target_value_2"]):
                    continue

            applicable.append({
                "modifier": c["modifier"],
                "reason": c["reason"],
                "constraint_id": c["id"]
            })
        return applicable

    def _calculate_confidence(self, action_type: str, features: dict, baseline: dict) -> float:
        """
        Confidence based on sample size AND variance.
        High count + low variance = high confidence.
        High count + high variance = medium confidence.
        Low count = low confidence regardless.
        """
        n = baseline["sample_size"]
        if n == 0:
            return 0.1

        # Count component: sigmoid
        count_conf = 0.1 + 0.6 * (1 / (1 + math.exp(-0.15 * (n - 10))))

        # Variance component: coefficient of variation
        mean_imp = baseline["impressions"]
        std_imp = baseline.get("std", mean_imp * 0.5)
        cv = safe_div(std_imp, mean_imp, 1.0)

        # Low CV (< 0.3) = high variance confidence, high CV (> 1.0) = low
        variance_conf = max(0.2, min(1.0, 1.0 - cv * 0.6))

        return min(0.95, count_conf * variance_conf)

    def _lookup_actual(self, pred) -> Optional[dict]:
        if pred["action_type"] == "post_tweet":
            # Real schema: posted has id/text/territory but NO metrics columns.
            # Metrics live in the separate `metrics` table keyed by post_id.
            row = self.db.execute(
                """SELECT m.impressions, m.likes, m.retweets, m.replies, m.bookmarks
                   FROM metrics m
                   WHERE m.post_id = ?
                   ORDER BY m.scraped_at DESC LIMIT 1""",
                (pred["action_ref"],)
            ).fetchone()
            if not row:
                # Fallback: match by text through queue -> posted -> metrics
                row = self.db.execute(
                    """SELECT m.impressions, m.likes, m.retweets, m.replies, m.bookmarks
                       FROM metrics m
                       JOIN posted p ON m.post_id = p.id
                       WHERE p.text = (SELECT text FROM queue WHERE id = ?)
                       ORDER BY m.scraped_at DESC LIMIT 1""",
                    (pred["action_ref"],)
                ).fetchone()
            if row:
                return {
                    "impressions": row["impressions"] or 0,
                    "engagements": (row["likes"] or 0) + (row["retweets"] or 0) + (row["replies"] or 0),
                }
        elif pred["action_type"] == "post_reply":
            # Reply metrics: check metrics table for reply post_id
            row = self.db.execute(
                """SELECT m.impressions, m.likes, m.retweets, m.replies
                   FROM metrics m
                   WHERE m.post_id = ?
                   ORDER BY m.scraped_at DESC LIMIT 1""",
                (pred["action_ref"],)
            ).fetchone()
            if row:
                return {
                    "impressions": row["impressions"] or 0,
                    "engagements": (row["likes"] or 0) + (row["retweets"] or 0) + (row["replies"] or 0),
                }
        return None

    def _generate_lesson(self, pred, actual: dict, z_score: float, surprise: float) -> str:
        if abs(z_score) < 1.0:
            return "within expected range, no signal"

        actual_imp = actual.get("impressions", 0)
        pred_imp = pred["predicted_impressions"] or 1
        direction = "over" if pred_imp > actual_imp else "under"
        territory = pred["territory"] or "unknown"
        hour = pred["hour_bucket"]

        parts = [f"{direction}predicted {territory} by {abs(z_score):.1f}σ ({abs(pred_imp - actual_imp):.0f} imp)"]

        # Check confounders for explanations
        try:
            conf = json.loads(pred["confounders_json"]) if pred["confounders_json"] else {}
            if conf.get("quality_multiplier", 1.0) < 0.85:
                parts.append(f"Claude quality was degraded (x{conf['quality_multiplier']:.2f})")
            if conf.get("spacing_multiplier", 1.0) < 0.85:
                parts.append("content spacing penalty active")
        except (json.JSONDecodeError, TypeError):
            pass

        if hour and (hour < 9 or hour > 22):
            parts.append(f"off-peak hour {hour}")

        return "; ".join(parts)

    def _pearson_r(self, x: List[float], y: List[float]) -> float:
        """Compute Pearson correlation coefficient."""
        n = min(len(x), len(y))
        if n < 3:
            return 0.0
        x, y = x[:n], y[:n]
        mx, my = statistics.mean(x), statistics.mean(y)
        sx, sy = statistics.stdev(x), statistics.stdev(y)
        if sx == 0 or sy == 0:
            return 0.0
        cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / (n - 1)
        return cov / (sx * sy)

    def _compute_trend(self, errors: List[float]) -> Tuple[str, float]:
        """Linear regression of errors over index to detect improvement/degradation."""
        n = len(errors)
        if n < 6:
            return "insufficient_data", 0.0

        # Simple linear regression: y = a + b*x
        x_mean = (n - 1) / 2
        y_mean = statistics.mean(errors)

        num = sum((i - x_mean) * (e - y_mean) for i, e in enumerate(errors))
        den = sum((i - x_mean) ** 2 for i in range(n))

        slope = safe_div(num, den)
        # Normalize slope by mean
        rel_slope = safe_div(slope, y_mean)

        if rel_slope < -0.02:
            return "improving", slope
        elif rel_slope > 0.02:
            return "degrading", slope
        return "stable", slope


# ---------------------------------------------------------------------------
# 2. SPIKE DETECTOR
# ---------------------------------------------------------------------------

class SpikeDetector:
    """
    Identifies outlier successes and analyzes what made them work.
    The spike replication rate is the ultimate success metric.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.log = logging.getLogger("spikes")

    def detect_and_analyze(self) -> int:
        """Find new spikes and create spike_events entries."""
        new_spikes = self.db.execute(
            """SELECT * FROM predictions
               WHERE is_spike = 1
               AND id NOT IN (SELECT prediction_id FROM spike_events)
               ORDER BY actual_impressions DESC"""
        ).fetchall()

        created = 0
        for spike in new_spikes:
            expected = spike["predicted_impressions"] or 1
            actual = spike["actual_impressions"] or 0
            magnitude = safe_div(actual, expected, 1)
            z = spike["z_score"] or 0

            # Build analysis context
            analysis_prompt = self._build_analysis_prompt(spike)

            self.db.execute(
                """INSERT INTO spike_events
                   (ts, prediction_id, actual_impressions, expected_impressions,
                    spike_magnitude, z_score, analysis_prompt)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), spike["id"], actual, expected, magnitude, z, analysis_prompt)
            )
            created += 1
            self.log.info("Spike event created: prediction #%d, %.1fx expected, z=%.1f",
                         spike["id"], magnitude, z)

        self.db.commit()
        return created

    def compute_replication_scorecard(self):
        """
        THE key metric: after a spike, do the next N tweets perform better than baseline?
        """
        spikes = self.db.execute(
            "SELECT * FROM spike_events ORDER BY ts DESC LIMIT 20"
        ).fetchall()

        if not spikes:
            return

        total_spikes = len(spikes)
        lifts = []

        for spike in spikes:
            spike_ts = spike["ts"]

            # Get 10 tweets after this spike
            after_spike = self.db.execute(
                """SELECT actual_impressions FROM predictions
                   WHERE action_type = 'post_tweet' AND actual_impressions IS NOT NULL
                   AND ts > ? ORDER BY ts ASC LIMIT 10""",
                (spike_ts,)
            ).fetchall()

            # Get baseline: 10 tweets before the spike
            before_spike = self.db.execute(
                """SELECT actual_impressions FROM predictions
                   WHERE action_type = 'post_tweet' AND actual_impressions IS NOT NULL
                   AND ts < ? ORDER BY ts DESC LIMIT 10""",
                (spike_ts,)
            ).fetchall()

            if len(after_spike) >= 5 and len(before_spike) >= 5:
                avg_after = statistics.mean([r["actual_impressions"] for r in after_spike])
                avg_before = statistics.mean([r["actual_impressions"] for r in before_spike])
                lift = safe_div(avg_after, avg_before, 1.0)
                lifts.append(lift)

        if lifts:
            avg_lift = statistics.mean(lifts)
            replications_succeeded = sum(1 for l in lifts if l > 1.2)  # >20% lift = success

            self.db.execute(
                """INSERT INTO spike_replication_scorecard
                   (ts, window_label, spikes_detected, spikes_analyzed,
                    replications_attempted, replications_succeeded, replication_rate,
                    avg_performance_10_after_spike, avg_performance_baseline, post_spike_lift)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), "rolling", total_spikes, len(lifts),
                 len(lifts), replications_succeeded,
                 safe_div(replications_succeeded, len(lifts)),
                 statistics.mean([l for l in lifts]) if lifts else 0,
                 1.0, avg_lift)
            )
            self.db.commit()

            self.log.info(
                "Spike replication scorecard: %d/%d spikes analyzed, %.0f%% replication rate, avg lift %.2fx",
                len(lifts), total_spikes,
                safe_div(replications_succeeded, len(lifts)) * 100, avg_lift
            )

    def _build_analysis_prompt(self, spike) -> str:
        """Build the prompt we'd send Claude to analyze a spike."""
        # Get the original tweet text
        tweet_text = ""
        if spike["action_ref"]:
            row = self.db.execute(
                "SELECT text FROM queue WHERE id = ?", (spike["action_ref"],)
            ).fetchone()
            if row:
                tweet_text = row["text"]

        # Get nearby tweets for comparison
        before = self.db.execute(
            """SELECT text, territory, actual_impressions, actual_engagement_rate
               FROM predictions p
               LEFT JOIN queue q ON p.action_ref = CAST(q.id AS TEXT)
               WHERE p.action_type = 'post_tweet' AND p.actual_impressions IS NOT NULL
               AND p.ts < ? ORDER BY p.ts DESC LIMIT 3""",
            (spike["ts"],)
        ).fetchall()

        after = self.db.execute(
            """SELECT text, territory, actual_impressions, actual_engagement_rate
               FROM predictions p
               LEFT JOIN queue q ON p.action_ref = CAST(q.id AS TEXT)
               WHERE p.action_type = 'post_tweet' AND p.actual_impressions IS NOT NULL
               AND p.ts > ? ORDER BY p.ts ASC LIMIT 3""",
            (spike["ts"],)
        ).fetchall()

        confounders = ""
        try:
            conf = json.loads(spike["confounders_json"]) if spike["confounders_json"] else {}
            confounders = json.dumps(conf, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass

        prompt = f"""Analyze this tweet that performed {safe_div(spike['actual_impressions'], spike['predicted_impressions'], 1):.1f}x better than expected.

THE SPIKE:
- Text: {tweet_text}
- Territory: {spike['territory']}
- Hour: {spike['hour_bucket']} IST
- Expected: {spike['predicted_impressions']:.0f} impressions
- Actual: {spike['actual_impressions']:.0f} impressions
- Confounders: {confounders}

NEARBY TWEETS (for comparison):
Before: {json.dumps([dict(r) for r in before], indent=2) if before else 'none'}
After: {json.dumps([dict(r) for r in after], indent=2) if after else 'none'}

Analyze:
1. What specific factors made this tweet outperform? (be concrete, not generic)
2. Which of these factors are REPLICABLE (we can deliberately reproduce)?
3. Which were CONTEXTUAL/LUCKY (timing, trending topics, algorithm mood)?
4. What is the underlying BELIEF about the audience that this spike reveals?

Output as JSON:
{{
  "identified_factors": ["factor1", "factor2", ...],
  "replicable_factors": ["factor1", ...],
  "non_replicable_factors": ["factor1", ...],
  "audience_belief": "one sentence about what this reveals about the audience",
  "content_belief": "one sentence about what content pattern this validates"
}}"""
        return prompt

    def get_unanalyzed_spikes(self) -> list:
        """Get spikes that need Claude analysis."""
        return self.db.execute(
            """SELECT * FROM spike_events
               WHERE analysis_response IS NULL
               ORDER BY spike_magnitude DESC LIMIT 5"""
        ).fetchall()

    def store_analysis(self, spike_id: int, analysis: dict):
        """Store Claude's spike analysis."""
        self.db.execute(
            """UPDATE spike_events SET
               analysis_response = ?,
               identified_factors = ?,
               replicable_factors = ?,
               non_replicable_factors = ?
               WHERE id = ?""",
            (json.dumps(analysis),
             json.dumps(analysis.get("identified_factors", [])),
             json.dumps(analysis.get("replicable_factors", [])),
             json.dumps(analysis.get("non_replicable_factors", [])),
             spike_id)
        )
        self.db.commit()


# ---------------------------------------------------------------------------
# 3. MYTHOS — World Model
# ---------------------------------------------------------------------------

class Mythos:
    """
    The narrative intelligence layer.
    Maintains beliefs about HOW and WHY things work, not just WHAT works.
    Beliefs guide prediction, constrain optimization, and guard identity.

    Types of beliefs:
    - audience_model: "My audience are practitioners who see through hype"
    - platform_model: "Twitter rewards early replies on trending threads"
    - content_model: "Contrarian takes with concrete examples outperform abstractions"
    - identity_model: "I am building credibility as a builder-PM, not an engagement farmer"
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.log = logging.getLogger("mythos")

    def seed_beliefs(self, beliefs: List[dict]):
        """
        Seed initial world model beliefs. Call once at setup.
        Each belief: {type, belief, confidence, serves_identity, identity_note}
        """
        for b in beliefs:
            existing = self.db.execute(
                "SELECT id FROM world_model WHERE belief = ? AND status = 'active'",
                (b["belief"],)
            ).fetchone()
            if existing:
                continue

            self.db.execute(
                """INSERT INTO world_model
                   (ts, belief_type, belief, belief_confidence, source,
                    serves_identity, identity_note, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), b["type"], b["belief"],
                 b.get("confidence", 0.5), "seed",
                 b.get("serves_identity", 1),
                 b.get("identity_note", ""), "active")
            )
        self.db.commit()
        self.log.info("Seeded %d beliefs", len(beliefs))

    def get_active_beliefs(self, belief_type: str = None) -> list:
        """Get active beliefs, optionally filtered by type."""
        if belief_type:
            rows = self.db.execute(
                "SELECT * FROM world_model WHERE status = 'active' AND belief_type = ? ORDER BY belief_confidence DESC",
                (belief_type,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM world_model WHERE status = 'active' ORDER BY belief_confidence DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_belief_evidence(self, belief_id: int, prediction_id: int, supports: bool):
        """Update a belief's evidence based on a prediction outcome."""
        belief = self.db.execute("SELECT * FROM world_model WHERE id = ?", (belief_id,)).fetchone()
        if not belief:
            return

        supporting = json.loads(belief["supporting_evidence"] or "[]")
        contradicting = json.loads(belief["contradicting_evidence"] or "[]")

        if supports:
            supporting.append(prediction_id)
        else:
            contradicting.append(prediction_id)

        total = len(supporting) + len(contradicting)
        new_confidence = safe_div(len(supporting), total, 0.5)
        # Blend with prior (don't swing too wildly)
        blended_confidence = belief["belief_confidence"] * 0.7 + new_confidence * 0.3

        self.db.execute(
            """UPDATE world_model SET
               supporting_evidence = ?, contradicting_evidence = ?,
               evidence_count = ?, belief_confidence = ?, last_tested = ?
               WHERE id = ?""",
            (json.dumps(supporting), json.dumps(contradicting),
             total, blended_confidence, now_iso(), belief_id)
        )

        # Deprecate if confidence drops too low
        if blended_confidence < 0.2 and total >= 5:
            self.db.execute(
                """UPDATE world_model SET status = 'disproven',
                   deprecated_reason = ? WHERE id = ?""",
                (f"confidence dropped to {blended_confidence:.2f} after {total} observations", belief_id)
            )
            self.log.info("Belief #%d disproven: %s", belief_id, belief["belief"][:60])

        self.db.commit()

    def create_belief_from_spike(self, spike_analysis: dict, spike_event_id: int) -> Optional[int]:
        """Create a new world model belief from spike analysis."""
        audience_belief = spike_analysis.get("audience_belief", "")
        content_belief = spike_analysis.get("content_belief", "")

        belief_ids = []

        if audience_belief:
            cur = self.db.execute(
                """INSERT INTO world_model
                   (ts, belief_type, belief, belief_confidence, source,
                    serves_identity, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), "audience_model", audience_belief, 0.4,
                 "spike_analysis", 1, "active")
            )
            belief_ids.append(cur.lastrowid)

        if content_belief:
            cur = self.db.execute(
                """INSERT INTO world_model
                   (ts, belief_type, belief, belief_confidence, source,
                    serves_identity, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), "content_model", content_belief, 0.4,
                 "spike_analysis", 1, "active")
            )
            belief_ids.append(cur.lastrowid)

        if belief_ids:
            self.db.execute(
                "UPDATE spike_events SET beliefs_created = ? WHERE id = ?",
                (json.dumps(belief_ids), spike_event_id)
            )
            self.db.commit()
            self.log.info("Created %d beliefs from spike #%d", len(belief_ids), spike_event_id)

        return belief_ids[0] if belief_ids else None

    def identity_drift_check(self) -> dict:
        """
        Check if recent optimization is drifting away from identity beliefs.
        Returns drift score (0 = on brand, 1 = completely off brand) and details.
        """
        identity_beliefs = self.get_active_beliefs("identity_model")
        if not identity_beliefs:
            return {"drift_score": 0, "details": "no identity beliefs defined"}

        # Check recent constraints and experiments
        recent_constraints = self.db.execute(
            """SELECT * FROM learned_constraints
               WHERE active = 1 AND ts > ?""",
            (hours_ago(168),)
        ).fetchall()

        recent_experiments = self.db.execute(
            """SELECT * FROM knob_experiments
               WHERE status IN ('running', 'kept') AND ts > ?""",
            (hours_ago(168),)
        ).fetchall()

        # Simple heuristic: are we boosting engagement-farming patterns?
        drift_signals = []

        for c in recent_constraints:
            # If we're boosting low-quality patterns
            if c["modifier"] > 1.5 and "engagement" in (c["reason"] or "").lower():
                drift_signals.append(f"boosting {c['target_value']} by x{c['modifier']:.1f}")

        for e in recent_experiments:
            # If we're lowering quality thresholds
            if "min_score" in e["knob_name"] and e["new_value"] < e["old_value"]:
                drift_signals.append(f"lowered {e['knob_name']} from {e['old_value']} to {e['new_value']}")

        drift_score = min(1.0, len(drift_signals) * 0.3)
        return {
            "drift_score": drift_score,
            "details": "; ".join(drift_signals) if drift_signals else "no drift detected",
            "identity_beliefs": [b["belief"] for b in identity_beliefs[:3]]
        }

    def build_mythos_prompt_section(self) -> str:
        """Build a prompt section injecting world model into generation."""
        beliefs = self.get_active_beliefs()
        if not beliefs:
            return ""

        parts = ["## WORLD MODEL (what you believe about your audience and content)"]
        for b in beliefs[:10]:
            conf_label = "strong" if b["belief_confidence"] > 0.7 else "moderate" if b["belief_confidence"] > 0.4 else "tentative"
            parts.append(f"- [{conf_label}] {b['belief']}")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 4. SELF-MODIFIER (v2 — statistical rigor)
# ---------------------------------------------------------------------------

class SelfModifier:
    """
    v2: Experiments use Welch's t-test with effect size.
    Proposal system can reach more than just territory knobs.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.log = logging.getLogger("modifier")
        self.safety = self._load_safety_config()

    def propose_experiment(self) -> Optional[int]:
        running = self.db.execute(
            "SELECT COUNT(*) as n FROM knob_experiments WHERE status = 'running'"
        ).fetchone()["n"]
        if running >= self.safety.get("max_concurrent_experiments", 2):
            return None

        # Try multiple analysis strategies (not just territory)
        analysis = (
            self._analyze_territory_errors() or
            self._analyze_timing_errors() or
            self._analyze_reply_quality() or
            self._analyze_exploration_rate() or
            self._analyze_content_spacing()
        )

        if not analysis:
            self.log.info("No clear signal for knob change")
            return None

        return self._create_experiment(analysis)

    def check_experiments(self):
        running = self.db.execute(
            "SELECT * FROM knob_experiments WHERE status = 'running'"
        ).fetchall()

        for exp in running:
            started = parse_ts(exp["started_at"]) if exp["started_at"] else parse_ts(exp["ts"])
            duration = timedelta(hours=exp["duration_hours"] or 48)

            if now_ist() < started + duration:
                remaining = (started + duration) - now_ist()
                self.log.info("Experiment #%d has %.1f hours remaining",
                             exp["id"], remaining.total_seconds() / 3600)
                continue

            self._conclude_experiment(exp)

    def emergency_revert(self):
        threshold = self.safety.get("auto_revert_if_engagement_drops_by", 0.30)
        running = self.db.execute(
            "SELECT * FROM knob_experiments WHERE status = 'running'"
        ).fetchall()

        for exp in running:
            started = exp["started_at"] or exp["ts"]
            metric_before = exp["metric_before"]
            if not metric_before or metric_before == 0:
                continue

            current_metric = self._get_metric_stats(exp["metric_name"], started)
            if current_metric is None:
                continue

            drop = safe_div(metric_before - current_metric["mean"], metric_before)
            if drop > threshold:
                self.log.warning(
                    "EMERGENCY REVERT: Experiment #%d caused %.0f%% drop",
                    exp["id"], drop * 100
                )
                self._revert_experiment(exp, f"emergency: {drop*100:.0f}% drop")

    # --- Analysis strategies ---

    def _analyze_territory_errors(self) -> Optional[dict]:
        territory_errors = self.db.execute(
            """SELECT territory, AVG(z_score) as avg_z, AVG(error_impressions) as avg_error,
                      COUNT(*) as n, AVG(surprise_score) as avg_surprise
               FROM predictions
               WHERE actual_impressions IS NOT NULL AND ts > ?
               GROUP BY territory HAVING n >= 5
               ORDER BY avg_surprise DESC""",
            (hours_ago(72),)
        ).fetchall()

        if not territory_errors:
            return None

        worst = territory_errors[0]
        if worst["avg_surprise"] < 0.3:
            return None

        knob_name = f"territory_weight_{worst['territory']}"
        knob = self.db.execute(
            "SELECT * FROM knob_state WHERE knob_name = ?", (knob_name,)
        ).fetchone()
        if not knob:
            return None

        avg_error = worst["avg_error"] or 0
        direction = -1 if avg_error > 0 else 1

        return {
            "knob": knob_name,
            "direction": direction,
            "magnitude": min(0.05, abs(worst["avg_surprise"]) * 0.1),
            "hypothesis": f"{'reducing' if direction < 0 else 'increasing'} {worst['territory']} weight (avg z-score: {worst['avg_z']:.1f}, n={worst['n']})",
            "rationale": f"territory {worst['territory']} has avg surprise {worst['avg_surprise']:.2f} across {worst['n']} predictions"
        }

    def _analyze_timing_errors(self) -> Optional[dict]:
        """Should we shift posting hours?"""
        hour_perf = self.db.execute(
            """SELECT hour_bucket, AVG(actual_engagement_rate) as avg_rate, COUNT(*) as n
               FROM predictions
               WHERE actual_engagement_rate IS NOT NULL AND ts > ?
               GROUP BY hour_bucket HAVING n >= 3
               ORDER BY avg_rate DESC""",
            (hours_ago(168),)
        ).fetchall()

        if len(hour_perf) < 4:
            return None

        best_hours = [h["hour_bucket"] for h in hour_perf[:3]]
        current_start = self.db.execute(
            "SELECT current_value FROM knob_state WHERE knob_name = 'posting_hour_start'"
        ).fetchone()

        if not current_start:
            return None

        # If best performing hours are earlier than current start, suggest shifting earlier
        if all(h < current_start["current_value"] for h in best_hours):
            return {
                "knob": "posting_hour_start",
                "direction": -1,
                "magnitude": 1.0,
                "hypothesis": f"shifting posting start earlier (best hours: {best_hours})",
                "rationale": f"top performing hours {best_hours} are before current start {current_start['current_value']}"
            }
        return None

    def _analyze_reply_quality(self) -> Optional[dict]:
        reply_preds = self.db.execute(
            """SELECT z_score, error_engagements
               FROM predictions
               WHERE action_type = 'post_reply' AND actual_engagements IS NOT NULL AND ts > ?""",
            (hours_ago(72),)
        ).fetchall()

        if len(reply_preds) < 5:
            return None

        avg_z = statistics.mean([r["z_score"] or 0 for r in reply_preds])
        if avg_z < -1.0:  # consistently overpredicting reply quality
            return {
                "knob": "reply_min_score",
                "direction": 1,
                "magnitude": 0.5,
                "hypothesis": "raising reply quality threshold (replies consistently underperform predictions)",
                "rationale": f"reply avg z-score is {avg_z:.1f} across {len(reply_preds)} replies"
            }
        return None

    def _analyze_exploration_rate(self) -> Optional[dict]:
        """If we're finding lots of spikes, we might be exploring well (or not enough)."""
        recent_spikes = self.db.execute(
            "SELECT COUNT(*) as n FROM predictions WHERE is_spike = 1 AND ts > ?",
            (hours_ago(168),)
        ).fetchone()["n"]

        total_recent = self.db.execute(
            "SELECT COUNT(*) as n FROM predictions WHERE actual_impressions IS NOT NULL AND ts > ?",
            (hours_ago(168),)
        ).fetchone()["n"]

        if total_recent < 20:
            return None

        spike_rate = safe_div(recent_spikes, total_recent)
        current_explore = self.db.execute(
            "SELECT current_value FROM knob_state WHERE knob_name = 'exploration_rate'"
        ).fetchone()

        if not current_explore:
            return None

        if spike_rate < 0.02 and current_explore["current_value"] < 0.35:
            return {
                "knob": "exploration_rate",
                "direction": 1,
                "magnitude": 0.05,
                "hypothesis": f"increasing exploration (spike rate only {spike_rate:.1%}, need more variance)",
                "rationale": f"only {recent_spikes} spikes in {total_recent} predictions"
            }
        return None

    def _analyze_content_spacing(self) -> Optional[dict]:
        """Are we posting too much?"""
        current_max = self.db.execute(
            "SELECT current_value FROM knob_state WHERE knob_name = 'max_daily_actions'"
        ).fetchone()
        if not current_max:
            return None

        # Check if high-volume days underperform
        daily_perf = self.db.execute(
            """SELECT DATE(ts) as day, COUNT(*) as n, AVG(actual_engagement_rate) as avg_rate
               FROM predictions
               WHERE actual_engagement_rate IS NOT NULL AND ts > ?
               GROUP BY DATE(ts) HAVING n >= 3""",
            (hours_ago(336),)
        ).fetchall()

        if len(daily_perf) < 5:
            return None

        # Correlation between volume and performance
        volumes = [d["n"] for d in daily_perf]
        rates = [d["avg_rate"] for d in daily_perf]

        # Simple: compare high-volume days vs low-volume days
        median_vol = statistics.median(volumes)
        high_vol_rates = [r for v, r in zip(volumes, rates) if v > median_vol]
        low_vol_rates = [r for v, r in zip(volumes, rates) if v <= median_vol]

        if high_vol_rates and low_vol_rates:
            high_avg = statistics.mean(high_vol_rates)
            low_avg = statistics.mean(low_vol_rates)

            if high_avg < low_avg * 0.7:  # high volume days 30%+ worse
                return {
                    "knob": "max_daily_actions",
                    "direction": -1,
                    "magnitude": 5,
                    "hypothesis": "reducing daily actions (high-volume days underperform by 30%+)",
                    "rationale": f"high-volume avg rate: {high_avg:.4f}, low-volume: {low_avg:.4f}"
                }
        return None

    # --- Experiment lifecycle ---

    def _create_experiment(self, analysis: dict) -> Optional[int]:
        knob_name = analysis["knob"]
        knob = self.db.execute(
            "SELECT * FROM knob_state WHERE knob_name = ?", (knob_name,)
        ).fetchone()
        if not knob:
            return None

        current = knob["current_value"]
        change = analysis["magnitude"] * analysis["direction"]
        new_value = max(knob["min_value"], min(knob["max_value"], current + change))

        if abs(new_value - current) < 0.01:
            return None

        # Check cooldown
        recent_revert = self.db.execute(
            """SELECT concluded_at FROM knob_experiments
               WHERE knob_name = ? AND verdict = 'reverted'
               ORDER BY concluded_at DESC LIMIT 1""",
            (knob_name,)
        ).fetchone()
        if recent_revert and recent_revert["concluded_at"]:
            cooldown = self.safety.get("cooldown_after_revert_hours", 72)
            if parse_ts(recent_revert["concluded_at"]) > now_ist() - timedelta(hours=cooldown):
                return None

        change_pct = abs(new_value - current) / max(abs(current), 0.01)
        needs_approval = change_pct > self.safety.get("require_human_approval_above", 0.4)

        # Get baseline stats for statistical testing
        baseline_stats = self._get_metric_stats("engagement_rate", hours_ago(168))
        baseline_mean = baseline_stats["mean"] if baseline_stats else 0
        baseline_std = baseline_stats["std"] if baseline_stats else 0
        baseline_n = baseline_stats["n"] if baseline_stats else 0

        cur = self.db.execute(
            """INSERT INTO knob_experiments
               (ts, knob_name, old_value, new_value, hypothesis, rationale,
                status, duration_hours, metric_name,
                metric_before, metric_before_stddev, metric_before_n)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), knob_name, current, new_value,
             analysis["hypothesis"], analysis["rationale"],
             "proposed" if needs_approval else "running",
             self.safety.get("duration_hours", 48), "engagement_rate",
             baseline_mean, baseline_std, baseline_n)
        )
        exp_id = cur.lastrowid

        if not needs_approval:
            self._apply_knob_change(knob_name, new_value, exp_id)

        self.db.commit()
        self.log.info("Experiment #%d: %s %.3f -> %.3f (%s)",
                     exp_id, knob_name, current, new_value,
                     "PROPOSED" if needs_approval else "RUNNING")
        return exp_id

    def _conclude_experiment(self, exp):
        """Conclude with Welch's t-test and effect size."""
        started = exp["started_at"] or exp["ts"]
        after_stats = self._get_metric_stats(exp["metric_name"], started)

        if not after_stats or after_stats["n"] < MIN_SAMPLE_SIZE_EXPERIMENT:
            # Extend if not enough data
            self.db.execute(
                "UPDATE knob_experiments SET duration_hours = duration_hours + 24 WHERE id = ?",
                (exp["id"],)
            )
            self.db.commit()
            self.log.info("Experiment #%d extended: only %d samples", exp["id"], after_stats["n"] if after_stats else 0)
            return

        before_mean = exp["metric_before"] or 0
        before_std = exp["metric_before_stddev"] or after_stats["std"]
        before_n = exp["metric_before_n"] or after_stats["n"]

        after_mean = after_stats["mean"]
        after_std = after_stats["std"]
        after_n = after_stats["n"]

        # Welch's t-test
        t_stat, p_value = welch_t_test(before_mean, before_std, before_n,
                                        after_mean, after_std, after_n)
        effect = cohens_d(before_mean, before_std, before_n,
                         after_mean, after_std, after_n)

        # Decision logic
        if p_value < P_VALUE_THRESHOLD and effect > MIN_EFFECT_SIZE and after_mean > before_mean:
            verdict = "kept"
            reasoning = (f"statistically significant improvement: "
                        f"t={t_stat:.2f}, p={p_value:.3f}, d={effect:.2f}, "
                        f"mean {before_mean:.4f} -> {after_mean:.4f}")
        elif p_value < P_VALUE_THRESHOLD and effect > MIN_EFFECT_SIZE and after_mean < before_mean:
            verdict = "reverted"
            reasoning = (f"statistically significant degradation: "
                        f"t={t_stat:.2f}, p={p_value:.3f}, d={effect:.2f}")
        elif p_value >= P_VALUE_THRESHOLD:
            verdict = "inconclusive"
            reasoning = (f"not significant: t={t_stat:.2f}, p={p_value:.3f}, d={effect:.2f} "
                        f"(need p<{P_VALUE_THRESHOLD}, d>{MIN_EFFECT_SIZE})")
        else:
            verdict = "inconclusive"
            reasoning = f"effect too small: d={effect:.2f}"

        self.db.execute(
            """UPDATE knob_experiments SET
               metric_after = ?, metric_after_stddev = ?, metric_after_n = ?,
               t_statistic = ?, p_value = ?, effect_size = ?,
               verdict = ?, verdict_reasoning = ?, concluded_at = ?, status = 'concluded'
               WHERE id = ?""",
            (after_mean, after_std, after_n,
             t_stat, p_value, effect,
             verdict, reasoning, now_iso(), exp["id"])
        )

        if verdict == "reverted" or verdict == "inconclusive":
            self._revert_experiment(exp, reasoning)
        
        self.db.commit()
        self.log.info("Experiment #%d concluded: %s (t=%.2f, p=%.3f, d=%.2f)",
                     exp["id"], verdict, t_stat, p_value, effect)

    def _apply_knob_change(self, knob_name: str, new_value: float, exp_id: int):
        self.db.execute(
            "UPDATE knob_state SET current_value = ?, last_modified = ?, modified_by = ? WHERE knob_name = ?",
            (new_value, now_iso(), "cortex", knob_name)
        )
        self.db.execute(
            "UPDATE knob_experiments SET status = 'running', started_at = ? WHERE id = ?",
            (now_iso(), exp_id)
        )
        self.db.commit()

    def _revert_experiment(self, exp, reason: str):
        self.db.execute(
            "UPDATE knob_state SET current_value = ?, last_modified = ?, modified_by = ? WHERE knob_name = ?",
            (exp["old_value"], now_iso(), "cortex_revert", exp["knob_name"])
        )
        self.db.execute(
            "UPDATE knob_experiments SET verdict = 'reverted', verdict_reasoning = ?, concluded_at = ? WHERE id = ?",
            (reason, now_iso(), exp["id"])
        )
        self.db.commit()

    def _get_metric_stats(self, metric_name: str, since: str) -> Optional[dict]:
        if metric_name == "engagement_rate":
            rows = self.db.execute(
                """SELECT actual_engagement_rate FROM predictions
                   WHERE actual_engagement_rate IS NOT NULL AND actual_impressions > 0 AND ts > ?""",
                (since,)
            ).fetchall()
            if not rows:
                return None
            values = [r["actual_engagement_rate"] for r in rows]
            return {
                "mean": statistics.mean(values),
                "std": statistics.stdev(values) if len(values) > 1 else 0,
                "n": len(values)
            }
        return None

    def _load_safety_config(self) -> dict:
        if KNOBS_PATH.exists():
            config = json.loads(KNOBS_PATH.read_text())
            safety = config.get("safety", {})
            safety.update(config.get("experiment_defaults", {}))
            return safety
        return {}


# ---------------------------------------------------------------------------
# 5. ADAPTIVE MEMORY (v2 — compound constraints, spike-derived)
# ---------------------------------------------------------------------------

class AdaptiveMemory:
    """
    v2: Compound constraints, spike-derived constraints, mythos-linked.
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.log = logging.getLogger("memory")

    def learn_from_predictions(self):
        self._learn_territory_constraints()
        self._learn_hook_pattern_constraints()
        self._learn_timing_constraints()
        self._learn_target_constraints()
        self._learn_compound_constraints()
        self._deprecate_stale_constraints()

    def apply_constraints_to_score(self, scope: str, features: dict, base_score: float) -> float:
        constraints = self.db.execute(
            "SELECT * FROM learned_constraints WHERE active = 1 AND scope = ?",
            (scope,)
        ).fetchall()

        modified_score = base_score
        for c in constraints:
            field_val = str(features.get(c["target_field"], ""))
            if field_val != str(c["target_value"]):
                continue

            if c["is_compound"] and c["target_field_2"]:
                field_val_2 = str(features.get(c["target_field_2"], ""))
                if field_val_2 != str(c["target_value_2"]):
                    continue

            modified_score *= c["modifier"]

        return modified_score

    def _learn_territory_constraints(self):
        territories = self.db.execute(
            """SELECT territory,
                      AVG(actual_engagement_rate) as avg_rate,
                      COUNT(*) as n
               FROM predictions
               WHERE actual_impressions IS NOT NULL AND territory IS NOT NULL
               GROUP BY territory HAVING n >= 5"""
        ).fetchall()

        if not territories:
            return

        overall_rate = self.db.execute(
            "SELECT AVG(actual_engagement_rate) as rate FROM predictions WHERE actual_impressions IS NOT NULL"
        ).fetchone()["rate"] or 0.02

        for t in territories:
            if not t["avg_rate"] or overall_rate == 0:
                continue
            ratio = t["avg_rate"] / overall_rate
            if ratio < 0.6 or ratio > 1.5:
                self._upsert_constraint(
                    scope="tweet", target_field="territory", target_value=t["territory"],
                    modifier=max(0.3, min(1.8, ratio)),
                    reason=f"{t['territory']} at {ratio:.0%} of avg (n={t['n']})",
                    observation_count=t["n"], min_observations=8, source="statistical"
                )

    def _learn_hook_pattern_constraints(self):
        patterns = self.db.execute(
            """SELECT hook_pattern, AVG(actual_engagement_rate) as avg_rate, COUNT(*) as n
               FROM predictions
               WHERE actual_impressions IS NOT NULL AND hook_pattern IS NOT NULL AND hook_pattern != 'unknown'
               GROUP BY hook_pattern HAVING n >= 3"""
        ).fetchall()

        overall_rate = self.db.execute(
            "SELECT AVG(actual_engagement_rate) as rate FROM predictions WHERE actual_impressions IS NOT NULL"
        ).fetchone()["rate"] or 0.02

        for p in patterns:
            if not p["avg_rate"] or overall_rate == 0:
                continue
            ratio = p["avg_rate"] / overall_rate
            if ratio < 0.5 or ratio > 1.8:
                self._upsert_constraint(
                    scope="tweet", target_field="hook_pattern", target_value=p["hook_pattern"],
                    modifier=max(0.2, min(2.0, ratio)),
                    reason=f"hook '{p['hook_pattern']}' at {ratio:.0%} of avg (n={p['n']})",
                    observation_count=p["n"], min_observations=5, source="statistical"
                )

    def _learn_timing_constraints(self):
        hours = self.db.execute(
            """SELECT hour_bucket, AVG(actual_engagement_rate) as avg_rate, COUNT(*) as n
               FROM predictions
               WHERE actual_impressions IS NOT NULL AND hour_bucket IS NOT NULL
               GROUP BY hour_bucket HAVING n >= 3"""
        ).fetchall()

        if not hours:
            return

        overall_rate = statistics.mean([h["avg_rate"] or 0 for h in hours])
        if overall_rate == 0:
            return

        for h in hours:
            if not h["avg_rate"]:
                continue
            ratio = h["avg_rate"] / overall_rate
            if ratio < 0.5 or ratio > 1.5:
                self._upsert_constraint(
                    scope="tweet", target_field="hour_bucket", target_value=str(h["hour_bucket"]),
                    modifier=max(0.4, min(1.8, ratio)),
                    reason=f"hour {h['hour_bucket']} IST at {ratio:.0%} of avg (n={h['n']})",
                    observation_count=h["n"], min_observations=5, source="statistical"
                )

    def _learn_target_constraints(self):
        targets = self.db.execute(
            """SELECT target_handle, AVG(actual_engagements) as avg_eng, COUNT(*) as n
               FROM predictions
               WHERE action_type = 'post_reply' AND actual_engagements IS NOT NULL
                     AND target_handle IS NOT NULL AND target_handle != ''
               GROUP BY target_handle HAVING n >= 3"""
        ).fetchall()

        if not targets:
            return

        overall_eng = self.db.execute(
            "SELECT AVG(actual_engagements) as eng FROM predictions WHERE action_type = 'post_reply' AND actual_engagements IS NOT NULL"
        ).fetchone()["eng"] or 1

        for t in targets:
            if not t["avg_eng"] or overall_eng == 0:
                continue
            ratio = t["avg_eng"] / overall_eng
            if ratio < 0.3 or ratio > 2.0:
                self._upsert_constraint(
                    scope="reply", target_field="target_handle", target_value=t["target_handle"],
                    modifier=max(0.1, min(2.5, ratio)),
                    reason=f"@{t['target_handle']} at {ratio:.0%} of avg engagement (n={t['n']})",
                    observation_count=t["n"], min_observations=4, source="statistical"
                )

    def _learn_compound_constraints(self):
        """
        Learn two-dimensional constraints: territory + hour, territory + hook_pattern.
        These capture interaction effects that single-field constraints miss.
        """
        # Territory + hour_bucket interactions
        combos = self.db.execute(
            """SELECT territory, hour_bucket,
                      AVG(actual_engagement_rate) as avg_rate, COUNT(*) as n
               FROM predictions
               WHERE actual_impressions IS NOT NULL AND territory IS NOT NULL
               GROUP BY territory, hour_bucket
               HAVING n >= 3"""
        ).fetchall()

        overall_rate = self.db.execute(
            "SELECT AVG(actual_engagement_rate) as rate FROM predictions WHERE actual_impressions IS NOT NULL"
        ).fetchone()["rate"] or 0.02

        for c in combos:
            if not c["avg_rate"] or overall_rate == 0:
                continue
            ratio = c["avg_rate"] / overall_rate

            # Only create compound constraint if the interaction effect is strong
            # AND different from the individual effects
            if ratio < 0.4 or ratio > 2.0:
                self._upsert_compound_constraint(
                    scope="tweet",
                    field1="territory", value1=c["territory"],
                    field2="hour_bucket", value2=str(c["hour_bucket"]),
                    modifier=max(0.2, min(2.5, ratio)),
                    reason=f"{c['territory']} at hour {c['hour_bucket']} IST: {ratio:.0%} of avg (n={c['n']})",
                    observation_count=c["n"], min_observations=4
                )

    def _upsert_constraint(self, scope, target_field, target_value, modifier, reason,
                           observation_count, min_observations, source="statistical"):
        existing = self.db.execute(
            """SELECT id, observation_count, modifier FROM learned_constraints
               WHERE scope = ? AND target_field = ? AND target_value = ? AND is_compound = 0 AND active >= 0""",
            (scope, target_field, target_value)
        ).fetchone()

        should_activate = observation_count >= min_observations

        if existing:
            # Weighted blend: more new data = more weight to new modifier
            new_weight = min(0.6, observation_count / (observation_count + existing["observation_count"]))
            old_weight = 1.0 - new_weight
            blended = existing["modifier"] * old_weight + modifier * new_weight

            self.db.execute(
                """UPDATE learned_constraints SET
                   modifier = ?, reason = ?, observation_count = ?, active = ?, ts = ?
                   WHERE id = ?""",
                (blended, reason, observation_count,
                 1 if should_activate else 0, now_iso(), existing["id"])
            )
        else:
            self.db.execute(
                """INSERT INTO learned_constraints
                   (ts, constraint_type, scope, target_field, target_value,
                    modifier, reason, observation_count, min_observations, active, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), "score_multiplier", scope, target_field, target_value,
                 modifier, reason, observation_count, min_observations,
                 1 if should_activate else 0, source)
            )
            if should_activate:
                self.log.info("NEW CONSTRAINT: %s.%s=%s x%.2f (%s)",
                             scope, target_field, target_value, modifier, reason)
        self.db.commit()

    def _upsert_compound_constraint(self, scope, field1, value1, field2, value2,
                                     modifier, reason, observation_count, min_observations):
        existing = self.db.execute(
            """SELECT id, observation_count, modifier FROM learned_constraints
               WHERE scope = ? AND target_field = ? AND target_value = ?
               AND target_field_2 = ? AND target_value_2 = ? AND is_compound = 1 AND active >= 0""",
            (scope, field1, value1, field2, value2)
        ).fetchone()

        should_activate = observation_count >= min_observations

        if existing:
            new_weight = min(0.6, observation_count / (observation_count + existing["observation_count"]))
            blended = existing["modifier"] * (1 - new_weight) + modifier * new_weight
            self.db.execute(
                """UPDATE learned_constraints SET
                   modifier = ?, reason = ?, observation_count = ?, active = ?, ts = ?
                   WHERE id = ?""",
                (blended, reason, observation_count,
                 1 if should_activate else 0, now_iso(), existing["id"])
            )
        else:
            self.db.execute(
                """INSERT INTO learned_constraints
                   (ts, constraint_type, scope, target_field, target_value,
                    target_field_2, target_value_2, is_compound,
                    modifier, reason, observation_count, min_observations, active, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), "compound_multiplier", scope, field1, value1, field2, value2,
                 modifier, reason, observation_count, min_observations,
                 1 if should_activate else 0, "statistical")
            )
            if should_activate:
                self.log.info("NEW COMPOUND: %s.%s=%s+%s=%s x%.2f",
                             scope, field1, value1, field2, value2, modifier)
        self.db.commit()

    def _deprecate_stale_constraints(self):
        stale_cutoff = hours_ago(336)
        stale = self.db.execute(
            "SELECT id FROM learned_constraints WHERE active = 1 AND ts < ?",
            (stale_cutoff,)
        ).fetchall()
        for s in stale:
            self.db.execute(
                """UPDATE learned_constraints SET active = -1,
                   deprecated_at = ?, deprecated_reason = 'stale: 14 days without reinforcement'
                   WHERE id = ?""",
                (now_iso(), s["id"])
            )
        self.db.commit()


# ---------------------------------------------------------------------------
# 6. COGNITIVE CONTINUITY (v2 — dynamic goals, strategy tracking)
# ---------------------------------------------------------------------------

class CognitiveContinuity:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.log = logging.getLogger("continuity")

    def get_current_state(self) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM cognitive_state ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def save_state(self, goals: list, experiments: list, attention: str,
                   questions: list, hypotheses: list, surprises: list,
                   confidence: float, explore_vs_exploit: float, narrative: str,
                   strategy: dict = None, identity_drift: dict = None):
        prev = self.get_current_state()
        prev_id = prev["id"] if prev else None
        cycle = (prev["cycle_number"] + 1) if prev else 1

        delta = self._compute_delta(prev, {
            "active_goals": goals,
            "confidence_level": confidence,
            "exploration_vs_exploitation": explore_vs_exploit,
            "recent_surprises": surprises,
        }) if prev else "initial state"

        # Strategy tracking
        strategy = strategy or {}
        strategy_perf = strategy.get("performance", 0)
        strategy_duration = 1
        should_pivot = 0

        if prev:
            try:
                prev_strategy = json.loads(prev["current_strategy"] or "{}")
            except (json.JSONDecodeError, TypeError):
                prev_strategy = {}

            if prev_strategy.get("name") == strategy.get("name"):
                strategy_duration = (prev["strategy_duration_cycles"] or 0) + 1
            else:
                strategy_duration = 1

            # Pivot if strategy has been running 7+ cycles with declining performance
            if strategy_duration >= 7 and strategy_perf < (prev.get("strategy_performance") or 0) * 0.8:
                should_pivot = 1

        drift = identity_drift or {"drift_score": 0, "details": ""}

        self.db.execute(
            """INSERT INTO cognitive_state
               (ts, cycle_number, active_goals, active_experiments, attention_focus,
                pending_questions, working_hypotheses, recent_surprises,
                confidence_level, exploration_vs_exploitation, narrative,
                current_strategy, strategy_performance, strategy_duration_cycles, should_pivot,
                identity_drift_score, drift_details,
                previous_state_id, state_delta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), cycle,
             json.dumps(goals), json.dumps(experiments), attention,
             json.dumps(questions), json.dumps(hypotheses), json.dumps(surprises),
             confidence, explore_vs_exploit, narrative,
             json.dumps(strategy), strategy_perf, strategy_duration, should_pivot,
             drift["drift_score"], drift.get("details", ""),
             prev_id, delta)
        )
        self.db.commit()

    def build_resumption_prompt(self) -> str:
        state = self.get_current_state()
        if not state:
            return "This is your first cycle. No prior state exists."

        parts = [
            "## RESUMING FROM PREVIOUS STATE",
            f"Cycle: {state['cycle_number']} | Last active: {state['ts']}",
            "",
            f"### Narrative: {state['narrative']}",
            "",
            f"### Current strategy: {state['current_strategy']}",
            f"### Strategy duration: {state['strategy_duration_cycles']} cycles",
        ]

        if state.get("should_pivot"):
            parts.append("### ⚠ PIVOT SIGNAL: strategy performance declining, consider changing approach")

        if state.get("identity_drift_score", 0) > 0.3:
            parts.append(f"### ⚠ IDENTITY DRIFT: score {state['identity_drift_score']:.2f} — {state.get('drift_details', '')}")

        parts.extend([
            f"### Goals: {state['active_goals']}",
            f"### Open questions: {state['pending_questions']}",
            f"### Hypotheses: {state['working_hypotheses']}",
            f"### Surprises: {state['recent_surprises']}",
            f"### Confidence: {state['confidence_level']:.2f} | Explore/exploit: {state['exploration_vs_exploitation']:.2f}",
        ])

        return "\n".join(parts)

    def _compute_delta(self, prev: dict, current: dict) -> str:
        changes = []
        prev_conf = prev.get("confidence_level", 0.5)
        curr_conf = current.get("confidence_level", 0.5)
        if abs(curr_conf - prev_conf) > 0.1:
            changes.append(f"confidence {'up' if curr_conf > prev_conf else 'down'} ({prev_conf:.2f}->{curr_conf:.2f})")

        prev_explore = prev.get("exploration_vs_exploitation", 0.5)
        curr_explore = current.get("exploration_vs_exploitation", 0.5)
        if abs(curr_explore - prev_explore) > 0.1:
            changes.append("more exploratory" if curr_explore > prev_explore else "more exploitative")

        surprises = current.get("recent_surprises", [])
        if surprises:
            changes.append(f"{len(surprises)} new surprises")

        return "; ".join(changes) if changes else "minimal change"


# ---------------------------------------------------------------------------
# INTEGRATION API (for brain.py, hands.py, khud-x.py)
# ---------------------------------------------------------------------------

def cortex_score_tweet(db: sqlite3.Connection, tweet_row: dict, base_score: float) -> float:
    memory = AdaptiveMemory(db)
    features = PredictionEngine(db)._extract_tweet_features(tweet_row)
    return memory.apply_constraints_to_score("tweet", features, base_score)

def cortex_score_reply(db: sqlite3.Connection, reply_row: dict, base_score: float) -> float:
    memory = AdaptiveMemory(db)
    features = PredictionEngine(db)._extract_reply_features(reply_row)
    return memory.apply_constraints_to_score("reply", features, base_score)

def cortex_before_post(db: sqlite3.Connection, action_type: str, row: dict,
                       context: dict = None) -> int:
    """
    v2: Now accepts context dict with confounders.
    context = {
        "session_token_count": 150000,
        "follower_count": 2500,
        "parent_tweet_velocity": 0.5,  # for replies
        "parent_tweet_age_minutes": 15,
        ...
    }
    """
    engine = PredictionEngine(db)
    if action_type == "post_tweet":
        return engine.predict_tweet(row, context)
    elif action_type == "post_reply":
        return engine.predict_reply(row, context)
    return -1

def cortex_get_knob(db: sqlite3.Connection, knob_name: str, default: float = None) -> float:
    row = db.execute(
        "SELECT current_value FROM knob_state WHERE knob_name = ?", (knob_name,)
    ).fetchone()
    return row["current_value"] if row else default

def cortex_get_resumption_prompt(db: sqlite3.Connection) -> str:
    return CognitiveContinuity(db).build_resumption_prompt()

def cortex_get_mythos_prompt(db: sqlite3.Connection) -> str:
    """Inject world model beliefs into generation prompt."""
    return Mythos(db).build_mythos_prompt_section()

def cortex_should_reset_session(db: sqlite3.Connection, session_token_count: int) -> bool:
    """
    Call from brain.py before generating candidates.
    Returns True if Claude session should be reset for quality.
    """
    if session_token_count > CLAUDE_SESSION_HARD_LIMIT:
        cortex_log_event(db, "confounders", "session_reset_recommended",
                        f"Session at {session_token_count} tokens, recommend reset")
        return True
    return False


# ---------------------------------------------------------------------------
# MAIN CYCLE
# ---------------------------------------------------------------------------

def run_full_cycle():
    db = get_db()
    init_schema(db)
    init_knobs(db)

    log.info("=== CORTEX v2 CYCLE START ===")

    measured = 0
    new_spikes = 0
    active_constraints = 0
    compound_constraints = 0
    exp_id = None
    drift = {"drift_score": 0, "details": "not checked"}

    # 1. Measure outcomes
    try:
        predictor = PredictionEngine(db)
        measured = predictor.measure_outcomes()
        log.info("Step 1: Measured %d predictions", measured)
    except Exception as e:
        log.error("Step 1 (measure) failed: %s", e)
        predictor = PredictionEngine(db)

    # 2. Detect spikes
    try:
        spiker = SpikeDetector(db)
        new_spikes = spiker.detect_and_analyze()
        log.info("Step 2: %d new spikes detected", new_spikes)
    except Exception as e:
        log.error("Step 2 (spikes) failed: %s", e)
        spiker = SpikeDetector(db)

    # 2.5. Analyze spikes with Claude API (if available)
    if CortexClaude:
        try:
            claude = CortexClaude(db)
            analyzed = claude.analyze_pending_spikes()
            if analyzed:
                log.info("Step 2.5: Analyzed %d spikes with Claude", analyzed)
        except Exception as e:
            log.error("Step 2.5 (Claude spike analysis) failed: %s", e)

    # 3. Learn constraints
    try:
        memory = AdaptiveMemory(db)
        memory.learn_from_predictions()
        active_constraints = db.execute(
            "SELECT COUNT(*) as n FROM learned_constraints WHERE active = 1"
        ).fetchone()["n"]
        compound_constraints = db.execute(
            "SELECT COUNT(*) as n FROM learned_constraints WHERE active = 1 AND is_compound = 1"
        ).fetchone()["n"]
        log.info("Step 3: %d active constraints (%d compound)", active_constraints, compound_constraints)
    except Exception as e:
        log.error("Step 3 (constraints) failed: %s", e)

    # 4. Check/propose experiments
    try:
        modifier = SelfModifier(db)
        modifier.emergency_revert()
        modifier.check_experiments()
        exp_id = modifier.propose_experiment()
        log.info("Step 4: %s", f"Proposed experiment #{exp_id}" if exp_id else "No experiment proposed")
    except Exception as e:
        log.error("Step 4 (experiments) failed: %s", e)

    # 5. Performance + spike replication summary
    try:
        predictor.compute_performance_summary()
        spiker.compute_replication_scorecard()
    except Exception as e:
        log.error("Step 5 (performance summary) failed: %s", e)

    # 6. Mythos: identity drift check + belief testing
    try:
        mythos = Mythos(db)
        drift = mythos.identity_drift_check()
        if drift["drift_score"] > 0.3:
            log.warning("IDENTITY DRIFT: %.2f -- %s", drift["drift_score"], drift["details"])
    except Exception as e:
        log.error("Step 6 (mythos) failed: %s", e)

    # 6.5. Test beliefs against recent prediction outcomes
    if CortexClaude:
        try:
            claude = CortexClaude(db)
            belief_tests = claude.test_beliefs_against_recent(hours_back=48)
            if belief_tests:
                log.info("Step 6.5: Tested %d belief-prediction pairs", belief_tests)
        except Exception as e:
            log.error("Step 6.5 (belief testing) failed: %s", e)

    # 7. Cognitive continuity -- DYNAMIC goals based on actual state
    try:
        continuity = CognitiveContinuity(db)
        prev_state = continuity.get_current_state()

        running_experiments = db.execute(
            "SELECT knob_name, new_value, hypothesis FROM knob_experiments WHERE status = 'running'"
        ).fetchall()

        recent_surprises = db.execute(
            """SELECT territory, z_score, lesson FROM predictions
               WHERE z_score IS NOT NULL AND ABS(z_score) > 2.0 AND measured_at IS NOT NULL
               ORDER BY measured_at DESC LIMIT 5"""
        ).fetchall()

        perf = db.execute(
            """SELECT trend, mean_abs_error, accuracy_within_50pct, correlation
               FROM prediction_performance
               WHERE window_label = 'week'
               ORDER BY ts DESC LIMIT 1"""
        ).fetchone()

        spike_scorecard = db.execute(
            "SELECT * FROM spike_replication_scorecard ORDER BY ts DESC LIMIT 1"
        ).fetchone()

        # Dynamic goals based on what's actually happening
        goals = []
        if perf and perf["trend"] == "degrading":
            goals.append("URGENT: prediction accuracy degrading -- investigate cause")
        if drift["drift_score"] > 0.3:
            goals.append(f"IDENTITY: drift detected ({drift['drift_score']:.2f}) -- realign with identity beliefs")
        if spike_scorecard and (spike_scorecard["replication_rate"] or 0) < 0.2:
            goals.append("improve spike replication rate (currently below 20%)")
        elif spike_scorecard and (spike_scorecard["replication_rate"] or 0) > 0.4:
            goals.append("spike replication working -- maintain and refine")

        # Standard goals (lower priority)
        goals.extend([
            "improve prediction accuracy",
            "grow engagement through learned constraints",
            "analyze unanalyzed spikes for replicable patterns"
        ])

        # Explore/exploit balance
        total_predictions = db.execute(
            "SELECT COUNT(*) as n FROM predictions WHERE actual_impressions IS NOT NULL"
        ).fetchone()["n"]
        if total_predictions < 30:
            explore_exploit = 0.7
        elif total_predictions < 100:
            explore_exploit = 0.4
        else:
            explore_exploit = 0.2

        # Confidence from multiple signals
        confidence = 0.5
        if perf:
            if perf["correlation"] and perf["correlation"] > 0.5:
                confidence = min(0.85, 0.5 + perf["correlation"] * 0.3)
            elif perf["trend"] == "degrading":
                confidence = max(0.2, confidence - 0.2)

        # Strategy
        strategy = {
            "name": "spike_replication" if (spike_scorecard and new_spikes > 0) else "baseline_optimization",
            "performance": spike_scorecard["replication_rate"] if spike_scorecard else 0,
        }

        # Build narrative
        narrative_parts = [f"Cycle {(prev_state['cycle_number'] + 1) if prev_state else 1}."]
        narrative_parts.append(f"Measured {measured}, {new_spikes} spikes.")
        narrative_parts.append(f"{active_constraints} constraints ({compound_constraints} compound).")
        if perf:
            narrative_parts.append(f"Prediction r={perf['correlation']:.2f}, trend: {perf['trend']}.")
        if spike_scorecard:
            narrative_parts.append(f"Spike replication: {(spike_scorecard['replication_rate'] or 0):.0%}.")
        if drift["drift_score"] > 0:
            narrative_parts.append(f"Identity drift: {drift['drift_score']:.2f}.")

        continuity.save_state(
            goals=goals,
            experiments=[dict(e) for e in running_experiments],
            attention=f"spike replication ({spike_scorecard['replication_rate']:.0%})" if spike_scorecard and spike_scorecard['replication_rate'] else "baseline learning",
            questions=[
                "which spike factors are most replicable?",
                "are confounders (Claude quality, follower growth) explaining prediction errors?",
                "which compound constraints have the strongest signal?",
            ],
            hypotheses=[e["hypothesis"] for e in running_experiments] if running_experiments else ["system is learning baselines"],
            surprises=[
                {"territory": s["territory"], "z_score": s["z_score"], "lesson": s["lesson"]}
                for s in recent_surprises
            ],
            confidence=confidence,
            explore_vs_exploit=explore_exploit,
            narrative=" ".join(narrative_parts),
            strategy=strategy,
            identity_drift=drift,
        )
    except Exception as e:
        log.error("Step 7 (cognitive continuity) failed: %s", e)

    log.info("=== CORTEX v2 CYCLE COMPLETE ===")

    # Print summary
    print("\n--- CORTEX v2 STATUS ---")
    print(f"Predictions measured: {measured}")
    print(f"New spikes: {new_spikes}")
    print(f"Active constraints: {active_constraints} ({compound_constraints} compound)")
    print(f"Running experiments: {len(running_experiments)}")
    if perf:
        print(f"Prediction: trend={perf['trend']}, r={perf['correlation']:.2f}, accuracy={perf['accuracy_within_50pct']:.0%}")
    if spike_scorecard:
        print(f"Spike replication rate: {(spike_scorecard['replication_rate'] or 0):.0%}")
    print(f"Identity drift: {drift['drift_score']:.2f}")
    print(f"Explore/exploit: {explore_exploit:.2f} | Confidence: {confidence:.2f}")
    print(f"Strategy: {strategy['name']}")
    print("------------------------\n")

    db.close()


def run_predict_only():
    db = get_db()
    init_schema(db)
    predictor = PredictionEngine(db)
    measured = predictor.measure_outcomes()
    predictor.compute_performance_summary()
    log.info("Measured %d predictions", measured)
    db.close()

def run_modify_only():
    db = get_db()
    init_schema(db)
    init_knobs(db)
    modifier = SelfModifier(db)
    modifier.emergency_revert()
    modifier.check_experiments()
    modifier.propose_experiment()
    db.close()

def run_mythos_only():
    db = get_db()
    init_schema(db)
    mythos = Mythos(db)
    drift = mythos.identity_drift_check()
    beliefs = mythos.get_active_beliefs()
    print(f"\nActive beliefs: {len(beliefs)}")
    for b in beliefs:
        print(f"  [{b['belief_type']}] (conf {b['belief_confidence']:.2f}) {b['belief']}")
    print(f"\nIdentity drift: {drift['drift_score']:.2f} — {drift['details']}")
    db.close()

def run_spikes_only():
    db = get_db()
    init_schema(db)
    spiker = SpikeDetector(db)
    new = spiker.detect_and_analyze()
    spiker.compute_replication_scorecard()
    print(f"\nNew spikes: {new}")
    unanalyzed = spiker.get_unanalyzed_spikes()
    print(f"Unanalyzed spikes: {len(unanalyzed)}")
    for s in unanalyzed:
        print(f"  #{s['id']}: {s['spike_magnitude']:.1f}x expected")
        print(f"  Prompt: {s['analysis_prompt'][:200]}...")
    db.close()

def seed_mythos():
    """Seed initial world model beliefs for ARIA."""
    db = get_db()
    init_schema(db)
    mythos = Mythos(db)

    # These should be customized per account. These are Rishabh's ARIA beliefs.
    beliefs = [
        {
            "type": "identity_model",
            "belief": "I am building credibility as a builder-PM who thinks at the intersection of AI, product, and craft — not an engagement farmer or content marketer",
            "confidence": 0.9,
            "serves_identity": 1,
            "identity_note": "core identity constraint: never optimize toward engagement bait at the expense of credibility"
        },
        {
            "type": "audience_model",
            "belief": "My best-performing audience are practitioners (PMs, engineers, founders) who are building with AI and value concrete insight over abstract thought leadership",
            "confidence": 0.6,
            "serves_identity": 1,
        },
        {
            "type": "audience_model",
            "belief": "The audience rewards specificity and personal experience over generic takes — showing what you built beats talking about what could be built",
            "confidence": 0.5,
            "serves_identity": 1,
        },
        {
            "type": "content_model",
            "belief": "Contrarian takes on mainstream narratives outperform consensus views, but only when backed by concrete evidence or personal experience",
            "confidence": 0.5,
            "serves_identity": 1,
        },
        {
            "type": "content_model",
            "belief": "Inversion-structured tweets (X is not Y, it's actually Z) have diminishing returns — the format signals AI-generated content to sophisticated readers",
            "confidence": 0.6,
            "serves_identity": 1,
            "identity_note": "this is why we have the inversion_penalty knob"
        },
        {
            "type": "platform_model",
            "belief": "Reply visibility is dominated by timing (early replies on fast-moving threads) and relevance to the original poster — not reply quality alone",
            "confidence": 0.5,
            "serves_identity": 0,
        },
        {
            "type": "platform_model",
            "belief": "Twitter's distribution algorithm rewards consistent posting cadence more than volume — posting 5 good tweets is better than 15 mediocre ones",
            "confidence": 0.4,
            "serves_identity": 1,
        },
        {
            "type": "content_model",
            "belief": "Tweets with concrete numbers, specific tool names, or personal metrics outperform abstract observations by 2-3x",
            "confidence": 0.4,
            "serves_identity": 1,
        },
    ]

    mythos.seed_beliefs(beliefs)
    print(f"Seeded {len(beliefs)} initial world model beliefs")
    db.close()


def print_status():
    db = get_db()
    print("\n=== CORTEX v2 STATUS ===\n")

    # Predictions
    total = db.execute("SELECT COUNT(*) as n FROM predictions").fetchone()
    measured = db.execute("SELECT COUNT(*) as n FROM predictions WHERE actual_impressions IS NOT NULL").fetchone()
    spikes = db.execute("SELECT COUNT(*) as n FROM predictions WHERE is_spike = 1").fetchone()
    print(f"Predictions: {total['n']} total, {measured['n']} measured, {spikes['n']} spikes")

    perf = db.execute(
        "SELECT * FROM prediction_performance WHERE window_label = 'week' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if perf:
        print(f"  Weekly: MAE={perf['mean_abs_error']:.1f}, r={perf['correlation']:.2f}, "
              f"accuracy={perf['accuracy_within_50pct']:.0%}, trend={perf['trend']}")

    # Spike replication
    scorecard = db.execute(
        "SELECT * FROM spike_replication_scorecard ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if scorecard:
        print(f"\nSpike replication: {(scorecard['replication_rate'] or 0):.0%} "
              f"({scorecard['replications_succeeded']}/{scorecard['replications_attempted']})")
        print(f"  Post-spike lift: {scorecard['post_spike_lift']:.2f}x")

    # World model
    beliefs = db.execute("SELECT COUNT(*) as n FROM world_model WHERE status = 'active'").fetchone()
    print(f"\nWorld model: {beliefs['n']} active beliefs")
    top_beliefs = db.execute(
        "SELECT belief_type, belief, belief_confidence FROM world_model WHERE status = 'active' ORDER BY belief_confidence DESC LIMIT 5"
    ).fetchall()
    for b in top_beliefs:
        print(f"  [{b['belief_type']}] ({b['belief_confidence']:.2f}) {b['belief'][:80]}")

    # Constraints
    constraints = db.execute("SELECT COUNT(*) as n FROM learned_constraints WHERE active = 1").fetchone()
    compounds = db.execute("SELECT COUNT(*) as n FROM learned_constraints WHERE active = 1 AND is_compound = 1").fetchone()
    print(f"\nConstraints: {constraints['n']} active ({compounds['n']} compound)")

    # Experiments
    experiments = db.execute("SELECT * FROM knob_experiments ORDER BY ts DESC LIMIT 5").fetchall()
    if experiments:
        print(f"\nRecent experiments:")
        for e in experiments:
            pval = f"p={e['p_value']:.3f}" if e['p_value'] else "no stats"
            print(f"  #{e['id']} {e['knob_name']}: {e['old_value']:.3f}->{e['new_value']:.3f} "
                  f"[{e['status']}] {e['verdict'] or ''} ({pval})")

    # Cognitive state
    state = db.execute("SELECT * FROM cognitive_state ORDER BY id DESC LIMIT 1").fetchone()
    if state:
        print(f"\nCognitive state (cycle {state['cycle_number']}):")
        print(f"  {state['narrative']}")
        print(f"  Strategy: {state['current_strategy']} (duration: {state['strategy_duration_cycles']} cycles)")
        try:
            if state["should_pivot"]:
                print("  PIVOT RECOMMENDED")
        except (IndexError, KeyError):
            pass
        print(f"  Identity drift: {state['identity_drift_score']:.2f}")

    print("\n========================\n")
    db.close()


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARIA Cortex v2 - Intelligence Layer")
    parser.add_argument("--predict", action="store_true", help="Prediction + measurement only")
    parser.add_argument("--modify", action="store_true", help="Self-modification only")
    parser.add_argument("--mythos", action="store_true", help="Mythos/world model only")
    parser.add_argument("--spikes", action="store_true", help="Spike analysis only")
    parser.add_argument("--status", action="store_true", help="Print current state")
    parser.add_argument("--seed-mythos", action="store_true", help="Seed initial world model beliefs")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.predict:
        run_predict_only()
    elif args.modify:
        run_modify_only()
    elif args.mythos:
        run_mythos_only()
    elif args.spikes:
        run_spikes_only()
    elif args.seed_mythos:
        seed_mythos()
    else:
        run_full_cycle()
