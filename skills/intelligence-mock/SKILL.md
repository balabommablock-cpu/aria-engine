---
name: intelligence-mock
description: Read memory/target-inbox.jsonl and return one unclaimed reply opportunity. Placeholder for real X API stream 1. Uses `bash` with inline python for all file operations — no `edit` or `Edit` tool (those are unreliable on long JSON strings in gemma4:26b).
metadata:
  openclaw:
    emoji: "📡"
---

# intelligence-mock

the stand-in for real X api intelligence until credentials arrive. reads `memory/target-inbox.jsonl`, picks one `status:"new"` entry, marks it `processing`, returns it. no creativity required — this is a state machine.

## the one tool this skill uses

**`bash`** — for everything. reads, parses, mutations, all via `bash` with inline `python3 -c "..."`. do not use `edit`, `Edit`, `read`, `Read`, `write`, `Write`. use only `bash`.

## inputs

none. operates entirely on filesystem state.

## what to do

execute immediately. one bash call does all the work.

### step 1 — pick + mark + return in one atomic script

invoke `bash` with this command (literally, do not modify):

```
python3 -c "
import json, datetime, sys
path = '/Users/boredfolio/.openclaw/agents/aria/workspace/memory/target-inbox.jsonl'
lines = open(path).read().splitlines()
entries = []
for i, line in enumerate(lines):
    if not line.strip():
        continue
    try:
        d = json.loads(line)
    except:
        continue
    if '_comment' in d or '_example' in d:
        continue
    if d.get('status') != 'new':
        continue
    entries.append((i, d))

if not entries:
    print(json.dumps({'status': 'empty', 'action': 'none', 'note': 'target-inbox has no new entries'}))
    sys.exit(0)

entries.sort(key=lambda x: x[1].get('timestamp',''))
idx, picked = entries[0]
picked['status'] = 'processing'
picked['picked_at'] = datetime.datetime.now().isoformat()

out = []
for i, line in enumerate(lines):
    if i == idx:
        out.append(json.dumps(picked, ensure_ascii=False))
    else:
        out.append(line)
open(path, 'w').write('\n'.join(out) + '\n')

result = {
    'status': 'PICKED',
    'entry': {
        'id': picked.get('id'),
        'timestamp': picked.get('timestamp'),
        'author_handle': picked.get('author_handle'),
        'author_context': picked.get('author_context'),
        'original': picked.get('original'),
        'url': picked.get('url')
    }
}
print(json.dumps(result, ensure_ascii=False))
"
```

this single script:
1. reads target-inbox.jsonl
2. filters to `status:"new"` entries (skips `_comment` / `_example`)
3. returns empty if nothing to pick
4. picks the oldest new entry by timestamp
5. marks it `processing` with a `picked_at` timestamp
6. rewrites the file preserving every other line byte-for-byte
7. prints the picked entry as JSON

### step 2 — parse the output

the bash tool returns the script's stdout. parse it as JSON.

- if `{"status": "empty", ...}`: return that to the caller. the reply-opportunity-pipeline program will exit silently (no card sent).
- if `{"status": "PICKED", "entry": {...}}`: return that to the caller. it passes `entry` to `content-engine` as the `target` parameter.

## rules for intelligence-mock itself

- **never use `edit` / `Edit` / `read` / `Read` / `write` / `Write`.** only `bash`.
- **never invent entries.** if the inbox is empty, the script returns `status: "empty"`. do not fabricate tweets.
- **never pick more than one entry per run.** the script picks exactly one.
- **always mark the picked entry `processing` before returning.** the script does this atomically.
- **never mutate other entries.** the script only touches the picked entry's `status` and `picked_at` fields.

## when this skill gets retired

once real X API credentials are available:
1. disable the `aria-reply-pipeline` cron
2. replace this skill with one that pulls from the X API
3. re-enable the cron

until then, this skill is the whole intelligence layer.
