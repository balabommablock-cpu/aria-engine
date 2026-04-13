
const { chromium } = require('playwright');
const { withCdpLock } = require('/Users/boredfolio/.openclaw/agents/aria/workspace/scripts/cdp-lock');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('https://x.com/shreyas/status/2039760142980514275', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
  await page.waitForTimeout(3595);

  // find the like button on the main tweet (first article)
  const article = page.locator('article[data-testid="tweet"]').first();
  const likeBtn = article.locator('button[data-testid="like"]');
  const count = await likeBtn.count();
  if (count === 0) {
    // already liked (button becomes "unlike")
    console.log('already liked or button not found');
    await browser.close();
    process.exit(0);
  }
  await likeBtn.click({ timeout: 5000 });
  console.log('liked');
  await page.waitForTimeout(1500);
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
