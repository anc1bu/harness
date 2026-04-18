"""
Flask backend for Harness — Sapcons.
Serves the SPA and provides a REST API backed by SQLite.

Run: python server.py
"""

import glob
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
import csv
from io import BytesIO, StringIO
from xml.etree.ElementTree import iterparse
from flask import Flask, request, jsonify, send_from_directory, Response
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
    """V3 — Two modes:
    - No DD03L in DB: file must only contain TABNAME='DD03L' rows (short-circuit on first offender).
    - DD03L in DB: mixed TABNAMEs allowed, but DD03L rows in Excel must exactly match DB row-by-row.
    """
    table_name, table_type = ctx['table_name'], ctx['table_type']
    t = f'[{table_type}]'
    offender = ctx.get('_v3_offender')
    if offender:
        return _ValResult(
            code='V3',
            error=(f'[V3]{t} {table_name}: DD03L cannot be mixed with other table names in the TABNAME column. '
                   f'Please upload a file that contains only DD03L entries. Found other values: {offender}'),
            fields=[{'name': offender, 'note': None}],
        )
    if ctx.get('_v3_mismatch'):
        return _ValResult(
            code='V3',
            error=f'[V3]{t} DD03L entries in the Excel and DB do not match. Delete DD03L and start over.',
            fields=[],
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
    if '_fieldname_vals' in ctx:
        fieldname_vals = ctx['_fieldname_vals']
    else:
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


def _count_xlsx_rows(file_path):
    """Count data rows in an XLSX without openpyxl.

    Scans <row> elements in the sheet XML inside the ZIP directly.
    Uses a fraction of the memory and time compared to openpyxl for large files.
    Returns row count excluding the header row.
    """
    try:
        with zipfile.ZipFile(file_path) as zf:
            # Find the first worksheet (xl/worksheets/sheet1.xml is standard)
            sheet_name = next(
                (n for n in zf.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml', n)),
                None
            )
            if not sheet_name:
                return 0
            with zf.open(sheet_name) as f:
                row_count = 0
                for _, el in iterparse(f, events=['end']):
                    if el.tag.endswith('}row') or el.tag == 'row':
                        row_count += 1
                    el.clear()
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
                el.clear()
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


def _stream_xlsx_rows(file_path, n_cols):
    """Stream data rows from an XLSX using ZIP/XML — no openpyxl, constant memory.

    Yields each data row (after the header) as a list of n_cols values (str or None).
    Handles sparse rows: missing cells are yielded as None.
    """
    with zipfile.ZipFile(file_path) as zf:
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
                if event == 'end':
                    el.clear()


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
        CREATE TABLE IF NOT EXISTS panel_assignments (
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            custname   TEXT    NOT NULL,
            orig_table TEXT    NOT NULL,
            panel      TEXT    NOT NULL DEFAULT 'customizing',
            PRIMARY KEY (user_id, custname, orig_table)
        );
    ''')

    # Schema migrations for existing DBs (idempotent)
    for sql in [
        'ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE sessions ADD COLUMN custname TEXT',
        'ALTER TABLE _table_meta ADD COLUMN custname TEXT',
        'ALTER TABLE _table_meta ADD COLUMN orig_table TEXT',
        'ALTER TABLE _table_meta ADD COLUMN row_count INTEGER',
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
        '''CREATE TABLE IF NOT EXISTS panel_assignments (
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            custname   TEXT    NOT NULL,
            orig_table TEXT    NOT NULL,
            panel      TEXT    NOT NULL DEFAULT 'customizing',
            PRIMARY KEY (user_id, custname, orig_table)
        )''',
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


def _cleanup_stale_uploads():
    """Remove leftover temp files and mark orphaned jobs from a previous crashed run."""
    for path in glob.glob('/tmp/harness_upload_*.xlsx'):
        try:
            os.unlink(path)
        except OSError:
            pass
    # Any job still pending at startup had its background thread killed — mark as error.
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE upload_jobs SET status='error', error='Upload interrupted (server restarted)' "
                "WHERE status='pending'"
            )
    except Exception:
        pass


_cleanup_stale_uploads()


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
    token = request.headers.get('Authorization', '').removeprefix('Bearer ')
    if not token and request.method == 'GET':
        token = request.args.get('token', '')
    return token


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


def _session_user_id():
    with get_db() as conn:
        session = conn.execute(
            'SELECT user_id FROM sessions WHERE token = ?', (_get_token(),)
        ).fetchone()
    return session['user_id'] if session else None


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
            'SELECT table_name, orig_table, system, client, date, row_count FROM _table_meta '
            'WHERE custname = ? ORDER BY orig_table',
            (custname,)
        ).fetchall()
    return jsonify([{
        'table':      r['table_name'],
        'orig_table': r['orig_table'] or r['table_name'],
        'system':     r['system'],
        'client':     r['client'],
        'date':       r['date'],
        'count':      r['row_count'] or 0,
    } for r in meta])


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


# ── Panel assignments ──────────────────────────────────────────────────────

@app.get('/api/panel-assignments')
@require_auth
def get_panel_assignments():
    user_id  = _session_user_id()
    custname = _session_custname()
    with get_db() as conn:
        rows = conn.execute(
            'SELECT orig_table, panel FROM panel_assignments WHERE user_id=? AND custname=?',
            (user_id, custname)
        ).fetchall()
    return jsonify({r['orig_table']: r['panel'] for r in rows})


@app.post('/api/panel-assignments')
@require_auth
def set_panel_assignment():
    user_id  = _session_user_id()
    custname = _session_custname()
    data     = request.json or {}
    orig_table = data.get('orig_table', '').strip()
    panel      = data.get('panel', '').strip()
    if not orig_table or panel not in ('customizing', 'secondary'):
        return jsonify({'error': 'Invalid payload'}), 400
    with get_db() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO panel_assignments (user_id, custname, orig_table, panel) VALUES (?,?,?,?)',
            (user_id, custname, orig_table, panel)
        )
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

    # ── Step 2: Run header-only validations synchronously ────────────────────
    # For master tables (DD03L), V3/V4/V5 require streaming all data rows which
    # can take minutes on large files and causes Cloudflare 524 timeouts.
    # Run only V1+V2 (header-only) here; V3/V4/V5 run in the background thread.
    # For basis/customizing, all validations need only headers or DB — run them here.
    if table_type == 'master':
        sync_steps = [_v1_technical_headers, _v2_required_cols]
    else:
        # V6/V7/V8 already ran in pre-file step above; only V1 and V9 need headers
        sync_steps = [_v1_technical_headers, _v9_column_match]

    data_rows = []  # master: loaded in background; basis/customizing: not needed for sync validation
    sync_ctx = {
        'table_name': table_name, 'table_type': table_type,
        'dd03l_db_name': dd03l_db_name, 'headers': headers, 'data_rows': data_rows,
    }
    for step in sync_steps:
        vr = step(sync_ctx)
        if vr:
            with get_db() as conn:
                _log_val_fields(conn, custname, vr.code, table_name, vr.fields)
                if vr.code in _EXCEPTION_VALIDATIONS and vr.fields:
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
                    # all fields excepted — fall through
                else:
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

    # Write file bytes to a temp file so the background thread reads from disk
    # instead of holding the full XLSX in RAM (prevents OOM on large uploads).
    tmp_path = f'/tmp/harness_upload_{job_id}.xlsx'
    with open(tmp_path, 'wb') as tmp_f:
        tmp_f.write(file_bytes)
    del file_bytes  # release RAM immediately; thread will use the disk file

    threading.Thread(
        target=_bg_insert,
        args=(job_id, custname, tmp_path, headers, data_rows,
              table_name, db_table_name, dd03l_db_name, table_type, system, client, date),
        daemon=True,
    ).start()

    return jsonify({'job_id': job_id})


def _bg_insert(job_id, custname, file_path, headers, data_rows,
               table_name, db_table_name, dd03l_db_name, table_type, system, client, date):
    """Background thread: validate (master only), count rows, insert into SQLite."""
    try:
        n = len(headers)

        # ── For master tables: one streaming scan for V3/V4/V5 validation + key_fields ──
        # Never materialise all rows into a list — 58 MB XLSX = ~750 MB XML = OOM.
        # Collect only the tiny metadata needed (TABNAME, FIELDNAME, KEYFLAG, ROLLNAME).
        key_fields = set()
        if table_type == 'master':
            with get_db() as conn:
                conn.execute('UPDATE upload_jobs SET phase=? WHERE job_id=?', ('validating', job_id))

            tabname_idx   = headers.index('TABNAME')   if 'TABNAME'   in headers else None
            fieldname_idx = headers.index('FIELDNAME') if 'FIELDNAME' in headers else None
            keyflag_idx   = headers.index('KEYFLAG')   if 'KEYFLAG'   in headers else None
            rollname_idx  = headers.index('ROLLNAME')  if 'ROLLNAME'  in headers else None

            all_tabnames      = set()
            dd03l_fieldnames  = set()
            v3_offender       = None
            v3_mismatch       = None
            excel_dd03l_rows  = {}  # pk_tuple -> {col: val} for row-by-row comparison

            # Load existing DD03L rows from DB (keyed by PK) for comparison
            db_dd03l_rows = {}
            with get_db() as conn:
                db_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (dd03l_db_name,)
                ).fetchone()
                if db_exists:
                    db_cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{dd03l_db_name}")').fetchall()]
                    for r in conn.execute(f'SELECT * FROM "{dd03l_db_name}" WHERE TABNAME = \'DD03L\'').fetchall():
                        rd = {db_cols[i]: (r[i] or '').strip() for i in range(len(db_cols))}
                        pk = (rd.get('FIELDNAME',''), rd.get('AS4LOCAL',''), rd.get('AS4VERS',''), rd.get('POSITION',''))
                        db_dd03l_rows[pk] = rd
            dd03l_in_db = bool(db_dd03l_rows)

            for row in _stream_xlsx_rows(file_path, n):
                tab = str(row[tabname_idx]).strip().upper()  if tabname_idx   is not None and row[tabname_idx]   else None
                fld = str(row[fieldname_idx]).strip()        if fieldname_idx is not None and row[fieldname_idx] else None
                flg = str(row[keyflag_idx]).strip().upper()  if keyflag_idx   is not None and row[keyflag_idx]   else None
                rol = str(row[rollname_idx]).strip()         if rollname_idx  is not None and row[rollname_idx]  else None
                if tab: all_tabnames.add(tab)
                if not dd03l_in_db:
                    # No DD03L in DB: short-circuit on first non-DD03L TABNAME
                    if tab and tab != 'DD03L' and v3_offender is None:
                        v3_offender = tab
                        break
                if tab == 'DD03L':
                    if fld and rol: dd03l_fieldnames.add(fld)
                    if flg == 'X' and fld: key_fields.add(fld)
                    if dd03l_in_db:
                        rd = {headers[i]: (str(row[i]) if row[i] is not None else '').strip() for i in range(n)}
                        pk = (rd.get('FIELDNAME',''), rd.get('AS4LOCAL',''), rd.get('AS4VERS',''), rd.get('POSITION',''))
                        excel_dd03l_rows[pk] = rd

            # Row-by-row comparison when DD03L already exists in DB
            if dd03l_in_db:
                if set(excel_dd03l_rows.keys()) != set(db_dd03l_rows.keys()):
                    v3_mismatch = True
                else:
                    for pk, excel_row in excel_dd03l_rows.items():
                        db_row = db_dd03l_rows[pk]
                        if any(excel_row.get(c, '') != db_row.get(c, '') for c in headers):
                            v3_mismatch = True
                            break

            bg_ctx = {
                'table_name':    table_name,   'table_type':    table_type,
                'dd03l_db_name': dd03l_db_name, 'headers':       headers,
                'data_rows':     [],            'all_tabnames':  all_tabnames,
                '_fieldname_vals': dd03l_fieldnames,
                '_v3_offender':  v3_offender,
                '_v3_mismatch':  v3_mismatch,
            }
            for step in [_v3_no_mixed_tabnames, _v4_self_ref_columns, _v5_non_self_ref_columns]:
                vr = step(bg_ctx)
                if vr:
                    with get_db() as conn:
                        _log_val_fields(conn, custname, vr.code, table_name, vr.fields)
                        conn.execute(
                            'UPDATE upload_jobs SET status=?, error=? WHERE job_id=?',
                            ('error', vr.error, job_id)
                        )
                    return
        else:
            with get_db() as conn:
                rows = conn.execute(
                    f'SELECT FIELDNAME FROM "{dd03l_db_name}" WHERE TABNAME = ? AND KEYFLAG = \'X\'',
                    (table_name.upper(),)
                ).fetchall()
                key_fields = {r[0].strip() for r in rows if r[0] is not None}

        # ── Count total rows so the frontend can show a determinate progress bar ──
        total_rows = _count_xlsx_rows(file_path)
        with get_db() as conn:
            conn.execute('UPDATE upload_jobs SET total_rows=? WHERE job_id=?', (total_rows, job_id))

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

        # ── Row source: stream for all table types — never hold all rows in RAM ──
        def _stream_rows():
            for row in _stream_xlsx_rows(file_path, n):
                yield [str(row[i]) if i < len(row) and row[i] is not None else None for i in range(n)]

        row_source = _stream_rows()

        # ── Create table + meta ───────────────────────────────────────────────
        # Master tables (DD03L) are always full-replacement uploads: drop and
        # recreate so the PRIMARY KEY is always applied correctly on re-uploads.
        with get_db() as conn:
            if table_type == 'master':
                conn.execute(f'DROP TABLE IF EXISTS "{db_table_name}"')
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
                conn.execute(f'CREATE TABLE "{tmp}" ({col_defs})')
                conn.execute(
                    f'INSERT OR IGNORE INTO "{tmp}" '
                    f'SELECT * FROM "{db_table_name}" '
                    f'ORDER BY TABNAME ASC, CAST(POSITION AS INTEGER) ASC'
                )
                conn.execute(f'DROP TABLE "{db_table_name}"')
                conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{db_table_name}"')
            # Index TABNAME (and composite TABNAME+FIELDNAME) so per-table lookups
            # in get_table_data don't full-scan 1M+ rows on every column query.
            with get_db() as conn:
                conn.execute(f'UPDATE upload_jobs SET phase=? WHERE job_id=?', ('indexing', job_id))
            with get_db() as conn:
                conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{db_table_name}_tabname" ON "{db_table_name}" (TABNAME)')
                conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{db_table_name}_tabname_fieldname" ON "{db_table_name}" (TABNAME, FIELDNAME)')

        with get_db() as conn:
            conn.execute(
                'UPDATE upload_jobs SET status=?, rows_inserted=?, orig_table=?, table_name=?, table_type=? WHERE job_id=?',
                ('done', rows_inserted, table_name, db_table_name, table_type, job_id)
            )
            actual_count = conn.execute(f'SELECT COUNT(*) FROM "{db_table_name}"').fetchone()[0]
            conn.execute(
                'UPDATE _table_meta SET row_count=? WHERE table_name=?',
                (actual_count, db_table_name)
            )

    except Exception as e:
        with get_db() as conn:
            conn.execute(
                'UPDATE upload_jobs SET status=?, error=? WHERE job_id=?',
                ('error', str(e), job_id)
            )
    finally:
        try:
            os.unlink(file_path)
        except OSError:
            pass


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


# ── Enrichment helpers ────────────────────────────────────────────────────

def _setup_enrichment(conn, orig_table, custname, system, raw_cols):
    """Prefetch all lookup metadata for a table's columns. Touches no row data."""
    dd03l_name = f'{custname}_{system}_DD03L'
    dd04t_name = f'{custname}_{system}_DD04T'
    dd08l_name = f'{custname}_{system}_DD08L'
    dd07t_name = f'{custname}_{system}_DD07T'

    dd04t_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd04t_name,)
    ).fetchone()
    dd04t_count = conn.execute(f'SELECT COUNT(*) FROM "{dd04t_name}"').fetchone()[0] if dd04t_exists else 0
    dd04t_missing = not dd04t_exists or dd04t_count == 0

    dd08l_exists_check = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd08l_name,)
    ).fetchone()
    dd08l_text_count = conn.execute(
        f'SELECT COUNT(*) FROM "{dd08l_name}" WHERE FRKART=?', ('TEXT',)
    ).fetchone()[0] if dd08l_exists_check else 0
    dd08l_missing = not dd08l_exists_check or dd08l_text_count == 0

    dd03l_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd03l_name,)
    ).fetchone()

    src_dd03l: dict = {}
    if dd03l_exists:
        for r in conn.execute(
            f'SELECT FIELDNAME, ROLLNAME, CHECKTABLE, DOMNAME FROM "{dd03l_name}" WHERE TABNAME=?',
            (orig_table.upper(),)
        ).fetchall():
            fn = r['FIELDNAME']
            if fn not in src_dd03l:
                src_dd03l[fn] = {
                    'rollname':   (r['ROLLNAME']   or '').strip(),
                    'checktable': (r['CHECKTABLE'] or '').strip(),
                    'domname':    (r['DOMNAME']    or '').strip(),
                }

    vshow2_exceptions = set()
    if not dd04t_missing:
        vshow2_exceptions = _get_exceptions(conn, custname, 'V-Show-2', orig_table)

    dd04t_map: dict = {}
    if not dd04t_missing and src_dd03l:
        rns = list({v['rollname'] for v in src_dd03l.values() if v['rollname']})
        if rns:
            ph = ','.join('?' * len(rns))
            for r in conn.execute(
                f'SELECT ROLLNAME, SCRTEXT_M FROM "{dd04t_name}" WHERE ROLLNAME IN ({ph}) AND DDLANGUAGE=?',
                (*rns, 'EN')
            ).fetchall():
                dd04t_map[r['ROLLNAME']] = (r['SCRTEXT_M'] or '').strip()

    enriched_cols = []
    all_missing   = []
    missing_fields = []
    partial_descriptions = False

    for col in raw_cols:
        if dd04t_missing:
            enriched_cols.append(col)
            continue
        info = src_dd03l.get(col)
        rollname = info['rollname'] if info else ''
        scrtext  = dd04t_map.get(rollname, '') if rollname else ''
        if scrtext:
            enriched_cols.append(f'{col} - {scrtext}')
        else:
            enriched_cols.append(col)
            all_missing.append(col)
            if col not in vshow2_exceptions:
                missing_fields.append(col)
                partial_descriptions = True

    dd07t_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (dd07t_name,)
    ).fetchone()

    col_lookup = {}
    col_hints  = {}

    if dd08l_missing:
        for col in raw_cols:
            col_hints[col] = ["Upload DD08L with FRKART='TEXT' to see cell descriptions"]
    elif dd08l_exists_check and dd03l_exists:
        checktable_for_col = {
            col: src_dd03l[col]['checktable']
            for col in raw_cols
            if col in src_dd03l and src_dd03l[col]['checktable'] and src_dd03l[col]['checktable'] != '*'
        }
        unique_cts = list(set(checktable_for_col.values()))

        chain_map: dict = {}
        if unique_cts:
            ph = ','.join('?' * len(unique_cts))
            for r in conn.execute(
                f'SELECT TABNAME, FIELDNAME, CHECKTABLE FROM "{dd03l_name}" WHERE TABNAME IN ({ph})',
                unique_cts
            ).fetchall():
                key = (r['TABNAME'], r['FIELDNAME'])
                if key not in chain_map:
                    chain_map[key] = (r['CHECKTABLE'] or '').strip()

        lookup_ct_for_col = {}
        for col, ct in checktable_for_col.items():
            nxt = chain_map.get((ct, col), '')
            lookup_ct_for_col[col] = nxt if nxt and nxt != '*' else ct

        unique_lookup_cts = list(set(lookup_ct_for_col.values()))

        dd08l_map: dict = {}
        if unique_lookup_cts:
            ph = ','.join('?' * len(unique_lookup_cts))
            for r in conn.execute(
                f'SELECT FIELDNAME, CHECKTABLE, TABNAME FROM "{dd08l_name}" '
                f'WHERE CHECKTABLE IN ({ph}) AND AS4LOCAL=? AND FRKART=?',
                (*unique_lookup_cts, 'A', 'TEXT')
            ).fetchall():
                key = (r['FIELDNAME'], r['CHECKTABLE'])
                if key not in dd08l_map:
                    dd08l_map[key] = r['TABNAME']

        self_text_map: dict = {}
        for r in conn.execute(
            f'SELECT FIELDNAME, TABNAME FROM "{dd08l_name}" '
            f'WHERE CHECKTABLE=? AND AS4LOCAL=? AND FRKART=?',
            (orig_table, 'A', 'TEXT')
        ).fetchall():
            if r['FIELDNAME'] not in self_text_map:
                self_text_map[r['FIELDNAME']] = r['TABNAME']

        all_tables_needed = list(
            set(unique_cts) | set(unique_lookup_cts) | ({orig_table} if self_text_map else set())
        )
        kf_map: dict = {}
        if all_tables_needed:
            ph = ','.join('?' * len(all_tables_needed))
            for r in conn.execute(
                f'SELECT TABNAME, FIELDNAME FROM "{dd03l_name}" '
                f'WHERE TABNAME IN ({ph}) AND KEYFLAG=? AND FIELDNAME!=?',
                (*all_tables_needed, 'X', 'MANDT')
            ).fetchall():
                kf_map.setdefault(r['TABNAME'], []).append(r['FIELDNAME'])

        all_text_table_names = list(set(list(dd08l_map.values()) + list(self_text_map.values())))
        potential_text_dbs = {f'{custname}_{system}_{tt}' for tt in all_text_table_names if tt}
        existing_tables: set = set()
        if potential_text_dbs:
            ph = ','.join('?' * len(potential_text_dbs))
            existing_tables = {
                r[0] for r in conn.execute(
                    f'SELECT name FROM sqlite_master WHERE type=? AND name IN ({ph})',
                    ('table', *potential_text_dbs)
                ).fetchall()
            }

        text_field_map: dict = {}
        if all_text_table_names:
            ph = ','.join('?' * len(all_text_table_names))
            for r in conn.execute(
                f'SELECT TABNAME, FIELDNAME FROM "{dd03l_name}" '
                f'WHERE TABNAME IN ({ph}) AND (KEYFLAG IS NULL OR KEYFLAG=?) '
                f'AND FIELDNAME NOT IN (?,?,?)',
                (*all_text_table_names, '', 'MANDT', 'SPRAS', 'LANGU')
            ).fetchall():
                if r['TABNAME'] not in text_field_map:
                    text_field_map[r['TABNAME']] = r['FIELDNAME']

        for col in raw_cols:
            checktable = checktable_for_col.get(col)
            if not checktable:
                continue
            lookup_ct = lookup_ct_for_col.get(col, checktable)
            tt = dd08l_map.get((col, lookup_ct))
            text_key_field = col

            if not tt:
                for kf in dict.fromkeys(kf_map.get(lookup_ct, [])):
                    if kf != col and (kf, lookup_ct) in dd08l_map:
                        tt = dd08l_map[(kf, lookup_ct)]
                        text_key_field = kf
                        break

            if not tt:
                col_hints[col] = [f'No text table found for {col} in DD08L']
                continue

            text_db = f'{custname}_{system}_{tt}'
            if text_db not in existing_tables:
                col_hints[col] = [f'Upload {tt} to see {col} descriptions']
                continue

            key_fields = kf_map.get(checktable, [])
            if not key_fields:
                col_hints[col] = [f'Upload DD03L with entries for {checktable} to see {col} descriptions']
                continue

            text_field = text_field_map.get(tt)
            if not text_field:
                col_hints[col] = [f'No text field found in DD03L for {tt}']
                continue

            col_lookup[col] = {
                'text_db': text_db, 'key_fields': key_fields,
                'text_table': tt,   'text_key_field': text_key_field,
                'text_field': text_field,
            }

        for field, tt in self_text_map.items():
            if field not in raw_cols or field in col_lookup or field in col_hints:
                continue
            text_db = f'{custname}_{system}_{tt}'
            if text_db not in existing_tables:
                col_hints[field] = [f'Upload {tt} to see {field} descriptions']
                continue
            key_fields = kf_map.get(orig_table, [])
            if not key_fields:
                continue
            text_field = text_field_map.get(tt)
            if not text_field:
                continue
            col_lookup[field] = {
                'text_db': text_db, 'key_fields': key_fields,
                'text_table': tt,   'text_key_field': field,
                'text_field': text_field,
            }

    if dd03l_exists:
        for col in raw_cols:
            if col in col_lookup or col in col_hints:
                continue
            info = src_dd03l.get(col)
            if not info or not info['domname']:
                continue
            if info['checktable'] and info['checktable'] != '*':
                continue
            if dd07t_exists:
                col_lookup[col] = {'source': 'dd07t', 'domname': info['domname'], 'dd07t_db': dd07t_name}
            else:
                col_hints[col] = [f'Upload DD07T to see {col} descriptions']

    row_keys = set(raw_cols)
    for col, cfg in col_lookup.items():
        if cfg.get('source') == 'dd07t':
            rows_dd07t = conn.execute(
                f'SELECT DOMVALUE_L, DDTEXT FROM "{cfg["dd07t_db"]}" '
                'WHERE DOMNAME=? AND DDLANGUAGE=? AND AS4LOCAL=?',
                (cfg['domname'], 'EN', 'A')
            ).fetchall()
            cfg['_preload'] = {r['DOMVALUE_L']: (r['DDTEXT'] or '').strip() for r in rows_dd07t}
            continue

        text_key_field = cfg.get('text_key_field', col)
        text_db_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{cfg["text_db"]}")')}
        seen_kfs: set = set()
        available_kfs = []
        for kf in cfg['key_fields']:
            if kf not in seen_kfs and kf in text_db_cols and (kf in row_keys or kf == text_key_field):
                seen_kfs.add(kf)
                available_kfs.append(kf)
        text_field = cfg['text_field']
        if not available_kfs or text_field not in text_db_cols:
            continue

        sel_cols = ', '.join(f'"{kf}"' for kf in available_kfs)
        preload: dict = {}
        for r in conn.execute(
            f'SELECT {sel_cols}, "{text_field}" FROM "{cfg["text_db"]}" WHERE SPRAS=?', ('EN',)
        ).fetchall():
            vtext = (r[text_field] or '').strip()
            if vtext:
                preload[tuple(r[kf] for kf in available_kfs)] = vtext
        cfg['_preload'] = preload
        cfg['_available_kfs'] = available_kfs

    plain_pairs   = []
    dd07t_triples = []
    text_triples  = []
    for raw_col, enriched_col in zip(raw_cols, enriched_cols):
        cfg = col_lookup.get(raw_col)
        if cfg is None:
            plain_pairs.append((raw_col, enriched_col))
        elif cfg.get('source') == 'dd07t':
            dd07t_triples.append((raw_col, enriched_col, cfg))
        else:
            text_triples.append((raw_col, enriched_col, cfg))

    return {
        'enriched_cols':        enriched_cols,
        'col_lookup':           col_lookup,
        'col_hints':            col_hints,
        'plain_pairs':          plain_pairs,
        'dd07t_triples':        dd07t_triples,
        'text_triples':         text_triples,
        'dd04t_missing':        dd04t_missing,
        'dd08l_missing':        dd08l_missing,
        'partial_descriptions': partial_descriptions,
        'missing_fields':       missing_fields,
        'all_missing':          all_missing,
    }


def _enrich_row(row_d, plain_pairs, dd07t_triples, text_triples):
    """Apply enrichment to a single row dict. Returns (enriched_row, dd07t_miss_set)."""
    enriched_row = {ec: row_d.get(rc) for rc, ec in plain_pairs}
    dd07t_miss = set()

    for rc, ec, cfg in dd07t_triples:
        val = row_d.get(rc)
        desc = None
        if val is not None and str(val).strip():
            desc = cfg['_preload'].get(str(val)) or None
            if desc is None:
                dd07t_miss.add(rc)
        enriched_row[ec] = f'{val} - {desc}' if (desc and val is not None and str(val).strip()) else val

    for rc, ec, cfg in text_triples:
        val = row_d.get(rc)
        desc = None
        available_kfs = cfg.get('_available_kfs')
        if available_kfs:
            text_key_field = cfg.get('text_key_field', rc)
            key_vals = tuple(
                row_d.get(rc) if (kf == text_key_field and text_key_field != rc) else row_d.get(kf)
                for kf in available_kfs
            )
            if not any(v is None or str(v).strip() == '' for v in key_vals):
                desc = cfg['_preload'].get(key_vals) or None
        enriched_row[ec] = f'{val} - {desc}' if (desc and val is not None and str(val).strip() != '') else val

    return enriched_row, dd07t_miss


# ── Table data route ───────────────────────────────────────────────────────

@app.get('/api/tables/<table>/data')
@require_auth
def get_table_data(table):
    custname = _session_custname()
    offset   = max(0, int(request.args.get('offset', 0)))
    limit    = max(1, min(int(request.args.get('limit', 5000)), 10000))
    with get_db() as conn:
        meta = conn.execute(
            'SELECT orig_table, system FROM _table_meta WHERE table_name = ? AND custname = ?',
            (table, custname)
        ).fetchone()
        if not meta:
            return jsonify({'error': 'Table not found'}), 404

        orig_table = meta['orig_table'] or table
        system     = meta['system']

        # Get valid column names to whitelist filter params
        try:
            table_cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        where_parts, filter_params = _parse_filter_clauses(request.args, table_cols)
        where_sql = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

        try:
            total    = conn.execute(f'SELECT COUNT(*) FROM "{table}" {where_sql}', filter_params).fetchone()[0]
            raw_rows = conn.execute(
                f'SELECT * FROM "{table}" {where_sql} LIMIT ? OFFSET ?', (*filter_params, limit, offset)
            ).fetchall()
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        if not raw_rows:
            return jsonify({'columns': [], 'raw_columns': [], 'rows': [], 'total': total, 'offset': offset, 'limit': limit,
                            'dd04t_missing': False, 'dd08l_missing': False, 'partial_descriptions': False,
                            'missing_fields': [], 'col_text_tables': {}})

        raw_cols = list(raw_rows[0].keys())
        enr = _setup_enrichment(conn, orig_table, custname, system, raw_cols)

        if enr['all_missing']:
            _log_val_fields(conn, custname, 'V-Show-2', orig_table, enr['all_missing'])

        plain_pairs   = enr['plain_pairs']
        dd07t_triples = enr['dd07t_triples']
        text_triples  = enr['text_triples']

        dd07t_miss_all = set()
        rows_out = []
        for row in raw_rows:
            er, miss = _enrich_row(dict(row), plain_pairs, dd07t_triples, text_triples)
            rows_out.append(er)
            dd07t_miss_all |= miss

        col_hints  = enr['col_hints']
        col_lookup = enr['col_lookup']
        for col in dd07t_miss_all:
            if col not in col_hints:
                col_hints[col] = [f'No domain values defined for {col} ({col_lookup[col]["domname"]})']

        col_text_tables = {
            ec: col_hints[rc]
            for rc, ec in zip(raw_cols, enr['enriched_cols'])
            if rc in col_hints
        }

    return jsonify({
        'columns':              enr['enriched_cols'],
        'raw_columns':          raw_cols,
        'rows':                 rows_out,
        'total':                total,
        'offset':               offset,
        'limit':                limit,
        'dd04t_missing':        enr['dd04t_missing'],
        'dd08l_missing':        enr['dd08l_missing'],
        'partial_descriptions': enr['partial_descriptions'],
        'missing_fields':       enr['missing_fields'],
        'col_text_tables':      col_text_tables,
    })


@app.get('/api/tables/<table>/export')
@require_auth
def export_table(table):
    custname = _session_custname()

    # Resolve metadata and enrichment setup before streaming
    with get_db() as conn:
        meta = conn.execute(
            'SELECT orig_table, system FROM _table_meta WHERE table_name = ? AND custname = ?',
            (table, custname)
        ).fetchone()
        if not meta:
            return jsonify({'error': 'Table not found'}), 404

        orig_table = meta['orig_table'] or table
        system     = meta['system']

        try:
            raw_cols_row = conn.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchone()
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        if not raw_cols_row:
            return Response('', mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename="{orig_table}_{system}.csv"'})

        raw_cols = list(raw_cols_row.keys())
        enr = _setup_enrichment(conn, orig_table, custname, system, raw_cols)

    # Generator opens its own connection so the with-block above can close cleanly
    def generate(table=table, orig_table=orig_table, enr=enr):
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow(enr['enriched_cols'])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        plain_pairs   = enr['plain_pairs']
        dd07t_triples = enr['dd07t_triples']
        text_triples  = enr['text_triples']

        with get_db() as conn2:
            batch_size = 1000
            offset = 0
            while True:
                rows = conn2.execute(
                    f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (batch_size, offset)
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    er, _ = _enrich_row(dict(row), plain_pairs, dd07t_triples, text_triples)
                    writer.writerow(er.values())
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)
                offset += batch_size

    filename = f'{orig_table}_{system}.csv'
    return Response(
        generate(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


def _parse_filter_clauses(args, table_cols, exclude_col=None):
    """Build (where_parts, params) from ?f.COL=pattern query args.
    Pattern starting with '=' is treated as pipe-delimited IN filter.
    Otherwise applied as LIKE (supports * wildcard).
    """
    where_parts, params = [], []
    for key, val in args.items():
        if key.startswith('f.') and val.strip():
            col = key[2:]
            if col not in table_cols or col == exclude_col:
                continue
            pat = val.strip()
            if pat.startswith('='):
                vals = [v for v in pat[1:].split('||') if v != '']
                if vals:
                    ph = ','.join('?' * len(vals))
                    where_parts.append(f'"{col}" IN ({ph})')
                    params.extend(vals)
            elif '*' in pat:
                where_parts.append(f'"{col}" LIKE ?')
                params.append(pat.replace('*', '%'))
            else:
                where_parts.append(f'"{col}" LIKE ?')
                params.append(f'%{pat}%')
    return where_parts, params


@app.get('/api/tables/<table>/distinct')
@require_auth
def get_column_distinct(table):
    custname = _session_custname()
    col = request.args.get('col', '').strip()
    if not col:
        return jsonify({'error': 'col param required'}), 400
    with get_db() as conn:
        meta = conn.execute(
            'SELECT 1 FROM _table_meta WHERE table_name=? AND custname=?', (table, custname)
        ).fetchone()
        if not meta:
            return jsonify({'error': 'Table not found'}), 404
        table_cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if col not in table_cols:
            return jsonify({'error': 'Column not found'}), 404
        where_parts, params = _parse_filter_clauses(request.args, table_cols, exclude_col=col)
        where_sql = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
        rows = conn.execute(
            f'SELECT DISTINCT "{col}" FROM "{table}" {where_sql} ORDER BY "{col}" LIMIT 10000',
            params
        ).fetchall()
    return jsonify({'values': ['' if r[0] is None else str(r[0]) for r in rows]})


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
