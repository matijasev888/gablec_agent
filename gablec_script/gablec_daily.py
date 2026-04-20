import os
import json
import sys
import time
import httpx
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from apify_client import ApifyClient
from google import genai
from google.genai.errors import ClientError, ServerError
from pathlib import Path
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#ponuda_gableca")

client_apify = ApifyClient(APIFY_TOKEN)
client_gemini = genai.Client(api_key=GOOGLE_API_KEY)

# Restaurant Facebook pages (removed Punkt - they don't post regular lunch menus)
FACEBOOK_PAGES = [
    "https://www.facebook.com/p/Restoran-Catering-Zaboky-100063838081316/",
    "https://www.facebook.com/p/Restaurant-Gra%C5%A1o-100055053834186/",
    "https://www.facebook.com/mondozabok/",
]

TZ = ZoneInfo("Europe/Zagreb")

# Croatian day names for nicer formatting
CROATIAN_DAYS = {
    0: "Ponedjeljak", 1: "Utorak", 2: "Srijeda", 
    3: "Četvrtak", 4: "Petak", 5: "Subota", 6: "Nedjelja"
}

# Cache file path - stored in workspace root for GitHub Actions cache
CACHE_FILE = Path(__file__).parent.parent / "menu_cache.json"


def get_week_start(d: date) -> date:
    """Get Monday of the week for a given date."""
    return d - timedelta(days=d.weekday())


def load_cache() -> dict:
    """Load cache from file, return empty cache if file doesn't exist or is invalid."""
    if not CACHE_FILE.exists():
        return {"week_start": None, "restaurants": {}}
    
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
            return cache
    except (json.JSONDecodeError, IOError):
        return {"week_start": None, "restaurants": {}}


def save_cache(cache: dict):
    """Save cache to file."""
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    print(f"Cache saved to {CACHE_FILE}")


def is_cache_valid_for_week(cache: dict, today: date) -> bool:
    """Check if cache is from the current week."""
    if not cache.get("week_start"):
        return False
    
    cache_week = date.fromisoformat(cache["week_start"])
    current_week = get_week_start(today)
    return cache_week == current_week


def get_cached_menu_for_today(cache: dict, restaurant_name: str, today: date) -> list | None:
    """Get cached menu for a restaurant for today, return None if not cached."""
    today_str = today.isoformat()
    restaurant_cache = cache.get("restaurants", {}).get(restaurant_name, {})
    menus = restaurant_cache.get("menus", {})
    return menus.get(today_str)


def download_all_images(media: list) -> list:
    """Download images from Facebook media attachments."""
    if not media:
        return []
    
    images = []
    for item in media:
        url = None
        if isinstance(item, dict):
            url = (item.get("photo_image") or {}).get("uri") or item.get("url") or item.get("thumbnail")
        if not url:
            continue
        try:
            r = httpx.get(url, timeout=10)
            if r.status_code == 200:
                content_type = r.headers.get("content-type", "")
                mime = content_type.split(";")[0].strip()
                if not mime:
                    mime = "image/jpeg"
                images.append({
                    "bytes": r.content,
                    "mime": mime
                })
        except Exception:
            continue
    
    # Limit to 2 images per post to reduce token usage
    return images[:2]


def to_local(dt_iso_utc: str) -> datetime:
    """Convert ISO UTC timestamp to local Zagreb time."""
    dt = datetime.fromisoformat(dt_iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(TZ)


def ask_gemini_for_weekly_menu(page_name: str, posts_data: list, today_date: date, skip_images: bool = False) -> dict:
    """
    Use Gemini AI to analyze posts and extract the FULL WEEKLY menu.
    Returns a dict with menus for each day of the week if found.
    """
    if not posts_data:
        return {"menu_type": "none", "menus": {}}
    
    croatian_day = CROATIAN_DAYS.get(today_date.weekday(), today_date.strftime('%A'))
    
    # Calculate the dates for this week (Mon-Fri)
    week_start = get_week_start(today_date)
    week_dates = {
        CROATIAN_DAYS[i]: (week_start + timedelta(days=i)).isoformat()
        for i in range(5)  # Mon-Fri
    }
    
    parts = [
        {"text": (
            f"Koristi samo hrvatski jezik. "
            f"Današnji datum je {today_date.isoformat()} ({croatian_day}). "
            f"Datumi ovog tjedna: Ponedjeljak={week_dates['Ponedjeljak']}, Utorak={week_dates['Utorak']}, "
            f"Srijeda={week_dates['Srijeda']}, Četvrtak={week_dates['Četvrtak']}, Petak={week_dates['Petak']}.\n\n"
            f"Analiziraj PAŽLJIVO sve objave za restoran '{page_name}'. "
            "Pronađi dnevne menije/gablece za OVAJ TJEDAN. "
            "Ako je objavljen TJEDNI MENI, izvuci stavke za SVAKI dan posebno. "
            "Ako je samo dnevni meni, izvuci ga za taj dan. "
            "Pročitaj slike ako sadrže meni. "
            "Ako imaš cijene, dodaj ih. "
            "Format stavke: naziv jela (cijena ako postoji). "
            "\n\nVrati JSON u formatu:\n"
            "{\n"
            '  "menu_type": "weekly" ili "daily" ili "none",\n'
            '  "menus": {\n'
            '    "YYYY-MM-DD": ["jelo1", "jelo2", ...],\n'
            '    "YYYY-MM-DD": ["jelo1", "jelo2", ...]\n'
            "  }\n"
            "}\n\n"
            "Koristi TOČNE datume iz gornjeg popisa. "
            "Ako nema menija za neki dan, ne uključuj taj datum u 'menus'.\n\n"
        )}
    ]
    
    has_images = False
    for idx, post in enumerate(posts_data, 1):
        parts.append({"text": f"\n--- Objava {idx} (objavljena: {post['posted_at_local']}) ---"})
        
        if post['text']:
            parts.append({"text": f"Tekst: {post['text']}"})
        else:
            parts.append({"text": "Tekst: (nema teksta)"})
        
        if post['images'] and not skip_images:
            has_images = True
            parts.append({"text": f"Slike ({len(post['images'])} komada):"})
            for img in post['images']:
                parts.append({"inline_data": {"mime_type": img["mime"], "data": img["bytes"]}})
        else:
            parts.append({"text": "(Nema slika)"})
    
    # Retry logic for rate limiting (429 errors) and image errors
    max_retries = 3
    resp = None
    for attempt in range(max_retries):
        try:
            resp = client_gemini.models.generate_content(
                model="gemini-2.0-flash",
                contents=[{"role": "user", "parts": parts}],
            )
            break
        except ServerError as e:
            if attempt < max_retries - 1:
                wait_time = 60 * (attempt + 1)
                print(f"Server error ({e}), waiting {wait_time}s before retry (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            raise
        except ClientError as e:
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            if is_rate_limit and attempt < max_retries - 1:
                wait_time = 60 * (attempt + 1)
                print(f"Rate limited, waiting {wait_time}s before retry (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            is_image_error = "400" in error_str and "INVALID_ARGUMENT" in error_str
            if is_image_error and has_images and not skip_images:
                print(f"Image processing failed for {page_name}, retrying without images...")
                return ask_gemini_for_weekly_menu(page_name, posts_data, today_date, skip_images=True)
            raise
    
    if resp is None:
        return {"menu_type": "none", "menus": {}}
    
    txt = (resp.text or "").strip()
    
    # Remove markdown code block if present
    if txt.startswith("```"):
        lines = txt.split('\n')
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        txt = '\n'.join(lines).strip()
    
    try:
        j = json.loads(txt)
        if isinstance(j, dict) and "menu_type" in j and "menus" in j:
            return j
    except json.JSONDecodeError:
        print(f"Failed to parse Gemini response for {page_name}: {txt[:200]}")
    
    return {"menu_type": "none", "menus": {}}


def build_slack_blocks(today_lunch: dict, today_date: date) -> list:
    """Build rich Slack Block Kit message."""
    croatian_day = CROATIAN_DAYS.get(today_date.weekday(), "")
    
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Gableci za {croatian_day.lower()} ({today_date.strftime('%d.%m.%Y.')})",
                "emoji": True
            }
        },
        {"type": "divider"}
    ]
    
    for name, info in today_lunch.items():
        if info["items"]:
            menu_text = "\n".join([f"• {item}" for item in info["items"]])
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{name}*\n{menu_text}"
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Facebook",
                        "emoji": True
                    },
                    "url": info["facebook_url"],
                    "action_id": f"fb-{name[:20]}"
                }
            })
        else:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{name}*\n_Nema objave za danas_"
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Facebook",
                        "emoji": True
                    },
                    "url": info["facebook_url"],
                    "action_id": f"fb-{name[:20]}"
                }
            })
        
        blocks.append({"type": "divider"})
    
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": "Podaci prikupljeni s Facebooka pomoću AI analize"
            }
        ]
    })
    
    return blocks


def build_fallback_text(today_lunch: dict, today_date: date) -> str:
    """Build plain text fallback for notifications."""
    croatian_day = CROATIAN_DAYS.get(today_date.weekday(), "")
    lines = [f"Gableci za {croatian_day.lower()} ({today_date.strftime('%d.%m.%Y.')}):\n"]
    
    for name, info in today_lunch.items():
        if info["items"]:
            lines.append(f"{name}:")
            for item in info["items"]:
                lines.append(f"  • {item}")
            lines.append("")
        else:
            lines.append(f"{name}: Nema objave za danas\n")
    
    return "\n".join(lines)


def send_to_slack(today_lunch: dict, today_date: date, max_retries: int = 3) -> bool:
    """Send formatted message to Slack with retry logic."""
    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    
    blocks = build_slack_blocks(today_lunch, today_date)
    fallback_text = build_fallback_text(today_lunch, today_date)
    
    for attempt in range(1, max_retries + 1):
        try:
            slack_client.chat_postMessage(
                channel=SLACK_CHANNEL,
                text=fallback_text,
                blocks=blocks,
                unfurl_links=False,
                unfurl_media=False
            )
            print(f"Message sent to {SLACK_CHANNEL} successfully!")
            return True
        except SlackApiError as e:
            print(f"Slack API error (attempt {attempt}/{max_retries}): {e.response['error']}")
            if attempt == max_retries:
                return False
        except Exception as e:
            print(f"Unexpected error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                return False
    
    return False


def fetch_facebook_posts(page_url: str, since_date: date) -> list:
    """Fetch posts from a Facebook page using Apify."""
    try:
        run = client_apify.actor("apify/facebook-posts-scraper").call(run_input={
            "startUrls": [{"url": page_url}],
            "proxy": {"apifyProxyGroups": ["RESIDENTIAL"]},
            "maxRequestRetries": 10,
            "onlyPostsNewerThan": since_date.isoformat(),
            "resultsLimit": 10
        })
        
        dataset = client_apify.dataset(run["defaultDatasetId"])
        page_out = []
        
        for item in dataset.iterate_items():
            page_name = item.get("user", {}).get("name")
            text = item.get("text")
            post_url = item.get("topLevelUrl") or item.get("url") or item.get("facebookUrl")
            posted_local = to_local(item.get("time"))
            images = download_all_images(item.get("media", []))
            
            page_out.append({
                "page_name": page_name,
                "text": text,
                "posted_at_local": posted_local.isoformat(),
                "post_url": post_url,
                "images": images
            })
        
        page_out.sort(key=lambda x: x["posted_at_local"], reverse=True)
        return page_out
        
    except Exception as e:
        print(f"Error fetching {page_url}: {e}")
        return []


def scrape_and_process():
    """
    Phase 1: Scrape Facebook and process with Gemini.
    Only scrapes restaurants that don't have today's menu cached.
    Run at 7:00 Zagreb time.
    """
    now_local = datetime.now(TZ)
    today_local = now_local.date()
    today_str = today_local.isoformat()
    
    print(f"=== SCRAPE & PROCESS - {today_local.isoformat()} ===")
    print(f"Day: {CROATIAN_DAYS.get(today_local.weekday(), '')}")
    print("=" * 60)
    
    # Check if it's weekend
    if today_local.weekday() >= 5:
        print("Weekend - skipping scrape.")
        return
    
    # Load cache
    cache = load_cache()
    
    # Check if it's a new week (Monday) - clear cache
    if today_local.weekday() == 0:  # Monday
        if not is_cache_valid_for_week(cache, today_local):
            print("New week started - clearing cache")
            cache = {
                "week_start": get_week_start(today_local).isoformat(),
                "restaurants": {}
            }
    
    # Ensure week_start is set
    if not cache.get("week_start"):
        cache["week_start"] = get_week_start(today_local).isoformat()
    
    # Look back 4 days for posts (catches weekend posts for Monday)
    since_date = today_local - timedelta(days=4)
    
    # Process each restaurant
    restaurants_to_process = []
    for page_url in FACEBOOK_PAGES:
        # Get display name from URL for checking cache
        url_name = page_url.rstrip('/').split('/')[-1]
        
        # Check all cached restaurants to find matching one
        found_cached = False
        for cached_name, cached_data in cache.get("restaurants", {}).items():
            if cached_data.get("facebook_url") == page_url:
                # Check if we have today's menu
                if get_cached_menu_for_today(cache, cached_name, today_local) is not None:
                    print(f"[CACHED] {cached_name} - already have menu for today")
                    found_cached = True
                    break
        
        if not found_cached:
            restaurants_to_process.append(page_url)
    
    if not restaurants_to_process:
        print("\nAll restaurants have menus cached for today!")
        save_cache(cache)
        return
    
    print(f"\nRestaurants to process: {len(restaurants_to_process)}")
    
    # Fetch and process each restaurant
    for idx, page_url in enumerate(restaurants_to_process):
        # Add delay between API calls (skip first)
        if idx > 0:
            print("Waiting 30s to avoid rate limiting...")
            time.sleep(30)
        
        print(f"\n{'='*40}")
        print(f"Fetching: {page_url}")
        
        posts = fetch_facebook_posts(page_url, since_date)
        
        if not posts:
            print(f"No posts found for {page_url}")
            continue
        
        display_name = posts[0]["page_name"] if posts and posts[0]["page_name"] else page_url.split("/")[-2]
        print(f"Restaurant: {display_name}")
        print(f"Posts found: {len(posts)}")
        
        # Call Gemini to extract weekly menu
        print(f"Analyzing with Gemini...")
        result = ask_gemini_for_weekly_menu(display_name, posts, today_local)
        
        print(f"Menu type: {result.get('menu_type', 'none')}")
        print(f"Days with menus: {list(result.get('menus', {}).keys())}")
        
        # Update cache and save after each restaurant so partial progress isn't lost
        cache["restaurants"][display_name] = {
            "facebook_url": page_url,
            "last_scrape": now_local.isoformat(),
            "menu_type": result.get("menu_type", "none"),
            "menus": result.get("menus", {})
        }
        save_cache(cache)
    
    print("\n" + "=" * 60)
    print("SCRAPE & PROCESS COMPLETE")
    print("=" * 60)


def send_daily_message():
    """
    Phase 2: Send Slack message with today's menus from cache.
    Run at 8:00 Zagreb time.
    """
    now_local = datetime.now(TZ)
    today_local = now_local.date()
    today_str = today_local.isoformat()
    
    print(f"=== SEND SLACK MESSAGE - {today_local.isoformat()} ===")
    print(f"Day: {CROATIAN_DAYS.get(today_local.weekday(), '')}")
    print("=" * 60)
    
    # Check if it's weekend
    if today_local.weekday() >= 5:
        print("Weekend - skipping Slack message.")
        return True
    
    # Load cache
    cache = load_cache()
    
    if not is_cache_valid_for_week(cache, today_local):
        print("WARNING: Cache is from a different week!")
    
    # Build today's lunch menu from cache
    today_lunch = {}
    
    for page_url in FACEBOOK_PAGES:
        # Find restaurant in cache
        found = False
        for restaurant_name, restaurant_data in cache.get("restaurants", {}).items():
            if restaurant_data.get("facebook_url") == page_url:
                menus = restaurant_data.get("menus", {})
                today_menu = menus.get(today_str, [])
                
                today_lunch[restaurant_name] = {
                    "restaurant": restaurant_name,
                    "items": today_menu,
                    "facebook_url": page_url
                }
                found = True
                break
        
        if not found:
            # Restaurant not in cache - show as missing
            url_name = page_url.rstrip('/').split('/')[-1]
            today_lunch[url_name] = {
                "restaurant": url_name,
                "items": [],
                "facebook_url": page_url
            }
    
    # Print summary
    print("\nMENU SUMMARY:")
    for name, info in today_lunch.items():
        status = f"{len(info['items'])} items" if info["items"] else "No menu"
        print(f"  {name}: {status}")
    
    # Send to Slack
    print("\n" + "=" * 60)
    print(f"Sending to Slack channel: {SLACK_CHANNEL}")
    
    return send_to_slack(today_lunch, today_local)


def main():
    """
    Main entry point - runs both phases (for backward compatibility and manual runs).
    """
    scrape_and_process()
    return send_daily_message()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Gablec Daily Bot")
    parser.add_argument(
        "--mode",
        choices=["scrape", "send", "full"],
        default="full",
        help="Run mode: 'scrape' for fetching/processing, 'send' for Slack message, 'full' for both"
    )
    args = parser.parse_args()
    
    try:
        if args.mode == "scrape":
            scrape_and_process()
            sys.exit(0)
        elif args.mode == "send":
            success = send_daily_message()
            sys.exit(0 if success else 1)
        else:  # full
            success = main()
            sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
