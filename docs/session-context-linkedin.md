# ARIA LinkedIn Session Context

Paste this into a new Claude Code chat to continue LinkedIn work with full context.

---

## SYSTEM OVERVIEW

LinkedIn is the SECOND platform for ARIA. Same voice as X, different format. The LinkedIn Khud brain script exists and is wired but NOT activated yet. Rishabh will activate when ready.

### Architecture:

| Component | Path | Status |
|-----------|------|--------|
| Brain script | `~/.openclaw/agents/aria/workspace/scripts/aria-khud-li.py` | built, tested, NOT scheduled |
| Plist | `~/Library/LaunchAgents/com.aria.khud-li.plist` | exists, NOT loaded |
| Memory system | `aria-memory.py` (shared with X) | tables created (memory_episodic_li, memory_semantic_li, memory_procedural_li) |
| DB tables | reflections_li, khud_actions_li, linkedin_queue, linkedin_posted | created, empty |
| Dashboard | `workspace/dashboard/khud-dashboard.py` port 8421, /linkedin tab | live |

### Key Paths:

- Workspace: `/Users/boredfolio/.openclaw/agents/aria/workspace/`
- Scripts: `workspace/scripts/`
- DB: `workspace/memory/aria.db` (shared with X, separate tables)
- Voice config: `workspace/voice.json` (shared)
- Replication prompt: `workspace/docs/claude-khud-linkedin-replication-prompt.md`

### To Activate LinkedIn Khud:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aria.khud-li.plist
```

### To Test Without Activating:

```bash
cd ~/.openclaw/agents/aria/workspace/scripts
python3 -c "
import sys; sys.path.insert(0, '.')
import importlib
khud = importlib.import_module('aria-khud-li')
# dry context test (no Claude call):
db = khud.get_db()
khud.init_db()
khud.init_khud_li_tables(db)
ctx = khud.gather_context(db)
prompt = khud.build_brain_prompt(ctx)
print(f'Prompt length: {len(prompt)} chars')
print(prompt[:500])
db.close()
"
```

## THE VOICE (same as X, adapted for LinkedIn)

Handle: @BalabommaRao (Rishabh)
Same golden tweets. Same territories. Same bans. Same red lines.

### LinkedIn-specific format rules:

- Up to 3000 characters. Use the space.
- Same dry observations but with BREATHING ROOM.
- Light storytelling. Concrete examples from building, product, organizations.
- A compressed X tweet can EXPAND into a LinkedIn post with one specific story behind it.
- Opening line matters. It's what shows before "see more." Make it earn the click.
- NO hashtags. NO "thoughts?" NO engagement bait. NO "let me know in the comments."
- NO em dashes. NO hyphens as formatting devices.
- Professional but not corporate. Builder-with-taste voice.
- Natural case.

## WHAT LINKEDIN KHUD CAN DO

Actions: reflect, generate_posts, adjust, investigate, experiment, learn, codify_skill

It sees:
- LinkedIn posts published (last 24h)
- LinkedIn queue state
- Territory distribution (7d)
- X posts from last 7d (for cross-platform adaptation ideas)
- Its own reflections and deep memory
- Current time

It has its OWN memory (separate from X):
- memory_episodic_li (observations)
- memory_semantic_li (confirmed knowledge)
- memory_procedural_li (codified skills)

## WHAT STILL NEEDS BUILDING

1. **LinkedIn posting mechanism** -- aria-khud-li.py generates content guidance, but there's no hands process for LinkedIn yet. Need a LinkedIn poster (Playwright CDP or API).
2. **LinkedIn scraper** -- to read target accounts' LinkedIn posts for reply/engagement context.
3. **LinkedIn reply system** -- different from X. Longer, more considered replies.
4. **Content generation** -- brain.py only generates X tweets. Need a LinkedIn content generator that takes Khud's guidance and produces 3000-char posts.
5. **Cross-platform adaptation** -- Khud can see X posts, but the actual "expand X tweet into LinkedIn post" logic doesn't exist yet.

## CURRENT STATE

- LinkedIn: nothing posted. Queue empty. Brain is fresh.
- All tables created and ready.
- Memory system wired with embeddings (nomic-embed-text via Ollama, local, zero cost).
- Dashboard shows LinkedIn tab at http://localhost:8421

## IMPORTANT USER PREFERENCES

- Same as X: Rishabh approves, Claude executes everything.
- Ship don't plan. Act decisively.
- No flaunting metrics/titles. Voice is about what he thinks, not what he's done.
- NO em dashes. Natural case. Direct, dry.
- Images and growth hacks required.
