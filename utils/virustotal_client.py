"""
VirusTotal API v3 — URL-Reputation (vereinfacht).

Erfordert ``VIRUSTOTAL_API_KEY``. Ergebnisse werden kurz gecacht (TTL),
da das Kontingent begrenzt ist.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

VT_API = "https://www.virustotal.com/api/v3"


@dataclass(frozen=True)
class UrlScanVerdict:
    """Auswertung einer URL."""

    url: str
    malicious: int
    suspicious: int
    harmless: int
    undetected: int
    permalink: Optional[str]

    @property
    def is_positive(self) -> bool:
        """True wenn genügend Engines Alarm schlagen (policy extern)."""
        return self.malicious > 0 or self.suspicious > 0


class VirusTotalClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 25.0,
    ) -> None:
        self._headers = {"x-apikey": api_key}
        self._timeout = timeout_s
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
        )

    async def aclose(self) -> None:
        """Schließt die persistente HTTP-Session."""
        await self._client.aclose()

    def _url_id(self, url: str) -> str:
        return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")

    async def fetch_url_report(self, url: str) -> Optional[dict[str, Any]]:
        """GET /urls/{id} — None wenn nicht vorhanden."""
        uid = self._url_id(url)
        try:
            r = await self._client.get(f"{VT_API}/urls/{uid}")
        except httpx.HTTPError as exc:
            logger.warning("VirusTotal fetch_url_report Netzwerkfehler: %s", exc)
            return None
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def submit_url_scan(self, url: str) -> str:
        """POST /urls — liefert analysis_id."""
        r = await self._client.post(
            f"{VT_API}/urls",
            data={"url": url},
        )
        r.raise_for_status()
        data = r.json()
        return str(data.get("data", {}).get("id", ""))

    async def get_analysis(self, analysis_id: str) -> dict[str, Any]:
        r = await self._client.get(f"{VT_API}/analyses/{analysis_id}")
        r.raise_for_status()
        return r.json()

    async def get_url_verdict(
        self,
        url: str,
        *,
        poll_attempts: int = 6,
        poll_delay_s: float = 1.5,
    ) -> Optional[UrlScanVerdict]:
        """
        Liefert Statistiken zu einer URL; reicht ggf. Scan ein und pollt die Analyse.
        """
        try:
            data = await self.fetch_url_report(url)
        except httpx.HTTPError as exc:
            logger.warning("VirusTotal get_url_verdict Fehler für %s: %s", url[:80], exc)
            return None

        if data is None:
            try:
                aid = await self.submit_url_scan(url)
            except httpx.HTTPError as e:
                logger.warning("VirusTotal Scan-Submit fehlgeschlagen: %s", e)
                return None
            for _ in range(poll_attempts):
                await asyncio.sleep(poll_delay_s)
                try:
                    an = await self.get_analysis(aid)
                except httpx.HTTPError:
                    continue
                status = (an.get("data", {}) or {}).get("attributes", {}).get("status", "")
                if status == "completed":
                    stats = (an.get("data", {}) or {}).get("attributes", {}).get("stats", {})
                    return self._stats_to_verdict(url, stats, None)
            logger.warning("VirusTotal: Analyse-Timeout für %s", url[:80])
            return None

        attrs = (data.get("data", {}) or {}).get("attributes", {}) or {}
        stats = attrs.get("last_analysis_stats") or {}
        link = attrs.get("links", {}).get("self") if isinstance(attrs.get("links"), dict) else None
        return self._stats_to_verdict(url, stats, link)

    @staticmethod
    def _stats_to_verdict(
        url: str,
        stats: dict[str, Any],
        permalink: Optional[str],
    ) -> UrlScanVerdict:
        return UrlScanVerdict(
            url=url,
            malicious=int(stats.get("malicious", 0) or 0),
            suspicious=int(stats.get("suspicious", 0) or 0),
            harmless=int(stats.get("harmless", 0) or 0),
            undetected=int(stats.get("undetected", 0) or 0),
            permalink=permalink,
        )
