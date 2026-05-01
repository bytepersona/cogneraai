"""Laden und Validieren der Umgebungskonfiguration."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Zentrale Konfiguration aus Umgebungsvariablen und optionaler .env-Datei."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    discord_token: str
    anthropic_api_key: str

    discord_guild_id: Optional[int] = None

    claude_model_haiku: str = "claude-3-haiku-20240307"
    claude_model_sonnet: str = "claude-3-5-sonnet-20241022"

    confidence_threshold: int = Field(default=75, ge=0, le=100)

    database_path: Path = Path("./data/moderation.db")

    log_level: str = "INFO"

    rate_limit_per_user_per_minute: int = Field(default=8, ge=1)
    message_cache_ttl_seconds: int = Field(default=30, ge=0)
    context_message_count: int = Field(default=20, ge=5, le=50)

    moderation_queue_max: int = Field(default=500, ge=10, le=50_000)
    anthropic_circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    anthropic_circuit_reset_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)

    use_oracle: bool = False
    oracle_user: Optional[str] = None
    oracle_password: Optional[str] = None
    oracle_dsn: Optional[str] = None


def load_settings() -> Settings:
    """Lädt die Einstellungen (fehlende Pflichtfelder lösen ValidationError aus)."""
    return Settings()
