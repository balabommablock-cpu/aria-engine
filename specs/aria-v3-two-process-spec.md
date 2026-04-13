# ARIA v3: Two-Process Architecture Specification

## 1. SQLite Schema

Database: `~/.openclaw/agents/aria/workspace/memory/aria.db`

```sql
-- Tweet queue: brain writes, hands reads+updates
CREATE TABLE queue (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    territory   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','posting','expired','posted')),
    scores_json TEXT,  -- {"provocation":7,"bookmark":8,"hook":6,"length_bonus":1.5,"composite":28.5}
    image_type  TEXT DEFAULT 'none',
    generated_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,  -- generated_at + 48h
    generator   TEXT DEFAULT 'claude-opus'
);
CREATE INDEX idx_queue_status ON queue(status);
CREATE INDEX idx_queue_expires ON queue(expires_at);

-- Posted tweets: hands writes, brain reads for context
CREATE TABLE posted (
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
CREATE INDEX idx_posted_at ON posted(posted_at);

-- RSS signals: brain writes+reads
CREATE TABLE signals (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    territory   TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT,
    scraped_at  TEXT NOT NULL
);
CREATE INDEX idx_signals_scraped ON signals(scraped_at);

-- Reply targets: loaded from target-handles.json, brain manages
CREATE TABLE reply_targets (
    handle      TEXT PRIMARY KEY,
    priority    INTEGER NOT NULL DEFAULT 2,
    themes_json TEXT,
    author_context TEXT,
    last_replied_at TEXT,
    reply_count INTEGER DEFAULT 0
);

-- Reply drafts: brain writes, hands reads+updates
CREATE TABLE reply_drafts (
    id          TEXT PRIMARY KEY,
    target_handle TEXT NOT NULL REFERENCES reply_targets(handle),
    target_tweet_url TEXT NOT NULL,
    target_tweet_text TEXT NOT NULL,
    reply_text  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ready' CHECK(status IN ('ready','posting','posted','failed','expired')),
    score       REAL DEFAULT 0,
    generated_at TEXT NOT NULL,
    posted_at   TEXT
);
CREATE INDEX idx_reply_drafts_status ON reply_drafts(status);

-- Engagement log: hands writes
CREATE TABLE engagements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,  -- 'self_reply','outbound_reply','like','bookmark'
    post_id     TEXT,
    target_handle TEXT,
    target_tweet_url TEXT,
    text        TEXT,
    performed_at TEXT NOT NULL
);

-- Metrics: hands writes, brain reads
CREATE TABLE metrics (
    post_id     TEXT NOT NULL REFERENCES posted(id),
    scraped_at  TEXT NOT NULL,
    impressions INTEGER DEFAULT 0,
    likes       INTEGER DEFAULT 0,
    replies     INTEGER DEFAULT 0,
    retweets    INTEGER DEFAULT 0,
    bookmarks   INTEGER DEFAULT 0,
    PRIMARY KEY (post_id, scraped_at)
);

-- Key-value state for both processes
CREATE TABLE state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

-- Structured log for both processes
CREATE TABLE engine_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    process     TEXT NOT NULL,  -- 'brain' or 'hands'
    level       TEXT DEFAULT 'info',
    message     TEXT NOT NULL
);
CREATE INDEX idx_log_ts ON engine_log(ts);
```

## 2. Brain Process (aria-brain.py)

Runs every 30 min via launchd. Zero CDP dependency.

```python
# --- Config ---
DB_PATH = WORKSPACE / "memory" / "aria.db"
LOCK_PATH = WORKSPACE / "locks" / "brain.lock"
VOICE_PATH = WORKSPACE / "voice.json"
TARGETS_PATH = WORKSPACE / "memory" / "target-handles.json"

# --- Functions ---

def acquire_lock() -> bool:
    """flock on brain.lock, non-blocking. Returns False if already running."""

def get_db() -> sqlite3.Connection:
    """Open DB with WAL mode, 5s busy_timeout."""

def log_db(db, msg: str, level: str = "info"):
    """INSERT into engine_log with process='brain'."""

def load_voice() -> dict:
    """Read voice.json. Cached per cycle."""

def jitter_sleep():
    """Sleep random 15-90s on startup (anti-pattern detection)."""

# -- Signals --
def refresh_signals(db):
    """Fetch RSS_FEEDS. Insert new rows into signals. Cap at 200 rows (delete oldest). 
    Skip if last refresh < 3h (check state table key 'brain.last_signal_at')."""

# -- Generation --
def count_queued(db) -> int:
    """SELECT COUNT(*) FROM queue WHERE status='queued' AND expires_at > now."""

def expire_stale_candidates(db):
    """UPDATE queue SET status='expired' WHERE expires_at < now AND status='queued'."""

def pick_territories(voice: dict) -> list[str]:
    """Weighted random pick of 4 territories. Ensure >=2 unique. 
    Check posted table last 7 days for variety compliance."""

def build_generation_prompt(voice: dict, territories: list, signals: list, avoid_texts: list) -> str:
    """Construct the Claude prompt. Includes: golden tweets, territory prompts, 
    recent signals, avoid list, structure rules, hard bans. 
    Identical logic to current phase_generate prompt."""

def call_claude(prompt: str) -> str | None:
    """subprocess.run claude CLI with -p --model opus. 120s timeout."""

def parse_batch_response(raw: str, voice: dict) -> list[dict]:
    """Parse ---delimited blocks. For each: extract text+scores, enforce hard bans 
    (character strip, word/phrase reject), force lowercase, length check 30-280, 
    dedup against avoid list, compute composite score. Return list of candidate dicts."""

def generate_tweets(db, voice: dict):
    """If count_queued < 3: expire stale, pick territories, build prompt, 
    call claude, parse response, INSERT passing candidates into queue."""

# -- Reply Drafts --
def load_target_handles(db):
    """Read target-handles.json, UPSERT into reply_targets table."""

def pick_reply_target(db) -> dict | None:
    """Pick one target handle where:
    - priority 1 first, then 2, then 3
    - last_replied_at is oldest (or NULL)
    - at least 4h since last reply to same handle
    Returns handle row or None."""

def fetch_target_recent_tweet(handle: str) -> dict | None:
    """Use RSS or nitter/public API to get latest tweet text+url for handle. 
    NO CDP. Falls back to None if unavailable.
    For v3 cold start: hardcode nitter.net/{handle} RSS as source."""

def build_reply_prompt(voice: dict, target: dict, tweet_text: str) -> str:
    """Prompt Claude to write a contextual reply. Uses: author_context from target, 
    the tweet text, golden tweets for voice, reply_style from voice.engage. 
    Rules: lowercase, no bans, max 200 chars, must add genuine insight not flattery."""

def generate_reply_draft(db, voice: dict):
    """If fewer than 3 ready reply_drafts: pick_reply_target, fetch their tweet, 
    build prompt, call claude, INSERT into reply_drafts.
    Skip if no targets available or all handles on cooldown."""

# -- Main --
def main():
    jitter_sleep()
    if not acquire_lock(): sys.exit(0)
    db = get_db()
    voice = load_voice()
    expire_stale_candidates(db)
    refresh_signals(db)
    generate_tweets(db, voice)
    load_target_handles(db)
    generate_reply_draft(db, voice)
    db.close()
```

Quality gates: composite >= 22 (from `voice.algo_scoring.min_composite_to_queue`). Hard ban enforcement is character-level strip + word/phrase-level reject. Variety enforced via `pick_territories` checking 7-day posted history.

## 3. Hands Process (aria-hands.py)

Runs every 10 min via launchd. CDP only. Exactly ONE action per cycle.

```python
LOCK_PATH = WORKSPACE / "locks" / "hands.lock"

def acquire_lock() -> bool:
    """flock on hands.lock, non-blocking."""

def get_db() -> sqlite3.Connection:
def log_db(db, msg: str, level: str = "info"):
def load_voice() -> dict:
def now_ist() -> datetime:
def in_posting_window(voice: dict) -> bool:
def gap_ok(db, voice: dict) -> bool:
    """Check min_gap_hours since last posted_at."""

# -- CDP Actions --
def do_post_tweet(text: str, image_path: str = None) -> tuple[bool, str]:
    """Call post_tweet.js via node. Returns (success, tweet_url)."""

def do_post_reply(tweet_url: str, reply_text: str) -> bool:
    """Navigate to tweet_url, click reply, type text, submit. 
    Uses existing _post_reply.js inline CDP logic."""

def do_scrape_metrics(db) -> bool:
    """Navigate to own profile analytics. Scrape impressions/likes/replies 
    for posts in last 7 days. INSERT into metrics table."""

def do_like_tweet(tweet_url: str) -> bool:
    """Navigate, click heart. For humanization."""

def do_bookmark_tweet(tweet_url: str) -> bool:
    """Navigate, click bookmark. For humanization."""

# -- Priority Picker --
def pick_action(db, voice: dict) -> tuple[str, dict]:
    """Returns (action_type, context_dict). Priority order:
    
    P1: 'post_tweet' -- if in_posting_window AND gap_ok AND queue has status='queued'
        context = best candidate by composite score
    P2: 'post_reply' -- if reply_drafts has status='ready'
        context = oldest ready draft
    P3: 'self_reply' -- if most recent posted row has self_replied=0 AND age < 30min
        context = posted row (generates reply text inline via call_claude)
    P4: 'scrape_metrics' -- if state key 'hands.last_metrics_at' > 4h stale
        context = {}
    P5: 'passive_action' -- random like or bookmark on a target account's tweet
        context = {tweet_url from reply_targets recent tweet cache}
    
    Returns ('idle', {}) if nothing to do.
    """

def execute_action(db, voice: dict, action: str, ctx: dict):
    """Switch on action type, execute, update DB:
    
    post_tweet: 
        UPDATE queue SET status='posting'. Anti-detect delay 30-180s.
        do_post_tweet. On success: INSERT into posted, UPDATE queue status='posted'. 
        Telegram notify. On fail: revert status='queued', increment fail counter.
    
    post_reply:
        UPDATE reply_drafts SET status='posting'. Delay 20-90s.
        do_post_reply. On success: status='posted', UPDATE reply_targets.last_replied_at.
        INSERT into engagements. On fail: status='failed'.
    
    self_reply:
        call_claude for reply text (no CDP yet). Delay 90-300s.
        do_post_reply. UPDATE posted.self_replied=1, self_reply_text=text.
        INSERT into engagements.
    
    scrape_metrics:
        do_scrape_metrics. UPDATE state 'hands.last_metrics_at'.
    
    passive_action:
        do_like_tweet or do_bookmark_tweet. INSERT into engagements.
    """

def main():
    if not acquire_lock(): sys.exit(0)
    db = get_db()
    voice = load_voice()
    action, ctx = pick_action(db, voice)
    log_db(db, f"action={action}")
    if action != 'idle':
        execute_action(db, voice, action, ctx)
    db.close()
```

No startup jitter for hands -- the 30-180s pre-post delay inside execute_action handles anti-detection.

## 4. Reply Target System

Targets come from `/workspace/memory/target-handles.json` (19 handles, already exists). Brain loads this file into `reply_targets` table via UPSERT on each cycle.

**How Brain picks which tweet to reply to:** `fetch_target_recent_tweet` hits a public RSS proxy (nitter RSS or similar no-auth endpoint). No CDP needed. If unavailable, skip that handle this cycle.

**Contextual reply generation:** The prompt includes `author_context` from target-handles.json (describes the target's voice, themes, audience fit), the actual tweet text, golden tweets for voice calibration, and `voice.engage.reply_style` ("extend the observation with a concrete example or second angle").

**Queue limits:** Max 3 ready reply_drafts at any time. Brain skips generation if 3 exist.

**Cooldown:** 4h minimum between replies to the same handle (`reply_targets.last_replied_at`). Priority 1 handles get checked first, then 2, then 3. Within same priority, oldest `last_replied_at` wins.

## 5. Cold-Start Discovery

**Hashtags:** Not used. `voice.hard_bans.characters` includes `#`. The account grows via reply-to-target strategy (outbound replies to 19 high-signal accounts) and organic impressions from quality content.

**@mention strategy:** Also banned in original tweets (`voice.hard_bans.patterns` includes `@\w+`). Growth comes from replies appearing in target accounts' reply threads, not from tagging.

**Profile optimization checklist:**
- Bio: matches voice territories (building, orgs, ai, taste). No credentials.
- Pinned tweet: best golden tweet (g11: "the most dangerous person in tech right now...")
- Display name: matches handle style, lowercase
- Header/avatar: set manually before engine starts

## 6. Launchd Plists

**com.aria.brain.plist:**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.aria.brain</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/boredfolio/.openclaw/agents/aria/workspace/scripts/aria-brain.py</string>
    </array>
    <key>StartInterval</key><integer>1800</integer>
    <key>StandardOutPath</key><string>/Users/boredfolio/.openclaw/agents/aria/workspace/logs/brain-stdout.log</string>
    <key>StandardErrorPath</key><string>/Users/boredfolio/.openclaw/agents/aria/workspace/logs/brain-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ARIA_WORKSPACE</key><string>/Users/boredfolio/.openclaw/agents/aria/workspace</string>
        <key>CLAUDE_CLI</key><string>/Users/boredfolio/.local/bin/claude</string>
        <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/Users/boredfolio/.local/bin</string>
    </dict>
</dict>
</plist>
```

**com.aria.hands.plist:** Same structure, `StartInterval` = 600, script = `aria-hands.py`. Additional env vars:
```
CDP_URL = http://127.0.0.1:28800
X_USERNAME = BalabommaRao
```

Install: `cp *.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.aria.brain.plist && launchctl load ~/Library/LaunchAgents/com.aria.hands.plist`

## 7. Migration Plan

**Step 1: Create DB + migrate data (one-time script: `migrate-to-sqlite.py`)**
- Create `aria.db` with schema above
- Read `queue.jsonl` -> INSERT into `queue` (status='queued' rows only)
- Read `posted.jsonl` -> INSERT into `posted`
- Read `signals.jsonl` -> INSERT into `signals`
- Read `engagements.jsonl` -> INSERT into `engagements`
- Read `target-handles.json` -> INSERT into `reply_targets`
- Read `engine_state.json` -> INSERT key/value pairs into `state`

**Step 2: Deploy new scripts**
- Write `aria-brain.py` and `aria-hands.py` to scripts/
- Test both with `--dry-run` flag

**Step 3: Cut over**
- `launchctl unload` all old cron jobs (27 of them per memory notes)
- `launchctl load` the two new plists
- Rename `aria-engine.py` to `aria-engine.py.v2-retired`

**Step 4: Verify**
- Watch `brain-stdout.log` for one cycle (signals + generation)
- Watch `hands-stdout.log` for one cycle (should pick post_tweet or idle)
- Check `aria.db` tables have data via `sqlite3 aria.db ".tables"`

Old JSONL files stay in place as backup. No deletion until 7 days of clean v3 operation.
