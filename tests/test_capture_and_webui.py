"""Tests for the 2.1.0 additions: loguru capture, day slicing and the WebUI API."""

import asyncio
import gzip
import logging
import sys
import tempfile
import zipfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.command_handler import CommandHandler
from core.log_cleaner import LogCleaner
from core.loguru_capture import BootstrapBackfill, LoguruCapture


def _stamp(days_ago: float = 0.0) -> str:
    moment = datetime.now() - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


class _Collector(logging.Handler):
    """Minimal handler that keeps the records it receives."""

    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(record)


class DaySlicingTests(unittest.TestCase):
    """Regression tests for "send/export <days> only ever packed everything"."""

    def _prepare(self, temp: str) -> Path:
        root = Path(temp)
        target = root / "all" / "all.log"
        target.parent.mkdir(parents=True)
        target.write_text(
            "\n".join(
                [
                    f"[{_stamp(9)}] [INFO] [core:1] nine days ago",
                    f"[{_stamp(5)}] [INFO] [core:2] five days ago",
                    f"[{_stamp(0.2)}] [INFO] [core:3] a few hours ago",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return root

    def _archive_text(self, archive_path) -> str:
        with zipfile.ZipFile(archive_path) as result:
            return "\n".join(
                result.read(name).decode("utf-8")
                for name in result.namelist()
                if name != "ABOUT.txt"
            )

    def test_active_log_is_sliced_by_record_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._prepare(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            _message, archive_path = asyncio.run(command.handle_send("all", 3))
            self.assertIsNotNone(archive_path)
            contents = self._archive_text(archive_path)
            self.assertIn("a few hours ago", contents)
            self.assertNotIn("five days ago", contents)
            self.assertNotIn("nine days ago", contents)

    def test_larger_window_keeps_more_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._prepare(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            _message, archive_path = asyncio.run(command.handle_send("all", 7))
            contents = self._archive_text(archive_path)
            self.assertIn("five days ago", contents)
            self.assertNotIn("nine days ago", contents)

    def test_days_accepts_string_input(self):
        """AstrBot hands command arguments over as raw strings."""

        with tempfile.TemporaryDirectory() as temp:
            root = self._prepare(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            message, archive_path = asyncio.run(command.handle_send("all", "3"))
            self.assertIn("最近 3 天", message)
            contents = self._archive_text(archive_path)
            self.assertNotIn("five days ago", contents)

    def test_slicing_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._prepare(temp)
            command = CommandHandler(
                root, LogCleaner(root, {}), slice_by_record_time=False
            )

            _message, archive_path = asyncio.run(command.handle_send("all", 3))
            contents = self._archive_text(archive_path)
            self.assertIn("nine days ago", contents)

    def test_files_without_timestamps_are_kept_whole(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "all" / "all.log"
            target.parent.mkdir(parents=True)
            target.write_text("no timestamp here\nanother line\n", encoding="utf-8")

            command = CommandHandler(root, LogCleaner(root, {}))
            _message, archive_path = asyncio.run(command.handle_send("all", 1))
            contents = self._archive_text(archive_path)
            self.assertIn("no timestamp here", contents)
            self.assertIn("another line", contents)

    def test_fully_recent_compressed_member_is_copied_verbatim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archived = root / "all" / "all.log.1.gz"
            archived.parent.mkdir(parents=True)
            with gzip.open(archived, "wb") as stream:
                stream.write(f"[{_stamp(0.2)}] [INFO] [core:1] rotated today\n".encode())

            command = CommandHandler(root, LogCleaner(root, {}))
            _message, archive_path = asyncio.run(command.handle_send("all", 1))
            with zipfile.ZipFile(archive_path) as result:
                self.assertIn("all/all.log.1.gz", result.namelist())
                payload = result.read("all/all.log.1.gz")
            self.assertEqual(payload, archived.read_bytes())

    def test_compressed_member_spanning_cutoff_is_sliced_to_plain_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archived = root / "all" / "all.log.1.gz"
            archived.parent.mkdir(parents=True)
            with gzip.open(archived, "wb") as stream:
                stream.write(
                    (
                        f"[{_stamp(9)}] [INFO] [core:1] very old\n"
                        f"[{_stamp(0.2)}] [INFO] [core:2] still fresh\n"
                    ).encode()
                )

            command = CommandHandler(root, LogCleaner(root, {}))
            _message, archive_path = asyncio.run(command.handle_send("all", 1))
            with zipfile.ZipFile(archive_path) as result:
                names = result.namelist()
                self.assertIn("all/all.log.1", names)
                self.assertNotIn("all/all.log.1.gz", names)
                body = result.read("all/all.log.1").decode("utf-8")
            self.assertIn("still fresh", body)
            self.assertNotIn("very old", body)

    def test_export_forwards_string_days(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._prepare(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            message = asyncio.run(command.handle_export("2"))
            self.assertIn("导出完成", message)
            self.assertIn("包含: 1 个日志文件", message)
            exported = sorted((root / "exports").glob("logs_export_*.zip"))
            self.assertTrue(exported)
            contents = self._archive_text(exported[-1])
            with zipfile.ZipFile(exported[-1]) as result:
                about = result.read("ABOUT.txt").decode("utf-8")
            self.assertIn("最近 2 天日志", about)
            self.assertIn("a few hours ago", contents)
            self.assertNotIn("five days ago", contents)

    def test_invalid_days_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._prepare(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            message, archive_path = asyncio.run(command.handle_send("all", "abc"))
            self.assertIsNone(archive_path)
            self.assertTrue(message.startswith("❌"))


class CaptureTests(unittest.TestCase):
    def test_loguru_record_is_converted(self):
        collector = _Collector()
        capture = LoguruCapture(collector)
        moment = datetime.now()
        source = {
            "name": "astrbot",
            "level": type("Lvl", (), {"name": "SUCCESS", "no": 25})(),
            "message": "[astrbot_plugin_demo] loaded",
            "file": type("F", (), {"path": "/srv/astrbot/core/log.py"})(),
            "line": 12,
            "function": "info",
            "time": moment,
            "extra": {
                "plugin_tag": "[astrbot_plugin_demo]",
                "source_file": "astrbot_plugin_demo.main",
                "source_line": 88,
            },
            "exception": None,
        }

        record = capture._build_record(source)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.levelname, "SUCCESS")
        self.assertEqual(record.levelno, 25)
        self.assertEqual(record.plugin_tag, "[astrbot_plugin_demo]")
        self.assertEqual(record.lineno, 88)
        self.assertEqual(record.filename, "astrbot_plugin_demo.main")
        self.assertAlmostEqual(record.created, moment.timestamp(), places=3)
        self.assertTrue(record.logvault_via_loguru)

    def test_trace_records_are_dropped_unless_enabled(self):
        collector = _Collector()
        source = {
            "name": "astrbot",
            "level": type("Lvl", (), {"name": "TRACE", "no": 5})(),
            "message": "noisy",
            "file": type("F", (), {"path": "x.py"})(),
            "line": 1,
            "function": "f",
            "time": datetime.now(),
            "extra": {},
            "exception": None,
        }
        self.assertIsNone(LoguruCapture(collector)._build_record(source))
        self.assertIsNotNone(
            LoguruCapture(collector, include_trace=True)._build_record(source)
        )

    def test_backfill_entry_restores_routing_hints(self):
        entry = {
            "level": "WARNING",
            "time": datetime.now().timestamp(),
            "data": (
                "\x1b[32m[2026-08-27 10:00:00.123]\x1b[0m "
                "[astrbot_plugin_demo] [demo.main:42] something happened"
            ),
            "category": "plugin",
        }

        record = BootstrapBackfill.build_record(entry)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.levelname, "WARNING")
        self.assertEqual(record.plugin_tag, "[astrbot_plugin_demo]")
        self.assertEqual(record.source_file, "demo.main")
        self.assertEqual(record.source_line, 42)
        self.assertNotIn("\x1b", record.getMessage())
        self.assertNotIn("2026-08-27", record.getMessage())
        self.assertTrue(record.logvault_backfill)

    def test_backfill_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entries = [
                {"level": "INFO", "time": 1000.0, "data": "first", "category": "system"},
                {"level": "INFO", "time": 2000.0, "data": "second", "category": "system"},
            ]
            backfill = BootstrapBackfill(root)
            backfill.cached_entries = lambda: entries  # type: ignore[method-assign]
            collector = _Collector()

            self.assertEqual(backfill.replay(collector), 2)
            self.assertEqual(backfill.replay(collector), 0)

            entries.append(
                {"level": "INFO", "time": 3000.0, "data": "third", "category": "system"}
            )
            self.assertEqual(backfill.replay(collector), 1)
            self.assertEqual(len(collector.records), 3)

    def test_empty_backfill_lines_are_skipped(self):
        self.assertIsNone(
            BootstrapBackfill.build_record(
                {"level": "INFO", "time": 1.0, "data": "[2026-08-27 10:00:00.123]"}
            )
        )


class WebApiSurfaceTests(unittest.TestCase):
    def _seed(self, temp: str) -> Path:
        root = Path(temp)
        (root / "all").mkdir(parents=True)
        (root / "errors").mkdir(parents=True)
        (root / "plugins" / "astrbot_plugin_demo").mkdir(parents=True)
        (root / "all" / "all.log").write_text(
            "\n".join(
                [
                    f"[{_stamp(0.1)}] [DEBUG] [core:1] debug line",
                    f"[{_stamp(0.1)}] [INFO] [core:2] info line token=real-secret",
                    f"[{_stamp(0.1)}] [ERROR] [core:3] error line",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "errors" / "error.log.1").write_text("rotated error\n", encoding="utf-8")
        (root / "plugins" / "astrbot_plugin_demo" / "plugin.log").write_text(
            "plugin line\n", encoding="utf-8"
        )
        return root

    def test_categories_are_grouped_by_source_and_kind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            categories = command.list_categories()
            keys = {(item["source"], item["key"]) for item in categories}
            self.assertIn(("current", CommandHandler.ALL_CATEGORY), keys)
            self.assertIn(("current", "all"), keys)
            self.assertIn(("current", "errors"), keys)
            self.assertIn(("current", "plugins/astrbot_plugin_demo"), keys)

            by_key = {item["key"]: item for item in categories}
            self.assertEqual(by_key["plugins/astrbot_plugin_demo"]["kind"], "plugin")
            self.assertEqual(by_key["errors"]["kind"], "builtin")
            self.assertEqual(by_key[CommandHandler.ALL_CATEGORY]["count"], 3)

    def test_list_files_reports_delete_eligibility(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            files = {item["relative"]: item for item in command.list_files("current")}
            self.assertTrue(files["all/all.log"]["active"])
            self.assertFalse(files["all/all.log"]["deletable"])
            self.assertTrue(files["errors/error.log.1"]["deletable"])
            self.assertEqual(
                files["plugins/astrbot_plugin_demo/plugin.log"]["category"],
                "plugins/astrbot_plugin_demo",
            )

    def test_read_file_lines_filters_by_level_and_masks_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            from core.sensitive_filter import SensitiveFilter

            command = CommandHandler(
                root, LogCleaner(root, {}), sensitive_filter=SensitiveFilter(["token"])
            )
            file_id = CommandHandler.make_file_id(
                "current", root, root / "all" / "all.log"
            )

            payload = command.read_file_lines(file_id, tail=100, level="ERROR")
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(len(payload["lines"]), 1)
            self.assertIn("error line", payload["lines"][0])

            payload = command.read_file_lines(file_id, tail=100, keyword="token")
            assert payload is not None
            self.assertNotIn("real-secret", "\n".join(payload["lines"]))

    def test_tail_bytes_returns_only_new_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            command = CommandHandler(root, LogCleaner(root, {}))
            target = root / "all" / "all.log"
            file_id = CommandHandler.make_file_id("current", root, target)

            first = command.tail_bytes(file_id, position=0)
            assert first is not None
            self.assertTrue(first["supported"])
            with target.open("a", encoding="utf-8") as stream:
                stream.write("fresh line\n")

            second = command.tail_bytes(file_id, position=first["position"])
            assert second is not None
            self.assertEqual(second["lines"], ["fresh line"])

    def test_tail_bytes_flags_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            command = CommandHandler(root, LogCleaner(root, {}))
            target = root / "all" / "all.log"
            file_id = CommandHandler.make_file_id("current", root, target)

            payload = command.tail_bytes(file_id, position=10_000_000)
            assert payload is not None
            self.assertTrue(payload["reset"])

    def test_resolve_file_rejects_escapes_and_unknown_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            command = CommandHandler(root, LogCleaner(root, {}))
            separator = CommandHandler.FILE_ID_SEPARATOR

            self.assertIsNone(command.resolve_file("all/all.log"))
            self.assertIsNone(command.resolve_file(f"current{separator}../secret.log"))
            self.assertIsNone(command.resolve_file(f"current{separator}..\\secret.log"))
            self.assertIsNone(command.resolve_file(f"nope{separator}all/all.log"))
            self.assertIsNone(command.resolve_file(f"current{separator}all/missing.log"))
            self.assertIsNotNone(command.resolve_file(f"current{separator}all/all.log"))

    def test_delete_files_protects_active_and_foreign_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            host = Path(temp) / "host_logs"
            host.mkdir()
            (host / "astrbot.log.1").write_text("host\n", encoding="utf-8")
            command = CommandHandler(
                root, LogCleaner(root, {}), host_log_dirs=[str(host)]
            )
            separator = CommandHandler.FILE_ID_SEPARATOR

            host_id = next(
                item["id"]
                for item in command.list_files()
                if item["source_kind"] == "host"
            )
            result = command.delete_files(
                [
                    f"current{separator}all/all.log",
                    host_id,
                    "bogus",
                    f"current{separator}errors/error.log.1",
                ]
            )

            self.assertEqual(result["deleted"], 1)
            self.assertEqual(len(result["skipped"]), 3)
            self.assertTrue((root / "all" / "all.log").exists())
            self.assertTrue((host / "astrbot.log.1").exists())
            self.assertFalse((root / "errors" / "error.log.1").exists())

    def test_overview_payload_exposes_sources_and_categories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self._seed(temp)
            command = CommandHandler(root, LogCleaner(root, {}))

            payload = command.overview_payload()
            self.assertEqual(payload["data_dir"], str(root))
            self.assertTrue(payload["slice_by_record_time"])
            self.assertIn("current", {item["label"] for item in payload["sources"]})
            self.assertTrue(payload["categories"])


class CleanerIntervalTests(unittest.TestCase):
    def test_interval_defaults_and_clamping(self):
        root = Path(tempfile.gettempdir())
        self.assertEqual(LogCleaner(root, {})._interval_seconds(), 3600.0)
        self.assertEqual(
            LogCleaner(root, {"clean_interval_minutes": 5})._interval_seconds(), 300.0
        )
        self.assertEqual(
            LogCleaner(root, {"clean_interval_minutes": 0})._interval_seconds(), 60.0
        )
        self.assertEqual(
            LogCleaner(root, {"clean_interval_minutes": 99999})._interval_seconds(),
            10080 * 60.0,
        )
        self.assertEqual(
            LogCleaner(root, {"clean_interval_minutes": "oops"})._interval_seconds(),
            3600.0,
        )


if __name__ == "__main__":
    unittest.main()
