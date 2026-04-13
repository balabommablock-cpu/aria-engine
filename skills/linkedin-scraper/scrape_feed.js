#!/usr/bin/env node
/**
 * scrape_feed.js — linkedin scraper (mirror of x-scraper)
 *
 * Visits each target's recent-activity page on linkedin.com via the CDP-attached
 * Chrome, extracts their own recent posts (not reshares, not comments), and
 * appends them to memory/target-inbox.jsonl with status="new".
 *
 * LinkedIn bot detection is even more aggressive than X's, so this script is
 * heavily paced:
 *   - 8-20s linger per page (randomized)
 *   - 2-5 scrolls with jitter
 *   - 5-15s pause between targets
 *   - network-idle wait on navigation
 *   - mouse movement simulation
 *   - cooldown: don't run if last run was < 25 min ago
 *
 * CLI flags mirror x-scraper:
 *   node scrape_feed.js                  # rotate by priority
 *   node scrape_feed.js --all             # scrape every target
 *   node scrape_feed.js --slug=deepakshenoy
 *   node scrape_feed.js --dry-run
 */

const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

// 2026-04-12: Swallow Playwright dialog race condition. See x-scraper header.
process.on('unhandledRejection', (err) => {
  if (err && err.message && err.message.includes('No dialog is showing')) return;
  console.error('[li-scraper] unhandled rejection:', err);
  process.exit(1);
});
// Filesystem mutex around CDP access. Same fix as x-scraper/scrape_timeline.js
// applied 2026-04-12 after the simonw/BalabommaRao collision. See that file
// header for details.
const { withCdpLock } = require('../../scripts/cdp-lock');

// ── paths ────────────────────────────────────────────────────────────────────
const HOME = os.homedir();
const ARIA_WORKSPACE = path.join(HOME, '.openclaw/agents/aria/workspace');
const TARGETS_PATH = path.join(ARIA_WORKSPACE, 'memory/target-handles-linkedin.json');
const INBOX_PATH = path.join(ARIA_WORKSPACE, 'memory/target-inbox.jsonl');
const STATE_PATH = path.join(ARIA_WORKSPACE, 'memory/linkedin-scraper-state.json');

// ── config ───────────────────────────────────────────────────────────────────
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:28800';
const MAX_POSTS_PER_TARGET = parseInt(process.env.LI_SCRAPER_MAX_PER_TARGET || '4', 10);
const RECENT_WINDOW_MS = parseInt(process.env.LI_SCRAPER_RECENT_WINDOW_MS || String(7 * 24 * 60 * 60 * 1000), 10);
// linkedin posts have more context; require more substance to be worth drafting.
const MIN_TEXT_LEN = 80;
const NAV_TIMEOUT_MS = 25000;

// human-pacing (even slower than x-scraper — linkedin is stricter)
const PER_PAGE_LINGER_MS_MIN = 8000;
const PER_PAGE_LINGER_MS_MAX = 20000;
const BETWEEN_TARGET_MS_MIN = 5000;
const BETWEEN_TARGET_MS_MAX = 15000;
const SCROLL_COUNT_MIN = 2;
const SCROLL_COUNT_MAX = 5;
const SCROLL_DELTA_MIN = 500;
const SCROLL_DELTA_MAX = 1500;
const SCROLL_PAUSE_MS_MIN = 900;
const SCROLL_PAUSE_MS_MAX = 2200;

function rand(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── cli flags ────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const SCRAPE_ALL = args.includes('--all');
const DRY_RUN = args.includes('--dry-run');
const ONLY_SLUG = (args.find(a => a.startsWith('--slug=')) || '').split('=')[1] || null;

// ── logging ──────────────────────────────────────────────────────────────────
const log = (...m) => console.error('[li-scraper]', ...m);
const logf = (...m) => log(...m);

// ── utils ────────────────────────────────────────────────────────────────────
function loadTargets() {
  const raw = JSON.parse(fs.readFileSync(TARGETS_PATH, 'utf8'));
  // flatten people + companies into one list, tagging type
  const people = (raw.people || []).map(p => ({ ...p, type: 'person' }));
  const companies = (raw.companies || []).map(c => ({ ...c, type: 'company' }));
  return [...people, ...companies];
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8')); }
  catch (e) { return { run_count: 0, last_run_iso: null }; }
}

function saveState(state) {
  fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2));
}

function loadExistingUrls() {
  const urls = new Set();
  if (!fs.existsSync(INBOX_PATH)) return urls;
  const lines = fs.readFileSync(INBOX_PATH, 'utf8').split('\n');
  for (const line of lines) {
    const t = line.trim();
    if (!t) continue;
    try {
      const row = JSON.parse(t);
      if (row.url) urls.add(row.url);
    } catch (e) { /* skip */ }
  }
  return urls;
}

// Mirror of the x-scraper author-diversity guard. Any author that already
// has a non-terminal row (new / processing / pending-approval) gets their
// newly-scraped posts written as status="parked" so the reply-pipeline cron
// never sees a stack of same-author rows.
//
// Freshness gate (2026-04-12): mirrors the same fix landed in
// x-scraper/scrape_timeline.js. Stale rows (older than reply-auto's
// INBOX_FRESH_HOURS window) must NOT count as active, otherwise zombie
// rows silently starve the author of new engagement.
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
      const postTs = row.timestamp ? Date.parse(row.timestamp) : NaN;
      if (isFinite(postTs) && (now - postTs) > ACTIVE_FRESH_WINDOW_MS) continue;
      const raw = (row.author_handle || row.author_slug || row.author || '').toString().trim().toLowerCase();
      if (raw) active.add(raw);
    } catch (e) { /* skip */ }
  }
  return active;
}

function appendInboxRow(row) {
  fs.appendFileSync(INBOX_PATH, JSON.stringify(row) + '\n');
}

function makeId() {
  return 'li-' + crypto.randomBytes(6).toString('hex');
}

function pickTargetsForThisRun(targets, runCount) {
  if (SCRAPE_ALL) return targets;
  if (ONLY_SLUG) {
    return targets.filter(t => t.slug.toLowerCase() === ONLY_SLUG.toLowerCase());
  }
  return targets.filter(t => {
    const p = t.priority || 2;
    if (p === 1) return true;
    if (p === 2) return runCount % 2 === 0;
    if (p === 3) return runCount % 6 === 0;
    return true;
  });
}

function profileUrlFor(target) {
  if (target.type === 'company') {
    return `https://www.linkedin.com/company/${target.slug}/posts/?feedView=all`;
  }
  return `https://www.linkedin.com/in/${target.slug}/recent-activity/all/`;
}

// ── scrape one target ────────────────────────────────────────────────────────
async function scrapeTarget(page, target) {
  const url = profileUrlFor(target);
  logf(`→ ${target.handle} (${target.type}, ${target.slug})`);

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
    try { await page.waitForLoadState('networkidle', { timeout: 8000 }); }
    catch (e) { /* networkidle often times out on feeds, fine */ }
  } catch (e) {
    logf(`  nav failed: ${e.message}`);
    return [];
  }

  // detect auth wall: linkedin redirects to /login if not logged in
  const currentUrl = page.url();
  if (/\/login/.test(currentUrl) || /\/uas\/login/.test(currentUrl)) {
    logf(`  AUTH WALL: redirected to login — user not logged into linkedin`);
    return [];
  }

  // Landing-URL assertion. LinkedIn redirects stale slugs to current ones
  // without warning — if /in/old-slug becomes /in/new-slug, we'd scrape
  // new-slug's posts and attribute them to the old-slug config row. This
  // is the same attribution bug class that bit target-poll-helper and
  // find-my-tweet-url, just with slug-redirects instead of CDP stale-page
  // contamination. Companion to the 2026-04-12 CDP hygiene sweep.
  const slugMatch = currentUrl.match(/\/(?:in|company)\/([^/?#]+)/i);
  if (!slugMatch || slugMatch[1].toLowerCase() !== target.slug.toLowerCase()) {
    logf(`  SLUG-REDIRECT: wanted /${target.slug}, landed on ${currentUrl} — skipping to avoid attribution poisoning`);
    return [];
  }

  // detect the "Join LinkedIn" auth wall that sometimes shows for non-logged
  // users without a redirect
  const hasAuthWall = await page.evaluate(() => {
    const bodyText = document.body.innerText || '';
    return /Sign in to view/i.test(bodyText) || /Join LinkedIn/i.test(bodyText) && bodyText.length < 500;
  });
  if (hasAuthWall) {
    logf(`  AUTH WALL: page has auth prompt — user not logged in`);
    return [];
  }

  // initial settle
  await sleep(rand(1500, 3500));

  try {
    const vp = page.viewportSize() || { width: 1200, height: 800 };
    await page.mouse.move(rand(200, vp.width - 200), rand(200, vp.height - 200), { steps: rand(5, 15) });
  } catch (e) { /* best-effort */ }

  // scroll multiple times to force lazy-load of posts below the fold
  const scrollCount = rand(SCROLL_COUNT_MIN, SCROLL_COUNT_MAX);
  for (let i = 0; i < scrollCount; i++) {
    const delta = rand(SCROLL_DELTA_MIN, SCROLL_DELTA_MAX);
    await page.evaluate((d) => window.scrollBy(0, d), delta);
    await sleep(rand(SCROLL_PAUSE_MS_MIN, SCROLL_PAUSE_MS_MAX));
  }

  // extract posts
  const posts = await page.evaluate((opts) => {
    const { MAX, MIN_LEN, RECENT_WINDOW_MS } = opts;
    const now = Date.now();

    // linkedin's DOM is notoriously volatile. try multiple known selectors
    // for the feed update wrapper.
    const selectors = [
      'div.feed-shared-update-v2',
      'div[data-test-id="main-feed-activity-card"]',
      'div.occludable-update',
      'article.update-components-update-v2',
    ];
    let updates = [];
    for (const sel of selectors) {
      const found = Array.from(document.querySelectorAll(sel));
      if (found.length > 0) { updates = found; break; }
    }

    const out = [];
    for (const u of updates) {
      try {
        // skip reshares AND recent-activity-feed contamination — the
        // /recent-activity/all/ feed includes posts that the target LIKED
        // or COMMENTED on (where the body is someone else's content with
        // a "X liked this" / "X commented on this" header), and naively
        // extracting body-text there produces attribution bugs: we'd store
        // the OTHER person's post body with our target's author_handle.
        const shareContext = u.querySelector('.update-components-header, .feed-shared-update-v2__context');
        if (shareContext) {
          const t = (shareContext.innerText || '').toLowerCase();
          if (/reshared|reposted|shared this|liked this|commented on this|replied to this|celebrated|congratulated|follows? this/.test(t)) continue;
        }

        // extract body text — try several known selectors
        const bodyEl = u.querySelector(
          '.update-components-text .break-words, ' +
          '.feed-shared-update-v2__description .break-words, ' +
          '.feed-shared-text, ' +
          'div[dir="ltr"] .break-words'
        );
        if (!bodyEl) continue;
        const bodyText = (bodyEl.innerText || '').trim();
        if (!bodyText || bodyText.length < MIN_LEN) continue;

        // skip if it's just a reshare quoted-body (no original commentary)
        // detected heuristically: very short and followed by another update block

        // extract post URL from the "follow" / share menu — linkedin embeds
        // urn:li:activity:... ids and sometimes a direct anchor on the time
        const timeAnchor = u.querySelector('a.feed-shared-actor__sub-description-link, a[href*="/feed/update/"], a[data-test-app-aware-link][href*="activity"]');
        let postUrl = null;
        if (timeAnchor) {
          const href = timeAnchor.getAttribute('href');
          if (href) {
            postUrl = href.startsWith('http') ? href : ('https://www.linkedin.com' + href);
            // strip query params that often include tracking
            postUrl = postUrl.split('?')[0];
          }
        }

        // fallback: scrape the urn from data attribute
        if (!postUrl) {
          const urn = u.getAttribute('data-urn') || u.getAttribute('data-id');
          if (urn && /activity:\d+/.test(urn)) {
            const m = urn.match(/activity:(\d+)/);
            if (m) postUrl = `https://www.linkedin.com/feed/update/urn:li:activity:${m[1]}`;
          }
        }

        if (!postUrl) continue;

        // extract the author name from the actor block (so we can verify it's
        // actually posts BY this person, not content they liked)
        const authorEl = u.querySelector('.update-components-actor__title, .feed-shared-actor__name');
        const authorText = authorEl ? (authorEl.innerText || '').trim() : '';

        // age detection is hard on linkedin (they use "2d", "3h" strings not
        // iso timestamps). accept everything that made it this far — rely on
        // the url-dedupe in the parent to avoid re-adding.
        out.push({
          text: bodyText,
          url: postUrl,
          author: authorText,
          timestamp: new Date().toISOString(),  // approximate — linkedin doesn't expose iso
        });
        if (out.length >= MAX) break;
      } catch (e) { /* skip bad node */ }
    }
    return out;
  }, { MAX: MAX_POSTS_PER_TARGET, MIN_LEN: MIN_TEXT_LEN, RECENT_WINDOW_MS });

  // linger to simulate reading
  const linger = rand(PER_PAGE_LINGER_MS_MIN, PER_PAGE_LINGER_MS_MAX);
  await sleep(Math.max(linger - scrollCount * 1500, 500));

  logf(`  extracted ${posts.length} candidate(s)`);
  return posts;
}

// ── main ─────────────────────────────────────────────────────────────────────
async function main() {
  const startedAt = Date.now();
  const state = loadState();
  state.run_count = (state.run_count || 0) + 1;

  const MIN_COOLDOWN_MS = parseInt(process.env.LI_SCRAPER_MIN_COOLDOWN_MS || String(25 * 60 * 1000), 10);
  if (state.last_run_iso && !ONLY_SLUG && !SCRAPE_ALL) {
    const sinceLast = Date.now() - Date.parse(state.last_run_iso);
    if (sinceLast < MIN_COOLDOWN_MS) {
      logf(`SKIP: last run was ${Math.round(sinceLast/1000)}s ago (cooldown ${Math.round(MIN_COOLDOWN_MS/1000)}s)`);
      process.stdout.write(JSON.stringify({ run: state.run_count, skipped: 'cooldown', since_last_ms: sinceLast }) + '\n');
      process.exit(0);
    }
  }

  const targets = loadTargets();
  const selected = pickTargetsForThisRun(targets, state.run_count);
  logf(`run=${state.run_count} selected=${selected.length}/${targets.length} dryRun=${DRY_RUN}`);

  // Per-target CDP locking — same rationale as x-scraper. Lock is held for
  // ~20-40s per target (linkedin is slow), released during BETWEEN_TARGET
  // sleeps so race-sensitive helpers (cdp-health, gemma-target-poller) can
  // acquire during the gap.

  const existing = loadExistingUrls();
  const activeAuthors = loadActiveAuthorKeys();
  logf(`inbox has ${existing.size} known URLs · ${activeAuthors.size} authors already active`);

  const justAddedActive = new Set();
  const newRows = [];
  const perTargetStats = {};
  let authWallHit = false;

  for (let idx = 0; idx < selected.length; idx++) {
    const t = selected[idx];
    let posts = [];
    // The auth-wall check needs the final URL of the scrape attempt, so we
    // capture it inside the lock closure alongside the posts array.
    let finalUrl = '';
    try {
      const result = await withCdpLock(async () => {
        const browser = await chromium.connectOverCDP(CDP_URL);
        const ctx = browser.contexts()[0];
        if (!ctx) throw new Error('no browser context');
        const page = await ctx.newPage();
        try {
          const scraped = await scrapeTarget(page, t);
          return { posts: scraped, finalUrl: page.url() };
        } finally {
          try { await page.close(); } catch (_) { /* ignore */ }
        }
      });
      posts = result.posts;
      finalUrl = result.finalUrl;
    } catch (e) {
      logf(`  scrape error: ${e.message}`);
      perTargetStats[t.handle] = { scraped: 0, added: 0, error: e.message };
      if (idx < selected.length - 1) {
        await sleep(rand(BETWEEN_TARGET_MS_MIN, BETWEEN_TARGET_MS_MAX));
      }
      continue;
    }

    // if first target returned an auth wall, linkedin session is dead — abort run
    if (posts.length === 0 && idx === 0) {
      if (/\/login/.test(finalUrl) || /\/uas/.test(finalUrl)) {
        authWallHit = true;
        perTargetStats[t.handle] = { scraped: 0, added: 0, error: 'auth-wall' };
        break;
      }
    }

    // Author key — prefer handle (display name), fall back to slug
    const authorKey = ((t.handle || t.slug) || '').toString().trim().toLowerCase();

    let added = 0;
    let parked = 0;
    for (const p of posts) {
      if (existing.has(p.url)) continue;
      const alreadyActive = activeAuthors.has(authorKey) || justAddedActive.has(authorKey);
      const status = alreadyActive ? 'parked' : 'new';
      const row = {
        id: makeId(),
        timestamp: p.timestamp,
        author_handle: t.handle,
        author_slug: t.slug,
        author_context: t.author_context,
        themes: t.themes || [],
        original: p.text,
        url: p.url,
        status,
        source: 'linkedin-scraper',
        platform: 'linkedin',
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
      existing.add(p.url);
    }
    perTargetStats[t.handle] = { scraped: posts.length, added, parked };

    if (idx < selected.length - 1) {
      const waitMs = rand(BETWEEN_TARGET_MS_MIN, BETWEEN_TARGET_MS_MAX);
      logf(`  ... waiting ${Math.round(waitMs/1000)}s before next target`);
      await sleep(waitMs);
    }
  }

  // No outer page.close() — each per-target withCdpLock iteration opens
  // and closes its own tab inside the lock.

  if (!DRY_RUN) {
    for (const r of newRows) appendInboxRow(r);
  }

  state.last_run_iso = new Date().toISOString();
  state.last_added = newRows.length;
  state.last_stats = perTargetStats;
  state.last_duration_ms = Date.now() - startedAt;
  state.last_auth_wall = authWallHit;
  saveState(state);

  const summary = {
    run: state.run_count,
    targets_scraped: selected.length,
    new_items: newRows.length,
    duration_ms: state.last_duration_ms,
    dry_run: DRY_RUN,
    auth_wall: authWallHit,
    per_target: perTargetStats,
  };
  process.stdout.write(JSON.stringify(summary) + '\n');

  setTimeout(() => process.exit(authWallHit ? 4 : 0), 500);
}

main().catch(err => {
  logf(`FATAL: ${err.stack || err.message}`);
  process.exit(1);
});
