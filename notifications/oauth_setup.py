"""Interactive Gmail OAuth authorization; never sends email."""
from __future__ import annotations
import os
from pathlib import Path
from config import Settings
from notifications.transports import GmailApiTransport

def authorize(settings: Settings) -> None:
    if os.getenv("CONFIRM_GMAIL_OAUTH", "").strip() != "I_APPROVE_GMAIL_OAUTH":
        raise RuntimeError("Set CONFIRM_GMAIL_OAUTH=I_APPROVE_GMAIL_OAUTH to start the Gmail OAuth consent flow")
    if not settings.gmail_client_file.is_file():
        raise FileNotFoundError(f"OAuth client file is missing: {settings.gmail_client_file}")
    try: from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc: raise RuntimeError("Install google-auth-oauthlib") from exc
    flow=InstalledAppFlow.from_client_secrets_file(str(settings.gmail_client_file),GmailApiTransport.SCOPES)
    credentials=flow.run_local_server(port=0,open_browser=True,access_type="offline",prompt="consent select_account",
                                     **({"login_hint":settings.sender_email} if settings.sender_email else {}))
    settings.gmail_token_file.parent.mkdir(parents=True,exist_ok=True)
    settings.gmail_token_file.write_text(credentials.to_json(),encoding="utf-8")
    try: settings.gmail_token_file.chmod(0o600)
    except OSError: pass
    print(f"OAuth token saved outside source code at: {settings.gmail_token_file}; it contains a refresh token")

if __name__=="__main__": authorize(Settings.from_env())
