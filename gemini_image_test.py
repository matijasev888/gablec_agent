"""Real end-to-end test: run EACH fallback model through the actual
menu-extraction code path on freshly-scraped Facebook posts WITH images.

It reuses gablec_daily.ask_gemini_for_weekly_menu unchanged (so the real
prompt, image inlining, and JSON parsing are exercised) but forces a single
model per call by wrapping the genai generate_content method.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path("gablec_script/.env"))
sys.path.insert(0, str(Path("gablec_script").resolve()))

import gablec_daily as gd  # noqa: E402

MODELS = ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]

today = gd.datetime.now(gd.TZ).date()
since = gd.get_week_start(today)
print(f"Today={today}  scraping posts since week start {since}\n")

# 1) Scrape real posts (with images) until we find a page that has some.
sample_posts, sample_name = None, None
for url in gd.FACEBOOK_PAGES:
    print(f"Scraping {url} ...")
    posts = gd.fetch_facebook_posts(url, since, retries=2, retry_delay=5)
    n_imgs = sum(len(p.get("images", [])) for p in posts)
    print(f"  -> {len(posts)} posts, {n_imgs} images")
    if posts and n_imgs > 0 and sample_posts is None:
        sample_posts, sample_name = posts, url
        # keep scanning the rest just for visibility, but we have our sample

if not sample_posts:
    print("\nNo posts with images found right now (restaurants may not have "
          "posted this week's menu yet). Re-run later.")
    sys.exit(0)

print(f"\nUsing posts from {sample_name} for the per-model image test.")
n_imgs = sum(len(p.get('images', [])) for p in sample_posts)
print(f"Sample: {len(sample_posts)} posts, {n_imgs} images\n")

# 2) Force each model through the REAL extraction function.
orig = gd.client_gemini.models.generate_content

def forced(model_name):
    def _call(model, contents, **kw):
        return orig(model=model_name, contents=contents, **kw)
    return _call

print("=" * 70)
for m in MODELS:
    gd.client_gemini.models.generate_content = forced(m)
    print(f"\n### {m}")
    try:
        result = gd.ask_gemini_for_weekly_menu(sample_name, sample_posts, today)
        mt = result.get("menu_type")
        menus = result.get("menus", {})
        total_items = sum(len(v) for v in menus.values())
        verdict = "PASS" if mt in ("weekly", "daily") and total_items > 0 else \
                  "OK (model worked, but found no menu)" if mt == "none" else "?"
        print(f"  -> menu_type={mt}, days={len(menus)}, items={total_items}  [{verdict}]")
        for day, items in sorted(menus.items()):
            print(f"     {day}: {items[:6]}")
    except Exception as e:
        print(f"  -> EXCEPTION {type(e).__name__}: {str(e)[:120]}")

gd.client_gemini.models.generate_content = orig
print("\n" + "=" * 70)
print("Done. PASS = model read the real posts/images and returned menu JSON.")
