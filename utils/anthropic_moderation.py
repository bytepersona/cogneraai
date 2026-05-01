"""Anthropic API: Haiku primär, Sonnet als Fallback."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic

from utils.json_extract import extract_json_object
from utils.models import ClaudeModerationResponse, ModerationDecision, Severity

if TYPE_CHECKING:
    from utils.config import Settings

logger = logging.getLogger(__name__)


class AnthropicModerationClient:
    """Kapselt Modellauswahl, Fallback und Parsing."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def moderate(
        self,
        *,
        system_prompt: str,
        user_payload: str,
        guild_confidence_threshold: int,
    ) -> ClaudeModerationResponse:
        """
        Ruft zuerst Haiku auf; bei niedriger Confidence oder kritischen Entscheidungen Sonnet.

        Schwellenwert: aus Guild-Konfiguration (Fallback-Wert aus globalem Setting beim ersten Start).
        """
        primary = await self._call_model(
            model=self._settings.claude_model_haiku,
            system_prompt=system_prompt,
            user_payload=user_payload,
        )

        if self._should_fallback_sonnet(primary, guild_confidence_threshold):
            logger.info(
                "Sonnet-Fallback: confidence=%s decision=%s severity=%s",
                primary.confidence,
                primary.moderation_decision,
                primary.severity,
            )
            return await self._call_model(
                model=self._settings.claude_model_sonnet,
                system_prompt=system_prompt,
                user_payload=user_payload,
            )

        return primary

    def _should_fallback_sonnet(
        self,
        r: ClaudeModerationResponse,
        threshold: int,
    ) -> bool:
        """Unter Schwelle, Ban-Entscheidung oder kritische Severity → zweites Modell."""
        if r.confidence < threshold:
            return True
        if r.moderation_decision == ModerationDecision.BAN:
            return True
        if r.severity == Severity.CRITICAL:
            return True
        return False

    async def _call_model(
        self,
        *,
        model: str,
        system_prompt: str,
        user_payload: str,
    ) -> ClaudeModerationResponse:
        try:
            msg = await self._client.messages.create(
                model=model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_payload}],
            )
        except Exception:
            logger.exception("Anthropic API Fehler (model=%s)", model)
            raise

        text = self._concatenate_text_blocks(msg)
        try:
            raw = extract_json_object(text)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error("JSON-Parse-Fehler: %s | Rohauszug: %s", e, text[:500])
            raise

        try:
            return ClaudeModerationResponse.model_validate_loose(raw)
        except Exception as e:
            logger.error("Schema-Validierung fehlgeschlagen: %s | Daten: %s", e, raw)
            raise

    @staticmethod
    def _concatenate_text_blocks(msg: object) -> str:
        parts: list[str] = []
        for block in getattr(msg, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts).strip()
