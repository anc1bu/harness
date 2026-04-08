// Hash-based SPA router.
// Routes map hash strings (e.g. '#/dashboard') to view modules with a mount(container) export.

import { isAuthenticated } from './auth.js';
import { getState } from './state.js';

const routes = {};
const PUBLIC_ROUTES = new Set(['#/login']);
const ADMIN_ROUTES  = new Set(['#/admin']);
let appEl = null;

export function register(hash, viewModule) {
  routes[hash] = viewModule;
}

export function navigate(hash) {
  location.hash = hash;
}

export function init(container) {
  appEl = container;
  window.addEventListener('hashchange', _render);
  _render();
}

async function _render() {
  const hash = location.hash || '#/login';

  if (!isAuthenticated() && !PUBLIC_ROUTES.has(hash)) {
    navigate('#/login');
    return;
  }

  if (isAuthenticated() && hash === '#/login') {
    const user = getState('user');
    const hasCust = !!localStorage.getItem('custname');
    navigate((!hasCust && user?.is_admin) ? '#/admin' : '#/dashboard');
    return;
  }

  // Authenticated but no customer selected — only admin pages are accessible (admins can bypass)
  if (isAuthenticated() && !localStorage.getItem('custname') && !ADMIN_ROUTES.has(hash)) {
    const user = getState('user');
    if (!user?.is_admin) {
      navigate('#/admin');
      return;
    }
  }

  if (ADMIN_ROUTES.has(hash)) {
    const user = getState('user');
    if (!user?.is_admin) {
      navigate('#/dashboard');
      return;
    }
  }

  const view = routes[hash];
  if (!view) {
    navigate(isAuthenticated() ? '#/dashboard' : '#/login');
    return;
  }

  appEl.innerHTML = '';
  view.mount(appEl);
}
