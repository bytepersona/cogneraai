"""UI: „See Evaluation“ für /check und Paginierung für /cases."""

from __future__ import annotations

import io
from typing import List

import discord

from core_bot import ModerationBot
from utils.database import ModLogEntry
from utils.discord_embeds import build_case_browser_embed


def staff_can_inspect(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    p = interaction.user.guild_permissions
    return bool(p.administrator or p.view_audit_log or p.ban_members or p.manage_messages)


class CheckEvaluationView(discord.ui.View):
    def __init__(self, *, json_text: str, ts_iso: str, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self._json = json_text
        self._ts = ts_iso

    @discord.ui.button(label="See Evaluation", style=discord.ButtonStyle.primary, row=0)
    async def see_evaluation(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not staff_can_inspect(interaction):
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        buf = io.BytesIO(self._json.encode("utf-8"))
        await interaction.response.send_message(
            content=f"Auswertung · Timestamp (UTC): `{self._ts}`",
            file=discord.File(buf, filename="evaluation.json"),
            ephemeral=True,
        )


def entry_embed(entry: ModLogEntry) -> discord.Embed:
    label = entry.case_ref or str(entry.id)
    return build_case_browser_embed(
        case_label=label,
        action=entry.action,
        target_user_id=entry.target_user_id,
        channel_id=entry.channel_id,
        message_snapshot=entry.message_content_snapshot,
        reason=entry.reason,
        details=entry.details,
        log_id=entry.id,
        created_at_iso=entry.created_at_iso,
        evaluation_json=entry.evaluation_json,
    )


class CasesPaginatorView(discord.ui.View):
    """◀ / ▶ zwischen gespeicherten Fällen; „See Evaluation“ lädt JSON aus der DB."""

    def __init__(
        self,
        *,
        bot: ModerationBot,
        guild_id: int,
        entries: List[ModLogEntry],
        timeout: float = 600.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild_id = guild_id
        self.entries = entries
        self.idx = 0
        self._sync_nav_buttons()

    def _sync_nav_buttons(self) -> None:
        self.prev_b.disabled = self.idx <= 0
        self.next_b.disabled = self.idx >= max(0, len(self.entries) - 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_b(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not staff_can_inspect(interaction) or interaction.guild is None:
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        if interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Ungültiger Server.", ephemeral=True)
            return
        self.idx = max(0, self.idx - 1)
        self._sync_nav_buttons()
        await interaction.response.edit_message(
            embed=entry_embed(self.entries[self.idx]),
            view=self,
        )

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_b(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not staff_can_inspect(interaction) or interaction.guild is None:
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        if interaction.guild.id != self.guild_id:
            await interaction.response.send_message("Ungültiger Server.", ephemeral=True)
            return
        self.idx = min(len(self.entries) - 1, self.idx + 1)
        self._sync_nav_buttons()
        await interaction.response.edit_message(
            embed=entry_embed(self.entries[self.idx]),
            view=self,
        )

    @discord.ui.button(label="See Evaluation", style=discord.ButtonStyle.primary, row=0)
    async def see_eval(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not staff_can_inspect(interaction) or interaction.guild is None:
            await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
            return
        cur = self.entries[self.idx]
        db = self.bot.db
        entry = cur
        if db is not None:
            fresh = await db.fetch_mod_log_by_id(interaction.guild.id, cur.id)
            if fresh is not None:
                entry = fresh
        if not entry.evaluation_json:
            await interaction.response.send_message("Keine Auswertung gespeichert.", ephemeral=True)
            return
        buf = io.BytesIO(entry.evaluation_json.encode("utf-8"))
        await interaction.response.send_message(
            content=f"Auswertung · `{entry.case_ref or entry.id}` · `{entry.created_at_iso}`",
            file=discord.File(buf, filename="evaluation.json"),
            ephemeral=True,
        )
