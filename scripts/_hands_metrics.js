
const { chromium } = require('playwright');
const { withCdpLock } = require('/Users/boredfolio/.openclaw/agents/aria/workspace/scripts/cdp-lock');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];

  // navigate to own profile
  await page.goto('https://x.com/BalabommaRao', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
  await page.waitForTimeout(3000);

  // scrape visible tweet metrics
  const metrics = await page.evaluate(() => {
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    const results = [];
    for (const art of articles) {
      const textEl = art.querySelector('[data-testid="tweetText"]');
      const text = textEl ? textEl.innerText.trim().slice(0, 80) : '';

      // extract analytics row: views, replies, retweets, likes, bookmarks
      const groups = art.querySelectorAll('[role="group"] button');
      let replies = 0, retweets = 0, likes = 0, bookmarks = 0, views = 0;
      for (const btn of groups) {
        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
        const match = label.match(/(\d[\d,]*)\s+(repl|retweet|like|bookmark|view)/);
        if (match) {
          const num = parseInt(match[1].replace(/,/g, ''), 10) || 0;
          if (label.includes('repl')) replies = num;
          else if (label.includes('retweet')) retweets = num;
          else if (label.includes('like')) likes = num;
          else if (label.includes('bookmark')) bookmarks = num;
          else if (label.includes('view')) views = num;
        }
      }
      // also try the analytics link for views
      const analyticsLink = art.querySelector('a[href*="/analytics"]');
      if (analyticsLink && views === 0) {
        const vMatch = (analyticsLink.getAttribute('aria-label') || '').match(/(\d[\d,]*)\s*view/i);
        if (vMatch) views = parseInt(vMatch[1].replace(/,/g, ''), 10) || 0;
      }

      results.push({ text, replies, retweets, likes, bookmarks, views });
    }
    return results;
  });

  console.log(JSON.stringify(metrics));
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
