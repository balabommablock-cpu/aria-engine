# NEXT-SESSION.md — aria v4 handoff (updated 2026-04-12 ~01:30 IST)

read this FIRST when resuming work on aria.

---

## 🔴 PHASE-C CRITICAL-PATH SHIP LOG — 2026-04-12 ~01:00 IST

A background research agent ran a **hostile review** of the phase-B machine
and returned 6 P0 issues. The most devastating:

> "No follower-flow attribution. The system is blind to what actually works.
>  It optimizes on like_rate + save_rate, which correlates with follows but
>  isn't the same thing. A post with 3k views and 0 follows is worse than a
>  reply with 40 views and 8 follows. The system is literally training itself
>  AWAY from the goal."

This session fixed that + two adjacent critical-path issues. Shipped:

### 1. Follower-flow attribution (P0-4) — the reward signal rebuild
- **`follower-attribution.py`** (new): reads follower-history.jsonl, computes
  per-platform count deltas between consecutive snapshots, finds approved
  posts in each `[ts_a − 20min, ts_b]` window, equal-splits the delta across
  N posts in window, writes one attribution row per (post, share) to
  `memory/follower-attribution.jsonl`. Deterministic `window_id` (sha1 of
  `platform|ts_a|ts_b`) makes re-runs idempotent. Handles "ambient" case
  (delta with no posts in window). **6 synthetic unit tests pass** covering
  basic attribution, equal-split, idempotency, negative delta (unfollows),
  platform isolation, and ambient bucket.
- **`reward-reducer.py`** (new): reads follower-attribution.jsonl + approved-
  drafts + pending-cards, computes per-dim **Bayesian-smoothed follows-per-
  post**, writes `hook-weights.json` in the SAME schema the content crons
  already consume via `hook_weights.biased_choice`. Smoothing:
  `smoothed_rate = (follows + global_mean * 3.0) / (n + 3.0)`. Prevents
  one lucky post from driving a weight to 2.5 during cold start. **6 unit
  tests pass** covering cold-start, winner-emergence, smoothing cap,
  zero-follow negative signal, and drop-in file compatibility with
  `hook_weights.biased_choice`.
- **`follower-count-tracker.py`** edit: after appending a snapshot, chains
  `follower-attribution.py` as a detached subprocess so every new snapshot
  is bound to posts-in-window immediately (no 30-min lag).
- **`aria-dispatcher.py`** edits:
  - `follower-tracker` → split into `follower-tracker-x` (30m cadence) and
    `follower-tracker-li` (2h cadence). LinkedIn at 2h because its auth-wall
    risk on repeated profile reads is higher and its follow events are slow
    enough that 2h granularity is fine. X at 30m gives enough granularity
    to bind deltas to individual posts.
  - NEW `follower-attribution` safety-net job at 30m cadence
  - NEW `reward-reducer` job at 15m cadence **replaces** the killed
    `claude-hook-review` job (the one that was training the system AWAY
    from the goal)
- Content crons now all read `hook-weights.json` via `biased_choice`:
  - `original-post-cron.py` — already wired (prior session); biases mode,
    topic, save_hook dims
  - `thread-cron.py` — **NEW this session**; biases archetype (via
    `dim_weight`) and topic (via `biased_choice`). Before this edit the
    thread cron was flying blind of the reward signal.
  - `carousel-cron.py` — **NEW this session**; biases topic (via
    `biased_choice`). Before this edit the carousel cron was random-uniform.
  - `qt-cron.py` — not biasable; its selection is source-driven (we QT
    target-inbox posts, not self-selected archetypes).

### 2. Race-path latency fix (P0-1)
- **`first-reply-hunter.py`** edit: added `TRUSTED_FAST_PATH_HANDLES` (8
  priority-1 handles) + `local_regex_gate()` as a ~5ms alternative to the
  Haiku voice gate (which has 9-50s variance — too slow for first-3 races).
- Regex gate checks: length ≤240, no markdown fences, no @mention prefix,
  banned words (delve/leverage/navigate/...), banned transitions
  (moreover/furthermore/...), banned endings (hashtags, "thoughts?"),
  sycophancy ("great point", "100%"), ego tells ("as a PM,", "in my experience,").
- For trusted handles, gate is local. For everyone else, Haiku.
- 8 sample drafts tested → gate decisions match intent.

### 3. Account-health monitor (P1-4)
- **`account-health-monitor.py`** (new): evaluates 3 degradation signals
  per platform:
  - **flatline** — ≥3 posts in last 24h but follower delta ≤ 0 (alarm)
  - **view_collapse** — recent median views < 20% of 7-day baseline median
    (alarm)
  - **metrics_gap** — posted in last 4h but `post-metrics.jsonl` has no row
    for ANY recent post (warn; this is the real shadow-ban canary — metrics
    scraper broken OR X serving 404s)
- Writes `memory/account-health.json` with per-platform summary + overall
  severity. Emits events at appropriate level (alarm/warn/ok). Does NOT
  auto-gate posting (false-positive risk too high in week 1; CEO brief will
  surface alarms for human decision).
- Dispatcher: wired at 30m cadence as `account-health` job.
- **8 unit tests pass** covering cold-start, flatline detection/silence,
  view-collapse detection/silence, metrics-gap detection/silence, and
  overall-severity rollup.

### 4. CDP failover detection (P0-6)
- **`cdp-health-monitor.py`** (new): 1-minute tripwire that probes
  `http://127.0.0.1:28800/json/version` with a 3-second timeout. On two
  consecutive failures (`FAIL_THRESHOLD=2`), raises `memory/cdp-down.flag`
  so poster crons early-exit before wasting a subprocess launch. On one
  recovery (`RECOVERY_THRESHOLD=1`), clears the flag immediately — fast
  recovery is more important than false-positive protection here because
  every minute of posting opportunity matters against the 30d clock.
- State file `memory/cdp-health.json` tracks status, consecutive counts,
  browser version, latency. State machine is `unknown → ok | dead` with
  hysteresis.
- Emits three event types: `cdp-dead` (transition into dead), `cdp-recovered`
  (transition back to ok), and `cdp-still-dead` (every 10th check while dead
  = every 10 min at 1m cadence — so the CEO brief keeps surfacing it
  without flooding events.jsonl).
- **Does NOT auto-restart chrome** — that's a human decision. Rishabh sees
  the alarm via CEO brief / tail events and manually brings chrome back up.
  Auto-restart would risk racing with an in-flight session.
- **`auto-approve-cron.py`** edit: added `CDP_DOWN_FLAG` gate check right
  after `PAUSED_FLAG`. If the flag exists, emits `cdp-down-defer` and
  returns early with `{"result": "cdp-down", "flag": flag_data}`. Verified
  by manually raising the flag → running `--dry-run` → confirming the gate
  blocked → removing the flag.
- Dispatcher: wired at **1m cadence** (the fastest cron in the system) as
  `cdp-health`. This is deliberate — we want the flag to appear within ~2
  minutes of CDP actually dying, not after the next posting attempt fails.

### 5. Dead-zone tweet length gate (phase-C #10)
- Hostile review flagged X's "mobile show-more cutoff" around the 200-char
  mark. Posts in the **200-280 char dead-zone** trigger a "show more"
  truncation on mobile that crushes impression depth: too long to be read
  as a punchy one-liner, too short to be read as authoritative long-form.
- **`original-post-cron.py`** edit: added a deterministic pre-check in
  `gate_draft()` that rejects `200 <= len(draft) <= 280` for X drafts
  BEFORE calling the Haiku voice gate (saves a model call). Also added
  a hard `> 280` safety net. Drafter prompt updated to "UNDER 180 chars,
  punchy, scroll-stopping — DO NOT produce 200-280". **Smoke-tested**:
  7/7 cases pass (punchy fall-through, dead-zone blocks at 200/240/280,
  over-limit block at 300, LinkedIn fall-through at 500).
- **`thread-cron.py`** edit: added dead-zone check on `tweets[0]` only
  (continuation tweets don't face the mobile cutoff because readers who
  clicked to expand have already committed). Drafter prompt now says
  "HOOK: UNDER 180 characters — if it hits the mobile cutoff the whole
  thread dies".
- **`qt-cron.py`** edit: tightened drafter from 240→180 and added the
  same 200-280 pre-check. QT commentary ≥200 chars hits mobile "show more"
  AND partially hides the embedded quoted card, which kills the whole
  point of a QT.
- Live dry-run: first Sonnet draft came back at 166 chars — the prompt
  instruction alone is enough; the pre-check is belt-and-suspenders.

### Machine state right now (2026-04-12 ~01:30 IST)
- Dispatcher has **26 jobs** (was 22): +follower-tracker-x, +follower-tracker-li,
  +follower-attribution, +reward-reducer, +account-health, +cdp-health;
  -follower-tracker, -claude-hook-review. (Net +4 after 2 removed.)
- Latest tracker tick seeded follower-history with 3 X snapshots, 1 LI
  snapshot. All X snapshots show 0 followers (cold start, no growth yet).
- `memory/account-health.json` exists, reports "ok" (cold-start safe).
- `memory/cdp-health.json` exists, status=ok, chrome up.
- `memory/cdp-down.flag` does NOT exist (chrome healthy).
- `memory/follower-attribution.jsonl` doesn't exist yet (no non-zero deltas
  to attribute). It will appear the moment the first delta shows up.
- `memory/hook-weights.json` doesn't exist yet (reward-reducer bails cleanly
  on "ambient-only" until real data arrives).
- The single org-26ea3175 card still pending — quiet-hour block until 07:00 IST.

### What to watch in the next session
- After 07:00 IST, the first original post should fire. Check:
  - `follower-attribution.jsonl` starts getting rows (watch for the first
    non-ambient attribution — that's the first real reward signal)
  - `hook-weights.json` gets written once attribution has ≥1 real post
  - `account-health.json` severity stays "ok" for the first posting day
  - events.jsonl shows `follower-tracker attrib-chained` every 30m
- If `account-health.json` reports alarm on day 1, DON'T auto-stop —
  investigate first (likely false positive at tiny sample size).
- Run `python3 reward-reducer.py --dry-run` manually after 24h to inspect
  the first real weight table. Look for obvious bias problems (one hook
  pinned at 2.5, one at 0.3 on single posts).

### Phase-C backlog (still unshipped)
From the same hostile review, 2 P0 issues remain (P0-6 shipped above):
- **P0-2 velocity scanner**: scrape X's homepage feed at 1m cadence, track
  which tweets are going viral in real-time, first-reply into them even
  when the AUTHOR isn't in the priority-1 list. Needs API or scraper
  rebuild — scraper already has viewport but not real-time tracking.
  Deferred — needs more design.
- **P0-5 local voice model**: fine-tune gemma on rishabh's existing 4640-
  follower LinkedIn post history to eliminate the claude-sonnet cost on
  every draft. Biggest unit-economics lever. Deferred — weekend project.

Non-P0 phase-C items still unshipped:
- **#1** Alt-text auto-fill for images (X accessibility + algo boost)
- **#2** LinkedIn see-more gate audit — confirm carousel body text lands
  above the see-more fold with the promise payload
- **#8** LinkedIn tag-candidate surfacer (~1h) — prefill `@` mentions
  from Rishabh's network when topic matches
- **#9** Sunday peak slot (~30m) — move Sunday origin cadence to 10am IST
  where his audience engagement peaks per LinkedIn analytics

---

## 🟣 PHASE-B CONTINUATION BRIEF — 2026-04-12 ~00:30 IST

Two overnight claude sessions worked the stack from "vibe-coded MVP loop" toward
a real growth machine. This section is the canonical handoff; the older 17:30
section below is still accurate for phase-A (MVP loop + approve path) but was
written before the autonomous-growth layer existed.

### Baseline numbers (seeded 2026-04-12 00:22 IST)
- X @BalabommaRao: **0 followers** · 2 posts · new account
- LinkedIn brishabhrao: **4,640 followers** · 500+ connections
- Total: 4,640 → target 100,000 in 30 days (deadline **2026-05-11**)
- Delta required: ~95,360 = ~3,178/day average

follower-history.jsonl now has the real baseline. Daily CEO brief will read it.

### What was wired this session (phase-B)

**Anti-bot-detection foundation (most critical gate):**
- `pacing.py` — behavioral governor with per_day/per_6h/per_1h caps, quiet-hours
  23-07 IST, ±30% jitter on min_gap, skip_pct for non-essential actions. All
  posting/reply/edit crons gate-check via `can_act(action_type)` before firing
  and `record_action()` after success.
- Action-type taxonomy: x_post_original, x_thread, x_qt, x_hot_take, x_reply,
  x_first_reply, x_edit, x_self_reply, x_qt_of_own, li_post, li_carousel, li_poll
- Dispatcher startup jitter 0-90s so posts never land on multiples of :00/:05
- Keystroke delay widened 25-45ms → **35-85ms** across all 5 poster scripts
  (post_tweet, post_qt, post_thread, linkedin post_create, post_comment) — the
  tight 25-45 band was a bot-tell, 35-85 sits plausibly in 130-170 WPM

**Growth-loop content crons (the work queue):**
- original-post-cron (2h) · thread-cron (8h) · qt-cron (8h) · carousel-cron (36h)
- hot-take-detector (30m) — clusters priority-handle posts for contrarian drafts
- linkedin-poll-cron (6h)
- All with Sonnet drafter + Haiku voice gate + save_hook bias

**Bookmark-optimized learning loop (the edge):**
- `SAVE_HOOKS` library in original-post-cron with 10 rhetorical shapes tuned
  for the save_rate = bookmarks/views signal (mental-model, counterintuitive-
  claim, field-note, numbered-frame, you-know-when, specific-metric,
  failure-artifact, contrarian-definition, taste-threshold, one-sentence-essay)
- gemma-metrics-collector scrapes per-post metrics every 15m
- claude-hook-reviewer (every 6h) reads post-metrics.jsonl, buckets by
  archetype/mode/topic/hook_pattern/save_hook, computes save_rate + like_rate,
  calls Claude-opus with bookmark-priority prompt, writes hook-weights.json
- All content crons read weights via `hook_weights.biased_choice` at draft time
- save_hook field propagated to thread/qt/carousel rows for bucketing

**First-reply hunter (the moat move):**
- `first-reply-hunter.py` races to first-3 replies under 100k+ author posts.
  Synchronous flow: claim → draft (sonnet) → gate (haiku) → enqueue pre-gated
  → approve-post.py inline. 2m cadence matches gemma-target-poller.
- **Race-cutoff fix**: RACE_CUTOFF_MIN was 6.0 → bumped to **8.0**. The swyx
  test case detected at 3min-old aged past the 6-min cutoff by the next hunter
  tick. 8min leaves ~4min for "first few replies" window.
- **Poller→hunter chain**: gemma-target-poller now fires `first-reply-hunter.py`
  as a detached Popen immediately after appending fresh hot-targets. Closes the
  0-2min poller-tick → hunter-tick lag. Fire-and-forget so poller returns fast.

**Fixes found by critique:**
- `mark_paced_deferred` in auto-approve-cron was mutating pending-cards.jsonl
  even in --dry-run mode (violated feedback_never_run_approve_as_test). Added
  `dry_run` param with early return.
- `claude-call.py` had no `--mode` argument but 6+ callers were passing it →
  systemic rc=2. Added `--mode {text,json}` with strict-JSON prompt injection
  + code-fence strip. Affected: linkedin-poll, qt-of-own, edit-window,
  self-reply, ab-pick, others.
- `follower-count-helper.js` X-side returned exit 1 — X layout rotated and
  the old href-based selectors broke. Rewrote with **3-strategy fallback**:
  href selectors → profile-header scan → full body regex. Same pattern for
  LinkedIn side. Verified both: X=0 via body-scan, LinkedIn=4640 via top-card.
- LinkedIn slug was wrong (`rishabhkotrike` → /404/). Real slug via /in/me/
  redirect is **`brishabhrao`**. Fixed in follower-count-tracker.py.

### Machine state right now
- **1 real original post queued**: `org-26ea3175` — "Craft scales until the person
  who cared leaves. Then it's just scale." topic=craft-vs-scale, image attached,
  pre_gated=true, paced_deferral_count=4 (correctly blocked by quiet-hour h=00).
  Will fire when h ≥ 07 IST.
- Dispatcher is **running** (22 jobs registered, tick every 5min, recent ticks
  firing 9+ jobs)
- linkedin-scraper just added 32 fresh inbox rows at 00:12:55 — scraping works
- reply-auto got 0 fresh inbox rows (inbox-prioritizer hasn't scored the new
  batch yet — needs one more gemma-inbox-prior tick, 1h cadence)
- hot-targets.jsonl has 1 aged entry (swyx post from 00:03) — too old to race

### Critical gaps still open (ranked by leverage × effort)

1. **No viral/first-reply hit yet** — the machine can generate posts but no data
   on which survive the algorithm. Until at least 3-5 posts have 48h of metrics,
   the hook-weights loop has nothing to learn from. Priority: let the queue fire
   at 07:00, watch for first-reply-hunter claims during peak windows (14-16 IST,
   20-23 IST based on US author overlap).

2. **Priority-1 handles = only 4** (karpathy, simonw, swyx, eugeneyan) — detection
   rate is bottlenecked by how often THEY post in the 2min poll window. More
   priority-1 handles = more hunt opportunities but also more noise. Consider
   promoting dharmesh/rauchg from priority-2 after seeing hit rate.

3. **LinkedIn leverage barely used** — 4640 existing followers is the biggest
   seed we have, but linkedin-poll fires only every 6h and carousel every 36h.
   LinkedIn's algorithm rewards creator consistency. Consider shortening
   carousel cadence to 24h OR adding a linkedin-original cron at 6h.

4. **No "meta-origin" post** — the single highest-leverage launch move is a
   transparent "I'm doing a 30-day public experiment" post that leverages the
   existing 4640 LinkedIn audience. Did NOT write this autonomously — needs
   rishabh's judgment on voice + stakes framing. See §8 "Suggested next move".

5. **Anti-bot-detection is structural but unverified** — pacing + jitter +
   keystroke variance are in place, but we haven't actually been flagged yet
   (only ~5 posts ever sent from this account). The real test is 30 days of
   volume. Watch for: sudden drop in post visibility, captcha challenges,
   password-reset emails, "suspicious login" warnings.

### 🔬 Phase-C backlog (research-ranked, unshipped)

A background research agent ran a full survey of 2025-2026 X + LinkedIn
growth mechanics. Cross-referenced against phase-B state, these are the
highest-ROI items NOT yet shipped. Ranked by `(lift × underutilization) / difficulty`.

| # | Mechanic | Status | ROI | Unblock |
|---|---|---|---|---|
| 1 | Alt-text auto-fill on every X image | ❌ not built | 30 | needs live composer testing — X alt button flow is finicky; deferred to avoid breaking the post path. When shipped: +5-15% reach, leaked-algo-verified `has_alt_text` weight. |
| 2 | LI "see more" dwell-hack gate | ❓ not verified | 56 | audit linkedin-poll/post-create draft gates — ensure 2-line hook + empty-line + long body structure is required. If not, add Haiku gate for `first_break_before_280_chars`. |
| 3 | LI native newsletter cron | ❌ not built | 36 | manual step: rishabh enables Creator Mode + creates newsletter. Then weekly cron reflows top 5 posts → send. ONLY LI mechanic that bypasses the feed algo (30-50% open rate). |
| 4 | Verified-only reply filter flag | ⚠️ de-facto shipped | 40 | all 4 priority-1 handles (karpathy, simonw, swyx, eugeneyan) are verified — so effectively in place, but no code-level filter. Add `verified_only=true` gate if we expand the target list beyond curated handles. |
| 5 | First-hour LI comment pod | ❌ not built | 20 | NOT automatable — needs 10-peer relationship. Rishabh task. |
| 6 | Community Notes contributor loop | ❌ not built | ~15 | needs manual approve-rate on CN candidate drafts. Nice-to-have. |
| 7 | LI native video (30-90s captioned) | ❌ not built | ~15 | heavy: whisper + ffmpeg + TTS. Defer until text+carousel velocity is proven. |
| 8 | Hashtag-tag-candidates for LI posts | ❌ not built | ~12 | add peers.json + Claude-selected @mentions. Medium lift, low effort (1 hour). |
| 9 | Sunday 6-9pm peak slot for LI | ❌ not built | ~10 | dispatcher tweak: special Sunday-evening fire-window for top-priority content. 30 min. |
| 10 | Tweet-length sweet-spot gate (70-140 OR 700-1000) | ❌ not built | ~8 | Haiku gate for dead-zone 200-600 chars on original-post-cron. 15 min. |

**Already shipped from the research agent's top-10:**
- ✅ Bookmark-optimized hooks (SAVE_HOOKS library, save_rate signal)
- ✅ First-reply racing (first-reply-hunter.py + poller chain)
- ✅ Thread self-reply Part 2 (self-reply-cron.py)
- ✅ LI PDF carousels (carousel-cron.py)
- ✅ Image card on every post (pick-image.py + make-image.py)
- ✅ Poll attachments on LI (linkedin-poll-cron.py)
- ✅ Quote-tweet of own post 24h later (qt-of-own-cron.py)
- ✅ Peak-window profiling (peak-window-profiler.py — but NOT yet fed into
  content-cron scheduling; data is there, scheduler isn't peak-aware)

**Phase-C suggested order** (ship during next session with ability to test):
1. #10 Tweet-length gate (15 min, pure config) — reject 200-600 char drafts
2. #8 LI tag-candidates (1 hour, pure config) — peers.json + Claude select
3. #9 Sunday peak slot (30 min, dispatcher edit)
4. #2 LI "see more" gate audit (30 min, test + maybe Haiku gate)
5. #1 Alt-text auto-fill (needs live composer to verify) — HIGHEST real ROI
6. #3 LI newsletter (needs Rishabh manual step first)

**Biggest conceptual insight from the research:**
> "Bookmark-optimization gets you velocity, first-reply racing gets you reach,
>  newsletter locks in the audience."

Phase-B nailed bookmark-velocity + first-reply reach. Phase-C should focus on
*retention* (newsletter, peer pod) and *on-post micro-optimizations* (alt-text,
length gate, see-more gate).

### Suggested next move for the operator (rishabh)

**At 07:00 IST when the first org card fires**, verify:
1. The post lands on @BalabommaRao (check /with_replies)
2. The image renders correctly
3. follower-count-tracker is scheduled daily — it should log a fresh row
4. events.jsonl has a successful `approve-post → posted_at` trail
5. No quiet-hour mutation of pending-cards past deferral_count=4

**Strategic prompt for claude next session:**
> "Look at post-metrics.jsonl (first 24h of live metrics). Run claude-hook-
>  reviewer manually to see what the first bucket analysis says. If save_rate
>  on the mental-model hook is >0.008, double down on that archetype. If below
>  0.003, kill it and bias toward the shape that's winning. Also check
>  follower-history.jsonl delta vs yesterday — if <100 net followers/day,
>  we're off pace and need to revisit the priority-1 handle list."

### Files touched (phase-B)
- **new**: first-reply-hunter.py, hot-take-detector.py, linkedin-poll-cron.py,
  edit-window-cron.py, self-reply-cron.py, qt-of-own-cron.py,
  post-engagement-tracker.py, peak-window-profiler.py, claude-hook-reviewer.py,
  gemma-metrics-collector.py, gemma-inbox-prioritizer.py
- **modified**: auto-approve-cron.py (dry-run fix), claude-call.py (--mode fix),
  gemma-target-poller.py (hunter chain), first-reply-hunter.py (cutoff 8min),
  follower-count-helper.js (multi-strategy), follower-count-tracker.py (slug),
  original-post-cron.py (save_hook bias), thread/qt/carousel-cron (save_hook
  field), all 5 poster scripts (keystroke 35-85ms).

---

## 🟢 STATUS UPDATE — 2026-04-11 ~17:30 IST

all 4 last-mile items are DONE. aria is fully autonomous on approve. see "last-mile work" section below (now annotated with completion notes). the new helper scripts live at `~/.openclaw/agents/aria/workspace/scripts/`:

- `start-cdp-chrome.sh` — idempotent chrome launcher, uses persistent profile at `~/.openclaw/agents/aria/chrome-profile/` (no more /tmp dependency, survives reboot)
- `approve-post.py` — atomic post+state orchestrator, called by APPROVE handler. finds pending card, subprocesses `node post_tweet.js` (argv array, zero shell escaping), updates all three state files, prints one-line JSON result
- `tg-reply.py` — reliable telegram reply helper (replaces the broken `message` tool for DM sessions). uses bot API via urllib

AGENTS.md's APPROVE / EDIT / SKIP handlers and the daily-angles program were rewritten to use these helpers. the reply parser now has a step-0 precheck for `awaiting_edit` so the EDIT handler's two-turn flow works.

**stale test-001 state was cleaned up** (pending-cards + target-inbox both marked `skipped` with reason; backups at `/tmp/pending-cards-before-cleanup-20260411-173058.jsonl.bak` + target-inbox).

**remaining hand-off steps for rishabh:**
1. seed a real target-inbox entry with `status: "new"` (rishabh's first real X reply opp).
2. wait for cron (or trigger manually via `openclaw cron run <reply-pipeline-id>`).
3. expect a telegram DM card. reply "approve".
4. aria auto-posts. verify tweet is live on `@BalabommaRao`.

## current status: MVP loop works end-to-end

**proven on 2026-04-11 at ~16:55 asia/kolkata**:
- aria drafted a rishabh-voiced tweet via gemma4:26b
- rishabh approved via telegram DM (the ❤️ reaction)
- the clawhub `x-twitter-poster` skill posted it to x.com
- verified live on `@BalabommaRao` profile: `"hello world. the plumbing is mostly working."`

screenshot evidence: rishabh confirmed "it worked". playwright navigation to profile confirmed top tweet is the posted content with "1m" timestamp.

## what's on disk (persistent)

| path | purpose |
|---|---|
| `~/.openclaw/agents/aria/workspace/` | aria's workspace (7 core md files + memory/ + skills/ + ROADMAP.md) |
| `~/.openclaw/openclaw.json` | gateway + model + cron config — has the critical openai-completions fix |
| `~/.openclaw/cron/jobs.json` | 7 cron jobs registered (2 enabled + 5 disabled stubs) |
| `~/.openclaw/workspace/skills/x-twitter-poster/` | clawhub-installed X posting skill (uses playwright + CDP) |
| `/tmp/chrome-aria-profile/` | **⚠️ WILL NOT SURVIVE REBOOT** — isolated chrome profile with X session cookies |

## critical config values (do NOT lose these)

```json
// ~/.openclaw/openclaw.json — the ollama provider must look like this
{
  "models": {
    "providers": {
      "ollama": {
        "baseUrl": "http://127.0.0.1:11434/v1",   // must have /v1
        "api": "openai-completions",               // NOT "ollama"
        "models": [{
          "id": "gemma4:26b",
          "reasoning": false,                      // critical — suppresses <channel|> Harmony leakage
          "contextWindow": 65536,
          "maxTokens": 4096
        }]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": "ollama/gemma4:26b",
      "llm": { "idleTimeoutSeconds": 1200 },
      "timeoutSeconds": 1800
    }
  }
}
```

**env var** (survives until reboot via `launchctl setenv`):
```
OLLAMA_KEEP_ALIVE="-1"
```

**rishabh's telegram chat id**: `7353580848`

## the stack — what invokes what

```
cron (every 40m)
  │ "run the reply-opportunity-pipeline program per AGENTS.md"
  ▼
aria agent turn (gemma4:26b via openai-completions)
  │ steps in AGENTS.md:
  │   1. bash + python → pick entry, mark processing
  │   2. draft inline (uses voice rules embedded in AGENTS.md)
  │   3. compliance inline
  │   4. bash + python → update target-inbox + append pending-cards
  │   5. return card body as final text
  ▼
cron delivery layer (--announce --channel telegram --to 7353580848)
  ▼
telegram DM to rishabh
  ▼
rishabh replies "approve" / "edit" / "skip" in DM
  ▼
aria persistent DM session handles reply (handlers in AGENTS.md)
  ▼
[CURRENT GAP] — on approve, aria should auto-invoke x-twitter-poster
  ▼
node ~/.openclaw/workspace/skills/x-twitter-poster/post_tweet.js "<draft>"
  │ env: X_USERNAME=BalabommaRao CDP_URL=http://127.0.0.1:28800
  ▼
playwright → chromium.connectOverCDP → x.com/compose/post → keyboard.type → Meta+Enter
  ▼
live tweet on @BalabommaRao
```

## last-mile work (4 items — ALL COMPLETED 2026-04-11 ~17:30 IST)

~~these are the remaining steps to make aria fully autonomous on approve:~~ — all shipped. section kept below as historical context and as the spec that defined the shape of the final implementation.

### 1. move chrome profile out of /tmp (critical — survives reboot) ✅ DONE

```bash
mkdir -p ~/.openclaw/agents/aria/chrome-profile
# if /tmp/chrome-aria-profile still exists (pre-reboot):
cp -R /tmp/chrome-aria-profile/. ~/.openclaw/agents/aria/chrome-profile/
# otherwise start fresh:
cp "$HOME/Library/Application Support/Google/Chrome/Default/Cookies" ~/.openclaw/agents/aria/chrome-profile/Default/Cookies
cp "$HOME/Library/Application Support/Google/Chrome/Local State" ~/.openclaw/agents/aria/chrome-profile/"Local State"
```

then update all references from `/tmp/chrome-aria-profile` to `~/.openclaw/agents/aria/chrome-profile`.

### 2. CDP chrome startup script ✅ DONE — `scripts/start-cdp-chrome.sh`, tested, CDP came up in 2s

write `~/.openclaw/agents/aria/workspace/scripts/start-cdp-chrome.sh`:

```bash
#!/bin/bash
# idempotent: only launches chrome if CDP isn't already responding
if ! curl -s --max-time 2 http://127.0.0.1:28800/json/version >/dev/null 2>&1; then
  pkill -9 -f "chrome-aria-profile" 2>/dev/null
  sleep 2
  /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
    --remote-debugging-port=28800 \
    --user-data-dir=$HOME/.openclaw/agents/aria/chrome-profile \
    --no-first-run --no-default-browser-check \
    https://x.com/home > /tmp/chrome-aria-cdp.log 2>&1 &
  disown
  sleep 8
fi
# verify
curl -s --max-time 3 http://127.0.0.1:28800/json/version | python3 -c "import json,sys; d=json.load(sys.stdin); print('CDP alive:', d.get('Browser'))"
```

chmod +x it.

### 3. register x-twitter-poster in aria's workspace ✅ DONE — symlink to main workspace, post_tweet.js accessible

the skill is currently in `~/.openclaw/workspace/skills/` (main agent's workspace). copy or symlink to aria's workspace:

```bash
ln -s ~/.openclaw/workspace/skills/x-twitter-poster ~/.openclaw/agents/aria/workspace/skills/x-twitter-poster
```

OR copy the entire dir (including node_modules with playwright) so aria's session catalog sees it.

### 4. update AGENTS.md APPROVE handler to auto-post ✅ DONE — A1+A2+A3 steps use bash→start-cdp-chrome.sh, bash→approve-post.py, bash→tg-reply.py. fan-out: EDIT/SKIP handlers + daily-angles program also converted off `message` tool. reply parser has step-0 awaiting_edit precheck.

in `~/.openclaw/agents/aria/workspace/AGENTS.md`, the current APPROVE handler logs to `approved-drafts.jsonl` and replies with the raw draft for manual copy. change it to:

```
### handler: APPROVE

1. bash → find single row in memory/pending-cards.jsonl with status==sent, extract draft
2. bash → run the CDP-chrome startup script (idempotent — skips if already running)
3. bash → run: cd ~/.openclaw/workspace/skills/x-twitter-poster && X_USERNAME=BalabommaRao CDP_URL=http://127.0.0.1:28800 node post_tweet.js '<escaped draft>'
4. parse the result. if success:
   a. bash → append to approved-drafts.jsonl with rishabh_action:"auto-posted", public_url from result
   b. bash → update target-inbox entry status: approved
   c. bash → update pending-cards row status: approved
   d. message tool → send "✓ posted. live now."
5. if failure: message tool → send "❌ post failed: <reason>. here's the raw draft to paste manually:" + draft in code block
```

watch out for escaping single quotes in the draft when passing to bash. use a temp file or base64 to avoid shell injection risk.

## the BIG lessons from 2026-04-11 session (don't repeat the mistakes)

1. **openclaw's ollama provider must use `api: "openai-completions"` + `/v1` baseUrl.** the native `api: "ollama"` mode doesn't pass tool schemas correctly — gemma emits tool calls as text instead of function calls. this was the single biggest blocker tonight. the fix came from `haimaker.ai/blog/gemma-4-ollama-openclaw-setup/`.

2. **`reasoning: false` on the model spec is critical.** without it, gemma4:26b leaks harmony-format reasoning tokens (`<channel|>>thought`) into the final response, which openclaw can't parse.

3. **chrome 147+ refuses CDP on the default profile.** chrome will accept `--remote-debugging-port=28800` but silently decline to start devtools. it prints this to stderr: *"DevTools remote debugging requires a non-default data directory. Specify this using --user-data-dir."* solution: use an isolated profile and clone cookies from default.

4. **gemma4:26b can't reliably use the `message` tool via openclaw's runtime.** even after the openai-completions fix, aria kept writing the `message` tool call as markdown JSON blocks instead of invoking the tool. **workaround**: set the cron job with `--channel telegram --to 7353580848 --announce`, and have aria's program return the card body text as its final response. the cron's built-in delivery layer handles the actual send. this is why `bash` works (it's simpler and gemma handles it fine) but `message` doesn't.

5. **don't use `edit`/`Edit` tool for jsonl state mutations.** gemma4:26b can't reproduce long JSON strings on rewrites — it hallucinates typos. use `bash` with inline `python3 -c "..."` that does json.loads/json.dumps round-trips. this preserves text byte-for-byte.

6. **`OLLAMA_KEEP_ALIVE=-1` via `launchctl setenv` keeps the model pinned in GPU memory.** without it, gemma4:26b unloads after ~4 min idle and the next request pays ~5 min cold load cost.

7. **`ollama ps` CONTEXT field shows the last request's num_ctx, not the model's max.** it can change between requests depending on what the client asks for. openclaw's openai-completions adapter currently requests 32k by default, not the 65k i put in the config. worth investigating if context becomes a limit.

8. **gemma produces actually-good drafts** when fed the right context. see the two examples from tonight:
   - *"The indian way is essentially just highly efficient chaos held together by UPI and WhatsApp..."* (run #3 draft)
   - *"hello world. the plumbing is mostly working."* (the DM-session reply that rishabh posted)
   
   quality bar is ~50-60% of claude's (estimate), which is usable with the voice-check loop. don't waste time on model swaps unless quality degrades materially.

9. **`aria is responding in the persistent telegram DM session` is how the real magic happens.** cron sessions are isolated write-only work; DM sessions are conversational and hold context. the `hi` → `hi.` reply and the `hello world` draft both came from the DM session, not cron runs.

## current state files

- `memory/target-inbox.jsonl` — has `test-001` in `pending-approval` state (the test tweet we posted). next run will skip it.
- `memory/pending-cards.jsonl` — has one `sent` row for `test-001`
- `memory/approved-drafts.jsonl` — empty (we posted via direct skill call, not the approve handler, so nothing landed here)
- cron: `aria-reply-pipeline` enabled, 40m interval. `aria-daily-angles` enabled, 08:57 asia/kolkata.

## commands to resume next session

```bash
# 1. verify state
openclaw status
openclaw cron list --all
openclaw agents list

# 2. check CDP chrome is running (it's not, after reboot — /tmp cleared)
curl -s http://127.0.0.1:28800/json/version

# 3. if CDP dead and /tmp chrome profile gone, rebuild:
mkdir -p /tmp/chrome-aria-profile/Default
cp "$HOME/Library/Application Support/Google/Chrome/Default/Cookies" /tmp/chrome-aria-profile/Default/Cookies
cp "$HOME/Library/Application Support/Google/Chrome/Local State" /tmp/chrome-aria-profile/"Local State"
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=28800 \
  --user-data-dir=/tmp/chrome-aria-profile \
  --no-first-run --no-default-browser-check \
  https://x.com/home > /tmp/chrome-cdp.log 2>&1 &
disown

# 4. re-set OLLAMA_KEEP_ALIVE if the machine rebooted
launchctl setenv OLLAMA_KEEP_ALIVE "-1"

# 5. seed a real tweet to test:
# edit memory/target-inbox.jsonl, add a line with status:"new"

# 6. fire cron manually:
openclaw cron run <reply-pipeline-job-id>   # get id from cron list

# 7. after rishabh replies "approve" in telegram, the current flow logs to approved-drafts but doesn't auto-post yet.
# to post manually (until last-mile #4 is wired):
cd ~/.openclaw/workspace/skills/x-twitter-poster && X_USERNAME=BalabommaRao CDP_URL=http://127.0.0.1:28800 node post_tweet.js "<the draft>"
```

## plan file (if i need to reconstruct reasoning)

`/Users/boredfolio/.claude/plans/drifting-scribbling-beacon.md` — the original milestone 0 plan, pre-debugging.
