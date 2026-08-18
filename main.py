"""AstrBot entry point for the LogVault logging plugin."""

from __future__ import annotations

import asyncio
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
from .core.sensitive_filter import SensitiveFilter


PLUGIN_ID = "astrbot_plugin_logvault"
PLUGIN_VERSION = "2.0.1"
LEGACY_PLUGIN_ID = "astrbot_plugin_logplus"
LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


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
        self._init_task: asyncio.Task | None = None

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

    async def _initialize_plugin(self):
        try:
            config = self.config_manager.as_dict()
            if config.get("enable_sensitive_filter", True):
                self.sensitive_filter = SensitiveFilter(
                    keywords=self.config_manager.get_sensitive_keywords(), enabled=True
                )

            self.log_handler = LogVaultHandler(
                self.data_dir, config, sensitive_filter=self.sensitive_filter
            )
            level_name = str(config.get("log_level", "DEBUG")).upper()
            self.log_handler.setLevel(LOG_LEVELS.get(level_name, logging_level_default()))
            logger.addHandler(self.log_handler)

            self.log_cleaner = LogCleaner(self.data_dir, config)
            await self.log_cleaner.start()
            self.command_handler = CommandHandler(
                self.data_dir,
                self.log_cleaner,
                self._legacy_data_dirs(),
                plugin_catalog_provider=self._installed_plugin_catalog,
            )
            legacy_note = len(self.command_handler.additional_data_dirs)
            logger.info(
                "✅ LogVault 已启动，日志目录: %s%s",
                self.data_dir,
                f"，已发现 {legacy_note} 个旧数据目录" if legacy_note else "",
            )
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
        if self.log_cleaner:
            await self.log_cleaner.stop()
        if self.log_handler:
            logger.removeHandler(self.log_handler)
            self.log_handler.close()
        logger.info("LogVault 已停止")

    @filter.command_group("logvault", alias={"logplus"})
    def logvault(self):
        """LogVault commands; ``logplus`` remains a compatibility alias."""

    @logvault.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看日志状态。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(await self.command_handler.handle_status())

    @logvault.command("search")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索当前及兼容旧目录中的日志。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(await self.command_handler.handle_search(keyword))

    @logvault.command("clean")
    async def cmd_clean(self, event: AstrMessageEvent):
        """手动清理已关闭的旧日志。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(await self.command_handler.handle_clean())

    @logvault.command("export")
    async def cmd_export(self, event: AstrMessageEvent, days: int = 7):
        """导出最近指定天数的日志。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(await self.command_handler.handle_export(days))

    @logvault.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示命令帮助。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return
        yield event.plain_result(self.command_handler.handle_help())

    @logvault.command("send")
    async def cmd_send(
        self,
        event: AstrMessageEvent,
        target: str = "",
        plugin: str = "",
        days: int = 7,
    ):
        """发送最近 N 天的日志；支持 ``plugin NAME DAYS``。"""
        if not self.command_handler:
            yield event.plain_result("❌ 插件尚未初始化完成")
            return

        target_lower = str(target or "").casefold()
        if target_lower == "plugin":
            if not plugin:
                yield event.plain_result(
                    "❌ 用法: /logplus send plugin <插件名> [天数]\n"
                    "   或: /logvault send plugin <插件名> [天数]"
                )
                return
            final_target = plugin
        elif target:
            # The parser passes the second token to ``plugin`` for commands
            # such as ``send all 3``; accept that convenient shorthand too.
            if plugin and str(plugin).isdigit() and days == 7:
                days = int(plugin)
            elif plugin:
                yield event.plain_result("❌ 用法: /logplus send all|errors [天数]")
                return
            final_target = target
        else:
            yield event.plain_result(
                "❌ 用法:\n"
                "  /logplus send all [天数]\n"
                "  /logplus send errors [天数]\n"
                "  /logplus send plugin <插件名> [天数]"
            )
            return

        message, zip_path = await self.command_handler.handle_send(final_target, days)
        if zip_path and zip_path.exists():
            yield event.chain_result(
                [Plain(message), File(name=zip_path.name, file=str(zip_path))]
            )
        else:
            yield event.plain_result(message)


def logging_level_default() -> int:
    """Keep the default level in one small, import-safe helper."""

    return 10
