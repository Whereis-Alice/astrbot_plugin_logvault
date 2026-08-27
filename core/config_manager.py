from typing import Any, ClassVar


class ConfigManager:
    """配置管理器"""

    DEFAULTS: ClassVar[dict[str, Any]] = {
        "log_level": "DEBUG",
        "capture_mode": "auto",
        "capture_third_party": True,
        "force_source_debug": False,
        "include_trace_log": False,
        "backfill_startup_logs": True,
        "backfill_limit": 500,
        "max_file_size_mb": 10,
        "backup_count": 5,
        "rotation_strategy": "size",
        "rotation_interval": "daily",
        "enable_all_log": True,
        "enable_core_log": True,
        "enable_error_log": True,
        "enable_plugin_separation": True,
        "enable_compression": True,
        "compression_after_days": 1,
        "auto_clean_enabled": True,
        "max_total_size_mb": 500,
        "max_age_days": 30,
        "clean_interval_minutes": 60,
        "enable_sensitive_filter": True,
        "sensitive_keywords": "token,password,secret,api_key,apikey,access_key,accesskey",
        "include_legacy_data": True,
        "legacy_data_dirs": [],
        "host_log_dirs": [],
        "slice_by_record_time": True,
    }

    def __init__(self, config: dict):
        self._config = config or {}

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持默认值"""
        if key in self._config:
            return self._config[key]
        if key in self.DEFAULTS:
            return self.DEFAULTS[key]
        return default

    def get_sensitive_keywords(self) -> list[str]:
        """获取敏感词列表"""
        keywords_str = self.get("sensitive_keywords", "")
        if isinstance(keywords_str, str):
            return [k.strip() for k in keywords_str.split(",") if k.strip()]
        return keywords_str if isinstance(keywords_str, list) else []

    def get_legacy_data_dirs(self) -> list[str]:
        """Return explicitly configured read-only legacy data directories."""

        value = self.get("legacy_data_dirs", [])
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def get_host_log_dirs(self) -> list[str]:
        """Return extra shared-log directories used by plugin filtering."""

        value = self.get("host_log_dirs", [])
        if isinstance(value, str):
            value = value.splitlines()
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def as_dict(self) -> dict:
        """返回完整配置字典"""
        result = dict(self.DEFAULTS)
        result.update(self._config)
        return result

    def update(self, config: dict):
        """更新配置"""
        self._config.update(config)
