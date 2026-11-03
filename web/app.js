/* 护苗 · 家庭网关控制台 —— 无框架，SSE 实时更新 */

const $ = (id) => document.getElementById(id);

const loginLayer = $("login");
const loginForm = $("loginForm");
const tokenInput = $("tokenInput");
const loginHint = $("loginHint");
const live = $("live");
const liveText = $("liveText");
const deviceList = $("deviceList");
const deviceCount = $("deviceCount");
const modeTag = $("modeTag");
const cmdForm = $("cmdForm");
const targetInput = $("target");
const blockBtn = $("blockBtn");
const unblockBtn = $("unblockBtn");
const blockedList = $("blockedList");
const blockedCount = $("blockedCount");
const blockedEmpty = $("blockedEmpty");
const historyList = $("history");
const historyEmpty = $("historyEmpty");
const logoutBtn = $("logout");
const toastEl = $("toast");

let token = "";
let clientId = "";
let devices = [];
let history = [];
let selected = null;
let busy = false;
let es = null;
let toastTimer = null;
const pending = {};

/* ---------- 小工具 ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function currentDevice() {
  return devices.find((d) => d.id === selected) || null;
}

function setLive(state) {
  live.dataset.state = state;
  liveText.textContent =
    state === "live" ? "实时连接" : state === "down" ? "已断开" : "连接中";
}

function toast(text, good = true) {
  toastEl.textContent = text;
  toastEl.classList.toggle("bad", good === false);
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 3600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "X-Auth-Token": token,
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) {
    const err = new Error("unauthorized");
    err.status = 401;
    throw err;
  }
  const data = await res.json().catch(() => ({ ok: false, msg: "响应格式错误" }));
  data.httpStatus = res.status;
  return data;
}

function makeClientId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().slice(0, 8);
  return Math.random().toString(36).slice(2, 10);
}

/* ---------- 渲染 ---------- */

function renderAll() {
  renderDevices();
  renderMode();
  renderBlocked();
  renderHistory();
  syncButtons();
}

function renderDevices() {
  deviceList.replaceChildren();
  deviceCount.textContent = `${devices.length} 台在线`;

  if (!devices.length) {
    deviceList.append(el("div", "device-empty", "暂无在线网关，请先启动 gateway.py"));
    return;
  }

  devices.forEach((device) => {
    const btn = el("button", "device");
    btn.type = "button";
    btn.setAttribute("aria-pressed", String(device.id === selected));
    btn.append(el("span", "device-dot"));
    btn.append(el("span", "device-id", device.id));
    const count = (device.blocked || []).length;
    btn.append(el("span", "device-sub", count ? `${count} 项管控` : "未管控"));
    btn.addEventListener("click", () => {
      selected = device.id;
      localStorage.setItem("hp_device", selected);
      renderAll();
    });
    deviceList.append(btn);
  });
}

function renderMode() {
  const device = currentDevice();
  if (!device || !device.mode) {
    modeTag.hidden = true;
    return;
  }
  const label = device.mode === "iptables" ? "真实 iptables" : "模拟模式";
  modeTag.textContent = label;
  modeTag.hidden = false;
}

function renderBlocked() {
  blockedList.replaceChildren();
  const device = currentDevice();
  const items = device ? device.blocked || [] : [];
  blockedCount.textContent = `${items.length} 项`;
  blockedEmpty.hidden = items.length > 0;

  items.forEach((site) => {
    const li = el("li");
    li.append(el("span", "site", site));
    const lift = el("button", "lift", "解除");
    lift.type = "button";
    lift.disabled = busy;
    lift.addEventListener("click", () => sendCommand("unblock", site));
    li.append(lift);
    blockedList.append(li);
  });
}

function renderHistory() {
  historyList.replaceChildren();
  const rows = history.slice(-30).reverse();
  historyEmpty.hidden = rows.length > 0;

  rows.forEach((row) => {
    const li = el("li");
    li.append(el("span", "h-time", row.time || ""));

    const main = el("div", "h-main");
    const isBlock = row.action === "block";
    main.append(el("span", `badge ${isBlock ? "block" : "unblock"}`, isBlock ? "阻断" : "解除"));
    main.append(el("span", "h-target", row.target || "-"));
    main.append(el("span", "h-device", row.device_id || ""));
    li.append(main);

    const msg = row.msg || (row.ok ? "执行成功" : "执行失败");
    li.append(el("div", "h-msg", msg));
    historyList.append(li);
  });
}

function syncButtons() {
  blockBtn.disabled = busy || !currentDevice();
  unblockBtn.disabled = busy || !currentDevice();
  blockBtn.textContent = busy ? "等待网关回执…" : "立即阻断";
}

/* ---------- 数据加载 ---------- */

function applyStatus(data) {
  devices = Array.isArray(data.devices) ? data.devices : [];
  history = Array.isArray(data.history) ? data.history : [];

  const saved = localStorage.getItem("hp_device");
  const stillThere = devices.some((d) => d.id === selected);
  if (!stillThere) {
    selected = devices.some((d) => d.id === saved)
      ? saved
      : devices.length
        ? devices[0].id
        : null;
  }
  renderAll();
}

async function loadStatus() {
  const data = await api("/api/status");
  applyStatus(data);
}

/* ---------- 命令 ---------- */

async function sendCommand(action, rawTarget) {
  if (busy) return;
  const target = String(rawTarget || "").trim();
  if (!target) {
    toast("请输入要管控的网址或 IP", false);
    return;
  }
  if (!currentDevice()) {
    toast("请先选择在线网关", false);
    return;
  }

  busy = true;
  syncButtons();
  try {
    const data = await api("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_id: selected,
        action,
        target,
        client_id: clientId,
      }),
    });

    if (!data.ok) {
      busy = false;
      syncButtons();
      toast(data.msg || "下发失败", false);
      return;
    }

    toast(`已下发${action === "block" ? "阻断" : "解除"}「${target}」，等待网关回执…`);
    const reqId = data.req_id;
    pending[reqId] = setTimeout(() => {
      delete pending[reqId];
      busy = false;
      syncButtons();
      toast("等待网关回执超时，请检查网关在线状态", false);
    }, 16000);
  } catch (err) {
    busy = false;
    syncButtons();
    if (err.status === 401) {
      logout();
      return;
    }
    toast("网络异常，无法连接中转服务器", false);
  }
}

/* ---------- SSE 实时通道 ---------- */

function handleMessage(msg) {
  if (msg.type === "hello" || msg.type === "status") {
    applyStatus(msg.status || msg);
    return;
  }

  if (msg.type === "state") {
    const payload = msg.payload || {};
    const device = devices.find((d) => d.id === msg.device_id);
    if (device) {
      device.blocked = Array.isArray(payload.blocked) ? payload.blocked : [];
      device.mode = payload.mode || device.mode;
      renderAll();
    } else {
      loadStatus().catch(() => {});
    }
    return;
  }

  if (msg.type === "event") {
    const online = msg.event === "gateway_online";
    toast(`网关 ${msg.device_id} ${online ? "已上线" : "已离线"}`, online);
    loadStatus().catch(() => {});
    return;
  }

  if (msg.type === "result") {
    const payload = msg.payload || {};
    if (msg.req_id && pending[msg.req_id]) {
      clearTimeout(pending[msg.req_id]);
      delete pending[msg.req_id];
    }
    busy = false;
    syncButtons();
    toast(payload.ok ? `执行成功：${payload.msg}` : `执行失败：${payload.msg}`, !!payload.ok);
    loadStatus().catch(() => {});
  }
}

function connectSSE() {
  if (es) es.close();
  const url = `/api/events?token=${encodeURIComponent(token)}&client_id=${encodeURIComponent(clientId)}`;
  es = new EventSource(url);

  es.onopen = () => setLive("live");
  es.onerror = () => setLive("down");
  es.onmessage = (ev) => {
    try {
      handleMessage(JSON.parse(ev.data));
    } catch (err) {
      /* 忽略坏消息 */
    }
  };
}

/* ---------- 登录与启动 ---------- */

function showLogin(message) {
  loginLayer.hidden = false;
  loginHint.classList.remove("bad");
  loginHint.textContent =
    message || "默认令牌 parent123，可用 WEB_TOKEN 环境变量修改。";
  if (message) loginHint.classList.add("bad");
  setTimeout(() => tokenInput.focus(), 30);
}

function hideLogin() {
  loginLayer.hidden = true;
}

function logout() {
  token = "";
  localStorage.removeItem("hp_token");
  if (es) {
    es.close();
    es = null;
  }
  Object.values(pending).forEach(clearTimeout);
  Object.keys(pending).forEach((key) => delete pending[key]);
  devices = [];
  history = [];
  selected = null;
  busy = false;
  setLive("connecting");
  renderAll();
  showLogin("已退出登录。");
}

async function boot() {
  clientId = localStorage.getItem("hp_client") || makeClientId();
  localStorage.setItem("hp_client", clientId);
  token = localStorage.getItem("hp_token") || "";

  if (!token) {
    renderAll();
    showLogin();
    return;
  }

  try {
    await loadStatus();
    hideLogin();
    connectSSE();
  } catch (err) {
    if (err.status === 401) {
      localStorage.removeItem("hp_token");
      token = "";
      showLogin("令牌已失效，请重新输入。");
    } else {
      renderAll();
      showLogin("无法连接中转服务器，请确认 relay.py 已启动。");
    }
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = tokenInput.value.trim();
  if (!value) return;

  token = value;
  try {
    await loadStatus();
    localStorage.setItem("hp_token", token);
    tokenInput.value = "";
    hideLogin();
    connectSSE();
  } catch (err) {
    token = "";
    if (err.status === 401) {
      showLogin("令牌不正确，请重试。");
    } else {
      showLogin("无法连接中转服务器，请确认 relay.py 已启动。");
    }
  }
});

cmdForm.addEventListener("submit", (event) => {
  event.preventDefault();
  sendCommand("block", targetInput.value);
});

unblockBtn.addEventListener("click", () => sendCommand("unblock", targetInput.value));

logoutBtn.addEventListener("click", logout);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && token) loadStatus().catch(() => {});
});

renderAll();
boot();
