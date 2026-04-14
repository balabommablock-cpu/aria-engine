/**
 * post_linkedin_comment.js -- Post a comment on a LinkedIn post via CDP.
 *
 * Usage:
 *   node post_linkedin_comment.js <post_url> "comment text here"
 *
 * Output: JSON result to stdout { success, message }
 */

const { chromium } = require('playwright');
const { withCdpLock } = require('./cdp-lock');

const CONFIG = {
  CDP_URL: process.env.CDP_URL || 'http://127.0.0.1:28800',
};

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function randBetween(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

async function postComment(postUrl, commentText) {
  let browser, page;
  try {
    browser = await chromium.connectOverCDP(CONFIG.CDP_URL);
    const context = browser.contexts()[0];
    page = await context.newPage();

    // Navigate to the post
    console.error(`comment: navigating to ${postUrl}`);
    await page.goto(postUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(randBetween(3000, 5000));

    // Check login state
    const loggedIn = await page.evaluate(() => {
      if (document.querySelector('.join-form') ||
          document.querySelector('[data-tracking-control-name="guest_homepage-basic_sign-in-button"]'))
        return false;
      return true;
    });
    if (!loggedIn) {
      return { success: false, message: 'not logged in' };
    }

    // Find and click the comment button/area to focus the comment box
    console.error('comment: looking for comment input...');
    const commentTriggerSelectors = [
      // The comment button that reveals the input
      'button[aria-label*="Comment"]',
      'button[aria-label*="comment"]',
      '[data-control-name="comment"]',
      // Sometimes the comment box is already visible
      '.comments-comment-box__form',
      '.comments-comment-texteditor',
    ];

    let commentAreaOpened = false;
    for (const sel of commentTriggerSelectors) {
      try {
        const el = page.locator(sel).first();
        if ((await el.count()) > 0) {
          await el.click({ timeout: 5000 });
          commentAreaOpened = true;
          console.error(`comment: clicked trigger via: ${sel}`);
          break;
        }
      } catch (_) {}
    }

    if (!commentAreaOpened) {
      // Try scrolling to the social actions bar first
      await page.evaluate(() => {
        const actions = document.querySelector('.social-details-social-activity');
        if (actions) actions.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
      await sleep(1500);
      // Retry
      for (const sel of commentTriggerSelectors) {
        try {
          const el = page.locator(sel).first();
          if ((await el.count()) > 0) {
            await el.click({ timeout: 5000 });
            commentAreaOpened = true;
            console.error(`comment: clicked trigger after scroll via: ${sel}`);
            break;
          }
        } catch (_) {}
      }
    }

    await sleep(randBetween(1500, 2500));

    // Find the comment text editor
    console.error('comment: looking for editor...');
    const editorSelectors = [
      '.comments-comment-box [role="textbox"][contenteditable="true"]',
      '.ql-editor[contenteditable="true"]',
      '.comments-comment-texteditor [contenteditable="true"]',
      '[data-placeholder="Add a comment\u2026"] ',
      '.comments-comment-box__form [contenteditable="true"]',
      // broader fallback
      '.comments-comment-box [contenteditable="true"]',
    ];

    let editorFound = false;
    for (const sel of editorSelectors) {
      try {
        const el = page.locator(sel).first();
        if ((await el.count()) > 0) {
          await el.click({ timeout: 5000 });
          editorFound = true;
          console.error(`comment: found editor via: ${sel}`);
          break;
        }
      } catch (_) {}
    }

    if (!editorFound) {
      return { success: false, message: 'could not find comment editor' };
    }
    await sleep(randBetween(300, 600));

    // Type the comment with human-like delay
    console.error(`comment: typing ${commentText.length} chars...`);
    const typingDelay = commentText.length > 500 ? randBetween(10, 20) : randBetween(20, 45);
    await page.keyboard.type(commentText, { delay: typingDelay });
    await sleep(randBetween(1000, 2000));

    // Click the submit/post button for the comment
    console.error('comment: submitting...');
    const submitSelectors = [
      'button.comments-comment-box__submit-button',
      '.comments-comment-box button[type="submit"]',
      'button[aria-label="Post comment"]',
      'button[data-control-name="comment_submit"]',
      // fallback: any submit-looking button near the comment box
      '.comments-comment-box button.artdeco-button--primary',
    ];

    let submitted = false;
    for (const sel of submitSelectors) {
      try {
        const el = page.locator(sel).first();
        if ((await el.count()) > 0) {
          const disabled = await el.getAttribute('disabled');
          if (disabled) continue;
          await el.click({ timeout: 5000 });
          submitted = true;
          console.error(`comment: submitted via: ${sel}`);
          break;
        }
      } catch (_) {}
    }

    if (!submitted) {
      // Keyboard fallback
      console.error('comment: trying Ctrl+Enter...');
      const sendKey = process.platform === 'darwin' ? 'Meta+Enter' : 'Control+Enter';
      await page.keyboard.press(sendKey);
      submitted = true;
    }

    // Wait and verify
    await sleep(4000);
    const result = await page.evaluate((text) => {
      // Check if our comment appears in the comments section
      const comments = document.querySelectorAll(
        '.comments-comment-item, .comments-comment-entity'
      );
      const snippet = text.substring(0, 50);
      for (const c of comments) {
        if ((c.innerText || '').includes(snippet)) {
          return { found: true };
        }
      }
      // Check for error toasts
      const toasts = document.querySelectorAll('[role="alert"], .artdeco-toast-item');
      const toastText = Array.from(toasts).map(t => t.innerText.toLowerCase()).join(' ');
      if (toastText.includes('error') || toastText.includes('failed')) {
        return { found: false, error: toastText.substring(0, 100) };
      }
      // Check if comment box is now empty (content was submitted)
      const editors = document.querySelectorAll('.comments-comment-box [contenteditable="true"]');
      let editorEmpty = true;
      editors.forEach(e => {
        if ((e.innerText || '').trim().length > 10) editorEmpty = false;
      });
      return { found: false, editorEmpty };
    }, commentText);

    if (result.found) {
      return { success: true, message: 'comment posted and verified' };
    }
    if (result.error) {
      return { success: false, message: `error: ${result.error}` };
    }
    if (result.editorEmpty) {
      return { success: true, message: 'comment likely posted (editor cleared)' };
    }

    return { success: false, message: 'could not verify comment was posted' };
  } catch (error) {
    console.error(`comment error: ${error.message}`);
    return { success: false, message: error.message };
  } finally {
    try { if (page) await page.close(); } catch (_) {}
    try { if (browser) await browser.close(); } catch (_) {}
  }
}

// CLI entry
if (require.main === module) {
  const postUrl = process.argv[2];
  const commentText = process.argv[3];
  if (!postUrl || !commentText) {
    console.error('usage: node post_linkedin_comment.js <post_url> "comment text"');
    process.exit(1);
  }

  withCdpLock(() => postComment(postUrl, commentText), 180000)
    .then((result) => {
      console.log(JSON.stringify(result));
      process.exit(result.success ? 0 : 1);
    })
    .catch((err) => {
      console.log(JSON.stringify({ success: false, message: err.message }));
      process.exit(1);
    });
}

module.exports = { postComment };
