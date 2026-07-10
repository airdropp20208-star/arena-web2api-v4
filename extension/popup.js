// Popup — HTTP polling version

function refreshStatus() {
  chrome.runtime.sendMessage({ type: "get_status" }, (resp) => {
    if (!resp) return;
    const statusEl = document.getElementById("status");
    if (resp.connected) {
      statusEl.className = "status connected";
      statusEl.textContent = "✓ Connected to " + resp.serverUrl;
    } else {
      statusEl.className = "status disconnected";
      statusEl.textContent = "✗ Disconnected" + (resp.lastError ? " — " + resp.lastError : "");
    }
    document.getElementById("serverUrl").value = resp.serverUrl || "http://127.0.0.1:8000";
    document.getElementById("tokenCount").textContent = resp.tokenCount || 0;
    document.getElementById("error").textContent = resp.lastError || "";
  });

  // Check server directly
  const url = document.getElementById("serverUrl").value || "http://127.0.0.1:8000";
  fetch(url + "/health", { method: "GET" })
    .then(r => r.json())
    .then(d => {
      document.getElementById("serverStatus").textContent = d.status === "ok" ? "✓ running" : "?";
      document.getElementById("serverStatus").style.color = "#4a9";
    })
    .catch(() => {
      document.getElementById("serverStatus").textContent = "✗ down";
      document.getElementById("serverStatus").style.color = "#f66";
    });

  // Check arena tab
  chrome.tabs.query({ url: "https://arena.ai/*" }, (tabs) => {
    const el = document.getElementById("arenaTab");
    if (tabs && tabs.length > 0) {
      el.textContent = "✓ open (" + tabs.length + ")";
      el.style.color = "#4a9";
    } else {
      el.textContent = "✗ not open";
      el.style.color = "#f66";
    }
  });
}

document.getElementById("save").addEventListener("click", () => {
  const serverUrl = document.getElementById("serverUrl").value.trim().replace(/\/$/, "");
  chrome.runtime.sendMessage({ type: "set_server_url", serverUrl: serverUrl }, () => {
    setTimeout(refreshStatus, 500);
  });
});

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "force_reconnect" }, () => {
    setTimeout(refreshStatus, 500);
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
    if (resp && resp.ok) {
      errEl.className = "success";
      errEl.textContent = "✓ Cookies OK: " + Object.keys(resp.cookies).join(", ");
    } else {
      errEl.textContent = "✗ " + (resp && resp.error ? resp.error : "fail");
    }
    setTimeout(refreshStatus, 1000);
  });
});

refreshStatus();
let refreshTimer = setInterval(refreshStatus, 2000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  } else {
    refreshStatus();
    refreshTimer = setInterval(refreshStatus, 2000);
  }
});
