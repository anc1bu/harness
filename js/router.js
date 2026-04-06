// Hash-based SPA router.
// Routes map hash strings (e.g. '#/dashboard') to view modules with a mount(container) export.

import { isAuthenticated } from './auth.js';

const routes = {};
const PUBLIC_ROUTES = new Set(['#/login']);
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
    navigate('#/dashboard');
    return;
  }

  const view = routes[hash];
  if (!view) {
    navigate(isAuthenticated() ? '#/dashboard' : '#/login');
    return;
  }

  appEl.innerHTML = '';
  view.mount(appEl);
}
