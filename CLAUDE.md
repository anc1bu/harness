# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working Guidelines

Before starting any task, state how you'll verify the work. After completing it, verify it.

**Frontend** (`index.html`): Vanilla JS SPA. No build step, no framework. Testing is manual via the web UI.

## Development

**Local (full stack):**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
HARNESS_NO_AUTH=1 python app.py   # runs on http://127.0.0.1:5000
```

**Deployment:** Push to `main` → GitHub Actions SSHes into VPS and runs `deploy/provision.sh`. The script is idempotent (sets up venv, systemd service, nginx). No manual deploy step needed.

## Architecture

SAP data analysis platform. Users upload SAP SE16N Excel exports; the app stores them in SQLite and lets users browse, enrich, and visualize table relationships.

**Stack:** Flask (`app.py`) → gunicorn → nginx → Cloudflare. Frontend is `index.html` (all JS/CSS embedded, D3.js v7 for graph visualization).

### Databases (`data/`, git-ignored)

Two SQLite databases, both initialized on startup:

- **`users.db`** — auth: `users` table (email, pwhash) and `projects` table for multi-tenant scoping.
- **`harness.db`** — main data store: dynamic transactional tables created per upload, tracked in `_table_meta`.

Both use WAL mode. Upserts use `INSERT OR REPLACE`.

### Key Backend Patterns (`app.py`)

- `init_db()` / `init_harness_db()` — schema creation called on startup.
- `@login_required` decorator gates all data endpoints (bypass with `HARNESS_NO_AUTH=1`).
- Upload file naming convention: `TABLE_SYSTEM_CLIENT_YYYYMMDD.xlsx` (regex-validated).
- Primary keys are inferred from field definitions (`KEYFLAG='X'`).

### Storage Rules

- All persistent data goes in SQLite — no JSON files for data storage.
- Test/debug Excel files go in `test-excel/`.
