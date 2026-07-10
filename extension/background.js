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
// Pre-token: extension tự gen token mỗi 80s, POST về server
// Server cache token, dùng ngay khi chat request → realtime
const PRETOKEN_INTERVAL_MS = 80000;
const TOKEN_TTL_MS = 110000; // 110s (token valid ~120s)
const COOKIE_INTERVAL_MS = 300000; // 5 min — auto-submit cookies

let connected = false;
let lastError = "";
let tokenCount = 0;
let cookieRefreshCount = 0;
let pollTimer = null;
let preTokenTimer = null;
let cookieTimer = null;
let isGenerating = false;
let lastPreTokenAt = 0;

// ── Load config ────────────────────────────────────────────────────────────
chrome.storage.local.get(["serverUrl"], (result) => {
    if (result.serverUrl) {
        SERVER_URL = result.serverUrl;
    }
    startPolling();
    startPreToken();
    startCookieSubmit();
});

// ── Alarm để giữ background alive ──────────────────────────────────────────
chrome.alarms.create("keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "keepalive") {
        if (!pollTimer) startPolling();
        if (!preTokenTimer) startPreToken();
        if (!cookieTimer) startCookieSubmit();
    }
});

// ── KHÔNG auto-open arena.ai tab — user tự mở khi cần ─────────────────────
// Tránh tạo nhiều tab mỗi lần extension reload
// (Android kill background → reload → tạo tab mới → lặp lại)

// ── Pre-token: tự gen token mỗi 80s, POST về server ───────────────────────
// Server cache token, dùng ngay khi chat request → realtime (0ms latency)
function startPreToken() {
    if (preTokenTimer) return;
    console.log("[ArenaBroker] Starting pre-token gen every", PRETOKEN_INTERVAL_MS / 1000, "s");
    // Gen ngay lần đầu (sau 3s delay cho tab load)
    setTimeout(genAndSubmitPreToken, 3000);
    preTokenTimer = setInterval(genAndSubmitPreToken, PRETOKEN_INTERVAL_MS);
}

async function genAndSubmitPreToken() {
    // Skip nếu đang gen on-demand
    if (isGenerating) {
        console.log("[ArenaBroker] Pre-token skipped — on-demand gen in progress");
        return;
    }
    // Skip nếu token chưa expire
    if (lastPreTokenAt && (Date.now() - lastPreTokenAt) < PRETOKEN_INTERVAL_MS) {
        return;
    }

    console.log("[ArenaBroker] Pre-token gen started...");
    try {
        const token = await Promise.race([
            generateTokenInArenaTab(),
            new Promise((_, reject) => setTimeout(() => reject(new Error("Pre-token timeout 15s")), 15000)),
        ]);

        if (!token || token.length < 50) {
            throw new Error("Invalid pre-token");
        }

        // POST pre-token to server (id = "pretoken")
        const resp = await fetch(`${SERVER_URL}/admin/token`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                id: "pretoken",
                token: token,
                ok: true,
                pre: true,  // flag: this is a pre-token, cache it
            }),
        });

        if (resp.ok) {
            lastPreTokenAt = Date.now();
            tokenCount++;
            console.log("[ArenaBroker] Pre-token submitted ✓ (len=" + token.length + ")");
        }
    } catch (e) {
        console.log("[ArenaBroker] Pre-token gen failed (will retry):", e.message);
        // Không fatal — sẽ retry lần sau
    }
}

// ── Auto-cookie: extract + POST mỗi 5min ──────────────────────────────────
// Server tự update cookie pool — user không cần paste .env
function startCookieSubmit() {
    if (cookieTimer) return;
    console.log("[ArenaBroker] Starting auto-cookie submit every", COOKIE_INTERVAL_MS / 1000, "s");
    // Submit ngay sau 5s (sau khi tab load)
    setTimeout(submitCookies, 5000);
    cookieTimer = setInterval(submitCookies, COOKIE_INTERVAL_MS);
}

async function submitCookies() {
    try {
        const cookies = await extractArenaCookies();
        if (!cookies || !cookies["arena-auth-prod-v1.0"]) return;

        const resp = await fetch(`${SERVER_URL}/admin/cookies/submit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cookies: cookies }),
        });

        if (resp.ok) {
            cookieRefreshCount++;
            console.log("[ArenaBroker] Auto-cookie submitted ✓ (keys:", Object.keys(cookies).length + ")");
        }
    } catch (e) {
        console.log("[ArenaBroker] Auto-cookie failed (will retry):", e.message);
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
