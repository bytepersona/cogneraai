"""Bot-Klasse und gemeinsame Attribute für Cogs (vermeidet Zirkelimporte mit main)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord
from cachetools import TTLCache
from discord import app_commands
from discord.ext import commands

from utils.anthropic_moderation import AnthropicModerationClient
from utils.circuit_breaker import AsyncCircuitBreaker
from utils.config import Settings
from utils.database import ModerationDatabase
from utils.moderation_jobs import ModerationJob
from utils.rate_limit import SlidingWindowRateLimiter
from utils.virustotal_client import VirusTotalClient

logger = logging.getLogger(__name__)


class ModerationBot(commands.Bot):
    """
    Zentraler Bot mit Intents, Slash-Commands und Referenzen auf DB / KI / Rate-Limit / Cache.

    Cogs greifen über self.bot auf diese Attribute zu.
    """

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.guild_messages = True

        super().__init__(command_prefix="!", intents=intents, help_command=None)

        self.settings: Settings = settings
        self.db: ModerationDatabase | None = None
        self.ai: AnthropicModerationClient | None = None
        self.rate_limiter: SlidingWindowRateLimiter | None = None
        self.msg_cache: TTLCache[str, bool] | None = None
        self.moderation_queue: Optional[asyncio.Queue[ModerationJob]] = None
        self.anthropic_breaker: Optional[AsyncCircuitBreaker] = None
        self.vt_client: VirusTotalClient | None = None
        self.vt_url_cache: TTLCache[str, Any] | None = None
        self.report_rate_cache: TTLCache[int, int] = TTLCache(maxsize=10_000, ttl=3600.0)

    async def setup_hook(self) -> None:
        """Initialisiert persistente Dienste und lädt Erweiterungen."""
        from utils.database import create_database

        self.db = await create_database(self.settings)
        self.ai = AnthropicModerationClient(self.settings)
        self.rate_limiter = SlidingWindowRateLimiter(
            self.settings.rate_limit_per_user_per_minute,
            window_seconds=60.0,
        )
        self.msg_cache = TTLCache(maxsize=10_000, ttl=self.settings.message_cache_ttl_seconds)
        self.moderation_queue = asyncio.Queue(maxsize=self.settings.moderation_queue_max)
        self.anthropic_breaker = AsyncCircuitBreaker(
            failure_threshold=self.settings.anthropic_circuit_failure_threshold,
            reset_timeout_s=self.settings.anthropic_circuit_reset_seconds,
            name="anthropic",
        )
        if self.settings.virustotal_api_key:
            self.vt_client = VirusTotalClient(self.settings.virustotal_api_key)
            self.vt_url_cache = TTLCache(maxsize=2_000, ttl=300.0)

        for ext in ("cogs.moderation", "cogs.admin"):
            try:
                await self.load_extension(ext)
            except Exception:
                logger.exception("Cog '%s' konnte nicht geladen werden — Bot läuft weiter.", ext)

        guild_id = self.settings.discord_guild_id
        try:
            if guild_id is not None:
                g = discord.Object(id=guild_id)
                # Cogs tragen App-Commands als *globale* Tree-Commands ein. `tree.sync(guild=…)`
                # sendet nur den Guild-Bucket — ohne copy_global_to wäre der leer → 0 Befehle.
                self.tree.copy_global_to(guild=g)
                synced = await self.tree.sync(guild=g)
                logger.info("Slash-Commands synchronisiert (Guild %s): %d Befehle", guild_id, len(synced))
            else:
                synced = await self.tree.sync()
                logger.info("Slash-Commands global synchronisiert: %d Befehle", len(synced))
        except Exception:
            logger.exception("Slash-Command-Sync fehlgeschlagen")

        self.tree.on_error = self._on_tree_error

    async def _on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """Fängt unbehandelte App-Command-Fehler ab und sendet eine lesbare Antwort."""
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "Du hast keine Berechtigung für diesen Befehl."
        elif isinstance(error, app_commands.CommandNotFound):
            msg = "Befehl nicht gefunden."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = f"Fehlende Rechte: {', '.join(error.missing_permissions)}."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = f"Dem Bot fehlen Rechte: {', '.join(error.missing_permissions)}."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Cooldown aktiv — bitte {error.retry_after:.0f}s warten."
        else:
            logger.exception("Unbehandelter App-Command-Fehler", exc_info=error)
            msg = "Ein unerwarteter Fehler ist aufgetreten. Details wurden geloggt."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def close(self) -> None:
        if self.vt_client is not None:
            try:
                await self.vt_client.aclose()
            except Exception:
                logger.warning("Fehler beim Schließen des VirusTotal-Clients.")
        await super().close()
