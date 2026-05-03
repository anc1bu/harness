// Settings view — user customization and database table management.

import { api } from '../api.js';
import { logout } from '../auth.js';
import { navigate } from '../router.js';
import { toast } from '../components/modal.js';

const ALL_SECTIONS = ['Tables', 'Password', 'Users'];
const ADMIN_SECTIONS = new Set(['Users']);

export function mount(container) {
  const user = JSON.parse(localStorage.getItem('user') || 'null');
  const SECTIONS = ALL_SECTIONS.filter(s => !ADMIN_SECTIONS.has(s) || user?.is_admin);

  container.innerHTML = `
    <div id="topbar">
      <div class="logo">HARNESS</div>
      <div style="display:flex;gap:8px">
        <button class="btn inline" id="btn-back" style="margin:0">Back to Dashboard</button>
        <button class="btn inline danger" id="btn-logout" style="margin:0">Logout</button>
      </div>
    </div>
    <div id="settings-view">
      <div class="settings-layout">
        <div class="settings-nav" id="settings-nav">
          ${SECTIONS.map(s => `<div class="snav-item" data-section="${s}">${s}</div>`).join('')}
        </div>
        <div class="settings-content" id="settings-content"></div>
      </div>
    </div>
  `;

  container.querySelector('#btn-back').addEventListener('click', () => navigate('#/dashboard'));
  container.querySelector('#btn-logout').addEventListener('click', async () => {
    await logout();
    navigate('#/login');
  });

  const navItems = container.querySelectorAll('.snav-item');
  navItems.forEach(item => {
    item.addEventListener('click', () => {
      navItems.forEach(n => n.classList.remove('active'));
      item.classList.add('active');
      _renderSection(container, item.dataset.section);
    });
  });

  // Load first section by default
  navItems[0].classList.add('active');
  _renderSection(container, SECTIONS[0]);
}

// ── Section renderers ──────────────────────────────────────────────────────

async function _renderSection(container, section) {
  const content = container.querySelector('#settings-content');
  content.innerHTML = `<div class="settings-section-title">${section}</div><div id="section-body"></div>`;
  const body = content.querySelector('#section-body');

  if (section === 'Tables')   await _renderTables(body);
  if (section === 'Password') await _renderPassword(body);
  if (section === 'Users')    await _renderUsers(body);
}

async function _renderTables(el) {
  try {
    const tables = await api.get('/api/tables');
    if (!tables.length) {
      el.innerHTML = '<div style="color:var(--text-dim);font-size:12px">No tables found.</div>';
      return;
    }
    el.innerHTML = tables.map(t => `
      <div class="col-list-item" style="justify-content:space-between;align-items:center;padding:10px 12px">
        <span class="cli-tech">${t}</span>
        <button class="btn inline danger" data-table="${t}" style="margin:0;padding:4px 10px;font-size:10px">Drop</button>
      </div>
    `).join('');

    el.querySelectorAll('.btn.danger').forEach(btn => {
      btn.addEventListener('click', async () => {
        const table = btn.dataset.table;
        if (!confirm(`Drop table "${table}"? This cannot be undone.`)) return;
        try {
          await api.delete(`/api/tables/${encodeURIComponent(table)}`);
          toast(`Table "${table}" dropped.`, 'warn');
          _renderTables(el);
        } catch (err) {
          toast(err.message, 'err');
        }
      });
    });
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function _renderPassword(el) {
  el.innerHTML = `
    <div class="ctrl-label">Current Password</div>
    <input type="password" id="pw-current" autocomplete="current-password" />
    <div class="ctrl-label">New Password</div>
    <input type="password" id="pw-new" autocomplete="new-password" />
    <div class="ctrl-label">Confirm New Password</div>
    <input type="password" id="pw-confirm" autocomplete="new-password" />
    <button class="btn primary" id="btn-change-pw" style="margin-top:12px">Change Password</button>
  `;
  el.querySelector('#btn-change-pw').addEventListener('click', async () => {
    const current = el.querySelector('#pw-current').value;
    const newPw   = el.querySelector('#pw-new').value;
    const confirm = el.querySelector('#pw-confirm').value;
    if (!current || !newPw || !confirm) { toast('All fields are required.', 'warn'); return; }
    if (newPw !== confirm) { toast('New passwords do not match.', 'warn'); return; }
    try {
      await api.post('/api/auth/change-password', { current_password: current, new_password: newPw });
      toast('Password changed successfully.', 'ok');
      el.querySelectorAll('input').forEach(i => i.value = '');
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

async function _renderUsers(el) {
  try {
    const users = await api.get('/api/users');
    el.innerHTML = `
      <div style="margin-bottom:18px">
        ${users.map(u => `
          <div class="col-list-item" style="padding:10px 12px;gap:10px;flex-wrap:wrap">
            <span class="cli-tech" style="flex:1;min-width:100px">${u.username}</span>
            <input type="password" placeholder="New password" data-user-id="${u.id}"
              class="user-reset-pw" style="width:160px;margin:0" autocomplete="new-password" />
            <button class="btn inline" data-user-id="${u.id}"
              style="margin:0;padding:4px 10px;font-size:10px">Reset Password</button>
          </div>
        `).join('')}
      </div>
      <div class="settings-section-title">Add User</div>
      <div class="ctrl-label">Username</div>
      <input type="text" id="new-username" />
      <div class="ctrl-label">Password</div>
      <input type="password" id="new-password" />
      <button class="btn primary" id="btn-add-user">Add User</button>
    `;

    el.querySelectorAll('.btn.inline[data-user-id]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const uid  = btn.dataset.userId;
        const input = el.querySelector(`.user-reset-pw[data-user-id="${uid}"]`);
        const pw = input.value.trim();
        if (!pw) { toast('Enter a new password first.', 'warn'); return; }
        try {
          await api.patch(`/api/users/${uid}`, { password: pw });
          toast('Password reset.', 'ok');
          input.value = '';
        } catch (err) {
          toast(err.message, 'err');
        }
      });
    });

    el.querySelector('#btn-add-user').addEventListener('click', async () => {
      const username = el.querySelector('#new-username').value.trim();
      const password = el.querySelector('#new-password').value;
      if (!username || !password) { toast('Username and password required.', 'warn'); return; }
      try {
        await api.post('/api/users', { username, password });
        toast(`User "${username}" created.`, 'ok');
        _renderUsers(el);
      } catch (err) {
        toast(err.message, 'err');
      }
    });
  } catch (err) {
    toast(err.message, 'err');
  }
}
