"""
Flask backend for Harness — Sapcons.
Serves the SPA and provides a REST API backed by SQLite.

Run: python server.py
"""

import os
import re
import secrets
import hashlib
import sqlite3
from functools import wraps
from io import BytesIO
from flask import Flask, request, jsonify, send_from_directory
import openpyxl

app = Flask(__name__, static_folder='.', static_url_path='')
DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'harness.db')

_UPLOAD_RE = re.compile(r'^([A-Za-z0-9]+)_([A-Za-z0-9]+)_([A-Za-z0-9]+)_(\d+)\.xlsx$', re.IGNORECASE)
_SYSTEM_TABLES = ('users', 'sessions', '_table_meta')

# ── Table type classification ──────────────────────────────────────────────

def _determine_table_type(name, headers, data_rows):
    """Determines and returns the table type: 'master', 'configuration', or 'customizing'.

    This must be called before any validation so the correct ruleset is applied.

    Classification rules:
      - DD03L                  → master
      - Any other DD* table    → configuration
      - Everything else        → customizing
    """
    upper = name.upper()
    if upper == 'DD03L':
        return 'master'
    if upper.startswith('DD'):
        return 'configuration'
    return 'customizing'


# ── Validations ────────────────────────────────────────────────────────────

def _validate_general(filename):
    """Filename format check — applies to all uploads."""
    if not _UPLOAD_RE.match(filename):
        return f'Invalid filename "{filename}". Expected: {{TABLENAME}}_{{SYSTEM}}_{{CLIENT}}_{{DATE}}.xlsx'
    return None


def _validate_master(table_name, headers, data_rows, table_type):
    """Validations for master tables (e.g. DD03L)."""
    t = f'[{table_type}]'

    # V1: TABNAME and FIELDNAME columns must exist
    missing_cols = [c for c in ('TABNAME', 'FIELDNAME') if c not in headers]
    if missing_cols:
        return f'[V1]{t} {table_name}: missing required columns: {", ".join(missing_cols)}'

    tabname_idx = headers.index('TABNAME')

    # V2: if DD03L appears in TABNAME, no other TABNAME values are allowed
    all_tabnames = {
        str(r[tabname_idx]).strip().upper()
        for r in data_rows
        if r[tabname_idx] is not None and str(r[tabname_idx]).strip() != ''
    }
    if 'DD03L' in all_tabnames and len(all_tabnames) > 1:
        others = sorted(all_tabnames - {'DD03L'})
        sample = ', '.join(others[:5]) + (' …' if len(others) > 5 else '')
        return (
            f'[V2]{t} {table_name}: DD03L cannot be mixed with other table names in the TABNAME column. '
            f'Please upload a file that contains only DD03L entries. Found other values: {sample}'
        )

    # V3: only if all TABNAME values equal DD03L — Excel columns must match FIELDNAME values
    if all_tabnames == {'DD03L'}:
        fieldname_idx = headers.index('FIELDNAME')
        rollname_idx  = headers.index('ROLLNAME') if 'ROLLNAME' in headers else None
        fieldname_vals = {
            str(r[fieldname_idx]).strip()
            for r in data_rows
            if r[fieldname_idx] is not None
            and (rollname_idx is None or (r[rollname_idx] is not None and str(r[rollname_idx]).strip() != ''))
        }
        headers_set = set(headers)
        if headers_set != fieldname_vals:
            extra   = sorted(headers_set - fieldname_vals)
            missing = sorted(fieldname_vals - headers_set)
            parts = []
            if extra:   parts.append(f'extra columns: {", ".join(extra)}')
            if missing: parts.append(f'missing columns: {", ".join(missing)}')
            return f'[V3]{t} {table_name}: the uploaded file columns do not match the expected field definitions. {"; ".join(parts)}'

    # V4: only if TABNAME values are not DD03L — Excel columns must match DD03L field definitions
    if 'DD03L' not in all_tabnames:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT FIELDNAME FROM DD03L WHERE TABNAME = ? AND ROLLNAME IS NOT NULL AND ROLLNAME != ''",
                (table_name.upper(),)
            ).fetchall()
            fieldname_vals = {r[0].strip() for r in rows if r[0] is not None}
            headers_set = set(headers)
            if headers_set != fieldname_vals:
                extra   = sorted(headers_set - fieldname_vals)
                missing = sorted(fieldname_vals - headers_set)
                parts = []
                if extra:   parts.append(f'extra columns: {", ".join(extra)}')
                if missing: parts.append(f'missing columns: {", ".join(missing)}')
                return f'[V4]{t} {table_name}: columns do not match DD03L field definitions. {"; ".join(parts)}'

    return None


def _validate_configuration(table_name, headers, data_rows, table_type):
    """Validations for configuration tables (DD*)."""
    t = f'[{table_type}]'
    with get_db() as conn:
        dd03l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='DD03L'"
        ).fetchone()
        dd03l_count = conn.execute("SELECT COUNT(*) FROM DD03L").fetchone()[0] if dd03l_exists else 0
        if not dd03l_exists or dd03l_count == 0:
            return f'[V5]{t} DD03L master table is not loaded. Upload DD03L before uploading configuration tables.'

        master_count = conn.execute(
            "SELECT COUNT(*) FROM DD03L WHERE TABNAME = 'DD03L'"
        ).fetchone()[0]
        if master_count < 30:
            return (
                f'[V6]{t} DD03L master data is incomplete ({master_count} rows where TABNAME="DD03L", '
                f'need at least 30). Upload a complete DD03L file first.'
            )

        # V7: query FIELDNAME entries for this table (ROLLNAME not empty)
        rows = conn.execute(
            "SELECT FIELDNAME FROM DD03L WHERE TABNAME = ? AND ROLLNAME IS NOT NULL AND ROLLNAME != ''",
            (table_name.upper(),)
        ).fetchall()
        fieldname_vals = {r[0].strip() for r in rows if r[0] is not None}

        if not fieldname_vals:
            return f'[V7]{t} no entries in master table (DD03L) for {table_name}.'

        headers_set = set(headers)
        if headers_set != fieldname_vals:
            extra   = sorted(headers_set - fieldname_vals)
            missing = sorted(fieldname_vals - headers_set)
            parts = []
            if extra:   parts.append(f'extra columns: {", ".join(extra)}')
            if missing: parts.append(f'missing columns: {", ".join(missing)}')
            return f'[V8]{t} First upload DD03L file with DD03L entries.'

    return None


def _validate_customizing(table_name, headers, data_rows, table_type):
    """Validations for customizing tables — V4, V5, V7 (no V6)."""
    t = f'[{table_type}]'
    with get_db() as conn:
        dd03l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='DD03L'"
        ).fetchone()
        dd03l_count = conn.execute("SELECT COUNT(*) FROM DD03L").fetchone()[0] if dd03l_exists else 0
        if not dd03l_exists or dd03l_count == 0:
            return f'[V5]{t} DD03L master table is not loaded. Upload DD03L before uploading customizing tables.'

        master_count = conn.execute(
            "SELECT COUNT(*) FROM DD03L WHERE TABNAME = 'DD03L'"
        ).fetchone()[0]
        if master_count < 30:
            return (
                f'[V6]{t} DD03L master data is incomplete ({master_count} rows where TABNAME="DD03L", '
                f'need at least 30). Upload a complete DD03L file first.'
            )

        rows = conn.execute(
            "SELECT FIELDNAME FROM DD03L WHERE TABNAME = ? AND ROLLNAME IS NOT NULL AND ROLLNAME != ''",
            (table_name.upper(),)
        ).fetchall()
        fieldname_vals = {r[0].strip() for r in rows if r[0] is not None}

        headers_set = set(headers)
        if headers_set != fieldname_vals:
            extra   = sorted(headers_set - fieldname_vals)
            missing = sorted(fieldname_vals - headers_set)
            parts = []
            if extra:   parts.append(f'extra columns: {", ".join(extra)}')
            if missing: parts.append(f'missing columns: {", ".join(missing)}')
            return f'[V8]{t} First upload DD03L file with DD03L entries.'

    return None


# ── Database helpers ───────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS _table_meta (
                table_name TEXT PRIMARY KEY,
                system     TEXT NOT NULL,
                client     TEXT NOT NULL,
                date       TEXT NOT NULL
            );
        ''')
        # Create a default admin user if no users exist
        if not conn.execute('SELECT 1 FROM users').fetchone():
            conn.execute(
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                ('admin', _hash('admin'))
            )


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ── Auth helpers ───────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        with get_db() as conn:
            session = conn.execute(
                'SELECT user_id FROM sessions WHERE token = ?', (token,)
            ).fetchone()
        if not session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper


# ── Auth routes ────────────────────────────────────────────────────────────

@app.post('/api/auth/login')
def login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400

    with get_db() as conn:
        user = conn.execute(
            'SELECT * FROM users WHERE username = ? AND password_hash = ?',
            (username, _hash(password))
        ).fetchone()
        if not user:
            return jsonify({'error': 'Invalid credentials'}), 401

        token = secrets.token_hex(32)
        conn.execute('INSERT INTO sessions (token, user_id) VALUES (?, ?)', (token, user['id']))

    return jsonify({'token': token, 'user': {'id': user['id'], 'username': user['username']}})


@app.post('/api/auth/logout')
@require_auth
def logout():
    token = request.headers.get('Authorization', '').removeprefix('Bearer ')
    with get_db() as conn:
        conn.execute('DELETE FROM sessions WHERE token = ?', (token,))
    return jsonify({'ok': True})


# ── Table routes ───────────────────────────────────────────────────────────

@app.get('/api/tables')
@require_auth
def list_tables():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT IN ('users','sessions','_table_meta') ORDER BY name"
        ).fetchall()
    return jsonify([r['name'] for r in rows])


@app.get('/api/tables/info')
@require_auth
def list_tables_info():
    with get_db() as conn:
        meta = conn.execute(
            'SELECT table_name, system, client, date FROM _table_meta ORDER BY table_name'
        ).fetchall()
        result = []
        for r in meta:
            count = conn.execute(f'SELECT COUNT(*) FROM "{r["table_name"]}"').fetchone()[0]
            result.append({
                'table': r['table_name'],
                'system': r['system'],
                'client': r['client'],
                'date': r['date'],
                'count': count,
            })
    return jsonify(result)


@app.get('/api/tables/<table>/data')
@require_auth
def get_table_data(table):
    with get_db() as conn:
        # Validate table exists and is not a system table
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?"
            " AND name NOT IN ('users','sessions','_table_meta')", (table,)
        ).fetchone()
        if not exists:
            return jsonify({'error': 'Table not found'}), 404

        cur = conn.execute(f'SELECT * FROM "{table}" LIMIT 5000')
        columns = [d[0] for d in cur.description]
        rows = [dict(r) for r in cur.fetchall()]

    return jsonify({'columns': columns, 'rows': rows})


@app.delete('/api/tables/<table>')
@require_auth
def drop_table(table):
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?"
            " AND name NOT IN ('users','sessions','_table_meta')", (table,)
        ).fetchone()
        if not exists:
            return jsonify({'error': 'Table not found'}), 404
        conn.execute(f'DELETE FROM "{table}"')
        conn.execute('DELETE FROM _table_meta WHERE table_name = ?', (table,))
    return jsonify({'ok': True})


# ── Upload route ───────────────────────────────────────────────────────────

@app.post('/api/upload')
@require_auth
def upload_excel():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    filename = f.filename or ''

    m = _UPLOAD_RE.match(filename)
    if not m:
        return jsonify({
            'error': f'Invalid filename "{filename}". Expected: {{TABLENAME}}_{{SYSTEM}}_{{CLIENT}}_{{DATE}}.xlsx'
        }), 422

    table_name, system, client, date = m.group(1), m.group(2), m.group(3), m.group(4)

    if table_name.lower() in _SYSTEM_TABLES:
        return jsonify({'error': f'Table "{table_name}" is protected'}), 403

    try:
        wb = openpyxl.load_workbook(BytesIO(f.read()), read_only=True, data_only=True)
    except Exception as e:
        return jsonify({'error': f'Cannot read Excel file: {e}'}), 422

    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)

    try:
        header_row = next(rows_iter)
    except StopIteration:
        return jsonify({'error': 'Excel file is empty'}), 422

    headers = [str(h).strip() if h is not None else f'col_{i}' for i, h in enumerate(header_row)]
    if not headers:
        return jsonify({'error': 'Excel file has no columns'}), 422

    data_rows = list(rows_iter)

    # ── Step 1: Determine table type ──────────────────────────────────────────
    table_type = _determine_table_type(table_name, headers, data_rows)

    # ── Step 2: Run type-specific validations ─────────────────────────────────
    if table_type == 'master':
        err = _validate_master(table_name, headers, data_rows, table_type)
    elif table_type == 'configuration':
        err = _validate_configuration(table_name, headers, data_rows, table_type)
    else:
        err = _validate_customizing(table_name, headers, data_rows, table_type)
    if err:
        return jsonify({'error': err}), 422

    # ── Step 3: Determine key fields ──────────────────────────────────────────
    key_fields = set()
    if table_type == 'master':
        keyflag_idx   = headers.index('KEYFLAG')   if 'KEYFLAG'   in headers else None
        fieldname_idx = headers.index('FIELDNAME') if 'FIELDNAME' in headers else None
        if keyflag_idx is not None and fieldname_idx is not None:
            for row in data_rows:
                if (len(row) > keyflag_idx and str(row[keyflag_idx] or '').strip().upper() == 'X'
                        and len(row) > fieldname_idx and row[fieldname_idx] is not None):
                    key_fields.add(str(row[fieldname_idx]).strip())
    else:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT FIELDNAME FROM DD03L WHERE TABNAME = ? AND KEYFLAG = 'X'",
                (table_name.upper(),)
            ).fetchall()
            key_fields = {r[0].strip() for r in rows if r[0] is not None}

    # ── Create table if needed, then insert ───────────────────────────────────
    def _col_def(h):
        return f'"{h}" TEXT PRIMARY KEY' if len(key_fields) == 1 and h in key_fields else f'"{h}" TEXT'

    if len(key_fields) > 1:
        col_defs = ', '.join(f'"{h}" TEXT' for h in headers)
        pk_cols  = ', '.join(f'"{k}"' for k in headers if k in key_fields)
        if pk_cols:
            col_defs += f', PRIMARY KEY ({pk_cols})'
    else:
        col_defs = ', '.join(_col_def(h) for h in headers)

    placeholders = ', '.join('?' for _ in headers)

    with get_db() as conn:
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')
        conn.execute(
            'INSERT OR REPLACE INTO _table_meta (table_name, system, client, date) VALUES (?, ?, ?, ?)',
            (table_name, system, client, date)
        )
        rows_inserted = 0
        for row in data_rows:
            values = [
                str(row[i]) if i < len(row) and row[i] is not None else None
                for i in range(len(headers))
            ]
            conn.execute(f'INSERT OR REPLACE INTO "{table_name}" VALUES ({placeholders})', values)
            rows_inserted += 1

    return jsonify({'ok': True, 'table': table_name, 'table_type': table_type, 'rows_inserted': rows_inserted})


# ── User management routes ─────────────────────────────────────────────────

@app.get('/api/users')
@require_auth
def list_users():
    with get_db() as conn:
        users = conn.execute('SELECT id, username FROM users ORDER BY username').fetchall()
    return jsonify([dict(u) for u in users])


@app.post('/api/users')
@require_auth
def create_user():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400

    with get_db() as conn:
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                (username, _hash(password))
            )
        except sqlite3.IntegrityError:
            return jsonify({'error': f'Username "{username}" already exists'}), 409

    return jsonify({'ok': True}), 201


# ── SPA fallback ───────────────────────────────────────────────────────────

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_spa(path):
    # Serve static assets (js/, css/) directly; everything else → index.html
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')


# ── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    print('Default credentials: admin / admin')
    app.run(debug=True, port=5000)
