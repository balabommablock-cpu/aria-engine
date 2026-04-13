#!/usr/bin/env python3
"""
aria-shared.py -- Shared utilities for Brain + Hands processes.
Database, logging, voice config, time helpers, lock management.
"""

from __future__ import annotations
import json, os, sys, sqlite3, fcntl, time, random, hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

# ============================================================
# PATHS
# ============================================================

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")))

DB_PATH        = WORKSPACE / "memory" / "aria.db"
VOICE_PATH     = WORKSPACE / "voice.json"
TARGETS_PATH   = WORKSPACE / "memory" / "target-handles.json"
LOG_DIR        = WORKSPACE / "logs"

CLAUDE_CLI     = os.environ.get("CLAUDE_CLI",
    os.path.expanduser("~/.local/bin/claude"))
CDP_URL        = os.environ.get("CDP_URL", "http://127.0.0.1:28800")
CDP_PORT       = 28800
X_USERNAME     = os.environ.get("X_USERNAME", "BalabommaRao")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN",
    "8794849679:AAGUiW5aIKeGzChVeSOMzpMFeqw0g-gGqII")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "7353580848")

POST_TWEET_JS  = Path(os.path.expanduser(
    "~/.openclaw/workspace/skills/x-twitter-poster/post_tweet.js"))

DRY_RUN = "--dry-run" in sys.argv

# Handles that consistently fail (can't find tweets, subscription-locked, etc.)
HANDLE_BLACKLIST = {"johncutlefish"}


# ============================================================
# DATABASE
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    territory   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN ('queued','posting','expired','posted')),
    scores_json TEXT,
    image_type  TEXT DEFAULT 'none',
    image_path  TEXT,
    generated_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    generator   TEXT DEFAULT 'claude-opus'
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
CREATE INDEX IF NOT EXISTS idx_queue_expires ON queue(expires_at);

CREATE TABLE IF NOT EXISTS posted (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    territory   TEXT,
    scores_json TEXT,
    image_type  TEXT DEFAULT 'none',
    tweet_url   TEXT,
    posted_at   TEXT NOT NULL,
    self_reply_text TEXT,
    self_replied INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posted_at ON posted(posted_at);

CREATE TABLE IF NOT EXISTS signals (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    territory   TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    scraped_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_scraped ON signals(scraped_at);

CREATE TABLE IF NOT EXISTS reply_targets (
    handle      TEXT PRIMARY KEY,
    priority    INTEGER NOT NULL DEFAULT 2,
    territory   TEXT,
    themes_json TEXT,
    author_context TEXT,
    last_replied_at TEXT,
    reply_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reply_drafts (
    id          TEXT PRIMARY KEY,
    target_handle TEXT NOT NULL,
    target_tweet_url TEXT NOT NULL,
    target_tweet_text TEXT NOT NULL,
    reply_text  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ready'
                CHECK(status IN ('ready','posting','posted','failed','expired')),
    score       REAL DEFAULT 0,
    generated_at TEXT NOT NULL,
    posted_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_reply_drafts_status ON reply_drafts(status);

CREATE TABLE IF NOT EXISTS engagements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    post_id     TEXT,
    target_handle TEXT,
    target_tweet_url TEXT,
    text        TEXT,
    performed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    post_id     TEXT NOT NULL,
    scraped_at  TEXT NOT NULL,
    impressions INTEGER DEFAULT 0,
    likes       INTEGER DEFAULT 0,
    replies     INTEGER DEFAULT 0,
    retweets    INTEGER DEFAULT 0,
    bookmarks   INTEGER DEFAULT 0,
    PRIMARY KEY (post_id, scraped_at)
);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS engine_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    process     TEXT NOT NULL,
    level       TEXT DEFAULT 'info',
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON engine_log(ts);
"""


def get_db() -> sqlite3.Connection:
    """Open DB with WAL mode, 5s busy timeout."""
    db = sqlite3.connect(str(DB_PATH), timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = sqlite3.Row
    return db


def init_db():
    """Create all tables if they don't exist."""
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
    db.close()


# ============================================================
# LOGGING
# ============================================================

def log(msg: str, process: str = "engine", level: str = "info"):
    """Log to both file and DB."""
    ts = now_utc().isoformat()
    line = f"[{ts[:19]}] {msg}"
    print(line, flush=True)

    log_file = LOG_DIR / f"{process}.log"
    try:
        with open(log_file, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

    try:
        db = get_db()
        db.execute(
            "INSERT INTO engine_log (ts, process, level, message) VALUES (?,?,?,?)",
            (ts, process, level, msg)
        )
        # Trim log to last 5000 entries
        db.execute("""
            DELETE FROM engine_log WHERE id NOT IN (
                SELECT id FROM engine_log ORDER BY id DESC LIMIT 5000
            )
        """)
        db.commit()
        db.close()
    except Exception:
        pass


def log_brain(msg: str, level: str = "info"):
    log(msg, process="brain", level=level)


def log_hands(msg: str, level: str = "info"):
    log(msg, process="hands", level=level)


# ============================================================
# STATE
# ============================================================

def get_state(db, key: str, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(db, key: str, value: str):
    db.execute(
        "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?,?,?)",
        (key, value, now_utc().isoformat())
    )
    db.commit()


# ============================================================
# VOICE
# ============================================================

_voice_cache = None

def load_voice() -> dict:
    global _voice_cache
    if _voice_cache is None:
        with open(VOICE_PATH) as f:
            _voice_cache = json.load(f)
    return _voice_cache


# ============================================================
# TIME HELPERS
# ============================================================

IST = timezone(timedelta(hours=5, minutes=30))

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_ist() -> datetime:
    return datetime.now(IST)

def parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

def ts_age_hours(ts_str: str) -> float:
    ts = parse_ts(ts_str)
    if not ts:
        return 999
    return (now_utc() - ts).total_seconds() / 3600


# ============================================================
# LOCK
# ============================================================

_lock_fd = None

def acquire_lock(lock_name: str) -> bool:
    """Non-blocking flock. Returns False if already held."""
    global _lock_fd
    lock_path = WORKSPACE / "locks" / f"{lock_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _lock_fd = open(lock_path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(f"{os.getpid()}\n{now_utc().isoformat()}\n")
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        return False


def release_lock():
    global _lock_fd
    if _lock_fd:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None


# ============================================================
# CLAUDE CLI
# ============================================================

import subprocess

def call_claude(prompt: str, max_retries: int = 2) -> str | None:
    """Call Claude Opus 4.6 via CLI. For creative/intelligent tasks.
    Returns text or None on failure."""
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                [CLAUDE_CLI, "-p", "--model", "opus"],
                input=prompt,
                capture_output=True, text=True, timeout=180
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            if attempt < max_retries:
                time.sleep(5)
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                time.sleep(5)
        except Exception as e:
            log(f"claude error: {e}", level="error")
            break
    return None


def call_gemma(prompt: str, timeout: int = 60) -> str | None:
    """Call Gemma 4 26B via ollama. For mechanical tasks that need no intelligence.
    Use ONLY for: summarization of structured data, simple classification,
    text reformatting. NEVER for creative generation or voice-sensitive content."""
    try:
        result = subprocess.run(
            ["ollama", "run", "gemma4:26b"],
            input=prompt,
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log("gemma timeout", level="error")
    except Exception as e:
        log(f"gemma error: {e}", level="error")
    return None


# ============================================================
# TELEGRAM
# ============================================================

from urllib import request as urllib_request

def send_telegram(message: str):
    """Send HTML message via Telegram bot."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }).encode()
        req = urllib_request.Request(url, data=data,
            headers={"Content-Type": "application/json"})
        urllib_request.urlopen(req, timeout=10)
    except Exception:
        pass


# ============================================================
# HELPERS
# ============================================================

def make_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:10]


def in_posting_window(voice: dict) -> tuple[bool, str]:
    """Check if current IST time is within a posting window.
    Returns (in_window, window_name_or_next)."""
    ist = now_ist()
    current_time = ist.strftime("%H:%M")
    windows = voice.get("timing", {}).get("windows_ist", [])

    for w in windows:
        if w["start"] <= current_time <= w["end"]:
            return True, w["name"]

    next_w = [w for w in windows if w["start"] > current_time]
    if next_w:
        return False, f"next: {next_w[0]['name']} at {next_w[0]['start']}"
    return False, "past all windows today"


def gap_ok(db, voice: dict) -> bool:
    """Check if enough time has passed since last post."""
    min_gap = voice.get("timing", {}).get("min_gap_hours", 2)
    row = db.execute(
        "SELECT posted_at FROM posted ORDER BY posted_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return True
    gap_h = ts_age_hours(row["posted_at"])
    return gap_h >= min_gap
