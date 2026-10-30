"""Explicitly gated test email command; preview mode is always mock-only."""
from __future__ import annotations
import argparse
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from config import Settings
from database import Database, stable_hash
from notifications.email import NotificationService, load_recipients
from notifications.transports import MockEmailProvider, create_transport

def test_email(settings: Settings, live: bool=False, confirm_real_send: bool=False) -> dict:
    if live:
        if not confirm_real_send: raise RuntimeError("Live test email requires --confirm-real-send")
        if settings.dry_run or not settings.allow_real_email: raise RuntimeError("Live test email requires DRY_RUN=false and ENABLE_REAL_EMAIL=true")
        if settings.email_transport!="gmail": raise RuntimeError("Only the institutionally approved Gmail API adapter is available")
        provider=create_transport(settings)
    else:
        settings=replace(settings,dry_run=True)
        provider=MockEmailProvider()
    recipients=load_recipients(settings.recipients_file)
    if not recipients: raise RuntimeError(f"No recipients configured in {settings.recipients_file}")
    db=Database(settings.database_path)
    service=NotificationService(settings,db,provider)
    now=datetime.now(timezone.utc).isoformat()
    html=("<h2>SIH monitor email configuration test</h2>"
          f"<p>Generated at {now}</p><p>This is a test message. It contains no SIH result data.</p>"
          f"<p>Configured sender: {settings.sender_email}</p><p>Configured recipients: {len(recipients)}</p>")
    outcome=service.notify("SIH_EMAIL_TEST","test",stable_hash([now,recipients]),"[SIH Monitor] Email configuration test",html,"local://email-test")
    previews=[]
    if isinstance(provider,MockEmailProvider):
        for message in provider.messages:
            previews.append({"from":str(message["From"]),"to":str(message["To"]),"bcc":str(message["Bcc"]),"subject":str(message["Subject"]),"type":str(message["X-SIH-Notification-Type"]),"body":message.get_body(preferencelist=("html",)).get_content()})
    return {"mode":"live" if live else "mock preview","outcome":outcome,"messages":previews}

def main() -> None:
    parser=argparse.ArgumentParser(description="Preview or explicitly send a test message to recipients.yaml")
    parser.add_argument("--send",action="store_true",help="send a real test message (requires explicit confirmation and approved config)")
    parser.add_argument("--confirm-real-send",action="store_true",help="confirm delivery to all recipients.yaml addresses")
    args=parser.parse_args()
    settings=Settings.from_env()
    result=test_email(settings,live=args.send,confirm_real_send=args.confirm_real_send)
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=="__main__": main()
