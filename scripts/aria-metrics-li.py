#!/usr/bin/env python3
"""
aria-metrics-li.py -- LinkedIn Metrics Scraper (L26-L28).

Scrapes post analytics, profile analytics, and follower data.
Runs daily via launchd. Feeds into learning loops.

Each cycle:
  1. Scrape metrics for recent posts via CDP (navigate to each post)
  2. Scrape profile analytics from LinkedIn dashboard
  3. Record follower count
  4. Trigger phase advancement check (L36)
"""

from __future__ import annotations

import json, os, sys, subprocess, random, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import importlib
shared = importlib.import_module("aria-shared")
li_db = importlib.import_module("aria-linkedin-db")

get_db        = shared.get_db
init_db       = shared.init_db
send_telegram = shared.send_telegram
now_utc       = shared.now_utc
parse_ts      = shared.parse_ts
ts_age_hours  = shared.ts_age_hours
get_state     = shared.get_state
set_state     = shared.set_state
acquire_lock  = shared.acquire_lock
release_lock  = shared.release_lock
DRY_RUN       = shared.DRY_RUN
WORKSPACE     = shared.WORKSPACE
log           = shared.log
CDP_URL       = shared.CDP_URL

VOICE_PATH = WORKSPACE / "voice.json"

# Phase thresholds (LinkedIn)
PHASE_THRESHOLDS = {
    1: 0,      # Phase 1: 0+ followers
    2: 100,    # Phase 2: 100+
    3: 500,    # Phase 3: 500+
    4: 2000,   # Phase 4: 2000+
    5: 5000,   # Phase 5: 5000+
}


def log_met(msg: str, level: str = "info"):
    log(msg, process="metrics_li", level=level)


# ============================================================
# CDP: Scrape post metrics from LinkedIn
# ============================================================

def scrape_post_metrics_cdp(post_url: str) -> dict:
    """Navigate to a post and extract engagement metrics.
    Returns {impressions, likes, comments, shares} or empty dict on failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log_met("playwright not available for sync import, using node fallback")
        return _scrape_post_metrics_node(post_url)

    return _scrape_post_metrics_node(post_url)


def _scrape_post_metrics_node(post_url: str) -> dict:
    """Scrape post metrics via a quick node script."""
    # We'll use read_post_comments.js which already navigates to the post
    # and we can extract engagement from there. For now, return empty
    # and rely on the read_comments script data.

    env = os.environ.copy()
    env["CDP_URL"] = CDP_URL
    env["NODE_PATH"] = os.path.expanduser(
        "~/.openclaw/workspace/skills/x-twitter-poster/node_modules"
    )

    script = f"""
const {{ chromium }} = require('playwright');
const {{ withCdpLock }} = require('./cdp-lock');

async function scrape() {{
  let browser, page;
  try {{
    browser = await chromium.connectOverCDP('{CDP_URL}');
    const context = browser.contexts()[0];
    page = await context.newPage();
    await page.goto('{post_url}', {{ waitUntil: 'domcontentloaded', timeout: 25000 }});
    await new Promise(r => setTimeout(r, 4000));

    const metrics = await page.evaluate(() => {{
      const likesEl = document.querySelector(
        '.social-details-social-counts__reactions-count, [data-test-id="social-actions__reaction-count"]'
      );
      const commentsEl = document.querySelector('button[aria-label*="comment"]');
      const sharesEl = document.querySelector('button[aria-label*="repost"]');

      const parse = (el) => {{
        if (!el) return 0;
        const t = (el.innerText || el.getAttribute('aria-label') || '').replace(/[^0-9]/g, '');
        return parseInt(t, 10) || 0;
      }};

      return {{
        likes: parse(likesEl),
        comments: parse(commentsEl),
        shares: parse(sharesEl),
        impressions: 0
      }};
    }});
    return metrics;
  }} catch (e) {{
    return {{ error: e.message }};
  }} finally {{
    try {{ if (page) await page.close(); }} catch(_) {{}}
    try {{ if (browser) await browser.close(); }} catch(_) {{}}
  }}
}}

withCdpLock(() => scrape(), 60000).then(r => {{
  console.log(JSON.stringify(r));
  process.exit(r.error ? 1 : 0);
}});
"""
    script_path = SCRIPTS_DIR / "_tmp_metrics_scrape.js"
    try:
        script_path.write_text(script)
        result = subprocess.run(
            ["node", str(script_path)],
            env=env, cwd=str(SCRIPTS_DIR),
            capture_output=True, text=True, timeout=90
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
        return {}
    except Exception as e:
        log_met(f"metrics scrape error: {e}", level="error")
        return {}
    finally:
        try:
            script_path.unlink()
        except Exception:
            pass


# ============================================================
# SCRAPE FOLLOWER COUNT
# ============================================================

def scrape_follower_count_cdp() -> int:
    """Navigate to own profile and extract follower count."""
    env = os.environ.copy()
    env["CDP_URL"] = CDP_URL
    env["NODE_PATH"] = os.path.expanduser(
        "~/.openclaw/workspace/skills/x-twitter-poster/node_modules"
    )

    script = """
const { chromium } = require('playwright');
const { withCdpLock } = require('./cdp-lock');

async function getFollowers() {
  let browser, page;
  try {
    browser = await chromium.connectOverCDP('%s');
    const context = browser.contexts()[0];
    page = await context.newPage();
    await page.goto('https://www.linkedin.com/in/me/', {
      waitUntil: 'domcontentloaded', timeout: 25000
    });
    await new Promise(r => setTimeout(r, 4000));

    const count = await page.evaluate(() => {
      // Look for follower count on profile
      const els = document.querySelectorAll('span, p, div');
      for (const el of els) {
        const t = (el.innerText || '').toLowerCase();
        const m = t.match(/(\\d[\\d,]+)\\s*follower/);
        if (m) return parseInt(m[1].replace(/,/g, ''), 10);
      }
      return 0;
    });
    return { count };
  } catch(e) {
    return { error: e.message, count: 0 };
  } finally {
    try { if (page) await page.close(); } catch(_) {}
    try { if (browser) await browser.close(); } catch(_) {}
  }
}

withCdpLock(() => getFollowers(), 60000).then(r => {
  console.log(JSON.stringify(r));
  process.exit(0);
});
""" % CDP_URL

    script_path = SCRIPTS_DIR / "_tmp_followers.js"
    try:
        script_path.write_text(script)
        result = subprocess.run(
            ["node", str(script_path)],
            env=env, cwd=str(SCRIPTS_DIR),
            capture_output=True, text=True, timeout=90
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return data.get("count", 0)
        return 0
    except Exception as e:
        log_met(f"follower scrape error: {e}", level="error")
        return 0
    finally:
        try:
            script_path.unlink()
        except Exception:
            pass


# ============================================================
# PHASE ADVANCEMENT (L36)
# ============================================================

def check_phase_advancement(db, follower_count: int):
    """Check if we should advance to the next phase."""
    voice = json.loads(VOICE_PATH.read_text()) if VOICE_PATH.exists() else {}
    current_phase = voice.get("phase", {}).get("current", 0)

    # Check LinkedIn-specific phases
    li_phase = int(get_state(db, "linkedin.phase", "1") or "1")

    for phase, threshold in sorted(PHASE_THRESHOLDS.items()):
        if follower_count >= threshold:
            new_phase = phase

    if new_phase > li_phase:
        set_state(db, "linkedin.phase", str(new_phase))
        log_met(f"PHASE ADVANCEMENT: {li_phase} -> {new_phase} ({follower_count} followers)")
        send_telegram(
            f"<b>ARIA LinkedIn Phase Advancement</b>\n\n"
            f"Phase {li_phase} -> Phase {new_phase}\n"
            f"Followers: {follower_count}\n\n"
            f"New loops unlocked for phase {new_phase}."
        )


# ============================================================
# MAIN
# ============================================================

def main():
    if not acquire_lock("metrics_li"):
        print("metrics scraper already running, exiting")
        return

    try:
        db = get_db()
        init_db()
        li_db.init_linkedin_tables(db)

        log_met("metrics cycle start")
        ts = now_utc().isoformat()

        # 1. Scrape post metrics for recent posts
        cutoff = (now_utc() - timedelta(days=14)).isoformat()
        recent_posts = db.execute(
            "SELECT id, content, post_url, posted_at FROM linkedin_posted "
            "WHERE posted_at > ? AND post_url IS NOT NULL "
            "ORDER BY posted_at DESC LIMIT 10",
            (cutoff,)
        ).fetchall()

        posts_scraped = 0
        for post in recent_posts:
            if not post["post_url"] or "linkedin.com" not in str(post["post_url"]):
                continue

            metrics = _scrape_post_metrics_node(post["post_url"])
            if metrics and not metrics.get("error"):
                db.execute(
                    "INSERT INTO li_post_metrics "
                    "(post_id, post_url, impressions, likes, comments, shares, scraped_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (post["id"], post["post_url"],
                     metrics.get("impressions", 0), metrics.get("likes", 0),
                     metrics.get("comments", 0), metrics.get("shares", 0), ts)
                )
                posts_scraped += 1
                log_met(f"post {post['id']}: likes={metrics.get('likes',0)}, "
                        f"comments={metrics.get('comments',0)}")

            # Delay between scrapes
            time.sleep(random.randint(5, 15))

        db.commit()
        log_met(f"scraped metrics for {posts_scraped} posts")

        # 2. Scrape follower count
        follower_count = scrape_follower_count_cdp()
        if follower_count > 0:
            # Get previous count for delta
            prev = db.execute(
                "SELECT count FROM li_followers ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
            prev_count = prev["count"] if prev else 0

            db.execute(
                "INSERT INTO li_followers (count, new_7d, checked_at) VALUES (?,?,?)",
                (follower_count, max(0, follower_count - prev_count), ts)
            )
            db.commit()
            log_met(f"followers: {follower_count} (prev: {prev_count}, delta: {follower_count - prev_count})")

            # 3. Phase advancement check
            check_phase_advancement(db, follower_count)
        else:
            log_met("could not scrape follower count")

        # Summary
        set_state(db, "metrics_li.last_run", ts)
        set_state(db, "metrics_li.last_result",
                  f"posts={posts_scraped}, followers={follower_count}")

        db.close()
        log_met("metrics cycle complete")

    except Exception as e:
        log_met(f"cycle CRASHED: {e}\n{traceback.format_exc()}", level="error")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
