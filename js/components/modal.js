// Global modal component. The overlay lives in index.html so it persists across view changes.

export function toast(msg, type = 'ok') {
  const overlay = document.getElementById('modal-overlay');
  const box     = document.getElementById('modal-box');
  document.getElementById('modal-msg').textContent = msg;
  box.className = type === 'err' ? 'err' : type === 'warn' ? 'warn' : '';
  overlay.classList.add('show');
  document.getElementById('modal-ok').onclick = () => overlay.classList.remove('show');
}
