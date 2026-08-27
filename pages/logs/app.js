// LogVault console (v2.2.0)
// A tab based operations console for the plugin Web API in core/web_api.py.
// Layout rules: every flex/grid child sets min-width:0 in style.css, so long
// paths, plugin names and warnings truncate instead of stretching the page.

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
const TABS = ["overview", "live", "files", "search", "diag"];

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
  toastTimer: null,
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

function readStore(key, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null || value === "" ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function writeStore(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (err) {
    /* private mode or storage disabled: skin simply is not remembered */
  }
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

function renderBundleCard() {
  const list = el("plugin-options");
  if (list) {
    const names = Array.from(
      new Set(
        state.categories
          .filter((item) => item.kind === "plugin")
          .map((item) => item.name)
      )
    ).sort();
    list.innerHTML = names
      .map((name) => '<option value="' + esc(name) + '"></option>')
      .join("");
  }
  const note = el("slice-note");
  if (note) {
    const slice = state.overview ? state.overview.slice_by_record_time !== false : true;
    note.textContent = slice
      ? t(
          "bundle.sliceOn",
          "按记录时间切片已开启：打包时会逐行截取指定天数内的记录，跨天的汇总日志也能精确裁剪。"
        )
      : t(
          "bundle.sliceOff",
          "按记录时间切片已关闭：只按文件修改时间筛选，命中的文件会整体打包。"
        );
  }
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
  renderBundleCard();
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
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
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
  try {
    const data = await apiGet("search", { keyword, limit });
    const results = asList(data, "results");
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

async function downloadBundle() {
  const scope = el("bundle-target").value;
  let target = scope;
  if (scope === "plugin") {
    target = (el("bundle-plugin").value || "").trim();
    if (!target) {
      toast(t("toast.needPlugin", "请填写插件名"), "error");
      el("bundle-plugin").focus();
      return;
    }
  }
  const days = Math.max(1, Math.min(Number(el("bundle-days").value) || 7, 3650));
  el("bundle-days").value = days;
  const hint = el("bundle-hint");
  const button = el("btn-bundle");
  button.disabled = true;
  if (hint) hint.textContent = t("bundle.working", "正在打包，请稍候...");
  const safe = target.replace(/[^A-Za-z0-9_.-]+/g, "_");
  try {
    await bridge.download(
      "bundle",
      { target, days },
      "logvault_" + safe + "_" + days + "d_" + stamp() + ".zip"
    );
    if (hint) hint.textContent = t("bundle.done", "打包完成");
    toast(t("toast.bundled", "已开始下载压缩包"));
  } catch (err) {
    if (hint) hint.textContent = "";
    toast(errorText(err), "error");
  } finally {
    button.disabled = false;
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
  const question = t("confirm.delete", "确认删除选中的日志文件？该操作不可恢复。");
  if (!window.confirm(question + " (" + ids.length + ")")) return;
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

async function cleanNow() {
  if (!window.confirm(t("confirm.clean", "立即执行压缩与清理？超期日志会被删除。"))) return;
  const button = el("btn-clean");
  button.disabled = true;
  try {
    const result = await apiPost("clean");
    toast(
      t("toast.compressed", "压缩") + " " + (result.compressed || 0) + " · " +
        t("toast.deleted", "已删除") + " " + (result.deleted || 0) + " · " +
        t("toast.freed", "释放") + " " + formatSize(result.freed_bytes)
    );
    await loadOverview(true);
  } catch (err) {
    toast(errorText(err), "error");
  } finally {
    button.disabled = false;
  }
}

// -- static text -----------------------------------------------------------

const PLACEHOLDERS = [
  ["bundle-plugin", "placeholder.plugin"],
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
  el("btn-clean").addEventListener("click", cleanNow);

  el("warn-banner").addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-banner-toggle]");
    if (!toggle) return;
    const banner = el("warn-banner");
    const collapsed = banner.classList.toggle("is-collapsed");
    toggle.textContent = collapsed
      ? t("action.expand", "展开")
      : t("action.collapse", "收起");
  });

  el("bundle-target").addEventListener("change", (event) => {
    el("bundle-plugin-field").hidden = event.target.value !== "plugin";
  });
  el("btn-bundle").addEventListener("click", downloadBundle);
  el("bundle-plugin").addEventListener("keydown", (event) => {
    if (event.key === "Enter") downloadBundle();
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

  el("btn-search").addEventListener("click", runSearch);
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

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
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
