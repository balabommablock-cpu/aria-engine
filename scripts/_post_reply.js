
const CDP = require('chrome-remote-interface');
(async () => {
    const client = await CDP({port: 28800});
    const {Page, Runtime} = client;
    await Page.enable();

    await Page.navigate({url: 'https://x.com/BalabommaRao'});
    await new Promise(r => setTimeout(r, 8826));

    // Scroll like a human
    await Runtime.evaluate({expression: 'window.scrollBy(0, 200)'});
    await new Promise(r => setTimeout(r, 1432));

    const clicked = await Runtime.evaluate({
        expression: `(function() {
            const replyBtn = document.querySelector('[data-testid="reply"]');
            if (replyBtn) { replyBtn.click(); return true; }
            return false;
        })()`
        , returnByValue: true
    });

    if (!clicked.result.value) {
        console.error('reply button not found');
        process.exit(1);
    }

    await new Promise(r => setTimeout(r, 2355));

    const replyText = "the real product of most organizations is the appearance of coordination.";
    await Runtime.evaluate({
        expression: `(function() {
            const editor = document.querySelector('[data-testid="tweetTextarea_0"]');
            if (editor) {
                editor.focus();
                document.execCommand('insertText', false, ${JSON.stringify(replyText)});
                return true;
            }
            return false;
        })()`
        , returnByValue: true
    });

    await new Promise(r => setTimeout(r, 1427));

    await Runtime.evaluate({
        expression: `(function() {
            const btns = document.querySelectorAll('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]');
            for (const btn of btns) {
                if (!btn.disabled) { btn.click(); return true; }
            }
            return false;
        })()`
        , returnByValue: true
    });

    await new Promise(r => setTimeout(r, 3000));
    console.log('reply posted');
    await client.close();
})().catch(e => { console.error(e.message); process.exit(1); });
