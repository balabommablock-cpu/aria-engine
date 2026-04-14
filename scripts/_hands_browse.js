
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];
  const currentUrl = page.url();

  // only browse timeline if we're on x.com
  if (!currentUrl.includes('x.com')) {
    await browser.close();
    return;
  }

  // go to home timeline
  await page.goto('https://x.com/home', { waitUntil: 'domcontentloaded', timeout: 15000 });
  await page.waitForTimeout(2053);

  // scroll down naturally (humans don't just stare at the top)
  for (let i = 0; i < 4; i++) {
    await page.mouse.wheel(0, 611);
    await page.waitForTimeout(1696);
  }

  // maybe pause on a tweet (dwell time looks human)
  await page.waitForTimeout(1976);

  await browser.close();
})().catch(() => {});
