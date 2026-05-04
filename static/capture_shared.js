(function () {
  function toNumber(value, fallback) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
    return Number.isFinite(Number(fallback)) ? Number(fallback) : 0;
  }

  function roundPrice(value) {
    const amount = Math.max(0, toNumber(value, 0));
    return Math.round(amount / 5000) * 5000;
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
    return roundPrice(subtotal * (margin > 0 ? margin : 1));
  }

  function sanitizeProfileName(name) {
    return String(name || '');
  }

  function normalizeProfileRows(rows, fallbackRowFactory) {
    const normalized = (Array.isArray(rows) ? rows : [])
      .map(function (row) {
        return {
          // Spread first so extra fields (colors, image_urls, parts_configuration, etc.) survive
          ...(row && typeof row === 'object' ? row : {}),
          id: String((row && row.id) || ''),
          name: sanitizeProfileName((row && row.name) || ''),
          price: toNumber(row && row.price, 0),
          weight_g: toNumber(row && row.weight_g, 0),
          estimated_print_hours: toNumber(row && row.estimated_print_hours, 0),
          is_default: Boolean(row && row.is_default),
          manual_price: Boolean(row && row.manual_price),
          colors: Array.isArray(row && row.colors) ? row.colors : [],
        };
      })
      .filter(function (row) {
        return row.name !== '';
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
      var name = sanitizeProfileName((row && row.name) || '');
      return {
        id: String((row && row.id) || ''),
        name: name,
        price: roundPrice(row && row.price),
        is_default: Boolean(row && row.is_default),
        weight_g: toNumber(row && row.weight_g, 0),
        estimated_print_hours: toNumber(row && row.estimated_print_hours, 0),
        colors: Array.isArray(row && row.colors) ? row.colors : [],
      };
    }).filter(function (row) { return row.name !== ''; });
  }

  window.CaptureShared = {
    calcPrice: calcPrice,
    roundPrice: roundPrice,
    sanitizeProfileName: sanitizeProfileName,
    normalizeProfileRows: normalizeProfileRows,
    getDefaultProfileRow: getDefaultProfileRow,
    buildProfilePricingPayload: buildProfilePricingPayload,
    toNumber: toNumber,
  };
})();
