"""Consistency tests for the 2.3.0 console assets and the /log command entry."""

import json
import re
import struct
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.command_handler import CommandHandler
from core.log_cleaner import LogCleaner

PAGE_DIR = PLUGIN_ROOT / "pages" / "logs"
ASSET_DIR = PLUGIN_ROOT / "assets"
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

    def test_brand_mark_is_a_theme_neutral_icon(self):
        start = self.html.index('<span class="lv-mark"')
        end = self.html.index("</span>", start)
        mark = self.html[start:end]
        # An inline stroke icon inherits the accent colour of every skin; the
        # old "LV" monogram did not.
        self.assertIn("<svg", mark)
        self.assertIn('stroke="currentColor"', mark)
        self.assertNotIn("LV", mark)
        self.assertIn("color:var(--accent)", self.css.split(".lv-mark{", 1)[1][:160])

    def test_follow_file_picker_is_a_searchable_combobox(self):
        # The native <select> stays in the markup as the source of truth, so
        # loadLive/startLive keep reading a single value.
        self.assertIn('id="live-file"', self.html)
        self.assertIn('id="live-file-input"', self.html)
        self.assertIn('id="live-file-list"', self.html)
        self.assertIn('role="combobox"', self.html)
        self.assertIn('role="listbox"', self.html)
        for name in ("renderCombo", "openCombo", "closeCombo", "commitCombo", "moveCombo"):
            self.assertIn(f"function {name}(", self.js)
        self.assertIn(".lv-combo-list{", self.css)
        self.assertIn(".lv-combo-native{display:none;}", self.css)

    def test_combo_keeps_the_toolbar_layout(self):
        # The popup is absolutely positioned inside the existing field, so the
        # toolbar row keeps its original height and column count.
        self.assertIn(".lv-combo{position:relative", self.css)
        self.assertIn("position:absolute", self.css.split(".lv-combo-list{", 1)[1][:200])
        toolbar = self.html.split('id="panel-live"', 1)[1].split("</div>\n          <div id=", 1)[0]
        self.assertEqual(4, toolbar.count('class="lv-field'))


    def test_export_centre_has_its_own_panel(self):
        # 2.3.0 moved the bundle card out of the overview into a dedicated tab,
        # so the old identifiers must be gone and the new ones present.
        self.assertNotIn('id="btn-bundle"', self.html)
        self.assertNotIn('id="bundle-target"', self.html)
        self.assertNotIn('data-tab="export"', self.html.split('id="panel-', 1)[1])
        for ident in (
            "panel-export",
            "export-scope",
            "export-preset",
            "export-plugin",
            "export-format",
            "export-days",
            "export-since",
            "export-until",
            "export-levels",
            "export-keyword",
            "export-mask",
            "export-preview",
            "export-history",
            "btn-export-plan",
            "btn-export-run",
            "btn-export-reload",
            "btn-export-purge",
        ):
            self.assertIn(f'id="{ident}"', self.html)

    def test_export_shortcuts_exist_on_the_other_tabs(self):
        # Every browsing surface offers a one-click hand-off to the exporter.
        for ident in (
            "btn-export-files",
            "btn-export-category",
            "btn-export-search",
            "btn-live-export",
        ):
            self.assertIn(f'id="{ident}"', self.html)

    def test_every_export_button_is_translated(self):
        buttons = [
            tag
            for tag in re.findall(r"<button[^>]*>", self.html)
            if re.search(r'id="(btn-export[^"]+|btn-live-export)"', tag)
        ]
        self.assertGreaterEqual(len(buttons), 6)
        self.assertTrue(buttons)
        for markup in buttons:
            self.assertIn("data-i18n=", markup)

    def test_export_widgets_are_styled(self):
        for selector in (".lv-export{", ".lv-chip{", ".lv-preview{", ".lv-hrow{"):
            self.assertIn(selector, self.css)
        # The two-column layout has to collapse on narrow consoles.
        self.assertIn(".lv-export{grid-template-columns:minmax(0,1fr);}", self.css)

    def test_export_levels_are_rendered_from_the_script(self):
        self.assertIn("EXPORT_LEVELS", self.js)
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            self.assertIn(level, self.js)
        # Downloads must go through the one-shot token endpoint.
        self.assertIn('"export_file"', self.js)
        self.assertIn('"export_plan"', self.js)


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
            [
                f"tab.{name}"
                for name in ("overview", "live", "files", "search", "export", "diag")
            ]
            + [f"kind.{name}" for name in ("all", "builtin", "plugin", "other")]
            + [
                f"skin.{name}"
                for name in ("auto", "console", "daylight", "glass", "synthwave", "matrix")
            ]
        )
        for key in expected:
            self.assertIn(key, available)


class BrandAssetTests(unittest.TestCase):
    """The logo lives in assets/ and the console must not fork its own copy."""

    @classmethod
    def setUpClass(cls):
        cls.svg = (ASSET_DIR / "logo.svg").read_text(encoding="utf-8")
        cls.html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")

    def test_the_vector_mark_inherits_the_current_colour(self):
        self.assertIn('stroke="currentColor"', self.svg)
        self.assertIn("<title>LogVault</title>", self.svg)
        self.assertIn('viewBox="0 0 24 24"', self.svg)

    def test_the_console_header_reuses_the_asset_geometry(self):
        start = self.html.index('<span class="lv-mark"')
        end = self.html.index("</span>", start)
        inline = re.findall(r'\sd="([^"]+)"', self.html[start:end])
        asset = re.findall(r'\sd="([^"]+)"', self.svg)
        self.assertEqual(3, len(asset))
        self.assertEqual(asset, inline)

    def test_the_raster_and_social_derivatives_are_committed(self):
        for name in (
            "logo-badge.svg",
            "logo-512.png",
            "logo-256.png",
            "logo-128.png",
            "logo-32.png",
            "social-preview.png",
        ):
            path = ASSET_DIR / name
            with self.subTest(asset=name):
                self.assertTrue(path.is_file(), f"missing {name}")
                self.assertGreater(path.stat().st_size, 0)

    def test_astrbot_picks_up_the_dashboard_icon(self):
        """AstrBot resolves plugin icons from a root-level logo.png only."""

        logo = PLUGIN_ROOT / "logo.png"
        self.assertTrue(logo.is_file(), "AstrBot expects <plugin>/logo.png")
        head = logo.read_bytes()[:24]
        self.assertEqual(b"\x89PNG\r\n\x1a\n", head[:8])
        width, height = struct.unpack(">II", head[16:24])
        self.assertEqual((512, 512), (width, height))

    def test_the_square_variant_shares_the_mark_geometry(self):
        square = (ASSET_DIR / "logo-square.svg").read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(r'\sd="([^"]+)"', self.svg),
            re.findall(r'\sd="([^"]+)"', square),
        )
        # The dashboard applies its own corner radius, so this variant must
        # stay full-bleed; a baked-in radius would clip twice.
        self.assertNotIn("rx=", square)

    def test_the_readme_shows_the_logo(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("assets/logo-128.png", readme)

if __name__ == "__main__":
    unittest.main()
