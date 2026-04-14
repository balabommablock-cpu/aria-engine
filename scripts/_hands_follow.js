
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:28800');
  const page = browser.contexts()[0].pages()[0];

  await page.goto('https://x.com/drjimfan', { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(3853);

  // check if already following
  const followBtn = await page.$('[data-testid$="-follow"]');
  if (!followBtn) {
    console.log('NO_FOLLOW_BTN');
    await browser.close();
    return;
  }
  const label = await followBtn.getAttribute('data-testid');
  if (label && label.includes('unfollow')) {
    console.log('ALREADY_FOLLOWING');
    await browser.close();
    return;
  }

  await followBtn.click();
  await page.waitForTimeout(1837);
  console.log('FOLLOWED');
  await browser.close();
})().catch(e => { console.error(e.message); process.exit(1); });
