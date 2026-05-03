"""Pydantic-Modelle für die strukturierte Claude-Antwort (JSON)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from utils.default_server_rules import normalize_violated_rule_ids


class Severity(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ModerationDecision(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    DELETE = "delete"
    TIMEOUT = "timeout"
    BAN = "ban"


class ClaudeModerationResponse(BaseModel):
    """
    Erwartetes JSON von Claude (ModeratorAI).

    Schema-Version ermöglicht spätere Migrationen.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = Field("1.0", alias="schema_version")
    moderation_decision: ModerationDecision = Field(..., alias="moderation_decision")
    confidence: int = Field(..., ge=0, le=100, alias="confidence")
    severity: Severity = Field(Severity.NONE, alias="severity")
    reason: str = Field("", alias="reason")
    explanation: str = Field("", alias="explanation")
    timeout_minutes: Optional[int] = Field(None, alias="timeout_minutes")
    user_facing_message: str = Field("", alias="user_facing_message")
    requires_manual_review: bool = Field(False, alias="requires_manual_review")
    violated_rule_ids: list[str] = Field(default_factory=list, alias="violated_rule_ids")

    @field_validator("user_facing_message", "reason", "explanation", "schema_version", mode="before")
    @classmethod
    def _null_str_to_empty(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    @field_validator("violated_rule_ids", mode="before")
    @classmethod
    def _normalize_violated_rule_ids(cls, v: Any) -> list[str]:
        return normalize_violated_rule_ids(v)

    @field_validator("timeout_minutes", mode="before")
    @classmethod
    def empty_timeout_to_none(cls, v: Any) -> Optional[int]:
        if v is None or v == "":
            return None
        return int(v)

    @classmethod
    def model_validate_loose(cls, data: dict[str, Any]) -> ClaudeModerationResponse:
        """Normalisiert Aliase (snake_case) falls das Modell andere Keys sendet."""
        normalized: dict[str, Any] = dict(data)
        key_map = {
            "moderationDecision": "moderation_decision",
            "userFacingMessage": "user_facing_message",
            "requiresManualReview": "requires_manual_review",
            "timeoutMinutes": "timeout_minutes",
            "violatedRuleIds": "violated_rule_ids",
        }
        for old, new in key_map.items():
            if old in normalized and new not in normalized:
                normalized[new] = normalized.pop(old)
        return cls.model_validate(normalized)
