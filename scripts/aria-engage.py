#!/usr/bin/env python3
"""
aria-engage.py  --  ARIA v2.1: Engagement Engine

Monitors replies to posted tweets and handles the 150x algo signal:
  1. Self-reply: posts a second angle on own tweet within 5 min
  2. Reply-back: monitors incoming replies, drafts responses in voice
  3. Sends Telegram alerts for replies needing personal response

The first 30-60 min after posting is the critical algo window.
This script exists to maximize engagement velocity in that window.

Usage:
    python3 aria-engage.py                   # check all recent posts
    python3 aria-engage.py --post-id abc123  # check specific post
    python3 aria-engage.py --self-reply-only # just generate self-replies
    python3 aria-engage.py --dry-run
"""

import json, os, sys, subprocess, argparse, re, time, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urllib_request

# --- anti-detection: random startup delay (1-8 min) ---
if "--dry-run" not in sys.argv:
    _jitter = random.randint(60, 480)
    print(f"[jitter] sleeping {_jitter}s before engage run")
    time.sleep(_jitter)

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE", os.path.expanduser("~/.openclaw/agents/aria/workspace")))
VOICE_PATH = WORKSPACE / "voice.json"
POSTED_PATH = WORKSPACE / "memory" / "posted.jsonl"
ENGAGE_LOG_PATH = WORKSPACE / "memory" / "engagements.jsonl"
LOG_PATH = WORKSPACE / "logs" / "engage.log"

OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:26b")
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:28800")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8794849679:AAGUiW5aIKeGzChVeSOMzpMFeqw0g-gGqII")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7353580848")

SELF_REPLY_WINDOW_MIN = 10  # only self-reply to posts < 10 min old
REPLY_BACK_WINDOW_MIN = 60  # monitor replies for 60 min
MAX_AUTO_REPLY_BACKS = 3    # max auto-replies per post


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


def load_posted():
    if not POSTED_PATH.exists():
        return []
    posts = []
    with open(POSTED_PATH) as f:
        for line in f:
            try:
                posts.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                pass
    return posts


def save_posted(posts):
    with open(POSTED_PATH, "w") as f:
        for post in posts:
            f.write(json.dumps(post) + "\n")


def call_ollama(prompt, temperature=0.8):
    url = f"{OLLAMA_BASE}/v1/chat/completions"
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": 200,
        "stream": False
    }).encode()
    req = urllib_request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib_request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"ollama error: {e}")
        return None


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode()
        req = urllib_request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib_request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"telegram error: {e}")


def get_recent_posts(posts, window_minutes):
    """Get posts within the engagement window."""
    now = datetime.now(timezone.utc)
    recent = []
    for post in posts:
        if post.get("status") != "live":
            continue
        posted_at = post.get("posted_at", "").replace("Z", "+00:00")
        try:
            t = datetime.fromisoformat(posted_at)
            age_min = (now - t).total_seconds() / 60
            if age_min < window_minutes:
                post["_age_min"] = age_min
                recent.append(post)
        except ValueError:
            pass
    return recent


# ---- SELF-REPLY ----

def generate_self_reply(original_text, voice):
    """Generate a self-reply: second angle on the same observation."""
    examples = random.sample(voice["golden_tweets"], min(3, len(voice["golden_tweets"])))
    examples_text = "\n".join([f"- {e['text']}" for e in examples])

    prompt = f"""you posted this tweet:
"{original_text}"

write a self-reply that adds a second angle. not a continuation, not an explanation. a new observation related to the same territory. it should feel like a natural "also:" thought, not a thread.

voice examples:
{examples_text}

rules:
- lowercase, 1-2 sentences, max 200 chars
- no em dashes, no hashtags, no emojis
- don't explain the original tweet
- don't start with "also" or "and" or "to add"
- same deadpan observer tone
- must stand alone as a good tweet even without the original

write ONLY the reply text. nothing else."""

    result = call_ollama(prompt)
    if result:
        clean = result.strip().strip('"').strip("'")
        clean = re.sub(r'^(reply|tweet|also|and)[:\s]*', '', clean, flags=re.IGNORECASE).strip()
        return clean.strip('"').strip("'")
    return None


def post_reply_to_tweet(tweet_url, reply_text):
    """Post a reply to a specific tweet via CDP."""
    script = f"""
const CDP = require('chrome-remote-interface');
(async () => {{
    const client = await CDP({{port: 28800}});
    const {{Page, Runtime}} = client;
    await Page.enable();

    // Navigate to the tweet
    await Page.navigate({{url: '{tweet_url}'}});
    await new Promise(r => setTimeout(r, 4000 + Math.random() * 6000));

    // Human-like: scroll a bit before acting
    await Runtime.evaluate({{expression: 'window.scrollBy(0, ' + Math.floor(50 + Math.random() * 200) + ')'}});
    await new Promise(r => setTimeout(r, 1000 + Math.random() * 3000));

    // Click reply button
    const clicked = await Runtime.evaluate({{
        expression: `
            (function() {{
                const replyBtn = document.querySelector('[data-testid="reply"]');
                if (replyBtn) {{ replyBtn.click(); return true; }}
                return false;
            }})()
        `,
        returnByValue: true
    }});

    if (!clicked.result.value) {{
        console.error('reply button not found');
        process.exit(1);
    }}

    await new Promise(r => setTimeout(r, 1500 + Math.random() * 3000));

    // Type reply text
    const replyText = {json.dumps(reply_text)};
    await Runtime.evaluate({{
        expression: `
            (function() {{
                const editor = document.querySelector('[data-testid="tweetTextarea_0"]');
                if (editor) {{
                    editor.focus();
                    document.execCommand('insertText', false, ${{JSON.stringify(replyText)}});
                    return true;
                }}
                return false;
            }})()
        `,
        returnByValue: true
    }});

    await new Promise(r => setTimeout(r, 800 + Math.random() * 2000));

    // Click post reply
    await Runtime.evaluate({{
        expression: `
            (function() {{
                const btns = document.querySelectorAll('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
                for (const btn of btns) {{
                    if (!btn.disabled) {{ btn.click(); return true; }}
                }}
                return false;
            }})()
        `,
        returnByValue: true
    }});

    await new Promise(r => setTimeout(r, 3000));
    console.log('reply posted');
    await client.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    script_path = WORKSPACE / "scripts" / "_post_reply.js"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    with open(script_path, "w") as f:
        f.write(script)

    try:
        result = subprocess.run(
            ["node", str(script_path)], capture_output=True, text=True, timeout=60
        )
        return result.returncode == 0
    except Exception as e:
        log(f"reply post error: {e}")
        return False


# ---- REPLY-BACK (monitoring incoming replies) ----

def scrape_replies_to_tweet(tweet_url):
    """Scrape replies to a specific tweet via CDP."""
    script = f"""
const CDP = require('chrome-remote-interface');
(async () => {{
    const client = await CDP({{port: 28800}});
    const {{Page, Runtime}} = client;
    await Page.enable();
    await Page.navigate({{url: '{tweet_url}'}});
    await new Promise(r => setTimeout(r, 3000 + Math.random() * 5000));

    // Scroll down to load replies (human-like varying scroll)
    await Runtime.evaluate({{expression: 'window.scrollBy(0, ' + Math.floor(300 + Math.random() * 500) + ')'}});
    await new Promise(r => setTimeout(r, 1500 + Math.random() * 3000));

    const result = await Runtime.evaluate({{
        expression: `
            (function() {{
                const articles = document.querySelectorAll('article[data-testid="tweet"]');
                const replies = [];
                // Skip first article (the original tweet)
                for (let i = 1; i < Math.min(articles.length, 10); i++) {{
                    const textEl = articles[i].querySelector('[data-testid="tweetText"]');
                    const userEl = articles[i].querySelector('[data-testid="User-Name"]');
                    if (textEl) {{
                        replies.push({{
                            text: textEl.textContent.trim(),
                            user: userEl ? userEl.textContent.trim() : 'unknown'
                        }});
                    }}
                }}
                return replies;
            }})()
        `,
        returnByValue: true
    }});

    console.log(JSON.stringify(result.result.value || []));
    await client.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    script_path = WORKSPACE / "scripts" / "_scrape_replies.js"
    with open(script_path, "w") as f:
        f.write(script)

    try:
        result = subprocess.run(
            ["node", str(script_path)], capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except:
        pass
    return []


def generate_reply_back(original_text, their_reply, voice):
    """Draft a reply-back to someone who replied to our tweet."""
    examples = random.sample(voice["golden_tweets"], min(3, len(voice["golden_tweets"])))
    examples_text = "\n".join([f"- {e['text']}" for e in examples])

    prompt = f"""your original tweet:
"{original_text}"

someone replied:
"{their_reply}"

write a reply-back that extends the conversation. same voice as these examples:
{examples_text}

rules:
- lowercase, 1-2 sentences, max 180 chars
- never say "thanks", "great point", "exactly", or any sycophancy
- never use em dashes, hashtags, emojis
- either: add a new angle they didn't think of, gently disagree with specifics, or ask a question that deepens the topic
- same deadpan observer tone. you're having a conversation with a peer, not thanking a fan.

write ONLY the reply text. nothing else."""

    result = call_ollama(prompt)
    if result:
        clean = result.strip().strip('"').strip("'")
        return clean
    return None


def log_engagement(post_id, action, text, target_user=None):
    """Log engagement action."""
    ENGAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ENGAGE_LOG_PATH, "a") as f:
        f.write(json.dumps({
            "post_id": post_id,
            "action": action,
            "text": text,
            "target_user": target_user,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }) + "\n")


def main():
    parser = argparse.ArgumentParser(description="ARIA v2.1: Engagement Engine")
    parser.add_argument("--post-id", type=str, help="check specific post")
    parser.add_argument("--self-reply-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log("=" * 60)
    log("ARIA engage starting")

    voice = load_voice()
    posts = load_posted()

    # Get posts in engagement window
    if args.post_id:
        targets = [p for p in posts if p.get("id") == args.post_id]
    else:
        targets = get_recent_posts(posts, REPLY_BACK_WINDOW_MIN)

    log(f"posts in engagement window: {len(targets)}")

    for post in targets:
        age = post.get("_age_min", 999)
        tweet_url = post.get("tweet_url", "")
        log(f"\npost [{post.get('id', '?')}] age={age:.0f}min: \"{post['text'][:60]}...\"")

        # 1. Self-reply (only if fresh and not already self-replied)
        if age < SELF_REPLY_WINDOW_MIN and not post.get("self_replied"):
            log("  generating self-reply...")
            self_reply = generate_self_reply(post["text"], voice)
            if self_reply:
                log(f"  self-reply: \"{self_reply}\"")
                if not args.dry_run and tweet_url:
                    success = post_reply_to_tweet(tweet_url, self_reply)
                    if success:
                        post["self_replied"] = True
                        post["self_reply_text"] = self_reply
                        log("  self-reply POSTED")
                        log_engagement(post.get("id"), "self_reply", self_reply)
                    else:
                        log("  self-reply post FAILED")
                elif args.dry_run:
                    log("  DRY RUN: would post self-reply")

        if args.self_reply_only:
            continue

        # 2. Monitor replies and reply-back
        if tweet_url and not tweet_url.startswith("posted"):
            log("  checking for replies...")
            replies = scrape_replies_to_tweet(tweet_url)
            log(f"  found {len(replies)} replies")

            replied_to = post.get("replied_to_users", [])
            reply_count = len(replied_to)

            for reply in replies:
                user = reply.get("user", "unknown")
                their_text = reply.get("text", "")

                # Skip if already replied to this user or hit max
                if user in replied_to or reply_count >= MAX_AUTO_REPLY_BACKS:
                    continue

                # Skip obvious bots / low-quality
                if len(their_text) < 10 or their_text.lower() in ["great", "nice", "true", "facts"]:
                    continue

                log(f"  reply from {user}: \"{their_text[:60]}...\"")

                # Generate reply-back
                reply_back = generate_reply_back(post["text"], their_text, voice)
                if reply_back:
                    log(f"  reply-back draft: \"{reply_back}\"")

                    # Send to Telegram for approval (safer than auto-posting replies)
                    send_telegram(
                        f"<b>Reply to your tweet</b>\n\n"
                        f"<i>{post['text'][:100]}</i>\n\n"
                        f"<b>{user}:</b> {their_text[:200]}\n\n"
                        f"<b>Draft reply-back:</b>\n{reply_back}\n\n"
                        f"Reply 'post' to approve or ignore to skip."
                    )

                    replied_to.append(user)
                    reply_count += 1
                    log_engagement(post.get("id"), "reply_back_drafted", reply_back, user)

                    # anti-detection: random pause between processing replies
                    time.sleep(random.randint(15, 90))

            post["replied_to_users"] = replied_to

    # Save updated posts
    if not args.dry_run:
        save_posted(posts)
        log("posted.jsonl updated")

    log("\nengage complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
