"""Bot-Klasse und gemeinsame Attribute für Cogs (vermeidet Zirkelimporte mit main)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord
from cachetools import TTLCache
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

        await self.load_extension("cogs.moderation")
        await self.load_extension("cogs.admin")

        guild_id = self.settings.discord_guild_id
        try:
            if guild_id is not None:
                g = discord.Object(id=guild_id)
                synced = await self.tree.sync(guild=g)
                logger.info("Slash-Commands synchronisiert (Guild %s): %d Befehle", guild_id, len(synced))
            else:
                synced = await self.tree.sync()
                logger.info("Slash-Commands global synchronisiert: %d Befehle", len(synced))
        except Exception:
            logger.exception("Slash-Command-Sync fehlgeschlagen")
