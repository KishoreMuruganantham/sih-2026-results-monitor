"""Interactive, one-time Drive OAuth setup; authorization is a separate action."""
from __future__ import annotations

import json
import os
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/drive.file"


def authorize(client_file: str = "secrets/google_drive_oauth_client.json",
             token_file: str = "secrets/drive_token.json") -> None:
    if os.getenv("CONFIRM_GOOGLE_OAUTH", "").strip() != "I_APPROVE_DRIVE_OAUTH":
        raise RuntimeError("Set CONFIRM_GOOGLE_OAUTH=I_APPROVE_DRIVE_OAUTH only after explicit approval")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_file, [SCOPE])
    login_hint = os.getenv("SIH_SENDER_EMAIL", "")
    credentials = flow.run_local_server(port=0, open_browser=True, access_type="offline",
                                        prompt="consent select_account",
                                        **({"login_hint": login_hint} if login_hint else {}))
    from googleapiclient.discovery import build
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    folder_id = os.getenv("DRIVE_FOLDER_ID", "").strip()
    folder_name = os.getenv("DRIVE_FOLDER_NAME", "SIH-2026-Monitor")
    if folder_id:
        folder = service.files().get(fileId=folder_id, fields="id,name,mimeType,trashed").execute()
        if (folder.get("name") != folder_name or folder.get("mimeType") != "application/vnd.google-apps.folder"
                or folder.get("trashed", False)):
            raise RuntimeError(f"DRIVE_FOLDER_ID is not an active '{folder_name}' folder")
    else:
        folder = service.files().create(
            body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
            fields="id,name",
        ).execute()
        folder_id = folder["id"]
    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"Drive OAuth token saved to {path}; do not commit it. It contains a refresh token.")
    print(f"Drive folder: {folder_name}; ID: {folder_id}")


if __name__ == "__main__":
    authorize()
