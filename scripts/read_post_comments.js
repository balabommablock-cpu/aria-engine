/**
 * read_post_comments.js -- Read comments on a LinkedIn post via CDP.
 *
 * Usage:
 *   node read_post_comments.js <post_url>
 *
 * Output: JSON to stdout { error, comments: [{name, slug, headline, text, ...}] }
 */

const { chromium } = require('playwright');
const { withCdpLock } = require('./cdp-lock');

const CONFIG = {
  CDP_URL: process.env.CDP_URL || 'http://127.0.0.1:28800',
  MAX_COMMENTS: 30,
};

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function randBetween(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

async function readComments(postUrl) {
  let browser, page;
  try {
    browser = await chromium.connectOverCDP(CONFIG.CDP_URL);
    const context = browser.contexts()[0];
    page = await context.newPage();

    console.error(`read-comments: navigating to ${postUrl}`);
    await page.goto(postUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(randBetween(3000, 5000));

    // Check login
    const loggedIn = await page.evaluate(() => {
      return !document.querySelector('.join-form') &&
             !document.querySelector('[data-tracking-control-name="guest_homepage-basic_sign-in-button"]');
    });
    if (!loggedIn) {
      return { error: 'not logged in', comments: [] };
    }

    // Click "Load more comments" if available, scroll to reveal comments
    for (let i = 0; i < 3; i++) {
      await page.evaluate(() => window.scrollBy(0, 600));
      await sleep(1000);
      try {
        const loadMore = page.locator(
          'button:has-text("Load more comments"), ' +
          'button:has-text("Show previous comments"), ' +
          'button.comments-comments-list__load-more-comments-button'
        ).first();
        if ((await loadMore.count()) > 0) {
          await loadMore.click({ timeout: 3000 });
          await sleep(2000);
        }
      } catch (_) {}
    }

    // Extract comments
    const comments = await page.evaluate((maxComments) => {
      const results = [];
      const commentEls = document.querySelectorAll(
        '.comments-comment-item, ' +
        '.comments-comment-entity, ' +
        '.comments-comment-item__content-wrapper'
      );

      for (const el of commentEls) {
        if (results.length >= maxComments) break;

        // Commenter info
        const nameEl = el.querySelector(
          '.comments-post-meta__name-text, ' +
          '.comments-comment-item__post-meta .hoverable-link-text, ' +
          'a.app-aware-link span[dir="ltr"]'
        );
        const name = nameEl ? nameEl.innerText.trim() : '';

        const profileLink = el.querySelector(
          '.comments-post-meta__actor-link, ' +
          'a.app-aware-link[href*="/in/"]'
        );
        let slug = '';
        if (profileLink) {
          const href = profileLink.getAttribute('href') || '';
          const match = href.match(/\/in\/([^/?]+)/);
          if (match) slug = match[1];
        }

        const headlineEl = el.querySelector(
          '.comments-post-meta__headline, ' +
          '.comments-comment-item__post-meta .t-black--light'
        );
        const headline = headlineEl ? headlineEl.innerText.trim() : '';

        // Comment text
        const textEl = el.querySelector(
          '.comments-comment-item__main-content, ' +
          '.comments-comment-item__inline-show-more-text, ' +
          '.feed-shared-inline-show-more-text span[dir="ltr"]'
        );
        const text = textEl ? textEl.innerText.trim() : '';
        if (!text) continue;

        // Time
        const timeEl = el.querySelector('time, .comments-comment-item__timestamp');
        const timeLabel = timeEl
          ? (timeEl.getAttribute('datetime') || timeEl.innerText.trim())
          : '';

        // Reply count (nested comments)
        const replyCountEl = el.querySelector(
          '.comments-comment-item__reply-count, ' +
          'button:has-text("repl")'
        );
        const replyCount = replyCountEl
          ? parseInt((replyCountEl.innerText || '').replace(/[^0-9]/g, ''), 10) || 0
          : 0;

        results.push({
          name,
          slug,
          headline: headline.substring(0, 200),
          text: text.substring(0, 1000),
          time_label: timeLabel,
          reply_count: replyCount,
        });
      }

      return results;
    }, CONFIG.MAX_COMMENTS);

    // Also extract the post text itself for context
    const postText = await page.evaluate(() => {
      const textEl = document.querySelector(
        '.feed-shared-update-v2__description, ' +
        '.feed-shared-text, ' +
        '.break-words span[dir="ltr"], ' +
        '.update-components-text span[dir="ltr"]'
      );
      return textEl ? textEl.innerText.trim().substring(0, 2000) : '';
    });

    console.error(`read-comments: found ${comments.length} comments`);
    return { error: null, post_text: postText, comments };
  } catch (error) {
    console.error(`read-comments error: ${error.message}`);
    return { error: error.message, comments: [] };
  } finally {
    try { if (page) await page.close(); } catch (_) {}
    try { if (browser) await browser.close(); } catch (_) {}
  }
}

// CLI entry
if (require.main === module) {
  const postUrl = process.argv[2];
  if (!postUrl) {
    console.error('usage: node read_post_comments.js <post_url>');
    process.exit(1);
  }

  withCdpLock(() => readComments(postUrl), 120000)
    .then((result) => {
      console.log(JSON.stringify(result));
      process.exit(result.error ? 1 : 0);
    })
    .catch((err) => {
      console.log(JSON.stringify({ error: err.message, comments: [] }));
      process.exit(1);
    });
}

module.exports = { readComments };
