# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working Guidelines

Before starting any task, state how you'll verify the work. After completing it, verify it.

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
│   ├── router.js       # Hash-based SPA router (#/login, #/dashboard, #/settings)
│   ├── state.js        # Centralized store with subscribe/notify pattern
│   ├── api.js          # Single fetch wrapper for all backend calls
│   ├── auth.js         # Session/login logic (localStorage token)
│   ├── views/          # Full-screen route handlers
│   │   ├── login.js
│   │   ├── dashboard.js
│   │   └── settings.js
│   └── components/     # Reusable UI pieces
│       ├── modal.js    # toast(msg, type) — 'ok' | 'warn' | 'err'
│       ├── graph.js    # D3 force-directed relationship graph
│       └── table.js    # Data table renderer (200-row preview)
└── css/
    └── theme.css       # CSS variables and base styles
```

### Key Patterns

- **Views**: Each view module exports `mount(container)` — renders itself into the given DOM element. The router calls `mount(appEl)` on route change.
- **Components**: Export a factory or render function; never touch the DOM outside their own root element.
- **State**: `state.js` is the single source of truth. Views subscribe to state slices; mutations go through state setters, never direct assignment.
- **API**: All `fetch()` calls go through `api.js`. It attaches the auth token and normalizes errors.
- **Routing**: Hash-based (`#/login`, `#/dashboard`, `#/settings`). Unauthenticated requests redirect to `#/login`.

### Design System

CSS variables defined in `theme.css` (dark terminal aesthetic):
- `--bg`, `--panel`, `--border` — surface colors
- `--accent` (#4ade80), `--accent2` (#86efac) — primary green palette
- `--warn` (#e0b050), `--danger` (#e06060) — status colors
- `--text`, `--text-dim` — typography

### Backend (server.py)

Flask + `sqlite3`. All API routes require a Bearer token (session stored in `sessions` table). Routes:
- `POST /api/auth/login` / `POST /api/auth/logout`
- `GET /api/tables`, `GET /api/tables/<table>/data`, `DELETE /api/tables/<table>`
- `GET /api/users`, `POST /api/users`
- Everything else → `index.html` (SPA fallback)

### Running

```bash
python3 server.py      # http://localhost:5000 — default login: admin / admin
```

## Data & Storage

- All data storage must use **SQLite** — no JSON files for data, regardless of size.
- Test/debug Excel files go in `test-excel/`.

