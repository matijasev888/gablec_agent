import os
import sys
import argparse
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

from gablec_daily import main, scrape_and_process, send_daily_message

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gablec Bot - Daily Lunch Menu for Slack")
    parser.add_argument(
        "--mode",
        choices=["scrape", "send", "full"],
        default="full",
        help="Run mode: 'scrape' for fetching/processing, 'send' for Slack message, 'full' for both"
    )
    args = parser.parse_args()
    
    print("=" * 60)
    print("GABLEC BOT - Daily Lunch Menu for Slack")
    print(f"Mode: {args.mode}")
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
    
    # Check required tokens based on mode
    missing = []
    if args.mode in ["scrape", "full"]:
        if not apify_token:
            missing.append("APIFY_TOKEN")
        if not google_api_key:
            missing.append("GOOGLE_API_KEY")
    if args.mode in ["send", "full"]:
        if not slack_bot_token:
            missing.append("SLACK_BOT_TOKEN")
    
    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    print("=" * 60)
    print()
    
    try:
        if args.mode == "scrape":
            scrape_and_process()
            print("\n" + "=" * 60)
            print("SUCCESS! Scrape and process complete.")
            print("=" * 60)
            sys.exit(0)
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
        else:  # full
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
