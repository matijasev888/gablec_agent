"""One-off probe: which Gemini models actually work on this API key / free tier.

Run with the project's GOOGLE_API_KEY (loaded from gablec_script/.env).
Does a tiny text generate_content call against each candidate and reports
success / failure with the error class so we can pick a real fallback chain.
"""
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai.errors import ClientError, ServerError

load_dotenv(Path(__file__).parent / "gablec_script" / ".env")
client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

# Candidates ordered newest/most-capable -> cheapest, plus the two that
# docs say are shut down (to confirm) and the bot's current list.
CANDIDATES = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.0-flash",        # docs: shut down
    "gemini-2.0-flash-lite",   # docs: shut down
]

print("=== Models the key can list ===")
try:
    listed = sorted(m.name.replace("models/", "") for m in client.models.list())
    for n in listed:
        print(" ", n)
except Exception as e:
    listed = []
    print("  list() failed:", e)

print("\n=== Live generate_content probe (free tier) ===")
results = []
for model in CANDIDATES:
    t0 = time.monotonic()
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[{"role": "user", "parts": [{"text": "Reply with the single word: OK"}]}],
        )
        dt = time.monotonic() - t0
        txt = (resp.text or "").strip().replace("\n", " ")[:30]
        print(f"  OK    {model:<26} {dt:5.2f}s  -> {txt!r}")
        results.append((model, "OK", dt))
    except (ClientError, ServerError) as e:
        dt = time.monotonic() - t0
        # Pull the HTTP status / status string for a compact label.
        code = getattr(e, "code", "?")
        msg = str(e).split(".")[0][:90]
        print(f"  FAIL  {model:<26} {dt:5.2f}s  -> [{code}] {msg}")
        results.append((model, f"FAIL {code}", dt))
    except Exception as e:  # noqa: BLE001 - probe should never crash
        print(f"  ERR   {model:<26}  -> {type(e).__name__}: {str(e)[:80]}")
        results.append((model, "ERR", 0))

print("\n=== Summary: models that WORK on this key ===")
working = [m for m, status, _ in results if status == "OK"]
for m in working:
    print("  ", m)
print(f"\n{len(working)}/{len(CANDIDATES)} candidates usable.")
