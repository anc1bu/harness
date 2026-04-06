# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Harness** is a SAP data reference and transactional data management tool. It ingests SAP metadata (DD03L field definitions, DD04T field descriptions, DD08L check-table mappings) and transactional table exports, then provides a web UI for browsing and enriching the data.

## Working Guidelines

Before starting any task, state how you'll verify the work. After completing it, verify it — check best practices, efficiency, and absence of regressions.

Always warn the user if a server restart is required after code changes.

## Running the App

```bash
# Development (Flask dev server)
venv/bin/python app.py

# Development with auth bypassed
HARNESS_NO_AUTH=1 venv/bin/python app.py

# Production-like (Gunicorn)
venv/bin/gunicorn --workers 2 --bind 127.0.0.1:5000 --timeout 600 app:app
```

Environment variables:
- `HARNESS_SECRET` — session key (auto-generated if unset)
- `HARNESS_DATA` — data directory (defaults to `./data`)
- `HARNESS_NO_AUTH=1` — bypass authentication (dev only)

No test suite exists. Testing is manual via the web UI or curl against the API.

## Architecture

**Backend** (`app.py`): Single-file Flask app. All routes and business logic live here.

**Frontend** (`index.html`, `login.html`): Vanilla JS SPA. No build step, no framework.

### Data Storage Layout

```
data/
  users.db        # SQLite: user accounts (auth only)
  harness.db      # SQLite: all application data
    _dd03l        # SAP field definitions (TABNAME+FIELDNAME)
    _dd03l_meta   # Upload metadata for DD03L
    dd04t         # SAP field descriptions (rollname as PK)
    dd04t_meta    # Upload metadata for DD04T
    _dd08l        # Domain/check table→text table mappings
    _dd08l_meta   # Upload metadata for DD08L
    _table_meta   # Upload registry for all transactional tables
    "<TABLE>"     # Dynamic per-table: created at upload time (e.g. "T685A", "VBAK")
```

All column names in dynamic tables are double-quoted to handle SQL keyword conflicts.

### Upload Flow

All uploads go through a single drop zone (`TABLE_SYSTEM_CLIENT_YYYYMMDD.xlsx`). The frontend routes the file based on filename prefix:

| Prefix | Frontend route | Backend endpoint |
|--------|---------------|-----------------|
| `DD03L` | `uploadRef()` | `POST /api/upload/dd03l` |
| `DD04T` | `uploadDD04T()` | `POST /api/upload/dd04t` |
| `DD08L` | `uploadRef()` | `POST /api/upload/dd08l` |
| anything else | `uploadTrans()` | `POST /api/upload/trans` |

### Key Backend Patterns

- **Single harness.db for all app data** — no JSON files; all storage is SQLite regardless of data size
- **Project-based scoping**: `session['project']` is set at login and filters all transactional data; reference tables (DD03L, DD04T, DD08L) are global
- **DD03L upload** (`/api/upload/dd03l`): validates ≥95% of headers match `_DD03L_ALL_COLS`; merges rows into `_dd03l` by TABNAME (replaces existing rows for uploaded TABNAMEs); triggers `_reenrich_all()`
- **DD04T upload streams NDJSON** progress to bypass Cloudflare's 100s timeout; uses `BEGIN EXCLUSIVE` + WAL mode for atomicity; requires `_dd03l_initialized()` first
- **DD08L upload** requires `_dd03l_initialized()` first; validates 100% column match against DD08L fields in `_dd03l`
- **Transactional upload** (`/api/upload/trans`): no column validation; `CREATE TABLE IF NOT EXISTS` + `INSERT OR REPLACE`; key fields (KEYFLAG='X' from `_dd03l`) form the PRIMARY KEY if available; new columns added via `ALTER TABLE ADD COLUMN`; if `table == 'DD03L'` the rows are also merged into `_dd03l`
- **SQLite IN-clause batching** at 500 items to stay under SQLite's 999-variable limit
- **Column enrichment**: FIELDNAME → ROLLNAME → DD04T description; runs at upload time and stored in `_table_meta.enriched_columns`; re-run via `_reenrich_all()` after any reference upload
- **Value description lookup** (`/api/data/<table>/describe`): 3-step chain — look up `CHECKTABLE` from `_dd03l` → find text table via `_dd08l` (FRKART='TEXT') → filter rows by SPRAS (EN/E) → return `{value: VTEXT}` map
- **`VTEXT` is hardcoded** as the value-description column in `describe_column`

### API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/signup` | Register |
| POST | `/api/auth/login` | Authenticate |
| POST | `/api/auth/logout` | Clear session |
| GET | `/api/auth/me` | Auth status check |
| POST | `/api/auth/set-project` | Set active project (create or select) |
| GET | `/api/projects` | List user's projects |
| POST | `/api/upload/dd03l` | Upload DD03L field definitions (merge into `_dd03l`) |
| POST | `/api/upload/dd04t` | Upload DD04T descriptions (streams NDJSON progress) |
| POST | `/api/upload/dd08l` | Upload DD08L check-table mappings |
| POST | `/api/upload/trans` | Upload transactional data (upsert, no column validation) |
| GET | `/api/status` | Dashboard status |
| GET | `/api/data/<table>` | Fetch table rows with enriched headers |
| POST | `/api/data/<table>/describe` | Resolve value descriptions via check table chain |
| DELETE | `/api/data/<table>` | Delete transactional table |
| DELETE | `/api/reference/<name>` | Delete reference data (dd03l / dd04t / dd08l) |

### Frontend Notes

- **Single upload zone** routes files by prefix to the correct endpoint
- **Config list** (`renderRefList`): shows loaded DD03L/DD04T/DD08L with system/client/date and a DELETE button each
- **Trans list** (`renderTableList`): shows all transactional tables with DELETE buttons
- **Render limit**: `renderTable()` shows only the first 200 rows
- **`describeColumn()`**: patches a description column inline after the source column; merged client-side without re-fetching
- **Global state**: `rows`, `columns`, `currentTable`, `serverStatus` are plain globals

## Deployment

Push to `main` → GitHub Actions SSHs into VPS → runs `deploy/provision.sh` (idempotent).

The stack: gunicorn (2 workers, 600s timeout) behind nginx (port 80, Cloudflare handles TLS, buffering disabled for streaming).
