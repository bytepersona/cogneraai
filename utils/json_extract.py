"""Robustes Extrahieren von JSON aus Claude-Antworten."""

from __future__ import annotations

import json
import re
from typing import Any


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, Any]:
    """
    Versucht, das erste JSON-Objekt aus Freitext zu parsen.

    Entfernt optional umschließende Markdown-Codeblöcke.
    """
    stripped = text.strip()
    m = _JSON_FENCE.search(stripped)
    if m:
        stripped = m.group(1).strip()
    # Rohobjekt suchen
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Kein JSON-Objekt in der Antwort gefunden.")
    blob = stripped[start : end + 1]
    data = json.loads(blob)
    if not isinstance(data, dict):
        raise ValueError("JSON-Wurzel ist kein Objekt.")
    return data
