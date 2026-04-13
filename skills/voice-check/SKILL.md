---
name: voice-check
description: Validate a draft against rishabh's voice rules. Returns PASS or FAIL with specific diagnoses. The final gate before any draft reaches telegram. Every content-producing skill calls this before emitting a draft. Stateless — caller decides what to do with the verdict.
metadata:
  openclaw:
    emoji: "🔍"
---

# voice-check

the single most load-bearing skill in the entire workspace. if this skill is wrong, every draft is wrong.

## inputs

```
{
  "draft": "the text to validate",
  "task": "reply" | "original",
  "source_context": "optional — the target post or the topic the draft is about"
}
```

## what to do

execute immediately. do not discuss the approach. follow these steps in order.

### step 1 — load the rules

read these files in order:
1. `memory/voice-rules.md` — the single most important file. contains §3 traits, §4 humor modes, §16 banned-words and punctuation rules.
2. `memory/aria-identity.md` — the identity declaration. use this to verify drafts are on-identity (disproportionate builder, range as compulsion, structural humor of proportion) not just on-voice.
3. `memory/emotional-truths.md` — §17. the hard pass/fail gate.
4. `memory/humor-calibration.md` — §4 example quotes for few-shot calibration.

### step 2 — hard fails first (literal-string checks)

these are **auto-fails**. if any of them hit, return FAIL immediately with the specific rule in `failures`.

1. **banned words:** scan the draft for literal occurrences of any word in the §16.3 banned-words list. case-insensitive. if any match → auto-fail with `failures: ["banned-word: <word>"]`.
2. **banned openers:** if the first 3 words of the draft match any banned-opener from §16.3 → auto-fail with `failures: ["banned-opener: <opener>"]`.
3. **banned transitions:** scan for any §16.3 banned-transition anywhere in the draft → auto-fail with `failures: ["banned-transition: <transition>"]`.
4. **banned endings:** if the last sentence matches any §16.3 banned-ending → auto-fail with `failures: ["banned-ending: <ending>"]`.
5. **banned phrases:** scan for "game-changer", "paradigm shift", "at the end of the day", "the reality is", "in today's", and any other §16.3 phrase → auto-fail.
6. **punctuation crimes (§16.1):**
   - contains an em-dash (`—`) → auto-fail with `failures: ["em-dash present"]`.
   - contains a semicolon (`;`) → auto-fail with `failures: ["semicolon present"]`.
   - a sentence has 3+ commas → auto-fail with `failures: ["sentence has 3+ commas"]`.
   - contains a colon-heavy setup like "X: Y" where Y restates X → soft flag, add to notes but don't auto-fail.

if any hard-fail hits, STOP here. return the FAIL verdict. do not proceed to step 3.

### step 3 — structural checks (§16.2)

not auto-fails, but count them up. 2+ structural violations = FAIL.

- **parallel triads:** "not just X, but Y and Z" — flag.
- **setup-payoff-reflection:** a three-act structure in a short post — flag.
- **consistent paragraph lengths:** if every paragraph is the same 3-4 sentence block — flag.
- **no sentence fragments:** real humans use fragments. if the draft has none and is ≥3 sentences, flag.
- **too clean logic:** every dot connects too neatly, no gaps or jumps — flag.

if the count is ≥2, FAIL with `failures: [...]` listing each flagged issue.

### step 4 — trait check (§3)

evaluate whether the draft exhibits rishabh's traits. score each trait 0 or 1 based on whether it shows up in the draft.

| trait | signal |
|---|---|
| 01 contrarian with receipts | makes a specific claim backed by experience, data, or structural analysis |
| 02 builder as identity | references an artifact or system, not just a thought |
| 03 creative wearing a pm mask | aesthetic, naming, or visual thinking shows through |
| 04 permanent outsider | insider detail without institutional deference |
| 05 self-aware ambition | acknowledges the scale/delusion ratio |
| 06 planning trap (shadow side) | [not a positive trait — don't score] |

**rule:** draft must score 1 on trait 01 (contrarian with receipts) AND at least 1 on traits 02–05. if the draft has no specific receipt → auto-FAIL with `failures: ["no receipt — specific claim required"]`. if it has a receipt but no other trait → FAIL with `failures: ["single-trait draft — needs another angle"]`.

### step 5 — humor mode check (§4)

evaluate whether the draft uses at least one of the nine humor modes: structural, precision, scale, deadpan, self-aware escalation, quiet bomb, callback, the turn, or compression.

**rule:** draft must use at least one humor mode **unless** it's a pure analytical observation (task = "reply" and the source context is a serious thread about something urgent). score 0 or 1.

if 0 and the context isn't serious-analytical → FAIL with `failures: ["no humor mode detected — voice reads flat"]`.

### step 6 — emotional truth gate (§17, HARD)

every draft must trigger at least ONE of the three emotional truths. read `memory/emotional-truths.md` to check each:

1. "this person sees things i don't see." → does it?
2. "this person is building something i want to watch." → does it?
3. "this person is like me but further along." → does it?

count = sum of yes/no across the three. if count == 0 → **hard FAIL** with `failures: ["no emotional truth triggered"]`. this is non-negotiable.

### step 7 — §18 single test (final judgment)

this is the one LLM-judgment call in the whole skill. ask yourself:

> *"Does this sound like it came from someone who was going to think this anyway, and the post is just the part that leaked out?"*

if yes, PASS. if no, FAIL with `failures: ["fails §18 — sounds like content, not like rishabh"]` and a one-sentence note explaining what makes it sound like content.

### step 8 — tone contamination (§16.4)

- **relentless positivity?** → flag
- **false balance (on-one-hand/on-the-other-hand)?** → flag
- **emotional signposting ("this is exciting", "i'm passionate about")?** → flag
- **overly clean logic, every dot connects?** → flag

count = flags. if count ≥ 2, FAIL.

## output format

always return a single JSON object:

```json
{
  "verdict": "PASS" | "FAIL",
  "failures": ["specific rule name: details", ...],
  "notes": "one-sentence explanation of the outcome — useful for the next redraft attempt"
}
```

if verdict is PASS, `failures` is `[]`.

## non-negotiable rules for voice-check itself

- **never return PASS on a draft with any hard-fail violation** from step 2, 4 (no receipt), or 6 (no emotional truth). these are non-negotiable.
- **never soften the verdict to be polite.** this skill exists precisely to be the unforgiving gate.
- **never rewrite the draft.** that's `content-engine`'s job. this skill only validates.
- **never hallucinate a pass.** if you're not sure, FAIL. it's cheaper to redraft than to publish "content".
