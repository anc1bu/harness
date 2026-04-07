# CLAUDE.md

## Architecture

**Stack**: Python Flask backend + Vanilla JS frontend. No build step, no JS framework, no ORM.

```
harness-dev/
├── index.html          # App shell only — no logic, just mounts #app and loads js/app.js
├── server.py           # Flask backend — REST API + serves static files
├── db/
│   └── harness.db      # SQLite database (single file)
├── js/
│   ├── app.js          # Entry point: bootstraps router, checks auth
│   ├── router.js       # Hash-based SPA router (#/login, #/dashboard, #/settings, #/admin)
│   ├── state.js        # Centralized store with subscribe/notify pattern
│   ├── api.js          # Single fetch wrapper for all backend calls
│   ├── auth.js         # Session/login logic (localStorage token)
│   ├── views/          # Full-screen route handlers
│   │   ├── login.js
│   │   ├── dashboard.js
│   │   ├── settings.js
│   │   └── admin.js    # Admin-only: customer + user management
│   └── components/     # Reusable UI pieces
│       ├── modal.js    # toast(msg, type) — 'ok' | 'warn' | 'err'
│       └── table.js    # Data table renderer (200-row preview)
└── css/
    └── theme.css       # CSS variables and base styles
```

### Key Patterns

- **Views**: Each view module exports `mount(container)` — renders itself into the given DOM element. The router calls `mount(appEl)` on route change.
- **Components**: Export a factory or render function; never touch the DOM outside their own root element.
- **State**: `state.js` is the single source of truth. Views subscribe to state slices; mutations go through state setters, never direct assignment.
- **API**: All `fetch()` calls go through `api.js`. It attaches the auth token and normalizes errors.
- **Routing**: Hash-based (`#/login`, `#/dashboard`, `#/settings`, `#/admin`). Unauthenticated requests redirect to `#/login`. Admin users without a customer selected are redirected to `#/admin`.

### Backend (server.py)

Flask + `sqlite3`. All API routes require a Bearer token (session stored in `sessions` table). Routes:
- `POST /api/auth/login` / `POST /api/auth/logout`
- `GET /api/tables`, `GET /api/tables/info`, `DELETE /api/tables/<table>`
- `POST /api/upload` — multipart Excel upload; filename must match `{TABLE}_{SYSTEM}_{CLIENT}_{DATE}.xlsx`
- `GET /api/users`, `POST /api/users`, `PATCH /api/users/<id>`
- `GET /api/customers`, `POST /api/customers`, `DELETE /api/customers/<custname>`
- `GET /api/users/<id>/customers`, `POST /api/users/<id>/customers`, `DELETE /api/users/<id>/customers/<custname>`
- Everything else → `index.html` (SPA fallback)

System tables (`users`, `sessions`, `_table_meta`) are excluded from all user-table queries. `_table_meta` stores upload metadata (system, client, date) keyed by table name.

### Running

```bash
python3 server.py      # http://localhost:5000 — default login: admin / admin
```

## Workflow

- Before doing any work, mention how you could verify that work.

## Data & Storage

- All data storage must use **SQLite** — no JSON files for data, regardless of size.
- Test/debug Excel files go in `test-excel/`.

# test deploy Tue Apr  7 06:37:00 PM CEST 2026
