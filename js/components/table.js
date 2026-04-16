// Data table renderer with per-column Excel-style filtering.

const PREVIEW_LIMIT = 5000;
const MAX_DROPDOWN_VALS = 500;

export function renderTable(wrapEl, { rows, columns, colTextTables = {} }) {
  // Cleanup previous render
  if (wrapEl._filterCleanup) wrapEl._filterCleanup();

  if (!rows.length) {
    wrapEl.innerHTML = '';
    return;
  }

  let cols = columns.length ? [...columns] : Object.keys(rows[0]);

  // ── Unique values per column (all rows, not just preview) ─────────────────
  const uniqueVals = new Map();
  cols.forEach(c => {
    const vals = [...new Set(rows.map(r => String(r[c] ?? '')))].sort();
    uniqueVals.set(c, vals);
  });

  // ── Filter state ──────────────────────────────────────────────────────────
  const activeFilters = new Map(); // col → Set<string>
  let openDropdownCol  = null;
  let _isSearchTyping  = false;

  // ── Build DOM ─────────────────────────────────────────────────────────────
  const container = document.createElement('div');
  container.className = 'tbl-container';

  // ── Export bar ────────────────────────────────────────────────────────────
  const exportBar = document.createElement('div');
  exportBar.className = 'tbl-export-bar';
  exportBar.innerHTML = `
    <span class="tbl-export-count"></span>
    <input type="text" class="tbl-pin-input" placeholder="Pin column…" title="Type exact column name and press Enter to move it first" autocomplete="off" spellcheck="false" />
    <button class="tbl-export-btn" title="Export to Excel">
      <svg width="22" height="22" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
        <rect width="24" height="24" rx="3" fill="#1d6f42"/>
        <rect x="13" y="3" width="8" height="18" rx="1" fill="#21a366"/>
        <rect x="3" y="3" width="11" height="18" rx="1" fill="#107c41"/>
        <line x1="13" y1="3" x2="13" y2="21" stroke="#185c37" stroke-width="0.5"/>
        <line x1="3" y1="9" x2="21" y2="9" stroke="#185c37" stroke-width="0.5" opacity="0.5"/>
        <line x1="3" y1="15" x2="21" y2="15" stroke="#185c37" stroke-width="0.5" opacity="0.5"/>
        <text x="7.5" y="16" font-family="Arial,sans-serif" font-size="11" font-weight="bold" fill="white" text-anchor="middle">X</text>
      </svg>
    </button>
  `;
  container.appendChild(exportBar);

  // Toolbar (visible only when filters are active)
  const toolbar = document.createElement('div');
  toolbar.className = 'tbl-toolbar';
  toolbar.style.display = 'none';
  toolbar.innerHTML = '<button class="tbl-clear-all">✕ CLEAR ALL FILTERS</button>';
  container.appendChild(toolbar);

  // Table
  const table = document.createElement('table');
  const thead  = document.createElement('thead');

  // ── Filter-input row (above header) ───────────────────────────────────────
  const filterTr = document.createElement('tr');
  filterTr.className = 'tbl-filter-row';
  thead.appendChild(filterTr);

  // ── Column-header row ─────────────────────────────────────────────────────
  const headerTr = document.createElement('tr');
  thead.appendChild(headerTr);

  // ── Rebuild column headers (called on init and after pin reorder) ─────────
  function _buildHeaders() {
    filterTr.innerHTML = '';
    headerTr.innerHTML = '';

    cols.forEach(c => {
      const fth = document.createElement('th');
      fth.className = 'tbl-filter-th';
      fth.dataset.col = c;
      const inp = document.createElement('input');
      inp.type        = 'text';
      inp.placeholder = '…';
      inp.className   = 'tbl-filter-input';
      inp.dataset.col = c;
      // Restore active filter text
      const existingFilter = activeFilters.get(c);
      if (existingFilter?.size) inp.value = [...existingFilter].join('|');
      fth.appendChild(inp);
      filterTr.appendChild(fth);

      const hth = document.createElement('th');
      hth.className   = 'tbl-col-header';
      hth.dataset.col = c;
      const colTip = colTextTables[c] ? colTextTables[c].map(_esc).join('&#10;') : _esc(c);
      hth.innerHTML   = `<span class="tbl-col-name" title="${colTip}">${_esc(c)}</span>`
                      + `<button class="tbl-filter-btn" data-col="${_esc(c)}" title="Filter">▾</button>`;
      headerTr.appendChild(hth);
    });

    _bindHeaderEvents();
    requestAnimationFrame(_fixStickyTop);
  }

  function _bindHeaderEvents() {
    filterTr.querySelectorAll('.tbl-filter-input').forEach(inp => {
      const col = inp.dataset.col;
      inp.addEventListener('focus', () => _openDropdown(col, inp.parentElement, inp.value, false));
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { _closeDropdown(); inp.blur(); }
      });
      inp.addEventListener('input', e => {
        const text = e.target.value;
        const matching = uniqueVals.get(col).filter(v => _matchesPattern(v, text));
        if (text && matching.length) activeFilters.set(col, new Set(matching));
        else activeFilters.delete(col);
        _renderRows();
        _openDropdown(col, inp.parentElement, text, false);
      });
    });

    headerTr.querySelectorAll('.tbl-filter-btn').forEach(btn => {
      const col = btn.dataset.col;
      btn.addEventListener('click', e => {
        e.stopPropagation();
        if (openDropdownCol === col) _closeDropdown();
        else {
          const fi = filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`);
          _openDropdown(col, btn.closest('th'), fi?.value || '', true);
        }
      });
    });
  }
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  table.appendChild(tbody);
  container.appendChild(table);

  // ── Floating dropdown (appended to <body> to escape overflow clipping) ────
  const dropdown = document.createElement('div');
  dropdown.className    = 'tbl-filter-dropdown';
  dropdown.style.display = 'none';
  document.body.appendChild(dropdown);

  // ── Sticky header: offset the header row below the filter row ─────────────
  // (runs after element is in the DOM)
  let stickyFrame;
  const _fixStickyTop = () => {
    const h = filterTr.offsetHeight;
    if (h) {
      headerTr.querySelectorAll('th').forEach(th => { th.style.top = `${h}px`; });
    } else {
      stickyFrame = requestAnimationFrame(_fixStickyTop);
    }
  };

  // ── Row rendering ─────────────────────────────────────────────────────────
  function _getFilteredRows() {
    if (!activeFilters.size) return rows;
    return rows.filter(r => {
      for (const [col, vals] of activeFilters) {
        if (vals.size > 0 && !vals.has(String(r[col] ?? ''))) return false;
      }
      return true;
    });
  }

  function _renderRows() {
    const filtered = _getFilteredRows();
    const preview  = filtered.slice(0, PREVIEW_LIMIT);

    let html = preview.map((r, i) => {
      let tr = `<tr data-i="${i}">`;
      cols.forEach(c => { tr += `<td>${_esc(String(r[c] ?? ''))}</td>`; });
      return tr + '</tr>';
    }).join('');

    if (!filtered.length) {
      html = `<tr><td colspan="${cols.length}" style="text-align:center;color:var(--text-dim);padding:16px">No rows match current filters</td></tr>`;
    } else if (filtered.length > PREVIEW_LIMIT) {
      html += `<tr><td colspan="${cols.length}" style="text-align:center;color:var(--text-dim);padding:10px">… ${(filtered.length - PREVIEW_LIMIT).toLocaleString()} more rows (${filtered.length.toLocaleString()} total filtered)</td></tr>`;
    }

    tbody.innerHTML = html;
    toolbar.style.display = activeFilters.size ? '' : 'none';
    exportBar.querySelector('.tbl-export-count').textContent =
      filtered.length.toLocaleString() + ' row' + (filtered.length === 1 ? '' : 's');

    // Update active-filter indicators on headers and filter inputs
    cols.forEach(c => {
      const isActive = (activeFilters.get(c)?.size ?? 0) > 0;
      headerTr.querySelector(`th[data-col="${c}"]`)?.classList.toggle('filter-active', isActive);
      filterTr.querySelector(`th[data-col="${c}"]`)?.classList.toggle('filter-active', isActive);
    });
  }

  // ── Dropdown ──────────────────────────────────────────────────────────────
  function _openDropdown(col, anchorEl, searchText, focusSearch) {
    openDropdownCol = col;
    _renderDropdown(col, anchorEl, searchText, focusSearch);
  }

  function _renderDropdown(col, anchorEl, searchText, focusSearch) {
    const allVals  = uniqueVals.get(col);
    const selected = activeFilters.get(col) || new Set();

    const matchVals  = searchText
      ? allVals.filter(v => _matchesPattern(v, searchText))
      : allVals;
    const visible  = matchVals.slice(0, MAX_DROPDOWN_VALS);
    const hasMore  = matchVals.length > MAX_DROPDOWN_VALS;
    const allChk   = visible.length > 0 && visible.every(v => selected.has(v));
    const someChk  = visible.some(v => selected.has(v));

    const listHtml = visible.length === 0
      ? '<div class="tfd-empty">No matching values</div>'
      : visible.map(v =>
          `<label class="tfd-item"><input type="checkbox" class="tfd-check-val" data-val="${_esc(v)}" ${selected.has(v) ? 'checked' : ''} />`
        + `<span>${_esc(v) || '<em style="color:var(--text-dim)">(empty)</em>'}</span></label>`
        ).join('')
        + (hasMore ? `<div class="tfd-more">… ${(matchVals.length - MAX_DROPDOWN_VALS).toLocaleString()} more — type to narrow</div>` : '');

    const isNewOpen = dropdown.style.display === 'none' || dropdown.dataset.col !== col;

    dropdown.innerHTML = `
      <div class="tfd-search-wrap">
        <input type="text" class="tfd-search" placeholder="Search values…" value="${_esc(searchText)}" />
      </div>
      <div class="tfd-actions">
        <label class="tfd-item tfd-select-all">
          <input type="checkbox" class="tfd-check-all" ${allChk ? 'checked' : ''} />
          <span>Select All</span>
        </label>
        <button class="tfd-clear">Clear</button>
      </div>
      <div class="tfd-list">${listHtml}</div>
    `;

    dropdown.querySelector('.tfd-check-all').indeterminate = someChk && !allChk;
    dropdown.dataset.col = col;

    if (isNewOpen) {
      _positionDropdown(anchorEl);
      dropdown.style.display = '';
    }

    // Focus management
    const searchInput = dropdown.querySelector('.tfd-search');
    if (focusSearch || _isSearchTyping) {
      searchInput.focus();
      searchInput.setSelectionRange(searchText.length, searchText.length);
    }
    _isSearchTyping = false;

    // ── Bind dropdown events ───────────────────────────────────────────────
    searchInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') { _closeDropdown(); return; }
    });

    searchInput.addEventListener('input', e => {
      _isSearchTyping = true;
      const text = e.target.value;
      filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`).value = text;
      // Auto-select all values that match the typed pattern
      const matching = allVals.filter(v => _matchesPattern(v, text));
      if (text && matching.length) activeFilters.set(col, new Set(matching));
      else activeFilters.delete(col);
      _renderRows();
      _renderDropdown(col, anchorEl, text, false);
    });

    dropdown.querySelector('.tfd-check-all').addEventListener('change', e => {
      const text = dropdown.querySelector('.tfd-search').value;
      const vis  = text
        ? allVals.filter(v => _matchesPattern(v, text))
        : allVals;
      const sel  = new Set(activeFilters.get(col) || []);
      if (e.target.checked) vis.forEach(v => sel.add(v));
      else                   vis.forEach(v => sel.delete(v));
      if (sel.size) activeFilters.set(col, sel); else activeFilters.delete(col);
      _renderRows();
      _renderDropdown(col, anchorEl, text, false);
    });

    dropdown.querySelectorAll('.tfd-check-val').forEach(cb => {
      cb.addEventListener('change', e => {
        const val  = e.target.dataset.val;
        const text = dropdown.querySelector('.tfd-search').value;
        const sel  = new Set(activeFilters.get(col) || []);
        if (e.target.checked) sel.add(val); else sel.delete(val);
        if (sel.size) activeFilters.set(col, sel); else activeFilters.delete(col);
        _renderRows();
        _renderDropdown(col, anchorEl, text, false);
      });
    });

    dropdown.querySelector('.tfd-clear').addEventListener('click', () => {
      activeFilters.delete(col);
      filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`).value = '';
      _renderRows();
      _closeDropdown();
    });
  }

  function _positionDropdown(anchorEl) {
    const rect = anchorEl.getBoundingClientRect();
    dropdown.style.left = `${rect.left}px`;
    dropdown.style.top  = `${rect.bottom + 2}px`;
    requestAnimationFrame(() => {
      const dr = dropdown.getBoundingClientRect();
      if (dr.right > window.innerWidth - 4) {
        dropdown.style.left = `${Math.max(4, rect.right - dr.width)}px`;
      }
    });
  }

  function _closeDropdown() {
    dropdown.style.display = 'none';
    openDropdownCol = null;
  }

  // ── Event bindings ────────────────────────────────────────────────────────

  // Pin column input
  exportBar.querySelector('.tbl-pin-input').addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const typed = e.target.value.trim();
    const idx = cols.findIndex(c => c.toLowerCase() === typed.toLowerCase());
    if (idx > 0) {
      const actual = cols[idx];
      cols.splice(idx, 1);
      cols.unshift(actual);
      _closeDropdown();
      _buildHeaders();
      _renderRows();
      e.target.value = '';
      e.target.classList.remove('tbl-pin-error');
    } else if (idx === -1 && typed) {
      e.target.classList.add('tbl-pin-error');
      setTimeout(() => e.target.classList.remove('tbl-pin-error'), 1000);
    }
  });

  // Export to CSV
  exportBar.querySelector('.tbl-export-btn').addEventListener('click', () => {
    const filtered = _getFilteredRows();
    const csv = _toCsv(filtered, cols);
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = 'export.csv';
    a.click();
    URL.revokeObjectURL(url);
  });

  // Clear all filters
  toolbar.querySelector('.tbl-clear-all').addEventListener('click', () => {
    activeFilters.clear();
    filterTr.querySelectorAll('.tbl-filter-input').forEach(i => { i.value = ''; });
    _closeDropdown();
    _renderRows();
  });

  // Close dropdown on outside click
  const _onDocClick = e => {
    if (!dropdown.contains(e.target) && !table.contains(e.target)) _closeDropdown();
  };
  document.addEventListener('click', _onDocClick);

  // ── Scroll position indicator ─────────────────────────────────────────
  const scrollIndicator = document.createElement('div');
  scrollIndicator.className = 'tbl-scroll-indicator';
  scrollIndicator.style.opacity = '0';
  document.body.appendChild(scrollIndicator);

  let _hideScrollTimer = null;
  let _rowHeight = null;

  const _onWrapScroll = () => {
    if (openDropdownCol) _closeDropdown();

    // Estimate row height once from a real rendered row
    if (!_rowHeight) {
      const sampleTr = tbody.querySelector('tr');
      _rowHeight = sampleTr ? sampleTr.offsetHeight || 28 : 28;
    }

    const filtered   = _getFilteredRows();
    const totalShown = Math.min(filtered.length, PREVIEW_LIMIT);
    const theadH     = thead.offsetHeight;
    const scrolled   = Math.max(0, wrapEl.scrollTop - theadH);
    const firstRow   = Math.floor(scrolled / _rowHeight) + 1;
    const lastRow    = Math.min(totalShown, Math.ceil((scrolled + wrapEl.clientHeight) / _rowHeight));

    scrollIndicator.textContent = `${firstRow} / ${totalShown}`;

    // Position: right side of wrapEl, vertically centered in the viewport slice
    const rect = wrapEl.getBoundingClientRect();
    scrollIndicator.style.top  = `${rect.top + rect.height / 2 - 12}px`;
    scrollIndicator.style.left = `${rect.right - 110}px`;
    scrollIndicator.style.opacity = '1';

    clearTimeout(_hideScrollTimer);
    _hideScrollTimer = setTimeout(() => { scrollIndicator.style.opacity = '0'; }, 1200);
  };

  wrapEl.addEventListener('scroll', _onWrapScroll);

  // ── Cleanup ───────────────────────────────────────────────────────────────
  wrapEl._filterCleanup = () => {
    document.removeEventListener('click', _onDocClick);
    wrapEl.removeEventListener('scroll', _onWrapScroll);
    clearTimeout(_hideScrollTimer);
    if (stickyFrame) cancelAnimationFrame(stickyFrame);
    dropdown.remove();
    scrollIndicator.remove();
    delete wrapEl._filterCleanup;
  };

  // ── Initial render ────────────────────────────────────────────────────────
  _buildHeaders();
  _renderRows();
  wrapEl.replaceChildren(container);
}

function _esc(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _matchesPattern(value, pattern) {
  if (!pattern) return true;
  if (pattern.includes('*')) {
    // Wildcard mode: * matches any sequence of characters
    const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&');
    const regexStr = escaped.replace(/\*/g, '.*');
    try { return new RegExp(`^${regexStr}$`, 'i').test(value); } catch { return false; }
  }
  return value.toLowerCase().includes(pattern.toLowerCase());
}

function _toCsv(rows, cols) {
  const esc = v => {
    const s = String(v ?? '');
    return (s.includes(',') || s.includes('"') || s.includes('\n') || s.includes('\r'))
      ? `"${s.replace(/"/g, '""')}"`
      : s;
  };
  const lines = [cols.map(esc).join(',')];
  for (const r of rows) lines.push(cols.map(c => esc(r[c] ?? '')).join(','));
  return lines.join('\r\n');
}
