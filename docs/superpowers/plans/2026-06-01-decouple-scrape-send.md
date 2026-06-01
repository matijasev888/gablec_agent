# Decoupled Scrape/Send With Morning Retries — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the lunch bot resilient to Apify intermittently returning 0 posts, by scraping repeatedly through the morning, sending as soon as the menu is complete (target 08:00 local) with a 09:30 deadline, and never posting an all-empty message.

**Architecture:** Reuse the existing two-phase code (`--mode scrape` / `--mode send`). Add a `send-final` mode and a `sent_date` double-post guard. Extract the send decision into a pure, unit-tested function. Switch cross-run state from `actions/cache` to committing `menu_cache.json` to the repo. Replace the single daily cron with seven crons (six scrapes + two sends) mapped to modes.

**Tech Stack:** Python 3.11+, `uv`, pytest (dev only, run via `uv run --with pytest`), GitHub Actions, Apify, Google Gemini, Slack SDK.

**Spec:** `docs/superpowers/specs/2026-06-01-decouple-scrape-send-design.md`

**Branch:** `fix/decouple-scrape-send` (already created and checked out).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `gablec_script/gablec_daily.py` | Core logic | Add pure `decide_send_action`, `count_ready_restaurants`, `build_today_lunch`; rework `send_daily_message(final, today)`; harden scrape empty-list check |
| `gablec_script/main.py` | CLI entry / env validation | Add `send-final` mode; require Slack token for it; dispatch `final=True` |
| `tests/conftest.py` | Test bootstrap | Dummy env + `sys.path` so `gablec_daily` imports |
| `tests/test_send_logic.py` | Unit tests | Cover the pure helpers and `send_daily_message` decisions |
| `pyproject.toml` | Pytest config | Add `[tool.pytest.ini_options]` |
| `.gitignore` | Tracking | Remove `menu_cache.json` so it can be committed |
| `.github/workflows/daily.yml` | Scheduling/state | New crons, mode mapping, `contents: write`, `concurrency`, commit-and-push |

**Note on testability:** `gablec_daily.py` constructs `ApifyClient` and `genai.Client` at import using env vars. With dummy values these construct without network calls (verified). `conftest.py` sets dummy env vars before import. The pure functions and `send_daily_message` (with injectable `today` and monkeypatched `load_cache`/`save_cache`/`send_to_slack`) are tested without live APIs.

---

## Task 1: Test scaffolding

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/__init__.py` (empty)
- Modify: `pyproject.toml`

- [ ] **Step 1: Create `tests/__init__.py`** (empty file)

```python
```

- [ ] **Step 2: Create `tests/conftest.py`**

```python
import os
import sys
from pathlib import Path

# Provide dummy credentials so importing gablec_daily (which builds API
# clients at module load) does not require a real .env. load_dotenv uses
# override=False, so a real local .env still wins when present.
os.environ.setdefault("APIFY_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("SLACK_BOT_TOKEN", "test-slack")

# gablec_daily.py is a standalone module inside gablec_script/, not a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gablec_script"))
```

- [ ] **Step 3: Add pytest config to `pyproject.toml`** (append at end of file)

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Verify pytest collects nothing yet (no error)**

Run: `uv run --with pytest pytest -q`
Expected: `no tests ran` (exit code 5 is fine) — importantly, NO import/collection errors.

- [ ] **Step 5: Commit**

```bash
git add tests/__init__.py tests/conftest.py pyproject.toml
git commit -m "$(cat <<'EOF'
test: add pytest scaffolding for gablec_daily

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `decide_send_action` pure decision function

The heart of the send logic, isolated for testing.

**Files:**
- Modify: `gablec_script/gablec_daily.py` (add new function near the other send helpers, e.g. just above `send_daily_message`)
- Test: `tests/test_send_logic.py`

- [ ] **Step 1: Write the failing test** — create `tests/test_send_logic.py`

```python
import pytest
import gablec_daily as gd


@pytest.mark.parametrize("ready,total,final,already_sent,expected", [
    # Already sent today -> always skip
    (3, 3, False, True, "skip_sent"),
    (0, 3, True, True, "skip_sent"),
    # Send #1 (not final): post only if ALL ready
    (3, 3, False, False, "post"),
    (2, 3, False, False, "defer"),
    (0, 3, False, False, "defer"),
    # Send #2 (final / deadline): post if >=1, skip if all empty
    (3, 3, True, False, "post"),
    (1, 3, True, False, "post"),
    (0, 3, True, False, "skip_empty"),
])
def test_decide_send_action(ready, total, final, already_sent, expected):
    assert gd.decide_send_action(ready, total, final, already_sent) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest tests/test_send_logic.py -q`
Expected: FAIL — `AttributeError: module 'gablec_daily' has no attribute 'decide_send_action'`

- [ ] **Step 3: Implement `decide_send_action`** in `gablec_script/gablec_daily.py` (insert immediately before `def send_daily_message(`)

```python
def decide_send_action(ready_count: int, total: int, final: bool, already_sent: bool) -> str:
    """Pure decision for the send phase.

    Returns one of:
      'skip_sent'  - already posted today, do nothing
      'defer'      - Send #1 and not all restaurants ready yet; wait for the deadline
      'skip_empty' - Send #2 (deadline) but nothing to post
      'post'       - go ahead and post to Slack
    """
    if already_sent:
        return "skip_sent"
    if not final:
        # Send #1 (08:00 target): only post a complete menu.
        return "post" if ready_count >= total else "defer"
    # Send #2 (09:30 deadline): post whatever we have, but never an all-empty message.
    return "post" if ready_count >= 1 else "skip_empty"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --with pytest pytest tests/test_send_logic.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add gablec_script/gablec_daily.py tests/test_send_logic.py
git commit -m "$(cat <<'EOF'
feat: add decide_send_action send-phase decision logic

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `build_today_lunch` + `count_ready_restaurants` helpers

Extract the cache→today's-menu construction (currently inline in `send_daily_message`) into a pure function, plus a readiness counter.

**Files:**
- Modify: `gablec_script/gablec_daily.py`
- Test: `tests/test_send_logic.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_send_logic.py`

```python
from datetime import date


def _cache_with(menus_by_url):
    """Build a cache dict keyed by restaurant name, given {facebook_url: {date: [items]}}."""
    restaurants = {}
    for i, (url, menus) in enumerate(menus_by_url.items()):
        restaurants[f"Rest{i}"] = {"facebook_url": url, "menus": menus}
    return {"week_start": "2026-06-01", "restaurants": restaurants}


def test_build_today_lunch_maps_each_page(monkeypatch):
    pages = ["https://a/", "https://b/"]
    monkeypatch.setattr(gd, "FACEBOOK_PAGES", pages)
    cache = _cache_with({
        "https://a/": {"2026-06-01": ["jelo1", "jelo2"]},
        "https://b/": {"2026-06-01": []},
    })
    lunch = gd.build_today_lunch(cache, date(2026, 6, 1))
    assert len(lunch) == 2
    items = {info["facebook_url"]: info["items"] for info in lunch.values()}
    assert items["https://a/"] == ["jelo1", "jelo2"]
    assert items["https://b/"] == []


def test_build_today_lunch_missing_restaurant_shows_empty(monkeypatch):
    pages = ["https://a/", "https://missing-page/"]
    monkeypatch.setattr(gd, "FACEBOOK_PAGES", pages)
    cache = _cache_with({"https://a/": {"2026-06-01": ["jelo1"]}})
    lunch = gd.build_today_lunch(cache, date(2026, 6, 1))
    assert len(lunch) == 2
    assert any(info["items"] == [] for info in lunch.values())


def test_count_ready_restaurants_ignores_empty_lists():
    lunch = {
        "A": {"items": ["x"], "facebook_url": "u1"},
        "B": {"items": [], "facebook_url": "u2"},
        "C": {"items": ["y", "z"], "facebook_url": "u3"},
    }
    assert gd.count_ready_restaurants(lunch) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_send_logic.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'build_today_lunch'`

- [ ] **Step 3: Implement both helpers** in `gablec_script/gablec_daily.py` (insert before `def decide_send_action(`)

```python
def build_today_lunch(cache: dict, today_date: date) -> dict:
    """Build the per-restaurant menu dict for today from the cache.

    Returns {display_name: {"restaurant", "items", "facebook_url"}} with one
    entry per page in FACEBOOK_PAGES. Restaurants not present in the cache get
    an empty item list.
    """
    today_str = today_date.isoformat()
    today_lunch = {}

    for page_url in FACEBOOK_PAGES:
        found = False
        for restaurant_name, restaurant_data in cache.get("restaurants", {}).items():
            if restaurant_data.get("facebook_url") == page_url:
                menus = restaurant_data.get("menus", {})
                today_lunch[restaurant_name] = {
                    "restaurant": restaurant_name,
                    "items": menus.get(today_str, []),
                    "facebook_url": page_url,
                }
                found = True
                break
        if not found:
            url_name = page_url.rstrip('/').split('/')[-1]
            today_lunch[url_name] = {
                "restaurant": url_name,
                "items": [],
                "facebook_url": page_url,
            }

    return today_lunch


def count_ready_restaurants(today_lunch: dict) -> int:
    """Number of restaurants with a non-empty menu for today."""
    return sum(1 for info in today_lunch.values() if info["items"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with pytest pytest tests/test_send_logic.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add gablec_script/gablec_daily.py tests/test_send_logic.py
git commit -m "$(cat <<'EOF'
feat: extract build_today_lunch and count_ready_restaurants helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Rework `send_daily_message(final, today)` with sent_date guard

Wire the helpers together, add the double-post guard, inject `today` for testability.

**Files:**
- Modify: `gablec_script/gablec_daily.py` (replace the body of `send_daily_message`)
- Test: `tests/test_send_logic.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_send_logic.py`

```python
class _SaveSpy:
    def __init__(self):
        self.saved = None

    def __call__(self, cache):
        self.saved = cache


def _setup_send(monkeypatch, cache, slack_result=True):
    """Wire FACEBOOK_PAGES, load_cache, save_cache, send_to_slack for a send test."""
    monkeypatch.setattr(gd, "FACEBOOK_PAGES", ["https://a/", "https://b/", "https://c/"])
    monkeypatch.setattr(gd, "load_cache", lambda: cache)
    spy = _SaveSpy()
    monkeypatch.setattr(gd, "save_cache", spy)
    sent = {"called": False}

    def fake_slack(today_lunch, today_date, max_retries=3):
        sent["called"] = True
        return slack_result

    monkeypatch.setattr(gd, "send_to_slack", fake_slack)
    return spy, sent


def _full_cache():
    return {
        "week_start": "2026-06-01",
        "restaurants": {
            "A": {"facebook_url": "https://a/", "menus": {"2026-06-01": ["a1"]}},
            "B": {"facebook_url": "https://b/", "menus": {"2026-06-01": ["b1"]}},
            "C": {"facebook_url": "https://c/", "menus": {"2026-06-01": ["c1"]}},
        },
    }


MONDAY = date(2026, 6, 1)


def test_send1_posts_when_all_ready(monkeypatch):
    spy, sent = _setup_send(monkeypatch, _full_cache())
    assert gd.send_daily_message(final=False, today=MONDAY) is True
    assert sent["called"] is True
    assert spy.saved["sent_date"] == "2026-06-01"


def test_send1_defers_when_not_all_ready(monkeypatch):
    cache = _full_cache()
    cache["restaurants"]["C"]["menus"] = {}  # C not ready
    spy, sent = _setup_send(monkeypatch, cache)
    assert gd.send_daily_message(final=False, today=MONDAY) is True
    assert sent["called"] is False          # did not post
    assert spy.saved is None                # did not mark sent


def test_send2_posts_partial(monkeypatch):
    cache = _full_cache()
    cache["restaurants"]["C"]["menus"] = {}  # only A,B ready
    spy, sent = _setup_send(monkeypatch, cache)
    assert gd.send_daily_message(final=True, today=MONDAY) is True
    assert sent["called"] is True
    assert spy.saved["sent_date"] == "2026-06-01"


def test_send2_skips_when_all_empty(monkeypatch):
    cache = {"week_start": "2026-06-01", "restaurants": {}}
    spy, sent = _setup_send(monkeypatch, cache)
    assert gd.send_daily_message(final=True, today=MONDAY) is True
    assert sent["called"] is False
    assert spy.saved is None


def test_already_sent_is_skipped(monkeypatch):
    cache = _full_cache()
    cache["sent_date"] = "2026-06-01"
    spy, sent = _setup_send(monkeypatch, cache)
    assert gd.send_daily_message(final=True, today=MONDAY) is True
    assert sent["called"] is False


def test_weekend_is_skipped(monkeypatch):
    spy, sent = _setup_send(monkeypatch, _full_cache())
    saturday = date(2026, 6, 6)
    assert gd.send_daily_message(final=True, today=saturday) is True
    assert sent["called"] is False


def test_slack_failure_does_not_mark_sent(monkeypatch):
    spy, sent = _setup_send(monkeypatch, _full_cache(), slack_result=False)
    assert gd.send_daily_message(final=False, today=MONDAY) is False
    assert spy.saved is None                # not marked sent on failure
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_send_logic.py -q`
Expected: FAIL — `TypeError: send_daily_message() got an unexpected keyword argument 'final'`

- [ ] **Step 3: Replace `send_daily_message`** in `gablec_script/gablec_daily.py` with:

```python
def send_daily_message(final: bool = False, today: date | None = None) -> bool:
    """
    Send Slack message with today's menus from cache.

    Send #1 (final=False, ~08:00 local): posts only when ALL restaurants are
    ready; otherwise defers (returns True, no post).
    Send #2 (final=True, ~09:30 deadline): posts whatever is ready; if all are
    empty it skips and logs a warning. The cache 'sent_date' guard prevents
    double-posting.

    Returns False only on an actual Slack send failure.
    """
    now_local = datetime.now(TZ)
    today_local = today or now_local.date()
    today_str = today_local.isoformat()

    label = "Send #2 (deadline)" if final else "Send #1"
    print(f"=== {label} - {today_str} ===")
    print(f"Day: {CROATIAN_DAYS.get(today_local.weekday(), '')}")
    print("=" * 60)

    if today_local.weekday() >= 5:
        print("Weekend - skipping Slack message.")
        return True

    cache = load_cache()

    if cache.get("sent_date") == today_str:
        print(f"Already sent today ({today_str}) - skipping.")
        return True

    if not is_cache_valid_for_week(cache, today_local):
        print("WARNING: Cache is from a different week!")

    today_lunch = build_today_lunch(cache, today_local)
    ready_count = count_ready_restaurants(today_lunch)
    total = len(FACEBOOK_PAGES)

    print("\nMENU SUMMARY:")
    for name, info in today_lunch.items():
        status = f"{len(info['items'])} items" if info["items"] else "No menu"
        print(f"  {name}: {status}")
    print(f"Ready: {ready_count}/{total}")

    action = decide_send_action(ready_count, total, final, already_sent=False)

    if action == "defer":
        print(f"Only {ready_count}/{total} ready - deferring to the 09:30 deadline send.")
        return True
    if action == "skip_empty":
        print("WARNING: all restaurants empty at the deadline - not posting.")
        return True

    # action == "post"
    print("\n" + "=" * 60)
    print(f"Sending to Slack channel: {SLACK_CHANNEL}")
    success = send_to_slack(today_lunch, today_local)
    if success:
        cache["sent_date"] = today_str
        save_cache(cache)
    return success
```

Note: `already_sent` is checked directly above via the `sent_date` early return, so `decide_send_action` is called with `already_sent=False` here. The `skip_sent` branch of `decide_send_action` remains independently unit-tested in Task 2.

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `uv run --with pytest pytest -q`
Expected: PASS (18 passed)

- [ ] **Step 5: Commit**

```bash
git add gablec_script/gablec_daily.py tests/test_send_logic.py
git commit -m "$(cat <<'EOF'
feat: rework send_daily_message with sent_date guard and final mode

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Harden scrape against empty-list "cached" menus

If Gemini ever returns today's date with an empty list, the scrape currently treats it as cached and stops retrying. Treat an empty list as "not ready."

**Files:**
- Modify: `gablec_script/gablec_daily.py` (inside `scrape_and_process`, the cache-hit check)

- [ ] **Step 1: Locate the current check** in `scrape_and_process` (around the `found_cached` loop):

```python
        for cached_name, cached_data in cache.get("restaurants", {}).items():
            if cached_data.get("facebook_url") == page_url:
                # Check if we have today's menu
                if get_cached_menu_for_today(cache, cached_name, today_local) is not None:
                    print(f"[CACHED] {cached_name} - already have menu for today")
                    found_cached = True
                    break
```

- [ ] **Step 2: Replace the inner `if` so an empty list counts as not-cached**

```python
        for cached_name, cached_data in cache.get("restaurants", {}).items():
            if cached_data.get("facebook_url") == page_url:
                # A non-empty menu for today counts as cached; an empty list
                # means Gemini found nothing, so keep retrying this restaurant.
                if get_cached_menu_for_today(cache, cached_name, today_local):
                    print(f"[CACHED] {cached_name} - already have menu for today")
                    found_cached = True
                    break
```

- [ ] **Step 3: Verify the suite still passes** (no behavior covered here breaks existing tests)

Run: `uv run --with pytest pytest -q`
Expected: PASS (18 passed)

- [ ] **Step 4: Commit**

```bash
git add gablec_script/gablec_daily.py
git commit -m "$(cat <<'EOF'
fix: treat empty cached menu as not-ready so scrape keeps retrying

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Add `send-final` mode to the CLI

**Files:**
- Modify: `gablec_script/main.py`
- Modify: `gablec_script/gablec_daily.py` (its own `__main__` block, for parity)

- [ ] **Step 1: Update `main.py` argument choices** — change the `--mode` argument:

```python
    parser.add_argument(
        "--mode",
        choices=["scrape", "send", "send-final", "full"],
        default="full",
        help="Run mode: 'scrape' fetch/process, 'send' early send (all ready), "
             "'send-final' deadline send (partial ok), 'full' for both"
    )
```

- [ ] **Step 2: Update the Slack-token requirement check in `main.py`**

Find:
```python
    if args.mode in ["send", "full"]:
        if not slack_bot_token:
            missing.append("SLACK_BOT_TOKEN")
```
Replace with:
```python
    if args.mode in ["send", "send-final", "full"]:
        if not slack_bot_token:
            missing.append("SLACK_BOT_TOKEN")
```

- [ ] **Step 3: Update the dispatch block in `main.py`**

Find the `elif args.mode == "send":` branch and add a `send-final` branch before the `else` (full). Replace:
```python
        elif args.mode == "send":
            success = send_daily_message()
            if success:
                print("\n" + "=" * 60)
                print("SUCCESS! Lunch menus posted to Slack.")
                print("=" * 60)
                sys.exit(0)
            else:
                print("\n" + "=" * 60)
                print("FAILED: Could not post to Slack.")
                print("=" * 60)
                sys.exit(1)
```
with:
```python
        elif args.mode in ("send", "send-final"):
            success = send_daily_message(final=(args.mode == "send-final"))
            if success:
                print("\n" + "=" * 60)
                print("Send phase complete.")
                print("=" * 60)
                sys.exit(0)
            else:
                print("\n" + "=" * 60)
                print("FAILED: Could not post to Slack.")
                print("=" * 60)
                sys.exit(1)
```

- [ ] **Step 4: Update `gablec_daily.py`'s own `__main__`** for parity. Find:
```python
    parser.add_argument(
        "--mode",
        choices=["scrape", "send", "full"],
        default="full",
        help="Run mode: 'scrape' for fetching/processing, 'send' for Slack message, 'full' for both"
    )
```
Replace with:
```python
    parser.add_argument(
        "--mode",
        choices=["scrape", "send", "send-final", "full"],
        default="full",
        help="Run mode: 'scrape', 'send' (early), 'send-final' (deadline), 'full'"
    )
```
Then find:
```python
        elif args.mode == "send":
            success = send_daily_message()
            sys.exit(0 if success else 1)
```
Replace with:
```python
        elif args.mode in ("send", "send-final"):
            success = send_daily_message(final=(args.mode == "send-final"))
            sys.exit(0 if success else 1)
```

- [ ] **Step 5: Smoke-test the CLI parses the new mode** (no network — it will fail fast on missing real tokens, which is fine; we only check argparse accepts it)

Run: `cd gablec_script && APIFY_TOKEN=x GOOGLE_API_KEY=x SLACK_BOT_TOKEN=x uv run --project .. python main.py --mode send-final --help; cd ..`
Expected: help text prints listing `send-final` as a valid choice (exit 0).

- [ ] **Step 6: Commit**

```bash
git add gablec_script/main.py gablec_script/gablec_daily.py
git commit -m "$(cat <<'EOF'
feat: add send-final CLI mode for the deadline send

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Track `menu_cache.json` in the repo

**Files:**
- Modify: `.gitignore`
- Add: `menu_cache.json` (the current working-tree file already holds this week's freshly-scraped data)

- [ ] **Step 1: Remove the `menu_cache.json` entry from `.gitignore`**

Find these two trailing lines and delete them:
```
# Cache file (generated by bot, cached by GitHub Actions)
menu_cache.json
```

- [ ] **Step 2: Confirm the working-tree cache is this week's data**

Run: `python -c "import json;c=json.load(open('menu_cache.json'));print('week_start',c['week_start']);print('restaurants',list(c['restaurants']))"`
Expected: `week_start 2026-06-01` and the three restaurant names. (If it shows an older week, run `cd gablec_script && uv run --project .. python main.py --mode scrape` first to repopulate, then `cd ..`.)

- [ ] **Step 3: Track and commit**

```bash
git add .gitignore menu_cache.json
git commit -m "$(cat <<'EOF'
chore: track menu_cache.json in repo for cross-run state

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Rewrite the GitHub Actions workflow

**Files:**
- Modify: `.github/workflows/daily.yml` (full replacement)

- [ ] **Step 1: Replace the entire file contents** of `.github/workflows/daily.yml` with:

```yaml
name: daily-lunch-bot

on:
  schedule:
    # Times are UTC. Zagreb summer = UTC+2 (winter UTC+1, so one hour earlier).
    - cron: "30 4 * * 1-5"   # ~06:30 local - scrape
    - cron: "0 5 * * 1-5"    # ~07:00 local - scrape
    - cron: "30 5 * * 1-5"   # ~07:30 local - scrape
    - cron: "0 6 * * 1-5"    # ~08:00 local - SEND #1 (post if all ready)
    - cron: "30 6 * * 1-5"   # ~08:30 local - scrape
    - cron: "0 7 * * 1-5"    # ~09:00 local - scrape
    - cron: "30 7 * * 1-5"   # ~09:30 local - SEND #2 (deadline, partial ok)
  workflow_dispatch:
    inputs:
      mode:
        description: 'Run mode'
        required: true
        default: 'full'
        type: choice
        options:
          - scrape
          - send
          - send-final
          - full

permissions:
  contents: write

concurrency:
  group: gablec-bot
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    env:
      APIFY_TOKEN: ${{ secrets.APIFY_TOKEN }}
      GOOGLE_API_KEY: ${{ secrets.GOOGLE_API_KEY }}
      SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
      SLACK_CHANNEL: ${{ secrets.SLACK_CHANNEL }}

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Install dependencies
        run: |
          pip install uv
          uv pip install --system --no-cache .

      - name: Determine run mode
        id: mode
        run: |
          if [ "${{ github.event_name }}" == "workflow_dispatch" ]; then
            echo "mode=${{ inputs.mode }}" >> "$GITHUB_OUTPUT"
          else
            case "${{ github.event.schedule }}" in
              "0 6 * * 1-5")  echo "mode=send" >> "$GITHUB_OUTPUT" ;;
              "30 7 * * 1-5") echo "mode=send-final" >> "$GITHUB_OUTPUT" ;;
              *)              echo "mode=scrape" >> "$GITHUB_OUTPUT" ;;
            esac
          fi
          echo "Detected mode: $(grep mode "$GITHUB_OUTPUT" | cut -d= -f2)"

      - name: Run lunch bot
        run: python main.py --mode ${{ steps.mode.outputs.mode }}
        working-directory: gablec_script

      - name: Commit updated cache
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          if git diff --quiet -- menu_cache.json; then
            echo "No cache changes to commit."
          else
            git pull --rebase --autostash origin "${{ github.ref_name }}" || true
            git add menu_cache.json
            git commit -m "chore: update menu cache [skip ci]"
            git push
          fi
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/daily.yml')); print('YAML OK')"`
Expected: `YAML OK`
(If pyyaml is missing: `uv run --with pyyaml python -c "..."`.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/daily.yml
git commit -m "$(cat <<'EOF'
ci: morning scrape retries, split send phases, commit cache to repo

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Full verification and integration check

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run: `uv run --with pytest pytest -q`
Expected: PASS (18 passed)

- [ ] **Step 2: Live end-to-end dry run of the send decision** using the real (committed) cache, without sending, by forcing the early-return path. Run a local `send` and confirm it posts (this week's cache is populated, so Send #1 should post and set `sent_date`):

Run: `cd gablec_script && uv run --project .. python main.py --mode send; cd ..`
Expected: prints `Ready: 3/3`, posts to Slack, prints `Send phase complete.` Then `menu_cache.json` gains `"sent_date": "<today>"`.

> NOTE: this performs a REAL Slack post to the configured channel. Only run if a real post is acceptable; otherwise skip and rely on the unit tests. If skipped, say so.

- [ ] **Step 3: Confirm re-running `send` is now a no-op** (sent_date guard)

Run: `cd gablec_script && uv run --project .. python main.py --mode send; cd ..`
Expected: prints `Already sent today (...) - skipping.` and does NOT post again.

- [ ] **Step 4: Reset the test `sent_date` if Step 2/3 were run** so production starts clean (only if today is a weekday the bot should still post):

Run: `python -c "import json;c=json.load(open('menu_cache.json'));c.pop('sent_date',None);json.dump(c,open('menu_cache.json','w'),ensure_ascii=False,indent=2);print('sent_date cleared')"`
Then commit if changed: `git add menu_cache.json && git commit -m "chore: clear test sent_date" || true`

- [ ] **Step 5: Push the branch and open a PR**

```bash
git push -u origin fix/decouple-scrape-send
gh pr create --fill --title "Resilient scrape/send: morning retries + commit-to-repo cache"
```

- [ ] **Step 6: Note for after merge** — scheduled crons only run on the **default branch**. Until merged to `master`, test the workflow with `gh workflow run daily.yml -f mode=scrape` (and `-f mode=send-final`) against the branch via the Actions UI / dispatch. Verify the "Commit updated cache" step pushes successfully (confirms `contents: write` works).

---

## Self-Review

**Spec coverage:**
- Schedule (6 scrapes + 2 sends, mode mapping) → Task 8 ✓
- Send #1 all-ready / Send #2 partial-or-skip-empty → Tasks 2, 4 ✓
- `sent_date` double-post guard → Task 4 ✓
- Commit cache to repo + un-ignore → Tasks 7, 8 ✓
- `permissions: contents: write`, `concurrency`, pull-rebase, change-guard → Task 8 ✓
- `send-final` mode wiring → Task 6 ✓
- Empty-list hardening edge case → Task 5 ✓
- Testing (sent_date guard, send #1/#2, ready_count, empty-as-not-ready) → Tasks 2–4 ✓
- NOT doing: within-run retry, DST → respected (no tasks) ✓
- Out of scope: secret rotation, Node-20 warning → intentionally excluded ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every command shows expected output. ✓

**Type/name consistency:** `decide_send_action(ready_count, total, final, already_sent)`, `build_today_lunch(cache, today_date)`, `count_ready_restaurants(today_lunch)`, `send_daily_message(final=False, today=None)` — names/signatures consistent across Tasks 2, 3, 4, 6 and tests. Cache key `sent_date` consistent across Tasks 4, 9. Workflow cron strings in Task 8 match the mode-mapping `case`. ✓

**Note on test count:** running totals (8 → 11 → 18) assume tests are added cumulatively as written; the absolute number matters less than all-passing.
