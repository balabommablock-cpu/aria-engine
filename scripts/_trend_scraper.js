
const CDP = require('chrome-remote-interface');
(async () => {
    const client = await CDP({port: 28800});
    const {Page, Runtime} = client;
    await Page.enable();
    await Page.navigate({url: 'https://x.com/explore/tabs/trending'});
    await new Promise(r => setTimeout(r, 5000));
    const result = await Runtime.evaluate({
        expression: `
            Array.from(document.querySelectorAll('[data-testid="trend"]')).slice(0, 10).map(el => {
                const spans = el.querySelectorAll('span');
                const texts = Array.from(spans).map(s => s.textContent).filter(t => t.length > 2);
                return texts.join(' | ');
            })
        `,
        returnByValue: true
    });
    console.log(JSON.stringify(result.result.value || []));
    await client.close();
})().catch(e => { console.error(e.message); process.exit(1); });
