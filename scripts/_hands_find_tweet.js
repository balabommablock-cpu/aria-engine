
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('https://x.com/BalabommaRao', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
  await page.waitForTimeout(3754);

  const targetHandle = 'BalabommaRao'.toLowerCase();
  const result = await page.evaluate((handle) => {
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    const candidates = [];
    const MAX_CANDIDATES = 5;

    for (const art of articles) {
      if (candidates.length >= MAX_CANDIDATES) break;

      // skip pinned tweets
      const pinned = art.querySelector('[data-testid="socialContext"]');
      if (pinned && pinned.innerText.toLowerCase().includes('pinned')) continue;

      // skip retweets (socialContext says "X reposted")
      if (pinned && pinned.innerText.toLowerCase().includes('reposted')) continue;

      // verify this tweet is actually by the target author
      const authorLinks = art.querySelectorAll('a[role="link"]');
      let isAuthor = false;
      for (const link of authorLinks) {
        const href = (link.getAttribute('href') || '').toLowerCase();
        if (href === '/' + handle) {
          isAuthor = true;
          break;
        }
      }
      if (!isAuthor) continue;

      // check tweet freshness (skip tweets older than 48 hours)
      const timeEl = art.querySelector('time[datetime]');
      if (timeEl) {
        const tweetDate = new Date(timeEl.getAttribute('datetime'));
        const ageHours = (Date.now() - tweetDate.getTime()) / (1000 * 60 * 60);
        if (ageHours > 720) continue;  // 30 days: high-value targets may tweet infrequently
      }

      // skip subscription-locked tweets (buttons disabled, "Subscribe to unlock")
      const artText = art.innerText || '';
      if (artText.includes('Subscribe to unlock')) continue;
      const replyBtn = art.querySelector('button[data-testid="reply"]');
      if (replyBtn && (replyBtn.disabled || replyBtn.getAttribute('aria-disabled') === 'true')) continue;

      // get the status link and tweet text
      const links = art.querySelectorAll('a[href*="/status/"]');
      for (const a of links) {
        const href = a.getAttribute('href');
        if (href && href.match(/\/status\/\d+$/) && href.toLowerCase().includes('/' + handle + '/')) {
          const textEl = art.querySelector('[data-testid="tweetText"]');
          const text = textEl ? textEl.innerText : '';

          // extract like count from engagement group buttons
          let likes = 0;
          const groupBtns = art.querySelectorAll('[role="group"] button');
          for (const btn of groupBtns) {
            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
            const likeMatch = label.match(/(\d+)\s*like/);
            if (likeMatch) {
              likes = parseInt(likeMatch[1], 10);
              break;
            }
          }

          candidates.push({
            url: 'https://x.com' + href,
            text: text.substring(0, 500),
            likes: likes
          });
          break;  // found the status link for this article, move to next article
        }
      }
    }

    if (candidates.length === 0) return 'null';

    // pick the tweet with the highest like count; fall back to first if all have 0 likes
    const hasEngagement = candidates.some(c => c.likes > 0);
    let best;
    if (hasEngagement) {
      best = candidates.reduce((a, b) => (b.likes > a.likes ? b : a));
    } else {
      best = candidates[0];
    }
    return JSON.stringify({ url: best.url, text: best.text });
  }, targetHandle);

  if (result === 'null') {
    // second pass: if no author-matched tweet found, log it and return null
    // (better to skip than reply to wrong person's tweet)
    console.log('null');
  } else {
    console.log(result);
  }
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
