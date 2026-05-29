"""
One-time Gmail OAuth setup.

Run this ONCE (with a browser available) to create token.json.
After that, copy token.json to the Raspberry Pi and you're done.
See GMAIL_SETUP.md for the full walkthrough.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")


def main():
    if not Path(CREDENTIALS_FILE).exists():
        print(f"ERROR: '{CREDENTIALS_FILE}' not found.")
        print("See GMAIL_SETUP.md for instructions on getting credentials.json.")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Run:  pip install -r requirements.txt  first.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)

    print("A browser window will open — log in and grant read-only Gmail access.")
    print("If no browser is available, see GMAIL_SETUP.md for the headless option.\n")

    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"\nSuccess!  Token saved → {TOKEN_FILE}")
    print("Copy token.json to your Raspberry Pi next to main.py, then run:")
    print("  python main.py")


if __name__ == "__main__":
    main()
