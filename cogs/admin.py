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
        try:
            await mitglied.send(text)
        except discord.Forbidden:
            if interaction.channel and isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(f"{mitglied.mention}: {text}", delete_after=120)
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
            lines.append(
                f"`{e.created_at_iso}` **{e.action}** Ziel={e.target_user_id} — {e.reason[:80]}",
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


async def setup(bot: ModerationBot) -> None:
    await bot.add_cog(AdminCog(bot))
