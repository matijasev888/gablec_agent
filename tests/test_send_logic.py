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
