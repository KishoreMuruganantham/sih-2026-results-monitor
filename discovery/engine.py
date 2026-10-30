"""Fault-isolated discovery from official pages, URL hypotheses, and search RSS."""
from __future__ import annotations
import re
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin, urlparse
import httpx
from bs4 import BeautifulSoup
from config import Settings
from database import Database

RESULT_HINT = re.compile(r"(screening.?result|result.?batch|shortlist|shortlisted|sih.?2026)", re.I)

@dataclass(frozen=True)
class Candidate:
    url: str
    source: str

class DiscoveryEngine:
    def __init__(self, settings: Settings, database: Database, client: httpx.Client | None = None):
        self.settings=settings; self.database=database
        self.client=client or httpx.Client(timeout=settings.request_timeout, follow_redirects=True, headers={"User-Agent":"SIHResultsMonitor/1.0 (polite public page monitor)"})

    def _get(self,url: str) -> httpx.Response:
        last=None
        for attempt in range(max(1,self.settings.max_retries)):
            try:
                if attempt: time.sleep(min(2**attempt,8))
                return self.client.get(url)
            except httpx.HTTPError as exc:
                last=exc
        raise last or RuntimeError("request failed")

    def crawl(self, seeds: tuple[str,...] | None = None) -> list[Candidate]:
        found=[]
        for seed in seeds or self.settings.sih_seeds:
            try:
                response=self._get(seed)
                if response.status_code >= 400: raise httpx.HTTPStatusError(f"HTTP {response.status_code}",request=response.request,response=response)
                soup=BeautifulSoup(response.text,"html.parser")
                for a in soup.find_all("a",href=True):
                    url=urljoin(str(response.url),a["href"])
                    host=(urlparse(url).hostname or "").lower()
                    label=f"{a.get_text(' ',strip=True)} {url}"
                    if host in self.settings.discovery_domains and RESULT_HINT.search(label): found.append(Candidate(url,"official_crawl"))
                self.database.event("crawl_success",f"found {len(found)} candidate link(s)",seed)
            except Exception as exc:
                self.database.event("crawl_failure",str(exc),seed,"WARNING")
        return self._dedupe(found)

    def probe_patterns(self) -> list[Candidate]:
        candidates=[]
        for pattern in self.settings.candidate_url_patterns:
            urls=[pattern.format(n=n) for n in range(1,self.settings.candidate_batch_limit+1)] if "{n}" in pattern else [pattern]
            for url in urls:
                try:
                    response=self._get(url)
                    if response.status_code < 400 and response.url.host in self.settings.discovery_domains:
                        # Candidate only: verification remains a separate gate.
                        candidates.append(Candidate(str(response.url),"url_hypothesis"))
                except Exception as exc:
                    self.database.event("probe_failure",str(exc),url,"WARNING")
        return self._dedupe(candidates)

    def search_signals(self) -> list[Candidate]:
        """Use Bing RSS queries as discovery hints; returned links are untrusted."""
        candidates=[]
        for query in self.settings.discovery_queries:
            rss=f"https://www.bing.com/search?format=rss&q={quote_plus(query + ' site:sih.gov.in OR site:education.gov.in OR site:aicte-india.org OR site:svce.ac.in')}"
            try:
                response=self._get(rss)
                if response.status_code >= 400: raise RuntimeError(f"HTTP {response.status_code}")
                soup=BeautifulSoup(response.text,"xml")
                for item in soup.find_all("item"):
                    link=item.find("link")
                    if not link: continue
                    url=link.get_text(strip=True)
                    if (urlparse(url).hostname or "").lower() in self.settings.discovery_domains: candidates.append(Candidate(url,"search_signal"))
                self.database.event("search_success",f"{len(candidates)} candidate(s)",rss)
            except Exception as exc:
                self.database.event("search_failure",str(exc),rss,"WARNING")
        return self._dedupe(candidates)

    def discover(self) -> list[Candidate]:
        # Each method is isolated; failure of the home page does not stop probes/search.
        all_candidates=[]
        for method in (self.crawl,self.probe_patterns,self.search_signals):
            try:
                all_candidates.extend(method())
            except Exception as exc:
                self.database.event("discovery_strategy_failure",str(exc),level="WARNING")
        all_candidates=self._dedupe(all_candidates)
        for c in all_candidates:
            self.database.register_page(c.url)
        return all_candidates

    @staticmethod
    def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
        seen=set(); result=[]
        for candidate in candidates:
            if candidate.url not in seen: seen.add(candidate.url); result.append(candidate)
        return result
