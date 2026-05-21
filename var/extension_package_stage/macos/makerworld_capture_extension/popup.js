const STORAGE_KEYS = {
  settings: "capture_settings",
  authToken: "extension_auth_token",
};

const DEFAULT_API_BASE = "http://127.0.0.1:5000";

const statusLine = document.getElementById("status-line");
const loggedOutView = document.getElementById("logged-out-view");
const loggedInView = document.getElementById("logged-in-view");
const usernameInput = document.getElementById("username-input");
const passwordInput = document.getElementById("password-input");
const loginBtn = document.getElementById("login-btn");
const logoutBtn = document.getElementById("logout-btn");
const welcomeLine = document.getElementById("welcome-line");
const fallbackView = document.getElementById("fallback-view");
const fallbackTitle = document.getElementById("fallback-title");
const fallbackCopy = document.getElementById("fallback-copy");
const openSetupBtn = document.getElementById("open-setup-btn");
const openPortalBtn = document.getElementById("open-portal-btn");

function sendRuntimeMessage(payload) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(payload, function (response) {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: chrome.runtime.lastError.message || "Extension runtime error" });
          return;
        }
        resolve(response || { ok: false, error: "No response" });
      });
    } catch (error) {
      resolve({ ok: false, error: (error && error.message) || "Extension runtime error" });
    }
  });
}

function hideFallbackView() {
  if (fallbackView) fallbackView.hidden = true;
}

function showDesktopSetupFallback(apiBase) {
  if (!fallbackView) return;
  fallbackView.hidden = false;
  if (fallbackTitle) fallbackTitle.textContent = "Start Local Backend";
  if (fallbackCopy) {
    fallbackCopy.textContent = `Backend server not responding (${apiBase}). Open setup guide, run bootstrap, then reopen this popup.`;
  }
  if (openPortalBtn) openPortalBtn.hidden = true;
}

function showChromebookFallback() {
  if (!fallbackView) return;
  fallbackView.hidden = false;
  if (fallbackTitle) fallbackTitle.textContent = "Chromebook Mode";
  if (fallbackCopy) {
    fallbackCopy.textContent = "Chromebook cannot run this local Flask backend. Continue in the hosted web portal.";
  }
  if (openPortalBtn) openPortalBtn.hidden = false;
}

async function getPlatformMode() {
  const response = await sendRuntimeMessage({ action: "get_platform_mode" });
  if (!response || !response.ok) {
    return { mode: "desktop-local" };
  }
  return response;
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

function setStatus(text, isError = false) {
  if (!statusLine) return;
  statusLine.textContent = String(text || "");
  statusLine.classList.toggle("error", Boolean(isError));
}

async function getApiBase() {
  const localData = await chrome.storage.local.get([STORAGE_KEYS.settings]);
  let settings = localData[STORAGE_KEYS.settings] || null;
  if (!settings) {
    const synced = await chrome.storage.sync.get([STORAGE_KEYS.settings]);
    settings = synced[STORAGE_KEYS.settings] || {};
    if (Object.keys(settings).length) {
      await chrome.storage.local.set({ [STORAGE_KEYS.settings]: settings });
    }
  }
  return normalizeLocalApiBase(settings && settings.apiBase);
}

async function authFetch(path, options = {}) {
  const apiBase = await getApiBase();
  const authData = await chrome.storage.local.get([STORAGE_KEYS.authToken]);
  const authToken = String(authData[STORAGE_KEYS.authToken] || "").trim();
  const response = await fetch(`${apiBase}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(authToken ? { "X-Extension-Auth": authToken } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  return { response, payload };
}

function renderAuthState(state) {
  const loggedIn = Boolean(state && state.logged_in);
  if (loggedOutView) loggedOutView.hidden = loggedIn;
  if (loggedInView) loggedInView.hidden = !loggedIn;
  if (welcomeLine) {
    const username = String((state && state.username) || "").trim();
    welcomeLine.textContent = username
      ? `Logged in as ${username}. Orders from the extension will use this account.`
      : "You are logged in.";
  }
}

async function checkBackendHealth() {
  // Check if the Flask backend is running and responding.
  try {
    const apiBase = await getApiBase();
    const response = await fetch(`${apiBase}/api/health`, {
      credentials: "include",
      cache: "no-store",
      timeout: 3000,
    });
    return response.ok;
  } catch (error) {
    return false;
  }
}

async function refreshAuthState() {
  try {
    const platform = await getPlatformMode();
    if (platform.mode === "chromebook-hosted") {
      renderAuthState({ logged_in: false, username: "" });
      await chrome.storage.local.remove([STORAGE_KEYS.authToken]);
      showChromebookFallback();
      setStatus("Use hosted website mode on Chromebook.", false);
      return;
    }

    // Check backend health first
    const backendHealthy = await checkBackendHealth();
    if (!backendHealthy) {
      renderAuthState({ logged_in: false, username: "" });
      await chrome.storage.local.remove([STORAGE_KEYS.authToken]);
      const apiBase = await getApiBase();
      showDesktopSetupFallback(apiBase);
      setStatus(`Backend server not responding (${apiBase}).`, true);
      return;
    }

    hideFallbackView();

    const { response, payload } = await authFetch("/extension-api/user-auth-status", { method: "GET" });
    if (!response.ok || !payload || !payload.ok) {
      throw new Error((payload && payload.error) || "Could not check login status.");
    }
    renderAuthState(payload);
    if (payload.logged_in) {
      if (payload.extension_auth_token) {
        await chrome.storage.local.set({ [STORAGE_KEYS.authToken]: String(payload.extension_auth_token) });
      }
      setStatus("Signed in. Hover a model and press Q to open the order popup.");
    } else {
      await chrome.storage.local.remove([STORAGE_KEYS.authToken]);
      setStatus("Sign in to connect extension ordering to your account.");
    }
  } catch (error) {
    renderAuthState({ logged_in: false, username: "" });
    setStatus((error && error.message) || "Unable to reach the print portal app.", true);
  }
}

async function handleLogin() {
  const username = String((usernameInput && usernameInput.value) || "").trim();
  const password = String((passwordInput && passwordInput.value) || "");
  if (!username || !password) {
    setStatus("Username and password are required.", true);
    return;
  }

  loginBtn && (loginBtn.disabled = true);
  setStatus("Signing in...");
  try {
    const { response, payload } = await authFetch("/extension-api/user-login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    if (!response.ok || !payload || !payload.ok) {
      throw new Error((payload && payload.error) || "Login failed.");
    }
    if (payload.extension_auth_token) {
      await chrome.storage.local.set({ [STORAGE_KEYS.authToken]: String(payload.extension_auth_token) });
    }
    if (passwordInput) passwordInput.value = "";
    await refreshAuthState();
  } catch (error) {
    setStatus((error && error.message) || "Login failed.", true);
  } finally {
    loginBtn && (loginBtn.disabled = false);
  }
}

async function handleLogout() {
  logoutBtn && (logoutBtn.disabled = true);
  setStatus("Signing out...");
  try {
    const { response, payload } = await authFetch("/extension-api/user-logout", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (!response.ok || !payload || !payload.ok) {
      throw new Error((payload && payload.error) || "Logout failed.");
    }
    await chrome.storage.local.remove([STORAGE_KEYS.authToken]);
    await refreshAuthState();
  } catch (error) {
    setStatus((error && error.message) || "Logout failed.", true);
  } finally {
    logoutBtn && (logoutBtn.disabled = false);
  }
}

loginBtn?.addEventListener("click", handleLogin);
logoutBtn?.addEventListener("click", handleLogout);

passwordInput?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    handleLogin();
  }
});

openSetupBtn?.addEventListener("click", async () => {
  await sendRuntimeMessage({ action: "open_extension_setup" });
});

openPortalBtn?.addEventListener("click", async () => {
  await sendRuntimeMessage({ action: "open_hosted_portal" });
});

refreshAuthState();
