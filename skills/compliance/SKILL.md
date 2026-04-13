---
name: compliance
description: 5-check gate for drafts. Kills anything that creates professional, regulatory, legal, platform, or brand risk. This skill is the reason rishabh can approve drafts without reading each one paranoidly. Hard-coded with MOFSL + SEBI + India-specific rules.
metadata:
  openclaw:
    emoji: "🛑"
---

# compliance

5 checks, in order. any KILL stops the draft from reaching telegram. WARN lets it through with a banner. PASS is the happy path.

## inputs

```
{
  "draft": "the text to check",
  "task": "reply" | "original"
}
```

## what to do

execute immediately. do not discuss your approach. run all 5 checks in order, accumulate failures, return the worst verdict encountered.

### step 0 — load context

read `memory/career-context.md` for:
- employer awareness (MOFSL / motilal oswal references)
- regulatory context (SEBI, AMFI, RBI rules rishabh operates under)
- what he can and can't legitimately claim to know

read `memory/anti-patterns.md` for the §15 checklist.

### check 1 — PROFESSIONAL (employer risk)

**fail (KILL)** if the draft:
- names MOFSL, motilal oswal, or any variant of the employer name in a way that implies company position on anything
- names specific colleagues, managers, executives, or team members
- mentions specific internal systems, product codenames, or proprietary architectures (advisory pro is public; internal codenames are not)
- discloses non-public business metrics, AUM figures, customer counts, or revenue data
- criticizes a direct MOFSL competitor in a way that reads like the company's position ("motilal oswal thinks X about zerodha" vs. general industry commentary)

**pass** if the draft:
- mentions "i built a thing at a large fintech" or "one of india's largest brokerages" (vague, generic, no leak)
- describes general industry dynamics without claiming to speak for the employer
- references advisory pro or public features that are already in marketing material

### check 2 — REGULATORY (SEBI risk — HARDEST)

**fail (KILL)** if the draft:
- makes any investment claim: "this fund is good", "this stock will go up", "avoid this sector"
- makes price predictions: "nifty will hit X", "this will 10x", "time to buy"
- offers stock tips, mutual fund recommendations, or portfolio advice
- implies fiduciary advice without the required RIA (Registered Investment Advisor) license
- discusses upcoming regulatory changes in a way that could be read as insider information

**pass** if the draft:
- describes regulatory mechanics in general terms ("SEBI's process for X works like this")
- comments on industry structure without claiming a recommendation
- discusses his own built artifacts without claiming they're investment advice

> **SEBI risk is the single sharpest edge in compliance.** when in doubt, KILL. this is not the check to be lenient on.

### check 3 — LEGAL (defamation, confidentiality, IP)

**fail (KILL)** if the draft:
- defames a named individual (specific person + specific false claim)
- discloses confidential agreements, NDAs, or client data
- reproduces copyrighted material beyond fair-use snippet length
- makes specific claims about a named company's internal operations that can't be publicly verified

**pass** if the draft:
- describes a named company's public product behavior (fair game)
- criticizes an idea or a system without attacking an individual
- uses short fair-use quotes with attribution

### check 4 — PLATFORM (X / LinkedIn TOS)

**fail (KILL)** if the draft:
- violates X TOS (hate speech, harassment, coordinated inauthentic behavior, violent content)
- violates LinkedIn professional standards (similar list, stricter on political content)
- contains content that would trigger a platform ban (CSAM, threats, explicit content)
- appears to coordinate with other accounts in a way that looks inauthentic

**pass:** almost everything rishabh writes is platform-safe because it's structural observation, not attack. this check is the cheapest of the five.

### check 5 — BRAND (alignment with the two rishabhs, §9)

this is a **soft** check. use WARN, not KILL, unless the draft directly contradicts his established identity.

**WARN** if the draft:
- performs ambition (claiming more than current status justifies) — nudge 14.9
- performs humility (excessive hedging that downplays real achievements) — nudge 14.9
- performs the bhilai origin as narrative — **this one is a KILL per §14.8**, not a warn
- sounds like marketing or LinkedIn-lingo
- breaks the two-rishabhs rule (public drafts should match the online register — sharp, edited, slightly intimidating in specificity)

**pass** if the draft matches the online register and calibrates ambition correctly.

## output format

```json
{
  "verdict": "PASS" | "KILL" | "WARN",
  "failures": [
    {"check": "professional" | "regulatory" | "legal" | "platform" | "brand", "severity": "kill" | "warn", "detail": "specific rule violated"}
  ],
  "notes": "one-sentence explanation",
  "warning_banner": "text to prepend to the telegram card if verdict is WARN (null otherwise)"
}
```

priority: if ANY check emits KILL, the verdict is KILL. if no KILL but any WARN, verdict is WARN. otherwise PASS.

## rules for compliance itself

- **when in doubt, KILL.** it is cheaper for rishabh to redraft than to receive a SEBI notice or a legal letter.
- **SEBI is the sharpest edge.** never let an investment claim through. never let a price prediction through. never let fund-specific advice through.
- **the bhilai check is a KILL, not a warn.** §14.8 is clear.
- **never rewrite the draft.** compliance is a gate, not a collaborator. return the verdict and let `content-engine` decide if a retry is worth it.
- **never invent violations.** don't flag things that aren't actually in the draft just to be safe. false positives wear rishabh's trust down.
