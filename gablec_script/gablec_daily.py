import os
import json
import sys
import time
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apify_client import ApifyClient
from google import genai
from google.genai.errors import ClientError
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

FACEBOOK_PAGES = [
    "https://www.facebook.com/p/Restoran-Catering-Zaboky-100063838081316/",
    "https://www.facebook.com/p/Restaurant-Gra%C5%A1o-100055053834186/",
    "https://www.facebook.com/mondozabok/",
    "https://www.facebook.com/punktbeerhouse"
]

TZ = ZoneInfo("Europe/Zagreb")

# Croatian day names for nicer formatting
CROATIAN_DAYS = {
    0: "Ponedjeljak", 1: "Utorak", 2: "Srijeda", 
    3: "Četvrtak", 4: "Petak", 5: "Subota", 6: "Nedjelja"
}


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
                # Detect mime type from content-type header, with fallback
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
    
    return images


def to_local(dt_iso_utc: str) -> datetime:
    """Convert ISO UTC timestamp to local Zagreb time."""
    dt = datetime.fromisoformat(dt_iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(TZ)


def ask_gemini_for_all_posts(page_name: str, posts_data: list, today_date, skip_images: bool = False) -> dict:
    """Use Gemini AI to analyze posts and extract today's menu."""
    if not posts_data:
        return {"has_today": False, "items": []}
    
    croatian_day = CROATIAN_DAYS.get(today_date.weekday(), today_date.strftime('%A'))
    
    parts = [
        {"text": (
            f"Koristi samo hrvatski jezik. "
            f"Današnji datum je {today_date.isoformat()} ({croatian_day}). "
            f"Analiziraj PAŽLJIVO sve objave za restoran '{page_name}'. "
            "Objave mogu biti: tjedni meniji, dnevne ponude, ili specijalne ponude. "
            "Pronađi gablec/ručak za DANAŠNJI dan. "
            "Ako objava ima sliku tjednog menija, pročitaj je i izdvoji samo današnji dan. "
            "Ako imaš cijene, dodaj ih. "
            "Format stavke: naziv jela (cijena ako postoji). "
            "Vrati JSON: {{\"has_today\": bool, \"items\": [string]}}. "
            "Ako STVARNO nema ponude za današnji dan, vrati has_today=false i items=[].\n\n"
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
                model="gemini-2.5-flash",
                contents=[{"role": "user", "parts": parts}],
            )
            break
        except ClientError as e:
            error_str = str(e)
            # Check for rate limit error (429)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            if is_rate_limit and attempt < max_retries - 1:
                wait_time = 60 * (attempt + 1)  # 60s, 120s, 180s
                print(f"Rate limited, waiting {wait_time}s before retry (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            # Check for invalid image error (400) - retry without images
            is_image_error = "400" in error_str and "INVALID_ARGUMENT" in error_str
            if is_image_error and has_images and not skip_images:
                print(f"Image processing failed for {page_name}, retrying without images...")
                return ask_gemini_for_all_posts(page_name, posts_data, today_date, skip_images=True)
            raise
    
    if resp is None:
        return {"has_today": False, "items": []}
    
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
        if isinstance(j, dict) and "has_today" in j and "items" in j:
            return j
    except json.JSONDecodeError:
        print(f"Failed to parse Gemini response for {page_name}")
    
    return {"has_today": False, "items": []}


def build_slack_blocks(today_lunch: dict, today_date) -> list:
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
            # Restaurant with menu
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
            # Restaurant without menu
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
    
    # Footer
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"Podaci prikupljeni s Facebooka pomoću AI analize"
            }
        ]
    })
    
    return blocks


def build_fallback_text(today_lunch: dict, today_date) -> str:
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


def send_to_slack(today_lunch: dict, today_date, max_retries: int = 3) -> bool:
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


def fetch_facebook_posts(page_url: str, since_date) -> list:
    """Fetch posts from a Facebook page using Apify."""
    try:
        run = client_apify.actor("apify/facebook-posts-scraper").call(run_input={
            "startUrls": [{"url": page_url}],
            "proxy": {"apifyProxyGroups": ["RESIDENTIAL"]},
            "maxRequestRetries": 10,
            "onlyPostsNewerThan": since_date.isoformat(),
            "resultsLimit": 15
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


def main():
    """Main entry point for the lunch menu bot."""
    now_local = datetime.now(TZ)
    today_local = now_local.date()
    
    # Calculate date for filtering posts (last 7 days to catch weekly menus)
    since_date = today_local - timedelta(days=7)
    
    print(f"Daily Lunch Menu Bot - {today_local.isoformat()}")
    print(f"Day: {CROATIAN_DAYS.get(today_local.weekday(), '')}")
    print("=" * 60)
    
    # Check if it's weekend
    if today_local.weekday() >= 5:
        print("Weekend - most restaurants don't have lunch menus.")
        print("Running anyway in case some do...")
    
    # Fetch posts from all Facebook pages
    results = {}
    for page_url in FACEBOOK_PAGES:
        print(f"\nFetching: {page_url}")
        results[page_url] = fetch_facebook_posts(page_url, since_date)
    
    # Process posts with Gemini AI (with delays to avoid rate limiting)
    today_lunch = {}
    for idx, (page_url, posts) in enumerate(results.items()):
        # Add delay between API calls to stay under rate limit (skip first)
        if idx > 0:
            print("Waiting 20s to avoid rate limiting...")
            time.sleep(20)
        
        display_name = posts[0]["page_name"] if posts and posts[0]["page_name"] else page_url.split("/")[-2]
        
        print(f"\nAnalyzing {display_name}...")
        pj = ask_gemini_for_all_posts(display_name, posts, today_local)
        
        today_lunch[display_name] = {
            "restaurant": display_name,
            "items": pj.get("items", []) if pj.get("has_today") else [],
            "facebook_url": page_url
        }
    
    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY:")
    for name, info in today_lunch.items():
        status = f"{len(info['items'])} items" if info["items"] else "No menu"
        print(f"  {name}: {status}")
    
    # Send to Slack
    print("\n" + "=" * 60)
    print(f"Sending to Slack channel: {SLACK_CHANNEL}")
    
    return send_to_slack(today_lunch, today_local)

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
