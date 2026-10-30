"""Environment-backed application configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path(os.getenv("SIH_DATABASE_PATH", "data/sih_monitor.sqlite3"))
    snapshots_dir: Path = Path(os.getenv("SIH_SNAPSHOTS_DIR", "data/snapshots"))
    log_level: str = os.getenv("SIH_LOG_LEVEL", "INFO").upper()
    dry_run: bool = _bool("DRY_RUN", True)
    request_timeout: float = float(os.getenv("SIH_REQUEST_TIMEOUT", "20"))
    request_interval: float = float(os.getenv("SIH_REQUEST_INTERVAL", "2"))
    max_retries: int = int(os.getenv("SIH_MAX_RETRIES", "3"))
    sih_seeds: tuple[str, ...] = tuple(x.strip() for x in os.getenv("SIH_SEED_URLS", "https://www.sih.gov.in/").split(",") if x.strip())
    discovery_domains: tuple[str, ...] = tuple(x.strip().lower() for x in os.getenv("DISCOVERY_DOMAINS", "www.sih.gov.in,sih.gov.in,education.gov.in,aicte-india.org,svce.ac.in").split(",") if x.strip())
    discovery_queries: tuple[str, ...] = tuple(x.strip() for x in os.getenv("DISCOVERY_QUERIES", "SIH 2026 screening results,SIH 2026 shortlisted teams,SIH 2026 batch results").split(",") if x.strip())
    candidate_url_patterns: tuple[str, ...] = tuple(x.strip() for x in os.getenv("SIH_CANDIDATE_PATTERNS", "https://www.sih.gov.in/sih2026/screeningresult,https://www.sih.gov.in/sih2026/screeningresult-batch{n}").split(",") if x.strip())
    candidate_batch_limit: int = int(os.getenv("SIH_CANDIDATE_BATCH_LIMIT", "12"))
    sender_email: str = os.getenv("SIH_SENDER_EMAIL", "")
    recipients_file: Path = Path(os.getenv("SIH_RECIPIENTS_FILE", "recipients.yaml"))
    email_transport: str = os.getenv("EMAIL_TRANSPORT", "mock").strip().lower()
    allow_real_email: bool = _bool("ENABLE_REAL_EMAIL", False)
    gmail_client_file: Path = Path(os.getenv("GMAIL_OAUTH_CLIENT_FILE", "secrets/google_oauth_client.json"))
    gmail_token_file: Path = Path(os.getenv("GMAIL_OAUTH_TOKEN_FILE", "data/gmail_token.json"))
    drive_token_file: Path = Path(os.getenv("DRIVE_OAUTH_TOKEN_FILE", "secrets/drive_token.json"))
    drive_folder_id: str = os.getenv("DRIVE_FOLDER_ID", "")
    drive_folder_name: str = os.getenv("DRIVE_FOLDER_NAME", "SIH-2026-Monitor")
    @classmethod
    def from_env(cls) -> "Settings":
        return cls()
