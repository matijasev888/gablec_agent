import pytest
import gablec_daily as gd
from datetime import date


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
