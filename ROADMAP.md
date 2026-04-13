# ROADMAP.md — aria v4 architecture at a glance

the 10-domain architecture from `memory/aria-v4-spec.md` with current status. `[shipping now]` means it's operational tonight. `[disabled cron]` means a cron job exists but is disabled until unblock criteria are met. `[deferred]` means not even a stub — documented but not scaffolded.

---

## domain 1 — intelligence (10 streams)

| stream | status | notes |
|---|---|---|
| 1. target account monitoring | `[shipping now — mock]` | `skills/intelligence-mock` reads `memory/target-inbox.jsonl`. real X API replaces this. |
| 2. trending topic detection | `[deferred]` | needs X + LinkedIn API |
| 3. cross-platform signals (reddit, hn, ph) | `[deferred]` | candidate: use bundled `blogwatcher` skill for RSS feeds |
| 4. competitor and peer monitoring | `[deferred]` | needs X API |
| 5. audience behavior | `[deferred]` | needs real follower data |
| 6. mention and amplification | `[deferred]` | needs X API webhooks |
| 7. interest graph position | `[disabled cron]` `aria-interest-graph` | unblocks when: X API + openclaw memory semantic index populated with ≥20 approved drafts |
| 8. negative signal detection | `[disabled cron]` `aria-negative-signals` | unblocks when: real posting history + follower-count snapshots |
| 9. engagement curve analyzer | `[disabled cron]` `aria-engagement-curves` | unblocks when: X API impressions data |
| 10. follower quality scorer | `[disabled cron]` `aria-follower-quality` | unblocks when: `xurl` CLI + X API developer credentials |

## domain 2 — strategy

| component | status | notes |
|---|---|---|
| semantic consistency management | `[deferred]` | needs populated memory index |
| negative signal response | `[deferred]` | needs stream 8 |
| engagement curve optimization | `[deferred]` | needs stream 9 |
| follow strategy | `[deferred]` | needs target-seed-accounts.md populated by rishabh |
| notification tap-through optimization | `[deferred]` | needs real engagement data |
| session depth awareness | `[embedded in content-engine SKILL.md]` | loose version, not a full strategy skill |

## domain 3 — content

| component | status | notes |
|---|---|---|
| content-engine + voice-check loop | `[shipping now]` | `skills/content-engine` + `skills/voice-check` |
| dwell time optimization | `[deferred]` | needs real engagement data to optimize for |
| hook pattern tagging | `[shipping now]` | content-engine tags every draft with a hook_pattern |
| conversation depth strategy | `[deferred]` | needs real reply chain data |
| thread completion optimization | `[deferred]` | threads aren't in the milestone 0 format set |
| semantic consistency enforcement | `[deferred]` | needs populated memory index |
| session depth content design | `[embedded in voice-rules.md]` | aware but not instrumented |
| media type pool strategy | `[deferred]` | text-only this milestone |

## domain 4 — distribution

| component | status | notes |
|---|---|---|
| quote-tweet strategy | `[deferred]` | card type 3 not yet implemented |
| cross-platform arbitrage | `[deferred]` | card type 4 not yet implemented |
| thread-based distribution | `[deferred]` | threads deferred |

## domain 5 — conversion

all `[deferred]` — profile optimization, notification tap-through, follow triggers. unblocks when: real engagement data is flowing.

## domain 6 — network (CRM)

| component | status | notes |
|---|---|---|
| target-seed-accounts template | `[shipping now — stub]` | `memory/target-seed-accounts.md` waiting for rishabh to populate |
| conversation depth tracking | `[deferred]` | needs real reply chain data |
| relationship advancement (cold→warm→engaged→allied) | `[deferred]` | needs network-crm skill + real engagement signals |

## domain 7 — compliance

**`[shipping now — complete]`** — `skills/compliance` implements all 5 checks (professional, regulatory, legal, platform, brand). the only domain that is complete in milestone 0.

## domain 8 — experimentation

all `[deferred]` — algorithm signal experiments (dwell time, hook pattern, thread length, media type, conversation depth). unblocks when: real engagement data lets us measure experimental variants.

## domain 9 — evolution

all `[deferred]` — monthly recalibration, platform algorithm change detection, engagement curve evolution, shadow-reduction recovery. unblocks when: ≥60 days of run history.

## domain 10 — platform intelligence

`[disabled cron]` `aria-platform-health` — account health score, shadow-reduction detection, platform-wide trend detection, media type distribution, keyword effectiveness. unblocks when: ≥30 days of posting history + 10 baseline comparison accounts identified.

---

## shipping tonight (vertical slice)

1. dedicated `aria` agent with broad telegram binding
2. workspace populated with 7 identity files, 12 memory atoms, 5 state files
3. 5 skills: voice-check, content-engine, compliance, intelligence-mock, telegram-card
4. 2 standing-order programs in `AGENTS.md`: reply-opportunity-pipeline, daily-angles
5. 1 inline program: behavioral-nudge-check (with §14.1 publishing-drought + §14.6 playing-it-safe)
6. 2 enabled cron jobs: aria-daily-angles (08:57 asia/kolkata), aria-reply-pipeline (every 40 min)
7. 5 disabled cron stubs for interest-graph, negative-signals, engagement-curves, follower-quality, platform-health
8. end-to-end manual loop: seed target-inbox → trigger pipeline → receive telegram card → approve → draft lands in approved-drafts.jsonl → manual post to X

## first posts rishabh can ship

- seed 1 tweet into `memory/target-inbox.jsonl`
- run `openclaw agent --agent aria --message "run the reply-opportunity-pipeline program..."` or wait for the next cron
- receive telegram card, click APPROVE
- copy the draft from aria's reply message
- paste into X, post

done.

---

## meta warning (§14.4)

aria is itself the kind of system §14.4 of the personality doc warns about: an ambitious multi-domain architecture built for an account with 400 followers. the ratio of system complexity to audience size is comically embarrassing.

**the single test that keeps aria from becoming procrastination:** does rishabh approve a real card today that leads to a real post on X? if yes, aria is doing its job. if no after 48 hours, §14.1 fires and the system gets questioned.

every standing order in `AGENTS.md` opens with *"execute immediately. do not discuss. the plan is this file. you are the execution."* this is the primary defense against ARIA itself falling into the trap it was built to solve.
