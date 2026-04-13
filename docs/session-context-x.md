# ARIA X (Twitter) Session Context

Paste this into a new Claude Code chat to continue X work with full context.

---

## SYSTEM OVERVIEW

ARIA is an autonomous Twitter engine for @BalabommaRao (Rishabh). It runs on a Mac Studio M4 Max via launchd.

### Architecture (5 services, all launchd):

| Service | Script | Interval | What it does |
|---------|--------|----------|-------------|
| brain | aria-brain.py | 30 min | Generates tweets + reply drafts via Claude. Zero CDP. |
| hands | aria-hands.py | 10 min | Posts tweets, replies, follows via CDP (Playwright). One action per cycle. |
| khud | aria-khud-x.py | 1 hour | The living brain. Gets world state, decides strategy, reflects, learns. |
| watchdog | aria-watchdog.py | 15 min | Health checks, CDP self-heal, alerts. |
| retro | aria-retro.py | 6 hours | Retrospective analysis. |

### Key Paths:

- Workspace: `/Users/boredfolio/.openclaw/agents/aria/workspace/`
- Scripts: `workspace/scripts/`
- DB: `workspace/memory/aria.db`
- Voice config: `workspace/voice.json`
- Target handles: `workspace/memory/target-handles.json`
- Logs: `workspace/logs/` (brain-stdout.log, hands-stderr.log, etc.)
- Plists: `~/Library/LaunchAgents/com.aria.{brain,hands,khud,watchdog,retro}.plist`
- Dashboard: `workspace/dashboard/khud-dashboard.py` (port 8421)
- Node: `/Users/boredfolio/.nvm/versions/node/v20.20.2/bin/node`
- NODE_PATH: `/Users/boredfolio/.openclaw/workspace/skills/x-twitter-poster/node_modules`
- CDP: `http://127.0.0.1:28800` (Playwright)

### DB Tables:

- `queue` -- tweet candidates (id, text, territory, scores_json, image_type, card_text, status)
- `posted` -- published tweets
- `reply_drafts` -- reply candidates (status: ready/posting/posted/failed/expired)
- `reply_targets` -- target accounts with priority, cooldown
- `engagements` -- follows, likes, replies
- `reflections_x` -- Khud's reflections
- `khud_actions_x` -- Khud's proposed actions
- `memory_episodic_x` -- observations with embeddings
- `memory_semantic_x` -- confirmed knowledge (graduated from episodic)
- `memory_procedural_x` -- codified skills/templates
- `decision_ledger` -- full audit trail of every decision
- `state` -- key-value store for cross-process state
- `engine_log` -- all logs
- `metrics` -- scraped profile analytics

### Claude Khud (X) -- The Living Brain:

NOT a task executor. Gets state of the world, decides what to do.

Three-layer memory:
1. **Episodic**: observations stored with nomic-embed-text embeddings for semantic search
2. **Semantic**: confirmed patterns graduated from episodic (confidence-scored)
3. **Procedural**: codified reusable skills/templates

Actions it can take: reflect, generate_tweets, generate_replies, adjust, investigate, experiment, learn, codify_skill

Current guidance it's set:
- Tweet direction: "diversify hard. need: 1 tooling absurdity, 1 product craft, 1 Indian/local texture, 1 AI gap, 2 wildcards. ALL must avoid the inversion structure."
- Reply direction: "do NOT use the inversion structure. extend their thought with a specific concrete example. shorter is better."

## THE VOICE

Handle: @BalabommaRao (Rishabh)
Who: IIM-K MBA, VP Product at MOFSL. Compulsive builder. Creative wearing a PM mask.

### Golden tweets (THE voice):

1. "spent 36 hours automating a process i had never once done successfully by hand. 27 cron jobs. a dispatcher. a dashboard. zero tweets posted."
2. "product management is the only job where being creative is a liability until it suddenly saves the quarter."
3. "some people build things to make money. some people build things to solve problems. and then there's the third type who builds things because not building feels like a systems failure."
4. "the most interesting thing about AI isn't that it can think. it's that it makes you realize how little of your job was thinking."
5. "the most dangerous person in tech right now is a single builder with taste, an API key, and nothing to lose."

### Territories:

- building (30%): compulsion to build, absurdity of solo creation
- organizations (25%): how orgs actually work vs pretend to
- ai (25%): what AI reveals about humans
- taste_agency (20%): taste, conviction, doing over discussing

### Hard bans:

No em dashes, en dashes, !, #. No: delve, nuanced, landscape, leverage, synergy, etc. No: "here's the thing", "hot take", "unpopular opinion". No starting with "This.", "So,", "Look,". No numbered lists. No @mentions. No URLs.

### Red lines:

Never mention MOFSL, IIM-K, VP title, credentials, bhilai-as-underdog, market returns. No flaunting metrics/titles.

### Formatting:

Natural case. NO forced lowercase. NO em dashes. NO hyphens as formatting. Direct, dry. Match DM register.

## CURRENT STATE (as of 2026-04-13, Day 1)

- 7 tweets posted, 17 replies landed, 15 failed replies (47% waste), 15 follows
- Daily cap: 60 actions. Hit 58/60 by 10:50 PM IST.
- Territory skew: organizations=3, building=2, ai=1, taste_agency=1. Needs more tooling_absurdity, product_craft, local_texture.
- 3 tweets queued (all organizations, no card_text -- will post text-only)
- No followers yet. Cold start.

## LEARNED KNOWLEDGE (semantic memory, 0.85+ confidence)

1. "concrete, visual, specific details outperform abstract reframes in replies. the tram token reply to jasonfried is the proof."
2. "strong replies ADD something from outside the original tweet. weak replies REFRAME. the inversion template is structurally always a reframe."
3. "quote card images must NEVER repeat the tweet text. the image should contain a complementary punchline." (0.95)

## KNOWN BUGS / ISSUES

1. **@johncutlefish**: scraper can't find his tweets. 8 failures. Khud proposed blacklisting. Not implemented yet.
2. **@shreyas**: subscription-locked tweets disable reply/like buttons. Fixed in hands.py (skip logic added).
3. **Brain's blind reply drafts**: ~90% get replaced by contextual replies anyway. Wastes Claude tokens. Should kill brain's blind LLM calls for replies (approved but not implemented).
4. **Territory stagnation**: all 3 queued tweets are organizations. Khud's guidance hasn't taken effect yet.
5. **Unverified tweet**: "shipped something at 2am" (terminal screenshot) URL never captured.
6. **Brain crash**: `'list' object has no attribute 'get'` in load_targets at line 631. target-handles.json format may have changed.

## KEY DECISIONS PENDING

1. Implement johncutlefish blacklist (Khud proposed, not executed)
2. Kill brain's blind reply LLM calls (user approved)
3. Tighten inversion tic (cap at 1 in 5 replies)
4. Diversify territories (tooling_absurdity, product_craft, local_texture)
5. Investigate "Claude failed" errors for @natfriedman and @fchollet

## IMPORTANT USER PREFERENCES

- Rishabh approves + opines. Claude does ALL execution. No "please click X" prompts.
- Ship don't plan. Rishabh is the Steve Jobs, Claude is the Steve Woz.
- Never run approve-post.py as a test (not idempotent). Use --dry-run.
- Images + growth hacks required. Text-only won't cut it.
- Parse timestamps: Python 3.9 fromisoformat can't handle "Z" suffix. Use .replace("Z", "+00:00").

## COMMANDS

```bash
# check all services
launchctl list | grep aria

# reload a service
launchctl unload ~/Library/LaunchAgents/com.aria.brain.plist && launchctl load ~/Library/LaunchAgents/com.aria.brain.plist

# check recent logs
python3 -c "import sqlite3; db=sqlite3.connect('~/.openclaw/agents/aria/workspace/memory/aria.db'); db.row_factory=sqlite3.Row; [print(f'{r[\"ts\"][:19]} [{r[\"process\"]}] {r[\"message\"][:120]}') for r in db.execute('SELECT * FROM engine_log ORDER BY id DESC LIMIT 20')]"

# start dashboard
cd ~/.openclaw/agents/aria/workspace/dashboard && python3 khud-dashboard.py

# dry-run hands
ARIA_DRY_RUN=1 python3 ~/.openclaw/agents/aria/workspace/scripts/aria-hands.py
```
