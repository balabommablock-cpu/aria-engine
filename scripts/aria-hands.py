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
            # prefer stderr for the actual exception; fall back to stdout for
            # scripts that only write to stdout. cap at 1000 so the full node
            # output is visible in hands.log when diagnosing failures.
            err = result.stderr.strip()[:1000] or result.stdout.strip()[:1000] or "unknown error"
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
            err = result.stderr.strip()[:1000] or result.stdout.strip()[:1000] or "unknown error"
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


def find_recent_tweet_with_text(handle):
    """Navigate to a target's profile and grab URL + text of their highest-engagement recent tweet.
    Scans up to 5 valid tweets and picks the one with the most likes.
    Returns (tweet_url, tweet_text) or (None, None)."""
    clean_handle = handle.lstrip("@")
    script = f"""
const {{ chromium }} = require('playwright');

(async () => {{
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('https://x.com/{clean_handle}', {{ waitUntil: 'domcontentloaded', timeout: 30000 }});
  await page.waitForSelector('article[data-testid="tweet"]', {{ timeout: 15000 }});
  await page.waitForTimeout({random.randint(2000, 4000)});

  const targetHandle = '{clean_handle}'.toLowerCase();
  const result = await page.evaluate((handle) => {{
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    const candidates = [];
    const MAX_CANDIDATES = 5;

    for (const art of articles) {{
      if (candidates.length >= MAX_CANDIDATES) break;

      // skip pinned tweets
      const pinned = art.querySelector('[data-testid="socialContext"]');
      if (pinned && pinned.innerText.toLowerCase().includes('pinned')) continue;

      // skip retweets (socialContext says "X reposted")
      if (pinned && pinned.innerText.toLowerCase().includes('reposted')) continue;

      // verify this tweet is actually by the target author
      const authorLinks = art.querySelectorAll('a[role="link"]');
      let isAuthor = false;
      for (const link of authorLinks) {{
        const href = (link.getAttribute('href') || '').toLowerCase();
        if (href === '/' + handle) {{
          isAuthor = true;
          break;
        }}
      }}
      if (!isAuthor) continue;

      // check tweet freshness (skip tweets older than 48 hours)
      const timeEl = art.querySelector('time[datetime]');
      if (timeEl) {{
        const tweetDate = new Date(timeEl.getAttribute('datetime'));
        const ageHours = (Date.now() - tweetDate.getTime()) / (1000 * 60 * 60);
        if (ageHours > 720) continue;  // 30 days: high-value targets may tweet infrequently
      }}

      // skip subscription-locked tweets (buttons disabled, "Subscribe to unlock")
      const artText = art.innerText || '';
      if (artText.includes('Subscribe to unlock')) continue;
      const replyBtn = art.querySelector('button[data-testid="reply"]');
      if (replyBtn && (replyBtn.disabled || replyBtn.getAttribute('aria-disabled') === 'true')) continue;

      // get the status link and tweet text
      const links = art.querySelectorAll('a[href*="/status/"]');
      for (const a of links) {{
        const href = a.getAttribute('href');
        if (href && href.match(/\\/status\\/\\d+$/) && href.toLowerCase().includes('/' + handle + '/')) {{
          const textEl = art.querySelector('[data-testid="tweetText"]');
          const text = textEl ? textEl.innerText : '';

          // extract like count from engagement group buttons
          let likes = 0;
          const groupBtns = art.querySelectorAll('[role="group"] button');
          for (const btn of groupBtns) {{
            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
            const likeMatch = label.match(/(\\d+)\\s*like/);
            if (likeMatch) {{
              likes = parseInt(likeMatch[1], 10);
              break;
            }}
          }}

          candidates.push({{
            url: 'https://x.com' + href,
            text: text.substring(0, 500),
            likes: likes
          }});
          break;  // found the status link for this article, move to next article
        }}
      }}
    }}

    if (candidates.length === 0) return 'null';

    // pick the tweet with the highest like count; fall back to first if all have 0 likes
    const hasEngagement = candidates.some(c => c.likes > 0);
    let best;
    if (hasEngagement) {{
      best = candidates.reduce((a, b) => (b.likes > a.likes ? b : a));
    }} else {{
      best = candidates[0];
    }}
    return JSON.stringify({{ url: best.url, text: best.text }});
  }}, targetHandle);

  if (result === 'null') {{
    // second pass: if no author-matched tweet found, log it and return null
    // (better to skip than reply to wrong person's tweet)
    console.log('null');
  }} else {{
    console.log(result);
  }}
  await browser.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    success, output = _run_inline_node_raw(script, "find_tweet", timeout=60)
    if success and output.strip() and output.strip() != "null":
        try:
            # parse the last line (in case node logs other things)
            last_line = output.strip().split("\n")[-1].strip()
            data = json.loads(last_line)
            url = data.get("url", "")
            text = data.get("text", "")
            if "x.com" in url and "/status/" in url:
                return url, text
        except (json.JSONDecodeError, KeyError):
            pass
    return None, None


def find_recent_tweet_url(handle):
    """Backward compat wrapper. Returns tweet_url or None."""
    url, _ = find_recent_tweet_with_text(handle)
    return url


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
    # node resolves require() from script file location, not cwd.
    # inline scripts live in workspace/scripts/ which has no node_modules.
    # NODE_PATH tells node to also search x-twitter-poster/node_modules.
    env["NODE_PATH"] = str(POST_TWEET_JS.parent / "node_modules")

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

def log_decision(db, actor, decision_type, before_state, decision, outcome=None):
    """Log a decision to the decision ledger for full traceability."""
    try:
        ts = now_utc().isoformat()
        db.execute(
            "INSERT INTO decision_ledger (ts, actor, decision_type, before_state, decision, outcome) "
            "VALUES (?,?,?,?,?,?)",
            (ts, actor, decision_type,
             json.dumps(before_state) if isinstance(before_state, dict) else str(before_state),
             decision,
             outcome)
        )
        db.commit()
    except Exception:
        pass  # never break the pipeline for ledger


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
    blacklist = ",".join(f"'{h}'" for h in shared.HANDLE_BLACKLIST)
    reply = db.execute(
        f"SELECT * FROM reply_drafts WHERE status='ready' "
        f"AND target_handle NOT IN ({blacklist}) "
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

    # --- P5: strategic_follow (cold start: follow before random likes) ---
    unfollowed = db.execute(
        "SELECT handle FROM reply_targets "
        "WHERE handle NOT IN (SELECT target_handle FROM engagements WHERE action='follow') "
        "ORDER BY priority ASC, RANDOM() LIMIT 1"
    ).fetchone()
    if unfollowed:
        log_hands(f"P5 strategic_follow: @{unfollowed['handle']}")
        return "strategic_follow", {"handle": unfollowed["handle"]}

    # --- P6: passive_action ---
    target = db.execute(
        "SELECT handle FROM reply_targets ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if target:
        log_hands(f"P6 passive_action: target={target['handle']}")
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
        elif action == "strategic_follow":
            _exec_strategic_follow(db, ctx)
        else:
            log_hands(f"unknown action: {action}")
    except Exception as e:
        log_hands(f"execute_action error ({action}): {e}", level="error")
        log_hands(traceback.format_exc(), level="error")


def _exec_post_tweet(db, voice, ctx):
    """Post the top queued tweet. Renders quote card if image_type calls for it."""
    post_id = ctx["id"]
    text = ctx["text"]
    image_type = ctx.get("image_type", "none")
    image_path = ctx.get("image_path")

    # render quote card / terminal screenshot if needed
    # RULE: quote card text must NEVER be the same as the tweet text
    if not image_path and image_type in ("quote_card", "terminal_screenshot"):
        try:
            sys.path.insert(0, str(WORKSPACE / "scripts"))
            import importlib
            qc = importlib.import_module("aria-quote-card")
            # for quote cards, use card_text (different from tweet). fall back to text-only if empty
            render_text = text
            if image_type == "quote_card":
                card_text = ctx.get("card_text", "")
                if card_text and card_text.strip():
                    render_text = card_text
                    log_hands(f"post_tweet: card uses complementary text: \"{card_text[:60]}\"")
                else:
                    log_hands(f"post_tweet: no card_text, skipping quote card")
                    image_type = "none"
                    render_text = None
            if render_text:
                rendered = qc.render_for_queue_item(render_text, image_type, post_id)
                if rendered:
                    image_path = rendered
                    log_hands(f"post_tweet: rendered {image_type} -> {rendered}")
        except Exception as e:
            log_hands(f"post_tweet: image render failed ({e}), posting text-only")

    # mark as posting
    db.execute("UPDATE queue SET status='posting' WHERE id=?", (post_id,))
    db.commit()

    # anti-detection delay: tight but random
    delay = random.randint(15, 60)
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

        # post-publish verification: check if tweet actually appeared
        verified = False
        if tweet_url:
            verified = True  # URL returned means CDP confirmed it
            log_hands(f"post_tweet: verified via URL return")
        else:
            # no URL from CDP, try to verify by checking profile
            log_hands(f"post_tweet: no URL captured, verifying via profile check...")
            time.sleep(10)
            found_url = find_recent_tweet_url(X_USERNAME)
            if found_url:
                tweet_url = found_url
                verified = True
                log_hands(f"post_tweet: verified via profile, url={found_url}")
            else:
                log_hands(f"post_tweet: WARNING could not verify post appeared")

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

        status = "VERIFIED" if verified else "UNVERIFIED"
        log_hands(f"post_tweet {status}: {post_id} -> {tweet_url or 'url not captured'}")
        send_telegram(
            f"<b>ARIA posted</b> ({status.lower()})\n"
            f"{text[:200]}\n\n"
            f"territory: {ctx.get('territory', '?')}\n"
            f"url: {tweet_url or 'n/a'}"
        )

        # decision ledger: record the full reasoning
        scores = {}
        try:
            scores = json.loads(ctx.get("scores_json", "{}") or "{}")
        except Exception:
            pass
        log_decision(db, "hands", "post_tweet", {
            "queue_depth": db.execute("SELECT COUNT(*) as c FROM queue WHERE status='queued'").fetchone()["c"],
            "territory": ctx.get("territory"),
            "composite_score": scores.get("composite"),
            "hook_pattern": scores.get("hook_pattern"),
            "image_type": ctx.get("image_type", "none"),
            "khud_tweet_guidance": (get_state(db, "khud.tweet_guidance") or "")[:200],
        }, f"posted tweet {post_id}: {text[:120]}", f"{status} url={tweet_url}")
    else:
        # revert to queued
        db.execute("UPDATE queue SET status='queued' WHERE id=?", (post_id,))
        db.commit()
        log_hands(f"post_tweet FAILED: {result}", level="error")
        send_telegram(f"<b>ARIA post failed</b>\n{result[:200]}")

        log_decision(db, "hands", "post_tweet_failed", {
            "territory": ctx.get("territory"),
            "image_type": ctx.get("image_type", "none"),
        }, f"attempted tweet {post_id}: {text[:120]}", f"FAILED: {result[:200]}")


def _generate_contextual_reply(voice, target_handle, tweet_text, recent_replies):
    """Generate a reply that actually responds to the specific tweet.
    Returns reply text or None."""
    golden = voice.get("golden_tweets", [])
    examples = random.sample(golden, min(4, len(golden)))
    examples_text = "\n".join(f"- {e['text']}" for e in examples)

    # check if Claude Khud left reply guidance
    khud_guidance = ""
    try:
        _db = get_db()
        khud_guidance = get_state(_db, "khud.reply_guidance") or ""
        _db.close()
    except:
        pass

    # show recent replies so Claude avoids repeating patterns
    avoid_text = ""
    if recent_replies:
        avoid_text = "\nRECENT REPLIES (do NOT repeat these openings or patterns):\n"
        avoid_text += "\n".join(f"- {r}" for r in recent_replies[-5:])

    hard_bans = voice.get("hard_bans", {})
    ban_words = ", ".join(hard_bans.get("words", [])[:15])

    prompt = f"""you are @BalabommaRao replying to @{target_handle}'s tweet.

THE ACTUAL TWEET YOU ARE REPLYING TO:
"{tweet_text}"

YOUR VOICE (match this exactly):
{examples_text}
{avoid_text}

WRITE A REPLY THAT:
1. directly responds to what @{target_handle} said. reference their specific point.
2. adds your own angle, a concrete example, or a counter-observation
3. sounds like a peer who read the tweet and had a genuine reaction
4. does NOT start with the same word as any recent reply above
5. does NOT start with "built" or any past participle opener unless it's genuinely the best way in

WHAT MAKES A GOOD REPLY:
- it shows you understood the tweet, not just the topic
- it adds something the original author didn't say
- it might make the author want to reply back
- it reads like one human talking to another

WHAT MAKES A BAD REPLY:
- generic observation that could apply to any tweet on this topic
- starts with the same pattern every time (e.g. "built X that Y")
- flattery: "great point", "so true", "this", "100%"
- advice: "you should", "try to", "have you considered"
- sounds like a standalone tweet shoved into a reply slot

CREATIVE DIRECTION FROM CLAUDE KHUD (your strategist brain -- follow this):
{khud_guidance if khud_guidance else "(no specific guidance -- use your own judgment)"}

RULES:
- natural case. capitalize where it reads better. not ALL CAPS.
- max 260 characters (substantive but tight. premium account, replies rank higher, so make them count.)
- no em dashes, no hyphens as formatting, no hashtags, no exclamation marks
- no banned words: {ban_words}
- no links, no @mentions in the body
- periods only for punctuation

RESPOND WITH ONLY THE REPLY TEXT. nothing else."""

    result = call_claude(prompt)
    if not result:
        return None

    # clean (no longer forcing lowercase)
    text = result.strip().strip('"').strip("'").strip()
    for ch in hard_bans.get("characters", []):
        text = text.replace(ch, "")
    text = text.strip()

    # basic validation (premium: longer replies rank higher)
    if len(text) < 10 or len(text) > 280:
        return None

    # flattery check
    for fp in ["great point", "so true", "love this", "this is spot on",
               "well said", "couldn't agree more", "nailed it", "exactly",
               "100%", "brilliant"]:
        if fp in text:
            return None

    return text


def _exec_post_reply(db, voice, ctx):
    """Post an outbound reply to a target account.
    Key improvement: generates a CONTEXTUAL reply using the actual tweet text,
    not the generic pre-generated draft from brain."""
    draft_id = ctx["id"]
    target_handle = ctx["target_handle"]
    fallback_text = ctx["reply_text"]  # brain's pre-generated draft (fallback)
    target_tweet_url = ctx.get("target_tweet_url", "")

    # mark as posting
    db.execute("UPDATE reply_drafts SET status='posting' WHERE id=?", (draft_id,))
    db.commit()

    if DRY_RUN:
        log_hands(f"DRY RUN: would reply to {target_handle}: {fallback_text[:80]}")
        db.execute("UPDATE reply_drafts SET status='ready' WHERE id=?", (draft_id,))
        db.commit()
        return

    # anti-detection delay: fast for cold start, still random
    delay = random.randint(10, 45)
    log_hands(f"post_reply: delay {delay}s before replying to {target_handle}")
    time.sleep(delay)

    # find the actual tweet (URL + text)
    tweet_url = target_tweet_url
    tweet_text = ""
    if not tweet_url or not tweet_url.startswith("http") or "/status/" not in tweet_url:
        log_hands(f"post_reply: finding tweet for @{target_handle}")
        tweet_url, tweet_text = find_recent_tweet_with_text(target_handle)
        if not tweet_url:
            log_hands(f"post_reply: could not find tweet for @{target_handle}, failing", level="error")
            db.execute("UPDATE reply_drafts SET status='failed' WHERE id=?", (draft_id,))
            db.commit()
            return

    log_hands(f"post_reply: target tweet -> {tweet_url}")
    if tweet_text:
        log_hands(f"post_reply: tweet text -> \"{tweet_text[:80]}...\"")

    # dedup: don't reply to the same tweet twice
    already = db.execute(
        "SELECT id FROM reply_drafts WHERE target_tweet_url=? AND status='posted' LIMIT 1",
        (tweet_url,)
    ).fetchone()
    if already:
        log_hands(f"post_reply: already replied to {tweet_url}, skipping")
        db.execute("UPDATE reply_drafts SET status='expired' WHERE id=?", (draft_id,))
        db.commit()
        return

    # like the tweet before replying (natural human behavior, shows in their notifs)
    if random.random() < 0.7:  # 70% of the time, like first
        _like_tweet(tweet_url)
        time.sleep(random.randint(3, 8))  # brief pause between like and reply

    # generate a contextual reply using the actual tweet text
    reply_text = fallback_text  # default to brain's draft
    if tweet_text and len(tweet_text) > 10:
        # get recent replies to avoid pattern repetition
        recent = db.execute(
            "SELECT reply_text FROM reply_drafts WHERE status='posted' "
            "ORDER BY posted_at DESC LIMIT 5"
        ).fetchall()
        recent_replies = [r["reply_text"] for r in recent]

        contextual = _generate_contextual_reply(
            voice, target_handle, tweet_text, recent_replies
        )
        if contextual:
            reply_text = contextual
            log_hands(f"post_reply: using contextual reply (not brain draft)")
        else:
            log_hands(f"post_reply: contextual generation failed, using brain draft")
    else:
        log_hands(f"post_reply: no tweet text available, using brain draft")

    success, result = do_post_reply(tweet_url, reply_text)

    ts = now_utc().isoformat()
    if success:
        db.execute(
            "UPDATE reply_drafts SET status='posted', posted_at=?, reply_text=?, target_tweet_url=? WHERE id=?",
            (ts, reply_text, tweet_url, draft_id)
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

        # decision ledger
        was_contextual = (reply_text != fallback_text)
        log_decision(db, "hands", "post_reply", {
            "target": target_handle,
            "tweet_text": (tweet_text or "")[:200],
            "was_contextual": was_contextual,
            "brain_draft": fallback_text[:120],
            "khud_reply_guidance": (get_state(db, "khud.reply_guidance") or "")[:200],
        }, f"replied to @{target_handle}: {reply_text[:120]}", f"SUCCESS url={tweet_url}")
    else:
        db.execute("UPDATE reply_drafts SET status='failed' WHERE id=?", (draft_id,))
        db.commit()
        log_hands(f"post_reply FAILED: {result}", level="error")

        log_decision(db, "hands", "post_reply_failed", {
            "target": target_handle,
            "tweet_url": tweet_url,
        }, f"attempted reply to @{target_handle}", f"FAILED: {result[:200]}")


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

    # self-reply timing: 120-180s looks natural and hits the algo's
    # first scoring pass while the tweet is still fresh
    delay = random.randint(120, 180)
    log_hands(f"self_reply: delay {delay}s (targeting 2-3min window)")
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


def _exec_strategic_follow(db, ctx):
    """Follow a target account. Cold start growth hack:
    many people check who followed them and follow back if profile is interesting."""
    handle = ctx["handle"]
    log_hands(f"strategic_follow: @{handle}")

    if DRY_RUN:
        log_hands(f"DRY RUN: would follow @{handle}")
        return

    delay = random.randint(5, 20)
    time.sleep(delay)

    clean_handle = handle.lstrip("@")
    script = f"""
const {{ chromium }} = require('playwright');
(async () => {{
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('https://x.com/{clean_handle}', {{ waitUntil: 'domcontentloaded', timeout: 30000 }});
  await page.waitForTimeout({random.randint(2000, 4000)});

  // check if already following
  const followBtn = await page.$('[data-testid$="-follow"]');
  if (!followBtn) {{
    console.log('NO_FOLLOW_BTN');
    await browser.close();
    return;
  }}
  const label = await followBtn.getAttribute('data-testid');
  if (label && label.includes('unfollow')) {{
    console.log('ALREADY_FOLLOWING');
    await browser.close();
    return;
  }}

  await followBtn.click();
  await page.waitForTimeout({random.randint(1000, 2000)});
  console.log('FOLLOWED');
  await browser.close();
}})().catch(e => {{ console.error(e.message); process.exit(1); }});
"""
    success, output = _run_inline_node_raw(script, "follow", timeout=45)
    ts = now_utc().isoformat()

    if success and "FOLLOWED" in output:
        db.execute(
            "INSERT INTO engagements (action, target_handle, performed_at) "
            "VALUES ('follow', ?, ?)",
            (handle, ts)
        )
        db.commit()
        log_hands(f"strategic_follow SUCCESS: @{handle}")
    elif "ALREADY_FOLLOWING" in (output or ""):
        # record so we don't try again
        db.execute(
            "INSERT INTO engagements (action, target_handle, performed_at) "
            "VALUES ('follow', ?, ?)",
            (handle, ts)
        )
        db.commit()
        log_hands(f"strategic_follow: already following @{handle}")
    else:
        log_hands(f"strategic_follow FAILED: @{handle} - {output[:100]}")


# ============================================================
# MAIN
# ============================================================

def _like_tweet(tweet_url):
    """Like a tweet as a pre-reply engagement signal. Failures are non-fatal."""
    try:
        success, msg = do_like_tweet(tweet_url)
        if success:
            log_hands(f"pre-reply like: {tweet_url}")
        else:
            log_hands(f"pre-reply like failed (non-fatal): {msg}")
    except Exception:
        pass  # liking is optional, never fail the reply flow


def _humanize_browse(seconds=None):
    """Scroll the timeline before taking an action. Makes CDP sessions
    look like a human who opened X, read some tweets, then acted.
    X's detection looks for accounts that ONLY compose and never browse."""
    if seconds is None:
        seconds = random.randint(8, 25)
    try:
        script = f"""
const {{ chromium }} = require('playwright');
(async () => {{
  const browser = await chromium.connectOverCDP('{CDP_URL}');
  const page = browser.contexts()[0].pages()[0];
  const currentUrl = page.url();

  // only browse timeline if we're on x.com
  if (!currentUrl.includes('x.com')) {{
    await browser.close();
    return;
  }}

  // go to home timeline
  await page.goto('https://x.com/home', {{ waitUntil: 'domcontentloaded', timeout: 15000 }});
  await page.waitForTimeout({random.randint(2000, 4000)});

  // scroll down naturally (humans don't just stare at the top)
  for (let i = 0; i < {random.randint(2, 5)}; i++) {{
    await page.mouse.wheel(0, {random.randint(300, 700)});
    await page.waitForTimeout({random.randint(800, 2500)});
  }}

  // maybe pause on a tweet (dwell time looks human)
  await page.waitForTimeout({random.randint(1000, 3000)});

  await browser.close();
}})().catch(() => {{}});
"""
        log_hands(f"humanize: browsing timeline for ~{seconds}s")
        _run_inline_node_raw(script, "browse", timeout=30)
        log_hands("humanize: browse done")
    except Exception:
        log_hands("humanize: browse skipped (cdp issue)")  # optional, never fail the cycle


def _check_daily_cap(db) -> bool:
    """Safety cap per day (IST timezone reset).
    24/7 mode: 120 actions/day (15 tweets + ~105 replies/likes/follows).
    Returns True if under cap, False if we should skip this cycle."""
    # IST midnight = 18:30 UTC previous day
    from datetime import timedelta as td
    ist_now = now_utc() + td(hours=5, minutes=30)
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0)
    utc_start = (ist_midnight - td(hours=5, minutes=30)).isoformat()

    tweets_today = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' "
        "AND message = 'action=post_tweet' AND ts > ?",
        (utc_start,)
    ).fetchone()["c"]

    total_today = db.execute(
        "SELECT COUNT(*) as c FROM engine_log WHERE process='hands' "
        "AND message LIKE 'action=%' AND ts > ?",
        (utc_start,)
    ).fetchone()["c"]

    if tweets_today >= 15:
        log_hands(f"daily cap: {tweets_today} tweets (max 15)")
        return False
    if total_today >= 120:
        log_hands(f"daily cap: {total_today} total actions (max 120)")
        return False
    return True


def _random_skip() -> bool:
    """10% chance to skip a cycle entirely. humans don't check X every
    single 10 minutes like clockwork. random gaps look natural."""
    return random.random() < 0.10


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

        # anti-detection: random skip (disabled during cold-start catch-up)
        # if _random_skip():
        #     log_hands("random skip (humanization)")
        #     db.close()
        #     return

        # anti-detection: daily action cap
        if not _check_daily_cap(db):
            log_hands("daily cap reached, skipping")
            # send one telegram per day when cap first hits so it doesn't look
            # like the system went silent
            cap_key = f"hands.cap_notified_{now_utc().strftime('%Y-%m-%d')}"
            if not get_state(db, cap_key):
                set_state(db, cap_key, now_utc().isoformat())
                q = db.execute("SELECT COUNT(*) as c FROM queue WHERE status='queued'").fetchone()["c"]
                r = db.execute("SELECT COUNT(*) as c FROM reply_drafts WHERE status='ready'").fetchone()["c"]
                send_telegram(f"daily cap hit. paused until midnight IST.\nqueue: {q} originals, {r} replies ready")
            db.close()
            return

        # anti-detection: browse timeline before acting (look human)
        _humanize_browse()

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
