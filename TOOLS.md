# TOOLS.md — aria local notes

## telegram

- **rishabh_chat_id:** `7353580848` (single source of truth: `memory/telegram-routing.json`)
- **bot:** openclaw-managed, polling mode, token in `~/.openclaw/openclaw.json`
- **my routing:** all telegram DMs → aria (broad binding; bot is private to rishabh per setup conversation)

## llm

- **model:** ollama/gemma4:31b (local, free, 262k context window, no cost per token)
- **thinking level:** `medium` for content generation and program execution. `low` only for boot checks.
- **fallback:** none this milestone. no anthropic/openai key.

## rishabh's public handles (populate as discovered)

- **x:** _to be populated — ask rishabh_
- **linkedin:** _to be populated — ask rishabh_
- **github:** _to be populated — ask rishabh_
- **boredfolio:** boredfolio.com (the mutual fund platform)

## workspace paths that matter

- `memory/voice-rules.md` — the single most important file. `skills/voice-check` loads it on every draft.
- `memory/target-inbox.jsonl` — incoming reply opportunities (hand-seeded until real x api).
- `memory/approved-drafts.jsonl` — drafts rishabh approved. best few-shot calibration signal the content-engine has.
- `memory/pending-cards.jsonl` — cards in-flight, keyed by source_id. callback handlers read this.
- `memory/telegram-routing.json` — chat id + bot metadata. every skill that sends to telegram loads this first.

## things to remember

- **skills are markdown files.** the LLM reads them and executes them. no python runtime this milestone.
- **cron sessions are isolated.** every run is a fresh session. state crosses runs only via the filesystem (jsonl files). every skill must be idempotent.
- **DM sessions are persistent** (per-channel-peer). button callbacks from telegram cards arrive in the persistent DM session, not in cron sessions. this is how the APPROVE/EDIT/SKIP flow works.
