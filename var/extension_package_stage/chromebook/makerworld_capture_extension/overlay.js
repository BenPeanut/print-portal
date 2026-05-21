(function () {
  const STORAGE_KEYS = {
    settings: "capture_settings",
    authToken: "extension_auth_token",
  };
  const DEFAULT_API_BASE = "http://127.0.0.1:5000";
  const CART_STORAGE_KEY = "bcart_v1";

  const params = new URLSearchParams(window.location.search || "");
  const modelUrl = String(params.get("model_url") || "").trim();
  const apiBaseFromQuery = String(params.get("api_base") || "").trim();
  const tokenFromQuery = String(params.get("ext_auth") || "").trim();

  const runtime = {
    apiBase: "",
    authToken: tokenFromQuery,
    filaments: [],
    sessionBound: false,
  };

  const modalState = {
    item: null,
    profile: "",
    profileObj: null,
    profiles: [],
    parts: [],
    selections: {},
    lazyAvailability: {},
    profileSelectControl: null,
  };

  const cartState = {
    items: [],
    selectedIds: new Set(),
  };

  const checkoutState = {
    inFlight: false,
    result: null,
  };

  function normalizeQuantity(value) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed) || parsed < 1) return 1;
    return Math.min(parsed, 99);
  }

  function formatPrice(value) {
    return "Rp" + Number(value || 0).toLocaleString("id-ID");
  }

  function cartItemFingerprint(item) {
    const safe = item || {};
    const parts = Array.isArray(safe.multiMappings)
      ? safe.multiMappings.map(function (m) {
        return String((m && m.part) || "").trim().toLowerCase() + ":" + String((m && m.filament) || "").trim().toLowerCase();
      }).sort().join("|")
      : "";
    return [
      String(safe.link || "").trim().toLowerCase(),
      String(safe.displayName || "").trim().toLowerCase(),
      String(safe.profile || "").trim().toLowerCase(),
      String(safe.singleFilament || "").trim().toLowerCase(),
      parts,
    ].join("||");
  }

  function loadCartItems() {
    try {
      const parsed = JSON.parse(localStorage.getItem(CART_STORAGE_KEY) || '{"items":[]}');
      const rows = Array.isArray(parsed.items) ? parsed.items : [];
      return rows.map(function (row) {
        return Object.assign({}, row, { quantity: normalizeQuantity(row && row.quantity) });
      });
    } catch {
      return [];
    }
  }

  function saveCartItems() {
    localStorage.setItem(CART_STORAGE_KEY, JSON.stringify({ items: cartState.items || [] }));
  }

  function upsertCartItem(item) {
    if (!item || typeof item !== "object") return;
    checkoutState.result = null;
    item.quantity = normalizeQuantity(item.quantity);
    const incomingOrderId = String(item.orderId || item.order_id || "").trim();
    const incomingFp = cartItemFingerprint(item);
    const existing = (cartState.items || []).find(function (row) {
      const rowOrderId = String(row.orderId || row.order_id || "").trim();
      if (incomingOrderId && rowOrderId && incomingOrderId === rowOrderId) return true;
      if (!incomingOrderId) return cartItemFingerprint(row) === incomingFp;
      return false;
    });
    if (!existing) {
      cartState.items.push(item);
    } else {
      existing.quantity = normalizeQuantity(existing.quantity) + normalizeQuantity(item.quantity);
      existing.quantity = normalizeQuantity(existing.quantity);
    }
    saveCartItems();
  }

  function normalizeLocalApiBase(raw) {
    const fallback = DEFAULT_API_BASE;
    const text = String(raw || "").trim() || fallback;
    try {
      const parsed = new URL(text);
      const host = String(parsed.hostname || "").toLowerCase();
      if (host !== "127.0.0.1" && host !== "localhost") {
        return fallback;
      }
      return `${parsed.protocol}//${parsed.host}`.replace(/\/$/, "");
    } catch {
      return fallback;
    }
  }

  async function bootstrapRuntime() {
    const storage = await chrome.storage.local.get([STORAGE_KEYS.settings, STORAGE_KEYS.authToken]);
    const settings = storage[STORAGE_KEYS.settings] || {};
    runtime.apiBase = normalizeLocalApiBase(apiBaseFromQuery || settings.apiBase || DEFAULT_API_BASE);
    if (!runtime.authToken) {
      runtime.authToken = String(storage[STORAGE_KEYS.authToken] || "").trim();
    }
    if (!runtime.sessionBound && runtime.authToken) {
      try {
        const response = await fetch(`${runtime.apiBase}/extension-api/user-auth-status`, {
          method: "GET",
          credentials: "include",
          headers: {
            "Accept": "application/json",
            "X-Extension-Auth": runtime.authToken,
          },
        });
        const payload = await response.json().catch(function () { return {}; });
        if (response.ok && payload && payload.ok && payload.logged_in) {
          if (payload.extension_auth_token) {
            runtime.authToken = String(payload.extension_auth_token || "").trim();
            await chrome.storage.local.set({ [STORAGE_KEYS.authToken]: runtime.authToken });
          }
          runtime.sessionBound = true;
        }
      } catch (e) {
        // Leave runtime usable; subsequent API calls still carry X-Extension-Auth.
      }
    }
  }

  function getHeaders(baseHeaders) {
    return {
      ...(baseHeaders || {}),
      ...(runtime.authToken ? { "X-Extension-Auth": runtime.authToken } : {}),
    };
  }

  function setStatus(message, isError) {
    const statusEl = document.getElementById("featured-modal-status");
    if (!statusEl) return;
    statusEl.textContent = String(message || "");
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function setCartFeedback(message, isError) {
    const feedbackEl = document.getElementById("cart-feedback");
    if (!feedbackEl) return;
    feedbackEl.textContent = String(message || "");
    feedbackEl.classList.toggle("error", Boolean(isError));
  }

  function clearCheckoutResult() {
    checkoutState.result = null;
    setCartFeedback("", false);
  }

  function postToParent(action, payload) {
    window.parent.postMessage({ source: "mw-extension-overlay", action, ...(payload || {}) }, "*");
  }

  function openPortalPage(path) {
    const normalizedPath = "/" + String(path || "").replace(/^\/+/, "");
    const url = runtime.apiBase ? (runtime.apiBase + normalizedPath) : normalizedPath;
    try {
      postToParent("open-url", { url: url });
    } catch (error) {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  }

  function getFilamentCatalog() {
    const rows = Array.isArray(runtime.filaments) ? runtime.filaments : [];
    return rows.map(function (f) {
      if (typeof f === "string") return { name: f, hex: "#8b8b8b", remaining_g: 0, out_of_stock: false };
      return {
        name: String((f && f.name) || "").trim(),
        hex: String((f && f.hex) || "#8b8b8b").trim() || "#8b8b8b",
        remaining_g: Number((f && f.remaining_g != null) ? f.remaining_g : ((f && f.total_g) || 0)),
        out_of_stock: Boolean(f && f.out_of_stock),
      };
    }).filter(function (f) { return f.name; });
  }

  function parseSuggestedColorMap(text) {
    const map = {};
    const segments = String(text || "").split("|").map(function (seg) { return seg.trim(); }).filter(Boolean);
    segments.forEach(function (seg) {
      const idx = seg.indexOf(":");
      if (idx > -1) {
        const partName = seg.slice(0, idx).trim();
        const colorName = seg.slice(idx + 1).trim();
        if (partName && colorName) map[partName.toLowerCase()] = colorName;
      } else {
        map.__default = seg;
      }
    });
    return map;
  }

  function normalizeHex(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const normalized = raw.startsWith("#") ? raw.toLowerCase() : ("#" + raw.toLowerCase());
    return /^#[0-9a-f]{6}$/.test(normalized) ? normalized : "";
  }

  function hexToLab(hex) {
    const normalized = normalizeHex(hex);
    if (!normalized) throw new Error("Invalid hex color");
    const value = normalized.slice(1);
    const r = parseInt(value.slice(0, 2), 16) / 255;
    const g = parseInt(value.slice(2, 4), 16) / 255;
    const b = parseInt(value.slice(4, 6), 16) / 255;
    const lin = function (channel) {
      return channel <= 0.04045 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4);
    };
    const rl = lin(r);
    const gl = lin(g);
    const bl = lin(b);
    const x = rl * 0.4124564 + gl * 0.3575761 + bl * 0.1804375;
    const y = rl * 0.2126729 + gl * 0.7151522 + bl * 0.0721750;
    const z = rl * 0.0193339 + gl * 0.1191920 + bl * 0.9503041;
    const pivot = function (channel) {
      return channel > 0.008856 ? Math.cbrt(channel) : (7.787 * channel) + (16 / 116);
    };
    const fx = pivot(x / 0.95047);
    const fy = pivot(y);
    const fz = pivot(z / 1.08883);
    return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
  }

  function colorDistance(hexA, hexB) {
    try {
      const left = hexToLab(hexA);
      const right = hexToLab(hexB);
      return Math.sqrt(
        Math.pow(left[0] - right[0], 2)
        + Math.pow(left[1] - right[1], 2)
        + Math.pow(left[2] - right[2], 2)
      );
    } catch {
      return Number.POSITIVE_INFINITY;
    }
  }

  function getColorLabelFromHex(hex) {
    const normalized = normalizeHex(hex);
    if (!normalized) return "";
    const palette = [
      { name: "Black", hex: "#000000" },
      { name: "White", hex: "#ffffff" },
      { name: "Gray", hex: "#8b8b8b" },
      { name: "Red", hex: "#ff0000" },
      { name: "Dark Red", hex: "#8b0000" },
      { name: "Green", hex: "#00ff00" },
      { name: "Blue", hex: "#0000ff" },
      { name: "Yellow", hex: "#ffff00" },
      { name: "Orange", hex: "#ff8800" },
      { name: "Purple", hex: "#7f3fbf" },
      { name: "Brown", hex: "#7b4b2a" },
      { name: "Cyan", hex: "#00c8ff" },
      { name: "Pink", hex: "#ff7fbf" },
    ];
    const ranked = palette
      .map(function (entry) {
        return { name: entry.name, dist: colorDistance(normalized, entry.hex) };
      })
      .sort(function (left, right) { return left.dist - right.dist; });
    return ranked.length ? ranked[0].name : "";
  }

  function normalizeItemProfiles(item) {
    function exactProfileName(name) {
      return String(name || "");
    }

    function dedupeProfiles(list) {
      const deduped = [];
      const byKey = new Map();
      list.forEach(function (entry) {
        if (!entry) return;
        const normalizedName = exactProfileName(entry.name);
        if (!normalizedName) return;
        const normalizedPrice = Number(entry.price || 0);
        const key = normalizedName + "|" + Math.round(normalizedPrice);
        const existingIdx = byKey.get(key);
        if (existingIdx === undefined) {
          const row = Object.assign({}, entry, {
            name: normalizedName,
            price: normalizedPrice,
          });
          byKey.set(key, deduped.length);
          deduped.push(row);
          return;
        }

        const existing = deduped[existingIdx];
        existing.is_default = Boolean(existing.is_default || entry.is_default);
        if (!existing.id && entry.id) existing.id = entry.id;
        if ((!existing.colors || !existing.colors.length) && Array.isArray(entry.colors) && entry.colors.length) {
          existing.colors = entry.colors;
        }
        if (!existing.weight_g && entry.weight_g) existing.weight_g = entry.weight_g;
      });
      return deduped;
    }

    if (!item || typeof item !== "object") return [];
    const pricing = Array.isArray(item.profile_pricing) ? item.profile_pricing : [];
    if (pricing.length) {
      const mapped = dedupeProfiles(pricing
        .map(function (p) {
          const name = exactProfileName((p && p.name) || "");
          if (!name) return null;
          return {
            id: String((p && p.id) || "").trim(),
            name: name,
            price: Number((p && (p.price !== undefined ? p.price : p.price_modifier)) || 0),
            is_default: Boolean(p && p.is_default),
            weight_g: Number((p && p.weight_g) || 0),
            colors: Array.isArray(p && p.colors) ? p.colors : [],
          };
        })
        .filter(Boolean));
      if (mapped.length && !mapped.some(function (p) { return p.is_default; })) {
        mapped[0].is_default = true;
      }
      return mapped;
    }
    return [];
  }

  function getProfileCustomization(item, profileObj) {
    if (!item || !profileObj) return null;
    const customizations = Array.isArray(item.profile_customizations) ? item.profile_customizations : [];
    const profileId = String(profileObj.id || "").trim();
    const profileName = String(profileObj.name || "");
    if (profileId) {
      const byId = customizations.find(function (entry) {
        return String((entry && entry.profile_id) || "").trim() === profileId;
      });
      if (byId) return byId;
    }
    return customizations.find(function (entry) {
      return String((entry && entry.profile_name) || "") === profileName;
    }) || null;
  }

  function deriveParts(item) {
    const parts = [];
    const suggestedMap = parseSuggestedColorMap(item.suggested_colors || item.suggested_filament || "");
    const rawParts = Array.isArray(item.part_options)
      ? item.part_options
      : (Array.isArray(item.category_options) ? item.category_options : []);

    rawParts.forEach(function (part, idx) {
      if (typeof part === "string") {
        const rawPartName = part.trim();
        if (!rawPartName) return;
        const suggestedName = suggestedMap[rawPartName.toLowerCase()] || suggestedMap.__default || String(item.suggested_filament || "").trim();
        parts.push(buildPartRecord(rawPartName, suggestedName, "", idx, 0));
        return;
      }
      if (part && typeof part === "object") {
        const rawPartName = String(part.part || part.name || part.label || part.title || "").trim();
        if (!rawPartName) return;
        const suggestedName = String(part.suggested_filament || part.filament || part.color || suggestedMap[rawPartName.toLowerCase()] || suggestedMap.__default || item.suggested_filament || "").trim();
        const suggestedHex = String(part.suggested_hex || part.hex || part.color_hex || "").trim();
        parts.push(buildPartRecord(rawPartName, suggestedName, suggestedHex, idx, Number(part.used_g || 0)));
      }
    });

    if (!parts.length) {
      const fallbackSuggested = String(item.suggested_filament || "").trim();
      parts.push(buildPartRecord("Main Part", fallbackSuggested, "", 0, Number(item.model_weight || 0)));
    }
    return parts;
  }

  function normalizeColorText(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  }

  function colorFamilyFromText(value) {
    const text = normalizeColorText(value);
    if (!text) return "";
    const families = {
      black: ["black", "onyx", "charcoal"],
      white: ["white", "ivory", "cream"],
      gray: ["gray", "grey", "slate", "graphite"],
      red: ["red", "crimson", "scarlet", "maroon", "burgundy"],
      blue: ["blue", "navy", "azure", "cobalt", "cyan"],
      green: ["green", "lime", "emerald", "olive", "mint"],
      yellow: ["yellow", "amber"],
      orange: ["orange", "tangerine", "coral"],
      purple: ["purple", "violet", "magenta", "lavender"],
      pink: ["pink", "rose", "fuchsia"],
      brown: ["brown", "chocolate", "tan", "beige"],
      gold: ["gold", "brass"],
      silver: ["silver", "steel", "chrome"],
      clear: ["clear", "transparent", "translucent"],
    };
    for (const key in families) {
      if (!Object.prototype.hasOwnProperty.call(families, key)) continue;
      const synonyms = families[key];
      if (synonyms.some(function (token) { return text.includes(token); })) {
        return key;
      }
    }
    return "";
  }

  function toTitleCaseWords(value) {
    return String(value || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .map(function (token) { return token.charAt(0).toUpperCase() + token.slice(1).toLowerCase(); })
      .join(" ");
  }

  function getColorLabelFromText(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    const family = colorFamilyFromText(text);
    if (family) return toTitleCaseWords(family);
    // If no known family is detected, keep the source text readable.
    return toTitleCaseWords(text);
  }

  function isGenericPartName(value, suggestedName, suggestedHex) {
    const text = normalizeColorText(value);
    if (!text) return true;
    if (/^(main part|part \d+|filler \d+|color part \d+|colour part \d+|material part \d+|section \d+|segment \d+)$/i.test(text)) {
      return true;
    }
    if (/ part$/i.test(text)) {
      const base = text.replace(/ part$/i, "").trim();
      const colorBases = [
        normalizeColorText(getColorLabelFromHex(suggestedHex)),
        normalizeColorText(getColorLabelFromText(suggestedName)),
        normalizeColorText(suggestedName),
      ].filter(Boolean);
      if (colorBases.some(function (candidate) { return candidate === base; })) {
        return true;
      }
    }
    return false;
  }

  function buildPartRecord(providedName, suggestedName, suggestedHex, idx, slotUsedG) {
    const resolvedName = isGenericPartName(providedName, suggestedName, suggestedHex)
      ? (getColorLabelFromHex(suggestedHex) || getColorLabelFromText(suggestedName) || ("Part " + (idx + 1)))
      : String(providedName || "").trim();
    return {
      name: resolvedName,
      suggested: String(suggestedName || "").trim(),
      suggestedHex: normalizeHex(suggestedHex),
      key: "part_" + idx,
      used_g: Number(slotUsedG || 0),
      resolvedSuggested: "",
    };
  }

  function scoreFilamentMatch(desiredName, filamentName) {
    const desired = normalizeColorText(desiredName);
    const candidate = normalizeColorText(filamentName);
    if (!desired || !candidate) return -1;
    if (desired === candidate) return 1000;

    let score = 0;
    if (candidate.includes(desired) || desired.includes(candidate)) score += 300;

    const desiredTokens = desired.split(/\s+/).filter(Boolean);
    const candidateTokens = new Set(candidate.split(/\s+/).filter(Boolean));
    desiredTokens.forEach(function (token) {
      if (candidateTokens.has(token)) score += 40;
      else if (candidate.includes(token)) score += 20;
    });

    const desiredFamily = colorFamilyFromText(desired);
    const candidateFamily = colorFamilyFromText(candidate);
    if (desiredFamily && candidateFamily && desiredFamily === candidateFamily) score += 160;

    return score;
  }

  function pickClosestFilamentName(desiredName, desiredHex, catalog, isAllowed) {
    const rows = Array.isArray(catalog) ? catalog : [];
    if (!rows.length) return "";
    const desired = String(desiredName || "").trim();
    const normalizedDesiredHex = normalizeHex(desiredHex);
    const allowedRows = typeof isAllowed === "function"
      ? rows.filter(function (row) { return isAllowed(row); })
      : rows.slice();
    if (!allowedRows.length) return "";
    if (!desired && !normalizedDesiredHex) return String((allowedRows[0] && allowedRows[0].name) || "").trim();

    const exact = allowedRows.find(function (row) {
      return String((row && row.name) || "").trim().toLowerCase() === desired.toLowerCase();
    });
    if (exact) return String(exact.name || "").trim();

    // Filaments without a real configured hex default to #8b8b8b (placeholder gray).
    // Using that placeholder as a real measurement steers hex ranking wrong, so
    // those filaments receive hexScore=0 and compete on text score only.
    const PLACEHOLDER_HEX = "#8b8b8b";
    const ranked = [];
    allowedRows.forEach(function (row) {
      const name = String((row && row.name) || "").trim();
      if (!name) return;
      const rowHex = normalizeHex(row.hex);
      const hasRealHex = Boolean(rowHex && rowHex !== PLACEHOLDER_HEX);
      const hexDist = (normalizedDesiredHex && hasRealHex)
        ? colorDistance(normalizedDesiredHex, rowHex)
        : null;
      // hexScore 0–100: 100 = perfect hex match, 0 = placeholder/no hex or very far.
      const hexScore = hexDist !== null ? Math.max(0, (150 - hexDist) / 1.5) : 0;
      const textScore = desired ? Math.max(0, scoreFilamentMatch(desired, name)) : 0;
      // Combined (higher = better). textScore/3 gives a strong name match up to ~333 pts,
      // so "White PLA" (unconfigured hex, text score ~500) outranks "Cyan PLA" (real hex,
      // hex score ~67) when the desired color is white.
      ranked.push({
        name: name,
        combined: hexScore + textScore / 3,
        remaining_g: Number((row && row.remaining_g) || 0),
      });
    });

    ranked.sort(function (left, right) {
      if (left.combined !== right.combined) return right.combined - left.combined;
      if (left.remaining_g !== right.remaining_g) return right.remaining_g - left.remaining_g;
      return left.name.localeCompare(right.name);
    });

    return ranked.length ? ranked[0].name : String((allowedRows[0] && allowedRows[0].name) || "").trim();
  }

  function getProfileColorSlots(profileObj, totalParts) {
    const colors = Array.isArray(profileObj && profileObj.colors) ? profileObj.colors.slice() : [];
    const slotCount = Math.max(Number(totalParts || 0), colors.length);
    const totalWeight = Number((profileObj && profileObj.weight_g) || 0);
    while (colors.length < slotCount) {
      colors.push({
        name: "",
        hex: "#8b8b8b",
        used_g: slotCount > 0 ? totalWeight / slotCount : 0,
      });
    }
    return colors;
  }

  function getAvailabilityContext() {
    const profileCustomization = getProfileCustomization(modalState.item, modalState.profileObj);
    const profileInsufficient = Array.isArray(profileCustomization && profileCustomization.insufficient_filaments)
      ? profileCustomization.insufficient_filaments
      : (Array.isArray(modalState.item && modalState.item.insufficient_filaments) ? modalState.item.insufficient_filaments : []);
    const profileInsufficientByPart = (profileCustomization && typeof profileCustomization.insufficient_filaments_by_part === "object")
      ? profileCustomization.insufficient_filaments_by_part
      : {};
    const profileSufficientByPart = (profileCustomization && typeof profileCustomization.sufficient_filaments_by_part === "object")
      ? profileCustomization.sufficient_filaments_by_part
      : {};
    const hasAnyByPart = Object.keys(profileInsufficientByPart).length > 0 || Object.keys(profileSufficientByPart).length > 0;
    const lazyInsufficient = (modalState.profileObj && modalState.lazyAvailability)
      ? (modalState.lazyAvailability[modalState.profileObj.name] || [])
      : [];
    return {
      profileInsufficient: profileInsufficient,
      profileInsufficientByPart: profileInsufficientByPart,
      profileSufficientByPart: profileSufficientByPart,
      hasAnyByPart: hasAnyByPart,
      lazyInsufficientSet: new Set((lazyInsufficient || []).map(function (name) {
        return String(name || "").trim().toLowerCase();
      }).filter(Boolean)),
      profileColors: getProfileColorSlots(modalState.profileObj, modalState.parts.length),
      profileTotalWg: Number((modalState.profileObj && modalState.profileObj.weight_g) || (modalState.item && (modalState.item.base_weight || modalState.item.weight_g || modalState.item.model_weight)) || 0),
    };
  }

  function isFilamentUnavailableForPart(filament, partIdx, availability) {
    const context = availability || getAvailabilityContext();
    const partKey = "part_" + partIdx;
    const partManualList = Array.isArray(context.profileInsufficientByPart[partKey]) ? context.profileInsufficientByPart[partKey] : null;
    const partAllowList = Array.isArray(context.profileSufficientByPart[partKey]) ? context.profileSufficientByPart[partKey] : [];
    const name = String((filament && filament.name) || "").trim().toLowerCase();
    if (!name) return true;
    const manualAllow = partAllowList.some(function (entry) {
      return String(entry || "").trim().toLowerCase() === name;
    });
    const manualInsuf = ((partManualList || (context.hasAnyByPart ? [] : context.profileInsufficient))).some(function (entry) {
      return String(entry || "").trim().toLowerCase() === name;
    });
    const lazyInsuf = context.lazyInsufficientSet.has(name);
    const slot = context.profileColors[partIdx] || null;
    const slotWg = slot && Number(slot.used_g || 0) > 0 ? Number(slot.used_g) : context.profileTotalWg;
    const autoInsuf = Boolean(filament && filament.out_of_stock) || (slotWg > 0 && Number((filament && filament.remaining_g) || 0) < slotWg);
    return manualInsuf || lazyInsuf || (!manualAllow && autoInsuf);
  }

  function derivePartsForProfile(item, profileObj) {
    const profileCustomization = getProfileCustomization(item, profileObj);
    const customParts = Array.isArray(profileCustomization && profileCustomization.parts_configuration)
      ? profileCustomization.parts_configuration
      : [];
    const profileColors = getProfileColorSlots(profileObj, customParts.length || 0);
    if (customParts.length) {
      return customParts.map(function (part, idx) {
        const slot = profileColors[idx] || null;
        const suggestedName = String(part.suggested_filament || part.filament || part.color || (slot && slot.name) || "").trim();
        const suggestedHex = String(part.suggested_hex || part.hex || part.color_hex || (slot && slot.hex) || "").trim();
        const providedName = String(part.part || part.name || "").trim();
        return buildPartRecord(providedName, suggestedName, suggestedHex, idx, Number(part.used_g || (slot && slot.used_g) || 0));
      });
    }

    const baseParts = deriveParts(item);
    if (!profileColors.length) return baseParts;

    const slotCount = Math.max(baseParts.length, profileColors.length);
    const parts = [];
    for (let idx = 0; idx < slotCount; idx += 1) {
      const basePart = baseParts[idx] || null;
      const slot = profileColors[idx] || null;
      const slotName = String((slot && slot.name) || "").trim();
      const basePartName = String((basePart && basePart.name) || "").trim() || ("Part " + (idx + 1));
      const suggestedName = String((basePart && basePart.suggested) || slotName || item.suggested_filament || "").trim();
      const suggestedHex = String((basePart && basePart.suggestedHex) || (slot && slot.hex) || "").trim();
      parts.push(buildPartRecord(basePartName, suggestedName, suggestedHex, idx, Number((basePart && basePart.used_g) || (slot && slot.used_g) || 0)));
    }
    return parts;
  }

  function getProfileImage(item, profileObj) {
    const profileCustomization = getProfileCustomization(item, profileObj);
    return String((profileCustomization && profileCustomization.image_url) || item.image_url || "").trim();
  }

  function renderPrice() {
    const priceEl = document.getElementById("featured-modal-price");
    if (!priceEl) return;
    const total = modalState.profileObj ? Number(modalState.profileObj.price || 0) : 0;
    priceEl.textContent = "Rp" + Number(total).toLocaleString("id-ID");
  }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function getSelectedCartItems(items) {
    return (items || []).filter(function (item) {
      return cartState.selectedIds.has(String((item && item.id) || ""));
    });
  }

  function syncCartSelection(items) {
    const ids = (items || []).map(function (item) { return String((item && item.id) || ""); }).filter(Boolean);
    const idSet = new Set(ids);
    cartState.selectedIds = new Set(Array.from(cartState.selectedIds).filter(function (id) { return idSet.has(id); }));
    if (cartState.selectedIds.size === 0 && ids.length > 0) {
      ids.forEach(function (id) { cartState.selectedIds.add(id); });
    }
  }

  function renderDrawerTotals(items) {
    const totalEl = document.getElementById("cart-total-price");
    const checkoutBtn = document.getElementById("cart-checkout-btn");
    const selectAllEl = document.getElementById("cart-select-all");
    const selectedCountEl = document.getElementById("cart-selected-count");
    const selectedItems = getSelectedCartItems(items);
    const total = selectedItems.reduce(function (sum, item) {
      return sum + (Number(item.estimatedPrice) || 0) * normalizeQuantity(item.quantity);
    }, 0);
    const selectedUnits = selectedItems.reduce(function (sum, item) {
      return sum + normalizeQuantity(item.quantity);
    }, 0);

    if (totalEl) totalEl.textContent = formatPrice(total);
    if (checkoutBtn) {
      const hasSelection = selectedItems.length > 0;
      checkoutBtn.textContent = checkoutState.inFlight ? "Processing..." : "Proceed to Checkout";
      checkoutBtn.disabled = checkoutState.inFlight || !hasSelection;
      checkoutBtn.setAttribute("data-empty", hasSelection ? "false" : "true");
    }
    if (selectedCountEl) selectedCountEl.textContent = selectedItems.length + " item(s), " + selectedUnits + " unit(s) selected";
    if (selectAllEl) {
      const selectableCount = (items || []).length;
      selectAllEl.checked = selectableCount > 0 && selectedItems.length === selectableCount;
    }
  }

  function bindCartDrawerEvents(items) {
    document.querySelectorAll(".cart-item-select").forEach(function (el) {
      el.addEventListener("change", function () {
        const id = String(el.getAttribute("data-id") || "");
        if (!id) return;
        if (el.checked) cartState.selectedIds.add(id);
        else cartState.selectedIds.delete(id);
        renderDrawerTotals(items);
      });
    });

    const selectAllEl = document.getElementById("cart-select-all");
    if (selectAllEl) {
      selectAllEl.onchange = function () {
        const ids = (items || []).map(function (item) { return String((item && item.id) || ""); }).filter(Boolean);
        if (selectAllEl.checked) {
          ids.forEach(function (id) { cartState.selectedIds.add(id); });
        } else {
          cartState.selectedIds.clear();
        }
        renderCartDrawer();
      };
    }

    document.querySelectorAll(".cart-item-remove").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        const id = String(btn.getAttribute("data-id") || "").trim();
        const target = (cartState.items || []).find(function (row) { return String((row && row.id) || "") === id; }) || null;
        const orderId = String((target && (target.orderId || target.order_id || target.id)) || "").trim();
        if (!id) return;

        const removeItemAndRender = function () {
          cartState.items = (cartState.items || []).filter(function (row) { return String(row.id || "") !== id; });
          cartState.selectedIds.delete(id);
          saveCartItems();
          renderCartDrawer();
        };

        if (!orderId) {
          removeItemAndRender();
          return;
        }

        if (orderId) {
          try {
            btn.disabled = true;
            btn.classList.add("deleting");
            btn.setAttribute("aria-busy", "true");
            const originalText = btn.innerHTML;
            btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="animation: spin 0.75s linear infinite;"><circle cx="12" cy="12" r="10"></circle></svg>';
            
            const params = new URLSearchParams();
            if (runtime.authToken) params.set("ext_auth", runtime.authToken);
            const suffix = params.toString() ? ("?" + params.toString()) : "";
            const response = await fetch(`${runtime.apiBase}/cart/remove/${encodeURIComponent(orderId)}${suffix}`, {
              method: "POST",
              credentials: "include",
              headers: getHeaders(),
            });

            if (!response.ok) {
              throw new Error("Failed to remove cart item");
            }

            btn.classList.remove("deleting");
            btn.classList.add("deleted");
            btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
            await new Promise(function (resolve) { setTimeout(resolve, 350); });

            removeItemAndRender();
          } catch (e) {
            btn.classList.remove("deleting");
            btn.classList.add("error");
            btn.innerHTML = originalText;
            btn.disabled = false;
            btn.removeAttribute("aria-busy");
            setTimeout(function () {
              btn.classList.remove("error");
            }, 1800);
            return;
          }
        }
      });
    });
  }

  function toggleCartFooterVisibility(isSuccess) {
    ["cart-select-row", "cart-total-row", "cart-checkout-btn", "cart-view-btn", "cart-feedback"].forEach(function (id) {
      const el = document.getElementById(id);
      if (el) el.hidden = Boolean(isSuccess);
    });
  }

  function renderCheckoutSuccessView() {
    const bodyEl = document.getElementById("cart-drawer-body");
    if (!bodyEl) return;
    const result = checkoutState.result || {};
    const remainingCount = Array.isArray(cartState.items) ? cartState.items.length : 0;
    const orderCount = Number(result.orderCount || 0);
    const unitCount = Number(result.unitCount || 0);
    const summaryCopy = remainingCount
      ? "The selected items were checked out. You still have " + remainingCount + " item(s) left in your cart."
      : "Your selected items were checked out successfully. You can keep browsing without leaving MakerWorld.";

    bodyEl.innerHTML = ''
      + '<section class="cart-checkout-success">'
      + '<div class="cart-checkout-success-card">'
      + '<div class="cart-checkout-success-icon" aria-hidden="true">'
      + '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
      + '</div>'
      + '<h3 class="cart-checkout-success-title">' + escapeHtml(result.message || "Checkout complete.") + '</h3>'
      + '<p class="cart-checkout-success-copy">' + escapeHtml(summaryCopy) + '</p>'
      + '<div class="cart-checkout-success-stats">'
      + '<div class="cart-checkout-success-stat"><span class="cart-checkout-success-stat-value">' + escapeHtml(String(orderCount)) + '</span><span class="cart-checkout-success-stat-label">Orders created</span></div>'
      + '<div class="cart-checkout-success-stat"><span class="cart-checkout-success-stat-value">' + escapeHtml(formatPrice(result.grandTotal || 0)) + '</span><span class="cart-checkout-success-stat-label">Total</span></div>'
      + '</div>'
      + '<div class="cart-checkout-success-actions">'
      + '<button id="cart-success-view-orders" class="browse-card-btn" type="button">View Orders</button>'
      + '<button id="cart-success-back" class="muted-btn" type="button">' + (remainingCount ? 'Back to Cart' : 'Continue Browsing') + '</button>'
      + '</div>'
      + (unitCount > 0 ? '<p class="cart-checkout-success-copy">Processed ' + escapeHtml(String(unitCount)) + ' unit(s).</p>' : '')
      + '</div>'
      + '</section>';

    document.getElementById("cart-success-view-orders")?.addEventListener("click", function () {
      closeCartDrawer(true);
      openPortalPage("/history");
    });
    document.getElementById("cart-success-back")?.addEventListener("click", function () {
      clearCheckoutResult();
      renderCartDrawer();
    });
  }

  async function checkoutSelectedCartItems() {
    const selectedItems = getSelectedCartItems(cartState.items || []);
    if (!selectedItems.length) {
      setCartFeedback("Select at least one cart item.", true);
      return;
    }

    checkoutState.inFlight = true;
    checkoutState.result = null;
    setCartFeedback("Submitting checkout...", false);
    renderDrawerTotals(cartState.items || []);

    try {
      await bootstrapRuntime();
      const response = await fetch(`${runtime.apiBase}/checkout`, {
        method: "POST",
        credentials: "include",
        headers: getHeaders({
          "Accept": "application/json",
          "Content-Type": "application/json",
        }),
        body: JSON.stringify({
          items: selectedItems,
          ext_auth: runtime.authToken,
          response_mode: "json",
        }),
      });
      const payload = await response.json().catch(function () { return {}; });
      if (!response.ok || !payload.ok) {
        if (response.status === 401 || response.status === 403) {
          throw new Error("Session expired. Open the extension popup and sign in again.");
        }
        throw new Error(payload.error || "Checkout failed. Please try again.");
      }

      const removedIds = new Set(selectedItems.map(function (item) {
        return String((item && (item.id || item.orderId || item.order_id)) || "").trim();
      }).filter(Boolean));
      cartState.items = (cartState.items || []).filter(function (row) {
        const rowId = String((row && (row.id || row.orderId || row.order_id)) || "").trim();
        return !removedIds.has(rowId);
      });
      cartState.selectedIds = new Set(Array.from(cartState.selectedIds).filter(function (id) {
        return !removedIds.has(String(id || "").trim());
      }));
      saveCartItems();

      checkoutState.inFlight = false;
      checkoutState.result = {
        message: String(payload.message || "Checkout complete."),
        orderCount: Number(payload.order_count || removedIds.size || 0),
        unitCount: Number(payload.unit_count || 0),
        grandTotal: Number(payload.grand_total || 0),
        orderIds: Array.isArray(payload.order_ids) ? payload.order_ids : [],
      };
      setCartFeedback("", false);
      renderCartDrawer();
    } catch (error) {
      checkoutState.inFlight = false;
      setCartFeedback((error && error.message) ? error.message : "Checkout failed. Please try again.", true);
      renderDrawerTotals(cartState.items || []);
    }
  }

  function renderCartDrawer() {
    const bodyEl = document.getElementById("cart-drawer-body");
    if (!bodyEl) return;

    const items = Array.isArray(cartState.items) ? cartState.items : [];
    syncCartSelection(items);

    if (checkoutState.result) {
      toggleCartFooterVisibility(true);
      renderCheckoutSuccessView();
      return;
    }

    toggleCartFooterVisibility(false);

    if (!items.length) {
      bodyEl.innerHTML = '<div class="cart-drawer-empty"><p>Your cart is empty.</p></div>';
      renderDrawerTotals(items);
      return;
    }

    bodyEl.innerHTML = items.map(function (item) {
      const safeId = escapeHtml(String(item.id || ""));
      const qty = normalizeQuantity(item.quantity);
      const colorLabel = item.colorMode === "multi"
        ? ((Array.isArray(item.multiMappings) && item.multiMappings.length) ? item.multiMappings.length + " colors" : "Multi-color")
        : String(item.singleFilament || "Color TBD");
      const linePrice = formatPrice((Number(item.estimatedPrice) || 0) * qty);
      const safeChecked = cartState.selectedIds.has(String(item.id || "")) ? "checked" : "";
      const swatch = escapeHtml(item.filamentHex || "#cccccc");
      return '<div class="cart-drawer-item">'
        + '<input class="cart-item-select" type="checkbox" data-id="' + safeId + '" ' + safeChecked + '>'
        + '<div class="cart-item-main">'
        + '<div class="cart-item-swatch" style="background:' + swatch + ';" title="' + escapeHtml(colorLabel) + '"></div>'
        + '<div class="cart-item-details">'
        + '<p class="cart-item-name">' + escapeHtml(item.displayName || "Unnamed Model") + '</p>'
        + '<p class="cart-item-meta">Qty ' + qty + ' - ' + escapeHtml(colorLabel) + '</p>'
        + '</div>'
        + '</div>'
        + '<div class="cart-item-side">'
        + '<p class="cart-item-price">' + linePrice + '</p>'
        + '<button class="cart-item-remove" type="button" data-id="' + safeId + '" aria-label="Remove item">Remove</button>'
        + '</div>'
        + '</div>';
    }).join("");

    bindCartDrawerEvents(items);
    renderDrawerTotals(items);
  }

  function hideOrderModalOnly() {
    document.getElementById("featured-order-modal")?.classList.remove("open");
    setModalOpenClass(false);
  }

  function openCartDrawer() {
    renderCartDrawer();
    document.getElementById("cart-backdrop")?.classList.add("open");
    document.getElementById("cart-drawer")?.classList.add("open");
  }

  function closeCartDrawer(keepOverlayOpen) {
    document.getElementById("cart-backdrop")?.classList.remove("open");
    document.getElementById("cart-drawer")?.classList.remove("open");
    if (!keepOverlayOpen) {
      postToParent("close", {});
    }
  }

  function applyProfileSelection(profileObj, rerenderProfiles) {
    if (!profileObj) return;
    modalState.profile = profileObj.name;
    modalState.profileObj = profileObj;
    applySelectedProfileConfiguration();
    if (rerenderProfiles) renderProfiles();
    renderPrice();
  }

  function destroyProfileSelectControl() {
    if (modalState.profileSelectControl && typeof modalState.profileSelectControl.destroy === "function") {
      modalState.profileSelectControl.destroy();
    }
    modalState.profileSelectControl = null;
  }

  function initTomSelectControl(selectEl, profiles) {
    const control = new window.TomSelect(selectEl, {
      create: false,
      maxItems: 1,
      controlInput: null,
      searchField: ["text"],
      dropdownParent: "body",
      render: {
        option: function (data, escape) {
          const raw = String(data.text || "");
          const parts = raw.split(" — ");
          const name = escape(parts[0] || raw);
          const price = parts[1] ? escape(parts[1]) : "";
          return '<div class="profile-option-row">'
            + '<span class="profile-option-name">' + name + '</span>'
            + (price ? '<span class="profile-option-price">' + price + '</span>' : "")
            + "</div>";
        },
        item: function (data, escape) {
          const raw = String(data.text || "");
          const parts = raw.split(" — ");
          const name = escape(parts[0] || raw);
          return '<div class="profile-item-chip">' + name + "</div>";
        },
      },
    });
    control.on("change", function (value) {
      const chosen = String(value || "").trim();
      const matched = profiles.find(function (p) { return String(p.name || "").trim() === chosen; }) || profiles[0] || null;
      applyProfileSelection(matched, false);
    });
    return control;
  }

  function initFallbackProfileSelectControl(selectEl, profiles) {
    const wrapper = document.createElement("div");
    wrapper.className = "ts-wrapper single mw-ts-fallback";
    const control = document.createElement("div");
    control.className = "ts-control";
    const itemEl = document.createElement("div");
    itemEl.className = "item profile-item-chip";
    const arrow = document.createElement("span");
    arrow.className = "mw-ts-arrow";
    arrow.setAttribute("aria-hidden", "true");
    arrow.textContent = "▾";
    control.appendChild(itemEl);
    control.appendChild(arrow);

    const dropdown = document.createElement("div");
    dropdown.className = "ts-dropdown";
    const content = document.createElement("div");
    content.className = "ts-dropdown-content";
    dropdown.appendChild(content);

    function setCurrentValue(value) {
      const current = profiles.find(function (p) { return String(p.name || "").trim() === String(value || "").trim(); }) || profiles[0] || null;
      if (!current) {
        itemEl.textContent = "";
        return;
      }
      const price = Number(current.price || 0);
      itemEl.textContent = price > 0 ? current.name + " — Rp" + price.toLocaleString("id-ID") : current.name;
      const options = content.querySelectorAll(".option");
      options.forEach(function (opt) {
        opt.classList.toggle("active", String(opt.getAttribute("data-value") || "") === current.name);
      });
    }

    profiles.forEach(function (profileObj) {
      const option = document.createElement("div");
      option.className = "option";
      option.setAttribute("data-value", profileObj.name || "");
      const price = Number(profileObj.price || 0);
      option.innerHTML = '<div class="profile-option-row">'
        + '<span class="profile-option-name">' + escapeHtml(profileObj.name || "") + "</span>"
        + (price > 0 ? ('<span class="profile-option-price">Rp' + escapeHtml(price.toLocaleString("id-ID")) + "</span>") : "")
        + "</div>";
      option.addEventListener("click", function () {
        selectEl.value = profileObj.name || "";
        setCurrentValue(selectEl.value);
        dropdown.classList.remove("open");
        const matched = profiles.find(function (p) { return String(p.name || "").trim() === String(selectEl.value || "").trim(); }) || profiles[0] || null;
        applyProfileSelection(matched, false);
      });
      content.appendChild(option);
    });

    const toggleDropdown = function () {
      dropdown.classList.toggle("open");
    };
    const closeDropdown = function (event) {
      if (!wrapper.contains(event.target) && !dropdown.contains(event.target)) {
        dropdown.classList.remove("open");
      }
    };

    control.addEventListener("click", toggleDropdown);
    document.addEventListener("click", closeDropdown);
    setCurrentValue(selectEl.value);

    selectEl.style.display = "none";
    selectEl.insertAdjacentElement("afterend", wrapper);
    wrapper.appendChild(control);
    wrapper.appendChild(dropdown);

    return {
      destroy: function () {
        control.removeEventListener("click", toggleDropdown);
        document.removeEventListener("click", closeDropdown);
        wrapper.remove();
        selectEl.style.display = "";
      },
    };
  }

  function renderParts() {
    const partsWrap = document.getElementById("featured-modal-parts");
    if (!partsWrap) return;
    const filaments = getFilamentCatalog();
    partsWrap.innerHTML = "";
    const availability = getAvailabilityContext();

    modalState.parts.forEach(function (part, partIdx) {
      const card = document.createElement("div");
      card.className = "part-card";
      const title = document.createElement("p");
      title.className = "part-title";
      title.textContent = part.name;
      card.appendChild(title);

      const grid = document.createElement("div");
      grid.className = "swatch-grid";
      filaments.forEach(function (filament) {
        const swatch = document.createElement("button");
        swatch.type = "button";
        swatch.className = "swatch-btn";
        swatch.style.background = filament.hex || "#8b8b8b";
        swatch.setAttribute("data-name", filament.name);
        swatch.title = filament.name;
        const isInsuf = isFilamentUnavailableForPart(filament, partIdx, availability);

        if (isInsuf) {
          swatch.classList.add("swatch-insufficient");
          swatch.title = filament.name + " - Not enough available filament";
          swatch.disabled = true;
          swatch.setAttribute("aria-disabled", "true");
          const mark = document.createElement("span");
          mark.className = "swatch-insufficient-mark";
          mark.setAttribute("aria-hidden", "true");
          swatch.appendChild(mark);
        }

        const selected = String(modalState.selections[part.key] || "").toLowerCase() === filament.name.toLowerCase();
        if (selected && isInsuf) {
          delete modalState.selections[part.key];
        } else if (selected) {
          swatch.classList.add("active");
        }
        if (String(part.resolvedSuggested || part.suggested || "").toLowerCase() === filament.name.toLowerCase()) swatch.classList.add("suggested");
        swatch.addEventListener("click", function () {
          if (isInsuf) return;
          modalState.selections[part.key] = filament.name;
          renderParts();
        });
        grid.appendChild(swatch);
      });

      card.appendChild(grid);
      partsWrap.appendChild(card);
    });
  }

  function applySelectedProfileConfiguration() {
    if (!modalState.item) return;
    modalState.parts = derivePartsForProfile(modalState.item, modalState.profileObj);
    modalState.selections = {};
    const filamentCatalog = getFilamentCatalog();
    const availability = getAvailabilityContext();
    modalState.parts.forEach(function (part, idx) {
      const resolved = pickClosestFilamentName(part.suggested || "", part.suggestedHex || "", filamentCatalog, function (row) {
        return !isFilamentUnavailableForPart(row, idx, availability);
      });
      part.resolvedSuggested = resolved || "";
      modalState.selections[part.key] = resolved || "";
    });
    const img = document.getElementById("featured-modal-image");
    if (img) {
      const imageUrl = getProfileImage(modalState.item, modalState.profileObj);
      img.style.backgroundImage = imageUrl ? `url('${imageUrl.replace(/'/g, "\\'")}')` : "none";
    }
    renderParts();
  }

  function renderProfiles() {
    const wrap = document.getElementById("featured-modal-profile-pills");
    const selectWrap = document.getElementById("featured-modal-profile-select-wrap");
    const selectEl = document.getElementById("featured-modal-profile-select");
    if (!wrap || !selectWrap || !selectEl) return;

    destroyProfileSelectControl();

    const profiles = modalState.profiles || [];
    wrap.innerHTML = "";
    selectEl.innerHTML = "";
    selectEl.style.display = "";

    const useDropdown = profiles.length > 4;
    wrap.style.display = useDropdown ? "none" : "flex";
    selectWrap.style.display = useDropdown ? "block" : "none";

    profiles.forEach(function (profileObj) {
      const profileName = profileObj.name || "";
      const profilePrice = Number(profileObj.price || 0);
      const label = profilePrice > 0 ? profileName + " — Rp" + profilePrice.toLocaleString("id-ID") : profileName;
      if (useDropdown) {
        const opt = document.createElement("option");
        opt.value = profileName;
        opt.textContent = label;
        opt.selected = modalState.profile === profileName;
        selectEl.appendChild(opt);
        return;
      }

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "profile-pill" + (modalState.profile === profileName ? " active" : "");
      btn.textContent = label;
      btn.addEventListener("click", function () {
        applyProfileSelection(profileObj, true);
      });
      wrap.appendChild(btn);
    });

    if (useDropdown) {
      selectEl.onchange = function () {
        const chosen = String(selectEl.value || "");
        const matched = (modalState.profiles || []).find(function (p) { return String(p.name || "") === chosen; }) || (modalState.profiles && modalState.profiles[0]) || null;
        applyProfileSelection(matched, false);
      };
      if (typeof window.TomSelect === "function") {
        modalState.profileSelectControl = initTomSelectControl(selectEl, profiles);
      } else {
        modalState.profileSelectControl = initFallbackProfileSelectControl(selectEl, profiles);
      }
    }
  }

  function setModalOpenClass(isOpen) {
    const action = isOpen ? "add" : "remove";
    try {
      if (document.documentElement && document.documentElement.classList && typeof document.documentElement.classList[action] === "function") {
        document.documentElement.classList[action]("modal-open");
      }
    } catch (error) {
      // Ignore transient DOM teardown races.
    }
    try {
      if (document.body && document.body.classList && typeof document.body.classList[action] === "function") {
        document.body.classList[action]("modal-open");
      }
    } catch (error) {
      // Ignore transient DOM teardown races.
    }
  }

  function closeModal() {
    document.getElementById("featured-order-modal")?.classList.remove("open");
    setModalOpenClass(false);
    postToParent("close", {});
  }

  function openModal(item) {
    if (!item || !item.title) {
      setStatus("Could not load model details.", true);
      return;
    }
    const modal = document.getElementById("featured-order-modal");
    if (!modal) return;

    document.getElementById("featured-modal-title").textContent = item.title;
    document.getElementById("featured-modal-description").textContent = item.description || "Configure your print setup before ordering.";

    modalState.item = item;
    modalState.profiles = normalizeItemProfiles(item);
    modalState.profileObj = modalState.profiles.find(function (p) { return p.is_default; }) || modalState.profiles[0] || null;
    modalState.profile = modalState.profileObj ? modalState.profileObj.name : "";
    modalState.lazyAvailability = {};

    applySelectedProfileConfiguration();
    renderProfiles();
    renderPrice();

    modal.classList.add("open");
    setModalOpenClass(true);
    setStatus("", false);
  }

  async function loadCanonicalData() {
    await bootstrapRuntime();

    const itemParams = new URLSearchParams({ model_url: modelUrl });
    if (runtime.authToken) itemParams.set("ext_auth", runtime.authToken);

    const [itemRes, appDataRes] = await Promise.all([
      fetch(`${runtime.apiBase}/extension-api/hover-order-item?${itemParams.toString()}`, {
        method: "GET",
        credentials: "include",
        headers: getHeaders(),
      }),
      fetch(`${runtime.apiBase}/extension-api/app-data`, {
        method: "GET",
        credentials: "include",
        headers: getHeaders(),
      }),
    ]);

    const itemPayload = await itemRes.json().catch(function () { return {}; });
    const appDataPayload = await appDataRes.json().catch(function () { return {}; });

    if (!itemRes.ok || !itemPayload.ok || !itemPayload.item) {
      throw new Error(itemPayload.error || "Failed to load model details.");
    }

    runtime.filaments = Array.isArray(appDataPayload && appDataPayload.filaments)
      ? appDataPayload.filaments
      : [];

    const pageDataEl = document.getElementById("page-data");
    if (pageDataEl) {
      pageDataEl.dataset.filaments = JSON.stringify(runtime.filaments || []);
    }

    openModal(itemPayload.item);
  }

  async function confirmOrder() {
    const item = modalState.item || {};
    if (!item) return;
    const itemTitle = String(item.title || item.name || "Featured Print").trim();
    const itemLink = String(item.makerworld_url || item.link || modelUrl || "").trim();
    if (!itemLink) {
      setStatus("Missing model link. Re-open the order popup and try again.", true);
      return;
    }

    const profile = modalState.profile || "";
    const selectedParts = (modalState.parts || []).map(function (part, idx) {
      return {
        part: part.name,
        filament: modalState.selections[part.key] || "",
        partIdx: idx,
      };
    });

    const hasUnselectedPart = selectedParts.some(function (entry) {
      return !String(entry.filament || "").trim();
    });
    if (hasUnselectedPart) {
      setStatus("Please select an available color for every part before adding to cart.", true);
      return;
    }

    const activeCustomization = getProfileCustomization(item, modalState.profileObj);
    const manualInsufficient = new Set(
      (Array.isArray(activeCustomization && activeCustomization.insufficient_filaments)
        ? activeCustomization.insufficient_filaments
        : (Array.isArray(item.insufficient_filaments) ? item.insufficient_filaments : []))
        .map(function (n) { return String(n || "").trim().toLowerCase(); })
        .filter(Boolean)
    );
    const manualInsufficientByPart = (activeCustomization && typeof activeCustomization.insufficient_filaments_by_part === "object")
      ? activeCustomization.insufficient_filaments_by_part
      : {};
    const manualSufficientByPart = (activeCustomization && typeof activeCustomization.sufficient_filaments_by_part === "object")
      ? activeCustomization.sufficient_filaments_by_part
      : {};
    const hasAnyByPartManual = Object.keys(manualInsufficientByPart).length > 0 || Object.keys(manualSufficientByPart).length > 0;
    const catalog = getFilamentCatalog();
    const valProfileColors = Array.isArray(modalState.profileObj && modalState.profileObj.colors) ? modalState.profileObj.colors : [];
    const valTotalWg = Number((modalState.profileObj && modalState.profileObj.weight_g) || (item && (item.base_weight || item.weight_g || item.model_weight)) || 0);

    const picksUnavailable = selectedParts.some(function (entry) {
      const key = String(entry.filament || "").trim().toLowerCase();
      if (!key) return true;
      const partKey = "part_" + Number(entry.partIdx || 0);
      const partManualSet = new Set(
        (Array.isArray(manualInsufficientByPart[partKey]) ? manualInsufficientByPart[partKey] : [])
          .map(function (n) { return String(n || "").trim().toLowerCase(); })
          .filter(Boolean)
      );
      const partAllowSet = new Set(
        (Array.isArray(manualSufficientByPart[partKey]) ? manualSufficientByPart[partKey] : [])
          .map(function (n) { return String(n || "").trim().toLowerCase(); })
          .filter(Boolean)
      );
      if ((partManualSet.size ? partManualSet : (hasAnyByPartManual ? new Set() : manualInsufficient)).has(key)) return true;
      const filament = catalog.find(function (f) { return String(f.name || "").toLowerCase() === key; }) || null;
      if (!filament && catalog.length > 0) return true;
      if (filament && !partAllowSet.has(key) && Boolean(filament.out_of_stock)) return true;
      const slotWg = (valProfileColors[entry.partIdx] && Number(valProfileColors[entry.partIdx].used_g || 0) > 0)
        ? Number(valProfileColors[entry.partIdx].used_g)
        : valTotalWg;
      return !partAllowSet.has(key) && slotWg > 0 && Number(filament.remaining_g || 0) < slotWg;
    });

    if (picksUnavailable) {
      setStatus("One or more selected colors are unavailable. Please pick colors without a red X.", true);
      return;
    }

    const suggestedColors = selectedParts.map(function (entry) { return entry.part + ": " + entry.filament; }).join(" | ");
    const primaryFilament = selectedParts.length ? selectedParts[0].filament : "";
    const dynamicPrice = modalState.profileObj ? Number(modalState.profileObj.price || 0) : 0;

    const btn = document.getElementById("featured-modal-confirm");
    const originalText = btn ? btn.textContent : "";
    if (btn) {
      btn.textContent = "Sending...";
      btn.disabled = true;
    }

    try {
      const params = new URLSearchParams();
      if (runtime.authToken) params.set("ext_auth", runtime.authToken);
      const suffix = params.toString() ? `?${params.toString()}` : "";
      const res = await fetch(`${runtime.apiBase}/create_featured_order${suffix}`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        credentials: "include",
        body: JSON.stringify({
          title: itemTitle,
          makerworld_link: itemLink,
          price: dynamicPrice,
          filament: primaryFilament,
          profile: profile,
          suggested_colors: suggestedColors,
          category_choices: selectedParts,
          item_id: item.id,
          ext_auth: runtime.authToken,
        }),
      });
      const data = await res.json().catch(function () { return {}; });
      if (res.ok && data.order_id) {
        const filamentCatalog = getFilamentCatalog();
        const pickedFilament = filamentCatalog.find(function (row) {
          return String((row && row.name) || "").toLowerCase() === String(primaryFilament || "").toLowerCase();
        }) || null;
        const cartItem = {
          id: Math.random().toString(36).slice(2, 10),
          orderId: data.order_id,
          displayName: itemTitle,
          link: itemLink,
          colorMode: selectedParts.length > 1 ? "multi" : "single",
          singleFilament: primaryFilament || "",
          filamentHex: (pickedFilament && pickedFilament.hex) ? pickedFilament.hex : "#8b8b8b",
          multiMappings: selectedParts,
          weight: Number(item.model_weight || 0),
          profile: profile,
          preferredDeliveryDate: "",
          estimatedPrice: Number(dynamicPrice || 0),
          quantity: 1,
          addedAt: new Date().toISOString(),
        };
        postToParent("cart-add", { item: cartItem, delayMs: 1000 });
        postToParent("toast", { ok: true, message: "Added to cart." });
        postToParent("close", {});
      } else {
        setStatus(data.error || "Unable to create order.", true);
      }
    } catch (error) {
      setStatus("Failed to create order. Please try again.", true);
    } finally {
      if (btn) {
        btn.textContent = originalText;
        btn.disabled = false;
      }
    }
  }

  function openCartPage() {
    openPortalPage("/cart");
  }

  async function loadBackendCart() {
    try {
      await bootstrapRuntime();
      const params = new URLSearchParams();
      if (runtime.authToken) params.set("ext_auth", runtime.authToken);
      const suffix = params.toString() ? `?${params.toString()}` : "";
      const res = await fetch(`${runtime.apiBase}/cart/orders${suffix}`, {
        method: "GET",
        credentials: "include",
        headers: getHeaders(),
      });
      if (!res.ok) {
        if (res.status === 401 || res.status === 403) {
          setStatus("Session expired. Open the extension popup and sign in again.", true);
        }
        return;
      }
      const data = await res.json().catch(function () { return {}; });
      if (!data.items || !Array.isArray(data.items)) return;
      const seenOrderIds = new Set();
      const merged = [];
      data.items.forEach(function (order) {
        const oid = String(order.id || "").trim();
        if (!oid) return;
        merged.push({
          id: oid,
          orderId: oid,
          displayName: order.product_name || order.name || "Unnamed",
          link: order.link || "",
          colorMode: (order.color || "").includes("|") ? "multi" : "single",
          singleFilament: order.color ? String(order.color).split("|")[0].split(":").pop().trim() : "",
          multiMappings: [],
          filamentHex: "#8b8b8b",
          weight: (Number(order.print_weight_g) || 0) / (Number(order.quantity) || 1),
          profile: order.profile || "",
          estimatedPrice: Number(order.print_price || 0),
          quantity: normalizeQuantity(order.quantity || 1),
          addedAt: order.created_at || "",
        });
        seenOrderIds.add(oid);
      });
      (cartState.items || []).forEach(function (item) {
        const oid = String(item.orderId || item.order_id || item.id || "").trim();
        // Only keep local items that have no orderId (unsaved drafts).
        // Items with an orderId not returned by the backend have left the cart (checked out, etc.).
        if (!oid) {
          merged.push(item);
        }
      });
      cartState.items = merged;
      saveCartItems();
      renderCartDrawer();
    } catch (e) {}
  }

  document.getElementById("featured-modal-cancel")?.addEventListener("click", closeModal);
  document.getElementById("featured-modal-backdrop")?.addEventListener("click", closeModal);
  document.getElementById("cart-drawer-close")?.addEventListener("click", function () { closeCartDrawer(false); });
  document.getElementById("cart-backdrop")?.addEventListener("click", function () { closeCartDrawer(false); });
  document.getElementById("cart-checkout-btn")?.addEventListener("click", function (event) {
    event.preventDefault();
    checkoutSelectedCartItems();
  });
  document.getElementById("cart-view-btn")?.addEventListener("click", function (event) {
    event.preventDefault();
    closeCartDrawer(true);
    openCartPage();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    const cartOpen = document.getElementById("cart-drawer")?.classList.contains("open");
    if (cartOpen) {
      closeCartDrawer(false);
      return;
    }
    closeModal();
  });
  document.getElementById("featured-modal-confirm")?.addEventListener("click", confirmOrder);

  cartState.items = loadCartItems();
  loadBackendCart().then(function () {
    renderCartDrawer();
  }).catch(function () {
    renderCartDrawer();
  });
  loadCanonicalData().catch(function (error) {
    const message = (error && error.message) ? error.message : "Unable to load order popup.";
    setStatus(message, true);
    document.getElementById("featured-modal-title").textContent = "Unable to load model";
    document.getElementById("featured-modal-description").textContent = "Open the extension popup and sign in again if needed.";
  });
})();
