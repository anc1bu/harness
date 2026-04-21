// Data table renderer with per-column Excel-style filtering.

const PREVIEW_LIMIT = 5000;
const MAX_DROPDOWN_VALS = 500;

export function renderTable(wrapEl, { rows: initRows, columns, rawColumns = [], colTextTables = {}, total: initTotal, onExport, onFilter, onDistinct, colWidths = {}, onSaveColWidths, colOrder = [], onSaveColOrder, onClearLayout }) {
  // Cleanup previous render
  if (wrapEl._filterCleanup) wrapEl._filterCleanup();

  if (!initRows.length) {
    wrapEl.innerHTML = '';
    return;
  }

  let cols = columns.length ? [...columns] : Object.keys(initRows[0]);

  // Apply saved column order (keep any new cols not in saved order at the end)
  const _origCols = [...cols];
  if (colOrder.length) {
    const saved = colOrder.filter(c => cols.includes(c));
    const extra = cols.filter(c => !saved.includes(c));
    cols = [...saved, ...extra];
  }

  // ── Mutable row state (server-side filtering replaces these) ──────────────
  let rows        = initRows;
  let serverTotal = initTotal ?? initRows.length;

  // ── Enriched-col → raw-col mapping (for server filter params) ─────────────
  const enrichedToRaw = new Map(columns.map((c, i) => [c, rawColumns[i] ?? c.split(' - ')[0]]));

  // ── Unique values per column (rebuilt after each server fetch) ────────────
  const uniqueVals = new Map();
  function _rebuildUniqueVals() {
    cols.forEach(c => {
      uniqueVals.set(c, [...new Set(rows.map(r => String(r[c] ?? '')))].sort());
    });
  }
  _rebuildUniqueVals();

  // ── Filter state ──────────────────────────────────────────────────────────
  const activeFilters   = new Map(); // col → Set<string>  (client-side, used when no onFilter)
  const activePatterns  = new Map(); // col → string        (server LIKE search)
  const activeCheckboxes = new Map(); // col → Set<string>  (server IN filter)
  const colDistinctCache = new Map(); // rawCol → string[] | null (null = loading)
  let openDropdownCol   = null;
  let _isSearchTyping   = false;
  let _filterTimer      = null;
  let _dropdownTimer    = null;
  let _currentLabelMap  = {}; // updated each time _renderDropdown runs

  // ── Row selection state ───────────────────────────────────────────────────
  const selectedRows = new Set(); // Set of data-i strings (indices into _filteredCache)
  let _dragSelecting = false;
  let _dragSelectVal = true;

  // ── Column drag-reorder state ─────────────────────────────────────────────
  let _dragColSrc    = null;  // source index into cols
  let _dragColDst    = null;  // insertion index
  let _dragColActive = false; // mouse moved enough to be a real drag

  // ── Build filter param object from current active patterns + checkboxes ───
  function _buildCurrentFilters(excludeEnrichedCol = null) {
    const filters = {};
    for (const [ec, pat] of activePatterns) {
      if (ec !== excludeEnrichedCol && pat) {
        filters[enrichedToRaw.get(ec) ?? ec.split(' - ')[0]] = pat;
      }
    }
    for (const [ec, vals] of activeCheckboxes) {
      if (ec !== excludeEnrichedCol && vals.size) {
        filters[enrichedToRaw.get(ec) ?? ec.split(' - ')[0]] = '=' + [...vals].join('||');
      }
    }
    return filters;
  }

  // ── Server-side fetch on filter change ────────────────────────────────────
  async function _fetchFiltered() {
    if (!onFilter) { _renderRows(); return; }
    try {
      const data = await onFilter(_buildCurrentFilters());
      rows = data.rows;
      serverTotal = data.total;
      activeFilters.clear();
      _closeDropdown();
    } catch { /* ignore fetch errors during typing */ }
    _renderRows();
  }

  // ── Build DOM ─────────────────────────────────────────────────────────────
  const container = document.createElement('div');
  container.className = 'tbl-container';

  // ── Export bar ────────────────────────────────────────────────────────────
  const exportBar = document.createElement('div');
  exportBar.className = 'tbl-export-bar';
  exportBar.innerHTML = `
    <span class="tbl-export-count"></span>
    <div class="tbl-pin-group">
      <input type="text" class="tbl-pin-input" placeholder="Pin column…" title="Type exact column name and press Enter to move it first" autocomplete="off" spellcheck="false" />
      <button class="tbl-export-btn" title="Export to CSV">
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
      <button class="tbl-clear-all" style="display:none" title="Clear all filters">✕ CLEAR ALL FILTERS</button>
      <button class="tbl-clear-layout" style="display:none" title="Reset column order to default">↺ RESET LAYOUT</button>
    </div>
  `;
  container.appendChild(exportBar);

  const clearAllBtn = exportBar.querySelector('.tbl-clear-all');

  // Table
  const table = document.createElement('table');
  table.style.tableLayout = 'fixed';
  const colgroup = document.createElement('colgroup');
  table.appendChild(colgroup);
  const thead  = document.createElement('thead');

  // ── Filter-input row (above header) ───────────────────────────────────────
  const filterTr = document.createElement('tr');
  filterTr.className = 'tbl-filter-row';
  thead.appendChild(filterTr);

  // ── Column-header row ─────────────────────────────────────────────────────
  const headerTr = document.createElement('tr');
  thead.appendChild(headerTr);

  // ── Selection helpers ─────────────────────────────────────────────────────
  function _applyRowSelection(tr, selected) {
    tr.classList.toggle('tbl-row-selected', selected);
    const cb = tr.querySelector('.tbl-sel-cb');
    if (cb) cb.checked = selected;
  }

  function _updateSelAllCheckbox() {
    const selAll = headerTr.querySelector('.tbl-sel-all');
    if (!selAll) return;
    const total = _filteredCache.length;
    selAll.checked = total > 0 && selectedRows.size >= total;
    selAll.indeterminate = selectedRows.size > 0 && selectedRows.size < total;
  }

  function _updateClearLayoutBtn() {
    const btn = exportBar.querySelector('.tbl-clear-layout');
    if (!btn) return;
    const isReordered = cols.length !== _origCols.length || cols.some((c, i) => c !== _origCols[i]);
    btn.style.display = isReordered ? '' : 'none';
  }

  // ── Rebuild column headers (called on init and after pin reorder) ─────────
  function _buildHeaders() {
    filterTr.innerHTML = '';
    headerTr.innerHTML = '';
    colgroup.innerHTML = '';

    // Checkbox column
    const selCol = document.createElement('col');
    selCol.style.width = '32px';
    colgroup.appendChild(selCol);

    const selFilterTh = document.createElement('th');
    selFilterTh.className = 'tbl-filter-th tbl-sel-th';
    filterTr.appendChild(selFilterTh);

    const selHeaderTh = document.createElement('th');
    selHeaderTh.className = 'tbl-sel-th';
    selHeaderTh.innerHTML = '<input type="checkbox" class="tbl-sel-all" title="Select / deselect all">';
    headerTr.appendChild(selHeaderTh);

    cols.forEach(c => {
      const col = document.createElement('col');
      const saved = colWidths[c];
      if (saved) {
        col.style.width = `${saved}px`;
      } else {
        const sampleMax = rows.slice(0, 30).reduce((m, r) => Math.max(m, String(r[c] ?? '').length), 0);
        const charLen   = Math.max(c.length, sampleMax);
        col.style.width = `${Math.max(80, Math.min(320, charLen * 8))}px`;
      }
      colgroup.appendChild(col);
    });

    cols.forEach(c => {
      const fth = document.createElement('th');
      fth.className = 'tbl-filter-th';
      fth.dataset.col = c;
      const inp = document.createElement('input');
      inp.type        = 'text';
      inp.placeholder = 'search  (Z* = starts with)';
      inp.className   = 'tbl-filter-input';
      inp.dataset.col = c;
      // Restore active filter text
      const existingCheckboxes = activeCheckboxes.get(c);
      const existingPattern    = activePatterns.get(c);
      const existingFilter     = activeFilters.get(c);
      if (existingCheckboxes?.size) inp.value = [...existingCheckboxes].join(' | ');
      else if (existingPattern)     inp.value = existingPattern;
      else if (existingFilter?.size) inp.value = [...existingFilter].join('|');
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
    _fixStickyTop();
    _updateClearLayoutBtn();
  }

  function _bindHeaderEvents() {
    filterTr.querySelectorAll('.tbl-filter-input').forEach(inp => {
      const col = inp.dataset.col;
      inp.addEventListener('focus', () => {
        const rawCol = enrichedToRaw.get(col) ?? col.split(' - ')[0];
        if (onDistinct && !colDistinctCache.has(rawCol)) {
          inp.disabled    = true;
          inp.placeholder = 'Loading…';
          inp.classList.add('tbl-filter-loading');
        }
        _openDropdown(col, inp.parentElement, inp.value, false);
      });
      inp.addEventListener('input', e => {
        const text = e.target.value;
        if (onFilter) {
          activeFilters.delete(col);
          activeCheckboxes.delete(col);
          activePatterns.delete(col);
          clearTimeout(_filterTimer);
        } else {
          activeFilters.delete(col);
        }
        clearTimeout(_dropdownTimer);
        const _doDropdown = () => {
          if (onFilter && text) {
            const rawCol2  = enrichedToRaw.get(col) ?? col.split(' - ')[0];
            const cached2  = colDistinctCache.get(rawCol2);
            if (cached2?.values) {
              const labels2 = cached2.labels || {};
              const matching = cached2.values.filter(v => {
                const lbl  = labels2[v] || '';
                const desc = lbl.includes(' - ') ? lbl.slice(lbl.indexOf(' - ') + 3) : '';
                return _matchesPattern(v, text) || _matchesPattern(lbl, text) || _matchesPattern(desc, text);
              });
              if (matching.length) { activeCheckboxes.set(col, new Set(matching)); }
              else                  { activePatterns.set(col, text); }
            } else {
              activePatterns.set(col, text);
            }
          } else if (!onFilter) {
            const matching = text ? uniqueVals.get(col).filter(v => _matchesPattern(v, text)) : [];
            if (matching.length) activeFilters.set(col, new Set(matching));
            else activeFilters.delete(col);
          }
          _openDropdown(col, inp.parentElement, text, false);
        };
        _dropdownTimer = setTimeout(_doDropdown, 400);
      });
      inp.addEventListener('keydown', e => {
        if (e.key !== 'Enter') return;
        clearTimeout(_filterTimer);
        clearTimeout(_dropdownTimer);
        _closeDropdown();
        inp.blur();
        if (onFilter) {
          const text     = inp.value;
          const rawCol2  = enrichedToRaw.get(col) ?? col.split(' - ')[0];
          const cached2  = colDistinctCache.get(rawCol2);
          if (text && cached2?.values) {
            const labels2 = cached2.labels || {};
            const matching = cached2.values.filter(v => {
              const lbl  = labels2[v] || '';
              const desc = lbl.includes(' - ') ? lbl.slice(lbl.indexOf(' - ') + 3) : '';
              return _matchesPattern(v, text) || _matchesPattern(lbl, text) || _matchesPattern(desc, text);
            });
            if (matching.length) {
              activeCheckboxes.set(col, new Set(matching));
              activePatterns.delete(col);
            }
          }
          _fetchFiltered();
        } else {
          const text = inp.value;
          const matching = text ? uniqueVals.get(col)?.filter(v => _matchesPattern(v, text)) ?? [] : [];
          if (matching.length) activeFilters.set(col, new Set(matching));
          else activeFilters.delete(col);
          _renderRows();
        }
      });
    });

    headerTr.querySelector('.tbl-sel-all')?.addEventListener('change', e => {
      if (e.target.checked) {
        for (let i = 0; i < _filteredCache.length; i++) selectedRows.add(String(i));
      } else {
        selectedRows.clear();
      }
      tbody.querySelectorAll('tr[data-i]').forEach(tr => {
        _applyRowSelection(tr, selectedRows.has(tr.dataset.i));
      });
      _updateSelAllCheckbox();
    });

    headerTr.querySelectorAll('.tbl-col-header').forEach(hth => {
      const col = hth.dataset.col;

      hth.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        if (e.target.closest('.tbl-filter-btn')) return;
        const idx = cols.indexOf(col);
        if (idx === -1) return;
        _dragColSrc    = idx;
        _dragColDst    = idx;
        _dragColActive = false;
        _dragGhost.textContent = col;
        document.body.style.userSelect = 'none';
        document.addEventListener('mousemove', _onColMouseMove);
        document.addEventListener('mouseup',   _onColMouseUp);
      });

      hth.addEventListener('click', e => {
        if (_dragColActive) return; // was a drag, not a click
        if (e.target.closest('.tbl-filter-btn')) return;
        e.stopPropagation();
        if (openDropdownCol === col) _closeDropdown();
        else {
          const fi = filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`);
          _openDropdown(col, hth, fi?.value || '', true);
        }
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

  // ── Row selection events (event delegation on tbody) ──────────────────────
  tbody.addEventListener('mousedown', e => {
    const cb = e.target.closest('.tbl-sel-cb');
    if (!cb) return;
    e.preventDefault(); // prevent text selection during drag
    const tr = cb.closest('tr[data-i]');
    if (!tr) return;
    const idx = tr.dataset.i;
    _dragSelectVal = !selectedRows.has(idx);
    _dragSelecting = true;
    if (_dragSelectVal) selectedRows.add(idx); else selectedRows.delete(idx);
    _applyRowSelection(tr, _dragSelectVal);
    _updateSelAllCheckbox();
  });

  tbody.addEventListener('mouseover', e => {
    if (!_dragSelecting) return;
    const tr = e.target.closest('tr[data-i]');
    if (!tr) return;
    const idx = tr.dataset.i;
    if (_dragSelectVal) selectedRows.add(idx); else selectedRows.delete(idx);
    _applyRowSelection(tr, _dragSelectVal);
    _updateSelAllCheckbox();
  });

  // Prevent browser from toggling checkbox state after mousedown already handled it
  tbody.addEventListener('click', e => {
    if (e.target.closest('.tbl-sel-cb')) e.preventDefault();
  });

  const _onDocMouseUp = () => { _dragSelecting = false; };
  document.addEventListener('mouseup', _onDocMouseUp);

  // ── Column drag-reorder ghost + insertion line ────────────────────────────
  const _dragGhost = document.createElement('div');
  _dragGhost.className    = 'tbl-drag-ghost';
  _dragGhost.style.display = 'none';
  document.body.appendChild(_dragGhost);

  const _dragLine = document.createElement('div');
  _dragLine.className    = 'tbl-drag-line';
  _dragLine.style.display = 'none';
  document.body.appendChild(_dragLine);

  const _onColMouseMove = (e) => {
    _dragGhost.style.left = `${e.clientX + 14}px`;
    _dragGhost.style.top  = `${e.clientY - 14}px`;
    _dragGhost.style.display = '';

    const ths = [...headerTr.querySelectorAll('.tbl-col-header')];
    let dst = ths.length;
    for (let i = 0; i < ths.length; i++) {
      const r = ths[i].getBoundingClientRect();
      if (e.clientX < r.left + r.width / 2) { dst = i; break; }
    }
    _dragColDst = dst;

    const lineRect = dst < ths.length
      ? ths[dst].getBoundingClientRect()
      : ths[ths.length - 1].getBoundingClientRect();
    const lineX = dst < ths.length ? lineRect.left : lineRect.right;
    const hdrRect = thead.getBoundingClientRect();
    _dragLine.style.left   = `${lineX - 1}px`;
    _dragLine.style.top    = `${hdrRect.top}px`;
    _dragLine.style.height = `${thead.offsetHeight}px`;
    _dragLine.style.display = '';

    _dragColActive = true;
  };

  const _onColMouseUp = () => {
    document.removeEventListener('mousemove', _onColMouseMove);
    document.removeEventListener('mouseup',   _onColMouseUp);
    document.body.style.userSelect = '';
    _dragGhost.style.display = 'none';
    _dragLine.style.display  = 'none';

    const src      = _dragColSrc;
    const dst      = _dragColDst;
    const wasActive = _dragColActive;
    _dragColSrc = null;
    _dragColDst = null;

    // Reset _dragColActive after the pending click event fires
    requestAnimationFrame(() => { _dragColActive = false; });

    if (wasActive && dst !== null && dst !== src && dst !== src + 1) {
      const [moved] = cols.splice(src, 1);
      cols.splice(dst > src ? dst - 1 : dst, 0, moved);
      _buildHeaders();
      _renderRows();
      if (onSaveColOrder) onSaveColOrder([...cols]);
    }
  };

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
  const RENDER_BATCH = 200;
  let _filteredCache  = [];
  let _renderedCount  = 0;
  let _batchObserver  = null;

  function _getFilteredRows() {
    if (!activeFilters.size) return rows;
    return rows.filter(r => {
      for (const [col, vals] of activeFilters) {
        if (vals.size > 0 && !vals.has(String(r[col] ?? ''))) return false;
      }
      return true;
    });
  }

  function _disposeBatchObserver() {
    if (_batchObserver) { _batchObserver.disconnect(); _batchObserver = null; }
  }

  function _buildRowsHtml(batch, startIdx) {
    return batch.map((r, i) => {
      const idx = String(startIdx + i);
      const sel = selectedRows.has(idx);
      let tr = `<tr data-i="${idx}"${sel ? ' class="tbl-row-selected"' : ''}>`;
      tr += `<td class="tbl-sel-td"><input type="checkbox" class="tbl-sel-cb"${sel ? ' checked' : ''}></td>`;
      cols.forEach(c => {
        const v = _esc(String(r[c] ?? ''));
        tr += `<td title="${v}">${v}</td>`;
      });
      return tr + '</tr>';
    }).join('');
  }

  function _appendBatch() {
    _disposeBatchObserver();
    const toRender = Math.min(_filteredCache.length, PREVIEW_LIMIT);
    const start    = _renderedCount;
    const end      = Math.min(start + RENDER_BATCH, toRender);

    tbody.querySelector('.tbl-sentinel')?.remove();
    tbody.insertAdjacentHTML('beforeend', _buildRowsHtml(_filteredCache.slice(start, end), start));
    _renderedCount = end;

    if (_renderedCount < toRender) {
      const sentinel = document.createElement('tr');
      sentinel.className = 'tbl-sentinel';
      sentinel.innerHTML = `<td colspan="${cols.length + 1}" style="padding:6px;text-align:center;color:var(--text-dim);font-size:11px">${_renderedCount.toLocaleString()} / ${toRender.toLocaleString()} rows loaded</td>`;
      tbody.appendChild(sentinel);
      _batchObserver = new IntersectionObserver(entries => {
        if (entries[0].isIntersecting) _appendBatch();
      }, { root: wrapEl, rootMargin: '300px' });
      _batchObserver.observe(sentinel);
    } else if (_filteredCache.length > PREVIEW_LIMIT) {
      tbody.insertAdjacentHTML('beforeend',
        `<tr><td colspan="${cols.length + 1}" style="text-align:center;color:var(--text-dim);padding:10px">… ${(_filteredCache.length - PREVIEW_LIMIT).toLocaleString()} more rows (${_filteredCache.length.toLocaleString()} total filtered)</td></tr>`
      );
    }
  }

  function _renderRows() {
    _disposeBatchObserver();
    selectedRows.clear();
    _filteredCache = _getFilteredRows();
    _renderedCount = 0;

    if (!_filteredCache.length) {
      tbody.innerHTML = `<tr><td colspan="${cols.length + 1}" style="text-align:center;color:var(--text-dim);padding:16px">No rows match current filters</td></tr>`;
    } else {
      tbody.innerHTML = _buildRowsHtml(_filteredCache.slice(0, RENDER_BATCH), 0);
      _renderedCount = Math.min(RENDER_BATCH, Math.min(_filteredCache.length, PREVIEW_LIMIT));
      if (_renderedCount < Math.min(_filteredCache.length, PREVIEW_LIMIT)) {
        const sentinel = document.createElement('tr');
        sentinel.className = 'tbl-sentinel';
        sentinel.innerHTML = `<td colspan="${cols.length + 1}" style="padding:6px;text-align:center;color:var(--text-dim);font-size:11px">${_renderedCount.toLocaleString()} / ${Math.min(_filteredCache.length, PREVIEW_LIMIT).toLocaleString()} rows loaded</td>`;
        tbody.appendChild(sentinel);
        _batchObserver = new IntersectionObserver(entries => {
          if (entries[0].isIntersecting) _appendBatch();
        }, { root: wrapEl, rootMargin: '300px' });
        _batchObserver.observe(sentinel);
      } else if (_filteredCache.length > PREVIEW_LIMIT) {
        tbody.insertAdjacentHTML('beforeend',
          `<tr><td colspan="${cols.length + 1}" style="text-align:center;color:var(--text-dim);padding:10px">… ${(_filteredCache.length - PREVIEW_LIMIT).toLocaleString()} more rows (${_filteredCache.length.toLocaleString()} total filtered)</td></tr>`
        );
      }
    }

    clearAllBtn.style.display = (activeFilters.size || activePatterns.size || activeCheckboxes.size) ? '' : 'none';
    const shownCount = _filteredCache.length;
    const countEl    = exportBar.querySelector('.tbl-export-count');
    countEl.textContent = shownCount === serverTotal
      ? shownCount.toLocaleString() + ' row' + (shownCount === 1 ? '' : 's')
      : `${shownCount.toLocaleString()} of ${serverTotal.toLocaleString()} rows`;

    cols.forEach(c => {
      const isActive = (activeFilters.get(c)?.size ?? 0) > 0 || activePatterns.has(c) || (activeCheckboxes.get(c)?.size ?? 0) > 0;
      headerTr.querySelector(`th[data-col="${c}"]`)?.classList.toggle('filter-active', isActive);
      filterTr.querySelector(`th[data-col="${c}"]`)?.classList.toggle('filter-active', isActive);
    });
    _updateSelAllCheckbox();
  }

  // ── Dropdown ──────────────────────────────────────────────────────────────
  function _openDropdown(col, anchorEl, searchText, focusSearch) {
    openDropdownCol = col;
    const rawCol = enrichedToRaw.get(col) ?? col.split(' - ')[0];

    _renderDropdown(col, rawCol, anchorEl, searchText, focusSearch);

    if (onDistinct && !colDistinctCache.has(rawCol)) {
      colDistinctCache.set(rawCol, null); // mark loading
      onDistinct(rawCol, _buildCurrentFilters(col)).then(vals => {
        colDistinctCache.set(rawCol, vals);
        const headerInp = filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`);
        if (headerInp?.disabled) {
          headerInp.disabled    = false;
          headerInp.placeholder = 'search  (Z* = starts with)';
          headerInp.classList.remove('tbl-filter-loading');
          headerInp.focus();
        }
        if (openDropdownCol === col) _renderDropdown(col, rawCol, anchorEl, searchText, focusSearch);
      }).catch(() => {
        colDistinctCache.set(rawCol, []);
        const headerInp = filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`);
        if (headerInp?.disabled) {
          headerInp.disabled    = false;
          headerInp.placeholder = 'search  (Z* = starts with)';
          headerInp.classList.remove('tbl-filter-loading');
        }
      });
    }
  }

  function _renderDropdown(col, rawCol, anchorEl, searchText, focusSearch) {
    // Decide value source: distinct cache (server) or uniqueVals (loaded rows)
    let allVals, labelMap = {};
    if (onDistinct) {
      const cached = colDistinctCache.get(rawCol);
      if (cached === null) {
        // Still loading — show spinner
        const isNewOpen = dropdown.style.display === 'none' || dropdown.dataset.col !== col;
        dropdown.innerHTML = '<div class="tfd-loading">Loading…</div>';
        dropdown.dataset.col = col;
        if (isNewOpen) { _positionDropdown(anchorEl); dropdown.style.display = ''; }
        return;
      }
      allVals  = cached?.values ?? cached ?? [];
      labelMap = cached?.labels ?? {};
    } else {
      allVals = uniqueVals.get(col) ?? [];
    }
    _currentLabelMap = labelMap;

    const selected = onFilter ? (activeCheckboxes.get(col) || new Set()) : (activeFilters.get(col) || new Set());

    const _getDesc = v => { const l = labelMap[v] || ''; const i = l.indexOf(' - '); return i >= 0 ? l.slice(i + 3) : ''; };
    const _matchesLabel = (v, pat) => _matchesPattern(v, pat) || _matchesPattern(labelMap[v] || '', pat) || _matchesPattern(_getDesc(v), pat);
    const matchVals = searchText ? allVals.filter(v => _matchesLabel(v, searchText)) : allVals;
    const visible   = matchVals.slice(0, MAX_DROPDOWN_VALS);
    const hasMore   = matchVals.length > MAX_DROPDOWN_VALS;
    const allChk    = visible.length > 0 && visible.every(v => selected.has(v));
    const someChk   = visible.some(v => selected.has(v));

    const listHtml = visible.length === 0
      ? '<div class="tfd-empty">No matching values</div>'
      : visible.map(v => {
          const display = labelMap[v] || v;
          return `<label class="tfd-item"><input type="checkbox" class="tfd-check-val" data-val="${_esc(v)}" ${selected.has(v) ? 'checked' : ''} />`
               + `<span>${_esc(display) || '<em style="color:var(--text-dim)">(empty)</em>'}</span></label>`;
        }).join('')
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

    if (isNewOpen) { _positionDropdown(anchorEl); dropdown.style.display = ''; }

    const searchInput = dropdown.querySelector('.tfd-search');
    if (focusSearch || _isSearchTyping) {
      searchInput.focus();
      searchInput.setSelectionRange(searchText.length, searchText.length);
    }
    _isSearchTyping = false;

    // ── Dropdown events ────────────────────────────────────────────────────
    // Typing: select matching options visually only — rows update on Enter.
    searchInput.addEventListener('input', e => {
      _isSearchTyping = true;
      const text = e.target.value;
      clearTimeout(_filterTimer);
      const matching = text ? allVals.filter(v => _matchesLabel(v, text)) : [];
      if (onFilter) {
        if (matching.length) activeCheckboxes.set(col, new Set(matching));
        else activeCheckboxes.delete(col);
        activeFilters.delete(col);
        activePatterns.delete(col);
      } else {
        if (text && matching.length) activeFilters.set(col, new Set(matching));
        else activeFilters.delete(col);
      }
      _renderDropdown(col, rawCol, anchorEl, text, false);
    });

    // Enter: apply selected options to rows.

    dropdown.querySelector('.tfd-check-all').addEventListener('change', e => {
      const text = dropdown.querySelector('.tfd-search').value;
      const vis  = text ? allVals.filter(v => _matchesLabel(v, text)) : allVals;
      if (onFilter) {
        const sel = new Set(activeCheckboxes.get(col) || []);
        if (e.target.checked) vis.forEach(v => sel.add(v));
        else                   vis.forEach(v => sel.delete(v));
        if (sel.size) activeCheckboxes.set(col, sel); else activeCheckboxes.delete(col);
        activePatterns.delete(col);
        filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`).value = sel.size ? [...sel].join(' | ') : '';
        _renderDropdown(col, rawCol, anchorEl, text, false);
        clearTimeout(_filterTimer);
        _filterTimer = setTimeout(_fetchFiltered, 400);
      } else {
        const sel = new Set(activeFilters.get(col) || []);
        if (e.target.checked) vis.forEach(v => sel.add(v));
        else                   vis.forEach(v => sel.delete(v));
        if (sel.size) activeFilters.set(col, sel); else activeFilters.delete(col);
        _renderRows();
        _renderDropdown(col, rawCol, anchorEl, text, false);
      }
    });

    dropdown.querySelectorAll('.tfd-check-val').forEach(cb => {
      cb.addEventListener('change', e => {
        const val  = e.target.dataset.val;
        const text = dropdown.querySelector('.tfd-search').value;
        if (onFilter) {
          const sel = new Set(activeCheckboxes.get(col) || []);
          if (e.target.checked) sel.add(val); else sel.delete(val);
          if (sel.size) activeCheckboxes.set(col, sel); else activeCheckboxes.delete(col);
          activePatterns.delete(col);
          filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`).value = sel.size ? [...sel].join(' | ') : '';
          _renderDropdown(col, rawCol, anchorEl, text, false);
          clearTimeout(_filterTimer);
          _filterTimer = setTimeout(_fetchFiltered, 400);
        } else {
          const sel = new Set(activeFilters.get(col) || []);
          if (e.target.checked) sel.add(val); else sel.delete(val);
          if (sel.size) activeFilters.set(col, sel); else activeFilters.delete(col);
          _renderRows();
          _renderDropdown(col, rawCol, anchorEl, text, false);
        }
      });
    });

    dropdown.querySelector('.tfd-clear').addEventListener('click', () => {
      activeFilters.delete(col);
      activePatterns.delete(col);
      activeCheckboxes.delete(col);
      filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`).value = '';
      _closeDropdown();
      if (onFilter) _fetchFiltered(); else _renderRows();
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

  // ── Apply dropdown selection & close ─────────────────────────────────────
  function _applyDropdown() {
    const col = openDropdownCol;
    if (!col) return;
    clearTimeout(_filterTimer);
    _closeDropdown();
    const filterInputEl = filterTr.querySelector(`.tbl-filter-input[data-col="${col}"]`);
    if (onFilter) {
      const sel = activeCheckboxes.get(col);
      if (filterInputEl) filterInputEl.value = sel?.size ? [...sel].map(v => _currentLabelMap[v] || v).join(' | ') : '';
      _fetchFiltered();
    } else {
      const sel = activeFilters.get(col);
      if (filterInputEl) filterInputEl.value = sel?.size ? [...sel].map(v => _currentLabelMap[v] || v).join(' | ') : '';
      _renderRows();
    }
  }

  // ── Event bindings ────────────────────────────────────────────────────────

  // Pin column input
  exportBar.querySelector('.tbl-pin-input').addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const typed = e.target.value.trim().toLowerCase();
    const idx = cols.findIndex(c => {
      const cl = c.toLowerCase();
      return cl === typed || cl.startsWith(typed + ' - ');
    });
    if (idx > 0) {
      const actual = cols[idx];
      cols.splice(idx, 1);
      cols.unshift(actual);
      clearTimeout(_dropdownTimer);
      clearTimeout(_filterTimer);
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

  // Export
  exportBar.querySelector('.tbl-export-btn').addEventListener('click', () => {
    if (onExport) {
      onExport();
    } else {
      const filtered = _getFilteredRows();
      const csv = _toCsv(filtered, cols);
      const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' });
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      a.href     = url;
      a.download = 'export.csv';
      a.click();
      URL.revokeObjectURL(url);
    }
  });

  // Reset column layout
  exportBar.querySelector('.tbl-clear-layout').addEventListener('click', () => {
    cols.splice(0, cols.length, ..._origCols);
    _buildHeaders();
    _renderRows();
    if (onClearLayout) onClearLayout();
  });

  // Clear all filters
  clearAllBtn.addEventListener('click', () => {
    activeFilters.clear();
    activePatterns.clear();
    activeCheckboxes.clear();
    filterTr.querySelectorAll('.tbl-filter-input').forEach(i => { i.value = ''; });
    _closeDropdown();
    if (onFilter) _fetchFiltered(); else _renderRows();
  });

  // Close dropdown on outside click
  const _onDocClick = e => {
    if (!dropdown.contains(e.target) && !table.contains(e.target)) _closeDropdown();
  };
  document.addEventListener('click', _onDocClick);

  // Close + apply dropdown on Enter regardless of where focus is
  const _onDocKeydown = e => {
    if (e.key !== 'Enter' || !openDropdownCol) return;
    if (e.target.classList.contains('tbl-filter-input')) return; // has its own handler
    _applyDropdown();
  };
  document.addEventListener('keydown', _onDocKeydown);

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

    const totalShown = Math.min(_filteredCache.length, PREVIEW_LIMIT);
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
    document.removeEventListener('keydown', _onDocKeydown);
    document.removeEventListener('mouseup', _onDocMouseUp);
    document.removeEventListener('mousemove', _onColMouseMove);
    document.removeEventListener('mouseup', _onColMouseUp);
    wrapEl.removeEventListener('scroll', _onWrapScroll);
    _disposeBatchObserver();
    clearTimeout(_hideScrollTimer);
    clearTimeout(_filterTimer);
    if (stickyFrame) cancelAnimationFrame(stickyFrame);
    dropdown.remove();
    scrollIndicator.remove();
    _dragGhost.remove();
    _dragLine.remove();
    delete wrapEl._filterCleanup;
  };

  // ── Initial render ────────────────────────────────────────────────────────
  _buildHeaders();
  _renderRows();
  wrapEl.replaceChildren(container);

  // ── Measure + save column widths after first paint ────────────────────────
  if (onSaveColWidths) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const widths = {};
      headerTr.querySelectorAll('th').forEach((th, i) => {
        if (i === 0) return; // skip checkbox column
        if (cols[i - 1]) widths[cols[i - 1]] = th.offsetWidth;
      });
      onSaveColWidths(widths);
    }));
  }
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
