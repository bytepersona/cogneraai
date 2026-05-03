"""
/settings — Interaktives Konfigurationspanel (Discord UI).

Nur Admins und der Server-Inhaber dürfen den Befehl nutzen.
Fehlende Rechte: Bot schweigt (keine Antwort).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core_bot import ModerationBot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Permission guard
# ---------------------------------------------------------------------------

def _is_admin_or_owner(interaction: discord.Interaction) -> bool:
    """True wenn der Nutzer Server-Inhaber oder Administrator ist."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.guild.owner_id == interaction.user.id:
        return True
    return interaction.user.guild_permissions.administrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yn(val: object) -> str:
    return "✅ Aktiv" if val else "❌ Inaktiv"


def _cfg_embed(guild: discord.Guild, cfg: dict[str, Any]) -> discord.Embed:
    """Übersichts-Embed mit allen aktuellen Einstellungen."""
    emb = discord.Embed(
        title=f"⚙️ Einstellungen · {guild.name}",
        color=discord.Color.blurple(),
    )

    # KI & Moderation
    emb.add_field(name="KI-Moderation", value=_yn(cfg.get("ai_enabled", True)), inline=True)
    emb.add_field(name="Dry-Run", value=_yn(cfg.get("dry_run", False)), inline=True)
    emb.add_field(
        name="Confidence-Schwelle",
        value=str(cfg.get("confidence_threshold", 75)),
        inline=True,
    )
    emb.add_field(
        name="Standard-Timeout",
        value=f"{cfg.get('default_timeout_minutes', 10)} Min.",
        inline=True,
    )

    # Kanäle
    modlog = cfg.get("mod_log_channel_id")
    report = cfg.get("report_channel_id")
    rv_inbox = cfg.get("review_inbox_channel_id")
    rv_appr = cfg.get("review_approved_channel_id")
    rv_decl = cfg.get("review_declined_channel_id")
    emb.add_field(
        name="Mod-Log-Kanal",
        value=f"<#{modlog}>" if modlog else "–",
        inline=True,
    )
    emb.add_field(
        name="Report-Kanal",
        value=f"<#{report}>" if report else "–",
        inline=True,
    )
    emb.add_field(
        name="Review-Eingang",
        value=f"<#{rv_inbox}>" if rv_inbox else "– (Fallback: Mod-Log)",
        inline=True,
    )
    emb.add_field(
        name="Review Bestätigt →",
        value=f"<#{rv_appr}>" if rv_appr else "– (Fallback: Mod-Log)",
        inline=True,
    )
    emb.add_field(
        name="Review Abgelehnt →",
        value=f"<#{rv_decl}>" if rv_decl else "– (Fallback: Mod-Log)",
        inline=True,
    )

    # Review-Queue
    emb.add_field(
        name="Review-Queue",
        value=f"{_yn(cfg.get('review_queue_enabled', True))} | Boden: {cfg.get('review_confidence_floor', 50)}",
        inline=True,
    )

    # Strike-Eskalation
    emb.add_field(
        name="Strike-Eskalation",
        value=_yn(cfg.get("strike_escalation_enabled", False)),
        inline=True,
    )

    # Embed-TTL
    ttl = cfg.get("mod_embed_delete_after_seconds")
    emb.add_field(
        name="Embed-TTL",
        value=f"{ttl}s" if ttl else "–",
        inline=True,
    )

    # URL-Scan
    vt_m = cfg.get("vt_malicious_threshold", 1)
    vt_s = cfg.get("vt_suspicious_threshold", 3)
    emb.add_field(
        name="URL-Scan (VirusTotal)",
        value=f"{_yn(cfg.get('url_scan_enabled', False))} | mal≥{vt_m}, sus≥{vt_s}",
        inline=False,
    )

    # Whitelist
    wl_u = cfg.get("whitelist_user_ids", [])
    wl_r = cfg.get("whitelist_role_ids", [])
    wl_c = cfg.get("whitelist_channel_ids", [])
    wl_u_fmt = ", ".join(f"<@{i}>" for i in wl_u[:8]) or "–"
    wl_r_fmt = ", ".join(f"<@&{i}>" for i in wl_r[:8]) or "–"
    wl_c_fmt = ", ".join(f"<#{i}>" for i in wl_c[:8]) or "–"
    emb.add_field(name=f"Whitelist Nutzer/Bots ({len(wl_u)})", value=wl_u_fmt, inline=False)
    emb.add_field(name=f"Whitelist Rollen ({len(wl_r)})", value=wl_r_fmt, inline=False)
    emb.add_field(name=f"Whitelist Kanäle ({len(wl_c)})", value=wl_c_fmt, inline=False)

    emb.set_footer(text="Wähle eine Kategorie unten um Einstellungen zu ändern.")
    emb.timestamp = discord.utils.utcnow()
    return emb


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class _IntModal(discord.ui.Modal):
    """Einzeiliger Modal für einen Integer-Wert."""

    value_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Wert",
        placeholder="Zahl eingeben…",
        max_length=10,
    )

    def __init__(self, title: str, label: str, placeholder: str, current: str) -> None:
        super().__init__(title=title)
        self.value_input.label = label
        self.value_input.placeholder = placeholder
        self.value_input.default = current
        self._result: Optional[int] = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self._result = int(self.value_input.value.strip())
            await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(
                "Ungültige Zahl.", ephemeral=True
            )


class _ChannelIdModal(discord.ui.Modal):
    """Modal zur Eingabe einer Channel-ID (oder 0 zum Entfernen)."""

    channel_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Kanal-ID",
        placeholder="Kanal-ID (0 = entfernen)",
        max_length=25,
    )

    def __init__(self, title: str, current: Optional[int]) -> None:
        super().__init__(title=title)
        self.channel_input.default = str(current) if current else ""
        self._result: Optional[int] = _UNSET_SENTINEL

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.channel_input.value.strip()
        if raw == "" or raw == "0":
            self._result = None
        else:
            try:
                self._result = int(raw)
            except ValueError:
                await interaction.response.send_message("Ungültige ID.", ephemeral=True)
                return
        await interaction.response.defer()


class _WhitelistModal(discord.ui.Modal):
    """Modal: Whitelist-ID (User/Rolle/Kanal) hinzufügen oder entfernen."""

    id_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Discord-ID",
        placeholder="Nutzer-, Rollen- oder Kanal-ID",
        max_length=30,
    )

    def __init__(self, title: str, label: str) -> None:
        super().__init__(title=title)
        self.id_input.label = label
        self._result: Optional[int] = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            self._result = int(self.id_input.value.strip())
            await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(
                "Ungültige Discord-ID.", ephemeral=True
            )


_UNSET_SENTINEL = object()


# ---------------------------------------------------------------------------
# Sub-Views (per Kategorie)
# ---------------------------------------------------------------------------

class _KIView(discord.ui.View):
    """KI & Moderation Einstellungen."""

    def __init__(self, parent: "SettingsView") -> None:
        super().__init__(timeout=300.0)
        self._p = parent

    @discord.ui.button(label="KI-Moderation toggle", style=discord.ButtonStyle.primary)
    async def toggle_ai(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        new = not bool(cfg.get("ai_enabled", True))
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, ai_enabled=new)  # type: ignore[union-attr]
        await self._p._refresh(interaction, f"KI-Moderation: **{'an' if new else 'aus'}**.")

    @discord.ui.button(label="Dry-Run toggle", style=discord.ButtonStyle.secondary)
    async def toggle_dry_run(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        new = not bool(cfg.get("dry_run", False))
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, dry_run=new)  # type: ignore[union-attr]
        await self._p._refresh(interaction, f"Dry-Run: **{'an' if new else 'aus'}**.")

    @discord.ui.button(label="Confidence-Schwelle setzen", style=discord.ButtonStyle.secondary)
    async def set_threshold(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _IntModal(
            "Confidence-Schwelle",
            "Wert (0–92)",
            "z.B. 75",
            str(cfg.get("confidence_threshold", 75)),
        )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not None:
            val = max(0, min(92, modal._result))
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, confidence_threshold=val)  # type: ignore[union-attr]
            await self._p._refresh(None, f"Confidence-Schwelle: **{val}**.", guild=interaction.guild)

    @discord.ui.button(label="Standard-Timeout setzen", style=discord.ButtonStyle.secondary)
    async def set_timeout(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _IntModal(
            "Standard-Timeout",
            "Minuten (1–40320)",
            "z.B. 10",
            str(cfg.get("default_timeout_minutes", 10)),
        )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not None:
            val = max(1, min(40320, modal._result))
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, default_timeout_minutes=val)  # type: ignore[union-attr]
            await self._p._refresh(None, f"Standard-Timeout: **{val} Min.**.", guild=interaction.guild)

    @discord.ui.button(label="← Zurück", style=discord.ButtonStyle.danger, row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._p._show_main(interaction)


class _KanaeleView(discord.ui.View):
    """Kanal-Zuweisungen."""

    def __init__(self, parent: "SettingsView") -> None:
        super().__init__(timeout=300.0)
        self._p = parent

    @discord.ui.button(label="Mod-Log-Kanal", style=discord.ButtonStyle.primary, row=0)
    async def set_modlog(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _ChannelIdModal("Mod-Log-Kanal", cfg.get("mod_log_channel_id"))
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not _UNSET_SENTINEL:
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, mod_log_channel_id=modal._result)  # type: ignore[union-attr]
            val = f"<#{modal._result}>" if modal._result else "–"
            await self._p._refresh(None, f"Mod-Log-Kanal: **{val}**.", guild=interaction.guild)

    @discord.ui.button(label="Report-Kanal", style=discord.ButtonStyle.primary, row=0)
    async def set_report(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _ChannelIdModal("Report-Kanal", cfg.get("report_channel_id"))
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not _UNSET_SENTINEL:
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, report_channel_id=modal._result)  # type: ignore[union-attr]
            val = f"<#{modal._result}>" if modal._result else "–"
            await self._p._refresh(None, f"Report-Kanal: **{val}**.", guild=interaction.guild)

    @discord.ui.button(label="Review-Eingang", style=discord.ButtonStyle.secondary, row=1)
    async def set_review_inbox(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _ChannelIdModal("Review-Eingangskanal", cfg.get("review_inbox_channel_id"))
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not _UNSET_SENTINEL:
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, review_inbox_channel_id=modal._result)  # type: ignore[union-attr]
            val = f"<#{modal._result}>" if modal._result else "– (Fallback: Mod-Log)"
            await self._p._refresh(None, f"Review-Eingangskanal: **{val}**.", guild=interaction.guild)

    @discord.ui.button(label="Review Bestätigt →", style=discord.ButtonStyle.secondary, row=1)
    async def set_review_approved(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _ChannelIdModal("Review-Bestätigungskanal", cfg.get("review_approved_channel_id"))
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not _UNSET_SENTINEL:
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, review_approved_channel_id=modal._result)  # type: ignore[union-attr]
            val = f"<#{modal._result}>" if modal._result else "– (Fallback: Mod-Log)"
            await self._p._refresh(None, f"Review-Bestätigungskanal: **{val}**.", guild=interaction.guild)

    @discord.ui.button(label="Review Abgelehnt →", style=discord.ButtonStyle.secondary, row=2)
    async def set_review_declined(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _ChannelIdModal("Review-Ablehnungskanal", cfg.get("review_declined_channel_id"))
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not _UNSET_SENTINEL:
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, review_declined_channel_id=modal._result)  # type: ignore[union-attr]
            val = f"<#{modal._result}>" if modal._result else "– (Fallback: Mod-Log)"
            await self._p._refresh(None, f"Review-Ablehnungskanal: **{val}**.", guild=interaction.guild)

    @discord.ui.button(label="← Zurück", style=discord.ButtonStyle.danger, row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._p._show_main(interaction)


class _WhitelistView(discord.ui.View):
    """Whitelist-Verwaltung für Nutzer/Bots, Rollen und Kanäle."""

    def __init__(self, parent: "SettingsView") -> None:
        super().__init__(timeout=300.0)
        self._p = parent

    # ── Nutzer / Bots ────────────────────────────────────────────────────

    @discord.ui.button(label="Nutzer/Bot hinzufügen", style=discord.ButtonStyle.success, row=0)
    async def wl_user_add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = _WhitelistModal("Nutzer/Bot zur Whitelist", "Nutzer- oder Bot-ID")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is None:
            return
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        ids: list[int] = list(cfg.get("whitelist_user_ids", []))
        if modal._result not in ids:
            ids.append(modal._result)
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, whitelist_user_ids=ids)  # type: ignore[union-attr]
        await self._p._refresh(None, f"<@{modal._result}> zur Nutzer-Whitelist hinzugefügt.", guild=interaction.guild)

    @discord.ui.button(label="Nutzer/Bot entfernen", style=discord.ButtonStyle.danger, row=0)
    async def wl_user_remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = _WhitelistModal("Nutzer/Bot von Whitelist entfernen", "Nutzer- oder Bot-ID")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is None:
            return
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        ids = [i for i in cfg.get("whitelist_user_ids", []) if i != modal._result]
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, whitelist_user_ids=ids)  # type: ignore[union-attr]
        await self._p._refresh(None, f"<@{modal._result}> von Nutzer-Whitelist entfernt.", guild=interaction.guild)

    # ── Rollen ───────────────────────────────────────────────────────────

    @discord.ui.button(label="Rolle hinzufügen", style=discord.ButtonStyle.success, row=1)
    async def wl_role_add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = _WhitelistModal("Rolle zur Whitelist", "Rollen-ID")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is None:
            return
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        ids: list[int] = list(cfg.get("whitelist_role_ids", []))
        if modal._result not in ids:
            ids.append(modal._result)
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, whitelist_role_ids=ids)  # type: ignore[union-attr]
        await self._p._refresh(None, f"<@&{modal._result}> zur Rollen-Whitelist hinzugefügt.", guild=interaction.guild)

    @discord.ui.button(label="Rolle entfernen", style=discord.ButtonStyle.danger, row=1)
    async def wl_role_remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = _WhitelistModal("Rolle von Whitelist entfernen", "Rollen-ID")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is None:
            return
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        ids = [i for i in cfg.get("whitelist_role_ids", []) if i != modal._result]
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, whitelist_role_ids=ids)  # type: ignore[union-attr]
        await self._p._refresh(None, f"<@&{modal._result}> von Rollen-Whitelist entfernt.", guild=interaction.guild)

    # ── Kanäle ───────────────────────────────────────────────────────────

    @discord.ui.button(label="Kanal hinzufügen", style=discord.ButtonStyle.success, row=2)
    async def wl_chan_add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = _WhitelistModal("Kanal zur Whitelist", "Kanal-ID")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is None:
            return
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        ids: list[int] = list(cfg.get("whitelist_channel_ids", []))
        if modal._result not in ids:
            ids.append(modal._result)
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, whitelist_channel_ids=ids)  # type: ignore[union-attr]
        await self._p._refresh(None, f"<#{modal._result}> zur Kanal-Whitelist hinzugefügt.", guild=interaction.guild)

    @discord.ui.button(label="Kanal entfernen", style=discord.ButtonStyle.danger, row=2)
    async def wl_chan_remove(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        modal = _WhitelistModal("Kanal von Whitelist entfernen", "Kanal-ID")
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is None:
            return
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        ids = [i for i in cfg.get("whitelist_channel_ids", []) if i != modal._result]
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, whitelist_channel_ids=ids)  # type: ignore[union-attr]
        await self._p._refresh(None, f"<#{modal._result}> von Kanal-Whitelist entfernt.", guild=interaction.guild)

    @discord.ui.button(label="← Zurück", style=discord.ButtonStyle.danger, row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._p._show_main(interaction)


class _ReviewView(discord.ui.View):
    """Review-Queue & Strike-Eskalation."""

    def __init__(self, parent: "SettingsView") -> None:
        super().__init__(timeout=300.0)
        self._p = parent

    @discord.ui.button(label="Review-Queue toggle", style=discord.ButtonStyle.primary)
    async def toggle_rq(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        new = not bool(cfg.get("review_queue_enabled", True))
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, review_queue_enabled=new)  # type: ignore[union-attr]
        await self._p._refresh(interaction, f"Review-Queue: **{'an' if new else 'aus'}**.")

    @discord.ui.button(label="Review-Boden setzen", style=discord.ButtonStyle.secondary)
    async def set_floor(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        modal = _IntModal(
            "Review-Confidence-Boden",
            "Wert (0–100)",
            "z.B. 50",
            str(cfg.get("review_confidence_floor", 50)),
        )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal._result is not None:
            val = max(0, min(100, modal._result))
            await self._p.bot.db.upsert_guild_config(interaction.guild.id, review_confidence_floor=val)  # type: ignore[union-attr]
            await self._p._refresh(None, f"Review-Boden: **{val}**.", guild=interaction.guild)

    @discord.ui.button(label="Strike-Eskalation toggle", style=discord.ButtonStyle.primary)
    async def toggle_strikes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cfg = await self._p._get_cfg(interaction)
        if cfg is None:
            return
        new = not bool(cfg.get("strike_escalation_enabled", False))
        await self._p.bot.db.upsert_guild_config(interaction.guild.id, strike_escalation_enabled=new)  # type: ignore[union-attr]
        await self._p._refresh(interaction, f"Strike-Eskalation: **{'an' if new else 'aus'}**.")

    @discord.ui.button(label="← Zurück", style=discord.ButtonStyle.danger, row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._p._show_main(interaction)


# ---------------------------------------------------------------------------
# Main SettingsView (Kategorie-Auswahl)
# ---------------------------------------------------------------------------

class _CategorySelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label="KI & Moderation",
                value="ki",
                description="KI, Dry-Run, Confidence, Timeout",
                emoji="🤖",
            ),
            discord.SelectOption(
                label="Kanäle",
                value="kanaele",
                description="Mod-Log- und Report-Kanal",
                emoji="📢",
            ),
            discord.SelectOption(
                label="Whitelist",
                value="whitelist",
                description="Nutzer, Bots, Rollen, Kanäle ausschließen",
                emoji="🛡️",
            ),
            discord.SelectOption(
                label="Review & Strikes",
                value="review",
                description="Review-Queue und Strike-Eskalation",
                emoji="⚖️",
            ),
        ]
        super().__init__(
            placeholder="Kategorie auswählen…",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        choice = self.values[0]
        if choice == "ki":
            sub = _KIView(view)
        elif choice == "kanaele":
            sub = _KanaeleView(view)
        elif choice == "whitelist":
            sub = _WhitelistView(view)
        else:
            sub = _ReviewView(view)
        cfg = await view._get_cfg(interaction)
        if cfg is None:
            return
        emb = _cfg_embed(interaction.guild, cfg)  # type: ignore[arg-type]
        await interaction.response.edit_message(embed=emb, view=sub)


class SettingsView(discord.ui.View):
    """Haupt-View: zeigt Kategorie-Auswahl."""

    def __init__(self, bot: ModerationBot, guild: discord.Guild) -> None:
        super().__init__(timeout=600.0)
        self.bot = bot
        self.guild = guild
        self.add_item(_CategorySelect())

    async def _get_cfg(
        self, interaction: discord.Interaction
    ) -> Optional[dict[str, Any]]:
        if self.bot.db is None or interaction.guild is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return None
        return await self.bot.db.get_guild_config(interaction.guild.id)

    async def _show_main(self, interaction: discord.Interaction) -> None:
        cfg = await self._get_cfg(interaction)
        if cfg is None:
            return
        emb = _cfg_embed(interaction.guild, cfg)  # type: ignore[arg-type]
        view = SettingsView(self.bot, interaction.guild)  # type: ignore[arg-type]
        await interaction.response.edit_message(embed=emb, view=view)

    async def _refresh(
        self,
        interaction: Optional[discord.Interaction],
        notice: str,
        *,
        guild: Optional[discord.Guild] = None,
    ) -> None:
        g = guild or (interaction.guild if interaction else None) or self.guild
        if self.bot.db is None or g is None:
            return
        cfg = await self.bot.db.get_guild_config(g.id)
        emb = _cfg_embed(g, cfg)
        view = SettingsView(self.bot, g)
        emb.description = f"✅ {notice}"
        if interaction is not None:
            try:
                if interaction.response.is_done():
                    await interaction.edit_original_response(embed=emb, view=view)
                else:
                    await interaction.response.edit_message(embed=emb, view=view)
            except discord.HTTPException:
                pass
        else:
            logger.debug("_refresh without interaction — embed update skipped.")


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class SettingsCog(commands.Cog):
    """Stellt /settings bereit."""

    def __init__(self, bot: ModerationBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="settings",
        description="Interaktives Einstellungs-Panel (nur Admins & Inhaber).",
    )
    async def slash_settings(self, interaction: discord.Interaction) -> None:
        # Nur Administrator oder Server-Inhaber — keine Antwort bei fehlenden Rechten
        if not _is_admin_or_owner(interaction):
            return

        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Dienst nicht verfügbar.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            cfg = await self.bot.db.get_guild_config(interaction.guild.id)
        except Exception:
            logger.exception("/settings DB-Fehler")
            await interaction.followup.send("Datenbankfehler.", ephemeral=True)
            return

        emb = _cfg_embed(interaction.guild, cfg)
        view = SettingsView(self.bot, interaction.guild)
        await interaction.followup.send(embed=emb, view=view, ephemeral=True)


async def setup(bot: ModerationBot) -> None:
    await bot.add_cog(SettingsCog(bot))
