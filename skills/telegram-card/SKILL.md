---
name: telegram-card
description: Send a telegram card to rishabh via the `message` tool. Plain-text cards with clear instructions for rishabh to reply with 'approve', 'edit', or 'skip'. State updates use `bash` with inline python (no `edit` tool — it is unreliable on long JSON strings).
metadata:
  openclaw:
    emoji: "📨"
---

# telegram-card

the output surface. bad card emission = invisible slice. this skill uses exactly TWO tools: `message` (to send) and `bash` (for state). no `edit`. no `write`. no capitalized variants. those are the rules.

> **IMPORTANT — tool calls, not JSON in text.** when this skill says "invoke the `message` tool", actually invoke it. emitting a JSON block in your text response IS NOT A TOOL CALL — it is a bug that looks like a delivery but reaches nothing. the `message` tool exists. call it the same way you call `bash`.

## the two tools this skill uses (EXACTLY)

1. **`message`** — sends a message through a registered channel (telegram). lowercase name. fields: `action`, `channel`, `target`, `message`.
2. **`bash`** — runs a shell command. fields: `command`.

do not use: `edit`, `Edit`, `write`, `Write`, `read`, `Read`. use only `bash` for filesystem. use only `message` for telegram.

## inputs

```
{
  "card_type": 1 | 5,
  "payload": { /* card-type-specific */ },
  "source_id": "unique id for this card (same as target-inbox entry id for type 1)",
  "warning_banner": "optional text or null"
}
```

### payload for card_type = 1 (reply opportunity)

```
{
  "original": "tweet text",
  "author_handle": "@someone",
  "author_context": "who they are",
  "draft": "rishabh-voiced reply",
  "hook_pattern": "precision-humor",
  "format": "reply",
  "emotional_truth": 1
}
```

## what to do

execute immediately. linear steps. use exactly the tool names shown. do not invent variations.

### step 1 — load telegram routing (bash + cat)

invoke `bash` with this command:

```
cat /Users/boredfolio/.openclaw/agents/aria/workspace/memory/telegram-routing.json
```

parse the output. extract `rishabh_chat_id`. hold it as `CHAT_ID` for the rest of this turn.

**if the file is missing or the chat id is empty:** return `{status: "error", details: "telegram-routing.json missing or empty"}` and stop. do NOT send to any other chat.

### step 2 — duplicate check (bash + grep)

invoke `bash` with this command (substitute `<source_id>` with the actual id):

```
grep -l '"source_id":"<source_id>"' /Users/boredfolio/.openclaw/agents/aria/workspace/memory/pending-cards.jsonl 2>/dev/null && echo "ALREADY" || echo "NEW"
```

if output is `ALREADY`: return `{status: "already-sent", source_id: "<source_id>"}` and stop. do not send a duplicate card.

### step 3 — build the card body text

match the §9 in-person register: lowercase, dry, warm. no headers, no markdown. use literal newlines (actual \n characters).

#### for card_type = 1 (reply opportunity)

```
new reply opp from {author_handle} ({author_context}).

they said:
> {original}

draft:
{draft}

hook: {hook_pattern}. triggers truth #{emotional_truth}.

reply to this message with one of:
  approve   — log the draft, i'll send you the raw text to copy-paste
  edit      — send your edit as the next message
  skip      — drop this one and move on
```

if `warning_banner` is not null, prepend this (note the trailing blank line):

```
⚠️ {warning_banner}

```

#### for card_type = 5 (daily angles)

```
morning angles. pick one.

1. {angles[0].opener}
    {angles[0].body}
    ({angles[0].hook_pattern}, truth #{angles[0].emotional_truth})

2. {angles[1].opener}
    {angles[1].body}
    ({angles[1].hook_pattern}, truth #{angles[1].emotional_truth})

[... up to 5 ...]

reply with:
  pick 1 / pick 2 / pick 3 / pick 4 / pick 5   — log the chosen angle
  skip                                         — skip today
```

### step 4 — INVOKE THE `message` TOOL (native tool call, not text output)

this is the step everything hinges on. make a native tool call to `message` with EXACTLY these four parameters:

- `action`: `"send"` (string)
- `channel`: `"telegram"` (string)
- `target`: the CHAT_ID string from step 1
- `message`: the body text from step 3

**this is a function call.** invoke `message` the same way you invoke `bash`. the model runtime dispatches the call to openclaw's telegram channel. do NOT write the parameters out as a markdown code block — that is a failure mode, not a send.

reference: the bundled `gh-issues` skill uses the exact same invocation pattern to send telegram notifications. it works.

### step 5 — append to pending-cards.jsonl (bash + python inline)

after the `message` tool call returns successfully, append a row to `memory/pending-cards.jsonl` via `bash`:

```
python3 -c "
import json, datetime
row = {
  'source_id': '<source_id>',
  'card_type': <card_type>,
  'payload': <payload dict as python>,
  'sent_at': datetime.datetime.now().isoformat(),
  'status': 'sent'
}
with open('/Users/boredfolio/.openclaw/agents/aria/workspace/memory/pending-cards.jsonl', 'a') as f:
    f.write(json.dumps(row, ensure_ascii=False) + '\n')
print('ok')
"
```

substitute the actual payload. use `ensure_ascii=False` to preserve unicode.

**why inline python and not edit?** the `edit` tool is pattern-match-replace and gemma4:26b can't reproduce long JSON strings exactly (it introduces typos). python `open('a')` appends cleanly without touching existing data.

### step 6 — update target-inbox.jsonl status (card_type 1 only, bash + python inline)

invoke `bash`:

```
python3 -c "
import json
path = '/Users/boredfolio/.openclaw/agents/aria/workspace/memory/target-inbox.jsonl'
lines = open(path).read().splitlines()
out = []
for line in lines:
    if not line.strip():
        continue
    try:
        d = json.loads(line)
        if d.get('id') == '<source_id>':
            d['status'] = 'pending-approval'
        out.append(json.dumps(d, ensure_ascii=False))
    except:
        out.append(line)
open(path, 'w').write('\n'.join(out) + '\n')
print('ok')
"
```

this script rewrites the file line-by-line preserving every field exactly. the only mutation is the single status field on the matching entry.

for card_type 5, there is no target-inbox entry — skip this step entirely.

### step 7 — return

return a small summary as text (not a tool call):

```json
{"status": "SENT", "source_id": "<source_id>", "details": "card delivered to telegram"}
```

## rules for telegram-card itself

- **never use `edit` or `Edit`.** use only `bash` for filesystem operations.
- **never use `Read` / `Write` (capitalized).** use only `bash` with `cat`, `python3`, etc.
- **never write the `message` tool call as JSON in text.** invoke it as a real tool call.
- **never send to a chat id that isn't in `memory/telegram-routing.json`.**
- **never send the same source_id twice** (step 2 guards this).
- **never include markdown tables or formatting in the card body.**
- **never include buttons this milestone.** plain text only.
