import base64
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
VOICE_SENDER = "voice-noreply@google.com"


class GmailReader:
    def __init__(self, credentials_file: str, token_file: str):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self._authenticate()

    def _authenticate(self):
        creds: Optional[Credentials] = None

        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # Run once interactively (requires a browser on first use).
                # On headless Pi: run setup_gmail.py on a machine with a browser,
                # then copy token.json to the Pi.
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "w") as f:
                f.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)
        logger.info("Gmail authenticated")

    def get_new_voice_emails(self, max_age_seconds: int = 120) -> List[Dict]:
        """Return unread Google Voice emails received within max_age_seconds."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        query = f"from:{VOICE_SENDER} is:unread"

        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=20)
                .execute()
            )
        except Exception as e:
            logger.error("Gmail list error: %s", e)
            return []

        emails = []
        for msg_ref in result.get("messages", []):
            try:
                msg = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=msg_ref["id"], format="full")
                    .execute()
                )
                ts = datetime.fromtimestamp(
                    int(msg["internalDate"]) / 1000, tz=timezone.utc
                )
                if ts < cutoff:
                    continue

                body = self._extract_plain_text(msg)
                if body:
                    emails.append({"id": msg["id"], "timestamp": ts, "body": body})
            except Exception as e:
                logger.error("Error reading message %s: %s", msg_ref["id"], e)

        return emails

    def _extract_plain_text(self, msg: dict) -> Optional[str]:
        """Recursively find the text/plain part and decode it."""

        def _walk(part: dict) -> Optional[str]:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    # Gmail uses URL-safe base64 without padding
                    return base64.urlsafe_b64decode(
                        data + "=="
                    ).decode("utf-8", errors="replace")
            for sub in part.get("parts", []):
                result = _walk(sub)
                if result:
                    return result
            return None

        return _walk(msg.get("payload", {}))
