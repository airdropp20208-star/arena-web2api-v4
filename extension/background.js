/**
 * Arena Token Broker — HTTP polling version (NO WebSocket).
 *
 * Extension poll http://127.0.0.1:8000/admin/poll mỗi 2s.
 * Nếu server cần token → gen token → POST http://127.0.0.1:8000/admin/token.
 *
 * Ưu điểm so với WebSocket:
 * - Không bị disconnect khi Android kill background
 * - Không cần broker-only.sh, không cần 2 luồng Termux
 * - Đơn giản, ít code, ít bug
 */

const SERVER_URL = "http://127.0.0.1:8000";
const POLL_INTERVAL_MS = 2000;
const RECAPTCHA_SITE_KEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0";
const RECAPTCHA_ACTION = "chat_submit";
const ARENA_URL = "https://arena.ai";

let connected = false;
let lastError = "";
let tokenCount = 0;
let cookieRefreshCount = 0;
let pollTimer = null;
let isGenerating = false;

// ── Load config ────────────────────────────────────────────────────────────
chrome.storage.local.get(["serverUrl"], (result) => {
    if (result.serverUrl) {
        SERVER_URL = result.serverUrl;
    }
    startPolling();
});

// ── Alarm để giữ background alive ──────────────────────────────────────────
chrome.alarms.create("keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "keepalive") {
        if (!pollTimer) {
            startPolling();
        }
    }
});

// ── Auto-reopen arena.ai tab khi user đóng ─────────────────────────────────
chrome.tabs.onRemoved.addListener(async (tabId, removeInfo) => {
    const tabs = await new Promise((resolve) => {
        chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
    });
    if (!tabs || tabs.length === 0) {
        console.log("[ArenaBroker] arena.ai tab closed, will reopen in 2s");
        setTimeout(ensureArenaTab, 2000);
    }
});

async function ensureArenaTab() {
    const tabs = await new Promise((resolve) => {
        chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
    });
    if (tabs && tabs.length > 0) {
        return tabs[0];
    }
    console.log("[ArenaBroker] Opening arena.ai tab (background)...");
    try {
        const tab = await new Promise((resolve) => {
            chrome.tabs.create({ url: ARENA_URL, active: false }, resolve);
        });
        await new Promise((r) => setTimeout(r, 5000));
        return tab;
    } catch (e) {
        console.error("[ArenaBroker] Failed to open arena.ai tab:", e);
        return null;
    }
}

// ── Polling loop ───────────────────────────────────────────────────────────
function startPolling() {
    if (pollTimer) return;
    console.log("[ArenaBroker] Starting HTTP polling to", SERVER_URL);
    poll();
    pollTimer = setInterval(poll, POLL_INTERVAL_MS);
}

async function poll() {
    try {
        const resp = await fetch(`${SERVER_URL}/admin/poll`, {
            method: "GET",
            headers: { "Content-Type": "application/json" },
        });

        if (!resp.ok) {
            lastError = `Server returned ${resp.status}`;
            if (connected) {
                console.error("[ArenaBroker]", lastError);
                connected = false;
                updateBadge("disconnected");
            }
            return;
        }

        if (!connected) {
            connected = true;
            lastError = "";
            console.log("[ArenaBroker] Connected to server via HTTP polling");
            updateBadge("connected");
        }

        const data = await resp.json();

        if (data.need_token && data.id && !isGenerating) {
            console.log("[ArenaBroker] Server needs token, id:", data.id);
            handleTokenRequest(data.id);
        }
    } catch (e) {
        if (connected) {
            lastError = `Cannot reach server: ${e.message}`;
            console.error("[ArenaBroker]", lastError);
            connected = false;
            updateBadge("disconnected");
        }
    }
}

// ── Token generation ───────────────────────────────────────────────────────
async function handleTokenRequest(requestId) {
    isGenerating = true;
    console.log("[ArenaBroker] Gen token for", requestId);
    const startTime = Date.now();
    try {
        // Timeout 15s
        const token = await Promise.race([
            generateTokenInArenaTab(),
            new Promise((_, reject) => setTimeout(() => reject(new Error("Timeout 15s")), 15000)),
        ]);

        if (!token || token.length < 50) {
            throw new Error(`Invalid token (len=${token ? token.length : 0})`);
        }

        tokenCount++;
        console.log(`[ArenaBroker] Token gen OK in ${Date.now()-startTime}ms, posting to server...`);

        // POST token to server
        const postResp = await fetch(`${SERVER_URL}/admin/token`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                id: requestId,
                token: token,
                ok: true,
            }),
        });

        if (postResp.ok) {
            console.log("[ArenaBroker] Token submitted to server ✓");
        } else {
            console.error("[ArenaBroker] Token submit failed:", postResp.status);
        }
    } catch (e) {
        console.error("[ArenaBroker] Token gen failed:", e);
        // POST error to server
        try {
            await fetch(`${SERVER_URL}/admin/token`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    id: requestId,
                    ok: false,
                    error: e.message || String(e),
                }),
            });
        } catch (postErr) {
            console.error("[ArenaBroker] Cannot post error to server:", postErr);
        }
    } finally {
        isGenerating = false;
    }
}

async function generateTokenInArenaTab() {
    const tabs = await new Promise((resolve) => {
        chrome.tabs.query({ url: "https://arena.ai/*" }, resolve);
    });

    if (!tabs || tabs.length === 0) {
        throw new Error("No arena.ai tab open");
    }

    const tab = tabs[0];
    const results = await new Promise((resolve) => {
        chrome.tabs.executeScript(tab.id, {
            code: `
                (async () => {
                    try {
                        const start = Date.now();
                        while (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {
                            if (Date.now() - start > 10000) return { error: 'grecaptcha not loaded' };
                            await new Promise(r => setTimeout(r, 200));
                        }
                        const token = await new Promise((resolve, reject) => {
                            grecaptcha.enterprise.ready(async () => {
                                try {
                                    const t = await grecaptcha.enterprise.execute(
                                        '${RECAPTCHA_SITE_KEY}',
                                        { action: '${RECAPTCHA_ACTION}' }
                                    );
                                    resolve(t);
                                } catch (e) { reject(e); }
                            });
                        });
                        if (!token || token.length < 50) return { error: 'token too short' };
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
    throw new Error("Unexpected result");
}

// ── Cookie extraction (for Test Cookies button) ────────────────────────────
async function extractArenaCookies() {
    const wantedNames = [
        "arena-auth-prod-v1.0", "arena-auth-prod-v1.1",
        "cf_clearance", "__cf_bm", "user_country_code",
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
    if (!out["arena-auth-prod-v1.0"]) {
        throw new Error("Missing arena-auth-prod-v1.0 cookie — login arena.ai first");
    }
    return out;
}

// ── Badge ──────────────────────────────────────────────────────────────────
function updateBadge(state) {
    let text = "";
    let color = "#999";
    if (state === "connected") {
        text = "ON";
        color = "#0a0";
    } else if (state === "disconnected") {
        text = "OFF";
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
            serverUrl: SERVER_URL,
            lastError: lastError,
            tokenCount: tokenCount,
            cookieRefreshCount: cookieRefreshCount,
        });
        return true;
    }
    if (msg.type === "set_server_url") {
        chrome.storage.local.set({ serverUrl: msg.serverUrl });
        SERVER_URL = msg.serverUrl;
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
        startPolling();
        sendResponse({ ok: true });
        return true;
    }
    if (msg.type === "test_token") {
        // Manually trigger a poll to check if server is up
        poll();
        sendResponse({ ok: true, message: "Polling triggered. Check popup status." });
        return true;
    }
    if (msg.type === "test_cookies") {
        extractArenaCookies().then((cookies) => {
            sendResponse({ ok: true, cookies: cookies });
        }).catch((e) => {
            sendResponse({ ok: false, error: e.message });
        });
        return true;
    }
    if (msg.type === "force_reconnect") {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
        connected = false;
        lastError = "";
        startPolling();
        sendResponse({ ok: true });
        return true;
    }
});

updateBadge("disconnected");
console.log("[ArenaBroker] Background loaded (HTTP polling mode)");
