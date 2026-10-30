"""One polling cycle, wrapped by Drive download/backup/upload synchronization."""
from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from config import Settings
from database import Database
from database import utc_now
from discovery.engine import DiscoveryEngine
from extraction.engine import ExtractionEngine
from notifications.email import NotificationService
from processing.pipeline import ResultPipeline
from storage.google_drive import (
    DriveStorageError, GoogleDriveSnapshotStore, create_sqlite_snapshot, sqlite_integrity_check,
)
from verification.engine import VerificationEngine


def batch_id_for_url(url: str) -> str:
    path = urlparse(url).path
    match = re.search(r"(?:batch|result)[^/]*?(\d+)", path, re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", path.strip("/").casefold()).strip("-") or "results"
    return f"SIH2026:{match.group(1) if match else slug}"


def run_once(settings: Settings | None = None, store: GoogleDriveSnapshotStore | None = None) -> dict:
    settings = settings or Settings.from_env()
    db_path = settings.database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Never fall back to a local stale DB when Drive is configured. A failed
    # listing/download raises and aborts before pipeline mutation.
    base = store.download_latest(db_path) if store else None
    if store and base is None and db_path.exists():
        raise DriveStorageError("Drive has no committed snapshot but a local database exists; refusing stale-state bootstrap")
    if db_path.exists():
        sqlite_integrity_check(db_path)
    database = Database(db_path)
    database.recover_stale_notifications()
    notifications = NotificationService(settings, database)
    notifications.retry_failed_due(max_attempts=5)
    # Resume durable outbox work on each poll. DRY_RUN=true keeps this mock-only.
    notifications.process_pending()

    discovery = DiscoveryEngine(settings, database)
    candidates = discovery.discover()
    verifier = VerificationEngine()
    pipeline = ResultPipeline(database, ExtractionEngine(), notifications=notifications)
    processed, rejected, failed = [], [], []
    with httpx.Client(timeout=settings.request_timeout, follow_redirects=True,
                       headers={"User-Agent": "SIHResultsMonitor/1.0 (polite public page monitor)"}) as client:
        for candidate in candidates:
            try:
                response = client.get(candidate.url)
                response.raise_for_status()
                filename = Path(urlparse(str(response.url)).path).name
                result = verifier.verify_document(response.content,
                    response.headers.get("content-type", ""), filename, str(response.url))
                database.update_page(candidate.url, last_checked_at=utc_now(),
                                     verification_status="verified" if result.verified else "rejected",
                                     failure_reason=None if result.verified else result.reason)
                if not result.verified:
                    rejected.append({"url": candidate.url, "reason": result.reason})
                    continue
                batch_id = batch_id_for_url(str(response.url))
                database.update_page(candidate.url, batch_id=batch_id, edition="SIH 2026")
                page = database.page(candidate.url)
                # Use the first successful verification time for the notification;
                # a link may have been discovered well before results were posted.
                discovered = datetime.fromisoformat(page["last_checked_at"].replace("Z", "+00:00"))
                detected_at_ist = discovered.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
                outcome = pipeline.process(batch_id, response.content, result,
                                           response.headers.get("content-type", ""),
                                           filename, detected_at_ist)
                processed.append({"batch_id": batch_id, "records": len(outcome.all_records),
                                  "svce": len(outcome.svce_records)})
            except Exception as exc:
                database.update_page(candidate.url, processing_status="failed", failure_reason=str(exc))
                database.event("candidate_processing_failure", str(exc), candidate.url, "ERROR")
                failed.append({"url": candidate.url, "error": str(exc)})
                # Isolate broken/unavailable sources; other batches and outbox work
                # can still be persisted and this candidate will be retried next poll.
                continue

    notifications.process_pending()
    sqlite_integrity_check(db_path)
    uploaded = None
    if store:
        # Retain local backups for debugging/recovery on the ephemeral runner and
        # publish a consistent backup snapshot; Drive keeps immutable generations.
        with tempfile.TemporaryDirectory(prefix="sih-snapshot-") as temp_dir:
            snapshot = Path(temp_dir) / "sih-monitor.sqlite3"
            create_sqlite_snapshot(db_path, snapshot)
            uploaded = store.upload_snapshot(snapshot, base)
    return {"discovered": len(candidates), "processed": processed, "rejected": rejected, "failed": failed,
            "snapshot_generation": uploaded.generation if uploaded else None}


if __name__ == "__main__":
    drive_store = GoogleDriveSnapshotStore.from_environment()
    result = run_once(store=drive_store)
    print(result)
