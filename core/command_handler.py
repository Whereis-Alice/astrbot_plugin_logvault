"""Non-blocking command operations for LogVault."""

from __future__ import annotations

import asyncio
import gzip
import os
import re
import shutil
import zipfile
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .log_cleaner import LogCleaner
from .log_handler import is_active_log_name


_LOG_RECORD_START_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}[ T]")
_RECORD_TIMESTAMP_RE = re.compile(
    r"^\s*\[(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?"
)
_LEVEL_TOKEN_RE = re.compile(r"\[([A-Za-z]{1,8})\s*\]")
_SHORT_LEVEL_ALIASES = {
    "T": "TRACE",
    "D": "DEBUG",
    "I": "INFO",
    "S": "SUCCESS",
    "W": "WARNING",
    "E": "ERROR",
    "C": "CRITICAL",
}


def _parse_record_time(line: str) -> float | None:
    """Return the POSIX timestamp of a log line, or None when it has none.

    Both LogVault and AstrBot start every record with a bracketed local
    timestamp.  Reading it is what allows "send/export <days>" to filter by
    record age instead of file mtime: an active log file such as all.log is
    appended to constantly, so its mtime is always "now" and mtime filtering
    silently exported the whole history.
    """

    match = _RECORD_TIMESTAMP_RE.match(line)
    if not match:
        return None
    try:
        microseconds = int((match.group(7) or "0").ljust(6, "0")[:6])
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6)),
            microseconds,
        ).timestamp()
    except (OSError, OverflowError, ValueError):
        return None


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


_LEVEL_ORDER = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARN": 30,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

#: Scopes an export request may use.
_EXPORT_SCOPES = ("selection", "category", "preset", "plugin")
#: Ready made presets, mapped to (subdirectory or None, display name).
_EXPORT_PRESETS = {
    "all": (None, "全部日志"),
    "errors": ("errors", "错误日志"),
    "core": ("core", "核心日志"),
}
_EXPORT_FORMATS = ("zip", "merged")
#: Suffixes recognised as generated export bundles.
_EXPORT_SUFFIXES = (".zip", ".txt")


def _parse_boundary(raw, end_of_day: bool = False) -> float | None:
    """Parse a WebUI date/datetime boundary into a POSIX timestamp."""

    text = str(raw or "").strip()
    if not text:
        return None
    normalised = text.replace("/", "-").replace("T", " ").strip()
    for pattern, date_only in (
        ("%Y-%m-%d %H:%M:%S", False),
        ("%Y-%m-%d %H:%M", False),
        ("%Y-%m-%d", True),
    ):
        try:
            moment = datetime.strptime(normalised, pattern)
        except ValueError:
            continue
        if date_only and end_of_day:
            moment = moment.replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
        return moment.timestamp()
    raise ValueError("时间格式无法识别，请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")


def _looks_like_date(raw) -> bool:
    return bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", str(raw or "").strip()))


@dataclass
class ExportSpec:
    """A normalised export request, shared by the WebUI and /log export."""

    scope: str = "preset"
    preset: str = "all"
    ids: tuple[str, ...] = ()
    source: str = ""
    category: str = ""
    plugin: str = ""
    days: int | None = 7
    since: float | None = None
    until: float | None = None
    levels: tuple[str, ...] = ()
    keyword: str = ""
    fmt: str = "zip"
    mask: bool = True
    prefix: str = "logvault_export"
    title: str = ""

    #: Hard ceiling on the ids one request may carry.
    MAX_IDS = 500

    @property
    def content_filtered(self) -> bool:
        """True when member bodies must be rewritten instead of copied."""

        return bool(self.levels or self.keyword)

    @classmethod
    def from_payload(cls, payload: Mapping | None, default_mask: bool = True):
        """Validate one JSON body, raising ValueError with a Chinese hint."""

        data = payload if isinstance(payload, Mapping) else {}
        scope = str(data.get("scope") or "preset").strip().casefold()
        if scope not in _EXPORT_SCOPES:
            raise ValueError("导出范围无效")

        raw_ids = data.get("ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, (list, tuple)):
            raise ValueError("ids 必须是数组")
        ids = tuple(str(item) for item in raw_ids if str(item or "").strip())
        if len(ids) > cls.MAX_IDS:
            raise ValueError(f"一次最多导出 {cls.MAX_IDS} 个文件")

        preset = str(data.get("preset") or "all").strip().casefold()
        if preset not in _EXPORT_PRESETS:
            raise ValueError("预设范围无效")

        since = _parse_boundary(data.get("since"))
        until = _parse_boundary(data.get("until"), end_of_day=True)
        if since is not None and until is not None and until < since:
            raise ValueError("结束时间不能早于开始时间")

        days = cls._parse_days(data.get("days"))

        raw_levels = data.get("levels") or []
        if isinstance(raw_levels, str):
            raw_levels = [raw_levels]
        levels: list[str] = []
        for item in raw_levels:
            token = str(item or "").strip().upper()
            if not token:
                continue
            if token not in _LEVEL_ORDER:
                raise ValueError(f"未知日志级别: {token}")
            if token not in levels:
                levels.append(token)

        keyword = str(data.get("keyword") or "").strip()[:200]
        fmt = str(data.get("format") or data.get("fmt") or "zip").strip().casefold()
        if fmt not in _EXPORT_FORMATS:
            raise ValueError("导出格式无效")

        raw_mask = data.get("mask")
        mask = bool(default_mask) if raw_mask is None else cls._parse_bool(raw_mask)

        return cls(
            scope=scope,
            preset=preset,
            ids=ids,
            source=str(data.get("source") or "").strip(),
            category=str(data.get("category") or "").strip(),
            plugin=str(data.get("plugin") or "").strip(),
            days=days,
            since=since,
            until=until,
            levels=tuple(levels),
            keyword=keyword,
            fmt=fmt,
            mask=mask,
        )

    @staticmethod
    def _parse_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        token = str(value or "").strip().casefold()
        return token in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_days(value) -> int | None:
        """Return the day window, or None for "no limit"."""

        if value is None:
            return 7
        if isinstance(value, bool):
            raise ValueError("天数必须是正整数")
        token = str(value).strip().casefold()
        if token == "":
            return 7
        if token in {"0", "all", "any", "unlimited", "不限", "全部"}:
            return None
        try:
            days = int(float(token))
        except (TypeError, ValueError) as exc:
            raise ValueError("天数必须是正整数") from exc
        if days < 1 or days > 3650:
            raise ValueError("天数必须在 1 到 3650 之间")
        return days


@dataclass
class ExportPlan:
    """Concrete files plus the effective window of one export request."""

    spec: ExportSpec
    entries: list[tuple[str, Path, Path]] = field(default_factory=list)
    title: str = ""
    aliases: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    since: float | None = None
    until: float | None = None
    slice_enabled: bool = True

    @property
    def window_filtered(self) -> bool:
        return self.slice_enabled and (self.since is not None or self.until is not None)


class CommandHandler:
    """Build status/search/export/send responses without blocking the event loop."""

    #: Separator used by the WebUI file identifiers, "<source>::<relative path>".
    FILE_ID_SEPARATOR = "::"
    #: Pseudo category that means "every file of this source".
    ALL_CATEGORY = "__all__"
    #: Lines inspected when looking for the first timestamp of a file.
    MAX_HEAD_SCAN_LINES = 200
    #: Hard ceiling for a single WebUI read so a huge file cannot stall a request.
    MAX_SCAN_LINES = 200_000
    #: Severity ranking shared by LogVault and AstrBot level names.
    LEVEL_ORDER = _LEVEL_ORDER
    #: Suffixes of the bundles produced under data/exports.
    EXPORT_SUFFIXES = _EXPORT_SUFFIXES

    def __init__(
        self,
        data_dir: Path,
        cleaner: LogCleaner,
        additional_data_dirs: Iterable[Path] | None = None,
        plugin_catalog_provider: PluginCatalogProvider | None = None,
        host_log_dirs: Iterable[Path] | None = None,
        sensitive_filter=None,
        slice_by_record_time: bool = True,
        mask_on_export: bool = True,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.cleaner = cleaner
        self.additional_data_dirs = self._dedupe_dirs(additional_data_dirs or [])
        self.plugin_catalog_provider = plugin_catalog_provider
        self.host_log_dirs = self._dedupe_dirs(host_log_dirs or [], include_missing=True)
        self.sensitive_filter = sensitive_filter
        self.slice_by_record_time = bool(slice_by_record_time)
        self.mask_on_export = bool(mask_on_export)

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

    async def handle_export(
        self, days: int | str | None = "", until: str = ""
    ) -> str:
        """Export recent logs, accepting a day count or a start date.

        ``/log export 2`` keeps the historical meaning, while
        ``/log export 2026-08-01 2026-08-20`` exports an explicit window and
        ``/log export 0`` drops the limit entirely.
        """

        raw = str("" if days is None else days).strip()
        payload: dict[str, object] = {"scope": "preset", "preset": "all"}
        if _looks_like_date(raw):
            payload["since"] = raw
            payload["days"] = 0
        else:
            payload["days"] = raw
        if str(until or "").strip():
            payload["until"] = until
        try:
            spec = ExportSpec.from_payload(payload, default_mask=self.mask_on_export)
        except ValueError as exc:
            return f"❌ {exc}"
        spec.prefix = "logs_export"
        spec.title = self._export_window_title(spec)
        try:
            result = await asyncio.to_thread(self.build_export, spec)
        except ValueError as exc:
            return f"❌ {exc}"
        size_mb = round(result["bytes"] / 1024 / 1024, 2)
        lines = [
            "📦 导出完成",
            f"├─ 范围: {spec.title}",
            f"├─ 文件: {result['path']}",
            f"├─ 包含: {result['files']} 个日志文件",
            f"└─ 大小: {size_mb} MB",
        ]
        return "\n".join(lines)

    def handle_help(self) -> str:
        return (
            "📋 LogVault 命令帮助\n"
            "├─ /log status              查看日志状态\n"
            "├─ /log search <词>         搜索日志关键词\n"
            "├─ /log clean               手动清理旧日志\n"
            "├─ /log export [天]         导出最近N天日志（默认7天，0=不限）\n"
            "├─ /log export <起> [止]    按日期导出，如 2026-08-01 2026-08-20\n"
            "├─ /log send all [天]       发送最近N天的全部日志\n"
            "├─ /log send errors [天]    发送最近N天的错误日志\n"
            "├─ /log send plugin <名> [天] 发送指定插件最近N天日志\n"
            "├─ /logvault、/logplus ...  兼容别名\n"
            "└─ /log help                显示此帮助\n"
            "更细的范围、级别、关键词与格式请用面板的「导出」页签。"
        )

    async def handle_send(
        self, target: str = "", days: int | str | None = 7
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
                        self._cutoff(days) if self.slice_by_record_time else None,
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
        count = await asyncio.to_thread(
            self._write_zip,
            zip_path,
            entries,
            label,
            self._cutoff(days),
            self.slice_by_record_time,
            self.masker(self.mask_on_export),
        )
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
        return self._plugin_entries_at(keyword, self._cutoff(days))

    def _plugin_entries_at(
        self, keyword: str, cutoff: float | None
    ) -> tuple[list[tuple[str, Path, Path]], str, str | None, set[str]]:
        """Resolve one plugin and its log files newer than *cutoff*.

        A *cutoff* of None means "no time limit", which is what the WebUI
        exporter uses when the user picks an unlimited window.
        """

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
        entries: list[tuple[str, Path, Path]] = []
        for label, root, plugin_dir in matched.log_dirs:
            for path in self._iter_files(root, plugin_dir):
                try:
                    if cutoff is None or path.stat().st_mtime >= cutoff:
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
        return self._recent_shared_plugin_entries_at(self._cutoff(days))

    def _recent_shared_plugin_entries_at(
        self, cutoff: float | None
    ) -> list[tuple[str, Path, Path]]:
        """Shared logs (all.log and friends) newer than *cutoff*."""

        entries: list[tuple[str, Path, Path]] = []
        for label, root, path in self._shared_log_files():
            try:
                if cutoff is None or path.stat().st_mtime >= cutoff:
                    entries.append((label, root, path))
            except OSError:
                continue
        return entries

    @classmethod
    def _filtered_log_text(
        cls,
        path: Path,
        aliases: set[str],
        sensitive_filter=None,
        cutoff: float | None = None,
    ) -> str:
        blocks: list[str] = []
        current: list[str] = []
        matched = False
        recent = True

        def flush() -> None:
            if matched and recent and current:
                blocks.append("".join(current))

        with cls._open_text(path) as stream:
            for line in stream:
                # AstrBot's backend file may format one record over three lines;
                # a new timestamp starts the next record. LogVault's formatter
                # emits one line per record and works with the same boundary.
                if current and _LOG_RECORD_START_RE.match(line):
                    flush()
                    current = []
                    matched = False
                    recent = True
                if not current and cutoff is not None:
                    # Keep records without a parsable timestamp: dropping them
                    # would hide logs written in an unexpected format.
                    stamp = _parse_record_time(line)
                    recent = stamp is None or stamp >= cutoff
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
        cutoff: float | None = None,
    ) -> int:
        count = 0
        seen: set[str] = set()
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                started = (
                    datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M:%S")
                    if cutoff is not None
                    else "不限"
                )
                archive.writestr(
                    "ABOUT.txt",
                    "LogVault\n"
                    f"{description}\n"
                    "来源：共享日志中按插件标识筛选的记录\n"
                    f"记录起点: {started}\n"
                    f"Generated: {datetime.now().isoformat(timespec='seconds')}\n",
                )
                for label, root, path in entries:
                    try:
                        key = os.path.normcase(str(path.resolve()))
                        if key in seen:
                            continue
                        content = cls._filtered_log_text(
                            path, aliases, sensitive_filter, cutoff
                        )
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

    @classmethod
    def _write_zip(
        cls,
        zip_path: Path,
        entries: list[tuple[str, Path, Path]],
        description: str,
        cutoff: float | None = None,
        slice_enabled: bool = True,
        masker=None,
    ) -> int:
        """Archive log files, trimming records older than *cutoff*.

        A file whose oldest parsable record is already newer than the cutoff is
        stored byte-for-byte, which keeps rotated .gz members intact.  A file
        that spans the cutoff is rewritten with only the matching records.  A
        file without any parsable timestamp is kept in full so that unusual
        formats are never silently dropped.

        When *masker* is given every member is decoded and rewritten, because
        a byte-for-byte copy would otherwise leak the secrets that the
        sensitive filter is supposed to hide.
        """

        count = 0
        seen: set[str] = set()
        slicing = bool(slice_enabled) and cutoff is not None
        boundary = float(cutoff) if cutoff is not None else 0.0
        about = [
            "LogVault",
            description,
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        ]
        if slicing:
            started = datetime.fromtimestamp(boundary).strftime("%Y-%m-%d %H:%M:%S")
            about.append(f"记录起点: {started}")
            about.append(
                "说明：跨越起点的日志文件已按记录时间裁剪；无法解析时间戳的文件保持完整。"
            )
        about.append("敏感信息: 已脱敏" if masker is not None else "敏感信息: 未脱敏")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("ABOUT.txt", "\n".join(about) + "\n")
                for label, root, path in entries:
                    try:
                        arcname = cls._relative_arcname(label, root, path)
                        if arcname in seen:
                            continue
                        text: str | None = None
                        if slicing:
                            oldest = cls._oldest_record_time(path)
                            if oldest is not None and oldest < boundary:
                                text = cls._slice_log_text(path, boundary)
                                if not text.strip():
                                    seen.add(arcname)
                                    continue
                        if masker is not None:
                            if text is None:
                                with cls._open_text(path) as stream:
                                    text = stream.read()
                            try:
                                text = masker(text)
                            except (AttributeError, TypeError, ValueError):
                                pass
                            if not text.strip():
                                seen.add(arcname)
                                continue
                        if text is None:
                            archive.write(path, arcname)
                        else:
                            archive.writestr(cls._sliced_arcname(arcname), text)
                        seen.add(arcname)
                        count += 1
                    except (OSError, EOFError, UnicodeError, ValueError):
                        continue
        except Exception:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return count

    @staticmethod
    def _open_text(path: Path):
        opener = gzip.open if path.name.casefold().endswith(".gz") else open
        return opener(path, "rt", encoding="utf-8", errors="ignore")

    @classmethod
    def _oldest_record_time(cls, path: Path) -> float | None:
        """Return the timestamp of the first parsable record near the top."""

        try:
            with cls._open_text(path) as stream:
                for index, line in enumerate(stream):
                    if index >= cls.MAX_HEAD_SCAN_LINES:
                        break
                    stamp = _parse_record_time(line)
                    if stamp is not None:
                        return stamp
        except (OSError, EOFError, UnicodeError, ValueError):
            return None
        return None

    @classmethod
    def _slice_log_text(cls, path: Path, cutoff: float) -> str:
        """Keep only the records at or after *cutoff*, traceback lines included."""

        kept: list[str] = []
        include = True
        with cls._open_text(path) as stream:
            for line in stream:
                if _LOG_RECORD_START_RE.match(line):
                    stamp = _parse_record_time(line)
                    include = True if stamp is None else stamp >= cutoff
                if include:
                    kept.append(line)
        return "".join(kept)

    @staticmethod
    def _sliced_arcname(arcname: str) -> str:
        """Sliced members are stored as plain text, so drop the .gz suffix."""

        return arcname[:-3] if arcname.casefold().endswith(".gz") else arcname

    # ------------------------------------------------------------------
    # Export kernel
    #
    # Selecting files and rendering them are deliberately separate so the
    # WebUI can pre-flight a request without writing anything, and so
    # sensitive-value masking happens on exactly one code path.
    # ------------------------------------------------------------------

    def export_dir(self) -> Path:
        directory = self.data_dir / "exports"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def masker(self, enabled: bool = True):
        """Return a callable that masks secrets, or None when disabled."""

        if not enabled or not self.sensitive_filter:
            return None
        mask_text = getattr(self.sensitive_filter, "mask_text", None)
        return mask_text if callable(mask_text) else None

    def _export_window(self, spec: ExportSpec) -> tuple[float | None, float | None]:
        """Resolve the effective (since, until) timestamps of *spec*."""

        since = spec.since
        if since is None and spec.days:
            since = self._cutoff(spec.days)
        return since, spec.until

    def plan_for(self, spec: ExportSpec) -> ExportPlan:
        """Turn *spec* into the concrete files it covers."""

        since, until = self._export_window(spec)
        plan = ExportPlan(
            spec=spec,
            since=since,
            until=until,
            slice_enabled=self.slice_by_record_time,
        )
        if spec.scope == "selection":
            self._collect_selection(plan)
        elif spec.scope == "category":
            self._collect_category(plan)
        elif spec.scope == "plugin":
            self._collect_plugin(plan)
        else:
            self._collect_preset(plan)
        if spec.title:
            plan.title = spec.title
        return plan

    def _collect_selection(self, plan: ExportPlan) -> None:
        if not plan.spec.ids:
            raise ValueError("请先选择要导出的日志文件")
        seen: set[str] = set()
        for file_id in plan.spec.ids:
            resolved = self.resolve_file(file_id)
            if resolved is None:
                plan.warnings.append(f"已跳过无效或越界的文件标识: {file_id}")
                continue
            label, root, path = resolved
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            plan.entries.append((label, root, path))
        plan.title = f"所选 {len(plan.entries)} 个日志文件"

    def _collect_category(self, plan: ExportPlan) -> None:
        wanted = plan.spec.category or self.ALL_CATEGORY
        wanted_source = plan.spec.source or None
        display = ""
        for label, root in self._browse_sources():
            if wanted_source and label != wanted_source:
                continue
            for path in self._iter_files(root):
                key, name, _kind = self._categorize(root, path)
                if wanted != self.ALL_CATEGORY and key != wanted:
                    continue
                display = display or name
                plan.entries.append((label, root, path))
        self._apply_mtime_prefilter(plan)
        if wanted == self.ALL_CATEGORY:
            plan.title = "全部分类" if not wanted_source else f"来源 {wanted_source} 的全部日志"
        else:
            plan.title = f"分类 {display or wanted}"

    def _collect_preset(self, plan: ExportPlan) -> None:
        subdir, name = _EXPORT_PRESETS[plan.spec.preset]
        for label, root in self._sources():
            base = root / subdir if subdir else root
            for path in self._iter_files(root, base):
                plan.entries.append((label, root, path))
        self._apply_mtime_prefilter(plan)
        plan.title = name

    def _collect_plugin(self, plan: ExportPlan) -> None:
        keyword = plan.spec.plugin.strip()
        if not keyword:
            raise ValueError("请提供插件名")
        entries, name, error, aliases = self._plugin_entries_at(keyword, plan.since)
        if error:
            raise ValueError(error.lstrip("❌ "))
        plan.title = f"插件 {name} 日志"
        if entries:
            plan.entries.extend(entries)
            return
        # No dedicated directory yet: fall back to filtering the shared
        # logs by the plugin identifier, exactly like /log send does.
        shared = self._recent_shared_plugin_entries_at(plan.since)
        if not shared:
            raise ValueError(f"插件 {name} 在所选时间范围内没有日志")
        plan.entries.extend(shared)
        plan.aliases = tuple(sorted(aliases))
        plan.title = f"插件 {name} 日志（自共享日志筛选）"

    def _apply_mtime_prefilter(self, plan: ExportPlan) -> None:
        """Drop files that cannot contain a record inside the window.

        Only a cheap stat() is used here; the exact trimming happens while
        the member is rendered.
        """

        if plan.since is None:
            return
        kept: list[tuple[str, Path, Path]] = []
        for label, root, path in plan.entries:
            try:
                if path.stat().st_mtime >= plan.since:
                    kept.append((label, root, path))
            except OSError:
                continue
        plan.entries = kept

    # -- rendering ------------------------------------------------------

    @classmethod
    def _record_blocks(cls, path: Path) -> Iterator[tuple[float | None, str]]:
        """Yield (timestamp, text) per log record, continuations included.

        AstrBot may spread one record over several lines (tracebacks, or its
        three-line backend format); a new bracketed timestamp starts the next
        record.  Blocks without a parsable timestamp yield None so callers can
        decide to keep them instead of silently dropping unknown formats.
        """

        current: list[str] = []
        stamp: float | None = None
        with cls._open_text(path) as stream:
            for line in stream:
                if current and _LOG_RECORD_START_RE.match(line):
                    yield stamp, "".join(current)
                    current = []
                    stamp = None
                if not current:
                    stamp = _parse_record_time(line)
                current.append(line)
        if current:
            yield stamp, "".join(current)

    def _will_trim(self, plan: ExportPlan, path: Path) -> bool:
        """Whether the active filters will actually drop records from *path*."""

        if plan.aliases or plan.spec.content_filtered:
            return True
        if not plan.window_filtered:
            return False
        if plan.until is not None:
            # The newest record is unknown without reading the file.
            return True
        oldest = self._oldest_record_time(path)
        return oldest is not None and plan.since is not None and oldest < plan.since

    def _needs_rewrite(self, plan: ExportPlan, path: Path, masker) -> bool:
        """Whether a member has to be re-rendered instead of copied."""

        return masker is not None or self._will_trim(plan, path)

    def _render_member(self, plan: ExportPlan, path: Path, masker) -> tuple[str, int]:
        """Return the filtered text of one member plus its kept record count."""

        allowed = {
            _LEVEL_ORDER[name] for name in plan.spec.levels if name in _LEVEL_ORDER
        }
        needle = plan.spec.keyword.casefold() or None
        aliases = set(plan.aliases)
        window = plan.window_filtered
        kept: list[str] = []
        for stamp, block in self._record_blocks(path):
            if window and stamp is not None:
                if plan.since is not None and stamp < plan.since:
                    continue
                if plan.until is not None and stamp > plan.until:
                    continue
            if allowed:
                level = self._line_level(block)
                if level is not None and _LEVEL_ORDER.get(level, 0) not in allowed:
                    continue
            if aliases and not self._line_matches_plugin(block, aliases):
                continue
            if needle and needle not in block.casefold():
                continue
            kept.append(block)
        text = "".join(kept)
        if masker is not None and text:
            try:
                text = masker(text)
            except (AttributeError, TypeError, ValueError):
                pass
        return text, len(kept)

    # -- writing --------------------------------------------------------

    @staticmethod
    def _export_stamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _export_slug(self, plan: ExportPlan) -> str:
        """Build a safe, collision free file name for the bundle."""

        prefix = re.sub(r"[^0-9A-Za-z_\-]+", "_", plan.spec.prefix or "")
        prefix = prefix.strip("_")[:60] or "logvault_export"
        suffix = ".txt" if plan.spec.fmt == "merged" else ".zip"
        return f"{prefix}_{self._export_stamp()}{suffix}"

    @staticmethod
    def _export_window_title(spec: ExportSpec) -> str:
        """Describe the time window of *spec* for humans."""

        if spec.since is not None or spec.until is not None:
            start = (
                datetime.fromtimestamp(spec.since).strftime("%Y-%m-%d")
                if spec.since is not None
                else "最早"
            )
            end = (
                datetime.fromtimestamp(spec.until).strftime("%Y-%m-%d")
                if spec.until is not None
                else "至今"
            )
            return f"{start} ~ {end} 日志"
        if spec.days:
            return f"最近 {spec.days} 天日志"
        return "全部日志"

    def _export_about(
        self, plan: ExportPlan, members: int, rewritten: int, masker
    ) -> list[str]:
        """Describe what a bundle holds and how it was filtered."""

        spec = plan.spec
        lines = [
            "LogVault 日志导出",
            plan.title or "日志导出",
            f"生成时间: {datetime.now().isoformat(timespec='seconds')}",
            f"文件数: {members}",
        ]
        if plan.since is not None or plan.until is not None:
            start = (
                datetime.fromtimestamp(plan.since).strftime("%Y-%m-%d %H:%M:%S")
                if plan.since is not None
                else "最早"
            )
            end = (
                datetime.fromtimestamp(plan.until).strftime("%Y-%m-%d %H:%M:%S")
                if plan.until is not None
                else "至今"
            )
            lines.append(f"时间范围: {start} ~ {end}")
        else:
            lines.append("时间范围: 不限")
        if spec.levels:
            lines.append(f"级别过滤: {', '.join(spec.levels)}")
        if spec.keyword:
            lines.append(f"关键词过滤: {spec.keyword}")
        if plan.aliases:
            lines.append(f"插件标识过滤: {', '.join(plan.aliases)}")
        lines.append("敏感信息: 已脱敏" if masker is not None else "敏感信息: 未脱敏")
        if rewritten:
            lines.append(f"按条件重写: {rewritten} 个文件（其余按原样保留）")
        lines.extend(f"提示: {warning}" for warning in plan.warnings)
        return lines

    def _export_members(self, plan: ExportPlan) -> Iterator[tuple[str, Path]]:
        """Yield (arcname, path) pairs of *plan*, duplicates removed."""

        seen: set[str] = set()
        for label, root, path in plan.entries:
            try:
                arcname = self._relative_arcname(label, root, path)
            except ValueError:
                continue
            if arcname in seen:
                continue
            seen.add(arcname)
            yield arcname, path

    def _write_export_zip(self, path: Path, plan: ExportPlan, masker) -> dict:
        """Write a ZIP bundle, rewriting only the members that need it."""

        count = 0
        rewritten = 0
        records = 0
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for arcname, source in self._export_members(plan):
                try:
                    if self._needs_rewrite(plan, source, masker):
                        text, kept = self._render_member(plan, source, masker)
                        if not text.strip():
                            continue
                        archive.writestr(self._sliced_arcname(arcname), text)
                        rewritten += 1
                        records += kept
                    else:
                        archive.write(source, arcname)
                except (OSError, EOFError, UnicodeError, ValueError):
                    plan.warnings.append(f"读取失败已跳过: {arcname}")
                    continue
                count += 1
            archive.writestr(
                "ABOUT.txt",
                "\n".join(self._export_about(plan, count, rewritten, masker)) + "\n",
            )
        return {"files": count, "rewritten": rewritten, "records": records}

    def _write_merged(self, path: Path, plan: ExportPlan, masker) -> dict:
        """Write one flat text file: header first, body streamed from a temp.

        The body is buffered on disk rather than in memory because a
        merged export of an unlimited window can be hundreds of MB.
        """

        count = 0
        records = 0
        body = path.with_name(path.name + ".part")
        try:
            with body.open("w", encoding="utf-8", newline="\n") as stream:
                for arcname, source in self._export_members(plan):
                    try:
                        text, kept = self._render_member(plan, source, masker)
                    except (OSError, EOFError, UnicodeError, ValueError):
                        plan.warnings.append(f"读取失败已跳过: {arcname}")
                        continue
                    if not text.strip():
                        continue
                    stream.write(f"\n===== {arcname} =====\n")
                    stream.write(text if text.endswith("\n") else text + "\n")
                    count += 1
                    records += kept
            header = "\n".join(self._export_about(plan, count, 0, masker))
            with path.open("w", encoding="utf-8", newline="\n") as target:
                target.write(header + "\n")
                with body.open("r", encoding="utf-8") as buffered:
                    shutil.copyfileobj(buffered, target)
        finally:
            try:
                body.unlink(missing_ok=True)
            except OSError:
                pass
        return {"files": count, "rewritten": count, "records": records}

    def build_export(self, spec: ExportSpec) -> dict:
        """Write one bundle under data/exports and describe the result."""

        plan = self.plan_for(spec)
        if not plan.entries:
            raise ValueError("所选范围内没有可导出的日志文件")
        masker = self.masker(spec.mask)
        path = self.export_dir() / self._export_slug(plan)
        if path.exists():
            # Two exports inside the same second must not clobber each other.
            stem, suffix = path.stem, path.suffix
            for index in range(2, 100):
                candidate = path.with_name(f"{stem}_{index}{suffix}")
                if not candidate.exists():
                    path = candidate
                    break
        try:
            if spec.fmt == "merged":
                result = self._write_merged(path, plan, masker)
            else:
                result = self._write_export_zip(path, plan, masker)
        except Exception:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if not result["files"]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ValueError("过滤条件过严，没有任何日志记录符合条件")
        result.update(
            name=path.name,
            path=path,
            bytes=path.stat().st_size,
            title=plan.title,
            format=spec.fmt,
            mask=masker is not None,
            warnings=list(plan.warnings),
            since=plan.since,
            until=plan.until,
        )
        return result

    def plan_export(self, spec: ExportSpec) -> dict:
        """Pre-flight an export request without writing anything."""

        plan = self.plan_for(spec)
        masker = self.masker(spec.mask)
        files = 0
        total = 0
        trimmed = 0
        for _arcname, source in self._export_members(plan):
            try:
                total += source.stat().st_size
            except OSError:
                continue
            files += 1
            if self._will_trim(plan, source):
                trimmed += 1
        return {
            "files": files,
            "bytes": total,
            "trimmed": trimmed,
            "title": plan.title,
            "format": spec.fmt,
            "mask": masker is not None,
            "masking_available": self.masker(True) is not None,
            "levels": list(spec.levels),
            "keyword": spec.keyword,
            "since": plan.since,
            "until": plan.until,
            "warnings": list(plan.warnings),
        }

    # -- history --------------------------------------------------------

    def list_exports(self) -> list[dict]:
        """List the bundles kept under data/exports, newest first."""

        items: list[dict] = []
        directory = self.export_dir()
        try:
            candidates = list(directory.iterdir())
        except OSError:
            return items
        for path in candidates:
            if not path.is_file():
                continue
            suffix = path.suffix.casefold()
            if suffix not in self.EXPORT_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "format": "merged" if suffix == ".txt" else "zip",
                }
            )
        items.sort(key=lambda item: item["mtime"], reverse=True)
        return items

    def resolve_export(self, name: str) -> Path | None:
        """Resolve a bundle name, refusing anything outside data/exports."""

        token = str(name or "").strip()
        if not token or token in {".", ".."}:
            return None
        if "/" in token or "\\" in token or Path(token).name != token:
            return None
        directory = self.export_dir().resolve()
        try:
            resolved = (directory / token).resolve()
        except OSError:
            return None
        if resolved.parent != directory:
            return None
        if resolved.suffix.casefold() not in self.EXPORT_SUFFIXES:
            return None
        return resolved if resolved.is_file() else None

    def delete_exports(self, names: Iterable[str] = (), purge_all: bool = False) -> dict:
        """Delete named bundles, or every bundle when *purge_all* is set."""

        targets: list[Path] = []
        skipped = 0
        if purge_all:
            directory = self.export_dir()
            targets = [directory / item["name"] for item in self.list_exports()]
        else:
            for name in names or ():
                resolved = self.resolve_export(name)
                if resolved is None:
                    skipped += 1
                    continue
                targets.append(resolved)
        deleted = 0
        freed = 0
        failed = 0
        for path in targets:
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                failed += 1
                continue
            deleted += 1
            freed += size
        return {
            "deleted": deleted,
            "freed_bytes": freed,
            "skipped": skipped,
            "failed": failed,
        }

    # ------------------------------------------------------------------
    # WebUI support
    #
    # The dashboard page needs to browse, read, tail, download and delete
    # log files.  All of it goes through the helpers below so that path
    # handling and sensitive-value masking stay in one place.
    # ------------------------------------------------------------------

    def _browse_sources(self) -> list[tuple[str, Path]]:
        """Every root the WebUI may browse: current, legacy and host dirs."""

        sources = list(self._sources())
        for index, log_dir in enumerate(self.host_log_dirs, start=1):
            if log_dir.is_dir():
                sources.append((f"host_{index}_{log_dir.name}", log_dir))
        return sources

    def _source_root(self, label: str) -> Path | None:
        for candidate, root in self._browse_sources():
            if candidate == label:
                return root
        return None

    @staticmethod
    def _source_kind(label: str) -> str:
        if label == "current":
            return "current"
        if label.startswith("host_"):
            return "host"
        return "legacy"

    @classmethod
    def make_file_id(cls, label: str, root: Path, path: Path) -> str:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        return f"{label}{cls.FILE_ID_SEPARATOR}{relative}"

    def resolve_file(self, file_id: str) -> tuple[str, Path, Path] | None:
        """Map a WebUI file id back to a real log file, refusing escapes."""

        raw = str(file_id or "")
        if self.FILE_ID_SEPARATOR not in raw:
            return None
        label, _, relative = raw.partition(self.FILE_ID_SEPARATOR)
        root = self._source_root(label.strip())
        if root is None:
            return None
        segments = relative.replace("\\", "/").split("/")
        if not segments or any(segment in ("", ".", "..") for segment in segments):
            return None
        try:
            root_resolved = root.resolve()
            target = (root_resolved / Path(*segments)).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        if not target.is_relative_to(root_resolved):
            return None
        if not target.is_file() or not _is_log_file(target):
            return None
        return label.strip(), root_resolved, target

    @classmethod
    def _line_level(cls, line: str) -> str | None:
        """Best-effort level of a log line, from the first known level token."""

        for match in _LEVEL_TOKEN_RE.finditer(line[:160]):
            token = match.group(1).upper()
            if token in cls.LEVEL_ORDER:
                return token
            alias = _SHORT_LEVEL_ALIASES.get(token)
            if alias:
                return alias
        return None

    @staticmethod
    def _categorize(root: Path, path: Path) -> tuple[str, str, str]:
        """Return (category key, display name, kind) for one log file."""

        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = (path.name,)
        top = parts[0].casefold() if parts else ""
        if top == "plugins" and len(parts) >= 2:
            return f"plugins/{parts[1]}", parts[1], "plugin"
        builtin = {"all": "汇总日志", "core": "核心日志", "errors": "错误日志"}
        if top in builtin:
            return top, builtin[top], "builtin"
        return "other", "其他日志", "other"

    def list_categories(self) -> list[dict]:
        """Group every readable log file into a browsable category tree."""

        groups: dict[tuple[str, str], dict] = {}

        def bucket(source: str, key: str, name: str, kind: str) -> dict:
            entry = groups.get((source, key))
            if entry is None:
                entry = {
                    "source": source,
                    "source_kind": self._source_kind(source),
                    "key": key,
                    "name": name,
                    "kind": kind,
                    "count": 0,
                    "size": 0,
                    "mtime": 0.0,
                }
                groups[(source, key)] = entry
            return entry

        for label, root in self._browse_sources():
            bucket(label, self.ALL_CATEGORY, "全部", "all")
            for path in self._iter_files(root):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key, name, kind = self._categorize(root, path)
                targets = [
                    bucket(label, self.ALL_CATEGORY, "全部", "all"),
                    bucket(label, key, name, kind),
                ]
                for entry in targets:
                    entry["count"] += 1
                    entry["size"] += stat.st_size
                    entry["mtime"] = max(entry["mtime"], stat.st_mtime)

        kind_order = {"all": 0, "builtin": 1, "plugin": 2, "other": 3}
        return sorted(
            groups.values(),
            key=lambda item: (
                0 if item["source"] == "current" else 1,
                item["source"],
                kind_order.get(item["kind"], 9),
                item["name"].casefold(),
            ),
        )

    def list_files(
        self, source: str | None = None, category: str | None = None
    ) -> list[dict]:
        wanted_source = (source or "").strip() or None
        wanted_category = (category or "").strip() or None
        files: list[dict] = []
        for label, root in self._browse_sources():
            if wanted_source and label != wanted_source:
                continue
            for path in self._iter_files(root):
                key, name, kind = self._categorize(root, path)
                if (
                    wanted_category
                    and wanted_category != self.ALL_CATEGORY
                    and key != wanted_category
                ):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    relative = path.name
                lowered = path.name.casefold()
                active = is_active_log_name(lowered)
                files.append(
                    {
                        "id": self.make_file_id(label, root, path),
                        "name": path.name,
                        "relative": relative,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "compressed": lowered.endswith(".gz"),
                        "active": active,
                        "source": label,
                        "source_kind": self._source_kind(label),
                        "category": key,
                        "category_name": name,
                        "category_kind": kind,
                        "deletable": label == "current" and not active,
                    }
                )
        files.sort(key=lambda item: item["mtime"], reverse=True)
        return files

    def _mask(self, text: str) -> str:
        if not text or not self.sensitive_filter:
            return text
        mask_text = getattr(self.sensitive_filter, "mask_text", None)
        if not callable(mask_text):
            return text
        try:
            return mask_text(text)
        except (AttributeError, TypeError, ValueError):
            return text

    def read_file_lines(
        self,
        file_id: str,
        tail: int = 500,
        level: str | None = None,
        keyword: str | None = None,
    ) -> dict | None:
        """Return the last *tail* matching lines of one log file."""

        resolved = self.resolve_file(file_id)
        if resolved is None:
            return None
        label, root, path = resolved
        try:
            tail = max(1, min(int(tail or 500), 5000))
        except (TypeError, ValueError):
            tail = 500
        wanted = (level or "").strip().upper() or None
        threshold = self.LEVEL_ORDER.get(wanted) if wanted else None
        needle = (keyword or "").strip().casefold() or None

        buffer: deque[str] = deque(maxlen=tail)
        scanned = 0
        matched = 0
        truncated = False
        keep = True
        error: str | None = None
        try:
            with self._open_text(path) as stream:
                for line in stream:
                    scanned += 1
                    if scanned > self.MAX_SCAN_LINES:
                        truncated = True
                        break
                    text = line.rstrip("\r\n")
                    if threshold is not None:
                        found = self._line_level(text)
                        if found is not None:
                            keep = self.LEVEL_ORDER.get(found, 0) >= threshold
                        if not keep:
                            continue
                    if needle and needle not in text.casefold():
                        continue
                    matched += 1
                    buffer.append(text)
        except (OSError, EOFError, UnicodeError, ValueError) as exc:
            error = f"读取失败: {exc}"

        lines = [self._mask(item) for item in buffer]
        try:
            stat = path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            size = 0
            mtime = 0.0
        return {
            "id": self.make_file_id(label, root, path),
            "name": path.name,
            "source": label,
            "lines": lines,
            "scanned": scanned,
            "matched": matched,
            "truncated": truncated,
            "size": size,
            "mtime": mtime,
            "compressed": path.name.casefold().endswith(".gz"),
            "position": size if not path.name.casefold().endswith(".gz") else 0,
            "error": error,
        }

    def tail_bytes(
        self, file_id: str, position: int = 0, limit: int = 65536
    ) -> dict | None:
        """Incrementally read new bytes so the WebUI can follow a live log."""

        resolved = self.resolve_file(file_id)
        if resolved is None:
            return None
        label, root, path = resolved
        identifier = self.make_file_id(label, root, path)
        if path.name.casefold().endswith(".gz"):
            return {
                "id": identifier,
                "supported": False,
                "position": 0,
                "size": 0,
                "reset": False,
                "lines": [],
            }
        try:
            limit = max(1024, min(int(limit or 65536), 1024 * 1024))
        except (TypeError, ValueError):
            limit = 65536
        try:
            size = path.stat().st_size
        except OSError:
            return None
        try:
            start = int(position or 0)
        except (TypeError, ValueError):
            start = 0
        reset = False
        if start < 0 or start > size:
            # The file was rotated or truncated under us; restart from the tail.
            start = max(0, size - limit)
            reset = True
        if size - start > limit:
            start = size - limit
            reset = True
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                chunk = stream.read(limit)
        except OSError:
            return None
        text = chunk.decode("utf-8", errors="ignore")
        consumed = len(chunk)
        if consumed and not text.endswith("\n"):
            cut = text.rfind("\n")
            if cut >= 0:
                consumed -= len(text[cut + 1 :].encode("utf-8", errors="ignore"))
                text = text[: cut + 1]
        if reset and start > 0:
            _, separator, remainder = text.partition("\n")
            if separator:
                text = remainder
        return {
            "id": identifier,
            "supported": True,
            "position": start + consumed,
            "size": size,
            "reset": reset,
            "lines": [self._mask(line) for line in text.splitlines()],
        }

    def delete_files(self, file_ids: Iterable[str]) -> dict:
        """Delete rotated log files from the current data dir only."""

        deleted = 0
        freed = 0
        skipped: list[dict] = []
        for file_id in file_ids or []:
            resolved = self.resolve_file(file_id)
            if resolved is None:
                skipped.append({"id": str(file_id), "reason": "无效或越界的文件标识"})
                continue
            label, _root, path = resolved
            if label != "current":
                skipped.append(
                    {"id": str(file_id), "reason": "只允许删除当前数据目录中的日志"}
                )
                continue
            if is_active_log_name(path.name):
                skipped.append(
                    {"id": str(file_id), "reason": "正在写入的日志不可删除"}
                )
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError as exc:
                skipped.append({"id": str(file_id), "reason": f"删除失败: {exc}"})
                continue
            deleted += 1
            freed += size
        return {"deleted": deleted, "skipped": skipped, "freed_bytes": freed}

    def overview_payload(self) -> dict:
        """Everything the WebUI needs for its first render."""

        stats = self.cleaner.get_stats() if self.cleaner else self._stats(self.data_dir)

        def iso(value) -> str | None:
            formatter = getattr(value, "isoformat", None)
            if not callable(formatter):
                return None
            try:
                return formatter(timespec="seconds")
            except TypeError:
                return formatter()

        directories = {
            str(name): {
                "count": int(item.get("count", 0)),
                "size": int(item.get("size", 0)),
            }
            for name, item in (stats.get("directories") or {}).items()
        }
        return {
            "data_dir": str(self.data_dir),
            "total_files": stats.get("total_files", 0),
            "total_size_mb": stats.get("total_size_mb", 0),
            "compressed_count": stats.get("compressed_count", 0),
            "oldest_file": iso(stats.get("oldest_file")),
            "newest_file": iso(stats.get("newest_file")),
            "directories": directories,
            "slice_by_record_time": self.slice_by_record_time,
            "sources": [
                {
                    "label": label,
                    "kind": self._source_kind(label),
                    "path": str(root),
                }
                for label, root in self._browse_sources()
            ],
            "categories": self.list_categories(),
        }
