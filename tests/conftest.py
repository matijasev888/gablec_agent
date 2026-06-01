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
