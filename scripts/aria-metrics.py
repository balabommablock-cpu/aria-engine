#!/usr/bin/env python3
"""
aria-metrics.py  --  ARIA v2.1: Automated Metrics Scraper

Navigates to own X profile via CDP, reads analytics for each
posted tweet, updates posted.jsonl with real numbers, and
triggers a weight recalculation.

Usage:
    python3 aria-metrics.py              # scrape + update weights
    python3 aria-metrics.py --scrape-only # just scrape, no learning
    python3 aria-metrics.py --dry-run    # show what would update
"""

import json, os, sys, subprocess, argparse, re, time, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urllib_request

# --- anti-detection: random startup delay (0-20 min) ---
if "--dry-run" not in sys.argv:
    _jitter = random.randint(60, 1200)
    print(f"[jitter] sleeping {_jitter}s before metrics run")
    time.sleep(_jitter)

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE", os.path.expanduser("~/.openclaw/agents/aria/workspace")))
POSTED_PATH = WORKSPACE / "memory" / "posted.jsonl"
VOICE_PATH = WORKSPACE / "voice.json"
LOG_PATH = WORKSPACE / "logs" / "metrics.log"
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:28800")
X_USERNAME = os.environ.get("X_USERNAME", "BalabommaRao")

SCRAPE_SCRIPT = """
// Scrapes tweet analytics from own profile page via CDP
// Expects to be on the user's profile page already
// Returns array of {text_preview, impressions, likes, replies, retweets, bookmarks}

async function scrapeMetrics(page) {
    // Get all tweet articles on the page
    const tweets = await page.evaluate(() => {
        const articles = document.querySelectorAll('article[data-testid="tweet"]');
        const results = [];
        for (const article of articles) {
            try {
                // Get tweet text
                const textEl = article.querySelector('[data-testid="tweetText"]');
                const text = textEl ? textEl.textContent.trim() : '';
                const preview = text.substring(0, 80);

                // Get metrics from aria labels on action buttons
                const groups = article.querySelectorAll('[role="group"]');
                let likes = 0, replies = 0, retweets = 0, bookmarks = 0, impressions = 0;

                for (const group of groups) {
                    const buttons = group.querySelectorAll('button');
                    for (const btn of buttons) {
                        const label = btn.getAttribute('aria-label') || '';
                        const match = label.match(/(\\d[\\d,]*)\\s*(repl|like|repost|bookmark|view)/i);
                        if (match) {
                            const num = parseInt(match[1].replace(/,/g, ''));
                            const type = match[2].toLowerCase();
                            if (type.startsWith('repl')) replies = num;
                            else if (type.startsWith('like')) likes = num;
                            else if (type.startsWith('repost')) retweets = num;
                            else if (type.startsWith('bookmark')) bookmarks = num;
                            else if (type.startsWith('view')) impressions = num;
                        }
                    }
                }

                // Also try the analytics link for impressions
                const analyticsLink = article.querySelector('a[href*="/analytics"]');
                if (analyticsLink) {
                    const aLabel = analyticsLink.getAttribute('aria-label') || '';
                    const viewMatch = aLabel.match(/(\\d[\\d,]*)\\s*view/i);
                    if (viewMatch) {
                        impressions = parseInt(viewMatch[1].replace(/,/g, ''));
                    }
                }

                results.push({
                    text_preview: preview,
                    impressions, likes, replies, retweets, bookmarks
                });
            } catch (e) {
                // skip malformed tweets
            }
        }
        return results;
    });
    return tweets;
}
"""


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


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


def check_cdp():
    try:
        req = urllib_request.Request(f"{CDP_URL}/json/version")
        with urllib_request.urlopen(req, timeout=5):
            return True
    except:
        return False


def scrape_profile_metrics():
    """Navigate to own profile via CDP and scrape tweet metrics."""
    script_path = WORKSPACE / "scripts" / "_metrics_scraper.js"

    node_script = f"""
const CDP = require('chrome-remote-interface');
(async () => {{
    let client;
    try {{
        client = await CDP({{port: 28800}});
        const {{Page, Runtime}} = client;
        await Page.enable();

        // Navigate to own profile
        await Page.navigate({{url: 'https://x.com/{X_USERNAME}'}});
        await new Promise(r => setTimeout(r, 6000));

        // Scroll down to load more tweets
        for (let i = 0; i < 3; i++) {{
            await Runtime.evaluate({{expression: 'window.scrollBy(0, 800)'}});
            await new Promise(r => setTimeout(r, 2000));
        }}

        // Scroll back up
        await Runtime.evaluate({{expression: 'window.scrollTo(0, 0)'}});
        await new Promise(r => setTimeout(r, 1000));

        // Scrape metrics from all visible tweets
        const result = await Runtime.evaluate({{
            expression: `
                (function() {{
                    const articles = document.querySelectorAll('article[data-testid="tweet"]');
                    const results = [];
                    for (const article of articles) {{
                        try {{
                            const textEl = article.querySelector('[data-testid="tweetText"]');
                            const text = textEl ? textEl.textContent.trim() : '';
                            const preview = text.substring(0, 100);

                            let likes = 0, replies = 0, retweets = 0, bookmarks = 0, impressions = 0;
                            const buttons = article.querySelectorAll('[role="group"] button');
                            for (const btn of buttons) {{
                                const label = btn.getAttribute('aria-label') || '';
                                const m = label.match(/(\\\\d[\\\\d,]*)\\\\s*(repl|like|repost|bookmark|view)/i);
                                if (m) {{
                                    const num = parseInt(m[1].replace(/,/g, ''));
                                    const type = m[2].toLowerCase();
                                    if (type.startsWith('repl')) replies = num;
                                    else if (type.startsWith('like')) likes = num;
                                    else if (type.startsWith('repost') || type.startsWith('retw')) retweets = num;
                                    else if (type.startsWith('bookmark')) bookmarks = num;
                                    else if (type.startsWith('view')) impressions = num;
                                }}
                            }}

                            const aLink = article.querySelector('a[href*="/analytics"]');
                            if (aLink) {{
                                const al = aLink.getAttribute('aria-label') || aLink.textContent || '';
                                const vm = al.match(/(\\\\d[\\\\d,]*)\\\\s*view/i);
                                if (vm) impressions = parseInt(vm[1].replace(/,/g, ''));
                                if (!impressions) {{
                                    const nm = al.match(/(\\\\d[\\\\d,.KkMm]+)/);
                                    if (nm) {{
                                        let v = nm[1].replace(/,/g, '');
                                        if (v.match(/[kK]$/)) v = parseFloat(v) * 1000;
                                        else if (v.match(/[mM]$/)) v = parseFloat(v) * 1000000;
                                        impressions = Math.round(parseFloat(v));
                                    }}
                                }}
                            }}

                            if (text) results.push({{text_preview: preview, impressions, likes, replies, retweets, bookmarks}});
                        }} catch(e) {{}}
                    }}
                    return results;
                }})()
            `,
            returnByValue: true
        }});

        console.log(JSON.stringify(result.result.value || []));
    }} catch (e) {{
        console.error(e.message);
        process.exit(1);
    }} finally {{
        if (client) await client.close();
    }}
}})();
"""
    script_path.parent.mkdir(parents=True, exist_ok=True)
    with open(script_path, "w") as f:
        f.write(node_script)

    try:
        result = subprocess.run(
            ["node", str(script_path)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        else:
            log(f"scrape error: {result.stderr[:300]}")
            return []
    except Exception as e:
        log(f"scrape exception: {e}")
        return []


def match_metrics_to_posts(scraped, posts):
    """Match scraped metrics to posted tweets by text similarity."""
    updated = 0
    for post in posts:
        if post.get("status") != "live":
            continue
        post_text = post.get("text", "").lower()[:80]
        best_match = None
        best_score = 0

        for s in scraped:
            preview = s.get("text_preview", "").lower()[:80]
            # simple overlap
            words_post = set(post_text.split())
            words_scrape = set(preview.split())
            if not words_post:
                continue
            overlap = len(words_post & words_scrape) / len(words_post)
            if overlap > best_score and overlap > 0.5:
                best_score = overlap
                best_match = s

        if best_match:
            post["metrics"] = {
                "impressions": best_match.get("impressions", 0),
                "likes": best_match.get("likes", 0),
                "replies": best_match.get("replies", 0),
                "retweets": best_match.get("retweets", 0),
                "bookmarks": best_match.get("bookmarks", 0),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "algo_score": compute_algo_score(best_match)
            }
            updated += 1
            log(f"  matched: \"{post_text[:50]}...\" "
                f"imp={best_match.get('impressions', 0)} "
                f"likes={best_match.get('likes', 0)} "
                f"replies={best_match.get('replies', 0)} "
                f"bookmarks={best_match.get('bookmarks', 0)}")

    return updated


def compute_algo_score(metrics):
    """Compute X algorithm score from engagement metrics."""
    return (
        metrics.get("likes", 0) * 1 +
        metrics.get("retweets", 0) * 20 +
        metrics.get("replies", 0) * 13.5 +
        metrics.get("bookmarks", 0) * 10
    )


def get_follower_count():
    """Scrape current follower count from profile page."""
    try:
        script = f"""
const CDP = require('chrome-remote-interface');
(async () => {{
    const client = await CDP({{port: 28800}});
    const {{Page, Runtime}} = client;
    await Page.enable();
    await Page.navigate({{url: 'https://x.com/{X_USERNAME}'}});
    await new Promise(r => setTimeout(r, 4000));
    const result = await Runtime.evaluate({{
        expression: `
            (function() {{
                const links = document.querySelectorAll('a[href*="/verified_followers"], a[href*="/followers"]');
                for (const link of links) {{
                    const text = link.textContent || '';
                    const m = text.match(/(\\\\d+)/);
                    if (m) return parseInt(m[1]);
                }}
                return 0;
            }})()
        `,
        returnByValue: true
    }});
    console.log(result.result.value || 0);
    await client.close();
}})();
"""
        script_path = WORKSPACE / "scripts" / "_follower_count.js"
        with open(script_path, "w") as f:
            f.write(script)
        result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            return int(result.stdout.strip() or 0)
    except:
        pass
    return None


def trigger_learning():
    """Run aria-learn.py to update weights based on new metrics."""
    learn_script = WORKSPACE / "scripts" / "aria-learn.py"
    if learn_script.exists():
        log("triggering learning cycle...")
        subprocess.run(
            [sys.executable, str(learn_script)],
            cwd=str(WORKSPACE),
            timeout=120
        )
    else:
        log("aria-learn.py not found, skipping learning trigger")


def main():
    parser = argparse.ArgumentParser(description="ARIA v2.1: Metrics Scraper")
    parser.add_argument("--scrape-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log("=" * 60)
    log("ARIA metrics starting")

    posts = load_posted()
    live_posts = [p for p in posts if p.get("status") == "live"]
    log(f"posted tweets: {len(live_posts)}")

    if not live_posts:
        log("no live posts to check")
        return

    if not check_cdp():
        log("CDP not running. start it first.")
        return

    # Scrape metrics
    log("scraping profile metrics via CDP...")
    scraped = scrape_profile_metrics()
    log(f"scraped {len(scraped)} tweets from profile")

    if not scraped:
        log("no metrics scraped. CDP may need manual login check.")
        return

    # Match and update
    updated = match_metrics_to_posts(scraped, posts)
    log(f"updated metrics for {updated}/{len(live_posts)} posts")

    # Get follower count
    follower_count = get_follower_count()
    if follower_count is not None:
        log(f"current followers: {follower_count}")
        # Append follower snapshot
        snapshot_path = WORKSPACE / "memory" / "followers.jsonl"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        with open(snapshot_path, "a") as f:
            f.write(json.dumps({
                "count": follower_count,
                "checked_at": datetime.now(timezone.utc).isoformat()
            }) + "\n")

    if not args.dry_run:
        save_posted(posts)
        log("posted.jsonl updated")

        # Trigger learning
        if not args.scrape_only:
            trigger_learning()
    else:
        log("DRY RUN: would update metrics above")

    log("metrics complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
