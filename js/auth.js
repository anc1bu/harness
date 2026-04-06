// Session/login logic. Token is stored in localStorage.

import { api } from './api.js';
import { setState } from './state.js';

export function isAuthenticated() {
  return !!localStorage.getItem('token');
}

export async function login(username, password) {
  const { token, user } = await api.post('/api/auth/login', { username, password });
  localStorage.setItem('token', token);
  setState('user', user);
  return user;
}

export async function logout() {
  try { await api.post('/api/auth/logout'); } catch { /* ignore */ }
  localStorage.removeItem('token');
  setState('user', null);
}
