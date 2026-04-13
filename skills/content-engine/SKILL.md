---
name: content-engine
description: Draft a rishabh-voiced reply or original post given a target and context. Loops through voice-check up to 2 times before giving up. The creative core of aria. Every public artifact originates here.
metadata:
  openclaw:
    emoji: "✍️"
---

# content-engine

the creative core. if the draft this skill produces fails the §18 single test, nothing else matters — debug here first.

## inputs

```
{
  "task": "reply" | "original",
  "target": {
    "original": "the tweet or post being replied to (only for task=reply)",
    "author_handle": "@someone (optional)",
    "author_context": "brief description of who they are and why they matter (optional)",
    "url": "link to the original (optional)"
  },
  "topic": "the topic or angle for original posts (only for task=original)",
  "few_shot_override": "optional: explicit examples to use instead of approved-drafts.jsonl"
}
```

## what to do

execute immediately. do not discuss your approach. do not produce a plan. draft, verify, possibly redraft, emit.

### step 1 — load the voice context

read these files in order:
1. `memory/aria-identity.md` — the identity declaration: what world he's building, what's the proof, through-line, humor, and commitment. this is the north star for WHAT to write about.
2. `memory/personality-core.md` — who rishabh is
3. `memory/voice-rules.md` — the banned-words list and trait/humor rules (this overlaps with what voice-check will load, but you need it here to steer generation, not just validation)
4. `memory/constellation-weights.md` — tonal calibration (which 2–3 influences to lean on)
5. `memory/belief-system.md` — his actual beliefs (don't invent opinions)
6. `memory/projection-rules.md` — show don't tell, link don't describe
7. `memory/framework-naming.md` — if the draft involves a new concept, check the naming protocol
8. `memory/career-context.md` — if the draft references insider knowledge, check what he can and can't claim to know
9. `memory/emotional-truths.md` — the three truths, one of which must fire

### step 2 — load the calibration examples

read `memory/approved-drafts.jsonl`. take the last 20 entries (or all of them if fewer than 20). these are the strongest available signal for "what rishabh actually approves." treat them as few-shot training examples.

if the file is empty (fresh install, no approvals yet), read `memory/humor-calibration.md` as the fallback calibration.

### step 3 — draft

#### if task == "reply":
- 1–3 sentences. no more.
- must contain a specific claim backed by receipt (experience / data / structural observation).
- must use at least one humor mode (structural / precision / scale / deadpan / self-aware escalation / turn / quiet bomb / compression).
- one insider angle if possible. use `memory/career-context.md` for what counts as insider.
- **never agree fully.** per §conversation-depth, agreement closes the loop. productive tension keeps it open. even when rishabh would basically agree, the draft should add a distinct angle or gentle pushback, not a "great point!" reply.
- **never end with a closed statement.** leave it open — a specific observation, a reframe, a question that invites further response.

#### if task == "original":
- 2–5 sentences. aim for distilled.
- pick a hook pattern explicitly and tag it in the output: `cold-open-with-number`, `contrarian-signal`, `insider-reveal`, `pattern-interrupt`, `tension`, or a new pattern that earns its own tag.
- first 100–140 chars should be a standalone hook (it's what shows in the notification preview).
- must pass the §18 test: sound like something rishabh was going to think anyway.

### step 4 — self-check before validation

before calling voice-check, check your own draft against these top 5 mistakes aria tends to make:

1. does it contain "delve", "nuanced", "landscape", or any other §16.3 banned word? if yes, rewrite.
2. does it start with "so,", "here's the thing", "i've been thinking", or any other banned opener? if yes, rewrite.
3. does it have an em-dash or semicolon? if yes, replace with periods.
4. does it have a parallel triad ("not just X, but Y and Z")? if yes, restructure.
5. does it end with "think about that" / "let that sink in" / similar? if yes, cut.

if you had to rewrite any, your internal draft is now attempt 1. if you rewrote nothing, proceed.

### step 5 — call voice-check

call `skills/voice-check` with `{draft: <your draft>, task: <task>, source_context: <target or topic>}`.

receive `{verdict, failures, notes}`.

- **if verdict == "PASS"** → go to step 7.
- **if verdict == "FAIL"** → go to step 6 for exactly one redraft attempt.

### step 6 — redraft (only once)

read the failures list. for each failure, apply the specific fix:
- banned word → replace with a rishabh-native synonym or restructure the sentence
- banned opener → cut the first 3 words, start from the real sentence
- banned ending → cut the last sentence or rewrite it to leave a thread open
- em-dash → period
- semicolon → period, split into two sentences
- parallel triad → cut one of the three items or restructure
- no receipt → add a specific claim from rishabh's career-context
- no emotional truth → decide which of the 3 truths to target, rewrite to hit it
- no humor mode → add one structural / precision / scale / deadpan / turn move
- fails §18 → this is the hardest to fix — rewrite from scratch with the note as guidance, or give up

call voice-check again with the redrafted version.

- **if verdict == "PASS"** → go to step 7.
- **if verdict == "FAIL"** → go to step 8 (give up). do NOT attempt a third redraft.

### step 7 — emit PASS

return:

```json
{
  "status": "PASS",
  "draft": "<the final approved draft>",
  "hook_pattern": "<which hook pattern was used>",
  "format": "observation" | "teardown" | "thread-opener" | "reply",
  "emotional_truth": 1 | 2 | 3,
  "attempts": [
    {"attempt": 1, "draft": "...", "voice_check": {...}},
    {"attempt": 2, "draft": "...", "voice_check": {...}}
  ]
}
```

### step 8 — emit gave-up

return:

```json
{
  "status": "gave-up",
  "reason": "voice-check failed twice — see attempts",
  "attempts": [
    {"attempt": 1, "draft": "...", "voice_check": {...}},
    {"attempt": 2, "draft": "...", "voice_check": {...}}
  ]
}
```

**never emit a third-attempt draft.** if two attempts failed, the topic or target isn't a good fit and forcing it will produce content that fails §18 worse than giving up. the caller (the `reply-opportunity-pipeline` program) will mark the entry `skipped` and move on.

## rules for content-engine itself

- **never skip the few-shot loading step.** approved-drafts.jsonl is the single best signal available and it gets better every time rishabh approves a card.
- **never invent a receipt.** if rishabh doesn't actually have the experience/data/observation, the draft can't claim it. fabricated receipts are a hard-fail of trait 01.
- **never ship a third-attempt draft.** two attempts is the ceiling. the gave-up path exists for a reason.
- **never reply with full agreement.** even if the target is 100% correct, the reply must open a new angle or close the loop.
- **never end on a closed statement.** open loops keep conversations going (personality §conversation-depth).
