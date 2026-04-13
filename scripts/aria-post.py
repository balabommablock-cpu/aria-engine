#!/usr/bin/env python3
"""
aria-post.py v2.1 -- ARIA Voice Engine: Post (Full Engine)

Now includes:
  - Timing optimization (posts in optimal IST windows)
  - Image attachment (quote cards, terminal screenshots via pick-image.py)
  - Self-reply trigger (kicks off aria-engage.py 5 min after posting)
  - Composite score ranking (not just voice score)

Usage:
    python3 aria-post.py              # post top candidate in current window
    python3 aria-post.py --dry-run    # show what would post
    python3 aria-post.py --force      # skip timing + pacing checks
    python3 aria-post.py --pick ID    # post specific candidate
"""

import json, os, sys, subprocess, argparse, time, threading, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urllib_request

# --- anti-detection: random startup delay (0-12 min) ---
if "--dry-run" not in sys.argv:
    _jitter = random.randint(30, 720)
    print(f"[jitter] sleeping {_jitter}s before post run")
    time.sleep(_jitter)

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE", os.path.expanduser("~/.openclaw/agents/aria/workspace")))
VOICE_PATH = WORKSPACE / "voice.json"
QUEUE_PATH = WORKSPACE / "memory" / "queue.jsonl"
POSTED_PATH = WORKSPACE / "memory" / "posted.jsonl"
LOG_PATH = WORKSPACE / "logs" / "post.log"

POST_TWEET_JS = Path(os.path.expanduser("~/.openclaw/workspace/skills/x-twitter-poster/post_tweet.js"))
PICK_IMAGE = WORKSPACE / "scripts" / "pick-image.py"
MAKE_IMAGE = WORKSPACE / "scripts" / "make-image.py"
ENGAGE_SCRIPT = WORKSPACE / "scripts" / "aria-engage.py"

X_USERNAME = os.environ.get("X_USERNAME", "BalabommaRao")
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:28800")
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


def load_queue():
    if not QUEUE_PATH.exists():
        return []
    candidates = []
    with open(QUEUE_PATH) as f:
        for line in f:
            try:
                c = json.loads(line.strip())
                if c.get("status") == "queued":
                    candidates.append(c)
            except json.JSONDecodeError:
                pass
    return candidates


def check_timing(voice, force=False):
    """Check if current time is in an optimal posting window."""
    if force:
        return True, "forced"

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    tz_name = voice.get("timing", {}).get("timezone", "Asia/Kolkata")
    try:
        tz = ZoneInfo(tz_name)
    except:
        return True, "timezone unavailable, posting anyway"

    now = datetime.now(tz)
    current_time = now.strftime("%H:%M")

    windows = voice.get("timing", {}).get("windows_ist", [])
    for w in windows:
        if w["start"] <= current_time <= w["end"]:
            return True, f"in {w['name']} window ({w['start']}-{w['end']})"

    # Find next window
    next_windows = [w for w in windows if w["start"] > current_time]
    if next_windows:
        nxt = next_windows[0]
        return False, f"not in window. next: {nxt['name']} at {nxt['start']}"
    return False, f"past all windows today. next: {windows[0]['name']} tomorrow at {windows[0]['start']}"


def check_pacing(voice, force=False):
    """Check minimum gap between posts."""
    if force:
        return True, "forced"

    min_gap = voice.get("timing", {}).get("min_gap_hours", 3)
    if not POSTED_PATH.exists():
        return True, "no previous posts"

    latest = None
    with open(POSTED_PATH) as f:
        for line in f:
            try:
                post = json.loads(line.strip())
                ts = post.get("posted_at", "").replace("Z", "+00:00")
                if ts:
                    t = datetime.fromisoformat(ts)
                    if latest is None or t > latest:
                        latest = t
            except:
                pass

    if latest is None:
        return True, "no valid timestamps"

    gap = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
    if gap < min_gap:
        return False, f"last post {gap:.1f}h ago (min {min_gap}h)"
    return True, f"last post {gap:.1f}h ago"


def pick_best(candidates, pick_id=None):
    if pick_id:
        for c in candidates:
            if c.get("id") == pick_id:
                return c
        return None
    if not candidates:
        return None
    # Sort by composite score
    return max(candidates, key=lambda c: c.get("scores", {}).get("composite", c.get("voice_score", 0)))


def generate_image(text, image_type, voice):
    """Generate image for the tweet if needed."""
    if image_type == "none" or not image_type:
        return None

    if image_type == "quote_card":
        style = voice.get("images", {}).get("quote_card_style", {})
        img_path = WORKSPACE / "images" / f"qc_{hashlib.md5(text.encode()).hexdigest()[:8]}.png"
        img_path.parent.mkdir(parents=True, exist_ok=True)

        # Use make-image.py if available
        if MAKE_IMAGE.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(MAKE_IMAGE),
                     "--template", "quote_card",
                     "--text", text,
                     "--output", str(img_path),
                     "--bg", style.get("bg_color", "#FAF8F3"),
                     "--fg", style.get("text_color", "#2C2B28"),
                     "--accent", style.get("accent_color", "#6B8F71")],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0 and img_path.exists():
                    return str(img_path)
            except Exception as e:
                log(f"image gen error: {e}")

    elif image_type == "terminal_screenshot":
        if PICK_IMAGE.exists():
            try:
                result = subprocess.run(
                    [sys.executable, str(PICK_IMAGE),
                     "--strategy", "terminal_screenshot",
                     "--text", text[:100]],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0:
                    path = result.stdout.strip()
                    if path and os.path.exists(path):
                        return path
            except:
                pass

    return None


def post_tweet(text, image_path=None):
    """Post tweet via post_tweet.js. Returns (success, result_info)."""
    if not POST_TWEET_JS.exists():
        return False, f"post_tweet.js not found"

    env = os.environ.copy()
    env["X_USERNAME"] = X_USERNAME
    env["CDP_URL"] = CDP_URL

    cmd = ["node", str(POST_TWEET_JS), text]
    if image_path:
        cmd.append("--image")
        cmd.append(image_path)

    try:
        result = subprocess.run(
            cmd, env=env, cwd=str(POST_TWEET_JS.parent),
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            tweet_url = None
            for line in result.stdout.split("\n"):
                if "twitter.com" in line or "x.com" in line:
                    tweet_url = line.strip()
                    break
            return True, tweet_url or "posted (url not captured)"
        else:
            return False, result.stderr.strip()[:300] or "unknown error"
    except subprocess.TimeoutExpired:
        return False, "timeout (120s)"
    except Exception as e:
        return False, str(e)


def mark_posted(candidate, tweet_url):
    # Remove from queue
    if QUEUE_PATH.exists():
        lines = []
        with open(QUEUE_PATH) as f:
            for line in f:
                try:
                    c = json.loads(line.strip())
                    if c.get("id") != candidate["id"]:
                        lines.append(line.strip())
                except:
                    lines.append(line.strip())
        with open(QUEUE_PATH, "w") as f:
            for line in lines:
                f.write(line + "\n")

    # Append to posted
    POSTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": candidate["id"],
        "text": candidate["text"],
        "territory": candidate.get("territory"),
        "scores": candidate.get("scores"),
        "image_type": candidate.get("image_type", "none"),
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "tweet_url": tweet_url,
        "status": "live",
        "self_replied": False,
        "replied_to_users": [],
        "metrics": {"impressions": None, "likes": None, "replies": None,
                    "retweets": None, "bookmarks": None, "checked_at": None}
    }
    with open(POSTED_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def schedule_engage(post_id, delay_min=5):
    """Schedule aria-engage.py to run after a delay (for self-reply)."""
    def run_delayed():
        time.sleep(delay_min * 60)
        if ENGAGE_SCRIPT.exists():
            log(f"triggering engage for post {post_id}")
            subprocess.run(
                [sys.executable, str(ENGAGE_SCRIPT), "--post-id", post_id, "--self-reply-only"],
                cwd=str(WORKSPACE), timeout=120
            )
    thread = threading.Thread(target=run_delayed, daemon=True)
    thread.start()
    log(f"engage scheduled in {delay_min} min for post {post_id}")


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode()
        req = urllib_request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib_request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"telegram error: {e}")


import hashlib

def main():
    parser = argparse.ArgumentParser(description="ARIA v2.1 Post")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--pick", type=str)
    args = parser.parse_args()

    log("=" * 60)
    log("ARIA v2.1 post starting")

    voice = load_voice()

    # Timing check
    ok, reason = check_timing(voice, force=args.force or args.dry_run)
    if not ok:
        log(f"TIMING: {reason}")
        return
    log(f"timing: {reason}")

    # Pacing check
    ok, reason = check_pacing(voice, force=args.force or args.dry_run)
    if not ok:
        log(f"PACING: {reason}")
        return
    log(f"pacing: {reason}")

    # Load queue
    candidates = load_queue()
    log(f"queue: {len(candidates)} candidates")
    if not candidates:
        log("queue empty. run aria-generate.py first.")
        return

    # Pick best
    candidate = pick_best(candidates, pick_id=args.pick)
    if not candidate:
        log("no candidate found")
        return

    scores = candidate.get("scores", {})
    log(f"selected: [{scores.get('composite', '?')}] \"{candidate['text'][:80]}...\"")
    log(f"territory: {candidate.get('territory')}, image: {candidate.get('image_type', 'none')}")

    if args.dry_run:
        log(f"DRY RUN: {candidate['text']}")
        return

    # Generate image if needed
    image_path = None
    if candidate.get("image_type") and candidate["image_type"] != "none":
        log(f"generating {candidate['image_type']} image...")
        image_path = generate_image(candidate["text"], candidate["image_type"], voice)
        if image_path:
            log(f"image: {image_path}")
        else:
            log("image gen failed, posting text-only")

    # Post
    log("posting...")
    success, result = post_tweet(candidate["text"], image_path)

    if success:
        log(f"POSTED: {result}")
        mark_posted(candidate, result)

        # Schedule self-reply
        schedule_engage(candidate["id"], delay_min=5)

        send_telegram(
            f"<b>ARIA posted</b>\n\n"
            f"{candidate['text']}\n\n"
            f"composite: {scores.get('composite', '?')} | "
            f"voice: {scores.get('voice', '?')} | "
            f"provocation: {scores.get('provocation', '?')}\n"
            f"territory: {candidate.get('territory')} | "
            f"image: {candidate.get('image_type', 'none')}\n"
            f"{result}"
        )
    else:
        log(f"FAILED: {result}")
        send_telegram(f"<b>ARIA post failed</b>\n\n{candidate['text'][:100]}...\n\nerror: {result}")

    log("post complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
