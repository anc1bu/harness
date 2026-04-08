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
from contextlib import contextmanager
from functools import wraps
from io import BytesIO
from flask import Flask, request, jsonify, send_from_directory
import openpyxl

app = Flask(__name__, static_folder='.', static_url_path='')
DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'harness.db')

_UPLOAD_RE   = re.compile(r'^([A-Za-z0-9]+)_([A-Za-z0-9]+)_([A-Za-z0-9]+)_(\d+)\.xlsx$', re.IGNORECASE)
_CUSTNAME_RE = re.compile(r'^[A-Za-z0-9]{3}$')
_SYSTEM_TABLES = ('users', 'sessions', '_table_meta', 'customers', 'user_customers')

# ── Table type classification ──────────────────────────────────────────────

def _determine_table_type(name):
    upper = name.upper()
    if upper == 'DD03L':
        return 'master'
    if upper.startswith('DD'):
        return 'configuration'
    return 'customizing'


# ── Validations ────────────────────────────────────────────────────────────

def _validate_headers_technical(headers, table_name, table_type):
    """V1 — all headers must be technical names (no spaces). Applies to all table types."""
    t = f'[{table_type}]'
    descriptive = [h for h in headers if ' ' in h]
    if descriptive:
        sample = ', '.join(f'"{h}"' for h in descriptive[:5])
        return (
            f'[V1]{t} {table_name}: column headers must be technical field names, not descriptions. '
            f'Descriptive headers found: {sample}'
        )
    return None


def _validate_master(table_name, headers, data_rows, table_type, dd03l_db_name):
    """Validations for master tables (e.g. DD03L)."""
    t = f'[{table_type}]'

    # V2: TABNAME and FIELDNAME columns must exist
    missing_cols = [c for c in ('TABNAME', 'FIELDNAME') if c not in headers]
    if missing_cols:
        return f'[V2]{t} {table_name}: missing required columns: {", ".join(missing_cols)}'

    tabname_idx = headers.index('TABNAME')

    # V3: if DD03L appears in TABNAME, no other TABNAME values are allowed
    all_tabnames = {
        str(r[tabname_idx]).strip().upper()
        for r in data_rows
        if r[tabname_idx] is not None and str(r[tabname_idx]).strip() != ''
    }
    if 'DD03L' in all_tabnames and len(all_tabnames) > 1:
        others = sorted(all_tabnames - {'DD03L'})
        sample = ', '.join(others[:5]) + (' …' if len(others) > 5 else '')
        return (
            f'[V3]{t} {table_name}: DD03L cannot be mixed with other table names in the TABNAME column. '
            f'Please upload a file that contains only DD03L entries. Found other values: {sample}'
        )

    # V4: only if all TABNAME values equal DD03L — Excel columns must match FIELDNAME values
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
            return f'[V4]{t} {table_name}: the uploaded file columns do not match the expected field definitions. {"; ".join(parts)}'

    # V5: only if TABNAME values are not DD03L — Excel columns must match DD03L field definitions
    if 'DD03L' not in all_tabnames:
        with get_db() as conn:
            rows = conn.execute(
                f'SELECT FIELDNAME FROM "{dd03l_db_name}" WHERE TABNAME = ? AND ROLLNAME IS NOT NULL AND ROLLNAME != \'\'',
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
                return f'[V5]{t} {table_name}: columns do not match DD03L field definitions. {"; ".join(parts)}'

    return None


def _validate_dd_headers(table_name, headers, table_type, dd03l_db_name, check_v8=True):
    """Shared DD03L prerequisite + header match check (V6, V7, V8, V9)."""
    t = f'[{table_type}]'
    with get_db() as conn:
        dd03l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (dd03l_db_name,)
        ).fetchone()
        dd03l_count = conn.execute(f'SELECT COUNT(*) FROM "{dd03l_db_name}"').fetchone()[0] if dd03l_exists else 0
        if not dd03l_exists or dd03l_count == 0:
            return f'[V6]{t} DD03L master table is not loaded. Upload DD03L before uploading {table_type} tables.'

        master_count = conn.execute(
            f'SELECT COUNT(*) FROM "{dd03l_db_name}" WHERE TABNAME = \'DD03L\''
        ).fetchone()[0]
        if master_count < 30:
            return (
                f'[V7]{t} DD03L master data is incomplete ({master_count} rows where TABNAME="DD03L", '
                f'need at least 30). Upload a complete DD03L file first.'
            )

        if check_v8:
            exists_in_dd03l = conn.execute(
                f'SELECT 1 FROM "{dd03l_db_name}" WHERE TABNAME = ?', (table_name.upper(),)
            ).fetchone()
            if not exists_in_dd03l:
                return f'[V8]{t} no entries in master table (DD03L) for {table_name}.'

        rows = conn.execute(
            f'SELECT FIELDNAME FROM "{dd03l_db_name}" WHERE TABNAME = ? AND ROLLNAME IS NOT NULL AND ROLLNAME != \'\'',
            (table_name.upper(),)
        ).fetchall()
        fieldname_vals = {r[0].strip() for r in rows if r[0] is not None}

        headers_set = set(headers) - {'MANDT'}
        fieldname_vals -= {'MANDT'}
        extra   = sorted(headers_set - fieldname_vals)
        missing = sorted(fieldname_vals - headers_set)
        if extra or missing:
            parts = []
            if extra:   parts.append(f'extra in Excel: {", ".join(extra[:5])}{"  …" if len(extra) > 5 else ""}')
            if missing: parts.append(f'missing from Excel: {", ".join(missing[:5])}{"  …" if len(missing) > 5 else ""}')
            return f'[V9]{t} {table_name}: Excel headers do not match DD03L field definitions. {"; ".join(parts)}'

    return None


def _validate_configuration(table_name, headers, data_rows, table_type, dd03l_db_name):
    return _validate_dd_headers(table_name, headers, table_type, dd03l_db_name, check_v8=True)


# ── Database helpers ───────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            custname   TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS _table_meta (
            table_name TEXT PRIMARY KEY,
            custname   TEXT,
            orig_table TEXT,
            system     TEXT NOT NULL,
            client     TEXT NOT NULL,
            date       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customers (
            custname TEXT PRIMARY KEY,
            name     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_customers (
            user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            custname TEXT NOT NULL REFERENCES customers(custname) ON DELETE CASCADE,
            PRIMARY KEY (user_id, custname)
        );
    ''')

    # Schema migrations for existing DBs (idempotent)
    for sql in [
        'ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE sessions ADD COLUMN custname TEXT',
        'ALTER TABLE _table_meta ADD COLUMN custname TEXT',
        'ALTER TABLE _table_meta ADD COLUMN orig_table TEXT',
    ]:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Ensure admin user has is_admin=1
    conn.execute("UPDATE users SET is_admin = 1 WHERE username = 'admin'")

    # Create default admin user if no users exist
    if not conn.execute('SELECT 1 FROM users').fetchone():
        conn.execute(
            'INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)',
            ('admin', _hash('admin'))
        )

    conn.commit()
    conn.close()


def _hash(password: str) -> str:
    """Hash a password with PBKDF2-SHA256 + random salt."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 260000)
    return f'pbkdf2:260000:{salt}:{h.hex()}'


def _verify_password(password: str, stored: str) -> bool:
    """Verify password. Supports legacy plain SHA-256 (no colon) and PBKDF2."""
    if ':' not in stored:
        return stored == hashlib.sha256(password.encode()).hexdigest()
    try:
        _, iterations, salt, hash_hex = stored.split(':')
        h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations))
        return h.hex() == hash_hex
    except Exception:
        return False


def _get_token():
    return request.headers.get('Authorization', '').removeprefix('Bearer ')


# ── Auth helpers ───────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        with get_db() as conn:
            session = conn.execute(
                'SELECT user_id FROM sessions WHERE token = ?', (_get_token(),)
            ).fetchone()
        if not session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        with get_db() as conn:
            row = conn.execute(
                'SELECT u.is_admin FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?',
                (_get_token(),)
            ).fetchone()
        if not row or not row['is_admin']:
            return jsonify({'error': 'Admin access required'}), 403
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
            'SELECT * FROM users WHERE username = ?', (username,)
        ).fetchone()
        if not user or not _verify_password(password, user['password_hash']):
            return jsonify({'error': 'Invalid credentials'}), 400

        # Upgrade legacy SHA-256 hash to PBKDF2 on successful login
        if ':' not in user['password_hash']:
            conn.execute(
                'UPDATE users SET password_hash = ? WHERE id = ?',
                (_hash(password), user['id'])
            )

        token = secrets.token_hex(32)
        conn.execute('INSERT INTO sessions (token, user_id) VALUES (?, ?)', (token, user['id']))

        customers = conn.execute(
            'SELECT c.custname, c.name FROM customers c '
            'JOIN user_customers uc ON c.custname = uc.custname '
            'WHERE uc.user_id = ? ORDER BY c.custname',
            (user['id'],)
        ).fetchall()

    return jsonify({
        'token': token,
        'user': {'id': user['id'], 'username': user['username'], 'is_admin': bool(user['is_admin'])},
        'customers': [{'custname': r['custname'], 'name': r['name']} for r in customers],
    })


@app.post('/api/auth/select-customer')
@require_auth
def select_customer():
    data = request.json or {}
    custname = data.get('custname', '').strip().upper()
    token = _get_token()

    with get_db() as conn:
        session = conn.execute('SELECT user_id FROM sessions WHERE token = ?', (token,)).fetchone()
        assigned = conn.execute(
            'SELECT 1 FROM user_customers WHERE user_id = ? AND custname = ?',
            (session['user_id'], custname)
        ).fetchone()
        if not assigned:
            return jsonify({'error': 'Customer not assigned to this user'}), 403
        conn.execute('UPDATE sessions SET custname = ? WHERE token = ?', (custname, token))

    return jsonify({'ok': True})


@app.post('/api/auth/logout')
@require_auth
def logout():
    with get_db() as conn:
        conn.execute('DELETE FROM sessions WHERE token = ?', (_get_token(),))
    return jsonify({'ok': True})


# ── Table routes ───────────────────────────────────────────────────────────

def _session_custname():
    with get_db() as conn:
        session = conn.execute(
            'SELECT custname FROM sessions WHERE token = ?', (_get_token(),)
        ).fetchone()
    return session['custname'] if session else None


@app.get('/api/tables')
@require_auth
def list_tables():
    custname = _session_custname()
    if not custname:
        return jsonify([])
    with get_db() as conn:
        rows = conn.execute(
            "SELECT table_name FROM _table_meta WHERE custname = ? ORDER BY orig_table",
            (custname,)
        ).fetchall()
    return jsonify([r['table_name'] for r in rows])


@app.get('/api/tables/info')
@require_auth
def list_tables_info():
    custname = _session_custname()
    if not custname:
        return jsonify([])
    with get_db() as conn:
        meta = conn.execute(
            'SELECT table_name, orig_table, system, client, date FROM _table_meta '
            'WHERE custname = ? ORDER BY orig_table',
            (custname,)
        ).fetchall()
        result = []
        for r in meta:
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{r["table_name"]}"').fetchone()[0]
            except Exception:
                count = 0
            result.append({
                'table':      r['table_name'],
                'orig_table': r['orig_table'] or r['table_name'],
                'system':     r['system'],
                'client':     r['client'],
                'date':       r['date'],
                'count':      count,
            })
    return jsonify(result)


@app.delete('/api/tables/<table>')
@require_auth
def drop_table(table):
    custname = _session_custname()
    with get_db() as conn:
        meta = conn.execute(
            "SELECT 1 FROM _table_meta WHERE table_name = ? AND custname = ?",
            (table, custname)
        ).fetchone()
        if not meta:
            return jsonify({'error': 'Table not found'}), 404
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute('DELETE FROM _table_meta WHERE table_name = ?', (table,))
    return jsonify({'ok': True})


# ── Upload route ───────────────────────────────────────────────────────────

@app.post('/api/upload')
@require_auth
def upload_excel():
    custname = _session_custname()
    if not custname:
        return jsonify({'error': 'No customer selected. Please log in again.'}), 422

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

    db_table_name = f'{custname}_{system}_{table_name}'
    dd03l_db_name = f'{custname}_{system}_DD03L'

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
    table_type = _determine_table_type(table_name)

    # ── Step 2: V1 — technical headers check (all table types) ───────────────
    err = _validate_headers_technical(headers, table_name, table_type)
    if err:
        return jsonify({'error': err}), 422

    # ── Step 3: Run type-specific validations ─────────────────────────────────
    if table_type == 'master':
        err = _validate_master(table_name, headers, data_rows, table_type, dd03l_db_name)
    else:
        err = _validate_configuration(table_name, headers, data_rows, table_type, dd03l_db_name)
    if err:
        return jsonify({'error': err}), 422

    # ── Step 4: Determine key fields ──────────────────────────────────────────
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
                f'SELECT FIELDNAME FROM "{dd03l_db_name}" WHERE TABNAME = ? AND KEYFLAG = \'X\'',
                (table_name.upper(),)
            ).fetchall()
            key_fields = {r[0].strip() for r in rows if r[0] is not None}

    # ── Step 5: Create table if needed, then insert ───────────────────────────
    def _col_def(h):
        return f'"{h}" TEXT PRIMARY KEY' if len(key_fields) == 1 and h in key_fields else f'"{h}" TEXT'

    if len(key_fields) > 1:
        col_defs = ', '.join(f'"{h}" TEXT' for h in headers)
        pk_cols  = ', '.join(f'"{k}"' for k in headers if k in key_fields)
        if pk_cols:
            col_defs += f', PRIMARY KEY ({pk_cols})'
    else:
        col_defs = ', '.join(_col_def(h) for h in headers)

    col_names    = ', '.join(f'"{h}"' for h in headers)
    placeholders = ', '.join('?' for _ in headers)

    with get_db() as conn:
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{db_table_name}" ({col_defs})')
        conn.execute(
            'INSERT OR REPLACE INTO _table_meta (table_name, custname, orig_table, system, client, date) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (db_table_name, custname, table_name, system, client, date)
        )
        all_values = [
            [str(row[i]) if i < len(row) and row[i] is not None else None for i in range(len(headers))]
            for row in data_rows
        ]
        conn.executemany(
            f'INSERT OR REPLACE INTO "{db_table_name}" ({col_names}) VALUES ({placeholders})',
            all_values
        )
        rows_inserted = len(all_values)

        # Sort DD03L by TABNAME, POSITION after every insert
        if table_name.upper() == 'DD03L' and 'TABNAME' in headers and 'POSITION' in headers:
            tmp = db_table_name + '__sorted_tmp'
            conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
            conn.execute(
                f'CREATE TABLE "{tmp}" AS '
                f'SELECT * FROM "{db_table_name}" '
                f'ORDER BY TABNAME ASC, CAST(POSITION AS INTEGER) ASC'
            )
            conn.execute(f'DROP TABLE "{db_table_name}"')
            conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{db_table_name}"')

    return jsonify({
        'ok': True,
        'table': db_table_name,
        'orig_table': table_name,
        'table_type': table_type,
        'rows_inserted': rows_inserted,
    })


# ── Table data route ───────────────────────────────────────────────────────

@app.get('/api/tables/<table>/data')
@require_auth
def get_table_data(table):
    custname = _session_custname()
    with get_db() as conn:
        meta = conn.execute(
            'SELECT orig_table, system FROM _table_meta WHERE table_name = ? AND custname = ?',
            (table, custname)
        ).fetchone()
        if not meta:
            return jsonify({'error': 'Table not found'}), 404

        orig_table = meta['orig_table'] or table
        system     = meta['system']
        dd03l_name = f'{custname}_{system}_DD03L'
        dd04t_name = f'{custname}_{system}_DD04T'

        # Fetch raw rows
        try:
            raw_rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        if not raw_rows:
            return jsonify({'columns': [], 'rows': [], 'dd04t_missing': False, 'partial_descriptions': False})

        raw_cols = list(raw_rows[0].keys())

        # Check DD04T existence and records
        dd04t_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd04t_name,)
        ).fetchone()
        dd04t_count = conn.execute(f'SELECT COUNT(*) FROM "{dd04t_name}"').fetchone()[0] if dd04t_exists else 0
        dd04t_missing = not dd04t_exists or dd04t_count == 0

        # Build enriched headers
        enriched_cols = []
        missing_fields = []
        partial_descriptions = False

        dd03l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd03l_name,)
        ).fetchone()

        for col in raw_cols:
            if dd04t_missing:
                enriched_cols.append(col)
                continue

            # Look up ROLLNAME from DD03L
            rollname_row = None
            if dd03l_exists:
                rollname_row = conn.execute(
                    f'SELECT ROLLNAME FROM "{dd03l_name}" WHERE TABNAME = ? AND FIELDNAME = ?',
                    (orig_table.upper(), col)
                ).fetchone()

            if not rollname_row or not rollname_row['ROLLNAME']:
                enriched_cols.append(col)
                missing_fields.append(col)
                partial_descriptions = True
                continue

            rollname = rollname_row['ROLLNAME'].strip()

            # Look up SCRTEXT_M from DD04T
            desc_row = conn.execute(
                f'SELECT SCRTEXT_M FROM "{dd04t_name}" WHERE ROLLNAME = ? AND DDLANGUAGE = ?',
                (rollname, 'EN')
            ).fetchone()

            if not desc_row or not desc_row['SCRTEXT_M']:
                enriched_cols.append(col)
                missing_fields.append(col)
                partial_descriptions = True
            else:
                enriched_cols.append(f'{col} - {desc_row["SCRTEXT_M"].strip()}')

        rows_out = [dict(zip(enriched_cols, [row[c] for c in raw_cols])) for row in raw_rows]

    return jsonify({
        'columns': enriched_cols,
        'rows': rows_out,
        'dd04t_missing': dd04t_missing,
        'partial_descriptions': partial_descriptions,
        'missing_fields': missing_fields,
    })


# ── Customer routes ────────────────────────────────────────────────────────

@app.get('/api/customers')
@require_auth
def list_customers():
    with get_db() as conn:
        rows = conn.execute('SELECT custname, name FROM customers ORDER BY custname').fetchall()
    return jsonify([dict(r) for r in rows])


@app.post('/api/customers')
@require_auth
@require_admin
def create_customer():
    data = request.json or {}
    custname = data.get('custname', '').strip().upper()
    name = data.get('name', '').strip()

    if not _CUSTNAME_RE.match(custname):
        return jsonify({'error': 'Customer code must be exactly 3 alphanumeric characters'}), 400
    if not name:
        return jsonify({'error': 'Customer name is required'}), 400

    with get_db() as conn:
        try:
            conn.execute('INSERT INTO customers (custname, name) VALUES (?, ?)', (custname, name))
        except sqlite3.IntegrityError:
            return jsonify({'error': f'Customer "{custname}" already exists'}), 409

    return jsonify({'ok': True}), 201


@app.delete('/api/customers/<custname>')
@require_auth
@require_admin
def delete_customer(custname):
    with get_db() as conn:
        exists = conn.execute(
            'SELECT 1 FROM customers WHERE custname = ?', (custname.upper(),)
        ).fetchone()
        if not exists:
            return jsonify({'error': 'Customer not found'}), 404
        conn.execute('DELETE FROM customers WHERE custname = ?', (custname.upper(),))
    return jsonify({'ok': True})


# ── User management routes ─────────────────────────────────────────────────

@app.get('/api/users')
@require_auth
def list_users():
    with get_db() as conn:
        users = conn.execute(
            'SELECT id, username, is_admin FROM users ORDER BY username'
        ).fetchall()
    return jsonify([dict(u) for u in users])


@app.post('/api/users')
@require_auth
@require_admin
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


@app.patch('/api/users/<int:user_id>')
@require_auth
@require_admin
def update_user(user_id):
    data = request.json or {}
    with get_db() as conn:
        user = conn.execute('SELECT username FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user:
            return jsonify({'error': 'User not found'}), 404
        if 'is_admin' in data:
            if user['username'] == 'admin' and not data['is_admin']:
                return jsonify({'error': 'Cannot remove admin role from "admin" user'}), 403
            conn.execute(
                'UPDATE users SET is_admin = ? WHERE id = ?',
                (1 if data['is_admin'] else 0, user_id)
            )
    return jsonify({'ok': True})


@app.get('/api/users/<int:user_id>/customers')
@require_auth
@require_admin
def get_user_customers(user_id):
    with get_db() as conn:
        rows = conn.execute(
            'SELECT c.custname, c.name FROM customers c '
            'JOIN user_customers uc ON c.custname = uc.custname '
            'WHERE uc.user_id = ? ORDER BY c.custname',
            (user_id,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post('/api/users/<int:user_id>/customers')
@require_auth
@require_admin
def assign_customer_to_user(user_id):
    data = request.json or {}
    custname = data.get('custname', '').strip().upper()
    with get_db() as conn:
        if not conn.execute('SELECT 1 FROM users WHERE id = ?', (user_id,)).fetchone():
            return jsonify({'error': 'User not found'}), 404
        if not conn.execute('SELECT 1 FROM customers WHERE custname = ?', (custname,)).fetchone():
            return jsonify({'error': 'Customer not found'}), 404
        try:
            conn.execute(
                'INSERT INTO user_customers (user_id, custname) VALUES (?, ?)', (user_id, custname)
            )
        except sqlite3.IntegrityError:
            pass  # Already assigned
    return jsonify({'ok': True})


@app.delete('/api/users/<int:user_id>/customers/<custname>')
@require_auth
@require_admin
def unassign_customer_from_user(user_id, custname):
    with get_db() as conn:
        conn.execute(
            'DELETE FROM user_customers WHERE user_id = ? AND custname = ?',
            (user_id, custname.upper())
        )
    return jsonify({'ok': True})


# ── SPA fallback ───────────────────────────────────────────────────────────

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_spa(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')


# ── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
