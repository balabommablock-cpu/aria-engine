#!/usr/bin/env node
/**
 * scrape_timeline.js
 *
 * Connects to the CDP-attached Chrome on :28800, opens a NEW tab (doesn't
 * disturb rishabh's existing tabs), walks each target handle's profile page,
 * extracts the 10-20 most recent tweets, filters for ones worth replying to,
 * and appends new entries to memory/target-inbox.jsonl with status="new".
 *
 * Filters:
 *   - no retweets ("repost" marker)
 *   - no replies (starts with @)
 *   - no promoted tweets (contains "Ad" / "Promoted")
 *   - text length >= 40 chars (must have substance)
 *   - posted within RECENT_WINDOW_MS (default 72h)
 *   - URL not already in inbox (dedupe)
 *
 * CLI:
 *   node scrape_timeline.js             # scrape all priority-1 + every-other-run priority-2
 *   node scrape_timeline.js --all       # scrape every handle regardless of priority
 *   node scrape_timeline.js --handle=@Nithin0dha   # scrape one handle only
 *   node scrape_timeline.js --dry-run   # don't write to inbox, just print what it would add
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

// 2026-04-12: Playwright's internal DialogManager fires Page.handleJavaScriptDialog
// on CDP dialog events, but the dialog may already be gone (race with Chrome's own
// auto-dismiss or a previous handler). This causes an unhandled ProtocolError that
// crashes the process. Swallow it — the dialog was dismissed, we don't care.
process.on('unhandledRejection', (err) => {
  if (err && err.message && err.message.includes('No dialog is showing')) return;
  // re-throw everything else
  console.error('[x-scraper] unhandled rejection:', err);
  process.exit(1);
});
// Filesystem mutex around CDP access. Before 2026-04-12 this scraper
// bypassed the lock and raced with the helpers (follower-count-helper,
// target-poll-helper, etc.) that also hit port 28800. Confirmed collision
// at 01:23:19 on 2026-04-12: tracker asked for BalabommaRao, mid-scrape of
// @simonw in this skill clobbered page state, tracker reported simonw URL.
const { withCdpLock } = require('../../scripts/cdp-lock');

// ── paths ────────────────────────────────────────────────────────────────────
const HOME = os.homedir();
const ARIA_WORKSPACE = path.join(HOME, '.openclaw/agents/aria/workspace');
const HANDLES_PATH = path.join(ARIA_WORKSPACE, 'memory/target-handles.json');
const INBOX_PATH = path.join(ARIA_WORKSPACE, 'memory/target-inbox.jsonl');
const STATE_PATH = path.join(ARIA_WORKSPACE, 'memory/scraper-state.json');

// ── config ───────────────────────────────────────────────────────────────────
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:28800';
const MAX_TWEETS_PER_HANDLE = parseInt(process.env.SCRAPER_MAX_PER_HANDLE || '5', 10);
const RECENT_WINDOW_MS = parseInt(process.env.SCRAPER_RECENT_WINDOW_MS || String(72 * 60 * 60 * 1000), 10);
const MIN_TEXT_LEN = 40;
const NAV_TIMEOUT_MS = 20000;

// human-pacing config. these ranges control how long the scraper LOOKS LIKE it's
// browsing each profile. x (twitter) aggressively detects bots — a scraper that
// visits 8 profiles in 12s is a dead giveaway. with the ranges below each run
// takes ~3-5 minutes for 5 handles, which is slow for a script but is what
// a human actually looks like when reading through a feed.
const PER_PAGE_LINGER_MS_MIN = 6000;
const PER_PAGE_LINGER_MS_MAX = 15000;
const BETWEEN_HANDLE_MS_MIN = 4000;
const BETWEEN_HANDLE_MS_MAX = 12000;
const SCROLL_COUNT_MIN = 2;
const SCROLL_COUNT_MAX = 4;
const SCROLL_DELTA_MIN = 600;
const SCROLL_DELTA_MAX = 1600;
const SCROLL_PAUSE_MS_MIN = 700;
const SCROLL_PAUSE_MS_MAX = 1800;

function rand(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── cli flags ────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const SCRAPE_ALL = args.includes('--all');
const DRY_RUN = args.includes('--dry-run');
const ONLY_HANDLE = (args.find(a => a.startsWith('--handle=')) || '').split('=')[1] || null;

// ── logging ──────────────────────────────────────────────────────────────────
const log = (...m) => console.error('[x-scraper]', ...m);
const LOG_LINES = [];
const logf = (...m) => { LOG_LINES.push(m.join(' ')); log(...m); };

// ── utils ────────────────────────────────────────────────────────────────────
function loadHandles() {
  const raw = JSON.parse(fs.readFileSync(HANDLES_PATH, 'utf8'));
  return raw.handles || [];
}

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
  } catch (e) {
    return { run_count: 0, last_run_iso: null };
  }
}

function saveState(state) {
  fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

function loadExistingUrls() {
  // read target-inbox.jsonl and collect every URL that's already been picked up,
  // regardless of status. this prevents re-adding tweets we've seen (even if they
  // were skipped or blocked).
  const urls = new Set();
  if (!fs.existsSync(INBOX_PATH)) return urls;
  const lines = fs.readFileSync(INBOX_PATH, 'utf8').split('\n');
  for (const line of lines) {
    const t = line.trim();
    if (!t) continue;
    try {
      const row = JSON.parse(t);
      if (row.url) urls.add(row.url);
    } catch (e) { /* skip malformed */ }
  }
  return urls;
}

// Author-diversity check: which authors already have an active (non-terminal)
// row in the inbox? A row is "active" if its status puts it in the reply queue:
// new, processing, pending-approval. If an author already has one, any fresh
// row we scrape for that author is written as status="parked" instead of "new"
// so it's visible for manual promotion but never picked up by the cron.
//
// Freshness gate (2026-04-12): a stale post (older than reply-auto's
// INBOX_FRESH_HOURS window) must NOT count as active. Otherwise a
// zombie 49h-old karpathy row silently starves the account of all new
// karpathy engagement — reply-auto would skip the zombie anyway, so
// holding the slot for it accomplishes nothing and loses every fresh
// reply opportunity from that author until the zombie is manually
// cleaned. Fix after finding 9 stale new rows blocking 47 parked rows
// in the audit from the parse_ts Z-bug fix session.
const ACTIVE_FRESH_WINDOW_MS = 8 * 60 * 60 * 1000;
function loadActiveAuthorKeys() {
  const active = new Set();
  if (!fs.existsSync(INBOX_PATH)) return active;
  const lines = fs.readFileSync(INBOX_PATH, 'utf8').split('\n');
  const ACTIVE_STATUSES = new Set(['new', 'processing', 'pending-approval']);
  const now = Date.now();
  for (const line of lines) {
    const t = line.trim();
    if (!t) continue;
    try {
      const row = JSON.parse(t);
      if (!ACTIVE_STATUSES.has(row.status)) continue;
      // Stale post → no longer blocks. If timestamp is missing/unparseable
      // we fall through to counting it as active (safer default, avoids
      // opening a loophole if scraper output regresses).
      const postTs = row.timestamp ? Date.parse(row.timestamp) : NaN;
      if (isFinite(postTs) && (now - postTs) > ACTIVE_FRESH_WINDOW_MS) continue;
      const raw = (row.author_handle || row.author_slug || row.author || '').toString().trim().toLowerCase();
      if (raw) active.add(raw);
    } catch (e) { /* skip malformed */ }
  }
  return active;
}

function appendInboxRow(row) {
  const line = JSON.stringify(row) + '\n';
  fs.appendFileSync(INBOX_PATH, line);
}

function makeId() {
  return 'scr-' + crypto.randomBytes(6).toString('hex');
}

function pickHandlesForThisRun(handles, runCount) {
  if (SCRAPE_ALL) return handles;
  if (ONLY_HANDLE) {
    const norm = ONLY_HANDLE.startsWith('@') ? ONLY_HANDLE : '@' + ONLY_HANDLE;
    return handles.filter(h => h.handle.toLowerCase() === norm.toLowerCase());
  }
  // priority rotation:
  //   p1: every run
  //   p2: every other run
  //   p3: every 6 runs (roughly once a day at 4h cadence)
  return handles.filter(h => {
    const p = h.priority || 2;
    if (p === 1) return true;
    if (p === 2) return runCount % 2 === 0;
    if (p === 3) return runCount % 6 === 0;
    return true;
  });
}

// ── scrape one handle ────────────────────────────────────────────────────────
async function scrapeHandle(page, handleObj) {
  const handle = handleObj.handle.replace(/^@/, '');
  const url = `https://x.com/${handle}`;
  logf(`→ ${handleObj.handle}`);

  try {
    // networkidle is closer to what a human browser does than domcontentloaded:
    // waits for the xhr/fetch requests to quiet down before we interact.
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
    try {
      await page.waitForLoadState('networkidle', { timeout: 6000 });
    } catch (e) { /* networkidle can time out on feeds; not fatal */ }
  } catch (e) {
    logf(`  nav failed: ${e.message}`);
    return [];
  }

  // initial settle — a real human takes 1-3s to get oriented on a new page
  await sleep(rand(1200, 2800));

  // simulate mouse wander — some bot detectors watch for zero mouse events
  try {
    const vp = page.viewportSize() || { width: 1200, height: 800 };
    await page.mouse.move(rand(200, vp.width - 200), rand(200, vp.height - 200), { steps: rand(5, 15) });
  } catch (e) { /* mouse move is best-effort */ }

  // do multiple scrolls with randomized deltas and pauses. this mimics reading
  // down a feed: scroll, read, scroll, read, scroll, leave. two to four scrolls
  // keeps total time-on-page in the 6-15s range.
  const scrollCount = rand(SCROLL_COUNT_MIN, SCROLL_COUNT_MAX);
  for (let i = 0; i < scrollCount; i++) {
    const delta = rand(SCROLL_DELTA_MIN, SCROLL_DELTA_MAX);
    await page.evaluate((d) => window.scrollBy(0, d), delta);
    await sleep(rand(SCROLL_PAUSE_MS_MIN, SCROLL_PAUSE_MS_MAX));
  }

  // final linger — top up to the min time-on-page if we were too quick
  const lingerTarget = rand(PER_PAGE_LINGER_MS_MIN, PER_PAGE_LINGER_MS_MAX);
  const elapsed = scrollCount * 1300;  // rough estimate
  if (elapsed < lingerTarget) {
    await sleep(lingerTarget - elapsed);
  }

  // Profile-URL assertion. X can redirect (suspended, renamed, rate-limit,
  // shared-CDP-tab stale state), leaving us on a different profile than
  // we asked for. Without this check, subsequent DOM walks happily scrape
  // whoever's timeline is ACTUALLY on screen under the wrong handle.
  // 2026-04-12 fix after @Nithin0dha row was written with a /BalabommaRao
  // status URL (companion to the target-poll-helper bug from the same day).
  const currentUrl = page.url();
  const pathMatch = currentUrl.match(/^https?:\/\/(?:www\.)?x\.com\/([^/?#]+)/i);
  if (!pathMatch || pathMatch[1].toLowerCase() !== handle.toLowerCase()) {
    logf(`  profile-redirect: wanted /${handle}, got ${currentUrl} — skip`);
    return [];
  }

  // extract tweets. the author's profile page lists their own posts + pinned
  // posts + replies (if "replies" tab is active). the default is "posts" which
  // is what we want.
  const tweets = await page.evaluate((opts) => {
    const { MAX, MIN_LEN, RECENT_WINDOW_MS, TARGET_HANDLE } = opts;
    const now = Date.now();
    const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
    const out = [];
    for (const a of articles) {
      try {
        // detect repost banner
        const socialContext = a.querySelector('[data-testid="socialContext"]');
        if (socialContext && /repost/i.test(socialContext.innerText || '')) continue;

        // detect reply (starts with "Replying to @...")
        const replyingTo = Array.from(a.querySelectorAll('*')).find(
          el => el.innerText && /^Replying to /i.test(el.innerText)
        );
        if (replyingTo) continue;

        // detect promoted / ad
        const text = a.innerText || '';
        if (/(^|\n)(Promoted|Ad)(\n|$)/.test(text)) continue;

        // extract tweet body
        const tweetText = a.querySelector('[data-testid="tweetText"]');
        if (!tweetText) continue;
        const bodyText = (tweetText.innerText || '').trim();
        if (!bodyText || bodyText.length < MIN_LEN) continue;

        // extract timestamp + URL (the time element is wrapped in the status link)
        const timeEl = a.querySelector('time');
        if (!timeEl) continue;
        const iso = timeEl.getAttribute('datetime');
        if (!iso) continue;
        const ts = Date.parse(iso);
        if (!ts || (now - ts) > RECENT_WINDOW_MS) continue;

        const linkEl = timeEl.closest('a[href*="/status/"]');
        if (!linkEl) continue;
        const href = linkEl.getAttribute('href');

        // Author assertion from the href itself. X nests "Who to follow"
        // widgets and QT cards as article[data-testid="tweet"], so the
        // first hit inside document is NOT guaranteed to be a post by
        // the profile owner. Parse /<author>/status/<id> and bail if
        // author != TARGET_HANDLE. Companion to the profile-URL check
        // outside page.evaluate — one catches redirects, this catches
        // nested non-target content on the correct profile.
        const hrefMatch = href.match(/\/([^/]+)\/status\/(\d+)/);
        if (!hrefMatch) continue;
        const [, articleAuthor] = hrefMatch;
        if (articleAuthor.toLowerCase() !== TARGET_HANDLE.toLowerCase()) continue;

        const fullUrl = href.startsWith('http') ? href : ('https://x.com' + href);

        out.push({
          text: bodyText,
          timestamp: iso,
          url: fullUrl,
        });
        if (out.length >= MAX) break;
      } catch (e) { /* skip bad node */ }
    }
    return out;
  }, { MAX: MAX_TWEETS_PER_HANDLE, MIN_LEN: MIN_TEXT_LEN, RECENT_WINDOW_MS, TARGET_HANDLE: handle });

  logf(`  extracted ${tweets.length} candidate(s)`);
  return tweets;
}

// ── main ─────────────────────────────────────────────────────────────────────
async function main() {
  const startedAt = Date.now();
  const state = loadState();
  state.run_count = (state.run_count || 0) + 1;

  // cooldown: if we scraped less than MIN_COOLDOWN_MS ago, skip this run. this
  // guards against accidentally running the scraper in tight succession (e.g.
  // if launchd fires early and then again on schedule).
  const MIN_COOLDOWN_MS = parseInt(process.env.SCRAPER_MIN_COOLDOWN_MS || String(20 * 60 * 1000), 10);
  if (state.last_run_iso && !ONLY_HANDLE && !SCRAPE_ALL) {
    const sinceLast = Date.now() - Date.parse(state.last_run_iso);
    if (sinceLast < MIN_COOLDOWN_MS) {
      logf(`SKIP: last run was ${Math.round(sinceLast/1000)}s ago (cooldown ${Math.round(MIN_COOLDOWN_MS/1000)}s)`);
      process.stdout.write(JSON.stringify({ run: state.run_count, skipped: 'cooldown', since_last_ms: sinceLast }) + '\n');
      process.exit(0);
    }
  }

  const handles = loadHandles();
  const selected = pickHandlesForThisRun(handles, state.run_count);
  logf(`run=${state.run_count} selected=${selected.length}/${handles.length} dryRun=${DRY_RUN}`);

  // Per-handle CDP locking: connect + open fresh tab + scrape + close +
  // disconnect happens inside withCdpLock for each handle. We DO NOT hold
  // the lock across the pacing sleep — that would starve race-sensitive
  // helpers (cdp-health, gemma-target-poller) for the full ~2-5min scraper
  // run. Lock is held for ~15-30s per handle, released for 4-12s between.

  const existing = loadExistingUrls();
  const activeAuthors = loadActiveAuthorKeys();
  logf(`inbox has ${existing.size} known URLs · ${activeAuthors.size} authors already active`);

  // Track authors we add a NEW row for *within this run* so we don't add
  // multiple new rows for the same author even in a single scraper pass
  // (e.g. if two priority-1 handles alias to the same account).
  const justAddedActive = new Set();

  const newRows = [];
  const perHandleStats = {};

  for (let idx = 0; idx < selected.length; idx++) {
    const h = selected[idx];
    let tweets = [];
    try {
      tweets = await withCdpLock(async () => {
        const browser = await chromium.connectOverCDP(CDP_URL);
        const ctx = browser.contexts()[0];
        if (!ctx) throw new Error('no browser context found');
        // open a fresh tab so we don't disturb rishabh's browsing; closed
        // in the finally block so we don't leave tabs behind across handles.
        const page = await ctx.newPage();
        try {
          return await scrapeHandle(page, h);
        } finally {
          try { await page.close(); } catch (_) { /* ignore */ }
        }
      });
    } catch (e) {
      logf(`  scrape error: ${e.message}`);
      perHandleStats[h.handle] = { scraped: 0, added: 0, error: e.message };
      // still pause before next handle even on error (don't burst-retry)
      if (idx < selected.length - 1) {
        await sleep(rand(BETWEEN_HANDLE_MS_MIN, BETWEEN_HANDLE_MS_MAX));
      }
      continue;
    }

    const authorKey = (h.handle || '').toString().trim().toLowerCase();

    let added = 0;
    let parked = 0;
    for (const tw of tweets) {
      if (existing.has(tw.url)) continue;
      // Author-diversity cap: first new URL for an author that has no active
      // row becomes status="new"; all further URLs for that author get
      // status="parked" so the reply-pipeline cron never sees a stack of
      // same-author rows waiting in line. Manual promotion is still possible
      // by flipping status back to "new".
      const alreadyActive = activeAuthors.has(authorKey) || justAddedActive.has(authorKey);
      const status = alreadyActive ? 'parked' : 'new';
      const row = {
        id: makeId(),
        timestamp: tw.timestamp,
        author_handle: h.handle,
        author_context: h.author_context,
        themes: h.themes || [],
        original: tw.text,
        url: tw.url,
        status,
        source: 'x-scraper',
        scraped_at: new Date().toISOString(),
      };
      if (status === 'parked') {
        row.parked_reason = 'author-already-active';
        parked++;
      } else {
        justAddedActive.add(authorKey);
        added++;
      }
      newRows.push(row);
      existing.add(tw.url);
    }
    perHandleStats[h.handle] = { scraped: tweets.length, added, parked };

    // randomized pause between handles to avoid burst pattern detection
    if (idx < selected.length - 1) {
      const waitMs = rand(BETWEEN_HANDLE_MS_MIN, BETWEEN_HANDLE_MS_MAX);
      logf(`  ... waiting ${Math.round(waitMs/1000)}s before next handle`);
      await sleep(waitMs);
    }
  }

  // No outer page.close() needed — each per-handle withCdpLock iteration
  // opens and closes its own tab inside the lock.

  if (!DRY_RUN) {
    for (const r of newRows) appendInboxRow(r);
  }

  state.last_run_iso = new Date().toISOString();
  state.last_added = newRows.length;
  state.last_stats = perHandleStats;
  state.last_duration_ms = Date.now() - startedAt;
  saveState(state);

  // final summary goes to STDOUT (clean single line) so cron can deliver it
  const summary = {
    run: state.run_count,
    handles_scraped: selected.length,
    new_items: newRows.length,
    duration_ms: state.last_duration_ms,
    dry_run: DRY_RUN,
    per_handle: perHandleStats,
  };
  process.stdout.write(JSON.stringify(summary) + '\n');

  // give playwright a beat to drain, then exit
  setTimeout(() => process.exit(0), 500);
}

main().catch(err => {
  logf(`FATAL: ${err.stack || err.message}`);
  process.exit(1);
});
