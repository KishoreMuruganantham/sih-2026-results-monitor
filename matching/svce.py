"""Deterministic SVCE identification with an explicit uncertainty queue."""
from __future__ import annotations
import difflib
from typing import Any
from database import stable_hash
from extraction.engine import normalize_text

SVCE_CODE="c 16572"
SVCE_NAME="sri venkateswara college of engineering"
NAME_ALIASES={SVCE_NAME,"sri venkateswara college of engineering chennai","sri venkateshwara college of engineering","sri venkateswara college of engineering svce"}
LOCATION_SUFFIXES={"chennai","kanchipuram","kancheepuram","tamil","nadu","pennalur","sriperumbudur","svce"}

class SVCEMatcher:
    def classify(self, records: list[dict[str,Any]]) -> tuple[list[dict[str,Any]],list[dict[str,Any]]]:
        matched=[]; review=[]
        for record in records:
            source=record["source"]; fields=record["normalized"]
            code=normalize_text(fields.get("institution_code",""))
            name=normalize_text(fields.get("institution_name",""))
            item={**record,"fingerprint":stable_hash(source)}
            name_match=name in NAME_ALIASES or (name.startswith(SVCE_NAME+" ") and set(name.split()[len(SVCE_NAME.split()):]).issubset(LOCATION_SUFFIXES))
            if code==SVCE_CODE or name_match:
                item["match_reason"]="AISHE code C-16572" if code==SVCE_CODE else "normalized institution name"
                matched.append(item); continue
            base_similarity=difflib.SequenceMatcher(None,name,SVCE_NAME).ratio() if name else 0
            tokens=set(name.split()); target=set(SVCE_NAME.split())
            overlap=len(tokens & target)/len(target) if target else 0
            if base_similarity>=0.68 or overlap>=0.80:
                item["reason"]="institution name resembles SVCE but is not a confirmed match"
                review.append(item)
        return matched,review
