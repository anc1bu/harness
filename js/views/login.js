// Login view — credentials + customer selection.

import { login, selectCustomer } from '../auth.js';
import { navigate } from '../router.js';

export function mount(container) {
  container.innerHTML = `
    <div id="login-view">
      <div class="login-box" id="step-creds">
        <div class="login-title">Harness <span style="color:var(--accent2)">//</span> Sapcons</div>
        <div class="login-error" id="login-error"></div>
        <div class="ctrl-label">Username</div>
        <input type="text" id="login-username" autocomplete="username" />
        <div class="ctrl-label">Password</div>
        <input type="password" id="login-password" autocomplete="current-password" />
        <button class="btn primary" id="login-btn" style="margin-top:10px">Sign In</button>
      </div>

      <div class="login-box" id="step-customer" style="display:none">
        <div class="login-title">Select Customer</div>
        <div class="login-error" id="cust-error"></div>
        <div id="customer-list" style="display:flex;flex-direction:column;gap:8px;margin-top:8px"></div>
      </div>
    </div>
  `;

  const credBox  = container.querySelector('#step-creds');
  const custBox  = container.querySelector('#step-customer');
  const errorEl  = container.querySelector('#login-error');
  const btn      = container.querySelector('#login-btn');
  const usernameEl = container.querySelector('#login-username');
  const passwordEl = container.querySelector('#login-password');

  async function submit() {
    errorEl.textContent = '';
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    try {
      const result = await login(usernameEl.value.trim(), passwordEl.value);
      const customers = result.customers;

      if (!customers.length) {
        if (result.user?.is_admin) {
          navigate('#/admin');
          return;
        }
        errorEl.textContent = 'No customers assigned to your account. Contact an admin.';
        btn.disabled = false;
        btn.textContent = 'Sign In';
        return;
      }

      if (customers.length === 1) {
        await selectCustomer(customers[0].custname, customers[0].name);
        navigate('#/dashboard');
        return;
      }

      // Multiple customers — show selection step
      credBox.style.display = 'none';
      custBox.style.display = '';
      _renderCustomerList(custBox, customers);
    } catch (err) {
      errorEl.textContent = err.message;
      btn.disabled = false;
      btn.textContent = 'Sign In';
    }
  }

  btn.addEventListener('click', submit);
  passwordEl.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  usernameEl.focus();
}

function _renderCustomerList(custBox, customers) {
  const listEl   = custBox.querySelector('#customer-list');
  const errorEl  = custBox.querySelector('#cust-error');

  listEl.innerHTML = customers.map(c => `
    <button class="btn" data-custname="${c.custname}" data-name="${c.name}" style="text-align:left;margin-bottom:0">
      <span style="color:var(--accent2)">${c.custname}</span>
      <span style="color:var(--text-dim);margin-left:10px;font-size:10px">${c.name}</span>
    </button>
  `).join('');

  listEl.querySelectorAll('.btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      errorEl.textContent = '';
      btn.disabled = true;
      try {
        await selectCustomer(btn.dataset.custname, btn.dataset.name);
        navigate('#/dashboard');
      } catch (err) {
        errorEl.textContent = err.message;
        btn.disabled = false;
      }
    });
  });
}
