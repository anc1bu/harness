"""
Flask backend for Harness — Sapcons.
Serves the SPA and provides a REST API backed by SQLite.

Run: python server.py
"""

import os
import secrets
import hashlib
import sqlite3
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='.', static_url_path='')
DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'harness.db')


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
            " AND name NOT IN ('users','sessions') ORDER BY name"
        ).fetchall()
    return jsonify([r['name'] for r in rows])


@app.get('/api/tables/<table>/data')
@require_auth
def get_table_data(table):
    with get_db() as conn:
        # Validate table exists and is not a system table
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?"
            " AND name NOT IN ('users','sessions')", (table,)
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
            " AND name NOT IN ('users','sessions')", (table,)
        ).fetchone()
        if not exists:
            return jsonify({'error': 'Table not found'}), 404
        conn.execute(f'DROP TABLE "{table}"')
    return jsonify({'ok': True})


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
