import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "desktop" / "plugin.js"


class DesktopPluginContractTests(unittest.TestCase):
    def test_plugin_is_valid_plain_javascript_with_only_supported_imports(self):
        self.assertTrue(PLUGIN.is_file())
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the desktop plugin syntax check")
        completed = subprocess.run(
            [node, "--check", str(PLUGIN)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        source = PLUGIN.read_text(encoding="utf-8")
        imports = re.findall(r"from\s+['\"]([^'\"]+)['\"]", source)
        self.assertTrue(imports)
        self.assertTrue(
            set(imports) <= {"@hermes/plugin-sdk", "react", "react/jsx-runtime"},
            imports,
        )
        self.assertNotRegex(source, r"</?[A-Za-z][^>]*>")

    def test_plugin_registers_pane_page_sidebar_status_and_palette(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("id: ID", source)
        self.assertIn("const ID = 'apple-music-lyrics'", source)
        for contribution in (
            "PANES_AREA",
            "ROUTES_AREA",
            "SIDEBAR_NAV_AREA",
            "STATUSBAR_AREAS.right",
            "PALETTE_AREA",
        ):
            self.assertIn(contribution, source)
        self.assertIn("query.isError && !data", source)
        self.assertIn("host.state.profile", source)
        self.assertRegex(source, r"queryKey\([^)]*profile")
        self.assertGreaterEqual(source.count("assertActiveProfile(profile)"), 4)
        self.assertIn("'/permissions'", source)
        self.assertNotIn("x-apple.systempreferences", source)

    def test_styles_use_theme_variables_instead_of_hardcoded_colors(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertNotRegex(source, r"#[0-9A-Fa-f]{3,8}\b")
        self.assertNotRegex(source, r"\brgb(?:a)?\(")
        self.assertIn("var(--ui-accent)", source)


if __name__ == "__main__":
    unittest.main()
