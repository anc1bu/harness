# CLAUDE.md

## Stack

Python Flask + Vanilla JS SPA. No build step, no JS framework, no ORM. SQLite only.

## File Tree

```
harness/
├── server.py              # Flask backend — all API routes + static serving
├── db/harness.db          # SQLite (single file)
├── data/
│   └── reference/
│       ├── dd03l.json     # Preloaded SAP DD03L metadata (12 KB)
│       └── dd04t.sqlite   # Preloaded text descriptions (96 MB) — fallback if DD04T not uploaded
├── js/
│   ├── app.js             # Entry point: bootstraps router, restores session
│   ├── router.js          # Hash-based SPA router with auth guards
│   ├── state.js           # Centralized store — getState/setState/subscribe/unsubscribe
│   ├── api.js             # Fetch wrapper: attaches Bearer token, handles 401 redirect
│   ├── auth.js            # login/logout/selectCustomer, persists to localStorage
│   ├── views/
│   │   ├── login.js       # Step 1: credentials → Step 2: customer selection
│   │   ├── dashboard.js   # Main data explorer (table panel + upload + panel assignments)
│   │   ├── settings.js    # Tables (drop), Password (self-service), Users (admin)
│   │   └── admin.js       # Customers, Users, Validation logs/exceptions, Sub-panels
│   └── components/
│       ├── table.js       # Excel-style table renderer
│       ├── modal.js       # toast(msg, type) — 'ok' | 'warn' | 'err'
│       └── avatar.js      # 8 named avatars + dropdown picker (stored in localStorage)
└── css/theme.css          # CSS variables, dark theme, all shared styles
```

## Key Patterns

- **Views**: export `mount(container)` — renders into DOM element. Router calls `mount(appEl)` on route change.
- **State**: `state.js` is the single source of truth. Mutations go through `setState`, never direct assignment.
- **API**: all backend calls go through `api.js`. Use `api.uploadWithProgress()` (XHR) for file uploads; `api.get/post/patch/delete()` for everything else.
- **Routing**: `#/login`, `#/dashboard`, `#/settings`, `#/admin`. Unauthenticated → `#/login`. Auth + no customer selected + non-admin → rejected. Non-admin cannot access `#/admin`.
- **Static caching**: server sends `Cache-Control: no-store` for all `.js` and `.css` — prevents Cloudflare/browser caching stale JS/CSS.

## Backend (server.py)

Auth: Bearer token in `Authorization` header (or `?token=` for GET). Token = `secrets.token_hex(32)` stored in `sessions` table. Default login: `admin` / `admin`.

Password hashing: PBKDF2-HMAC-SHA256, 260k iterations, random 16-byte salt. Format: `pbkdf2:260000:{salt}:{hash}`. Legacy plain SHA-256 auto-upgraded on next login. Min length enforced at API: 6 chars.

`_SYSTEM_TABLES = ('users', 'sessions', '_table_meta', 'customers', 'user_customers')` — excluded from user-table queries. `validation_logs`, `validation_exceptions`, `upload_jobs`, `panel_assignments`, `panel_section_states`, `sub_panels`, `user_table_prefs` also exist but are NOT in `_SYSTEM_TABLES`.

### Routes

```
POST /api/auth/login
POST /api/auth/logout
POST /api/auth/select-customer
POST /api/auth/change-password          # requires current_password + new_password (min 6)

GET  /api/tables                        # list internal table names
GET  /api/tables/info                   # list with orig_table, system, client, date, count, description
DEL  /api/tables/<table>

GET  /api/tables/<table>/data           # params: offset, limit (max 10000), f.COL=pattern
GET  /api/tables/<table>/distinct       # params: col, f.COL=... — for filter dropdowns
GET  /api/tables/<table>/export         # streaming CSV, all rows enriched
GET|PATCH|DEL /api/tables/<table>/layout        # col_order + col_widths, per user
PATCH /api/tables/<table>/col-widths    # legacy; layout PATCH preferred

GET|POST /api/panel-assignments
GET|POST /api/panel-sections
GET|POST /api/sub-panels
PATCH|DEL /api/sub-panels/<id>

POST /api/upload                        # multipart; filename: {TABLE}_{SYSTEM}_{CLIENT}_{DATE}.xlsx
GET  /api/upload/status/<job_id>        # {status, phase, total_rows, rows_inserted, orig_table, error}

GET  /api/users
POST /api/users
PATCH /api/users/<id>                   # accepts: is_admin, password (admin-only reset)

GET  /api/customers
POST /api/customers
DEL  /api/customers/<custname>

GET  /api/users/<id>/customers
POST /api/users/<id>/customers
DEL  /api/users/<id>/customers/<custname>

GET  /api/validation-logs
GET|POST /api/validation-exceptions
DEL  /api/validation-exceptions/<id>

* → index.html (SPA fallback)
```

### Table Types

`_determine_table_type(name)`:
- `'master'` — DD03L only
- `'basis'` — any other DD* prefix
- `'customizing'` — everything else

Validation pipelines differ per type. Master: V1–V5. Basis/customizing: V1, V6–V9.

### Upload Pipeline

Synchronous: filename parse → upload gate check (DD03L-first, system/client lock) → read headers via ZIP/XML stream → run header-only validations → write temp file to `/tmp/harness_upload_{job_id}.xlsx` → return `{job_id}`.

Background thread phases: `queued → validating → counting → inserting → sorting → indexing → done` (or `error`). Inserts in 1000-row batches. DD03L sorted by TABNAME+POSITION after insert. Temp file deleted in finally block.

### Enrichment System

When serving table data, columns are joined with SAP text tables to produce enriched names (`COL - Description`) and enriched cell values (`value - label`). Sources:

- **DD04T** — ROLLNAME → field description (used for column headers)
- **DD08L** — which text table to join per field
- **DD07T** — domain value descriptions
- **T683T** — subtotal descriptions (T683S.KSCHL special case)
- **TMC1T** — KOTABNR descriptions (tables with KOTABNR + KVEWE)

`data/reference/dd04t.sqlite` is used as fallback if DD04T hasn't been uploaded. Enrichment metadata is cached in-process via `_cached_setup_enrichment()`.

`GET /api/tables/<table>/data` response includes:
- `columns` — enriched names; `raw_columns` — SAP names (parallel arrays)
- `col_text_tables` — `{enriched_col: [hint_strings]}` for UI tooltips
- `dd04t_missing`, `partial_descriptions`, `missing_fields` — for frontend warnings

Filter params use **raw column names**: `f.KSCHL=RR`, `f.KSCHL==RR||PR` (IN filter).

## Frontend — table.js

```js
renderTable(wrapEl, {
  rows, columns, rawColumns, colTextTables, total,
  onExport, onFilter, onDistinct,
  colWidths, onSaveColWidths,
  colOrder, onSaveColOrder, onClearLayout,
  initialFilters,   // {raw_col: pattern} — pre-seed filter state
  onFilterChange,   // (filters) → void — called on every filter change (both server-side and client-side) + clear
})
```

Constants: `PREVIEW_LIMIT = 5000`, `RENDER_BATCH = 200` (lazy via IntersectionObserver), `MAX_DROPDOWN_VALS = 500`.

Filter state: `activeFilters` (client-side, `col → Set<string>`), `activePatterns` (server text, `col → string`), `activeCheckboxes` (server IN, `col → Set<string>`). Keys are enriched column names. `_buildCurrentFilters()` maps server-side state back to raw names. `_buildFiltersForCarryover()` includes `activeFilters` too — used for `onFilterChange`.

`initialFilters` seeding: builds `rawToEnriched` reverse map; seeds `activePatterns`/`activeCheckboxes` (server-side) and `activeFilters` (client-side, matched against `uniqueVals`) before first render. Columns absent from the new table are silently skipped.

Pin column: type exact name + Enter → `cols.unshift(col)` → `_buildHeaders()` + `_renderRows()` + `onSaveColOrder()`.

Layout reset button (↺ RESET LAYOUT): visible when `cols` differs from `_origCols`.

## Frontend — dashboard.js

State stored on the container element (not `state.js`):
```js
container._selectedTables    = new Map()  // table → {table, origTable, description}
container._partitionCleanups = []         // cleanup fns for multi-table renders
container._activeFilters     = {}         // raw_col → pattern, shared across table selections
container._tableLoading      = bool
```

Sticky filters: `_activeFilters` is passed as `initialFilters` to every `renderTable` call and as filters to the initial `_fetchData()`. Updated via `onFilterChange`. Cleared to `{}` by "Clear All Filters".

Single table → `_loadTableData()` (full width). Multiple tables (max 4) → `_renderPartitioned()` → `_loadTableDataInto()` per partition.

## Rules

- Before doing any work, mention how you could verify that work.
- Before implementing any new feature, analyze and explicitly state its performance impact: endpoints affected, estimated extra DB queries per request, risk of slowdown. Warn if non-trivial before writing code.
- All data storage must use **SQLite** — no JSON files for data.
- Test/debug Excel files go in `test-excel/`.
- Before implementing any feature involving SAP table structure (columns, key fields, relationships), always query DD03L first to inspect actual metadata. Key fields (`KEYFLAG='X'`) must always be included when creating or joining tables — never assume from SAP knowledge alone.
