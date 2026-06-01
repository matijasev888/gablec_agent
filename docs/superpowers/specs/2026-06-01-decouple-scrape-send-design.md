# Decouple scrape/send with morning retries — Design

**Date:** 2026-06-01
**Status:** Approved (pending spec review)

## Problem

On Monday 2026-06-01 the bot posted an all-empty menu to Slack ("Nema objave za danas" for
all three restaurants), even though the restaurants had posted their weekly menus the day
before.

### Root cause (verified, not assumed)

Production run `26743731796` (08:26 UTC) log shows:

```
Cache restored successfully
New week started - clearing cache      ← cache logic worked correctly
Restaurants to process: 3              ← all 3 queued correctly
No posts found for ...Zaboky...        ← Apify returned 0 posts
No posts found for ...Grašo...
No posts found for ...mondozabok...
MENU SUMMARY: No menu / No menu / No menu
```

The **caching was not at fault**. The Apify Facebook scraper returned an empty dataset even
though the actor reported `SUCCEEDED`. Evidence the posts existed at run time:

- Zaboky posted its weekly menu **2026-05-31 18:22 UTC** (Sunday)
- Grašo posted **2026-05-31 11:52 UTC** (Sunday)

Both are hours *before* the 08:26 UTC run. Re-running the **identical query** at ~12:12 UTC the
same day returned the posts (2 / 1 / 1). So Apify intermittently returns 0 posts — classic
Facebook-scraper flakiness (residential proxy IP soft-blocked: page resolves, feed comes back
empty). The existing `maxRequestRetries: 10` does not help because those requests *succeed*;
they just yield nothing.

Two issues:

1. **Trigger (external):** Apify intermittently returns 0 posts even when posts exist.
2. **Design flaw:** zero resilience. A 0-post scrape is silently accepted and, because the job
   runs in `full` mode (scrape→send in one shot, once per day), an empty result is immediately
   posted with no retry.

## Goal

Make the bot resilient to transient Apify failures by retrying the scrape across the morning
(each attempt gets a fresh proxy IP) and sending as early as the menu is complete, with a hard
deadline.

- **Target send: 08:00 local** — if all menus are ready, send then.
- **Deadline: 09:30 local** — keep scraping until then; send the latest regardless.

## Approach

Reuse the existing two-phase design. The code already supports `--mode scrape` / `--mode send`,
and `scrape_and_process()` already skips any restaurant already cached for today (so once a
restaurant is cached, later scrape runs make **zero** Apify calls for it — the extra scheduled
runs are essentially free). The change is **scheduling + reliable shared state + a
double-post guard**.

### Schedule (GitHub Actions cron, Mon–Fri, all times UTC; Zagreb summer = UTC+2)

| Cron (UTC)        | Local   | Mode    | Behavior |
|-------------------|---------|---------|----------|
| `30 4 * * 1-5`    | 06:30   | scrape  | fill cache |
| `0 5 * * 1-5`     | 07:00   | scrape  | fill cache |
| `30 5 * * 1-5`    | 07:30   | scrape  | fill cache |
| `0 6 * * 1-5`     | 08:00   | send    | **Send #1** — send if all 3 ready |
| `30 6 * * 1-5`    | 08:30   | scrape  | catch stragglers |
| `0 7 * * 1-5`     | 09:00   | scrape  | catch stragglers |
| `30 7 * * 1-5`    | 09:30   | send    | **Send #2** — send latest (deadline) |

Mode is selected from `github.event.schedule`: the two send crons (`0 6 * * 1-5`,
`30 7 * * 1-5`) map to `send`; everything else maps to `scrape`. `workflow_dispatch` keeps the
manual `mode` input (scrape/send/full).

> GitHub cron can fire 10–30 min late, so these times are approximate. They are fixed UTC, so
> in winter (CET, UTC+1) the local times shift one hour earlier (07:00 send / 08:30 deadline).
> DST handling is out of scope.

### Send logic (new `sent_date` guard)

Add `"sent_date"` (ISO date string) to the cache top level.

`send_daily_message()` becomes:

1. If `cache["sent_date"] == today` → **skip** (already posted today). Prevents double-post.
2. Build `today_lunch` from cache (existing logic).
3. Count restaurants that have a non-empty menu for today (`ready_count`).
4. Decide by which send this is:
   - **Send #1 (08:00):** post only if `ready_count == 3` (all ready). Otherwise skip (no mark).
   - **Send #2 (09:30, deadline):** post if `ready_count >= 1`. If `ready_count == 0` (all
     empty) → **skip and log a warning** (do not post the all-empty message).
5. On a successful post: set `cache["sent_date"] = today`, save, and commit the cache.

How does `send_daily_message()` know if it is Send #1 or Send #2? Pass an explicit flag —
e.g. `send_daily_message(final: bool)` — wired from a new `--mode send-final` (or a
`--final` flag on `send`). Send #1 uses `send`, Send #2 uses the final variant. This keeps the
"all 3 vs. any" rule out of time-of-day guessing.

Individual missing restaurants still render "Nema objave za danas" when at least one menu
exists (existing `build_slack_blocks` behavior, unchanged).

### State persistence: commit cache to the repo

**Precondition:** `menu_cache.json` is currently **gitignored and untracked** — the existing
setup relies entirely on `actions/cache`. To commit it to the repo we must first remove
`menu_cache.json` from `.gitignore` and `git add` it (commit the current, freshly-scraped
week's data as the initial tracked version).

Then each **scrape** run and each **successful send** run commits the updated file back:

- Workflow needs `permissions: contents: write`.
- A `concurrency` group (e.g. `group: gablec-bot`, `cancel-in-progress: false`) so runs never
  overlap and race the push.
- Before pushing: `git pull --rebase` as a safety net.
- Only commit when `menu_cache.json` actually changed (`git diff --quiet` guard).
- Commit message e.g. `chore: update menu cache [skip ci]` so the commit does not trigger other
  workflows.

This replaces the current `actions/cache/restore|save` steps (removed). It is transparent,
gives history, and avoids cache-eviction/propagation surprises.

## Components changed

- `gablec_script/gablec_daily.py`
  - `send_daily_message(final: bool = False)` — add `sent_date` guard, all-3-vs-any rule,
    all-empty skip, set+save `sent_date` on success.
  - Helper to count ready restaurants (pure function, unit-testable).
- `gablec_script/main.py` — add the final-send mode/flag; pass `final` through.
- `.github/workflows/daily.yml` — new cron schedule, mode mapping, `contents: write`,
  `concurrency`, commit-and-push steps; remove `actions/cache` steps.
- `.gitignore` — remove the `menu_cache.json` entry so the cache can be tracked/committed.

## Explicitly NOT doing (YAGNI)

- **No within-run Apify retry.** The 6 scheduled scrape attempts each get a fresh proxy IP,
  which is the real fix. Per-run retries would burn credits for <30 min faster recovery. Easy
  to add later if the scheduled cadence proves insufficient.
- **No DST handling.** Fixed UTC cron; accept the winter one-hour shift.

## Edge cases

- **Restaurant genuinely has no menu today** (closed/holiday): never gets cached → keeps being
  scraped all morning (harmless, just Apify calls) → renders "Nema objave za danas". At the
  9:30 deadline it posts with the others.
- **All three empty at 9:30** (e.g. holiday, nobody posted): Send #2 skips and logs a warning —
  no message that day.
- **Empty-list cached for today:** `get_cached_menu_for_today` returns `[]` (not `None`) which
  the scrape treats as "cached" and skips. Gemini only returns dates it found a menu for, so
  this is unlikely, but harden `ready_count`/the scrape check to treat an empty list as "not
  ready" so it keeps retrying.
- **Manual run mid-morning:** `workflow_dispatch` with `mode=full` or `mode=send` still works;
  `sent_date` guard prevents a manual send from double-posting after an automatic one.

## Testing

Pytest unit tests, no live APIs (mock Slack/Apify/Gemini boundaries):

- `sent_date` guard: skip when `sent_date == today`.
- Send #1: posts when all 3 ready; skips (no mark) when only some ready.
- Send #2: posts when ≥1 ready; skips + warns when all empty.
- `ready_count` treats empty list as not-ready.
- Successful send sets `sent_date` and saves cache.

## Out of scope (tracked separately)

- Leaked secrets: `gablec_script/.env`, `test/`, and `step_by_step/` are gitignored (so not in
  the current tree), but the git history likely contains committed API keys (see the
  "new api key" commit) and the hardcoded Gmail app password in `step_by_step/README.md`. These
  should be rotated and scrubbed from history.
- Node-20 deprecation warning on the GitHub Actions (action versions) — address only if trivial
  while editing the workflow.
