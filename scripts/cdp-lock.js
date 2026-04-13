/**
 * cdp-lock.js -- simple CDP mutex using lockfile
 * Ensures only one CDP consumer runs at a time.
 */

const fs = require('fs');
const path = require('path');

const LOCK_FILE = path.join(
  process.env.ARIA_WORKSPACE || path.join(process.env.HOME, '.openclaw/agents/aria/workspace'),
  'memory', 'cdp.lock'
);

const POLL_INTERVAL = 500; // ms

async function withCdpLock(fn, timeoutMs = 120000) {
  const start = Date.now();

  // Try to acquire lock
  while (true) {
    try {
      // O_EXCL: fail if file already exists (atomic create)
      const fd = fs.openSync(LOCK_FILE, 'wx');
      fs.writeSync(fd, `${process.pid}\n${new Date().toISOString()}\n`);
      fs.closeSync(fd);
      break; // acquired
    } catch (e) {
      if (e.code === 'EEXIST') {
        // Lock held by someone else -- check if stale (>5 min)
        try {
          const stat = fs.statSync(LOCK_FILE);
          const age = Date.now() - stat.mtimeMs;
          if (age > 300000) {
            // Stale lock, break it
            fs.unlinkSync(LOCK_FILE);
            continue;
          }
        } catch (_) { /* file vanished, retry */ continue; }

        if (Date.now() - start > timeoutMs) {
          throw new Error(`cdp-lock: timeout after ${timeoutMs}ms waiting for lock`);
        }
        await new Promise(r => setTimeout(r, POLL_INTERVAL));
      } else {
        throw e;
      }
    }
  }

  // Run the function under lock
  try {
    return await fn();
  } finally {
    try { fs.unlinkSync(LOCK_FILE); } catch (_) {}
  }
}

module.exports = { withCdpLock };
