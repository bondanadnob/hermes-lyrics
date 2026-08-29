import os
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

    def test_plugin_registers_every_supported_extension_area(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("const ID = 'lyrics-for-hermes'", source)
        self.assertIn("const ROUTE = '/lyrics-for-hermes'", source)
        self.assertIn("name: 'Lyrics for Hermes'", source)
        for contribution in (
            "PANES_AREA",
            "ROUTES_AREA",
            "SIDEBAR_NAV_AREA",
            "STATUSBAR_AREAS.right",
            "PALETTE_AREA",
        ):
            self.assertIn(contribution, source)
        self.assertIn("ctx.registerMany([", source)
        self.assertIn("id: 'lyrics-for-hermes.open'", source)

    def test_plugin_retains_react_query_and_active_profile_safety(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("useQuery", source)
        self.assertIn("useMutation", source)
        self.assertIn("useQueryClient", source)
        self.assertIn("const data = query.isError ? undefined : retainedData", source)
        self.assertIn("Music status unknown", source)
        self.assertIn("host.state.profile", source)
        self.assertRegex(source, r"queryKey\([^)]*profile")
        self.assertGreaterEqual(source.count("assertActiveProfile(profile)"), 4)
        self.assertIn("'/permissions'", source)
        self.assertNotIn("x-apple.systempreferences", source)

    def test_removed_provider_has_no_desktop_routes_or_visible_ui(self):
        source = PLUGIN.read_text(encoding="utf-8")

        for forbidden in (
            "SegmentedControl",
            "Nearby",
            "Shazam",
            "Microphone",
            "'/source'",
            "'/ambient/",
            "source === 'ambient'",
        ):
            self.assertNotIn(forbidden, source)

    def test_play_pause_button_uses_explicit_transport_semantics(self):
        source = PLUGIN.read_text(encoding="utf-8")
        music_source = (
            ROOT / "dashboard" / "apple_music_lyrics_backend" / "music.py"
        ).read_text(encoding="utf-8")

        self.assertIn("label: track.state === 'playing' ? 'Pause' : 'Play'", source)
        self.assertIn("runAction(track.state === 'playing' ? 'pause' : 'play')", source)
        self.assertNotIn("runAction('play_pause')", source)
        self.assertIn('"play":', music_source)
        self.assertIn('"pause":', music_source)

    def test_controls_and_seek_respect_backend_capabilities(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("const canControl = track.can_control !== false", source)
        self.assertIn("canControl &&", source)
        self.assertIn("const canSeek = track.can_seek !== false", source)
        self.assertIn("role: canSeek ? 'slider' : 'progressbar'", source)
        self.assertIn("onClick: canSeek ? seekFromEvent : undefined", source)
        self.assertIn("function LyricsScroller({ canSeek,", source)
        self.assertIn("disabled: !canSeek || !lyrics.synced || working", source)
        self.assertGreaterEqual(source.count("track.duration > 0 &&"), 2)

    def test_highlighting_auto_scroll_and_reduced_motion_are_retained(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("function findCurrentIndex", source)
        self.assertIn("function ActiveLyric", source)
        self.assertIn("scrollIntoView", source)
        self.assertIn("prefers-reduced-motion: reduce", source)
        self.assertIn("behavior: reduceMotion ? 'auto' : 'smooth'", source)
        self.assertIn("Resume following", source)

    def test_styles_use_theme_variables_instead_of_hardcoded_colors(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertNotRegex(source, r"#[0-9A-Fa-f]{3,8}\b")
        self.assertNotRegex(source, r"\brgb(?:a)?\(")
        self.assertIn("var(--ui-accent)", source)
        self.assertIn("focus-visible:ring-ring/40", source)
        self.assertNotRegex(source, r"focus-visible:ring-ring(?:\\s|['\"])")

    def test_music_artwork_is_loaded_independently_of_lyrics(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("function useMusicArtwork", source)
        self.assertIn("/artwork?identity=", source)
        self.assertRegex(
            source,
            r"const localArtworkUrl\s*=\s*useMemo\(\s*\(\) => safeArtworkUrl\(artworkQuery\.data\?\.data_url\)",
        )
        self.assertIn("data?.artwork?.remote_url", source)
        self.assertIn("selectArtworkSources", source)
        self.assertNotIn("url: lyrics?.artwork_url", source)

    def test_artwork_validation_fallback_and_cache_bounds_are_retained(self):
        source = PLUGIN.read_text(encoding="utf-8")

        for expected in (
            "hostname.endsWith('.mzstatic.com')",
            "!parsed.username",
            "!parsed.password",
            "parsed.port === '' || parsed.port === '443'",
            "data:image/(?:jpeg|png);base64",
            "if (value.length > 2048) return null",
            "function Artwork({ url, fallbackUrl, compact })",
            "fallbackUrl: artworkFallbackUrl",
            "candidates.find",
            "const artworkProfileQueryKey",
            "staleTime: 300000",
            "gcTime: 10000",
            "retry: artworkRetry",
            "refetchOnMount: false",
            "queryKey: artworkProfileQueryKey(ctx, profile)",
        ):
            self.assertIn(expected, source)
        self.assertGreaterEqual(source.count("() => safeArtworkUrl"), 4)

    def test_runtime_and_readme_have_no_stale_visible_provider_branding(self):
        visible = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PLUGIN, ROOT / "dashboard" / "dist" / "index.js", ROOT / "README.md")
        )
        for forbidden in ("Shazam", "Nearby", "private cache", "Apple Music Lyrics"):
            self.assertNotIn(forbidden, visible)

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

    def test_cached_state_poll_failures_are_visibly_unknown_at_runtime(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the desktop runtime check")
        hermes_repository = Path(os.environ.get("HERMES_REPO_ROOT", ROOT))
        react_runtime = hermes_repository / "node_modules" / "react" / "index.js"
        if not react_runtime.is_file():
            self.skipTest("desktop React runtime is not installed")

        environment = os.environ.copy()
        environment.update(
            {
                "HERMES_REPO_ROOT": str(hermes_repository),
                "PLUGIN_PATH": str(PLUGIN),
                "SDK_SHIM_PATH": str(ROOT / "tests" / "desktop_plugin_sdk_shim.mjs"),
            }
        )
        completed = subprocess.run(
            [
                node,
                "--import",
                str(ROOT / "tests" / "register_desktop_plugin_loader.mjs"),
                str(ROOT / "tests" / "desktop_state_failure_runtime.mjs"),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
