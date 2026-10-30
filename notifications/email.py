"""Mock-only email outbox, recipient validation, message rendering, and recovery."""
from __future__ import annotations
import json
import os
import re
from html import escape
from pathlib import Path
from typing import Any
import yaml
from config import Settings
from database import Database, normalize_revision_value
from notifications.transports import EmailTransport, MockEmailProvider, create_transport

ADDRESS_RE=re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

def valid_address(value: str) -> bool:
    return bool(ADDRESS_RE.fullmatch(value.strip())) and len(value)<255

def load_recipients(path: str | Path) -> list[str]:
    secret_config=os.getenv("SIH_RECIPIENTS_YAML", "")
    if secret_config.strip():
        config=yaml.safe_load(secret_config) or {}
    else:
        with Path(path).open("r",encoding="utf-8") as handle: config=yaml.safe_load(handle) or {}
    recipients=config.get("recipients",[])
    if not isinstance(recipients,list): raise ValueError("recipients.yaml must contain a 'recipients' list")
    result=[]; seen=set()
    for item in recipients:
        address=item.get("email") if isinstance(item,dict) else item
        if isinstance(address,str):
            address=address.strip()
            if address and address.casefold() not in seen: seen.add(address.casefold()); result.append(address)
    return result

class NotificationService:
    def __init__(self,settings: Settings,database: Database,provider: EmailTransport | None=None,lease_seconds: int=300):
        self.settings=settings; self.database=database; self.provider=provider or create_transport(settings); self.lease_seconds=lease_seconds
        if settings.dry_run and not self.provider.is_mock:
            raise RuntimeError("DRY_RUN blocks all live email transports")
        if not settings.dry_run and not settings.allow_real_email:
            raise RuntimeError("Live email transport requires ENABLE_REAL_EMAIL=true")

    def notification_builder(self,batch_id: str,source_url: str,svce_records: list[dict[str,Any]],review_records: list[dict[str,Any]],detected_at_ist: str):
        """Build initial/revision payloads within the same transaction as the batch snapshot."""
        try: recipients=load_recipients(self.settings.recipients_file)
        except Exception as exc:
            recipients=[]
        label=batch_id.replace("SIH2026:","Batch ").replace("SIH 2026:","Batch ")
        def build(kind: str,content_hash: str,revision: dict[str,Any] | None) -> dict[str,Any]:
            if kind=="new_batch":
                subject=f"[SIH 2026] {label} Screening Results - SVCE"
                body=render_batch_email(label,detected_at_ist,source_url,svce_records,review_records)
            else:
                subject=f"[SIH 2026] {label} Results Update - SVCE"
                body=render_revision_email(label,revision or {},detected_at_ist,source_url,review_records,svce_records)
            return {"subject":subject,"body_html":body,"source_url":source_url,"recipients":recipients}
        return build

    def notify(self,batch_id: str,notification_type: str,content_hash: str,subject: str,html: str,source_url: str="") -> dict[str,Any]:
        """Compatibility entry point: persist once, then process the durable outbox item."""
        try: recipients=load_recipients(self.settings.recipients_file)
        except Exception as exc: recipients=[]; self.database.event("recipient_config_error",str(exc),level="ERROR")
        self.database.enqueue_notification(batch_id,notification_type,content_hash,subject,html,source_url,recipients)
        with self.database.connect() as db:
            row=db.execute("SELECT id FROM notifications WHERE batch_id=? AND notification_type=? AND content_hash=?",(batch_id,notification_type,content_hash)).fetchone()
        return self.process_pending(notification_id=row[0])

    def process_pending(self,notification_id: int | None=None,batch_id: str | None=None) -> dict[str,Any]:
        self.database.recover_stale_notifications(self.lease_seconds)
        if notification_id is not None: rows=[self.database.notification(notification_id)]
        elif batch_id:
            with self.database.connect() as db: rows=db.execute("SELECT * FROM notifications WHERE batch_id=? AND status IN ('PENDING','RETRY_PENDING') ORDER BY id",(batch_id,)).fetchall()
        else: rows=self.database.pending_notifications()
        results=[]
        for row in rows:
            if row is not None and self.database.claim_notification(row["id"]): results.append(self._process_claimed(row))
        return {"dry_run":self.settings.dry_run,"notifications":results}

    def _process_claimed(self,row) -> dict[str,Any]:
        nid=row["id"]
        try: recipients=json.loads(row["recipients_json"] or "[]")
        except Exception: recipients=[]
        try: recipients.extend(load_recipients(self.settings.recipients_file))
        except Exception as exc: self.database.event("recipient_config_error",str(exc),level="ERROR")
        recipients=list(dict.fromkeys(x.casefold() if isinstance(x,str) else str(x) for x in recipients))
        if not valid_address(self.settings.sender_email):
            self.database.finish_notification(nid,"FAILED","Invalid configured sender address")
            return {"id":nid,"status":"FAILED","deliveries":[]}
        outcomes=[]
        for address in recipients:
            if not isinstance(address,str) or not valid_address(address):
                bad=str(address); self.database.set_recipient_status(nid,bad,"INVALID","Invalid email address")
                self.database.event("recipient_invalid",f"Invalid recipient: {bad}",level="WARNING")
                outcomes.append({"recipient":bad,"status":"INVALID"}); continue
            with self.database.connect() as db: existing=db.execute("SELECT status FROM notification_recipients WHERE notification_id=? AND recipient_email=?",(nid,address)).fetchone()
            if existing and existing[0] in {"MOCKED","SENT","INVALID"}: continue
            self.database.mark_recipient_processing(nid,address)
            try:
                key=f"notification:{nid}:recipient:{address.casefold()}"
                result=self.provider.send_bcc(self.settings.sender_email,address,row["email_subject"],row["body_html"] or "",row["notification_type"],key)
                if result.accepted:
                    status="MOCKED" if self.provider.is_mock else "SENT"
                    error=result.detail if self.provider.is_mock else None
                else: status="FAILED"; error=result.detail
                self.database.set_recipient_status(nid,address,status,error,result.message_id)
                outcomes.append({"recipient":address,"status":status,"detail":error})
            except Exception as exc:
                self.database.set_recipient_status(nid,address,"FAILED",str(exc))
                outcomes.append({"recipient":address,"status":"FAILED","detail":str(exc)})
        statuses=self.database.recipient_statuses(nid)
        any_delivered=any(x["status"] in {"MOCKED","SENT"} for x in statuses)
        complete=bool(statuses) and all(x["status"] in {"MOCKED","SENT","INVALID"} for x in statuses)
        final=("SENT" if complete and any(x["status"]=="SENT" for x in statuses) else ("MOCKED" if complete and any_delivered else "FAILED"))
        error=None if final in {"SENT","MOCKED"} else ("No valid recipients configured" if not any_delivered and not any(x["status"]=="FAILED" for x in statuses) else "One or more recipient attempts failed")
        self.database.finish_notification(nid,final,error)
        return {"id":nid,"batch_id":row["batch_id"],"type":row["notification_type"],"source_url":row["source_url"],"status":final,"deliveries":outcomes}

    def retry_failed(self,notification_id: int) -> dict[str,Any]:
        if not self.database.retry_notification(notification_id): return {"queued":False,"reason":"notification is not retryable"}
        return self.process_pending(notification_id=notification_id)

    def retry_failed_due(self,max_attempts: int=5) -> int:
        """Queue failed outbox entries for the next polling cycle, with a cap."""
        with self.database.connect() as db:
            rows=db.execute("SELECT id FROM notifications WHERE status='FAILED' AND attempt_count<? ORDER BY id",(max_attempts,)).fetchall()
        return sum(1 for row in rows if self.database.retry_notification(row[0]))

def render_batch_email(batch_label: str,detected_at_ist: str,source_url: str,records: list[dict[str,Any]],review_records: list[dict[str,Any]] | None=None) -> str:
    if not records:
        body="<p>Results have been published for this batch, but no SVCE teams were identified.</p>"
    else:
        rows=[]
        for item in records:
            v=item.get("normalized",{})
            rows.append("<tr>"+"".join(f"<td>{escape(str(v.get(k,'')))}</td>" for k in ("problem_statement_id","team_id","team_name","team_leader","institution_name","institution_code","status"))+"</tr>")
        body="<table border='1'><thead><tr><th>Problem Statement ID</th><th>Team ID</th><th>Team</th><th>Team leader</th><th>Institution</th><th>AISHE code</th><th>Selection status</th></tr></thead><tbody>"+"".join(rows)+"</tbody></table>"
    reviews=review_records or []
    summary=f"<p>Potential institution matches requiring review (not confirmed SVCE): {len(reviews)}</p>"
    if reviews:
        summary+="<table border='1'><thead><tr><th>PS ID</th><th>Team ID</th><th>Team</th><th>Team leader</th><th>Institution</th><th>Status</th><th>Review reason</th></tr></thead><tbody>"
        summary+="".join("<tr>"+"".join(f"<td>{escape(str(x.get('normalized',{}).get(k,'')))}</td>" for k in ("problem_statement_id","team_id","team_name","team_leader","institution_name","status"))+f"<td>{escape(str(x.get('reason','')))}</td></tr>" for x in reviews)+"</tbody></table>"
    url=escape(source_url,quote=True)
    return f"<h2>{escape(batch_label)}</h2><p>Discovered (IST): {escape(detected_at_ist)}<br>Source: <a href='{url}'>{url}</a></p><p>SVCE teams identified: {len(records)}</p>{body}{summary}"

def render_revision_email(batch_label: str,revision: dict[str,Any],revision_at_ist: str,source_url: str,review_records: list[dict[str,Any]] | None=None,svce_records: list[dict[str,Any]] | None=None) -> str:
    rows=[]
    for change in revision.get("changed_records",[]):
        before=change.get("previous") or {}; after=change.get("current") or {}
        fields=sorted(set(before)|set(after))
        for field in fields:
            old="(not present)" if change["change"]=="added" else before.get(field,"(not present)")
            new="(not present)" if change["change"]=="removed" else after.get(field,"(not present)")
            if change["change"]=="updated" and normalize_revision_value(old)==normalize_revision_value(new): continue
            rows.append(f"<tr><td>{escape(str(change['record_key']))}</td><td>{escape(str(field))}</td><td>{escape(str(old))}</td><td>{escape(str(new))}</td></tr>")
    body="<p>No field-level differences were available; inspect the official source.</p>" if not rows else "<table border='1'><thead><tr><th>Record</th><th>Field</th><th>Previous</th><th>Current</th></tr></thead><tbody>"+"".join(rows)+"</tbody></table>"
    reviews=review_records or []
    current=render_batch_email(batch_label,revision_at_ist,source_url,svce_records or [],reviews)
    current=current.replace("Detected (IST):", "Updated (IST):")
    return current+f"<h3>Meaningful changes in this update</h3>{body}"
