"""Append-only SQLite snapshots in a dedicated Google Drive folder.

Snapshots are immutable files linked by parent ID. Drive is not treated as a
transactional store: competing writers create a detectable fork, never replace
an older snapshot.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNAPSHOT_PREFIX = "sih-monitor-db-g"
APP_PROPERTY = "sihMonitorSnapshot"


class DriveStorageError(RuntimeError):
    """Drive unavailable, inconsistent, or unsafe to update."""


class SnapshotConflict(DriveStorageError):
    """The snapshot lineage changed or contains competing writers."""


@dataclass(frozen=True)
class Snapshot:
    file_id: str
    generation: int
    parent_id: str
    sha256: str
    md5: str
    name: str


def sqlite_integrity_check(path: str | Path) -> None:
    """Raise when a downloaded or locally generated database is not sound."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise DriveStorageError("SQLite snapshot is missing or empty")
    try:
        db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            rows = db.execute("PRAGMA integrity_check").fetchall()
            fk = db.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise DriveStorageError(f"SQLite integrity check failed: {exc}") from exc
    if rows != [("ok",)] or fk:
        raise DriveStorageError(f"SQLite integrity check failed: {rows!r}; foreign key errors={len(fk)}")


def create_sqlite_snapshot(source: str | Path, destination: str | Path) -> str:
    """Use SQLite's backup API to create and verify a consistent DB snapshot."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        source_db = sqlite3.connect(source, timeout=30)
        target_db = sqlite3.connect(destination)
        try:
            source_db.backup(target_db, pages=256)
        finally:
            source_db.close()
            target_db.close()
    except sqlite3.DatabaseError as exc:
        destination.unlink(missing_ok=True)
        raise DriveStorageError(f"Could not create SQLite backup snapshot: {exc}") from exc
    sqlite_integrity_check(destination)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


class GoogleDriveSnapshotStore:
    def __init__(self, service: Any, folder_id: str, folder_name: str = "SIH-2026-Monitor",
                 downloader_factory=None, uploader_factory=None):
        if not folder_id:
            raise DriveStorageError("DRIVE_FOLDER_ID must identify the approved SIH-2026-Monitor folder")
        self.service = service
        self.folder_id = folder_id
        self.folder_name = folder_name
        self.downloader_factory = downloader_factory
        self.uploader_factory = uploader_factory

    @classmethod
    def from_environment(cls, folder_id: str | None = None) -> "GoogleDriveSnapshotStore":
        """Create read/write service from a refresh-token JSON secret/file."""
        token_json = os.getenv("DRIVE_OAUTH_TOKEN_JSON")
        token_path = Path(os.getenv("DRIVE_OAUTH_TOKEN_FILE", "secrets/drive_token.json"))
        if token_json:
            info = json.loads(token_json)
        elif token_path.is_file():
            info = json.loads(token_path.read_text(encoding="utf-8"))
        else:
            raise DriveStorageError("Drive OAuth token is not configured")
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            credentials = Credentials.from_authorized_user_info(
                info, scopes=["https://www.googleapis.com/auth/drive.file"]
            )
            service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        except Exception as exc:
            raise DriveStorageError(f"Could not initialize Google Drive OAuth: {exc}") from exc
        return cls(service, folder_id or os.getenv("DRIVE_FOLDER_ID", ""),
                   os.getenv("DRIVE_FOLDER_NAME", "SIH-2026-Monitor"))

    def _verify_folder(self) -> None:
        try:
            folder = self.service.files().get(
                fileId=self.folder_id, fields="id,name,mimeType,trashed"
            ).execute()
        except Exception as exc:
            raise DriveStorageError(f"Could not verify the configured Drive folder: {exc}") from exc
        if (folder.get("name") != self.folder_name or
                folder.get("mimeType") != "application/vnd.google-apps.folder" or
                folder.get("trashed", False)):
            raise DriveStorageError(f"Configured Drive folder must be an active '{self.folder_name}' folder")

    def list_snapshots(self) -> list[Snapshot]:
        self._verify_folder()
        query = f"'{self.folder_id}' in parents and trashed = false"
        page_token = None
        result: list[Snapshot] = []
        try:
            while True:
                response = self.service.files().list(
                    q=query,
                    pageSize=1000,
                    pageToken=page_token,
                    fields="nextPageToken,files(id,name,size,md5Checksum,appProperties,mimeType)",
                ).execute()
                for item in response.get("files", []):
                    props = item.get("appProperties") or {}
                    if not item.get("name", "").startswith(SNAPSHOT_PREFIX) or props.get(APP_PROPERTY) != "1":
                        continue
                    try:
                        result.append(Snapshot(
                            item["id"], int(props["generation"]), props.get("parent", ""),
                            props["sha256"], item.get("md5Checksum", ""), item["name"],
                        ))
                    except (KeyError, ValueError) as exc:
                        raise DriveStorageError(f"Malformed snapshot metadata for {item.get('id')}") from exc
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except DriveStorageError:
            raise
        except Exception as exc:
            raise DriveStorageError(f"Could not list Drive snapshots: {exc}") from exc
        ids = {s.file_id for s in result}
        if len(ids) != len(result):
            raise DriveStorageError("Drive returned duplicate snapshot IDs")
        return result

    @staticmethod
    def latest(snapshots: list[Snapshot]) -> Snapshot | None:
        if not snapshots:
            return None
        children = {s.parent_id for s in snapshots if s.parent_id}
        tips = [s for s in snapshots if s.file_id not in children]
        if len(tips) != 1:
            raise SnapshotConflict(f"Snapshot history has {len(tips)} tips; manual recovery is required")
        tip = tips[0]
        # Validate the entire parent chain and monotonic generations.
        by_id = {s.file_id: s for s in snapshots}
        seen: set[str] = set()
        cur = tip
        while cur.parent_id:
            if cur.file_id in seen or cur.parent_id not in by_id:
                raise SnapshotConflict("Snapshot history has a cycle or missing parent")
            seen.add(cur.file_id)
            parent = by_id[cur.parent_id]
            if parent.generation + 1 != cur.generation:
                raise SnapshotConflict("Snapshot generations are not consecutive")
            cur = parent
        if cur.generation != 1:
            raise SnapshotConflict("Snapshot history does not start at generation 1")
        if len(seen) + 1 != len(snapshots):
            raise SnapshotConflict("Snapshot folder contains disconnected or competing history")
        return tip

    def download_latest(self, destination: str | Path) -> Snapshot | None:
        snapshots = self.list_snapshots()
        latest = self.latest(snapshots)
        if latest is None:
            return None
        try:
            if self.downloader_factory:
                downloader_type = self.downloader_factory
            else:
                from googleapiclient.http import MediaIoBaseDownload as downloader_type
            request = self.service.files().get_media(fileId=latest.file_id)
            output = io.BytesIO()
            downloader = downloader_type(output, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            payload = output.getvalue()
        except Exception as exc:
            raise DriveStorageError(f"Could not download latest Drive snapshot: {exc}") from exc
        if hashlib.sha256(payload).hexdigest() != latest.sha256:
            raise DriveStorageError("Downloaded snapshot SHA-256 does not match its manifest")
        if latest.md5 and hashlib.md5(payload).hexdigest() != latest.md5:
            raise DriveStorageError("Downloaded snapshot MD5 does not match Drive metadata")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        try:
            sqlite_integrity_check(path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return latest

    def upload_snapshot(self, snapshot_path: str | Path, expected_parent: Snapshot | None) -> Snapshot:
        """Append a verified snapshot only if the observed parent remains latest."""
        snapshot_path = Path(snapshot_path)
        sqlite_integrity_check(snapshot_path)
        payload = snapshot_path.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        md5 = hashlib.md5(payload).hexdigest()
        # Optimistic check. GitHub workflow concurrency is the primary single-writer
        # control; a cross-client race creates an append-only fork, never an overwrite.
        current = self.latest(self.list_snapshots())
        expected_id = expected_parent.file_id if expected_parent else ""
        current_id = current.file_id if current else ""
        if current_id != expected_id:
            raise SnapshotConflict("Drive snapshot advanced during this run; refusing stale upload")
        generation = (expected_parent.generation + 1) if expected_parent else 1
        name = f"{SNAPSHOT_PREFIX}{generation}-{sha256[:16]}.sqlite3"
        metadata = {
            "name": name,
            "mimeType": "application/vnd.sqlite3",
            "parents": [self.folder_id],
            "appProperties": {
                APP_PROPERTY: "1", "generation": str(generation),
                "parent": expected_id, "sha256": sha256,
            },
        }
        try:
            if self.uploader_factory:
                uploader_type = self.uploader_factory
            else:
                from googleapiclient.http import MediaIoBaseUpload as uploader_type
            media = uploader_type(io.BytesIO(payload), mimetype="application/vnd.sqlite3", resumable=True)
            item = self.service.files().create(
                body=metadata, media_body=media,
                fields="id,name,md5Checksum,appProperties",
            ).execute()
        except Exception as exc:
            raise DriveStorageError(f"Could not upload Drive snapshot: {exc}") from exc
        if item.get("md5Checksum") and item["md5Checksum"] != md5:
            raise DriveStorageError("Drive upload checksum did not match local snapshot")
        props = item.get("appProperties", {})
        if props.get("sha256") != sha256 or props.get("parent", "") != expected_id:
            raise DriveStorageError("Drive upload metadata verification failed")
        uploaded = Snapshot(item["id"], generation, expected_id, sha256, item.get("md5Checksum", md5), item["name"])
        # Detect another writer racing after the preflight. Preserve both files and
        # fail the run; next invocation will identify the fork for manual recovery.
        self.latest(self.list_snapshots())
        return uploaded
