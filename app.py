"""Harness backend — auth + reference/transactional data store."""
from flask import Flask, request, jsonify, send_from_directory, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import load_workbook
from datetime import datetime, timedelta
from functools import wraps
import sqlite3, json, os, re, secrets

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
    dd04t = _read_ref('dd04t.json')
    def info(d, row_key='rows'):
        if not d: return None
        return {'loaded': True,
                'rows': d.get(row_key if isinstance(d.get(row_key), int) else '') or
                        (len(d.get('rows', [])) if isinstance(d.get('rows'), list) else d.get('rows', 0)),
                'uploadedAt': d.get('uploadedAt'),
                'filename': d.get('filename')}
    dd03l_info = None
    if dd03l:
        dd03l_info = {'loaded': True, 'rows': len(dd03l.get('rows', [])),
                      'uploadedAt': dd03l.get('uploadedAt'), 'filename': dd03l.get('filename'),
                      'tabnames': dd03l.get('tabnames', [])}
    dd04t_info = None
    if dd04t:
        dd04t_info = {'loaded': True, 'rows': dd04t.get('rows', 0),
                      'uploadedAt': dd04t.get('uploadedAt'), 'filename': dd04t.get('filename')}
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
    return jsonify(dd03l=dd03l_info, dd04t=dd04t_info, tables=tables)

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
    slim = []
    for r in rows:
        tn = str(r.get('TABNAME', '')).strip()
        fn = str(r.get('FIELDNAME', '')).strip()
        if not tn or not fn: continue
        slim.append({'TABNAME': tn, 'FIELDNAME': fn,
                     'ROLLNAME': str(r.get('ROLLNAME', '')).strip(),
                     'KEYFLAG': str(r.get('KEYFLAG', '')).strip(),
                     'POSITION': r.get('POSITION', '')})
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
    return jsonify(ok=True, rows=len(slim), tabnames=len(tabnames),
                   merged=list(uploaded_tabnames))

@app.post('/api/upload/dd04t')
@login_required
def up_dd04t():
    f = request.files.get('file')
    if not f: return jsonify(error='no_file'), 400
    wb = load_workbook(f, read_only=True, data_only=True)
    lookup = {}
    eng = {'EN', 'E', 'en', 'e'}
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
            if not rn or rn in lookup: continue
            def cell(key):
                i = idx.get(key, -1)
                if i < 0: return ''
                v = r[i]
                return '' if v is None else str(v).strip()
            lookup[rn] = {'SCRTEXT_M': cell('SCRTEXT_M'), 'SCRTEXT_L': cell('SCRTEXT_L'),
                          'SCRTEXT_S': cell('SCRTEXT_S'), 'DDTEXT': cell('DDTEXT'),
                          'REPTEXT': cell('REPTEXT')}
    out = {'lookup': lookup, 'rows': len(lookup),
           'uploadedAt': datetime.utcnow().isoformat() + 'Z',
           'filename': f.filename}
    with open(os.path.join(DATA, 'reference', 'dd04t.json'), 'w') as o:
        json.dump(out, o)
    return jsonify(ok=True, rows=len(lookup))

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
    dd03l = _read_ref('dd03l.json')
    if not dd03l:
        return jsonify(error='dd03l_missing'), 400
    if table not in dd03l.get('tabnames', []):
        return jsonify(error='table_not_in_dd03l', table=table,
                       hint='Re-upload DD03L with this table'), 400
    wb = load_workbook(f, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    headers = None; rows = []
    for r in ws.iter_rows(values_only=True):
        if headers is None:
            headers = [str(x).strip() if x is not None else '' for x in r]
            continue
        if all(v is None or v == '' for v in r): continue
        rows.append({h: ('' if v is None else v) for h, v in zip(headers, r)})
    out = {'table': table, 'system': system, 'client': client, 'date': date,
           'filename': f.filename,
           'uploadedAt': datetime.utcnow().isoformat() + 'Z',
           'columns': headers, 'rows': rows}
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
    textfield = request.args.get('textfield', 'SCRTEXT_M')
    dd03l = _read_ref('dd03l.json') or {}
    dd04t = _read_ref('dd04t.json') or {}
    f2r = {}
    for r in dd03l.get('rows', []):
        if r.get('TABNAME') == table and r.get('FIELDNAME'):
            f2r.setdefault(r['FIELDNAME'], r.get('ROLLNAME', ''))
    lookup = dd04t.get('lookup', {})
    rename = {}; matched = 0
    for c in d.get('columns', []):
        roll = f2r.get(c, '')
        txt = lookup.get(roll, {}).get(textfield, '') if roll else ''
        if txt:
            rename[c] = f'{c} - {txt}'; matched += 1
        else:
            rename[c] = c
    cols = [rename[c] for c in d.get('columns', [])]
    rows = [{rename[c]: r.get(c, '') for c in d.get('columns', [])}
            for r in d.get('rows', [])]
    return jsonify(table=table, columns=cols, rows=rows,
                   matched=matched, total=len(d.get('columns', [])),
                   filename=d.get('filename'), uploadedAt=d.get('uploadedAt'),
                   system=d.get('system'), client=d.get('client'), date=d.get('date'))

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
