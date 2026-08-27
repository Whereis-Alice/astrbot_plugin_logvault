"""Background compression and retention for closed log files."""

from __future__ import annotations

import asyncio
import gzip
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class LogFileInfo:
    path: Path
    size: int
    mtime: datetime
    is_compressed: bool
    is_active: bool


def _is_log_file(path: Path) -> bool:
    """Recognise active and rotated log names without matching arbitrary data."""

    name = path.name.casefold()
    name = name.removesuffix(".gz")
    return name.endswith(".log") or ".log." in name


class LogCleaner:
    """Compress and remove *closed* logs without touching active streams."""

    #: Suffixes written by the exporter; nothing else under exports is removed.
    EXPORT_SUFFIXES = (".zip", ".txt")

    def __init__(self, data_dir: Path, config: dict):
        self.data_dir = Path(data_dir)
        self.config = config
        self._task: asyncio.Task | None = None

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _interval_seconds(self) -> float:
        """Maintenance period in seconds, clamped to 1 minute .. 7 days."""

        minutes = self._positive_int(
            self.config.get("clean_interval_minutes", 60), 60, minimum=1
        )
        return float(min(minutes, 10080) * 60)

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(self._interval_seconds())
                await self.cleanup()
            except asyncio.CancelledError:
                break
            except Exception:
                # A maintenance failure must not stop log capture.
                continue

    @staticmethod
    def _positive_int(value, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError):
            return default

    async def cleanup(self) -> dict[str, int]:
        result = {"compressed": 0, "deleted": 0, "freed_bytes": 0}

        if self.config.get("enable_compression", True):
            days = self._positive_int(self.config.get("compression_after_days", 1), 1)
            result["compressed"] = await self._compress_old_logs(days)

        if self.config.get("auto_clean_enabled", True):
            max_age = self._positive_int(self.config.get("max_age_days", 30), 30)
            max_size_mb = self._positive_int(
                self.config.get("max_total_size_mb", 500), 500
            )
            deleted, freed = await self._clean_old_logs(
                max_age, max_size_mb * 1024 * 1024
            )
            result["deleted"] = deleted
            result["freed_bytes"] = freed

        # Export bundles live outside the log retention scan on purpose, so
        # without this pass data/exports grew until the disk filled up.
        purged, purged_bytes = await asyncio.to_thread(self._clean_exports)
        result["exports_deleted"] = purged
        result["deleted"] += purged
        result["freed_bytes"] += purged_bytes

        return result

    def _clean_exports(self) -> tuple[int, int]:
        """Enforce age, count and size limits on data/exports.

        Only generated bundles are considered, and the newest bundle is
        always kept so that a just finished export cannot vanish mid
        download.
        """

        directory = self.data_dir / "exports"
        if not directory.is_dir():
            return 0, 0
        retention_days = self._positive_int(
            self.config.get("export_retention_days", 7), 7
        )
        max_files = self._positive_int(self.config.get("export_max_files", 20), 20)
        max_bytes = (
            self._positive_int(self.config.get("export_max_total_mb", 512), 512)
            * 1024
            * 1024
        )
        bundles: list[tuple[float, int, Path]] = []
        try:
            candidates = list(directory.iterdir())
        except OSError:
            return 0, 0
        for path in candidates:
            if not path.is_file():
                continue
            if path.suffix.casefold() not in self.EXPORT_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            bundles.append((stat.st_mtime, stat.st_size, path))
        if not bundles:
            return 0, 0
        bundles.sort(key=lambda item: item[0], reverse=True)
        cutoff = (datetime.now() - timedelta(days=retention_days)).timestamp()
        doomed: list[tuple[float, int, Path]] = []
        kept: list[tuple[float, int, Path]] = []
        for index, bundle in enumerate(bundles):
            too_old = retention_days > 0 and bundle[0] < cutoff
            too_many = max_files > 0 and index >= max_files
            # index 0 is the newest bundle and is always preserved.
            if index and (too_old or too_many):
                doomed.append(bundle)
            else:
                kept.append(bundle)
        if max_bytes > 0:
            total = sum(item[1] for item in kept)
            while len(kept) > 1 and total > max_bytes:
                victim = kept.pop()
                total -= victim[1]
                doomed.append(victim)
        deleted = 0
        freed = 0
        for _mtime, size, path in doomed:
            try:
                path.unlink()
            except OSError:
                continue
            deleted += 1
            freed += size
        return deleted, freed

    async def _compress_old_logs(self, days: int) -> int:
        threshold = datetime.now() - timedelta(days=days)
        count = 0
        for info in self._scan_log_files():
            # Never unlink or replace the file a FileHandler may still hold.
            if info.is_active or info.is_compressed or info.mtime >= threshold:
                continue
            if await self._compress_file(info.path):
                count += 1
        return count

    async def _compress_file(self, filepath: Path) -> bool:
        file_name = filepath.name.casefold()
        if (
            not filepath.is_file()
            or file_name.endswith(".log")
            or file_name.endswith(".log.active")
        ):
            return False
        try:
            source_stat = filepath.stat()
            destination = filepath.with_name(filepath.name + ".gz")
            temporary = destination.with_name(
                f".{destination.name}.tmp-{os.getpid()}"
            )
            await asyncio.to_thread(
                self._do_compress,
                filepath,
                destination,
                temporary,
                source_stat.st_atime,
                source_stat.st_mtime,
            )
            return True
        except (OSError, EOFError):
            return False

    @staticmethod
    def _do_compress(
        src: Path,
        dst: Path,
        temporary: Path,
        atime: float,
        mtime: float,
    ):
        with src.open("rb") as source_stream, gzip.open(temporary, "wb") as out:
            shutil.copyfileobj(source_stream, out)
        os.replace(temporary, dst)
        src.unlink()
        os.utime(dst, (atime, mtime))

    async def _clean_old_logs(
        self, max_age_days: int, max_total_size: int
    ) -> tuple[int, int]:
        threshold = datetime.now() - timedelta(days=max_age_days)
        files = sorted(self._scan_log_files(), key=lambda item: item.mtime)
        total_size = sum(item.size for item in files)
        deleted = 0
        freed = 0

        for info in files:
            # Active files are intentionally excluded even if the configured
            # total-size limit is exceeded; deleting one loses future records
            # on POSIX while the handler still owns its open descriptor.
            if info.is_active:
                continue
            if info.mtime < threshold or total_size > max_total_size:
                try:
                    size = info.path.stat().st_size
                    info.path.unlink()
                    deleted += 1
                    freed += size
                    total_size -= size
                except OSError:
                    continue
        return deleted, freed

    def _scan_log_files(self) -> list[LogFileInfo]:
        files: list[LogFileInfo] = []
        if not self.data_dir.exists():
            return files

        for path in self.data_dir.rglob("*"):
            if not path.is_file() or not _is_log_file(path):
                continue
            try:
                relative = path.relative_to(self.data_dir)
                if any(part.casefold() == "exports" for part in relative.parts):
                    continue
                stat = path.stat()
                name = path.name.casefold()
                files.append(
                    LogFileInfo(
                        path=path,
                        size=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime),
                        is_compressed=name.endswith(".gz"),
                        is_active=name.endswith(".log") or name.endswith(".log.active"),
                    )
                )
            except (OSError, ValueError):
                continue
        return files

    def get_stats(self) -> dict:
        files = self._scan_log_files()
        total_size = sum(item.size for item in files)
        directories: dict[str, dict[str, int]] = {}
        for info in files:
            try:
                relative = info.path.relative_to(self.data_dir)
            except ValueError:
                continue
            top_dir = relative.parts[0] if relative.parts else "root"
            stat = directories.setdefault(top_dir, {"count": 0, "size": 0})
            stat["count"] += 1
            stat["size"] += info.size

        return {
            "total_files": len(files),
            "total_size_mb": round(total_size / 1024 / 1024, 2),
            "compressed_count": sum(1 for item in files if item.is_compressed),
            "directories": directories,
            "oldest_file": min((item.mtime for item in files), default=None),
            "newest_file": max((item.mtime for item in files), default=None),
        }
