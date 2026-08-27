// LogVault dashboard page.  Renders the log catalog, viewer and maintenance
// actions on top of the plugin Web API registered by core/web_api.py.

const bridge = window.AstrBotPluginPage;

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
const KIND_ORDER = { all: 0, builtin: 1, plugin: 2, other: 3 };
const SOURCE_LABELS = {
  current: "当前数据目录",
  legacy: "旧数据目录",
  host: "AstrBot 主日志目录",
};
const FOLLOW_INTERVAL_MS = 2000;

const state = {
  overview: null,
  categories: [],
  files: [],
  category: null,
  selected: new Set(),
  file: null,
  position: 0,
  followTimer: null,
  toastTimer: null,
  busy: false,
};

const el = (id) => document.getElementById(id);

// -- small helpers ---------------------------------------------------------

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

function lineLevel(line) {
  const match = LEVEL_RE.exec(String(line).slice(0, 160));
  if (!match) return "";
  const token = match[1].toUpperCase();
  return LEVEL_ALIASES[token] || token;
}

function toast(message, kind) {
  const node = el("toast");
  if (!node) return;
  node.textContent = String(message);
  if (kind === "error") node.dataset.kind = "error";
  else delete node.dataset.kind;
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
  const base = SOURCE_LABELS[kind] || source;
  if (kind === "host" || kind === "legacy") return base + " (" + source + ")";
  return base;
}

// -- rendering -------------------------------------------------------------

function chip(label, value, warn) {
  return (
    '<span class="lv-chip' + (warn ? " lv-chip-warn" : "") + '">' +
    esc(label) + ": <strong>" + esc(value) + "</strong></span>"
  );
}

function renderStats() {
  const node = el("stats");
  if (!node) return;
  const data = state.overview;
  if (!data) {
    node.innerHTML = "";
    return;
  }
  const capture = data.capture || {};
  const parts = [];
  parts.push(chip(t("stats.files", "文件数"), data.total_files || 0));
  parts.push(chip(t("stats.size", "占用"), (data.total_size_mb || 0) + " MB"));
  parts.push(chip(t("stats.compressed", "已压缩"), data.compressed_count || 0));
  if (data.newest_file) parts.push(chip(t("stats.newest", "最新"), data.newest_file));
  if (data.oldest_file) parts.push(chip(t("stats.oldest", "最旧"), data.oldest_file));
  const mode = capture.mode || "unknown";
  const modeWarn = mode !== "loguru" && mode !== "logging";
  parts.push(chip(t("stats.mode", "捕获模式"), mode, modeWarn));
  if (capture.handler_level) {
    parts.push(chip(t("stats.handlerLevel", "写入级别"), capture.handler_level));
  }
  if (capture.astrbot_effective_level) {
    parts.push(
      chip(
        t("stats.sourceLevel", "AstrBot 级别"),
        capture.astrbot_effective_level,
        capture.astrbot_effective_level !== "DEBUG"
      )
    );
  }
  if (capture.forwarded !== undefined) {
    parts.push(chip(t("stats.forwarded", "已转发"), capture.forwarded));
  }
  if (capture.dropped) {
    parts.push(chip(t("stats.dropped", "丢弃"), capture.dropped, true));
  }
  if (capture.backfilled) {
    parts.push(chip(t("stats.backfilled", "启动回填"), capture.backfilled));
  }
  if (capture.plugin_loggers !== undefined) {
    parts.push(chip(t("stats.pluginLoggers", "插件日志器"), capture.plugin_loggers));
  }
  if (capture.version) parts.push(chip(t("stats.version", "版本"), capture.version));
  if (data.slice_by_record_time === false) {
    parts.push(chip(t("stats.slice", "按记录时间切片"), t("common.off", "关闭"), true));
  }
  const warnings = Array.isArray(capture.warnings) ? capture.warnings : [];
  for (const warning of warnings) {
    parts.push(chip(t("stats.warning", "提示"), warning, true));
  }
  if (data.data_dir) parts.push(chip(t("stats.dataDir", "数据目录"), data.data_dir));
  node.innerHTML = parts.join("");
}

function visibleCategories() {
  const needle = (el("category-filter").value || "").trim().toLowerCase();
  if (!needle) return state.categories;
  return state.categories.filter((item) => {
    const haystack = (item.name || "") + " " + (item.key || "") + " " + (item.source || "");
    return haystack.toLowerCase().includes(needle);
  });
}

function renderTree() {
  const node = el("tree");
  if (!node) return;
  const items = visibleCategories();
  if (!items.length) {
    node.innerHTML = '<p class="lv-muted lv-empty">' + esc(t("tree.empty", "没有匹配的分类")) + "</p>";
    return;
  }
  const groups = new Map();
  for (const item of items) {
    const key = item.source;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  const html = [];
  for (const [source, entries] of groups) {
    entries.sort((a, b) => {
      const ka = KIND_ORDER[a.kind] === undefined ? 9 : KIND_ORDER[a.kind];
      const kb = KIND_ORDER[b.kind] === undefined ? 9 : KIND_ORDER[b.kind];
      if (ka !== kb) return ka - kb;
      return String(a.name).localeCompare(String(b.name));
    });
    const kind = entries[0] ? entries[0].source_kind : "";
    html.push('<div class="lv-tree-group">');
    html.push('<p class="lv-muted">' + esc(sourceLabel(source, kind)) + "</p>");
    for (const item of entries) {
      const selected =
        state.category &&
        state.category.source === item.source &&
        state.category.key === item.key;
      html.push(
        '<button type="button" role="treeitem" class="lv-tree-item' +
          (item.kind === "plugin" ? " lv-tree-indent" : "") +
          '" aria-selected="' + (selected ? "true" : "false") +
          '" data-source="' + esc(item.source) +
          '" data-key="' + esc(item.key) +
          '" data-name="' + esc(item.name) +
          '" data-kind="' + esc(item.kind) + '">' +
          '<span class="lv-name">' + esc(item.name) + "</span>" +
          '<span class="lv-count">' + esc(item.count || 0) + " / " + esc(formatSize(item.size)) + "</span>" +
          "</button>"
      );
    }
    html.push("</div>");
  }
  node.innerHTML = html.join("");
}

function visibleFiles() {
  const needle = (el("file-filter").value || "").trim().toLowerCase();
  if (!needle) return state.files;
  return state.files.filter((item) =>
    ((item.relative || item.name || "") + " " + (item.category_name || "")).toLowerCase().includes(needle)
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
      : t("files.pick", "请选择左侧分类。");
  } else {
    empty.hidden = true;
    body.innerHTML = items
      .map((item) => {
        const opened = state.file && state.file.id === item.id;
        const tags = [];
        if (item.active) {
          tags.push('<span class="lv-tag lv-tag-active">' + esc(t("tag.active", "写入中")) + "</span>");
        }
        if (item.compressed) tags.push('<span class="lv-tag lv-tag-gz">gz</span>');
        const checkbox = item.deletable
          ? '<input type="checkbox" class="lv-check" data-id="' + esc(item.id) + '"' +
            (state.selected.has(item.id) ? " checked" : "") +
            ' aria-label="' + esc(item.name) + '" />'
          : "";
        return (
          '<tr data-id="' + esc(item.id) + '" aria-selected="' + (opened ? "true" : "false") + '">' +
          '<td class="lv-col-check">' + checkbox + "</td>" +
          '<td class="lv-name" title="' + esc(item.relative || item.name) + '">' +
          esc(item.relative || item.name) + tags.join("") + "</td>" +
          "<td>" + esc(item.category_name || item.category || "-") + "</td>" +
          '<td class="lv-num">' + esc(formatSize(item.size)) + "</td>" +
          "<td>" + esc(formatTime(item.mtime)) + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }
  const deletable = items.filter((item) => item.deletable);
  const checkAll = el("check-all");
  if (checkAll) {
    checkAll.disabled = deletable.length === 0;
    checkAll.checked =
      deletable.length > 0 && deletable.every((item) => state.selected.has(item.id));
  }
  el("btn-delete").disabled = state.selected.size === 0;
  const scope = el("file-scope");
  if (scope) {
    scope.textContent = state.category
      ? state.category.name + " · " + items.length + " " + t("files.unit", "个文件")
      : t("files.none", "未选择分类");
  }
}

function renderLines(lines, append) {
  const view = el("log-view");
  if (!view) return;
  const html = (lines || [])
    .map((line) => {
      const level = lineLevel(line);
      const cls = level ? "lv-line lv-line-" + level : "lv-line";
      return '<span class="' + cls + '">' + esc(line) + "</span>";
    })
    .join("");
  const atBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 24;
  if (append) view.insertAdjacentHTML("beforeend", html);
  else view.innerHTML = html;
  if (!append || atBottom) view.scrollTop = view.scrollHeight;
}

// -- data loading ----------------------------------------------------------

async function loadOverview(quiet) {
  try {
    const data = await apiGet("overview");
    state.overview = data;
    state.categories = Array.isArray(data.categories) ? data.categories : [];
    renderStats();
    if (state.category) {
      const still = state.categories.some(
        (item) => item.source === state.category.source && item.key === state.category.key
      );
      if (!still) state.category = null;
    }
    if (!state.category && state.categories.length) {
      const first = state.categories[0];
      state.category = { source: first.source, key: first.key, name: first.name };
    }
    renderTree();
    await loadFiles();
    if (!quiet) toast(t("toast.refreshed", "已刷新"));
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function loadFiles() {
  if (!state.category) {
    state.files = [];
    state.selected.clear();
    renderFiles();
    return;
  }
  try {
    const data = await apiGet("files", {
      source: state.category.source,
      category: state.category.key,
    });
    state.files = asList(data, "files");
    const ids = new Set(state.files.map((item) => item.id));
    for (const id of Array.from(state.selected)) {
      if (!ids.has(id)) state.selected.delete(id);
    }
    renderFiles();
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function loadContent(fileId) {
  const id = fileId || (state.file && state.file.id);
  if (!id) return;
  stopFollow(true);
  try {
    const data = await apiGet("content", {
      id,
      tail: Number(el("view-tail").value) || 500,
      level: el("view-level").value || "",
      keyword: el("view-keyword").value || "",
    });
    state.file = data;
    state.position = Number(data.position) || 0;
    renderLines(data.lines, false);
    renderFiles();
    el("viewer-title").textContent = data.name + " · " + sourceLabel(data.source, "");
    el("btn-reload").disabled = false;
    el("btn-download").disabled = false;
    const followBox = el("view-follow");
    const followable = !data.compressed;
    followBox.disabled = !followable;
    if (!followable) followBox.checked = false;
    const meta = [];
    meta.push(t("meta.matched", "命中行") + ": " + (data.matched || 0));
    meta.push(t("meta.scanned", "扫描行") + ": " + (data.scanned || 0));
    meta.push(t("meta.size", "大小") + ": " + formatSize(data.size));
    meta.push(t("meta.mtime", "修改时间") + ": " + formatTime(data.mtime));
    if (data.truncated) meta.push(t("meta.truncated", "已达扫描上限，仅显示部分内容"));
    if (data.compressed) meta.push(t("meta.compressed", "压缩文件不支持实时跟随"));
    if (data.error) meta.push(data.error);
    el("view-meta").textContent = meta.join(" · ");
    if (followBox.checked) startFollow();
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function pollTail() {
  if (!state.file || state.busy) return;
  state.busy = true;
  try {
    const data = await apiGet("tail", { id: state.file.id, position: state.position });
    if (!data || data.supported === false) {
      stopFollow();
      el("view-follow").checked = false;
      toast(t("toast.followUnsupported", "该文件不支持实时跟随"), "error");
      return;
    }
    state.position = Number(data.position) || 0;
    if (data.reset) {
      renderLines(data.lines, false);
      toast(t("toast.rotated", "日志已轮换，视图已重置"));
    } else if (data.lines && data.lines.length) {
      renderLines(data.lines, true);
    }
  } catch (err) {
    stopFollow();
    el("view-follow").checked = false;
    toast(errorText(err), "error");
  } finally {
    state.busy = false;
  }
}

function startFollow() {
  stopFollow(true);
  if (!state.file || state.file.compressed) return;
  state.followTimer = window.setInterval(pollTail, FOLLOW_INTERVAL_MS);
}

function stopFollow(keepCheckbox) {
  if (state.followTimer) {
    window.clearInterval(state.followTimer);
    state.followTimer = null;
  }
  if (!keepCheckbox) {
    const box = el("view-follow");
    if (box) box.checked = false;
  }
}

// -- actions ---------------------------------------------------------------

async function downloadBundle() {
  const mode = el("bundle-target").value;
  const days = Math.max(1, Math.min(Number(el("bundle-days").value) || 7, 3650));
  let target = mode;
  if (mode === "plugin") {
    target = (el("bundle-plugin").value || "").trim();
    if (!target) {
      toast(t("toast.needPlugin", "请填写插件名"), "error");
      return;
    }
  }
  const hint = el("bundle-hint");
  hint.textContent = t("bundle.working", "正在打包...");
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "");
  const name = "logvault_" + (mode === "plugin" ? target : mode) + "_" + days + "d_" + stamp + ".zip";
  try {
    await bridge.download("bundle", { target, days }, name);
    hint.textContent = t("bundle.done", "打包完成");
  } catch (err) {
    hint.textContent = "";
    toast(errorText(err), "error");
  }
}

async function deleteSelected() {
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  const question = t("confirm.delete", "确认删除选中的日志文件？此操作不可恢复。");
  if (!window.confirm(question + " (" + ids.length + ")")) return;
  try {
    const data = await apiPost("delete", { ids });
    state.selected.clear();
    const skipped = Array.isArray(data.skipped) ? data.skipped : [];
    let message =
      t("toast.deleted", "已删除") + " " + (data.deleted || 0) + " · " + formatSize(data.freed_bytes);
    if (skipped.length) {
      message += " · " + t("toast.skipped", "跳过") + " " + skipped.length + " (" + skipped[0].reason + ")";
    }
    toast(message, skipped.length ? "error" : undefined);
    await loadOverview(true);
  } catch (err) {
    toast(errorText(err), "error");
  }
}

async function cleanNow() {
  if (!window.confirm(t("confirm.clean", "立即执行压缩与过期清理？"))) return;
  const button = el("btn-clean");
  button.disabled = true;
  try {
    const data = await apiPost("clean", {});
    toast(
      t("toast.cleaned", "清理完成") +
        " · " + t("toast.compressed", "压缩") + " " + (data.compressed || 0) +
        " · " + t("toast.removed", "删除") + " " + (data.deleted || 0) +
        " · " + formatSize(data.freed_bytes)
    );
    await loadOverview(true);
  } catch (err) {
    toast(errorText(err), "error");
  } finally {
    button.disabled = false;
  }
}

async function downloadCurrent() {
  if (!state.file) return;
  try {
    await bridge.download("download", { id: state.file.id }, state.file.name);
  } catch (err) {
    toast(errorText(err), "error");
  }
}

// -- wiring ----------------------------------------------------------------

function applyStaticText() {
  el("page-title").textContent = t("title", "日志中心");
  el("page-desc").textContent = t(
    "desc",
    "按来源与插件分类查看、跟随、下载和清理 AstrBot 日志。"
  );
  el("btn-refresh").textContent = t("action.refresh", "刷新");
  el("btn-clean").textContent = t("action.clean", "立即清理");
  el("btn-bundle").textContent = t("action.bundle", "下载 ZIP");
  el("btn-delete").textContent = t("action.delete", "删除所选");
  el("btn-reload").textContent = t("action.reload", "重新加载");
  el("btn-download").textContent = t("action.download", "下载");
  el("category-filter").placeholder = t("placeholder.category", "过滤分类");
  el("file-filter").placeholder = t("placeholder.file", "过滤文件名");
  el("view-keyword").placeholder = t("placeholder.keyword", "关键词");
}

function bindEvents() {
  el("btn-refresh").addEventListener("click", () => loadOverview(false));
  el("btn-clean").addEventListener("click", cleanNow);
  el("btn-bundle").addEventListener("click", downloadBundle);
  el("btn-delete").addEventListener("click", deleteSelected);
  el("btn-reload").addEventListener("click", () => loadContent());
  el("btn-download").addEventListener("click", downloadCurrent);

  el("bundle-target").addEventListener("change", (event) => {
    el("bundle-plugin").hidden = event.target.value !== "plugin";
    el("bundle-hint").textContent = "";
  });

  el("category-filter").addEventListener("input", renderTree);
  el("file-filter").addEventListener("input", renderFiles);

  el("tree").addEventListener("click", (event) => {
    const button = event.target.closest(".lv-tree-item");
    if (!button) return;
    state.category = {
      source: button.dataset.source,
      key: button.dataset.key,
      name: button.dataset.name,
    };
    state.selected.clear();
    renderTree();
    loadFiles();
  });

  el("file-rows").addEventListener("change", (event) => {
    const box = event.target.closest(".lv-check");
    if (!box) return;
    if (box.checked) state.selected.add(box.dataset.id);
    else state.selected.delete(box.dataset.id);
    el("btn-delete").disabled = state.selected.size === 0;
    const deletable = visibleFiles().filter((item) => item.deletable);
    el("check-all").checked =
      deletable.length > 0 && deletable.every((item) => state.selected.has(item.id));
  });

  el("file-rows").addEventListener("click", (event) => {
    if (event.target.closest(".lv-check")) return;
    const row = event.target.closest("tr");
    if (!row || !row.dataset.id) return;
    loadContent(row.dataset.id);
  });

  el("check-all").addEventListener("change", (event) => {
    const deletable = visibleFiles().filter((item) => item.deletable);
    for (const item of deletable) {
      if (event.target.checked) state.selected.add(item.id);
      else state.selected.delete(item.id);
    }
    renderFiles();
  });

  el("view-level").addEventListener("change", () => loadContent());
  el("view-tail").addEventListener("change", () => loadContent());
  let keywordTimer = null;
  el("view-keyword").addEventListener("input", () => {
    if (keywordTimer) window.clearTimeout(keywordTimer);
    keywordTimer = window.setTimeout(() => loadContent(), 400);
  });

  el("view-follow").addEventListener("change", (event) => {
    if (event.target.checked) startFollow();
    else stopFollow(true);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopFollow(true);
    else if (el("view-follow").checked) startFollow();
  });

  window.addEventListener("beforeunload", () => stopFollow(true));
}

async function main() {
  if (!bridge || typeof bridge.ready !== "function") {
    document.body.innerHTML =
      '<p class="lv-muted">AstrBotPluginPage bridge 不可用，请在 AstrBot Dashboard 中打开此页面。</p>';
    return;
  }
  await bridge.ready();
  applyStaticText();
  bindEvents();
  // AstrBot 4.27 exposes onContext(); older builds used onContextChange().
  const subscribe =
    typeof bridge.onContext === "function"
      ? bridge.onContext.bind(bridge)
      : typeof bridge.onContextChange === "function"
        ? bridge.onContextChange.bind(bridge)
        : null;
  if (subscribe) {
    let first = true;
    subscribe(() => {
      applyStaticText();
      if (first) {
        // onContext() replays the current context immediately; nothing to
        // re-render before the first load finishes.
        first = false;
        return;
      }
      renderStats();
      renderTree();
      renderFiles();
    });
  }
  await loadOverview(true);
}

main().catch((err) => {
  toast(errorText(err), "error");
});
