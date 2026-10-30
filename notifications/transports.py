"""Email transport boundary with mock and explicitly gated Gmail API adapters."""
from __future__ import annotations
import base64
from dataclasses import dataclass
from email.message import EmailMessage
import hashlib
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

@dataclass(frozen=True)
class TransportResult:
    accepted: bool
    message_id: str
    detail: str

@runtime_checkable
class EmailTransport(Protocol):
    is_mock: bool
    def send_bcc(self,sender: str,recipient: str,subject: str,html: str,notification_type: str="",idempotency_key: str="") -> TransportResult: ...

class MockEmailProvider:
    """Captures complete per-recipient BCC messages; it has no network capability."""
    is_mock=True
    def __init__(self): self.messages: list[EmailMessage]=[]
    def send_bcc(self,sender: str,recipient: str,subject: str,html: str,notification_type: str="",idempotency_key: str="") -> TransportResult:
        message=_build_message(sender,recipient,subject,html,notification_type,idempotency_key)
        self.messages.append(message)
        return TransportResult(True,f"mock-{len(self.messages)}","captured by mock; no email sent")

class GmailApiTransport:
    """Gmail API send adapter. It requires a separately authorized OAuth token."""
    is_mock=False
    SCOPES=["https://www.googleapis.com/auth/gmail.send"]

    def __init__(self,sender: str,token_file: str | Path,service=None):
        self.sender=sender; self.token_file=Path(token_file); self._service=service

    def _get_service(self):
        if self._service is not None: return self._service
        secret_token=os.getenv("GMAIL_OAUTH_TOKEN_JSON", "")
        if secret_token.strip():
            self.token_file.parent.mkdir(parents=True,exist_ok=True)
            self.token_file.write_text(secret_token,encoding="utf-8")
            try: self.token_file.chmod(0o600)
            except OSError: pass
        if not self.token_file.is_file():
            raise RuntimeError(f"OAuth token not found at {self.token_file}; run the documented Gmail OAuth setup after institutional approval")
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc: raise RuntimeError("Install the Google API dependencies from requirements.txt") from exc
        credentials=Credentials.from_authorized_user_file(str(self.token_file),self.SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self.token_file.parent.mkdir(parents=True,exist_ok=True)
            self.token_file.write_text(credentials.to_json(),encoding="utf-8")
        if not credentials.valid: raise RuntimeError("Gmail OAuth token is invalid or expired; repeat the approved OAuth setup")
        self._service=build("gmail","v1",credentials=credentials,cache_discovery=False)
        return self._service

    def send_bcc(self,sender: str,recipient: str,subject: str,html: str,notification_type: str="",idempotency_key: str="") -> TransportResult:
        if sender.casefold()!=self.sender.casefold(): raise RuntimeError("The message sender does not match the authorized sender")
        service=self._get_service()
        message=_build_message(sender,recipient,subject,html,notification_type,idempotency_key)
        raw=base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        response=service.users().messages().send(userId="me",body={"raw":raw}).execute()
        return TransportResult(True,str(response.get("id","")),"accepted by Gmail API")

def _build_message(sender: str,recipient: str,subject: str,html: str,notification_type: str,idempotency_key: str="") -> EmailMessage:
    message=EmailMessage(); message["From"]=sender; message["To"]=sender; message["Bcc"]=recipient
    message["Subject"]=subject; message["X-SIH-Notification-Type"]=notification_type
    if not idempotency_key: idempotency_key="|".join((sender.casefold(),recipient.casefold(),subject,notification_type,html))
    digest=hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    domain=sender.split("@",1)[-1]
    message["Message-ID"]=f"<sih-{digest}@{domain}>"
    message.set_content("SIH results notification (HTML version attached).")
    message.add_alternative(html,subtype="html")
    return message

def create_transport(settings):
    """Choose a provider without ever enabling live sending implicitly."""
    if settings.dry_run or settings.email_transport=="mock": return MockEmailProvider()
    if not settings.allow_real_email:
        raise RuntimeError("Live email is disabled; set ENABLE_REAL_EMAIL=true only after explicit approval")
    if settings.email_transport=="gmail": return GmailApiTransport(settings.sender_email,settings.gmail_token_file)
    raise RuntimeError(f"Unsupported/unapproved EMAIL_TRANSPORT={settings.email_transport!r}; no SVCE SMTP endpoint is configured")
