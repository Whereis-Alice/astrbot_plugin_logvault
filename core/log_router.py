import os
import re
from pathlib import Path


_PLUGIN_ID_RE = re.compile(r"(?<![A-Za-z0-9_])astrbot_plugin_[A-Za-z0-9_]+", re.IGNORECASE)
_PLUGIN_LOGGER_PREFIX = "astrbot.plugin."


def _path_parts(pathname: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return path components with Windows/POSIX separators handled equally.

    ``LogRecord.pathname`` comes from the host process and can use a different
    separator than the platform running this code (for example, a Windows path
    may be restored from a log or test on a POSIX host).  Normalising both
    separators before splitting keeps routing independent of that detail.
    """

    normalized = os.fspath(pathname).replace("\\", "/")
    return tuple(part for part in normalized.split("/") if part not in ("", "."))


def _marker_index(parts: tuple[str, ...], marker: str) -> int | None:
    """Find a path marker case-insensitively and return its component index."""

    marker_lower = marker.lower()
    for index, part in enumerate(parts):
        if part.lower() == marker_lower:
            return index
    return None


class LogRouter:
    """日志路由器，负责日志来源判断和路径解析"""

    # Marker pairs used by AstrBot's external and built-in plugin layouts.
    # Components are compared case-insensitively while plugin names retain
    # their original case.
    PLUGIN_PATHS = (("data", "plugins"), ("astrbot", "builtin_stars"))

    @staticmethod
    def is_plugin_path(pathname: str | os.PathLike[str]) -> bool:
        """判断路径是否来自插件"""
        parts = _path_parts(pathname)
        lowered = tuple(part.lower() for part in parts)

        for parent, marker in LogRouter.PLUGIN_PATHS:
            for index in range(len(lowered) - 1):
                if lowered[index : index + 2] == (parent, marker):
                    return index + 2 < len(parts)

        # Some deployments expose a top-level ``plugins`` directory instead
        # of ``data/plugins``.  Keep this fallback for those paths while still
        # requiring a plugin-name component after the marker.
        index = _marker_index(parts, "plugins")
        return index is not None and index + 1 < len(parts)

    @staticmethod
    def extract_plugin_name(pathname: str | os.PathLike[str]) -> str | None:
        """从路径提取插件名"""
        parts = _path_parts(pathname)
        lowered = tuple(part.lower() for part in parts)

        # Prefer the explicit AstrBot layouts so a nested, unrelated
        # ``plugins`` directory cannot shadow the actual source location.
        for parent, marker in LogRouter.PLUGIN_PATHS:
            for index in range(len(lowered) - 1):
                if lowered[index : index + 2] == (parent, marker):
                    plugin_index = index + 2
                    if plugin_index < len(parts):
                        return parts[plugin_index]

        # Fallback for installations with a top-level ``plugins`` directory.
        idx = _marker_index(parts, "plugins")
        if idx is not None and idx + 1 < len(parts):
            return parts[idx + 1]

        return None

    @staticmethod
    def _extract_explicit_plugin_name(value: object, allow_generic: bool = False) -> str | None:
        """Extract a plugin ID from AstrBot's enriched LogRecord fields."""

        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None

        match = _PLUGIN_ID_RE.search(text)
        if match:
            return match.group(0)
        if not allow_generic:
            return None

        candidate = text.strip("[](){} ").split(":", 1)[0].split(".", 1)[0]
        if (
            not candidate
            or candidate.casefold() in {"core", "plug", "plugin"}
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", candidate)
        ):
            return None
        return candidate

    @staticmethod
    def extract_plugin_name_from_record(record: object) -> str | None:
        """Resolve plugin identity from a LogRecord, including AstrBot fields.

        Recent AstrBot releases enrich records before forwarding them to the
        console.  Depending on the logging bridge, ``pathname`` can point to
        the bridge module even though ``plugin_tag`` or ``source_file`` still
        contains the real plugin ID.  Prefer the normal path route, then use
        those enriched fields and the conventional ``[astrbot_plugin_*]``
        message prefix as compatibility fallbacks.
        """

        pathname = getattr(record, "pathname", None)
        plugin_name = LogRouter.extract_plugin_name(pathname or "")
        if plugin_name:
            return plugin_name

        logger_name = str(getattr(record, "name", "") or "")
        if logger_name.casefold().startswith(_PLUGIN_LOGGER_PREFIX):
            plugin_name = logger_name[len(_PLUGIN_LOGGER_PREFIX) :].strip()
            if plugin_name:
                return plugin_name

        for attribute in ("plugin_name", "plugin_id", "star_name"):
            plugin_name = LogRouter._extract_explicit_plugin_name(
                getattr(record, attribute, None), allow_generic=True
            )
            if plugin_name:
                return plugin_name

        # plugin_tag is authoritative: AstrBot sets it to "[<plugin_name>]"
        # for per-plugin loggers and to "[Core]"/"[Plug]" otherwise.  Generic
        # names are accepted here so plugins whose directory does not follow
        # the astrbot_plugin_* convention are still routed by name; the
        # candidate check rejects the Core/Plug placeholders.
        plugin_name = LogRouter._extract_explicit_plugin_name(
            getattr(record, "plugin_tag", None), allow_generic=True
        )
        if plugin_name:
            return plugin_name

        # source_file is "<dirname>.<module>" and only trustworthy when it
        # carries a real plugin ID; a bare "core.foo" must not become a plugin.
        plugin_name = LogRouter._extract_explicit_plugin_name(
            getattr(record, "source_file", None)
        )
        if plugin_name:
            return plugin_name

        try:
            message = record.getMessage()  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            message = getattr(record, "msg", "")
        match = re.match(r"\s*\[(astrbot_plugin_[A-Za-z0-9_]+)\]", str(message), re.IGNORECASE)
        return match.group(1) if match else None

    @staticmethod
    def get_source_type(pathname: str) -> str:
        """获取日志来源类型: plugin 或 core"""
        return "plugin" if LogRouter.is_plugin_path(pathname) else "core"

    @staticmethod
    def get_log_dir(data_dir: Path, pathname: str) -> Path:
        """根据日志来源获取日志目录"""
        if LogRouter.is_plugin_path(pathname):
            plugin_name = LogRouter.extract_plugin_name(pathname)
            if plugin_name:
                return data_dir / "plugins" / plugin_name
            return data_dir / "plugins" / "unknown"
        return data_dir / "core"
