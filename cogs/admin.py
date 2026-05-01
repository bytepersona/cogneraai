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
from utils.discord_embeds import build_user_notice_embed
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
        assert interaction.guild is not None and self.bot.db is not None
        await self.bot.db.upsert_guild_config(interaction.guild.id, server_rules=regeln)
        await interaction.response.send_message("Server-Regeln wurden gespeichert.", ephemeral=True)

    @mod_config.command(name="threshold", description="Confidence-Schwellenwert pro Guild (0–100)")
    @app_commands.describe(wert="Unter diesem Wert wird Sonnet nachgeschaltet (sofern nicht Ban/kritisch)")
    @_manage_guild_check()
    async def mod_config_threshold(self, interaction: discord.Interaction, wert: int) -> None:
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
        await self.bot.db.upsert_guild_config(interaction.guild.id, dry_run=aktiv)
        await interaction.response.send_message(
            f"Dry-Run (Shadow): **{'an' if aktiv else 'aus'}**.",
            ephemeral=True,
        )

    @mod_config.command(name="review-queue", description="Review-Warteschlange für Ban/low confidence aktivieren")
    @app_commands.describe(aktiv="True = kritische Fälle als Buttons im Mod-Log")
    @_manage_guild_check()
    async def mod_config_review_queue(self, interaction: discord.Interaction, aktiv: bool) -> None:
        assert interaction.guild is not None and self.bot.db is not None
        await self.bot.db.upsert_guild_config(interaction.guild.id, review_queue_enabled=aktiv)
        await interaction.response.send_message(
            f"Review-Queue: **{'an' if aktiv else 'aus'}**.",
            ephemeral=True,
        )

    @mod_config.command(name="review-floor", description="Unter dieser Confidence → Review-Queue (zusätzlich zu Ban)")
    @app_commands.describe(wert="0–100")
    @_manage_guild_check()
    async def mod_config_review_floor(self, interaction: discord.Interaction, wert: int) -> None:
        assert interaction.guild is not None and self.bot.db is not None
        wert = max(0, min(100, wert))
        await self.bot.db.upsert_guild_config(interaction.guild.id, review_confidence_floor=wert)
        await interaction.response.send_message(f"Review-Floor: **{wert}**.", ephemeral=True)

    @mod_config.command(name="strikes", description="Strike-Eskalation (Cap von Ban auf mildere Aktionen)")
    @app_commands.describe(aktiv="True = Strikes begrenzen schwere KI-Aktionen")
    @_manage_guild_check()
    async def mod_config_strikes(self, interaction: discord.Interaction, aktiv: bool) -> None:
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
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
        description="Auto-Löschen von Bot-Mod-Log-/Review-Nachrichten (Sekunden); überschreibt BOT_MESSAGE_DELETE_AFTER_SECONDS",
    )
    @app_commands.describe(sekunden="1–600 Sekunden bis zur automatischen Löschung der Bot-Hinweise")
    @_manage_guild_check()
    async def mod_config_mod_embed_ttl(self, interaction: discord.Interaction, sekunden: int) -> None:
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
        await self.bot.db.upsert_guild_config(
            interaction.guild.id,
            mod_embed_delete_after_seconds=None,
        )
        await interaction.response.send_message(
            "Mod-Embed-TTL: wieder **globale** Zeit aus der Bot-Konfiguration (.env).",
            ephemeral=True,
        )

    @app_commands.command(name="warn", description="Manuelle Verwarnung (DM mit Fallback)")
    @app_commands.describe(mitglied="Zielnutzer", grund="Kurzer Grund")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def slash_warn(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
        grund: str,
    ) -> None:
        assert interaction.guild is not None and interaction.user is not None and self.bot.db is not None
        await interaction.response.defer(ephemeral=True)
        text = f"Du hast eine Verwarnung erhalten: {grund}"
        warn_embed = build_user_notice_embed(text, ModerationDecision.WARN)
        try:
            await mitglied.send(embed=warn_embed)
        except discord.Forbidden:
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(
                    content=mitglied.mention,
                    embed=warn_embed,
                    delete_after=120,
                )
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
        assert interaction.guild is not None and interaction.user is not None and self.bot.db is not None
        days = max(0, min(7, nachrichten_tage))
        await interaction.response.defer(ephemeral=True)
        await interaction.guild.ban(mitglied, reason=grund, delete_message_days=days)
        await self.bot.db.add_mod_log(
            interaction.guild.id,
            mitglied.id,
            "ban",
            grund,
            channel_id=interaction.channel.id if interaction.channel else None,
            actor_id=interaction.user.id,
            details=f"Manueller Ban, delete_message_days={days}",
        )
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
        assert interaction.guild is not None and self.bot.db is not None
        await interaction.response.defer(ephemeral=True)
        limit = max(1, min(25, limit))
        logs = await self.bot.db.fetch_mod_logs(
            interaction.guild.id,
            limit=limit,
            target_user_id=nutzer.id if nutzer else None,
        )
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
        assert interaction.guild is not None and self.bot.db is not None
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
        assert interaction.guild is not None and self.bot.db is not None
        await interaction.response.defer(ephemeral=True)
        limit = max(1, min(5000, limit))
        logs = await self.bot.db.fetch_mod_logs_range(
            interaction.guild.id,
            limit=limit,
            days=tage,
        )
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
        assert interaction.guild is not None and self.bot.db is not None
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

    @app_commands.command(
        name="check",
        description="Text mit der KI-Moderation prüfen — reines JSON nur für dich (ephemeral).",
    )
    @app_commands.describe(text="Beispielnachricht / Text zur Bewertung")
    @_check_inspect_permissions()
    async def slash_check(self, interaction: discord.Interaction, text: str) -> None:
        assert interaction.guild is not None
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
        if len(out) <= 1900:
            await interaction.followup.send(out, ephemeral=True)
        else:
            buf = io.BytesIO(out.encode("utf-8"))
            await interaction.followup.send(
                "Ergebnis als JSON-Datei:",
                file=discord.File(buf, filename="moderation-check.json"),
                ephemeral=True,
            )


async def setup(bot: ModerationBot) -> None:
    await bot.add_cog(AdminCog(bot))
