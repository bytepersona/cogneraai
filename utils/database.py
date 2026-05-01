"""
Persistente Speicherung (SQLite Standard).

Oracle Autonomous: Konfiguration ist in `Settings` vorgesehen; für produktiven
Oracle-Betrieb müsste hier ein zweites Backend (z. B. oracledb + Connection Pool)
implementiert und per `use_oracle` umgeschaltet werden.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from utils.config import Settings
from utils.strike_escalation import default_strike_escalation_json

logger = logging.getLogger(__name__)

# Sentinel: „Parameter nicht gesetzt“ vs. explizit ``None`` (Kanal löschen)
_UNSET = object()


@dataclass(frozen=True)
class StoredMessage:
    """Eine gespeicherte Channel-Nachricht für Kontext."""

    author_id: int
    author_name: str
    content: str
    created_at_iso: str
    message_id: int


@dataclass(frozen=True)
class ModLogEntry:
    """Eintrag für Moderations-Logs."""

    id: int
    guild_id: int
    channel_id: int | None
    target_user_id: int
    actor_id: int | None
    action: str
    reason: str
    details: str | None
    created_at_iso: str


@dataclass(frozen=True)
class ReviewQueueEntry:
    """Ausstehende manuelle Freigabe."""

    id: int
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    proposed_decision: str
    payload_json: str
    status: str
    created_at_iso: str
    jump_url: Optional[str]


class ModerationDatabase:
    """SQLite-Zugriff für Guild-Konfiguration, Nachrichtenverlauf, Warnungen und Logs."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def connect(self) -> None:
        """Erstellt das Verzeichnis und initialisiert Tabellen."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.executescript(_SCHEMA_SQL)
            await _migrate_schema(db)
            await db.commit()
        logger.info("Datenbank bereit: %s", self._db_path)

    async def get_guild_config(self, guild_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM guild_config WHERE guild_id = ?",
                (guild_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return _default_guild_config(guild_id)
            return _row_to_guild_config(dict(row))

    async def upsert_guild_config(
        self,
        guild_id: int,
        *,
        server_rules: str | None = None,
        confidence_threshold: int | None = None,
        default_timeout_minutes: int | None = None,
        mod_log_channel_id: Any = _UNSET,
        whitelist_user_ids: list[int] | None = None,
        whitelist_role_ids: list[int] | None = None,
        whitelist_channel_ids: list[int] | None = None,
        ai_enabled: bool | None = None,
        dry_run: bool | None = None,
        strike_escalation_enabled: bool | None = None,
        strike_escalation_json: str | None = None,
        review_queue_enabled: bool | None = None,
        review_confidence_floor: int | None = None,
        report_channel_id: Any = _UNSET,
    ) -> None:
        current = await self.get_guild_config(guild_id)
        merged = {
            "server_rules": server_rules if server_rules is not None else current["server_rules"],
            "confidence_threshold": confidence_threshold
            if confidence_threshold is not None
            else current["confidence_threshold"],
            "default_timeout_minutes": default_timeout_minutes
            if default_timeout_minutes is not None
            else current["default_timeout_minutes"],
            "mod_log_channel_id": current["mod_log_channel_id"]
            if mod_log_channel_id is _UNSET
            else mod_log_channel_id,
            "whitelist_user_ids": whitelist_user_ids
            if whitelist_user_ids is not None
            else current["whitelist_user_ids"],
            "whitelist_role_ids": whitelist_role_ids
            if whitelist_role_ids is not None
            else current["whitelist_role_ids"],
            "whitelist_channel_ids": whitelist_channel_ids
            if whitelist_channel_ids is not None
            else current["whitelist_channel_ids"],
            "ai_enabled": ai_enabled if ai_enabled is not None else current["ai_enabled"],
            "dry_run": dry_run if dry_run is not None else current["dry_run"],
            "strike_escalation_enabled": strike_escalation_enabled
            if strike_escalation_enabled is not None
            else current["strike_escalation_enabled"],
            "strike_escalation_json": strike_escalation_json
            if strike_escalation_json is not None
            else current["strike_escalation_json"],
            "review_queue_enabled": review_queue_enabled
            if review_queue_enabled is not None
            else current["review_queue_enabled"],
            "review_confidence_floor": review_confidence_floor
            if review_confidence_floor is not None
            else current["review_confidence_floor"],
            "report_channel_id": current["report_channel_id"]
            if report_channel_id is _UNSET
            else report_channel_id,
        }
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO guild_config (
                    guild_id, server_rules, confidence_threshold, default_timeout_minutes,
                    mod_log_channel_id, whitelist_user_ids, whitelist_role_ids,
                    whitelist_channel_ids, ai_enabled,
                    dry_run, strike_escalation_enabled, strike_escalation_json,
                    review_queue_enabled, review_confidence_floor, report_channel_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    server_rules = excluded.server_rules,
                    confidence_threshold = excluded.confidence_threshold,
                    default_timeout_minutes = excluded.default_timeout_minutes,
                    mod_log_channel_id = excluded.mod_log_channel_id,
                    whitelist_user_ids = excluded.whitelist_user_ids,
                    whitelist_role_ids = excluded.whitelist_role_ids,
                    whitelist_channel_ids = excluded.whitelist_channel_ids,
                    ai_enabled = excluded.ai_enabled,
                    dry_run = excluded.dry_run,
                    strike_escalation_enabled = excluded.strike_escalation_enabled,
                    strike_escalation_json = excluded.strike_escalation_json,
                    review_queue_enabled = excluded.review_queue_enabled,
                    review_confidence_floor = excluded.review_confidence_floor,
                    report_channel_id = excluded.report_channel_id
                """,
                (
                    guild_id,
                    merged["server_rules"],
                    merged["confidence_threshold"],
                    merged["default_timeout_minutes"],
                    merged["mod_log_channel_id"],
                    json.dumps(merged["whitelist_user_ids"]),
                    json.dumps(merged["whitelist_role_ids"]),
                    json.dumps(merged["whitelist_channel_ids"]),
                    1 if merged["ai_enabled"] else 0,
                    1 if merged["dry_run"] else 0,
                    1 if merged["strike_escalation_enabled"] else 0,
                    merged["strike_escalation_json"],
                    1 if merged["review_queue_enabled"] else 0,
                    merged["review_confidence_floor"],
                    merged["report_channel_id"],
                ),
            )
            await db.commit()

    async def insert_message(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        author_name: str,
        content: str,
        created_at_iso: str,
        *,
        keep_per_channel: int = 200,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO message_history (
                    guild_id, channel_id, message_id, author_id, author_name, content, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    message_id,
                    author_id,
                    author_name,
                    content[:4000],
                    created_at_iso,
                ),
            )
            await db.execute(
                """
                DELETE FROM message_history
                WHERE channel_id = ?
                AND id NOT IN (
                    SELECT id FROM (
                        SELECT id FROM message_history
                        WHERE channel_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                    )
                )
                """,
                (channel_id, channel_id, keep_per_channel),
            )
            await db.commit()

    async def fetch_recent_messages(
        self,
        channel_id: int,
        limit: int,
    ) -> list[StoredMessage]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT author_id, author_name, content, created_at_iso, message_id
                FROM message_history
                WHERE channel_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (channel_id, limit),
            )
            rows = await cur.fetchall()
        # chronologisch aufsteigend für Lesbarkeit
        out: list[StoredMessage] = []
        for row in reversed(rows):
            r = dict(row)
            out.append(
                StoredMessage(
                    author_id=int(r["author_id"]),
                    author_name=str(r["author_name"]),
                    content=str(r["content"]),
                    created_at_iso=str(r["created_at_iso"]),
                    message_id=int(r["message_id"]),
                )
            )
        return out

    async def add_warning(
        self,
        guild_id: int,
        user_id: int,
        reason: str,
        *,
        moderator_id: int | None = None,
        source: str = "manual",
    ) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO user_warnings (guild_id, user_id, moderator_id, reason, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, moderator_id, reason[:2000], source),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def count_recent_warnings(self, guild_id: int, user_id: int, limit: int = 10) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM user_warnings
                    WHERE guild_id = ? AND user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (guild_id, user_id, limit),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def fetch_recent_warnings_text(self, guild_id: int, user_id: int, limit: int = 5) -> str:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT reason, source, created_at_iso FROM user_warnings
                WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (guild_id, user_id, limit),
            )
            rows = await cur.fetchall()
        if not rows:
            return "Keine gespeicherten Verwarnungen."
        lines = []
        for r in rows:
            d = dict(r)
            lines.append(
                f"- [{d['created_at_iso']}] ({d['source']}) {d['reason'][:500]}",
            )
        return "\n".join(lines)

    async def add_mod_log(
        self,
        guild_id: int,
        target_user_id: int,
        action: str,
        reason: str,
        *,
        channel_id: int | None = None,
        actor_id: int | None = None,
        details: str | None = None,
    ) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO mod_logs (
                    guild_id, channel_id, target_user_id, actor_id, action, reason, details, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    target_user_id,
                    actor_id,
                    action,
                    reason[:2000],
                    details[:4000] if details else None,
                    ts,
                ),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def fetch_mod_logs(
        self,
        guild_id: int,
        *,
        limit: int = 20,
        target_user_id: int | None = None,
    ) -> list[ModLogEntry]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if target_user_id is None:
                cur = await db.execute(
                    """
                    SELECT id, guild_id, channel_id, target_user_id, actor_id, action, reason, details, created_at_iso
                    FROM mod_logs WHERE guild_id = ? ORDER BY id DESC LIMIT ?
                    """,
                    (guild_id, limit),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT id, guild_id, channel_id, target_user_id, actor_id, action, reason, details, created_at_iso
                    FROM mod_logs
                    WHERE guild_id = ? AND target_user_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (guild_id, target_user_id, limit),
                )
            rows = await cur.fetchall()
        result: list[ModLogEntry] = []
        for row in rows:
            r = dict(row)
            result.append(
                ModLogEntry(
                    id=int(r["id"]),
                    guild_id=int(r["guild_id"]),
                    channel_id=int(r["channel_id"]) if r["channel_id"] is not None else None,
                    target_user_id=int(r["target_user_id"]),
                    actor_id=int(r["actor_id"]) if r["actor_id"] is not None else None,
                    action=str(r["action"]),
                    reason=str(r["reason"]),
                    details=str(r["details"]) if r["details"] is not None else None,
                    created_at_iso=str(r["created_at_iso"]),
                )
            )
        return result

    async def increment_user_strike(self, guild_id: int, user_id: int) -> int:
        """Erhöht den Strike-Zähler um 1 und liefert den neuen Wert."""
        ts = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO user_strikes (guild_id, user_id, strikes, updated_at_iso)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    strikes = strikes + 1,
                    updated_at_iso = excluded.updated_at_iso
                """,
                (guild_id, user_id, ts),
            )
            cur = await db.execute(
                "SELECT strikes FROM user_strikes WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = await cur.fetchone()
            await db.commit()
        return int(row[0]) if row else 1

    async def get_user_strikes(self, guild_id: int, user_id: int) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT strikes FROM user_strikes WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def insert_review_queue(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        author_id: int,
        proposed_decision: str,
        payload_json: str,
        *,
        jump_url: Optional[str] = None,
    ) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO review_queue (
                    guild_id, channel_id, message_id, author_id,
                    proposed_decision, payload_json, status, created_at_iso, jump_url
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    guild_id,
                    channel_id,
                    message_id,
                    author_id,
                    proposed_decision,
                    payload_json[:12000],
                    ts,
                    jump_url,
                ),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def get_review_queue_entry(self, entry_id: int) -> Optional[ReviewQueueEntry]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM review_queue WHERE id = ?",
                (entry_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        r = dict(row)
        return ReviewQueueEntry(
            id=int(r["id"]),
            guild_id=int(r["guild_id"]),
            channel_id=int(r["channel_id"]),
            message_id=int(r["message_id"]),
            author_id=int(r["author_id"]),
            proposed_decision=str(r["proposed_decision"]),
            payload_json=str(r["payload_json"]),
            status=str(r["status"]),
            created_at_iso=str(r["created_at_iso"]),
            jump_url=str(r["jump_url"]) if r["jump_url"] is not None else None,
        )

    async def update_review_queue_status(self, entry_id: int, status: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE review_queue SET status = ? WHERE id = ?",
                (status, entry_id),
            )
            await db.commit()

    async def aggregate_mod_actions(
        self,
        guild_id: int,
        *,
        days: int = 7,
    ) -> dict[str, int]:
        """Zählt Moderationsaktionen seit `days` Tagen."""
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                """
                SELECT action, COUNT(*) FROM mod_logs
                WHERE guild_id = ? AND created_at_iso >= ?
                GROUP BY action
                """,
                (guild_id, since),
            )
            rows = await cur.fetchall()
        return {str(a): int(c) for a, c in rows}

    async def fetch_mod_logs_range(
        self,
        guild_id: int,
        *,
        limit: int = 5000,
        days: Optional[int] = None,
    ) -> list[ModLogEntry]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if days is not None:
                since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
                cur = await db.execute(
                    """
                    SELECT id, guild_id, channel_id, target_user_id, actor_id, action, reason, details, created_at_iso
                    FROM mod_logs
                    WHERE guild_id = ? AND created_at_iso >= ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (guild_id, since, limit),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT id, guild_id, channel_id, target_user_id, actor_id, action, reason, details, created_at_iso
                    FROM mod_logs WHERE guild_id = ? ORDER BY id DESC LIMIT ?
                    """,
                    (guild_id, limit),
                )
            rows = await cur.fetchall()
        result: list[ModLogEntry] = []
        for row in rows:
            r = dict(row)
            result.append(
                ModLogEntry(
                    id=int(r["id"]),
                    guild_id=int(r["guild_id"]),
                    channel_id=int(r["channel_id"]) if r["channel_id"] is not None else None,
                    target_user_id=int(r["target_user_id"]),
                    actor_id=int(r["actor_id"]) if r["actor_id"] is not None else None,
                    action=str(r["action"]),
                    reason=str(r["reason"]),
                    details=str(r["details"]) if r["details"] is not None else None,
                    created_at_iso=str(r["created_at_iso"]),
                )
            )
        return result


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    """Fügt fehlende Spalten (ältere DBs) und neue Tabellen hinzu."""
    cur = await db.execute("PRAGMA table_info(guild_config)")
    cols = {str(r[1]) for r in await cur.fetchall()}
    alters: list[tuple[str, str]] = [
        ("dry_run", "INTEGER NOT NULL DEFAULT 0"),
        ("strike_escalation_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("strike_escalation_json", "TEXT NOT NULL DEFAULT ''"),
        ("review_queue_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("review_confidence_floor", "INTEGER NOT NULL DEFAULT 50"),
        ("report_channel_id", "INTEGER"),
    ]
    for name, decl in alters:
        if name not in cols:
            await db.execute(f"ALTER TABLE guild_config ADD COLUMN {name} {decl}")


def _default_guild_config(guild_id: int) -> dict[str, Any]:
    return {
        "guild_id": guild_id,
        "server_rules": (
            "(Noch keine Regeln gesetzt — nutze /mod-config um Verhaltensregeln zu hinterlegen.)"
        ),
        "confidence_threshold": 75,
        "default_timeout_minutes": 10,
        "mod_log_channel_id": None,
        "whitelist_user_ids": [],
        "whitelist_role_ids": [],
        "whitelist_channel_ids": [],
        "ai_enabled": True,
        "dry_run": False,
        "strike_escalation_enabled": False,
        "strike_escalation_json": default_strike_escalation_json(),
        "review_queue_enabled": True,
        "review_confidence_floor": 50,
        "report_channel_id": None,
    }


def _row_to_guild_config(row: dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    sj = str(r.get("strike_escalation_json") or "").strip()
    if not sj:
        sj = default_strike_escalation_json()
    dry_run = bool(r.get("dry_run", 0))
    strike_en = bool(r.get("strike_escalation_enabled", 0))
    rq_en = bool(r.get("review_queue_enabled", 1))
    rq_floor = int(r.get("review_confidence_floor", 50))
    rcid = r.get("report_channel_id")
    return {
        "guild_id": int(r["guild_id"]),
        "server_rules": str(r["server_rules"]),
        "confidence_threshold": int(r["confidence_threshold"]),
        "default_timeout_minutes": int(r["default_timeout_minutes"]),
        "mod_log_channel_id": int(r["mod_log_channel_id"])
        if r["mod_log_channel_id"] is not None
        else None,
        "whitelist_user_ids": json.loads(r["whitelist_user_ids"] or "[]"),
        "whitelist_role_ids": json.loads(r["whitelist_role_ids"] or "[]"),
        "whitelist_channel_ids": json.loads(r["whitelist_channel_ids"] or "[]"),
        "ai_enabled": bool(r["ai_enabled"]),
        "dry_run": dry_run,
        "strike_escalation_enabled": strike_en,
        "strike_escalation_json": sj,
        "review_queue_enabled": rq_en,
        "review_confidence_floor": rq_floor,
        "report_channel_id": int(rcid) if rcid is not None else None,
    }


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    server_rules TEXT NOT NULL DEFAULT '',
    confidence_threshold INTEGER NOT NULL DEFAULT 75,
    default_timeout_minutes INTEGER NOT NULL DEFAULT 10,
    mod_log_channel_id INTEGER,
    whitelist_user_ids TEXT NOT NULL DEFAULT '[]',
    whitelist_role_ids TEXT NOT NULL DEFAULT '[]',
    whitelist_channel_ids TEXT NOT NULL DEFAULT '[]',
    ai_enabled INTEGER NOT NULL DEFAULT 1,
    dry_run INTEGER NOT NULL DEFAULT 0,
    strike_escalation_enabled INTEGER NOT NULL DEFAULT 0,
    strike_escalation_json TEXT NOT NULL DEFAULT '',
    review_queue_enabled INTEGER NOT NULL DEFAULT 1,
    review_confidence_floor INTEGER NOT NULL DEFAULT 50,
    report_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS message_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at_iso TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_channel ON message_history(channel_id);

CREATE TABLE IF NOT EXISTS user_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER,
    reason TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at_iso TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_warn_guild_user ON user_warnings(guild_id, user_id);

CREATE TABLE IF NOT EXISTS mod_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    target_user_id INTEGER NOT NULL,
    actor_id INTEGER,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    details TEXT,
    created_at_iso TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mod_guild ON mod_logs(guild_id);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    proposed_decision TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at_iso TEXT NOT NULL,
    jump_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_guild ON review_queue(guild_id);

CREATE TABLE IF NOT EXISTS user_strikes (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    strikes INTEGER NOT NULL DEFAULT 0,
    updated_at_iso TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""


async def create_database(settings: Settings) -> ModerationDatabase:
    """
    Factory: bei `use_oracle=True` könnte hier ein Oracle-Backend zurückgegeben werden.
    Aktuell immer SQLite.
    """
    if settings.use_oracle:
        logger.warning(
            "USE_ORACLE ist True, aber nur SQLite ist implementiert — verwende SQLite. "
            "Oracle bitte in utils/database.py anbinden.",
        )
    db = ModerationDatabase(settings.database_path)
    await db.connect()
    return db
