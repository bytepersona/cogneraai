"""Einheitliche Discord-Embeds für Moderationsmeldungen."""

from __future__ import annotations

from typing import Optional

import discord

from utils.models import ModerationDecision

FOOTER_USER = "ModeratorAI · Automatische Moderation"
FOOTER_MODLOG = "ModeratorAI · Moderations-Protokoll"


def color_for_decision(d: ModerationDecision) -> discord.Color:
    mapping = {
        ModerationDecision.WARN: discord.Color.gold(),
        ModerationDecision.DELETE: discord.Color.orange(),
        ModerationDecision.TIMEOUT: discord.Color.blue(),
        ModerationDecision.BAN: discord.Color.red(),
        ModerationDecision.ALLOW: discord.Color.dark_gray(),
    }
    return mapping.get(d, discord.Color.light_gray())


def title_for_user_notice(d: ModerationDecision) -> str:
    titles = {
        ModerationDecision.WARN: "Verwarnung",
        ModerationDecision.DELETE: "Nachricht entfernt",
        ModerationDecision.TIMEOUT: "Zeitlimit (Timeout)",
        ModerationDecision.BAN: "Sperre",
        ModerationDecision.ALLOW: "Hinweis",
    }
    return titles.get(d, "Mitteilung der Moderation")


def title_for_mod_log(d: ModerationDecision, *, simulated: bool) -> str:
    prefix = "[Simulation] " if simulated else ""
    titles = {
        ModerationDecision.WARN: f"{prefix}Verwarnung",
        ModerationDecision.DELETE: f"{prefix}Nachricht gelöscht",
        ModerationDecision.TIMEOUT: f"{prefix}Timeout",
        ModerationDecision.BAN: f"{prefix}Ban",
        ModerationDecision.ALLOW: f"{prefix}Hinweis",
    }
    return titles.get(d, f"{prefix}Moderation")


def build_user_notice_embed(text: str, decision: ModerationDecision) -> discord.Embed:
    """Embed für DM oder öffentlichen Fallback bei Moderationshinweisen."""
    body = (text or "").strip()[:4096]
    embed = discord.Embed(
        title=title_for_user_notice(decision),
        description=body if body else "—",
        color=color_for_decision(decision),
    )
    embed.set_footer(text=FOOTER_USER)
    return embed


def build_mod_log_embed(
    *,
    decision: ModerationDecision,
    target_display: str,
    target_id: int,
    reason: str,
    jump_url: Optional[str] = None,
    detail: Optional[str] = None,
    simulated: bool = False,
    timeout_minutes: Optional[int] = None,
    case_ref: Optional[str] = None,
    event_ref: Optional[str] = None,
) -> discord.Embed:
    """Embed für den konfigurierten Mod-Log-Kanal."""
    embed = discord.Embed(
        title=title_for_mod_log(decision, simulated=simulated),
        color=discord.Color.dark_gray() if simulated else color_for_decision(decision),
    )
    embed.add_field(
        name="Nutzer",
        value=f"{target_display}\n`{target_id}`",
        inline=True,
    )
    embed.add_field(
        name="Vorgehen",
        value=f"`{decision.value}`",
        inline=True,
    )
    if case_ref:
        embed.add_field(name="Fall-ID", value=f"`{case_ref}`", inline=True)
    if event_ref:
        embed.add_field(name="Ereignis-ID", value=f"`{event_ref}`", inline=True)
    if timeout_minutes is not None:
        embed.add_field(name="Dauer", value=f"{timeout_minutes} Min.", inline=True)

    reason_fmt = (reason or "—").strip()[:1024]
    embed.add_field(name="Grund", value=reason_fmt or "—", inline=False)

    if detail:
        embed.add_field(
            name="Details",
            value=detail.strip()[:1024],
            inline=False,
        )

    if jump_url:
        embed.add_field(name="Nachricht", value=f"[Im Kanal öffnen]({jump_url})", inline=False)

    if simulated:
        embed.description = "*Dry-Run — es wurden keine Discord-Aktionen ausgeführt.*"

    embed.set_footer(text=FOOTER_MODLOG)
    embed.timestamp = discord.utils.utcnow()
    return embed
