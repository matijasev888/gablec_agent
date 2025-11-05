import os
import json
import sys
import httpx
import calendar
import mimetypes
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from apify_client import ApifyClient
from google import genai
from pathlib import Path
from dotenv import load_dotenv
from slack_sdk import WebClient



env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

APIFY_TOKEN = os.getenv("APIFY_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECIPIENTS = os.getenv("EMAIL_RECIPIENTS", "").split(",")
EMAIL_RECIPIENTS = [r.strip() for r in EMAIL_RECIPIENTS if r.strip()]
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

client_apify = ApifyClient(APIFY_TOKEN)
client_gemini = genai.Client(api_key=GOOGLE_API_KEY)

facebook_pages = [
    "https://www.facebook.com/p/Restoran-Catering-Zaboky-100063838081316/",
    "https://www.facebook.com/p/Restaurant-Gra%C5%A1o-100055053834186/",
    "https://www.facebook.com/mondozabok/",
    "https://www.facebook.com/punktbeerhouse"
]

tz = ZoneInfo("Europe/Zagreb")
now_local = datetime.now(tz)
today_local = now_local.date()
first_weekday = calendar.Calendar().getfirstweekday()
offset = (today_local.weekday() - first_weekday) % 7
pom_day = today_local - timedelta(days=offset + 3)


def download_all_images(media):
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
                mime, _ = mimetypes.guess_type(url)
                images.append({
                    "bytes": r.content,
                    "mime": mime or "image/jpeg"
                })
        except Exception:
            continue
    
    return images


def to_local(dt_iso_utc):
    dt = datetime.fromisoformat(dt_iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(tz)


def ask_gemini_for_all_posts(page_name, posts_data):
    if not posts_data:
        return {"has_today": False, "items": []}
    
    parts = [
        {"text": (
            f"Koristi samo hrvatski jezik. "
            f"Današnji datum je {today_local.isoformat()} (dan u tjednu: {today_local.strftime('%A')}). "
            f"Analiziraj PAŽLJIVO sve objave za restoran '{page_name}'. "
            "Objave mogu biti: tjedni meniji, dnevne ponude, ili specijalne ponude. "
            "Pronađi gablec/ručak za DANAŠNJI dan. "
            "Ako objava ima sliku tjednog menija, pročitaj je i izdvoji samo današnji dan. "
            "Ako imaš cijene, dodaj ih. "
            "Ako možeš procijeniti makronutrijente (P/M/U u %), dodaj ih, ali NE izmišljaj - ako ne znaš, NE dodaj ih. "
            "Format stavke: naziv jela (cijena ako postoji). "
            "Vrati JSON: {{\"has_today\": bool, \"items\": [string]}}. "
            "Ako STVARNO nema ponude za današnji dan, vrati has_today=false i items=[].\n\n"
        )}
    ]
    
    for idx, post in enumerate(posts_data, 1):
        parts.append({"text": f"\n--- Objava {idx} (objavljena: {post['posted_at_local']}) ---"})
        
        if post['text']:
            parts.append({"text": f"Tekst: {post['text']}"})
        else:
            parts.append({"text": "Tekst: (nema teksta)"})
        
        if post['images']:
            parts.append({"text": f"Slike ({len(post['images'])} komada):"})
            for img in post['images']:
                parts.append({"inline_data": {"mime_type": img["mime"], "data": img["bytes"]}})
        else:
            parts.append({"text": "(Nema slika)"})
    
    resp = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=[{"role": "user", "parts": parts}],
    )
    
    txt = (resp.text or "").strip()
    
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
    except Exception:
        pass
    
    return {"has_today": False, "items": []}


def send_email(subject, body, sender, recipients, password):
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = ', '.join(recipients)
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp_server:
            smtp_server.login(sender, password)
            smtp_server.sendmail(sender, recipients, msg.as_string())
        
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def main():
    print(f"Daily Lunch Menu Scraper - {today_local.isoformat()}")
    print("=" * 80)
    
    results = {}
    for page_url in facebook_pages:
        try:
            run = client_apify.actor("apify/facebook-posts-scraper").call(run_input={
                "startUrls": [{"url": page_url}],
                "proxy": {"apifyProxyGroups": ["RESIDENTIAL"]},
                "maxRequestRetries": 10,
                "onlyPostsNewerThan": pom_day.isoformat(),
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
            results[page_url] = page_out
            
        except Exception as e:
            print(f"Error fetching {page_url}: {e}")
            results[page_url] = []
    
    today_lunch = {}
    page_urls_map = {}
    for page_url, posts in results.items():
        display_name = posts[0]["page_name"] if posts and posts[0]["page_name"] else page_url
        page_urls_map[display_name] = page_url
        
        pj = ask_gemini_for_all_posts(display_name, posts)
        
        if pj and pj.get("has_today") and pj.get("items"):
            found = {
                "restaurant": display_name,
                "items": pj.get("items", []),
                "facebook_url": page_url
            }
        else:
            found = {
                "restaurant": display_name,
                "items": [],
                "facebook_url": page_url
            }
        
        today_lunch[display_name] = found
    
    subject = f"Gableci danas – {today_local.isoformat()}"
    lines = [f"Gableci danas ({today_local.isoformat()}):\n"]
    
    for name, info in today_lunch.items():
        if info["items"]:
            lines.append(f"{name}:")
            for item in info["items"]:
                lines.append(f"  - {item}")
            lines.append(f"  Facebook: {info['facebook_url']}")
            lines.append("")
        else:
            lines.append(f"{name}: Nema objave za danas")
            lines.append(f"  Facebook: {info['facebook_url']}")
            lines.append("")
    
    body = "\n".join(lines)
    
    """
    email_sent = send_email(subject, body, EMAIL_SENDER, EMAIL_RECIPIENTS, EMAIL_PASSWORD)
    
    if email_sent:
        print("\nEmail sent successfully!")
    else:
        print("\nFailed to send email")
    
    print(f"\nSummary:")
    for name, info in today_lunch.items():
        if info["items"]:
            print(f"  {name}: {len(info['items'])} items")
        else:
            print(f"  {name}: No menu")
    
    return email_sent
    """

    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    slack_client.chat_postMessage(channel="#ponuda_gableca", text=body, unfurl_links=False, unfurl_media=False,)

    return True

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
