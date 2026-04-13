const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];
  await page.goto('https://x.com/simonw', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
  await page.waitForTimeout(3000);

  const result = await page.evaluate(() => {
    const articles = document.querySelectorAll('article[data-testid="tweet"]');
    const data = [];
    for (const art of articles) {
      const pinned = art.querySelector('[data-testid="socialContext"]');
      const pinnedText = pinned ? pinned.innerText : '';
      const authorLinks = art.querySelectorAll('a[role="link"]');
      const hrefs = [];
      for (const link of authorLinks) hrefs.push(link.getAttribute('href'));
      const timeEl = art.querySelector('time[datetime]');
      const dt = timeEl ? timeEl.getAttribute('datetime') : 'no-time';
      const textEl = art.querySelector('[data-testid="tweetText"]');
      const text = textEl ? textEl.innerText.substring(0, 80) : 'no-text';
      const statusLinks = art.querySelectorAll('a[href*="/status/"]');
      const statuses = [];
      for (const s of statusLinks) statuses.push(s.getAttribute('href'));
      data.push({pinnedText, hrefs: hrefs.slice(0, 5), datetime: dt, text, statuses: statuses.slice(0, 3)});
    }
    return JSON.stringify(data.slice(0, 5), null, 2);
  });
  console.log(result);
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
