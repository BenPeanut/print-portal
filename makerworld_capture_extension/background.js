const STORAGE_KEYS = {
  settings: "capture_settings",
  latest: "latest_capture",
  authToken: "extension_auth_token",
};

const HOSTED_PORTAL_BASE = "https://print-portal-qm9p.onrender.com";
const HOSTED_EXTENSION_SETUP_URL = `${HOSTED_PORTAL_BASE}/extension-install`;

const DEFAULT_SETTINGS = {
  apiBase: "http://127.0.0.1:5000",
  apiKey: "",
  targetUserId: "",
  targetUsername: "",
  baseFeeOverride: "",
  pricePerGramOverride: "",
  powerCostOverride: "",
  profitMarginOverride: "",
  defaultProfile: "1",
  defaultFilament: "Bamboo Green PLA",
  defaultQuantity: 1
};

function normalizeLocalApiBase(raw) {
  const fallback = DEFAULT_SETTINGS.apiBase;
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

function getPlatformInfoSafe() {
  return new Promise((resolve) => {
    try {
      if (!chrome.runtime || typeof chrome.runtime.getPlatformInfo !== "function") {
        resolve({ os: "unknown" });
        return;
      }
      chrome.runtime.getPlatformInfo((info) => {
        if (chrome.runtime.lastError) {
          resolve({ os: "unknown" });
          return;
        }
        resolve(info || { os: "unknown" });
      });
    } catch {
      resolve({ os: "unknown" });
    }
  });
}

async function isChromeOS() {
  return true; // TESTING: force Chromebook mode — revert before deploying
  const info = await getPlatformInfoSafe(); // eslint-disable-line no-unreachable
  return String((info && info.os) || "").toLowerCase() === "cros";
}

function normalizeUrl(raw) {
  const text = String(raw || "").trim();
  if (!text) return "";
  try {
    const withProtocol = /^https?:\/\//i.test(text) ? text : `https://${text}`;
    const parsed = new URL(withProtocol);
    if (!/makerworld\.com$/i.test(parsed.hostname) && !/\.makerworld\.com$/i.test(parsed.hostname)) {
      return "";
    }
    if (!/\/models\//i.test(parsed.pathname || "")) {
      return "";
    }
    if (!/profileId[-_]\d+/i.test(parsed.hash || "")) {
      parsed.hash = "";
    }
    return parsed.toString();
  } catch {
    return "";
  }
}

function mergeProfileHash(modelUrl, sourcePage) {
  try {
    const model = new URL(modelUrl);
    const source = new URL(sourcePage || "");
    if (
      /profileId[-_]\d+/i.test(source.hash || "") &&
      model.hostname === source.hostname &&
      model.pathname === source.pathname
    ) {
      model.hash = source.hash;
      return model.toString();
    }
  } catch {
    // Ignore malformed URLs and fall back to the normalized model URL.
  }
  return modelUrl;
}

async function getSettings() {
  const localData = await chrome.storage.local.get([STORAGE_KEYS.settings]);
  let settings = localData[STORAGE_KEYS.settings] || null;
  if (!settings) {
    const syncData = await chrome.storage.sync.get([STORAGE_KEYS.settings]);
    settings = syncData[STORAGE_KEYS.settings] || {};
    if (Object.keys(settings).length) {
      await chrome.storage.local.set({ [STORAGE_KEYS.settings]: settings });
    }
  }
  const merged = { ...DEFAULT_SETTINGS, ...(settings || {}) };
  merged.apiBase = normalizeLocalApiBase(merged.apiBase);
  return merged;
}

async function openHostedPortal(path = "") {
  const clean = String(path || "").trim();
  const target = clean ? `${HOSTED_PORTAL_BASE}/${clean.replace(/^\/+/, "")}` : HOSTED_PORTAL_BASE;
  await chrome.tabs.create({ url: target });
}

chrome.runtime.onInstalled.addListener((details) => {
  if (!details || details.reason !== "install") return;
  chrome.tabs.create({ url: HOSTED_EXTENSION_SETUP_URL });
});

async function getExtensionAuthToken() {
  const localData = await chrome.storage.local.get([STORAGE_KEYS.authToken]);
  return String(localData[STORAGE_KEYS.authToken] || "").trim();
}

async function buildExtensionAuthHeaders(baseHeaders = {}) {
  const token = await getExtensionAuthToken();
  if (!token) return { ...baseHeaders };
  return {
    ...baseHeaders,
    "X-Extension-Auth": token,
  };
}

async function apiFetchJson(path, options = {}) {
  const settings = await getSettings();
  const apiBase = normalizeLocalApiBase(settings.apiBase || DEFAULT_SETTINGS.apiBase);
  const method = String(options.method || "GET").toUpperCase();
  const body = options.body === undefined ? null : options.body;
  const headers = await buildExtensionAuthHeaders(
    body !== null ? { "Content-Type": "application/json" } : {}
  );

  const response = await fetch(`${apiBase}${path}`, {
    method,
    credentials: "include",
    headers,
    body: body !== null ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  return { response, payload };
}

function withTimeout(promise, ms, errorMessage) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(errorMessage)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

async function fetchMetricsFromBackend(modelUrl, settings) {
  const apiBase = normalizeLocalApiBase(settings.apiBase);
  const params = new URLSearchParams({
    model_url: String(modelUrl || ""),
  });
  if (String(settings.baseFeeOverride || "").trim() !== "") params.set("base_fee", settings.baseFeeOverride);
  if (String(settings.pricePerGramOverride || "").trim() !== "") params.set("price_per_gram", settings.pricePerGramOverride);
  if (String(settings.powerCostOverride || "").trim() !== "") params.set("power_cost_per_hour", settings.powerCostOverride);
  if (String(settings.profitMarginOverride || "").trim() !== "") params.set("profit_margin", settings.profitMarginOverride);

  const endpoint = `${apiBase}/extension-api/scrape-model-metrics?${params.toString()}`;
  const headers = await buildExtensionAuthHeaders();
  const response = await fetch(endpoint, { method: "GET", credentials: "include", headers });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || !result.ok) {
    throw new Error(result.error || `Scrape request failed (${response.status})`);
  }
  return result;
}

function firstMatch(html, pattern, flags = "i") {
  const match = new RegExp(pattern, flags).exec(html);
  return match ? String(match[1] || "").trim() : "";
}

function htmlDecode(text) {
  return String(text || "")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();
}

function sanitizeModelTitle(rawTitle) {
  let title = String(rawTitle || "").replace(/\s+/g, " ").trim();
  if (!title) return "MakerWorld Model";

  title = title.replace(/\s*[-|]\s*MakerWorld\s*$/i, "").trim();
  const segments = title.split(/\s*[|-]\s*/).map((part) => part.trim()).filter(Boolean);
  if (segments.length) {
    title = segments[0];
  }

  title = title
    .replace(/\s*[\[(][^\])]*(?:layer\s*height|infill|infill\s*density|nozzle|line\s*width|wall\s*count|supports?)\b[^\])]*[\])]/gi, "")
    .replace(/\s*(?:[,;/]|\s+-\s+)\s*(?:\d+(?:\.\d+)?\s*mm\b|\d{1,3}\s*%\s*infill\b|layer\s*height\b[^,;/-]*)\s*$/gi, "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[\-|,;/\s]+$/g, "");

  return title || "MakerWorld Model";
}

function weightGuessG(html) {
  const lower = html.toLowerCase();
  const patterns = [
    /weight[^\d]{0,20}(\d+(?:\.\d+)?)\s*g/,
    /model weight[^\d]{0,20}(\d+(?:\.\d+)?)\s*g/,
    /(\d+(?:\.\d+)?)\s*grams/
  ];
  for (const pattern of patterns) {
    const match = pattern.exec(lower);
    if (!match) continue;
    const value = Number(match[1]);
    if (Number.isFinite(value) && value >= 0) return value;
  }
  return 0;
}

function printTimeGuessHours(html) {
  const lower = html.toLowerCase();
  // Try "Xh Ym" format: e.g. "2h 30m", "1h", "45m"
  const hmPattern = /(?:estimated\s+)?(?:print\s+)?time[^<\n]{0,60}?(?:(\d+)h)?\s*(?:(\d+)m)?/;
  const hmMatch = hmPattern.exec(lower);
  if (hmMatch && (hmMatch[1] || hmMatch[2])) {
    const h = Number(hmMatch[1] || 0);
    const m = Number(hmMatch[2] || 0);
    const total = h + m / 60;
    if (Number.isFinite(total) && total > 0) return Math.round(total * 100) / 100;
  }
  // Try "X hours Y minutes"
  const longPattern = /(?:(\d+(?:\.\d+)?)\s*hours?)?\s*(?:(\d+(?:\.\d+)?)\s*minute)?/;
  const longMatch = lower.includes('hour') ? longPattern.exec(lower) : null;
  if (longMatch && (longMatch[1] || longMatch[2])) {
    const h = Number(longMatch[1] || 0);
    const m = Number(longMatch[2] || 0);
    const total = h + m / 60;
    if (Number.isFinite(total) && total > 0) return Math.round(total * 100) / 100;
  }
  // Try JSON-embedded "printTime" or "print_time"
  const jsonPattern = /"(?:printTime|print_time|estimatedPrintTime)":\s*"?(\d+(?:\.\d+)?)h?\s*(\d+)?m?"?/i;
  const jsonMatch = jsonPattern.exec(html);
  if (jsonMatch) {
    const h = Number(jsonMatch[1] || 0);
    const m = Number(jsonMatch[2] || 0);
    const total = h + m / 60;
    if (Number.isFinite(total) && total > 0) return Math.round(total * 100) / 100;
  }
  return 0;
}

async function scrapeModel(modelUrl) {
  const response = await withTimeout(
    fetch(modelUrl, { method: "GET" }),
    12000,
    "Model page request timed out."
  );
  if (!response.ok) {
    throw new Error(`Model request failed (${response.status})`);
  }

  const html = await withTimeout(response.text(), 12000, "Model page read timed out.");
  const ogTitle = firstMatch(html, `<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']`);
  const htmlTitle = firstMatch(html, "<title>(.*?)</title>", "is");
  const ogDesc = firstMatch(html, `<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']+)["']`);
  const ogImage = firstMatch(html, `<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']`);

  const title = sanitizeModelTitle(htmlDecode(ogTitle || htmlTitle || "MakerWorld Model"));

  return {
    model_url: modelUrl,
    title,
    description: htmlDecode(ogDesc),
    image_url: htmlDecode(ogImage),
    weight_guess_g: Number(weightGuessG(html).toFixed(2)),
    print_time_hours: Number(printTimeGuessHours(html).toFixed(2)),
    captured_at: new Date().toISOString()
  };
}

async function saveLatestCapture(latestCapture) {
  await chrome.storage.local.set({ [STORAGE_KEYS.latest]: latestCapture });
  await chrome.action.setBadgeText({ text: "1" });
  await chrome.action.setBadgeBackgroundColor({ color: "#1f7a50" });
}

async function clearBadge() {
  await chrome.action.setBadgeText({ text: "" });
}

function buildDesktopCapturePushUrl(apiBase) {
  const base = normalizeLocalApiBase(apiBase || DEFAULT_SETTINGS.apiBase || "http://127.0.0.1:5000");
  return `${base}/extension-api/desktop-capture/push`;
}

async function openIsolatedQuickPopup() {
  // Kept for backward compatibility; no-op. UI now opens as an in-page overlay.
  return;
}

function buildDesktopCaptureFrameUrl(apiBase, modelUrl) {
  const base = normalizeLocalApiBase(apiBase || DEFAULT_SETTINGS.apiBase || "http://127.0.0.1:5000");
  const params = new URLSearchParams({
    source: "extension_overlay",
    embedded: "1",
    quick_capture: "1",
    _ts: String(Date.now()),
  });
  if (String(modelUrl || "").trim()) {
    params.set("makerworld_link", String(modelUrl || "").trim());
    params.set("auto_load", "1");
  }
  return `${base}/desktop-capture?${params.toString()}`;
}

function buildOrderOverlayFrameUrl(apiBase, modelUrl, authToken) {
  const params = new URLSearchParams({
    model_url: String(modelUrl || "").trim(),
    api_base: normalizeLocalApiBase(apiBase || DEFAULT_SETTINGS.apiBase || "http://127.0.0.1:5000"),
    _ts: String(Date.now()),
  });
  if (String(authToken || "").trim()) {
    params.set("ext_auth", String(authToken || "").trim());
  }
  return chrome.runtime.getURL(`overlay.html?${params.toString()}`);
}

async function openEmbeddedOverlayInTab(sender, settings, modelUrl) {
  const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
  if (!Number.isInteger(tabId)) {
    return;
  }

  const frameUrl = buildDesktopCaptureFrameUrl(settings && settings.apiBase, modelUrl);
  await new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(
      tabId,
      { action: "open_capture_overlay", frameUrl },
      (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message || "Failed to open overlay."));
          return;
        }
        if (!response || !response.ok) {
          reject(new Error((response && response.error) || "Overlay not acknowledged by page."));
          return;
        }
        resolve(response);
      }
    );
  });
}

async function openOrderOverlayInTab(sender, settings, modelUrl) {
  if (await isChromeOS()) {
    await openHostedPortal();
    return;
  }
  const tabId = sender && sender.tab && Number.isInteger(sender.tab.id) ? sender.tab.id : null;
  if (!Number.isInteger(tabId)) {
    throw new Error("Active MakerWorld tab not found.");
  }

  const authToken = await getExtensionAuthToken();
  const frameUrl = buildOrderOverlayFrameUrl(settings && settings.apiBase, modelUrl, authToken);
  await new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(
      tabId,
      { action: "open_capture_overlay", frameUrl },
      (response) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message || "Failed to open order overlay."));
          return;
        }
        if (!response || !response.ok) {
          reject(new Error((response && response.error) || "Order overlay not acknowledged by page."));
          return;
        }
        resolve(response);
      }
    );
  });
}


async function pushDesktopCaptureSignal(settings, modelUrl, triggeredAt) {
  const endpoint = buildDesktopCapturePushUrl(settings && settings.apiBase);
  const payload = {
    model_url: String(modelUrl || "").trim(),
    source: "mw_q_hotkey",
    triggered_at: String(triggeredAt || ""),
  };

  const headers = await buildExtensionAuthHeaders({ "Content-Type": "application/json" });
  const response = await fetch(endpoint, {
    method: "POST",
    headers,
    credentials: "include",
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok || !result.ok) {
    throw new Error(result.error || `Desktop app push failed (${response.status})`);
  }
  return result;
}

async function captureFromHover(message, sender) {
  const normalizedUrl = normalizeUrl(message.modelUrl);
  const modelUrl = mergeProfileHash(normalizedUrl, message.sourcePage);
  if (!modelUrl) {
    throw new Error("Hovered link is not a valid MakerWorld model URL.");
  }

  const settings = await getSettings();
  try {
    await pushDesktopCaptureSignal(settings, modelUrl, message.triggeredAt);
  } catch {
    // Non-fatal: popup iframe and local capture storage are primary paths.
  }
  const model = await scrapeModel(modelUrl).catch((error) => ({
    model_url: modelUrl,
    title: "MakerWorld Model",
    description: "",
    image_url: "",
    weight_guess_g: 0,
    print_time_hours: 0,
    captured_at: new Date().toISOString(),
    scrape_error: error && error.message ? String(error.message) : "Client scrape failed",
  }));

  const latestCapture = {
    source_page: String(message.sourcePage || ""),
    triggered_at: String(message.triggeredAt || ""),
    model,
    scraped_metrics: null,
    backend_error: "",
    backend_pending: true,
    suggested_order: {
      name: model.title,
      link: model.model_url,
      image_url: model.image_url,
      description: model.description,
      print_weight_g: Number((model.weight_guess_g || 0).toFixed(2)),
      print_time_hours: Number((model.print_time_hours || 0).toFixed(2)),
      profile: String(settings.defaultProfile || ""),
      color: String(settings.defaultFilament || ""),
      quantity: Math.max(1, Math.min(20, Number(settings.defaultQuantity || 1)))
    },
    captured_at: new Date().toISOString()
  };

  await saveLatestCapture(latestCapture);

  try {
    await openEmbeddedOverlayInTab(sender, settings, modelUrl);
  } catch {
    // Non-fatal: capture data is still saved and available via extension popup icon.
  }

  withTimeout(
    fetchMetricsFromBackend(modelUrl, settings),
    10000,
    "Backend scrape timed out."
  )
    .then(async (backendMetrics) => {
      const weightFromBackend = Number(backendMetrics?.weight_g ?? NaN);
      const hoursFromBackend = Number(backendMetrics?.estimated_print_hours ?? NaN);
      const scrapedProfiles = Array.isArray(backendMetrics?.profiles) ? backendMetrics.profiles : [];
      const defaultProfile = scrapedProfiles.find((p) => p && p.is_default) || scrapedProfiles[0] || null;

      const updatedCapture = {
        ...latestCapture,
        scraped_metrics: backendMetrics || null,
        backend_error: "",
        backend_pending: false,
        suggested_order: {
          ...latestCapture.suggested_order,
          profile: defaultProfile && defaultProfile.name
            ? String(defaultProfile.name)
            : latestCapture.suggested_order.profile,
          print_weight_g: defaultProfile && Number.isFinite(Number(defaultProfile.weight_g))
            ? Number(Number(defaultProfile.weight_g).toFixed(2))
            : (Number.isFinite(weightFromBackend) && weightFromBackend > 0
              ? Number(weightFromBackend.toFixed(2))
              : latestCapture.suggested_order.print_weight_g),
          print_time_hours: defaultProfile && Number.isFinite(Number(defaultProfile.estimated_print_hours))
            ? Number(Number(defaultProfile.estimated_print_hours).toFixed(2))
            : (Number.isFinite(hoursFromBackend) && hoursFromBackend >= 0
              ? Number(hoursFromBackend.toFixed(2))
              : latestCapture.suggested_order.print_time_hours),
          profile_pricing: scrapedProfiles.map((p) => ({
            name: String((p && p.name) || ""),
            price: Number((p && p.price) || 0),
            is_default: Boolean(p && p.is_default),
            weight_g: Number((p && p.weight_g) || 0),
            estimated_print_hours: Number((p && p.estimated_print_hours) || 0),
          })).filter((p) => p.name),
        },
      };
      await saveLatestCapture(updatedCapture);
    })
    .catch(async (error) => {
      const updatedCapture = {
        ...latestCapture,
        backend_pending: false,
        backend_error: error && error.message ? String(error.message) : "Backend scrape failed.",
      };
      await saveLatestCapture(updatedCapture);
    });

  return latestCapture;
}

async function confirmLatest(overrides) {
  const data = await chrome.storage.local.get([STORAGE_KEYS.latest]);
  const latest = data[STORAGE_KEYS.latest];
  if (!latest || !latest.suggested_order) {
    throw new Error("No captured model yet. Hover a model and press Alt+Q first.");
  }

  const settings = await getSettings();
  const apiBase = normalizeLocalApiBase(settings.apiBase || DEFAULT_SETTINGS.apiBase);

  // Resolve target user — prefer overrides, then fall back to settings
  const targetUserId = String(overrides.targetUserId || "").trim();
  const targetUsername = String(overrides.targetUsername || settings.targetUsername || "").trim();

  const payload = {
    target_user_id: targetUserId,
    target_username: targetUsername,
    title: String(overrides.title || latest.suggested_order.name || "MakerWorld Model").trim(),
    makerworld_url: String(overrides.makerworld_url || latest.suggested_order.link || "").trim(),
    image_url: String(overrides.image_url || latest.suggested_order.image_url || "").trim(),
    description: String(overrides.description || latest.suggested_order.description || "").trim(),
    suggested_filament: String(overrides.suggested_filament || latest.suggested_order.color || "").trim(),
    suggested_colors: String(overrides.suggested_colors || overrides.suggested_filament || latest.suggested_order.color || "").trim(),
    suggested_profile: String(overrides.suggested_profile || "").trim(),
    profile_pricing: overrides.profile_pricing || [],
    price: Number(overrides.price || 0),
    print_weight_g: Number(overrides.printWeightG ?? latest.suggested_order.print_weight_g ?? 0),
    estimated_print_hours: Number(overrides.printHours ?? latest.suggested_order.print_time_hours ?? 0),
  };

  const headers = await buildExtensionAuthHeaders({ "Content-Type": "application/json" });
  const response = await fetch(`${apiBase}/extension-api/confirm-capture`, {
    method: "POST",
    headers,
    credentials: "include",
    body: JSON.stringify(payload)
  });

  const result = await response.json().catch(() => ({ ok: false, error: "Invalid API response" }));
  if (!response.ok || !result.ok) {
    throw new Error(result.error || `Request failed (${response.status})`);
  }

  await clearBadge();
  return result;
}

async function fetchPricingPopupPayload(message) {
  const modelUrl = mergeProfileHash(normalizeUrl(message && message.modelUrl), message && message.sourcePage);
  if (!modelUrl) {
    throw new Error("Hovered link is not a valid MakerWorld model URL.");
  }

  const settings = await getSettings();
  const apiBase = normalizeLocalApiBase(settings.apiBase || DEFAULT_SETTINGS.apiBase);

  const authHeaders = await buildExtensionAuthHeaders();
  const [metrics, appData, pricingConfig] = await Promise.all([
    withTimeout(fetchMetricsFromBackend(modelUrl, settings), 15000, "Model scrape timed out."),
    withTimeout(
      fetch(`${apiBase}/extension-api/app-data`, {
        credentials: "include",
        headers: authHeaders,
      })
        .then((res) => res.json().catch(() => ({})).then((json) => ({ ok: res.ok, json }))),
      10000,
      "App data request timed out."
    ),
    withTimeout(
      fetch(`${apiBase}/extension-api/pricing-config`, {
        credentials: "include",
        headers: authHeaders,
      })
        .then((res) => res.json().catch(() => ({})).then((json) => ({ ok: res.ok, json }))),
      10000,
      "Pricing config request timed out."
    ),
  ]);

  if (!appData.ok || !appData.json || !appData.json.ok) {
    throw new Error((appData.json && appData.json.error) || "Failed to fetch app data.");
  }
  if (!pricingConfig.ok || !pricingConfig.json || !pricingConfig.json.ok) {
    throw new Error((pricingConfig.json && pricingConfig.json.error) || "Failed to fetch pricing configuration.");
  }

  const profiles = Array.isArray(metrics && metrics.profiles) ? metrics.profiles : [];
  const defaultProfile = profiles.find((p) => p && p.is_default) || profiles[0] || null;

  return {
    ok: true,
    model_url: modelUrl,
    title: String((metrics && metrics.title) || "MakerWorld Model"),
    image_url: String((metrics && metrics.image_url) || ""),
    profiles,
    default_profile_id: String((defaultProfile && defaultProfile.id) || ""),
    default_profile_name: String((defaultProfile && defaultProfile.name) || ""),
    price_config: pricingConfig.json,
    filaments: Array.isArray(appData.json.filaments) ? appData.json.filaments : [],
    users: Array.isArray(appData.json.users) ? appData.json.users : [],
    metrics,
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.action) return;

  if (message.action === "capture_from_hover") {
    (async function () {
      try {
        if (await isChromeOS()) {
          await openHostedPortal();
          sendResponse({ ok: true, fallback: "hosted_portal", url: HOSTED_PORTAL_BASE });
          return;
        }
        const latestCapture = await captureFromHover(message, sender);
        sendResponse({ ok: true, latestCapture });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Capture failed" });
      }
    })();
    return true;
  }

  if (message.action === "open_hover_order_overlay") {
    (async function () {
      try {
        if (await isChromeOS()) {
          await openHostedPortal();
          sendResponse({ ok: true, fallback: "hosted_portal", url: HOSTED_PORTAL_BASE });
          return;
        }
        const normalizedUrl = normalizeUrl(message.modelUrl);
        const modelUrl = mergeProfileHash(normalizedUrl, message.sourcePage);
        if (!modelUrl) {
          throw new Error("Hovered link is not a valid MakerWorld model URL.");
        }
        const settings = await getSettings();
        await openOrderOverlayInTab(sender, settings, modelUrl);
        sendResponse({ ok: true });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Failed to open order overlay." });
      }
    })();
    return true;
  }

  if (message.action === "open_extension_setup") {
    (async function () {
      try {
        await chrome.tabs.create({ url: HOSTED_EXTENSION_SETUP_URL });
        sendResponse({ ok: true, url: HOSTED_EXTENSION_SETUP_URL });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Could not open setup page." });
      }
    })();
    return true;
  }

  if (message.action === "open_hosted_portal") {
    (async function () {
      try {
        await openHostedPortal();
        sendResponse({ ok: true, url: HOSTED_PORTAL_BASE });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Could not open hosted portal." });
      }
    })();
    return true;
  }

  if (message.action === "get_platform_mode") {
    (async function () {
      try {
        const cros = await isChromeOS();
        sendResponse({
          ok: true,
          mode: cros ? "chromebook-hosted" : "desktop-local",
          hosted_url: HOSTED_PORTAL_BASE,
          setup_url: HOSTED_EXTENSION_SETUP_URL,
        });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Failed to detect platform." });
      }
    })();
    return true;
  }

  if (message.action === "scan_active_tab_for_models") {
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      const tab = tabs && tabs[0];
      if (!tab || !tab.id) {
        sendResponse({ ok: true, models: [], total_found: 0, suggested_count: 0, error: "No active tab." });
        return;
      }
      chrome.tabs.sendMessage(
        tab.id,
        { action: "get_page_models", options: message.options || {} },
        function (response) {
          if (chrome.runtime.lastError) {
            sendResponse({
              ok: true,
              models: [],
              total_found: 0,
              suggested_count: 0,
              error: "Open a MakerWorld page and refresh it once, then scan again.",
            });
            return;
          }
          sendResponse(response || { ok: true, models: [], total_found: 0, suggested_count: 0 });
        }
      );
    });
    return true;
  }

  if (message.action === "confirm_latest") {
    confirmLatest(message.overrides || {})
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message || "Confirm failed" }));
    return true;
  }

  if (message.action === "fetch_pricing_popup") {
    fetchPricingPopupPayload(message)
      .then((payload) => sendResponse(payload))
      .catch((error) => sendResponse({ ok: false, error: error.message || "Failed to fetch popup data" }));
    return true;
  }

  if (message.action === "get_cart_orders") {
    (async function () {
      try {
        const { response, payload } = await apiFetchJson("/cart/orders", { method: "GET" });
        if (!response.ok || !payload || !payload.ok) {
          throw new Error((payload && payload.error) || `Cart request failed (${response.status})`);
        }
        sendResponse({ ok: true, items: Array.isArray(payload.items) ? payload.items : [] });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Failed to load cart." });
      }
    })();
    return true;
  }

  if (message.action === "save_cart_item") {
    console.log("[BG] save_cart_item called:", message && message.item && message.item.link);
    (async function () {
      try {
        const payloadBody = message && message.item && typeof message.item === "object" ? message.item : {};
        console.log("[BG] Sending to /cart/save-item:", { link: payloadBody.link, display_name: payloadBody.displayName });
        const { response, payload } = await apiFetchJson("/cart/save-item", {
          method: "POST",
          body: payloadBody,
        });
        console.log("[BG] /cart/save-item response:", response.status, payload);
        if (!response.ok || !payload || !payload.ok) {
          console.error("[BG] save_cart_item failed:", payload && payload.error);
          throw new Error((payload && payload.error) || `Save cart item failed (${response.status})`);
        }
        console.log("[BG] save_cart_item success: order_id =", payload.order_id);
        sendResponse({ ok: true, order_id: String(payload.order_id || "") });
      } catch (error) {
        console.error("[BG] save_cart_item error:", error && error.message);
        sendResponse({ ok: false, error: error && error.message ? error.message : "Failed to save cart item." });
      }
    })();
    return true;
  }

  if (message.action === "remove_cart_item") {
    (async function () {
      try {
        const orderId = String((message && message.orderId) || "").trim();
        if (!orderId) {
          throw new Error("Missing order id.");
        }
        const { response, payload } = await apiFetchJson(`/cart/remove/${encodeURIComponent(orderId)}`, {
          method: "POST",
        });
        if (!response.ok || !payload || !payload.ok) {
          throw new Error((payload && payload.error) || `Remove cart item failed (${response.status})`);
        }
        sendResponse({ ok: true, removed: Boolean(payload.removed), order_id: String(payload.order_id || orderId) });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Failed to remove cart item." });
      }
    })();
    return true;
  }

  if (message.action === "checkout_cart") {
    (async function () {
      try {
        const items = Array.isArray(message && message.items) ? message.items : [];
        if (!items.length) {
          throw new Error("Select at least one cart item.");
        }
        const { response, payload } = await apiFetchJson("/checkout", {
          method: "POST",
          body: {
            items,
            response_mode: "json",
          },
        });
        if (!response.ok || !payload || !payload.ok) {
          throw new Error((payload && payload.error) || `Checkout failed (${response.status})`);
        }
        sendResponse({
          ok: true,
          order_ids: Array.isArray(payload.order_ids) ? payload.order_ids : [],
          order_count: Number(payload.order_count || 0),
          unit_count: Number(payload.unit_count || 0),
          grand_total: Number(payload.grand_total || 0),
          message: String(payload.message || "Checkout complete."),
        });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Failed to check out cart." });
      }
    })();
    return true;
  }

  if (message.action === "clear_badge") {
    clearBadge().then(() => sendResponse({ ok: true }));
    return true;
  }

  if (message.action === "get_extension_auth_token") {
    (async function () {
      try {
        const token = await getExtensionAuthToken();
        sendResponse({ ok: true, token: String(token || "") });
      } catch (error) {
        sendResponse({ ok: false, error: error && error.message ? error.message : "Failed to load extension auth token." });
      }
    })();
    return true;
  }

});
