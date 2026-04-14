/**
 * scan_linkedin_profile.js -- Scan a LinkedIn profile's recent activity.
 *
 * Connects via CDP, navigates to the profile's activity page,
 * extracts recent posts with text + engagement counts.
 *
 * Usage:
 *   node scan_linkedin_profile.js <profile_slug>
 *   node scan_linkedin_profile.js shreyasdoshi
 *
 * Output: JSON array of posts to stdout.
 */

const { chromium } = require('playwright');
const { withCdpLock } = require('./cdp-lock');

const CONFIG = {
  CDP_URL: process.env.CDP_URL || 'http://127.0.0.1:28800',
  MAX_POSTS: 5,
  PAGE_TIMEOUT: 30000,
};

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function randBetween(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

async function scanProfile(slug) {
  let browser, page;
  try {
    browser = await chromium.connectOverCDP(CONFIG.CDP_URL);
    const context = browser.contexts()[0];
    page = await context.newPage();

    // Navigate to activity page
    const url = `https://www.linkedin.com/in/${slug}/recent-activity/all/`;
    console.error(`scan: navigating to ${url}`);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: CONFIG.PAGE_TIMEOUT });
    await sleep(randBetween(3000, 5000));

    // Check if page loaded properly
    const pageState = await page.evaluate(() => {
      if (document.querySelector('.join-form') ||
          document.querySelector('[data-tracking-control-name="guest_homepage-basic_sign-in-button"]'))
        return 'not_logged_in';
      if (document.querySelector('.profile-creator-shared-feed-update__container') ||
          document.querySelector('.feed-shared-update-v2') ||
          document.querySelector('[data-urn]'))
        return 'loaded';
      return 'unknown';
    });

    if (pageState === 'not_logged_in') {
      return { error: 'not logged in', posts: [] };
    }

    // Scroll once to load more posts
    await page.evaluate(() => window.scrollBy(0, 800));
    await sleep(randBetween(1500, 2500));

    // Extract posts from the activity feed
    const posts = await page.evaluate((maxPosts) => {
      const results = [];
      // LinkedIn activity feed uses various container selectors
      const containers = document.querySelectorAll(
        '.feed-shared-update-v2, [data-urn*="activity"], .profile-creator-shared-feed-update__container'
      );

      for (const container of containers) {
        if (results.length >= maxPosts) break;

        // Extract post text
        const textEl = container.querySelector(
          '.feed-shared-update-v2__description, ' +
          '.feed-shared-text, ' +
          '.break-words span[dir="ltr"], ' +
          '.update-components-text span[dir="ltr"]'
        );
        const text = textEl ? textEl.innerText.trim() : '';
        if (!text || text.length < 20) continue;

        // Extract engagement counts
        const likesEl = container.querySelector(
          '.social-details-social-counts__reactions-count, ' +
          '[data-test-id="social-actions__reaction-count"]'
        );
        const commentsEl = container.querySelector(
          '.social-details-social-counts__comments, ' +
          'button[aria-label*="comment"]'
        );
        const sharesEl = container.querySelector(
          '.social-details-social-counts__item--with-social-proof'
        );

        const parseSocialCount = (el) => {
          if (!el) return 0;
          const t = (el.innerText || el.getAttribute('aria-label') || '').replace(/[^0-9]/g, '');
          return parseInt(t, 10) || 0;
        };

        // Extract post URL from data-urn or link
        let postUrl = '';
        const urn = container.getAttribute('data-urn');
        if (urn) {
          const activityId = urn.split(':').pop();
          postUrl = `https://www.linkedin.com/feed/update/urn:li:activity:${activityId}/`;
        }
        if (!postUrl) {
          const link = container.querySelector('a[href*="/feed/update/"]');
          if (link) postUrl = link.href;
        }

        // Extract timestamp
        const timeEl = container.querySelector(
          '.feed-shared-actor__sub-description span, ' +
          'time, ' +
          '.update-components-actor__sub-description span'
        );
        const timeText = timeEl ? timeEl.innerText.trim() : '';

        results.push({
          text: text.substring(0, 2000),
          url: postUrl,
          likes: parseSocialCount(likesEl),
          comments: parseSocialCount(commentsEl),
          shares: parseSocialCount(sharesEl),
          time_label: timeText,
          text_length: text.length,
        });
      }

      return results;
    }, CONFIG.MAX_POSTS);

    console.error(`scan: found ${posts.length} posts for ${slug}`);
    return { error: null, posts };
  } catch (error) {
    console.error(`scan error: ${error.message}`);
    return { error: error.message, posts: [] };
  } finally {
    try { if (page) await page.close(); } catch (_) {}
    try { if (browser) await browser.close(); } catch (_) {}
  }
}

// CLI entry
if (require.main === module) {
  const slug = process.argv[2];
  if (!slug) {
    console.error('usage: node scan_linkedin_profile.js <profile_slug>');
    process.exit(1);
  }

  withCdpLock(() => scanProfile(slug), 120000)
    .then((result) => {
      // Posts go to stdout (JSON), logs go to stderr
      console.log(JSON.stringify(result));
      process.exit(result.error ? 1 : 0);
    })
    .catch((err) => {
      console.log(JSON.stringify({ error: err.message, posts: [] }));
      process.exit(1);
    });
}

module.exports = { scanProfile };
