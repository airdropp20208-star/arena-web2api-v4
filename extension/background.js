/**
 * Arena Token Broker — background script (MV2 persistent).
 *
 * Mantiene WebSocket connection tới arena-web2api server (default ws://localhost:8765).
 * Khi server cần reCAPTCHA token → gửi WS message {"type":"need_token","id":"..."}
 * → extension executeScript trong arena.ai tab (MAIN world) để gọi grecaptcha.enterprise.execute
 * → gửi token back qua WS: {"type":"token","id":"...","token":"..."}
 *
 * Auto-reconnect khi WS disconnect. Alarms wake background every 25s (Kiwi may throttle).
 *
 * Auto-keepalive:
 *   - Auto-open arena.ai tab nếu chưa có (background start)
 *   - Auto-reopen arena.ai tab nếu user đóng
 *   - Auto-relogin qua /sign-in/email nếu session hết hạn (khi server báo 401)
 *   - Cookie refresh on demand (server request → extension extract → send back)
 */

const DEFAULT_WS_URL = "ws://127.0.0.1:8765";
const ARENA_URL = "https://arena.ai";
const RECAPTCHA_SITE_KEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0";
const RECAPTCHA_ACTION = "chat_submit";
const RECONNECT_DELAY_MS = 3000;
const TOKEN_REQUEST_TIMEOUT_MS = 30000;
// KHÔNG auto-open arena.ai tab — user sẽ mở manual khi cần
// (tránh che game khi extension load)
const AUTO_OPEN_ARENA = false;

// Arena credentials for auto-relogin (loaded from storage)
let arenaEmail = "";
let arenaPassword = "";

let ws = null;
let wsUrl = DEFAULT_WS_URL;
let connected = false;
let lastError = "";
let tokenCount = 0;
let cookieRefreshCount = 0;
let pendingRequests = new Map(); // id → {resolve, reject, timer}

// ── Load config ────────────────────────────────────────────────────────────
chrome.storage.local.get(["wsUrl", "arenaEmail", "arenaPassword"], (result) => {
  if (result.wsUrl) wsUrl = result.wsUrl;
  if (result.arenaEmail) arenaEmail = result.arenaEmail;
  if (result.arenaPassword) arenaPassword = result.arenaPassword;
  connect();
  // Auto-open arena.ai tab on extension start
  if (AUTO_OPEN_ARENA) {
    setTimeout(ensureArenaTab, 1500);
  }
});

// ── Alarm để giữ background alive + reconnect ──────────────────────────────
chrome.alarms.create("keepalive", { periodInMinutes: 0.4 }); // ~24s
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "keepalive") {
    if (!ws || ws.readyState === WebSocket.CLOSED) {
      connect();
    }
    // Also check arena.ai tab is still alive
    if (AUTO_OPEN_ARENA) {
      ensureArenaTab().catch(() => {});
    }
  }
});

// ── Auto-reopen arena.ai tab khi user đóng ─────────────────────────────────
chrome.tabs.onRemoved.addListener(async (tabId, removeInfo) => {
  if (!AUTO_OPEN_ARENA) return;
  // Check if any arena.ai tab still exists
  const tabs = await new Promise((resolve) => {
    chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
  });
  if (!tabs || tabs.length === 0) {
    console.log("[ArenaBroker] arena.ai tab closed, will reopen in", ARENA_TAB_REOPEN_DELAY_MS, "ms");
    setTimeout(ensureArenaTab, ARENA_TAB_REOPEN_DELAY_MS);
  }
});

async function ensureArenaTab() {
  const tabs = await new Promise((resolve) => {
    chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
  });
  if (tabs && tabs.length > 0) {
    return tabs[0];
  }
  // Open new tab in background (active=false to not steal focus)
  console.log("[ArenaBroker] Opening arena.ai tab (background)...");
  try {
    const tab = await new Promise((resolve) => {
      chrome.tabs.create({ url: ARENA_URL, active: false }, resolve);
    });
    console.log("[ArenaBroker] arena.ai tab opened:", tab.id);
    // Wait for tab to load
    await new Promise((r) => setTimeout(r, 5000));
    // Auto-relogin if we have credentials
    if (arenaEmail && arenaPassword) {
      await autoRelogin();
    }
    return tab;
  } catch (e) {
    console.error("[ArenaBroker] Failed to open arena.ai tab:", e);
    return null;
  }
}

async function autoRelogin() {
  if (!arenaEmail || !arenaPassword) {
    console.log("[ArenaBroker] No credentials, skip auto-relogin");
    return false;
  }
  const tabs = await new Promise((resolve) => {
    chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
  });
  if (!tabs || tabs.length === 0) {
    console.log("[ArenaBroker] No arena.ai tab for relogin");
    return false;
  }
  const tab = tabs[0];
  console.log("[ArenaBroker] Auto-relogin as", arenaEmail);
  try {
    const result = await new Promise((resolve) => {
      chrome.tabs.executeScript(tab.id, {
        code: `
          (async () => {
            try {
              const r = await fetch('/nextjs-api/sign-in/email', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                  email: ${JSON.stringify(arenaEmail)},
                  password: ${JSON.stringify(arenaPassword)},
                  shouldLinkHistory: true
                })
              });
              let body = null; try { body = await r.json(); } catch(e) {}
              return {status: r.status, success: body && body.success, message: body && body.message};
            } catch (e) {
              return {error: e.message || String(e)};
            }
          })();
        `,
      }, (results) => {
        if (chrome.runtime.lastError) {
          resolve([{error: chrome.runtime.lastError.message}]);
        } else {
          resolve(results || []);
        }
      });
    });
    const r = result && result[0];
    if (r && r.status === 200 && r.success) {
      console.log("[ArenaBroker] Auto-relogin OK");
      return true;
    } else {
      console.error("[ArenaBroker] Auto-relogin failed:", r);
      return false;
    }
  } catch (e) {
    console.error("[ArenaBroker] Auto-relogin error:", e);
    return false;
  }
}

// ── WebSocket connection ───────────────────────────────────────────────────
let reconnectAttempts = 0;
let maxReconnectInterval = 30000; // cap at 30s

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  reconnectAttempts++;
  log("Connecting to " + wsUrl + " (attempt " + reconnectAttempts + ")...");
  try {
    ws = new WebSocket(wsUrl);
  } catch (e) {
    lastError = "WebSocket construction failed: " + e.message;
    log(lastError);
    // Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s, 30s, ...
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), maxReconnectInterval);
    setTimeout(connect, delay);
    return;
  }

  ws.onopen = () => {
    connected = true;
    lastError = "";
    reconnectAttempts = 0; // reset on success
    console.log("[ArenaBroker] WS connected to", wsUrl);
    // Send hello with capabilities
    ws.send(JSON.stringify({
      type: "hello",
      agent: "kiwi-extension",
      version: "1.0.0",
      hasArenaTab: false, // will update after check
    }));
    updateBadge("connected");
    checkArenaTab();
  };

  ws.onclose = (event) => {
    connected = false;
    const reason = event.reason || "(no reason)";
    const code = event.code;
    lastError = `Disconnected (code=${code}, reason="${reason}")`;
    console.log("[ArenaBroker] WS closed:", { code, reason });
    updateBadge("disconnected");
    // Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s
    const delay = Math.min(1000 * Math.pow(2, Math.max(0, reconnectAttempts - 1)), maxReconnectInterval);
    console.log("[ArenaBroker] Reconnect in", delay, "ms");
    setTimeout(connect, delay);
  };

  ws.onerror = (err) => {
    // ECONNREFUSED = server not running on that port
    // Try to extract more info
    let errMsg = "WebSocket error";
    if (reconnectAttempts === 1) {
      errMsg = "Cannot connect to " + wsUrl + " — server chưa chạy hoặc port sai. Chạy 'arena start' trong Termux.";
    } else if (reconnectAttempts > 5) {
      errMsg = "Still cannot connect after " + reconnectAttempts + " attempts. Check: (1) server running (arena status), (2) port 8765 not blocked, (3) wsUrl correct in popup.";
    } else {
      errMsg = "WebSocket error (attempt " + reconnectAttempts + ")";
    }
    lastError = errMsg;
    console.error("[ArenaBroker]", errMsg, err);
  };

  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      console.error("[ArenaBroker] Invalid message:", event.data);
      return;
    }

    if (msg.type === "need_token") {
      handleTokenRequest(msg.id || Date.now().toString());
    } else if (msg.type === "need_cookies") {
      handleCookieRequest(msg.id || Date.now().toString());
    } else if (msg.type === "relogin") {
      // Server báo 401 → extension relogin để refresh arena-auth cookie
      const ok = await autoRelogin();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "relogin_result", id: msg.id, ok: ok }));
      }
    } else if (msg.type === "ping") {
      ws.send(JSON.stringify({ type: "pong", id: msg.id }));
    } else {
      console.log("[ArenaBroker] Unknown message:", msg);
    }
  };
}

// ── Cookie refresh handler ─────────────────────────────────────────────────
async function handleCookieRequest(id) {
  console.log("[ArenaBroker] Cookie request", id);
  try {
    const cookies = await extractArenaCookies();
    cookieRefreshCount++;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "cookies",
        id: id,
        ok: true,
        cookies: cookies,
      }));
    }
    console.log("[ArenaBroker] Cookies sent for", id, "— keys:", Object.keys(cookies));
  } catch (e) {
    console.error("[ArenaBroker] Cookie extract failed:", e);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "cookies",
        id: id,
        ok: false,
        error: e.message || String(e),
      }));
    }
  }
}

async function extractArenaCookies() {
  // Lấy cookies từ Chrome cookie API (chrome.cookies.get)
  // Cần permission "cookies" — đã có trong manifest
  const wantedNames = [
    "arena-auth-prod-v1.0",
    "arena-auth-prod-v1.1",
    "cf_clearance",
    "__cf_bm",
    "user_country_code",
  ];
  const out = {};
  for (const name of wantedNames) {
    const cookie = await new Promise((resolve) => {
      chrome.cookies.get({ url: "https://arena.ai", name: name }, resolve);
    });
    if (cookie && cookie.value) {
      out[name] = cookie.value;
    }
  }
  if (!out["arena-auth-prod-v1.0"] || !out["arena-auth-prod-v1.1"]) {
    throw new Error(
      "Missing arena-auth-prod-v1.0 or .1 cookie. Make sure arena.ai is logged in."
    );
  }
  return out;
}

// ── Token generation ───────────────────────────────────────────────────────
async function handleTokenRequest(id) {
  console.log("[ArenaBroker] Token request", id);
  const startTime = Date.now();
  try {
    // Timeout 15s — nếu grecaptcha không trả token, fail
    const token = await Promise.race([
      generateTokenInArenaTab(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Token gen timeout (15s)")), 15000)),
    ]);
    if (!token) {
      throw new Error("generateTokenInArenaTab returned empty token");
    }
    tokenCount++;
    console.log("[ArenaBroker] Token gen OK", id, "len=" + token.length, "in " + (Date.now()-startTime) + "ms");

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "token",
        id: id,
        token: token,
        ok: true,
      }));
      console.log("[ArenaBroker] Token sent to broker for", id);
    } else {
      console.warn("[ArenaBroker] WS not open, token not sent (count still incremented)");
    }
    return token;
  } catch (e) {
    console.error("[ArenaBroker] Token gen failed:", e);
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "token",
        id: id,
        ok: false,
        error: e.message || String(e),
      }));
    }
    throw e;  // re-throw để caller catch
  }
}

async function generateTokenInArenaTab() {
  // Tìm tab arena.ai đang mở
  const tabs = await new Promise((resolve) => {
    chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
  });

  if (!tabs || tabs.length === 0) {
    throw new Error("No arena.ai tab open. Please open https://arena.ai in Kiwi Browser.");
  }

  // Lấy tab active nhất (hoặc tab đầu tiên)
  const tab = tabs[0];
  console.log("[ArenaBroker] Using arena.ai tab:", tab.id, tab.url);

  // Execute trong MAIN world để truy cập window.grecaptcha
  const results = await new Promise((resolve) => {
    chrome.tabs.executeScript(tab.id, {
      code: `
        (async () => {
          try {
            // Đợi grecaptcha load (max 10s)
            const start = Date.now();
            while (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {
              if (Date.now() - start > 10000) {
                return { error: 'grecaptcha not loaded after 10s' };
              }
              await new Promise(r => setTimeout(r, 200));
            }
            // Gen token
            const token = await new Promise((resolve, reject) => {
              grecaptcha.enterprise.ready(async () => {
                try {
                  const t = await grecaptcha.enterprise.execute(
                    '${RECAPTCHA_SITE_KEY}',
                    { action: '${RECAPTCHA_ACTION}' }
                  );
                  resolve(t);
                } catch (e) {
                  reject(e);
                }
              });
            });
            if (!token || token.length < 100) {
              return { error: 'token too short: ' + (token ? token.length : 0) };
            }
            return { token: token };
          } catch (e) {
            return { error: e.message || String(e) };
          }
        })();
      `,
    }, (results) => {
      if (chrome.runtime.lastError) {
        resolve([{ error: chrome.runtime.lastError.message }]);
      } else {
        resolve(results || []);
      }
    });
  });

  if (!results || results.length === 0) {
    throw new Error("executeScript returned no result");
  }

  const result = results[0];
  if (typeof result === 'object' && result.error) {
    throw new Error(result.error);
  }
  if (typeof result === 'object' && result.token) {
    return result.token;
  }
  throw new Error("Unexpected result: " + JSON.stringify(result));
}

// ── Helpers ────────────────────────────────────────────────────────────────
async function checkArenaTab() {
  const tabs = await new Promise((resolve) => {
    chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
  });
  const hasTab = tabs && tabs.length > 0;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "status", hasArenaTab: hasTab, tabCount: tabs.length }));
  }
  return hasTab;
}

function updateBadge(state) {
  let text = "";
  let color = "#999";
  if (state === "connected") {
    text = "ON";
    color = "#0a0";
  } else if (state === "disconnected") {
    text = "OFF";
    color = "#a00";
  } else if (state === "error") {
    text = "ERR";
    color = "#a00";
  }
  chrome.browserAction.setBadgeText({ text });
  chrome.browserAction.setBadgeBackgroundColor({ color });
}

// ── Message từ popup ───────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "get_status") {
    sendResponse({
      connected: connected,
      wsUrl: wsUrl,
      lastError: lastError,
      tokenCount: tokenCount,
      cookieRefreshCount: cookieRefreshCount,
      hasArenaTab: true, // checked separately
    });
    return true;
  }
  if (msg.type === "set_ws_url") {
    wsUrl = msg.wsUrl || DEFAULT_WS_URL;
    chrome.storage.local.set({ wsUrl: wsUrl });
    if (ws) {
      ws.close();
      // reconnect sẽ tự động
    } else {
      connect();
    }
    sendResponse({ ok: true });
    return true;
  }
  if (msg.type === "force_reconnect") {
    // Force close + reconnect ngay lập tức
    if (ws) {
      try { ws.close(); } catch(e) {}
      ws = null;
    }
    reconnectAttempts = 0;
    lastError = "";
    connect();
    sendResponse({ ok: true });
    return true;
  }
  if (msg.type === "test_token") {
    // Fix: handleTokenRequest trả về token, không throw khi success
    // Cần await result trước khi sendResponse
    const testId = "test_" + Date.now();
    handleTokenRequest(testId).then(() => {
      // tokenCount đã tăng trong handleTokenRequest
      sendResponse({ ok: true, tokenCount: tokenCount });
    }).catch((e) => {
      sendResponse({ ok: false, error: e.message || String(e) });
    });
    return true;  // keep channel open for async sendResponse
  }
  if (msg.type === "test_cookies") {
    extractArenaCookies().then((cookies) => {
      cookieRefreshCount++;
      sendResponse({ ok: true, cookies: cookies });
    }).catch((e) => {
      sendResponse({ ok: false, error: e.message });
    });
    return true;
  }
  if (msg.type === "update_credentials") {
    chrome.storage.local.get(["arenaEmail", "arenaPassword"], (result) => {
      arenaEmail = result.arenaEmail || "";
      arenaPassword = result.arenaPassword || "";
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.type === "relogin_now") {
    autoRelogin().then((ok) => {
      sendResponse({ ok: ok, error: ok ? null : "relogin failed (check console)" });
    }).catch((e) => {
      sendResponse({ ok: false, error: e.message });
    });
    return true;
  }
});

// Initial badge
updateBadge("disconnected");
console.log("[ArenaBroker] Background loaded, will connect to", wsUrl);
