import importlib.util
import unittest
from pathlib import Path

# Load this dependency directly so the focused routing test remains runnable
# while the plugin's optional core modules are being refactored.
_router_path = Path(__file__).resolve().parents[1] / "core" / "log_router.py"
_spec = importlib.util.spec_from_file_location("log_router_under_test", _router_path)
assert _spec and _spec.loader
_router_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_router_module)
LogRouter = _router_module.LogRouter


class LogRouterPathTests(unittest.TestCase):
    def test_windows_external_plugin_path(self):
        path = r"C:\AstrBot\data\plugins\astrbot_plugin_dynamic_card_plus\main.py"
        self.assertTrue(LogRouter.is_plugin_path(path))
        self.assertEqual(
            LogRouter.extract_plugin_name(path), "astrbot_plugin_dynamic_card_plus"
        )

    def test_posix_external_plugin_path(self):
        path = "/srv/astrbot/data/plugins/astrbot_plugin_demo/main.py"
        self.assertTrue(LogRouter.is_plugin_path(path))
        self.assertEqual(LogRouter.extract_plugin_name(path), "astrbot_plugin_demo")

    def test_windows_builtin_plugin_path(self):
        path = r"D:\AstrBot\astrbot\builtin_stars\webchat\handler.py"
        self.assertTrue(LogRouter.is_plugin_path(path))
        self.assertEqual(LogRouter.extract_plugin_name(path), "webchat")

    def test_top_level_plugins_path(self):
        # Some exported/install layouts omit the ``data`` directory.
        path = r"D:\backup\plugins\astrbot_plugin_demo\plugin.log"
        self.assertTrue(LogRouter.is_plugin_path(path))
        self.assertEqual(LogRouter.extract_plugin_name(path), "astrbot_plugin_demo")

    def test_core_path_is_not_plugin(self):
        path = r"C:\AstrBot\astrbot\core\logging.py"
        self.assertFalse(LogRouter.is_plugin_path(path))
        self.assertIsNone(LogRouter.extract_plugin_name(path))


if __name__ == "__main__":
    unittest.main()
