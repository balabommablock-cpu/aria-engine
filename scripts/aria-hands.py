#!/usr/bin/env python3
"""
aria-hands.py -- Hands process for ARIA v3.

CDP-only. Runs every 10 min via launchd.
Exactly ONE action per cycle, priority-ordered:
  P1  post_tweet      -- original tweet from queue
  P2  post_reply      -- outbound reply to a target account
  P3  self_reply      -- thread extension on own recent tweet
  P4  scrape_metrics  -- own profile analytics
  P5  passive_action  -- random like for humanization

Golden metric: views and engagement.
"""

import json, os, sys, random, time, subprocess, traceback

# ---- path setup so we can import aria-shared as a module ----
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

import importlib
shared = importlib.import_module("aria-shared")

# pull everything we need into module scope
get_db          = shared.get_db
init_db         = shared.init_db
log_hands       = shared.log_hands
load_voice      = shared.load_voice
acquire_lock    = shared.acquire_lock
release_lock    = shared.release_lock
call_claude     = shared.call_claude
send_telegram   = shared.send_telegram
in_posting_window = shared.in_posting_window
gap_ok          = shared.gap_ok
now_utc         = shared.now_utc
now_ist         = shared.now_ist
parse_ts        = shared.parse_ts
ts_age_hours    = shared.ts_age_hours
get_state       = shared.get_state
set_state       = shared.set_state
make_id         = shared.make_id
DRY_RUN         = shared.DRY_RUN
POST_TWEET_JS   = shared.POST_TWEET_JS
CDP_URL         = shared.CDP_URL
CDP_PORT        = shared.CDP_PORT
X_USERNAME      = shared.X_USERNAME
WORKSPACE       = shared.WORKSPACE


# ============================================================
# CDP ACTIONS
# ============================================================

def do_post_tweet(text, image_path=None):
    """Post original tweet via node post_tweet.js. Returns (success, tweet_url_or_err)."""
    if not POST_TWEET_JS.exists():
        return False, "post_tweet.js not found"

    env = os.environ.copy()
    env["X_USERNAME"] = X_USERNAME
    env["CDP_URL"] = CDP_URL

    cmd = ["node", str(POST_TWEET_JS), text]
    if image_path and os.path.isfile(image_path):
        # image goes as positional arg after the empty reply-to slot
        cmd.append("")          # no reply-to url
        cmd.append(image_path)

    try:
        result = subprocess.run(
            cmd, env=env, cwd=str(POST_TWEET_JS.parent),
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            tweet_url = None
            for line in result.stdout.split("\n"):
                if "x.com" in line or "twitter.com" in line:
                    tweet_url = line.strip()
                    break
            return True, tweet_url or "posted (url not captured)"
        else:
            err = result.stderr.strip()[:300] or result.stdout.strip()[:300] or "unknown error"
            return False, err
    except subprocess.TimeoutExpired:
        return False, "timeout (180s)"
    except Exception as e:
        return False, str(e)


def do_post_reply(tweet_url, reply_text, image_path=None):
    """Post a reply to a tweet via node post_tweet.js (reply-in-thread mode).
    Returns (success, result_url_or_err)."""
    if not POST_TWEET_JS.exists():
        return False, "post_tweet.js not found"

    env = os.environ.copy()
    env["X_USERNAME"] = X_USERNAME
    env["CDP_URL"] = CDP_URL

    # post_tweet.js signature: node post_tweet.js <text> <replyToUrl> [imagePaths...]
    cmd = ["node", str(POST_TWEET_JS), reply_text, tweet_url]
    if image_path and os.path.isfile(image_path):
        cmd.append(image_path)

    try:
        result = subprocess.run(
            cmd, env=env, cwd=str(POST_TWEET_JS.parent),
            capture_output=True, text=True, timeout=180
        )
        if result.returncode == 0:
            url = None
            for line in result.stdout.split("\n"):
                if "x.com" in line or "twitter.com" in line:
                    url = line.strip()
                    break
            return True, url or tweet_url
        else:
            err = result.stderr.strip()[:300] or result.stdout.strip()[:300] or "unknown error"
            return False, err
    except subprocess.TimeoutExpired:
        return False, "timeout (180s)"
    except Exception as e:
        return False, str(e)


def do_like_tweet(tweet_url):
    """Navigate to a tweet and like it via CDP. Returns (success, msg)."""
    # Generate a tiny inline node script that connects via CDP,
    # navigates to the tweet, and clicks the like button.
    script = f"""
const {{ chromium }} = require('playwright');
const {{ withCdpLock }} = require('{WORKSPACE}/scripts/cdp-lock');

(async () => {{
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('{tweet_url}', {{ waitUntil: 'domcontentloaded', timeout: 30000 }});
  await page.waitForSelector('article[data-testid="tweet"]', {{ timeout: 15000 }});
  await page.waitForTimeout({random.randint(1500, 4000)});

  // find the like button on the main tweet (first article)
  const article = page.locator('article[data-testid="tweet"]').first();
  const likeBtn = article.locator('button[data-testid="like"]');
  const count = await likeBtn.count();
  if (count === 0) {{
    // already liked (button becomes "unlike")
    console.log('already liked or button not found');
    await browser.close();
    process.exit(0);
  }}
  await likeBtn.click({{ timeout: 5000 }});
  console.log('liked');
  await page.waitForTimeout(1500);
  await browser.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    return _run_inline_node(script, "like")


def do_scrape_metrics(db):
    """Scrape basic metrics for recent posted tweets.
    Navigate to own profile analytics and pull numbers.
    Returns (success, msg)."""
    # Grab recent posted tweets (last 7 days)
    rows = db.execute(
        "SELECT id, tweet_url, posted_at FROM posted "
        "WHERE posted_at > datetime('now', '-7 days') "
        "ORDER BY posted_at DESC LIMIT 20"
    ).fetchall()

    if not rows:
        return True, "no recent posts to scrape"

    script = f"""
const {{ chromium }} = require('playwright');
const {{ withCdpLock }} = require('{WORKSPACE}/scripts/cdp-lock');

(async () => {{
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const page = browser.contexts()[0].pages()[0];

  // navigate to own profile
  await page.goto('https://x.com/{X_USERNAME}', {{ waitUntil: 'domcontentloaded', timeout: 30000 }});
  await page.waitForSelector('article[data-testid="tweet"]', {{ timeout: 15000 }});
  await page.waitForTimeout(3000);

  // scrape visible tweet metrics
  const metrics = await page.evaluate(() => {{
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    const results = [];
    for (const art of articles) {{
      const textEl = art.querySelector('[data-testid="tweetText"]');
      const text = textEl ? textEl.innerText.trim().slice(0, 80) : '';

      // extract analytics row: views, replies, retweets, likes, bookmarks
      const groups = art.querySelectorAll('[role="group"] button');
      let replies = 0, retweets = 0, likes = 0, bookmarks = 0, views = 0;
      for (const btn of groups) {{
        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
        const match = label.match(/(\\d[\\d,]*)\\s+(repl|retweet|like|bookmark|view)/);
        if (match) {{
          const num = parseInt(match[1].replace(/,/g, ''), 10) || 0;
          if (label.includes('repl')) replies = num;
          else if (label.includes('retweet')) retweets = num;
          else if (label.includes('like')) likes = num;
          else if (label.includes('bookmark')) bookmarks = num;
          else if (label.includes('view')) views = num;
        }}
      }}
      // also try the analytics link for views
      const analyticsLink = art.querySelector('a[href*="/analytics"]');
      if (analyticsLink && views === 0) {{
        const vMatch = (analyticsLink.getAttribute('aria-label') || '').match(/(\\d[\\d,]*)\\s*view/i);
        if (vMatch) views = parseInt(vMatch[1].replace(/,/g, ''), 10) || 0;
      }}

      results.push({{ text, replies, retweets, likes, bookmarks, views }});
    }}
    return results;
  }});

  console.log(JSON.stringify(metrics));
  await browser.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    success, output = _run_inline_node_raw(script, "metrics", timeout=90)
    if not success:
        return False, output

    # parse the JSON output and insert into metrics table
    try:
        scraped = json.loads(output.strip().split("\n")[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"could not parse metrics json: {output[:200]}"

    ts = now_utc().isoformat()
    inserted = 0
    for m in scraped:
        # try to match scraped text to a posted row
        text_head = m.get("text", "")[:60]
        if not text_head:
            continue
        row = db.execute(
            "SELECT id FROM posted WHERE text LIKE ? LIMIT 1",
            (text_head + "%",)
        ).fetchone()
        if row:
            db.execute(
                "INSERT OR REPLACE INTO metrics "
                "(post_id, scraped_at, impressions, likes, replies, retweets, bookmarks) "
                "VALUES (?,?,?,?,?,?,?)",
                (row["id"], ts, m.get("views", 0), m.get("likes", 0),
                 m.get("replies", 0), m.get("retweets", 0), m.get("bookmarks", 0))
            )
            inserted += 1
    db.commit()
    return True, f"scraped {inserted} posts"


def find_recent_tweet_url(handle):
    """Navigate to a target's profile and grab the URL of their most recent tweet.
    Returns tweet_url or None."""
    clean_handle = handle.lstrip("@")
    script = f"""
const {{ chromium }} = require('playwright');
const {{ withCdpLock }} = require('{WORKSPACE}/scripts/cdp-lock');

(async () => {{
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('https://x.com/{clean_handle}', {{ waitUntil: 'domcontentloaded', timeout: 30000 }});
  await page.waitForSelector('article[data-testid="tweet"]', {{ timeout: 15000 }});
  await page.waitForTimeout({random.randint(2000, 4000)});

  // find the first tweet's permalink
  const url = await page.evaluate(() => {{
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    for (const art of articles) {{
      // skip pinned tweets if possible -- they have a "Pinned" label
      const pinned = art.querySelector('[data-testid="socialContext"]');
      if (pinned && pinned.innerText.toLowerCase().includes('pinned')) continue;

      const links = art.querySelectorAll('a[href*="/status/"]');
      for (const a of links) {{
        const href = a.getAttribute('href');
        if (href && href.match(/\\/status\\/\\d+$/)) {{
          return 'https://x.com' + href;
        }}
      }}
    }}
    // fallback: just take the first article's status link
    const firstArt = document.querySelector('article[data-testid="tweet"]');
    if (firstArt) {{
      const link = firstArt.querySelector('a[href*="/status/"]');
      if (link) return 'https://x.com' + link.getAttribute('href');
    }}
    return null;
  }});

  console.log(url || 'NONE');
  await browser.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    success, output = _run_inline_node_raw(script, "find_tweet", timeout=60)
    if success and output.strip() and output.strip() != "NONE":
        url = output.strip().split("\n")[-1].strip()
        if "x.com" in url and "/status/" in url:
            return url
    return None


# ---- node script runner helpers ----

def _run_inline_node(script, label, timeout=60):
    """Write and run a node script via cdp-lock. Returns (success, msg)."""
    success, output = _run_inline_node_raw(script, label, timeout)
    return success, output


def _run_inline_node_raw(script_body, label, timeout=60):
    """Write script to temp file, run with node, return (success, stdout_or_stderr)."""
    script_path = WORKSPACE / "scripts" / f"_hands_{label}.js"
    # Wrap the script body inside withCdpLock
    wrapped = f"""
const {{ withCdpLock }} = require('{WORKSPACE}/scripts/cdp-lock');
withCdpLock(async () => {{
{_indent(script_body)}
}}, {timeout * 1000}).then(() => {{
  process.exit(0);
}}).catch(e => {{
  console.error('cdp-lock error:', e.message);
  process.exit(1);
}});
"""
    # Actually, the script_body already has its own chromium.connectOverCDP and
    # withCdpLock require. We should run the script_body as-is but wrap it in
    # withCdpLock at the top level. Let's just write the raw script -- it
    # already imports withCdpLock but doesn't call it. We need to restructure.
    #
    # Simpler approach: write script_body directly. It already has the
    # require('playwright') and the IIFE. The CDP lock is handled by post_tweet.js
    # for post actions. For inline scripts (like, metrics, find_tweet), we
    # should wrap them. But the script_body above already has the shape
    # (async () => { ... })().catch(...). Let's just write it raw -- the
    # flock-based hands lock already prevents parallel hands runs, and the
    # CDP lock file is only needed when hands and post_tweet.js compete,
    # which they don't since hands calls post_tweet.js sequentially.

    script_path.parent.mkdir(parents=True, exist_ok=True)
    with open(script_path, "w") as f:
        f.write(script_body)

    env = os.environ.copy()
    env["CDP_URL"] = CDP_URL
    env["X_USERNAME"] = X_USERNAME

    try:
        result = subprocess.run(
            ["node", str(script_path)],
            env=env, cwd=str(POST_TWEET_JS.parent),
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()[:500] or result.stdout.strip()[:500] or "unknown error"
    except subprocess.TimeoutExpired:
        return False, f"timeout ({timeout}s)"
    except Exception as e:
        return False, str(e)


def _indent(text, spaces=2):
    """Indent every line of text."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.split("\n"))


# ============================================================
# PRIORITY PICKER
# ============================================================

def pick_action(db, voice):
    """Pick exactly one action. Returns (action_type, context_dict).
    Priority: post_tweet > post_reply > self_reply > scrape_metrics > passive_action.
    Returns ('idle', {}) if nothing to do."""

    # --- P1: post_tweet ---
    in_window, window_info = in_posting_window(voice)
    has_gap = gap_ok(db, voice)
    if in_window and has_gap:
        row = db.execute(
            "SELECT * FROM queue WHERE status='queued' AND expires_at > ? "
            "ORDER BY json_extract(scores_json, '$.composite') DESC LIMIT 1",
            (now_utc().isoformat(),)
        ).fetchone()
        if row:
            log_hands(f"P1 post_tweet: window={window_info}, candidate={row['id']}")
            return "post_tweet", dict(row)
    elif in_window and not has_gap:
        log_hands(f"P1 skip: in window ({window_info}) but gap not met")
    else:
        log_hands(f"P1 skip: {window_info}")

    # --- P2: post_reply ---
    reply = db.execute(
        "SELECT * FROM reply_drafts WHERE status='ready' "
        "ORDER BY generated_at ASC LIMIT 1"
    ).fetchone()
    if reply:
        log_hands(f"P2 post_reply: target={reply['target_handle']}, draft={reply['id']}")
        return "post_reply", dict(reply)

    # --- P3: self_reply ---
    recent = db.execute(
        "SELECT * FROM posted WHERE self_replied=0 "
        "ORDER BY posted_at DESC LIMIT 1"
    ).fetchone()
    if recent:
        age_h = ts_age_hours(recent["posted_at"])
        if age_h < 0.5:  # 30 minutes
            log_hands(f"P3 self_reply: post={recent['id']}, age={age_h*60:.0f}min")
            return "self_reply", dict(recent)

    # --- P4: scrape_metrics ---
    last_metrics = get_state(db, "hands.last_metrics_at")
    if ts_age_hours(last_metrics) > 4:
        log_hands("P4 scrape_metrics: stale or never scraped")
        return "scrape_metrics", {}

    # --- P5: passive_action ---
    # pick a random reply target and like their recent tweet
    target = db.execute(
        "SELECT handle FROM reply_targets ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if target:
        log_hands(f"P5 passive_action: target={target['handle']}")
        return "passive_action", {"handle": target["handle"]}

    log_hands("idle: nothing to do")
    return "idle", {}


# ============================================================
# EXECUTE ACTION
# ============================================================

def execute_action(db, voice, action, ctx):
    """Execute the chosen action. One action per cycle."""
    try:
        if action == "post_tweet":
            _exec_post_tweet(db, voice, ctx)
        elif action == "post_reply":
            _exec_post_reply(db, voice, ctx)
        elif action == "self_reply":
            _exec_self_reply(db, voice, ctx)
        elif action == "scrape_metrics":
            _exec_scrape_metrics(db)
        elif action == "passive_action":
            _exec_passive_action(db, ctx)
        else:
            log_hands(f"unknown action: {action}")
    except Exception as e:
        log_hands(f"execute_action error ({action}): {e}", level="error")
        log_hands(traceback.format_exc(), level="error")


def _exec_post_tweet(db, voice, ctx):
    """Post the top queued tweet."""
    post_id = ctx["id"]
    text = ctx["text"]
    image_path = ctx.get("image_path")

    # mark as posting
    db.execute("UPDATE queue SET status='posting' WHERE id=?", (post_id,))
    db.commit()

    # anti-detection delay
    delay = random.randint(30, 180)
    log_hands(f"post_tweet: anti-detect delay {delay}s")

    if DRY_RUN:
        log_hands(f"DRY RUN: would post [{post_id}]: {text[:80]}")
        db.execute("UPDATE queue SET status='queued' WHERE id=?", (post_id,))
        db.commit()
        return

    time.sleep(delay)

    success, result = do_post_tweet(text, image_path)

    if success:
        tweet_url = result if result.startswith("http") else None
        ts = now_utc().isoformat()

        # insert into posted
        db.execute(
            "INSERT OR REPLACE INTO posted "
            "(id, text, territory, scores_json, image_type, tweet_url, posted_at, self_replied) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (post_id, text, ctx.get("territory"), ctx.get("scores_json"),
             ctx.get("image_type", "none"), tweet_url, ts)
        )
        # mark queue row as posted
        db.execute("UPDATE queue SET status='posted' WHERE id=?", (post_id,))
        db.commit()

        log_hands(f"post_tweet SUCCESS: {post_id} -> {tweet_url or 'url not captured'}")
        send_telegram(
            f"<b>ARIA posted</b>\n"
            f"{text[:200]}\n\n"
            f"territory: {ctx.get('territory', '?')}\n"
            f"url: {tweet_url or 'n/a'}"
        )
    else:
        # revert to queued
        db.execute("UPDATE queue SET status='queued' WHERE id=?", (post_id,))
        db.commit()
        log_hands(f"post_tweet FAILED: {result}", level="error")
        send_telegram(f"<b>ARIA post failed</b>\n{result[:200]}")


def _exec_post_reply(db, voice, ctx):
    """Post an outbound reply to a target account."""
    draft_id = ctx["id"]
    target_handle = ctx["target_handle"]
    reply_text = ctx["reply_text"]
    target_tweet_url = ctx.get("target_tweet_url", "")

    # mark as posting
    db.execute("UPDATE reply_drafts SET status='posting' WHERE id=?", (draft_id,))
    db.commit()

    if DRY_RUN:
        log_hands(f"DRY RUN: would reply to {target_handle}: {reply_text[:80]}")
        db.execute("UPDATE reply_drafts SET status='ready' WHERE id=?", (draft_id,))
        db.commit()
        return

    # anti-detection delay
    delay = random.randint(20, 90)
    log_hands(f"post_reply: delay {delay}s before replying to {target_handle}")
    time.sleep(delay)

    # Brain may not have a tweet URL (no CDP). Hands must find one.
    # Navigate to the target's profile and find a recent tweet.
    tweet_url = target_tweet_url
    if not tweet_url or not tweet_url.startswith("http") or "/status/" not in tweet_url:
        log_hands(f"post_reply: no valid tweet url from brain, finding one for @{target_handle}")
        tweet_url = find_recent_tweet_url(target_handle)
        if not tweet_url:
            log_hands(f"post_reply: could not find tweet for @{target_handle}, failing", level="error")
            db.execute("UPDATE reply_drafts SET status='failed' WHERE id=?", (draft_id,))
            db.commit()
            return

    log_hands(f"post_reply: target tweet -> {tweet_url}")

    success, result = do_post_reply(tweet_url, reply_text)

    ts = now_utc().isoformat()
    if success:
        db.execute(
            "UPDATE reply_drafts SET status='posted', posted_at=? WHERE id=?",
            (ts, draft_id)
        )
        # update reply_targets cooldown
        db.execute(
            "UPDATE reply_targets SET last_replied_at=?, "
            "reply_count = COALESCE(reply_count, 0) + 1 "
            "WHERE handle=?",
            (ts, target_handle)
        )
        # log engagement
        db.execute(
            "INSERT INTO engagements (action, target_handle, target_tweet_url, text, performed_at) "
            "VALUES ('outbound_reply', ?, ?, ?, ?)",
            (target_handle, tweet_url, reply_text, ts)
        )
        db.commit()

        log_hands(f"post_reply SUCCESS: @{target_handle} -> {tweet_url}")
        send_telegram(
            f"<b>ARIA replied</b>\n"
            f"to: @{target_handle}\n"
            f"tweet: {tweet_url}\n"
            f"reply: {reply_text[:200]}"
        )
    else:
        db.execute("UPDATE reply_drafts SET status='failed' WHERE id=?", (draft_id,))
        db.commit()
        log_hands(f"post_reply FAILED: {result}", level="error")


def _exec_self_reply(db, voice, ctx):
    """Generate and post a self-reply to extend a recent tweet thread."""
    post_id = ctx["id"]
    original_text = ctx["text"]
    tweet_url = ctx.get("tweet_url", "")

    if not tweet_url or not tweet_url.startswith("http"):
        log_hands(f"self_reply: no valid tweet_url for {post_id}, skipping")
        # mark as done so we don't retry forever
        db.execute("UPDATE posted SET self_replied=1 WHERE id=?", (post_id,))
        db.commit()
        return

    # generate the reply text via claude
    golden = voice.get("golden_tweets", [])
    examples = random.sample(golden, min(3, len(golden)))
    examples_text = "\n".join(f"- {e['text']}" for e in examples)
    reply_style = voice.get("engage", {}).get("reply_style",
        "extend the observation with a concrete example or second angle.")

    prompt = f"""you posted this tweet as @{X_USERNAME}:
"{original_text}"

write a self-reply that adds a second angle. not a continuation, not an explanation. a new observation that extends the thread.

{reply_style}

voice examples:
{examples_text}

rules:
- lowercase, 1-2 sentences, max 200 chars
- no em dashes, no hyphens as formatting, no hashtags, no emojis
- same deadpan observer tone
- periods only for punctuation

write ONLY the reply text, nothing else."""

    log_hands(f"self_reply: generating reply for {post_id}")
    reply_text = call_claude(prompt)

    if not reply_text:
        log_hands("self_reply: claude returned nothing", level="error")
        return

    # clean the reply
    reply_text = reply_text.strip().strip('"').strip("'").lower()
    # enforce hard bans
    for char in voice.get("hard_bans", {}).get("characters", []):
        reply_text = reply_text.replace(char, "")
    reply_text = reply_text.strip()

    if len(reply_text) < 10 or len(reply_text) > 280:
        log_hands(f"self_reply: bad length ({len(reply_text)}), skipping")
        return

    if DRY_RUN:
        log_hands(f"DRY RUN: would self-reply to {post_id}: {reply_text[:80]}")
        return

    # anti-detection delay (longer for self-replies to look natural)
    delay = random.randint(90, 300)
    log_hands(f"self_reply: delay {delay}s")
    time.sleep(delay)

    success, result = do_post_reply(tweet_url, reply_text)

    ts = now_utc().isoformat()
    if success:
        db.execute(
            "UPDATE posted SET self_replied=1, self_reply_text=? WHERE id=?",
            (reply_text, post_id)
        )
        db.execute(
            "INSERT INTO engagements (action, post_id, text, performed_at) "
            "VALUES ('self_reply', ?, ?, ?)",
            (post_id, reply_text, ts)
        )
        db.commit()
        log_hands(f"self_reply SUCCESS: {post_id} -> {reply_text[:80]}")
    else:
        log_hands(f"self_reply FAILED: {result}", level="error")


def _exec_scrape_metrics(db):
    """Scrape own profile metrics."""
    log_hands("scrape_metrics: starting")

    if DRY_RUN:
        log_hands("DRY RUN: would scrape metrics")
        return

    success, msg = do_scrape_metrics(db)
    ts = now_utc().isoformat()
    set_state(db, "hands.last_metrics_at", ts)

    if success:
        log_hands(f"scrape_metrics SUCCESS: {msg}")
    else:
        log_hands(f"scrape_metrics FAILED: {msg}", level="error")


def _exec_passive_action(db, ctx):
    """Like a recent tweet from a target account for humanization."""
    handle = ctx.get("handle", "")
    if not handle:
        log_hands("passive_action: no handle provided")
        return

    if DRY_RUN:
        log_hands(f"DRY RUN: would like a tweet from @{handle}")
        return

    # anti-detection delay
    delay = random.randint(10, 60)
    log_hands(f"passive_action: delay {delay}s before liking @{handle}")
    time.sleep(delay)

    # find their recent tweet
    tweet_url = find_recent_tweet_url(handle)
    if not tweet_url:
        log_hands(f"passive_action: could not find tweet for @{handle}")
        return

    log_hands(f"passive_action: liking {tweet_url}")
    success, msg = do_like_tweet(tweet_url)

    ts = now_utc().isoformat()
    if success:
        db.execute(
            "INSERT INTO engagements (action, target_handle, target_tweet_url, performed_at) "
            "VALUES ('like', ?, ?, ?)",
            (handle, tweet_url, ts)
        )
        db.commit()
        log_hands(f"passive_action SUCCESS: liked @{handle}")
    else:
        log_hands(f"passive_action FAILED: {msg}", level="error")


# ============================================================
# MAIN
# ============================================================

def main():
    log_hands("=" * 40)
    log_hands(f"hands cycle starting (dry_run={DRY_RUN})")

    if not acquire_lock("hands"):
        log_hands("lock held by another hands process, exiting")
        sys.exit(0)

    try:
        init_db()
        db = get_db()
        voice = load_voice()

        action, ctx = pick_action(db, voice)
        log_hands(f"action={action}")

        if action != "idle":
            execute_action(db, voice, action, ctx)
        else:
            log_hands("cycle complete: idle")

        db.close()
    except Exception as e:
        log_hands(f"fatal error: {e}", level="error")
        log_hands(traceback.format_exc(), level="error")
    finally:
        release_lock()
        log_hands("hands cycle done")


if __name__ == "__main__":
    main()
