---
name: x-scraper
description: |
  Scrape recent tweets from a curated list of target handles and append them
  as new rows in memory/target-inbox.jsonl so aria's reply-pipeline has
  fresh material to draft against.

  Use this when:
  - target-inbox.jsonl is empty or running low on status="new" entries
  - a new run of the x-scraper cron is firing
  - rishabh asks "go look at what X/Y/Z is posting"

  Works by connecting to the CDP-attached Chrome on :28800, opening a fresh
  tab (so the user's existing tabs are undisturbed), visiting each target's
  profile page, pulling recent tweets that pass a relevance filter, and
  appending them to target-inbox.jsonl with status="new".
---

# x-scraper

## why this exists

aria's reply-pipeline (cron `6c726652`, every 40m) drafts a reply to one
unclaimed entry from `memory/target-inbox.jsonl`. when the inbox is empty,
the cron returns the literal word "empty" to telegram and rishabh gets
nothing useful. **the inbox is the bottleneck for the entire factory**.

before this skill existed, the inbox was manually seeded. that doesn't scale.
x-scraper replaces manual seeding with continuous scraping of a curated
target list.

## inputs

1. **`memory/target-handles.json`** — the watch list. each entry has:
   - `handle` (e.g. `@Nithin0dha`)
   - `priority` 1/2/3 (1 = every run, 2 = every other run, 3 = once a day)
   - `themes` (hint tags for future filtering)
   - `author_context` (copied into each inbox row so the drafter has context)

2. **`memory/target-inbox.jsonl`** — existing inbox. the scraper reads every
   URL already in here (any status) to dedupe — a tweet we've already seen
   is never re-added.

3. **Chrome on CDP :28800** — user's logged-in browser. the scraper opens a
   new tab, scrapes, closes the tab. existing tabs are left alone.

## outputs

1. **appended rows in `target-inbox.jsonl`**, one per new candidate tweet:
   ```json
   {
     "id": "scr-a1b2c3...",
     "timestamp": "2026-04-11T12:30:00.000Z",
     "author_handle": "@Nithin0dha",
     "author_context": "founder of Zerodha...",
     "themes": ["indian_markets", "retail_investors"],
     "original": "the indian retail investor is finally maturing...",
     "url": "https://x.com/Nithin0dha/status/1234567890",
     "status": "new",
     "source": "x-scraper",
     "scraped_at": "2026-04-11T12:31:05.123Z"
   }
   ```

2. **stdout**: single JSON line with the run summary (count added, per-handle
   stats, duration).

3. **`memory/scraper-state.json`**: persistent state tracking run count
   (used for priority rotation) and last-run stats.

## filters

a tweet must pass ALL of these to be added to the inbox:

- NOT a repost (detected via `[data-testid="socialContext"]` text matching /repost/i)
- NOT a reply (detected via text starting with "Replying to ")
- NOT promoted / ad
- body text length >= 40 chars
- posted within the last 72 hours (`SCRAPER_RECENT_WINDOW_MS` env var, default 72h)
- URL not already in target-inbox.jsonl

## invocation

### manual smoke-test

```bash
cd ~/.openclaw/agents/aria/workspace/skills/x-scraper
node scrape_timeline.js --dry-run
```

### scrape one handle only

```bash
node scrape_timeline.js --handle=@Nithin0dha
```

### full run (all handles, ignoring priority rotation)

```bash
node scrape_timeline.js --all
```

### default run (priority rotation based on run count)

```bash
node scrape_timeline.js
```

## priority rotation

- **priority 1**: scraped every run (hot targets)
- **priority 2**: scraped every other run
- **priority 3**: scraped every 6th run (once a day if cron is 4h)

this keeps the run fast (a full run of 10 handles takes ~30-45s; priority
rotation typically limits it to 4-6 handles per run).

## pre-requisites

- Chrome launched with `--remote-debugging-port=28800` and logged into x.com
- `playwright` installed (shared via symlink to `x-twitter-poster/node_modules`)
- `memory/target-handles.json` populated

## failure modes

| error | cause | fix |
|---|---|---|
| `cannot connect to CDP at http://127.0.0.1:28800` | Chrome isn't running with CDP | run `bash ~/.openclaw/agents/aria/workspace/scripts/start-cdp.sh` |
| `no browser context found` | Chrome running but no pages open | open any tab in the CDP Chrome |
| `nav failed: Timeout` | x.com slow or rate-limited | retry next run; not a hard failure |
| `extracted 0 candidate(s)` for every handle | logged out, or x.com changed selectors | re-login; if selectors broke, update the `page.evaluate` block |

## extending

- to change priority rotation, edit `pickHandlesForThisRun` in scrape_timeline.js
- to change filters, edit the `page.evaluate` block in `scrapeHandle`
- to add a handle, edit `memory/target-handles.json`
- do NOT write metadata into `target-inbox.jsonl` directly — always go through
  this script so dedupe + field shape is consistent
