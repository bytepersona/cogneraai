"""Tests für Regel-ID-Normalisierung und Katalog."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.default_server_rules import (
    KNOWN_RULE_IDS,
    format_rule_ids_line,
    normalize_violated_rule_ids,
)
from utils.models import ClaudeModerationResponse, ModerationDecision, Severity


def test_normalize_accepts_list_and_dedupes() -> None:
    assert normalize_violated_rule_ids(["1.1.2", " 1.1.2 ", "9.9.9", "x", ""]) == ["1.1.2", "9.9.9"]


def test_normalize_accepts_comma_string() -> None:
    assert normalize_violated_rule_ids("3.2.1; 3.2.2") == ["3.2.1", "3.2.2"]


def test_catalog_covers_all_paragraph_ids() -> None:
    assert "6.2.2" in KNOWN_RULE_IDS
    assert "3.3.1" in KNOWN_RULE_IDS


def test_format_rule_ids_line() -> None:
    s = format_rule_ids_line(["1.1.1", "2.1.2"])
    assert "`1.1.1`" in s and "`2.1.2`" in s


def test_model_accepts_violated_rule_ids_alias() -> None:
    r = ClaudeModerationResponse.model_validate_loose(
        {
            "moderation_decision": "warn",
            "confidence": 80,
            "violatedRuleIds": ["1.3.1", "bad", "1.3.1"],
        },
    )
    assert r.violated_rule_ids == ["1.3.1"]
    assert r.moderation_decision == ModerationDecision.WARN


def test_model_defaults_empty_violated_rule_ids() -> None:
    r = ClaudeModerationResponse(
        moderation_decision=ModerationDecision.ALLOW,
        confidence=100,
        severity=Severity.NONE,
    )
    assert r.violated_rule_ids == []
