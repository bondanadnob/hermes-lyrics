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
        self.assertIn("const data = query.isError ? undefined : retainedData", source)
        self.assertIn("Microphone status unknown", source)
        self.assertIn("Music status unknown", source)
        self.assertIn("host.state.profile", source)
        self.assertRegex(source, r"queryKey\([^)]*profile")
        self.assertGreaterEqual(source.count("assertActiveProfile(profile)"), 4)
        self.assertIn("'/permissions'", source)
        self.assertNotIn("x-apple.systempreferences", source)

    def test_plugin_description_mentions_nearby_recognition(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertRegex(source, r"description: '[^']*Nearby[^']*'")

    def test_ambient_ui_has_an_explicit_source_switch(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("SegmentedControl", source)
        self.assertIn("{ id: 'music_app', label: 'Music.app' }", source)
        self.assertIn("{ id: 'ambient', label: 'Nearby' }", source)
        self.assertIn("path: '/source'", source)
        self.assertIn("body: { source: nextSource }", source)

    def test_ambient_ui_has_separate_listen_and_stop_actions(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("path: '/ambient/listen'", source)
        self.assertIn("path: '/ambient/stop'", source)
        self.assertIn("Listen for 8 seconds", source)
        self.assertIn("Microphone active", source)

    def test_ambient_ui_discloses_the_unofficial_network_provider(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("Unofficial Shazam fingerprint service", source)
        self.assertIn("request metadata and your IP address", source)
        self.assertIn("track metadata is sent to LRCLIB", source)
        self.assertIn("Audio is not saved", source)

    def test_ambient_ui_discloses_the_recognized_artwork_cdn(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("Apple's mzstatic.com CDN", source)
        self.assertIn("image request and your IP address", source)

    def test_readme_discloses_both_nearby_network_recipients(self):
        source = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Shazam infrastructure receives the fingerprint", source)
        self.assertIn("After a Nearby match, LRCLIB receives track metadata", source)

    def test_readme_documents_the_in_memory_capture_encoding(self):
        source = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("FFmpeg with AVFoundation and `libvorbis` support", source)
        self.assertIn("encoded as Ogg Vorbis in process memory", source)

    def test_readme_labels_the_private_offset_as_an_assumption(self):
        source = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(
            "treated as the reference-track time matching the start of the captured query",
            source,
        )
        self.assertIn("private endpoint is unofficial", source)

    def test_ambient_track_hides_music_app_transport_controls(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("const canControl = track.can_control !== false", source)
        self.assertIn("canControl &&", source)

    def test_ambient_timeline_is_read_only_progress(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("const canSeek = track.can_seek !== false", source)
        self.assertIn("role: canSeek ? 'slider' : 'progressbar'", source)
        self.assertIn("onClick: canSeek ? seekFromEvent : undefined", source)

    def test_tracks_without_duration_omit_the_timeline(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertGreaterEqual(source.count("track.duration > 0 &&"), 2)

    def test_ambient_lyric_lines_never_seek_music_app(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("function LyricsScroller({ canSeek,", source)
        self.assertIn("disabled: !canSeek || !lyrics.synced || working", source)
        self.assertIn("canSeek: data.track.can_seek !== false", source)

    def test_ambient_artwork_does_not_query_music_app(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("data?.track?.source !== 'ambient'", source)

    def test_ambient_artwork_uses_the_recognized_remote_image(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn(
            "ambientArtwork ? { url: cachedArtworkUrl, fallbackUrl: null }",
            source,
        )

    def test_ambient_match_without_lyrics_has_ambient_guidance(self):
        source = PLUGIN.read_text(encoding="utf-8")

        self.assertIn("Song identified, but no synchronized lyrics were found", source)
        self.assertIn("children: source === 'ambient' ? 'Listen again' : 'Refresh lyrics'", source)

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

    def test_cached_state_poll_failures_are_visibly_unknown_at_runtime(self):
        node = shutil.which("node")
        if node is None:
            self.fail("node is required for the desktop runtime check")
        hermes_repository = Path(
            os.environ.get(
                "HERMES_REPO_ROOT",
                Path.home() / ".hermes" / "hermes-agent",
            )
        )
        react_runtime = hermes_repository / "node_modules" / "react" / "index.js"
        if not react_runtime.is_file():
            self.skipTest("Hermes desktop React runtime is not installed")

        environment = os.environ.copy()
        environment.update(
            {
                "HERMES_REPO_ROOT": str(hermes_repository),
                "PLUGIN_PATH": str(PLUGIN),
                "SDK_SHIM_PATH": str(
                    ROOT / "tests" / "desktop_plugin_sdk_shim.mjs"
                ),
            }
        )
        completed = subprocess.run(
            [
                node,
                "--no-warnings",
                "--experimental-loader",
                str(ROOT / "tests" / "desktop_plugin_loader.mjs"),
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
