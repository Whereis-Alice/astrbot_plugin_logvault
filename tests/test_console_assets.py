"""Consistency tests for the 2.2.0 console assets and the /log command entry."""

import json
import re
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.command_handler import CommandHandler
from core.log_cleaner import LogCleaner

PAGE_DIR = PLUGIN_ROOT / "pages" / "logs"
I18N_DIR = PLUGIN_ROOT / ".astrbot-plugin" / "i18n"


def _flatten(node, prefix=""):
    """Collect the dotted leaf paths of a nested translation document."""

    keys = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys |= _flatten(value, path)
        else:
            keys.add(path)
    return keys


class CommandEntryTests(unittest.TestCase):
    def test_command_group_is_log_with_legacy_aliases(self):
        source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command_group("log"', source)
        self.assertIn('"logvault"', source)
        self.assertIn('"logplus"', source)
        self.assertNotIn('@logvault.command(', source)

    def test_help_uses_the_log_prefix(self):
        handler = CommandHandler(PLUGIN_ROOT, LogCleaner(PLUGIN_ROOT, {}))
        help_text = handler.handle_help()
        for command in ("status", "search", "clean", "export", "send", "help"):
            self.assertIn(f"/log {command}", help_text)
        # The legacy names must stay documented as aliases.
        self.assertIn("/logvault", help_text)
        self.assertIn("/logplus", help_text)


class ConsoleAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
        cls.css = (PAGE_DIR / "style.css").read_text(encoding="utf-8")

    def test_every_looked_up_element_exists_in_the_markup(self):
        wanted = set(re.findall(r'\bel\("([^"]+)"\)', self.js))
        present = set(re.findall(r'\sid="([^"]+)"', self.html))
        self.assertTrue(wanted)
        self.assertEqual(set(), wanted - present)

    def test_all_declared_skins_are_styled(self):
        block = self.js.split("];", 1)[0]
        skins = re.findall(r'id: "([a-z]+)"', block)
        self.assertIn("glass", skins)
        for skin in skins:
            if skin == "auto":
                # "auto" is mapped onto a concrete skin at runtime.
                continue
            self.assertIn(f'[data-skin="{skin}"]', self.css)

    def test_glass_skin_is_translucent_and_glowing(self):
        start = self.css.index('html[data-skin="glass"]{')
        end = self.css.index("html[data-skin=", start + 10)
        block = self.css[start:end]
        # Translucency and the neon glow are driven by custom properties that
        # the shared rules consume, so assert on the tokens plus one consumer.
        self.assertIn("--blur:", block)
        self.assertIn("--glow:", block)
        self.assertIn("rgba(", block)
        self.assertIn("backdrop-filter:blur(var(--blur))", self.css)

    def test_both_density_modes_are_styled(self):
        for density in ("compact", "cozy"):
            self.assertIn(f'[data-density="{density}"]', self.css)

    def test_every_tab_has_a_panel(self):
        tabs = set(re.findall(r'data-tab="([a-z]+)"', self.html))
        panels = set(re.findall(r'id="panel-([a-z]+)"', self.html))
        self.assertEqual(tabs, panels)


class TranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zh = json.loads((I18N_DIR / "zh-CN.json").read_text(encoding="utf-8"))
        cls.en = json.loads((I18N_DIR / "en-US.json").read_text(encoding="utf-8"))

    def test_locales_expose_the_same_keys(self):
        self.assertEqual(_flatten(self.zh), _flatten(self.en))

    def test_page_keys_are_nested_under_pages_logs(self):
        # The dashboard bridge walks the document level by level, so a flat
        # "pages.logs.title" key would never resolve.
        self.assertIn("logs", self.zh["pages"])
        self.assertIn("title", self.zh["pages"]["logs"])

    def test_markup_and_script_keys_are_translated(self):
        available = {
            key.removeprefix("pages.logs.")
            for key in _flatten(self.zh)
            if key.startswith("pages.logs.")
        }
        html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
        js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
        used = set(re.findall(r'data-i18n="([^"]+)"', html))
        for key in re.findall(r'\bt\(\s*"([^"]+)"', js):
            if key.endswith(".") or key == "pages.logs.":
                # Dynamic families such as t("tab." + name) are checked below.
                continue
            used.add(key)
        self.assertTrue(used)
        self.assertEqual(set(), used - available)

    def test_dynamic_key_families_are_complete(self):
        available = {
            key.removeprefix("pages.logs.")
            for key in _flatten(self.zh)
            if key.startswith("pages.logs.")
        }
        expected = (
            [f"tab.{name}" for name in ("overview", "live", "files", "search", "diag")]
            + [f"kind.{name}" for name in ("all", "builtin", "plugin", "other")]
            + [
                f"skin.{name}"
                for name in ("auto", "console", "daylight", "glass", "synthwave", "matrix")
            ]
        )
        for key in expected:
            self.assertIn(key, available)


if __name__ == "__main__":
    unittest.main()
