"""Integration-Tests für utils.database (In-Memory SQLite)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


async def _make_db(path: str):
    from pathlib import Path
    from utils.database import ModerationDatabase

    db = ModerationDatabase(Path(path))
    await db.connect()
    return db


# ── Schema / Setup ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_setup_creates_tables(db_path):
    db = await _make_db(db_path)
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in await cur.fetchall()}
    assert "guild_config" in tables
    assert "mod_logs" in tables
    assert "user_warnings" in tables
    assert "user_strikes" in tables


# ── Guild config ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_guild_config(db_path):
    db = await _make_db(db_path)
    cfg = await db.get_guild_config(12345)
    assert "ai_enabled" in cfg
    assert "confidence_threshold" in cfg


@pytest.mark.asyncio
async def test_upsert_and_fetch_guild_config(db_path):
    db = await _make_db(db_path)
    await db.upsert_guild_config(12345, ai_enabled=False, confidence_threshold=75)
    cfg = await db.get_guild_config(12345)
    assert cfg["ai_enabled"] is False
    assert int(cfg["confidence_threshold"]) == 75


# ── Warnings ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_and_count_warnings(db_path):
    db = await _make_db(db_path)
    await db.add_warning(1, 42, "Spam", moderator_id=99, source="manual")
    await db.add_warning(1, 42, "Spam 2", moderator_id=99, source="ai")
    count = await db.count_recent_warnings(1, 42)
    assert count == 2


@pytest.mark.asyncio
async def test_fetch_recent_warnings_text(db_path):
    db = await _make_db(db_path)
    await db.add_warning(1, 42, "Bad word", moderator_id=99, source="ai")
    text = await db.fetch_recent_warnings_text(1, 42)
    assert "Bad word" in text


# ── Mod logs ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_mod_log_returns_id_and_case_ref(db_path):
    db = await _make_db(db_path)
    log_id, case_ref = await db.add_mod_log(1, 42, "warn", "Test reason")
    assert isinstance(log_id, int)
    assert case_ref.startswith("CASE-")


@pytest.mark.asyncio
async def test_fetch_mod_logs(db_path):
    db = await _make_db(db_path)
    await db.add_mod_log(1, 42, "warn", "First")
    await db.add_mod_log(1, 42, "delete", "Second")
    logs = await db.fetch_mod_logs(1, limit=10)
    assert len(logs) == 2


@pytest.mark.asyncio
async def test_fetch_mod_log_by_case_ref(db_path):
    db = await _make_db(db_path)
    _, case_ref = await db.add_mod_log(1, 42, "ban", "Bad user", evaluation_json='{"ok": true}')
    found = await db.fetch_mod_log_by_case_ref(1, case_ref)
    assert found is not None
    assert found.case_ref == case_ref
    assert found.action == "ban"


@pytest.mark.asyncio
async def test_fetch_cases_with_evaluation(db_path):
    db = await _make_db(db_path)
    await db.add_mod_log(1, 42, "warn", "No eval")
    await db.add_mod_log(1, 42, "ban", "With eval", evaluation_json='{"decision": "ban"}')
    cases = await db.fetch_cases_with_evaluation(1)
    assert len(cases) == 1
    assert cases[0].evaluation_json is not None


# ── Strikes ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strikes_start_at_zero(db_path):
    db = await _make_db(db_path)
    strikes = await db.get_user_strikes(1, 99)
    assert strikes == 0


@pytest.mark.asyncio
async def test_increment_strikes(db_path):
    db = await _make_db(db_path)
    await db.increment_user_strike(1, 99)
    await db.increment_user_strike(1, 99)
    strikes = await db.get_user_strikes(1, 99)
    assert strikes == 2


# ── Sequence IDs ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_allocate_case_ref_unique(db_path):
    db = await _make_db(db_path)
    r1 = await db.allocate_case_ref(1)
    r2 = await db.allocate_case_ref(1)
    assert r1 != r2
    assert r1.startswith("CASE-")
    assert r2.startswith("CASE-")


@pytest.mark.asyncio
async def test_allocate_event_ref_unique(db_path):
    db = await _make_db(db_path)
    r1 = await db.allocate_event_ref(1)
    r2 = await db.allocate_event_ref(1)
    assert r1 != r2
    assert r1.startswith("EVT-")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
