
const { chromium } = require('playwright');
const { withCdpLock } = require('/Users/boredfolio/.openclaw/agents/aria/workspace/scripts/cdp-lock');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('https://x.com/karpathy', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
  await page.waitForTimeout(2521);

  // find the first tweet's permalink
  const url = await page.evaluate(() => {
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    for (const art of articles) {
      // skip pinned tweets if possible -- they have a "Pinned" label
      const pinned = art.querySelector('[data-testid="socialContext"]');
      if (pinned && pinned.innerText.toLowerCase().includes('pinned')) continue;

      const links = art.querySelectorAll('a[href*="/status/"]');
      for (const a of links) {
        const href = a.getAttribute('href');
        if (href && href.match(/\/status\/\d+$/)) {
          return 'https://x.com' + href;
        }
      }
    }
    // fallback: just take the first article's status link
    const firstArt = document.querySelector('article[data-testid="tweet"]');
    if (firstArt) {
      const link = firstArt.querySelector('a[href*="/status/"]');
      if (link) return 'https://x.com' + link.getAttribute('href');
    }
    return null;
  });

  console.log(url || 'NONE');
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
