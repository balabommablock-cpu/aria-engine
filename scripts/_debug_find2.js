const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const contexts = browser.contexts();
  console.log('contexts:', contexts.length);
  if (contexts.length === 0) {
    console.log('NO CONTEXTS');
    process.exit(1);
  }
  const page = contexts[0].pages()[0];
  console.log('page url:', page.url());
  await page.goto('https://x.com/simonw', { waitUntil: 'domcontentloaded', timeout: 30000 });
  console.log('navigated');
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
  console.log('found articles');
  await page.waitForTimeout(2000);
  
  const count = await page.evaluate(() => document.querySelectorAll('article[data-testid="tweet"]').length);
  console.log('article count:', count);
  
  // check author hrefs in first article
  const debug = await page.evaluate(() => {
    const art = document.querySelectorAll('article[data-testid="tweet"]')[0];
    if (!art) return 'no article';
    const links = art.querySelectorAll('a[role="link"]');
    const hrefs = [];
    for (const l of links) hrefs.push(l.getAttribute('href'));
    const timeEl = art.querySelector('time[datetime]');
    const statusLinks = art.querySelectorAll('a[href*="/status/"]');
    const statuses = [];
    for (const s of statusLinks) statuses.push(s.getAttribute('href'));
    return JSON.stringify({hrefs, time: timeEl ? timeEl.getAttribute('datetime') : null, statuses});
  });
  console.log('first article:', debug);
  
  await browser.close();
})().catch(e => { console.error('ERROR:', e.message); process.exit(1); });
