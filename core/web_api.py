"""Dashboard Web API for LogVault's plugin page.

The endpoints registered here back pages/logs/index.html.  They are all
read-mostly wrappers around CommandHandler, which owns path validation and
sensitive-value masking, so the HTTP layer stays thin.

AstrBot already protects plugin Web APIs with require_dashboard_user plus a
plugin scope dependency, therefore no extra authentication is implemented
here.  Anyone able to reach these routes can already read the dashboard
console.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from pathlib import Path
from typing import Any

from .command_handler import ExportSpec

try:  # AstrBot 4.27+ ships a framework-neutral helper module.
    from astrbot.api.web import (  # type: ignore[attr-defined]
        error_response,
        file_response,
        json_response,
        request,
    )

    _NATIVE_WEB_API = True
    _WEB_AVAILABLE = True
except ImportError:  # pragma: no cover - older AstrBot runs on quart
    _NATIVE_WEB_API = False
    try:
        from quart import jsonify, request, send_file  # type: ignore[import-not-found]

        _WEB_AVAILABLE = True
    except ImportError:
        # Neither backend is importable.  Keep the module importable so the
        # rest of the plugin still loads; register() then registers nothing.
        _WEB_AVAILABLE = False
        jsonify = None  # type: ignore[assignment]
        request = None  # type: ignore[assignment]
        send_file = None  # type: ignore[assignment]

    def json_response(  # type: ignore[misc]
        data: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ):
        response = jsonify({} if data is None else data)
        response.status_code = status_code
        if headers:
            response.headers.update(headers)
        return response

    def error_response(  # type: ignore[misc]
        message: str,
        *,
        status_code: int = 400,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ):
        return json_response(
            {"status": "error", "message": message, "data": data},
            status_code=status_code,
            headers=headers,
        )

    def file_response(  # type: ignore[misc]
        path: str | Path,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        try:
            return send_file(
                str(path),
                as_attachment=True,
                download_name=filename,
                mimetype=content_type,
            )
        except TypeError:
            # quart < 0.15 used attachment_filename instead.
            return send_file(
                str(path),
                as_attachment=True,
                attachment_filename=filename,
                mimetype=content_type,
            )


def _query(name: str, default: str = "") -> str:
    """Read one query string value across both request implementations."""

    container = request.query if _NATIVE_WEB_API else request.args
    try:
        value = container.get(name, default)
    except (AttributeError, TypeError):
        return default
    return default if value is None else str(value)


def _query_int(name: str, default: int) -> int:
    raw = _query(name, "")
    if raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _export_mime(fmt: str) -> str:
    return "text/plain; charset=utf-8" if fmt == "merged" else "application/zip"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


async def _json_body() -> dict:
    try:
        if _NATIVE_WEB_API:
            payload = await request.json(default={})
        else:
            payload = await request.get_json(silent=True)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


#: Console preferences that may be persisted, mapped to their allowed values.
#: Anything outside these tables is dropped, so a hand-crafted POST can never
#: smuggle arbitrary content into the plugin data directory.
CONSOLE_PREFS: dict[str, tuple[str, ...]] = {
    "skin": ("auto", "console", "daylight", "glass", "synthwave", "matrix"),
    "density": ("compact", "cozy"),
    "tab": ("overview", "live", "files", "search", "export", "diag"),
}
#: File under the plugin data directory that holds the persisted preferences.
CONSOLE_PREFS_FILE = "console_prefs.json"


def _sanitise_prefs(payload: Any) -> dict[str, str]:
    """Keep only known keys whose value is one of the allowed choices."""

    if not isinstance(payload, dict):
        return {}
    clean: dict[str, str] = {}
    for key, allowed in CONSOLE_PREFS.items():
        value = payload.get(key)
        if isinstance(value, str) and value in allowed:
            clean[key] = value
    return clean


def read_console_prefs(path: Path) -> dict[str, str]:
    """Load the stored console preferences; anything unreadable yields {}."""

    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        return _sanitise_prefs(json.loads(raw))
    except ValueError:
        return {}


def write_console_prefs(path: Path, payload: Any) -> dict[str, str]:
    """Merge ``payload`` into the stored preferences and return the result."""

    merged = {**read_console_prefs(path), **_sanitise_prefs(payload)}
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written aside and swapped in, so a crash cannot leave a truncated file
    # that would reset every preference on the next load.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return merged


class LogVaultWebApi:
    """Register and serve the dashboard endpoints of the logs page."""

    #: How long a pre-flighted export stays downloadable.
    EXPORT_TOKEN_TTL = 600.0
    #: Pre-flight results kept at once; oldest ones are dropped first.
    EXPORT_TOKEN_MAX = 8

    def __init__(self, plugin):
        self.plugin = plugin
        # The bridge can only download over GET with a query string, so an
        # export request is pre-flighted over POST and handed a short lived
        # token instead of squeezing file ids into the URL.
        self._export_tokens: dict[str, tuple[float, ExportSpec]] = {}

    # -- registration ---------------------------------------------------

    def register(self, plugin_id: str) -> list[str]:
        """Register every endpoint; returns the routes that were accepted."""

        register_web_api = getattr(self.plugin.context, "register_web_api", None)
        if not _WEB_AVAILABLE or not callable(register_web_api):
            return []
        endpoints = (
            ("overview", self.overview, ["GET"], "LogVault 概览与分类"),
            ("files", self.files, ["GET"], "LogVault 日志文件列表"),
            ("content", self.content, ["GET"], "LogVault 日志内容"),
            ("tail", self.tail, ["GET"], "LogVault 日志增量跟随"),
            ("search", self.search, ["GET"], "LogVault 日志搜索"),
            ("download", self.download, ["GET"], "LogVault 单文件下载"),
            ("bundle", self.bundle, ["GET"], "LogVault 按天数打包下载"),
            ("delete", self.delete, ["POST"], "LogVault 删除轮换日志"),
            ("clean", self.clean, ["POST"], "LogVault 手动清理"),
            ("capture", self.capture, ["GET"], "LogVault 捕获状态诊断"),
            ("export_plan", self.export_plan, ["POST"], "LogVault 导出预检"),
            ("export_file", self.export_file, ["GET"], "LogVault 导出下载"),
            ("export_history", self.export_history, ["GET"], "LogVault 导出历史"),
            ("export_download", self.export_download, ["GET"], "LogVault 历史包下载"),
            ("export_purge", self.export_purge, ["POST"], "LogVault 清理导出包"),
            ("prefs", self.prefs, ["GET"], "LogVault 控制台偏好"),
            ("prefs_save", self.prefs_save, ["POST"], "LogVault 控制台偏好保存"),
        )
        routes: list[str] = []
        for name, handler, methods, description in endpoints:
            route = f"/{plugin_id}/{name}"
            try:
                register_web_api(route, handler, methods, description)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            routes.append(route)
        return routes

    # -- helpers --------------------------------------------------------

    @property
    def commands(self):
        return getattr(self.plugin, "command_handler", None)

    @staticmethod
    def _not_ready():
        return error_response("LogVault 尚未初始化完成，请稍后重试", status_code=503)

    @property
    def _prefs_path(self) -> Path | None:
        data_dir = getattr(self.plugin, "data_dir", None)
        if not data_dir:
            return None
        try:
            return Path(data_dir) / CONSOLE_PREFS_FILE
        except TypeError:
            return None

    # -- endpoints ------------------------------------------------------

    async def overview(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        payload = await asyncio.to_thread(commands.overview_payload)
        payload["capture"] = self.plugin.capture_diagnostics()
        return json_response({"status": "ok", "data": payload})

    async def files(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        files = await asyncio.to_thread(
            commands.list_files, _query("source"), _query("category")
        )
        return json_response({"status": "ok", "data": {"files": files}})

    async def content(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        file_id = _query("id")
        if not file_id:
            return error_response("缺少参数 id")
        payload = await asyncio.to_thread(
            commands.read_file_lines,
            file_id,
            _query_int("tail", 500),
            _query("level"),
            _query("keyword"),
        )
        if payload is None:
            return error_response("日志文件不存在或不可访问", status_code=404)
        return json_response({"status": "ok", "data": payload})

    async def tail(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        file_id = _query("id")
        if not file_id:
            return error_response("缺少参数 id")
        payload = await asyncio.to_thread(
            commands.tail_bytes,
            file_id,
            _query_int("position", 0),
            _query_int("limit", 65536),
        )
        if payload is None:
            return error_response("日志文件不存在或不可访问", status_code=404)
        return json_response({"status": "ok", "data": payload})

    async def search(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        keyword = _query("keyword")
        if not keyword.strip():
            return error_response("缺少参数 keyword")
        limit = max(1, min(_query_int("limit", 100), 500))
        results, total = await asyncio.to_thread(commands._search_sync, keyword, limit)
        masked = [commands._mask(item) for item in results]
        return json_response(
            {
                "status": "ok",
                "data": {"keyword": keyword, "total": total, "results": masked},
            }
        )

    async def download(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        resolved = await asyncio.to_thread(commands.resolve_file, _query("id"))
        if resolved is None:
            return error_response("日志文件不存在或不可访问", status_code=404)
        _label, _root, path = resolved
        return file_response(
            path, filename=path.name, content_type="application/octet-stream"
        )

    async def bundle(self):
        """Build the same archive as /log send and stream it back."""

        commands = self.commands
        if commands is None:
            return self._not_ready()
        target = _query("target", "all").strip() or "all"
        days = _query_int("days", 7)
        message, zip_path = await commands.handle_send(target, days)
        if zip_path is None or not Path(zip_path).exists():
            return error_response(message, status_code=404)
        return file_response(
            zip_path, filename=Path(zip_path).name, content_type="application/zip"
        )

    async def delete(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        payload = await _json_body()
        raw_ids = payload.get("ids") or payload.get("id") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, list) or not raw_ids:
            return error_response("请提供要删除的日志文件 ids")
        if len(raw_ids) > 500:
            return error_response("一次最多删除 500 个文件")
        result = await asyncio.to_thread(commands.delete_files, raw_ids)
        return json_response({"status": "ok", "data": result})

    async def clean(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        cleaner = getattr(commands, "cleaner", None)
        if cleaner is None:
            return error_response("清理器尚未初始化", status_code=503)
        result = await cleaner.cleanup()
        return json_response({"status": "ok", "data": result})

    async def capture(self):
        return json_response(
            {"status": "ok", "data": self.plugin.capture_diagnostics()}
        )

    # -- export ---------------------------------------------------------

    def _remember_export(self, spec: ExportSpec) -> str:
        """Store a pre-flighted spec and return its one-shot token."""

        now = time.monotonic()
        self._export_tokens = {
            key: value
            for key, value in self._export_tokens.items()
            if now - value[0] <= self.EXPORT_TOKEN_TTL
        }
        while len(self._export_tokens) >= self.EXPORT_TOKEN_MAX:
            oldest = min(
                self._export_tokens, key=lambda key: self._export_tokens[key][0]
            )
            self._export_tokens.pop(oldest, None)
        token = secrets.token_urlsafe(18)
        self._export_tokens[token] = (now, spec)
        return token

    def _take_export(self, token: str) -> ExportSpec | None:
        """Consume a token; it is never valid twice."""

        entry = self._export_tokens.pop(str(token or "").strip(), None)
        if entry is None:
            return None
        created, spec = entry
        if time.monotonic() - created > self.EXPORT_TOKEN_TTL:
            return None
        return spec

    async def export_plan(self):
        """Count what an export would contain, without writing anything."""

        commands = self.commands
        if commands is None:
            return self._not_ready()
        payload = await _json_body()
        default_mask = bool(getattr(commands, "mask_on_export", True))
        try:
            spec = ExportSpec.from_payload(payload, default_mask=default_mask)
            preview = await asyncio.to_thread(commands.plan_export, spec)
        except ValueError as exc:
            return error_response(str(exc))
        preview["token"] = self._remember_export(spec) if preview["files"] else ""
        return json_response({"status": "ok", "data": preview})

    async def export_file(self):
        """Build the bundle behind a pre-flight token and stream it back."""

        commands = self.commands
        if commands is None:
            return self._not_ready()
        spec = self._take_export(_query("token"))
        if spec is None:
            return error_response("导出令牌已失效，请重新预检", status_code=410)
        try:
            result = await asyncio.to_thread(commands.build_export, spec)
        except ValueError as exc:
            return error_response(str(exc))
        path = Path(result["path"])
        if not path.exists():
            return error_response("导出文件生成失败", status_code=500)
        return file_response(
            path, filename=path.name, content_type=_export_mime(result["format"])
        )

    async def export_history(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        items = await asyncio.to_thread(commands.list_exports)
        return json_response({"status": "ok", "data": {"exports": items}})

    async def export_download(self):
        """Re-download a bundle that is still kept under data/exports."""

        commands = self.commands
        if commands is None:
            return self._not_ready()
        resolved = await asyncio.to_thread(commands.resolve_export, _query("name"))
        if resolved is None:
            return error_response("导出包不存在或已被清理", status_code=404)
        fmt = "merged" if resolved.suffix.casefold() == ".txt" else "zip"
        return file_response(
            resolved, filename=resolved.name, content_type=_export_mime(fmt)
        )

    async def export_purge(self):
        commands = self.commands
        if commands is None:
            return self._not_ready()
        payload = await _json_body()
        purge_all = _truthy(payload.get("all"))
        raw_names = payload.get("names") or payload.get("name") or []
        if isinstance(raw_names, str):
            raw_names = [raw_names]
        if not isinstance(raw_names, list):
            return error_response("names 必须是数组")
        if not purge_all and not raw_names:
            return error_response("请提供要删除的导出包名称")
        if len(raw_names) > 200:
            return error_response("一次最多删除 200 个导出包")
        result = await asyncio.to_thread(
            commands.delete_exports, [str(item) for item in raw_names], purge_all
        )
        return json_response({"status": "ok", "data": result})

    # -- console preferences --------------------------------------------

    async def prefs(self):
        """Return the stored console preferences (skin, density, active tab).

        The dashboard sandbox gives the plugin page an opaque origin, so
        window.localStorage raises there and the console cannot remember
        anything on its own.  A missing file is not an error; the console
        then simply starts on its defaults.
        """

        path = self._prefs_path
        if path is None:
            return json_response({"status": "ok", "data": {"prefs": {}}})
        prefs = await asyncio.to_thread(read_console_prefs, path)
        return json_response({"status": "ok", "data": {"prefs": prefs}})

    async def prefs_save(self):
        path = self._prefs_path
        if path is None:
            return self._not_ready()
        payload = await _json_body()
        try:
            prefs = await asyncio.to_thread(write_console_prefs, path, payload)
        except OSError:
            return error_response("控制台偏好写入失败", status_code=500)
        return json_response({"status": "ok", "data": {"prefs": prefs}})
