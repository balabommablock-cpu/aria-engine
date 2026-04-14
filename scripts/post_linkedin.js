/**
 * post_linkedin.js -- LinkedIn post publisher via Playwright CDP.
 *
 * Connects to the user's Chrome (same CDP instance as X),
 * opens a NEW tab for LinkedIn, posts content, closes tab.
 * Uses cdp-lock to avoid contention with X poster.
 *
 * Usage:
 *   node post_linkedin.js "Your LinkedIn post content here"
 */

const { chromium } = require('playwright');
const { withCdpLock } = require('./cdp-lock');

const CONFIG = {
  CDP_URL: process.env.CDP_URL || 'http://127.0.0.1:28800',
};

function randBetween(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/**
 * Post content to LinkedIn.
 * Opens a new tab, navigates to LinkedIn, uses the composer, posts, closes tab.
 */
async function postToLinkedIn(content, imagePath = '') {
  console.log('linkedin-poster: starting...');
  console.log('  content length:', content.length);
  console.log('  preview:', content.substring(0, 80) + '...');
  if (imagePath) console.log('  image:', imagePath);

  let browser;
  let page;
  try {
    // 1. Connect to Chrome
    browser = await chromium.connectOverCDP(CONFIG.CDP_URL);
    const context = browser.contexts()[0];

    // Open a NEW tab so we don't disrupt X tabs
    page = await context.newPage();

    // 2. Navigate to LinkedIn feed
    console.log('  navigating to linkedin.com/feed...');
    await page.goto('https://www.linkedin.com/feed/', {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    await sleep(randBetween(3000, 5000));

    // 3. Check if logged in (look for feed elements or sign-in prompt)
    const loginState = await page.evaluate(() => {
      // If we see a sign-in button or join form, not logged in
      if (document.querySelector('[data-tracking-control-name="guest_homepage-basic_sign-in-button"]'))
        return 'not_logged_in';
      if (document.querySelector('.join-form'))
        return 'not_logged_in';
      // If we see the feed or nav, we're in
      if (document.querySelector('.feed-shared-update-v2') ||
          document.querySelector('.global-nav') ||
          document.querySelector('[data-test-id="feed-sort-toggle"]') ||
          document.querySelector('.share-box'))
        return 'logged_in';
      return 'unknown';
    });

    if (loginState === 'not_logged_in') {
      throw new Error('not logged into LinkedIn in this Chrome instance');
    }
    console.log('  login state:', loginState);

    // 4. Click "Start a post" to open composer
    console.log('  opening composer...');
    const startPostSelectors = [
      // modern LinkedIn feed composer triggers
      '.share-box-feed-entry__trigger',
      'button.share-box-feed-entry__trigger',
      '[data-view-name="share-box-feed-entry__trigger"]',
      '.share-box-feed-entry__closed-share-box button',
      // text-based fallback
      'button:has-text("Start a post")',
      // the share box top area that acts as trigger
      '.share-box-feed-entry__top-bar',
    ];

    let composerOpened = false;
    for (const sel of startPostSelectors) {
      try {
        const el = page.locator(sel).first();
        if ((await el.count()) > 0) {
          await el.click({ timeout: 5000 });
          composerOpened = true;
          console.log('  clicked start-a-post via:', sel);
          break;
        }
      } catch (_) {
        // try next selector
      }
    }

    if (!composerOpened) {
      // last resort: click the share box area itself
      try {
        await page.click('.share-box', { timeout: 5000 });
        composerOpened = true;
        console.log('  clicked share-box container');
      } catch (_) {}
    }

    if (!composerOpened) {
      throw new Error('could not find "Start a post" button on LinkedIn feed');
    }

    await sleep(randBetween(2000, 3500));

    // 5. Find the text editor in the modal
    console.log('  looking for editor...');
    const editorSelectors = [
      '[role="textbox"][contenteditable="true"]',
      '.ql-editor[contenteditable="true"]',
      '[data-placeholder="What do you want to talk about?"]',
      '.editor-content [contenteditable="true"]',
      '.share-creation-state__text-editor [contenteditable="true"]',
      // broader: any contenteditable inside a dialog
      '[role="dialog"] [contenteditable="true"]',
    ];

    let editorFound = false;
    for (const sel of editorSelectors) {
      try {
        const el = page.locator(sel).first();
        if ((await el.count()) > 0) {
          await el.click({ timeout: 5000 });
          editorFound = true;
          console.log('  found editor via:', sel);
          break;
        }
      } catch (_) {}
    }

    if (!editorFound) {
      throw new Error('could not find text editor in LinkedIn composer modal');
    }
    await sleep(randBetween(300, 600));

    // 6. Type content with human-like delay
    console.log('  typing', content.length, 'characters...');

    // LinkedIn's editor is a contenteditable div. Playwright's keyboard.type
    // works well with these. Use a moderate delay for natural appearance.
    // For very long content (>1500 chars), use faster typing to avoid timeout.
    const typingDelay = content.length > 1500 ? randBetween(8, 18) : randBetween(18, 40);
    await page.keyboard.type(content, { delay: typingDelay });
    console.log('  typed successfully');
    await sleep(randBetween(1500, 3000));

    // 6b. Attach image if provided
    if (imagePath) {
      console.log('  attaching image:', imagePath);
      // LinkedIn composer has a file input for media. Find it.
      const fileInputSelectors = [
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
      ];
      let imageAttached = false;
      for (const sel of fileInputSelectors) {
        try {
          const input = page.locator(sel).first();
          if ((await input.count()) > 0) {
            await input.setInputFiles(imagePath);
            imageAttached = true;
            console.log('  image attached via:', sel);
            break;
          }
        } catch (_) {}
      }

      if (!imageAttached) {
        // fallback: click the image/media button to trigger file dialog
        const mediaButtonSelectors = [
          'button[aria-label="Add a photo"]',
          'button[aria-label="Add media"]',
          '[data-test-icon="image-medium"]',
          'button:has([data-test-icon="image-medium"])',
        ];
        for (const sel of mediaButtonSelectors) {
          try {
            const btn = page.locator(sel).first();
            if ((await btn.count()) > 0) {
              await btn.click({ timeout: 3000 });
              await sleep(1500);
              // now try file input again
              const input = page.locator('input[type="file"]').first();
              if ((await input.count()) > 0) {
                await input.setInputFiles(imagePath);
                imageAttached = true;
                console.log('  image attached via media button +', sel);
              }
              break;
            }
          } catch (_) {}
        }
      }

      if (imageAttached) {
        // wait for upload preview
        await sleep(randBetween(3000, 5000));
        console.log('  image upload complete');
      } else {
        console.log('  warning: could not attach image, posting text-only');
      }
    }

    // 7. Click the Post button
    console.log('  clicking Post...');
    const postSelectors = [
      'button.share-actions__primary-action',
      '[data-control-name="share.post"]',
      // text-based: look for a "Post" button inside the dialog
      '[role="dialog"] button:has-text("Post")',
      'button:has-text("Post")',
    ];

    let postClicked = false;
    for (const sel of postSelectors) {
      try {
        const candidates = await page.locator(sel).all();
        for (const btn of candidates) {
          const text = ((await btn.innerText()) || '').trim();
          // make sure it says "Post" and not "Repost" or "Post settings"
          if (text === 'Post' || text === 'Post' || sel.includes('primary-action')) {
            const disabled = await btn.getAttribute('disabled');
            const ariaDisabled = await btn.getAttribute('aria-disabled');
            if (disabled || ariaDisabled === 'true') continue;
            await btn.click({ timeout: 5000 });
            postClicked = true;
            console.log('  clicked Post via:', sel, '(text:', text + ')');
            break;
          }
        }
        if (postClicked) break;
      } catch (_) {}
    }

    if (!postClicked) {
      // keyboard fallback: Ctrl/Cmd+Enter
      const sendKey = process.platform === 'darwin' ? 'Meta+Enter' : 'Control+Enter';
      console.log('  post button not found, trying', sendKey);
      await page.keyboard.press(sendKey);
    }

    // 8. Wait for success signal
    // LinkedIn's post confirmation is unreliable via DOM checks.
    // Strategy: wait, then check multiple signals. If we clicked Post
    // successfully and no error appeared, assume success.
    console.log('  waiting for confirmation...');
    await sleep(5000);

    let success = false;
    let message = 'unknown';

    const state = await page.evaluate(() => {
      // check for any modal / composer still open
      const dialogs = document.querySelectorAll('[role="dialog"]');
      const editors = document.querySelectorAll('[contenteditable="true"]');
      const toasts = Array.from(
        document.querySelectorAll('[role="alert"], .artdeco-toast-item, .artdeco-toasts')
      );
      const toastText = toasts.map((t) => (t.innerText || '').toLowerCase()).join(' | ');
      // check if any editor still has content (means post didn't go through)
      let editorHasContent = false;
      editors.forEach((e) => {
        if ((e.innerText || '').trim().length > 20) editorHasContent = true;
      });
      return {
        dialogCount: dialogs.length,
        editorHasContent,
        toastText,
        url: window.location.href,
      };
    });

    console.log('  state:', JSON.stringify(state));

    // error toast is a hard failure
    if (
      state.toastText.includes('error') ||
      state.toastText.includes('failed') ||
      state.toastText.includes('try again') ||
      state.toastText.includes('something went wrong')
    ) {
      message = 'LinkedIn error: ' + state.toastText.slice(0, 120);
    }
    // editor still has our content means it didn't submit
    else if (state.editorHasContent) {
      // wait a bit more and retry check
      await sleep(5000);
      const recheck = await page.evaluate(() => {
        const eds = document.querySelectorAll('[contenteditable="true"]');
        let has = false;
        eds.forEach((e) => {
          if ((e.innerText || '').trim().length > 20) has = true;
        });
        return has;
      });
      if (recheck) {
        message = 'post may not have submitted (editor still has content)';
      } else {
        success = true;
        message = 'post published (editor cleared after delay)';
      }
    }
    // dialog gone or no content in editors = success
    else {
      success = true;
      message = 'post published';
      if (state.toastText) {
        message += ' (toast: ' + state.toastText.slice(0, 60) + ')';
      }
    }

    // 9. Try to grab the post URL from the feed
    let postUrl = null;
    if (success) {
      await sleep(2000);
      try {
        // navigate to profile to find the new post
        await page.goto('https://www.linkedin.com/in/me/recent-activity/all/', {
          waitUntil: 'domcontentloaded',
          timeout: 15000,
        });
        await sleep(3000);
        postUrl = await page.evaluate(() => {
          // find the most recent activity link
          const links = document.querySelectorAll('a[href*="/feed/update/"]');
          return links.length > 0 ? links[0].href : null;
        });
      } catch (_) {
        // url capture is nice-to-have, not critical
      }
    }

    console.log('  result:', success ? 'SUCCESS' : 'FAILED', '-', message);
    return {
      success,
      message,
      url: postUrl || 'url not captured',
    };
  } catch (error) {
    console.error('  error:', error.message);
    return { success: false, message: error.message };
  } finally {
    // always close the LinkedIn tab
    try {
      if (page) await page.close();
    } catch (_) {}
    try {
      if (browser) await browser.close();
    } catch (_) {}
  }
}

// CLI entry point
if (require.main === module) {
  const content = process.argv[2];
  const imagePath = process.argv[3] || '';
  if (!content) {
    console.error('usage: node post_linkedin.js "post content" [image_path]');
    process.exit(1);
  }

  withCdpLock(() => postToLinkedIn(content, imagePath), 180000).then((result) => {
    console.log('\nresult:', JSON.stringify(result));
    process.exit(result.success ? 0 : 1);
  });
}

module.exports = { postToLinkedIn };
