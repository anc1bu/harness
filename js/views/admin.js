// Admin view — customer and user management (admin-only).

import { api } from '../api.js';
import { logout } from '../auth.js';
import { navigate } from '../router.js';
import { toast } from '../components/modal.js';

const SECTIONS = ['Customers', 'Users'];

export function mount(container) {
  container.innerHTML = `
    <div id="topbar">
      <div class="logo">HARNESS <span>//</span> SAPCONS</div>
      <div style="display:flex;gap:8px">
        <button class="btn inline" id="btn-back" style="margin:0">Back to Dashboard</button>
        <button class="btn inline danger" id="btn-logout" style="margin:0">Logout</button>
      </div>
    </div>
    <div id="settings-view">
      <div class="settings-layout">
        <div class="settings-nav" id="admin-nav">
          ${SECTIONS.map(s => `<div class="snav-item" data-section="${s}">${s}</div>`).join('')}
        </div>
        <div class="settings-content" id="admin-content"></div>
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

  navItems[0].classList.add('active');
  _renderSection(container, SECTIONS[0]);
}

// ── Section dispatcher ─────────────────────────────────────────────────────

async function _renderSection(container, section) {
  const content = container.querySelector('#admin-content');
  content.innerHTML = `<div class="settings-section-title">${section}</div><div id="section-body"></div>`;
  const body = content.querySelector('#section-body');
  if (section === 'Customers') await _renderCustomers(body);
  if (section === 'Users')     await _renderUsers(body);
}

// ── Customers ──────────────────────────────────────────────────────────────

async function _renderCustomers(el) {
  try {
    const customers = await api.get('/api/customers');
    el.innerHTML = `
      <div style="margin-bottom:24px">
        ${customers.length ? `
          <table class="meta-table" style="margin-bottom:12px">
            <thead><tr><th>Code</th><th>Name</th><th>Action</th></tr></thead>
            <tbody>
              ${customers.map(c => `
                <tr>
                  <td class="mt-name">${c.custname}</td>
                  <td>${c.name}</td>
                  <td>
                    <button class="btn danger btn-del-cust" data-custname="${c.custname}"
                      style="padding:2px 8px;font-size:10px;margin:0">Delete</button>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        ` : '<div style="color:var(--text-dim);font-size:12px;margin-bottom:12px">No customers yet.</div>'}
      </div>
      <div class="settings-section-title">Add Customer</div>
      <div class="ctrl-label">Customer Code (3 alphanumeric)</div>
      <input type="text" id="new-custname" maxlength="3" style="text-transform:uppercase" />
      <div class="ctrl-label">Customer Name</div>
      <input type="text" id="new-custname-label" />
      <button class="btn primary" id="btn-add-cust">Add Customer</button>
    `;

    el.querySelectorAll('.btn-del-cust').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm(`Delete customer "${btn.dataset.custname}"?`)) return;
        try {
          await api.delete(`/api/customers/${encodeURIComponent(btn.dataset.custname)}`);
          toast(`Customer "${btn.dataset.custname}" deleted.`, 'warn');
          _renderCustomers(el);
        } catch (err) {
          toast(err.message, 'err');
        }
      });
    });

    el.querySelector('#btn-add-cust').addEventListener('click', async () => {
      const custname = el.querySelector('#new-custname').value.trim().toUpperCase();
      const name     = el.querySelector('#new-custname-label').value.trim();
      if (!custname || !name) { toast('Code and name are required.', 'warn'); return; }
      try {
        await api.post('/api/customers', { custname, name });
        toast(`Customer "${custname}" created.`, 'ok');
        _renderCustomers(el);
      } catch (err) {
        toast(err.message, 'err');
      }
    });
  } catch (err) {
    toast(err.message, 'err');
  }
}

// ── Users ──────────────────────────────────────────────────────────────────

async function _renderUsers(el) {
  try {
    const [users, allCustomers] = await Promise.all([
      api.get('/api/users'),
      api.get('/api/customers'),
    ]);

    el.innerHTML = `
      <table class="meta-table" style="margin-bottom:24px">
        <thead><tr><th>Username</th><th>Admin</th><th>Actions</th></tr></thead>
        <tbody id="users-tbody"></tbody>
      </table>
      <div id="cust-assign-panel" style="display:none">
        <div class="settings-section-title" id="cust-assign-title"></div>
        <div id="cust-assign-list"></div>
      </div>
      <div class="settings-section-title">Add User</div>
      <div class="ctrl-label">Username</div>
      <input type="text" id="new-username" />
      <div class="ctrl-label">Password</div>
      <input type="password" id="new-password" />
      <button class="btn primary" id="btn-add-user">Add User</button>
    `;

    _renderUserRows(el, users, allCustomers);

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

function _renderUserRows(el, users, allCustomers) {
  const tbody = el.querySelector('#users-tbody');
  tbody.innerHTML = users.map(u => `
    <tr data-user-id="${u.id}">
      <td class="mt-name">${u.username}</td>
      <td>
        <label style="display:flex;align-items:center;gap:6px;cursor:${u.username === 'admin' ? 'not-allowed' : 'pointer'}">
          <input type="checkbox" class="chk-admin" data-user-id="${u.id}" data-username="${u.username}"
            ${u.is_admin ? 'checked' : ''} ${u.username === 'admin' ? 'disabled' : ''} />
          <span style="font-size:10px;color:var(--text-dim)">${u.is_admin ? 'Yes' : 'No'}</span>
        </label>
      </td>
      <td>
        <button class="btn inline btn-manage-cust" data-user-id="${u.id}" data-username="${u.username}"
          style="margin:0;padding:2px 10px;font-size:10px">Customers</button>
      </td>
    </tr>
  `).join('');

  tbody.querySelectorAll('.chk-admin').forEach(chk => {
    chk.addEventListener('change', async () => {
      try {
        await api.patch(`/api/users/${chk.dataset.userId}`, { is_admin: chk.checked });
        const label = chk.nextElementSibling;
        if (label) label.textContent = chk.checked ? 'Yes' : 'No';
      } catch (err) {
        toast(err.message, 'err');
        chk.checked = !chk.checked;
      }
    });
  });

  tbody.querySelectorAll('.btn-manage-cust').forEach(btn => {
    btn.addEventListener('click', () =>
      _toggleCustomerPanel(el, btn.dataset.userId, btn.dataset.username, allCustomers)
    );
  });
}

async function _toggleCustomerPanel(el, userId, username, allCustomers) {
  const panel     = el.querySelector('#cust-assign-panel');
  const title     = el.querySelector('#cust-assign-title');
  const listEl    = el.querySelector('#cust-assign-list');
  const isVisible = panel.style.display !== 'none' && panel.dataset.userId === userId;

  if (isVisible) {
    panel.style.display = 'none';
    panel.dataset.userId = '';
    return;
  }

  panel.style.display = '';
  panel.dataset.userId = userId;
  title.textContent = `Customer Assignments — ${username}`;
  listEl.innerHTML = '<div style="color:var(--text-dim);font-size:11px">Loading…</div>';

  try {
    const assigned = await api.get(`/api/users/${userId}/customers`);
    const assignedSet = new Set(assigned.map(c => c.custname));

    if (!allCustomers.length) {
      listEl.innerHTML = '<div style="color:var(--text-dim);font-size:11px">No customers defined. Add customers first.</div>';
      return;
    }

    listEl.innerHTML = allCustomers.map(c => `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer;font-size:11px">
        <input type="checkbox" class="chk-cust" data-custname="${c.custname}" data-user-id="${userId}"
          ${assignedSet.has(c.custname) ? 'checked' : ''} />
        <span style="color:var(--accent2)">${c.custname}</span>
        <span style="color:var(--text-dim)">${c.name}</span>
      </label>
    `).join('');

    listEl.querySelectorAll('.chk-cust').forEach(chk => {
      chk.addEventListener('change', async () => {
        try {
          if (chk.checked) {
            await api.post(`/api/users/${chk.dataset.userId}/customers`, { custname: chk.dataset.custname });
          } else {
            await api.delete(`/api/users/${chk.dataset.userId}/customers/${chk.dataset.custname}`);
          }
        } catch (err) {
          toast(err.message, 'err');
          chk.checked = !chk.checked;
        }
      });
    });
  } catch (err) {
    toast(err.message, 'err');
  }
}
