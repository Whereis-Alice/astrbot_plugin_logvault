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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

from .log_router import LogRouter


_PLUGIN_TAG_PLACEHOLDERS = {"core", "plug", "plugin", ""}


_compress_executor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="logvault_compress"
)


#: ``<base>.log.<slot>`` names are recycled by every rollover, so an archive
#: named after the slot number is replaced again on the next cycle.
_ROTATION_SLOT_RE = re.compile(r"^(?P<base>.+\.log)\.(?P<slot>\d+)$", re.IGNORECASE)


#: Suffixes of files a logging handler may still hold open.  ``.log`` is the
#: canonical stream and ``.log.active`` is the fallback :func:`_prepare_log_path`
#: creates when a stale directory occupies the canonical path.
_ACTIVE_LOG_SUFFIXES = (".log", ".log.active")


def is_active_log_name(name: str) -> bool:
    """True when *name* belongs to a stream a handler is still writing to.

    Compressing or unlinking one of these looks fine on POSIX -- the call
    succeeds -- but the handler keeps writing into an unlinked inode and every
    later record disappears without an error.  The rule was duplicated in the
    cleaner, the compressor and the console file list, and the console copy had
    already drifted: it only knew about ``.log``.
    """

    lowered = name.casefold()
    return lowered.endswith(_ACTIVE_LOG_SUFFIXES)


def archive_destination(source: Path, mtime: float | None = None) -> Path:
    """Return a collision-free ``.gz`` path for a closed rotated log.

    Numbered rotation slots (``all.log.1`` .. ``all.log.5``) are reused on every
    rollover.  Naming the archive after the slot therefore made each cycle
    overwrite the previous archive through ``os.replace``: the archive count
    never grew and older generations were destroyed silently.  Stamping the
    archive with the source mtime keeps every generation and leaves retention
    (age and total size) as the only thing that removes them.
    """

    match = _ROTATION_SLOT_RE.match(source.name)
    if match:
        if mtime is None:
            try:
                mtime = source.stat().st_mtime
            except OSError:
                mtime = None
        moment = datetime.fromtimestamp(mtime) if mtime else datetime.now()
        stem = f"{match.group('base')}.{moment.strftime('%Y%m%d-%H%M%S')}"
    else:
        # Time rotation already produces unique names such as
        # ``all.log.2026-09-01``; those stay readable as-is.
        stem = source.name

    candidate = source.with_name(f"{stem}.gz")
    counter = 1
    while candidate.exists():
        candidate = source.with_name(f"{stem}-{counter}.gz")
        counter += 1
    return candidate


def _atomic_compress_file(filepath: str | os.PathLike[str]) -> bool:
    """Compress a closed rotated file and preserve its original mtime."""

    source = Path(filepath)
    if not source.is_file():
        return False

    try:
        source_stat = source.stat()
    except OSError:
        return False
    destination = archive_destination(source, source_stat.st_mtime)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
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


class LogVaultFormatter(logging.Formatter):
    """Formatter that keeps AstrBot's plugin tag in the persisted line.

    The tag is what makes a shared file such as all.log filterable per plugin
    later on (see CommandHandler._line_matches_plugin) and it is the only
    reliable owner marker for records whose pathname points at the logging
    bridge instead of the plugin module.
    """

    def format(self, record: logging.LogRecord) -> str:
        tag = str(getattr(record, "plugin_tag", "") or "").strip()
        if not tag:
            tag = "[Core]"
        elif not (tag.startswith("[") and tag.endswith("]")):
            tag = f"[{tag}]"
        # Written back onto the record so every sibling handler formats the
        # same value instead of recomputing it.
        record.logvault_tag = tag
        return super().format(record)


class LogVaultHandler(logging.Handler):
    """Persist AstrBot records into global, core, error, and plugin files."""

    def __init__(
        self,
        data_dir: Path,
        config: dict,
        sensitive_filter=None,
        plugin_name_resolver: Callable[[str], str | None] | None = None,
    ):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.config = config
        self.sensitive_filter = sensitive_filter
        # Resolves a "<dirname>" hint against the set of installed plugins so
        # records logged through a plain logging.getLogger(__name__) call are
        # still attributed to their owner.
        self.plugin_name_resolver = plugin_name_resolver
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
            LogVaultFormatter(
                fmt=(
                    "[%(asctime)s] [%(levelname)-5s] %(logvault_tag)s "
                    "[%(filename)s:%(lineno)d]: %(message)s"
                ),
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

    def _resolve_plugin_hint(self, record: logging.LogRecord) -> str | None:
        """Attribute a record to an installed plugin using weak hints.

        A plugin that calls logging.getLogger(__name__) instead of
        astrbot.api.logger is enriched with plugin_tag "[Plug]" and
        source_file "<plugin_dir>.<module>".  The directory prefix is only
        accepted when it matches a plugin AstrBot actually has installed, so a
        core module such as "core.utils" is never mistaken for a plugin.
        """

        resolver = self.plugin_name_resolver
        if resolver is None:
            return None
        candidates: list[str] = []
        source_file = str(getattr(record, "source_file", "") or "").strip()
        if source_file:
            candidates.append(source_file.split(".", 1)[0])
        tag = str(getattr(record, "plugin_tag", "") or "").strip().strip("[]")
        if tag and tag.casefold() not in _PLUGIN_TAG_PLACEHOLDERS:
            candidates.append(tag)
        for candidate in candidates:
            if not candidate:
                continue
            try:
                resolved = resolver(candidate)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                return None
            if resolved:
                return str(resolved)
        return None

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

            plugin_name = LogRouter.extract_plugin_name_from_record(
                record
            ) or self._resolve_plugin_hint(record)
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
