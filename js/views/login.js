// Login view — rendered when the user is unauthenticated.

import { login } from '../auth.js';
import { navigate } from '../router.js';

export function mount(container) {
  container.innerHTML = `
    <div id="login-view">
      <div class="login-box">
        <div class="login-title">Harness <span style="color:var(--accent2)">//</span> Sapcons</div>
        <div class="login-error" id="login-error"></div>
        <div class="ctrl-label">Username</div>
        <input type="text" id="login-username" autocomplete="username" />
        <div class="ctrl-label">Password</div>
        <input type="password" id="login-password" autocomplete="current-password" />
        <button class="btn primary" id="login-btn" style="margin-top:10px">Sign In</button>
      </div>
    </div>
  `;

  const usernameEl = container.querySelector('#login-username');
  const passwordEl = container.querySelector('#login-password');
  const btn        = container.querySelector('#login-btn');
  const errorEl    = container.querySelector('#login-error');

  async function submit() {
    errorEl.textContent = '';
    btn.disabled = true;
    btn.textContent = 'Signing in…';
    try {
      await login(usernameEl.value.trim(), passwordEl.value);
      navigate('#/dashboard');
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
