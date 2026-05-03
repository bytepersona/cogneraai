"""
Kanonische Server-Regeln: identischer Text für Prompt/DB-Default und Katalog für Regel-IDs.

Die KI soll Regelverstöße als numerische IDs nennen (z. B. 1.1.2, 3.2.1), die mit diesem Katalog übereinstimmen.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Volltext, der im System-Prompt und als DB-Standard erscheint (Admin kann per /mod-config überschreiben).
CANONICAL_SERVER_RULES_TEXT = """Server-Regeln (immer berücksichtigen):

§1 Allgemeines Verhalten
1.1 Respektvoller Umgang: Keine Beleidigungen, persönliche Angriffe, Hassrede, Diskriminierung oder Mobbing (1.1.1–1.1.3).
1.2 Höflichkeit: Respektvolle Sprache, kein öffentliches Drama oder Streit (1.2.1–1.2.2).
1.3 Spamming: Kein Flooding, Massen-Pings, übermäßiger Emote-/Caps-Lock-Einsatz (1.3.1–1.3.3).
1.4 Werbung: Keine unautorisierten Links, Einladungen oder Selbstpromotion (1.4.1–1.4.2).
1.5 Kanalnutzung: Nur themenbezogene Beiträge (1.5.1–1.5.2).

§2 Inhalte & Medien
2.1 NSFW/Explizit: Verboten in Text, Bild, Video, Voice (2.1.1–2.1.2).
2.2 Illegales: Keine strafbaren, extremistischen oder urheberrechtsverletzenden Inhalte (2.2.1–2.2.2).
2.3 Spoiler/Sensibles: Spoiler markieren, Trigger-Warnungen empfohlen (2.3.1–2.3.2).

§3 Cybercrime & Sicherheit
3.1 Hacking: Keine Tools, Exploits, Cracks, Token-Grabber (3.1.1–3.1.2).
3.2 Phishing & Scams: Verboten (3.2.1–3.2.2).
3.3 Doxxing: Streng verboten, auch Androhung (3.3.1).
3.4 Betrug: Keine Fake-Gewinne, Account-Trading etc. (3.4.1–3.4.2).
3.5 Raubkopien: Verboten (3.5.1).

§4 Privatsphäre
4.1 Persönliche Daten: Nicht teilen oder veröffentlichen (4.1.1–4.1.2).
4.2 Aufnahmen: Keine Voice-Mitschnitte oder private Screenshots ohne Zustimmung (4.2.1–4.2.2).

§5 Technische Regeln
5.1 Nick/Avatar: Keine beleidigenden Inhalte (5.1.1).
5.2 Voice-Etikette: Kein Stören, Lärm, Trolling (5.2.1–5.2.2).
5.3 Bots: Nur bestimmungsgemäße Nutzung (5.3.1–5.3.2).

§6 Melden & Konflikte
6.1 Meldungen: Über Melde-Kanal oder privat an Moderation (6.1.1–6.1.2).
6.2 Meta-Diskussionen: Keine öffentlichen Debatten über Moderation (6.2.1–6.2.2).
"""

_RULE_ID_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

# Kurzbezeichnungen je Unterpunkt (aus dem Regelwerk abgeleitet; gleiche Kernaussage bei zusammengehörigen IDs).
_RULE_LABELS: dict[str, str] = {}
for pid, title, lo, hi, blurb in (
    ("1.1", "Respektvoller Umgang", 1, 3, "Beleidigungen, Angriffe, Hassrede, Diskriminierung, Mobbing"),
    ("1.2", "Höflichkeit", 1, 2, "respektvolle Sprache; kein Drama/Streit"),
    ("1.3", "Spamming", 1, 3, "Flooding, Massen-Pings, übermäßig Emotes/Caps"),
    ("1.4", "Werbung", 1, 2, "unautorisierte Links, Einladungen, Selbstpromotion"),
    ("1.5", "Kanalnutzung", 1, 2, "themenbezogene Beiträge"),
    ("2.1", "NSFW/Explizit", 1, 2, "explizite Inhalte (Text/Bild/Video/Voice)"),
    ("2.2", "Illegales", 1, 2, "strafbare/extremistische/urheberrechtsverletzende Inhalte"),
    ("2.3", "Spoiler/Sensibles", 1, 2, "Spoiler markieren; Trigger-Warnungen empfohlen"),
    ("3.1", "Hacking", 1, 2, "Tools, Exploits, Cracks, Token-Grabber"),
    ("3.2", "Phishing & Scams", 1, 2, "Phishing und Betrugsversuche"),
    ("3.3", "Doxxing", 1, 1, "Veröffentlichung/Androhung privater Daten"),
    ("3.4", "Betrug", 1, 2, "Fake-Gewinne, Account-Trading u. Ä."),
    ("3.5", "Raubkopien", 1, 1, "Raubkopien und illegale Kopien"),
    ("4.1", "Persönliche Daten", 1, 2, "Daten nicht teilen/veröffentlichen"),
    ("4.2", "Aufnahmen", 1, 2, "Voice/Screenshots ohne Zustimmung"),
    ("5.1", "Nick/Avatar", 1, 1, "beleidigende Nicknames/Avatare"),
    ("5.2", "Voice-Etikette", 1, 2, "Störungen, Lärm, Trolling"),
    ("5.3", "Bots", 1, 2, "nur bestimmungsgemäße Bot-Nutzung"),
    ("6.1", "Meldungen", 1, 2, "Meldung über Kanal oder privat an Mods"),
    ("6.2", "Meta-Diskussionen", 1, 2, "keine öffentlichen Mod-Debatten"),
):
    for n in range(lo, hi + 1):
        rid = f"{pid}.{n}"
        _RULE_LABELS[rid] = f"§{pid.split('.')[0]}.{pid} {title}: {blurb}"

KNOWN_RULE_IDS: frozenset[str] = frozenset(_RULE_LABELS.keys())


def normalize_violated_rule_ids(raw: Any) -> list[str]:
    """Akzeptiert Liste oder Einzelstring; dedupliziert; nur gültiges ID-Format."""
    if raw is None:
        return []
    items: Iterable[Any]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        items = re.split(r"[\s,;]+", s)
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        x = str(item).strip()
        if not x or x in seen:
            continue
        if _RULE_ID_PATTERN.match(x):
            seen.add(x)
            out.append(x)
    return out


def format_rule_ids_line(ids: list[str]) -> str:
    """Eine kompakte Zeile für Embeds (Discord-Feldlängen beachten)."""
    if not ids:
        return "—"
    parts = [f"`{i}`" for i in ids]
    return ", ".join(parts)


def format_rule_ids_with_labels(ids: list[str], *, max_chars: int = 900) -> str:
    """IDs mit Kurzlabels; unbekannte IDs werden dennoch ausgegeben."""
    lines: list[str] = []
    for rid in ids:
        label = _RULE_LABELS.get(rid)
        if label:
            lines.append(f"**{rid}** — {label}")
        else:
            lines.append(f"**{rid}** — (nicht im lokalen Regelkatalog)")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
