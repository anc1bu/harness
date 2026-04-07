// Session/login logic. Token and customer are stored in localStorage.

import { api } from './api.js';
import { setState } from './state.js';

export function isAuthenticated() {
  const token = localStorage.getItem('token');
  if (!token) return false;
  // Admin users can operate without a customer selected
  try {
    const user = JSON.parse(localStorage.getItem('user') || 'null');
    if (user?.is_admin) return true;
  } catch {}
  return !!localStorage.getItem('custname');
}

export function restoreSession() {
  const userStr  = localStorage.getItem('user');
  const custname = localStorage.getItem('custname');
  const custLabel = localStorage.getItem('custname_label');
  if (userStr)  setState('user', JSON.parse(userStr));
  if (custname) setState('customer', { custname, name: custLabel || custname });
}

export async function login(username, password) {
  const data = await api.post('/api/auth/login', { username, password });
  localStorage.setItem('token', data.token);
  localStorage.setItem('user', JSON.stringify(data.user));
  setState('user', data.user);
  return data; // { token, user, customers }
}

export async function selectCustomer(custname, name) {
  await api.post('/api/auth/select-customer', { custname });
  localStorage.setItem('custname', custname);
  localStorage.setItem('custname_label', name || custname);
  setState('customer', { custname, name: name || custname });
}

export async function logout() {
  try { await api.post('/api/auth/logout'); } catch { /* ignore */ }
  localStorage.removeItem('token');
  localStorage.removeItem('custname');
  localStorage.removeItem('custname_label');
  localStorage.removeItem('user');
  setState('user', null);
  setState('customer', null);
}
