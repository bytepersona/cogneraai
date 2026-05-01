"""Strike-basierte Eskalation: modelierte Entscheidung auf maximal erlaubte Aktion begrenzen."""

from __future__ import annotations

import json
import logging
from typing import Any

from utils.models import ClaudeModerationResponse, ModerationDecision

logger = logging.getLogger(__name__)

_ACTION_RANK: dict[ModerationDecision, int] = {
    ModerationDecision.ALLOW: 0,
    ModerationDecision.WARN: 1,
    ModerationDecision.DELETE: 2,
    ModerationDecision.TIMEOUT: 3,
    ModerationDecision.BAN: 4,
}

_RANK_TO_ACTION: dict[int, ModerationDecision] = {
    0: ModerationDecision.ALLOW,
    1: ModerationDecision.WARN,
    2: ModerationDecision.DELETE,
    3: ModerationDecision.TIMEOUT,
    4: ModerationDecision.BAN,
}


def default_strike_escalation_json() -> str:
    """Standard-Stufen (JSON in guild_config)."""
    cfg = {
        "tiers": [
            {"ceil_strikes": 2, "cap": "warn"},
            {"ceil_strikes": 5, "cap": "delete"},
            {"ceil_strikes": 12, "cap": "timeout"},
            {"ceil_strikes": 999999, "cap": "ban"},
        ],
    }
    return json.dumps(cfg, ensure_ascii=False)


def cap_decision_by_strikes(
    response: ClaudeModerationResponse,
    strike_count: int,
    escalation_json: str | None,
) -> ClaudeModerationResponse:
    """
    Begrenzt die Moderationsentscheidung nach Kumulierten Strikes.

    `strike_count` ist der Stand **nach** Inkrement für diesen Vorfall.
    """
    if not escalation_json:
        return response
    try:
        cfg: dict[str, Any] = json.loads(escalation_json)
    except json.JSONDecodeError:
        logger.warning("strike_escalation_json ungültig — keine Cap-Anwendung.")
        return response

    tiers = cfg.get("tiers") or []
    if not tiers:
        return response

    cap_action_str = "warn"
    for tier in sorted(tiers, key=lambda t: int(t.get("ceil_strikes", 999999))):
        ceil_m = int(tier.get("ceil_strikes", 999999))
        if strike_count <= ceil_m:
            cap_action_str = str(tier.get("cap", "warn")).lower()
            break

    try:
        cap_decision = ModerationDecision(cap_action_str)
    except ValueError:
        cap_decision = ModerationDecision.WARN

    model_rank = _ACTION_RANK.get(response.moderation_decision, 0)
    cap_rank = _ACTION_RANK.get(cap_decision, 1)
    effective_rank = min(model_rank, cap_rank)

    if effective_rank == model_rank:
        return response

    new_action = _RANK_TO_ACTION[effective_rank]
    out = response.model_copy(
        update={
            "moderation_decision": new_action,
            "explanation": (
                f"{response.explanation} [Strike-Cap: angefordert "
                f"{response.moderation_decision.value}, Strikes={strike_count} → {new_action.value}]"
            ),
        },
    )
    if new_action != ModerationDecision.TIMEOUT:
        out = out.model_copy(update={"timeout_minutes": None})
    return out
