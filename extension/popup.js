// Popup script — show status, save config, test token, manage credentials

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "get_status" }, (resp) => {
    if (!resp) return;
    const statusEl = document.getElementById("status");
    if (resp.connected) {
      statusEl.className = "status connected";
      statusEl.textContent = "✓ Connected to " + resp.wsUrl;
    } else {
      statusEl.className = "status disconnected";
      statusEl.textContent = "✗ Disconnected" + (resp.lastError ? " — " + resp.lastError : "");
    }
    document.getElementById("wsUrl").value = resp.wsUrl || "ws://127.0.0.1:8765";
    document.getElementById("tokenCount").textContent = resp.tokenCount || 0;
    document.getElementById("cookieCount").textContent = resp.cookieRefreshCount || 0;
    document.getElementById("error").textContent = resp.lastError || "";
  });

  // Check arena.ai tab
  chrome.tabs.query({ url: "https://arena.ai/*" }, (tabs) => {
    const el = document.getElementById("arenaTab");
    if (tabs && tabs.length > 0) {
      el.textContent = "✓ open (" + tabs.length + " tab)";
      el.style.color = "#4a9";
    } else {
      el.textContent = "✗ not open";
      el.style.color = "#f66";
    }
  });

  // Load saved credentials
  chrome.storage.local.get(["arenaEmail", "arenaPassword"], (result) => {
    if (result.arenaEmail) document.getElementById("arenaEmail").value = result.arenaEmail;
    if (result.arenaPassword) document.getElementById("arenaPassword").value = result.arenaPassword;
  });
}

document.getElementById("save").addEventListener("click", () => {
  const wsUrl = document.getElementById("wsUrl").value.trim();
  chrome.runtime.sendMessage({ type: "set_ws_url", wsUrl: wsUrl }, () => {
    setTimeout(refreshStatus, 500);
  });
});

document.getElementById("reconnect").addEventListener("click", () => {
  const btn = document.getElementById("reconnect");
  btn.textContent = "Reconnecting...";
  btn.disabled = true;
  chrome.runtime.sendMessage({ type: "force_reconnect" }, (resp) => {
    btn.textContent = "Force Reconnect";
    btn.disabled = false;
    setTimeout(refreshStatus, 1000);
  });
});

document.getElementById("test").addEventListener("click", () => {
  const btn = document.getElementById("test");
  btn.textContent = "Generating...";
  btn.disabled = true;
  chrome.runtime.sendMessage({ type: "test_token" }, (resp) => {
    btn.textContent = "Test Token";
    btn.disabled = false;
    const errEl = document.getElementById("error");
    errEl.className = "error";
    if (resp && resp.ok) {
      errEl.className = "success";
      errEl.textContent = "✓ Token generated successfully!";
      setTimeout(() => { errEl.textContent = ""; errEl.className = "error"; }, 3000);
    } else {
      errEl.textContent = "✗ " + (resp && resp.error ? resp.error : "unknown error");
    }
    refreshStatus();
  });
});

document.getElementById("testCookies").addEventListener("click", () => {
  const btn = document.getElementById("testCookies");
  btn.textContent = "Extracting...";
  btn.disabled = true;
  chrome.runtime.sendMessage({ type: "test_cookies" }, (resp) => {
    btn.textContent = "Test Cookies";
    btn.disabled = false;
    const errEl = document.getElementById("error");
    errEl.className = "error";
    if (resp && resp.ok) {
      errEl.className = "success";
      errEl.textContent = "✓ Cookies OK — keys: " + Object.keys(resp.cookies).join(", ");
      setTimeout(() => { errEl.textContent = ""; errEl.className = "error"; }, 3000);
    } else {
      errEl.textContent = "✗ " + (resp && resp.error ? resp.error : "unknown error");
    }
    refreshStatus();
  });
});

document.getElementById("saveCreds").addEventListener("click", () => {
  const email = document.getElementById("arenaEmail").value.trim();
  const password = document.getElementById("arenaPassword").value;
  chrome.storage.local.set({ arenaEmail: email, arenaPassword: password }, () => {
    chrome.runtime.sendMessage({ type: "update_credentials" });
    const errEl = document.getElementById("error");
    errEl.className = "success";
    errEl.textContent = "✓ Credentials saved locally";
    setTimeout(() => { errEl.textContent = ""; errEl.className = "error"; }, 2000);
  });
});

document.getElementById("reloginNow").addEventListener("click", () => {
  const btn = document.getElementById("reloginNow");
  btn.textContent = "Logging in...";
  btn.disabled = true;
  chrome.runtime.sendMessage({ type: "relogin_now" }, (resp) => {
    btn.textContent = "Relogin Now";
    btn.disabled = false;
    const errEl = document.getElementById("error");
    errEl.className = "error";
    if (resp && resp.ok) {
      errEl.className = "success";
      errEl.textContent = "✓ Relogin successful — cookies refreshed";
      setTimeout(() => { errEl.textContent = ""; errEl.className = "error"; }, 3000);
    } else {
      errEl.textContent = "✗ Relogin failed: " + (resp && resp.error ? resp.error : "unknown");
    }
  });
});

refreshStatus();
// Pause refresh when popup hidden — fix #21 (battery save)
// Visibility API: document.hidden = true when popup closed
let refreshTimer = null;

function startRefresh() {
  if (refreshTimer) return;
  refreshTimer = setInterval(refreshStatus, 2000);
}

function stopRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

// Initial start
startRefresh();

// Pause when hidden, resume when visible
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopRefresh();
  } else {
    refreshStatus(); // immediate refresh on reopen
    startRefresh();
  }
});

// Also pause on window blur (popup loses focus)
window.addEventListener("blur", stopRefresh);
window.addEventListener("focus", () => {
  refreshStatus();
  startRefresh();
});
