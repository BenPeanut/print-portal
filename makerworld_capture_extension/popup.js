const STORAGE_KEYS = {
  settings: "extension_settings",
  latest: "latest_captured_model"
};

const DEFAULT_API_BASE = "http://127.0.0.1:5000";

const statusLine = document.getElementById("status-line");
const appFrame = document.getElementById("app-frame");
const reloadFrameBtn = document.getElementById("reload-frame-btn");
const popupParams = new URLSearchParams(window.location.search || "");
const isEmbeddedMode = popupParams.get("embedded") === "1";

if (isEmbeddedMode) {
  document.body.classList.add("embedded-mode");
}

function setStatus(text, isError = false) {
  if (!statusLine) return;
  statusLine.textContent = String(text || "");
  statusLine.classList.toggle("error", Boolean(isError));
}

async function getApiBase() {
  const synced = await chrome.storage.sync.get([STORAGE_KEYS.settings]);
  const settings = synced[STORAGE_KEYS.settings] || {};
  return String(settings.apiBase || DEFAULT_API_BASE).replace(/\/$/, "");
}

async function isApiBaseReachable(apiBase) {
  const url = `${apiBase}/api/health?_ts=${encodeURIComponent(String(Date.now()))}`;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1800);
    const res = await fetch(url, { method: "GET", cache: "no-store", signal: controller.signal });
    clearTimeout(timer);
    return Boolean(res && res.ok);
  } catch {
    return false;
  }
}

async function setApiBase(apiBase) {
  const synced = await chrome.storage.sync.get([STORAGE_KEYS.settings]);
  const settings = synced[STORAGE_KEYS.settings] || {};
  const next = { ...settings, apiBase: String(apiBase || DEFAULT_API_BASE).replace(/\/$/, "") };
  await chrome.storage.sync.set({ [STORAGE_KEYS.settings]: next });
}

async function getLatestModelUrl() {
  const local = await chrome.storage.local.get([STORAGE_KEYS.latest]);
  const latest = local[STORAGE_KEYS.latest] || {};
  const link =
    (latest && latest.suggested_order && latest.suggested_order.link) ||
    (latest && latest.model && latest.model.model_url) ||
    "";
  return String(link || "").trim();
}

function buildFrameUrl(apiBase, makerworldLink) {
  const params = new URLSearchParams({
    source: "extension_popup",
    _ts: String(Date.now())
  });
  if (isEmbeddedMode) {
    params.set("quick_capture", "1");
  }
  if (makerworldLink) {
    params.set("makerworld_link", makerworldLink);
    params.set("auto_load", "1");
  }
  return `${apiBase}/desktop-capture?${params.toString()}`;
}

async function loadFrame() {
  try {
    let apiBase = await getApiBase();
    let reachable = await isApiBaseReachable(apiBase);
    if (!reachable && apiBase !== DEFAULT_API_BASE) {
      const fallbackReachable = await isApiBaseReachable(DEFAULT_API_BASE);
      if (fallbackReachable) {
        apiBase = DEFAULT_API_BASE;
        await setApiBase(apiBase);
        setStatus(`Saved API base was unreachable. Switched to ${apiBase}.`, false);
      }
      reachable = fallbackReachable;
    }
    if (!reachable) {
      throw new Error(`Could not reach app at ${apiBase}. Start Flask/background app first.`);
    }
    const modelUrl = await getLatestModelUrl();
    const frameUrl = buildFrameUrl(apiBase, modelUrl);
    appFrame.src = frameUrl;
    if (modelUrl) {
      setStatus("Loaded app with latest Q-captured model.");
    } else {
      setStatus("App loaded. Hover a MakerWorld model and press Q.");
    }
  } catch (error) {
    setStatus(error && error.message ? error.message : "Failed to load app frame.", true);
  }
}

reloadFrameBtn?.addEventListener("click", () => {
  loadFrame();
});

appFrame?.addEventListener("load", () => {
  chrome.runtime.sendMessage({ action: "clear_badge" }, () => {});
});

// Resize iframe to full content height so the popup scrolls as one unified page
window.addEventListener("message", (e) => {
  if (e.data && e.data.type === "dc_resize" && typeof e.data.height === "number") {
    if (appFrame) appFrame.style.height = e.data.height + "px";
    return;
  }
  if (e.data && (e.data.type === "dc_close_overlay" || e.data.type === "dc_close_popup")) {
    window.close();
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local") return;
  if (!changes[STORAGE_KEYS.latest]) return;
  loadFrame();
});

loadFrame();
