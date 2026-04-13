# ARIA Next Session Context — 2026-04-12 evening

> **STATUS: FULL STOP. Everything disabled. Brainstorming from scratch.**
> Rishabh wants to discuss WHAT before HOW. Do NOT start any processes.

---

## 1. CURRENT STATE

- All LaunchAgents unloaded (dispatcher, x-scraper, linkedin-scraper)
- All aria processes killed, CDP lock cleared
- Pending cards queue cleaned (old imageless/bad-voice cards deleted)
- 27 cron jobs built over 36 hours. Zero X followers. LinkedIn 4640 to 4641 (one follower).
- Rishabh manually deleted multiple bad posts (em dashes, assistant tone, no images)

## 2. THE GOAL

10 organic X followers first. Then scale.

Only 3 things matter:
1. People see rishabh's name (reach)
2. They click profile (curiosity from sharp reply)
3. They follow (profile has 2-3 good pinned originals)

Simplest path: **5-10 sharp replies/day on fresh big-account tweets + 1-2 originals on timeline**.

## 3. WHY 0 FOLLOWERS (the real diagnosis)

1. Scrapers ran but content was stale (8h+ old). Reply cron skipped everything.
2. Old drafts had no images, bad voice (em dashes, assistant tone). Rishabh manually deleted.
3. CDP lock contention: scraper holds lock 10-20 min, blocks all posting.
4. Pacing kept deferring everything that made it through the gate.
5. Net: lots of churning, near-zero posts actually going live.

## 4. WHAT ACTUALLY WORKS (proven this session)

- Voice gate hard checks (em dash, banned words, sycophancy) catch violations deterministically
- Image routing (quote cards, terminal screenshots, mood images) generates real PNGs
- LinkedIn hashtags (3-5 per post) added and verified
- CDP dialog crash fix prevents scraper/poster crashes
- Dry-run produces on-voice content: "voice: the ability to be wrong in a recognizable way." (43 chars, X punchy, mood image)

## 5. WHAT'S BROKEN CONCEPTUALLY

- 27 jobs is premature optimization. No proof the core loop works.
- reply-auto-cron has 8h freshness window that rejects everything because scrapers are slow
- CDP is single shared resource: scraping and posting can't happen simultaneously
- Pacing tuned for steady-state, not cold start
- No posts actually went live today through the autonomous pipeline

---

## 6. ARCHITECTURE (as of stop)

```
SCRAPE                    DRAFT                     POST
scrape_timeline.js  -->  reply-auto-cron.py   -->  auto-approve-cron.py
scrape_feed.js      -->  linkedin-comment-cron -->    approve-post.py
                         original-post-cron    -->      post_tweet.js (X)
                         thread/qt/carousel    -->      post_create.js (LI)
                                                        post_comment.js (LI)
```

27 jobs via aria-dispatcher.py. All data in jsonl files under memory/.

## 7. FIXES SHIPPED THIS SESSION (2026-04-12 afternoon)

1. Hard deterministic voice gate in all 4 draft crons + auto-approve pre_gated path
2. Image routing fix: originals prefer images (none score -1.0), quote-card boosted
3. LinkedIn hashtags: 3-5 relevant tags for LinkedIn originals, banned for X
4. CDP dialog race condition fix: unhandledRejection handler in 4 scripts
5. Cleaned pending queue of old imageless/bad-voice cards
6. Added Indian X handles (@paraschopra, @VarunMayya, @kunalb11, @Nithin0dha)
7. Added Indian LinkedIn handles (kunalshah1, varunmayya, etc.)

---

## 8. USER PROFILE: RISHABH

**who**: Rishabh Balabomma. VP Product at MOFSL. IIM-K EPGP MBA concurrent. Career: RedSeer, Rapido, Riskcovry, MOFSL.

**self-description**: creative wearing a PM mask. builder-as-identity. "22,000 words. 4 github stars. both numbers are accurate."

**collaboration mode**: jobs/woz dynamic. rishabh is jobs (direction, taste, approval). claude is woz (all execution). "take the best route, i want it, run asap, you understand it better than me."

**despises**: em dashes, generic AI voice, hedging, "delve"/"nuanced"/"landscape", performative humility/ambition

**tone**: lowercase, direct, dry, peer-to-peer. match his DM register.

**approval signal**: heart reaction on telegram. silence = keep going.
**disapproval signal**: "it's not working" = correction, not abandonment. pivot immediately.

**red lines**: never name MOFSL/colleagues in risky ways (SEBI risk), no investment claims, no bhilai-as-underdog narrative, no fabricated receipts.

**X handle**: @BalabommaRao
**LinkedIn slug**: brishabhrao (not rishabhkotrike)

## 9. KEY FEEDBACK RULES

### simplicity first
27-job engine produced 0 followers. Build minimum viable loop, prove it works, then layer. "has the simplest version been proven?" before adding anything.

### approve-only role
rishabh only approves and opines. claude does ALL execution (clicks, pastes, posts, saves). never say "please click X" or "paste this".

### ship don't plan
act decisively, ask only on irreversibles. don't present 3 options when you can pick the best one.

### no flaunting
strip metrics/titles/credentials from all public copy. only what he THINKS ABOUT, not what he's done.

### tone and formatting
NO em dashes ever. NO hyphens as formatting. lowercase, direct, dry. match DM register.

### voice quality
gemma4:26b is sufficient. don't propose model swaps without specific quality complaint. tighten prompts not models.

### images required
text-only won't cut it. use pick-image.py (9 strategies). variety over templates. "not always creating image, you have to screenshot something and many other things be creative."

### CDP lock contention
scraper holds lock 10-20 min, blocks posting. must design around this: separate instances, time-slicing, or posting priority.

### pacing cold start
6-min gaps kill cold start. with 0 followers, risk of looking like a bot is near zero. tighten pacing later.

### never run approve-post.py as test
NOT idempotent. if sent row exists, it posts a real tweet. always use --dry-run.

### parse_ts Z-suffix
Python 3.9 can't parse "Z". must .replace("Z","+00:00") + .astimezone() before stripping tz.

### scraper freshness
loadActiveAuthorKeys must skip stale rows past INBOX_FRESH_HOURS, or zombie rows block all new engagement.

### CDP shared-page hygiene
assert landing-URL + per-article author match after page.goto, else stale state writes wrong-author data.

### not vibe coded
voice-safe content crons on timers are not a growth machine. real growth needs closed feedback loops. BUT: prove core loop first before building telemetry.

## 10. TECHNICAL REFERENCE

### key paths
| what | where |
|---|---|
| aria workspace | `~/.openclaw/agents/aria/workspace/` |
| identity/orders | `AGENTS.md`, `SOUL.md`, `USER.md`, `IDENTITY.md` |
| state files | `memory/target-inbox.jsonl`, `memory/pending-cards.jsonl`, `memory/approved-drafts.jsonl` |
| dispatcher | `scripts/aria-dispatcher.py` + `~/Library/LaunchAgents/com.aria.dispatcher.plist` |
| content crons | `scripts/{original-post,reply-auto,linkedin-comment,thread,qt,carousel}-cron.py` |
| auto-approve | `scripts/auto-approve-cron.py` -> `scripts/approve-post.py` |
| X poster | `~/.openclaw/workspace/skills/x-twitter-poster/post_tweet.js` |
| X thread | `~/.openclaw/workspace/skills/x-twitter-poster/post_thread.js` |
| X QT | `~/.openclaw/workspace/skills/x-twitter-poster/post_qt.js` |
| LI poster | `~/.openclaw/workspace/skills/linkedin-poster/post_create.js` |
| LI comment | `~/.openclaw/workspace/skills/linkedin-poster/post_comment.js` |
| X scraper | `skills/x-scraper/scrape_timeline.js` |
| LI scraper | `skills/linkedin-scraper/scrape_feed.js` |
| image router | `scripts/pick-image.py` (9 strategies) |
| image gen | `scripts/make-image.py` (4 templates, 1200x1200) |
| carousel gen | `scripts/make-carousel.py` (1080x1350 PDF) |
| pacing | `scripts/pacing.py` |
| voice gate | `scripts/claude-call.py` + hard checks in each cron's gate_draft() |
| dashboard | `dashboard/index.html` + `dashboard/server.py` on :28889 |

### key identifiers
| what | value |
|---|---|
| telegram chat ID | `7353580848` |
| telegram bot token | `8794849679:AAGUiW5aIKeGzChVeSOMzpMFeqw0g-gGqII` |
| X handle | `@BalabommaRao` |
| ollama model | `gemma4:26b` |
| CDP port | `28800` |
| openclaw gateway | `:18789` |

### resume commands
```bash
# verify infra
openclaw status
ollama list && ollama ps
curl -s http://127.0.0.1:28800/json/version  # CDP chrome

# if CDP dead
bash ~/.openclaw/agents/aria/workspace/scripts/start-cdp-chrome.sh

# manual post (bypasses pipeline)
cd ~/.openclaw/workspace/skills/x-twitter-poster && \
  X_USERNAME=BalabommaRao CDP_URL=http://127.0.0.1:28800 \
  node post_tweet.js "draft text"

# dry-run content generation
python3 scripts/original-post-cron.py --platform x --dry-run --force
python3 scripts/original-post-cron.py --platform linkedin --dry-run --force

# dispatcher (currently UNLOADED — do not reload without rishabh's go)
launchctl load -w ~/Library/LaunchAgents/com.aria.dispatcher.plist
```

## 11. GEMMA/OPENCLAW INTEGRATION LESSONS

1. openclaw ollama: must use `api: "openai-completions"` + `/v1` baseUrl
2. model spec needs `reasoning: false` (gemma4 leaks harmony tokens otherwise)
3. chrome 147+ refuses CDP on default profile — must use `--user-data-dir`
4. gemma can't reliably invoke `message` tool — use tg-reply.py or --announce cron delivery
5. don't use Edit tool for jsonl mutations — use python3 json round-trips via bash
6. `OLLAMA_KEEP_ALIVE=-1` via launchctl pins model in GPU memory
7. `ollama ps` CONTEXT column shows last request's num_ctx, not max
8. gemma produces good drafts when context is structured right (~40% voice-check fail rate, usable with retry)
9. cron sessions are isolated/fresh; DM sessions are persistent. state crosses via filesystem only.

---

## 12. SESSION HISTORY

- **2026-04-12 afternoon**: voice fixes, image routing, LinkedIn hashtags, CDP fix. then FULL STOP.
- **2026-04-12 morning**: phase-D pipe repair (threads/QTs unblocked, LinkedIn comments fix)
- **2026-04-12 ~01:00**: phase-C (follower-attribution, reward-reducer, account-health)
- **2026-04-12 ~00:30**: phase-B (pacing, first-reply-hunter, save-hooks, follower-tracker)
- **2026-04-11 night**: autonomous growth loops shipped (27 jobs)
- **2026-04-11 evening**: auto-post incident (stale draft posted, rolled back)
- **2026-04-11 PM**: factory pivot (dashboard, MCP, scrapers)
- **2026-04-11 AM**: milestone 0 (first tweet via CDP, approve flow)
