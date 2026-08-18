"""Non-blocking command operations for LogVault."""

from __future__ import annotations

import asyncio
import gzip
import os
import re
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .log_cleaner import LogCleaner


_LOG_RECORD_START_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}[ T]")


def _is_log_file(path: Path) -> bool:
    name = path.name.casefold()
    name = name.removesuffix(".gz")
    return name.endswith(".log") or ".log." in name


@dataclass
class PluginCatalogEntry:
    """One installed plugin and any log directories that belong to it."""

    name: str
    aliases: set[str] = field(default_factory=set)
    log_dirs: list[tuple[str, Path, Path]] = field(default_factory=list)


PluginCatalogProvider = Callable[[], Mapping[str, Iterable[str]]]


class CommandHandler:
    """Build status/search/export/send responses without blocking the event loop."""

    def __init__(
        self,
        data_dir: Path,
        cleaner: LogCleaner,
        additional_data_dirs: Iterable[Path] | None = None,
        plugin_catalog_provider: PluginCatalogProvider | None = None,
        host_log_dirs: Iterable[Path] | None = None,
        sensitive_filter=None,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.cleaner = cleaner
        self.additional_data_dirs = self._dedupe_dirs(additional_data_dirs or [])
        self.plugin_catalog_provider = plugin_catalog_provider
        self.host_log_dirs = self._dedupe_dirs(host_log_dirs or [], include_missing=True)
        self.sensitive_filter = sensitive_filter

    @staticmethod
    def _dedupe_dirs(
        paths: Iterable[Path], include_missing: bool = False
    ) -> list[Path]:
        result: list[Path] = []
        seen: set[str] = set()
        for value in paths:
            try:
                path = Path(value).expanduser().resolve()
            except (OSError, RuntimeError, TypeError):
                continue
            key = os.path.normcase(str(path))
            if (
                (include_missing or (path.exists() and path.is_dir()))
                and key not in seen
            ):
                seen.add(key)
                result.append(path)
        return result

    def _sources(self) -> Iterator[tuple[str, Path]]:
        yield "current", self.data_dir
        for index, path in enumerate(self.additional_data_dirs, start=1):
            yield f"legacy_{index}_{path.name}", path

    def _plugin_log_dirs(self) -> Iterator[tuple[str, Path, Path]]:
        for label, root in self._sources():
            plugins_dir = root / "plugins"
            if not plugins_dir.is_dir():
                continue
            try:
                plugin_dirs = sorted(
                    (item for item in plugins_dir.iterdir() if item.is_dir()),
                    key=lambda item: item.name.casefold(),
                )
            except OSError:
                continue
            for plugin_dir in plugin_dirs:
                yield label, root, plugin_dir

    def _shared_log_files(self) -> Iterator[tuple[str, Path, Path]]:
        """Yield shared LogVault and host logs for plugin-specific fallback."""

        seen: set[str] = set()
        for label, root in self._sources():
            for category in ("all", "core"):
                for path in self._iter_files(root, root / category):
                    key = os.path.normcase(str(path.resolve()))
                    if key not in seen:
                        seen.add(key)
                        yield label, root, path

        for index, log_dir in enumerate(self.host_log_dirs, start=1):
            if not log_dir.is_dir():
                continue
            try:
                paths = sorted(
                    (item for item in log_dir.rglob("*") if item.is_file()),
                    key=lambda item: item.as_posix().casefold(),
                )
            except OSError:
                continue
            for path in paths:
                if not _is_log_file(path):
                    continue
                key = os.path.normcase(str(path.resolve()))
                if key not in seen:
                    seen.add(key)
                    yield f"host_{index}_{log_dir.name}", log_dir, path

    def _plugin_catalog(self) -> list[PluginCatalogEntry]:
        """Merge AstrBot's installed-plugin registry with existing log dirs."""

        entries: dict[str, PluginCatalogEntry] = {}
        if self.plugin_catalog_provider:
            try:
                registered = self.plugin_catalog_provider()
            except (AttributeError, RuntimeError, TypeError):
                registered = {}
            if isinstance(registered, Mapping):
                for raw_name, raw_aliases in registered.items():
                    name = str(raw_name or "").strip()
                    if not name:
                        continue
                    key = name.casefold()
                    entry = entries.setdefault(key, PluginCatalogEntry(name=name))
                    entry.aliases.add(name)
                    aliases = (
                        [raw_aliases]
                        if isinstance(raw_aliases, str)
                        else raw_aliases or []
                    )
                    entry.aliases.update(
                        str(alias).strip()
                        for alias in aliases
                        if str(alias or "").strip()
                    )

        alias_owners: dict[str, str] = {}
        for key, entry in entries.items():
            for alias in entry.aliases:
                alias_owners.setdefault(alias.casefold(), key)

        for label, root, plugin_dir in self._plugin_log_dirs():
            directory_name = plugin_dir.name
            key = alias_owners.get(directory_name.casefold(), directory_name.casefold())
            entry = entries.setdefault(
                key, PluginCatalogEntry(name=directory_name, aliases={directory_name})
            )
            entry.aliases.add(directory_name)
            entry.log_dirs.append((label, root, plugin_dir))

        return sorted(entries.values(), key=lambda item: item.name.casefold())

    @staticmethod
    def _relative_arcname(source_label: str, root: Path, path: Path) -> str:
        relative = path.relative_to(root).as_posix()
        return relative if source_label == "current" else f"legacy/{source_label}/{relative}"

    @staticmethod
    def _iter_files(root: Path, subdir: Path | None = None) -> Iterator[Path]:
        base = subdir if subdir is not None else root
        if not base.exists() or not base.is_dir():
            return
        try:
            paths = sorted(base.rglob("*"), key=lambda item: item.as_posix().casefold())
        except OSError:
            return
        for path in paths:
            if not path.is_file() or not _is_log_file(path):
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part.casefold() == "exports" for part in relative.parts):
                continue
            yield path

    @staticmethod
    def _valid_days(days: int | str | None, default: int = 7) -> int:
        if days is None or days == "":
            return default
        if isinstance(days, bool):
            raise ValueError("天数必须是正整数")
        try:
            value = int(days)
        except (TypeError, ValueError) as exc:
            raise ValueError("天数必须是正整数") from exc
        if value < 1 or value > 3650:
            raise ValueError("天数必须在 1 到 3650 之间")
        return value

    @staticmethod
    def _cutoff(days: int) -> float:
        return (datetime.now() - timedelta(days=days)).timestamp()

    async def handle_status(self) -> str:
        stats = self.cleaner.get_stats() if self.cleaner else self._stats(self.data_dir)
        lines = [
            "📊 日志状态",
            f"├─ 文件总数: {stats['total_files']}",
            f"├─ 总大小: {stats['total_size_mb']} MB",
            f"├─ 已压缩: {stats['compressed_count']} 个",
        ]
        if stats["oldest_file"]:
            lines.append(f"├─ 最早日志: {stats['oldest_file'].strftime('%Y-%m-%d %H:%M')}")
        if stats["newest_file"]:
            lines.append(f"├─ 最新日志: {stats['newest_file'].strftime('%Y-%m-%d %H:%M')}")

        if self.additional_data_dirs:
            legacy_count = sum(
                1 for _, root in self._sources() if root != self.data_dir for _ in self._iter_files(root)
            )
            lines.append(f"├─ 可读取的旧数据: {legacy_count} 个文件")

        lines.append("└─ 目录统计:")
        for directory, stat in stats["directories"].items():
            size_mb = round(stat["size"] / 1024 / 1024, 2)
            lines.append(f"   ├─ {directory}: {stat['count']} 个, {size_mb} MB")
        return "\n".join(lines)

    @staticmethod
    def _stats(root: Path) -> dict:
        files = list(CommandHandler._iter_files(root))
        sizes = [path.stat().st_size for path in files if path.exists()]
        mtimes = [datetime.fromtimestamp(path.stat().st_mtime) for path in files if path.exists()]
        directories: dict[str, dict[str, int]] = {}
        for path in files:
            try:
                top = path.relative_to(root).parts[0]
            except (ValueError, IndexError):
                top = "root"
            item = directories.setdefault(top, {"count": 0, "size": 0})
            item["count"] += 1
            item["size"] += path.stat().st_size
        return {
            "total_files": len(files),
            "total_size_mb": round(sum(sizes) / 1024 / 1024, 2),
            "compressed_count": sum(1 for path in files if path.name.casefold().endswith(".gz")),
            "directories": directories,
            "oldest_file": min(mtimes, default=None),
            "newest_file": max(mtimes, default=None),
        }

    async def handle_search(self, keyword: str, limit: int = 50) -> str:
        keyword = str(keyword or "").strip()
        if not keyword:
            return "❌ 请提供搜索关键词"
        limit = max(1, min(int(limit), 500))
        results, total = await asyncio.to_thread(self._search_sync, keyword, limit)
        if not results:
            return f"🔍 未找到包含 '{keyword}' 的日志"
        shown = len(results)
        suffix = "（已达到显示上限）" if total > shown else ""
        return f"🔍 搜索 '{keyword}' 结果（显示 {shown}/{total} 条）{suffix}:\n" + "\n".join(results)

    def _search_sync(self, keyword: str, limit: int) -> tuple[list[str], int]:
        needle = keyword.casefold()
        results: list[str] = []
        total = 0
        for label, root in self._sources():
            for path in self._iter_files(root):
                try:
                    opener = gzip.open if path.name.casefold().endswith(".gz") else open
                    with opener(path, "rt", encoding="utf-8", errors="ignore") as stream:
                        for line_number, line in enumerate(stream, start=1):
                            if needle in line.casefold():
                                total += 1
                                if len(results) < limit:
                                    relative = self._relative_arcname(label, root, path)
                                    results.append(f"[{relative}:{line_number}] {line.strip()[:160]}")
                except (OSError, EOFError, UnicodeError):
                    continue
        return results, total

    async def handle_clean(self) -> str:
        if not self.cleaner:
            return "❌ 清理器尚未初始化"
        result = await self.cleaner.cleanup()
        freed_mb = round(result["freed_bytes"] / 1024 / 1024, 2)
        return (
            "🧹 清理完成\n"
            f"├─ 压缩文件: {result['compressed']} 个\n"
            f"├─ 删除文件: {result['deleted']} 个\n"
            f"└─ 释放空间: {freed_mb} MB"
        )

    async def handle_export(self, days: int = 7) -> str:
        try:
            days = self._valid_days(days)
        except ValueError as exc:
            return f"❌ {exc}"

        export_dir = self.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        zip_path = export_dir / self._archive_name("logs_export")
        entries = self._recent_entries(days)
        if not entries:
            return f"❌ 最近 {days} 天没有找到日志文件"
        count = await asyncio.to_thread(self._write_zip, zip_path, entries, f"最近 {days} 天日志")
        size_mb = round(zip_path.stat().st_size / 1024 / 1024, 2)
        return f"📦 导出完成\n├─ 文件: {zip_path}\n├─ 包含: {count} 个日志文件\n└─ 大小: {size_mb} MB"

    def handle_help(self) -> str:
        return (
            "📋 LogVault 命令帮助\n"
            "├─ /logvault status              查看日志状态\n"
            "├─ /logvault search <词>         搜索日志关键词\n"
            "├─ /logvault clean               手动清理旧日志\n"
            "├─ /logvault export [天]         导出最近N天日志（默认7天）\n"
            "├─ /logvault send all [天]       发送最近N天的全部日志\n"
            "├─ /logvault send errors [天]    发送最近N天的错误日志\n"
            "├─ /logvault send plugin <名> [天] 发送指定插件最近N天日志\n"
            "├─ /logplus ...                  兼容别名\n"
            "└─ /logvault help                显示此帮助"
        )

    async def handle_send(
        self, target: str = "", days: int = 7
    ) -> tuple[str, Path | None]:
        target = str(target or "").strip()
        if not target:
            return "❌ 请指定发送目标: all / errors / plugin <插件名> [天数]", None
        try:
            days = self._valid_days(days)
        except ValueError as exc:
            return f"❌ {exc}", None

        target_lower = target.casefold()
        export_dir = self.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = self._archive_name("").removeprefix("_").removesuffix(".zip")

        if target_lower == "all":
            entries = self._recent_entries(days)
            label = f"最近 {days} 天全部日志"
            filename = f"all_logs_{timestamp}.zip"
        elif target_lower == "errors":
            entries = self._recent_entries(days, category="errors")
            label = f"最近 {days} 天错误日志"
            filename = f"error_logs_{timestamp}.zip"
        else:
            entries, plugin_name, error, plugin_aliases = self._plugin_entries(
                target, days
            )
            if error:
                return error, None
            label = f"插件 {plugin_name} 最近 {days} 天日志"
            filename = f"plugin_{plugin_name}_{timestamp}.zip"

        if not entries:
            if target_lower not in {"all", "errors"}:
                shared_entries = self._recent_shared_plugin_entries(
                    days, plugin_aliases
                )
                if shared_entries:
                    zip_path = export_dir / filename
                    count = await asyncio.to_thread(
                        self._write_filtered_plugin_zip,
                        zip_path,
                        shared_entries,
                        plugin_aliases,
                        label,
                        self.sensitive_filter,
                    )
                    if count:
                        size_mb = round(zip_path.stat().st_size / 1024 / 1024, 2)
                        message = (
                            f"📦 插件 {plugin_name} 日志已从共享日志中筛选打包（最近 {days} 天）\n"
                            f"├─ 文件数: {count}\n└─ 大小: {size_mb} MB"
                        )
                        return message, zip_path
                message = (
                    f"❌ 已识别插件 '{plugin_name}'，但最近 {days} 天没有捕获到它的日志文件\n"
                    "旧进程在 LogVault 接入前写出的控制台记录无法回溯；请更新到 2.0.3 并重启 AstrBot 后再产生一条日志。"
                )
                return message, None
            return f"❌ 最近 {days} 天没有找到可发送的日志文件", None

        zip_path = export_dir / filename
        count = await asyncio.to_thread(self._write_zip, zip_path, entries, label)
        size_mb = round(zip_path.stat().st_size / 1024 / 1024, 2)
        if target_lower == "all":
            title = "全部日志"
        elif target_lower == "errors":
            title = "错误日志"
        else:
            title = f"插件 {plugin_name} 日志"
        return f"📦 {title}已打包（最近 {days} 天）\n├─ 文件数: {count}\n└─ 大小: {size_mb} MB", zip_path

    def _recent_entries(self, days: int, category: str | None = None) -> list[tuple[str, Path, Path]]:
        cutoff = self._cutoff(days)
        entries: list[tuple[str, Path, Path]] = []
        for label, root in self._sources():
            base = root / category if category else root
            for path in self._iter_files(root, base):
                try:
                    if path.stat().st_mtime >= cutoff:
                        entries.append((label, root, path))
                except OSError:
                    continue
        return entries

    def _plugin_entries(
        self, keyword: str, days: int
    ) -> tuple[list[tuple[str, Path, Path]], str, str | None, set[str]]:
        needle = keyword.casefold()
        catalog = self._plugin_catalog()
        matches = [
            entry
            for entry in catalog
            if any(needle in alias.casefold() for alias in entry.aliases)
        ]

        if not matches:
            choices = (
                "\n".join(f"  - {entry.name}" for entry in catalog)
                or "  （暂无已识别插件）"
            )
            return [], "", f"❌ 未找到匹配 '{keyword}' 的插件\n可用插件:\n{choices}", set()
        if len(matches) > 1:
            choices = "\n".join(f"  - {entry.name}" for entry in matches)
            return [], "", f"❌ 找到多个匹配的插件，请更具体:\n{choices}", set()

        matched = matches[0]
        plugin_name = matched.name
        cutoff = self._cutoff(days)
        entries: list[tuple[str, Path, Path]] = []
        for label, root, plugin_dir in matched.log_dirs:
            for path in self._iter_files(root, plugin_dir):
                try:
                    if path.stat().st_mtime >= cutoff:
                        entries.append((label, root, path))
                except OSError:
                        continue
        return entries, plugin_name, None, set(matched.aliases)

    @staticmethod
    def _line_matches_plugin(line: str, aliases: set[str]) -> bool:
        line_lower = line.casefold()
        return any(alias.casefold() in line_lower for alias in aliases if alias)

    def _recent_shared_plugin_entries(
        self, days: int, aliases: set[str]
    ) -> list[tuple[str, Path, Path]]:
        cutoff = self._cutoff(days)
        entries: list[tuple[str, Path, Path]] = []
        for label, root, path in self._shared_log_files():
            try:
                if path.stat().st_mtime >= cutoff:
                    entries.append((label, root, path))
            except OSError:
                continue
        return entries

    @classmethod
    def _filtered_log_text(cls, path: Path, aliases: set[str], sensitive_filter=None) -> str:
        opener = gzip.open if path.name.casefold().endswith(".gz") else open
        blocks: list[str] = []
        current: list[str] = []
        matched = False

        def flush() -> None:
            if matched and current:
                blocks.append("".join(current))

        with opener(path, "rt", encoding="utf-8", errors="ignore") as stream:
            for line in stream:
                # AstrBot's backend file may format one record over three lines;
                # a new timestamp starts the next record. LogVault's formatter
                # emits one line per record and works with the same boundary.
                if current and _LOG_RECORD_START_RE.match(line):
                    flush()
                    current = []
                    matched = False
                current.append(line)
                if cls._line_matches_plugin(line, aliases):
                    matched = True
            flush()
        content = "".join(blocks)
        if sensitive_filter and content:
            mask_text = getattr(sensitive_filter, "mask_text", None)
            if callable(mask_text):
                try:
                    content = mask_text(content)
                except (AttributeError, TypeError, ValueError):
                    pass
        return content

    @classmethod
    def _write_filtered_plugin_zip(
        cls,
        zip_path: Path,
        entries: list[tuple[str, Path, Path]],
        aliases: set[str],
        description: str,
        sensitive_filter=None,
    ) -> int:
        count = 0
        seen: set[str] = set()
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "ABOUT.txt",
                    "LogVault\n"
                    f"{description}\n"
                    "来源：共享日志中按插件标识筛选的记录\n"
                    f"Generated: {datetime.now().isoformat(timespec='seconds')}\n",
                )
                for label, root, path in entries:
                    try:
                        key = os.path.normcase(str(path.resolve()))
                        if key in seen:
                            continue
                        content = cls._filtered_log_text(path, aliases, sensitive_filter)
                        if not content:
                            continue
                        relative = path.relative_to(root).as_posix()
                        archive.writestr(f"filtered/{label}/{relative}", content)
                        seen.add(key)
                        count += 1
                    except (OSError, EOFError, UnicodeError, ValueError):
                        continue
        except Exception:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if count == 0:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
        return count

    @staticmethod
    def _archive_name(prefix: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"{prefix}_{stamp}.zip" if prefix else f"_{stamp}.zip"

    @staticmethod
    def _write_zip(
        zip_path: Path,
        entries: list[tuple[str, Path, Path]],
        description: str,
    ) -> int:
        count = 0
        seen: set[str] = set()
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "ABOUT.txt",
                    f"LogVault\n{description}\nGenerated: {datetime.now().isoformat(timespec='seconds')}\n",
                )
                for label, root, path in entries:
                    try:
                        arcname = CommandHandler._relative_arcname(label, root, path)
                        if arcname in seen:
                            continue
                        archive.write(path, arcname)
                        seen.add(arcname)
                        count += 1
                    except (OSError, ValueError):
                        continue
        except Exception:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return count
