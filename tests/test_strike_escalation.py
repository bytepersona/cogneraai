"""Unit-Tests für utils.strike_escalation."""

from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.models import ClaudeModerationResponse, ModerationDecision, Severity
from utils.strike_escalation import cap_decision_by_strikes, default_strike_escalation_json


def _make_response(decision: ModerationDecision) -> ClaudeModerationResponse:
    return ClaudeModerationResponse(
        moderation_decision=decision,
        confidence=90,
        severity=Severity.HIGH,
        reason="Test",
        explanation="Test",
        violates_rules=True,
        requires_manual_review=False,
        timeout_minutes=10 if decision == ModerationDecision.TIMEOUT else None,
    )


def test_default_escalation_json_is_valid_json() -> None:
    raw = default_strike_escalation_json()
    data = json.loads(raw)
    assert "tiers" in data
    assert len(data["tiers"]) > 0


def test_no_escalation_json_returns_original() -> None:
    resp = _make_response(ModerationDecision.BAN)
    result = cap_decision_by_strikes(resp, 1, None)
    assert result.moderation_decision == ModerationDecision.BAN


def test_invalid_json_returns_original() -> None:
    resp = _make_response(ModerationDecision.BAN)
    result = cap_decision_by_strikes(resp, 1, "not-valid-json{")
    assert result.moderation_decision == ModerationDecision.BAN


def test_low_strikes_capped_to_warn() -> None:
    escalation = json.dumps({
        "tiers": [
            {"ceil_strikes": 2, "cap": "warn"},
            {"ceil_strikes": 999999, "cap": "ban"},
        ]
    })
    resp = _make_response(ModerationDecision.BAN)
    result = cap_decision_by_strikes(resp, 1, escalation)
    assert result.moderation_decision == ModerationDecision.WARN


def test_mid_strikes_capped_to_delete() -> None:
    escalation = json.dumps({
        "tiers": [
            {"ceil_strikes": 2, "cap": "warn"},
            {"ceil_strikes": 5, "cap": "delete"},
            {"ceil_strikes": 999999, "cap": "ban"},
        ]
    })
    resp = _make_response(ModerationDecision.BAN)
    result = cap_decision_by_strikes(resp, 3, escalation)
    assert result.moderation_decision == ModerationDecision.DELETE


def test_high_strikes_not_capped() -> None:
    escalation = json.dumps({
        "tiers": [
            {"ceil_strikes": 2, "cap": "warn"},
            {"ceil_strikes": 999999, "cap": "ban"},
        ]
    })
    resp = _make_response(ModerationDecision.BAN)
    result = cap_decision_by_strikes(resp, 50, escalation)
    assert result.moderation_decision == ModerationDecision.BAN


def test_decision_below_cap_unchanged() -> None:
    escalation = json.dumps({
        "tiers": [
            {"ceil_strikes": 999999, "cap": "ban"},
        ]
    })
    resp = _make_response(ModerationDecision.WARN)
    result = cap_decision_by_strikes(resp, 1, escalation)
    assert result.moderation_decision == ModerationDecision.WARN


def test_explanation_contains_strike_cap_note() -> None:
    escalation = json.dumps({
        "tiers": [
            {"ceil_strikes": 2, "cap": "warn"},
            {"ceil_strikes": 999999, "cap": "ban"},
        ]
    })
    resp = _make_response(ModerationDecision.BAN)
    result = cap_decision_by_strikes(resp, 1, escalation)
    assert "Strike-Cap" in (result.explanation or "")


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
