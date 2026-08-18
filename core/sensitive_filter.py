import copy
import logging
import re
from typing import ClassVar


class SensitiveFilter:
    """敏感信息脱敏处理器

    注意: 此类不再继承 logging.Filter，而是作为独立的脱敏工具。
    应在 Handler.emit 中对复制的 LogRecord 调用 mask_record 方法，
    以避免影响其他 Handler。

    仅支持 f-string 格式日志中 key=value 模式的脱敏。
    对于参数化日志 (logger.info("%s", value))，会对 args 中的值进行独立检测。
    """

    DEFAULT_KEYWORDS: ClassVar[list[str]] = [
        "token",
        "password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
        "access_key",
        "accesskey",
        "private_key",
        "privatekey",
        "credential",
        "auth",
    ]

    MASK: ClassVar[str] = "***"

    def __init__(self, keywords: list[str] | None = None, enabled: bool = True):
        self.enabled = enabled
        self.keywords = [str(item).strip() for item in (keywords or self.DEFAULT_KEYWORDS) if str(item).strip()]
        self._compile_patterns()

    def _compile_patterns(self):
        """编译正则表达式模式"""
        self.patterns = []

        for keyword in self.keywords:
            escaped_keyword = re.escape(keyword)
            # 匹配 key=value 或 key: value 或 "key": "value"
            patterns = [
                # key=value
                rf'({escaped_keyword})\s*=\s*["\']?([^"\'\s,}}\]]+)["\']?',
                # key: value
                rf'({escaped_keyword})\s*:\s*["\']?([^"\'\s,}}\]]+)["\']?',
                # "key": "value"
                rf'["\']({escaped_keyword})["\']\s*:\s*["\']([^"\']+)["\']',
            ]
            for p in patterns:
                self.patterns.append(re.compile(p, re.IGNORECASE))

    def mask_record(self, record: logging.LogRecord) -> logging.LogRecord:
        """对 LogRecord 进行脱敏处理，返回副本以避免影响其他 Handler"""
        if not self.enabled:
            return record

        masked_record = copy.copy(record)
        try:
            # Render first so parameterised records such as
            # ``logger.info("token=%s count=%d", token, count)`` are masked
            # without converting the integer argument into a string and
            # breaking logging's later ``%`` interpolation.
            rendered = record.getMessage()
        except Exception:
            rendered = str(getattr(record, "msg", ""))
        masked_record.msg = self._mask_sensitive(rendered)
        masked_record.args = ()
        return masked_record

    def _mask_sensitive(self, text: str) -> str:
        """脱敏敏感信息"""
        result = text
        for pattern in self.patterns:
            result = pattern.sub(rf"\1={self.MASK}", result)
        return result

    def mask_text(self, text: str) -> str:
        """Mask a text block such as a shared AstrBot log fallback."""

        if not self.enabled:
            return text
        return self._mask_sensitive(str(text))

    def update_keywords(self, keywords: list[str]):
        """更新敏感词列表"""
        self.keywords = keywords
        self._compile_patterns()
