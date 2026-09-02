// LogVault console (v2.3.4)
// A tab based operations console for the plugin Web API in core/web_api.py.
// Layout rules: every flex/grid child sets min-width:0 in style.css, so long
// paths, plugin names and warnings truncate instead of stretching the page.
//
// Sandbox rules: the dashboard mounts this page in an iframe sandboxed with
// "allow-scripts allow-forms allow-downloads" only.  Without allow-modals the
// native confirm()/alert()/prompt() dialogs are suppressed and return false,
// and without allow-same-origin the origin is opaque so localStorage throws.
// Therefore:
//   * never call a native dialog -> await askConfirm() renders one in-page,
//   * never trust localStorage alone -> readStore/writeStore mirror the value
//     in memory and persist it through the prefs / prefs_save endpoints,
//   * navigator.clipboard can reject -> copyStream falls back to execCommand.

const bridge = window.AstrBotPluginPage;

const SKINS = [
  { id: "auto", label: "跟随 Dashboard" },
  { id: "console", label: "深空控制台" },
  { id: "daylight", label: "明昼" },
  { id: "glass", label: "玻璃荧光" },
  { id: "synthwave", label: "赛博霓虹" },
  { id: "matrix", label: "终端绿" },
];
const SKIN_IDS = SKINS.map((item) => item.id);
const STORE_SKIN = "logvault.skin";
const STORE_DENSITY = "logvault.density";
const STORE_TAB = "logvault.tab";
const TABS = ["overview", "live", "files", "search", "export", "diag"];
const EXPORT_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

const LINE_RE =
  /^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[^\]]*\]\s*\[([A-Za-z]+)\s*\]\s*\[([^\]]*)\]\s*(?:\[([^\]]*)\])?\s*:?\s*([\s\S]*)$/;
const LEVEL_RE = /\[(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|SUCCESS|TRACE|D|I|W|E|C)\s*\]/;
const LEVEL_ALIASES = {
  D: "DEBUG",
  I: "INFO",
  W: "WARNING",
  WARN: "WARNING",
  E: "ERROR",
  C: "CRITICAL",
  TRACE: "DEBUG",
};
const LEVEL_RANK = { DEBUG: 10, INFO: 20, SUCCESS: 25, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const KIND_ORDER = { all: 0, builtin: 1, plugin: 2, other: 3 };
const KIND_LABELS = {
  all: "全部",
  builtin: "内置分类",
  plugin: "插件日志",
  other: "其他",
};
const SOURCE_LABELS = {
  current: "当前数据目录",
  legacy: "旧数据目录",
  host: "AstrBot 主日志目录",
};
const FOLLOW_INTERVAL_MS = 2000;
const MAX_STREAM_LINES = 2000;

const state = {
  tab: "overview",
  skin: "console",
  density: "compact",
  overview: null,
  capture: null,
  categories: [],
  files: [],
  category: null,
  selected: new Set(),
  sort: "mtime",
  allFiles: [],
  live: { id: "", position: 0, entries: [], timer: null, busy: false, dropped: 0 },
  combo: { options: [], visible: [], query: "", active: -1, open: false },
  view: { file: null, position: 0, entries: [], timer: null, busy: false },
  search: { keyword: "", results: [] },
  exporter: {
    levels: new Set(),
    plan: null,
    token: "",
    busy: false,
    history: [],
    historyLoaded: false,
  },
  toastTimer: null,
  confirm: { resolve: null, origin: null },
};

const el = (id) => document.getElementById(id);

// -- helpers ---------------------------------------------------------------

function t(key, fallback) {
  try {
    const value = bridge.t("pages.logs." + key, fallback);
    return value === undefined || value === null || value === "" ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function esc(value) {
  return String(value === undefined || value === null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function highlight(text, keyword) {
  const raw = String(text === undefined || text === null ? "" : text);
  const needle = String(keyword || "").toLowerCase();
  if (!needle) return esc(raw);
  const hay = raw.toLowerCase();
  let out = "";
  let from = 0;
  let at = hay.indexOf(needle, from);
  while (at !== -1) {
    out +=
      esc(raw.slice(from, at)) +
      "<mark>" +
      esc(raw.slice(at, at + needle.length)) +
      "</mark>";
    from = at + needle.length;
    at = hay.indexOf(needle, from);
  }
  return out + esc(raw.slice(from));
}

function formatSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return (unit === 0 ? size : size.toFixed(size >= 100 ? 0 : 1)) + " " + units[unit];
}

function formatTime(epochSeconds) {
  const value = Number(epochSeconds);
  if (!Number.isFinite(value) || value <= 0) return "-";
  try {
    return new Date(value * 1000).toLocaleString();
  } catch (err) {
    return "-";
  }
}

function debounce(fn, wait) {
  let timer = null;
  return function debounced() {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(fn, wait);
  };
}

// -- preferences -----------------------------------------------------------
// The plugin page runs in a sandboxed iframe without allow-same-origin, so its
// origin is opaque and window.localStorage raises SecurityError on both read
// and write.  Preferences are therefore held in memory for the session,
// mirrored into localStorage when it happens to work (standalone preview), and
// persisted server side through the prefs / prefs_save endpoints.

const memoryStore = new Map();
const PREF_KEYS = { skin: STORE_SKIN, density: STORE_DENSITY, tab: STORE_TAB };
let prefSyncTimer = null;
let prefSynced = "";

function readStore(key, fallback) {
  const cached = memoryStore.get(key);
  if (cached !== undefined && cached !== "") return cached;
  try {
    const value = window.localStorage.getItem(key);
    if (value !== null && value !== "") {
      memoryStore.set(key, value);
      return value;
    }
  } catch (err) {
    /* opaque origin: the in-memory copy is the only local source */
  }
  return fallback;
}

function writeStore(key, value) {
  memoryStore.set(key, String(value));
  try {
    window.localStorage.setItem(key, String(value));
  } catch (err) {
    /* opaque origin: the server side copy below is the only persistence */
  }
  schedulePrefSync();
}

function prefPayload() {
  const payload = {};
  for (const name of Object.keys(PREF_KEYS)) {
    const value = memoryStore.get(PREF_KEYS[name]);
    if (value) payload[name] = value;
  }
  return payload;
}

// Skin, density and tab changes arrive in bursts (a click can touch two of
// them), so the write is debounced and skipped when nothing actually moved.
function schedulePrefSync() {
  if (prefSyncTimer) window.clearTimeout(prefSyncTimer);
  prefSyncTimer = window.setTimeout(() => {
    prefSyncTimer = null;
    const payload = prefPayload();
    const fingerprint = JSON.stringify(payload);
    if (fingerprint === prefSynced) return;
    prefSynced = fingerprint;
    apiPost("prefs_save", payload).catch(() => {
      // Older installs have no such endpoint; retry on the next change.
      prefSynced = "";
    });
  }, 400);
}

async function loadPrefs() {
  let payload = null;
  try {
    payload = await apiGet("prefs");
  } catch (err) {
    return;
  }
  const prefs = payload && typeof payload.prefs === "object" ? payload.prefs : null;
  if (!prefs) return;
  for (const name of Object.keys(PREF_KEYS)) {
    const value = prefs[name];
    if (typeof value === "string" && value !== "") {
      memoryStore.set(PREF_KEYS[name], value);
    }
  }
  prefSynced = JSON.stringify(prefPayload());
}

function toast(message, kind) {
  const node = el("toast");
  if (!node) return;
  node.textContent = String(message);
  node.dataset.kind = kind === "error" ? "err" : "ok";
  node.hidden = false;
  if (state.toastTimer) window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => {
    node.hidden = true;
  }, kind === "error" ? 6000 : 3000);
}

// -- confirm dialog --------------------------------------------------------
// Native confirm() never opens inside the dashboard sandbox (no allow-modals)
// and silently evaluates to false, which used to make destructive buttons look
// dead.  askConfirm() renders the same question in-page and resolves a boolean.

function askConfirm(options) {
  const layer = el("confirm-layer");
  const okButton = el("confirm-ok");
  if (!layer || !okButton) return Promise.resolve(false);
  const config = options || {};
  // A queued dialog would leak its promise, so resolve any stale one first.
  closeConfirm(false);
  el("confirm-title").textContent = config.title || t("confirm.title", "需要确认");
  el("confirm-text").textContent = config.message || "";
  const note = el("confirm-note");
  note.textContent = config.note || "";
  note.hidden = !config.note;
  okButton.textContent = config.confirmLabel || t("action.confirm", "确认");
  okButton.className = config.danger
    ? "lv-btn lv-btn-danger-solid"
    : "lv-btn lv-btn-primary";
  const origin = document.activeElement;
  state.confirm.origin =
    origin && typeof origin.focus === "function" ? origin : null;
  layer.hidden = false;
  // Focus has to move after the layer is painted, otherwise it is refused.
  window.setTimeout(() => okButton.focus(), 0);
  return new Promise((resolve) => {
    state.confirm.resolve = resolve;
  });
}

// Returns true when a dialog really was open, so the Escape handler can tell
// whether it consumed the key or should close the drawer instead.
function closeConfirm(answer) {
  const layer = el("confirm-layer");
  if (!layer || layer.hidden) return false;
  layer.hidden = true;
  const resolve = state.confirm.resolve;
  const origin = state.confirm.origin;
  state.confirm.resolve = null;
  state.confirm.origin = null;
  if (origin && document.contains(origin)) {
    try {
      origin.focus();
    } catch (err) {
      /* the trigger may already be disabled by the action it started */
    }
  }
  if (resolve) resolve(Boolean(answer));
  return true;
}

function unwrap(res) {
  if (res && typeof res === "object" && "data" in res) {
    if (res.status && res.status !== "ok") {
      throw new Error(res.message || t("error.request", "请求失败"));
    }
    return res.data === undefined || res.data === null ? res : res.data;
  }
  return res;
}

function errorText(err) {
  if (!err) return t("error.request", "请求失败");
  if (typeof err === "string") return err;
  return err.message || String(err);
}

async function apiGet(endpoint, params) {
  return unwrap(await bridge.apiGet(endpoint, params || {}));
}

async function apiPost(endpoint, body) {
  return unwrap(await bridge.apiPost(endpoint, body || {}));
}

function asList(payload, key) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload[key])) return payload[key];
  return [];
}

function sourceLabel(source, kind) {
  const known = SOURCE_LABELS[kind] || SOURCE_LABELS[source];
  if (!known) return String(source || "-");
  if (kind === "host" || kind === "legacy") return known + " (" + source + ")";
  return known;
}

function kindLabel(kind) {
  return t("kind." + kind, KIND_LABELS[kind] || kind || "-");
}

function lineLevel(line) {
  const match = LEVEL_RE.exec(String(line).slice(0, 160));
  if (!match) return "";
  const token = match[1].toUpperCase();
  return LEVEL_ALIASES[token] || token;
}

// Split one formatted record into its columns so the stream can render a
// terminal-like grid; unparsable lines fall back to a single raw cell.
function parseLine(line) {
  const raw = String(line === undefined || line === null ? "" : line);
  const match = LINE_RE.exec(raw);
  if (!match) {
    const level = lineLevel(raw);
    return { raw, level, time: "", tag: "", where: "", message: raw, parsed: false };
  }
  const token = String(match[2] || "").toUpperCase();
  return {
    raw,
    level: LEVEL_ALIASES[token] || token,
    time: match[1] || "",
    tag: match[3] || "",
    where: match[4] || "",
    message: match[5] || "",
    parsed: true,
  };
}

function toEntries(lines) {
  return (Array.isArray(lines) ? lines : []).map(parseLine);
}

// -- skin + density --------------------------------------------------------

function isDarkContext() {
  try {
    const context = bridge.getContext ? bridge.getContext() : null;
    if (context && typeof context.isDark === "boolean") return context.isDark;
  } catch (err) {
    /* fall through to the media query */
  }
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch (err) {
    return true;
  }
}

function applySkin() {
  const effective =
    state.skin === "auto" ? (isDarkContext() ? "console" : "daylight") : state.skin;
  document.documentElement.dataset.skin = effective;
  const select = el("skin-select");
  if (select && select.value !== state.skin) select.value = state.skin;
}

function setSkin(id) {
  state.skin = SKIN_IDS.indexOf(id) === -1 ? "console" : id;
  writeStore(STORE_SKIN, state.skin);
  applySkin();
}

function buildSkinOptions() {
  const select = el("skin-select");
  if (!select) return;
  select.innerHTML = SKINS.map(
    (item) =>
      "<option value=\"" + esc(item.id) + "\">" +
      esc(t("skin." + item.id, item.label)) +
      "</option>"
  ).join("");
  select.value = state.skin;
}

function applyDensity() {
  document.documentElement.dataset.density = state.density;
  const button = el("btn-density");
  if (button) {
    button.textContent =
      state.density === "compact"
        ? t("density.compact", "紧凑")
        : t("density.cozy", "宽松");
    button.title = t("density.hint", "切换行高");
  }
}

function toggleDensity() {
  state.density = state.density === "compact" ? "cozy" : "compact";
  writeStore(STORE_DENSITY, state.density);
  applyDensity();
}

// -- tabs ------------------------------------------------------------------

function setTab(name) {
  const tab = TABS.indexOf(name) === -1 ? "overview" : name;
  state.tab = tab;
  writeStore(STORE_TAB, tab);
  for (const button of document.querySelectorAll(".lv-tab")) {
    const active = button.dataset.tab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".lv-panel")) {
    const active = panel.id === "panel-" + tab;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  }
  if (tab === "live") startLive();
  else stopLive();
  if (tab === "diag" && !state.capture) loadDiagnostics();
  if (tab === "export") {
    renderExportPanel();
    if (!state.exporter.historyLoaded) loadExportHistory();
  }
  updateStatusBar();
}

// -- shared stream renderer ------------------------------------------------

function levelPasses(entry, minimum) {
  if (!minimum) return true;
  const floor = LEVEL_RANK[minimum] || 0;
  const rank = LEVEL_RANK[entry.level] || 0;
  if (!rank) return floor <= 0;
  return rank >= floor;
}

function entryPasses(entry, filter) {
  if (!levelPasses(entry, filter.level)) return false;
  if (filter.tag && entry.tag !== filter.tag) return false;
  if (filter.keyword) {
    if (entry.raw.toLowerCase().indexOf(filter.keyword.toLowerCase()) === -1) return false;
  }
  return true;
}

function entryHtml(entry, keyword) {
  if (!entry.parsed) {
    return (
      '<div class="lv-line lv-line-raw"' +
      (entry.level ? ' data-level="' + esc(entry.level) + '"' : "") +
      '><span class="lv-msg">' +
      highlight(entry.raw, keyword) +
      "</span></div>"
    );
  }
  const where = entry.where
    ? '<span class="lv-src">' + esc(entry.where) + "</span>"
    : '<span class="lv-src"></span>';
  return (
    '<div class="lv-line" data-level="' + esc(entry.level || "") + '">' +
    '<span class="lv-time">' + esc(entry.time.slice(11) || entry.time) + "</span>" +
    '<span class="lv-lvl">' + esc(entry.level || "-") + "</span>" +
    '<span class="lv-tag" title="' + esc(entry.tag) + '">' + esc(entry.tag) + "</span>" +
    '<span class="lv-msg">' + highlight(entry.message, keyword) + where + "</span>" +
    "</div>"
  );
}

// Renders at most MAX_STREAM_LINES rows: a 200k line file must never freeze
// the dashboard tab.
function renderStream(node, entries, filter) {
  if (!node) return { shown: 0, total: 0, clipped: false };
  const total = entries.length;
  const matched = [];
  for (const entry of entries) {
    if (entryPasses(entry, filter)) matched.push(entry);
  }
  const clipped = matched.length > MAX_STREAM_LINES;
  const visible = clipped ? matched.slice(matched.length - MAX_STREAM_LINES) : matched;
  const atBottom = node.scrollTop + node.clientHeight >= node.scrollHeight - 24;
  node.innerHTML = visible.length
    ? visible.map((entry) => entryHtml(entry, filter.keyword)).join("")
    : '<p class="lv-empty">' + esc(t("stream.empty", "没有匹配的日志行")) + "</p>";
  if (filter.follow !== false && (atBottom || filter.jump)) node.scrollTop = node.scrollHeight;
  return { shown: visible.length, total, clipped, matched: matched.length };
}

function pushEntries(bucket, lines) {
  const added = toEntries(lines);
  if (!added.length) return 0;
  bucket.push.apply(bucket, added);
  // Keep a bounded ring buffer so a long follow session cannot grow forever.
  const overflow = bucket.length - MAX_STREAM_LINES * 3;
  if (overflow > 0) bucket.splice(0, overflow);
  return added.length;
}

// -- overview --------------------------------------------------------------

function metricHtml(label, value, sub, kind) {
  return (
    '<dl class="lv-metric"' + (kind ? ' data-kind="' + esc(kind) + '"' : "") + ">" +
    "<dt>" + esc(label) + "</dt>" +
    "<dd>" + esc(value) +
    (sub ? '<span class="lv-metric-sub">' + esc(sub) + "</span>" : "") +
    "</dd></dl>"
  );
}

function kvHtml(rows) {
  if (!rows.length) return '<p class="lv-empty">-</p>';
  return (
    '<dl class="lv-kv">' +
    rows
      .map((row) => "<dt>" + esc(row[0]) + "</dt><dd>" + esc(row[1]) + "</dd>")
      .join("") +
    "</dl>"
  );
}

function captureMode(capture) {
  const mode = (capture && capture.mode) || "unknown";
  const labels = {
    loguru: t("capture.loguru", "loguru 全量捕获"),
    logging: t("capture.logging", "logging 兼容模式"),
    pending: t("capture.pending", "初始化中"),
  };
  const healthy = mode === "loguru";
  return {
    mode,
    text: labels[mode] || mode,
    kind: healthy ? "ok" : mode === "logging" ? "warn" : "err",
  };
}

function renderMetrics() {
  const node = el("metrics");
  if (!node) return;
  const data = state.overview;
  if (!data) {
    node.innerHTML = "";
    return;
  }
  const capture = data.capture || {};
  const mode = captureMode(capture);
  const parts = [];
  parts.push(
    metricHtml(
      t("stats.files", "日志文件"),
      data.total_files || 0,
      t("stats.compressed", "已压缩") + " " + (data.compressed_count || 0)
    )
  );
  parts.push(
    metricHtml(
      t("stats.size", "占用空间"),
      (data.total_size_mb || 0) + " MB",
      data.data_dir || ""
    )
  );
  parts.push(
    metricHtml(
      t("stats.mode", "采集模式"),
      mode.text,
      t("stats.handlerLevel", "写入级别") + " " + (capture.handler_level || "-"),
      mode.kind
    )
  );
  if (mode.mode === "loguru") {
    parts.push(
      metricHtml(
        t("stats.forwarded", "已转发"),
        capture.forwarded || 0,
        t("stats.dropped", "丢弃") + " " + (capture.dropped || 0),
        capture.dropped ? "warn" : ""
      )
    );
  } else {
    parts.push(
      metricHtml(
        t("stats.attached", "已挂载 logger"),
        (capture.attached_loggers || []).length,
        t("stats.backfilled", "启动回填") + " " + (capture.backfilled || 0)
      )
    );
  }
  parts.push(
    metricHtml(
      t("stats.pluginLoggers", "插件日志器"),
      capture.plugin_loggers === undefined ? "-" : capture.plugin_loggers,
      t("stats.installed", "已安装插件") + " " + (capture.installed_plugins || 0)
    )
  );
  node.innerHTML = parts.join("");
}

function renderWarnings() {
  const node = el("warn-banner");
  if (!node) return;
  const data = state.overview || {};
  const capture = data.capture || {};
  const items = [];
  for (const warning of Array.isArray(capture.warnings) ? capture.warnings : []) {
    items.push(String(warning));
  }
  if (capture.dropped) {
    items.push(
      t("warn.dropped", "有日志被丢弃，通常是磁盘写入失败或过滤过严") +
        ": " + capture.dropped
    );
  }
  if (data.slice_by_record_time === false) {
    items.push(
      t(
        "warn.slice",
        "按记录时间切片已关闭：导出天数只按文件修改时间筛选，跨天的汇总日志会整体打包"
      )
    );
  }
  if (capture.mode && capture.mode !== "loguru" && capture.mode !== "logging") {
    items.push(t("warn.mode", "采集通路尚未就绪，请查看采集诊断页"));
  }
  if (!Array.isArray(capture.web_routes) || !capture.web_routes.length) {
    items.push(t("warn.routes", "WebUI 接口未注册，当前 AstrBot 版本可能过旧"));
  }
  if (!items.length) {
    node.hidden = true;
    node.innerHTML = "";
    return;
  }
  node.hidden = false;
  const collapsed = items.length > 3;
  node.classList.toggle("is-collapsed", collapsed);
  node.innerHTML =
    '<div class="lv-banner-head"><span class="lv-banner-title">' +
    esc(t("warn.title", "需要注意") + " (" + items.length + ")") +
    '</span><button type="button" class="lv-btn lv-btn-ghost lv-btn-mini" data-banner-toggle="1">' +
    esc(collapsed ? t("action.expand", "展开") : t("action.collapse", "收起")) +
    '</button></div><ul class="lv-banner-list">' +
    items.map((item) => "<li>" + esc(item) + "</li>").join("") +
    "</ul>";
}

function renderCaptureCard() {
  const body = el("capture-body");
  const pill = el("capture-mode");
  if (!body || !pill) return;
  const capture = (state.overview && state.overview.capture) || {};
  const mode = captureMode(capture);
  pill.textContent = mode.text;
  pill.dataset.kind = mode.kind;
  const rows = [
    [t("capture.handlerLevel", "写入级别"), capture.handler_level || "-"],
    [t("capture.sourceLevel", "astrbot 生效级别"), capture.astrbot_effective_level || "-"],
    [t("capture.forwarded", "已转发 / 丢弃"), (capture.forwarded || 0) + " / " + (capture.dropped || 0)],
    [t("capture.backfilled", "启动回填"), capture.backfilled || 0],
    [t("capture.attached", "已挂载 logger"), (capture.attached_loggers || []).length],
    [t("capture.routes", "WebUI 接口"), (capture.web_routes || []).length],
    [t("capture.version", "插件版本"), capture.version || "-"],
  ];
  body.innerHTML = kvHtml(rows);
}

function renderSourcesCard() {
  const body = el("sources-body");
  const pill = el("sources-count");
  if (!body || !pill) return;
  const sources = (state.overview && state.overview.sources) || [];
  pill.textContent = sources.length + " " + t("unit.source", "个来源");
  if (!sources.length) {
    body.innerHTML = '<p class="lv-empty">' + esc(t("sources.empty", "暂无可读来源")) + "</p>";
    return;
  }
  const stats = new Map();
  for (const item of state.categories) {
    if (item.key !== "__all__") continue;
    stats.set(item.source, item);
  }
  body.innerHTML =
    '<div class="lv-list">' +
    sources
      .map((item) => {
        const info = stats.get(item.label);
        const count = info ? info.count : 0;
        return (
          '<div class="lv-row"><div class="lv-row-main">' +
          '<div class="lv-row-title">' + esc(sourceLabel(item.label, item.kind)) + "</div>" +
          '<div class="lv-row-sub" title="' + esc(item.path) + '">' + esc(item.path) + "</div>" +
          '</div><span class="lv-row-num">' +
          esc(count + " " + t("unit.file", "个") + (info ? " · " + formatSize(info.size) : "")) +
          "</span></div>"
        );
      })
      .join("") +
    "</div>";
}

// Nothing to clean used to look exactly like a failed request: three zeros in a
// toast.  The card spells out what was scanned and which threshold held each
// group back, so "0" becomes an answer instead of a symptom.
function formatDuration(hours) {
  const value = Number(hours);
  if (!Number.isFinite(value) || value < 0) return "-";
  if (value < 1) {
    return Math.max(1, Math.round(value * 60)) + " " + t("unit.minute", "分钟");
  }
  if (value < 48) {
    const rounded = value < 10 ? Math.round(value * 10) / 10 : Math.round(value);
    return rounded + " " + t("unit.hour", "小时");
  }
  return Math.round(value / 24) + " " + t("unit.day", "天");
}

function thresholdText(limits, forced) {
  const parts = [];
  if (forced && limits.compression_after_days) {
    parts.push(t("clean.limitGzForced", "压缩：本次忽略延迟"));
  } else if (limits.compression_after_days) {
    parts.push(
      t("clean.limitGz", "压缩 >%s 天").replace("%s", String(limits.compression_after_days))
    );
  } else {
    parts.push(t("clean.limitGzOff", "压缩已关闭"));
  }
  if (limits.max_age_days) {
    parts.push(t("clean.limitAge", "删除 >%s 天").replace("%s", String(limits.max_age_days)));
  }
  if (limits.max_total_size_mb) {
    parts.push(
      t("clean.limitSize", "总量 >%s MB").replace("%s", String(limits.max_total_size_mb))
    );
  }
  if (!limits.max_age_days && !limits.max_total_size_mb) {
    parts.push(t("clean.limitCleanOff", "自动清理已关闭"));
  }
  return parts.join(" · ");
}

function renderCleanReport(result) {
  const card = el("clean-card");
  const body = el("clean-body");
  const pill = el("clean-when");
  if (!card || !body) return;
  const data = result || {};
  const skipped = data.skipped || {};
  const limits = data.thresholds || {};
  const acted = (data.compressed || 0) + (data.deleted || 0);
  const rows = [];
  rows.push([
    t("clean.acted", "本次动作"),
    t("toast.compressed", "压缩") + " " + (data.compressed || 0) + " · " +
      t("toast.deleted", "已删除") + " " + (data.deleted || 0) + " · " +
      t("toast.freed", "释放") + " " + formatSize(data.freed_bytes),
  ]);
  rows.push([
    t("clean.scanned", "扫描范围"),
    (data.scanned || 0) + " " + t("unit.file", "个") + " · " + formatSize(data.total_bytes),
  ]);
  rows.push([
    t("clean.skipped", "跳过"),
    t("clean.skipActive", "写入中") + " " + (skipped.active || 0) + " · " +
      t("clean.skipGz", "已归档") + " " + (skipped.already_compressed || 0) + " · " +
      t("clean.skipNew", "未到压缩时间") + " " + (skipped.too_new || 0),
  ]);
  rows.push([t("clean.limits", "生效阈值"), thresholdText(limits, data.forced)]);
  if (data.next_compress_in_hours !== null && data.next_compress_in_hours !== undefined) {
    rows.push([
      t("clean.next", "下一批压缩"),
      t("clean.nextIn", "约 %s 后").replace(
        "%s",
        formatDuration(data.next_compress_in_hours)
      ),
    ]);
  }
  if (data.oldest_age_hours !== null && data.oldest_age_hours !== undefined) {
    rows.push([
      t("clean.oldest", "最旧归档"),
      t("clean.oldestAge", "%s 前").replace("%s", formatDuration(data.oldest_age_hours)),
    ]);
  }
  if (data.exports_deleted) {
    rows.push([t("clean.exports", "导出包"), String(data.exports_deleted)]);
  }
  const note = acted
    ? ""
    : '<p class="lv-muted">' +
      esc(t("clean.noop", "所有文件都还在阈值内，无需压缩或删除；这属于正常状态。")) +
      "</p>";
  body.innerHTML = kvHtml(rows) + note;
  // Waiting a day for the first archive is the usual reason a manual run
  // looks idle, so offer the one action that is safe to take right now.
  const canForce = !data.forced && (skipped.too_new || 0) > 0;
  const actions = el("clean-actions");
  const force = el("btn-clean-deep");
  if (actions && force) {
    force.textContent = t("clean.forceNow", "立即压缩 %s 个轮换日志").replace(
      "%s",
      String(skipped.too_new || 0)
    );
    actions.hidden = !canForce;
  }
  if (pill) {
    pill.textContent = new Date().toLocaleTimeString();
    pill.dataset.kind = acted ? "ok" : "info";
  }
  card.hidden = false;
}

function renderDistCard() {
  const body = el("dist-body");
  const pill = el("dist-total");
  if (!body || !pill) return;
  const items = state.categories.filter(
    (item) => item.key !== "__all__" && (item.size || item.count)
  );
  const total = items.reduce((sum, item) => sum + (item.size || 0), 0);
  pill.textContent = formatSize(total);
  if (!items.length) {
    body.innerHTML = '<p class="lv-empty">' + esc(t("dist.empty", "暂无日志")) + "</p>";
    return;
  }
  const sorted = items.slice().sort((a, b) => (b.size || 0) - (a.size || 0));
  const top = sorted.slice(0, 12);
  const rest = sorted.slice(12);
  const bars = top.map((item) => {
    const ratio = total > 0 ? Math.max(2, Math.round(((item.size || 0) / total) * 100)) : 0;
    return (
      '<div class="lv-bar"><span class="lv-bar-name" title="' +
      esc(item.name + " · " + kindLabel(item.kind)) + '">' +
      esc(item.name) + '</span><span class="lv-bar-val">' +
      esc(formatSize(item.size) + " · " + (item.count || 0)) +
      '</span><span class="lv-bar-track"><i class="lv-bar-fill" style="width:' +
      ratio + '%"></i></span></div>'
    );
  });
  if (rest.length) {
    const restSize = rest.reduce((sum, item) => sum + (item.size || 0), 0);
    bars.push(
      '<div class="lv-bar"><span class="lv-bar-name">' +
        esc(t("dist.rest", "其他") + " (" + rest.length + ")") +
        '</span><span class="lv-bar-val">' + esc(formatSize(restSize)) +
        '</span><span class="lv-bar-track"><i class="lv-bar-fill" style="width:' +
        (total > 0 ? Math.round((restSize / total) * 100) : 0) + '%"></i></span></div>'
    );
  }
  body.innerHTML = '<div class="lv-bars">' + bars.join("") + "</div>";
}

// -- export tab ------------------------------------------------------------

// The datalist is shared by the builder and stays in sync with the tree.
function renderPluginOptions() {
  const list = el("plugin-options");
  if (!list) return;
  const names = Array.from(
    new Set(
      state.categories.filter((item) => item.kind === "plugin").map((item) => item.name)
    )
  ).sort();
  list.innerHTML = names
    .map((name) => '<option value="' + esc(name) + '"></option>')
    .join("");
}

function renderSliceNote() {
  const note = el("slice-note");
  if (!note) return;
  const slice = state.overview ? state.overview.slice_by_record_time !== false : true;
  note.textContent = slice
    ? t(
        "export.sliceOn",
        "按记录时间切片已开启：导出时会逐行截取时间窗内的记录，跨天的汇总日志也能精确裁剪。"
      )
    : t(
        "export.sliceOff",
        "按记录时间切片已关闭：只按文件修改时间筛选，命中的文件会整体导出。"
      );
}

function exportScope() {
  const select = el("export-scope");
  return select ? select.value : "preset";
}

// Any change to the form makes the parked pre-flight (and its one shot
// token) obsolete, so the preview is cleared instead of showing stale counts.
function invalidateExportPlan() {
  state.exporter.plan = null;
  state.exporter.token = "";
  renderExportPreview();
}

function renderExportLevels() {
  const box = el("export-levels");
  if (!box) return;
  box.innerHTML = EXPORT_LEVELS.map((level) => {
    const on = state.exporter.levels.has(level);
    return (
      '<button type="button" class="lv-chip' + (on ? " is-on" : "") +
      '" data-level="' + level + '" aria-pressed="' + (on ? "true" : "false") + '">' +
      esc(level) + "</button>"
    );
  }).join("");
}

function renderExportScopeNote() {
  const note = el("export-scope-note");
  const preset = el("export-preset-field");
  const plugin = el("export-plugin-field");
  const scope = exportScope();
  if (preset) preset.hidden = scope !== "preset";
  if (plugin) plugin.hidden = scope !== "plugin";
  if (!note) return;
  if (scope === "selection") {
    note.textContent =
      state.selected.size > 0
        ? t("export.noteSelection", "将导出日志文件页勾选的文件") +
          " (" + state.selected.size + ")"
        : t("export.noteSelectionEmpty", "日志文件页还没有勾选任何文件");
    return;
  }
  if (scope === "category") {
    note.textContent = state.category
      ? t("export.noteCategory", "将导出当前分类") + ": " + state.category.name
      : t("export.noteCategoryEmpty", "请先在日志文件页选择一个分类");
    return;
  }
  if (scope === "plugin") {
    note.textContent = t("export.notePlugin", "按插件名匹配其专属目录与共享日志中的相关记录");
    return;
  }
  note.textContent = t("export.notePreset", "预设范围覆盖全部已登记的日志来源");
}

function renderExportPreview() {
  const box = el("export-preview");
  const pill = el("export-pill");
  const plan = state.exporter.plan;
  if (pill) pill.textContent = plan ? plan.files + " " + t("unit.file", "个") : "";
  if (!box) return;
  if (!plan) {
    box.innerHTML =
      '<p class="lv-empty">' + esc(t("export.previewHint", "点击预检，先看看会导出什么")) + "</p>";
    return;
  }
  const rows = [
    [t("export.rowRange", "范围"), plan.title || "-"],
    [
      t("export.rowFiles", "文件"),
      plan.files + " " + t("unit.file", "个") + " · " + formatSize(plan.bytes),
    ],
    [
      t("export.rowTrimmed", "逐行裁剪"),
      plan.trimmed
        ? plan.trimmed + " " + t("unit.file", "个")
        : t("export.none", "无"),
    ],
    [
      t("export.rowFormat", "格式"),
      plan.format === "merged" ? t("export.formatMerged", "合并 TXT") : t("export.formatZip", "ZIP 压缩包"),
    ],
    [
      t("export.rowMask", "脱敏"),
      plan.mask
        ? t("export.maskOn", "已开启")
        : plan.masking_available === false
          ? t("export.maskUnavailable", "脱敏组件未启用")
          : t("export.maskOff", "已关闭"),
    ],
  ];
  const filters = [];
  if (plan.levels && plan.levels.length) filters.push(plan.levels.join(" / "));
  if (plan.keyword) filters.push(t("export.keyword", "关键词") + ": " + plan.keyword);
  rows.push([
    t("export.rowFilter", "内容过滤"),
    filters.length ? filters.join(" · ") : t("export.none", "无"),
  ]);
  let html = kvHtml(rows);
  const warnings = asList({ warnings: plan.warnings }, "warnings");
  if (warnings.length) {
    html +=
      '<ul class="lv-list lv-list-warn">' +
      warnings.map((item) => "<li>" + esc(item) + "</li>").join("") +
      "</ul>";
  }
  if (!plan.files) {
    html +=
      '<p class="lv-note">' +
      esc(t("export.emptyPlan", "当前条件没有命中任何文件，请放宽时间窗或过滤条件。")) +
      "</p>";
  }
  box.innerHTML = html;
}

function renderExportHistory() {
  const box = el("export-history");
  const pill = el("export-history-pill");
  const purge = el("btn-export-purge");
  const items = state.exporter.history;
  const total = items.reduce((sum, item) => sum + (item.size || 0), 0);
  if (pill) pill.textContent = items.length ? items.length + " · " + formatSize(total) : "";
  if (purge) purge.disabled = items.length === 0;
  if (!box) return;
  if (!items.length) {
    box.innerHTML =
      '<p class="lv-empty">' + esc(t("export.historyEmpty", "还没有生成过导出包")) + "</p>";
    return;
  }
  box.innerHTML =
    '<div class="lv-hlist">' +
    items
      .map((item) => {
        const label = item.format === "merged" ? "TXT" : "ZIP";
        return (
          '<div class="lv-hrow">' +
          '<span class="lv-hrow-main"><span class="lv-hrow-name" title="' +
          esc(item.name) + '">' + esc(item.name) + "</span>" +
          '<span class="lv-hrow-meta">' +
          '<span class="lv-badge" data-kind="info">' + label + "</span> " +
          esc(formatSize(item.size) + " · " + formatTime(item.mtime)) +
          "</span></span>" +
          '<span class="lv-hrow-act">' +
          '<button type="button" class="lv-btn lv-btn-ghost lv-btn-mini" data-grab="' +
          esc(item.name) + '">' + esc(t("action.download", "下载")) + "</button>" +
          '<button type="button" class="lv-btn lv-btn-ghost lv-btn-mini" data-drop="' +
          esc(item.name) + '">' + esc(t("action.delete2", "删除")) + "</button>" +
          "</span></div>"
        );
      })
      .join("") +
    "</div>";
}

function renderExportPanel() {
  renderPluginOptions();
  renderSliceNote();
  renderExportScopeNote();
  renderExportLevels();
  renderExportPreview();
  renderExportHistory();
  const run = el("btn-export-run");
  if (run) run.disabled = state.exporter.busy;
  const plan = el("btn-export-plan");
  if (plan) plan.disabled = state.exporter.busy;
}

function updateStatusBar() {
  const left = el("status-left");
  const right = el("status-right");
  const data = state.overview;
  if (left) {
    left.textContent = data
      ? (data.data_dir || "-") +
        " · " + (data.total_files || 0) + " " + t("unit.file", "个") +
        " · " + (data.total_size_mb || 0) + " MB"
      : t("status.loading", "正在加载...");
    left.title = left.textContent;
  }
  if (right) {
    const parts = [t("status.tab", "当前页") + ": " + t("tab." + state.tab, state.tab)];
    if (data && data.newest_file) parts.push(t("stats.newest", "最新") + ": " + data.newest_file);
    if (state.refreshedAt) parts.push(t("status.refreshed", "刷新于") + " " + state.refreshedAt);
    right.textContent = parts.join(" · ");
    right.title = right.textContent;
  }
}

function renderOverview() {
  renderMetrics();
  renderWarnings();
  renderCaptureCard();
  renderSourcesCard();
  renderDistCard();
  renderExportPanel();
  updateStatusBar();
}

// -- files tab -------------------------------------------------------------

function visibleCategories() {
  const needle = (el("category-filter").value || "").trim().toLowerCase();
  if (!needle) return state.categories;
  return state.categories.filter((item) => {
    const haystack =
      (item.name || "") + " " + (item.key || "") + " " + (item.source || "");
    return haystack.toLowerCase().indexOf(needle) !== -1;
  });
}

function nodeHtml(item) {
  const active =
    state.category &&
    state.category.source === item.source &&
    state.category.key === item.key;
  return (
    '<button type="button" role="treeitem" class="lv-node' +
    (active ? " is-active" : "") +
    '" aria-selected="' + (active ? "true" : "false") +
    '" data-source="' + esc(item.source) +
    '" data-key="' + esc(item.key) +
    '" data-name="' + esc(item.name) +
    '" title="' + esc(item.name + " · " + (item.count || 0) + " · " + formatSize(item.size)) +
    '"><span class="lv-node-name">' + esc(item.name) + "</span>" +
    '<span class="lv-node-count">' + esc(item.count || 0) + "</span></button>"
  );
}

// Two level grouping: data source first, then category kind, so plugin logs
// are always browsable as their own block instead of one long flat list.
function renderTree() {
  const node = el("tree");
  if (!node) return;
  const items = visibleCategories();
  if (!items.length) {
    node.innerHTML = '<p class="lv-empty">' + esc(t("tree.empty", "没有匹配的分类")) + "</p>";
    return;
  }
  const sources = [];
  const bySource = new Map();
  for (const item of items) {
    if (!bySource.has(item.source)) {
      bySource.set(item.source, []);
      sources.push(item.source);
    }
    bySource.get(item.source).push(item);
  }
  const html = [];
  for (const source of sources) {
    const entries = bySource.get(source);
    const kind = entries[0] ? entries[0].source_kind : "";
    const files = entries.reduce(
      (sum, item) => (item.key === "__all__" ? sum + (item.count || 0) : sum),
      0
    );
    html.push('<div class="lv-tree-group">');
    html.push(
      '<p class="lv-tree-label"><span class="lv-node-name" title="' +
        esc(sourceLabel(source, kind)) + '">' + esc(sourceLabel(source, kind)) +
        '</span><span class="lv-node-count">' + esc(files) + "</span></p>"
    );
    const kinds = ["all", "builtin", "plugin", "other"];
    for (const group of kinds) {
      const bucket = entries
        .filter((item) => item.kind === group)
        .sort((a, b) => String(a.name).localeCompare(String(b.name)));
      if (!bucket.length) continue;
      if (group !== "all") {
        html.push(
          '<p class="lv-tree-label"><span>' + esc(kindLabel(group)) +
            '</span><span class="lv-node-count">' + bucket.length + "</span></p>"
        );
      }
      for (const item of bucket) html.push(nodeHtml(item));
    }
    html.push("</div>");
  }
  node.innerHTML = html.join("");
}

function visibleFiles() {
  const needle = (el("file-filter").value || "").trim().toLowerCase();
  const items = needle
    ? state.files.filter(
        (item) =>
          ((item.relative || item.name || "") + " " + (item.category_name || ""))
            .toLowerCase()
            .indexOf(needle) !== -1
      )
    : state.files.slice();
  const sort = state.sort;
  items.sort((a, b) => {
    if (sort === "size") return (b.size || 0) - (a.size || 0);
    if (sort === "name") {
      return String(a.relative || a.name).localeCompare(String(b.relative || b.name));
    }
    return (b.mtime || 0) - (a.mtime || 0);
  });
  return items;
}

function fileRowHtml(item) {
  const opened = state.view.file && state.view.file.id === item.id;
  const tags = [];
  if (item.active) {
    tags.push('<span class="lv-badge" data-kind="ok">' + esc(t("tag.active", "写入中")) + "</span>");
  }
  if (item.compressed) tags.push('<span class="lv-badge" data-kind="info">gz</span>');
  const check = item.deletable
    ? '<input type="checkbox" class="lv-check" data-id="' + esc(item.id) + '"' +
      (state.selected.has(item.id) ? " checked" : "") +
      ' aria-label="' + esc(item.name) + '" />'
    : "";
  return (
    '<tr data-id="' + esc(item.id) + '" class="' + (opened ? "is-selected" : "") + '">' +
    '<td class="lv-col-check">' + check + "</td>" +
    '<td><div class="lv-cell-file"><div class="lv-cell-name">' +
    '<button type="button" class="lv-file-link" data-open="' + esc(item.id) +
    '" title="' + esc(item.relative || item.name) + '">' + esc(item.name) + "</button>" +
    tags.join("") + "</div>" +
    '<div class="lv-cell-path" title="' + esc(item.relative || "") + '">' +
    esc(item.relative || "") + "</div></div></td>" +
    '<td><div class="lv-cell-cat" title="' + esc(item.category_name || item.category || "") +
    '">' + esc(item.category_name || item.category || "-") + "</div></td>" +
    '<td class="lv-num">' + esc(formatSize(item.size)) + "</td>" +
    "<td>" + esc(formatTime(item.mtime)) + "</td>" +
    '<td class="lv-col-act"><div class="lv-acts">' +
    '<button type="button" class="lv-btn lv-btn-ghost lv-btn-mini" data-open="' +
    esc(item.id) + '">' + esc(t("action.view", "查看")) + "</button>" +
    '<button type="button" class="lv-btn lv-btn-ghost lv-btn-mini" data-download="' +
    esc(item.id) + '">' + esc(t("action.download", "下载")) + "</button>" +
    "</div></td></tr>"
  );
}

function renderFiles() {
  const body = el("file-rows");
  const empty = el("file-empty");
  if (!body || !empty) return;
  const items = visibleFiles();
  if (!items.length) {
    body.innerHTML = "";
    empty.hidden = false;
    empty.textContent = state.category
      ? t("files.empty", "该分类下没有日志文件")
      : t("files.pick", "请选择左侧分类");
  } else {
    empty.hidden = true;
    body.innerHTML = items.map(fileRowHtml).join("");
  }
  const deletable = items.filter((item) => item.deletable);
  const checkAll = el("check-all");
  if (checkAll) {
    checkAll.disabled = deletable.length === 0;
    checkAll.checked =
      deletable.length > 0 && deletable.every((item) => state.selected.has(item.id));
  }
  const remove = el("btn-delete");
  if (remove) {
    remove.disabled = state.selected.size === 0;
    remove.textContent =
      state.selected.size > 0
        ? t("action.delete", "删除所选") + " (" + state.selected.size + ")"
        : t("action.delete", "删除所选");
  }
  const share = el("btn-export-files");
  if (share) {
    share.disabled = state.selected.size === 0;
    share.textContent =
      state.selected.size > 0
        ? t("action.exportSelected", "导出所选") + " (" + state.selected.size + ")"
        : t("action.exportSelected", "导出所选");
  }
  if (state.tab === "export") renderExportScopeNote();
  const scope = el("file-scope");
  if (scope) {
    const text = state.category
      ? state.category.name + " · " + items.length + " " + t("unit.file", "个")
      : t("files.none", "未选择分类");
    scope.textContent = text;
    scope.title = text;
  }
}

// -- drawer viewer ---------------------------------------------------------

function viewFilter(jump) {
  return {
    level: "",
    tag: "",
    keyword: el("view-keyword").value.trim(),
    follow: el("view-follow").checked,
    jump: Boolean(jump),
  };
}

function renderView(jump) {
  const result = renderStream(el("log-view"), state.view.entries, viewFilter(jump));
  const file = state.view.file;
  const meta = [];
  if (file) {
    meta.push(t("meta.shown", "显示") + " " + result.shown + " / " + result.total);
    if (result.clipped) meta.push(t("meta.clipped", "仅渲染最后 2000 行"));
    meta.push(t("meta.matched", "命中行") + ": " + (file.matched || 0));
    meta.push(t("meta.scanned", "扫描行") + ": " + (file.scanned || 0));
    meta.push(t("meta.size", "大小") + ": " + formatSize(file.size));
    if (file.truncated) meta.push(t("meta.truncated", "已达扫描上限，仅显示部分内容"));
    if (file.compressed) meta.push(t("meta.compressed", "压缩文件不支持实时跟随"));
    if (file.error) meta.push(file.error);
  }
  el("view-meta").textContent = meta.join(" · ");
}

function closeDrawer() {
  stopViewFollow();
  const drawer = el("drawer");
  if (!drawer) return;
  drawer.hidden = true;
  drawer.setAttribute("aria-hidden", "true");
  state.view.file = null;
  state.view.entries = [];
  renderFiles();
}

async function openDrawer(fileId) {
  const drawer = el("drawer");
  if (!drawer) return;
  drawer.hidden = false;
  drawer.setAttribute("aria-hidden", "false");
  await loadContent(fileId);
}

async function loadContent(fileId) {
  const id = fileId || (state.view.file && state.view.file.id);
  if (!id) return;
  stopViewFollow(true);
  try {
    const data = await apiGet("content", {
      id,
      tail: Number(el("view-tail").value) || 500,
      level: el("view-level").value || "",
      keyword: "",
    });
    state.view.file = data;
    state.view.position = Number(data.position) || 0;
    state.view.entries = toEntries(data.lines);
    el("drawer-title").textContent = data.name || "-";
    const sub = (data.source ? sourceLabel(data.source, "") + " · " : "") + formatTime(data.mtime);
    el("drawer-sub").textContent = sub;
    el("drawer-sub").title = sub;
    const follow = el("view-follow");
    follow.disabled = Boolean(data.compressed);
    if (data.compressed) follow.checked = false;
    renderView(true);
    renderFiles();
    if (follow.checked) startViewFollow();
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function pollView() {
  const file = state.view.file;
  if (!file || state.view.busy) return;
  state.view.busy = true;
  try {
    const data = await apiGet("tail", { id: file.id, position: state.view.position });
    if (!data || data.supported === false) {
      stopViewFollow();
      toast(t("toast.followUnsupported", "该文件不支持实时跟随"), "error");
      return;
    }
    state.view.position = Number(data.position) || 0;
    if (data.reset) {
      state.view.entries = toEntries(data.lines);
      toast(t("toast.rotated", "日志已轮换，视图已重置"));
      renderView(true);
    } else if (pushEntries(state.view.entries, data.lines)) {
      renderView(false);
    }
  } catch (err) {
    stopViewFollow();
    toast(errorText(err), "error");
  } finally {
    state.view.busy = false;
  }
}

function startViewFollow() {
  stopViewFollow(true);
  const file = state.view.file;
  if (!file || file.compressed) return;
  state.view.timer = window.setInterval(pollView, FOLLOW_INTERVAL_MS);
}

function stopViewFollow(keepCheckbox) {
  if (state.view.timer) {
    window.clearInterval(state.view.timer);
    state.view.timer = null;
  }
  if (!keepCheckbox) {
    const box = el("view-follow");
    if (box) box.checked = false;
  }
}

// -- live tab --------------------------------------------------------------

function liveFilter(jump) {
  return {
    level: el("live-level").value,
    tag: el("live-tag").value,
    keyword: el("live-keyword").value.trim(),
    follow: el("live-follow").checked,
    jump: Boolean(jump),
  };
}

function renderLiveTags() {
  const select = el("live-tag");
  if (!select) return;
  const current = select.value;
  const tags = new Set();
  for (const entry of state.live.entries) {
    if (entry.tag) tags.add(entry.tag);
  }
  const options = [
    '<option value="">' + esc(t("live.allTags", "全部来源")) + "</option>",
  ];
  for (const tag of Array.from(tags).sort()) {
    options.push('<option value="' + esc(tag) + '">' + esc(tag) + "</option>");
  }
  select.innerHTML = options.join("");
  select.value = tags.has(current) ? current : "";
}

function renderLive(jump) {
  const result = renderStream(el("live-stream"), state.live.entries, liveFilter(jump));
  const meta = el("live-meta");
  if (meta) {
    meta.textContent =
      t("live.shown", "当前展示") + " " + result.shown + " / " + result.total + " " +
      t("unit.line", "行") +
      (result.clipped ? " · " + t("meta.clipped", "仅渲染最后 2000 行") : "");
  }
  const hint = el("live-hint");
  if (hint) {
    hint.textContent = state.live.timer
      ? t("live.polling", "每 2 秒增量拉取")
      : t("live.paused", "已暂停");
  }
}

// The follow-file picker is a searchable combobox on top of a hidden native
// <select>. The select stays the single source of truth so every existing
// reader (loadLive, startLive) and its "change" event keep working unchanged.
function comboLabel(item) {
  return (item.relative || item.name || "") + " · " + formatSize(item.size);
}

function comboHaystack(item) {
  return (
    (item.relative || "") +
    " " +
    (item.name || "") +
    " " +
    (item.category_name || "") +
    " " +
    sourceLabel(item.source, item.source_kind)
  ).toLowerCase();
}

function comboTokens() {
  return state.combo.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

function comboFiltered() {
  const tokens = comboTokens();
  if (!tokens.length) return state.combo.options.slice();
  return state.combo.options.filter((item) => {
    const hay = comboHaystack(item);
    return tokens.every((token) => hay.indexOf(token) !== -1);
  });
}

function comboOptionHtml(item, index, needle) {
  const classes = ["lv-combo-option"];
  if (index === state.combo.active) classes.push("is-active");
  if (item.id === state.live.id) classes.push("is-current");
  return (
    '<div class="' + classes.join(" ") + '" role="option" data-index="' + index +
    '" data-id="' + esc(item.id) + '" aria-selected="' + (item.id === state.live.id) + '">' +
    '<span class="lv-combo-name">' + highlight(item.relative || item.name, needle) + "</span>" +
    '<span class="lv-combo-size">' + esc(formatSize(item.size)) + "</span>" +
    "</div>"
  );
}

function renderCombo() {
  const box = el("live-file-list");
  if (!box) return;
  const items = comboFiltered();
  state.combo.visible = items;
  if (!items.length) {
    state.combo.active = -1;
    box.innerHTML =
      '<div class="lv-combo-empty">' + esc(t("live.noMatch", "没有匹配的文件")) + "</div>";
    return;
  }
  if (state.combo.active >= items.length) state.combo.active = items.length - 1;
  if (state.combo.active < 0) {
    const at = items.findIndex((item) => item.id === state.live.id);
    state.combo.active = at === -1 ? 0 : at;
  }
  const needle = comboTokens()[0] || "";
  const html = [];
  let group = null;
  items.forEach((item, index) => {
    if (item.source !== group) {
      group = item.source;
      html.push(
        '<div class="lv-combo-group">' +
          esc(sourceLabel(item.source, item.source_kind)) +
          "</div>"
      );
    }
    html.push(comboOptionHtml(item, index, needle));
  });
  box.innerHTML = html.join("");
  const active = box.querySelector(".lv-combo-option.is-active");
  if (active && typeof active.scrollIntoView === "function") {
    active.scrollIntoView({ block: "nearest" });
  }
}

function comboSync() {
  const input = el("live-file-input");
  if (!input) return;
  const current = state.combo.options.find((item) => item.id === state.live.id);
  input.value = current ? comboLabel(current) : "";
  input.title = current ? current.relative || current.name || "" : "";
}

function openCombo() {
  const box = el("live-file-list");
  const wrap = el("live-file-combo");
  const input = el("live-file-input");
  if (!box || !wrap || !input || !state.combo.options.length) return;
  state.combo.open = true;
  state.combo.query = "";
  state.combo.active = -1;
  box.hidden = false;
  wrap.classList.add("is-open");
  input.setAttribute("aria-expanded", "true");
  if (input.value) input.select();
  renderCombo();
}

function closeCombo() {
  const box = el("live-file-list");
  const wrap = el("live-file-combo");
  const input = el("live-file-input");
  state.combo.open = false;
  state.combo.query = "";
  state.combo.active = -1;
  if (box) box.hidden = true;
  if (wrap) wrap.classList.remove("is-open");
  if (input) input.setAttribute("aria-expanded", "false");
  comboSync();
}

function commitCombo(id) {
  const select = el("live-file");
  if (!select || !id) return;
  const changed = id !== state.live.id;
  select.value = id;
  state.live.id = id;
  closeCombo();
  if (changed) select.dispatchEvent(new Event("change"));
}

function moveCombo(step) {
  if (!state.combo.open) {
    openCombo();
    return;
  }
  const total = state.combo.visible.length;
  if (!total) return;
  const next = (state.combo.active + step + total) % total;
  state.combo.active = next;
  renderCombo();
}

function buildLiveFiles() {
  const select = el("live-file");
  if (!select) return;
  const followable = state.allFiles.filter((item) => !item.compressed);
  if (!followable.length) {
    state.combo.options = [];
    select.innerHTML = '<option value="">' + esc(t("live.noFile", "暂无可跟随文件")) + "</option>";
    const empty = el("live-file-input");
    if (empty) {
      empty.value = "";
      empty.placeholder = t("live.noFile", "暂无可跟随文件");
    }
    closeCombo();
    return;
  }
  const bySource = new Map();
  for (const item of followable) {
    if (!bySource.has(item.source)) bySource.set(item.source, []);
    bySource.get(item.source).push(item);
  }
  const ordered = [];
  const html = [];
  for (const [source, items] of bySource) {
    items.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
    html.push(
      '<optgroup label="' + esc(sourceLabel(source, items[0].source_kind)) + '">'
    );
    for (const item of items) {
      ordered.push(item);
      html.push(
        '<option value="' + esc(item.id) + '">' +
          esc(comboLabel(item)) +
          "</option>"
      );
    }
    html.push("</optgroup>");
  }
  select.innerHTML = html.join("");
  state.combo.options = ordered;
  const preferred =
    ordered.find((item) => item.id === state.live.id) ||
    ordered.find((item) => item.relative === "all/all.log" && item.source === "current") ||
    ordered.find((item) => item.active) ||
    ordered[0];
  state.live.id = preferred.id;
  select.value = preferred.id;
  const input = el("live-file-input");
  if (input) input.placeholder = t("placeholder.liveFile", "搜索或选择日志文件");
  if (state.combo.open) renderCombo();
  else comboSync();
}

async function loadLive(reset) {
  const select = el("live-file");
  if (!select || !select.value) return;
  state.live.id = select.value;
  try {
    const data = await apiGet("content", { id: state.live.id, tail: 800 });
    state.live.entries = toEntries(data.lines);
    state.live.position = Number(data.position) || 0;
    renderLiveTags();
    renderLive(true);
    if (reset) toast(t("toast.liveLoaded", "已载入日志尾部"));
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function pollLive() {
  if (!state.live.id || state.live.busy) return;
  if (document.hidden) return;
  state.live.busy = true;
  try {
    const data = await apiGet("tail", { id: state.live.id, position: state.live.position });
    if (!data || data.supported === false) {
      stopLive();
      return;
    }
    state.live.position = Number(data.position) || 0;
    if (data.reset) {
      state.live.entries = toEntries(data.lines);
      renderLiveTags();
      renderLive(true);
    } else if (pushEntries(state.live.entries, data.lines)) {
      renderLiveTags();
      renderLive(false);
    }
  } catch (err) {
    stopLive();
    toast(errorText(err), "error");
  } finally {
    state.live.busy = false;
  }
}

function startLive() {
  stopLive();
  if (!state.live.id) buildLiveFiles();
  if (!state.live.id) return;
  if (!state.live.entries.length) loadLive(false);
  if (!el("live-follow").checked) {
    renderLive(false);
    return;
  }
  state.live.timer = window.setInterval(pollLive, FOLLOW_INTERVAL_MS);
  renderLive(false);
}

function stopLive() {
  if (state.live.timer) {
    window.clearInterval(state.live.timer);
    state.live.timer = null;
  }
  const hint = el("live-hint");
  if (hint && state.tab === "live") hint.textContent = t("live.paused", "已暂停");
}

function copyStream(entries, filter) {
  const text = entries
    .filter((entry) => entryPasses(entry, filter))
    .map((entry) => entry.raw)
    .join("\n");
  if (!text) {
    toast(t("toast.nothingToCopy", "没有可复制的内容"), "error");
    return;
  }
  const done = () => toast(t("toast.copied", "已复制到剪贴板"));
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
      return;
    }
  } catch (err) {
    /* fall through to the textarea fallback */
  }
  fallbackCopy(text, done);
}

function fallbackCopy(text, done) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "readonly");
  area.style.position = "fixed";
  area.style.top = "-9999px";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.focus();
  area.select();
  try {
    // Safari ignores select() on a read-only textarea without this.
    area.setSelectionRange(0, text.length);
  } catch (err) {
    /* not every engine implements it on textarea */
  }
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (err) {
    ok = false;
  }
  document.body.removeChild(area);
  if (ok) done();
  else toast(t("toast.copyFailed", "复制失败，请手动选择文本"), "error");
}

// -- search tab ------------------------------------------------------------

const HIT_RE = /^\[([^\]]+)\]\s*([\s\S]*)$/;

async function runSearch() {
  const keyword = el("search-keyword").value.trim();
  const meta = el("search-meta");
  const box = el("search-results");
  if (!keyword) {
    toast(t("toast.needKeyword", "请输入搜索关键词"), "error");
    return;
  }
  const limit = Math.max(10, Math.min(Number(el("search-limit").value) || 100, 500));
  meta.textContent = t("search.working", "正在搜索...");
  box.innerHTML = "";
  state.search = { keyword, results: [] };
  syncSearchExport();
  try {
    const data = await apiGet("search", { keyword, limit });
    const results = asList(data, "results").map((row) => String(row));
    state.search = { keyword, results };
    syncSearchExport();
    meta.textContent =
      t("search.result", "命中") + " " + results.length + " / " + (data.total || 0);
    if (!results.length) {
      box.innerHTML = '<p class="lv-empty">' + esc(t("search.empty", "没有命中的日志行")) + "</p>";
      return;
    }
    box.innerHTML = results
      .map((row) => {
        const match = HIT_RE.exec(String(row));
        if (!match) return '<div class="lv-hit">' + highlight(row, keyword) + "</div>";
        return (
          '<div class="lv-hit"><span class="lv-badge" data-kind="info">' +
          esc(match[1]) + "</span> " + highlight(match[2], keyword) + "</div>"
        );
      })
      .join("");
  } catch (err) {
    meta.textContent = "";
    toast(errorText(err), "error");
  }
}

function syncSearchExport() {
  const button = el("btn-export-search");
  if (!button) return;
  const total = state.search.results.length;
  button.disabled = total === 0;
  button.textContent =
    total > 0
      ? t("action.exportHits", "导出结果") + " (" + total + ")"
      : t("action.exportHits", "导出结果");
}

// -- diagnostics tab -------------------------------------------------------

function badgeList(values, kind) {
  if (!values.length) return "";
  return values
    .map(
      (value) =>
        '<span class="lv-badge"' + (kind ? ' data-kind="' + kind + '"' : "") +
        ' title="' + esc(value) + '">' + esc(value) + "</span>"
    )
    .join(" ");
}

function renderDiagnostics() {
  const capture = state.capture || (state.overview && state.overview.capture) || {};
  const mode = captureMode(capture);
  el("diag-pipeline").innerHTML = kvHtml([
    [t("diag.mode", "采集模式"), mode.text],
    [t("diag.loguru", "loguru sink"), capture.loguru_active ? t("common.on", "已接入") : t("common.off", "未接入")],
    [t("capture.handlerLevel", "写入级别"), capture.handler_level || "-"],
    [t("capture.sourceLevel", "astrbot 生效级别"), capture.astrbot_effective_level || "-"],
    [t("capture.forwarded", "已转发 / 丢弃"), (capture.forwarded || 0) + " / " + (capture.dropped || 0)],
    [t("capture.backfilled", "启动回填"), capture.backfilled || 0],
    [t("stats.pluginLoggers", "插件日志器"), capture.plugin_loggers === undefined ? "-" : capture.plugin_loggers],
    [t("stats.installed", "已安装插件"), capture.installed_plugins || 0],
    [t("capture.version", "插件版本"), capture.version || "-"],
  ]);
  const routes = Array.isArray(capture.web_routes) ? capture.web_routes : [];
  el("diag-routes").innerHTML = routes.length
    ? badgeList(routes, "ok")
    : '<p class="lv-empty">' + esc(t("diag.noRoutes", "未注册任何接口")) + "</p>";
  const loggers = Array.isArray(capture.attached_loggers) ? capture.attached_loggers : [];
  el("diag-loggers").innerHTML = loggers.length
    ? badgeList(loggers, "info")
    : '<p class="lv-empty">' +
      esc(t("diag.noLoggers", "loguru 模式下无需挂载 logging handler")) +
      "</p>";
  const tips = [];
  for (const warning of Array.isArray(capture.warnings) ? capture.warnings : []) {
    tips.push(String(warning));
  }
  if (mode.mode !== "loguru") {
    tips.push(
      t(
        "tip.loguru",
        "当前不是 loguru 模式：AstrBot 的控制台输出可能只有部分被记录，建议升级 AstrBot 或检查 loguru 是否可导入。"
      )
    );
  }
  if (capture.astrbot_effective_level && capture.astrbot_effective_level !== "DEBUG") {
    tips.push(
      t("tip.level", "astrbot 生效级别不是 DEBUG，低于该级别的日志在到达 LogVault 之前就被丢弃了。")
    );
  }
  if (capture.dropped) {
    tips.push(t("tip.dropped", "存在被丢弃的记录，请检查数据目录是否可写、磁盘是否已满。"));
  }
  if (!tips.length) tips.push(t("tip.healthy", "采集链路正常，未发现问题。"));
  el("diag-tips").innerHTML =
    '<ul class="lv-banner-list">' +
    tips.map((item) => "<li>" + esc(item) + "</li>").join("") +
    "</ul>";
}

async function loadDiagnostics() {
  try {
    state.capture = await apiGet("capture");
  } catch (err) {
    state.capture = (state.overview && state.overview.capture) || null;
  }
  renderDiagnostics();
}

// -- data loading ----------------------------------------------------------

async function loadFiles() {
  if (!state.category) {
    state.files = [];
    renderFiles();
    return;
  }
  try {
    const data = await apiGet("files", {
      source: state.category.source,
      category: state.category.key,
    });
    state.files = asList(data, "files");
  } catch (err) {
    state.files = [];
    toast(errorText(err), "error");
  }
  const alive = new Set(state.files.map((item) => item.id));
  for (const id of Array.from(state.selected)) {
    if (!alive.has(id)) state.selected.delete(id);
  }
  renderFiles();
}

// The live tab needs every followable file, not just the selected category.
async function loadAllFiles() {
  try {
    const data = await apiGet("files");
    state.allFiles = asList(data, "files");
  } catch (err) {
    state.allFiles = [];
  }
  buildLiveFiles();
}

async function loadOverview(quiet) {
  try {
    const data = await apiGet("overview");
    state.overview = data;
    state.capture = data.capture || state.capture;
    state.categories = Array.isArray(data.categories) ? data.categories : [];
    const match = state.category
      ? state.categories.find(
          (item) =>
            item.source === state.category.source && item.key === state.category.key
        )
      : null;
    state.category =
      match ||
      state.categories.find((item) => item.key === "__all__") ||
      state.categories[0] ||
      null;
    state.refreshedAt = new Date().toLocaleTimeString();
    renderOverview();
    renderTree();
    await loadFiles();
    await loadAllFiles();
    if (state.tab === "diag") renderDiagnostics();
    if (!quiet) toast(t("toast.refreshed", "已刷新"));
  } catch (err) {
    toast(errorText(err), "error");
  }
}

// -- actions ---------------------------------------------------------------

function stamp() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return (
    now.getFullYear() +
    pad(now.getMonth() + 1) +
    pad(now.getDate()) +
    "_" +
    pad(now.getHours()) +
    pad(now.getMinutes()) +
    pad(now.getSeconds())
  );
}

// Client side download for views that are already in memory (search hits,
// the live stream). Server bundles go through bridge.download instead.
function saveText(name, text) {
  try {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(() => URL.revokeObjectURL(url), 4000);
    return true;
  } catch (err) {
    toast(errorText(err), "error");
    return false;
  }
}

// -- export actions --------------------------------------------------------

function exportDays() {
  const input = el("export-days");
  const raw = String(input ? input.value : "").trim();
  if (raw === "") return 7;
  const days = Math.max(0, Math.min(Number(raw) || 0, 3650));
  if (input) input.value = days;
  return days;
}

// Builds the ExportSpec payload consumed by export_plan. Returns null when a
// required field is missing, after pointing the user at it.
function exportPayload() {
  const scope = exportScope();
  const body = {
    scope,
    format: el("export-format").value,
    mask: el("export-mask").checked,
    keyword: (el("export-keyword").value || "").trim(),
    levels: EXPORT_LEVELS.filter((level) => state.exporter.levels.has(level)),
    since: el("export-since").value || "",
    until: el("export-until").value || "",
    days: exportDays(),
  };
  if (scope === "preset") {
    body.preset = el("export-preset").value;
  } else if (scope === "plugin") {
    body.plugin = (el("export-plugin").value || "").trim();
    if (!body.plugin) {
      toast(t("toast.needPlugin", "请填写插件名"), "error");
      el("export-plugin").focus();
      return null;
    }
  } else if (scope === "category") {
    if (!state.category) {
      toast(t("toast.needCategory", "请先在日志文件页选择分类"), "error");
      return null;
    }
    body.source = state.category.source;
    body.category = state.category.key;
  } else if (scope === "selection") {
    body.ids = Array.from(state.selected).slice(0, 500);
    if (!body.ids.length) {
      toast(t("toast.needSelection", "请先在日志文件页勾选文件"), "error");
      return null;
    }
  }
  return body;
}

function exportFileName(plan) {
  const fmt = (plan && plan.format) || el("export-format").value;
  return "logvault_export_" + stamp() + (fmt === "merged" ? ".txt" : ".zip");
}

function setExportBusy(busy, hint) {
  state.exporter.busy = busy;
  for (const id of ["btn-export-plan", "btn-export-run"]) {
    const button = el(id);
    if (button) button.disabled = busy;
  }
  const box = el("export-hint");
  if (box) box.textContent = hint || "";
}

// Pre-flight: counts members without writing, and parks a one shot token so
// the follow up GET can stream the bundle (bridge.download cannot POST).
async function planExport(quiet) {
  const body = exportPayload();
  if (!body) return null;
  setExportBusy(true, t("export.planning", "正在预检..."));
  try {
    const plan = await apiPost("export_plan", body);
    state.exporter.plan = plan;
    state.exporter.token = plan.token || "";
    renderExportPreview();
    if (!quiet) {
      toast(
        plan.files
          ? t("toast.planned", "预检完成") + ": " + plan.files + " " + t("unit.file", "个") +
            " · " + formatSize(plan.bytes)
          : t("export.emptyPlan", "当前条件没有命中任何文件，请放宽时间窗或过滤条件。"),
        plan.files ? "ok" : "error"
      );
    }
    setExportBusy(false, "");
    return plan;
  } catch (err) {
    state.exporter.plan = null;
    state.exporter.token = "";
    renderExportPreview();
    setExportBusy(false, "");
    toast(errorText(err), "error");
    return null;
  }
}

async function runExport() {
  // Always re-plan: the token is single use and the form may have changed.
  const plan = await planExport(true);
  if (!plan) return;
  if (!plan.files || !state.exporter.token) {
    toast(t("export.emptyPlan", "当前条件没有命中任何文件，请放宽时间窗或过滤条件。"), "error");
    return;
  }
  const token = state.exporter.token;
  state.exporter.token = "";
  setExportBusy(true, t("export.working", "正在生成导出包..."));
  try {
    await bridge.download("export_file", { token }, exportFileName(plan));
    setExportBusy(false, t("export.done", "导出完成"));
    toast(t("toast.exported", "已开始下载导出包"));
    await loadExportHistory();
  } catch (err) {
    setExportBusy(false, "");
    toast(errorText(err), "error");
  }
}

async function loadExportHistory() {
  try {
    const data = await apiGet("export_history");
    state.exporter.history = asList(data, "exports");
    state.exporter.historyLoaded = true;
  } catch (err) {
    state.exporter.history = [];
  }
  renderExportHistory();
}

async function downloadExport(name) {
  try {
    await bridge.download("export_download", { name }, name);
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function purgeExports(names, all) {
  const ok = await askConfirm({
    title: all
      ? t("confirm.purgeAllTitle", "清空导出包")
      : t("confirm.purgeTitle", "删除导出包"),
    message: all
      ? t("confirm.purgeAll", "清空全部导出包？该操作不可恢复。")
      : t("confirm.purge", "删除该导出包？"),
    note: all ? t("confirm.purgeAllNote", "导出目录下保留的历史包会被全部移除。") : "",
    confirmLabel: t("action.confirmDelete", "确认删除"),
    danger: true,
  });
  if (!ok) return;
  try {
    const result = await apiPost("export_purge", all ? { all: true } : { names });
    toast(
      t("toast.deleted", "已删除") + " " + (result.deleted || 0) + " · " +
        t("toast.freed", "释放") + " " + formatSize(result.freed_bytes)
    );
  } catch (err) {
    toast(errorText(err), "error");
  }
  await loadExportHistory();
}

// Jumps to the export tab with the scope pre-selected, then pre-flights it.
function exportWithScope(scope) {
  const select = el("export-scope");
  if (select) select.value = scope;
  setTab("export");
  renderExportScopeNote();
  planExport(false);
}

function exportSearchHits() {
  const rows = state.search.results;
  if (!rows.length) {
    toast(t("toast.nothingToExport", "没有可导出的内容"), "error");
    return;
  }
  const header = [
    "# LogVault search export",
    "# keyword: " + state.search.keyword,
    "# hits: " + rows.length,
    "# generated: " + new Date().toLocaleString(),
    "",
  ].join("\n");
  if (saveText("logvault_search_" + stamp() + ".txt", header + rows.join("\n") + "\n")) {
    toast(t("toast.exported", "已开始下载导出包"));
  }
}

function exportLiveView() {
  const filter = liveFilter(false);
  const rows = state.live.entries
    .filter((entry) => entryPasses(entry, filter))
    .map((entry) => entry.raw);
  if (!rows.length) {
    toast(t("toast.nothingToExport", "没有可导出的内容"), "error");
    return;
  }
  const header = [
    "# LogVault live view export",
    "# file: " + (el("live-file").value || "-"),
    "# lines: " + rows.length,
    "# generated: " + new Date().toLocaleString(),
    "",
  ].join("\n");
  if (saveText("logvault_live_" + stamp() + ".txt", header + rows.join("\n") + "\n")) {
    toast(t("toast.exported", "已开始下载导出包"));
  }
}

async function downloadFile(id) {
  const file = state.files.find((item) => item.id === id) ||
    state.allFiles.find((item) => item.id === id) ||
    (state.view.file && state.view.file.id === id ? state.view.file : null);
  try {
    await bridge.download("download", { id }, (file && file.name) || "logvault.log");
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function deleteSelected() {
  const ids = Array.from(state.selected).slice(0, 500);
  if (!ids.length) return;
  const ok = await askConfirm({
    title: t("confirm.deleteTitle", "删除日志文件"),
    message: t("confirm.delete", "确认删除选中的日志文件？该操作不可恢复。"),
    note: t("confirm.deleteCount", "已选中 %s 个文件").replace("%s", String(ids.length)),
    confirmLabel: t("action.confirmDelete", "确认删除"),
    danger: true,
  });
  if (!ok) return;
  const button = el("btn-delete");
  button.disabled = true;
  try {
    const result = await apiPost("delete", { ids });
    const skipped = Array.isArray(result.skipped) ? result.skipped.length : 0;
    const parts = [
      t("toast.deleted", "已删除") + " " + (result.deleted || 0),
      t("toast.freed", "释放") + " " + formatSize(result.freed_bytes),
    ];
    if (skipped) parts.push(t("toast.skipped", "跳过") + " " + skipped);
    toast(parts.join(" · "));
    state.selected.clear();
    await loadOverview(true);
  } catch (err) {
    toast(errorText(err), "error");
  } finally {
    button.disabled = state.selected.size === 0;
  }
}

async function cleanNow(deep) {
  const ok = await askConfirm({
    title: deep
      ? t("confirm.cleanDeepTitle", "立即压缩轮换日志")
      : t("confirm.cleanTitle", "立即清理"),
    message: deep
      ? t("confirm.cleanDeep", "忽略压缩延迟，立即归档所有已轮换的日志？")
      : t("confirm.clean", "立即执行压缩与清理？超期日志会被删除。"),
    note: deep
      ? t("confirm.cleanDeepNote", "只压缩已关闭的轮换文件，正在写入的日志不受影响，压缩后仍可正常查看与搜索。")
      : t("confirm.cleanNote", "超过保留天数的日志会被压缩归档，过期归档会被删除。"),
    confirmLabel: t("action.confirmClean", "开始清理"),
    danger: true,
  });
  if (!ok) return;
  // Both buttons run the same pass, so lock them together: a second click
  // while the first pass is still scanning would duplicate the work.
  const buttons = [el("btn-clean"), el("btn-clean-deep")].filter(Boolean);
  buttons.forEach((node) => {
    node.disabled = true;
  });
  try {
    const result = await apiPost("clean", deep ? { deep: true } : {});
    const acted = (result.compressed || 0) + (result.deleted || 0);
    const parts = [
      t("toast.compressed", "压缩") + " " + (result.compressed || 0),
      t("toast.deleted", "已删除") + " " + (result.deleted || 0),
      t("toast.freed", "释放") + " " + formatSize(result.freed_bytes),
    ];
    if (!acted) parts.push(t("toast.cleanNoop", "无需清理，原因见总览"));
    toast(parts.join(" · "));
    renderCleanReport(result);
    // The report lives on the overview panel, and a run that changed nothing is
    // exactly the case where the user needs to read it.
    if (state.tab !== "overview") setTab("overview");
    await loadOverview(true);
  } catch (err) {
    toast(errorText(err), "error");
  } finally {
    buttons.forEach((node) => {
      node.disabled = false;
    });
  }
}

// -- static text -----------------------------------------------------------

const PLACEHOLDERS = [
  ["export-plugin", "placeholder.plugin"],
  ["export-keyword", "placeholder.exportKeyword"],
  ["live-keyword", "placeholder.liveKeyword"],
  ["live-file-input", "placeholder.liveFile"],
  ["category-filter", "placeholder.category"],
  ["file-filter", "placeholder.file"],
  ["search-keyword", "placeholder.search"],
  ["view-keyword", "placeholder.viewKeyword"],
];
const LABELS = [
  ["page-title", "title"],
  ["page-desc", "desc"],
  ["skin-label", "skin.label"],
  ["btn-refresh", "action.refresh"],
  ["btn-clean", "action.clean"],
  ["drawer-close", "action.close"],
];
const ARIA = [
  ["check-all", "files.checkAll"],
  ["file-sort", "files.sort"],
];

function defaultOf(node, attribute) {
  const key = "i18n" + attribute;
  if (node.dataset[key] === undefined) {
    node.dataset[key] =
      attribute === "Text" ? node.textContent : node.getAttribute(attribute.toLowerCase()) || "";
  }
  return node.dataset[key];
}

// Applies (or re-applies, after a locale switch) every static string. The
// first pass keeps the markup text as the fallback, so a missing i18n key
// never blanks a label.
function applyStaticText() {
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n, defaultOf(node, "Text"));
  }
  for (const pair of LABELS) {
    const node = el(pair[0]);
    if (node) node.textContent = t(pair[1], defaultOf(node, "Text"));
  }
  for (const pair of PLACEHOLDERS) {
    const node = el(pair[0]);
    if (node) node.placeholder = t(pair[1], defaultOf(node, "Placeholder"));
  }
  for (const pair of ARIA) {
    const node = el(pair[0]);
    if (!node) continue;
    if (node.dataset.i18nAria === undefined) {
      node.dataset.i18nAria = node.getAttribute("aria-label") || "";
    }
    node.setAttribute("aria-label", t(pair[1], node.dataset.i18nAria));
  }
  for (const id of ["live-level", "view-level"]) {
    const select = el(id);
    if (select && select.options.length) {
      select.options[0].textContent = t("live.allLevels", "全部级别");
    }
  }
  const title = el("drawer-title");
  if (title && !state.view.file) title.textContent = t("view.none", "未打开文件");
  applyDensity();
  buildSkinOptions();
}

// -- events ----------------------------------------------------------------

function bindEvents() {
  for (const button of document.querySelectorAll(".lv-tab")) {
    button.addEventListener("click", () => setTab(button.dataset.tab));
  }
  el("skin-select").addEventListener("change", (event) => setSkin(event.target.value));
  el("btn-density").addEventListener("click", toggleDensity);
  el("btn-refresh").addEventListener("click", () => loadOverview(false));
  el("btn-clean").addEventListener("click", () => cleanNow(false));
  el("btn-clean-deep").addEventListener("click", () => cleanNow(true));

  el("warn-banner").addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-banner-toggle]");
    if (!toggle) return;
    const banner = el("warn-banner");
    const collapsed = banner.classList.toggle("is-collapsed");
    toggle.textContent = collapsed
      ? t("action.expand", "展开")
      : t("action.collapse", "收起");
  });

  el("export-scope").addEventListener("change", () => {
    renderExportScopeNote();
    invalidateExportPlan();
  });
  el("export-levels").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-level]");
    if (!chip) return;
    const level = chip.dataset.level;
    if (state.exporter.levels.has(level)) state.exporter.levels.delete(level);
    else state.exporter.levels.add(level);
    renderExportLevels();
    invalidateExportPlan();
  });
  for (const id of [
    "export-preset",
    "export-plugin",
    "export-format",
    "export-days",
    "export-since",
    "export-until",
    "export-mask",
  ]) {
    const node = el(id);
    if (node) node.addEventListener("change", invalidateExportPlan);
  }
  el("export-keyword").addEventListener("input", debounce(invalidateExportPlan, 250));
  el("btn-export-window").addEventListener("click", () => {
    el("export-since").value = "";
    el("export-until").value = "";
    el("export-days").value = 7;
    invalidateExportPlan();
  });
  el("btn-export-plan").addEventListener("click", () => planExport(false));
  el("btn-export-run").addEventListener("click", runExport);
  el("export-plugin").addEventListener("keydown", (event) => {
    if (event.key === "Enter") planExport(false);
  });
  el("btn-export-reload").addEventListener("click", loadExportHistory);
  el("btn-export-purge").addEventListener("click", () => purgeExports([], true));
  el("export-history").addEventListener("click", (event) => {
    const grab = event.target.closest("[data-grab]");
    if (grab) {
      downloadExport(grab.dataset.grab);
      return;
    }
    const drop = event.target.closest("[data-drop]");
    if (drop) purgeExports([drop.dataset.drop], false);
  });

  el("live-file").addEventListener("change", () => {
    state.live.entries = [];
    state.live.position = 0;
    loadLive(false).then(() => startLive());
  });
  el("live-file-input").addEventListener("focus", openCombo);
  el("live-file-input").addEventListener("mousedown", (event) => {
    if (state.combo.open) return;
    openCombo();
    if (document.activeElement === event.target) event.preventDefault();
  });
  el("live-file-input").addEventListener("input", (event) => {
    state.combo.query = event.target.value;
    state.combo.active = 0;
    if (!state.combo.open) {
      const box = el("live-file-list");
      const wrap = el("live-file-combo");
      state.combo.open = true;
      if (box) box.hidden = false;
      if (wrap) wrap.classList.add("is-open");
      event.target.setAttribute("aria-expanded", "true");
    }
    renderCombo();
  });
  el("live-file-input").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveCombo(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveCombo(-1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const pick = state.combo.visible[state.combo.active];
      if (pick) commitCombo(pick.id);
    } else if (event.key === "Escape") {
      if (state.combo.open) event.stopPropagation();
      closeCombo();
    }
  });
  el("live-file-input").addEventListener("blur", () => {
    window.setTimeout(() => {
      if (document.activeElement !== el("live-file-input")) closeCombo();
    }, 0);
  });
  el("live-file-list").addEventListener("mousedown", (event) => event.preventDefault());
  el("live-file-list").addEventListener("click", (event) => {
    const option = event.target.closest(".lv-combo-option");
    if (option) commitCombo(option.dataset.id);
  });
  document.addEventListener("mousedown", (event) => {
    if (!state.combo.open) return;
    const node = event.target;
    if (!node || typeof node.closest !== "function" || !node.closest("#live-file-combo")) {
      closeCombo();
    }
  });
  el("live-level").addEventListener("change", () => renderLive(true));
  el("live-tag").addEventListener("change", () => renderLive(true));
  el("live-keyword").addEventListener("input", debounce(() => renderLive(true), 250));
  el("live-follow").addEventListener("change", (event) => {
    if (event.target.checked) startLive();
    else stopLive();
    renderLive(false);
  });
  el("btn-live-copy").addEventListener("click", () =>
    copyStream(state.live.entries, liveFilter(false))
  );
  el("btn-live-reload").addEventListener("click", () => loadLive(true));
  el("btn-live-export").addEventListener("click", exportLiveView);
  el("btn-live-clear").addEventListener("click", () => {
    state.live.entries = [];
    renderLiveTags();
    renderLive(true);
  });

  el("category-filter").addEventListener("input", debounce(renderTree, 200));
  el("tree").addEventListener("click", (event) => {
    const node = event.target.closest(".lv-node");
    if (!node) return;
    state.category = {
      source: node.dataset.source,
      key: node.dataset.key,
      name: node.dataset.name,
    };
    state.selected.clear();
    renderTree();
    loadFiles();
  });
  el("file-filter").addEventListener("input", debounce(renderFiles, 200));
  el("file-sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    renderFiles();
  });
  el("file-rows").addEventListener("change", (event) => {
    const box = event.target.closest(".lv-check");
    if (!box) return;
    if (box.checked) state.selected.add(box.dataset.id);
    else state.selected.delete(box.dataset.id);
    renderFiles();
  });
  el("file-rows").addEventListener("click", (event) => {
    const open = event.target.closest("[data-open]");
    if (open) {
      openDrawer(open.dataset.open);
      return;
    }
    const grab = event.target.closest("[data-download]");
    if (grab) downloadFile(grab.dataset.download);
  });
  el("check-all").addEventListener("change", (event) => {
    for (const item of visibleFiles()) {
      if (!item.deletable) continue;
      if (event.target.checked) state.selected.add(item.id);
      else state.selected.delete(item.id);
    }
    renderFiles();
  });
  el("btn-delete").addEventListener("click", deleteSelected);
  el("btn-export-files").addEventListener("click", () => exportWithScope("selection"));
  el("btn-export-category").addEventListener("click", () => exportWithScope("category"));

  el("btn-search").addEventListener("click", runSearch);
  el("btn-export-search").addEventListener("click", exportSearchHits);
  el("search-keyword").addEventListener("keydown", (event) => {
    if (event.key === "Enter") runSearch();
  });
  el("btn-diag-refresh").addEventListener("click", loadDiagnostics);

  el("drawer").addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) closeDrawer();
  });
  el("view-level").addEventListener("change", () => loadContent());
  el("view-tail").addEventListener("change", () => loadContent());
  // Keyword filtering stays client side: no extra request per keystroke.
  el("view-keyword").addEventListener("input", debounce(() => renderView(false), 250));
  el("view-follow").addEventListener("change", (event) => {
    if (event.target.checked) startViewFollow();
    else stopViewFollow(true);
  });
  el("btn-view-copy").addEventListener("click", () =>
    copyStream(state.view.entries, viewFilter(false))
  );
  el("btn-reload").addEventListener("click", () => loadContent());
  el("btn-download").addEventListener("click", () => {
    if (state.view.file) downloadFile(state.view.file.id);
  });

  el("confirm-ok").addEventListener("click", () => closeConfirm(true));
  for (const node of document.querySelectorAll("[data-confirm-cancel]")) {
    node.addEventListener("click", () => closeConfirm(false));
  }
  // The dialog only has two stops, so the focus trap stays this small.
  el("confirm-layer").addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const stops = [el("confirm-cancel"), el("confirm-ok")];
    const at = stops.indexOf(document.activeElement);
    const next = event.shiftKey
      ? (at <= 0 ? stops.length - 1 : at - 1)
      : (at === -1 || at === stops.length - 1 ? 0 : at + 1);
    event.preventDefault();
    stops[next].focus();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    // The dialog is modal, so it always wins over the drawer beneath it.
    if (closeConfirm(false)) {
      event.stopPropagation();
      return;
    }
    const drawer = el("drawer");
    if (drawer && !drawer.hidden) closeDrawer();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopLive();
      stopViewFollow(true);
      return;
    }
    if (state.tab === "live" && el("live-follow").checked) startLive();
    if (state.view.file && el("view-follow").checked) startViewFollow();
  });
  window.addEventListener("beforeunload", () => {
    stopLive();
    stopViewFollow(true);
  });
}

// -- boot ------------------------------------------------------------------

async function main() {
  if (!bridge) {
    document.body.innerHTML =
      "<p style=\"padding:24px;font-family:system-ui\">LogVault 需要在 AstrBot Dashboard 的插件页内打开。</p>";
    return;
  }
  await bridge.ready();
  await loadPrefs();
  state.skin = readStore(STORE_SKIN, "console");
  if (SKIN_IDS.indexOf(state.skin) === -1) state.skin = "console";
  state.density = readStore(STORE_DENSITY, "compact") === "cozy" ? "cozy" : "compact";
  const startTab = readStore(STORE_TAB, "overview");
  buildSkinOptions();
  applySkin();
  applyDensity();
  applyStaticText();
  bindEvents();
  let first = true;
  try {
    bridge.onContext(() => {
      if (first) {
        first = false;
        return;
      }
      applyStaticText();
      applySkin();
      renderOverview();
      renderTree();
      renderFiles();
    });
  } catch (err) {
    /* older dashboards do not expose onContext */
  }
  setTab(startTab);
  await loadOverview(true);
}

main().catch((err) => toast(errorText(err), "error"));
