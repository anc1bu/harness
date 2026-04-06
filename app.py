"""Harness backend — auth + reference/transactional data store."""
from flask import Flask, request, jsonify, send_from_directory, session, redirect, Response, stream_with_context
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import load_workbook
from datetime import datetime, timedelta
from functools import wraps
import sqlite3, json, os, re, secrets

# Map SAP SE16N description headers for DD03L to their technical field names
_DD03L_HEADER_MAP = {
    'Table Name': 'TABNAME', 'Field Name': 'FIELDNAME',
    'Activation State': 'AS4LOCAL', 'Version': 'AS4VERS',
    'Table position': 'POSITION', 'Table position.1': 'DBPOSITION',
    'Key field': 'KEYFLAG', 'Required Field': 'MANDATORY',
    'Data element': 'ROLLNAME', 'Check table': 'CHECKTABLE',
    'Admin. field': 'ADMINFIELD', 'ABAP type': 'INTTYPE',
    'Internal Length': 'INTLEN', 'Reference table': 'REFTABLE',
    'Name of include': 'PRECFIELD', 'Ref. field': 'REFFIELD',
    'Check module': 'CONROUT', 'Force NOT NULL': 'NOTNULL',
    'Data Type': 'DATATYPE', 'No. of Characters': 'LENG',
    'Decimal Places': 'DECIMALS', 'Domain name': 'DOMNAME',
    'Origin': 'SHLPORIGIN', 'Table': 'TABLETYPE', 'Depth': 'DEPTH',
    'Component Type': 'COMPTYPE', 'Type of Object Referenced': 'REFTYPE',
    'Text Lang.': 'LANGUFLAG', 'Anonymous': 'ANONYMOUS',
    'Output': 'OUTPUTSTYLE', 'SRS Identifier': 'SRS_ID',
}

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('HARNESS_DATA', os.path.join(HERE, 'data'))
os.makedirs(os.path.join(DATA, 'reference'), exist_ok=True)
os.makedirs(os.path.join(DATA, 'transactional'), exist_ok=True)

app = Flask(__name__, static_folder=HERE, static_url_path='')
app.secret_key = os.environ.get('HARNESS_SECRET', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200 MB
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Dev bypass: when HARNESS_NO_AUTH=1, skip all auth checks.
NO_AUTH = os.environ.get('HARNESS_NO_AUTH', '').strip() in ('1', 'true', 'yes')
DEV_EMAIL = 'dev@harness.local'

# ───────────── DB ─────────────
def db():
    c = sqlite3.connect(os.path.join(DATA, 'users.db'))
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        pwhash TEXT NOT NULL,
        created_at TEXT NOT NULL)''')
    c.commit(); c.close()
init_db()

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if NO_AUTH:
            return f(*a, **kw)
        if 'uid' not in session:
            return jsonify(error='auth_required'), 401
        return f(*a, **kw)
    return w

# ───────────── AUTH ─────────────
@app.post('/api/auth/signup')
def signup():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    pw = d.get('password') or ''
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify(error='invalid_email'), 400
    if len(pw) < 6:
        return jsonify(error='password_too_short'), 400
    c = db()
    try:
        c.execute('INSERT INTO users (email,pwhash,created_at) VALUES (?,?,?)',
                  (email, generate_password_hash(pw, method='pbkdf2:sha256'), datetime.utcnow().isoformat()))
        c.commit()
    except sqlite3.IntegrityError:
        c.close(); return jsonify(error='email_exists'), 409
    row = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
    c.close()
    session.permanent = True
    session['uid'] = row['id']; session['email'] = email
    return jsonify(ok=True, email=email)

@app.post('/api/auth/login')
def login():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    pw = d.get('password') or ''
    c = db()
    row = c.execute('SELECT id,pwhash FROM users WHERE email=?', (email,)).fetchone()
    c.close()
    if not row or not check_password_hash(row['pwhash'], pw):
        return jsonify(error='bad_credentials'), 401
    session.permanent = True
    session['uid'] = row['id']; session['email'] = email
    return jsonify(ok=True, email=email)

@app.post('/api/auth/logout')
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get('/api/auth/me')
def me():
    if NO_AUTH:
        return jsonify(authed=True, email=DEV_EMAIL, dev=True)
    if 'uid' not in session:
        return jsonify(authed=False)
    return jsonify(authed=True, email=session.get('email'))

# ───────────── STATUS ─────────────
def _read_ref(name):
    p = os.path.join(DATA, 'reference', name)
    if not os.path.exists(p): return None
    with open(p) as f: return json.load(f)

@app.get('/api/status')
@login_required
def status():
    dd03l = _read_ref('dd03l.json')
    dd03l_info = None
    if dd03l:
        dd03l_info = {'loaded': True, 'rows': len(dd03l.get('rows', [])),
                      'uploadedAt': dd03l.get('uploadedAt'), 'filename': dd03l.get('filename'),
                      'tabnames': dd03l.get('tabnames', [])}
    dd04t_info = _dd04t_info()
    dd08l_info = _dd08l_info()
    tables = []
    tdir = os.path.join(DATA, 'transactional')
    for fn in sorted(os.listdir(tdir)):
        if fn.endswith('.json'):
            with open(os.path.join(tdir, fn)) as f:
                d = json.load(f)
            tables.append({'name': fn[:-5], 'rows': len(d.get('rows', [])),
                           'columns': len(d.get('columns', [])),
                           'filename': d.get('filename'),
                           'uploadedAt': d.get('uploadedAt')})
    return jsonify(dd03l=dd03l_info, dd04t=dd04t_info, dd08l=dd08l_info, tables=tables)

# ───────────── UPLOADS ─────────────
def _parse_wb(fileobj):
    wb = load_workbook(fileobj, read_only=True, data_only=True)
    out = []
    for sn in wb.sheetnames:
        ws = wb[sn]
        headers = None
        for r in ws.iter_rows(values_only=True):
            if headers is None:
                headers = [str(x).strip() if x is not None else '' for x in r]
                continue
            if all(v is None or v == '' for v in r): continue
            out.append({h: ('' if v is None else v) for h, v in zip(headers, r)})
    return out, headers or []

@app.post('/api/upload/dd03l')
@login_required
def up_dd03l():
    f = request.files.get('file')
    if not f: return jsonify(error='no_file'), 400
    rows, _ = _parse_wb(f)
    # Normalize description headers to technical names if needed
    rows = [{_DD03L_HEADER_MAP.get(k, k): v for k, v in r.items()} for r in rows]
    slim = []
    for r in rows:
        tn = str(r.get('TABNAME', '')).strip()
        fn = str(r.get('FIELDNAME', '')).strip()
        if not tn or not fn: continue
        # Store all columns from the uploaded DD03L row
        clean = {k: (str(v).strip() if v is not None else '') for k, v in r.items()}
        slim.append(clean)
    uploaded_tabnames = {r['TABNAME'] for r in slim}
    # Merge: keep rows for tables not in this upload, replace rows for tables that ARE
    existing = _read_ref('dd03l.json')
    if existing:
        kept = [r for r in existing.get('rows', []) if r.get('TABNAME') not in uploaded_tabnames]
        slim = kept + slim
    tabnames = sorted({r['TABNAME'] for r in slim})
    out = {'rows': slim, 'tabnames': tabnames,
           'uploadedAt': datetime.utcnow().isoformat() + 'Z',
           'filename': f.filename}
    with open(os.path.join(DATA, 'reference', 'dd03l.json'), 'w') as o:
        json.dump(out, o)
    _reenrich_all()
    return jsonify(ok=True, rows=len(slim), tabnames=len(tabnames),
                   merged=list(uploaded_tabnames))

DD04T_DB = os.path.join(DATA, 'reference', 'dd04t.sqlite')

@app.post('/api/upload/dd04t')
@login_required
def up_dd04t():
    f = request.files.get('file')
    if not f: return jsonify(error='no_file'), 400
    # Persist the upload to disk first so we can stream progress while parsing.
    tmp_xlsx = os.path.join(DATA, 'reference', f'_dd04t_upload_{secrets.token_hex(4)}.xlsx')
    f.save(tmp_xlsx)
    original_name = f.filename

    def gen():
        tmp_db = DD04T_DB + '.tmp'
        try:
            if os.path.exists(tmp_db): os.remove(tmp_db)
            c = sqlite3.connect(tmp_db)
            c.execute('''CREATE TABLE dd04t (
                rollname TEXT PRIMARY KEY,
                SCRTEXT_M TEXT, SCRTEXT_L TEXT, SCRTEXT_S TEXT,
                DDTEXT TEXT, REPTEXT TEXT)''')
            c.execute('CREATE TABLE dd04t_meta (k TEXT PRIMARY KEY, v TEXT)')
            c.execute('PRAGMA synchronous=OFF')
            c.execute('PRAGMA journal_mode=MEMORY')
            wb = load_workbook(tmp_xlsx, read_only=True, data_only=True)
            eng = {'EN', 'E', 'en', 'e'}
            seen = set(); n = 0; BATCH = 5000; batch = []
            c.execute('BEGIN')
            yield json.dumps({'event': 'start'}) + '\n'
            for sn in wb.sheetnames:
                ws = wb[sn]
                headers = None; idx = {}
                for r in ws.iter_rows(values_only=True):
                    if headers is None:
                        headers = [str(x).strip() if x is not None else '' for x in r]
                        idx = {h: i for i, h in enumerate(headers)}
                        continue
                    if 'ROLLNAME' not in idx or 'DDLANGUAGE' not in idx: break
                    lang_v = r[idx['DDLANGUAGE']]
                    if lang_v is None or str(lang_v).strip() not in eng: continue
                    rn = r[idx['ROLLNAME']]
                    if rn is None: continue
                    rn = str(rn).strip()
                    if not rn or rn in seen: continue
                    seen.add(rn)
                    def cell(key):
                        i = idx.get(key, -1)
                        if i < 0: return ''
                        v = r[i]
                        return '' if v is None else str(v).strip()
                    batch.append((rn, cell('SCRTEXT_M'), cell('SCRTEXT_L'),
                                  cell('SCRTEXT_S'), cell('DDTEXT'), cell('REPTEXT')))
                    n += 1
                    if len(batch) >= BATCH:
                        c.executemany('INSERT OR IGNORE INTO dd04t VALUES (?,?,?,?,?,?)', batch)
                        batch.clear()
                        yield json.dumps({'event': 'progress', 'rows': n}) + '\n'
            if batch:
                c.executemany('INSERT OR IGNORE INTO dd04t VALUES (?,?,?,?,?,?)', batch)
            c.execute('INSERT OR REPLACE INTO dd04t_meta VALUES (?,?)',
                      ('uploadedAt', datetime.utcnow().isoformat() + 'Z'))
            c.execute('INSERT OR REPLACE INTO dd04t_meta VALUES (?,?)', ('filename', original_name))
            c.execute('INSERT OR REPLACE INTO dd04t_meta VALUES (?,?)', ('rows', str(n)))
            c.commit(); c.close()
            os.replace(tmp_db, DD04T_DB)
            _reenrich_all()
            yield json.dumps({'event': 'done', 'ok': True, 'rows': n}) + '\n'
        except Exception as e:
            yield json.dumps({'event': 'error', 'error': str(e)}) + '\n'
        finally:
            try: os.remove(tmp_xlsx)
            except OSError: pass

    return Response(stream_with_context(gen()), mimetype='application/x-ndjson',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})

def _dd04t_info():
    if not os.path.exists(DD04T_DB): return None
    c = sqlite3.connect(DD04T_DB)
    try:
        m = dict(c.execute('SELECT k,v FROM dd04t_meta').fetchall())
    except sqlite3.OperationalError:
        c.close(); return None
    c.close()
    rows = int(m.get('rows', 0))
    if rows == 0: return None
    return {'loaded': True, 'rows': rows,
            'uploadedAt': m.get('uploadedAt'), 'filename': m.get('filename')}

def _dd04t_lookup(rollnames, textfield):
    """Return {rollname: text} for given set of rollnames."""
    if not rollnames or not os.path.exists(DD04T_DB): return {}
    if textfield not in ('SCRTEXT_M', 'SCRTEXT_L', 'SCRTEXT_S', 'DDTEXT', 'REPTEXT'):
        textfield = 'SCRTEXT_M'
    c = sqlite3.connect(DD04T_DB)
    out = {}
    rollnames = list(rollnames)
    # Chunk the IN clause to stay under SQLite's variable limit (999)
    for i in range(0, len(rollnames), 500):
        chunk = rollnames[i:i+500]
        q = f'SELECT rollname,{textfield} FROM dd04t WHERE rollname IN ({",".join("?"*len(chunk))})'
        for rn, txt in c.execute(q, chunk):
            if txt: out[rn] = txt
    c.close()
    return out

# ───────────── ENRICHMENT HELPERS ─────────────
def _enrich_columns(table, columns, textfield='SCRTEXT_M'):
    """Return {fieldname: 'FIELDNAME - description'} for all resolvable columns."""
    dd03l = _read_ref('dd03l.json') or {}
    f2r = {}
    for r in dd03l.get('rows', []):
        if r.get('TABNAME') == table and r.get('FIELDNAME'):
            f2r.setdefault(r['FIELDNAME'], r.get('ROLLNAME', ''))
    needed_rolls = {v for v in f2r.values() if v}
    txts = _dd04t_lookup(needed_rolls, textfield)
    enriched = {}
    for c in columns:
        roll = f2r.get(c, '')
        txt = txts.get(roll, '') if roll else ''
        if txt:
            enriched[c] = f'{c} - {txt}'
    return enriched

def _reenrich_all():
    """Re-run enrichment for all stored transactional tables, updating resolved entries."""
    tdir = os.path.join(DATA, 'transactional')
    for fn in os.listdir(tdir):
        if not fn.endswith('.json'): continue
        p = os.path.join(tdir, fn)
        try:
            with open(p) as f:
                d = json.load(f)
            table = d.get('table', '')
            cols = d.get('columns', [])
            if not table or not cols: continue
            new_enriched = _enrich_columns(table, cols)
            existing = dict(d.get('enriched_columns', {}))
            existing.update(new_enriched)  # overwrite with fresh; keeps blanks not yet resolvable
            d['enriched_columns'] = existing
            with open(p, 'w') as f:
                json.dump(d, f)
        except Exception:
            pass

# ───────────── DD08L ─────────────
def _dd08l_info():
    dd08l = _read_ref('dd08l.json')
    if not dd08l: return None
    rows = dd08l.get('rows', [])
    if not rows: return None
    return {'loaded': True, 'rows': len(rows),
            'uploadedAt': dd08l.get('uploadedAt'), 'filename': dd08l.get('filename')}

def _dd08l_lookup(checktable):
    """Return the text-table TABNAME for a given check table (FRKART=TEXT), or None."""
    dd08l = _read_ref('dd08l.json')
    if not dd08l: return None
    for r in dd08l.get('rows', []):
        if r.get('CHECKTABLE') == checktable and r.get('FRKART') == 'TEXT':
            return r.get('TABNAME', '').strip() or None
    return None

@app.post('/api/upload/dd08l')
@login_required
def up_dd08l():
    f = request.files.get('file')
    if not f: return jsonify(error='no_file'), 400
    rows, _ = _parse_wb(f)
    clean_rows = [{k: (str(v).strip() if v is not None else '') for k, v in r.items()}
                  for r in rows]
    out = {'rows': clean_rows,
           'uploadedAt': datetime.utcnow().isoformat() + 'Z',
           'filename': f.filename}
    with open(os.path.join(DATA, 'reference', 'dd08l.json'), 'w') as o:
        json.dump(out, o)
    return jsonify(ok=True, rows=len(clean_rows))

FNAME_RE = re.compile(r'^([A-Z0-9]+)_([A-Z0-9]+)_(\d+)_(\d{8})\.xlsx?$', re.I)

@app.post('/api/upload/trans')
@login_required
def up_trans():
    f = request.files.get('file')
    if not f: return jsonify(error='no_file'), 400
    m = FNAME_RE.match(f.filename)
    if not m:
        return jsonify(error='bad_filename',
                       hint='Expected TABLE_SYSTEM_CLIENT_YYYYMMDD.xlsx'), 400
    table, system, client, date = (m.group(1).upper(), m.group(2).upper(),
                                   m.group(3), m.group(4))
    try: datetime.strptime(date, '%Y%m%d')
    except ValueError: return jsonify(error='bad_date'), 400
    wb = load_workbook(f, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    headers = None; rows = []
    for r in ws.iter_rows(values_only=True):
        if headers is None:
            headers = [str(x).strip() if x is not None else '' for x in r]
            continue
        if all(v is None or v == '' for v in r): continue
        rows.append({h: ('' if v is None else v) for h, v in zip(headers, r)})
    # Column validation: DD03L must be loaded and must contain this table
    dd03l = _read_ref('dd03l.json')
    if not dd03l:
        return jsonify(error='dd03l_required',
                       hint='DD03L is not loaded. Upload DD03L before uploading any table.'), 400
    tbl_fields = {r['FIELDNAME'] for r in dd03l.get('rows', [])
                  if r.get('TABNAME') == table and r.get('FIELDNAME')}
    if not tbl_fields:
        return jsonify(error='table_not_in_dd03l',
                       hint=f'No entries found in DD03L for table {table}. '
                            f'Upload the DD03L extract containing {table} first.'), 400
    # ≥95% of non-empty column headers must match known field names for this table
    non_empty_headers = [h for h in headers if h]
    matched_headers = [h for h in non_empty_headers if h in tbl_fields]
    unmatched_headers = [h for h in non_empty_headers if h not in tbl_fields]
    if not non_empty_headers or len(matched_headers) / len(non_empty_headers) < 0.95:
        return jsonify(error='non_technical_columns', table=table,
                       matched=len(matched_headers), total=len(non_empty_headers),
                       unmatched_sample=unmatched_headers[:10],
                       hint=f'Column headers must be SAP technical field names. '
                            f'Re-export from SE16N using technical column names.'), 400
    enriched_columns = _enrich_columns(table, headers)
    out = {'table': table, 'system': system, 'client': client, 'date': date,
           'filename': f.filename,
           'uploadedAt': datetime.utcnow().isoformat() + 'Z',
           'columns': headers, 'rows': rows,
           'enriched_columns': enriched_columns}
    with open(os.path.join(DATA, 'transactional', f'{table}.json'), 'w') as o:
        json.dump(out, o)
    return jsonify(ok=True, table=table, rows=len(rows), columns=len(headers),
                   system=system, client=client, date=date)

# ───────────── DATA ─────────────
@app.get('/api/data/<table>')
@login_required
def get_data(table):
    table = table.upper()
    p = os.path.join(DATA, 'transactional', f'{table}.json')
    if not os.path.exists(p): return jsonify(error='not_found'), 404
    with open(p) as f: d = json.load(f)
    enriched = d.get('enriched_columns')
    if enriched is None:
        # Legacy tables (uploaded before auto-enrichment): compute on the fly
        textfield = request.args.get('textfield', 'SCRTEXT_M')
        enriched = _enrich_columns(table, d.get('columns', []), textfield)
    cols = [enriched.get(c, c) for c in d.get('columns', [])]
    rows = [{enriched.get(c, c): r.get(c, '') for c in d.get('columns', [])}
            for r in d.get('rows', [])]
    matched = sum(1 for c in d.get('columns', []) if c in enriched)
    return jsonify(table=table, columns=cols, rows=rows,
                   matched=matched, total=len(d.get('columns', [])),
                   filename=d.get('filename'), uploadedAt=d.get('uploadedAt'),
                   system=d.get('system'), client=d.get('client'), date=d.get('date'))


@app.post('/api/data/<table>/describe')
@login_required
def describe_column(table):
    table = table.upper()
    d_req = request.get_json(silent=True) or {}
    field = str(d_req.get('field', '')).strip().upper()
    if not field:
        return jsonify(error='no_field'), 400

    p = os.path.join(DATA, 'transactional', f'{table}.json')
    if not os.path.exists(p):
        return jsonify(error='not_found'), 404

    # Step 1: look up CHECKTABLE from DD03L
    dd03l = _read_ref('dd03l.json') or {}
    checktable = ''
    for r in dd03l.get('rows', []):
        if r.get('TABNAME') == table and r.get('FIELDNAME') == field:
            checktable = str(r.get('CHECKTABLE', '')).strip()
            break
    if not checktable:
        return jsonify(error='no_checktable',
                       hint=f'No check table defined for {table}.{field} in DD03L'), 400

    # Step 2: find text table from DD08L
    text_table = _dd08l_lookup(checktable)
    if not text_table:
        dd08l = _read_ref('dd08l.json')
        if not dd08l:
            return jsonify(error='dd08l_missing',
                           hint='Upload DD08L to enable value description lookup'), 400
        return jsonify(error='no_text_table',
                       hint=f'No text table (FRKART=TEXT) found in DD08L for check table {checktable}'), 400

    # Step 3: load text table rows (warn if not uploaded yet)
    tt_path = os.path.join(DATA, 'transactional', f'{text_table}.json')
    warning = None
    values = {}
    if not os.path.exists(tt_path):
        warning = f'Upload {text_table} as a transactional table to get descriptions for {field}'
    else:
        with open(tt_path) as f:
            tt_data = json.load(f)
        tt_cols = tt_data.get('columns', [])
        has_spras = 'SPRAS' in tt_cols
        for row in tt_data.get('rows', []):
            if has_spras:
                lang = str(row.get('SPRAS', '')).strip().upper()
                if lang not in ('EN', 'E'):
                    continue
            val = str(row.get(field, '')).strip()
            vtext = str(row.get('VTEXT', '')).strip()
            if val and vtext:
                values[val] = vtext

    return jsonify(field=field,
                   description_column=f'{field} - Description',
                   checktable=checktable,
                   text_table=text_table,
                   values=values,
                   warning=warning)

@app.delete('/api/data/<table>')
@login_required
def del_data(table):
    table = table.upper()
    p = os.path.join(DATA, 'transactional', f'{table}.json')
    if os.path.exists(p): os.remove(p)
    return jsonify(ok=True)

# ───────────── STATIC ─────────────
@app.get('/')
def root():
    if not NO_AUTH and 'uid' not in session:
        return redirect('/login')
    return send_from_directory(HERE, 'index.html')

@app.get('/login')
def login_page():
    if NO_AUTH:
        return redirect('/')
    return send_from_directory(HERE, 'login.html')

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
