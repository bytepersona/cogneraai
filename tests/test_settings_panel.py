"""Tests für den /settings Admin-Guard und Hilfsfunktionen."""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cogs.settings_panel import _is_admin_or_owner, _yn, _cfg_embed


def _make_interaction(
    *,
    is_owner: bool = False,
    administrator: bool = False,
    guild_id: int = 1,
) -> MagicMock:
    guild = MagicMock()
    guild.id = guild_id
    guild.name = "Test Guild"
    guild.owner_id = 999 if not is_owner else 42

    perms = MagicMock()
    perms.administrator = administrator

    import discord as _discord
    member = MagicMock(spec=_discord.Member)
    member.id = 42
    member.guild_permissions = perms

    interaction = MagicMock()
    interaction.guild = guild
    interaction.user = member
    return interaction


# ── _is_admin_or_owner ──────────────────────────────────────────────────────

def test_owner_is_allowed() -> None:
    ix = _make_interaction(is_owner=True)
    assert _is_admin_or_owner(ix) is True


def test_admin_is_allowed() -> None:
    ix = _make_interaction(administrator=True)
    assert _is_admin_or_owner(ix) is True


def test_normal_member_blocked() -> None:
    ix = _make_interaction()
    assert _is_admin_or_owner(ix) is False


def test_no_guild_blocked() -> None:
    ix = _make_interaction()
    ix.guild = None
    assert _is_admin_or_owner(ix) is False


def test_non_member_blocked() -> None:
    ix = _make_interaction()
    ix.user = MagicMock(spec=["id"])  # no guild_permissions, not a Member
    assert _is_admin_or_owner(ix) is False


# ── _yn ─────────────────────────────────────────────────────────────────────

def test_yn_true() -> None:
    assert "Aktiv" in _yn(True)


def test_yn_false() -> None:
    assert "Inaktiv" in _yn(False)


# ── _cfg_embed sanity ────────────────────────────────────────────────────────

def test_cfg_embed_contains_key_fields() -> None:
    guild = MagicMock()
    guild.name = "TestServer"
    cfg = {
        "ai_enabled": True,
        "dry_run": False,
        "confidence_threshold": 75,
        "default_timeout_minutes": 10,
        "mod_log_channel_id": None,
        "report_channel_id": None,
        "review_queue_enabled": True,
        "review_confidence_floor": 50,
        "strike_escalation_enabled": False,
        "mod_embed_delete_after_seconds": None,
        "url_scan_enabled": False,
        "vt_malicious_threshold": 1,
        "vt_suspicious_threshold": 3,
        "whitelist_user_ids": [1, 2],
        "whitelist_role_ids": [],
        "whitelist_channel_ids": [5],
        "url_allowlist_domains": [],
    }
    embed = _cfg_embed(guild, cfg)
    field_names = [f.name for f in embed.fields]
    assert any("KI" in n for n in field_names)
    assert any("Whitelist Nutzer" in n for n in field_names)
    assert any("Whitelist Rollen" in n for n in field_names)
    assert any("Whitelist Kanäle" in n for n in field_names)
