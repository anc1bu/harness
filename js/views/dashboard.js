// Dashboard view — main data explorer with relationship graph and data table.

import { api } from '../api.js';
import { logout } from '../auth.js';
import { navigate } from '../router.js';
import { getState } from '../state.js';
import { toast } from '../components/modal.js';
import { renderTable } from '../components/table.js';
import { avatarDropdownHtml, initAvatarDropdown } from '../components/avatar.js';


// ── General validation ─────────────────────────────────────────────────────
const _FILENAME_RE = /^([A-Za-z0-9]+)_([A-Za-z0-9]+)_([A-Za-z0-9]+)_(\d+)\.xlsx$/i;

// ── Table type classification ──────────────────────────────────────────────
const _MASTER_TABLES = new Set(['DD03L']);

function _classifyTable(name) {
  const upper = name.toUpperCase();
  if (_MASTER_TABLES.has(upper)) return 'master';
  if (upper.startsWith('DD')) return 'basis';
  return 'customizing';
}

export function mount(container) {
  container.innerHTML = _html();

  _startClock(container);
  _loadTablesMeta(container);
  _initUpload(container);

  const user     = getState('user');
  const customer = getState('customer');

  const custLabel = container.querySelector('#customer-badge');
  if (customer) custLabel.textContent = `${customer.custname} — ${customer.name}`;

  const menuItems = [
    ...(user?.is_admin ? [{ label: 'Admin Page', action: () => navigate('#/admin') }] : []),
    { label: 'Logout', action: async () => { await logout(); navigate('#/login'); }, danger: true },
  ];
  initAvatarDropdown(container, menuItems);

}

// ── HTML ───────────────────────────────────────────────────────────────────

function _tableSection(id, label, tbodyId) {
  return `
    <div id="${id}">
      <div class="ctrl-label" style="padding:10px 12px 6px;">${label}</div>
      <table class="meta-table">
        <thead>
          <tr>
            <th>Table</th>
            <th>System</th>
            <th>Client</th>
            <th>Date</th>
            <th>Entry</th>
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
        <div class="cust-badge" id="customer-badge"></div>
        <div class="clock" id="clock">--:--:--</div>
        ${avatarDropdownHtml()}
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

          ${_tableSection('custom-tables-wrap', 'Customizing Tables', 'custom-meta-tbody')}
          ${_tableSection('basis-tables-wrap', 'Basis Tables', 'basis-meta-tbody')}

        </div>
      </div>

      <div class="panel" id="panel-table">
        <div class="panel-header" style="justify-content:space-between;">
          <div style="display:flex;align-items:center;gap:8px;"><div class="ph-dot"></div>DATA TABLE</div>
          <span id="table-name-label" style="color:var(--accent2);font-size:10px;letter-spacing:2px;"></span>
        </div>
        <div class="panel-body" style="padding:0;">
          <div class="empty-state" id="table-empty" style="height:100%">
            <div class="es-icon" style="font-size:24px">▤</div>
            <div>No data loaded</div>
          </div>
          <div id="table-wrap" style="display:none;"></div>
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
  const basisTbody  = container.querySelector('#basis-meta-tbody');
  const customTbody = container.querySelector('#custom-meta-tbody');

  const basisTables  = tables.filter(t => ['master', 'basis'].includes(_classifyTable(t.orig_table)));
  const customTables = tables.filter(t => _classifyTable(t.orig_table) === 'customizing');

  _fillBasisTbody(basisTbody, basisTables, container);
  _fillTbody(customTbody, customTables, 'No customizing tables', container);
}

function _rowHtml(t) {
  return `
    <tr class="mt-row" data-table="${t.table}" data-orig-table="${t.orig_table}" style="cursor:pointer;">
      <td class="mt-name">${t.orig_table}</td>
      <td>${t.system}</td>
      <td>${t.client}</td>
      <td>${t.date}</td>
      <td>${t.count}</td>
      <td><button class="btn danger mt-del" data-table="${t.table}" style="padding:2px 8px;font-size:10px;">DELETE</button></td>
    </tr>
  `;
}

function _bindTbody(tbody, container) {
  tbody.querySelectorAll('.mt-del').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _deleteTable(container, btn.dataset.table);
    });
  });
  tbody.querySelectorAll('.mt-row').forEach(row => {
    row.addEventListener('click', () => {
      if (container._tableLoading) return;
      _loadTableData(container, row.dataset.table, row.dataset.origTable);
    });
  });
}

function _fillBasisTbody(tbody, tables, container) {
  const hasDd03l = tables.some(t => t.orig_table.toUpperCase() === 'DD03L');
  let html = tables.map(_rowHtml).join('');
  if (!hasDd03l) {
    html += `
      <tr class="mt-dd03l-placeholder">
        <td class="mt-name" style="color:var(--text-dim)">DD03L</td>
        <td style="color:var(--text-dim)">—</td>
        <td style="color:var(--text-dim)">—</td>
        <td style="color:var(--text-dim)">—</td>
        <td style="color:var(--text-dim)">—</td>
        <td style="color:var(--text-dim);font-size:10px;letter-spacing:1px;">NOT UPLOADED</td>
      </tr>
    `;
  }
  tbody.innerHTML = html;
  _bindTbody(tbody, container);
}

function _fillTbody(tbody, tables, emptyMsg, container) {
  if (!tables.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="meta-empty">${emptyMsg}</td></tr>`;
    return;
  }
  tbody.innerHTML = tables.map(_rowHtml).join('');
  _bindTbody(tbody, container);
}

async function _deleteTable(container, table) {
  if (!confirm(`Delete table "${table}"? This cannot be undone.`)) return;
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

function _fmt(bytes) {
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1024)    return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

async function _handleFile(file, container, statusEl) {
  if (!_FILENAME_RE.test(file.name)) {
    toast(
      `Invalid filename: "${file.name}"\n\nExpected: {TABLENAME}_{SYSTEM}_{CLIENT}_{DATE}.xlsx\nExample: DD03L_DO2_100_20240406.xlsx`,
      'err'
    );
    return;
  }

  const [, tableName, system, client, date] = _FILENAME_RE.exec(file.name);

  // Inject pending row immediately so the user sees activity right away.
  // It will be removed if validation fails.
  const { fill, label, entry, row } = _injectPendingRow(container, tableName, system, client, date);
  label.textContent = 'Uploading file…';
  statusEl.textContent = `Validating ${file.name}…`;
  statusEl.className = 'upload-status';

  const formData = new FormData();
  formData.append('file', file);

  try {
    const result = await api.uploadWithProgress('/api/upload', formData, (loaded, total) => {
      const pct = Math.min(99, Math.round((loaded / total) * 100));
      fill.classList.add('determinate');
      fill.style.width = `${pct}%`;
      label.textContent = `${pct}%`;
    });

    // Validations passed — server returned job_id; total_rows counted in background
    fill.classList.remove('determinate');
    label.textContent = 'Counting rows…';
    statusEl.textContent = 'Validations passed — counting rows…';

    _pollJob(result.job_id, container, statusEl, fill, label, entry);
  } catch (err) {
    row.remove();
    await _loadTablesMeta(container);
    statusEl.textContent = '';
    toast(`Upload failed: ${err.message}`, 'err');
  }
}

function _pollJob(jobId, container, statusEl, fill, label, entry) {
  let lastProgress = Date.now();
  let lastInserted = -1;
  const STALE_MS = 5 * 60 * 1000; // 5 minutes without progress → give up

  const interval = setInterval(async () => {
    if (!container.querySelector('#drop-zone')) {
      clearInterval(interval);
      return;
    }
    try {
      const job      = await api.get(`/api/upload/status/${jobId}`);
      const inserted = job.rows_inserted || 0;
      const total    = job.total_rows;

      if (job.status === 'done') {
        clearInterval(interval);
        fill.classList.add('determinate');
        fill.style.width = '100%';
        label.textContent = '100%';
        entry.textContent = inserted.toLocaleString();
        statusEl.textContent = `✓ Done — ${inserted.toLocaleString()} rows inserted into "${job.orig_table}"`;
        statusEl.className = 'upload-status ok';
        document.addEventListener('click', () => { statusEl.textContent = ''; statusEl.className = 'upload-status'; }, { once: true });
        await _loadTablesMeta(container);

      } else if (job.status === 'error') {
        clearInterval(interval);
        await _loadTablesMeta(container);
        statusEl.textContent = '';
        toast(`Upload failed: ${job.error}`, 'err');

      } else if (job.phase === 'validating') {
        statusEl.textContent = 'Validating…';
        label.textContent = 'Validating…';
        lastProgress = Date.now(); // no row-level progress during validation — never time out here

      } else if (!total) {
        // Background thread still counting rows — keep indeterminate bar
        statusEl.textContent = 'Counting rows…';
        label.textContent = 'Counting rows…';

      } else if (job.phase === 'sorting') {
        fill.classList.add('determinate');
        fill.style.width = '100%';
        label.textContent = '100%';
        entry.textContent = `${inserted.toLocaleString()} / ${total ? total.toLocaleString() : '?'}`;
        statusEl.textContent = 'Sorting table…';

      } else if (inserted === 0) {
        fill.classList.add('determinate');
        fill.style.width = '0%';
        label.textContent = '0%';
        entry.textContent = `0 / ${total.toLocaleString()}`;
        statusEl.textContent = `Inserting ${total.toLocaleString()} rows into database…`;
        if (inserted !== lastInserted) { lastInserted = inserted; lastProgress = Date.now(); }

      } else {
        const pct = Math.min(100, Math.round((inserted / total) * 100));
        fill.classList.add('determinate');
        fill.style.width = `${pct}%`;
        label.textContent = `${pct}%`;
        entry.textContent = `${inserted.toLocaleString()} / ${total.toLocaleString()}`;
        statusEl.textContent = `Inserting… ${pct}%`;
        if (inserted !== lastInserted) { lastInserted = inserted; lastProgress = Date.now(); }
        if (Date.now() - lastProgress > STALE_MS) {
          clearInterval(interval);
          statusEl.textContent = '';
          toast('Upload stalled — no progress in 5 minutes.', 'err');
        }
      }

    } catch (err) {
      clearInterval(interval);
      statusEl.textContent = '';
      toast(`Status check failed: ${err.message}`, 'err');
    }
  }, 2000);
}

function _injectPendingRow(container, tableName, system, client, date) {
  const type    = _classifyTable(tableName);
  const tbodyId = type === 'customizing' ? 'custom-meta-tbody' : 'basis-meta-tbody';
  const tbody   = container.querySelector(`#${tbodyId}`);

  // Remove empty placeholder or DD03L placeholder if present
  const emptyRow = tbody.querySelector('.meta-empty');
  if (emptyRow) emptyRow.closest('tr').remove();
  const dd03lPlaceholder = tbody.querySelector('.mt-dd03l-placeholder');
  if (dd03lPlaceholder) dd03lPlaceholder.remove();

  // Remove any existing pending row for this table
  const existing = tbody.querySelector(`tr[data-pending="${tableName}"]`);
  if (existing) existing.remove();

  const tr = document.createElement('tr');
  tr.dataset.pending = tableName;
  tr.innerHTML = `
    <td class="mt-name">${tableName}</td>
    <td>${system}</td>
    <td>${client}</td>
    <td>${date}</td>
    <td class="mt-entry" style="color:var(--text-dim)">—</td>
    <td>
      <div style="display:flex;align-items:center;gap:6px;">
        <div class="upload-progress"><div class="upload-progress-fill"></div></div>
        <span class="upload-progress-label" style="font-size:10px;color:var(--text-dim);white-space:nowrap;">—</span>
      </div>
    </td>
  `;
  tbody.appendChild(tr);

  return {
    fill:  tr.querySelector('.upload-progress-fill'),
    label: tr.querySelector('.upload-progress-label'),
    entry: tr.querySelector('.mt-entry'),
    row:   tr,
  };
}

// ── Table data ─────────────────────────────────────────────────────────────

async function _loadTableData(container, table, origTable) {
  const emptyEl   = container.querySelector('#table-empty');
  const wrapEl    = container.querySelector('#table-wrap');
  const nameLabel = container.querySelector('#table-name-label');

  container._tableLoading = true;
  container.querySelectorAll('.mt-row').forEach(r => r.classList.add('mt-row-disabled'));

  emptyEl.style.display = 'none';
  wrapEl.style.display = '';
  wrapEl.innerHTML = '<div class="tbl-loading"><div class="tbl-spinner"></div><span>Loading…</span></div>';

  try {
    const data = await api.get(`/api/tables/${encodeURIComponent(table)}/data`);

    // V-Show-1: DD04T missing or empty → error, do not show
    if (data.dd04t_missing) {
      toast('pls upload DD04T table with English language', 'err');
      return;
    }

    // V-Show-2: DD04T exists but some descriptions missing → warning, still show
    if (data.partial_descriptions) {
      const fields  = (data.missing_fields || []).slice(0, 5);
      const more    = (data.missing_fields || []).length > 5 ? ` (+${data.missing_fields.length - 5} more)` : '';
      const fieldList = fields.length ? `\nMissing: ${fields.join(', ')}${more}` : '';
      toast(`Some of the descriptions are missing, pls update DD04T${fieldList}`, 'warn');
    }

    if (nameLabel) nameLabel.textContent = origTable || '';

    if (!data.rows || !data.rows.length) {
      emptyEl.style.display = '';
      wrapEl.style.display = 'none';
      return;
    }

    emptyEl.style.display = 'none';
    wrapEl.style.display = '';
    renderTable(wrapEl, { rows: data.rows, columns: data.columns, colTextTables: data.col_text_tables || {} });
  } catch (err) {
    toast(`Failed to load table data: ${err.message}`, 'err');
  } finally {
    container._tableLoading = false;
    container.querySelectorAll('.mt-row').forEach(r => r.classList.remove('mt-row-disabled'));
  }
}
