// Data table renderer. Previews up to 200 rows.
// Usage: renderTable(wrapEl, { rows, columns })

const PREVIEW_LIMIT = 200;

export function renderTable(wrapEl, { rows, columns }) {
  if (!rows.length) {
    wrapEl.innerHTML = '';
    return;
  }

  const preview = rows.slice(0, PREVIEW_LIMIT);
  const cols = columns.length ? columns : Object.keys(rows[0]);

  let html = '<table><thead><tr>';
  cols.forEach(c => { html += `<th title="${_esc(c)}">${_esc(c)}</th>`; });
  html += '</tr></thead><tbody>';

  preview.forEach((r, i) => {
    html += `<tr data-i="${i}">`;
    cols.forEach(c => { html += `<td>${_esc(String(r[c] ?? ''))}</td>`; });
    html += '</tr>';
  });

  if (rows.length > PREVIEW_LIMIT) {
    html += `<tr><td colspan="${cols.length}" style="text-align:center;color:var(--text-dim);padding:10px">… ${rows.length - PREVIEW_LIMIT} more rows</td></tr>`;
  }

  html += '</tbody></table>';
  wrapEl.innerHTML = html;
}

function _esc(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
