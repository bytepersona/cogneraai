"""URLs aus Freitext extrahieren."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_URL_RE = re.compile(
    r"https?://[^\s<>\"'`)\]}>]+",
    re.IGNORECASE,
)


def extract_http_urls(text: str) -> list[str]:
    """Findet http(s)-URLs; Duplikate werden entfernt, Reihenfolge bleibt."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:!?)")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def hostname_from_url(url: str) -> str | None:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower().strip()
        return host or None
    except Exception:
        return None
