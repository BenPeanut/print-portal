const SETTINGS_KEY = 'desktop_capture_settings_v2';

const DEFAULT_SETTINGS = {
  apiBase: window.location.origin,
  apiKey: '',
  targetUsernames: [],
  baseFeeOverride: '',
  pricePerGramOverride: '',
  powerCostOverride: '',
  profitMarginOverride: '',
};

let appData = null;
let pricingConfig = null;
let settings = { ...DEFAULT_SETTINGS };
let selectedFilament = '';
let featuredFilamentChoices = [];
let profileRowsState = [];
let modelGalleryImageChoices = [];
let autoLoadDebounceTimer = null;
let autoLoadInFlight = false;
let lastAutoLoadedLink = '';
let quickCaptureMode = false;
let desktopCaptureSignalId = 0;
let desktopCapturePollTimer = null;
const shared = window.CaptureShared || {};

const el = {
  globalStatus: document.getElementById('global-status'),
  modelThumb: document.getElementById('model-thumb'),
  modelImagePicker: document.getElementById('model-image-picker'),
  fieldTitle: document.getElementById('field-title'),
  fieldLink: document.getElementById('field-link'),
  fieldDescription: document.getElementById('field-description'),
  fieldShowInSlideshow: document.getElementById('field-show-in-slideshow'),
  desktopPartsList: document.getElementById('desktop-parts-list'),
  addPartBtn: document.getElementById('add-part-btn'),
  fieldFilament: document.getElementById('field-filament'),
  fieldSuggestedColors: document.getElementById('field-suggested-colors'),
  profileRows: document.getElementById('profile-rows'),
  addProfileBtn: document.getElementById('add-profile-btn'),
  fieldProfile: document.getElementById('field-profile'),
  fieldProfilePricing: document.getElementById('field-profile-pricing'),
  targetUsersGrid: document.getElementById('field-target-users-grid'),
  fieldWeight: document.getElementById('field-weight'),
  fieldHours: document.getElementById('field-hours'),
  fieldPrice: document.getElementById('field-price'),
  submitBtn: document.getElementById('submit-btn'),
  loadModelBtn: document.getElementById('load-model-btn'),
  sApiBase: document.getElementById('s-api-base'),
  sApiKey: document.getElementById('s-api-key'),
  sBaseFee: document.getElementById('s-base-fee'),
  sPricePerGram: document.getElementById('s-price-per-gram'),
  sPowerCost: document.getElementById('s-power-cost'),
  sProfitMargin: document.getElementById('s-profit-margin'),
  saveSettingsBtn: document.getElementById('save-settings-btn'),
  settingsStatus: document.getElementById('settings-status'),
};

function setModelThumb(url) {
  const safe = String(url || '').trim();
  el.modelThumb.style.backgroundImage = safe ? `url("${safe}")` : '';
}

function renderModelImagePicker(imageUrls) {
  if (!el.modelImagePicker) {
    return;
  }
  const urls = Array.from(new Set((Array.isArray(imageUrls) ? imageUrls : [])
    .map((u) => String(u || '').trim())
    .filter((u) => /^https?:\/\//i.test(u))));

  if (!urls.length) {
    el.modelImagePicker.innerHTML = '';
    el.modelImagePicker.classList.add('hidden');
    return;
  }

  const currentImage = String(el.modelThumb.style.backgroundImage || '')
    .replace(/^url\(['"]?/, '')
    .replace(/['"]?\)$/, '');

  el.modelImagePicker.innerHTML = '';
  urls.forEach((url, index) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'model-image-btn';
    btn.title = `Use image ${index + 1}`;
    if ((currentImage && currentImage === url) || (!currentImage && index === 0)) {
      btn.classList.add('active');
    }

    const img = document.createElement('img');
    img.src = url;
    img.alt = `Model image ${index + 1}`;
    img.loading = 'lazy';
    btn.appendChild(img);

    btn.addEventListener('click', () => {
      setModelThumb(url);
      el.modelImagePicker.querySelectorAll('.model-image-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
    });

    el.modelImagePicker.appendChild(btn);
  });

  if (!currentImage && urls[0]) {
    setModelThumb(urls[0]);
  }

  el.modelImagePicker.classList.remove('hidden');
}

function setSettingsStatus(text, isError = false) {
  el.settingsStatus.textContent = text || '';
  el.settingsStatus.style.color = isError ? '#7f1d1d' : '#065f46';
}

function showGlobalStatus(text, type = 'info') {
  el.globalStatus.textContent = text;
  el.globalStatus.className = 'status-bar ' + type;
  el.globalStatus.classList.remove('hidden');
}

function clearGlobalStatus() {
  el.globalStatus.classList.add('hidden');
  el.globalStatus.textContent = '';
}

function requestHostOverlayClose(reason) {
  try {
    window.parent.postMessage({ type: 'dc_close_overlay', reason: String(reason || '') }, '*');
  } catch {
    // Best effort only.
  }

  try {
    window.parent.postMessage({ type: 'dc_close_popup', reason: String(reason || '') }, '*');
  } catch {
    // Best effort only.
  }

  try {
    window.close();
  } catch {
    // Best effort only.
  }
}

function loadSettings() {
  try {
    const parsed = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    settings = { ...DEFAULT_SETTINGS, ...parsed };
  } catch {
    settings = { ...DEFAULT_SETTINGS };
  }
}

function saveSettings() {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

function desktopHeaders(extra = {}) {
  return {
    'X-Desktop-Client': '1',
    ...extra,
  };
}

function getApiBase() {
  return String(settings.apiBase || DEFAULT_SETTINGS.apiBase || window.location.origin).replace(/\/$/, '');
}

function normalizeModelUrl(raw) {
  const text = String(raw || '').trim();
  if (!text) return '';
  try {
    const parsed = new URL(text);
    const host = String(parsed.hostname || '').toLowerCase();
    const allowed =
      host === 'makerworld.com' || host.endsWith('.makerworld.com') ||
      host === 'printables.com' || host.endsWith('.printables.com');
    return allowed ? parsed.toString() : '';
  } catch {
    return '';
  }
}

function calcPrice(weight, hours) {
  if (typeof shared.calcPrice === 'function') {
    return shared.calcPrice({
      weight,
      hours,
      pricingConfig,
      overrides: {
        baseFeeOverride: settings.baseFeeOverride,
        pricePerGramOverride: settings.pricePerGramOverride,
        powerCostOverride: settings.powerCostOverride,
        profitMarginOverride: settings.profitMarginOverride,
      },
    });
  }
  const w = Math.max(0, Number(weight || 0));
  const h = Math.max(0, Number(hours || 0));
  return Math.round(((w + h) * 1000) / 5000) * 5000;
}

function buildApiUrl(path, params = {}) {
  const base = getApiBase();
  const query = new URLSearchParams(params);
  if (settings.apiKey) {
    query.set('api_key', settings.apiKey);
  }
  return `${base}${path}?${query.toString()}`;
}

function getPricingOverrideParams() {
  const params = {};
  if (String(settings.baseFeeOverride || '').trim() !== '') {
    params.base_fee = settings.baseFeeOverride;
  }
  if (String(settings.pricePerGramOverride || '').trim() !== '') {
    params.price_per_gram = settings.pricePerGramOverride;
  }
  if (String(settings.powerCostOverride || '').trim() !== '') {
    params.power_cost_per_hour = settings.powerCostOverride;
  }
  if (String(settings.profitMarginOverride || '').trim() !== '') {
    params.profit_margin = settings.profitMarginOverride;
  }
  return params;
}

function fillSettingsForm() {
  el.sApiBase.value = settings.apiBase || DEFAULT_SETTINGS.apiBase;
  el.sApiKey.value = settings.apiKey || '';
  el.sBaseFee.value = settings.baseFeeOverride || '';
  el.sPricePerGram.value = settings.pricePerGramOverride || '';
  el.sPowerCost.value = settings.powerCostOverride || '';
  el.sProfitMargin.value = settings.profitMarginOverride || '';
}

function parseSuggestedColors(value) {
  return String(value || '')
    .split('|')
    .map((token) => String(token || '').trim())
    .filter(Boolean);
}

function normalizeFilamentChoice(raw) {
  const name = String(raw?.name || '').trim();
  if (!name) {
    return null;
  }
  const totalG = Number(raw?.total_g ?? 0);
  const remainingGRaw = raw?.remaining_g;
  const remainingG = Number(remainingGRaw != null ? remainingGRaw : (Number.isFinite(totalG) ? totalG : 0));
  return {
    name,
    material: String(raw?.material || 'OTHER').trim() || 'OTHER',
    hex: String(raw?.color_hex || raw?.hex || raw?.color || '#8b8b8b').trim() || '#8b8b8b',
    remaining_g: Number.isFinite(remainingG) ? remainingG : 0,
    out_of_stock: Boolean(raw?.out_of_stock),
  };
}

// --- Colour matching helpers ---
function _hexToLab(hex) {
  const h = String(hex || '').replace('#', '').padEnd(6, '0').slice(0, 6);
  const r = parseInt(h.slice(0, 2), 16) / 255;
  const g = parseInt(h.slice(2, 4), 16) / 255;
  const b = parseInt(h.slice(4, 6), 16) / 255;
  const lin = (v) => v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  const rl = lin(r), gl = lin(g), bl = lin(b);
  const x = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375;
  const y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750;
  const z = rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041;
  const f = (t) => t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116;
  return [116 * f(y) - 16, 500 * (f(x / 0.95047) - f(y)), 200 * (f(y) - f(z / 1.08883))];
}

function _colorDistance(hex1, hex2) {
  try {
    const [L1, a1, b1] = _hexToLab(hex1);
    const [L2, a2, b2] = _hexToLab(hex2);
    return Math.sqrt((L1 - L2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2);
  } catch { return 999; }
}

function _nameFromHex(hex) {
  const palette = [
    { name: 'Black', hex: '#000000' },
    { name: 'White', hex: '#ffffff' },
    { name: 'Red', hex: '#ff0000' },
    { name: 'Green', hex: '#00ff00' },
    { name: 'Blue', hex: '#0000ff' },
    { name: 'Yellow', hex: '#ffff00' },
    { name: 'Orange', hex: '#ff8800' },
    { name: 'Purple', hex: '#7f3fbf' },
    { name: 'Brown', hex: '#7b4b2a' },
    { name: 'Gray', hex: '#8b8b8b' },
    { name: 'Cyan', hex: '#00c8ff' },
    { name: 'Pink', hex: '#ff7fbf' },
  ];
  const best = palette
    .map((p) => ({ ...p, d: _colorDistance(hex, p.hex) }))
    .sort((a, b) => a.d - b.d)[0];
  return best ? best.name : 'Color';
}

/**
 * Given a detected color hex + grams required, pick the closest filament from
 * appData.filaments that has enough remaining_g. Falls back to closest by color
 * if none has enough stock.
 */
function _suggestFilament(detectedHex, usedG) {
  const filaments = (appData && appData.filaments) || [];
  const available = filaments.filter((f) => !f.out_of_stock);
  if (!available.length) return null;
  const withDist = available.map((f) => ({
    ...f,
    _dist: _colorDistance(detectedHex, f.hex || f.color_hex || '#888888'),
  })).sort((a, b) => a._dist - b._dist);
  const enough = withDist.find((f) => (f.remaining_g || 0) >= (usedG || 0));
  return enough || withDist[0] || null;
}

// --- End colour matching helpers ---

function buildSwatchGrid(card, selectedColorName, profileIndex) {
  const swatchWrap = card.querySelector('.desktop-part-swatches');
  if (!swatchWrap) return;
  swatchWrap.innerHTML = '';
  const row = Number.isInteger(profileIndex) && profileRowsState[profileIndex] ? profileRowsState[profileIndex] : null;
  const _partsHost = card.parentElement;
  const _slotIndex = _partsHost ? Array.from(_partsHost.querySelectorAll('.featured-part-card')).indexOf(card) : -1;
  const partKey = `part_${_slotIndex >= 0 ? _slotIndex : 0}`;
  const byPartMap = (row && row.manual_insufficient_by_part && typeof row.manual_insufficient_by_part === 'object')
    ? row.manual_insufficient_by_part
    : {};
  const allowByPartMap = (row && row.manual_sufficient_by_part && typeof row.manual_sufficient_by_part === 'object')
    ? row.manual_sufficient_by_part
    : {};
  const hasAnyByPart = Object.keys(byPartMap).length > 0 || Object.keys(allowByPartMap).length > 0;
  const partSpecific = Array.isArray(byPartMap[partKey])
    ? byPartMap[partKey]
    : (hasAnyByPart ? [] : (Array.isArray(row && row.manual_insufficient_filaments) ? row.manual_insufficient_filaments : []));
  const allowPartSpecific = Array.isArray(allowByPartMap[partKey])
    ? allowByPartMap[partKey]
    : [];
  const rowManualSet = new Set(
    Array.isArray(partSpecific)
      ? partSpecific.map((n) => String(n || '').trim().toLowerCase()).filter(Boolean)
      : []
  );
  const rowAllowSet = new Set(
    Array.isArray(allowPartSpecific)
      ? allowPartSpecific.map((n) => String(n || '').trim().toLowerCase()).filter(Boolean)
      : []
  );

  const persistRowManualSet = () => {
    if (!row) return;
    const nextMap = (row.manual_insufficient_by_part && typeof row.manual_insufficient_by_part === 'object')
      ? { ...row.manual_insufficient_by_part }
      : {};
    const nextAllowMap = (row.manual_sufficient_by_part && typeof row.manual_sufficient_by_part === 'object')
      ? { ...row.manual_sufficient_by_part }
      : {};
    nextMap[partKey] = [...rowManualSet]
      .map((key) => {
        const match = featuredFilamentChoices.find((f) => String(f.name || '').trim().toLowerCase() === key);
        return match ? String(match.name || '').trim() : '';
      })
      .filter(Boolean);
    nextAllowMap[partKey] = [...rowAllowSet]
      .map((key) => {
        const match = featuredFilamentChoices.find((f) => String(f.name || '').trim().toLowerCase() === key);
        return match ? String(match.name || '').trim() : '';
      })
      .filter(Boolean);
    row.manual_insufficient_by_part = nextMap;
    row.manual_sufficient_by_part = nextAllowMap;
    row.manual_insufficient_filaments = Object.values(nextMap)
      .reduce((acc, arr) => acc.concat(Array.isArray(arr) ? arr : []), [])
      .map((n) => String(n || '').trim())
      .filter(Boolean)
      .filter((v, i, a) => a.findIndex((x) => String(x || '').toLowerCase() === String(v || '').toLowerCase()) === i);
  };

  const byMaterial = {};
  featuredFilamentChoices.forEach((f) => {
    const m = String(f.material || 'OTHER').trim().toUpperCase() || 'OTHER';
    if (!byMaterial[m]) byMaterial[m] = [];
    byMaterial[m].push(f);
  });
  Object.keys(byMaterial).sort().forEach((material) => {
    const section = document.createElement('div');
    section.className = 'featured-color-section';
    const matLabel = document.createElement('div');
    matLabel.className = 'featured-material-label';
    matLabel.textContent = material;
    section.appendChild(matLabel);
    const grid = document.createElement('div');
    grid.className = 'featured-swatch-grid';
    byMaterial[material].forEach((f) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'featured-swatch-btn';
      btn.dataset.name = f.name;
      btn.style.background = f.hex || '#8b8b8b';
      btn.title = f.name + (f.material ? ` (${f.material})` : '');
      const _modelWg = Number((row && row.weight_g) || (el.fieldWeight && el.fieldWeight.value) || 0) || 0;
      // Determine this card's slot index within its partsHost to get per-slot used_g
      const _slotWg = (row && Array.isArray(row.colors) && row.colors[_slotIndex] && Number(row.colors[_slotIndex].used_g || 0) > 0)
        ? Number(row.colors[_slotIndex].used_g)
        : _modelWg;
      const _autoInsuf = _slotWg > 0 && Number(f.remaining_g || 0) < _slotWg;
      const key = String(f.name || '').toLowerCase();
      const _manualAllow = rowAllowSet.has(key);
      const _manualInsuf = rowManualSet.has(key);
      const _isInsuf = _manualInsuf || (!_manualAllow && _autoInsuf);
      if (_isInsuf) {
        btn.classList.add('swatch-insufficient');
        btn.title = f.name + (f.material ? ` (${f.material})` : '') + ' — Not enough available filament';
        const mark = document.createElement('span');
        mark.className = 'swatch-insufficient-mark';
        mark.setAttribute('aria-hidden', 'true');
        btn.appendChild(mark);
      } else if (_manualAllow && _autoInsuf) {
        btn.title = f.name + (f.material ? ` (${f.material})` : '') + ' — Forced available override';
      }
      let clickTimer = null;
      const toggleManualDeny = () => {
        if (_manualInsuf) {
          // Was manually denied → remove deny, back to auto
          rowManualSet.delete(key);
        } else if (_manualAllow) {
          // Was force-allowed → remove allow, back to auto
          rowAllowSet.delete(key);
        } else if (_autoInsuf) {
          // Auto-blocked → force-allow it (double-click trumps automation)
          rowAllowSet.add(key);
          rowManualSet.delete(key);
        } else {
          // Clean/available → manually deny it
          rowManualSet.add(key);
          rowAllowSet.delete(key);
        }
        persistRowManualSet();
        renderFilamentSwatches(featuredFilamentChoices);
      };
      btn.addEventListener('dblclick', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (clickTimer) {
          clearTimeout(clickTimer);
          clickTimer = null;
        }
        toggleManualDeny();
      });
      btn.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (clickTimer) {
          clearTimeout(clickTimer);
          clickTimer = null;
        }
        toggleManualDeny();
      });
      if (String(selectedColorName || '').toLowerCase() === f.name.toLowerCase()) {
        btn.classList.add('active');
      }
      btn.addEventListener('click', (e) => {
        if (clickTimer) {
          clearTimeout(clickTimer);
          clickTimer = null;
        }
        clickTimer = setTimeout(() => {
          clickTimer = null;
        if (e.altKey) {
          if (rowAllowSet.has(key)) {
            rowAllowSet.delete(key);
          } else {
            rowAllowSet.add(key);
            rowManualSet.delete(key);
          }
          persistRowManualSet();
          renderFilamentSwatches(featuredFilamentChoices);
          return;
        }
        if (e.shiftKey) {
          if (rowManualSet.has(key)) {
            rowManualSet.delete(key);
          } else {
            rowManualSet.add(key);
            rowAllowSet.delete(key);
          }
          persistRowManualSet();
          renderFilamentSwatches(featuredFilamentChoices);
          return;
        }
        if (_manualInsuf || (_autoInsuf && !_manualAllow)) {
          rowAllowSet.add(key);
          rowManualSet.delete(key);
          persistRowManualSet();
        }
        card.querySelectorAll('.featured-swatch-btn').forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        if (typeof card._onChanged === 'function') {
          card._onChanged();
        }
        }, 220);
      });
      grid.appendChild(btn);
    });
    section.appendChild(grid);
    swatchWrap.appendChild(section);
  });
}

function addDesktopPartCard(partsHost, onChanged, partName, colorName, profileIndex) {
  const card = document.createElement('div');
  card._onChanged = onChanged;
  card.className = 'featured-part-card';
  card.dataset.profileIndex = Number.isInteger(profileIndex) ? String(profileIndex) : '';
  const header = document.createElement('div');
  header.className = 'featured-part-card-header';
  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'featured-part-name-input';
  nameInput.placeholder = 'Part name — leave blank for single-color print';
  nameInput.value = partName || '';
  nameInput.addEventListener('input', () => onChanged());
  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'featured-part-remove-btn';
  removeBtn.innerHTML = '&#x2715;';
  removeBtn.addEventListener('click', () => {
    card.remove();
    if (!partsHost.querySelector('.featured-part-card')) {
      addDesktopPartCard(partsHost, onChanged, '', '', profileIndex);
    }
    onChanged();
  });
  header.appendChild(nameInput);
  header.appendChild(removeBtn);
  card.appendChild(header);
  const swatchWrap = document.createElement('div');
  swatchWrap.className = 'featured-color-wrapper desktop-part-swatches';
  card.appendChild(swatchWrap);
  partsHost.appendChild(card);
  buildSwatchGrid(card, colorName || '', profileIndex);
  onChanged();
}

function collectColorPayload(partsHost) {
  const cards = Array.from(partsHost.querySelectorAll('.featured-part-card'));
  const parts = cards.map((card) => {
    const nameInput = card.querySelector('.featured-part-name-input');
    const activeBtn = card.querySelector('.featured-swatch-btn.active');
    return {
      part: nameInput ? nameInput.value.trim() : '',
      color: activeBtn ? (activeBtn.dataset.name || '') : '',
    };
  });
  const firstColor = (parts.find((p) => p.color) || {}).color || '';
  const suggestedColors = parts
    .filter((p) => p.color)
    .map((p) => (p.part ? `${p.part}: ${p.color}` : p.color))
    .join(' | ');

  return {
    firstColor,
    suggestedColors,
    partsConfiguration: parts
      .filter((p) => p.part || p.color)
      .map((p) => ({ part: p.part, suggested_filament: p.color || '' })),
  };
}

function syncProfileSpecificFields() {
  profileRowsState.forEach((row, index) => {
    const card = el.profileRows.querySelector(`[data-profile-index="${index}"]`);
    if (!card) {
      return;
    }
    const partsHost = card.querySelector('[data-profile-parts-list]');
    const payload = partsHost ? collectColorPayload(partsHost) : { firstColor: '', suggestedColors: '', partsConfiguration: [] };
    row.suggested_filament = payload.firstColor || '';
    row.suggested_colors = payload.suggestedColors || '';
    row.parts_configuration = payload.partsConfiguration || [];
    const byPartMap = (row.manual_insufficient_by_part && typeof row.manual_insufficient_by_part === 'object')
      ? row.manual_insufficient_by_part
      : {};
    const cleanedByPart = {};
    Object.keys(byPartMap).forEach((k) => {
      const vals = Array.isArray(byPartMap[k])
        ? byPartMap[k].map((n) => String(n || '').trim()).filter(Boolean)
        : [];
      if (vals.length) cleanedByPart[k] = vals;
    });
    row.manual_insufficient_by_part = cleanedByPart;
    const allowByPartMap = (row.manual_sufficient_by_part && typeof row.manual_sufficient_by_part === 'object')
      ? row.manual_sufficient_by_part
      : {};
    const cleanedAllowByPart = {};
    Object.keys(allowByPartMap).forEach((k) => {
      const vals = Array.isArray(allowByPartMap[k])
        ? allowByPartMap[k].map((n) => String(n || '').trim()).filter(Boolean)
        : [];
      if (vals.length) cleanedAllowByPart[k] = vals;
    });
    row.manual_sufficient_by_part = cleanedAllowByPart;
    row.manual_insufficient_filaments = Array.isArray(row.manual_insufficient_filaments)
      ? row.manual_insufficient_filaments.map((n) => String(n || '').trim()).filter(Boolean)
      : [];

    const activeImage = card.querySelector('.model-image-btn.active img');
    row.selected_image_url = activeImage ? String(activeImage.getAttribute('src') || '').trim() : (row.selected_image_url || '');
  });

  const def = getDefaultProfileRow();
  el.fieldFilament.value = def?.suggested_filament || '';
  el.fieldSuggestedColors.value = def?.suggested_colors || '';
  if (def?.selected_image_url) {
    setModelThumb(def.selected_image_url);
  }
}

function buildProfileImageChoices(row) {
  const own = Array.isArray(row.image_urls) ? row.image_urls : [];
  const ownSet = new Set(own);
  const merged = [];
  const push = (url, source) => {
    const u = String(url || '').trim();
    if (!/^https?:\/\//i.test(u)) {
      return;
    }
    if (merged.some((x) => x.url === u)) {
      return;
    }
    merged.push({ url: u, source });
  };

  own.forEach((u) => push(u, 'profile'));
  modelGalleryImageChoices.forEach((u) => {
    if (!ownSet.has(u)) {
      push(u, 'gallery');
    }
  });
  return merged;
}

function renderProfileImagePicker(host, row, onChanged) {
  host.innerHTML = '';
  const choices = buildProfileImageChoices(row);
  if (!choices.length) {
    return;
  }

  const selected = String(row.selected_image_url || choices[0].url || '').trim();
  choices.forEach((choice, idx) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'model-image-btn';
    btn.title = choice.source === 'profile'
      ? `From profile: ${row.name || 'Profile'} (image ${idx + 1})`
      : `From model gallery (image ${idx + 1})`;
    if (choice.url === selected) {
      btn.classList.add('active');
    }

    const img = document.createElement('img');
    img.src = choice.url;
    img.alt = `${row.name || 'Profile'} image ${idx + 1}`;
    img.loading = 'lazy';
    btn.appendChild(img);

    btn.addEventListener('click', () => {
      host.querySelectorAll('.model-image-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      row.selected_image_url = choice.url;
      onChanged();
    });

    host.appendChild(btn);
  });

  row.selected_image_url = selected;
}

function renderFilamentSwatches(filaments) {
  featuredFilamentChoices = (Array.isArray(filaments) ? filaments : [])
    .map(normalizeFilamentChoice)
    .filter(Boolean);
  // Re-render swatches in any existing part cards across all profile sections.
  Array.from(el.profileRows.querySelectorAll('.featured-part-card')).forEach((card) => {
    const activeBtn = card.querySelector('.featured-swatch-btn.active');
    const colorName = activeBtn ? (activeBtn.dataset.name || '') : '';
    const profileIndex = Number(card.dataset.profileIndex || '-1');
    buildSwatchGrid(card, colorName, Number.isInteger(profileIndex) && profileIndex >= 0 ? profileIndex : undefined);
  });
}

function renderUserCheckboxes(users) {
  const grid = el.targetUsersGrid;
  grid.innerHTML = '';

  // "All users" item
  const allLabel = document.createElement('label');
  allLabel.className = 'target-choice';
  const allCheck = document.createElement('input');
  allCheck.type = 'checkbox';
  allCheck.name = 'target_users';
  allCheck.value = 'ALL';
  allCheck.checked = true;
  allCheck.addEventListener('change', () => {
    if (allCheck.checked) {
      grid.querySelectorAll('input[name="target_users"]').forEach((cb) => {
        if (cb.value !== 'ALL') cb.checked = false;
      });
    }
  });
  allLabel.appendChild(allCheck);
  allLabel.appendChild(document.createTextNode(' All users'));
  grid.appendChild(allLabel);

  users.forEach((u) => {
    const lbl = document.createElement('label');
    lbl.className = 'target-choice';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.name = 'target_users';
    cb.value = u.username;
    cb.addEventListener('change', () => {
      if (cb.checked) {
        allCheck.checked = false;
      } else {
        const anyChecked = Array.from(grid.querySelectorAll('input[name="target_users"]')).some(
          (c) => c.value !== 'ALL' && c.checked
        );
        if (!anyChecked) allCheck.checked = true;
      }
    });
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(' ' + u.username));
    grid.appendChild(lbl);
  });

  // Restore previously-saved selection
  if (settings.targetUsernames && settings.targetUsernames.length) {
    const saved = settings.targetUsernames;
    if (saved.includes('ALL')) {
      allCheck.checked = true;
    } else {
      allCheck.checked = false;
      grid.querySelectorAll('input[name="target_users"]').forEach((cb) => {
        if (saved.includes(cb.value)) cb.checked = true;
      });
    }
  }
}

function normalizeProfileRows(rows) {
  if (typeof shared.normalizeProfileRows === 'function') {
    const sourceRows = Array.isArray(rows) ? rows : [];
    const baseRows = shared.normalizeProfileRows(rows, () => ({
      id: '',
      name: '',
      price: calcPrice(Number(el.fieldWeight.value || 0), Number(el.fieldHours.value || 0)),
      weight_g: Number(el.fieldWeight.value || 0),
      estimated_print_hours: Number(el.fieldHours.value || 0),
      is_default: true,
      manual_price: false,
    }));

    return (Array.isArray(baseRows) ? baseRows : []).map((row, index) => {
      const match = sourceRows.find((src) => {
        const srcId = String(src?.id || '').trim();
        const rowId = String(row?.id || '').trim();
        if (srcId && rowId && srcId === rowId) {
          return true;
        }
        return String(src?.name || '') === String(row?.name || '');
      }) || sourceRows[index] || {};

      return {
        ...row,
        colors: Array.isArray(match.colors) ? match.colors : [],
        image_urls: Array.isArray(match.image_urls) ? match.image_urls : [],
        selected_image_url: String(match.selected_image_url || '').trim(),
        parts_configuration: Array.isArray(match.parts_configuration) ? match.parts_configuration : [],
        suggested_filament: String(match.suggested_filament || '').trim(),
        suggested_colors: String(match.suggested_colors || '').trim(),
        manual_insufficient_by_part: (match.manual_insufficient_by_part && typeof match.manual_insufficient_by_part === 'object') ? match.manual_insufficient_by_part : {},
        manual_sufficient_by_part: (match.manual_sufficient_by_part && typeof match.manual_sufficient_by_part === 'object') ? match.manual_sufficient_by_part : ((match.sufficient_filaments_by_part && typeof match.sufficient_filaments_by_part === 'object') ? match.sufficient_filaments_by_part : {}),
        manual_insufficient_filaments: Array.isArray(match.manual_insufficient_filaments) ? match.manual_insufficient_filaments : [],
      };
    });
  }
  const normalized = (Array.isArray(rows) ? rows : [])
    .map((row, index) => ({
      id: String(row.id || ''),
      name: String(row.name || ''),
      price: Number(row.price || 0),
      weight_g: Number(row.weight_g || 0),
      estimated_print_hours: Number(row.estimated_print_hours || 0),
      is_default: Boolean(row.is_default),
      manual_price: false,
      colors: Array.isArray(row.colors) ? row.colors : [],
      image_urls: Array.isArray(row.image_urls) ? row.image_urls : [],
      selected_image_url: String(row.selected_image_url || '').trim(),
      parts_configuration: Array.isArray(row.parts_configuration) ? row.parts_configuration : [],
      suggested_filament: String(row.suggested_filament || '').trim(),
      suggested_colors: String(row.suggested_colors || '').trim(),
      manual_insufficient_by_part: (row.manual_insufficient_by_part && typeof row.manual_insufficient_by_part === 'object') ? row.manual_insufficient_by_part : {},
      manual_sufficient_by_part: (row.manual_sufficient_by_part && typeof row.manual_sufficient_by_part === 'object') ? row.manual_sufficient_by_part : ((row.sufficient_filaments_by_part && typeof row.sufficient_filaments_by_part === 'object') ? row.sufficient_filaments_by_part : {}),
      manual_insufficient_filaments: Array.isArray(row.manual_insufficient_filaments) ? row.manual_insufficient_filaments : [],
    }))
    .filter((row) => row.name !== '');

  if (!normalized.length) {
    normalized.push({
      id: '',
      name: '',
      price: calcPrice(Number(el.fieldWeight.value || 0), Number(el.fieldHours.value || 0)),
      weight_g: Number(el.fieldWeight.value || 0),
      estimated_print_hours: Number(el.fieldHours.value || 0),
      is_default: true,
      manual_price: false,
      colors: [],
      image_urls: [],
      selected_image_url: '',
      parts_configuration: [],
      suggested_filament: '',
      suggested_colors: '',
      manual_insufficient_by_part: {},
      manual_sufficient_by_part: {},
      manual_insufficient_filaments: [],
    });
  }

  if (!normalized.some((row) => row.is_default)) {
    normalized[0].is_default = true;
  }
  return normalized;
}

function getDefaultProfileRow() {
  if (typeof shared.getDefaultProfileRow === 'function') {
    return shared.getDefaultProfileRow(profileRowsState);
  }
  const index = profileRowsState.findIndex((row) => row.is_default);
  return index >= 0 ? profileRowsState[index] : (profileRowsState[0] || null);
}

function syncProfileFields() {
  const def = getDefaultProfileRow();
  if (def) {
    el.fieldProfile.value = def.name;
    el.fieldPrice.value = String(Math.round(Number(def.price || 0)));
    el.fieldWeight.value = String(Number(def.weight_g || 0));
    el.fieldHours.value = String(Number(def.estimated_print_hours || 0));
  }

  const payload = typeof shared.buildProfilePricingPayload === 'function'
    ? shared.buildProfilePricingPayload(profileRowsState)
    : [];

  el.fieldProfilePricing.value = JSON.stringify(payload);
}

function renderProfileRows(rows) {
  const collapsedByKey = {};
  profileRowsState.forEach((row) => {
    collapsedByKey[String(row._key || '')] = row._collapsed !== false;
  });

  profileRowsState = normalizeProfileRows(rows).map((row, index) => ({
    ...row,
    image_urls: Array.isArray(row.image_urls) ? row.image_urls : [],
    selected_image_url: String(row.selected_image_url || '').trim(),
    colors: Array.isArray(row.colors) ? row.colors : [],
    parts_configuration: Array.isArray(row.parts_configuration) ? row.parts_configuration : [],
    suggested_colors: String(row.suggested_colors || '').trim(),
    suggested_filament: String(row.suggested_filament || '').trim(),
    manual_insufficient_by_part: (row.manual_insufficient_by_part && typeof row.manual_insufficient_by_part === 'object') ? row.manual_insufficient_by_part : {},
    manual_sufficient_by_part: (row.manual_sufficient_by_part && typeof row.manual_sufficient_by_part === 'object') ? row.manual_sufficient_by_part : ((row.sufficient_filaments_by_part && typeof row.sufficient_filaments_by_part === 'object') ? row.sufficient_filaments_by_part : {}),
    manual_insufficient_filaments: Array.isArray(row.manual_insufficient_filaments) ? row.manual_insufficient_filaments : [],
    _key: row.id || `${row.name || 'profile'}-${index}`,
  }));
  profileRowsState = profileRowsState.map((row) => ({
    ...row,
    _collapsed: Object.prototype.hasOwnProperty.call(collapsedByKey, String(row._key || ''))
      ? collapsedByKey[String(row._key || '')]
      : true,
  }));
  el.profileRows.innerHTML = '';

  profileRowsState.forEach((row, index) => {
    const card = document.createElement('div');
    card.className = 'featured-profile-card';
    card.setAttribute('data-profile-index', String(index));

    const header = document.createElement('button');
    header.type = 'button';
    header.className = 'featured-profile-header' + (row._collapsed ? '' : ' expanded');

    const toggleIcon = document.createElement('span');
    toggleIcon.className = 'featured-profile-toggle';
    toggleIcon.innerHTML = '&#9662;';

    const headerTitle = document.createElement('span');
    headerTitle.className = 'featured-profile-header-title';
    headerTitle.textContent = String(row.name || `Profile ${index + 1}`).trim() || `Profile ${index + 1}`;

    const headerActions = document.createElement('span');
    headerActions.className = 'featured-profile-header-actions';

    const removeProfileBtn = document.createElement('button');
    removeProfileBtn.type = 'button';
    removeProfileBtn.className = 'featured-part-remove-btn featured-profile-remove-btn';
    removeProfileBtn.innerHTML = '&#x2715;';
    removeProfileBtn.title = 'Remove profile';
    removeProfileBtn.addEventListener('click', (event) => {
      event.stopPropagation();
      profileRowsState.splice(index, 1);
      if (profileRowsState.length && !profileRowsState.some((p) => p.is_default)) {
        profileRowsState[0].is_default = true;
      }
      renderProfileRows(profileRowsState);
    });
    headerActions.appendChild(removeProfileBtn);

    header.appendChild(toggleIcon);
    header.appendChild(headerTitle);
    header.appendChild(headerActions);

    const content = document.createElement('div');
    content.className = 'featured-profile-content';
    if (row._collapsed) {
      content.hidden = true;
    }

    header.addEventListener('click', () => {
      const next = !profileRowsState[index]._collapsed;
      if (!next) {
        profileRowsState = profileRowsState.map((p, i) => ({
          ...p,
          _collapsed: i === index ? false : true,
        }));
        renderProfileRows(profileRowsState);
        return;
      }
      profileRowsState[index]._collapsed = next;
      content.hidden = next;
      header.classList.toggle('expanded', !next);
    });

    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'featured-profile-name';
    nameInput.value = row.name;
    nameInput.placeholder = `Profile ${index + 1}`;
    nameInput.addEventListener('input', () => {
      profileRowsState[index].name = nameInput.value;
      headerTitle.textContent = String(nameInput.value || `Profile ${index + 1}`).trim() || `Profile ${index + 1}`;
      syncProfileFields();
    });

    const priceInput = document.createElement('input');
    priceInput.type = 'number';
    priceInput.className = 'featured-profile-price';
    priceInput.min = '0';
    priceInput.step = '1';
    priceInput.value = String(Math.round(Number(row.price || 0)));
    priceInput.addEventListener('input', () => {
      profileRowsState[index].price = Number(priceInput.value || 0);
      profileRowsState[index].manual_price = true;
      syncProfileFields();
    });

    const defaultWrap = document.createElement('label');
    defaultWrap.className = 'featured-profile-default';
    const defaultRadio = document.createElement('input');
    defaultRadio.type = 'radio';
    defaultRadio.name = 'default-profile-radio';
    defaultRadio.checked = Boolean(row.is_default);
    defaultRadio.addEventListener('change', () => {
      profileRowsState = profileRowsState.map((p, i) => ({ ...p, is_default: i === index }));
      renderProfileRows(profileRowsState);
    });
    defaultWrap.appendChild(defaultRadio);
    defaultWrap.appendChild(document.createTextNode('Default'));

    const metrics = document.createElement('div');
    metrics.className = 'featured-profile-metrics';
    metrics.textContent = `${Number(row.weight_g || 0).toFixed(2)}g | ${Number(row.estimated_print_hours || 0).toFixed(2)}h`;

    content.appendChild(nameInput);
    content.appendChild(priceInput);
    content.appendChild(defaultWrap);
    content.appendChild(metrics);

    const profileSpecificWrap = document.createElement('div');
    profileSpecificWrap.className = 'profile-specific-wrap';
    profileSpecificWrap.style.gridColumn = '1 / -1';

    const imageLabel = document.createElement('div');
    imageLabel.className = 'featured-material-label';
    imageLabel.textContent = 'Image options';
    profileSpecificWrap.appendChild(imageLabel);

    const imageHost = document.createElement('div');
    imageHost.className = 'model-image-picker';
    imageHost.setAttribute('data-profile-image-picker', '1');
    profileSpecificWrap.appendChild(imageHost);

    renderProfileImagePicker(imageHost, row, () => {
      syncProfileSpecificFields();
    });

    const colorsLabel = document.createElement('div');
    colorsLabel.className = 'featured-material-label';
    colorsLabel.style.marginTop = '8px';
    colorsLabel.textContent = 'Colors / parts for this profile';
    profileSpecificWrap.appendChild(colorsLabel);

    const partsHost = document.createElement('div');
    partsHost.className = 'featured-parts-list';
    partsHost.setAttribute('data-profile-parts-list', '1');
    profileSpecificWrap.appendChild(partsHost);

    const addPartBtn = document.createElement('button');
    addPartBtn.type = 'button';
    addPartBtn.className = 'featured-add-part-btn';
    addPartBtn.textContent = '+ Add Part';
    addPartBtn.addEventListener('click', () => {
      addDesktopPartCard(partsHost, () => syncProfileSpecificFields(), '', '', index);
      syncProfileSpecificFields();
    });
    profileSpecificWrap.appendChild(addPartBtn);

    content.appendChild(profileSpecificWrap);

    const hasConfiguredParts = Array.isArray(row.parts_configuration) && row.parts_configuration.length > 0;
    if (hasConfiguredParts) {
      row.parts_configuration.forEach((p) => {
        addDesktopPartCard(
          partsHost,
          () => syncProfileSpecificFields(),
          String(p.part || '').trim(),
          String(p.suggested_filament || '').trim(),
          index,
        );
      });
    } else if (Array.isArray(row.colors) && row.colors.length > 0) {
      row.colors.forEach((c) => {
        const suggested = _suggestFilament(c.hex || '#888888', c.used_g || 0);
        const partName = String(c.name || '').trim() || _nameFromHex(c.hex || '#888888') || (suggested ? suggested.name : '');
        addDesktopPartCard(partsHost, () => syncProfileSpecificFields(), partName, suggested ? suggested.name : '', index);
      });
    } else {
      addDesktopPartCard(partsHost, () => syncProfileSpecificFields(), '', '', index);
    }

    card.appendChild(header);
    card.appendChild(content);
    el.profileRows.appendChild(card);
  });

  syncProfileFields();
  syncProfileSpecificFields();
}

function refreshProfilePrices() {
  if (!profileRowsState.length) {
    renderProfileRows([]);
    return;
  }

  profileRowsState = profileRowsState.map((row) => {
    if (row.manual_price) {
      return row;
    }
    return {
      ...row,
      price: calcPrice(Number(row.weight_g || 0), Number(row.estimated_print_hours || 0)),
    };
  });

  renderProfileRows(profileRowsState);
}

async function loadModelData() {
  clearGlobalStatus();
  const modelUrl = String(el.fieldLink.value || '').trim();
  if (!modelUrl) {
    showGlobalStatus('Paste a MakerWorld URL first.', 'error');
    return;
  }

  try {
    el.loadModelBtn.disabled = true;
    el.loadModelBtn.textContent = 'Loading...';

    const scrapeRes = await fetch(
      buildApiUrl('/extension-api/scrape-model-metrics', {
        model_url: modelUrl,
        ...getPricingOverrideParams(),
      }),
      { headers: desktopHeaders() }
    );
    const scrape = await scrapeRes.json().catch(() => ({}));

    if (!scrapeRes.ok || !scrape.ok) {
      throw new Error(scrape.error || `Scrape failed (${scrapeRes.status})`);
    }

    if (scrape.title) {
      el.fieldTitle.value = scrape.title;
    }
    const scrapedImages = Array.isArray(scrape.image_urls) ? scrape.image_urls : [];
    if (scrape.image_url && !scrapedImages.includes(scrape.image_url)) {
      scrapedImages.unshift(scrape.image_url);
    }
    modelGalleryImageChoices = scrapedImages;
    renderModelImagePicker([]);

    el.fieldWeight.value = Number(scrape.weight_g || 0);
    el.fieldHours.value = Number(scrape.estimated_print_hours || 0);
    renderProfileRows(scrape.profiles || []);

    const profiles = scrape.profiles || [];
    const firstProfile = profiles[0] || null;
    const colorCount = Array.isArray(firstProfile?.colors) ? firstProfile.colors.length : 0;
    const colorsNote = colorCount > 0
      ? ` · ${colorCount} colour(s) on first profile`
      : ' · no colour breakdown found on first profile';
    showGlobalStatus('Model data loaded.' + colorsNote, 'success');
  } catch (error) {
    showGlobalStatus(error && error.message ? String(error.message) : 'Could not load model data.', 'error');
  } finally {
    el.loadModelBtn.disabled = false;
    el.loadModelBtn.textContent = 'Load Model Data';
  }
}

async function tryAutoLoadModelData(force = false) {
  const normalized = normalizeModelUrl(el.fieldLink.value || '');
  if (!normalized) {
    return;
  }
  if (!force && (autoLoadInFlight || normalized === lastAutoLoadedLink)) {
    return;
  }

  el.fieldLink.value = normalized;
  autoLoadInFlight = true;
  try {
    await loadModelData();
    lastAutoLoadedLink = normalized;
  } finally {
    autoLoadInFlight = false;
  }
}

async function pollDesktopCaptureSignal() {
  try {
    const params = {
      last_id: String(desktopCaptureSignalId || 0),
    };
    const res = await fetch(
      buildApiUrl('/extension-api/desktop-capture/poll', params),
      { headers: desktopHeaders() }
    );
    const payload = await res.json().catch(() => ({}));
    if (!res.ok || !payload.ok) {
      return;
    }

    const nextId = Number(payload.signal_id || 0);
    if (Number.isFinite(nextId) && nextId > desktopCaptureSignalId) {
      desktopCaptureSignalId = nextId;
    }

    if (!payload.has_update) {
      return;
    }

    const incoming = normalizeModelUrl(payload.model_url || '');
    if (!incoming) {
      return;
    }
    if (incoming === normalizeModelUrl(el.fieldLink.value || '') && incoming === lastAutoLoadedLink) {
      return;
    }

    el.fieldLink.value = incoming;
    await tryAutoLoadModelData(true);
    showGlobalStatus('Loaded model sent from MakerWorld hotkey.', 'success');
  } catch {
    // Polling is best effort; ignore transient errors.
  }
}

async function loadAppData() {
  try {
    const [appRes, pricingRes] = await Promise.all([
      fetch(buildApiUrl('/extension-api/app-data'), { headers: desktopHeaders() }),
      fetch(buildApiUrl('/extension-api/pricing-config'), { headers: desktopHeaders() }),
    ]);
    const appJson = await appRes.json().catch(() => ({}));
    const pricingJson = await pricingRes.json().catch(() => ({}));

    if (!appRes.ok || !appJson.ok) {
      throw new Error(appJson.error || `App data failed (${appRes.status})`);
    }
    if (!pricingRes.ok || !pricingJson.ok) {
      throw new Error(pricingJson.error || `Pricing failed (${pricingRes.status})`);
    }

    appData = appJson;
    pricingConfig = pricingJson;

    renderUserCheckboxes(appData.users || []);
    renderFilamentSwatches(appData.filaments || []);

    const defaultProfile = (appData.profiles || []).find((p) => p.is_default) || (appData.profiles || [])[0] || null;
    renderProfileRows(defaultProfile ? [{
      id: defaultProfile.id,
      name: defaultProfile.name,
      price: calcPrice(Number(el.fieldWeight.value || 0), Number(el.fieldHours.value || 0)),
      weight_g: Number(el.fieldWeight.value || 0),
      estimated_print_hours: Number(el.fieldHours.value || 0),
      is_default: true,
    }] : []);

    setSettingsStatus('Connected.', false);
  } catch (error) {
    setSettingsStatus(error && error.message ? String(error.message) : 'Failed to load app data.', true);
  }
}

async function submitCapture() {
  clearGlobalStatus();

  const checkedBoxes = Array.from(
    el.targetUsersGrid.querySelectorAll('input[name="target_users"]:checked')
  );
  const targetUsers = checkedBoxes.map((cb) => cb.value);
  if (!targetUsers.length) {
    showGlobalStatus('Select at least one target user.', 'error');
    return;
  }

  const title = String(el.fieldTitle.value || '').trim();
  const link = String(el.fieldLink.value || '').trim();

  if (!title) {
    showGlobalStatus('Model title is required.', 'error');
    return;
  }
  if (!link) {
    showGlobalStatus('MakerWorld URL is required.', 'error');
    return;
  }

  syncProfileSpecificFields();
  const defaultProfile = getDefaultProfileRow();
  if (!defaultProfile || !String(defaultProfile.suggested_filament || '').trim()) {
    showGlobalStatus('Please select suggested colors for the default profile.', 'error');
    return;
  }

  let profilePricing = [];
  try {
    profilePricing = JSON.parse(el.fieldProfilePricing.value || '[]');
  } catch {
    profilePricing = [];
  }

  settings.targetUsernames = targetUsers;
  saveSettings();

  const profileCustomizations = profileRowsState.map((p) => ({
    profile_id: p.id || '',
    profile_name: p.name || '',
    is_default: Boolean(p.is_default),
    image_url: String(p.selected_image_url || '').trim(),
    suggested_filament: String(p.suggested_filament || '').trim(),
    suggested_colors: String(p.suggested_colors || '').trim(),
    parts_configuration: Array.isArray(p.parts_configuration) ? p.parts_configuration : [],
    sufficient_filaments_by_part: (p.manual_sufficient_by_part && typeof p.manual_sufficient_by_part === 'object') ? p.manual_sufficient_by_part : {},
    insufficient_filaments_by_part: (p.manual_insufficient_by_part && typeof p.manual_insufficient_by_part === 'object') ? p.manual_insufficient_by_part : {},
    insufficient_filaments: Array.isArray(p.manual_insufficient_filaments) ? p.manual_insufficient_filaments : [],
  }));

  const payload = {
    api_key: settings.apiKey || '',
    target_users: targetUsers,
    title,
    makerworld_url: link,
    image_url: String(defaultProfile.selected_image_url || '').trim() || String(el.modelThumb.style.backgroundImage || '')
      .replace(/^url\(['"]?/, '')
      .replace(/['"]?\)$/, ''),
    description: String(el.fieldDescription.value || '').trim(),
    show_in_slideshow: Boolean(el.fieldShowInSlideshow && el.fieldShowInSlideshow.checked),
    suggested_filament: String(defaultProfile.suggested_filament || '').trim(),
    suggested_colors: String(defaultProfile.suggested_colors || '').trim(),
    suggested_profile: el.fieldProfile.value || '',
    profile_pricing: profilePricing,
    profile_customizations: profileCustomizations,
    parts_configuration: Array.isArray(defaultProfile.parts_configuration) ? defaultProfile.parts_configuration : [],
    category_options: Array.isArray(defaultProfile.parts_configuration)
      ? defaultProfile.parts_configuration.map((pc) => ({ part: String(pc.part || ''), suggested_filament: String(pc.suggested_filament || '') }))
      : [],
    price: Number(el.fieldPrice.value || 0),
    print_weight_g: Number(el.fieldWeight.value || 0),
    estimated_print_hours: Number(el.fieldHours.value || 0),
    insufficient_filaments: (() => {
      const wg = Number((defaultProfile && defaultProfile.weight_g) || (el.fieldWeight && el.fieldWeight.value) || 0) || 0;
      const res = new Set(
        [
          ...(Array.isArray(defaultProfile && defaultProfile.manual_insufficient_filaments)
            ? defaultProfile.manual_insufficient_filaments
            : []),
          ...Object.values((defaultProfile && defaultProfile.manual_insufficient_by_part && typeof defaultProfile.manual_insufficient_by_part === 'object')
            ? defaultProfile.manual_insufficient_by_part
            : {}).reduce((acc, arr) => acc.concat(Array.isArray(arr) ? arr : []), []),
        ].map((n) => String(n || '').trim().toLowerCase()).filter(Boolean)
      );
      if (wg > 0) {
        featuredFilamentChoices.forEach((f) => { if (Number(f.remaining_g || 0) < wg) res.add(f.name.toLowerCase()); });
      }
      return [...res].map((k) => (featuredFilamentChoices.find((f) => f.name.toLowerCase() === k) || {}).name || k);
    })(),
  };

  try {
    el.submitBtn.disabled = true;
    el.submitBtn.textContent = 'Submitting...';

    const res = await fetch(buildApiUrl('/extension-api/confirm-capture'), {
      method: 'POST',
      headers: desktopHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `Submit failed (${res.status})`);
    }

    showGlobalStatus('Suggested print added successfully.', 'success');
    setTimeout(() => {
      requestHostOverlayClose('submitted');
    }, 250);
  } catch (error) {
    showGlobalStatus(error && error.message ? String(error.message) : 'Submit failed.', 'error');
  } finally {
    el.submitBtn.disabled = false;
    el.submitBtn.textContent = 'Save Featured Item';
  }
}

function wireEvents() {
  el.saveSettingsBtn.addEventListener('click', async () => {
    settings = {
      ...settings,
      apiBase: String(el.sApiBase.value || '').trim() || DEFAULT_SETTINGS.apiBase,
      apiKey: String(el.sApiKey.value || '').trim(),
      baseFeeOverride: String(el.sBaseFee.value || '').trim(),
      pricePerGramOverride: String(el.sPricePerGram.value || '').trim(),
      powerCostOverride: String(el.sPowerCost.value || '').trim(),
      profitMarginOverride: String(el.sProfitMargin.value || '').trim(),
    };
    saveSettings();
    setSettingsStatus('Settings saved.', false);
    await loadAppData();
    refreshProfilePrices();
  });

  el.loadModelBtn.addEventListener('click', loadModelData);
  el.submitBtn.addEventListener('click', submitCapture);

  document.addEventListener('keydown', (event) => {
    if (!quickCaptureMode) return;
    if (event.key !== 'Enter') return;
    if (el.submitBtn && el.submitBtn.disabled) return;
    event.preventDefault();
    event.stopPropagation();
    submitCapture();
  }, true);

  el.fieldLink.addEventListener('paste', () => {
    setTimeout(() => {
      tryAutoLoadModelData(false);
    }, 0);
  });

  el.fieldLink.addEventListener('input', () => {
    if (autoLoadDebounceTimer) {
      clearTimeout(autoLoadDebounceTimer);
    }
    autoLoadDebounceTimer = setTimeout(() => {
      tryAutoLoadModelData(false);
    }, 450);
  });

  el.addProfileBtn.addEventListener('click', () => {
    profileRowsState.push({
      id: '',
      name: `Profile ${profileRowsState.length + 1}`,
      price: calcPrice(Number(el.fieldWeight.value || 0), Number(el.fieldHours.value || 0)),
      weight_g: Number(el.fieldWeight.value || 0),
      estimated_print_hours: Number(el.fieldHours.value || 0),
      is_default: profileRowsState.length === 0,
      manual_price: false,
      manual_sufficient_by_part: {},
      manual_insufficient_by_part: {},
      manual_insufficient_filaments: [],
    });
    renderProfileRows(profileRowsState);
  });

  el.fieldWeight.addEventListener('input', () => {
    const row = getDefaultProfileRow();
    if (!row) {
      return;
    }
    row.weight_g = Number(el.fieldWeight.value || 0);
    if (!row.manual_price) {
      row.price = calcPrice(row.weight_g, Number(row.estimated_print_hours || 0));
    }
    renderProfileRows(profileRowsState);
    renderFilamentSwatches(featuredFilamentChoices);
  });

  el.fieldHours.addEventListener('input', () => {
    const row = getDefaultProfileRow();
    if (!row) {
      return;
    }
    row.estimated_print_hours = Number(el.fieldHours.value || 0);
    if (!row.manual_price) {
      row.price = calcPrice(Number(row.weight_g || 0), row.estimated_print_hours);
    }
    renderProfileRows(profileRowsState);
  });
}

(async function boot() {
  loadSettings();
  fillSettingsForm();
  wireEvents();
  await loadAppData();

  try {
    const params = new URLSearchParams(window.location.search || '');
    const prefill = params.get('makerworld_link') || params.get('model_url') || '';
    const shouldAutoLoad = params.get('auto_load') === '1';
    quickCaptureMode = params.get('quick_capture') === '1' || params.get('source') === 'extension_popup' || params.get('source') === 'extension_overlay';
    const normalized = normalizeModelUrl(prefill);
    if (params.get('source') === 'extension_popup') {
      document.body.classList.add('extension-popup');
    }
    if (quickCaptureMode) {
      document.body.classList.add('quick-capture-wide');
    }
    if (normalized) {
      el.fieldLink.value = normalized;
      if (shouldAutoLoad) {
        await tryAutoLoadModelData(true);
      }
    }
  } catch {
    // Ignore malformed URL params.
  }

  if (desktopCapturePollTimer) {
    clearInterval(desktopCapturePollTimer);
  }
  desktopCapturePollTimer = setInterval(() => {
    pollDesktopCaptureSignal();
  }, 1200);
  pollDesktopCaptureSignal();
})();