#!/usr/bin/env python3
"""
aria-signals.py  --  ARIA v2.1: Signal Ingestion

Scrapes external signals to feed context-aware generation:
  1. Trending topics on X (via CDP scraping of Explore tab)
  2. RSS feeds from AI/tech/product blogs
  3. Recent high-engagement tweets from territory accounts

Saves to memory/signals.jsonl. aria-generate.py reads this
to make tweets topically relevant without being replies.

Usage:
    python3 aria-signals.py              # full signal scrape
    python3 aria-signals.py --rss-only   # just RSS, no CDP needed
    python3 aria-signals.py --dry-run    # show signals, don't save
"""

import json, os, sys, re, hashlib, argparse, time, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib import request as urllib_request
from xml.etree import ElementTree

# --- anti-detection: random startup delay (0-15 min) ---
if "--dry-run" not in sys.argv:
    _jitter = random.randint(30, 900)
    print(f"[jitter] sleeping {_jitter}s before signals run")
    time.sleep(_jitter)

WORKSPACE = Path(os.environ.get("ARIA_WORKSPACE", os.path.expanduser("~/.openclaw/agents/aria/workspace")))
SIGNALS_PATH = WORKSPACE / "memory" / "signals.jsonl"
LOG_PATH = WORKSPACE / "logs" / "signals.log"
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:28800")

# RSS feeds covering Rishabh's territory
RSS_FEEDS = [
    # AI / tech
    {"url": "https://blog.anthropic.com/rss.xml", "territory": "ai", "name": "Anthropic"},
    {"url": "https://openai.com/blog/rss.xml", "territory": "ai", "name": "OpenAI"},
    {"url": "https://blog.google/technology/ai/rss/", "territory": "ai", "name": "Google AI"},
    {"url": "https://lilianweng.github.io/index.xml", "territory": "ai", "name": "Lilian Weng"},
    {"url": "https://karpathy.ai/feed.xml", "territory": "ai", "name": "Karpathy"},
    # Product / building
    {"url": "https://www.svpg.com/feed/", "territory": "organizations", "name": "SVPG (Cagan)"},
    {"url": "https://world.hey.com/jason/feed.atom", "territory": "taste_agency", "name": "Jason Fried"},
    {"url": "https://www.lennysnewsletter.com/feed", "territory": "organizations", "name": "Lenny"},
    # Hacker News top (general signal)
    {"url": "https://hnrss.org/frontpage?count=10", "territory": "building", "name": "HN Front"},
]

# Accounts to monitor for topic signals (not for replying, for topic awareness)
TERRITORY_ACCOUNTS = [
    "@karpathy", "@nntaleb", "@fchollet", "@shreyas", "@jasonfried",
    "@sama", "@DarioAmodei", "@ESYudkowsky", "@AndrewYNg", "@chipro",
    "@aakashg0", "@lennysan", "@ID_AA_Carmack", "@AravSrinivas"
]

SIGNAL_EXPIRY_HOURS = 24
MAX_SIGNALS_KEPT = 100


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def fetch_rss(feed):
    """Fetch and parse an RSS/Atom feed. Returns list of signal dicts."""
    signals = []
    try:
        req = urllib_request.Request(feed["url"], headers={"User-Agent": "ARIA/2.1"})
        with urllib_request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        root = ElementTree.fromstring(content)

        # Handle both RSS and Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items[:5]:
            title = (item.findtext("title") or
                     item.findtext("atom:title", namespaces=ns) or "").strip()
            desc = (item.findtext("description") or
                    item.findtext("atom:summary", namespaces=ns) or "")
            # clean HTML from description
            desc = re.sub(r'<[^>]+>', '', desc).strip()[:300]
            link = item.findtext("link") or ""
            if not link:
                link_el = item.find("atom:link", ns)
                if link_el is not None:
                    link = link_el.get("href", "")

            if title:
                signals.append({
                    "type": "rss",
                    "source": feed["name"],
                    "territory": feed["territory"],
                    "title": title,
                    "summary": desc[:200],
                    "url": link,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "id": hashlib.md5(title.encode()).hexdigest()[:12]
                })
    except Exception as e:
        log(f"  RSS error ({feed['name']}): {e}")

    return signals


def scrape_x_trending_via_cdp():
    """
    Scrape X Explore/trending via CDP.
    Navigates to x.com/explore, reads trending topics.
    Returns list of signal dicts.
    """
    signals = []
    try:
        # Check CDP is running
        req = urllib_request.Request(f"{CDP_URL}/json/version")
        with urllib_request.urlopen(req, timeout=5) as resp:
            json.loads(resp.read().decode())

        # Use CDP to navigate to explore
        # This is a simplified version; full implementation uses puppeteer-like CDP protocol
        # For now, we use a node script approach
        import subprocess
        script = """
const CDP = require('chrome-remote-interface');
(async () => {
    const client = await CDP({port: 28800});
    const {Page, Runtime} = client;
    await Page.enable();
    await Page.navigate({url: 'https://x.com/explore/tabs/trending'});
    await new Promise(r => setTimeout(r, 5000));
    const result = await Runtime.evaluate({
        expression: `
            Array.from(document.querySelectorAll('[data-testid="trend"]')).slice(0, 10).map(el => {
                const spans = el.querySelectorAll('span');
                const texts = Array.from(spans).map(s => s.textContent).filter(t => t.length > 2);
                return texts.join(' | ');
            })
        `,
        returnByValue: true
    });
    console.log(JSON.stringify(result.result.value || []));
    await client.close();
})().catch(e => { console.error(e.message); process.exit(1); });
"""
        # Save and run
        script_path = WORKSPACE / "scripts" / "_trend_scraper.js"
        with open(script_path, "w") as f:
            f.write(script)

        result = subprocess.run(
            ["node", str(script_path)],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode == 0 and result.stdout.strip():
            trends = json.loads(result.stdout.strip())
            for i, trend_text in enumerate(trends[:10]):
                if trend_text:
                    signals.append({
                        "type": "x_trending",
                        "source": "X Explore",
                        "territory": "general",
                        "title": trend_text.split("|")[0].strip(),
                        "summary": trend_text,
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "id": hashlib.md5(trend_text.encode()).hexdigest()[:12]
                    })
        else:
            log(f"  CDP trending scrape failed: {result.stderr[:200]}")

    except Exception as e:
        log(f"  X trending scrape error: {e}")

    return signals


def load_existing_signals():
    """Load existing signals, prune expired ones."""
    if not SIGNALS_PATH.exists():
        return []
    now = datetime.now(timezone.utc)
    kept = []
    with open(SIGNALS_PATH) as f:
        for line in f:
            try:
                s = json.loads(line.strip())
                scraped = s.get("scraped_at", "").replace("Z", "+00:00")
                if scraped:
                    age = (now - datetime.fromisoformat(scraped)).total_seconds() / 3600
                    if age < SIGNAL_EXPIRY_HOURS:
                        kept.append(s)
            except (json.JSONDecodeError, ValueError):
                pass
    return kept[-MAX_SIGNALS_KEPT:]


def save_signals(signals):
    """Save signals to jsonl, deduped by id."""
    seen = set()
    deduped = []
    for s in signals:
        sid = s.get("id", "")
        if sid not in seen:
            seen.add(sid)
            deduped.append(s)

    SIGNALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNALS_PATH, "w") as f:
        for s in deduped[-MAX_SIGNALS_KEPT:]:
            f.write(json.dumps(s) + "\n")


def classify_signal_territory(title, summary):
    """Simple keyword-based territory classification for general signals."""
    text = (title + " " + summary).lower()
    ai_words = ["ai", "llm", "gpt", "claude", "model", "transformer", "neural", "machine learning",
                "agent", "reasoning", "inference", "training", "benchmark", "alignment"]
    build_words = ["build", "ship", "launch", "startup", "maker", "side project", "open source",
                   "indie", "solo", "bootstrap", "hack", "developer"]
    org_words = ["management", "team", "company", "leadership", "strategy", "roadmap", "meeting",
                 "culture", "hiring", "process", "bureaucracy", "enterprise"]
    taste_words = ["design", "taste", "decision", "conviction", "opinion", "philosophy",
                   "principle", "craft", "quality", "aesthetic"]

    scores = {
        "ai": sum(1 for w in ai_words if w in text),
        "building": sum(1 for w in build_words if w in text),
        "organizations": sum(1 for w in org_words if w in text),
        "taste_agency": sum(1 for w in taste_words if w in text)
    }
    if max(scores.values()) == 0:
        return "general"
    return max(scores, key=scores.get)


def main():
    parser = argparse.ArgumentParser(description="ARIA v2.1: Signal Ingestion")
    parser.add_argument("--rss-only", action="store_true", help="skip CDP, RSS feeds only")
    parser.add_argument("--dry-run", action="store_true", help="show signals, don't save")
    args = parser.parse_args()

    log("=" * 60)
    log("ARIA signals starting")

    existing = load_existing_signals()
    log(f"existing signals: {len(existing)} (after pruning expired)")

    new_signals = []

    # RSS feeds
    log(f"scraping {len(RSS_FEEDS)} RSS feeds...")
    for feed in RSS_FEEDS:
        items = fetch_rss(feed)
        log(f"  {feed['name']}: {len(items)} items")
        new_signals.extend(items)
        time.sleep(0.5)

    # X trending (if CDP available and not --rss-only)
    if not args.rss_only:
        log("scraping X trending topics...")
        trends = scrape_x_trending_via_cdp()
        # classify trending topics into territories
        for t in trends:
            if t["territory"] == "general":
                t["territory"] = classify_signal_territory(t["title"], t.get("summary", ""))
        log(f"  got {len(trends)} trending topics")
        new_signals.extend(trends)

    log(f"total new signals: {len(new_signals)}")

    if args.dry_run:
        for s in new_signals:
            log(f"  [{s['type']}] [{s['territory']}] {s['title'][:80]}")
    else:
        all_signals = existing + new_signals
        save_signals(all_signals)
        log(f"saved {min(len(all_signals), MAX_SIGNALS_KEPT)} signals to {SIGNALS_PATH}")

    log("signals complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
