# ARIA Dashboard Session Context

Paste this into a new Claude Code chat to continue dashboard work.

---

## DASHBOARD

Live at **http://localhost:8421** when running.

### To Start:

```bash
cd ~/.openclaw/agents/aria/workspace/dashboard && python3 khud-dashboard.py
```

### Architecture:

- Flask app, single file: `workspace/dashboard/khud-dashboard.py`
- Port: 8421
- Two tabs: X (Twitter) and LinkedIn
- Auto-refreshes every 30 seconds
- Reads from `workspace/memory/aria.db` (read-only, never mutates)

### What It Shows:

**X Tab -- Left Side (Live Activity):**
- Service status (brain, hands, khud, watchdog, retro)
- Stats: actions today/cap, tweets posted, replies landed, failed, queue depth, follows
- Territory distribution (7d)
- All tweets posted today (with territory, hook pattern, score, image type)
- All replies landed today (with target, text, tweet URL)
- Failed replies
- Queue (next tweets to post)
- Recent errors

**X Tab -- Right Side (Decisions):**
- Khud's current guidance (tweet direction, reply direction, priority targets)
- Learned knowledge (permanent semantic memory with confidence scores)
- Khud reflections (observations, patterns, experiments, taste)
- Decision ledger (every action with before-state, decision, outcome)
- Activity log (substantive engine events)

**LinkedIn Tab:**
- Same layout but for LinkedIn tables
- Currently shows "not active yet" since LinkedIn Khud hasn't been scheduled

### API Endpoints:

- `GET /` -- main dashboard HTML
- `GET /api/x` -- full X state as JSON
- `GET /api/linkedin` -- full LinkedIn state as JSON

### Key Files:

- Dashboard: `~/.openclaw/agents/aria/workspace/dashboard/khud-dashboard.py`
- Old dashboard (separate): `workspace/dashboard/server.py` on port 28889
- DB: `workspace/memory/aria.db`

### Decision Ledger Table Schema:

```sql
decision_ledger (
    id INTEGER PRIMARY KEY,
    ts TEXT,
    actor TEXT,          -- hands, brain, khud_x, khud_li
    decision_type TEXT,  -- post_tweet, post_reply, reflect:pattern, learn, etc.
    before_state TEXT,   -- JSON: what the system saw when deciding
    decision TEXT,       -- what was decided and why
    outcome TEXT,        -- what happened
    performance TEXT,    -- metrics (views, likes) when available
    course_correction TEXT -- what changed as a result
)
```

Currently wired into: hands.py (post_tweet, post_reply, failures) and khud-x.py (reflect, generate, learn).

### Styling:

- Dark theme (#0d0d0d background)
- Sage green accent (#6B8F71) matching ARIA's quote card style
- Cards with subtle borders
- Color-coded tags: green (territory), blue (hook pattern), orange (image type), red (errors)
- Decision entries have left green border
- Reflections have left orange border
- Knowledge cards have green-tinted background
