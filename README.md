# sendplan

A daily outreach planner for TikTok Shop creators that reads the shop's weekly outreach quota before it decides who to contact, and then checks what the platform actually accepted.

This is an independent prototype. It is not affiliated with, endorsed by or reviewed by Reacher or TikTok. It never calls a real API. All creators, quotas and results in this repository are synthetic.

## The problem

A brand recruiting affiliates has a list of creators, a sequence (a target collaboration invite, then follow-up DMs, then an email) and a daily send cap. Every day something picks who gets which message.

My first version of this project did that with flat per-channel caps that I made up, and it assumed every planned message was delivered. Both assumptions are wrong on TikTok Shop:

- TikTok limits how many creators a shop can reach per week, and the limit is split by creator level. The higher levels have the smallest allowance.
- A send can be refused for many reasons (quota used up, handle not found, five-message limit, already invited), and the planner only finds out afterwards from a per-creator outcome list.

A planner that sorts by score and ignores this spends most of its daily cap on high-level creators the platform will refuse, and then records them as contacted.

## Why this matters to Reacher

What I verified, with sources:

- Reacher's site says its outreach automation recruits up to 10,000 creators a day across DM, collab invites and email. (https://reacherapp.com/)
- Reacher publishes an OpenAPI document for its public API. `GET /automations/outreach-capacity` returns an `OutreachCapacityResponse` with a daily per-shop pool counted in UTC and TikTok's weekly `outreach_quota`, which can be split into `tiers` by creator level. The description says a null quota means unknown and never zero, that DMs do not count toward the daily pool, and that the pool is shared with direct target collab invites. (https://docs.reacherapp.com/openapi.json)
- The same document defines `GET /automations/{automation_id}/creators`, a per-creator outcome ledger (`AutomationCreatorOutcome`) with outcomes such as `invite_submitted`, `outreach_sent`, `previously_invited` and `skipped`, and 21 `skip_reason` codes including `creator_tier_quota_exhausted`, `five_message_limit_hit` and `creator_not_found`. It says to branch on the code and that a submitted invite is queued, not confirmed delivered.
- `AutomationSchedule` in that document holds one cap per weekday (`Monday_maxCreators` and so on, 0 to 10,000). Automation requests allow at most 5 initial messages and 3 follow-up steps.
- Reacher's own blog post on automated outreach says to pause follow-ups when a creator replies and to keep one contact history so messages are not duplicated across channels. (https://www.reacherapp.com/blog/automated-creator-outreach-how-to-scale-creator-messages-without-sounding-like-a-bot)
- For TikTok's side I used a third-party help article from Euka, dated November 2026, because I could not find the rule on a TikTok page I could read: each creator counts once per week even if they receive both an invite and a DM, there is a five-DM limit before a creator replies, Lv 0-1 creators are unlimited, and weekly limits per level depend on the shop's GMV band. (https://support.euka.ai/articles/9411832069-understanding-creator-outreach-limits-and-gmv-requirements)

So the two payload shapes this project reads are real and public. What Reacher does internally with them I do not know, and I have not guessed.

## What it does

1. `capacity.py` parses an `OutreachCapacityResponse` and an `AutomationSchedule`, using the public field names, and answers one question: how many more creators of level N can this shop reach before the reset. The answer is a number, no ceiling, or unknown.
2. `quota.py` plans one day. It gives each creator-level group a budget from the snapshot and charges a creator once per quota period. A follow-up in the same period as the first contact is free. A follow-up after the reset costs a new unit. It stops DM steps at five unanswered DMs, keeps invites inside the shop's daily pool, and applies the weekday cap.
3. `ledger.py` reads pages of the outcome ledger and updates state from what was accepted. Only a row that says submitted advances a creator. Quota refusals wait for the reset, unknown codes are retried and then dropped, a missing row leaves the send pending, and applying the same pages twice changes nothing.
4. `shopsim.py` is a synthetic shop that enforces the rules by itself and speaks the same two payload shapes, so I can measure a planner against something that says no.
5. `sendplan.py` keeps the canonical handle key and the v1 planner, which is now the baseline.

## Architecture

```
import rows -> Book (sendplan.py)  one record per canonical handle
                 |
capacity JSON -> parse_capacity ---+
schedule JSON -> parse_schedule ---+--> plan_day (quota.py) --> list of sends
                                                                   |
                                   (whatever delivers messages; here shopsim.py)
                                                                   |
Book <-- reconcile (ledger.py) <-- ledger pages (AutomationCreatorsResponse)
```

`plan_day` is pure. It reads the book and returns sends, counters and warnings. All state changes happen in `reconcile` and `Book.stop`.

## Key decisions and tradeoffs

- **Strict parsing.** A capacity payload with a missing field, a negative number, an unknown `quota_type` or a level listed in two tiers raises an error. I would rather skip a day than guess a quota. The cost is that a new enum value on their side breaks the run until someone looks.
- **Unknown is not zero.** With a null quota the default is to plan on the daily cap alone and attach a warning. `Policy(unknown="hold")` instead sends only what cannot cost a unit. A snapshot whose `resets_at` has passed is treated as unknown too.
- **When two numbers disagree, take the smaller.** If a tier's `remaining` is larger than its limits minus its used counts, I use the smaller value.
- **A tiered shop reports `unlimited: true` at the top level.** The schema says so, and it is easy to read that as no limit. The tiers decide.
- **Refused is not sent.** Outcome `unknown` and unrecognised codes are treated as not sent. That risks a second attempt at a message that did go out. I chose that over silently dropping a creator, and capped it at three tries.
- **Ledger code meanings are my reading of their names.** The spec lists the codes without defining most of them. For example I retry `blocked_by_outreach_limit` the next day because it could mean the daily pool. The mapping is one table at the top of `ledger.py`.
- **Tuning on expected value.** Replies are rare, so the realised reply score moves by several points between seeds. The simulator also adds up reply probability times score for every delivered send, and I tune on that.

## Install and usage

Written for Python 3.8 or newer and tested on 3.13. Standard library only, nothing to install.

```
python demo.py                               # under a minute
python -m unittest test_sendplan test_quota  # 60 tests
python bench.py                              # 48 simulated months
python bench.py --resume                     # continue a stopped benchmark
```

```python
import datetime as dt, json
from sendplan import Book
from capacity import parse_capacity, parse_schedule
from quota import QUOTA_SEQUENCE, Policy, plan_day
from ledger import reconcile

book = Book(sequence=QUOTA_SEQUENCE)
book.add("https://www.tiktok.com/@Glow.Kate", score=0.91, email="kate@example.test", level=4)
book.add("@glow.kate", score=0.40)            # merges into the row above

cap = parse_capacity(json.load(open("sample_capacity.json")))
schedule = parse_schedule({"Monday_maxCreators": 2000})
now = dt.datetime(2026, 11, 2, 20, 0, tzinfo=dt.timezone.utc)
policy = Policy(shop_daily_limit=2500)

plan = plan_day(book, cap, schedule, now, policy)
# ... deliver plan.sends, then fetch the outcome ledger pages ...
# reconcile(book, plan, pages, now, cap, policy)
```

## A real example

Output of `python demo.py`, copied from my run:

```
1. Reading sample_capacity.json (OutreachCapacityResponse shape, synthetic)
   daily pool used: 400, quota resets in 6 daily runs
   Lv 0-1  remaining this week: no ceiling
   Lv 2    remaining this week: 1400
   Lv 3    remaining this week: 480
   Lv 4-7  remaining this week: 290

2. One day on a synthetic shop of 3000 creators
   3299 import rows became 3000 creator records, 0 rejected
   Lv 0-1  remaining this week: no ceiling
   Lv 2    remaining this week: 299
   Lv 3    remaining this week: 87
   Lv 4-7  remaining this week: 60
   blind       plan: 300 sends, quota units by group {}, counters {}
   quota-aware plan: 300 sends, quota units by group {'Lv 2': 127, 'Lv 3': 87, 'Lv 4-7': 60}, counters {'planned_without_quota': 17, 'tier_quota': 327}
   ledger: 1 page(s), outcomes {'invite_submitted': 275, 'previously_invited': 6, 'skipped': 19, 'total': 300}
   skip reasons: {'creator_not_found': 6, 'creator_tier_quota_exhausted': 13}
   reconcile: {'sent': 275, 'step_already_done': 6, 'stopped': 6, 'held_until_reset': 13}
   reconcile again with the same pages: {'already_reconciled': 300}

3. Four weeks, same shop settings, seed 42
   planner                          delivered  refused  replies expected score
   v1: flat cap, assumes delivered       4918     1722      171           63.7
   blind: flat cap, reads ledger         4604     2036      157           66.4
   quota-aware                           6440      186      231           79.6
```

The 13 quota refusals in step 2 are sends the planner could not have known about. Other people in the synthetic shop used units after the morning snapshot, and 17 creators were imported without a level, so the planner sent them without knowing which group they would be charged to.

## Tests and verified results

60 tests pass with `python -W error -m unittest test_sendplan test_quota`. The 17 original tests are unchanged. The new ones cover malformed capacity payloads, null and zero quotas, stale snapshots, tier budgets, free and paid follow-ups, both throttle flags, the DM limit, every ledger action, duplicate and malformed ledger rows, idempotent reconcile, and loading a state file written by v1. One test runs a month on a synthetic shop with no outside usage and requires zero refusals. That only passes if the planner's model of the rules matches the simulator's on every send, and the simulator does not import the planner.

Benchmark, from `python bench.py`. Four simulated weeks, weekday cap 2,000 and weekend cap 500. "Refused" counts sends the synthetic platform rejected. "Reply score" is the sum of the brand's score for creators who replied, and "expected score" is the low-noise version described above. I tuned three policy settings on TRAIN (20,000 creators, seeds 101 to 103). HELDOUT uses 26,000 creators, a different level mix, different weekly limits, reply rates and defect rates, more outside usage, and seeds 901 to 903. I did not look at it while tuning. Numbers are means of three seeds.

HELDOUT:

| planner | delivered | refused | replies | replies Lv 2+ | reply score | expected score | quota units unused |
|---|---|---|---|---|---|---|---|
| v1: flat cap, assumes delivered | 26839 | 17160 | 814 | 689 | 447.8 | 433.3 | 4991 |
| blind: flat cap, reads ledger | 26651 | 17241 | 854 | 779 | 502.1 | 493.0 | 3268 |
| quota-aware, default policy | 40597 | 2936 | 1337 | 944 | 653.7 | 636.1 | 41 |
| quota-aware, tuned on TRAIN | 40904 | 2789 | 1340 | 919 | 653.5 | 637.8 | 0 |

TRAIN:

| planner | delivered | refused | replies | replies Lv 2+ | reply score | expected score | quota units unused |
|---|---|---|---|---|---|---|---|
| v1: flat cap, assumes delivered | 34756 | 9243 | 1191 | 560 | 460.1 | 457.0 | 3286 |
| blind: flat cap, reads ledger | 33990 | 9663 | 1144 | 639 | 493.0 | 482.2 | 1311 |
| quota-aware, default policy | 42557 | 1102 | 1485 | 650 | 545.1 | 546.5 | 802 |
| quota-aware, tuned on TRAIN | 42649 | 1023 | 1486 | 639 | 543.5 | 546.8 | 770 |

What I take from this:

- Reading the quota is the whole result. On HELDOUT the share of sends refused fell from 39.3% (blind) to 6.7%, delivered sends rose 52.3%, and expected score rose 29.0%.
- v1 assumed 17,160 refused sends were delivered and moved those creators forward in their sequence. Reading the ledger fixes the bookkeeping but not the waste: the blind planner still loses about the same number of slots.
- The tuning did almost nothing. The grid picked `reserve=1.0`, which beat the default by 0.3 points of expected score on TRAIN and 1.7 on HELDOUT. Both gaps are smaller than the spread between seeds (631.4 to 643.2 for the default on HELDOUT). I would not claim the tuned policy is better.
- Two grid results were consistent across seeds on TRAIN. Sending follow-ups that need a new unit before first contacts scored 546.5, against 539.5 for after and 534.3 for never. A 10% margin cut refusals from 1,102 to 943 with no measurable change in score.
- No planner repeated a step or wrote to a creator after a reply in any run.

## Limitations and next steps

- All data is synthetic. Reply rates, level mix, defect rates and outside usage are numbers I picked. The weekly limits per level come from a third-party help article, not from TikTok.
- "A follow-up after the reset costs a new unit" is my inference from the once-per-week rule. If TikTok does not charge for it, the paid follow-up logic is unnecessary.
- I do not know how Reacher's own scheduler uses these payloads, and nothing here was tested against the real API. The ledger `pagination` object is untyped in the spec, so the simulator's version of it is a guess and the planner ignores it.
- The planner assumes one fresh capacity snapshot per day and one automation. It does not coordinate several automations sharing the daily pool beyond reading `daily_creators_used`.
- Creators imported without a level are sent without a budget under the default policy, which caused some of the remaining refusals. Looking the level up first would be the next thing I would build.
- State lives in memory and is saved as one JSON file. A real system would keep it in PostgreSQL with a lock per shop.
- Renamed handles are still not matched. That needs a stable creator id from the platform.
