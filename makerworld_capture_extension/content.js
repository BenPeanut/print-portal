(() => {
  let hoveredModelUrl = "";
  let captureInFlight = false;
  const OVERLAY_ROOT_ID = "mw-capture-overlay-root";
  let lastPointerX = 0;
  let lastPointerY = 0;

  console.log("[MakerWorld Capture] Content script loaded");

  function showExtensionReady() {
    try {
      const badge = document.createElement("div");
      badge.id = "mw-capture-extension-ready";
      badge.textContent = "✓ Capture Ready (Q)";
      badge.style.position = "fixed";
      badge.style.bottom = "18px";
      badge.style.left = "18px";
      badge.style.padding = "8px 12px";
      badge.style.background = "#0f766e";
      badge.style.color = "#fff";
      badge.style.fontSize = "12px";
      badge.style.borderRadius = "6px";
      badge.style.fontFamily = "Segoe UI, Arial, sans-serif";
      badge.style.zIndex = "2147483646";
      badge.style.boxShadow = "0 4px 12px rgba(0,0,0,0.15)";
      document.body.appendChild(badge);
      setTimeout(() => {
        try { badge.remove(); } catch {}
      }, 6000);
    } catch (e) {
      console.log("[MakerWorld Capture] Badge error:", e);
    }
  }

  function findMakerLink(target) {
    let node = target;
    while (node && node !== document.body) {
      if (node.tagName === "A" && node.href && /makerworld\.com/i.test(node.href)) {
        return node.href;
      }
      node = node.parentElement;
    }
    return "";
  }

  function normalizeModelUrl(raw) {
    const text = String(raw || "").trim();
    if (!text) return "";
    try {
      const parsed = new URL(text, window.location.href);
      const host = String(parsed.hostname || "").toLowerCase();
      const isMaker = host === "makerworld.com" || host.endsWith(".makerworld.com");
      if (!isMaker) return "";
      if (!/\/models\//i.test(parsed.pathname || "")) return "";
      return parsed.toString();
    } catch {
      return "";
    }
  }

  function resolveCaptureUrl() {
    const hovered = normalizeModelUrl(hoveredModelUrl);
    if (hovered) return hovered;

    try {
      const pointed = document.elementFromPoint(lastPointerX, lastPointerY);
      const pointedLink = findMakerLink(pointed);
      const normalizedPointed = normalizeModelUrl(pointedLink);
      if (normalizedPointed) {
        hoveredModelUrl = normalizedPointed;
        return normalizedPointed;
      }
    } catch {
      // Ignore and continue fallback.
    }

    const current = normalizeModelUrl(window.location.href);
    if (current) {
      hoveredModelUrl = current;
      return current;
    }
    return "";
  }

  function showToast(text, ok, duration = 2200) {
    const existing = document.getElementById("mw-capture-extension-toast");
    if (existing) existing.remove();

    const toast = document.createElement("div");
    toast.id = "mw-capture-extension-toast";
    toast.textContent = text;
    toast.style.position = "fixed";
    toast.style.right = "18px";
    toast.style.bottom = "18px";
    toast.style.padding = "10px 13px";
    toast.style.borderRadius = "11px";
    toast.style.fontFamily = "Segoe UI, Arial, sans-serif";
    toast.style.fontSize = "13px";
    toast.style.zIndex = "2147483647";
    toast.style.color = "#fff";
    toast.style.background = ok ? "#1f7a50" : "#9f3131";
    toast.style.boxShadow = "0 10px 28px rgba(0,0,0,0.24)";

    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), duration);
  }

  function closeCaptureOverlay() {
    const root = document.getElementById(OVERLAY_ROOT_ID);
    if (root) {
      root.remove();
    }
  }

  function openCaptureOverlay(frameUrl) {
    if (!frameUrl) {
      return { ok: false, error: "Missing frame URL." };
    }

    closeCaptureOverlay();

    const root = document.createElement("div");
    root.id = OVERLAY_ROOT_ID;
    root.style.position = "fixed";
    root.style.inset = "0";
    root.style.zIndex = "2147483647";
    root.style.background = "rgba(10, 14, 23, 0.58)";
    root.style.backdropFilter = "blur(2px)";
    root.style.display = "flex";
    root.style.alignItems = "center";
    root.style.justifyContent = "center";

    const panel = document.createElement("div");
    panel.style.position = "relative";
    panel.style.width = "min(1220px, 96vw)";
    panel.style.height = "min(900px, 92vh)";
    panel.style.background = "#0f172a";
    panel.style.border = "1px solid rgba(148, 163, 184, 0.28)";
    panel.style.borderRadius = "14px";
    panel.style.overflow = "hidden";
    panel.style.boxShadow = "0 24px 80px rgba(0,0,0,0.45)";

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.textContent = "Close";
    closeBtn.style.position = "absolute";
    closeBtn.style.top = "10px";
    closeBtn.style.right = "10px";
    closeBtn.style.zIndex = "2";
    closeBtn.style.border = "1px solid rgba(148, 163, 184, 0.35)";
    closeBtn.style.background = "rgba(15, 23, 42, 0.88)";
    closeBtn.style.color = "#e2e8f0";
    closeBtn.style.borderRadius = "8px";
    closeBtn.style.padding = "6px 10px";
    closeBtn.style.cursor = "pointer";
    closeBtn.addEventListener("click", closeCaptureOverlay);

    const frame = document.createElement("iframe");
    frame.src = frameUrl;
    frame.title = "MakerWorld Desktop Capture";
    frame.style.width = "100%";
    frame.style.height = "100%";
    frame.style.border = "0";
    frame.setAttribute("sandbox", "allow-same-origin allow-scripts allow-forms allow-popups");

    root.addEventListener("click", (event) => {
      if (event.target === root) {
        closeCaptureOverlay();
      }
    });

    const handleEsc = (event) => {
      if (event.key === "Escape") {
        closeCaptureOverlay();
      }
    };
    document.addEventListener("keydown", handleEsc, true);

    const observer = new MutationObserver(() => {
      if (!document.body.contains(root)) {
        document.removeEventListener("keydown", handleEsc, true);
        observer.disconnect();
      }
    });
    observer.observe(document.body, { childList: true });

    panel.appendChild(closeBtn);
    panel.appendChild(frame);
    root.appendChild(panel);
    document.body.appendChild(root);
    return { ok: true };
  }

  function sendCaptureMessage(payload, onDone) {
    try {
      if (typeof chrome === "undefined" || !chrome.runtime || !chrome.runtime.id) {
        onDone({ ok: false, error: "Extension was reloaded. Refresh this page and try again." });
        return;
      }

      let finished = false;
      const failSafeTimer = setTimeout(() => {
        if (finished) return;
        finished = true;
        onDone({ ok: false, error: "Capture timed out waiting for extension response." });
      }, 15000);

      chrome.runtime.sendMessage(payload, (response) => {
        if (finished) return;
        finished = true;
        clearTimeout(failSafeTimer);

        if (chrome.runtime.lastError) {
          onDone({ ok: false, error: chrome.runtime.lastError.message || "Extension messaging failed." });
          return;
        }
        try {
          onDone(response);
        } catch {
          onDone({ ok: false, error: "Capture callback failed. Refresh this page and try again." });
        }
      });
    } catch (error) {
      onDone({
        ok: false,
        error: "Extension context invalidated. Refresh this page and try again."
      });
    }
  }

  document.addEventListener(
    "mouseover",
    (event) => {
      const url = findMakerLink(event.target);
      if (url) hoveredModelUrl = url;
    },
    true
  );

  document.addEventListener(
    "mousemove",
    (event) => {
      lastPointerX = Number(event.clientX || 0);
      lastPointerY = Number(event.clientY || 0);
      const url = findMakerLink(event.target);
      if (url) hoveredModelUrl = url;
    },
    true
  );

  function isQHotkey(event) {
    const key = String(event && event.key || "").toLowerCase();
    const code = String(event && event.code || "").toLowerCase();
    return key === "q" || code === "keyq";
  }

  function handleCaptureHotkey(event) {
    if (isQHotkey(event)) {
      console.log("[MakerWorld Capture] Q pressed. hoveredModelUrl:", hoveredModelUrl, "captureInFlight:", captureInFlight);
    }

    try {
      const tag = (event.target && event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || (event.target && event.target.isContentEditable)) return;
      if (event.repeat) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (!isQHotkey(event)) return;

      event.preventDefault();
      event.stopPropagation();

      const captureUrl = resolveCaptureUrl();

      if (!captureUrl) {
        showToast("Open or hover a MakerWorld model, then press Q.", false);
        return;
      }

      if (captureInFlight) {
        showToast("Capture already running...", true, 1600);
        return;
      }

      captureInFlight = true;
      showToast("Capturing model...", true, 5000);

      sendCaptureMessage(
        {
          action: "capture_from_hover",
          modelUrl: captureUrl,
          sourcePage: location.href,
          triggeredAt: new Date().toISOString()
        },
        (response) => {
          captureInFlight = false;
          if (!response || !response.ok) {
            showToast((response && response.error) || "Capture failed. Refresh this page and try again.", false, 4000);
            return;
          }
          showToast("Model captured. Opening in-page capture panel.", true, 3200);
        }
      );
    } catch {
      captureInFlight = false;
      showToast("Capture failed: extension context invalidated. Refresh this page.", false, 4000);
    }
  }

  function scanPageForModels() {
    const seen = new Set();
    const results = [];
    document.querySelectorAll("a[href]").forEach(function (a) {
      const norm = normalizeModelUrl(a.href);
      if (!norm || seen.has(norm)) return;
      seen.add(norm);

      let title = "";
      const container =
        a.closest("article, [class*='card'], [class*='item'], [class*='model'], li, section") ||
        a.parentElement;
      if (container && container !== document.body) {
        const heading = container.querySelector("h1, h2, h3, h4, h5");
        if (heading) title = (heading.textContent || "").trim();
        if (!title) {
          const titleEl = container.querySelector("[class*='title'], [class*='name'], [class*='label']");
          if (titleEl) title = (titleEl.textContent || "").trim();
        }
      }
      if (!title) title = (a.textContent || "").trim();
      if (!title) {
        const img = a.querySelector("img[alt]");
        if (img) title = (img.alt || "").trim();
      }
      title = title.replace(/\s+/g, " ").trim().slice(0, 120);

      const idMatch = norm.match(/\/models\/(\d+)/);
      const modelId = idMatch ? idMatch[1] : "";
      results.push({ url: norm, title: title || ("Model " + modelId), modelId: modelId });
    });
    return results;
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  async function deepScanModels(options) {
    const opts = options || {};
    const maxScrollSteps = Math.max(1, Math.min(120, Number(opts.maxScrollSteps || 36)));
    const settleMs = Math.max(120, Math.min(2000, Number(opts.settleMs || 350)));
    const stopAfterNoGrowth = Math.max(2, Math.min(12, Number(opts.stopAfterNoGrowth || 4)));
    const maxSuggestions = Math.max(10, Math.min(500, Number(opts.maxSuggestions || 120)));

    let best = scanPageForModels();
    let noGrowthRuns = 0;

    for (let step = 0; step < maxScrollSteps; step += 1) {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" });
      await sleep(settleMs);
      const current = scanPageForModels();
      if (current.length > best.length) {
        best = current;
        noGrowthRuns = 0;
      } else {
        noGrowthRuns += 1;
      }
      if (noGrowthRuns >= stopAfterNoGrowth) {
        break;
      }
    }

    return {
      models: best.slice(0, maxSuggestions),
      total_found: best.length,
      suggested_count: Math.min(best.length, maxSuggestions),
      scan_mode: "deep",
    };
  }

  // Use window capture phase so this fires before MakerWorld page JS,
  // which can call stopPropagation at document/element level.
  window.addEventListener("keydown", handleCaptureHotkey, true);

  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (!message) return;

      if (message.action === "get_page_models") {
        (async function () {
          try {
            const deep = Boolean(message.options && message.options.deep);
            const payload = deep
              ? await deepScanModels(message.options)
              : (function () {
                const models = scanPageForModels();
                return {
                  models: models,
                  total_found: models.length,
                  suggested_count: models.length,
                  scan_mode: "quick",
                };
              })();
            sendResponse({ ok: true, ...payload });
          } catch (error) {
            sendResponse({ ok: true, models: [], total_found: 0, suggested_count: 0, scan_mode: "error" });
          }
        })();
        return true;
      }

      if (message.action === "open_capture_overlay") {
        try {
          const result = openCaptureOverlay(String(message.frameUrl || ""));
          sendResponse(result);
        } catch (error) {
          sendResponse({ ok: false, error: error && error.message ? String(error.message) : "Overlay failed." });
        }
        return true;
      }
    });
  }

  window.addEventListener("message", (event) => {
    const data = event && event.data;
    if (!data || typeof data !== "object") {
      return;
    }
    if (data.type === "dc_close_overlay") {
      closeCaptureOverlay();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showExtensionReady);
  } else {
    showExtensionReady();
  }
})();
