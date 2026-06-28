"""
Gmail + Google Sheets OAuth setup.

Checks whether token.json already exists and covers the required scopes.
Only opens a browser if the token is missing, expired, or missing a scope.
See GMAIL_SETUP.md for the full walkthrough.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")


def main():
    if not Path(CREDENTIALS_FILE).exists():
        print(f"ERROR: '{CREDENTIALS_FILE}' not found.")
        print("See GMAIL_SETUP.md for instructions on getting credentials.json.")
        sys.exit(1)

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Run:  pip install -r requirements.txt  first.")
        sys.exit(1)

    creds = None

    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid and creds.has_scopes(SCOPES):
        print(f"Token already valid — nothing to do. ({TOKEN_FILE})")
        print("Delete token.json and re-run this script to force re-authorization.")
        return

    if creds and creds.expired and creds.refresh_token:
        print("Token expired — refreshing...")
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"Token refreshed → {TOKEN_FILE}")
        return

    print("A browser window will open — log in and grant access.")
    print("If no browser is available, see GMAIL_SETUP.md for the headless option.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"\nSuccess!  Token saved → {TOKEN_FILE}")


if __name__ == "__main__":
    main()
