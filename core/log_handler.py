"""Logging handlers used by LogVault.

The handler deliberately keeps the active ``*.log`` files open.  Compressing
or deleting an active file while its stream is open is safe-looking on Linux,
but subsequent writes then go to an unlinked inode and disappear from the
filesystem.  Rotated files are the only files this module compresses.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

from .log_router import LogRouter


_compress_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="logvault_compress"
)


def _atomic_compress_file(filepath: str | os.PathLike[str]) -> bool:
    """Compress a closed rotated file and preserve its original mtime."""

    source = Path(filepath)
    if not source.is_file():
        return False

    destination = source.with_name(source.name + ".gz")
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        source_stat = source.stat()
        with source.open("rb") as source_stream, gzip.open(temporary, "wb") as out:
            shutil.copyfileobj(source_stream, out)
        os.replace(temporary, destination)
        source.unlink()
        # gzip.open otherwise gives the archive a new timestamp.  Keeping the
        # source timestamp makes ``send ... <days>`` reflect log content age.
        os.utime(destination, (source_stat.st_atime, source_stat.st_mtime))
        return True
    except (OSError, EOFError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _compress_file_sync(filepath: str):
    """Backward-compatible wrapper used by the rotation helpers."""

    _atomic_compress_file(filepath)


def _next_legacy_path(path: Path) -> Path:
    """Return a recoverable sibling path for a malformed log directory."""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = path.with_name(f"{path.name}.legacy-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.legacy-{stamp}-{counter}")
        counter += 1
    return candidate


def _prepare_log_path(filepath: Path) -> Path:
    """Make room for a log file without discarding an existing directory.

    Older exports have occasionally been unpacked as ``plugin.log/plugin.log``
    directories.  Quarantine that directory and create the canonical file so
    logging can resume; the old data remains recoverable beside it.
    """

    if filepath.exists() and filepath.is_dir():
        try:
            filepath.rename(_next_legacy_path(filepath))
        except OSError:
            # A read-only or concurrently used directory should not prevent
            # all logging.  Fall back to a sibling active file.
            return filepath.with_name(filepath.name + ".active")
    return filepath


class CompressedRotatingFileHandler(RotatingFileHandler):
    """Size-based rotation with safe compression of the oldest slot."""

    def __init__(
        self,
        filename,
        mode="a",
        maxBytes=0,
        backupCount=0,
        encoding=None,
        delay=False,
        enable_compression=True,
    ):
        self.enable_compression = enable_compression
        super().__init__(filename, mode, maxBytes, backupCount, encoding, delay)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None

        if self.backupCount > 0:
            # Compress before moving the rotation slots.  The previous
            # implementation submitted this path to a worker and immediately
            # renamed it, so the worker could compress a different file.
            oldest = Path(f"{self.baseFilename}.{self.backupCount}")
            if oldest.exists():
                if self.enable_compression:
                    _atomic_compress_file(oldest)
                else:
                    try:
                        oldest.unlink()
                    except OSError:
                        pass

            for index in range(self.backupCount - 1, 0, -1):
                source = Path(self.rotation_filename(f"{self.baseFilename}.{index}"))
                destination = Path(
                    self.rotation_filename(f"{self.baseFilename}.{index + 1}")
                )
                if source.exists():
                    try:
                        if destination.exists():
                            destination.unlink()
                        source.rename(destination)
                    except OSError:
                        pass

            first_rotation = Path(self.rotation_filename(f"{self.baseFilename}.1"))
            try:
                if first_rotation.exists():
                    first_rotation.unlink()
                self.rotate(self.baseFilename, str(first_rotation))
            except OSError:
                # Re-open below; logging should continue even if a rotation is
                # temporarily blocked by another process.
                pass

        if not self.delay:
            self.stream = self._open()


class CompressedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Time-based rotation with asynchronous compression of closed files."""

    def __init__(
        self,
        filename,
        when="D",
        interval=1,
        backupCount=0,
        encoding=None,
        delay=False,
        utc=False,
        atTime=None,
        enable_compression=True,
    ):
        self.enable_compression = enable_compression
        super().__init__(
            filename,
            when,
            interval,
            backupCount,
            encoding,
            delay,
            utc,
            atTime,
        )

    def doRollover(self):
        super().doRollover()
        if self.enable_compression and self.backupCount > 0:
            _compress_executor.submit(
                _compress_old_files_sync,
                os.path.dirname(self.baseFilename),
                os.path.basename(self.baseFilename),
                self.baseFilename,
            )


def _compress_old_files_sync(dir_path: str, base_name: str, current_file: str):
    """Compress closed rotated files in a time-rotation directory."""

    now = datetime.now()
    try:
        for filename in os.listdir(dir_path):
            if not filename.startswith(base_name) or filename.endswith(".gz"):
                continue
            filepath = os.path.join(dir_path, filename)
            if os.path.abspath(filepath) == os.path.abspath(current_file):
                continue
            try:
                if not os.path.isfile(filepath):
                    continue
                mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                if (now - mtime).days >= 1:
                    _atomic_compress_file(filepath)
            except OSError:
                continue
    except OSError:
        pass


class LogVaultHandler(logging.Handler):
    """Persist AstrBot records into global, core, error, and plugin files."""

    def __init__(self, data_dir: Path, config: dict, sensitive_filter=None):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.config = config
        self.sensitive_filter = sensitive_filter
        self.handlers: dict[str, logging.Handler] = {}
        self._plugin_handler_lock = threading.Lock()
        self._init_directories()
        self._init_handlers()

    def _init_directories(self):
        for directory in ("all", "core", "errors", "plugins"):
            (self.data_dir / directory).mkdir(parents=True, exist_ok=True)

    def _numeric_config(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            return max(minimum, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _init_handlers(self):
        max_bytes = self._numeric_config("max_file_size_mb", 10, 1) * 1024 * 1024
        backup_count = self._numeric_config("backup_count", 5, 0)
        strategy = str(self.config.get("rotation_strategy", "size")).lower()
        interval = str(self.config.get("rotation_interval", "daily")).lower()
        enable_compression = bool(self.config.get("enable_compression", True))

        if self.config.get("enable_all_log", True):
            self.handlers["all"] = self._create_handler(
                self.data_dir / "all" / "all.log",
                max_bytes,
                backup_count,
                strategy,
                interval,
                enable_compression,
            )

        if self.config.get("enable_core_log", True):
            self.handlers["core"] = self._create_handler(
                self.data_dir / "core" / "core.log",
                max_bytes,
                backup_count,
                strategy,
                interval,
                enable_compression,
            )

        if self.config.get("enable_error_log", True):
            error_handler = self._create_handler(
                self.data_dir / "errors" / "error.log",
                max_bytes,
                backup_count,
                strategy,
                interval,
                enable_compression,
            )
            error_handler.setLevel(logging.ERROR)
            self.handlers["error"] = error_handler

    def _create_handler(
        self,
        filepath: Path,
        max_bytes: int,
        backup_count: int,
        strategy: str,
        interval: str,
        enable_compression: bool,
    ) -> logging.Handler:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath = _prepare_log_path(filepath)

        if strategy == "time":
            when = "H" if interval == "hourly" else "D"
            handler: logging.Handler = CompressedTimedRotatingFileHandler(
                str(filepath),
                when=when,
                backupCount=backup_count,
                encoding="utf-8",
                enable_compression=enable_compression,
            )
        else:
            # ``hybrid`` was historically accepted but implemented as size
            # rotation.  Keep that compatibility while avoiding a misleading
            # second, unsafe rotation implementation.
            handler = CompressedRotatingFileHandler(
                str(filepath),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                enable_compression=enable_compression,
            )

        handler.setFormatter(
            logging.Formatter(
                fmt="[%(asctime)s] [%(levelname)-5s] [%(filename)s:%(lineno)d]: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        return handler

    @staticmethod
    def _safe_plugin_name(plugin_name: str) -> str:
        candidate = str(plugin_name).strip()
        candidate = re.sub(r'[\\/:*?"<>|]+', "_", candidate)
        if candidate in {"", ".", ".."}:
            return "unknown"
        return candidate

    def get_plugin_handler(self, plugin_name: str) -> logging.Handler:
        """Get or lazily create the handler for one plugin."""

        plugin_name = self._safe_plugin_name(plugin_name)
        key = f"plugin_{plugin_name}"
        if key not in self.handlers:
            with self._plugin_handler_lock:
                if key not in self.handlers:
                    max_bytes = self._numeric_config("max_file_size_mb", 10, 1) * 1024 * 1024
                    backup_count = self._numeric_config("backup_count", 5, 0)
                    strategy = str(self.config.get("rotation_strategy", "size")).lower()
                    interval = str(self.config.get("rotation_interval", "daily")).lower()
                    enable_compression = bool(self.config.get("enable_compression", True))
                    self.handlers[key] = self._create_handler(
                        self.data_dir / "plugins" / plugin_name / "plugin.log",
                        max_bytes,
                        backup_count,
                        strategy,
                        interval,
                        enable_compression,
                    )
        return self.handlers[key]

    def emit(self, record: logging.LogRecord):
        try:
            seen_handlers = getattr(record, "_logvault_seen_handlers", None)
            if seen_handlers is None:
                seen_handlers = set()
                record.__dict__["_logvault_seen_handlers"] = seen_handlers
            handler_id = id(self)
            if handler_id in seen_handlers:
                return
            seen_handlers.add(handler_id)

            if self.sensitive_filter:
                record = self.sensitive_filter.mask_record(record)

            if "all" in self.handlers:
                self.handlers["all"].emit(record)

            plugin_name = LogRouter.extract_plugin_name_from_record(record)
            if plugin_name and self.config.get("enable_plugin_separation", True):
                self.get_plugin_handler(plugin_name).emit(record)
            elif "core" in self.handlers:
                self.handlers["core"].emit(record)

            if "error" in self.handlers and record.levelno >= logging.ERROR:
                self.handlers["error"].emit(record)

            self._flush_handlers()
        except Exception:
            self.handleError(record)

    def _flush_handlers(self):
        for handler in self.handlers.values():
            try:
                handler.flush()
            except OSError:
                pass

    def close(self):
        for handler in list(self.handlers.values()):
            try:
                handler.flush()
                handler.close()
            except OSError:
                pass
        self.handlers.clear()
        super().close()


# Compatibility alias for code that imported the upstream class directly.
LogPlusHandler = LogVaultHandler
