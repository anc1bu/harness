// Single fetch wrapper for all backend calls.
// Attaches auth token and normalizes errors into thrown Error objects.

import { toast } from './components/modal.js';
import { setState } from './state.js';

const BASE = '';

function _handleUnauthorized() {
  localStorage.removeItem('token');
  localStorage.removeItem('custname');
  localStorage.removeItem('custname_label');
  localStorage.removeItem('user');
  setState('user', null);
  setState('customer', null);
  toast('Session expired. Please log in again.', 'err');
  document.getElementById('modal-ok').addEventListener('click', () => {
    location.hash = '#/login';
  }, { once: true });
  return null;
}

async function request(method, path, body) {
  const token = localStorage.getItem('token');
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(BASE + path, {
    method,
    headers,
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  // Only trigger session expiry if we actually sent a token (not a failed login)
  if (res.status === 401 && token) return _handleUnauthorized();

  if (!res.ok) {
    const text = await res.text();
    let message;
    try { message = JSON.parse(text).error || text; } catch { message = `Server error (HTTP ${res.status})`; }
    throw new Error(message || `HTTP ${res.status}`);
  }

  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

async function upload(path, formData) {
  const token = localStorage.getItem('token');
  const headers = {};
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(BASE + path, { method: 'POST', headers, body: formData });

  if (res.status === 401 && token) return _handleUnauthorized();

  if (!res.ok) {
    const text = await res.text();
    let message;
    try { message = JSON.parse(text).error || text; } catch { message = `Server error (HTTP ${res.status})`; }
    throw new Error(message || `HTTP ${res.status}`);
  }

  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function uploadWithProgress(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr  = new XMLHttpRequest();
    const token = localStorage.getItem('token');

    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total);
    });

    xhr.addEventListener('load', () => {
      if (xhr.status === 401 && token) { _handleUnauthorized(); resolve(null); return; }
      if (xhr.status < 200 || xhr.status >= 300) {
        let msg;
        try { msg = JSON.parse(xhr.responseText).error || xhr.responseText; }
        catch { msg = `Server error (HTTP ${xhr.status})`; }
        reject(new Error(msg || `HTTP ${xhr.status}`));
        return;
      }
      try { resolve(xhr.responseText ? JSON.parse(xhr.responseText) : null); }
      catch { resolve(null); }
    });

    xhr.addEventListener('error', () => reject(new Error('Network error')));
    xhr.addEventListener('abort', () => reject(new Error('Upload aborted')));

    xhr.open('POST', BASE + path);
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    xhr.send(formData);
  });
}

export const api = {
  get:                (path)                        => request('GET',    path),
  post:               (path, body)                  => request('POST',   path, body),
  patch:              (path, body)                  => request('PATCH',  path, body),
  delete:             (path)                        => request('DELETE', path),
  upload:             (path, formData)              => upload(path, formData),
  uploadWithProgress: (path, formData, onProgress)  => uploadWithProgress(path, formData, onProgress),
};
