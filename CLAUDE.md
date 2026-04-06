# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Harness** is a SAP data reference and transactional data management tool. It ingests SAP metadata (DD03L field definitions, DD04T field descriptions) and transactional table exports, then provides a web UI for browsing, searching, and visualizing relationships between them.

## Running the App

```bash
# Development (Flask dev server)
python app.py
# or via Claude Code launch config: select "Flask Dev Server"

# Production-like (Gunicorn)
venv/bin/gunicorn --workers 2 --bind 127.0.0.1:5000 --timeout 600 app:app
```

Environment variables:
- `HARNESS_SECRET` — session key (auto-generated if unset)
- `HARNESS_DATA` — data directory (defaults to `./data`)
- `HARNESS_NO_AUTH=1` — bypass authentication (dev mode)

No test suite exists. Testing is manual via the web UI.

## Architecture

**Backend** (`app.py`): Single-file Flask app. All routes and business logic live here.

**Frontend** (`index.html`, `login.html`): Vanilla JS SPA with D3.js for graph visualization. No build step, no framework.

### Data Storage Layout

```
data/
  users.db                    # SQLite: user accounts
  reference/
    dd03l.json                # SAP field definitions (merged on upload)
    dd04t.sqlite              # SAP field descriptions (SQLite for fast lookup)
  transactional/
    TABLE_SYSTEM_CLIENT_DATE.json   # Per-table transactional data
```

### Key Backend Patterns

- **DD04T uses SQLite** (not JSON) because the dataset can be 46MB+; loading it all into memory caused OOM
- **DD04T upload streams NDJSON** progress to the client to bypass Cloudflare's 100s timeout
- **SQLite IN-clause batching** at 500 items to stay under SQLite's 999-variable limit
- **Column enrichment**: FIELDNAME → ROLLNAME → DD04T description lookup happens at `/api/data/<table>` time
- **Transactional file naming** is strictly validated: `TABLE_SYSTEM_CLIENT_YYYYMMDD.xlsx`; backend rejects files where <50% of columns match known SAP field names

### API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/signup` | Register |
| POST | `/api/auth/login` | Authenticate |
| POST | `/api/auth/logout` | Clear session |
| GET | `/api/auth/me` | Auth status check |
| POST | `/api/upload/dd03l` | Upload field definitions (merges) |
| POST | `/api/upload/dd04t` | Upload descriptions (streams NDJSON progress) |
| POST | `/api/upload/trans` | Upload transactional data |
| GET | `/api/status` | Dashboard status |
| GET | `/api/data/<table>` | Fetch table rows with enriched headers |
| DELETE | `/api/data/<table>` | Remove table |

## Deployment

Push to `main` → GitHub Actions SSHs into VPS → runs `deploy/provision.sh` (idempotent).

The stack: gunicorn (2 workers, 600s timeout) behind nginx (port 80, Cloudflare handles TLS, buffering disabled for streaming).
