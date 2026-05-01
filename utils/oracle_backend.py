"""
Optional: Oracle Autonomous Database — Platzhalter für ein zweites Backend.

Aktuell ist nur `utils.database.ModerationDatabase` (SQLite) implementiert.
Für Oracle würde man hier z. B. `oracledb` async verwenden und dieselben
Methoden wie `ModerationDatabase` implementieren, dann in `create_database`
bei `settings.use_oracle` instanziieren.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class OracleModerationDatabaseNotImplemented:
    """Reserviert für zukünftige Oracle-Implementierung."""

    def __init__(self) -> None:
        logger.warning(
            "OracleModerationDatabase ist nicht implementiert — nutze SQLite oder "
            "implementiere diese Klasse mit oracledb.",
        )
        raise NotImplementedError("Oracle-Backend noch nicht implementiert.")
