"""Capture AstrBot's loguru pipeline into LogVault's logging handler.

AstrBot 4.27 routes every logging record through
astrbot.core.log._LoguruInterceptHandler into a patched loguru logger.  That
bridge is installed on the root logger as well, so subscribing to loguru is the
only place where *all* records converge: core modules, plugins that use
astrbot.api.logger, plugins that use logging.getLogger(__name__), and
third-party libraries that only propagate to root.

Attaching a logging.Handler to a handful of known loggers -- the historic
LogVault approach -- misses the last two groups entirely, which is why plugin
logs looked incomplete.  LoguruCapture installs a single loguru sink and
converts each loguru record back into a logging.LogRecord, so the existing
routing, masking and rotation code keeps working unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LEADING_TIMESTAMP_RE = re.compile(
    r"^\s*\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]\s*"
)
_LEADING_TAG_RE = re.compile(r"^\[([^\[\]]+)\]")
_SOURCE_HINT_RE = re.compile(r"\[([A-Za-z0-9_][A-Za-z0-9_.\-]*):(\d+)\]")

# loguru ships two levels that logging does not know about.  Keeping the
# mapping explicit avoids "Level 5"/"Level 25" appearing in log files.
LOGURU_LEVEL_TO_LOGGING: dict[str, int] = {
    "TRACE": 5,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "SUCCESS": 25,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# AstrBot enriches records with these fields; LogRouter and the file formatter
# both rely on them, so they must survive the loguru round-trip.
_EXTRA_PASSTHROUGH = (
    "plugin_tag",
    "short_levelname",
    "astrbot_version_tag",
    "source_file",
    "source_line",
    "is_trace",
    "category",
)


def loguru_logger() -> Any | None:
    """Return AstrBot's loguru logger, or None when loguru is unavailable.

    loguru is a hard dependency of AstrBot 4.x but not of this plugin, so the
    import stays optional and the plugin degrades to logging handlers.
    """

    try:
        from loguru import logger as loguru_instance
    except Exception:  # pragma: no cover - depends on the host installation
        return None
    return loguru_instance


def _apply_timestamp(record: logging.LogRecord, created: float | None) -> None:
    """Replace the record creation time so file lines keep the original order."""

    if not created:
        return
    record.created = created
    record.msecs = (created - int(created)) * 1000
    start_time = getattr(logging, "_startTime", None)
    if isinstance(start_time, (int, float)):
        record.relativeCreated = (created - start_time) * 1000


class LoguruCapture:
    """Mirror every loguru record into a logging.Handler.

    The sink must never be combined with logging handlers for the same records:
    AstrBot forwards logging -> loguru, so keeping both would write every
    record twice.  main.py therefore treats the two capture modes as mutually
    exclusive.
    """

    def __init__(
        self,
        handler: logging.Handler,
        level: int = logging.DEBUG,
        include_trace: bool = False,
    ):
        self.handler = handler
        self.level = int(level)
        self.include_trace = bool(include_trace)
        # loguru TRACE (5) sits below logging.DEBUG (10), so a DEBUG sink would
        # never see it.  Widen the floor to TRACE only when trace logs were
        # explicitly requested and the configured level is not more selective
        # than DEBUG, otherwise "include trace" would silently override it.
        self.threshold = (
            LOGURU_LEVEL_TO_LOGGING["TRACE"]
            if self.include_trace and self.level <= logging.DEBUG
            else self.level
        )
        self._sink_id: int | None = None
        self._state = threading.local()
        self.forwarded = 0
        self.dropped = 0

    @property
    def active(self) -> bool:
        return self._sink_id is not None

    def start(self) -> bool:
        """Install the sink; return True when loguru capture is running."""

        if self._sink_id is not None:
            return True
        target = loguru_logger()
        if target is None:
            return False
        try:
            self._sink_id = target.add(
                self._sink,
                level=max(1, self.threshold),
                format="{message}",
                # catch keeps a sink failure from breaking AstrBot's own console
                # output; enqueue=False preserves ordering and avoids spawning a
                # second process-wide writer thread.
                catch=True,
                enqueue=False,
                backtrace=False,
                diagnose=False,
            )
        except (TypeError, ValueError, RuntimeError):
            self._sink_id = None
            return False
        return True

    def stop(self) -> None:
        """Remove the sink.  configure_logger never touches foreign sinks."""

        sink_id = self._sink_id
        self._sink_id = None
        if sink_id is None:
            return
        target = loguru_logger()
        if target is None:
            return
        try:
            target.remove(sink_id)
        except (ValueError, KeyError, RuntimeError):
            pass

    def _sink(self, message: Any) -> None:
        # A failure inside the handler (disk full, permission error) is logged
        # by logging itself and would re-enter this sink.  The thread-local flag
        # turns that recursion into a single dropped record.
        if getattr(self._state, "busy", False):
            self.dropped += 1
            return
        self._state.busy = True
        try:
            record = self._build_record(getattr(message, "record", None))
            if record is None:
                return
            self.handler.handle(record)
            self.forwarded += 1
        except Exception:
            self.dropped += 1
        finally:
            self._state.busy = False

    def _build_record(self, source: Any) -> logging.LogRecord | None:
        """Convert one loguru record into an equivalent logging.LogRecord."""

        if not source:
            return None
        try:
            extra = dict(source.get("extra") or {})
        except (AttributeError, TypeError):
            return None

        if extra.get("is_trace") and not self.include_trace:
            return None

        level = source.get("level")
        level_name = str(getattr(level, "name", "") or "INFO").upper()
        level_no = getattr(level, "no", None)
        if not isinstance(level_no, int):
            level_no = LOGURU_LEVEL_TO_LOGGING.get(level_name, logging.INFO)
        if level_no < self.threshold:
            return None

        file_info = source.get("file")
        pathname = str(getattr(file_info, "path", "") or "")

        exc_info = None
        exception = source.get("exception")
        if exception is not None:
            exc_type = getattr(exception, "type", None)
            if exc_type is not None:
                exc_info = (
                    exc_type,
                    getattr(exception, "value", None),
                    getattr(exception, "traceback", None),
                )

        record = logging.LogRecord(
            name=str(source.get("name") or "astrbot"),
            level=int(level_no),
            pathname=pathname,
            lineno=int(source.get("line") or 0),
            msg=str(source.get("message", "")),
            args=(),
            exc_info=exc_info,
            func=str(source.get("function") or ""),
        )
        # LogRecord derives levelname from the numeric level and would turn
        # loguru's TRACE/SUCCESS into "Level 5"/"Level 25".
        record.levelname = level_name

        try:
            _apply_timestamp(record, source["time"].timestamp())
        except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError):
            pass

        for key in _EXTRA_PASSTHROUGH:
            if key in extra:
                record.__dict__.setdefault(key, extra[key])

        # Without this every intercepted record points at astrbot/core/log.py,
        # because the intercept handler does not use opt(depth=...).
        source_file = str(extra.get("source_file") or "").strip()
        if source_file:
            record.filename = source_file
            record.module = source_file.rsplit(".", 1)[-1]
        source_line = extra.get("source_line")
        if isinstance(source_line, int) and source_line > 0:
            record.lineno = source_line

        record.logvault_via_loguru = True
        return record


class BootstrapBackfill:
    """Replay AstrBot's in-memory log cache captured before LogVault started.

    AstrBot keeps the last CACHED_SIZE console lines in LogBroker.log_cache for
    the dashboard console.  Those lines cover the startup window LogVault cannot
    observe (plugins load after the core has already logged), so replaying them
    once per process is what makes the persisted history complete instead of
    starting mid-boot.

    Only the formatted text is available, so entries are re-parsed: the leading
    [plugin_tag] and [source_file:line] markers are restored as record
    attributes, which is exactly what LogRouter needs for routing.
    """

    STATE_FILENAME = ".bootstrap_state.json"

    def __init__(self, data_dir: Path, limit: int = 500):
        self.state_path = Path(data_dir) / self.STATE_FILENAME
        self.limit = max(1, int(limit))

    def _load_last_time(self) -> float:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0.0
        try:
            return float(payload.get("last_time", 0.0))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _save_last_time(self, value: float) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"last_time": value}), encoding="utf-8"
            )
        except OSError:
            pass

    @staticmethod
    def cached_entries() -> list[dict]:
        try:
            from astrbot.core.log import LogManager
        except Exception:
            return []
        broker = getattr(LogManager, "_log_broker", None)
        cache = getattr(broker, "log_cache", None)
        if cache is None:
            return []
        try:
            return [item for item in list(cache) if isinstance(item, dict)]
        except (RuntimeError, TypeError):
            return []

    @staticmethod
    def build_record(entry: dict) -> logging.LogRecord | None:
        raw = _ANSI_RE.sub("", str(entry.get("data") or ""))
        text = _LEADING_TIMESTAMP_RE.sub("", raw.rstrip("\r\n")).strip()
        if not text:
            return None

        level_name = str(entry.get("level") or "INFO").upper()
        level_no = LOGURU_LEVEL_TO_LOGGING.get(level_name, logging.INFO)
        record = logging.LogRecord(
            name="astrbot.bootstrap",
            level=level_no,
            pathname="bootstrap",
            lineno=0,
            msg=text,
            args=(),
            exc_info=None,
        )
        record.levelname = level_name
        record.category = entry.get("category") or "system"

        tag = _LEADING_TAG_RE.match(text)
        if tag:
            record.plugin_tag = f"[{tag.group(1).strip()}]"
        hint = _SOURCE_HINT_RE.search(text)
        if hint:
            record.source_file = hint.group(1)
            try:
                record.source_line = int(hint.group(2))
            except ValueError:
                pass

        try:
            _apply_timestamp(record, float(entry.get("time") or 0.0))
        except (OSError, OverflowError, TypeError, ValueError):
            pass

        record.logvault_backfill = True
        return record

    def replay(self, handler: logging.Handler) -> int:
        """Forward not-yet-persisted cached entries; return how many were sent."""

        entries = self.cached_entries()
        if not entries:
            return 0

        last_time = self._load_last_time()
        newest = last_time
        count = 0
        for entry in entries[-self.limit :]:
            try:
                stamp = float(entry.get("time") or 0.0)
            except (TypeError, ValueError):
                stamp = 0.0
            # A plugin reload re-runs the backfill against the same cache; the
            # watermark keeps the persisted log free of duplicates.
            if stamp and stamp <= last_time:
                continue
            record = self.build_record(entry)
            if record is None:
                continue
            try:
                handler.handle(record)
            except Exception:
                continue
            count += 1
            newest = max(newest, stamp)

        if newest > last_time:
            self._save_last_time(newest)
        return count
