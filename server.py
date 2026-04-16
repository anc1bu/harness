"""
Flask backend for Harness — Sapcons.
Serves the SPA and provides a REST API backed by SQLite.

Run: python server.py
"""

import os
import re
import secrets
import hashlib
import hmac
import sqlite3
import threading
import zipfile
from contextlib import contextmanager
from functools import wraps
from io import BytesIO
from xml.etree.ElementTree import iterparse
from flask import Flask, request, jsonify, send_from_directory
import openpyxl
from collections import namedtuple

# Structured validation result: code ('V1'…'V9','V-Show-2'), human error string, field list
_ValResult = namedtuple('_ValResult', ['code', 'error', 'fields'])
# fields: list of {'name': str, 'note': str|None}  (note = 'extra'|'missing' for column checks)

_EXCEPTION_VALIDATIONS = frozenset({'V4', 'V5', 'V9', 'V-Show-2'})

app = Flask(__name__, static_folder='.', static_url_path='')

@app.after_request
def _no_store_static(response):
    """Prevent Cloudflare and browsers from caching JS/CSS — forces fresh fetch on every load."""
    if request.path.endswith(('.js', '.css')):
        response.headers['Cache-Control'] = 'no-store'
    return response
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
        return 'basis'
    return 'customizing'


# ── Validations ────────────────────────────────────────────────────────────
# Each function is a single validation step. ctx is a dict of shared inputs:
#   table_name, headers, data_rows, table_type, dd03l_db_name
# Returns _ValResult on failure, None to pass.

def _v1_technical_headers(ctx):
    """V1 — all headers must be technical names (no spaces). Applies to all table types."""
    table_name, headers, table_type = ctx['table_name'], ctx['headers'], ctx['table_type']
    t = f'[{table_type}]'
    descriptive = [h for h in headers if ' ' in h]
    if descriptive:
        sample = ', '.join(f'"{h}"' for h in descriptive[:5])
        return _ValResult(
            code='V1',
            error=(f'[V1]{t} {table_name}: column headers must be technical field names, not descriptions. '
                   f'Descriptive headers found: {sample}'),
            fields=[{'name': h, 'note': None} for h in descriptive],
        )
    return None


def _v2_required_cols(ctx):
    """V2 — TABNAME and FIELDNAME columns must exist (master only)."""
    table_name, headers, table_type = ctx['table_name'], ctx['headers'], ctx['table_type']
    t = f'[{table_type}]'
    missing_cols = [c for c in ('TABNAME', 'FIELDNAME') if c not in headers]
    if missing_cols:
        return _ValResult(
            code='V2',
            error=f'[V2]{t} {table_name}: missing required columns: {", ".join(missing_cols)}',
            fields=[{'name': c, 'note': None} for c in missing_cols],
        )
    return None


def _v3_no_mixed_tabnames(ctx):
    """V3 — DD03L cannot be mixed with other TABNAME values (master only)."""
    table_name, headers, data_rows, table_type = (
        ctx['table_name'], ctx['headers'], ctx['data_rows'], ctx['table_type']
    )
    t = f'[{table_type}]'
    tabname_idx = headers.index('TABNAME')
    all_tabnames = {
        str(r[tabname_idx]).strip().upper()
        for r in data_rows
        if r[tabname_idx] is not None and str(r[tabname_idx]).strip() != ''
    }
    ctx['all_tabnames'] = all_tabnames  # cache for V4/V5
    if 'DD03L' in all_tabnames and len(all_tabnames) > 1:
        others = sorted(all_tabnames - {'DD03L'})
        sample = ', '.join(others[:5]) + (' …' if len(others) > 5 else '')
        return _ValResult(
            code='V3',
            error=(f'[V3]{t} {table_name}: DD03L cannot be mixed with other table names in the TABNAME column. '
                   f'Please upload a file that contains only DD03L entries. Found other values: {sample}'),
            fields=[{'name': o, 'note': None} for o in others],
        )
    return None


def _v4_self_ref_columns(ctx):
    """V4 — self-referential DD03L: Excel columns must match FIELDNAME values (master only, all TABNAME='DD03L')."""
    table_name, headers, data_rows, table_type = (
        ctx['table_name'], ctx['headers'], ctx['data_rows'], ctx['table_type']
    )
    t = f'[{table_type}]'
    all_tabnames = ctx.get('all_tabnames', set())
    if all_tabnames != {'DD03L'}:
        return None
    fieldname_idx = headers.index('FIELDNAME')
    rollname_idx  = headers.index('ROLLNAME') if 'ROLLNAME' in headers else None
    fieldname_vals = {
        str(r[fieldname_idx]).strip()
        for r in data_rows
        if r[fieldname_idx] is not None
        and (rollname_idx is None or (r[rollname_idx] is not None and str(r[rollname_idx]).strip() != ''))
    }
    headers_set = set(headers)
    extra   = sorted(headers_set - fieldname_vals)
    missing = sorted(fieldname_vals - headers_set)
    if extra or missing:
        parts = []
        if extra:   parts.append(f'extra columns: {", ".join(extra)}')
        if missing: parts.append(f'missing columns: {", ".join(missing)}')
        return _ValResult(
            code='V4',
            error=f'[V4]{t} {table_name}: the uploaded file columns do not match the expected field definitions. {"; ".join(parts)}',
            fields=[{'name': f, 'note': 'extra'} for f in extra] + [{'name': f, 'note': 'missing'} for f in missing],
        )
    return None


def _v5_non_self_ref_columns(ctx):
    """V5 — non-self-referential DD03L: Excel columns must match existing DD03L DB definitions (master only, no TABNAME='DD03L')."""
    table_name, headers, table_type, dd03l_db_name = (
        ctx['table_name'], ctx['headers'], ctx['table_type'], ctx['dd03l_db_name']
    )
    t = f'[{table_type}]'
    all_tabnames = ctx.get('all_tabnames', set())
    if 'DD03L' in all_tabnames:
        return None
    with get_db() as conn:
        # Step 1: DD03L must exist in DB
        dd03l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (dd03l_db_name,)
        ).fetchone()
        if not dd03l_exists:
            return _ValResult(
                code='V5',
                error=f'[V5]{t} {table_name}: cannot validate columns — DD03L master table has not been uploaded yet.',
                fields=[],
            )
        # Step 2: DD03L must have at least 30 self-referential rows
        master_count = conn.execute(
            f'SELECT COUNT(*) FROM "{dd03l_db_name}" WHERE TABNAME = \'DD03L\''
        ).fetchone()[0]
        if master_count < 30:
            return _ValResult(
                code='V5',
                error=(f'[V5]{t} {table_name}: cannot validate columns — DD03L master data is incomplete '
                       f'({master_count} rows where TABNAME="DD03L", need at least 30). '
                       f'Upload a complete DD03L file first.'),
                fields=[],
            )
        # Step 3: DD03L must have rows for the uploaded table
        rows = conn.execute(
            f'SELECT FIELDNAME FROM "{dd03l_db_name}" WHERE TABNAME = ? AND ROLLNAME IS NOT NULL AND ROLLNAME != \'\'',
            (table_name.upper(),)
        ).fetchall()
        fieldname_vals = {r[0].strip() for r in rows if r[0] is not None}
        if not fieldname_vals:
            return _ValResult(
                code='V5',
                error=f'[V5]{t} {table_name}: no field definitions found in DD03L for table {table_name}.',
                fields=[],
            )
        # Step 4: Compare columns
        headers_set = set(headers)
        extra   = sorted(headers_set - fieldname_vals)
        missing = sorted(fieldname_vals - headers_set)
        if extra or missing:
            parts = []
            if extra:   parts.append(f'extra columns: {", ".join(extra)}')
            if missing: parts.append(f'missing columns: {", ".join(missing)}')
            return _ValResult(
                code='V5',
                error=f'[V5]{t} {table_name}: columns do not match DD03L field definitions. {"; ".join(parts)}',
                fields=[{'name': f, 'note': 'extra'} for f in extra] + [{'name': f, 'note': 'missing'} for f in missing],
            )
    return None


def _v6_dd03l_exists(ctx):
    """V6 — DD03L master table must be loaded (basis/customizing only)."""
    table_name, table_type, dd03l_db_name = ctx['table_name'], ctx['table_type'], ctx['dd03l_db_name']
    t = f'[{table_type}]'
    with get_db() as conn:
        dd03l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (dd03l_db_name,)
        ).fetchone()
        dd03l_count  = conn.execute(f'SELECT COUNT(*) FROM "{dd03l_db_name}"').fetchone()[0] if dd03l_exists else 0
        master_count = conn.execute(
            f'SELECT COUNT(*) FROM "{dd03l_db_name}" WHERE TABNAME = \'DD03L\''
        ).fetchone()[0] if dd03l_exists else 0
        ctx['master_count'] = master_count  # cache for V7
        if not dd03l_exists or dd03l_count == 0 or master_count == 0:
            return _ValResult(
                code='V6',
                error=f'[V6]{t} DD03L master table is not loaded. Upload DD03L before uploading {table_type} tables.',
                fields=[],
            )
    return None


def _v7_dd03l_complete(ctx):
    """V7 — DD03L must have at least 30 self-referential rows (basis/customizing only)."""
    table_name, table_type = ctx['table_name'], ctx['table_type']
    t = f'[{table_type}]'
    master_count = ctx.get('master_count', 0)
    if master_count < 30:
        return _ValResult(
            code='V7',
            error=(f'[V7]{t} DD03L master data is incomplete ({master_count} rows where TABNAME="DD03L", '
                   f'need at least 30). Upload a complete DD03L file first.'),
            fields=[],
        )
    return None


def _v8_table_in_dd03l(ctx):
    """V8 — uploaded table must have entries in DD03L (basis/customizing only)."""
    table_name, table_type, dd03l_db_name = ctx['table_name'], ctx['table_type'], ctx['dd03l_db_name']
    t = f'[{table_type}]'
    with get_db() as conn:
        exists_in_dd03l = conn.execute(
            f'SELECT 1 FROM "{dd03l_db_name}" WHERE TABNAME = ?', (table_name.upper(),)
        ).fetchone()
        if not exists_in_dd03l:
            return _ValResult(
                code='V8',
                error=f'[V8]{t} no entries in master table (DD03L) for {table_name}.',
                fields=[],
            )
    return None


def _v9_column_match(ctx):
    """V9 — Excel columns must match DD03L field definitions (basis/customizing only)."""
    table_name, headers, table_type, dd03l_db_name = (
        ctx['table_name'], ctx['headers'], ctx['table_type'], ctx['dd03l_db_name']
    )
    t = f'[{table_type}]'
    with get_db() as conn:
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
            return _ValResult(
                code='V9',
                error=f'[V9]{t} {table_name}: Excel headers do not match DD03L field definitions. {"; ".join(parts)}',
                fields=[{'name': f, 'note': 'extra'} for f in extra] + [{'name': f, 'note': 'missing'} for f in missing],
            )
    return None


# ── Validation pipelines ───────────────────────────────────────────────────

_VALIDATION_PIPELINE = {
    'master':      [_v1_technical_headers, _v2_required_cols, _v3_no_mixed_tabnames, _v4_self_ref_columns, _v5_non_self_ref_columns],
    'basis':       [_v1_technical_headers, _v6_dd03l_exists,  _v7_dd03l_complete,    _v8_table_in_dd03l,   _v9_column_match],
    'customizing': [_v1_technical_headers, _v6_dd03l_exists,  _v7_dd03l_complete,    _v8_table_in_dd03l,   _v9_column_match],
}


def _run_validations(table_name, headers, data_rows, table_type, dd03l_db_name):
    """Run the validation pipeline for the given table type. Returns first _ValResult or None."""
    ctx = {
        'table_name':    table_name,
        'headers':       headers,
        'data_rows':     data_rows,
        'table_type':    table_type,
        'dd03l_db_name': dd03l_db_name,
    }
    for step in _VALIDATION_PIPELINE.get(table_type, []):
        vr = step(ctx)
        if vr:
            return vr
    return None


# ── Validation log / exception helpers ────────────────────────────────────

def _get_exceptions(conn, custname, validation, table_name):
    """Return set of field names excepted for a given validation + table."""
    rows = conn.execute(
        'SELECT field_name FROM validation_exceptions WHERE custname=? AND validation=? AND table_name=?',
        (custname, validation, table_name.upper())
    ).fetchall()
    return {r[0] for r in rows}


def _log_val_fields(conn, custname, validation, table_name, fields):
    """Log validation violation fields. fields is a list of dicts or strings."""
    if fields:
        for f in fields:
            name = f['name'] if isinstance(f, dict) else f
            note = f.get('note') if isinstance(f, dict) else None
            conn.execute(
                'INSERT INTO validation_logs (custname, validation, table_name, field_name, note) '
                'VALUES (?,?,?,?,?)',
                (custname, validation, table_name.upper(), name, note)
            )
    else:
        conn.execute(
            'INSERT INTO validation_logs (custname, validation, table_name, field_name, note) '
            'VALUES (?,?,?,?,?)',
            (custname, validation, table_name.upper(), None, None)
        )


# ── Database helpers ───────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _batched(iterable, n):
    """Yield successive n-sized batches from iterable."""
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def _count_xlsx_rows(file_bytes):
    """Count data rows in an XLSX without openpyxl.

    Scans <row> elements in the sheet XML inside the ZIP directly.
    Uses a fraction of the memory and time compared to openpyxl for large files.
    Returns row count excluding the header row.
    """
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
            # Find the first worksheet (xl/worksheets/sheet1.xml is standard)
            sheet_name = next(
                (n for n in zf.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml', n)),
                None
            )
            if not sheet_name:
                return 0
            with zf.open(sheet_name) as f:
                row_count = sum(
                    1 for _, el in iterparse(f, events=['end'])
                    if el.tag.endswith('}row') or el.tag == 'row'
                )
        return max(0, row_count - 1)  # subtract header row
    except Exception:
        return 0


def _load_shared_strings(zf):
    """Load the shared strings table from an open ZipFile. Returns list indexed by position."""
    shared = []
    if 'xl/sharedStrings.xml' not in zf.namelist():
        return shared
    with zf.open('xl/sharedStrings.xml') as f:
        current_parts = []
        for event, el in iterparse(f, events=['start', 'end']):
            tag = el.tag.split('}')[-1]
            if event == 'start' and tag == 'si':
                current_parts = []
            elif event == 'end' and tag == 't':
                current_parts.append(el.text or '')
                el.clear()
            elif event == 'end' and tag == 'si':
                shared.append(''.join(current_parts))
    return shared


def _col_letters_to_idx(ref):
    """Convert a cell reference like 'A1' or 'BC3' to a 0-based column index."""
    idx = 0
    for ch in ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + (ord(ch.upper()) - 64)
    return idx - 1


def _read_xlsx_headers(file_bytes):
    """Read the header row from an XLSX using ZIP/XML streaming — no openpyxl.

    Returns (headers_list, error_string). On success error_string is None.
    """
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
            shared = _load_shared_strings(zf)
            sheet_name = next(
                (n for n in zf.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml', n)),
                None
            )
            if not sheet_name:
                return None, 'No worksheet found in Excel file'
            cells = {}
            in_first_row = False
            cur_ref = cur_type = cur_val = None
            in_is = False
            with zf.open(sheet_name) as f:
                for event, el in iterparse(f, events=['start', 'end']):
                    tag = el.tag.split('}')[-1]
                    if event == 'start' and tag == 'row':
                        if el.get('r', '1') == '1':
                            in_first_row = True
                        elif in_first_row:
                            break
                    elif event == 'start' and tag == 'c' and in_first_row:
                        cur_ref  = el.get('r', '')
                        cur_type = el.get('t', '')
                        cur_val  = None
                        in_is    = False
                    elif event == 'start' and tag == 'is':
                        in_is = True
                    elif event == 'end' and tag == 'v' and in_first_row and not in_is:
                        cur_val = el.text
                    elif event == 'end' and tag == 't' and in_first_row and in_is:
                        cur_val = el.text
                    elif event == 'end' and tag == 'c' and in_first_row and cur_ref:
                        col_idx = _col_letters_to_idx(cur_ref)
                        if cur_type == 's' and cur_val is not None:
                            val = shared[int(cur_val)] if int(cur_val) < len(shared) else ''
                        else:
                            val = cur_val or ''
                        cells[col_idx] = val
            if not cells:
                return None, 'Excel file is empty'
            max_col = max(cells.keys())
            return [cells.get(i, '') for i in range(max_col + 1)], None
    except Exception as e:
        return None, f'Cannot read Excel file: {e}'


def _stream_xlsx_rows(file_bytes, n_cols):
    """Stream data rows from an XLSX using ZIP/XML — no openpyxl, constant memory.

    Yields each data row (after the header) as a list of n_cols values (str or None).
    Handles sparse rows: missing cells are yielded as None.
    """
    with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
        shared = _load_shared_strings(zf)
        sheet_name = next(
            (n for n in zf.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml', n)),
            None
        )
        if not sheet_name:
            return
        with zf.open(sheet_name) as f:
            row_cells  = {}
            first_row  = True
            cur_ref    = cur_type = cur_val = None
            in_is      = False
            for event, el in iterparse(f, events=['start', 'end']):
                tag = el.tag.split('}')[-1]
                if event == 'start' and tag == 'row':
                    row_cells = {}
                elif event == 'end' and tag == 'row':
                    if first_row:
                        first_row = False
                    else:
                        yield [row_cells.get(i) for i in range(n_cols)]
                    row_cells = {}
                elif event == 'start' and tag == 'c':
                    cur_ref  = el.get('r', '')
                    cur_type = el.get('t', '')
                    cur_val  = None
                    in_is    = False
                elif event == 'start' and tag == 'is':
                    in_is = True
                elif event == 'end' and tag == 'v' and not in_is:
                    cur_val = el.text
                elif event == 'end' and tag == 't' and in_is:
                    cur_val = el.text
                elif event == 'end' and tag == 'c' and cur_ref:
                    col_idx = _col_letters_to_idx(cur_ref)
                    if col_idx < n_cols:
                        if cur_type == 's' and cur_val is not None:
                            val = shared[int(cur_val)] if int(cur_val) < len(shared) else None
                        else:
                            val = cur_val
                        row_cells[col_idx] = val


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
        CREATE TABLE IF NOT EXISTS validation_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            custname     TEXT NOT NULL,
            validation   TEXT NOT NULL,
            table_name   TEXT NOT NULL,
            field_name   TEXT,
            note         TEXT,
            triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS validation_exceptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            custname   TEXT NOT NULL,
            validation TEXT NOT NULL,
            table_name TEXT NOT NULL,
            field_name TEXT NOT NULL,
            added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(custname, validation, table_name, field_name)
        );
        CREATE TABLE IF NOT EXISTS upload_jobs (
            job_id         TEXT PRIMARY KEY,
            custname       TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            phase          TEXT,
            orig_table     TEXT,
            table_name     TEXT,
            table_type     TEXT,
            rows_inserted  INTEGER,
            error          TEXT,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')

    # Schema migrations for existing DBs (idempotent)
    for sql in [
        'ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE sessions ADD COLUMN custname TEXT',
        'ALTER TABLE _table_meta ADD COLUMN custname TEXT',
        'ALTER TABLE _table_meta ADD COLUMN orig_table TEXT',
        '''CREATE TABLE IF NOT EXISTS upload_jobs (
            job_id         TEXT PRIMARY KEY,
            custname       TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            orig_table     TEXT,
            table_name     TEXT,
            table_type     TEXT,
            total_rows     INTEGER,
            rows_inserted  INTEGER,
            error          TEXT,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        'ALTER TABLE upload_jobs ADD COLUMN total_rows INTEGER',
        'ALTER TABLE upload_jobs ADD COLUMN phase TEXT',
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
        return hmac.compare_digest(h.hex(), hash_hex)
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

    # ── Pre-file validation for basis/customizing (V6, V7, V8 need only DB) ──
    # Run these before reading the file so large uploads are rejected instantly.
    table_type = _determine_table_type(table_name)
    if table_type != 'master':
        pre_ctx = {
            'table_name': table_name, 'table_type': table_type,
            'dd03l_db_name': dd03l_db_name, 'headers': [], 'data_rows': [],
        }
        for step in [_v6_dd03l_exists, _v7_dd03l_complete, _v8_table_in_dd03l]:
            pre_vr = step(pre_ctx)
            if pre_vr:
                with get_db() as conn:
                    _log_val_fields(conn, custname, pre_vr.code, table_name, pre_vr.fields)
                return jsonify({'error': pre_vr.error}), 422

    # ── Read file bytes + headers only ────────────────────────────────────────
    # Use ZIP/XML streaming to avoid loading the full workbook with openpyxl.
    # openpyxl holds ~900 MB RAM for a 46 MB XLSX — on large files this causes OOM.
    file_bytes = f.read()
    raw_headers, err = _read_xlsx_headers(file_bytes)
    if err:
        return jsonify({'error': err}), 422

    headers = [str(h).strip() if h else f'col_{i}' for i, h in enumerate(raw_headers)]
    if not headers:
        return jsonify({'error': 'Excel file has no columns'}), 422

    # For master tables, load data_rows now — needed for V3/V4 validation.
    # Master files (DD03L) are small so openpyxl is fine here.
    # For basis/customizing, data_rows are not needed for validation.
    if table_type == 'master':
        try:
            wb = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
        except Exception as e:
            return jsonify({'error': f'Cannot read Excel file: {e}'}), 422
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        next(rows_iter)  # skip header
        data_rows = list(rows_iter)
        wb.close()
    else:
        data_rows = []

    # ── Step 2: Run validation pipeline ───────────────────────────────────────
    vr = _run_validations(table_name, headers, data_rows, table_type, dd03l_db_name)
    if vr:
        with get_db() as conn:
            _log_val_fields(conn, custname, vr.code, table_name, vr.fields)
            if vr.code in _EXCEPTION_VALIDATIONS and vr.fields:
                # Exception check only applies when there are specific fields to except
                exceptions = _get_exceptions(conn, custname, vr.code, table_name)
                remaining  = [f for f in vr.fields if f['name'] not in exceptions]
                if remaining:
                    extra   = [f['name'] for f in remaining if f.get('note') == 'extra']
                    missing = [f['name'] for f in remaining if f.get('note') == 'missing']
                    t = f'[{table_type}]'
                    parts = []
                    if vr.code == 'V9':
                        if extra:   parts.append(f'extra in Excel: {", ".join(extra[:5])}{"  …" if len(extra) > 5 else ""}')
                        if missing: parts.append(f'missing from Excel: {", ".join(missing[:5])}{"  …" if len(missing) > 5 else ""}')
                    else:
                        if extra:   parts.append(f'extra columns: {", ".join(extra[:5])}{"  …" if len(extra) > 5 else ""}')
                        if missing: parts.append(f'missing columns: {", ".join(missing[:5])}{"  …" if len(missing) > 5 else ""}')
                    return jsonify({'error': f'[{vr.code}]{t} {table_name}: {"; ".join(parts)}'}), 422
                # all fields excepted — fall through and proceed with upload
            else:
                # Hard block: prerequisite failures (empty fields) or non-exceptionable validations
                return jsonify({'error': vr.error}), 422

    # ── Step 3: Create job, start background insert ──────────────────────────
    # total_rows is counted inside the background thread to avoid blocking the
    # request (iterating 700K+ rows synchronously causes Cloudflare 524 timeouts).
    job_id = secrets.token_hex(8)
    with get_db() as conn:
        conn.execute(
            'INSERT INTO upload_jobs (job_id, custname, status) VALUES (?, ?, ?)',
            (job_id, custname, 'pending')
        )

    threading.Thread(
        target=_bg_insert,
        args=(job_id, custname, file_bytes, headers, data_rows,
              table_name, db_table_name, dd03l_db_name, table_type, system, client, date),
        daemon=True,
    ).start()

    return jsonify({'job_id': job_id})


def _bg_insert(job_id, custname, file_bytes, headers, data_rows,
               table_name, db_table_name, dd03l_db_name, table_type, system, client, date):
    """Background thread: count rows, then insert into SQLite and update job status."""
    try:
        n = len(headers)

        # ── Count total rows first so the frontend can show a determinate bar ──
        # For basis/customizing, scan raw ZIP/XML to count <row> elements instead
        # of using openpyxl — openpyxl holds ~900 MB RAM for a 46 MB XLSX and takes
        # several minutes, starving the gunicorn worker and causing 524 timeouts.
        if table_type == 'master':
            total_rows = len(data_rows)
        else:
            total_rows = _count_xlsx_rows(file_bytes)
        with get_db() as conn:
            conn.execute('UPDATE upload_jobs SET total_rows=? WHERE job_id=?', (total_rows, job_id))

        # ── Determine key fields ───────────────────────────────────────────────
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

        # ── Build column definitions ───────────────────────────────────────────
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

        # ── Row source: stream from file for basis/customizing, use cached rows for master ──
        def _stream_rows():
            # Use ZIP/XML streaming instead of openpyxl to avoid ~900 MB RAM per 46 MB file.
            yield from _stream_xlsx_rows(file_bytes, n)

        if table_type == 'master':
            row_source = (
                [str(row[i]) if i < len(row) and row[i] is not None else None for i in range(n)]
                for row in data_rows
            )
        else:
            row_source = _stream_rows()

        # ── Create table + meta ───────────────────────────────────────────────
        with get_db() as conn:
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{db_table_name}" ({col_defs})')
            conn.execute(
                'INSERT OR REPLACE INTO _table_meta (table_name, custname, orig_table, system, client, date) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (db_table_name, custname, table_name, system, client, date)
            )

        # ── Insert in batches — commit each batch, update progress after each ──
        rows_inserted = 0
        for batch in _batched(row_source, 1000):
            with get_db() as conn:
                conn.executemany(
                    f'INSERT OR REPLACE INTO "{db_table_name}" ({col_names}) VALUES ({placeholders})',
                    batch
                )
                rows_inserted += len(batch)
                conn.execute(
                    'UPDATE upload_jobs SET rows_inserted=? WHERE job_id=?',
                    (rows_inserted, job_id)
                )

        # ── Sort DD03L by TABNAME, POSITION after every insert ────────────────
        if table_name.upper() == 'DD03L' and 'TABNAME' in headers and 'POSITION' in headers:
            with get_db() as conn:
                conn.execute('UPDATE upload_jobs SET phase=? WHERE job_id=?', ('sorting', job_id))
            with get_db() as conn:
                tmp = db_table_name + '__sorted_tmp'
                conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
                conn.execute(
                    f'CREATE TABLE "{tmp}" AS '
                    f'SELECT * FROM "{db_table_name}" '
                    f'ORDER BY TABNAME ASC, CAST(POSITION AS INTEGER) ASC'
                )
                conn.execute(f'DROP TABLE "{db_table_name}"')
                conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{db_table_name}"')

        with get_db() as conn:
            conn.execute(
                'UPDATE upload_jobs SET status=?, rows_inserted=?, orig_table=?, table_name=?, table_type=? WHERE job_id=?',
                ('done', rows_inserted, table_name, db_table_name, table_type, job_id)
            )

    except Exception as e:
        with get_db() as conn:
            conn.execute(
                'UPDATE upload_jobs SET status=?, error=? WHERE job_id=?',
                ('error', str(e), job_id)
            )


@app.get('/api/upload/status/<job_id>')
@require_auth
def upload_status(job_id):
    custname = _session_custname()
    with get_db() as conn:
        job = conn.execute(
            'SELECT status, phase, orig_table, table_name, table_type, total_rows, rows_inserted, error '
            'FROM upload_jobs WHERE job_id=? AND custname=?',
            (job_id, custname)
        ).fetchone()
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(dict(job))


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
        dd08l_name = f'{custname}_{system}_DD08L'
        dd07t_name = f'{custname}_{system}_DD07T'

        # Fetch raw rows
        try:
            raw_rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        if not raw_rows:
            return jsonify({'columns': [], 'rows': [], 'dd04t_missing': False, 'dd08l_missing': False, 'partial_descriptions': False})

        raw_cols = list(raw_rows[0].keys())

        # Check DD04T existence and records
        dd04t_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd04t_name,)
        ).fetchone()
        dd04t_count = conn.execute(f'SELECT COUNT(*) FROM "{dd04t_name}"').fetchone()[0] if dd04t_exists else 0
        dd04t_missing = not dd04t_exists or dd04t_count == 0

        # Check DD08L existence and whether it has any FRKART='TEXT' entries at all
        dd08l_exists_check = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd08l_name,)
        ).fetchone()
        dd08l_text_count = conn.execute(
            f'SELECT COUNT(*) FROM "{dd08l_name}" WHERE FRKART=?', ('TEXT',)
        ).fetchone()[0] if dd08l_exists_check else 0
        dd08l_missing = not dd08l_exists_check or dd08l_text_count == 0

        # Build enriched headers
        enriched_cols = []
        all_missing   = []   # every field without a description (for logging)
        missing_fields = []  # non-excepted missing fields (returned to frontend)
        partial_descriptions = False

        dd03l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd03l_name,)
        ).fetchone()

        vshow2_exceptions = set()
        if not dd04t_missing:
            vshow2_exceptions = _get_exceptions(conn, custname, 'V-Show-2', orig_table)

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
                all_missing.append(col)
                if col not in vshow2_exceptions:
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
                all_missing.append(col)
                if col not in vshow2_exceptions:
                    missing_fields.append(col)
                    partial_descriptions = True
            else:
                enriched_cols.append(f'{col} - {desc_row["SCRTEXT_M"].strip()}')

        # Log V-Show-2 violations (all missing, regardless of exceptions)
        if all_missing:
            _log_val_fields(conn, custname, 'V-Show-2', orig_table, all_missing)

        # ── Build text-table lookup config per column (once per request) ──
        # For each column: DD03L → CHECKTABLE → DD08L (FRKART='TEXT') → text table → key fields
        dd08l_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd08l_name,)
        ).fetchone()

        dd07t_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd07t_name,)
        ).fetchone()

        col_lookup = {}      # col -> {'text_db': str, 'key_fields': [str], 'text_table': str}
        col_hints = {}       # col -> [hint lines for tooltip]
        if dd08l_exists_check and dd03l_exists:
            for col in raw_cols:
                ct_row = conn.execute(
                    f'SELECT CHECKTABLE FROM "{dd03l_name}" WHERE TABNAME=? AND FIELDNAME=?',
                    (orig_table.upper(), col)
                ).fetchone()
                if not ct_row or not ct_row['CHECKTABLE'] or ct_row['CHECKTABLE'].strip() == '*':
                    continue
                checktable = ct_row['CHECKTABLE'].strip()

                # Resolve chain one level: e.g. T685A.KAPPL → T681Z → T681A
                # so the DD08L TEXT lookup uses the actual key table (T681A), not the
                # intermediate check table (T681Z), and correctly finds T681B.
                next_ct_row = conn.execute(
                    f'SELECT CHECKTABLE FROM "{dd03l_name}" WHERE TABNAME=? AND FIELDNAME=?',
                    (checktable, col)
                ).fetchone()
                next_checktable = (next_ct_row['CHECKTABLE'] or '').strip() if next_ct_row else ''
                lookup_checktable = next_checktable if next_checktable and next_checktable != '*' else checktable

                tt_row = conn.execute(
                    f'SELECT TABNAME FROM "{dd08l_name}" WHERE FIELDNAME=? AND CHECKTABLE=? AND AS4LOCAL=? AND FRKART=?',
                    (col, lookup_checktable, 'A', 'TEXT')
                ).fetchone()
                if not tt_row or not tt_row['TABNAME']:
                    col_hints[col] = [
                        f'DD08L: no text entry for {orig_table}',
                        f'CHECKTABLE: {checktable}',
                    ]
                    continue
                text_table = tt_row['TABNAME'].strip()
                text_db = f'{custname}_{system}_{text_table}'

                tt_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (text_db,)
                ).fetchone()
                if not tt_exists:
                    col_hints[col] = [
                        f'CHECKTABLE: {checktable}',
                        f'Text table: {text_table} not uploaded',
                    ]
                    continue

                kf_rows = conn.execute(
                    f'SELECT FIELDNAME FROM "{dd03l_name}" WHERE TABNAME=? AND KEYFLAG=? AND FIELDNAME!=?',
                    (checktable, 'X', 'MANDT')
                ).fetchall()
                key_fields = [r['FIELDNAME'] for r in kf_rows if r['FIELDNAME']]
                if not key_fields:
                    col_hints[col] = [
                        f'CHECKTABLE: {checktable}',
                        f'Text table: {text_table}',
                        f'DD03L: no entries for {checktable}',
                    ]
                    continue

                col_lookup[col] = {'text_db': text_db, 'key_fields': key_fields, 'text_table': text_table}

        # ── DD07T domain value descriptions (columns without CHECKTABLE) ──
        if dd03l_exists:
            for col in raw_cols:
                if col in col_lookup or col in col_hints:
                    continue
                dom_row = conn.execute(
                    f'SELECT CHECKTABLE, DOMNAME FROM "{dd03l_name}" WHERE TABNAME=? AND FIELDNAME=?',
                    (orig_table.upper(), col)
                ).fetchone()
                if not dom_row or not dom_row['DOMNAME'] or not dom_row['DOMNAME'].strip():
                    continue
                checktable = (dom_row['CHECKTABLE'] or '').strip()
                if checktable and checktable != '*':
                    continue  # has a real CHECKTABLE — handled by DD08L path
                domname = dom_row['DOMNAME'].strip()
                if dd07t_exists:
                    col_lookup[col] = {'source': 'dd07t', 'domname': domname, 'dd07t_db': dd07t_name}
                else:
                    col_hints[col] = [f'DD07T: not uploaded (domain: {domname})']

        # ── Build rows with enriched cell values ──
        desc_cache = {}  # (col, key_vals_tuple) -> str or None

        row_keys = set(raw_cols)

        def _get_cell_desc(col, row):
            cfg = col_lookup.get(col)
            if not cfg:
                return None
            if cfg.get('source') == 'dd07t':
                val = row[col]
                if val is None or str(val).strip() == '':
                    return None
                cache_key = (col, val)
                if cache_key in desc_cache:
                    return desc_cache[cache_key]
                result = conn.execute(
                    f'SELECT DDTEXT FROM "{cfg["dd07t_db"]}" '
                    'WHERE DOMNAME=? AND DDLANGUAGE=? AND AS4LOCAL=? AND DOMVALUE_L=?',
                    (cfg['domname'], 'EN', 'A', str(val))
                ).fetchone()
                desc = result['DDTEXT'].strip() if result and result['DDTEXT'] else None
                desc_cache[cache_key] = desc
                return desc
            # Only use key fields that actually exist in the source row
            available_kfs = [kf for kf in cfg['key_fields'] if kf in row_keys]
            if not available_kfs:
                return None
            key_vals = tuple(row[kf] for kf in available_kfs)
            if any(v is None or str(v).strip() == '' for v in key_vals):
                return None
            cache_key = (col, key_vals)
            if cache_key in desc_cache:
                return desc_cache[cache_key]
            placeholders = ' AND '.join(f'"{kf}"=?' for kf in available_kfs)
            vtext_row = conn.execute(
                f'SELECT VTEXT FROM "{cfg["text_db"]}" WHERE SPRAS=? AND {placeholders}',
                ('EN', *key_vals)
            ).fetchone()
            desc = vtext_row['VTEXT'].strip() if vtext_row and vtext_row['VTEXT'] else None
            desc_cache[cache_key] = desc
            return desc

        dd07t_missing_cols = set()
        rows_out = []
        for row in raw_rows:
            enriched_row = {}
            for raw_col, enriched_col in zip(raw_cols, enriched_cols):
                val = row[raw_col]
                desc = _get_cell_desc(raw_col, row)
                cfg = col_lookup.get(raw_col)
                if cfg and cfg.get('source') == 'dd07t' and desc is None:
                    if val is not None and str(val).strip():
                        dd07t_missing_cols.add(raw_col)
                if desc and val is not None and str(val).strip() != '':
                    enriched_row[enriched_col] = f'{val} - {desc}'
                else:
                    enriched_row[enriched_col] = val
            rows_out.append(enriched_row)

        for col in dd07t_missing_cols:
            if col not in col_hints:
                domname = col_lookup[col]['domname']
                col_hints[col] = [f'DD07T: some values have no text (domain: {domname})']

        col_text_tables = {
            enriched_col: col_hints[raw_col]
            for raw_col, enriched_col in zip(raw_cols, enriched_cols)
            if raw_col in col_hints
        }

    return jsonify({
        'columns': enriched_cols,
        'rows': rows_out,
        'dd04t_missing': dd04t_missing,
        'dd08l_missing': dd08l_missing,
        'partial_descriptions': partial_descriptions,
        'missing_fields': missing_fields,
        'col_text_tables': col_text_tables,
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
@require_admin
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


# ── Validation log & exception routes ─────────────────────────────────────

@app.get('/api/validation-logs')
@require_auth
@require_admin
def list_validation_logs():
    custname = _session_custname()
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT vl.id, vl.validation, vl.table_name, vl.field_name, vl.note, vl.triggered_at,
                      CASE WHEN ve.id IS NOT NULL THEN 1 ELSE 0 END AS is_excepted
               FROM validation_logs vl
               LEFT JOIN validation_exceptions ve
                  ON ve.custname   = vl.custname
                 AND ve.validation = vl.validation
                 AND ve.table_name = vl.table_name
                 AND ve.field_name = vl.field_name
               WHERE vl.custname = ?
               ORDER BY vl.triggered_at DESC
               LIMIT 500''',
            (custname,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get('/api/validation-exceptions')
@require_auth
@require_admin
def list_validation_exceptions():
    custname = _session_custname()
    with get_db() as conn:
        rows = conn.execute(
            'SELECT id, validation, table_name, field_name, added_at '
            'FROM validation_exceptions WHERE custname=? ORDER BY added_at DESC',
            (custname,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post('/api/validation-exceptions')
@require_auth
@require_admin
def add_validation_exception():
    custname = _session_custname()
    data = request.json or {}
    validation = data.get('validation', '').strip()
    table_name = data.get('table_name', '').strip().upper()
    field_name = data.get('field_name', '').strip()
    if not validation or not table_name or not field_name:
        return jsonify({'error': 'validation, table_name and field_name are required'}), 400
    if validation not in _EXCEPTION_VALIDATIONS:
        return jsonify({'error': f'Exceptions not supported for {validation}'}), 400
    with get_db() as conn:
        try:
            conn.execute(
                'INSERT INTO validation_exceptions (custname, validation, table_name, field_name) VALUES (?,?,?,?)',
                (custname, validation, table_name, field_name)
            )
            row_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        except sqlite3.IntegrityError:
            row = conn.execute(
                'SELECT id FROM validation_exceptions WHERE custname=? AND validation=? AND table_name=? AND field_name=?',
                (custname, validation, table_name, field_name)
            ).fetchone()
            row_id = row['id']
    return jsonify({'ok': True, 'id': row_id}), 201


@app.delete('/api/validation-exceptions/<int:exc_id>')
@require_auth
@require_admin
def delete_validation_exception(exc_id):
    custname = _session_custname()
    with get_db() as conn:
        exists = conn.execute(
            'SELECT 1 FROM validation_exceptions WHERE id=? AND custname=?', (exc_id, custname)
        ).fetchone()
        if not exists:
            return jsonify({'error': 'Exception not found'}), 404
        conn.execute('DELETE FROM validation_exceptions WHERE id=?', (exc_id,))
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
