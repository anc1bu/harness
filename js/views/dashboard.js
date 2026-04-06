// Dashboard view — main data explorer with relationship graph and data table.

import { api } from '../api.js';
import { logout } from '../auth.js';
import { navigate } from '../router.js';
import { getState, setState, subscribe, unsubscribe } from '../state.js';
import { toast } from '../components/modal.js';
import { renderTable } from '../components/table.js';
import { mountGraph, highlightNode } from '../components/graph.js';

export function mount(container) {
  container.innerHTML = _html();

  _startClock(container);
  _loadTables(container);

  container.querySelector('#btn-logout').addEventListener('click', async () => {
    await logout();
    navigate('#/login');
  });
  container.querySelector('#btn-settings').addEventListener('click', () => navigate('#/settings'));

  const onRowsChange = (rows) => _refreshTable(container, rows);
  subscribe('rows', onRowsChange);

  // Cleanup subscriptions when view unmounts (router clears innerHTML)
  const observer = new MutationObserver(() => {
    if (!document.contains(container.querySelector('#panel-graph'))) {
      unsubscribe('rows', onRowsChange);
      observer.disconnect();
    }
  });
  observer.observe(container, { childList: true });
}

// ── Private helpers ────────────────────────────────────────────────────────

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
        <div class="panel-body">
          <div class="ctrl-section">
            <div class="ctrl-label">Table</div>
            <select id="table-select"><option value="">— select —</option></select>
          </div>
          <button class="btn primary" id="btn-load">Load Table</button>
          <div class="ctrl-section" id="stats-section" style="display:none">
            <div class="stat-grid">
              <div class="stat-box"><div class="sv" id="stat-rows">0</div><div class="sl">Rows</div></div>
              <div class="stat-box"><div class="sv" id="stat-cols">0</div><div class="sl">Columns</div></div>
            </div>
          </div>
          <div class="ctrl-section" id="cols-section" style="display:none">
            <div class="ctrl-label">Columns</div>
            <div class="col-list" id="col-list"></div>
          </div>
        </div>
      </div>

      <div class="panel" id="panel-graph">
        <div class="panel-header"><div class="ph-dot"></div>RELATIONSHIP GRAPH</div>
        <div class="panel-body" style="padding:0;position:relative;">
          <div class="empty-state" id="graph-empty">
            <div class="es-icon">◈</div>
            <div>Load a table to begin</div>
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

function _startClock(container) {
  const el = container.querySelector('#clock');
  const tick = () => { el.textContent = new Date().toTimeString().slice(0, 8); };
  tick();
  const id = setInterval(tick, 1000);
  // Stop clock when view unmounts
  const stop = new MutationObserver(() => { if (!document.contains(el)) { clearInterval(id); stop.disconnect(); } });
  stop.observe(container, { childList: true });
}

async function _loadTables(container) {
  try {
    const tables = await api.get('/api/tables');
    const sel = container.querySelector('#table-select');
    tables.forEach(t => {
      const opt = document.createElement('option');
      opt.value = opt.textContent = t;
      sel.appendChild(opt);
    });
  } catch (err) {
    toast(`Failed to load tables: ${err.message}`, 'err');
  }

  container.querySelector('#btn-load').addEventListener('click', () => {
    const table = container.querySelector('#table-select').value;
    if (!table) { toast('Select a table first.', 'warn'); return; }
    _fetchTableData(container, table);
  });
}

async function _fetchTableData(container, table) {
  try {
    const { rows, columns } = await api.get(`/api/tables/${encodeURIComponent(table)}/data`);
    setState('rows', rows);
    setState('columns', columns);
    setState('currentTable', table);
    _refreshStats(container, rows, columns);
    _refreshColumns(container, columns);
    _refreshTable(container, rows);
  } catch (err) {
    toast(`Failed to load "${table}": ${err.message}`, 'err');
  }
}

function _refreshStats(container, rows, columns) {
  container.querySelector('#stat-rows').textContent = rows.length;
  container.querySelector('#stat-cols').textContent = columns.length;
  container.querySelector('#stats-section').style.display = '';
}

function _refreshColumns(container, columns) {
  const list = container.querySelector('#col-list');
  list.innerHTML = columns.map(c => `
    <div class="col-list-item">
      <span class="cli-tech">${c}</span>
    </div>
  `).join('');
  container.querySelector('#cols-section').style.display = '';
}

function _refreshTable(container, rows) {
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
