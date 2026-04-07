const STORAGE_KEYS = {
  settings: "capture_settings",
  latest: "latest_capture"
};

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
  const data = await chrome.storage.sync.get([STORAGE_KEYS.settings]);
  return { ...DEFAULT_SETTINGS, ...(data[STORAGE_KEYS.settings] || {}) };
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
  const apiBase = String(settings.apiBase || DEFAULT_SETTINGS.apiBase).replace(/\/$/, "");
  const apiKey = String(settings.apiKey || "").trim();
  if (!apiKey) {
    throw new Error("Missing API key in extension settings.");
  }
  const params = new URLSearchParams({
    model_url: String(modelUrl || ""),
    api_key: String(apiKey || ""),
  });
  if (String(settings.baseFeeOverride || "").trim() !== "") params.set("base_fee", settings.baseFeeOverride);
  if (String(settings.pricePerGramOverride || "").trim() !== "") params.set("price_per_gram", settings.pricePerGramOverride);
  if (String(settings.powerCostOverride || "").trim() !== "") params.set("power_cost_per_hour", settings.powerCostOverride);
  if (String(settings.profitMarginOverride || "").trim() !== "") params.set("profit_margin", settings.profitMarginOverride);

  const endpoint = `${apiBase}/extension-api/scrape-model-metrics?${params.toString()}`;
  const response = await fetch(endpoint, { method: "GET" });
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
  const base = String(apiBase || DEFAULT_SETTINGS.apiBase || "http://127.0.0.1:5000").replace(/\/$/, "");
  return `${base}/extension-api/desktop-capture/push`;
}

async function openIsolatedQuickPopup() {
  // Kept for backward compatibility; no-op. UI now opens as an in-page overlay.
  return;
}

function buildDesktopCaptureFrameUrl(apiBase, modelUrl) {
  const base = String(apiBase || DEFAULT_SETTINGS.apiBase || "http://127.0.0.1:5000").replace(/\/$/, "");
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


async function pushDesktopCaptureSignal(settings, modelUrl, triggeredAt) {
  const endpoint = buildDesktopCapturePushUrl(settings && settings.apiBase);
  const payload = {
    api_key: String((settings && settings.apiKey) || "").trim(),
    model_url: String(modelUrl || "").trim(),
    source: "mw_q_hotkey",
    triggered_at: String(triggeredAt || ""),
  };

  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
    throw new Error("No captured model yet. Hover a model and press Q first.");
  }

  const settings = await getSettings();
  const apiBase = String(settings.apiBase || DEFAULT_SETTINGS.apiBase).replace(/\/$/, "");

  // Resolve target user — prefer overrides, then fall back to settings
  const targetUserId = String(overrides.targetUserId || "").trim();
  const targetUsername = String(overrides.targetUsername || settings.targetUsername || "").trim();

  const payload = {
    api_key: String(settings.apiKey || ""),
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

  if (!payload.api_key.trim()) {
    throw new Error("Missing API key in extension settings.");
  }

  const response = await fetch(`${apiBase}/extension-api/confirm-capture`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  const result = await response.json().catch(() => ({ ok: false, error: "Invalid API response" }));
  if (!response.ok || !result.ok) {
    throw new Error(result.error || `Request failed (${response.status})`);
  }

  await clearBadge();
  return result;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.action) return;

  if (message.action === "capture_from_hover") {
    captureFromHover(message, sender)
      .then((latestCapture) => sendResponse({ ok: true, latestCapture }))
      .catch((error) => sendResponse({ ok: false, error: error.message || "Capture failed" }));
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

  if (message.action === "clear_badge") {
    clearBadge().then(() => sendResponse({ ok: true }));
    return true;
  }
});
