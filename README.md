# Discord KI-Moderations-Bot (discord.py 2.4+)

Produktionsnaher Discord-Bot in Python, der eingehende Nachrichten mit der **Anthropic Claude API** bewertet (primär **Claude 3 Haiku**, Fallback **Claude 3.5 Sonnet** bei niedriger Confidence oder kritischen Entscheidungen wie Ban). Moderationsaktionen: Verwarnung (DM mit öffentlichem Fallback), Nachricht löschen, Timeout, Ban. Datenhaltung per **SQLite**; Oracle ist konfigurierbar vorbereitet.

## Voraussetzungen

- Python **3.9+** (empfohlen 3.10+)
- Discord-Bot mit aktivierten **Privileged Intents**: **Message Content Intent**
- Anthropic API-Schlüssel

## Installation

```bash
cd CogneraAI
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Trage in `.env` mindestens `DISCORD_TOKEN` und `ANTHROPIC_API_KEY` ein.

## Discord Developer Portal

1. Application → Bot → **Message Content Intent** aktivieren.
2. Bot einladen mit Berechtigungen u. a.: Nachrichten lesen/schreiben, Nachrichten verwalten, Mitglieder moderieren, Mitglieder bannen, Zeitgesteuerte Sperren (Timeout).

## Konfiguration (.env)

| Variable | Beschreibung |
|----------|----------------|
| `DISCORD_TOKEN` | Bot-Token (Pflicht) |
| `ANTHROPIC_API_KEY` | Anthropic API Key (Pflicht) |
| `DISCORD_GUILD_ID` | Optional: Server-ID für **schnelles** Syncen der Slash-Commands beim Entwickeln |
| `CLAUDE_MODEL_HAIKU` / `CLAUDE_MODEL_SONNET` | Modell-IDs (Standard: Haiku + Sonnet) |
| `CONFIDENCE_THRESHOLD` | Globale Standard-Schwelle 0–100 (Guild kann per `/mod-config threshold` überschreiben) |
| `DATABASE_PATH` | Pfad zur SQLite-Datei (Standard `./data/moderation.db`) |
| `RATE_LIMIT_PER_USER_PER_MINUTE` | Schutz vor API-Kosten-Spitzen |
| `MESSAGE_CACHE_TTL_SECONDS` | Kurzzeit-Cache identischer Nachrichten-Hashes |
| `CONTEXT_MESSAGE_COUNT` | Anzahl vorheriger Channel-Nachrichten für Claude |
| `MODERATION_QUEUE_MAX` | Maximale Länge der Nachrichten-Warteschlange (Worker) |
| `ANTHROPIC_CIRCUIT_FAILURE_THRESHOLD` | Nach so vielen API-Fehlern öffnet der Circuit Breaker |
| `ANTHROPIC_CIRCUIT_RESET_SECONDS` | Pause bis zur nächsten Test-Anfrage |

Optional Oracle (nur vorbereitet, Code in `utils/oracle_backend.py` / Hinweis in `utils/database.py`):

- `USE_ORACLE=true` aktuell nur Warnung — es wird weiter SQLite genutzt, bis ein Oracle-Backend implementiert ist.

## Start

```bash
python main.py
```

Beim ersten Start wird die Datenbank unter `DATABASE_PATH` angelegt.

## Slash-Commands

| Befehl | Berechtigung | Funktion |
|--------|----------------|----------|
| `/mod-config rules` | Server verwalten | Server-Regeln für den System-Prompt setzen |
| `/mod-config threshold` | Server verwalten | Confidence-Schwellenwert (Sonnet-Fallback) |
| `/mod-config modlog-channel` | Server verwalten | Optionaler Log-Kanal für KI-Aktionen |
| `/mod-config ai` | Server verwalten | KI-Moderation ein/aus |
| `/mod-config whitelist-*` | Server verwalten | Nutzer, Rollen, Kanäle von der KI ausnehmen |
| `/mod-config default-timeout` | Server verwalten | Fallback-Timeout in Minuten |
| `/mod-config dry-run` | Server verwalten | Shadow-Modus: nur Logs, keine Discord-Aktionen durch KI |
| `/mod-config review-queue` / `review-floor` | Server verwalten | Review-Warteschlange (Ban/low confidence) |
| `/mod-config strikes` | Server verwalten | Strike-Caps aktivieren (schwere Aktionen begrenzen) |
| `/mod-config report-channel` | Server verwalten | Ziel für `/report` |
| `/warn` | Moderieren | Manuelle Verwarnung + DB-Eintrag |
| `/ban` | Bannen | Manueller Ban + Log |
| `/mod-logs` | Server verwalten | Letzte Moderations-Einträge |
| `/mod-stats` | Server verwalten | Aggregierte Aktionen (Zeitraum in Tagen) |
| `/mod-export` | Server verwalten | CSV/JSON-Export der Logs |
| `/report` | Alle | Meldung per Nachrichten-Link an den Report-Kanal |

Review-Fälle erzeugen einen Eintrag mit Buttons (**Bestätigen** / **Ablehnen** / **Nur Timeout 10m**) im konfigurierten Mod-Log-Kanal.

**Slash-Sync:** Ohne `DISCORD_GUILD_ID` werden Befehle **global** synchronisiert (kann bis zu einer Stunde dauern, bis sie überall sichtbar sind). Mit gesetzter Guild-ID erfolgt das Sync **nur für diesen Server** (sofort beim Bot-Start).

## Architektur

- `main.py` — Einstieg, Logging, Bot-Start
- `core_bot.py` — `ModerationBot`-Klasse, `setup_hook` (DB, KI-Client, Rate-Limit, Cache, Cogs, Command-Sync)
- `cogs/moderation.py` — `on_message` → Queue, Worker, Kontext, Claude (mit Circuit Breaker), Strikes, Review-Queue, Aktionen
- `cogs/admin.py` — Slash-Administration, Stats, Export, Reports
- `utils/` — Konfiguration, SQLite (inkl. `review_queue`, `user_strikes`), Anthropic-Client, Strikes, Circuit Breaker, Prompts, JSON-Parsing, Rate-Limit, optional Oracle-Stub

Der **System-Prompt** (ModeratorAI, JSON-Schema) liegt in `utils/prompts.py` und wird zur Laufzeit mit Server-Regeln und Kontext gefüllt.

## Kosten & Sicherheit

- Rate-Limiting und TTL-Cache reduzieren doppelte API-Aufrufe.
- `/mod-config ai` und Whitelists erlauben Abschaltung oder Ausnahmen für sensible Bereiche.
- Ban und `severity: critical` lösen immer ein zweites Modell (Sonnet) aus — zusätzlich zu niedriger Confidence.

## Lizenz / Haftung

Dieses Projekt ist als Ausgangspunkt gedacht. Automatische Bans und Timeouts können Nutzer ausschließen — teste mit einer Test-Guild und minimalen Rechten, bis du die Antwortqualität kennst.
