"""Tests for the 2.3.0 export centre: windows, filters, masking and retention."""

import asyncio
import os
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.command_handler import CommandHandler, ExportSpec
from core.log_cleaner import LogCleaner
from core.sensitive_filter import SensitiveFilter


def _stamp(days_ago: float) -> str:
    moment = datetime.now() - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def _record(days_ago: float, level: str, message: str) -> str:
    return f"[{_stamp(days_ago)}] [{level}] [demo:1] {message}\n"


class ExportKernelTests(unittest.TestCase):
    """The shared kernel behind /log export and the WebUI export centre."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        (self.root / "all").mkdir(parents=True)
        (self.root / "errors").mkdir(parents=True)
        (self.root / "plugins" / "demo").mkdir(parents=True)
        # One file that spans the whole window, so day limits must trim it.
        self.spanning = self.root / "all" / "astrbot.log"
        self.spanning.write_text(
            _record(30, "INFO", "ancient line")
            + _record(9, "WARNING", "old warning")
            + _record(2, "INFO", "fresh line token=abcdef123456")
            + _record(0.2, "ERROR", "boom needle")
            ,
            encoding="utf-8",
        )
        self.errors = self.root / "errors" / "error.log"
        self.errors.write_text(
            # Chronological order, the way a real rotating log grows.
            _record(40, "ERROR", "stale failure") + _record(1, "ERROR", "recent failure"),
            encoding="utf-8",
        )
        self.plugin_log = self.root / "plugins" / "demo" / "plugin.log"
        self.plugin_log.write_text(_record(1, "INFO", "plugin alive"), encoding="utf-8")

    def tearDown(self):
        self._temp.cleanup()

    def handler(self, **kwargs) -> CommandHandler:
        return CommandHandler(self.root, LogCleaner(self.root, {}), **kwargs)

    @staticmethod
    def _zip_text(path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            return "\n".join(
                archive.read(name).decode("utf-8", "replace")
                for name in archive.namelist()
                if name != "ABOUT.txt"
            )

    # -- time window ----------------------------------------------------

    def test_day_window_actually_trims_records(self):
        """The 2.2.x bug: a day limit was ignored and everything shipped."""

        commands = self.handler()
        result = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=3, mask=False)
        )
        body = self._zip_text(Path(result["path"]))
        self.assertIn("fresh line", body)
        self.assertIn("boom needle", body)
        self.assertNotIn("ancient line", body)
        self.assertNotIn("old warning", body)

    def test_unlimited_window_keeps_every_record(self):
        commands = self.handler()
        result = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, mask=False)
        )
        body = self._zip_text(Path(result["path"]))
        self.assertIn("ancient line", body)
        self.assertIn("boom needle", body)

    def test_explicit_until_drops_newer_records(self):
        commands = self.handler()
        until = (datetime.now() - timedelta(days=1)).timestamp()
        result = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, until=until, mask=False)
        )
        body = self._zip_text(Path(result["path"]))
        self.assertIn("ancient line", body)
        self.assertNotIn("boom needle", body)

    def test_payload_parses_dates_and_the_unlimited_alias(self):
        spec = ExportSpec.from_payload(
            {"days": "all", "since": "2026-01-02", "until": "2026-01-03"}
        )
        self.assertIsNone(spec.days)
        self.assertIsNotNone(spec.since)
        # A date-only "until" must cover the whole day, not midnight.
        self.assertAlmostEqual(spec.until - spec.since, 172799.999999, places=3)

    def test_payload_rejects_impossible_requests(self):
        for payload in (
            {"scope": "nope"},
            {"preset": "nope"},
            {"format": "rar"},
            {"days": "-1"},
            {"days": "4000"},
            {"levels": ["LOUD"]},
            {"since": "not-a-date"},
            {"since": "2026-02-02", "until": "2026-02-01"},
            {"ids": ["a"] * (ExportSpec.MAX_IDS + 1)},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                ExportSpec.from_payload(payload)

    # -- content filters ------------------------------------------------

    def test_level_filter_keeps_only_the_requested_severities(self):
        commands = self.handler()
        result = commands.build_export(
            ExportSpec(
                scope="preset",
                preset="all",
                days=None,
                levels=("ERROR",),
                mask=False,
            )
        )
        body = self._zip_text(Path(result["path"]))
        self.assertIn("boom needle", body)
        self.assertIn("recent failure", body)
        self.assertNotIn("fresh line", body)
        self.assertNotIn("old warning", body)

    def test_keyword_filter_keeps_only_matching_records(self):
        commands = self.handler()
        result = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, keyword="needle", mask=False)
        )
        body = self._zip_text(Path(result["path"]))
        self.assertIn("boom needle", body)
        self.assertNotIn("plugin alive", body)

    def test_over_strict_filters_raise_instead_of_shipping_an_empty_bundle(self):
        commands = self.handler()
        with self.assertRaises(ValueError):
            commands.build_export(
                ExportSpec(scope="preset", preset="all", days=None, keyword="zzz-nope")
            )
        self.assertEqual([], commands.list_exports())

    # -- masking --------------------------------------------------------

    def test_build_export_masks_secrets_when_asked(self):
        commands = self.handler(sensitive_filter=SensitiveFilter())
        result = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, mask=True)
        )
        body = self._zip_text(Path(result["path"]))
        self.assertTrue(result["mask"])
        self.assertNotIn("abcdef123456", body)
        self.assertIn("token=***", body)

    def test_build_export_can_keep_the_raw_text(self):
        commands = self.handler(sensitive_filter=SensitiveFilter())
        result = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, mask=False)
        )
        self.assertFalse(result["mask"])
        self.assertIn("abcdef123456", self._zip_text(Path(result["path"])))

    def test_legacy_write_zip_also_masks(self):
        """/log send and /log export share _write_zip, which leaked secrets."""

        commands = self.handler(sensitive_filter=SensitiveFilter())
        target = self.root / "legacy.zip"
        count = commands._write_zip(
            target,
            [("current", self.root, self.spanning)],
            "legacy",
            masker=commands.masker(True),
        )
        self.assertEqual(1, count)
        self.assertNotIn("abcdef123456", self._zip_text(target))

    def test_masking_is_reported_as_unavailable_without_a_filter(self):
        commands = self.handler()
        preview = commands.plan_export(ExportSpec(scope="preset", preset="all"))
        self.assertFalse(preview["mask"])
        self.assertFalse(preview["masking_available"])

    # -- formats and scopes ---------------------------------------------

    def test_merged_format_produces_one_annotated_text_file(self):
        commands = self.handler()
        result = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, fmt="merged", mask=False)
        )
        path = Path(result["path"])
        self.assertEqual(".txt", path.suffix)
        text = path.read_text(encoding="utf-8")
        self.assertIn("LogVault", text)
        self.assertIn("=====", text)
        self.assertIn("boom needle", text)

    def test_errors_preset_only_covers_the_errors_tree(self):
        commands = self.handler()
        preview = commands.plan_export(
            ExportSpec(scope="preset", preset="errors", days=None)
        )
        self.assertEqual(1, preview["files"])

    def test_selection_scope_skips_out_of_tree_identifiers(self):
        commands = self.handler()
        good = commands.make_file_id("current", self.root, self.spanning)
        preview = commands.plan_export(
            ExportSpec(
                scope="selection",
                ids=(good, "current::../../etc/passwd", "nope::x.log"),
                days=None,
            )
        )
        self.assertEqual(1, preview["files"])
        self.assertEqual(2, len(preview["warnings"]))

    def test_selection_scope_requires_a_selection(self):
        with self.assertRaises(ValueError):
            self.handler().plan_export(ExportSpec(scope="selection"))

    def test_plugin_scope_finds_the_dedicated_directory(self):
        commands = self.handler()
        preview = commands.plan_export(
            ExportSpec(scope="plugin", plugin="demo", days=None)
        )
        self.assertEqual(1, preview["files"])
        self.assertIn("demo", preview["title"])

    def test_plan_reports_how_many_members_get_rewritten(self):
        commands = self.handler()
        preview = commands.plan_export(
            ExportSpec(scope="preset", preset="all", days=3)
        )
        # Every file survives the mtime pre-filter, but the two that reach
        # past the cutoff have to be re-rendered instead of copied.
        self.assertEqual(3, preview["files"])
        self.assertEqual(2, preview["trimmed"])

    # -- history --------------------------------------------------------

    def test_history_lists_and_purges_bundles(self):
        commands = self.handler()
        first = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, mask=False)
        )
        second = commands.build_export(
            ExportSpec(scope="preset", preset="all", days=None, fmt="merged", mask=False)
        )
        names = {item["name"] for item in commands.list_exports()}
        self.assertEqual({first["name"], second["name"]}, names)
        formats = {item["name"]: item["format"] for item in commands.list_exports()}
        self.assertEqual("zip", formats[first["name"]])
        self.assertEqual("merged", formats[second["name"]])

        purged = commands.delete_exports([first["name"], "ghost.zip"])
        self.assertEqual(1, purged["deleted"])
        self.assertEqual(1, purged["skipped"])
        self.assertEqual(1, len(commands.list_exports()))

        purged_all = commands.delete_exports(purge_all=True)
        self.assertEqual(1, purged_all["deleted"])
        self.assertEqual([], commands.list_exports())

    def test_resolve_export_refuses_traversal_and_foreign_suffixes(self):
        commands = self.handler()
        directory = commands.export_dir()
        (directory / "bundle.zip").write_bytes(b"PK\x05\x06" + bytes(18))
        (directory / "notes.md").write_text("hi", encoding="utf-8")
        self.assertIsNotNone(commands.resolve_export("bundle.zip"))
        for name in ("", ".", "..", "sub/bundle.zip", "..\\bundle.zip", "notes.md", "ghost.zip"):
            with self.subTest(name=name):
                self.assertIsNone(commands.resolve_export(name))


class ExportRetentionTests(unittest.TestCase):
    """data/exports used to grow forever; cleanup now covers it."""

    def test_cleanup_prunes_old_bundles_but_keeps_the_newest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exports = root / "exports"
            exports.mkdir()
            (root / "all").mkdir()
            log = root / "all" / "astrbot.log"
            log.write_text("keep me\n", encoding="utf-8")
            old_log_time = time.time() - 90 * 86400
            os.utime(log, (old_log_time, old_log_time))

            newest = exports / "logvault_export_new.zip"
            stale = exports / "logvault_export_old.zip"
            unrelated = exports / "README.md"
            for path in (newest, stale):
                path.write_bytes(b"x" * 32)
            unrelated.write_text("not a bundle", encoding="utf-8")
            aged = time.time() - 30 * 86400
            os.utime(stale, (aged, aged))
            os.utime(unrelated, (aged, aged))

            cleaner = LogCleaner(
                root,
                {
                    "enable_compression": False,
                    "auto_clean_enabled": False,
                    "export_retention_days": 7,
                },
            )
            result = asyncio.run(cleaner.cleanup())

            self.assertEqual(1, result["exports_deleted"])
            self.assertTrue(newest.exists())
            self.assertFalse(stale.exists())
            # Neither real logs nor foreign files are touched by this pass.
            self.assertTrue(log.exists())
            self.assertTrue(unrelated.exists())

    def test_count_limit_never_removes_the_last_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exports = root / "exports"
            exports.mkdir()
            for index in range(4):
                path = exports / f"logvault_export_{index}.zip"
                path.write_bytes(b"y" * 1024)
                aged = time.time() - index
                os.utime(path, (aged, aged))

            cleaner = LogCleaner(
                root,
                {
                    "enable_compression": False,
                    "auto_clean_enabled": False,
                    "export_retention_days": 0,
                    "export_max_files": 1,
                },
            )
            deleted, freed = cleaner._clean_exports()

            self.assertEqual(3, deleted)
            self.assertEqual(3 * 1024, freed)
            self.assertEqual(1, len(list(exports.glob("*.zip"))))

    def test_size_limit_keeps_at_least_one_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            exports = root / "exports"
            exports.mkdir()
            for index in range(3):
                path = exports / f"logvault_export_{index}.zip"
                path.write_bytes(b"z" * (2 * 1024 * 1024))
                aged = time.time() - index
                os.utime(path, (aged, aged))

            cleaner = LogCleaner(
                root,
                {
                    "enable_compression": False,
                    "auto_clean_enabled": False,
                    "export_retention_days": 0,
                    "export_max_files": 0,
                    "export_max_total_mb": 1,
                },
            )
            deleted, _freed = cleaner._clean_exports()

            self.assertEqual(2, deleted)
            self.assertEqual(1, len(list(exports.glob("*.zip"))))


class ExportTokenTests(unittest.TestCase):
    """The download bridge can only GET, so specs travel as one-shot tokens."""

    def setUp(self):
        from core.web_api import LogVaultWebApi

        self.api = LogVaultWebApi(plugin=object())

    def test_token_is_single_use(self):
        spec = ExportSpec(scope="preset", preset="all")
        token = self.api._remember_export(spec)
        self.assertIs(spec, self.api._take_export(token))
        self.assertIsNone(self.api._take_export(token))

    def test_expired_token_is_rejected(self):
        token = self.api._remember_export(ExportSpec())
        created, spec = self.api._export_tokens[token]
        self.api._export_tokens[token] = (
            created - self.api.EXPORT_TOKEN_TTL - 1,
            spec,
        )
        self.assertIsNone(self.api._take_export(token))

    def test_unknown_and_blank_tokens_are_rejected(self):
        for token in ("", "   ", "made-up"):
            with self.subTest(token=token):
                self.assertIsNone(self.api._take_export(token))

    def test_oldest_token_is_evicted_once_the_cache_is_full(self):
        tokens = [
            self.api._remember_export(ExportSpec())
            for _ in range(self.api.EXPORT_TOKEN_MAX + 2)
        ]
        self.assertLessEqual(len(self.api._export_tokens), self.api.EXPORT_TOKEN_MAX)
        self.assertIsNone(self.api._take_export(tokens[0]))
        self.assertIsNotNone(self.api._take_export(tokens[-1]))


if __name__ == "__main__":
    unittest.main()
