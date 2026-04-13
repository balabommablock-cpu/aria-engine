#!/usr/bin/env python3
"""
migrate-to-sqlite.py -- One-time migration from JSONL to SQLite.
Reads all existing JSONL files and imports into aria.db.
"""

import json, sys, os
sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE",
    os.path.expanduser("~/.openclaw/agents/aria/workspace")))

# Import shared module
from importlib.util import spec_from_file_location, module_from_spec
spec = spec_from_file_location("shared", WORKSPACE / "scripts" / "aria-shared.py")
shared = module_from_spec(spec)
spec.loader.exec_module(shared)


def load_jsonl(path):
    """Load JSONL file, skip bad lines."""
    items = []
    if not path.exists():
        return items
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  SKIP bad line in {path.name}")
    return items


def migrate():
    print("=" * 50)
    print("ARIA JSONL -> SQLite Migration")
    print("=" * 50)

    # Init DB
    shared.init_db()
    db = shared.get_db()

    # 1. Queue
    queue_path = WORKSPACE / "memory" / "queue.jsonl"
    items = load_jsonl(queue_path)
    count = 0
    for item in items:
        if item.get("status") != "queued":
            continue
        try:
            from datetime import datetime, timezone, timedelta
            gen_at = item.get("generated_at", shared.now_utc().isoformat())
            ts = shared.parse_ts(gen_at)
            expires = (ts + timedelta(hours=48)).isoformat() if ts else ""

            db.execute(
                """INSERT OR IGNORE INTO queue
                   (id, text, territory, status, scores_json, image_type, generated_at, expires_at, generator)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    item.get("id", shared.make_id(item.get("text", ""))),
                    item["text"],
                    item.get("territory", "building"),
                    "queued",
                    json.dumps(item.get("scores", {})),
                    item.get("image_type", "none"),
                    gen_at,
                    expires,
                    item.get("generator", "claude-opus")
                )
            )
            count += 1
        except Exception as e:
            print(f"  SKIP queue item: {e}")
    db.commit()
    print(f"queue: {count} candidates imported")

    # 2. Posted
    posted_path = WORKSPACE / "memory" / "posted.jsonl"
    items = load_jsonl(posted_path)
    count = 0
    for item in items:
        try:
            db.execute(
                """INSERT OR IGNORE INTO posted
                   (id, text, territory, scores_json, image_type, tweet_url, posted_at, self_reply_text, self_replied)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    item.get("id", shared.make_id(item.get("text", ""))),
                    item["text"],
                    item.get("territory"),
                    json.dumps(item.get("scores", {})),
                    item.get("image_type", "none"),
                    item.get("tweet_url"),
                    item.get("posted_at", shared.now_utc().isoformat()),
                    item.get("self_reply_text"),
                    1 if item.get("self_replied") else 0
                )
            )
            count += 1
        except Exception as e:
            print(f"  SKIP posted item: {e}")
    db.commit()
    print(f"posted: {count} tweets imported")

    # 3. Signals
    signals_path = WORKSPACE / "memory" / "signals.jsonl"
    items = load_jsonl(signals_path)
    count = 0
    for item in items:
        try:
            db.execute(
                """INSERT OR IGNORE INTO signals
                   (id, source, territory, title, url, scraped_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    item.get("id", shared.make_id(item.get("title", ""))),
                    item.get("source", "rss"),
                    item.get("territory", "building"),
                    item.get("title", ""),
                    item.get("url"),
                    item.get("scraped_at", shared.now_utc().isoformat())
                )
            )
            count += 1
        except Exception as e:
            pass  # Dupes expected
    db.commit()
    print(f"signals: {count} entries imported")

    # 4. Target handles
    targets_path = WORKSPACE / "memory" / "target-handles.json"
    if targets_path.exists():
        with open(targets_path) as f:
            targets = json.load(f)

        if isinstance(targets, list):
            target_list = targets
        elif isinstance(targets, dict):
            target_list = targets.get("handles", targets.get("targets", []))
        else:
            target_list = []

        count = 0
        for t in target_list:
            handle = t.get("handle", t.get("username", "")).lstrip("@")
            if not handle:
                continue
            try:
                db.execute(
                    """INSERT OR REPLACE INTO reply_targets
                       (handle, priority, territory, themes_json, author_context, reply_count)
                       VALUES (?,?,?,?,?,0)""",
                    (
                        handle,
                        t.get("priority", 2),
                        t.get("territory", t.get("theme", "building")),
                        json.dumps(t.get("themes", t.get("topics", []))),
                        t.get("author_context", t.get("context", ""))
                    )
                )
                count += 1
            except Exception as e:
                print(f"  SKIP target {handle}: {e}")
        db.commit()
        print(f"reply_targets: {count} handles imported")
    else:
        print("reply_targets: no target-handles.json found")

    # 5. Engine state
    state_path = WORKSPACE / "memory" / "engine_state.json"
    if state_path.exists():
        with open(state_path) as f:
            state = json.load(f)
        count = 0
        for k, v in state.items():
            shared.set_state(db, k, json.dumps(v) if isinstance(v, (dict, list)) else str(v))
            count += 1
        print(f"state: {count} keys imported")

    # 6. Engagements
    engage_path = WORKSPACE / "memory" / "engagements.jsonl"
    items = load_jsonl(engage_path)
    count = 0
    for item in items:
        try:
            db.execute(
                """INSERT INTO engagements
                   (action, post_id, target_handle, target_tweet_url, text, performed_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    item.get("action", "unknown"),
                    item.get("post_id"),
                    item.get("target_handle"),
                    item.get("target_tweet_url"),
                    item.get("text"),
                    item.get("performed_at", shared.now_utc().isoformat())
                )
            )
            count += 1
        except Exception as e:
            pass
    db.commit()
    print(f"engagements: {count} entries imported")

    # Summary
    print("\n" + "=" * 50)
    for table in ["queue", "posted", "signals", "reply_targets", "reply_drafts", "engagements", "metrics", "state"]:
        row = db.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()
        print(f"  {table}: {row['c']} rows")
    print("=" * 50)
    print(f"\nDatabase: {shared.DB_PATH}")
    print("Migration complete.")

    db.close()


if __name__ == "__main__":
    migrate()
