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

    def test_seek_focus_ring_uses_a_shipped_host_utility(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("focus-visible:ring-ring/40", source)
        self.assertNotRegex(source, r"focus-visible:ring-ring(?:\\s|['\"])")

    def test_music_artwork_is_loaded_independently_of_the_lyrics_provider(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("function useMusicArtwork", source)
        self.assertIn("/artwork?identity=", source)
        self.assertRegex(
            source,
            r"const localArtworkUrl\s*=\s*useMemo\(\s*\(\) => safeArtworkUrl\(artworkQuery\.data\?\.data_url\)",
        )
        self.assertIn("data?.artwork?.remote_url", source)
        self.assertIn("selectArtworkSources", source)
        self.assertIn("artworkUrl", source)
        self.assertNotIn("url: lyrics?.artwork_url", source)
        self.assertIn("hostname.endsWith('.mzstatic.com')", source)
        self.assertIn("!parsed.username", source)
        self.assertIn("!parsed.password", source)
        self.assertIn("parsed.port === '' || parsed.port === '443'", source)
        self.assertIn("data:image/(?:jpeg|png);base64", source)

    def test_artwork_cache_is_bounded_and_refresh_is_profile_scoped(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("const artworkProfileQueryKey", source)
        self.assertIn("staleTime: 300000", source)
        self.assertIn("gcTime: 10000", source)
        self.assertIn("retry: artworkRetry", source)
        self.assertIn("refetchOnMount: false", source)
        self.assertIn(
            "queryKey: artworkProfileQueryKey(ctx, profile)",
            source,
        )

    def test_artwork_validation_and_decode_failure_preserve_apple_fallback(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("if (value.length > 2048) return null", source)
        self.assertIn("function Artwork({ url, fallbackUrl, compact })", source)
        self.assertIn("fallbackUrl: artworkFallbackUrl", source)
        self.assertIn("candidates.find", source)
        self.assertGreaterEqual(source.count("() => safeArtworkUrl"), 4)

    def test_artwork_query_and_fallback_runtime_contract(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        completed = subprocess.run(
            [node, str(ROOT / "tests" / "desktop_artwork_runtime.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
