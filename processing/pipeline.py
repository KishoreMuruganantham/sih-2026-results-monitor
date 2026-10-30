"""Connect verification, extraction, SVCE matching, and persistent storage."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from extraction.engine import ExtractionEngine
from matching.svce import SVCEMatcher
from verification.engine import VerificationResult
from database import Database, stable_hash
from notifications.email import NotificationService
from config import Settings

@dataclass
class ProcessedBatch:
    batch_id: str
    source_format: str
    all_records: list[dict[str,Any]]
    svce_records: list[dict[str,Any]]
    review_records: list[dict[str,Any]]
    content_hash: str
    revision: dict[str,Any] | None = None
    notification: dict[str,Any] | None = None

class ResultPipeline:
    def __init__(self,database: Database,extractor: ExtractionEngine | None=None,matcher: SVCEMatcher | None=None,notifications: NotificationService | None=None):
        self.database=database; self.extractor=extractor or ExtractionEngine(); self.matcher=matcher or SVCEMatcher()
        self.notifications=notifications or NotificationService(Settings.from_env(),database)

    def process(self,batch_id: str,data: bytes,verification: VerificationResult,content_type: str="",filename: str="",detected_at_ist: str | None=None) -> ProcessedBatch:
        if not verification.verified:
            raise ValueError(f"Refusing to process unverified result page: {verification.reason}")
        extracted=self.extractor.extract(data,content_type,filename)
        for item in extracted.records: item["fingerprint"]=stable_hash(item["source"])
        extracted.records=list({item["fingerprint"]:item for item in extracted.records}.values())
        matched,review=self.matcher.classify(extracted.records)
        matched_ids={item["fingerprint"] for item in matched}; review_ids={item["fingerprint"] for item in review}
        all_records=[]
        for item in extracted.records:
            fields=item["normalized"]
            identity=("|".join((fields.get("problem_statement_id",""),fields.get("team_id",""))) if fields.get("team_id") and fields.get("problem_statement_id") else "|".join((fields.get("problem_statement_id",""),fields.get("team_name",""),fields.get("institution_code","") or fields.get("institution_name",""))))
            all_records.append({**item,"stable_key":stable_hash(identity.casefold()),"match_status":"svce" if item["fingerprint"] in matched_ids else ("review" if item["fingerprint"] in review_ids else "other")})
        digest=stable_hash(sorted((r["stable_key"],{k:str(v) for k,v in r["normalized"].items()}) for r in all_records))
        with self.database.connect() as db:
            exists=db.execute("SELECT 1 FROM pages WHERE batch_id=? LIMIT 1",(batch_id,)).fetchone()
        if not exists: self.database.register_page(f"local://{batch_id}",batch_id=batch_id)
        source_url=self.database.page_for_batch(batch_id) or ""
        builder=None
        if self.notifications:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            detected_at=detected_at_ist or datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
            builder=self.notifications.notification_builder(batch_id,source_url,matched,review,detected_at)
        revision=self.database.save_extracted_records(batch_id,all_records,review,digest,notification_builder=builder)
        self.database.event("batch_extracted",f"format={extracted.source_format}; records={len(extracted.records)}; svce={len(matched)}; review={len(review)}")
        notification=self.notifications.process_pending(batch_id=batch_id)
        return ProcessedBatch(batch_id,extracted.source_format,extracted.records,matched,review,digest,revision,notification)
