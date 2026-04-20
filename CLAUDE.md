# CLAUDE.md

## Architecture

**Stack**: Python Flask backend + Vanilla JS frontend. No build step, no JS framework, no ORM.

```
harness/
├── index.html          # App shell only — no logic, just mounts #app and loads js/app.js
├── server.py           # Flask backend — REST API + serves static files
├── db/
│   └── harness.db      # SQLite database (single file)
├── js/
│   ├── app.js          # Entry point: bootstraps router, checks auth
│   ├── router.js       # Hash-based SPA router (#/login, #/dashboard, #/settings, #/admin)
│   ├── state.js        # Centralized store with subscribe/notify pattern
│   ├── api.js          # Fetch + XHR wrapper for all backend calls (attaches auth token)
│   ├── auth.js         # Session/login logic (localStorage token)
│   ├── views/          # Full-screen route handlers
│   │   ├── login.js
│   │   ├── dashboard.js
│   │   ├── settings.js
│   │   └── admin.js    # Admin-only: customer + user management
│   └── components/     # Reusable UI pieces
│       ├── modal.js    # toast(msg, type) — 'ok' | 'warn' | 'err'
│       ├── table.js    # Data table renderer (200-row preview)
│       └── avatar.js   # Avatar dropdown (logout, admin link)
└── css/
    └── theme.css       # CSS variables and base styles
```

### Key Patterns

- **Views**: Each view module exports `mount(container)` — renders itself into the given DOM element. The router calls `mount(appEl)` on route change.
- **Components**: Export a factory or render function; never touch the DOM outside their own root element.
- **State**: `state.js` is the single source of truth. Views subscribe to state slices; mutations go through state setters, never direct assignment.
- **API**: All backend calls go through `api.js`. Use `api.uploadWithProgress()` (XHR) for file uploads to get progress events; `api.upload()` / `api.get()` etc. for everything else.
- **Routing**: Hash-based (`#/login`, `#/dashboard`, `#/settings`, `#/admin`). Unauthenticated requests redirect to `#/login`. Non-admin users without a customer selected are redirected to `#/admin`. Admin users can access all routes regardless of customer selection.
- **Static file caching**: `server.py` sends `Cache-Control: no-store` for all `.js` and `.css` responses — prevents Cloudflare and browsers from caching stale JS/CSS.

### Backend (server.py)

Flask + `sqlite3`. All API routes require a Bearer token (session stored in `sessions` table). Default login: `admin` / `admin`. Routes:
- `POST /api/auth/login` / `POST /api/auth/logout` / `POST /api/auth/select-customer`
- `GET /api/tables`, `GET /api/tables/info`, `DELETE /api/tables/<table>`, `GET /api/tables/<table>/data`
- `POST /api/upload` — multipart Excel upload; filename must match `{TABLE}_{SYSTEM}_{CLIENT}_{DATE}.xlsx`; validates synchronously then starts a background insert thread; returns `{job_id}`
- `GET /api/upload/status/<job_id>` — polls background job: `{status, phase, total_rows, rows_inserted, orig_table, error}`
- `GET /api/users`, `POST /api/users`, `PATCH /api/users/<id>`
- `GET /api/customers`, `POST /api/customers`, `DELETE /api/customers/<custname>`
- `GET /api/users/<id>/customers`, `POST /api/users/<id>/customers`, `DELETE /api/users/<id>/customers/<custname>`
- Everything else → `index.html` (SPA fallback)

**Upload pipeline**: pre-file validations (DB-only) → read headers → run full validation pipeline → start background thread → return `{job_id}`. Background thread: count rows via ZIP/XML scan (fast, low memory) → insert in 1000-row batches → sort DD03L if needed → mark done.

**Table types**: `master` (DD03L), `basis` (any DD* prefix), `customizing` (everything else). Validation pipelines differ per type.

**System tables** (excluded from all user-table queries): `users`, `sessions`, `_table_meta`, `customers`, `user_customers`, `validation_logs`, `validation_exceptions`, `upload_jobs`. `_table_meta` stores upload metadata (custname, orig_table, system, client, date) keyed by internal table name.

## Rules

- Before doing any work, mention how you could verify that work.
- Before implementing any new feature, analyze and explicitly state its performance impact: which endpoints or code paths are affected, estimated extra DB queries or computation per request, and any risk of slowdown. Warn the user if the impact is non-trivial before writing any code.
- All data storage must use **SQLite** — no JSON files for data, regardless of size.
- Test/debug Excel files go in `test-excel/`.
- Before implementing any feature that involves SAP table structure (columns, key fields, relationships), always query DD03L first to inspect the actual metadata. Key fields (`KEYFLAG='X'`) must always be included when creating or joining tables — never assume key fields from SAP knowledge alone, derive them from DD03L.
