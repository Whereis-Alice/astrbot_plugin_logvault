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
from pathlib import Path
from typing import Any

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


async def _json_body() -> dict:
    try:
        if _NATIVE_WEB_API:
            payload = await request.json(default={})
        else:
            payload = await request.get_json(silent=True)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class LogVaultWebApi:
    """Register and serve the dashboard endpoints of the logs page."""

    def __init__(self, plugin):
        self.plugin = plugin

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
