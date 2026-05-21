(() => {
  let hoveredModelUrl = "";
  let captureInFlight = false;
  const OVERLAY_ROOT_ID = "mw-capture-overlay-root";
  const FALLBACK_CART_CACHE_KEY = "mw_extension_cached_cart_items_v1";
  let lastPointerX = 0;
  let lastPointerY = 0;

  function getRuntime() {
    try {
      if (typeof chrome === "undefined" || !chrome.runtime) return null;
      return chrome.runtime;
    } catch (error) {
      return null;
    }
  }

  function sendRuntimeMessage(payload) {
    return new Promise(function (resolve) {
      const runtime = getRuntime();
      if (!runtime || typeof runtime.sendMessage !== "function") {
        resolve({ ok: false, error: "Extension runtime unavailable." });
        return;
      }
      try {
        runtime.sendMessage(payload, function (response) {
          const runtimeAfter = getRuntime();
          const lastError = runtimeAfter && runtimeAfter.lastError ? runtimeAfter.lastError : null;
          if (lastError) {
            resolve({ ok: false, error: lastError.message || "Extension messaging failed." });
            return;
          }
          resolve(response || { ok: false, error: "No response from extension." });
        });
      } catch (error) {
        resolve({ ok: false, error: (error && error.message) || "Extension messaging failed." });
      }
    });
  }

  console.log("[MakerWorld Capture] Content script loaded");

  function showExtensionReady() {
    try {
      const badge = document.createElement("div");
      badge.id = "mw-capture-extension-ready";
      badge.textContent = "✓ Extension Ready (Q = Order · C = Cart)";
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
        try { badge.remove(); } catch (error) {}
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
    } catch (error) {
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
    } catch (error) {
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

  function getHostCartNodes() {
    return {
      backdrop: document.getElementById("cart-backdrop"),
      drawer: document.getElementById("cart-drawer"),
      body: document.getElementById("cart-drawer-body"),
      closeBtn: document.getElementById("cart-drawer-close"),
      checkoutBtn: document.getElementById("cart-checkout-btn"),
      viewBtn: document.getElementById("cart-view-btn"),
      feedbackEl: document.getElementById("cart-feedback"),
      totalEl: document.getElementById("cart-total-price"),
      selectedCountEl: document.getElementById("cart-selected-count"),
      selectAllEl: document.getElementById("cart-select-all"),
      selectRow: document.getElementById("cart-select-row"),
      totalRow: document.getElementById("cart-total-row"),
    };
  }

  function ensureHostCartStyle() {
    if (document.getElementById("mw-extension-cart-style")) return;
    const style = document.createElement("style");
    style.id = "mw-extension-cart-style";
    style.textContent = [
      ".cart-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.38);backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);z-index:2200;opacity:0;pointer-events:none;transition:opacity .25s ease}",
      ".cart-backdrop.open{opacity:1;pointer-events:auto}",
      ".cart-drawer{position:fixed;top:0;right:0;height:100dvh;width:min(420px,96vw);background:#fff;border-radius:12px 0 0 12px;box-shadow:-14px 0 44px rgba(0,0,0,.22);z-index:2210;display:flex;flex-direction:column;transform:translateX(100%);transition:transform .3s cubic-bezier(.4,0,.2,1);font-family:'Segoe UI',Arial,sans-serif;font-size:16px;line-height:1.35}",
      ".cart-drawer.open{transform:translateX(0)}",
      ".cart-drawer-header{display:flex;align-items:center;justify-content:space-between;padding:18px 20px;border-bottom:1px solid #f0f0f0;background:#fff}",
      ".cart-drawer-title{font-size:17px;font-weight:900;color:#0f5132;margin:0;display:flex;align-items:center;gap:8px}",
      ".cart-drawer-close{border:none;background:none;cursor:pointer;color:#60766b;font-size:21px;padding:4px 6px;border-radius:6px;line-height:1}",
      ".cart-drawer-close:hover{background:#f3f8f5;color:#183027}",
      ".cart-drawer-body{flex:1;overflow-y:auto;padding:14px 16px 10px}",
      ".cart-drawer-empty{display:flex;align-items:center;justify-content:center;min-height:240px;color:#60766b;text-align:center}",
      ".cart-drawer-item{display:flex;align-items:center;gap:12px;padding:12px;border:1px solid #eee;border-radius:8px;margin-bottom:10px;background:#fff}",
      ".cart-item-select{width:18px;height:18px;accent-color:#00ae42;cursor:pointer;flex-shrink:0}",
      ".cart-item-main{flex:1;min-width:0;display:flex;align-items:center;gap:12px}",
      ".cart-item-swatch{width:15px;height:15px;border-radius:50%;border:1px solid rgba(0,0,0,.12);flex-shrink:0}",
      ".cart-item-details{flex:1;min-width:0}",
      ".cart-item-name{font-weight:800;font-size:15px;color:#183027;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin:0 0 3px}",
      ".cart-item-meta{font-size:13px;color:#888;margin:0}",
      ".cart-item-side{display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex-shrink:0}",
      ".cart-item-price{font-weight:900;font-size:15px;color:#183027;margin:0;white-space:nowrap}",
      ".cart-item-remove{border:none;background:transparent;cursor:pointer;color:#dc2626;padding:2px;display:flex;align-items:center;justify-content:center;border-radius:6px;line-height:1;transition:all 0.2s ease}",
      ".cart-item-remove:hover:not(:disabled){background:#fff1f1}",
      ".cart-item-remove:disabled{opacity:0.6;cursor:not-allowed}",
      ".cart-item-remove.deleting{color:#9ca3af}",
      ".cart-item-remove.deleting svg{animation:spin 0.75s linear infinite}",
      ".cart-item-remove.deleted{color:#4ade80}",
      ".cart-item-remove.error{color:#f87171;animation:pulse-error 0.4s ease}",
      "@keyframes spin{to{transform:rotate(360deg)}}",
      "@keyframes pulse-error{0%,100%{opacity:1}50%{opacity:0.5}}",
      ".cart-drawer-footer{padding:14px 20px 18px}",
      ".cart-footer-rule{border:none;border-top:1px solid #ededed;margin:0 0 14px}",
      ".cart-select-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border:1px solid #d8e6de;border-radius:10px;background:#fff;margin-bottom:12px}",
      ".cart-select-all{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:800;color:#1f5940}",
      ".cart-selected-count{font-size:13px;color:#5a7267;font-weight:700}",
      ".cart-total-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;font-size:15px;font-weight:800;color:#0f5132}",
      ".cart-total-price{font-size:23px;color:#00ae42}",
      ".cart-feedback{min-height:20px;margin:0 0 10px;color:#5a7267;font-size:13px;font-weight:700}",
      ".cart-feedback.error{color:#b91c1c}",
      ".cart-checkout-btn{display:block;width:100%;min-height:45px;padding:13px;background:linear-gradient(180deg,#10bf56 0%,#00ae42 100%);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:900;cursor:pointer;text-align:center;box-shadow:0 6px 18px rgba(0,174,66,.28)}",
      ".cart-checkout-btn[data-empty='true']{background:#b0c4b8;box-shadow:none;pointer-events:none}",
      ".cart-checkout-btn:disabled{opacity:.78;cursor:wait}",
      ".cart-view-btn{display:block;width:100%;min-height:42px;margin-top:8px;padding:10px;border:1px solid #cde2d7;border-radius:10px;text-align:center;text-decoration:none;color:#1f5940;font-size:14px;font-weight:800;background:#f7fcf9}",
      ".cart-view-btn:hover{background:#edf8f2}",
      ".cart-checkout-success{min-height:100%;display:flex;align-items:center;justify-content:center}",
      ".cart-checkout-success-card{width:100%;border:1px solid #d7e8de;border-radius:18px;background:linear-gradient(180deg,#ffffff 0%,#f4fbf7 100%);padding:24px 20px;text-align:center;box-shadow:0 18px 34px rgba(20,61,40,.08)}",
      ".cart-checkout-success-icon{width:56px;height:56px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;margin-bottom:14px;background:linear-gradient(180deg,#17c35a 0%,#00ae42 100%);color:#ffffff;box-shadow:0 10px 24px rgba(0,174,66,.24)}",
      ".cart-checkout-success-title{margin:0 0 8px;color:#143d2a;font-size:18px;font-weight:900}",
      ".cart-checkout-success-copy{margin:0 auto 16px;max-width:260px;color:#557266;font-size:14px;line-height:1.5}",
      ".cart-checkout-success-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:16px}",
      ".cart-checkout-success-stat{border:1px solid #dce9e2;border-radius:14px;padding:12px 10px;background:#ffffff}",
      ".cart-checkout-success-stat-value{display:block;color:#0f5132;font-size:17px;font-weight:900}",
      ".cart-checkout-success-stat-label{display:block;margin-top:4px;color:#6b7f76;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.05em}",
      ".cart-checkout-success-actions{display:flex;flex-direction:column;gap:8px}",
      ".cart-success-primary{display:block;width:100%;min-height:42px;padding:10px;border:none;border-radius:10px;background:#1a6b44;color:#fff;font-size:14px;font-weight:800;cursor:pointer}",
      ".cart-success-primary:hover{background:#0f4d30}",
      ".cart-success-secondary{display:block;width:100%;min-height:42px;padding:10px;border:1px solid #d8e6de;border-radius:10px;background:#fbfefd;color:#345f50;font-size:14px;font-weight:700;cursor:pointer}",
      ".cart-success-secondary:hover{background:#f4f8f6}",
      ".checkout-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.45);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);z-index:2190;opacity:0;pointer-events:none;transition:opacity .25s ease}",
      ".checkout-modal-backdrop.open{opacity:1;pointer-events:auto}",
      ".checkout-modal{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;z-index:2200;padding:20px;font-family:'Segoe UI',Arial,sans-serif;pointer-events:none;opacity:0;visibility:hidden;transition:opacity .2s ease,visibility .2s ease}",
      ".checkout-modal.open{pointer-events:auto;opacity:1;visibility:visible}",
      ".checkout-modal-container{width:100%;max-width:900px;max-height:90vh;background:#fff;border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.3);overflow:hidden;display:flex;flex-direction:column}",
      ".checkout-modal-header{display:flex;align-items:center;justify-content:space-between;padding:20px 24px;border-bottom:1px solid #e5e5e5;background:#fff}",
      ".checkout-modal-title{margin:0;font-size:18px;font-weight:900;color:#0f5132}",
      ".checkout-modal-close{border:none;background:none;cursor:pointer;color:#666;font-size:24px;padding:4px;border-radius:6px;line-height:1;transition:all .2s ease;z-index:2201}",
      ".checkout-modal-close:hover{background:#f0f0f0;color:#000}",
      ".checkout-modal-content{flex:1;overflow-y:auto;padding:24px;background:#fff}",
      ".checkout-modal-iframe{width:100%;height:75vh;border:0;background:#fff;display:block}",
      ".checkout-shell{max-width:860px;margin:0 auto}",
      ".checkout-modal-content #checkout-loading{display:none!important}",
      ".checkout-modal-content script{display:none!important}",
    ].join("");
    (document.head || document.documentElement).appendChild(style);
  }

  function ensureHostCartUi() {
    ensureHostCartStyle();
    const existing = getHostCartNodes();
    if (existing.drawer && existing.backdrop && existing.body) return existing;

    const shell = document.createElement("div");
    shell.id = "mw-extension-cart-root";
    shell.innerHTML = ''
      + '<div id="cart-backdrop" class="cart-backdrop" role="presentation"></div>'
      + '<aside id="cart-drawer" class="cart-drawer" aria-label="Shopping cart">'
      + '<div class="cart-drawer-header">'
      + '<h2 class="cart-drawer-title">Current Cart</h2>'
      + '<button id="cart-drawer-close" class="cart-drawer-close" type="button" aria-label="Close cart">&times;</button>'
      + '</div>'
      + '<div class="cart-drawer-body" id="cart-drawer-body"></div>'
      + '<div class="cart-drawer-footer">'
      + '<hr class="cart-footer-rule">'
      + '<div id="cart-select-row" class="cart-select-row">'
      + '<label class="cart-select-all"><input id="cart-select-all" type="checkbox" checked>Select all</label>'
      + '<span id="cart-selected-count" class="cart-selected-count">0 selected</span>'
      + '</div>'
      + '<div id="cart-total-row" class="cart-total-row"><span>Total Estimate</span><span class="cart-total-price" id="cart-total-price">Rp0</span></div>'
      + '<p id="cart-feedback" class="cart-feedback" aria-live="polite"></p>'
      + '<button id="cart-checkout-btn" class="cart-checkout-btn" type="button" data-empty="true">Open Cart to Checkout</button>'
      + '<a id="cart-view-btn" href="#" class="cart-view-btn">View Full Cart</a>'
      + '</div>'
      + '</aside>';
    (document.body || document.documentElement).appendChild(shell);
    return getHostCartNodes();
  }

  function closeDrawerFallback() {
    const nodes = ensureHostCartUi();
    if (nodes.drawer) nodes.drawer.classList.remove("open");
    if (nodes.backdrop) nodes.backdrop.classList.remove("open");
    if (document.body) document.body.style.overflow = "";
  }

  function formatPrice(value) {
    return "Rp" + Number(value || 0).toLocaleString("id-ID");
  }

  const fallbackCartState = {
    selectedIds: new Set(),
    cachedItems: [],
    listenersBound: false,
    checkoutResult: null,
    feedbackMessage: "",
    feedbackIsError: false,
  };

  function setFallbackCartFeedback(message, isError) {
    fallbackCartState.feedbackMessage = String(message || "");
    fallbackCartState.feedbackIsError = Boolean(isError);
    const nodes = getHostCartNodes();
    if (!nodes.feedbackEl) return;
    nodes.feedbackEl.textContent = fallbackCartState.feedbackMessage;
    nodes.feedbackEl.classList.toggle("error", fallbackCartState.feedbackIsError);
  }

  function clearFallbackCheckoutResult() {
    fallbackCartState.checkoutResult = null;
    setFallbackCartFeedback("", false);
  }

  function toggleFallbackCartFooterVisibility(isSuccess) {
    const nodes = getHostCartNodes();
    [
      nodes.selectRow,
      nodes.totalRow,
      nodes.feedbackEl,
      nodes.checkoutBtn,
      nodes.viewBtn,
    ].forEach(function (el) {
      if (el) el.hidden = Boolean(isSuccess);
    });
  }

  function openFallbackPortalPage(path) {
    const normalizedPath = "/" + String(path || "").replace(/^\/+/, "");
    window.open("http://127.0.0.1:5000" + normalizedPath, "_blank", "noopener,noreferrer");
  }

  function getCheckoutModalNodes() {
    return {
      backdrop: document.getElementById("checkout-modal-backdrop"),
      modal: document.getElementById("checkout-modal"),
      container: document.getElementById("checkout-modal-container"),
      closeBtn: document.getElementById("checkout-modal-close"),
      content: document.getElementById("checkout-modal-content"),
    };
  }

  function ensureCheckoutModal() {
    const existing = getCheckoutModalNodes();
    if (existing.modal && existing.backdrop && existing.content) return existing;

    const shell = document.createElement("div");
    shell.id = "mw-extension-checkout-root";
    shell.innerHTML = ''
      + '<div id="checkout-modal-backdrop" class="checkout-modal-backdrop" role="presentation"></div>'
      + '<div id="checkout-modal" class="checkout-modal" role="dialog" aria-modal="true" aria-labelledby="checkout-modal-title">'
      + '<div id="checkout-modal-container" class="checkout-modal-container">'
      + '<div class="checkout-modal-header">'
      + '<h2 id="checkout-modal-title" class="checkout-modal-title">Checkout</h2>'
      + '<button id="checkout-modal-close" class="checkout-modal-close" type="button" aria-label="Close checkout">&times;</button>'
      + '</div>'
      + '<div id="checkout-modal-content" class="checkout-modal-content"></div>'
      + '</div>'
      + '</div>';
    (document.body || document.documentElement).appendChild(shell);
    return getCheckoutModalNodes();
  }

  function closeCheckoutModal() {
    const nodes = getCheckoutModalNodes();
    if (nodes.modal) nodes.modal.classList.remove("open");
    if (nodes.backdrop) nodes.backdrop.classList.remove("open");
    if (nodes.content) nodes.content.innerHTML = "";
    if (document.body) document.body.style.overflow = "";
  }

  async function openCheckoutModal() {
    try {
      const nodes = ensureCheckoutModal();
      if (!nodes.content) return;

      const authResult = await sendRuntimeMessage({ action: "get_extension_auth_token" });
      let checkoutUrl = "http://127.0.0.1:5000/cart/embed";
      if (authResult && authResult.ok && authResult.token) {
        checkoutUrl += "?ext_auth=" + encodeURIComponent(String(authResult.token));
      }

      const iframe = document.createElement("iframe");
      iframe.className = "checkout-modal-iframe";
      iframe.src = checkoutUrl;
      iframe.title = "Cart Checkout";
      nodes.content.innerHTML = "";
      nodes.content.appendChild(iframe);

      if (nodes.modal) nodes.modal.classList.add("open");
      if (nodes.backdrop) nodes.backdrop.classList.add("open");
      if (document.body) document.body.style.overflow = "hidden";

      // Setup close button
      if (nodes.closeBtn) {
        nodes.closeBtn.onclick = function (e) {
          e.preventDefault();
          e.stopPropagation();
          closeCheckoutModal();
        };
      }

      // Setup backdrop click
      if (nodes.backdrop) {
        nodes.backdrop.onclick = function (e) {
          if (e.target === nodes.backdrop) {
            closeCheckoutModal();
          }
        };
      }

      // Setup escape key
      const handleEsc = function (e) {
        if (e.key === "Escape") {
          closeCheckoutModal();
          document.removeEventListener("keydown", handleEsc);
        }
      };
      document.addEventListener("keydown", handleEsc);

      // Cleanup observer
      if (nodes.modal) {
        const observer = new MutationObserver(() => {
          if (!document.body.contains(nodes.modal)) {
            observer.disconnect();
            document.removeEventListener("keydown", handleEsc);
          }
        });
        observer.observe(document.body, { childList: true });
      }
    } catch (error) {
      const nodes = getCheckoutModalNodes();
      if (nodes.content) {
        nodes.content.innerHTML = '<div style="padding:20px;text-align:center;color:#d9534f;"><p>Error opening checkout: ' + String(error.message || "Unknown error") + '</p></div>';
      }
    }
  }

  function renderFallbackCheckoutSuccessView(items) {
    const nodes = ensureHostCartUi();
    if (!nodes.body) return;
    const result = fallbackCartState.checkoutResult || {};
    const remainingCount = Array.isArray(items) ? items.length : 0;
    const orderCount = Number(result.orderCount || 0);
    const unitCount = Number(result.unitCount || 0);
    const summaryCopy = remainingCount
      ? "The selected items were checked out. You still have " + remainingCount + " item(s) left in your cart."
      : "Your selected items were checked out successfully. You can keep browsing without leaving MakerWorld.";

    nodes.body.innerHTML = ''
      + '<section class="cart-checkout-success">'
      + '<div class="cart-checkout-success-card">'
      + '<div class="cart-checkout-success-icon" aria-hidden="true">'
      + '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
      + '</div>'
      + '<h3 class="cart-checkout-success-title">' + String(result.message || "Checkout complete.") + '</h3>'
      + '<p class="cart-checkout-success-copy">' + summaryCopy + '</p>'
      + '<div class="cart-checkout-success-stats">'
      + '<div class="cart-checkout-success-stat"><span class="cart-checkout-success-stat-value">' + String(orderCount) + '</span><span class="cart-checkout-success-stat-label">Orders created</span></div>'
      + '<div class="cart-checkout-success-stat"><span class="cart-checkout-success-stat-value">' + formatPrice(result.grandTotal || 0) + '</span><span class="cart-checkout-success-stat-label">Total</span></div>'
      + '</div>'
      + '<div class="cart-checkout-success-actions">'
      + '<button id="cart-success-view-orders" class="cart-success-primary" type="button">View Orders</button>'
      + '<button id="cart-success-back" class="cart-success-secondary" type="button">' + (remainingCount ? 'Back to Cart' : 'Continue Browsing') + '</button>'
      + '</div>'
      + (unitCount > 0 ? '<p class="cart-checkout-success-copy">Processed ' + String(unitCount) + ' unit(s).</p>' : '')
      + '</div>'
      + '</section>';

    nodes.body.querySelector("#cart-success-view-orders")?.addEventListener("click", function () {
      closeDrawerFallback();
      openFallbackPortalPage("/history");
    });
    nodes.body.querySelector("#cart-success-back")?.addEventListener("click", function () {
      clearFallbackCheckoutResult();
      renderFallbackCartDrawer();
    });
  }

  function getCachedCartItems() {
    if (Array.isArray(fallbackCartState.cachedItems) && fallbackCartState.cachedItems.length) {
      return fallbackCartState.cachedItems.slice();
    }
    try {
      const parsed = JSON.parse(window.localStorage.getItem(FALLBACK_CART_CACHE_KEY) || "[]");
      const nextItems = Array.isArray(parsed) ? parsed : [];
      fallbackCartState.cachedItems = nextItems;
      return nextItems.slice();
    } catch (error) {
      return [];
    }
  }

  function setCachedCartItems(items) {
    const nextItems = Array.isArray(items) ? items : [];
    fallbackCartState.cachedItems = nextItems;
    try {
      window.localStorage.setItem(FALLBACK_CART_CACHE_KEY, JSON.stringify(nextItems));
    } catch (error) {
      // Keep flow non-fatal when cache write fails.
    }
    return nextItems.slice();
  }

  function upsertCachedCartItem(item) {
    if (!item || typeof item !== "object") return getCachedCartItems();
    fallbackCartState.checkoutResult = null;
    const id = String(item.id || item.orderId || item.order_id || "").trim();
    if (!id) return getCachedCartItems();
    const current = getCachedCartItems();
    const next = current.slice();
    const idx = next.findIndex(function (existing) {
      return String(existing && (existing.id || existing.orderId || existing.order_id) || "").trim() === id;
    });
    const normalized = Object.assign({}, item, {
      id: id,
      orderId: String(item.orderId || item.order_id || id).trim() || id,
    });
    if (idx >= 0) {
      next[idx] = Object.assign({}, next[idx], normalized);
    } else {
      next.push(normalized);
    }
    return setCachedCartItems(next);
  }

  function removeCachedCartItemById(id) {
    const cleanedId = String(id || "").trim();
    if (!cleanedId) return getCachedCartItems();
    const next = getCachedCartItems().filter(function (item) {
      return String(item && (item.id || item.orderId || item.order_id) || "").trim() !== cleanedId;
    });
    return setCachedCartItems(next);
  }

  function mapBackendOrderToCartItem(order) {
    const quantity = normalizeCartQuantity(order && order.quantity);
    const totalPrice = Number((order && order.print_price) || 0);
    const unitPrice = quantity > 0 ? totalPrice / quantity : totalPrice;
    const colorRaw = String((order && order.color) || "");
    const firstColor = colorRaw ? String(colorRaw).split("|")[0].split(":").pop().trim() : "";
    return {
      id: String((order && order.id) || "").trim(),
      orderId: String((order && order.id) || "").trim(),
      displayName: (order && (order.product_name || order.name)) || "Unnamed Model",
      link: (order && order.link) || "",
      colorMode: colorRaw.includes("|") ? "multi" : "single",
      singleFilament: firstColor,
      multiMappings: [],
      filamentHex: "#cccccc",
      profile: (order && order.profile) || "",
      weight: (Number((order && order.print_weight_g) || 0) || 0) / quantity,
      estimatedPrice: unitPrice,
      quantity: quantity,
    };
  }

  async function fetchBackendCartItems() {
    try {
      const response = await sendRuntimeMessage({ action: "get_cart_orders" });
      if (!response || !response.ok) {
        return getCachedCartItems();
      }
      const orders = Array.isArray(response.items) ? response.items : [];
      const items = orders.map(mapBackendOrderToCartItem).filter(function (item) {
        return String(item.id || "").trim();
      });
      setCachedCartItems(items);
      return items;
    } catch (error) {
      return getCachedCartItems();
    }
  }

  async function removeBackendCartItem(orderId) {
    const id = String(orderId || "").trim();
    if (!id) return { ok: false, error: "Missing order id." };
    return sendRuntimeMessage({ action: "remove_cart_item", orderId: id });
  }

  async function renderFallbackCartDrawer() {
    const nodes = ensureHostCartUi();
    const items = await fetchBackendCartItems();
    const allIds = items.map(function (item) { return String(item.id || ""); }).filter(Boolean);
    const idSet = new Set(allIds);
    fallbackCartState.selectedIds = new Set(Array.from(fallbackCartState.selectedIds).filter(function (id) { return idSet.has(id); }));
    if (!fallbackCartState.selectedIds.size) {
      allIds.forEach(function (id) { fallbackCartState.selectedIds.add(id); });
    }

    if (fallbackCartState.checkoutResult) {
      toggleFallbackCartFooterVisibility(true);
      renderFallbackCheckoutSuccessView(items);
      return;
    }

    toggleFallbackCartFooterVisibility(false);
    setFallbackCartFeedback(fallbackCartState.feedbackMessage, fallbackCartState.feedbackIsError);

    if (nodes.body) {
      if (!items.length) {
        nodes.body.innerHTML = '<div class="cart-drawer-empty"><p>Your cart is empty.</p></div>';
      } else {
        nodes.body.innerHTML = items.map(function (item) {
          const safeId = String(item.id || "").replace(/"/g, "&quot;");
          const qty = normalizeCartQuantity(item.quantity);
          const colorLabel = item.colorMode === "multi"
            ? ((Array.isArray(item.multiMappings) && item.multiMappings.length) ? item.multiMappings.length + " colors" : "Multi-color")
            : String(item.singleFilament || "Color TBD");
          const linePrice = formatPrice((Number(item.estimatedPrice) || 0) * qty);
          const checked = fallbackCartState.selectedIds.has(String(item.id || "")) ? "checked" : "";
          const swatch = String(item.filamentHex || "#cccccc");
          return ''
            + '<div class="cart-drawer-item">'
            + '<input class="cart-item-select" type="checkbox" data-id="' + safeId + '" ' + checked + '>'
            + '<div class="cart-item-main">'
            + '<div class="cart-item-swatch" style="background:' + swatch + ';"></div>'
            + '<div class="cart-item-details">'
            + '<p class="cart-item-name">' + String(item.displayName || "Unnamed Model") + '</p>'
            + '<p class="cart-item-meta">Qty ' + qty + ' - ' + colorLabel + '</p>'
            + '</div></div>'
            + '<div class="cart-item-side">'
            + '<p class="cart-item-price">' + linePrice + '</p>'
            + '<button class="cart-item-remove" type="button" data-id="' + safeId + '" aria-label="Remove item">'
            + '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>'
            + '</button>'
            + '</div></div>';
        }).join("");
      }
    }

    const selectedItems = items.filter(function (item) {
      return fallbackCartState.selectedIds.has(String(item.id || ""));
    });
    const total = selectedItems.reduce(function (sum, item) {
      return sum + (Number(item.estimatedPrice) || 0) * normalizeCartQuantity(item.quantity);
    }, 0);
    const selectedUnits = selectedItems.reduce(function (sum, item) {
      return sum + normalizeCartQuantity(item.quantity);
    }, 0);
    if (nodes.totalEl) nodes.totalEl.textContent = formatPrice(total);
    if (nodes.selectedCountEl) {
      nodes.selectedCountEl.textContent = selectedItems.length + " item(s), " + selectedUnits + " unit(s) selected";
    }
    if (nodes.checkoutBtn) {
      nodes.checkoutBtn.textContent = "Open Cart to Checkout";
      nodes.checkoutBtn.disabled = !selectedItems.length;
      nodes.checkoutBtn.setAttribute("data-empty", selectedItems.length ? "false" : "true");
    }
    if (nodes.selectAllEl) {
      nodes.selectAllEl.checked = items.length > 0 && selectedItems.length === items.length;
    }

    if (!fallbackCartState.listenersBound) {
      fallbackCartState.listenersBound = true;
      nodes.backdrop && nodes.backdrop.addEventListener("click", closeDrawerFallback);
      nodes.closeBtn && nodes.closeBtn.addEventListener("click", closeDrawerFallback);
      nodes.viewBtn && nodes.viewBtn.addEventListener("click", function (event) {
        event.preventDefault();
        openFallbackPortalPage("/cart");
      });
      nodes.checkoutBtn && nodes.checkoutBtn.addEventListener("click", function () {
        if (nodes.checkoutBtn.getAttribute("data-empty") === "true") return;
        const selectedItems = getCachedCartItems().filter(function (item) {
          return fallbackCartState.selectedIds.has(String(item && item.id || ""));
        });
        if (!selectedItems.length) {
          setFallbackCartFeedback("Select at least one cart item.", true);
          renderFallbackCartDrawer();
          return;
        }

        closeDrawerFallback();
        openCheckoutModal();
      });
      nodes.selectAllEl && nodes.selectAllEl.addEventListener("change", function () {
        const currentItems = getCachedCartItems();
        if (nodes.selectAllEl.checked) {
          fallbackCartState.selectedIds = new Set(currentItems.map(function (item) { return String(item.id || ""); }));
        } else {
          fallbackCartState.selectedIds.clear();
        }
        renderFallbackCartDrawer();
      });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") closeDrawerFallback();
      });
    }

    if (nodes.body) {
      nodes.body.querySelectorAll(".cart-item-select").forEach(function (el) {
        el.addEventListener("change", function () {
          const id = String(el.getAttribute("data-id") || "");
          if (!id) return;
          if (el.checked) fallbackCartState.selectedIds.add(id);
          else fallbackCartState.selectedIds.delete(id);
          renderFallbackCartDrawer();
        });
      });
      nodes.body.querySelectorAll(".cart-item-remove").forEach(function (btn) {
        btn.addEventListener("click", async function () {
          const id = String(btn.getAttribute("data-id") || "");
          if (!id) return;
          let originalText = btn.innerHTML;
          try {
            btn.disabled = true;
            btn.classList.add("deleting");
            btn.setAttribute("aria-busy", "true");
            btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="animation: spin 0.75s linear infinite;"><circle cx="12" cy="12" r="10"></circle></svg>';

            const result = await removeBackendCartItem(id);

            if (!result || !result.ok) {
              throw new Error((result && result.error) || "Failed to remove cart item.");
            }

            if (result && result.ok) {
              btn.classList.remove("deleting");
              btn.classList.add("deleted");
              btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>';
              await new Promise(function (resolve) { setTimeout(resolve, 350); });
            }

            removeCachedCartItemById(id);
            fallbackCartState.selectedIds.delete(id);
            renderFallbackCartDrawer();
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
        });
      });
    }
  }

  function openDrawerFallback() {
    const nodes = ensureHostCartUi();
    renderFallbackCartDrawer();
    if (nodes.drawer) nodes.drawer.classList.add("open");
    if (nodes.backdrop) nodes.backdrop.classList.add("open");
    if (document.body) {
      document.body.style.overflow = "hidden";
    }
  }

  function getCartOpenDelayMs(delayMs) {
    const parsed = Number.parseInt(delayMs, 10);
    if (!Number.isFinite(parsed) || parsed < 0) return 1000;
    return Math.min(parsed, 5000);
  }

  function normalizeCartQuantity(value) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed) || parsed < 1) return 1;
    return Math.min(parsed, 99);
  }

  async function saveCartItemToBackend(normalizedItem) {
    try {
      if (!normalizedItem || normalizedItem.orderId) return normalizedItem;
      const response = await sendRuntimeMessage({
        action: "save_cart_item",
        item: normalizedItem,
      });
      if (response && response.ok && response.order_id) {
        normalizedItem.orderId = String(response.order_id || "");
        normalizedItem.id = String(response.order_id || "");
      }
    } catch (error) {
      // Keep flow non-fatal when backend save fails.
    }
    return normalizedItem;
  }

  async function injectAddToCartBridge(cartItem, options) {
    if (!cartItem || typeof cartItem !== "object") {
      openDrawerFallback();
      return;
    }
    const normalizedItem = Object.assign({}, cartItem || {});
    normalizedItem.quantity = normalizeCartQuantity(normalizedItem.quantity);
    const delayMs = getCartOpenDelayMs(options && options.delayMs);
    await saveCartItemToBackend(normalizedItem);
    if (String(normalizedItem.id || normalizedItem.orderId || normalizedItem.order_id || "").trim()) {
      upsertCachedCartItem(normalizedItem);
    }

    if (typeof window.addToCart === "function") {
      setTimeout(function () {
        try {
          const maybePromise = window.addToCart(normalizedItem);
          if (maybePromise && typeof maybePromise.catch === "function") {
            maybePromise.catch(function () { openDrawerFallback(); });
          }
        } catch (error) {
          openDrawerFallback();
        }
      }, delayMs);
      return;
    }

    setTimeout(function () {
      openDrawerFallback();
    }, delayMs);
  }

  window.addEventListener("message", (event) => {
    const data = event && event.data;
    if (!data || data.source !== "mw-extension-overlay") {
      return;
    }
    if (data.action === "close") {
      closeCaptureOverlay();
      return;
    }
    if (data.action === "toast") {
      showToast(String(data.message || ""), Boolean(data.ok));
      return;
    }
    if (data.action === "cart-add") {
      injectAddToCartBridge(data.item || null, { delayMs: data.delayMs });
      return;
    }
    if (data.action === "open-url") {
      const targetUrl = String(data.url || "").trim();
      if (!targetUrl) return;
      try {
        window.location.href = targetUrl;
      } catch (error) {
        window.open(targetUrl, "_blank", "noopener,noreferrer");
      }
    }
  });

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
    root.style.padding = "12px";

    const frame = document.createElement("iframe");
    frame.src = frameUrl;
    frame.title = "MakerWorld Desktop Capture";
    frame.style.width = "min(1120px, 96vw)";
    frame.style.height = "min(900px, 92vh)";
    frame.style.border = "0";
    frame.style.borderRadius = "0";
    frame.style.overflow = "visible";
    frame.style.background = "transparent";
    frame.setAttribute("allowtransparency", "true");
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

    root.appendChild(frame);
    document.body.appendChild(root);
    return { ok: true };
  }

  function sendCaptureMessage(payload, onDone) {
    try {
      const runtime = getRuntime();
      if (!runtime || typeof runtime.sendMessage !== "function" || !runtime.id) {
        onDone({ ok: false, error: "Extension was reloaded. Refresh this page and try again." });
        return;
      }

      let finished = false;
      const failSafeTimer = setTimeout(() => {
        if (finished) return;
        finished = true;
        onDone({ ok: false, error: "Capture timed out waiting for extension response." });
      }, 15000);

      runtime.sendMessage(payload, (response) => {
        if (finished) return;
        finished = true;
        clearTimeout(failSafeTimer);

        const runtimeAfter = getRuntime();
        const lastError = runtimeAfter && runtimeAfter.lastError ? runtimeAfter.lastError : null;
        if (lastError) {
          onDone({ ok: false, error: lastError.message || "Extension messaging failed." });
          return;
        }
        try {
          onDone(response);
        } catch (error) {
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
    return Boolean(event) && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && key === "q";
  }

  function handleCaptureHotkey(event) {
    if (isQHotkey(event)) {
      console.log("[MakerWorld Capture] Q pressed. hoveredModelUrl:", hoveredModelUrl, "captureInFlight:", captureInFlight);
    }

    try {
      const tag = (event.target && event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || (event.target && event.target.isContentEditable)) return;
      if (event.repeat) return;
      if (!isQHotkey(event)) return;

      event.preventDefault();
      event.stopPropagation();

      const captureUrl = resolveCaptureUrl();

      if (!captureUrl) {
        showToast("Hover a MakerWorld model card, then press Q.", false);
        return;
      }

      if (captureInFlight) {
        showToast("Capture already running...", true, 1600);
        return;
      }

      captureInFlight = true;
      showToast("Opening model order popup...", true, 5000);

      sendCaptureMessage(
        {
          action: "open_hover_order_overlay",
          modelUrl: captureUrl,
          sourcePage: location.href,
          triggeredAt: new Date().toISOString()
        },
        (response) => {
          captureInFlight = false;
          if (!response || !response.ok) {
            showToast((response && response.error) || "Could not open order popup. Refresh this page and try again.", false, 4200);
            return;
          }
          showToast("Order popup opened.", true, 2400);
        }
      );
    } catch (error) {
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

  function isCartHotkey(event) {
    const key = String(event && event.key || "").toLowerCase();
    return Boolean(event) && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && key === "c";
  }

  function handleCartHotkey(event) {
    try {
      const tag = (event.target && event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || (event.target && event.target.isContentEditable)) return;
      if (event.repeat) return;
      if (!isCartHotkey(event)) return;
      // Don't intercept if the cart drawer is already open
      const drawer = document.getElementById("cart-drawer");
      if (drawer && drawer.classList.contains("open")) return;
      event.preventDefault();
      event.stopPropagation();
      openDrawerFallback();
    } catch (error) {
      // Keep flow non-fatal.
    }
  }

  // Use window capture phase so this fires before MakerWorld page JS,
  // which can call stopPropagation at document/element level.
  window.addEventListener("keydown", handleCaptureHotkey, true);
  window.addEventListener("keydown", handleCartHotkey, true);

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
    if (data.type === "closeCheckoutModal") {
      closeCheckoutModal();
    }
    if (data.action === "closeCheckoutModal") {
      closeCheckoutModal();
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showExtensionReady);
  } else {
    showExtensionReady();
  }
})();
