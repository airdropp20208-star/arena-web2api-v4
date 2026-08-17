const DEFAULT_SERVER_URL = "http://127.0.0.1:8010";
const POLL_ALARM = "poll";
const PRETOKEN_ALARM = "pretoken";
const COOKIE_ALARM = "cookies";
const WAKE_ALARM = "wake";
const RECAPTCHA_SITE_KEY = "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0";
const RECAPTCHA_ACTION = "chat_submit";

let SERVER_URL = DEFAULT_SERVER_URL;
let connected = false;
let lastError = "";
let tokenCount = 0;
let cookieRefreshCount = 0;
let isGenerating = false;

function normalizeServerUrl(value) {
  try {
    const url = new URL(value || DEFAULT_SERVER_URL);
    const localHost = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    if (url.protocol !== "http:" || !localHost) return DEFAULT_SERVER_URL;
    return url.href.replace(/\/$/, "");
  } catch (_) {
    return DEFAULT_SERVER_URL;
  }
}

function setBadge(state) {
  const api = chrome.action || chrome.browserAction;
  if (!api) return;
  const text = state === "connected" ? "ON" : state === "disconnected" ? "OFF" : "";
  api.setBadgeText({ text });
  api.setBadgeBackgroundColor({ color: state === "connected" ? "#16803c" : "#a51d2d" });
}

function markConnected(value, error = "") {
  connected = value;
  lastError = error;
  setBadge(value ? "connected" : "disconnected");
}

async function pollOnce() {
  try {
    const response = await fetch(`${SERVER_URL}/admin/poll`, {
      method: "GET",
      cache: "no-store",
      headers: { "Accept": "application/json" }
    });
    if (!response.ok) throw new Error(`server returned ${response.status}`);
    markConnected(true);
    const data = await response.json();
    if (data.need_token && data.id && !isGenerating) void handleTokenRequest(data.id);
  } catch (error) {
    markConnected(false, error?.message || String(error));
  }
}

function queryArenaTabs() {
  return new Promise((resolve) => chrome.tabs.query({ url: "https://arena.ai/*" }, resolve));
}

function getCookie(url, name) {
  return new Promise((resolve) => chrome.cookies.get({ url, name }, resolve));
}

async function extractArenaCookies() {
  const names = ["arena-auth-prod-v1.0", "arena-auth-prod-v1.1", "cf_clearance", "__cf_bm", "user_country_code"];
  const cookies = {};
  for (const name of names) {
    const cookie = await getCookie("https://arena.ai", name);
    if (cookie?.value) cookies[name] = cookie.value;
  }
  if (!cookies["arena-auth-prod-v1.0"]) throw new Error("Arena login cookie is not available");
  return cookies;
}

async function submitCookies() {
  try {
    const cookies = await extractArenaCookies();
    const response = await fetch(`${SERVER_URL}/admin/cookies/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookies })
    });
    if (!response.ok) throw new Error(`cookie submit returned ${response.status}`);
    cookieRefreshCount += 1;
  } catch (error) {
    lastError = error?.message || String(error);
  }
}

async function generateTokenInArenaTab() {
  const tabs = await queryArenaTabs();
  if (!tabs?.length) throw new Error("Open arena.ai in the Desktop Chrome profile");
  const result = await chrome.scripting.executeScript({
    target: { tabId: tabs[0].id },
    world: "MAIN",
    func: async (siteKey, action) => {
      const start = Date.now();
      while (typeof grecaptcha === "undefined" || !grecaptcha.enterprise) {
        if (Date.now() - start > 10000) return { error: "grecaptcha is not ready" };
        await new Promise((resolve) => setTimeout(resolve, 200));
      }
      try {
        const token = await new Promise((resolve, reject) => {
          grecaptcha.enterprise.ready(async () => {
            try { resolve(await grecaptcha.enterprise.execute(siteKey, { action })); }
            catch (error) { reject(error); }
          });
        });
        return token && token.length >= 50 ? { token } : { error: "invalid token" };
      } catch (error) {
        return { error: error?.message || String(error) };
      }
    },
    args: [RECAPTCHA_SITE_KEY, RECAPTCHA_ACTION]
  });
  const value = result?.[0]?.result?.result;
  if (!value?.token) throw new Error(value?.error || "token generation failed");
  return value.token;
}

async function postToken(id, token, ok, error, pre = false) {
  await fetch(`${SERVER_URL}/admin/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, token, ok, error, pre })
  });
}

async function handleTokenRequest(id) {
  isGenerating = true;
  try {
    const token = await Promise.race([
      generateTokenInArenaTab(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("token timeout")), 15000))
    ]);
    tokenCount += 1;
    await postToken(id, token, true, "", false);
  } catch (error) {
    await postToken(id, null, false, error?.message || String(error), false);
  } finally {
    isGenerating = false;
  }
}

async function generatePreToken() {
  if (isGenerating) return;
  isGenerating = true;
  try {
    const token = await generateTokenInArenaTab();
    tokenCount += 1;
    await postToken(`pre_${Date.now()}`, token, true, "", true);
  } catch (_) {
    // A missing tab or a temporarily unavailable grecaptcha is retried by the next alarm.
  } finally {
    isGenerating = false;
  }
}

function start() {
  chrome.storage.local.get(["serverUrl"], (result) => {
    SERVER_URL = normalizeServerUrl(result.serverUrl || DEFAULT_SERVER_URL);
    void pollOnce();
    void submitCookies();
    void generatePreToken();
  });
  chrome.alarms.create(WAKE_ALARM, { periodInMinutes: 0.5 });
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
  chrome.alarms.create(PRETOKEN_ALARM, { periodInMinutes: 1 });
  chrome.alarms.create(COOKIE_ALARM, { periodInMinutes: 5 });
}

chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === WAKE_ALARM || alarm.name === POLL_ALARM) void pollOnce();
  if (alarm.name === PRETOKEN_ALARM) void generatePreToken();
  if (alarm.name === COOKIE_ALARM) void submitCookies();
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "get_status") {
    sendResponse({ connected, serverUrl: SERVER_URL, lastError, tokenCount, cookieRefreshCount });
    return true;
  }
  if (message.type === "set_server_url") {
    SERVER_URL = normalizeServerUrl(message.serverUrl);
    chrome.storage.local.set({ serverUrl: SERVER_URL }, () => {
      markConnected(false, "reconnecting");
      void pollOnce();
      sendResponse({ ok: true, serverUrl: SERVER_URL });
    });
    return true;
  }
  if (message.type === "force_reconnect") {
    markConnected(false, "reconnecting");
    void pollOnce();
    sendResponse({ ok: true });
    return true;
  }
  if (message.type === "test_cookies") {
    extractArenaCookies().then((cookies) => sendResponse({ ok: true, cookies })).catch((error) => sendResponse({ ok: false, error: error?.message || String(error) }));
    return true;
  }
  return false;
});

start();
