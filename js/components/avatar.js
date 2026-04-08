// Avatar selector + dropdown component.
// Persists selected avatar in localStorage.

const _STORAGE_KEY = 'harness_avatar';

export const AVATARS = [
  {
    id: 'av1', label: 'Aria',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#26C6DA"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#0097A7"/>
      <circle cx="20" cy="20" r="8.5" fill="#FFD5A8"/>
      <ellipse cx="20" cy="13" rx="8.5" ry="5.5" fill="#2B1A0E"/>
      <circle cx="25" cy="11" r="4.5" fill="#2B1A0E"/>
      <circle cx="17" cy="20" r="1.2" fill="#2B1A0E"/>
      <circle cx="23" cy="20" r="1.2" fill="#2B1A0E"/>
      <path d="M17.5 23.5 Q20 25.5 22.5 23.5" stroke="#B8845A" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
  {
    id: 'av2', label: 'Marcus',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#1E88E5"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#1565C0"/>
      <circle cx="20" cy="20" r="8.5" fill="#7D4E35"/>
      <ellipse cx="20" cy="12.5" rx="8" ry="5" fill="#1C1C1C"/>
      <circle cx="17" cy="20" r="1.2" fill="#1C1C1C"/>
      <circle cx="23" cy="20" r="1.2" fill="#1C1C1C"/>
      <path d="M17.5 23.5 Q20 25.5 22.5 23.5" stroke="#5C3018" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
  {
    id: 'av3', label: 'Priya',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#8E24AA"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#6A1B9A"/>
      <circle cx="20" cy="20" r="8.5" fill="#D4956A"/>
      <ellipse cx="20" cy="12" rx="8.5" ry="6" fill="#1C1C1C"/>
      <path d="M11.5 15 Q11 26 13 30" stroke="#1C1C1C" stroke-width="3.5" stroke-linecap="round" fill="none"/>
      <path d="M28.5 15 Q29 26 27 30" stroke="#1C1C1C" stroke-width="3.5" stroke-linecap="round" fill="none"/>
      <circle cx="17" cy="21" r="1.2" fill="#1C1C1C"/>
      <circle cx="23" cy="21" r="1.2" fill="#1C1C1C"/>
      <path d="M17.5 24 Q20 26 22.5 24" stroke="#A06040" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
  {
    id: 'av4', label: 'Leo',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#FB8C00"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#E65100"/>
      <circle cx="20" cy="20" r="8.5" fill="#FFDAB9"/>
      <circle cx="15" cy="13" r="5" fill="#C0392B"/>
      <circle cx="20" cy="12" r="6" fill="#C0392B"/>
      <circle cx="25" cy="13" r="5" fill="#C0392B"/>
      <ellipse cx="20" cy="13.5" rx="8" ry="4" fill="#C0392B"/>
      <circle cx="17" cy="20" r="1.2" fill="#5D3310"/>
      <circle cx="23" cy="20" r="1.2" fill="#5D3310"/>
      <path d="M17.5 23.5 Q20 25.5 22.5 23.5" stroke="#B87850" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
  {
    id: 'av5', label: 'Zara',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#43A047"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#2E7D32"/>
      <circle cx="20" cy="20" r="8.5" fill="#5C3018"/>
      <circle cx="20" cy="13" r="8" fill="#1C1C1C"/>
      <circle cx="13" cy="16" r="5.5" fill="#1C1C1C"/>
      <circle cx="27" cy="16" r="5.5" fill="#1C1C1C"/>
      <circle cx="17" cy="20.5" r="1.2" fill="#1C1C1C"/>
      <circle cx="23" cy="20.5" r="1.2" fill="#1C1C1C"/>
      <path d="M17.5 24 Q20 26 22.5 24" stroke="#3D1A08" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
  {
    id: 'av6', label: 'Chen',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#3949AB"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#283593"/>
      <circle cx="20" cy="20" r="8.5" fill="#F5CBA7"/>
      <ellipse cx="20" cy="12.5" rx="7.5" ry="4.5" fill="#1C1C1C"/>
      <rect x="13.5" y="18" width="5.5" height="3.8" rx="1.8" stroke="#1C1C1C" stroke-width="1.2" fill="rgba(200,230,255,0.25)"/>
      <rect x="21" y="18" width="5.5" height="3.8" rx="1.8" stroke="#1C1C1C" stroke-width="1.2" fill="rgba(200,230,255,0.25)"/>
      <line x1="19" y1="19.9" x2="21" y2="19.9" stroke="#1C1C1C" stroke-width="1.2"/>
      <circle cx="16.3" cy="19.9" r="0.9" fill="#1C1C1C"/>
      <circle cx="23.8" cy="19.9" r="0.9" fill="#1C1C1C"/>
      <path d="M17 23.5 Q20 25.5 23 23.5" stroke="#C49A6C" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
  {
    id: 'av7', label: 'Maya',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#D81B60"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#880E4F"/>
      <circle cx="20" cy="20" r="8.5" fill="#FDDBB4"/>
      <ellipse cx="20" cy="12.5" rx="8.5" ry="5.5" fill="#E8C96A"/>
      <circle cx="27" cy="13" r="4" fill="#E8C96A"/>
      <ellipse cx="20" cy="11.5" rx="7" ry="4" fill="#D4B055"/>
      <circle cx="17" cy="20" r="1.2" fill="#3D2B0E"/>
      <circle cx="23" cy="20" r="1.2" fill="#3D2B0E"/>
      <path d="M17.5 23.5 Q20 25.5 22.5 23.5" stroke="#C49A6C" stroke-width="1.1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
  {
    id: 'av8', label: 'Omar',
    svg: `<svg viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="20" fill="#00796B"/>
      <path d="M5 40 Q5 29 20 29 Q35 29 35 40Z" fill="#004D40"/>
      <circle cx="20" cy="20" r="8.5" fill="#D4845A"/>
      <ellipse cx="20" cy="12.5" rx="7.5" ry="4.5" fill="#1C1C1C"/>
      <path d="M12 22 Q12 29 20 30 Q28 29 28 22 Q26 26 20 26.5 Q14 26 12 22Z" fill="#1C1C1C"/>
      <circle cx="17" cy="20" r="1.2" fill="#1C1C1C"/>
      <circle cx="23" cy="20" r="1.2" fill="#1C1C1C"/>
      <path d="M17.5 22.5 Q20 24 22.5 22.5" stroke="#A06040" stroke-width="1" fill="none" stroke-linecap="round"/>
    </svg>`,
  },
];

export function getCurrentAvatarId() {
  return localStorage.getItem(_STORAGE_KEY) || AVATARS[0].id;
}

export function getAvatarSvg(id) {
  return (AVATARS.find(a => a.id === id) || AVATARS[0]).svg;
}

export function avatarDropdownHtml() {
  return `
    <div class="avatar-wrap" id="avatar-wrap">
      <button class="avatar-btn" id="avatar-btn" title="Profile">
        ${getAvatarSvg(getCurrentAvatarId())}
      </button>
      <div class="avatar-dropdown" id="avatar-dropdown">
        <div id="avatar-menu"></div>
        <div class="avatar-divider" id="avatar-nav-divider"></div>
        <button class="avatar-menu-item" id="avatar-select-btn">
          <span id="avatar-select-arrow" style="display:inline-block;transition:transform .2s;margin-right:4px">▸</span>Select Avatar
        </button>
        <div class="avatar-picker" id="avatar-picker" style="display:none"></div>
      </div>
    </div>
  `;
}

export function initAvatarDropdown(container, menuItems) {
  const btn        = container.querySelector('#avatar-btn');
  const dropdown   = container.querySelector('#avatar-dropdown');
  const menu       = container.querySelector('#avatar-menu');
  const navDivider = container.querySelector('#avatar-nav-divider');
  const selectBtn  = container.querySelector('#avatar-select-btn');
  const picker     = container.querySelector('#avatar-picker');

  // Hide divider if no menu items
  if (!menuItems.length) navDivider.style.display = 'none';

  // Render menu items
  menu.innerHTML = menuItems.map((item, i) =>
    `<button class="avatar-menu-item${item.danger ? ' danger' : ''}" data-idx="${i}">${item.label}</button>`
  ).join('');

  menu.querySelectorAll('.avatar-menu-item').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      dropdown.classList.remove('open');
      menuItems[+el.dataset.idx].action();
    });
  });

  // Render avatar picker
  picker.innerHTML = AVATARS.map(a => `
    <button class="avatar-opt${a.id === getCurrentAvatarId() ? ' selected' : ''}"
            data-id="${a.id}" title="${a.label}">
      ${a.svg}
    </button>
  `).join('');

  picker.querySelectorAll('.avatar-opt').forEach(opt => {
    opt.addEventListener('click', (e) => {
      e.stopPropagation();
      localStorage.setItem(_STORAGE_KEY, opt.dataset.id);
      btn.innerHTML = getAvatarSvg(opt.dataset.id);
      picker.querySelectorAll('.avatar-opt').forEach(o => o.classList.toggle('selected', o === opt));
    });
  });

  // Toggle avatar picker section
  selectBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = picker.style.display !== 'none';
    picker.style.display = isOpen ? 'none' : '';
    const arrow = selectBtn.querySelector('#avatar-select-arrow');
    if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
  });

  // Toggle dropdown open/close
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const opening = !dropdown.classList.contains('open');
    dropdown.classList.toggle('open');
    // Collapse avatar picker when closing
    if (!opening) {
      picker.style.display = 'none';
      const arrow = selectBtn.querySelector('#avatar-select-arrow');
      if (arrow) arrow.style.transform = '';
    }
  });

  // Close on outside click
  const closeHandler = () => {
    dropdown.classList.remove('open');
    picker.style.display = 'none';
    const arrow = selectBtn.querySelector('#avatar-select-arrow');
    if (arrow) arrow.style.transform = '';
  };
  document.addEventListener('click', closeHandler);

  const cleanup = new MutationObserver(() => {
    if (!document.contains(container)) {
      document.removeEventListener('click', closeHandler);
      cleanup.disconnect();
    }
  });
  cleanup.observe(document.body, { childList: true, subtree: true });
}
