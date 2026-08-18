import asyncio
import gzip
import logging
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.command_handler import CommandHandler
from core.log_cleaner import LogCleaner
from core.log_handler import LogVaultHandler
from core.sensitive_filter import SensitiveFilter


class LogVaultBehaviourTests(unittest.TestCase):
    def test_linux_active_log_is_not_compressed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            active = root / "plugins" / "demo" / "plugin.log"
            rotated = root / "plugins" / "demo" / "plugin.log.1"
            active.parent.mkdir(parents=True)
            active.write_text("active\n", encoding="utf-8")
            rotated.write_text("rotated\n", encoding="utf-8")
            old = active.stat().st_mtime - 3 * 86400
            os.utime(active, (old, old))
            os.utime(rotated, (old, old))

            cleaner = LogCleaner(root, {"enable_compression": True})
            compressed = asyncio.run(cleaner._compress_old_logs(1))

            self.assertEqual(compressed, 1)
            self.assertTrue(active.exists())
            self.assertTrue((root / "plugins" / "demo" / "plugin.log.1.gz").exists())

    def test_dynamic_card_log_after_start_can_be_sent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "enable_all_log": False,
                "enable_core_log": False,
                "enable_error_log": False,
                "enable_plugin_separation": True,
                "max_file_size_mb": 10,
                "backup_count": 2,
                "rotation_strategy": "size",
                "enable_compression": False,
            }
            handler = LogVaultHandler(root, config)
            try:
                record = logging.LogRecord(
                    "astrbot",
                    logging.INFO,
                    "/srv/astrbot/data/plugins/astrbot_plugin_dynamic_card_plus/main.py",
                    10,
                    "recent message",
                    (),
                    None,
                )
                handler.emit(record)
                output = (
                    root
                    / "plugins"
                    / "astrbot_plugin_dynamic_card_plus"
                    / "plugin.log"
                )
                self.assertTrue(output.exists())
                self.assertIn("recent message", output.read_text(encoding="utf-8"))
            finally:
                handler.close()

            command = CommandHandler(
                root,
                LogCleaner(root, {}),
                plugin_catalog_provider=lambda: {
                    "astrbot_plugin_dynamic_card_plus": {
                        "astrbot_plugin_dynamic_card_plus",
                        "Dynamic Card Plus",
                    }
                },
            )
            message, archive_path = asyncio.run(
                command.handle_send("dynamic_card_plus", 3)
            )
            self.assertIsNotNone(archive_path)
            self.assertIn("最近 3 天", message)

    def test_send_plugin_filters_by_days(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_dir = root / "plugins" / "astrbot_plugin_demo"
            plugin_dir.mkdir(parents=True)
            recent = plugin_dir / "plugin.log"
            old = plugin_dir / "plugin.log.1.gz"
            recent.write_text("recent", encoding="utf-8")
            with gzip.open(old, "wb") as archive:
                archive.write(b"old")
            old_timestamp = recent.stat().st_mtime - 10 * 86400
            os.utime(old, (old_timestamp, old_timestamp))

            command = CommandHandler(root, LogCleaner(root, {}))
            message, archive_path = asyncio.run(command.handle_send("demo", 3))

            self.assertIsNotNone(archive_path)
            self.assertIn("最近 3 天", message)
            assert archive_path is not None
            with zipfile.ZipFile(archive_path) as result:
                names = result.namelist()
            self.assertIn("plugins/astrbot_plugin_demo/plugin.log", names)
            self.assertNotIn("plugins/astrbot_plugin_demo/plugin.log.1.gz", names)

    def test_installed_plugin_is_recognized_before_its_first_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "plugins" / "astrbot_plugin_logvault"
            existing.mkdir(parents=True)
            (existing / "plugin.log").write_text("logvault", encoding="utf-8")

            command = CommandHandler(
                root,
                LogCleaner(root, {}),
                plugin_catalog_provider=lambda: {
                    "astrbot_plugin_dynamic_card_plus": {
                        "astrbot_plugin_dynamic_card_plus",
                        "Dynamic Card Plus",
                    },
                    "astrbot_plugin_logvault": {"LogVault"},
                },
            )
            message, archive_path = asyncio.run(
                command.handle_send("dynamic_card_plus", 3)
            )

            self.assertIsNone(archive_path)
            self.assertIn("已识别插件 'astrbot_plugin_dynamic_card_plus'", message)
            self.assertIn("没有捕获到", message)
            self.assertNotIn("未找到匹配", message)

    def test_registered_plugin_alias_uses_existing_log_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_dir = root / "plugins" / "astrbot_plugin_dynamic_card_plus"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.log").write_text("recent", encoding="utf-8")

            command = CommandHandler(
                root,
                LogCleaner(root, {}),
                plugin_catalog_provider=lambda: {
                    "astrbot_plugin_dynamic_card_plus": {
                        "astrbot_plugin_dynamic_card_plus",
                        "Dynamic Card Plus",
                    }
                },
            )
            message, archive_path = asyncio.run(
                command.handle_send("Dynamic Card Plus", 3)
            )

            self.assertIsNotNone(archive_path)
            self.assertIn("插件 astrbot_plugin_dynamic_card_plus 日志", message)

    def test_parameterized_sensitive_log_keeps_numeric_formatting(self):
        record = logging.LogRecord(
            "astrbot",
            logging.INFO,
            "plugin.py",
            1,
            "token=%s count=%d",
            ("secret-value", 3),
            None,
        )
        masked = SensitiveFilter(["token"]).mask_record(record)
        self.assertEqual(masked.args, ())
        self.assertIn("token=***", masked.getMessage())
        self.assertIn("count=3", masked.getMessage())


if __name__ == "__main__":
    unittest.main()
