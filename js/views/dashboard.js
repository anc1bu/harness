// Dashboard view — main data explorer with relationship graph and data table.

import { api } from '../api.js';
import { logout } from '../auth.js';
import { navigate } from '../router.js';
import { getState, subscribe, unsubscribe } from '../state.js';
import { toast } from '../components/modal.js';
import { renderTable } from '../components/table.js';

// ── General validation ─────────────────────────────────────────────────────
const _FILENAME_RE = /^([A-Za-z0-9]+)_([A-Za-z0-9]+)_([A-Za-z0-9]+)_(\d+)\.xlsx$/i;

// ── Table type classification ──────────────────────────────────────────────
const _MASTER_TABLES = new Set(['DD03L']);

function _classifyTable(name) {
  const upper = name.toUpperCase();
  if (_MASTER_TABLES.has(upper)) return 'master';
  if (upper.startsWith('DD')) return 'configuration';
  return 'customizing';
}

export function mount(container) {
  container.innerHTML = _html();

  _startClock(container);
  _loadTablesMeta(container);
  _initUpload(container);

  container.querySelector('#btn-logout').addEventListener('click', async () => {
    await logout();
    navigate('#/login');
  });
  container.querySelector('#btn-settings').addEventListener('click', () => navigate('#/settings'));

  const onRowsChange = (rows) => _refreshDataTable(container, rows);
  subscribe('rows', onRowsChange);

  const observer = new MutationObserver(() => {
    if (!document.contains(container.querySelector('#panel-graph'))) {
      unsubscribe('rows', onRowsChange);
      observer.disconnect();
    }
  });
  observer.observe(container, { childList: true });
}

// ── HTML ───────────────────────────────────────────────────────────────────

function _tableSection(id, label, tbodyId) {
  return `
    <div id="${id}">
      <div class="ctrl-label" style="padding:10px 12px 6px;">${label}</div>
      <table class="meta-table">
        <thead>
          <tr>
            <th>Table Name</th>
            <th>System</th>
            <th>Client</th>
            <th>Date</th>
            <th>Entry Count</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="${tbodyId}">
          <tr><td colspan="6" class="meta-empty">No tables uploaded</td></tr>
        </tbody>
      </table>
    </div>
  `;
}

function _html() {
  return `
    <div id="topbar">
      <div class="logo">HARNESS <span>//</span> SAPCONS</div>
      <div style="display:flex;align-items:center;gap:16px">
        <div class="clock" id="clock">--:--:--</div>
        <div class="topbar-nav">
          <button class="btn inline" id="btn-settings">Settings</button>
          <button class="btn inline danger" id="btn-logout">Logout</button>
        </div>
      </div>
    </div>
    <div id="layout">
      <div class="panel" id="panel-control">
        <div class="panel-header"><div class="ph-dot"></div>CONTROL PANEL</div>
        <div class="panel-body" style="padding:0;display:flex;flex-direction:column;gap:0;">

          <div class="drop-zone" id="drop-zone">
            <div class="dz-icon">⬆</div>
            <div class="dz-text">drag &amp; drop or <a class="dz-upload-link" id="upload-link">upload</a> .xlsx here</div>
            <input type="file" id="upload-input" accept=".xlsx" style="display:none" />
          </div>
          <div class="upload-status" id="upload-status"></div>

          ${_tableSection('config-tables-wrap', 'Configuration Tables', 'config-meta-tbody')}
          ${_tableSection('custom-tables-wrap', 'Customizing Tables', 'custom-meta-tbody')}

        </div>
      </div>

      <div class="panel" id="panel-graph">
        <div class="panel-header"><div class="ph-dot"></div>RELATIONSHIP GRAPH</div>
        <div class="panel-body" style="padding:0;position:relative;">
          <div class="empty-state" id="graph-empty">
            <div class="es-icon">◈</div>
            <div>Upload a table to begin</div>
          </div>
          <svg id="graph-svg" style="display:none"></svg>
        </div>
      </div>

      <div class="panel" id="panel-table">
        <div class="panel-header"><div class="ph-dot"></div>DATA TABLE</div>
        <div class="panel-body" style="padding:0;">
          <div class="empty-state" id="table-empty" style="height:100%">
            <div class="es-icon" style="font-size:24px">▤</div>
            <div>No data loaded</div>
          </div>
          <div id="table-wrap" style="display:none;height:100%;overflow:auto;"></div>
        </div>
      </div>
    </div>
    <div id="tooltip">
      <div class="tt-title" id="tt-title"></div>
      <div id="tt-body"></div>
    </div>
  `;
}

// ── Clock ──────────────────────────────────────────────────────────────────

function _startClock(container) {
  const el = container.querySelector('#clock');
  const tick = () => { el.textContent = new Date().toTimeString().slice(0, 8); };
  tick();
  const id = setInterval(tick, 1000);
  const stop = new MutationObserver(() => { if (!document.contains(el)) { clearInterval(id); stop.disconnect(); } });
  stop.observe(container, { childList: true });
}

// ── Tables metadata list ───────────────────────────────────────────────────

async function _loadTablesMeta(container) {
  try {
    const tables = await api.get('/api/tables/info');
    _renderTablesMeta(container, tables);
  } catch (err) {
    toast(`Failed to load tables: ${err.message}`, 'err');
  }
}

function _renderTablesMeta(container, tables) {
  const configTbody = container.querySelector('#config-meta-tbody');
  const customTbody = container.querySelector('#custom-meta-tbody');

  const configTables = tables.filter(t => ['master', 'configuration'].includes(_classifyTable(t.table)));
  const customTables  = tables.filter(t => _classifyTable(t.table) === 'customizing');

  _fillTbody(configTbody, configTables, 'No configuration tables', container);
  _fillTbody(customTbody, customTables, 'No customizing tables', container);
}

function _fillTbody(tbody, tables, emptyMsg, container) {
  if (!tables.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="meta-empty">${emptyMsg}</td></tr>`;
    return;
  }
  tbody.innerHTML = tables.map(t => `
    <tr>
      <td class="mt-name">${t.table}</td>
      <td>${t.system}</td>
      <td>${t.client}</td>
      <td>${t.date}</td>
      <td>${t.count}</td>
      <td><button class="btn danger mt-del" data-table="${t.table}" style="padding:2px 8px;font-size:10px;">DELETE</button></td>
    </tr>
  `).join('');

  tbody.querySelectorAll('.mt-del').forEach(btn => {
    btn.addEventListener('click', () => _deleteTable(container, btn.dataset.table));
  });
}

async function _deleteTable(container, table) {
  try {
    await api.delete(`/api/tables/${encodeURIComponent(table)}`);
    await _loadTablesMeta(container);
  } catch (err) {
    toast(`Delete failed: ${err.message}`, 'err');
  }
}

// ── Upload (drag & drop + link) ────────────────────────────────────────────

function _initUpload(container) {
  const zone     = container.querySelector('#drop-zone');
  const input    = container.querySelector('#upload-input');
  const link     = container.querySelector('#upload-link');
  const statusEl = container.querySelector('#upload-status');

  // Upload link triggers file picker
  link.addEventListener('click', (e) => { e.preventDefault(); input.click(); });

  // Drag events
  zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('dz-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dz-over'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dz-over');
    const file = e.dataTransfer.files[0];
    if (file) _handleFile(file, container, statusEl);
  });

  // File picker selection
  input.addEventListener('change', () => {
    const file = input.files[0];
    input.value = '';
    if (file) _handleFile(file, container, statusEl);
  });
}

async function _handleFile(file, container, statusEl) {
  // General validation
  if (!_FILENAME_RE.test(file.name)) {
    toast(
      `Invalid filename: "${file.name}"\n\nExpected: {TABLENAME}_{SYSTEM}_{CLIENT}_{DATE}.xlsx\nExample: DD03L_DO2_100_20240406.xlsx`,
      'err'
    );
    return;
  }

  statusEl.textContent = `Uploading ${file.name}…`;
  statusEl.className = 'upload-status';

  const formData = new FormData();
  formData.append('file', file);

  try {
    const result = await api.upload('/api/upload', formData);
    statusEl.textContent = `✓ Inserted ${result.rows_inserted} rows into "${result.table}"`;
    statusEl.className = 'upload-status ok';
    const _clearStatus = () => { statusEl.textContent = ''; statusEl.className = 'upload-status'; document.removeEventListener('click', _clearStatus); };
    document.addEventListener('click', _clearStatus);
    await _loadTablesMeta(container);
  } catch (err) {
    statusEl.textContent = '';
    toast(`Upload failed: ${err.message}`, 'err');
  }
}

// ── Right panel data table ─────────────────────────────────────────────────

function _refreshDataTable(container, rows) {
  const emptyEl = container.querySelector('#table-empty');
  const wrapEl  = container.querySelector('#table-wrap');
  if (!rows || !rows.length) {
    emptyEl.style.display = '';
    wrapEl.style.display = 'none';
    return;
  }
  emptyEl.style.display = 'none';
  wrapEl.style.display = '';
  renderTable(wrapEl, { rows, columns: getState('columns') });
}
