import asyncio
import gzip
import logging
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.command_handler import CommandHandler
from core.config_manager import ConfigManager
from core.log_cleaner import LogCleaner
from core.log_handler import (
    CompressedRotatingFileHandler,
    LogVaultHandler,
    archive_destination,
)
from core.sensitive_filter import SensitiveFilter


def _stamp(days_ago: float = 0.0) -> str:
    """A LogVault/AstrBot style timestamp relative to now."""

    moment = datetime.now() - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


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
            self.assertFalse(rotated.exists())
            # Slot numbers are recycled by every rollover, so the archive is
            # stamped from the source mtime instead of reusing ".1.gz".
            archives = list((root / "plugins" / "demo").glob("plugin.log.*.gz"))
            self.assertEqual(1, len(archives))
            self.assertRegex(archives[0].name, r"^plugin\.log\.\d{8}-\d{6}\.gz$")

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

    def test_astrbot_enriched_plugin_fields_are_routed(self):
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
                    "/srv/astrbot/core/log.py",
                    1315,
                    "[astrbot_plugin_dynamic_card_plus] reminder reached tool group=%s",
                    (932436510,),
                    None,
                )
                record.plugin_tag = "[astrbot_plugin_dynamic_card_plus]"
                record.source_file = "astrbot_plugin_dynamic_card_plus.main"
                handler.emit(record)
                output = (
                    root
                    / "plugins"
                    / "astrbot_plugin_dynamic_card_plus"
                    / "plugin.log"
                )
                self.assertTrue(output.exists())
                self.assertIn("reminder reached tool", output.read_text(encoding="utf-8"))
            finally:
                handler.close()

    def test_dedicated_astrbot_plugin_logger_is_captured(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {
                "enable_all_log": True,
                "enable_core_log": False,
                "enable_error_log": False,
                "enable_plugin_separation": True,
                "max_file_size_mb": 10,
                "backup_count": 2,
                "rotation_strategy": "size",
                "enable_compression": False,
            }
            handler = LogVaultHandler(root, config)
            plugin_logger = logging.getLogger(
                "astrbot.plugin.astrbot_plugin_dynamic_card_plus"
            )
            previous_handlers = list(plugin_logger.handlers)
            previous_level = plugin_logger.level
            previous_propagate = plugin_logger.propagate
            try:
                plugin_logger.handlers = []
                plugin_logger.setLevel(logging.INFO)
                plugin_logger.propagate = False
                plugin_logger.addHandler(handler)
                plugin_logger.info(
                    "[astrbot_plugin_dynamic_card_plus] set_group_card succeeded"
                )
                output = (
                    root
                    / "plugins"
                    / "astrbot_plugin_dynamic_card_plus"
                    / "plugin.log"
                )
                self.assertTrue(output.exists())
                self.assertIn(
                    "set_group_card succeeded",
                    output.read_text(encoding="utf-8"),
                )
            finally:
                plugin_logger.removeHandler(handler)
                plugin_logger.handlers = previous_handlers
                plugin_logger.setLevel(previous_level)
                plugin_logger.propagate = previous_propagate
                handler.close()

    def test_send_plugin_filters_astrbot_backend_log_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host_logs = root / "logs"
            host_logs.mkdir(parents=True)
            backend_log = host_logs / "astrbot.log"
            stamp = _stamp()
            backend_log.write_text(
                f"[{stamp}] [astrbot_plugin_dynamic_card_plus]\n"
                "[INFO]\n"
                "[astrbot_plugin_dynamic_card_plus.main:1315]: [astrbot_plugin_dynamic_card_plus] reminder reached tool\n"
                f"[{stamp}] [Core]\n"
                "[INFO]\n"
                "[runners.tool_loop_agent_runner:1349]: unrelated core record\n",
                encoding="utf-8",
            )

            command = CommandHandler(
                root / "plugin_data" / "astrbot_plugin_logvault",
                LogCleaner(root / "plugin_data" / "astrbot_plugin_logvault", {}),
                plugin_catalog_provider=lambda: {
                    "astrbot_plugin_dynamic_card_plus": {
                        "astrbot_plugin_dynamic_card_plus"
                    }
                },
                host_log_dirs=[host_logs],
            )
            message, archive_path = asyncio.run(
                command.handle_send("dynamic_card_plus", 1)
            )

            self.assertIsNotNone(archive_path)
            self.assertIn("共享日志中筛选", message)
            assert archive_path is not None
            with zipfile.ZipFile(archive_path) as result:
                contents = "\n".join(
                    result.read(name).decode("utf-8")
                    for name in result.namelist()
                    if name != "ABOUT.txt"
                )
            self.assertIn("reminder reached tool", contents)
            self.assertNotIn("unrelated core record", contents)

    def test_shared_log_fallback_applies_sensitive_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            host_logs = root / "logs"
            host_logs.mkdir(parents=True)
            (host_logs / "astrbot.log").write_text(
                f"[{_stamp()}] [astrbot_plugin_demo]\n"
                "[INFO]\n"
                "[astrbot_plugin_demo.main:1]: [astrbot_plugin_demo] token=real-secret\n",
                encoding="utf-8",
            )
            data_dir = root / "plugin_data" / "astrbot_plugin_logvault"
            command = CommandHandler(
                data_dir,
                LogCleaner(data_dir, {}),
                plugin_catalog_provider=lambda: {
                    "astrbot_plugin_demo": {"astrbot_plugin_demo"}
                },
                host_log_dirs=[host_logs],
                sensitive_filter=SensitiveFilter(["token"]),
            )

            _, archive_path = asyncio.run(command.handle_send("demo", 1))

            self.assertIsNotNone(archive_path)
            assert archive_path is not None
            with zipfile.ZipFile(archive_path) as result:
                contents = "\n".join(
                    result.read(name).decode("utf-8")
                    for name in result.namelist()
                    if name != "ABOUT.txt"
                )
            self.assertIn("token=***", contents)
            self.assertNotIn("real-secret", contents)

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

    def test_nested_legacy_plugin_log_file_is_read(self):
        """Keep compatibility with old exports containing plugin.log/plugin.log."""

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            nested = (
                root
                / "plugins"
                / "astrbot_plugin_dynamic_card_plus"
                / "plugin.log"
                / "plugin.log"
            )
            nested.parent.mkdir(parents=True)
            nested.write_text("recent nested log\n", encoding="utf-8")

            command = CommandHandler(root, LogCleaner(root, {}))
            message, archive_path = asyncio.run(
                command.handle_send("dynamic_card_plus", 1)
            )

            self.assertIsNotNone(archive_path)
            self.assertIn("astrbot_plugin_dynamic_card_plus", message)
            assert archive_path is not None
            with zipfile.ZipFile(archive_path) as result:
                names = result.namelist()
                contents = "\n".join(
                    result.read(name).decode("utf-8")
                    for name in names
                    if name != "ABOUT.txt"
                )
            self.assertIn(
                "plugins/astrbot_plugin_dynamic_card_plus/plugin.log/plugin.log",
                names,
            )
            self.assertIn("recent nested log", contents)

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

    def test_host_log_dirs_accept_multiline_configuration(self):
        config = ConfigManager({"host_log_dirs": "/srv/astrbot/logs\n/var/log/astrbot"})
        self.assertEqual(
            config.get_host_log_dirs(),
            ["/srv/astrbot/logs", "/var/log/astrbot"],
        )


class ArchiveRetentionTests(unittest.TestCase):
    """Rotation slots are recycled, so archives must not be keyed on them."""

    def test_repeated_rollovers_keep_every_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "all.log"
            handler = CompressedRotatingFileHandler(
                str(base),
                maxBytes=64,
                backupCount=1,
                encoding="utf-8",
                enable_compression=True,
            )
            try:
                for generation in range(4):
                    base.write_text(f"generation-{generation}\n" * 8, encoding="utf-8")
                    handler.doRollover()
            finally:
                handler.close()

            archives = sorted(root.glob("all.log.*.gz"))
            # backupCount=1 means every rollover but the first archives one
            # generation.  Reusing "all.log.1.gz" collapsed them into a single
            # file, so history disappeared without a trace.
            self.assertGreaterEqual(len(archives), 3)
            recovered = set()
            for archive in archives:
                with gzip.open(archive, "rt", encoding="utf-8") as stream:
                    recovered.add(stream.readline().strip())
            self.assertEqual(len(archives), len(recovered))

    def test_cleaner_archives_do_not_overwrite_handler_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "all.log.1"
            first.write_text("older\n", encoding="utf-8")
            aged = first.stat().st_mtime - 5 * 86400
            os.utime(first, (aged, aged))
            cleaner = LogCleaner(root, {"enable_compression": True})
            self.assertEqual(1, asyncio.run(cleaner._compress_old_logs(1)))

            # The next rollover recreates the same slot name.
            second = root / "all.log.1"
            second.write_text("newer\n", encoding="utf-8")
            aged = second.stat().st_mtime - 4 * 86400
            os.utime(second, (aged, aged))
            self.assertEqual(1, asyncio.run(cleaner._compress_old_logs(1)))

            archives = sorted(root.glob("all.log.*.gz"))
            self.assertEqual(2, len(archives))
            payloads = set()
            for archive in archives:
                with gzip.open(archive, "rt", encoding="utf-8") as stream:
                    payloads.add(stream.read().strip())
            self.assertEqual({"older", "newer"}, payloads)

    def test_same_second_archives_get_a_counter_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "all.log.1"
            source.write_text("payload\n", encoding="utf-8")
            stamp = source.stat().st_mtime
            taken = archive_destination(source, stamp)
            taken.write_bytes(b"")
            self.assertEqual(f"{taken.name[:-3]}-1.gz", archive_destination(source, stamp).name)

    def test_time_rotation_names_keep_their_own_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "all.log.2026-09-01"
            source.write_text("payload\n", encoding="utf-8")
            self.assertEqual("all.log.2026-09-01.gz", archive_destination(source).name)


class CleanupReportTests(unittest.TestCase):
    """A pass with nothing to do must say why instead of reporting bare zeros."""

    def test_report_explains_why_nothing_was_touched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "all").mkdir()
            active = root / "all" / "all.log"
            fresh = root / "all" / "all.log.1"
            archived = root / "all" / "all.log.20260101-000000.gz"
            active.write_text("active\n", encoding="utf-8")
            fresh.write_text("rotated\n", encoding="utf-8")
            archived.write_bytes(b"gz")
            recent = fresh.stat().st_mtime - 6 * 3600
            os.utime(fresh, (recent, recent))

            cleaner = LogCleaner(root, {"compression_after_days": 1})
            result = asyncio.run(cleaner.cleanup())

            self.assertEqual(0, result["compressed"])
            self.assertEqual(0, result["deleted"])
            self.assertEqual(3, result["scanned"])
            self.assertEqual(1, result["skipped"]["active"])
            self.assertEqual(1, result["skipped"]["already_compressed"])
            self.assertEqual(1, result["skipped"]["too_new"])
            self.assertEqual(1, result["thresholds"]["compression_after_days"])
            self.assertEqual(30, result["thresholds"]["max_age_days"])
            self.assertEqual(500, result["thresholds"]["max_total_size_mb"])
            # 6 hours old with a 1 day threshold leaves roughly 18 hours.
            self.assertAlmostEqual(18.0, result["next_compress_in_hours"], delta=0.5)
            self.assertGreater(result["total_bytes"], 0)

    def test_disabled_passes_report_no_thresholds(self):
        with tempfile.TemporaryDirectory() as temp:
            cleaner = LogCleaner(
                Path(temp),
                {"enable_compression": False, "auto_clean_enabled": False},
            )
            result = asyncio.run(cleaner.cleanup())

            self.assertIsNone(result["thresholds"]["compression_after_days"])
            self.assertIsNone(result["thresholds"]["max_age_days"])
            self.assertIsNone(result["next_compress_in_hours"])
            self.assertEqual(0, result["scanned"])


class ForcedCompressionTests(unittest.TestCase):
    """"Clean now" from the console must not obey the overnight delay."""

    def test_forced_pass_archives_todays_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "all").mkdir()
            (root / "all" / "all.log").write_text("active\n", encoding="utf-8")
            rotated = root / "all" / "all.log.1"
            rotated.write_text("rotated\n" * 50, encoding="utf-8")
            recent = rotated.stat().st_mtime - 6 * 3600
            os.utime(rotated, (recent, recent))

            cleaner = LogCleaner(root, {"compression_after_days": 1})
            result = asyncio.run(cleaner.cleanup(force_compress=True))

            self.assertEqual(1, result["compressed"])
            self.assertTrue(result["forced"])
            self.assertFalse(rotated.exists())
            archives = list((root / "all").glob("all.log.*.gz"))
            self.assertEqual(1, len(archives))
            # The report still shows the configured delay: the override was a
            # one-off, not a settings change.
            self.assertEqual(1, result["thresholds"]["compression_after_days"])
            self.assertEqual(0, result["skipped"]["too_new"])
            self.assertIsNone(result["next_compress_in_hours"])

    def test_forced_pass_leaves_the_active_stream_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "all").mkdir()
            active = root / "all" / "all.log"
            active.write_text("active\n", encoding="utf-8")

            cleaner = LogCleaner(root, {})
            result = asyncio.run(cleaner.cleanup(force_compress=True))

            self.assertEqual(0, result["compressed"])
            self.assertTrue(active.exists())
            self.assertFalse(active.with_suffix(".log.gz").exists())
            self.assertEqual(1, result["skipped"]["active"])


class PurgeAllTests(unittest.TestCase):
    """The console's "purge logs" button empties the tree in a single pass."""

    def test_every_closed_file_goes_and_active_streams_stay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "all").mkdir()
            (root / "plugins" / "demo").mkdir(parents=True)
            (root / "exports").mkdir()
            active = root / "all" / "all.log"
            fallback = root / "plugins" / "demo" / "plugin.log.active"
            rotated = root / "all" / "all.log.1"
            plugin_rotated = root / "plugins" / "demo" / "plugin.log.1"
            archived = root / "all" / "all.log.20260101-000000.gz"
            bundle = root / "exports" / "logvault_20260101.zip"
            # ".log." in the middle of the name makes this look like a log to
            # the name filter; only the exports guard keeps it alive.
            plain_export = root / "exports" / "logvault_all.log.txt"
            for path in (active, fallback, rotated, plugin_rotated):
                path.write_text(path.name + "\n", encoding="utf-8")
            archived.write_bytes(b"gz-payload")
            bundle.write_bytes(b"zip-payload")
            plain_export.write_text("exported\n", encoding="utf-8")
            doomed = (rotated, plugin_rotated, archived)
            doomed_bytes = sum(path.stat().st_size for path in doomed)
            scanned_bytes = doomed_bytes + sum(
                path.stat().st_size for path in (active, fallback)
            )

            cleaner = LogCleaner(root, {})
            result = asyncio.run(cleaner.purge_all())

            self.assertEqual("purge", result["mode"])
            self.assertEqual(3, result["deleted"])
            self.assertEqual(doomed_bytes, result["freed_bytes"])
            self.assertEqual(0, result["compressed"])
            self.assertEqual(0, result["exports_deleted"])
            self.assertEqual(5, result["scanned"])
            self.assertEqual(scanned_bytes, result["total_bytes"])
            self.assertEqual(2, result["skipped"]["active"])
            # The pass obeyed no threshold, so echoing the configured values
            # would misdescribe what just happened.
            self.assertNotIn("thresholds", result)
            self.assertNotIn("next_compress_in_hours", result)
            for path in doomed:
                self.assertFalse(path.exists(), path.name)
            for path in (active, fallback, bundle, plain_export):
                self.assertTrue(path.exists(), path.name)

    def test_purge_ignores_the_retention_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "all").mkdir()
            rotated = root / "all" / "all.log.1"
            rotated.write_text("rotated\n", encoding="utf-8")
            cleaner = LogCleaner(
                root,
                {
                    "enable_compression": False,
                    "auto_clean_enabled": False,
                    "max_age_days": 3650,
                },
            )

            # Routine maintenance has nothing to do with this configuration...
            maintenance = asyncio.run(cleaner.cleanup())
            self.assertEqual(0, maintenance["compressed"])
            self.assertEqual(0, maintenance["deleted"])
            self.assertTrue(rotated.exists())

            # ...but a purge is an explicit order, not a policy evaluation.
            result = asyncio.run(cleaner.purge_all())
            self.assertEqual(1, result["deleted"])
            self.assertFalse(rotated.exists())

    def test_empty_tree_reports_zeros_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            result = asyncio.run(LogCleaner(Path(temp), {}).purge_all())

            self.assertEqual(0, result["scanned"])
            self.assertEqual(0, result["deleted"])
            self.assertEqual(0, result["freed_bytes"])
            self.assertEqual(0, result["total_bytes"])
            self.assertEqual(0, result["skipped"]["active"])


if __name__ == "__main__":
    unittest.main()
