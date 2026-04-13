---
name: linkedin-scraper
description: |
  Scrape recent posts from a curated list of LinkedIn people/companies and
  append them to memory/target-inbox.jsonl with platform="linkedin" so aria's
  reply-pipeline has linkedin material to draft against.

  Mirror of x-scraper but for LinkedIn. Connects to the same CDP-attached
  Chrome on :28800, opens a fresh tab, visits each target's recent activity
  page, extracts the 3-5 most recent original posts, and dedupes against the
  existing inbox.
---

# linkedin-scraper

## why this exists

Same rationale as x-scraper: the reply-pipeline drafts against whatever is in
`target-inbox.jsonl` with status="new". If we only fill the inbox from X,
linkedin growth is capped. linkedin has higher per-engagement reach for
thoughtful replies, so it's high-leverage to scrape.

## differences from x-scraper

| | x-scraper | linkedin-scraper |
|---|---|---|
| URL shape | `x.com/<handle>` | `linkedin.com/in/<slug>/recent-activity/all/` + `linkedin.com/company/<slug>/posts/` |
| DOM selectors | `article[data-testid="tweet"]` | `div.feed-shared-update-v2` (+ fallbacks) |
| timestamps | ISO from `<time datetime>` | linkedin uses "2d"/"3h" strings — we store scrape-time as approximation |
| min text | 40 chars | 80 chars (linkedin posts are longer) |
| per-run max | 5 | 4 |
| linger per page | 6-15s | 8-20s |
| between targets | 4-12s | 5-15s |
| cooldown | 20 min | 25 min |
| recent window | 72h | 7 days |
| auth detection | n/a | explicit — linkedin 302s to `/login` if logged out |

## config

`memory/target-handles-linkedin.json` — has two arrays:
- `people`: linkedin profiles (`slug` is the URL piece after `/in/`)
- `companies`: linkedin company pages (`slug` is the URL piece after `/company/`)

Each entry has `slug`, `handle` (display name), `priority` 1/2/3, `themes`,
`author_context`.

## anti-bot hardening

linkedin's bot detection is more aggressive than X. Mitigations:

1. **longer pacing**: all delays are ~50% higher than x-scraper
2. **auth wall detection**: if linkedin redirects to /login OR shows a
   "Sign in to view" prompt, exit gracefully and mark the run as auth-walled
3. **cooldown**: 25-min minimum between runs (launchd fires every 75m)
4. **jitter**: wrapper script adds 0-600s random delay before starting
5. **night skip**: no scraping 02-07 IST
6. **post-reply cooldown**: don't scrape right after we've posted (same
   logic as x-scraper)
7. **mouse movement**: simulated cursor wander per page
8. **multiple scrolls**: 2-5 random-delta scrolls before extracting
9. **new tab only**: never touches rishabh's existing tabs

## prereq: user logged into LinkedIn

The CDP Chrome must have an active linkedin session. If not, the scraper
detects the auth wall on the first target, aborts, and exits with code 4.
The launchd wrapper then sends a one-shot telegram alert: "linkedin session
dead — re-login".

To re-login: open the CDP chrome (port 28800), go to linkedin.com, sign in.
The session persists until the cookie expires or rishabh manually logs out.

## invocation

### manual smoke test

```bash
cd ~/.openclaw/agents/aria/workspace/skills/linkedin-scraper
node scrape_feed.js --dry-run --slug=deepakshenoy
```

### single target real run

```bash
node scrape_feed.js --slug=nithinkamath
```

### full run (ignoring priority rotation)

```bash
node scrape_feed.js --all
```

### via wrapper (adds jitter, night-skip, cooldown)

```bash
bash ~/.openclaw/agents/aria/workspace/scripts/run-linkedin-scraper.sh
```

### via wrapper, force (no jitter/skip)

```bash
bash ~/.openclaw/agents/aria/workspace/scripts/run-linkedin-scraper.sh --force --dry-run
```

## output

Rows appended to `target-inbox.jsonl` have these extra fields vs x-scraper:

- `platform`: "linkedin"
- `author_slug`: the linkedin URL slug
- `source`: "linkedin-scraper"
