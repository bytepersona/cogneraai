"""
Lokale Icon-Library für Discord-Embeds.

Jede Datei unter assets/icons/ wird als discord.File geladen und als
Thumbnail in ein Embed eingebunden (attachment://<dateiname>).
Unbekannte Keys geben None zurück; der Caller muss das graceful handhaben.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import discord

_ICONS_DIR = Path(__file__).parent.parent / "assets" / "icons"

# Mapping: symbolischer Name → Dateiname
ICON_NAMES: dict[str, str] = {
    "rule_warn": "rule_warn.png",
    "rule_delete": "rule_warn.png",   # gleiches Icon wie warn
    "rule_timeout": "rule_warn.png",  # gleiches Icon wie warn
    "rule_ban": "rule_ban.png",
    "info": "info.png",
}


def icon_for_decision(decision_value: str) -> str:
    """Gibt den symbolischen Icon-Namen für eine ModerationDecision zurück."""
    mapping = {
        "warn": "rule_warn",
        "delete": "rule_delete",
        "timeout": "rule_timeout",
        "ban": "rule_ban",
        "allow": "info",
    }
    return mapping.get(decision_value, "info")


def attach_thumbnail(embed: discord.Embed, name: str) -> Optional[discord.File]:
    """
    Setzt das Thumbnail des Embeds auf attachment://<dateiname> und gibt
    das discord.File zurück, das zusammen mit dem Embed gesendet werden muss.
    Gibt None zurück wenn das Icon nicht gefunden wird (Embed bleibt unverändert).
    """
    filename = ICON_NAMES.get(name)
    if not filename:
        return None
    path = _ICONS_DIR / filename
    if not path.exists():
        return None
    embed.set_thumbnail(url=f"attachment://{filename}")
    return discord.File(str(path), filename=filename)
