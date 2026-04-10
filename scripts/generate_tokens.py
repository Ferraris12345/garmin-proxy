"""
Garmin OAuth Token Generator (LOCAL USE ONLY)
==============================================
Run this script ONCE on your local Mac/PC to authenticate with Garmin
and print the OAuth tokens as JSON strings ready to paste into Railway
env vars.

WHY THIS EXISTS:
Garmin permanently blocks login attempts from Railway's IP ranges with
429 rate limits. Tokens must be generated from a residential IP (your
laptop), then injected into the Railway proxy as env vars.

USAGE:
  1. Install garth locally:        pip install garth==0.4.46
  2. Run this script:              python generate_tokens.py
  3. Enter your Garmin email + password when prompted
  4. Copy the two JSON strings it prints
  5. Paste them into Railway:
     - GARMIN_OAUTH1_TOKEN = <oauth1 JSON>
     - GARMIN_OAUTH2_TOKEN = <oauth2 JSON>
  6. Save → Railway auto-redeploys → proxy now works

TOKEN LIFETIME:
  - access_token:  ~25h, auto-refreshed by garth inside the proxy
  - refresh_token: ~30 days. When it expires, re-run this script locally
    and update the Railway env vars. You will see 401/503 errors from
    the proxy when rotation is needed.
"""

import json
import getpass
from pathlib import Path

import garth


def main():
    print("Garmin OAuth Token Generator")
    print("=" * 40)
    print()
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    print()
    print("Logging in to Garmin Connect...")

    try:
        garth.login(email, password)
    except Exception as e:
        print(f"ERROR: Login failed: {type(e).__name__}: {e}")
        print()
        print("If this is a 429 error, wait 30+ minutes and try again.")
        print("If it's a credentials error, double-check your email/password.")
        print("If you have MFA enabled, you will need to handle the prompt.")
        return

    # Save to a temp dir so we can read the JSON files back
    tmp_dir = Path.home() / ".garmin_tokens_tmp"
    tmp_dir.mkdir(exist_ok=True)
    garth.save(str(tmp_dir))

    oauth1_path = tmp_dir / "oauth1_token.json"
    oauth2_path = tmp_dir / "oauth2_token.json"

    if not oauth1_path.exists() or not oauth2_path.exists():
        print(f"ERROR: Expected token files not found in {tmp_dir}")
        return

    oauth1 = oauth1_path.read_text().strip()
    oauth2 = oauth2_path.read_text().strip()

    # Collapse to single-line JSON for env-var pasting
    oauth1_oneline = json.dumps(json.loads(oauth1), separators=(",", ":"))
    oauth2_oneline = json.dumps(json.loads(oauth2), separators=(",", ":"))

    print()
    print("SUCCESS! Copy the two values below into Railway env vars.")
    print("=" * 60)
    print()
    print("GARMIN_OAUTH1_TOKEN =")
    print(oauth1_oneline)
    print()
    print("GARMIN_OAUTH2_TOKEN =")
    print(oauth2_oneline)
    print()
    print("=" * 60)
    print(f"(Tokens also saved to {tmp_dir} — delete that folder when done.)")


if __name__ == "__main__":
    main()
