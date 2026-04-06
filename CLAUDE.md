# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Harness** is a SAP data reference and transactional data management tool. It ingests SAP metadata (DD03L field definitions, DD04T field descriptions) and transactional table exports, then provides a web UI for browsing, searching, and visualizing relationships between them.

## Working Guidelines

Before starting any task, state how you'll verify the work. After completing it, verify it — check best practices, efficiency, and absence of regressions.

Always warn the user if a server restart is required after code changes.

## Running the App

```bash
# Development (Flask dev server)
venv/bin/python app.py
# or via Claude Code launch config: select "Flask Dev Server"

# Production-like (Gunicorn)
venv/bin/gunicorn --workers 2 --bind 127.0.0.1:5000 --timeout 600 app:app
```

Environment variables:
- `HARNESS_SECRET` — session key (auto-generated if unset)
- `HARNESS_DATA` — data directory (defaults to `./data`)
- `HARNESS_NO_AUTH=1` — bypass authentication; set in production (`deploy/harness.service`) by design

No test suite exists. Testing is manual via the web UI.

## Architecture

**Backend** (`app.py`): Single-file Flask app. All routes and business logic live here.

**Frontend** (`index.html`, `login.html`): Vanilla JS SPA. No build step, no framework.

### Data Storage Layout

```
data/
  users.db        # SQLite: user accounts (auth only, separate from app data)
  harness.db      # SQLite: all application data
    _dd03l        # SAP field definitions (TABNAME+FIELDNAME as PK)
    _dd03l_meta   # Upload metadata for DD03L
    dd04t         # SAP field descriptions (rollname as PK)
    dd04t_meta    # Upload metadata for DD04T
    _dd08l        # Domain/check table→text table mappings
    _dd08l_meta   # Upload metadata for DD08L
    _table_meta   # Upload registry for all transactional SAP tables
    "<TABLE>"     # Dynamic per-table: created at upload time (e.g. "T685A", "VBAK")
```

All column names in dynamic tables are double-quoted to handle SQL keyword conflicts.

### Key Backend Patterns

- **Single harness.db for all app data** — no JSON files; all storage is SQLite regardless of data size
- **Project-based scoping**: `session['project']` is set at login and filters all data operations; users see only tables belonging to their active project
- **DD04T upload streams NDJSON** progress to the client to bypass Cloudflare's 100s timeout; uses `BEGIN EXCLUSIVE` + WAL mode for atomicity (readers see old data until COMMIT)
- **SQLite IN-clause batching** at 500 items to stay under SQLite's 999-variable limit
- **Column enrichment**: FIELDNAME → ROLLNAME → DD04T description lookup happens at `/api/data/<table>` time
- **Transactional file naming** is strictly validated: `TABLE_SYSTEM_CLIENT_YYYYMMDD.xlsx`; backend rejects files where <95% of columns match known SAP field names (100% threshold for DD03L uploads)
- **DD03L self-upload**: if filename starts with `DD03L` AND all rows have `TABNAME=DD03L`, the file is treated as a DD03L reference upload (bypasses transactional validation, updates `_dd03l`)
- **Upsert semantics for transactional uploads**: `CREATE TABLE IF NOT EXISTS` + `INSERT OR REPLACE`; key fields (KEYFLAG='X' from `_dd03l`) form the PRIMARY KEY; new columns on re-upload are added via `ALTER TABLE ADD COLUMN`; row count accumulates across uploads
- **Transactional tables keyed by TABLE name only**: `VBAK_SYS_100_20240101.xlsx` → table `"VBAK"` in harness.db; re-uploading merges/upserts into the same table
- **Re-enrichment on reference upload**: `_reenrich_all()` re-runs column enrichment across every stored transactional table whenever DD03L or DD04T is uploaded; merges new enrichments without discarding old ones
- **Value description lookup** (`/api/data/<table>/describe`): 3-step chain — look up `CHECKTABLE` from `_dd03l` for the field → find the text table via `_dd08l` (where `FRKART='TEXT'`) → filter transactional table rows by `SPRAS` (EN/E) and return `{value: VTEXT}` map
- **`VTEXT` is hardcoded** as the value-description column when resolving text table rows in `describe_column`
- **95%/100% column match threshold** applies to non-empty headers only (empty columns excluded from both numerator and denominator)

### API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/signup` | Register |
| POST | `/api/auth/login` | Authenticate |
| POST | `/api/auth/logout` | Clear session |
| GET | `/api/auth/me` | Auth status check |
| POST | `/api/auth/set-project` | Set active project (create or select) |
| GET | `/api/projects` | List user's projects |
| POST | `/api/upload/dd03l` | Upload field definitions |
| POST | `/api/upload/dd04t` | Upload descriptions (streams NDJSON progress) |
| POST | `/api/upload/dd08l` | Upload domain/text table mappings |
| POST | `/api/upload/trans` | Upload transactional data (upsert) |
| GET | `/api/status` | Dashboard status (row counts are total accumulated) |
| GET | `/api/data/<table>` | Fetch table rows with enriched headers |
| POST | `/api/data/<table>/describe` | Resolve value descriptions for a field via check table chain |
| DELETE | `/api/data/<table>` | Remove table |

### Frontend Notes

- **Layout**: Left panel (320px) split into "Configuration Tables" (DD* uploads) and "Transactional Tables" sections; main area shows data table; topbar with status dots and active project name
- **Render limit**: `renderTable()` shows only the first 200 rows; no pagination or server-side filtering
- **`describeColumn()`**: patches a description column inline after the source column; result is merged client-side without re-fetching the table
- **Global state**: `rows`, `columns`, `currentTable`, `serverStatus` are plain globals; updates flow as server fetch → global assignment → imperative DOM render

## Deployment

Push to `main` → GitHub Actions SSHs into VPS → runs `deploy/provision.sh` (idempotent).

The stack: gunicorn (2 workers, 600s timeout) behind nginx (port 80, Cloudflare handles TLS, buffering disabled for streaming).
