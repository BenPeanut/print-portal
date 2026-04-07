const STORAGE_KEYS = {
  settings: "capture_settings",
  latest: "latest_capture"
};

const DEFAULT_API_BASE = "http://127.0.0.1:5000";

const statusLine = document.getElementById("status-line");
const appFrame = document.getElementById("app-frame");
const reloadFrameBtn = document.getElementById("reload-frame-btn");
const scannerList = document.getElementById("scanner-list");
const modelCount = document.getElementById("model-count");
const scanBtn = document.getElementById("scan-btn");
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
      setStatus("Loaded app with latest captured model.");
    } else {
      setStatus("App loaded. Press Scan to discover model links on the page.");
    }
  } catch (error) {
    setStatus(error && error.message ? error.message : "Failed to load app frame.", true);
  }
}

async function captureModelFromList(modelUrl, btn) {
  setStatus("Capturing model...");
  try {
    const result = await new Promise((resolve) => {
      chrome.runtime.sendMessage(
        { action: "capture_from_hover", modelUrl, sourcePage: modelUrl, triggeredAt: new Date().toISOString() },
        resolve
      );
    });
    if (result && result.ok) {
      setStatus("Captured. Loading into app...");
      if (btn) {
        btn.textContent = "Captured";
        btn.style.color = "#0d7f3b";
      }
      await loadFrame();
    } else {
      setStatus((result && result.error) || "Capture failed.", true);
      if (btn) {
        btn.textContent = "Retry";
        btn.disabled = false;
      }
    }
  } catch (err) {
    setStatus("Capture failed.", true);
    if (btn) {
      btn.textContent = "Retry";
      btn.disabled = false;
    }
  }
}

function renderModelList(models) {
  if (!scannerList) return;
  scannerList.innerHTML = "";

  if (!models.length) {
    scannerList.innerHTML = "<p class=\"scanner-empty\">No model links found. Open a MakerWorld listing page and try Scan again.</p>";
    return;
  }

  models.forEach(function (m) {
    const row = document.createElement("div");
    row.className = "scanner-row";

    const titleEl = document.createElement("span");
    titleEl.className = "scanner-title";
    titleEl.textContent = m.title || m.url;
    titleEl.title = m.url;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "scanner-capture-btn";
    btn.textContent = "Capture";
    btn.addEventListener("click", function () {
      btn.textContent = "...";
      btn.disabled = true;
      captureModelFromList(m.url, btn);
    });

    row.appendChild(titleEl);
    row.appendChild(btn);
    scannerList.appendChild(row);
  });
}

async function scanAndRenderPageModels() {
  if (scanBtn) scanBtn.disabled = true;
  if (modelCount) modelCount.textContent = "Scanning...";
  if (scannerList) scannerList.innerHTML = "";
  setStatus("Deep scanning current MakerWorld page...");

  try {
    const response = await new Promise((resolve) => {
      chrome.runtime.sendMessage(
        {
          action: "scan_active_tab_for_models",
          options: {
            deep: true,
            maxScrollSteps: 48,
            settleMs: 320,
            stopAfterNoGrowth: 4,
            maxSuggestions: 120,
          },
        },
        resolve
      );
    });

    const models = (response && Array.isArray(response.models)) ? response.models : [];
    const totalFound = Number(response && response.total_found) || models.length;
    const suggestedCount = Number(response && response.suggested_count) || models.length;
    if (modelCount) modelCount.textContent = `${suggestedCount} suggested / ${totalFound} found`;

    renderModelList(models);

    if (response && response.error) {
      setStatus(response.error, true);
    } else {
      setStatus(`Scan complete: ${totalFound} found, suggesting ${suggestedCount}.`);
    }
  } catch (_err) {
    if (modelCount) modelCount.textContent = "0";
    renderModelList([]);
    setStatus("Could not scan the current tab.", true);
  } finally {
    if (scanBtn) scanBtn.disabled = false;
  }
}

reloadFrameBtn?.addEventListener("click", async () => {
  await loadFrame();
  await scanAndRenderPageModels();
});

scanBtn?.addEventListener("click", scanAndRenderPageModels);

appFrame?.addEventListener("load", () => {
  chrome.runtime.sendMessage({ action: "clear_badge" }, () => {});
});

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
scanAndRenderPageModels();
