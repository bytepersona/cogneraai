"""Domain-Allowlist für URL-Scanner (VirusTotal etc.)."""

from __future__ import annotations

from utils.url_parse import hostname_from_url


def domain_matches_allowlist(hostname: str, patterns: list[str]) -> bool:
    """
    True wenn der Hostname zu einem Eintrag passt.

    Einträge können volle Hostnamen sein oder Suffixe wie ``.discord.com``.
    """
    host = hostname.lower().strip().rstrip(".")
    if not host:
        return False
    for raw in patterns:
        p = (raw or "").strip().lower().rstrip(".")
        if not p:
            continue
        if p.startswith("."):
            if host.endswith(p[1:]) or host == p[1:]:
                return True
        elif host == p or host.endswith("." + p):
            return True
    return False


def url_is_allowlisted(url: str, patterns: list[str]) -> bool:
    host = hostname_from_url(url)
    if not host:
        return False
    return domain_matches_allowlist(host, patterns)


def default_url_allowlist() -> list[str]:
    """Sinnvolle Discord-/CDN-Defaults (können pro Guild überschrieben werden)."""
    return [
        "discord.com",
        "discordapp.com",
        "discord.gg",
        "cdn.discordapp.com",
        "media.discordapp.net",
        "tenor.com",
        "giphy.com",
    ]
