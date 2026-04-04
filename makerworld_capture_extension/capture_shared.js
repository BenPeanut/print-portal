(function () {
  function toNumber(value, fallback) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
    return Number.isFinite(Number(fallback)) ? Number(fallback) : 0;
  }

  function calcPrice(options) {
    const input = options || {};
    const config = input.pricingConfig || {};
    const overrides = input.overrides || {};

    const baseFee = toNumber(
      overrides.baseFeeOverride !== '' && overrides.baseFeeOverride != null ? overrides.baseFeeOverride : config.base_service_fee,
      0
    );
    const ppg = toNumber(
      overrides.pricePerGramOverride !== '' && overrides.pricePerGramOverride != null ? overrides.pricePerGramOverride : config.price_per_gram,
      0
    );
    const pch = toNumber(
      overrides.powerCostOverride !== '' && overrides.powerCostOverride != null ? overrides.powerCostOverride : config.power_cost_per_hour,
      0
    );
    const margin = toNumber(
      overrides.profitMarginOverride !== '' && overrides.profitMarginOverride != null ? overrides.profitMarginOverride : config.profit_margin,
      1.2
    );

    const weight = Math.max(0, toNumber(input.weight, 0));
    const hours = Math.max(0, toNumber(input.hours, 0));
    const subtotal = baseFee + (weight * ppg) + (hours * pch);
    return Math.round(subtotal * (margin > 0 ? margin : 1));
  }

  function isProfileTechnicalClause(text) {
    var value = String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!value) return false;
    if (/^designer\b/.test(value)) return true;
    if (/^\d+(?:\.\d+)?\s*h(?:ours?)?\b/.test(value)) return true;
    if (/^\d+\s*plates?\b|^plate\b/.test(value)) return true;
    var hasToken = /\b(layer\s*height|layer|infill|walls?|nozzle|supports?|line\s*width|speed|temperature|temp|plate)\b/.test(value);
    var hasValue = /(\d+(?:\.\d+)?\s*mm\b|\d{1,3}\s*%|\b\d+(?:\.\d+)?\b)/.test(value);
    return hasToken && hasValue;
  }

  function isLayerOnlyProfileName(name) {
    var text = String(name || '').trim().toLowerCase();
    if (!text) return false;
    text = text.replace(/\b\d+(?:\.\d+)?\s*mm\b/g, ' ');
    text = text.replace(/\b\d+(?:\.\d+)?\s*(?:micron|microns|um)\b/g, ' ');
    text = text.replace(/\b\d+(?:\.\d+)?\b/g, ' ');
    text = text.replace(/\b(?:layer|height|profile|print|walls?|infill|nozzle|supports?|line\s*width|mm|um|micron|microns|lh)\b/g, ' ');
    text = text.replace(/[^a-z]+/g, ' ').replace(/\s+/g, ' ').trim();
    return text === '';
  }

  function sanitizeProfileName(name, fallbackName) {
    var cleaned = String(name || '').replace(/\s+/g, ' ').trim();
    if (!cleaned) return String(fallbackName || 'Standard');

    [ /\s*\|\s*/, /\s*\/\s*/, /\s*[\-\u2013\u2014]\s*/ ].forEach(function (separator) {
      var parts = cleaned.split(separator).map(function (part) { return String(part || '').trim(); }).filter(Boolean);
      if (parts.length === 2 && parts[0].toLowerCase() === parts[1].toLowerCase()) {
        cleaned = parts[0];
      }
    });

    [ /\bdesigner\b/i, /\b\d+(?:\.\d+)?\s*h(?:ours?)?\b/i, /\b\d+\s*plates?\b/i, /\bplate\b/i ].forEach(function (pattern) {
      var match = cleaned.match(pattern);
      if (match && typeof match.index === 'number' && match.index > 0) {
        cleaned = cleaned.slice(0, match.index).replace(/[\s\-|,;/]+$/g, '');
      }
    });

    var splitMatch = cleaned.split(/\s*[\-\u2013\u2014|:]\s*/, 2);
    if (splitMatch.length === 2 && splitMatch[0] && splitMatch[1] && isProfileTechnicalClause(splitMatch[1])) {
      cleaned = splitMatch[0].trim();
    }

    var clauses = cleaned.split(/\s*(?:,|;|\u2022)\s*/).map(function (part) { return String(part || '').trim(); }).filter(Boolean);
    var keptClauses = clauses.filter(function (part) { return !isProfileTechnicalClause(part); });
    if (keptClauses.length) {
      cleaned = keptClauses.join(', ');
    }

    cleaned = cleaned.replace(/\s*[\[(][^\])]*(?:layer\s*height|infill|nozzle|wall|walls|supports?|line\s*width|speed|temperature|temp|mm|%)\b[^\])]*[\])]\s*$/i, '');

    var tokenMatch = cleaned.match(/\b(layer\s*height|layer|infill|walls?|nozzle|supports?|line\s*width|speed|temperature|temp|plate)\b/i);
    if (tokenMatch && typeof tokenMatch.index === 'number' && tokenMatch.index > 0) {
      var prefix = cleaned.slice(0, tokenMatch.index).replace(/[\s\-|,;/]+$/g, '');
      if (prefix) cleaned = prefix;
    }

    cleaned = cleaned.replace(/\s+/g, ' ').replace(/^[\s\-|,;/]+|[\s\-|,;/]+$/g, '');
    if (isLayerOnlyProfileName(cleaned)) return 'Standard';
    return cleaned || String(fallbackName || 'Standard');
  }

  function normalizeProfileRows(rows, fallbackRowFactory) {
    const normalized = (Array.isArray(rows) ? rows : [])
      .map(function (row, index) {
        var fallbackName = 'Profile ' + (index + 1);
        return {
          id: String((row && row.id) || ''),
          name: sanitizeProfileName((row && row.name) || fallbackName, fallbackName),
          price: toNumber(row && row.price, 0),
          weight_g: toNumber(row && row.weight_g, 0),
          estimated_print_hours: toNumber(row && row.estimated_print_hours, 0),
          is_default: Boolean(row && row.is_default),
          manual_price: Boolean(row && row.manual_price),
        };
      })
      .filter(function (row) {
        return row.name.trim();
      });

    if (!normalized.length && typeof fallbackRowFactory === 'function') {
      normalized.push(fallbackRowFactory());
    }

    if (normalized.length && !normalized.some(function (row) { return row.is_default; })) {
      normalized[0].is_default = true;
    }

    return normalized;
  }

  function getDefaultProfileRow(rows) {
    const list = Array.isArray(rows) ? rows : [];
    const idx = list.findIndex(function (row) { return row && row.is_default; });
    return idx >= 0 ? list[idx] : (list[0] || null);
  }

  function buildProfilePricingPayload(rows) {
    return (Array.isArray(rows) ? rows : []).map(function (row) {
      return {
        name: sanitizeProfileName((row && row.name) || '', 'Standard'),
        price: Math.round(toNumber(row && row.price, 0)),
        is_default: Boolean(row && row.is_default),
        weight_g: toNumber(row && row.weight_g, 0),
        estimated_print_hours: toNumber(row && row.estimated_print_hours, 0),
      };
    }).filter(function (row) { return row.name; });
  }

  window.CaptureShared = {
    calcPrice: calcPrice,
    sanitizeProfileName: sanitizeProfileName,
    normalizeProfileRows: normalizeProfileRows,
    getDefaultProfileRow: getDefaultProfileRow,
    buildProfilePricingPayload: buildProfilePricingPayload,
    toNumber: toNumber,
  };
})();
