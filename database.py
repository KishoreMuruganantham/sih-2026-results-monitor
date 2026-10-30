"""SQLite persistence for discovery, verification, and future notification state."""
from __future__ import annotations
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS pages(
 url TEXT PRIMARY KEY, edition TEXT, batch_id TEXT, discovered_at TEXT NOT NULL,
 verification_status TEXT NOT NULL DEFAULT 'pending', publication_status TEXT NOT NULL DEFAULT 'unknown',
 last_checked_at TEXT, last_modified TEXT, etag TEXT, content_hash TEXT,
 processing_status TEXT NOT NULL DEFAULT 'pending', failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(verification_status, processing_status);
CREATE TABLE IF NOT EXISTS records(
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 stable_key TEXT NOT NULL DEFAULT '', match_status TEXT NOT NULL DEFAULT 'other',
 source_data TEXT NOT NULL, normalized_data TEXT NOT NULL,
 UNIQUE(batch_id,fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id);
CREATE TABLE IF NOT EXISTS review_records(
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 source_data TEXT NOT NULL, normalized_data TEXT NOT NULL, review_reason TEXT NOT NULL,
 UNIQUE(batch_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS revisions(
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, previous_hash TEXT NOT NULL,
 new_hash TEXT NOT NULL, changed_records TEXT NOT NULL, revision_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, notification_type TEXT NOT NULL,
 content_hash TEXT NOT NULL, email_subject TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 attempt_count INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT, sent_at TEXT,
 provider_message_id TEXT, error TEXT, source_url TEXT, body_html TEXT,
 recipients_json TEXT NOT NULL DEFAULT '[]', created_at TEXT, updated_at TEXT,
 UNIQUE(batch_id,notification_type,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status,last_attempt_at);
CREATE TABLE IF NOT EXISTS notification_recipients(
 id INTEGER PRIMARY KEY, notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
 recipient_email TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempt_count INTEGER NOT NULL DEFAULT 0,
 last_attempt_at TEXT, delivered_at TEXT, provider_message_id TEXT, error TEXT,
 UNIQUE(notification_id,recipient_email)
);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, level TEXT NOT NULL,
 event_type TEXT NOT NULL, url TEXT, detail TEXT NOT NULL
);
"""

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def stable_hash(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            cols={r[1] for r in db.execute("PRAGMA table_info(records)")}
            if "stable_key" not in cols: db.execute("ALTER TABLE records ADD COLUMN stable_key TEXT NOT NULL DEFAULT ''")
            if "match_status" not in cols: db.execute("ALTER TABLE records ADD COLUMN match_status TEXT NOT NULL DEFAULT 'other'")
            ncols={r[1] for r in db.execute("PRAGMA table_info(notifications)")}
            for name,definition in {"source_url":"TEXT","body_html":"TEXT","recipients_json":"TEXT NOT NULL DEFAULT '[]'","created_at":"TEXT","updated_at":"TEXT"}.items():
                if name not in ncols: db.execute(f"ALTER TABLE notifications ADD COLUMN {name} {definition}")
            rev_indexes=db.execute("PRAGMA index_list(revisions)").fetchall()
            for index in rev_indexes:
                cols=[r[2] for r in db.execute(f"PRAGMA index_info('{index[1]}')").fetchall()]
                if index[2] and cols==["batch_id","new_hash"]:
                    db.executescript("CREATE TABLE revisions_v2(id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,previous_hash TEXT NOT NULL,new_hash TEXT NOT NULL,changed_records TEXT NOT NULL,revision_at TEXT NOT NULL); INSERT INTO revisions_v2 SELECT id,batch_id,previous_hash,new_hash,changed_records,revision_at FROM revisions; DROP TABLE revisions; ALTER TABLE revisions_v2 RENAME TO revisions;")
                    break

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, event_type: str, detail: str, url: str | None = None, level: str = "INFO") -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(occurred_at,level,event_type,url,detail) VALUES(?,?,?,?,?)", (utc_now(),level,event_type,url,detail))

    def register_page(self, url: str, edition: str = "SIH 2026", batch_id: str | None = None) -> bool:
        with self.connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO pages(url,edition,batch_id,discovered_at) VALUES(?,?,?,?)", (url,edition,batch_id,utc_now()))
            inserted = cur.rowcount == 1
            db.execute("INSERT INTO events(occurred_at,level,event_type,url,detail) VALUES(?,?,?,?,?)", (utc_now(),"INFO","page_discovered",url,"new" if inserted else "already_registered"))
            return inserted

    def page(self, url: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute("SELECT * FROM pages WHERE url=?",(url,)).fetchone()

    def page_for_batch(self, batch_id: str) -> str | None:
        with self.connect() as db:
            row=db.execute("SELECT url FROM pages WHERE batch_id=? ORDER BY discovered_at LIMIT 1",(batch_id,)).fetchone()
            return row[0] if row else None

    def update_page(self, url: str, **fields: Any) -> None:
        allowed = {"edition","batch_id","verification_status","publication_status","last_checked_at","last_modified","etag","content_hash","processing_status","failure_reason"}
        fields = {k:v for k,v in fields.items() if k in allowed}
        if not fields: return
        sql = ",".join(f"{k}=?" for k in fields)
        with self.connect() as db: db.execute(f"UPDATE pages SET {sql} WHERE url=?",(*fields.values(),url))

    def enqueue_notification(self, batch_id: str, notification_type: str, content_hash: str, subject: str, body_html: str="", source_url: str="", recipients: list[str] | None=None) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now=utc_now()
            cur=db.execute("INSERT OR IGNORE INTO notifications(batch_id,notification_type,content_hash,email_subject,body_html,source_url,recipients_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'PENDING',?,?)",(batch_id,notification_type,content_hash,subject,body_html,source_url,json.dumps(recipients or []),now,now))
            return cur.rowcount == 1

    def save_extracted_records(self, batch_id: str, records: list[dict[str, Any]], review: list[dict[str, Any]], content_hash: str, notification_builder=None) -> dict[str, Any] | None:
        """Replace the batch snapshot and record a meaningful revision when the normalized data changed."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous=db.execute("SELECT stable_key,source_data,normalized_data,match_status FROM records WHERE batch_id=?",(batch_id,)).fetchall()
            old={r[0]:(json.loads(r[1]),json.loads(r[2]),r[3]) for r in previous}
            current={item["stable_key"]:(item["source"],item["normalized"],item["match_status"]) for item in records}
            changes=[]
            for key in sorted(set(old)|set(current)):
                before=old.get(key); after=current.get(key)
                if before is None and after[2] in {"svce","review"}: changes.append({"record_key":key,"change":"added","previous":None,"current":after[0]})
                elif after is None and before[2] in {"svce","review"}: changes.append({"record_key":key,"change":"removed","previous":before[0],"current":None})
                elif before is not None and after is not None and (before[2] in {"svce","review"} or after[2] in {"svce","review"}):
                    old_fields={k:normalize_revision_value(v) for k,v in before[1].items()}
                    new_fields={k:normalize_revision_value(v) for k,v in after[1].items()}
                    if old_fields!=new_fields or before[2]!=after[2]:
                        changes.append({"record_key":key,"change":"updated","previous":before[0],"current":after[0],"previous_match":before[2],"current_match":after[2]})
            previous_hash=db.execute("SELECT content_hash FROM pages WHERE batch_id=?",(batch_id,)).fetchone()
            was_processed=bool(previous)
            revision=None
            if was_processed and changes and previous_hash and previous_hash[0] and previous_hash[0]!=content_hash:
                revision={"previous_hash":previous_hash[0],"new_hash":content_hash,"changed_records":changes}
                cur=db.execute("INSERT INTO revisions(batch_id,previous_hash,new_hash,changed_records,revision_at) VALUES(?,?,?,?,?)",
                    (batch_id,revision["previous_hash"],content_hash,json.dumps(changes,ensure_ascii=False,sort_keys=True),utc_now()))
                revision["revision_id"]=cur.lastrowid
            notification_kind="revision" if revision else ("new_batch" if not was_processed else None)
            if notification_kind and notification_builder:
                notify_hash=stable_hash([batch_id,revision["revision_id"],revision["previous_hash"],revision["new_hash"]]) if revision else content_hash
                payload=notification_builder(notification_kind,notify_hash,revision)
                now=utc_now()
                db.execute("INSERT OR IGNORE INTO notifications(batch_id,notification_type,content_hash,email_subject,body_html,source_url,recipients_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'PENDING',?,?)",
                    (batch_id,notification_kind,notify_hash,payload["subject"],payload["body_html"],payload.get("source_url",""),json.dumps(payload.get("recipients",[])),now,now))
            db.execute("DELETE FROM records WHERE batch_id=?",(batch_id,))
            db.execute("DELETE FROM review_records WHERE batch_id=?",(batch_id,))
            for item in records:
                db.execute("INSERT INTO records(batch_id,fingerprint,stable_key,match_status,source_data,normalized_data) VALUES(?,?,?,?,?,?)",
                    (batch_id,item["fingerprint"],item["stable_key"],item["match_status"],json.dumps(item["source"],ensure_ascii=False,sort_keys=True),json.dumps(item["normalized"],ensure_ascii=False,sort_keys=True)))
            for item in review:
                db.execute("INSERT INTO review_records(batch_id,fingerprint,source_data,normalized_data,review_reason) VALUES(?,?,?,?,?)",
                    (batch_id,item["fingerprint"],json.dumps(item["source"],ensure_ascii=False,sort_keys=True),json.dumps(item["normalized"],ensure_ascii=False,sort_keys=True),item["reason"]))
            db.execute("UPDATE pages SET processing_status='processed',content_hash=?,failure_reason=NULL WHERE batch_id=?",(content_hash,batch_id))
            return revision

    def notification(self, notification_id: int) -> sqlite3.Row | None:
        with self.connect() as db: return db.execute("SELECT * FROM notifications WHERE id=?",(notification_id,)).fetchone()

    def pending_notifications(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM notifications WHERE status IN ('PENDING','RETRY_PENDING') ORDER BY id").fetchall()

    def claim_notification(self, notification_id: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now=utc_now()
            cur=db.execute("UPDATE notifications SET status='PROCESSING',attempt_count=attempt_count+1,last_attempt_at=?,updated_at=?,error=NULL WHERE id=? AND status IN ('PENDING','RETRY_PENDING')",(now,now,notification_id))
            return cur.rowcount==1

    def finish_notification(self, notification_id: int, status: str, error: str | None=None) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now=utc_now()
            db.execute("UPDATE notifications SET status=?,sent_at=?,updated_at=?,error=? WHERE id=?",(status,now if status=='SENT' else None,now,error,notification_id))

    def recover_stale_notifications(self, older_than_seconds: int=300) -> int:
        from datetime import timedelta
        cutoff=(datetime.now(timezone.utc)-timedelta(seconds=older_than_seconds)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur=db.execute("UPDATE notifications SET status='RETRY_PENDING',updated_at=?,error='Recovered stale PROCESSING notification after restart' WHERE status='PROCESSING' AND updated_at<?",(utc_now(),cutoff))
            db.execute("UPDATE notification_recipients SET status='RETRY_PENDING',error='Recovered stale PROCESSING recipient' WHERE status='PROCESSING' AND last_attempt_at<?",(cutoff,))
            return cur.rowcount

    def set_recipient_status(self, notification_id: int, recipient: str, status: str, error: str | None=None, provider_message_id: str | None=None) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now=utc_now()
            db.execute("INSERT OR IGNORE INTO notification_recipients(notification_id,recipient_email,status) VALUES(?,?,?)",(notification_id,recipient,status))
            db.execute("UPDATE notification_recipients SET status=?,last_attempt_at=COALESCE(?,last_attempt_at),delivered_at=?,provider_message_id=COALESCE(?,provider_message_id),error=? WHERE notification_id=? AND recipient_email=?",(status,now if status not in {'INVALID'} else None,now if status=='SENT' else None,provider_message_id,error,notification_id,recipient))

    def mark_recipient_processing(self, notification_id: int, recipient: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now=utc_now()
            db.execute("INSERT OR IGNORE INTO notification_recipients(notification_id,recipient_email,status) VALUES(?,?, 'PENDING')",(notification_id,recipient))
            db.execute("UPDATE notification_recipients SET status='PROCESSING',attempt_count=attempt_count+1,last_attempt_at=?,error=NULL WHERE notification_id=? AND recipient_email=?",(now,notification_id,recipient))

    def recipient_statuses(self, notification_id: int) -> list[sqlite3.Row]:
        with self.connect() as db: return db.execute("SELECT * FROM notification_recipients WHERE notification_id=? ORDER BY id",(notification_id,)).fetchall()

    def retry_notification(self, notification_id: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE notification_recipients SET status='RETRY_PENDING' WHERE notification_id=? AND status='FAILED'",(notification_id,))
            cur=db.execute("UPDATE notifications SET status='RETRY_PENDING',updated_at=?,error=NULL WHERE id=? AND status IN ('FAILED','RETRY_PENDING')",(utc_now(),notification_id))
            return cur.rowcount==1

    def get_extracted_records(self, batch_id: str, include_review: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows=db.execute("SELECT fingerprint,source_data,normalized_data,match_status FROM records WHERE batch_id=? ORDER BY id",(batch_id,)).fetchall()
            result=[{"fingerprint":r[0],"source":json.loads(r[1]),"normalized":json.loads(r[2]),"match":r[3]} for r in rows]
            if include_review:
                rows=db.execute("SELECT fingerprint,source_data,normalized_data,review_reason FROM review_records WHERE batch_id=? ORDER BY id",(batch_id,)).fetchall()
                result.extend({"fingerprint":r[0],"source":json.loads(r[1]),"normalized":json.loads(r[2]),"match":"review","reason":r[3]} for r in rows)
            return result

def normalize_revision_value(value: Any) -> str:
    """Comparison normalization ignores case/punctuation/whitespace-only edits."""
    import re, unicodedata
    value=unicodedata.normalize("NFKD",str(value or "")).casefold()
    value="".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+"," ",value).strip()
