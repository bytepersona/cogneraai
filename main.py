"""
Einstiegspunkt: lädt Konfiguration, startet den Moderations-Bot (discord.py 2.4+).

Ausführung:
    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from pydantic import ValidationError

from core_bot import ModerationBot
from utils.config import load_settings


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


async def _run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    bot = ModerationBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Beendet durch Benutzer.")
    except ValidationError as exc:
        print(
            "Konfigurationsfehler — bitte .env prüfen:\n"
            + "\n".join(f"  {e['loc']}: {e['msg']}" for e in exc.errors()),
            file=sys.stderr,
        )
        sys.exit(1)
    except discord.LoginFailure:
        print(
            "Ungültiger Discord-Token (DISCORD_TOKEN). Bot konnte sich nicht anmelden.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
