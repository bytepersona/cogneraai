"""Slash-Commands für Konfiguration und manuelle Moderation."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core_bot import ModerationBot
from .case_views import CasesPaginatorView, CheckEvaluationView, entry_embed
from utils.discord_embeds import build_check_result_embed, build_user_notice_embed
from utils.models import ModerationDecision
from utils.prompts import MODERATOR_AI_SYSTEM_PROMPT, build_user_payload

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_URL = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)",
)


def _manage_guild_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CommandError("Nur auf einem Server verfügbar.")
        if not isinstance(interaction.user, discord.Member):
            return False
        return interaction.user.guild_permissions.manage_guild

    return app_commands.check(predicate)


def _check_inspect_permissions():
    """
    Mindestens eine: Protokoll anzeigen, Mitglieder bannen, Nachrichten verwalten
    (entspricht „view audit log“ / „ban members“ / „check messages“).
    """

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        if not isinstance(interaction.user, discord.Member):
            return False
        p = interaction.user.guild_permissions
        if p.administrator:
            return True
        if p.view_audit_log or p.ban_members or p.manage_messages:
            return True
        raise app_commands.CheckFailure(
            "Mindestens eine Berechtigung nötig: **Protokoll anzeigen**, **Mitglieder bannen** "
            "oder **Nachrichten verwalten**."
        )

    return app_commands.check(predicate)


class AdminCog(commands.Cog):
    """Administrative Slash-Befehle."""

    def __init__(self, bot: ModerationBot) -> None:
        self.bot = bot

    mod_config = app_commands.Group(
        name="mod-config",
        description="KI-Moderation und Server-Regeln konfigurieren",
    )

    @mod_config.command(name="rules", description="Server-Regeln für ModeratorAI setzen")
    @app_commands.describe(regeln="Voller Text der Hausregeln (Platzhalter im System-Prompt)")
    @_manage_guild_check()
    async def mod_config_rules(self, interaction: discord.Interaction, regeln: str) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await self.bot.db.upsert_guild_config(interaction.guild.id, server_rules=regeln)
        await interaction.response.send_message("Server-Regeln wurden gespeichert.", ephemeral=True)

    @mod_config.command(name="threshold", description="Confidence-Schwellenwert pro Guild (0–100)")
    @app_commands.describe(wert="Unter diesem Wert wird Sonnet nachgeschaltet (sofern nicht Ban/kritisch)")
    @_manage_guild_check()
    async def mod_config_threshold(self, interaction: discord.Interaction, wert: int) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        wert = max(0, min(100, wert))
        await self.bot.db.upsert_guild_config(interaction.guild.id, confidence_threshold=wert)
        await interaction.response.send_message(
            f"Schwellenwert auf **{wert}** gesetzt.",
            ephemeral=True,
        )

    @mod_config.command(name="modlog-channel", description="Kanal für Moderations-Hinweise (optional)")
    @app_commands.describe(kanal="Textkanal oder leer lassen zum Zurücksetzen")
    @_manage_guild_check()
    async def mod_config_modlog(
        self,
        interaction: discord.Interaction,
        kanal: Optional[discord.TextChannel] = None,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        cid = kanal.id if kanal else None
        await self.bot.db.upsert_guild_config(interaction.guild.id, mod_log_channel_id=cid)
        await interaction.response.send_message(
            "Mod-Log-Kanal aktualisiert." if kanal else "Mod-Log-Kanal zurückgesetzt.",
            ephemeral=True,
        )

    @mod_config.command(name="ai", description="KI-Moderation ein- oder ausschalten")
    @app_commands.describe(aktiv="True = Nachrichten werden analysiert")
    @_manage_guild_check()
    async def mod_config_ai(self, interaction: discord.Interaction, aktiv: bool) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await self.bot.db.upsert_guild_config(interaction.guild.id, ai_enabled=aktiv)
        await interaction.response.send_message(
            f"KI-Moderation: **{'an' if aktiv else 'aus'}**.",
            ephemeral=True,
        )

    @mod_config.command(name="whitelist-user-add", description="Nutzer von der KI-Moderation ausnehmen")
    @app_commands.describe(nutzer="Mitglied")
    @_manage_guild_check()
    async def wl_user_add(self, interaction: discord.Interaction, nutzer: discord.Member) -> None:
        await self._wl_user(interaction, nutzer.id, add=True)

    @mod_config.command(name="whitelist-user-remove", description="Nutzer wieder moderieren")
    @app_commands.describe(nutzer="Mitglied")
    @_manage_guild_check()
    async def wl_user_remove(self, interaction: discord.Interaction, nutzer: discord.Member) -> None:
        await self._wl_user(interaction, nutzer.id, add=False)

    async def _wl_user(
        self,
        interaction: discord.Interaction,
        user_id: int,
        *,
        add: bool,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        users: list[int] = list(cfg["whitelist_user_ids"])
        if add and user_id not in users:
            users.append(user_id)
        if not add and user_id in users:
            users.remove(user_id)
        await self.bot.db.upsert_guild_config(interaction.guild.id, whitelist_user_ids=users)
        await interaction.response.send_message("Whitelist (Nutzer) aktualisiert.", ephemeral=True)

    @mod_config.command(name="whitelist-role-add", description="Rolle von der KI-Moderation ausnehmen")
    @_manage_guild_check()
    async def wl_role_add(self, interaction: discord.Interaction, rolle: discord.Role) -> None:
        await self._wl_role(interaction, rolle.id, add=True)

    @mod_config.command(name="whitelist-role-remove", description="Rolle wieder moderieren")
    @_manage_guild_check()
    async def wl_role_remove(self, interaction: discord.Interaction, rolle: discord.Role) -> None:
        await self._wl_role(interaction, rolle.id, add=False)

    async def _wl_role(
        self,
        interaction: discord.Interaction,
        role_id: int,
        *,
        add: bool,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        roles: list[int] = list(cfg["whitelist_role_ids"])
        if add and role_id not in roles:
            roles.append(role_id)
        if not add and role_id in roles:
            roles.remove(role_id)
        await self.bot.db.upsert_guild_config(interaction.guild.id, whitelist_role_ids=roles)
        await interaction.response.send_message("Whitelist (Rolle) aktualisiert.", ephemeral=True)

    @mod_config.command(name="whitelist-channel-add", description="Kanal von der KI-Moderation ausnehmen")
    @_manage_guild_check()
    async def wl_ch_add(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel,
    ) -> None:
        await self._wl_ch(interaction, kanal.id, add=True)

    @mod_config.command(name="whitelist-channel-remove", description="Kanal wieder moderieren")
    @_manage_guild_check()
    async def wl_ch_remove(
        self,
        interaction: discord.Interaction,
        kanal: discord.TextChannel,
    ) -> None:
        await self._wl_ch(interaction, kanal.id, add=False)

    async def _wl_ch(
        self,
        interaction: discord.Interaction,
        channel_id: int,
        *,
        add: bool,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        chs: list[int] = list(cfg["whitelist_channel_ids"])
        if add and channel_id not in chs:
            chs.append(channel_id)
        if not add and channel_id in chs:
            chs.remove(channel_id)
        await self.bot.db.upsert_guild_config(interaction.guild.id, whitelist_channel_ids=chs)
        await interaction.response.send_message("Whitelist (Kanal) aktualisiert.", ephemeral=True)

    @mod_config.command(name="default-timeout", description="Standard-Timeout-Dauer in Minuten")
    @app_commands.describe(minuten="Fallback wenn das Modell keine Dauer setzt")
    @_manage_guild_check()
    async def mod_config_default_timeout(self, interaction: discord.Interaction, minuten: int) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        minuten = max(1, min(minuten, 40320))
        await self.bot.db.upsert_guild_config(
            interaction.guild.id,
            default_timeout_minutes=minuten,
        )
        await interaction.response.send_message(
            f"Standard-Timeout: **{minuten}** Minuten.",
            ephemeral=True,
        )

    @mod_config.command(name="dry-run", description="Shadow-Modus: KI loggt nur, führt keine Discord-Aktion aus")
    @app_commands.describe(aktiv="True = keine Deletes/Bans/Timeouts/DMs durch KI")
    @_manage_guild_check()
    async def mod_config_dry_run(self, interaction: discord.Interaction, aktiv: bool) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await self.bot.db.upsert_guild_config(interaction.guild.id, dry_run=aktiv)
        await interaction.response.send_message(
            f"Dry-Run (Shadow): **{'an' if aktiv else 'aus'}**.",
            ephemeral=True,
        )

    @mod_config.command(name="review-queue", description="Review-Warteschlange für Ban/low confidence aktivieren")
    @app_commands.describe(aktiv="True = kritische Fälle als Buttons im Mod-Log")
    @_manage_guild_check()
    async def mod_config_review_queue(self, interaction: discord.Interaction, aktiv: bool) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await self.bot.db.upsert_guild_config(interaction.guild.id, review_queue_enabled=aktiv)
        await interaction.response.send_message(
            f"Review-Queue: **{'an' if aktiv else 'aus'}**.",
            ephemeral=True,
        )

    @mod_config.command(name="review-floor", description="Unter dieser Confidence → Review-Queue (zusätzlich zu Ban)")
    @app_commands.describe(wert="0–100")
    @_manage_guild_check()
    async def mod_config_review_floor(self, interaction: discord.Interaction, wert: int) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        wert = max(0, min(100, wert))
        await self.bot.db.upsert_guild_config(interaction.guild.id, review_confidence_floor=wert)
        await interaction.response.send_message(f"Review-Floor: **{wert}**.", ephemeral=True)

    @mod_config.command(name="strikes", description="Strike-Eskalation (Cap von Ban auf mildere Aktionen)")
    @app_commands.describe(aktiv="True = Strikes begrenzen schwere KI-Aktionen")
    @_manage_guild_check()
    async def mod_config_strikes(self, interaction: discord.Interaction, aktiv: bool) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await self.bot.db.upsert_guild_config(interaction.guild.id, strike_escalation_enabled=aktiv)
        await interaction.response.send_message(
            f"Strike-Eskalation: **{'an' if aktiv else 'aus'}**.",
            ephemeral=True,
        )

    @mod_config.command(name="report-channel", description="Kanal für Nutzer-/report-Benachrichtigungen")
    @app_commands.describe(kanal="Textkanal oder leer zum Zurücksetzen")
    @_manage_guild_check()
    async def mod_config_report_channel(
        self,
        interaction: discord.Interaction,
        kanal: Optional[discord.TextChannel] = None,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        rid = kanal.id if kanal else None
        await self.bot.db.upsert_guild_config(interaction.guild.id, report_channel_id=rid)
        await interaction.response.send_message(
            "Report-Kanal gesetzt." if kanal else "Report-Kanal zurückgesetzt.",
            ephemeral=True,
        )

    @mod_config.command(
        name="url-scan",
        description="VirusTotal-URL-Prüfung (nur mit VIRUSTOTAL_API_KEY im Bot; pro Domain-Allowlist)",
    )
    @app_commands.describe(aktiv="True = nicht allowlistete Links vor der KI prüfen")
    @_manage_guild_check()
    async def mod_config_url_scan(self, interaction: discord.Interaction, aktiv: bool) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        if aktiv and getattr(self.bot, "vt_client", None) is None:
            await interaction.response.send_message(
                "VirusTotal ist auf diesem Bot nicht konfiguriert (`VIRUSTOTAL_API_KEY`).",
                ephemeral=True,
            )
            return
        await self.bot.db.upsert_guild_config(interaction.guild.id, url_scan_enabled=aktiv)
        await interaction.response.send_message(
            f"URL-Scan (VirusTotal): **{'an' if aktiv else 'aus'}**.",
            ephemeral=True,
        )

    @mod_config.command(name="url-allow-add", description="Domain zur URL-Allowlist hinzufügen (z. B. example.com)")
    @app_commands.describe(domain="Hostname, optional mit führendem Punkt als Suffix: .github.com")
    @_manage_guild_check()
    async def mod_config_url_allow_add(
        self,
        interaction: discord.Interaction,
        domain: str,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        d = domain.strip().lower()
        if not d:
            await interaction.response.send_message("Ungültige Domain.", ephemeral=True)
            return
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        doms: list[str] = list(cfg["url_allowlist_domains"])
        if d not in doms:
            doms.append(d)
        await self.bot.db.upsert_guild_config(interaction.guild.id, url_allowlist_domains=doms)
        await interaction.response.send_message(f"Allowlist: **{d}** ergänzt.", ephemeral=True)

    @mod_config.command(name="url-allow-remove", description="Domain von der URL-Allowlist entfernen")
    @app_commands.describe(domain="Gleiche Schreibweise wie bei add")
    @_manage_guild_check()
    async def mod_config_url_allow_remove(
        self,
        interaction: discord.Interaction,
        domain: str,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        d = domain.strip().lower()
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        doms = [x for x in cfg["url_allowlist_domains"] if x != d]
        await self.bot.db.upsert_guild_config(interaction.guild.id, url_allowlist_domains=doms)
        await interaction.response.send_message(f"Allowlist: **{d}** entfernt (falls vorhanden).", ephemeral=True)

    @mod_config.command(name="vt-thresholds", description="VirusTotal-Schwellen (malicious / suspicious Counts)")
    @app_commands.describe(
        malicious="Ab dieser Anzahl „malicious“-Engines wird blockiert",
        suspicious="Ab dieser Anzahl „suspicious“-Engines wird blockiert",
    )
    @_manage_guild_check()
    async def mod_config_vt_thresholds(
        self,
        interaction: discord.Interaction,
        malicious: int = 1,
        suspicious: int = 3,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        malicious = max(1, min(50, malicious))
        suspicious = max(0, min(50, suspicious))
        await self.bot.db.upsert_guild_config(
            interaction.guild.id,
            vt_malicious_threshold=malicious,
            vt_suspicious_threshold=suspicious,
        )
        await interaction.response.send_message(
            f"VirusTotal-Schwellen: malicious ≥ **{malicious}**, suspicious ≥ **{suspicious}**.",
            ephemeral=True,
        )

    @mod_config.command(
        name="mod-embed-ttl",
        description=(
            "Auto-Löschen von Bot-Hinweisen (Mod-Log/Review) in Sekunden; überschreibt .env-Standard."
        ),
    )
    @app_commands.describe(sekunden="1–600 Sekunden bis zur automatischen Löschung der Bot-Hinweise")
    @_manage_guild_check()
    async def mod_config_mod_embed_ttl(self, interaction: discord.Interaction, sekunden: int) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        s = max(1, min(600, sekunden))
        await self.bot.db.upsert_guild_config(
            interaction.guild.id,
            mod_embed_delete_after_seconds=s,
        )
        await interaction.response.send_message(
            f"Mod-Embed-TTL: **{s}** Sekunden (Bot-Hinweise im Mod-Log / Review).",
            ephemeral=True,
        )

    @mod_config.command(
        name="mod-embed-ttl-reset",
        description="Mod-Embed-TTL wieder auf globale BOT_MESSAGE_DELETE_AFTER_SECONDS aus .env setzen",
    )
    @_manage_guild_check()
    async def mod_config_mod_embed_ttl_reset(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await self.bot.db.upsert_guild_config(
            interaction.guild.id,
            mod_embed_delete_after_seconds=None,
        )
        await interaction.response.send_message(
            "Mod-Embed-TTL: wieder **globale** Zeit aus der Bot-Konfiguration (.env).",
            ephemeral=True,
        )

    @mod_config.command(name="status", description="Aktuelle Serverkonfiguration als Embed anzeigen")
    @_manage_guild_check()
    async def mod_config_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        except Exception:
            logger.exception("/mod-config status DB-Fehler")
            await interaction.followup.send("Datenbankfehler.", ephemeral=True)
            return

        def yn(val: object) -> str:
            return "✅ Ja" if val else "❌ Nein"

        emb = discord.Embed(
            title=f"Konfiguration: {interaction.guild.name}",
            color=discord.Color.blurple(),
        )

        modlog_ch = cfg.get("mod_log_channel_id")
        report_ch = cfg.get("report_channel_id")
        review_ch = cfg.get("review_channel_id")
        emb.add_field(name="KI-Moderation", value=yn(cfg.get("ai_enabled", True)), inline=True)
        emb.add_field(name="Dry-Run", value=yn(cfg.get("dry_run", False)), inline=True)
        emb.add_field(name="Confidence-Schwelle", value=str(cfg.get("confidence_threshold", 60)), inline=True)
        emb.add_field(name="Mod-Log-Kanal", value=f"<#{modlog_ch}>" if modlog_ch else "–", inline=True)
        emb.add_field(name="Report-Kanal", value=f"<#{report_ch}>" if report_ch else "–", inline=True)
        emb.add_field(name="Review-Kanal", value=f"<#{review_ch}>" if review_ch else "–", inline=True)

        embed_ttl = cfg.get("mod_embed_delete_after_seconds")
        global_ttl = self.bot.settings.bot_message_delete_after_seconds
        ttl_display = f"{embed_ttl}s (Guild)" if embed_ttl else f"{global_ttl}s (.env)"
        emb.add_field(name="Embed-TTL", value=ttl_display, inline=True)

        vt_enabled = yn(cfg.get("url_scan_enabled", False))
        vt_m = cfg.get("vt_malicious_threshold", 1)
        vt_s = cfg.get("vt_suspicious_threshold", 3)
        emb.add_field(name="URL-Scan (VirusTotal)", value=f"{vt_enabled} | malicious≥{vt_m}, suspicious≥{vt_s}", inline=False)

        allowlist = cfg.get("url_allowlist_domains", [])
        allowlist_text = ", ".join(f"`{d}`" for d in allowlist[:10]) if allowlist else "–"
        if len(allowlist) > 10:
            allowlist_text += f" (+{len(allowlist) - 10} weitere)"
        emb.add_field(name=f"URL-Allowlist ({len(allowlist)})", value=allowlist_text, inline=False)

        wl_users = cfg.get("whitelist_user_ids", [])
        wl_roles = cfg.get("whitelist_role_ids", [])
        wl_chans = cfg.get("whitelist_channel_ids", [])
        emb.add_field(
            name="Whitelist",
            value=f"Nutzer: {len(wl_users)} | Rollen: {len(wl_roles)} | Kanäle: {len(wl_chans)}",
            inline=True,
        )

        strikes_enabled = yn(cfg.get("strike_escalation_enabled", True))
        emb.add_field(name="Strike-Eskalation", value=strikes_enabled, inline=True)

        emb.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=emb, ephemeral=True)

    @mod_config.command(name="strike-tier-set", description="Strike-Tier setzen: bis zu X Strikes → max. Aktion")
    @app_commands.describe(
        bis_strikes="Obergrenze Strikes (z.B. 2 = bis 2 Strikes gilt dieser Tier)",
        aktion="warn / delete / timeout / ban",
    )
    @_manage_guild_check()
    async def mod_config_strike_tier_set(
        self,
        interaction: discord.Interaction,
        bis_strikes: int,
        aktion: str,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        aktion = aktion.strip().lower()
        valid_actions = {"allow", "warn", "delete", "timeout", "ban"}
        if aktion not in valid_actions:
            await interaction.response.send_message(
                f"Ungültige Aktion. Erlaubt: {', '.join(valid_actions)}",
                ephemeral=True,
            )
            return
        bis_strikes = max(1, min(999999, bis_strikes))
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        try:
            esc = json.loads(cfg.get("strike_escalation_json") or "{}")
        except json.JSONDecodeError:
            esc = {}
        tiers: list[dict] = esc.get("tiers", [])
        updated = False
        for tier in tiers:
            if int(tier.get("ceil_strikes", 0)) == bis_strikes:
                tier["cap"] = aktion
                updated = True
                break
        if not updated:
            tiers.append({"ceil_strikes": bis_strikes, "cap": aktion})
        tiers.sort(key=lambda t: int(t.get("ceil_strikes", 999999)))
        esc["tiers"] = tiers
        await self.bot.db.upsert_guild_config(
            interaction.guild.id,
            strike_escalation_json=json.dumps(esc, ensure_ascii=False),
        )
        await interaction.response.send_message(
            f"Strike-Tier gesetzt: bis **{bis_strikes}** Strikes → max. **{aktion}**.",
            ephemeral=True,
        )

    @mod_config.command(name="strike-tier-list", description="Aktuelle Strike-Tiers anzeigen")
    @_manage_guild_check()
    async def mod_config_strike_tier_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        enabled = yn_local = cfg.get("strike_escalation_enabled", True)
        try:
            esc = json.loads(cfg.get("strike_escalation_json") or "{}")
        except json.JSONDecodeError:
            esc = {}
        tiers: list[dict] = sorted(esc.get("tiers", []), key=lambda t: int(t.get("ceil_strikes", 999999)))
        if not tiers:
            await interaction.followup.send("Keine Strike-Tiers konfiguriert (Standard wird verwendet).", ephemeral=True)
            return
        emb = discord.Embed(
            title="Strike-Tiers",
            color=discord.Color.orange(),
            description=f"Strike-Eskalation: {'✅ Aktiv' if enabled else '❌ Deaktiviert'}",
        )
        for tier in tiers:
            ceil_s = tier.get("ceil_strikes", "?")
            cap = tier.get("cap", "?")
            emb.add_field(name=f"≤ {ceil_s} Strikes", value=f"max. **{cap}**", inline=True)
        await interaction.followup.send(embed=emb, ephemeral=True)

    @app_commands.command(name="warn", description="Manuelle Verwarnung (DM mit Fallback)")
    @app_commands.describe(mitglied="Zielnutzer", grund="Kurzer Grund")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def slash_warn(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
        grund: str,
    ) -> None:
        if interaction.guild is None or interaction.user is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        text = f"Du hast eine Verwarnung erhalten: {grund}"
        warn_embed = build_user_notice_embed(text, ModerationDecision.WARN)
        try:
            await mitglied.send(embed=warn_embed)
        except discord.Forbidden:
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                try:
                    await interaction.channel.send(
                        content=mitglied.mention,
                        embed=warn_embed,
                        delete_after=120,
                    )
                except discord.HTTPException:
                    pass
        except discord.HTTPException as exc:
            logger.warning("Verwarnung DM fehlgeschlagen: %s", exc)
        try:
            await self.bot.db.add_warning(
                interaction.guild.id,
                mitglied.id,
                grund,
                moderator_id=interaction.user.id,
                source="manual",
            )
            await self.bot.db.add_mod_log(
                interaction.guild.id,
                mitglied.id,
                "warn",
                grund,
                channel_id=interaction.channel.id if interaction.channel else None,
                actor_id=interaction.user.id,
                details="Manuelle Verwarnung",
            )
        except Exception:
            logger.exception("DB-Fehler beim Speichern der Verwarnung")
            await interaction.followup.send("Verwarnung konnte nicht gespeichert werden.", ephemeral=True)
            return
        await interaction.followup.send("Verwarnung protokolliert.", ephemeral=True)

    @app_commands.command(name="ban", description="Nutzer permanent bannen")
    @app_commands.describe(
        mitglied="Zielnutzer",
        grund="Grund",
        nachrichten_tage="Nachrichten dieses Nutzers der letzten X Tage löschen (0–7)",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def slash_ban(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
        grund: str,
        nachrichten_tage: int = 0,
    ) -> None:
        if interaction.guild is None or interaction.user is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        days = max(0, min(7, nachrichten_tage))
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.guild.ban(mitglied, reason=grund, delete_message_days=days)
        except discord.Forbidden:
            await interaction.followup.send("Fehlende Rechte zum Bannen.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Discord-Fehler: {exc}", ephemeral=True)
            return
        try:
            await self.bot.db.add_mod_log(
                interaction.guild.id,
                mitglied.id,
                "ban",
                grund,
                channel_id=interaction.channel.id if interaction.channel else None,
                actor_id=interaction.user.id,
                details=f"Manueller Ban, delete_message_days={days}",
            )
        except Exception:
            logger.exception("DB-Fehler beim Speichern des Ban-Logs")
        await interaction.followup.send("Nutzer gebannt.", ephemeral=True)

    @app_commands.command(name="mod-logs", description="Letzte Moderations-Einträge anzeigen")
    @app_commands.describe(nutzer="Optional filtern", limit="Anzahl (max 25)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def slash_mod_logs(
        self,
        interaction: discord.Interaction,
        nutzer: Optional[discord.Member] = None,
        limit: int = 10,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        limit = max(1, min(25, limit))
        try:
            logs = await self.bot.db.fetch_mod_logs(
                interaction.guild.id,
                limit=limit,
                target_user_id=nutzer.id if nutzer else None,
            )
        except Exception:
            logger.exception("DB-Fehler bei mod-logs")
            await interaction.followup.send("Datenbankfehler.", ephemeral=True)
            return
        if not logs:
            await interaction.followup.send("Keine Einträge.", ephemeral=True)
            return
        lines = []
        for e in logs:
            cref = f" `{e.case_ref}`" if getattr(e, "case_ref", None) else ""
            lines.append(
                f"`{e.created_at_iso}`{cref} **{e.action}** Ziel={e.target_user_id} — {e.reason[:80]}",
            )
        text = "\n".join(lines)[:3500]
        await interaction.followup.send(text, ephemeral=True)

    @app_commands.command(name="mod-stats", description="Aggregierte Moderationsaktionen der letzten Tage")
    @app_commands.describe(tage="Zeitraum in Tagen")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def slash_mod_stats(self, interaction: discord.Interaction, tage: int = 7) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        stats = await self.bot.db.aggregate_mod_actions(interaction.guild.id, days=tage)
        if not stats:
            await interaction.followup.send("Keine Einträge im Zeitraum.", ephemeral=True)
            return
        lines = [f"**Moderation ({tage} Tage)**"] + [f"- `{k}`: **{v}**" for k, v in sorted(stats.items())]
        await interaction.followup.send("\n".join(lines)[:3500], ephemeral=True)

    @app_commands.command(name="mod-export", description="Moderations-Logs als CSV oder JSON exportieren")
    @app_commands.describe(
        export_fmt="Exportformat",
        tage="Nur Einträge der letzten X Tage (optional, sonst letzte Zeilen)",
        limit="Max. Zeilen",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def slash_mod_export(
        self,
        interaction: discord.Interaction,
        export_fmt: str,
        tage: Optional[int] = None,
        limit: int = 2000,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        limit = max(1, min(5000, limit))
        try:
            logs = await self.bot.db.fetch_mod_logs_range(
                interaction.guild.id,
                limit=limit,
                days=tage,
            )
        except Exception:
            logger.exception("DB-Fehler bei mod-export")
            await interaction.followup.send("Datenbankfehler.", ephemeral=True)
            return
        if not logs:
            await interaction.followup.send("Keine Daten.", ephemeral=True)
            return
        fmt = export_fmt.lower().strip()
        if fmt == "json":
            payload = [
                {
                    "id": e.id,
                    "created_at_iso": e.created_at_iso,
                    "action": e.action,
                    "target_user_id": e.target_user_id,
                    "actor_id": e.actor_id,
                    "reason": e.reason,
                    "details": e.details,
                    "channel_id": e.channel_id,
                    "case_ref": e.case_ref,
                    "evaluation_json": e.evaluation_json,
                    "message_content_snapshot": e.message_content_snapshot,
                }
                for e in logs
            ]
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            f = discord.File(io.BytesIO(data), filename="mod_logs.json")
            await interaction.followup.send("Export:", file=f, ephemeral=True)
            return
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(
                ["id", "created_at_iso", "action", "target_user_id", "actor_id", "reason", "channel_id"],
            )
            for e in logs:
                w.writerow(
                    [
                        e.id,
                        e.created_at_iso,
                        e.action,
                        e.target_user_id,
                        e.actor_id or "",
                        e.reason.replace("\n", " ")[:500],
                        e.channel_id or "",
                    ],
                )
            f = discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename="mod_logs.csv")
            await interaction.followup.send("Export:", file=f, ephemeral=True)
            return
        await interaction.followup.send("Format: `csv` oder `json`.", ephemeral=True)

    @app_commands.command(name="report", description="Nachricht an die Moderation melden")
    @app_commands.describe(
        grund="Kurze Beschreibung",
        nachrichten_link="Discord-Link zur Nachricht (discord.com/channels/...)",
    )
    async def slash_report(
        self,
        interaction: discord.Interaction,
        grund: str,
        nachrichten_link: str,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        uid = interaction.user.id
        report_cache = self.bot.report_rate_cache
        count = report_cache.get(uid, 0)
        if count >= 3:
            await interaction.response.send_message(
                "Du hast das Report-Limit (3 Reports/Stunde) erreicht. Bitte warte.",
                ephemeral=True,
            )
            return
        report_cache[uid] = count + 1
        await interaction.response.defer(ephemeral=True)
        m = DISCORD_MESSAGE_URL.match(nachrichten_link.strip())
        if not m:
            await interaction.followup.send("Ungültiger Nachrichten-Link.", ephemeral=True)
            return
        g_id, ch_id, msg_id = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if g_id != interaction.guild.id:
            await interaction.followup.send("Link gehört zu einem anderen Server.", ephemeral=True)
            return
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        rcid = cfg.get("report_channel_id")
        if not rcid:
            await interaction.followup.send(
                "Kein Report-Kanal konfiguriert (/mod-config report-channel).",
                ephemeral=True,
            )
            return
        rch = interaction.guild.get_channel(int(rcid))
        if not isinstance(rch, discord.TextChannel):
            await interaction.followup.send("Report-Kanal ungültig.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(ch_id)
        ch_name = ch.name if isinstance(ch, discord.TextChannel) else str(ch_id)
        embed = discord.Embed(
            title="Nutzer-Report",
            description=grund[:2000],
            color=discord.Color.red(),
        )
        embed.add_field(name="Melder", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name="Kanal", value=f"#{ch_name} ({ch_id})", inline=True)
        embed.add_field(name="Nachricht", value=f"[Öffnen]({nachrichten_link})", inline=True)
        try:
            await rch.send(embed=embed)
        except discord.HTTPException:
            await interaction.followup.send("Konnte Report nicht posten.", ephemeral=True)
            return
        await interaction.followup.send("Report übermittelt.", ephemeral=True)

    @app_commands.command(name="profile", description="Zeigt Profil eines Nutzers: Avatar, Banner, ID, Beitrittsdatum")
    @app_commands.describe(nutzer="Mitglied oder User-ID (leer = eigenes Profil)")
    async def slash_profile(
        self,
        interaction: discord.Interaction,
        nutzer: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        target_member: Optional[discord.Member] = nutzer or (
            interaction.user if isinstance(interaction.user, discord.Member) else None
        )
        if target_member is None:
            await interaction.followup.send("Konnte Nutzer nicht auflösen.", ephemeral=True)
            return

        try:
            full_user = await self.bot.fetch_user(target_member.id)
        except discord.HTTPException:
            full_user = target_member

        emb = discord.Embed(
            title=str(target_member),
            color=target_member.color if target_member.color.value else discord.Color.blurple(),
        )
        emb.set_thumbnail(url=target_member.display_avatar.url)

        if getattr(full_user, "banner", None):
            emb.set_image(url=full_user.banner.url)

        emb.add_field(name="ID", value=f"`{target_member.id}`", inline=True)
        emb.add_field(
            name="Konto erstellt",
            value=discord.utils.format_dt(target_member.created_at, "F")
            + f" ({discord.utils.format_dt(target_member.created_at, 'R')})",
            inline=False,
        )
        if target_member.joined_at:
            emb.add_field(
                name="Server beigetreten",
                value=discord.utils.format_dt(target_member.joined_at, "F")
                + f" ({discord.utils.format_dt(target_member.joined_at, 'R')})",
                inline=False,
            )

        nick = target_member.nick
        if nick:
            emb.add_field(name="Nickname", value=nick, inline=True)

        status = getattr(target_member, "status", None)
        if status is not None:
            emb.add_field(name="Status", value=str(status).capitalize(), inline=True)

        roles = [r.mention for r in reversed(target_member.roles) if r.name != "@everyone"]
        if roles:
            roles_text = " ".join(roles[:20])
            if len(roles) > 20:
                roles_text += f" (+{len(roles) - 20} weitere)"
            emb.add_field(name=f"Rollen ({len(roles)})", value=roles_text[:1024], inline=False)

        avatar_url = target_member.display_avatar.url
        emb.add_field(
            name="Links",
            value=f"[Avatar]({avatar_url})"
            + (f" · [Banner]({full_user.banner.url})" if getattr(full_user, "banner", None) else ""),
            inline=False,
        )

        emb.set_footer(text=f"User-ID: {target_member.id}")
        emb.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=emb, ephemeral=True)

    @app_commands.command(name="user-info", description="Nutzer-Profil: Strikes, Verwarnungen, letzte Mod-Aktionen")
    @app_commands.describe(mitglied="Mitglied dessen Moderation-History angezeigt wird")
    @_check_inspect_permissions()
    async def slash_user_info(self, interaction: discord.Interaction, mitglied: discord.Member) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = self.bot.db
        try:
            strikes = await db.get_user_strikes(interaction.guild.id, mitglied.id)
            warnings_text = await db.fetch_recent_warnings_text(interaction.guild.id, mitglied.id, limit=5)
            recent_logs = await db.fetch_mod_logs(
                interaction.guild.id, limit=5, target_user_id=mitglied.id
            )
        except Exception:
            logger.exception("/user-info DB-Fehler")
            await interaction.followup.send("Datenbankfehler.", ephemeral=True)
            return

        emb = discord.Embed(
            title=f"Nutzer-Info: {mitglied}",
            color=discord.Color.blurple(),
        )
        emb.set_thumbnail(url=mitglied.display_avatar.url)
        emb.add_field(name="ID", value=str(mitglied.id), inline=True)
        emb.add_field(name="Konto erstellt", value=discord.utils.format_dt(mitglied.created_at, "R"), inline=True)
        emb.add_field(name="Beigetreten", value=discord.utils.format_dt(mitglied.joined_at, "R") if mitglied.joined_at else "–", inline=True)
        emb.add_field(name="Strikes", value=str(strikes), inline=True)
        emb.add_field(name="Letzte Verwarnungen (max. 5)", value=warnings_text[:1000], inline=False)
        if recent_logs:
            log_lines = []
            for e in recent_logs:
                cref = f" `{e.case_ref}`" if getattr(e, "case_ref", None) else ""
                log_lines.append(f"`{e.created_at_iso[:10]}`{cref} **{e.action}** – {e.reason[:80]}")
            emb.add_field(name="Letzte Mod-Aktionen (max. 5)", value="\n".join(log_lines)[:1000], inline=False)
        else:
            emb.add_field(name="Letzte Mod-Aktionen", value="Keine", inline=False)
        emb.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=emb, ephemeral=True)

    @app_commands.command(name="unmute", description="Timeout eines Mitglieds aufheben")
    @app_commands.describe(mitglied="Mitglied dessen Timeout aufgehoben wird", grund="Optionaler Grund")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def slash_unmute(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
        grund: str = "Manuell aufgehoben",
    ) -> None:
        if interaction.guild is None or interaction.user is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await mitglied.timeout(None, reason=grund)
        except discord.Forbidden:
            await interaction.followup.send("Fehlende Rechte zum Aufheben des Timeouts.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Discord-Fehler: {exc}", ephemeral=True)
            return
        try:
            await self.bot.db.add_mod_log(
                interaction.guild.id,
                mitglied.id,
                "unmute",
                grund,
                channel_id=interaction.channel.id if interaction.channel else None,
                actor_id=interaction.user.id,
                details="Timeout manuell aufgehoben",
            )
        except Exception:
            logger.exception("DB-Fehler beim Speichern von unmute")
        await interaction.followup.send(f"Timeout von **{mitglied}** aufgehoben.", ephemeral=True)

    @app_commands.command(name="appeal", description="Einspruch zu einer Moderation einreichen")
    @app_commands.describe(
        fall_ref="Fall-ID (z.B. CASE-1234-7) oder kurze Beschreibung",
        text="Dein Einspruch / Begründung",
    )
    async def slash_appeal(
        self,
        interaction: discord.Interaction,
        fall_ref: str,
        text: str,
    ) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        rcid = cfg.get("report_channel_id") or cfg.get("mod_log_channel_id")
        if not rcid:
            await interaction.followup.send(
                "Kein Mod-Kanal konfiguriert — bitte einen Moderator direkt kontaktieren.",
                ephemeral=True,
            )
            return
        rch = interaction.guild.get_channel(int(rcid))
        if not isinstance(rch, discord.TextChannel):
            await interaction.followup.send("Mod-Kanal nicht erreichbar.", ephemeral=True)
            return
        emb = discord.Embed(
            title="⚖️ Einspruch eingereicht",
            description=text[:2000],
            color=discord.Color.gold(),
        )
        emb.add_field(name="Einreicher", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        emb.add_field(name="Fall-Referenz", value=fall_ref[:200], inline=True)
        emb.timestamp = discord.utils.utcnow()
        try:
            await rch.send(embed=emb)
        except discord.HTTPException:
            await interaction.followup.send("Konnte Einspruch nicht posten.", ephemeral=True)
            return
        await interaction.followup.send(
            "Dein Einspruch wurde an die Moderation weitergeleitet.",
            ephemeral=True,
        )

    @app_commands.command(
        name="check",
        description="KI-Check: Kurz-Embed; volles JSON per Button See Evaluation (ephemeral).",
    )
    @app_commands.describe(text="Beispielnachricht / Text zur Bewertung")
    @_check_inspect_permissions()
    async def slash_check(self, interaction: discord.Interaction, text: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        body = (text or "").strip()[:8000]
        if not body:
            await interaction.response.send_message("Bitte einen Text angeben.", ephemeral=True)
            return
        ai = self.bot.ai
        db = self.bot.db
        if ai is None or db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        gcfg = await db.get_guild_config(interaction.guild.id)
        thr = int(gcfg["confidence_threshold"])
        system_prompt = MODERATOR_AI_SYSTEM_PROMPT.format(
            server_rules=gcfg["server_rules"],
            context_block="(Kein Channel-Kontext — manuelle Einzelprüfung per /check.)",
        )
        sample_block = (
            "Zeit: (Einzelprüfung)\n"
            "Autor: (n/v)\n"
            "Kanal: (n/v)\n"
            "Nachrichten-ID: (n/v)\n"
            f"Inhalt:\n{body}"
        )
        user_payload = build_user_payload(sample_block)
        try:
            result = await ai.moderate(
                system_prompt=system_prompt,
                user_payload=user_payload,
                guild_confidence_threshold=thr,
            )
        except Exception:
            logger.exception("/check — Moderations-API fehlgeschlagen")
            await interaction.followup.send(
                "Die Moderations-API ist fehlgeschlagen (Details in den Bot-Logs).",
                ephemeral=True,
            )
            return

        out = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
        ts = discord.utils.utcnow().replace(microsecond=0).isoformat() + "Z"
        emb = build_check_result_embed(
            text_preview=body,
            decision=result.moderation_decision,
            confidence=result.confidence,
            severity=getattr(result.severity, "value", str(result.severity)),
            reason=result.reason,
            requires_manual_review=result.requires_manual_review,
        )
        view = CheckEvaluationView(json_text=out, ts_iso=ts)
        await interaction.followup.send(embed=emb, view=view, ephemeral=True)

    @app_commands.command(
        name="cases",
        description="Letzte Fälle mit gespeicherter KI-Auswertung — Blättern & JSON-Button.",
    )
    @app_commands.describe(
        anzahl="Wie viele Fälle max. (1–15, neueste zuerst)",
        fall_ref="Direktsuche nach Fall-ID (z.B. CASE-1234-7)",
    )
    @_check_inspect_permissions()
    async def slash_cases(
        self,
        interaction: discord.Interaction,
        anzahl: int = 8,
        fall_ref: Optional[str] = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return
        db = self.bot.db
        if db is None:
            await interaction.response.send_message("Datenbank nicht verfügbar.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        if fall_ref:
            entry = await db.fetch_mod_log_by_case_ref(interaction.guild.id, fall_ref.strip())
            if entry is None:
                await interaction.followup.send(
                    f"Fall `{fall_ref}` nicht gefunden.",
                    ephemeral=True,
                )
                return
            emb = entry_embed(entry)
            view = CasesPaginatorView(
                bot=self.bot,
                guild_id=interaction.guild.id,
                entries=[entry],
            )
            await interaction.followup.send(embed=emb, view=view, ephemeral=True)
            return

        n = max(1, min(15, anzahl))
        entries = await db.fetch_cases_with_evaluation(interaction.guild.id, limit=n)
        if not entries:
            await interaction.followup.send(
                "Keine Fälle mit gespeicherter Auswertung. Nach dem Update nur neue Aktionen.",
                ephemeral=True,
            )
            return
        view = CasesPaginatorView(
            bot=self.bot,
            guild_id=interaction.guild.id,
            entries=entries,
        )
        emb0 = entry_embed(entries[0])
        await interaction.followup.send(
            content=f"**{len(entries)}** Fälle — ◀/▶ wechseln, **See Evaluation** = JSON + Zeitstempel.",
            embed=emb0,
            view=view,
            ephemeral=True,
        )


async def setup(bot: ModerationBot) -> None:
    await bot.add_cog(AdminCog(bot))
