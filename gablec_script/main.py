import os
import sys
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

from gablec_daily import main

if __name__ == "__main__":
    print("=" * 60)
    print("GABLEC BOT - Daily Lunch Menu for Slack")
    print("=" * 60)
    print()
    
    # Validate configuration
    apify_token = os.getenv("APIFY_TOKEN")
    google_api_key = os.getenv("GOOGLE_API_KEY")
    slack_bot_token = os.getenv("SLACK_BOT_TOKEN")
    slack_channel = os.getenv("SLACK_CHANNEL", "#ponuda_gableca")
    
    print("Configuration:")
    print(f"  Apify Token:      {'OK' if apify_token else 'MISSING'}")
    print(f"  Google API Key:   {'OK' if google_api_key else 'MISSING'}")
    print(f"  Slack Bot Token:  {'OK' if slack_bot_token else 'MISSING'}")
    print(f"  Slack Channel:    {slack_channel}")
    print()
    
    missing = []
    if not apify_token:
        missing.append("APIFY_TOKEN")
    if not google_api_key:
        missing.append("GOOGLE_API_KEY")
    if not slack_bot_token:
        missing.append("SLACK_BOT_TOKEN")
    
    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    print("=" * 60)
    print()
    
    try:
        success = main()
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
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
