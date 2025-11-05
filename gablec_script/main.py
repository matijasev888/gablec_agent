import os
import sys
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

from gablec_daily import main

if __name__ == "__main__":
    print("=" * 80)
    print("GABLEC AGENT - Daily Lunch Menu Scraper")
    print("=" * 80)
    print()
    
    apify_token = os.getenv("APIFY_TOKEN")
    google_api_key = os.getenv("GOOGLE_API_KEY")
    email_sender = os.getenv("EMAIL_SENDER")
    email_password = os.getenv("EMAIL_PASSWORD")
    email_recipients = os.getenv("EMAIL_RECIPIENTS", "").split(",")
    email_recipients = [r.strip() for r in email_recipients if r.strip()]
    
    print("Configuration:")
    print(f"  Apify Token: {'Set' if apify_token else 'Missing'}")
    print(f"  Google API Key: {'Set' if google_api_key else 'Missing'}")
    print(f"  Email Sender: {email_sender if email_sender else 'Missing'}")
    print(f"  Email Password: {'Set' if email_password else 'Missing'}")
    print(f"  Email Recipients: {len(email_recipients)} recipient(s)")
    for i, recipient in enumerate(email_recipients, 1):
        print(f"    {i}. {recipient}")
    print()
    
    if not all([apify_token, google_api_key, email_sender, email_password]):
        print("Error: Missing required configuration!")
        print("Please set all required variables in .env file")
        sys.exit(1)
    
    if not email_recipients:
        print("Error: No email recipients configured!")
        print("Please set EMAIL_RECIPIENTS in .env file")
        sys.exit(1)
    
    print("=" * 80)
    print()
    
    try:
        success = main()
        if success:
            print("\n" + "=" * 80)
            print("SUCCESS! Lunch menus sent to all recipients.")
            print("=" * 80)
            sys.exit(0)
        else:
            print("\n" + "=" * 80)
            print("WARNING: Script completed but email may not have been sent.")
            print("=" * 80)
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
