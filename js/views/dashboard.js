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

function _esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

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
  _initResize();
  _initSectionToggles(container);

  const user     = getState('user');
  const customer = getState('customer');

  const custLabel = container.querySelector('#customer-badge');
  if (customer) custLabel.textContent = `${customer.custname} ${customer.name}`;

  const menuItems = [
    ...(user?.is_admin ? [{ label: 'Admin Page', action: () => navigate('#/admin') }] : []),
    { label: 'Logout', action: async () => { await logout(); navigate('#/login'); }, danger: true },
  ];
  initAvatarDropdown(container, menuItems);

}

// ── HTML ───────────────────────────────────────────────────────────────────

function _tableSection(id, label, tbodyId, section, extraContent = '') {
  return `
    <div id="${id}" class="section-wrap" data-section="${section}">
      <div class="ctrl-label section-hdr" style="padding:10px 12px 6px;">
        <span>${label}</span>
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="section-count"></span>
          <span class="section-chevron">▾</span>
        </div>
      </div>
      <div class="section-body">
        <table class="meta-table">
          <thead>
            <tr>
              <th>Table</th>
              <th>Description</th>
              <th>Date</th>
              <th>Entry</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="${tbodyId}">
            <tr><td colspan="5" class="meta-empty">No tables uploaded</td></tr>
          </tbody>
        </table>
        ${extraContent}
      </div>
    </div>
  `;
}


function _html() {
  return `
    <div id="topbar">
      <div class="logo">HARNESS</div>
      <div style="display:flex;align-items:center;gap:16px">
        <div class="cust-badge" id="customer-badge"></div>
        <div class="clock" id="clock">--:--:--</div>
        ${avatarDropdownHtml()}
      </div>
    </div>
    <div id="layout">
      <div class="panel" id="panel-control">
        <div class="panel-header"><div class="ph-dot"></div>CONTROL PANEL<span id="sys-client-label" style="color:var(--text-dim);font-weight:400;letter-spacing:1px;"></span></div>
        <div class="panel-body" style="padding:0;display:flex;flex-direction:column;gap:0;">
          <div id="panel-resize"></div>

          <div class="drop-zone" id="drop-zone">
            <div class="dz-icon">⬆</div>
            <div class="dz-text">drag &amp; drop or <a class="dz-upload-link" id="upload-link">upload</a> .xlsx here</div>
            <input type="file" id="upload-input" accept=".xlsx" style="display:none" />
          </div>
          <div class="upload-status" id="upload-status"></div>

          ${_tableSection('custom-tables-wrap',     'Customizing Tables', 'custom-meta-tbody',     'customizing')}
          ${_tableSection('secondary-tables-wrap',  'Secondary Tables',   'secondary-meta-tbody',  'secondary')}
          ${_tableSection('basis-tables-wrap',      'Basis Tables',       'basis-meta-tbody',      'basis')}

        </div>
      </div>

      <div class="panel" id="panel-table">
        <div class="panel-header" style="justify-content:space-between;">
          <div style="display:flex;align-items:center;gap:8px;"><div class="ph-dot"></div>DATA TABLE</div>
          <div style="display:flex;align-items:center;gap:10px;min-width:0;">
            <span id="table-name-label" style="color:var(--accent2);font-size:13px;letter-spacing:1.5px;white-space:nowrap;font-weight:600;"></span>
            <span id="table-desc-label" style="color:var(--text-dim);font-size:12px;letter-spacing:0.3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>
          </div>
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
    const [info, assignments, sectionStates, subPanels] = await Promise.all([
      api.get('/api/tables/info'),
      api.get('/api/panel-assignments'),
      api.get('/api/panel-sections'),
      api.get('/api/sub-panels'),
    ]);
    const { tables, system_client: sysClient } = info;
    const label = container.querySelector('#sys-client-label');
    if (label) label.textContent = sysClient ? ` (${sysClient})` : '';
    _renderSubPanelSections(container, subPanels);
    _renderTablesMeta(container, tables, assignments, subPanels);
    _applySectionStates(container, sectionStates);
    _initSectionToggles(container);
    _autoFitControlPanel();
  } catch (err) {
    toast(`Failed to load tables: ${err.message}`, 'err');
  }
}

function _updateSectionCount(container, tbodyId) {
  const tbody = container.querySelector(`#${tbodyId}`);
  if (!tbody) return;
  const wrap  = tbody.closest('.section-wrap');
  if (!wrap) return;
  const count = tbody.querySelectorAll('.mt-row').length;
  const badge = wrap.querySelector('.section-count');
  if (badge) badge.textContent = `${count} table${count !== 1 ? 's' : ''}`;
}

function _applySectionStates(container, states) {
  container.querySelectorAll('.section-wrap').forEach(wrap => {
    const section = wrap.dataset.section;
    wrap.classList.toggle('section-collapsed', !!states[section]);
  });
}

function _initSectionToggles(container) {
  container.querySelectorAll('.section-hdr').forEach(hdr => {
    if (hdr._toggleBound) return;
    hdr._toggleBound = true;
    hdr.addEventListener('click', async () => {
      const wrap      = hdr.closest('.section-wrap');
      const section   = wrap.dataset.section;
      const collapsed = wrap.classList.toggle('section-collapsed');
      try { await api.post('/api/panel-sections', { section, collapsed }); } catch {}
    });
  });
}

function _autoFitControlPanel() {
  const layout = document.getElementById('layout');
  const ctrl   = document.getElementById('panel-control');
  if (!layout || !ctrl) return;
  layout.style.gridTemplateColumns = 'max-content 1fr';
  requestAnimationFrame(() => {
    const w = Math.max(320, Math.min(ctrl.offsetWidth + 2, Math.round(window.innerWidth * 0.6)));
    layout.style.gridTemplateColumns = `${w}px 1fr`;
  });
}

function _initResize() {
  const handle = document.getElementById('panel-resize');
  const layout = document.getElementById('layout');
  if (!handle || !layout) return;

  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = document.getElementById('panel-control').offsetWidth;
    handle.classList.add('resizing');
    document.body.style.cursor    = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = e => {
      const w = Math.max(280, startW + (e.clientX - startX));
      layout.style.gridTemplateColumns = `${w}px 1fr`;
    };
    const onUp = () => {
      handle.classList.remove('resizing');
      document.body.style.cursor    = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function _renderSubPanelSections(container, subPanels) {
  // Remove any previously rendered sub-panel sections
  container.querySelectorAll('.sub-section-wrap[data-sp-id]').forEach(el => el.remove());

  for (const sp of subPanels) {
    const parentWrap = container.querySelector(`.section-wrap[data-section="${sp.parent_panel}"]`);
    if (!parentWrap) continue;
    const parentBody = parentWrap.querySelector(':scope > .section-body');
    if (!parentBody) continue;

    const div = document.createElement('div');
    div.id              = `sp-wrap-${sp.id}`;
    div.className       = 'section-wrap sub-section-wrap';
    div.dataset.section = `sp:${sp.id}`;
    div.dataset.spId    = sp.id;
    div.innerHTML = `
      <div class="ctrl-label section-hdr sub-section-hdr">
        <span>${_esc(sp.name)}</span>
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="section-count"></span>
          <span class="section-chevron">▾</span>
        </div>
      </div>
      <div class="section-body">
        <table class="meta-table">
          <thead><tr><th>Table</th><th>Description</th><th>Date</th><th>Entry</th><th>Action</th></tr></thead>
          <tbody id="sp-tbody-${sp.id}">
            <tr><td colspan="5" class="meta-empty">No tables</td></tr>
          </tbody>
        </table>
      </div>
    `;
    parentBody.appendChild(div);
  }
}

function _renderTablesMeta(container, tables, assignments = {}, subPanels = []) {
  const basisTbody     = container.querySelector('#basis-meta-tbody');
  const customTbody    = container.querySelector('#custom-meta-tbody');
  const secondaryTbody = container.querySelector('#secondary-meta-tbody');

  const basisTables    = tables.filter(t => ['master', 'basis'].includes(_classifyTable(t.orig_table)));
  const customizingAll = tables.filter(t => _classifyTable(t.orig_table) === 'customizing');

  const spIds = new Set(subPanels.map(sp => String(sp.id)));

  const customTables    = customizingAll.filter(t => {
    const p = assignments[t.orig_table] || 'customizing';
    return p === 'customizing' || (p !== 'secondary' && !spIds.has(p));
  });
  const secondaryTables = customizingAll.filter(t => assignments[t.orig_table] === 'secondary');

  _fillBasisTbody(basisTbody, basisTables, container);
  _fillDraggableTbody(customTbody,    customTables,    'customizing', 'No customizing tables', container, subPanels);
  _fillDraggableTbody(secondaryTbody, secondaryTables, 'secondary',   'No secondary tables',   container, subPanels);
  _updateSectionCount(container, 'basis-meta-tbody');
  _updateSectionCount(container, 'secondary-meta-tbody');

  // Fill each sub-panel tbody
  let spTotalUnderCustomizing = 0;
  for (const sp of subPanels) {
    const spTbody = container.querySelector(`#sp-tbody-${sp.id}`);
    if (!spTbody) continue;
    const spTables = customizingAll.filter(t => assignments[t.orig_table] === String(sp.id));
    _fillDraggableTbody(spTbody, spTables, String(sp.id), 'No tables', container, subPanels);
    _updateSectionCount(container, `sp-tbody-${sp.id}`);
    if (sp.parent_panel === 'customizing') spTotalUnderCustomizing += spTables.length;
  }

  // Customizing count includes all nested sub-panel tables
  const customWrap = container.querySelector('#custom-tables-wrap');
  if (customWrap) {
    const total = customTables.length + spTotalUnderCustomizing;
    const badge = customWrap.querySelector(':scope > .section-hdr .section-count');
    if (badge) badge.textContent = `${total} table${total !== 1 ? 's' : ''}`;
  }
}

function _rowHtml(t, draggable = false) {
  return `
    <tr class="mt-row${draggable ? ' mt-draggable' : ''}" data-table="${t.table}" data-orig-table="${t.orig_table}" data-description="${t.description || ''}" style="cursor:pointer;" ${draggable ? 'draggable="true"' : ''}>
      <td class="mt-name">${draggable ? '<span class="mt-drag-handle" title="Drag to move">⠿</span> ' : ''}${t.orig_table}</td>
      <td class="mt-desc-td"><span class="mt-desc">${t.description || ''}</span></td>
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
      _loadTableData(container, row.dataset.table, row.dataset.origTable, row.dataset.description);
    });
  });
}

function _fillDraggableTbody(tbody, tables, panel, emptyMsg, container, subPanels = []) {
  tbody.innerHTML = tables.length
    ? tables.map(t => _rowHtml(t, true)).join('')
    : `<tr><td colspan="5" class="meta-empty">${emptyMsg}</td></tr>`;
  _bindTbody(tbody, container);
  _bindDragDrop(tbody, panel, container, subPanels);
}

function _bindDragDrop(tbody, panel, container, subPanels = []) {
  tbody.querySelectorAll('.mt-draggable').forEach(row => {
    row.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/plain', JSON.stringify({
        origTable: row.dataset.origTable,
        table:     row.dataset.table,
        fromPanel: panel,
      }));
      row.classList.add('mt-dragging');
    });
    row.addEventListener('dragend', () => row.classList.remove('mt-dragging'));
  });

  tbody.addEventListener('dragover', e => {
    e.preventDefault();
    tbody.classList.add('mt-drop-target');
  });
  tbody.addEventListener('dragleave', () => tbody.classList.remove('mt-drop-target'));

  tbody.addEventListener('drop', async e => {
    e.preventDefault();
    tbody.classList.remove('mt-drop-target');
    let data;
    try { data = JSON.parse(e.dataTransfer.getData('text/plain')); } catch { return; }
    if (data.fromPanel === panel) return;

    // Compatibility rules:
    // Sub-panel → its parent panel only
    // Parent panel → any of its sub-panels
    // secondary ↔ customizing only
    const targetSp = subPanels.find(sp => String(sp.id) === panel);
    const sourceSp = subPanels.find(sp => String(sp.id) === data.fromPanel);
    if (targetSp) {
      if (data.fromPanel !== targetSp.parent_panel) return;
    } else if (sourceSp) {
      if (panel !== sourceSp.parent_panel) return;
    } else {
      const staticCompat = { 'customizing': ['secondary'], 'secondary': ['customizing'] };
      if (!staticCompat[panel]?.includes(data.fromPanel)) return;
    }

    try {
      await api.post('/api/panel-assignments', { orig_table: data.origTable, panel });
      await _loadTablesMeta(container);
    } catch (err) {
      toast(`Failed to save panel assignment: ${err.message}`, 'err');
    }
  });
}

const _EXPECTED_BASIS = ['DD03L', 'DD04T', 'DD07T', 'DD02T', 'DD08L'];

function _fillBasisTbody(tbody, tables, container) {
  const uploaded = new Set(tables.map(t => t.orig_table.toUpperCase()));
  let html = tables.map(t => _rowHtml(t)).join('');
  for (const name of _EXPECTED_BASIS) {
    if (!uploaded.has(name)) {
      html += `
        <tr>
          <td class="mt-name" style="color:var(--text-dim)">${name}</td>
          <td style="color:var(--text-dim)">—</td>
          <td style="color:var(--text-dim)">—</td>
          <td style="color:var(--text-dim)">—</td>
          <td style="color:var(--text-dim);font-size:10px;letter-spacing:1px;">NOT UPLOADED</td>
        </tr>
      `;
    }
  }
  tbody.innerHTML = html;
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

function _setPanelLock(container, locked) {
  const panelBody = container.querySelector('#panel-control .panel-body');
  if (!panelBody) return;
  panelBody.classList.toggle('panel-uploading', locked);
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

  _setPanelLock(container, true);

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
    _setPanelLock(container, false);
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
        _setPanelLock(container, false);
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
        _setPanelLock(container, false);
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
          _setPanelLock(container, false);
          statusEl.textContent = '';
          toast('Upload stalled — no progress in 5 minutes.', 'err');
        }
      }

    } catch (err) {
      clearInterval(interval);
      _setPanelLock(container, false);
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

async function _loadTableData(container, table, origTable, description = '') {

  const emptyEl   = container.querySelector('#table-empty');
  const wrapEl    = container.querySelector('#table-wrap');
  const nameLabel = container.querySelector('#table-name-label');

  container._tableLoading = true;
  container.querySelectorAll('.mt-row').forEach(r => r.classList.add('mt-row-disabled'));

  emptyEl.style.display = 'none';
  wrapEl.style.display = '';
  wrapEl.innerHTML = '<div class="tbl-loading"><div class="tbl-spinner"></div><span>Loading…</span></div>';

  const _fetchData = (filters = {}) => {
    const params = new URLSearchParams({ offset: 0, limit: 10000 });
    for (const [col, pat] of Object.entries(filters)) {
      if (pat) params.set(`f.${col}`, pat);
    }
    return api.get(`/api/tables/${encodeURIComponent(table)}/data?${params}`);
  };

  try {
    const data = await _fetchData();

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
      toast(`Some of the header descriptions are missing, pls update DD04T${fieldList}`, 'warn');
    }

    if (nameLabel) nameLabel.textContent = origTable || '';
    const descLabel = container.querySelector('#table-desc-label');
    if (descLabel) descLabel.textContent = description || '';

    if (!data.rows || !data.rows.length) {
      emptyEl.style.display = '';
      wrapEl.style.display = 'none';
      return;
    }

    emptyEl.style.display = 'none';
    wrapEl.style.display = '';
    const onExport = () => {
      const token = localStorage.getItem('token') || '';
      const url = `/api/tables/${encodeURIComponent(table)}/export?token=${encodeURIComponent(token)}`;
      const a = document.createElement('a');
      a.href = url; a.download = `${origTable || table}.csv`; a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    };
    const allLoaded = data.total <= data.rows.length;
    const onFilter = allLoaded ? undefined : (filters) => _fetchData(filters);
    const onDistinct = allLoaded ? undefined : async (rawCol, currentFilters) => {
      const params = new URLSearchParams({ col: rawCol });
      for (const [col, pat] of Object.entries(currentFilters)) {
        if (pat) params.set(`f.${col}`, pat);
      }
      const res = await api.get(`/api/tables/${encodeURIComponent(table)}/distinct?${params}`);
      return { values: res.values || [], labels: res.labels || {} };
    };
    const onSaveColWidths = (widths) => {
      api.patch(`/api/tables/${encodeURIComponent(table)}/col-widths`, widths).catch(() => {});
    };
    const layoutData = await api.get(`/api/tables/${encodeURIComponent(table)}/layout`).catch(() => ({}));
    const onSaveColOrder = (order) => {
      api.patch(`/api/tables/${encodeURIComponent(table)}/layout`, { col_order: order }).catch(() => {});
    };
    const onClearLayout = () => {
      api['delete'](`/api/tables/${encodeURIComponent(table)}/layout`).catch(() => {});
    };
    renderTable(wrapEl, {
      rows: data.rows,
      columns: data.columns,
      rawColumns: data.raw_columns || [],
      colTextTables: data.col_text_tables || {},
      total: data.total,
      onExport,
      onFilter,
      onDistinct,
      colWidths: data.col_widths || {},
      onSaveColWidths,
      colOrder: layoutData.col_order || [],
      onSaveColOrder,
      onClearLayout,
    });

    // For server-side tables: prefetch all column distinct values in the background
    // so filter inputs are never disabled when the user clicks them.
    if (onDistinct && data.raw_columns?.length) {
      setTimeout(async () => {
        for (const rawCol of data.raw_columns) {
          try { await onDistinct(rawCol, {}); } catch {}
        }
      }, 800);
    }
  } catch (err) {
    toast(`Failed to load table data: ${err.message}`, 'err');
  } finally {
    container._tableLoading = false;
    container.querySelectorAll('.mt-row').forEach(r => r.classList.remove('mt-row-disabled'));
  }
}
