"""AstrBot entry point for the LogVault logging plugin."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star, StarTools, register

from .core.command_handler import CommandHandler
from .core.config_manager import ConfigManager
from .core.log_cleaner import LogCleaner
from .core.log_handler import LogVaultHandler
from .core.loguru_capture import BootstrapBackfill, LoguruCapture
from .core.sensitive_filter import SensitiveFilter
from .core.web_api import LogVaultWebApi


PLUGIN_ID = "astrbot_plugin_logvault"
PLUGIN_VERSION = "2.3.1"
LEGACY_PLUGIN_ID = "astrbot_plugin_logplus"
LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
ASTRBOT_PLUGIN_LOGGER_PREFIX = "astrbot.plugin."
# The loguru sink sees new plugin loggers immediately, so the polling watcher
# is only needed by the logging fallback and can stay comparatively slow.
LOGGER_WATCH_INTERVAL_SECONDS = 2.0
CAPTURE_MODES = ("auto", "loguru", "logging")
# The installed-plugin catalog is consulted per log record, so it is cached.
CATALOG_CACHE_SECONDS = 30.0


@register(
    "LogVault",
    "Whereis-Alice",
    "AstrBot 日志持久化与安全导出，修复跨平台路由和 Linux 活动日志丢失问题",
    PLUGIN_VERSION,
    "https://github.com/Whereis-Alice/astrbot_plugin_logvault",
)
class LogVaultPlugin(Star):
    """Persist, classify, retain, search, and send AstrBot logs."""

    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.context = context
        # Passing the ID explicitly is important after the plugin was renamed;
        # relying on stack inspection can resolve the old metadata in a mixed
        # installation.
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_ID)
        self.config_manager = ConfigManager(config)
        self.log_handler: LogVaultHandler | None = None
        self.log_cleaner: LogCleaner | None = None
        self.sensitive_filter: SensitiveFilter | None = None
        self.command_handler: CommandHandler | None = None
        self.loguru_capture: LoguruCapture | None = None
        self.web_api = LogVaultWebApi(self)
        self._init_task: asyncio.Task | None = None
        self._logger_watch_task: asyncio.Task | None = None
        self._attached_loggers: set[logging.Logger] = set()
        self._capture_mode = "pending"
        self._backfilled = 0
        self._catalog_index: dict[str, str] | None = None
        self._catalog_index_time = 0.0
        self._web_routes: list[str] = []
        self._level_warnings: list[str] = []

        try:
            loop = asyncio.get_running_loop()
            self._init_task = loop.create_task(self._initialize_plugin())
        except RuntimeError:
            # AstrBot normally constructs plugins inside its event loop.  Keep
            # construction safe for static tooling and unit tests as well.
            logger.warning("LogVault 将在事件循环可用后初始化")

    def _legacy_data_dirs(self) -> list[Path]:
        if not self.config_manager.get("include_legacy_data", True):
            return []

        candidates: list[Path] = [self.data_dir.parent / LEGACY_PLUGIN_ID]
        candidates.extend(Path(item) for item in self.config_manager.get_legacy_data_dirs())
        result: list[Path] = []
        seen: set[str] = {str(self.data_dir.resolve()).casefold()}
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            key = str(resolved).casefold()
            if resolved.is_dir() and key not in seen:
                seen.add(key)
                result.append(resolved)
        return result

    def _installed_plugin_catalog(self) -> dict[str, set[str]]:
        """Return live AstrBot plugin IDs and their searchable aliases."""

        catalog: dict[str, set[str]] = {}
        get_all_stars = getattr(self.context, "get_all_stars", None)
        stars = []
        if callable(get_all_stars):
            try:
                stars = get_all_stars()
            except (AttributeError, RuntimeError, TypeError):
                stars = []

        for star in stars:
            name = str(getattr(star, "name", "") or "").strip()
            root_name = str(getattr(star, "root_dir_name", "") or "").strip()
            canonical_name = name or root_name
            if not canonical_name:
                continue
            aliases = {canonical_name}
            for attribute in ("name", "root_dir_name", "display_name"):
                value = str(getattr(star, attribute, "") or "").strip()
                if value:
                    aliases.add(value)
            catalog.setdefault(canonical_name, set()).update(aliases)

        # Filesystem fallback for older AstrBot 4.x releases or disabled
        # plugins that are present under data/plugins but absent from the
        # live registry. StarTools stores our data at data/plugin_data/ID.
        plugins_root = self.data_dir.parent.parent / "plugins"
        try:
            plugin_dirs = [
                item
                for item in plugins_root.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ]
        except OSError:
            plugin_dirs = []
        for plugin_dir in plugin_dirs:
            root_name = plugin_dir.name.strip()
            if not root_name or root_name == "__pycache__":
                continue
            owner = next(
                (
                    name
                    for name, aliases in catalog.items()
                    if root_name.casefold()
                    in {alias.casefold() for alias in aliases}
                ),
                root_name,
            )
            catalog.setdefault(owner, set()).add(root_name)
        return catalog

    def _plugin_logger_names(self) -> set[str]:
        """Find AstrBot's dedicated logger names across supported versions."""

        names = {PLUGIN_ID, "LogVault"}
        logger_dict = logging.Logger.manager.loggerDict
        try:
            existing_names = list(logger_dict.items())
        except RuntimeError:
            existing_names = []
        names.update(
            logger_name[len(ASTRBOT_PLUGIN_LOGGER_PREFIX) :]
            for logger_name, logger_value in existing_names
            if isinstance(logger_value, logging.Logger)
            and logger_name.casefold().startswith(ASTRBOT_PLUGIN_LOGGER_PREFIX)
            and logger_name[len(ASTRBOT_PLUGIN_LOGGER_PREFIX) :].strip()
        )

        # AstrBot keeps the authoritative set on LogManager as well.  It is
        # useful during the small window between plugin registration and the
        # creation of the corresponding logging.Logger instance.
        try:
            from astrbot.core.log import LogManager

            manager_names = getattr(LogManager, "_plugin_logger_names", set())
            names.update(str(name).strip() for name in manager_names if str(name).strip())
        except (ImportError, AttributeError, RuntimeError, TypeError):
            pass

        get_all_stars = getattr(self.context, "get_all_stars", None)
        if callable(get_all_stars):
            try:
                stars = get_all_stars()
            except (AttributeError, RuntimeError, TypeError):
                stars = []
            for star in stars:
                for attribute in ("name", "root_dir_name"):
                    value = str(getattr(star, attribute, "") or "").strip()
                    if value:
                        names.add(value)
        return {name for name in names if name.strip()}

    def _host_log_dirs(self) -> list[Path]:
        """Resolve AstrBot's shared log directories for fallback filtering.

        AstrBot normally stores its file sink under ``data/logs``.  Recent
        versions also allow an absolute or data-relative custom path, so use
        the live core config when it is available and retain an explicit
        plugin setting for deployments that keep logs elsewhere.
        """

        data_root = self.data_dir.parent.parent
        directories: list[Path] = [data_root / "logs"]

        def add_directory(value: object) -> None:
            if value is None or str(value).strip() == "":
                return
            try:
                path = Path(str(value)).expanduser()
                if not path.is_absolute():
                    path = data_root / path
                directories.append(path)
            except (OSError, RuntimeError, TypeError):
                return

        for value in self.config_manager.get_host_log_dirs():
            add_directory(value)

        # AstrBot 4.27 uses log_file.path; older releases expose
        # log_file_path.  Both configured paths point to a file, so add the
        # parent directory rather than treating the file as a directory.
        configured_file: object = None
        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            try:
                core_config = get_config()
            except (AttributeError, RuntimeError, TypeError):
                core_config = None
            if core_config is not None:
                try:
                    legacy_path = core_config.get("log_file_path")
                except (AttributeError, TypeError):
                    legacy_path = None
                try:
                    log_file = core_config.get("log_file")
                except (AttributeError, TypeError):
                    log_file = None
                if isinstance(log_file, Mapping):
                    configured_file = log_file.get("path") or legacy_path
                else:
                    configured_file = legacy_path

        if configured_file is not None and str(configured_file).strip():
            try:
                file_path = Path(str(configured_file)).expanduser()
                if not file_path.is_absolute():
                    file_path = data_root / file_path
                directories.append(file_path.parent)
            except (OSError, RuntimeError, TypeError):
                pass

        result: list[Path] = []
        seen: set[str] = set()
        for directory in directories:
            try:
                resolved = directory.resolve()
            except (OSError, RuntimeError):
                continue
            key = str(resolved).casefold()
            if key not in seen:
                seen.add(key)
                result.append(resolved)
        return result

    def _attach_logging_handlers(self) -> None:
        """Attach LogVault to global and per-plugin loggers.

        AstrBot 4.27+ routes ``astrbot.api.logger`` calls to isolated
        ``astrbot.plugin.<name>`` loggers with ``propagate=False``.  The
        global logger alone therefore misses plugin records; older releases
        only need the global target, so both paths are supported here.
        """

        if not self.log_handler:
            return
        if self.loguru_capture is not None and self.loguru_capture.active:
            # AstrBot bridges logging -> loguru, so a mounted handler plus the
            # loguru sink would persist every record twice.
            return
        # The parent target preserves compatibility with releases where
        # plugin loggers propagated.  AstrBot 4.27+ sets propagate=False on
        # each dedicated logger, so those loggers are attached explicitly.
        targets = {
            logging.getLogger("astrbot"),
            logging.getLogger(ASTRBOT_PLUGIN_LOGGER_PREFIX.rstrip(".")),
        }
        if self.config_manager.get("capture_third_party", True):
            # Third-party libraries only ever reach the root logger, which is
            # where AstrBot installs its own bridge as well.  emit() de-dupes
            # per handler instance, so a record arriving through both the
            # root logger and "astrbot" is still written once.
            targets.add(logging.getLogger())
        targets.update(
            logging.getLogger(f"astrbot.plugin.{name}")
            for name in self._plugin_logger_names()
        )
        for target in targets:
            if self.log_handler not in target.handlers:
                target.addHandler(self.log_handler)
            self._attached_loggers.add(target)

    async def _logger_watch_loop(self):
        """Attach to plugin loggers created after LogVault starts."""

        while True:
            try:
                await asyncio.sleep(LOGGER_WATCH_INTERVAL_SECONDS)
                self._attach_logging_handlers()
            except asyncio.CancelledError:
                raise
            except (AttributeError, RuntimeError, TypeError):
                continue

    def _resolve_installed_plugin(self, hint: str) -> str | None:
        """Map a directory/tag hint to an installed plugin name.

        LogVaultHandler calls this for every record that has no explicit
        plugin marker, so the catalog is cached: rebuilding it walks
        data/plugins on disk and would be far too expensive per log line.
        """

        candidate = str(hint or "").strip()
        if not candidate:
            return None
        now = time.monotonic()
        if (
            self._catalog_index is None
            or now - self._catalog_index_time > CATALOG_CACHE_SECONDS
        ):
            index: dict[str, str] = {}
            try:
                catalog = self._installed_plugin_catalog()
            except (AttributeError, OSError, RuntimeError, TypeError):
                catalog = {}
            for name, aliases in catalog.items():
                for alias in aliases:
                    key = str(alias).strip().casefold()
                    if key:
                        index.setdefault(key, name)
            self._catalog_index = index
            self._catalog_index_time = now
        return self._catalog_index.get(candidate.casefold())

    def _audit_source_levels(self, level_name: str) -> None:
        """Make sure upstream loggers are verbose enough to be captured.

        AstrBot applies its dashboard log level to the ``astrbot`` logger and
        propagates it to every ``astrbot.plugin.*`` logger.  A record below
        that level is discarded before any handler or loguru sink runs, so a
        console set to INFO makes LogVault DEBUG files impossible.  The level
        is only changed when explicitly allowed, because it also affects what
        AstrBot prints to its own console.
        """

        self._level_warnings = []
        desired = LOG_LEVELS.get(level_name, logging.DEBUG)
        force = bool(self.config_manager.get("force_source_debug", False))
        targets = {logging.getLogger("astrbot")}
        targets.update(
            logging.getLogger(f"{ASTRBOT_PLUGIN_LOGGER_PREFIX}{name}")
            for name in self._plugin_logger_names()
        )
        blocked: list[str] = []
        for target in targets:
            effective = target.getEffectiveLevel()
            if effective <= desired:
                continue
            if force:
                try:
                    target.setLevel(desired)
                except (TypeError, ValueError):
                    continue
            else:
                blocked.append(f"{target.name}={logging.getLevelName(effective)}")
        if blocked:
            preview = "、".join(sorted(blocked)[:5])
            self._level_warnings.append(
                f"以下上游 logger 级别高于 {level_name}，这些记录不会进入日志文件："
                f"{preview}"
                f"{f'（共 {len(blocked)} 个）' if len(blocked) > 5 else ''}"
                "；如需完整日志请开启 force_source_debug 或调高 AstrBot 控制台日志级别"
            )

    def _start_capture(self, config: dict[str, Any]) -> None:
        """Pick the capture mechanism; loguru and logging are exclusive."""

        if not self.log_handler:
            return
        mode = str(config.get("capture_mode", "auto")).strip().casefold()
        if mode not in CAPTURE_MODES:
            mode = "auto"

        if mode in ("auto", "loguru"):
            capture = LoguruCapture(
                self.log_handler,
                level=self.log_handler.level or logging.DEBUG,
                include_trace=bool(config.get("include_trace_log", False)),
            )
            if capture.start():
                self.loguru_capture = capture
                self._capture_mode = "loguru"
                return
            if mode == "loguru":
                logger.warning("LogVault: loguru sink 安装失败，已回退到 logging 捕获")

        self._capture_mode = "logging"
        self._attach_logging_handlers()
        if self._logger_watch_task is None or self._logger_watch_task.done():
            self._logger_watch_task = asyncio.create_task(self._logger_watch_loop())

    def _replay_startup_logs(self, config: dict[str, Any]) -> None:
        """Persist the console lines AstrBot buffered before we loaded."""

        if not self.log_handler or not config.get("backfill_startup_logs", True):
            return
        try:
            limit = max(1, min(int(config.get("backfill_limit", 500)), 5000))
        except (TypeError, ValueError):
            limit = 500
        try:
            self._backfilled = BootstrapBackfill(self.data_dir, limit=limit).replay(
                self.log_handler
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("LogVault 启动日志回填跳过: %s", exc)

    def capture_diagnostics(self) -> dict[str, Any]:
        """Summarise the capture pipeline for /log status and the WebUI."""

        capture = self.loguru_capture
        astrbot_logger = logging.getLogger("astrbot")
        return {
            "mode": self._capture_mode,
            "loguru_active": bool(capture and capture.active),
            "forwarded": int(getattr(capture, "forwarded", 0) or 0),
            "dropped": int(getattr(capture, "dropped", 0) or 0),
            "attached_loggers": sorted(
                target.name or "root" for target in self._attached_loggers
            ),
            "handler_level": logging.getLevelName(
                self.log_handler.level if self.log_handler else logging.NOTSET
            ),
            "astrbot_effective_level": logging.getLevelName(
                astrbot_logger.getEffectiveLevel()
            ),
            "backfilled": self._backfilled,
            "plugin_loggers": len(self._plugin_logger_names()),
            "installed_plugins": len(self._installed_plugin_catalog()),
            "web_routes": list(self._web_routes),
            "warnings": list(self._level_warnings),
            "version": PLUGIN_VERSION,
        }

    async def _initialize_plugin(self):
        try:
            config = self.config_manager.as_dict()
            if config.get("enable_sensitive_filter", True):
                self.sensitive_filter = SensitiveFilter(
                    keywords=self.config_manager.get_sensitive_keywords(), enabled=True
                )

            self.log_handler = LogVaultHandler(
                self.data_dir,
                config,
                sensitive_filter=self.sensitive_filter,
                plugin_name_resolver=self._resolve_installed_plugin,
            )
            level_name = str(config.get("log_level", "DEBUG")).upper()
            self.log_handler.setLevel(LOG_LEVELS.get(level_name, logging.DEBUG))

            # Raising the upstream logger levels has to happen first: a record
            # dropped by its own logger never reaches any handler or sink.
            self._audit_source_levels(level_name)
            self._start_capture(config)
            self._replay_startup_logs(config)

            self.log_cleaner = LogCleaner(self.data_dir, config)
            await self.log_cleaner.start()
            self.command_handler = CommandHandler(
                self.data_dir,
                self.log_cleaner,
                self._legacy_data_dirs(),
                plugin_catalog_provider=self._installed_plugin_catalog,
                host_log_dirs=self._host_log_dirs(),
                sensitive_filter=self.sensitive_filter,
                slice_by_record_time=bool(config.get("slice_by_record_time", True)),
                mask_on_export=bool(config.get("mask_on_export", True)),
            )
            self._web_routes = self.web_api.register(PLUGIN_ID)
            legacy_note = len(self.command_handler.additional_data_dirs)
            logger.info(
                "✅ LogVault 已启动，日志目录: %s，捕获模式: %s%s%s",
                self.data_dir,
                self._capture_mode,
                f"，回填 {self._backfilled} 条启动日志" if self._backfilled else "",
                f"，已发现 {legacy_note} 个旧数据目录" if legacy_note else "",
            )
            for warning in self._level_warnings:
                logger.warning("LogVault: %s", warning)
        except Exception as exc:
            logger.error("LogVault 初始化失败: %s", exc, exc_info=True)

    async def terminate(self):
        """Stop maintenance and close all file streams on unload."""
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()
            try:
                await self._init_task
            except asyncio.CancelledError:
                pass
        if self._logger_watch_task and not self._logger_watch_task.done():
            self._logger_watch_task.cancel()
            try:
                await self._logger_watch_task
            except asyncio.CancelledError:
                pass
        if self.loguru_capture:
            self.loguru_capture.stop()
            self.loguru_capture = None
        if self.log_cleaner:
            await self.log_cleaner.stop()
        if self.log_handler:
            for target in list(self._attached_loggers):
                target.removeHandler(self.log_handler)
            self._attached_loggers.clear()
            self.log_handler.close()
            self.log_handler = None
        logger.info("LogVault 已停止")

    @filter.on_plugin_loaded()
    async def _on_plugin_loaded(self, metadata: Any = None):
        """React to a newly loaded plugin.

        A fresh plugin brings a new dedicated logger that inherits AstrBot's
        global level, and it invalidates the cached plugin catalog used to
        attribute records to plugin directories.
        """

        self._catalog_index = None
        if self.log_handler:
            self._audit_source_levels(
                str(self.config_manager.get("log_level", "DEBUG")).upper()
            )
        self._attach_logging_handlers()

    @filter.command_group("log", alias={"logvault", "logplus"})
    def log(self):
        """LogVault commands.

        ``log`` is the primary entry point; ``logvault`` and ``logplus`` stay
        registered as aliases so existing muscle memory keeps working.
        """

    @log.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看日志状态。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        status = await self.command_handler.handle_status()
        yield event.plain_result(f"{status}\n\n{self._capture_summary()}")

    def _capture_summary(self) -> str:
        """Human readable capture diagnostics appended to /log status."""

        info = self.capture_diagnostics()
        mode_labels = {
            "loguru": "loguru sink（全量捕获）",
            "logging": "logging 处理器（兼容模式）",
            "pending": "初始化中",
        }
        lines = [
            "📡 日志捕获",
            f"├─ 模式: {mode_labels.get(info['mode'], info['mode'])}",
            (
                f"├─ 写入级别: {info['handler_level']}"
                f"（astrbot 生效级别 {info['astrbot_effective_level']}）"
            ),
        ]
        if info["mode"] == "loguru":
            lines.append(
                f"├─ 已转发: {info['forwarded']} 条，丢弃: {info['dropped']} 条"
            )
        else:
            lines.append(f"├─ 已挂载 logger: {len(info['attached_loggers'])} 个")
        lines.append(
            f"├─ 启动回填: {info['backfilled']} 条，"
            f"插件 logger: {info['plugin_loggers']} 个，"
            f"已安装插件: {info['installed_plugins']} 个"
        )
        lines.append(
            f"└─ WebUI 接口: {len(info['web_routes'])} 个"
            if info["web_routes"]
            else "└─ WebUI 接口: 未注册（当前 AstrBot 版本不支持）"
        )
        for warning in info["warnings"]:
            lines.append(f"⚠️ {warning}")
        return "\n".join(lines)

    @log.command("search")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索当前及兼容旧目录中的日志。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(await self.command_handler.handle_search(keyword))

    @log.command("clean")
    async def cmd_clean(self, event: AstrMessageEvent):
        """手动清理已关闭的旧日志。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(await self.command_handler.handle_clean())

    @log.command("export")
    async def cmd_export(
        self, event: AstrMessageEvent, days: str = "", until: str = ""
    ):
        """导出日志：``[天数]``、``0`` 表示不限，或 ``<起始日期> [结束日期]``。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        # Both arguments stay str so a typo answers with our own hint instead
        # of making AstrBot's argument parser raise a conversion error.
        yield event.plain_result(
            await self.command_handler.handle_export(days, until)
        )

    @log.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示命令帮助。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(self.command_handler.handle_help())

    @log.command("send")
    async def cmd_send(
        self,
        event: AstrMessageEvent,
        target: str = "",
        plugin: str = "",
        days: str = "",
    ):
        """发送最近 N 天的日志；支持 ``plugin NAME DAYS``。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return

        usage = (
            "❌ 用法:\n"
            "  /log send all [天数]\n"
            "  /log send errors [天数]\n"
            "  /log send plugin <插件名> [天数]"
        )
        # Every argument is declared as str on purpose: AstrBot converts an
        # int-annotated argument eagerly and raises on a non-numeric token,
        # which used to swallow the day count and silently archive everything.
        target = str(target or "").strip()
        plugin = str(plugin or "").strip()
        days_token = str(days or "").strip()
        target_lower = target.casefold()

        if not target:
            yield event.plain_result(usage)
            return

        if target_lower == "plugin":
            if not plugin:
                yield event.plain_result(usage)
                return
            final_target = plugin
        else:
            # ``send all 3`` puts the day count into ``plugin``; treat the
            # trailing numeric token as the day count for that shorthand.
            if plugin:
                if days_token:
                    yield event.plain_result(usage)
                    return
                days_token = plugin
            final_target = target

        message, zip_path = await self.command_handler.handle_send(
            final_target, days_token
        )
        if zip_path and zip_path.exists():
            yield event.chain_result(
                [Plain(message), File(name=zip_path.name, file=str(zip_path))]
            )
        else:
            yield event.plain_result(message)
