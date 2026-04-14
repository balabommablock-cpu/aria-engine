#!/usr/bin/env python3
"""
Cortex Collector — Data collection layer for confounding variables.

Collects data that cortex needs but that doesn't exist in the ARIA DB:
  - Twitter account metrics (follower count, etc.)
  - Parent tweet metrics (for reply predictions)
  - Session state (Claude token usage)
  - External context (trending topics)

Usage:
    from cortex_collector import CortexCollector
    collector = CortexCollector(db)
    context = collector.collect_context(action_type="post_tweet")
    # Pass context to cortex_before_post(db, action_type, row, context)

Or standalone:
    python3 cortex_collector.py --snapshot     # take a snapshot now
    python3 cortex_collector.py --test         # test API connectivity
"""

import sqlite3
import json
import os
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict

IST = timezone(timedelta(hours=5, minutes=30))
WORKSPACE = Path(os.environ.get(
    "ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")
))

DB_PATH = WORKSPACE / "memory" / "aria.db"

# Session token tracking file — brain.py should write here after each Claude call
SESSION_STATE_FILE = WORKSPACE / "cortex" / "session_state.json"

log = logging.getLogger("collector")


class CortexCollector:
    """
    Collects confounding variables for cortex predictions.
    Degrades gracefully — returns defaults when data is unavailable.
    """

    def __init__(self, db: sqlite3.Connection, twitter_api=None):
        self.db = db
        self.twitter_api = twitter_api or self._init_twitter_api()
        self._account_cache = {}
        self._cache_ts = None
        self._schema_map = self._discover_schema()

    def collect_context(self, action_type: str = "post_tweet",
                        parent_tweet_id: str = None,
                        parent_tweet_data: dict = None) -> dict:
        """
        Collect all available confounders for a prediction.
        Returns a dict safe to pass as `context` to cortex_before_post().
        """
        context = {}

        # 1. Claude session state
        session = self._get_session_state()
        context["session_token_count"] = session.get("token_count", 0)
        context["candidates_generated_this_session"] = session.get("candidates_generated", 0)
        context["session_age_minutes"] = session.get("age_minutes", 0)
        context["generation_model"] = session.get("model", "unknown")

        # 2. Account metrics (cached for 1 hour)
        account = self._get_account_metrics()
        context["follower_count"] = account.get("followers_count", 0)
        context["following_count"] = account.get("following_count", 0)

        # 3. Reply context
        if action_type == "post_reply" and (parent_tweet_id or parent_tweet_data):
            parent = parent_tweet_data or self._get_tweet_metrics(parent_tweet_id)
            if parent:
                created_at = parent.get("created_at")
                if created_at:
                    try:
                        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        age_minutes = (datetime.now(IST) - created).total_seconds() / 60
                        context["parent_tweet_age_minutes"] = age_minutes
                    except (ValueError, TypeError):
                        pass

                metrics = parent.get("public_metrics", {})
                impressions = metrics.get("impression_count", 0)
                engagements = (metrics.get("like_count", 0) +
                              metrics.get("retweet_count", 0) +
                              metrics.get("reply_count", 0))
                context["parent_tweet_impressions"] = impressions

                age = context.get("parent_tweet_age_minutes", 1)
                if age > 0:
                    context["parent_tweet_velocity"] = engagements / max(age, 1)

                context["parent_author_follower_count"] = parent.get(
                    "author_followers", 0
                )

        # 4. Trending topics overlap (optional, expensive)
        context["trending_topics_overlap"] = 0  # default

        return context

    def get_lookup_columns(self) -> dict:
        """
        Returns the actual column names for tables that _lookup_actual() needs.
        Use this to adapt to the real schema.
        """
        return self._schema_map

    # --- Session state ---

    def _get_session_state(self) -> dict:
        """
        Read session state from file. brain.py writes this after each Claude call.
        Format: {"token_count": 150000, "candidates_generated": 12, "started_at": "...", "model": "claude-sonnet-4-20250514"}
        """
        if SESSION_STATE_FILE.exists():
            try:
                state = json.loads(SESSION_STATE_FILE.read_text())
                started = state.get("started_at", "")
                if started:
                    try:
                        start_dt = datetime.fromisoformat(started)
                        age = (datetime.now(IST) - start_dt).total_seconds() / 60
                        state["age_minutes"] = age
                    except (ValueError, TypeError):
                        state["age_minutes"] = 0
                return state
            except (json.JSONDecodeError, OSError) as e:
                log.debug("Could not read session state: %s", e)

        # Fallback: estimate from recent prediction activity
        try:
            recent = self.db.execute(
                """SELECT COUNT(*) as n FROM predictions
                   WHERE ts > datetime('now', '-2 hours')"""
            ).fetchone()["n"]
            return {
                "token_count": recent * 5000,  # rough estimate
                "candidates_generated": recent,
                "age_minutes": 120 if recent > 0 else 0,
                "model": "unknown",
            }
        except Exception:
            return {"token_count": 0, "candidates_generated": 0, "age_minutes": 0, "model": "unknown"}

    @staticmethod
    def write_session_state(token_count: int, candidates_generated: int, model: str):
        """
        Call this from brain.py after each Claude API call.
        Tracks cumulative tokens for the current session.

        Example in brain.py:
            from cortex_collector import CortexCollector
            CortexCollector.write_session_state(
                token_count=response.usage.input_tokens + response.usage.output_tokens,
                candidates_generated=len(candidates),
                model="claude-sonnet-4-20250514"
            )
        """
        SESSION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        existing = {}
        if SESSION_STATE_FILE.exists():
            try:
                existing = json.loads(SESSION_STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        # Accumulate tokens within session (reset if started_at is > 2 hours ago)
        started_at = existing.get("started_at", "")
        reset = False
        if started_at:
            try:
                start_dt = datetime.fromisoformat(started_at)
                if (datetime.now(IST) - start_dt).total_seconds() > 7200:
                    reset = True
            except (ValueError, TypeError):
                reset = True
        else:
            reset = True

        if reset:
            state = {
                "token_count": token_count,
                "candidates_generated": candidates_generated,
                "started_at": datetime.now(IST).isoformat(),
                "model": model,
            }
        else:
            state = {
                "token_count": existing.get("token_count", 0) + token_count,
                "candidates_generated": existing.get("candidates_generated", 0) + candidates_generated,
                "started_at": existing.get("started_at"),
                "model": model,
            }

        SESSION_STATE_FILE.write_text(json.dumps(state, indent=2))

    @staticmethod
    def reset_session():
        """Call when starting a new Claude Code session."""
        if SESSION_STATE_FILE.exists():
            SESSION_STATE_FILE.unlink()

    # --- Twitter API ---

    def _init_twitter_api(self):
        """
        Initialize Twitter API client. Looks for bearer token in ARIA's auth config.
        Returns None if not available (collector degrades gracefully).
        """
        auth_paths = [
            WORKSPACE / "auth" / "twitter.json",
            WORKSPACE / "config" / "twitter.json",
            Path(os.path.expanduser("~/.openclaw/agents/aria/auth.json")),
        ]

        bearer_token = os.environ.get("TWITTER_BEARER_TOKEN")

        if not bearer_token:
            for path in auth_paths:
                if path.exists():
                    try:
                        auth = json.loads(path.read_text())
                        bearer_token = auth.get("bearer_token") or auth.get("BEARER_TOKEN")
                        if bearer_token:
                            break
                    except (json.JSONDecodeError, OSError):
                        continue

        if not bearer_token:
            log.debug("No Twitter bearer token found — account metrics will use DB fallback")
            return None

        return {"bearer_token": bearer_token}

    def _get_account_metrics(self) -> dict:
        """Get account follower/following counts. Cached for 1 hour."""
        now = datetime.now(IST)
        if self._cache_ts and (now - self._cache_ts).total_seconds() < 3600:
            return self._account_cache

        # Try Twitter API
        if self.twitter_api:
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://api.twitter.com/2/users/me?user.fields=public_metrics",
                    headers={
                        "Authorization": f"Bearer {self.twitter_api['bearer_token']}",
                    }
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    metrics = data.get("data", {}).get("public_metrics", {})
                    self._account_cache = {
                        "followers_count": metrics.get("followers_count", 0),
                        "following_count": metrics.get("following_count", 0),
                        "tweet_count": metrics.get("tweet_count", 0),
                    }
                    self._cache_ts = now
                    return self._account_cache
            except Exception as e:
                log.debug("Twitter API failed: %s", e)

        # Fallback: last known from confounder_snapshots
        try:
            row = self.db.execute(
                """SELECT follower_count, following_count FROM confounder_snapshots
                   WHERE follower_count > 0 ORDER BY ts DESC LIMIT 1"""
            ).fetchone()
            if row:
                self._account_cache = {
                    "followers_count": row["follower_count"],
                    "following_count": row["following_count"],
                }
                self._cache_ts = now
                return self._account_cache
        except Exception:
            pass

        return {"followers_count": 0, "following_count": 0}

    def _get_tweet_metrics(self, tweet_id: str) -> Optional[dict]:
        """Fetch a specific tweet's metrics from Twitter API."""
        if not self.twitter_api or not tweet_id:
            return None

        try:
            import urllib.request
            url = (f"https://api.twitter.com/2/tweets/{tweet_id}"
                   f"?tweet.fields=public_metrics,created_at,author_id"
                   f"&expansions=author_id&user.fields=public_metrics")
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.twitter_api['bearer_token']}",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                tweet = data.get("data", {})

                # Get author follower count from includes
                author_followers = 0
                includes = data.get("includes", {})
                users = includes.get("users", [])
                if users:
                    author_followers = users[0].get("public_metrics", {}).get("followers_count", 0)

                tweet["author_followers"] = author_followers
                return tweet
        except Exception as e:
            log.debug("Failed to fetch tweet %s: %s", tweet_id, e)
            return None

    # --- Schema discovery ---

    def _discover_schema(self) -> dict:
        """
        Discover actual column names for tables that cortex reads from.
        ARIA schema: posted has no metrics columns. Metrics live in separate `metrics` table.
        """
        schema = {
            "metrics": {"impressions_col": "impressions", "likes_col": "likes",
                        "retweets_col": "retweets", "replies_col": "replies",
                        "bookmarks_col": "bookmarks", "post_id_col": "post_id"},
            "posted": {"id_col": "id", "text_col": "text", "territory_col": "territory"},
            "queue": {"text_col": "text", "territory_col": "territory"},
            "engagements": {"action_col": "action"},
        }

        for table_name in ["metrics", "posted", "queue", "engagements"]:
            try:
                cols = {c[1] for c in self.db.execute(f"PRAGMA table_info({table_name})").fetchall()}

                if table_name == "metrics":
                    for candidate in ["impressions", "views", "view_count"]:
                        if candidate in cols:
                            schema["metrics"]["impressions_col"] = candidate
                            break

                    for candidate in ["likes", "like_count", "favorites"]:
                        if candidate in cols:
                            schema["metrics"]["likes_col"] = candidate
                            break

                    for candidate in ["retweets", "retweet_count", "rt_count"]:
                        if candidate in cols:
                            schema["metrics"]["retweets_col"] = candidate
                            break

                    for candidate in ["replies", "reply_count"]:
                        if candidate in cols:
                            schema["metrics"]["replies_col"] = candidate
                            break

            except sqlite3.OperationalError:
                log.debug("Table %s does not exist", table_name)

        return schema

    def build_lookup_query(self, action_type: str) -> str:
        """
        Build the actual SQL query for _lookup_actual() based on discovered schema.
        ARIA schema: metrics are in separate `metrics` table, not in `posted`.
        """
        if action_type == "post_tweet":
            s = self._schema_map.get("metrics", {})
            imp_col = s.get("impressions_col", "impressions")
            likes_col = s.get("likes_col", "likes")
            rt_col = s.get("retweets_col", "retweets")
            replies_col = s.get("replies_col", "replies")
            return f"""SELECT m.{imp_col} as impressions,
                              m.{likes_col} as likes,
                              m.{rt_col} as retweets,
                              m.{replies_col} as replies
                       FROM metrics m
                       WHERE m.post_id = ?
                       ORDER BY m.scraped_at DESC LIMIT 1"""
        elif action_type == "post_reply":
            s = self._schema_map.get("metrics", {})
            imp_col = s.get("impressions_col", "impressions")
            likes_col = s.get("likes_col", "likes")
            return f"""SELECT m.{imp_col} as impressions,
                              m.{likes_col} as likes
                       FROM metrics m
                       WHERE m.post_id = ?
                       ORDER BY m.scraped_at DESC LIMIT 1"""
        return ""


# ---------------------------------------------------------------------------
# Standalone operations
# ---------------------------------------------------------------------------

def take_snapshot():
    """Take a confounder snapshot and print it."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    collector = CortexCollector(db)
    context = collector.collect_context("post_tweet")
    print(json.dumps(context, indent=2))
    db.close()

def test_connectivity():
    """Test Twitter API connectivity."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    collector = CortexCollector(db)

    print("Testing Twitter API...")
    if collector.twitter_api:
        metrics = collector._get_account_metrics()
        if metrics.get("followers_count", 0) > 0:
            print(f"  OK: {metrics['followers_count']} followers, {metrics['following_count']} following")
        else:
            print(f"  WARN: API connected but returned empty metrics: {metrics}")
    else:
        print("  SKIP: No bearer token found")

    print("\nTesting session state...")
    session = collector._get_session_state()
    print(f"  Session: {json.dumps(session, indent=2)}")

    print("\nSchema discovery...")
    schema = collector._schema_map
    for table, cols in schema.items():
        print(f"  {table}: {cols}")

    db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Cortex Collector")
    parser.add_argument("--snapshot", action="store_true", help="Take a confounder snapshot")
    parser.add_argument("--test", action="store_true", help="Test API connectivity")
    args = parser.parse_args()

    if args.test:
        test_connectivity()
    elif args.snapshot:
        take_snapshot()
    else:
        parser.print_help()
