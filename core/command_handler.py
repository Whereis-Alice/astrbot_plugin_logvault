"""Non-blocking command operations for LogVault."""

from __future__ import annotations

import asyncio
import gzip
import os
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

from .log_cleaner import LogCleaner


def _is_log_file(path: Path) -> bool:
    name = path.name.casefold()
    name = name.removesuffix(".gz")
    return name.endswith(".log") or ".log." in name


class CommandHandler:
    """Build status/search/export/send responses without blocking the event loop."""

    def __init__(
        self,
        data_dir: Path,
        cleaner: LogCleaner,
        additional_data_dirs: Iterable[Path] | None = None,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.cleaner = cleaner
        self.additional_data_dirs = self._dedupe_dirs(additional_data_dirs or [])

    @staticmethod
    def _dedupe_dirs(paths: Iterable[Path]) -> list[Path]:
        result: list[Path] = []
        seen: set[str] = set()
        for value in paths:
            try:
                path = Path(value).expanduser().resolve()
            except (OSError, RuntimeError, TypeError):
                continue
            key = os.path.normcase(str(path))
            if path.exists() and path.is_dir() and key not in seen:
                seen.add(key)
                result.append(path)
        return result

    def _sources(self) -> Iterator[tuple[str, Path]]:
        yield "current", self.data_dir
        for index, path in enumerate(self.additional_data_dirs, start=1):
            yield f"legacy_{index}_{path.name}", path

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
            entries, plugin_name, error = self._plugin_entries(target, days)
            if error:
                return error, None
            label = f"插件 {plugin_name} 最近 {days} 天日志"
            filename = f"plugin_{plugin_name}_{timestamp}.zip"

        if not entries:
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
    ) -> tuple[list[tuple[str, Path, Path]], str, str | None]:
        needle = keyword.casefold()
        candidates: list[tuple[str, str, Path, Path]] = []
        for label, root in self._sources():
            plugins_dir = root / "plugins"
            if not plugins_dir.is_dir():
                continue
            try:
                plugin_dirs = [item for item in plugins_dir.iterdir() if item.is_dir()]
            except OSError:
                continue
            for plugin_dir in plugin_dirs:
                if needle in plugin_dir.name.casefold():
                    candidates.append((plugin_dir.name.casefold(), plugin_dir.name, root, plugin_dir))

        unique = {item[0]: item[1] for item in candidates}
        if not unique:
            available = sorted(
                {
                    item.name
                    for _, root in self._sources()
                    for item in (root / "plugins").glob("*")
                    if item.is_dir()
                }
            )
            choices = "\n".join(f"  - {item}" for item in available) or "  （暂无插件日志目录）"
            return [], "", f"❌ 未找到匹配 '{keyword}' 的插件\n可用插件:\n{choices}"
        if len(unique) > 1:
            choices = "\n".join(f"  - {name}" for name in sorted(unique.values()))
            return [], "", f"❌ 找到多个匹配的插件，请更具体:\n{choices}"

        plugin_name = next(iter(unique.values()))
        cutoff = self._cutoff(days)
        entries: list[tuple[str, Path, Path]] = []
        for _, name, root, plugin_dir in candidates:
            if name.casefold() != plugin_name.casefold():
                continue
            for path in self._iter_files(root, plugin_dir):
                try:
                    if path.stat().st_mtime >= cutoff:
                        label = "current" if root == self.data_dir else next(
                            label for label, candidate in self._sources() if candidate == root
                        )
                        entries.append((label, root, path))
                except OSError:
                    continue
        return entries, plugin_name, None

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
