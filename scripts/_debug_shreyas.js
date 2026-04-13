const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];
  console.log('current url:', page.url());
  await page.goto('https://x.com/shreyas', { waitUntil: 'domcontentloaded', timeout: 30000 });
  console.log('navigated to shreyas');
  await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
  console.log('found articles');
  await page.waitForTimeout(2000);
  const debug = await page.evaluate(() => {
    const arts = document.querySelectorAll('article[data-testid="tweet"]');
    const results = [];
    for (let i = 0; i < Math.min(3, arts.length); i++) {
      const art = arts[i];
      const links = art.querySelectorAll('a[role="link"]');
      const hrefs = [];
      for (const l of links) hrefs.push(l.getAttribute('href'));
      const timeEl = art.querySelector('time[datetime]');
      const text = art.querySelector('[data-testid="tweetText"]');
      results.push({
        hrefs: hrefs.filter(h => h && h.startsWith('/')),
        time: timeEl ? timeEl.getAttribute('datetime') : null,
        text: text ? text.innerText.substring(0, 60) : 'no-text'
      });
    }
    return JSON.stringify(results, null, 2);
  });
  console.log(debug);
  await browser.close();
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
