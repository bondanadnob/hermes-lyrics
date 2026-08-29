import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lyrics_for_hermes_installer", ROOT / "scripts" / "install.py")
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def write_marker(path: Path, value: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "marker.txt").write_text(value, encoding="utf-8")


class InstallerTests(unittest.TestCase):
    def test_readme_documents_transactional_unified_install_and_legacy_key(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("plugins/lyrics-for-hermes", readme)
        self.assertIn("canonical plugin ID is `lyrics-for-hermes`", readme)
        self.assertIn("legacy `apple-music-lyrics` key", readme)
        for location in ("`plugins`", "`desktop-plugins`", "`plugin-data`"):
            self.assertIn(location, readme)
        self.assertIn("same filesystem transaction", readme)
        self.assertIn("failed swap restores", readme)

    def test_installer_has_no_removed_runtime_or_dependency_lock(self):
        source = (ROOT / "scripts" / "install.py").read_text(encoding="utf-8").casefold()

        for forbidden in ("recognition", "shazam", "ffmpeg", "requirements-recognition"):
            self.assertNotIn(forbidden, source)
        self.assertFalse((ROOT / "requirements-recognition.in").exists())
        self.assertFalse((ROOT / "requirements-recognition.lock").exists())

    def test_empty_hermes_home_defaults_to_the_user_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with (
                patch.dict(os.environ, {"HERMES_HOME": ""}),
                patch.object(installer.shutil, "which", return_value=None),
                patch.object(installer.Path, "home", return_value=home),
            ):
                resolved = installer.resolve_hermes_home()
        self.assertEqual(resolved, (home / ".hermes").resolve())

    def test_unset_hermes_home_resolves_the_active_cli_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profiles" / "work"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(installer.shutil, "which", return_value="/usr/local/bin/hermes"),
                patch.object(installer, "_profile_home_from_cli", return_value=profile.resolve()),
            ):
                resolved = installer.resolve_hermes_home()
        self.assertEqual(resolved, profile.resolve())

    def test_explicit_hermes_home_is_canonicalized(self):
        with tempfile.TemporaryDirectory() as temp:
            configured = Path(temp) / "profile" / ".." / "profile"
            with patch.dict(os.environ, {"HERMES_HOME": str(configured)}):
                resolved = installer.resolve_hermes_home()
        self.assertEqual(resolved, configured.resolve())

    def test_rejects_target_inside_source_before_copying(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            target = source / "profile" / "plugins" / installer.PLUGIN_ID
            with self.assertRaisesRegex(RuntimeError, "overlap"):
                installer.install_target(source, target, link=False, force=True)
            self.assertFalse(target.exists())

    def test_rejects_source_inside_target_before_force_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target"
            source = target / "checkout"
            source.mkdir(parents=True)
            marker = source / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "overlap"):
                installer.install_target(source, target, link=False, force=True)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_failed_forced_copy_preserves_existing_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            write_marker(source, "new")
            write_marker(target, "old")
            with patch.object(installer.shutil, "copytree", side_effect=OSError("copy failed")):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    installer.install_target(source, target, link=False, force=True)
            self.assertEqual((target / "marker.txt").read_text(encoding="utf-8"), "old")

    def test_copy_install_excludes_development_artifacts_but_keeps_dashboard_dist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            write_marker(source, "payload")
            for name in (".git", ".venv", ".ruff_cache", "build", "node_modules", "sample.egg-info"):
                write_marker(source / name, name)
            write_marker(source / "dashboard" / "dist", "desktop bundle")

            installer.install_target(source, target, link=False, force=False)

            for name in (".git", ".venv", ".ruff_cache", "build", "node_modules", "sample.egg-info"):
                self.assertFalse((target / name).exists(), name)
            self.assertEqual((target / "dashboard" / "dist" / "marker.txt").read_text(), "desktop bundle")

    def test_pair_staging_failure_preserves_both_existing_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_source, second_source = root / "source-a", root / "source-b"
            first_target, second_target = root / "target-a", root / "target-b"
            for path, value in (
                (first_source, "new-a"),
                (second_source, "new-b"),
                (first_target, "old-a"),
                (second_target, "old-b"),
            ):
                write_marker(path, value)
            real_copytree = installer.shutil.copytree

            def fail_second(source, *args, **kwargs):
                if Path(source) == second_source:
                    raise OSError("second staging failed")
                return real_copytree(source, *args, **kwargs)

            with patch.object(installer.shutil, "copytree", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "second staging failed"):
                    installer.install_bundle(
                        [(first_source, first_target), (second_source, second_target)],
                        link=False,
                        force=True,
                    )
            self.assertEqual((first_target / "marker.txt").read_text(), "old-a")
            self.assertEqual((second_target / "marker.txt").read_text(), "old-b")

    def test_pair_commit_failure_rolls_back_both_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_source, second_source = root / "source-a", root / "source-b"
            first_target, second_target = root / "target-a", root / "target-b"
            for path, value in (
                (first_source, "new-a"),
                (second_source, "new-b"),
                (first_target, "old-a"),
                (second_target, "old-b"),
            ):
                write_marker(path, value)
            real_rename = installer.Path.rename

            def fail_second_commit(path, destination):
                if path.name == "payload" and Path(destination) == second_target:
                    raise OSError("second commit failed")
                return real_rename(path, destination)

            with patch.object(installer.Path, "rename", fail_second_commit):
                with self.assertRaisesRegex(OSError, "second commit failed"):
                    installer.install_bundle(
                        [(first_source, first_target), (second_source, second_target)],
                        link=False,
                        force=True,
                    )
            self.assertEqual((first_target / "marker.txt").read_text(), "old-a")
            self.assertEqual((second_target / "marker.txt").read_text(), "old-b")

    def test_legacy_target_overlap_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            target = Path(temp) / "target"
            write_marker(source, "source")
            with self.assertRaisesRegex(RuntimeError, "legacy target overlaps"):
                installer.install_bundle(
                    [(source, target)],
                    link=False,
                    force=False,
                    obsolete_targets=[target / "nested"],
                )
            self.assertFalse(target.exists())

    def test_success_quarantines_all_three_legacy_profile_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile = root / "profile"
            target = profile / "plugins" / installer.PLUGIN_ID
            legacy = [
                profile / "plugins" / installer.LEGACY_PLUGIN_ID,
                profile / "desktop-plugins" / installer.LEGACY_PLUGIN_ID,
                profile / "plugin-data" / installer.LEGACY_PLUGIN_ID,
            ]
            write_marker(source, "new")
            for index, path in enumerate(legacy):
                write_marker(path, f"legacy-{index}")

            installer.install_bundle(
                [(source, target)],
                link=False,
                force=False,
                obsolete_targets=legacy,
            )

            self.assertEqual((target / "marker.txt").read_text(), "new")
            self.assertTrue(all(not path.exists() for path in legacy))

    def test_legacy_symlink_to_checkout_is_quarantined_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile = root / "profile"
            target = profile / "plugins" / installer.PLUGIN_ID
            legacy = profile / "desktop-plugins" / installer.LEGACY_PLUGIN_ID
            write_marker(source, "source")
            legacy.parent.mkdir(parents=True)
            legacy.symlink_to(source, target_is_directory=True)

            installer.install_bundle(
                [(source, target)],
                link=False,
                force=False,
                obsolete_targets=[legacy],
            )

            self.assertFalse(legacy.exists())
            self.assertEqual((source / "marker.txt").read_text(), "source")
            self.assertEqual((target / "marker.txt").read_text(), "source")

    def test_link_mode_promotes_checkout_symlink_and_finishes_legacy_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile = root / "profile"
            target = profile / "plugins" / installer.PLUGIN_ID
            legacy = profile / "plugins" / installer.LEGACY_PLUGIN_ID
            write_marker(source, "source")
            write_marker(legacy, "legacy")

            installer.install_bundle(
                [(source, target)],
                link=True,
                force=False,
                obsolete_targets=[legacy],
            )

            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), source.resolve())
            self.assertFalse(legacy.exists())
            self.assertEqual((source / "marker.txt").read_text(), "source")

    def test_installing_one_profile_never_touches_another_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile_a = root / "profiles" / "a"
            profile_b = root / "profiles" / "b"
            write_marker(source, "new")
            for profile, value in ((profile_a, "a"), (profile_b, "b")):
                write_marker(profile / "plugins" / installer.LEGACY_PLUGIN_ID, value)
                write_marker(profile / "desktop-plugins" / installer.LEGACY_PLUGIN_ID, value)
                write_marker(profile / "plugin-data" / installer.LEGACY_PLUGIN_ID, value)

            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", ["install.py", "--no-enable"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile_a),
                patch("builtins.print"),
            ):
                result = installer.main()

            self.assertEqual(result, 0)
            self.assertTrue((profile_a / "plugins" / installer.PLUGIN_ID).is_dir())
            for parent in ("plugins", "desktop-plugins", "plugin-data"):
                self.assertFalse((profile_a / parent / installer.LEGACY_PLUGIN_ID).exists())
                self.assertEqual(
                    (profile_b / parent / installer.LEGACY_PLUGIN_ID / "marker.txt").read_text(),
                    "b",
                )
            self.assertFalse((profile_b / "plugins" / installer.PLUGIN_ID).exists())

    def test_main_rejects_symlinked_profile_ancestor_before_cross_profile_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile_a = root / "profiles" / "a"
            profile_b = root / "profiles" / "b"
            write_marker(source, "new")
            profile_a.mkdir(parents=True)
            (profile_b / "plugins").mkdir(parents=True)
            profile_a.joinpath("plugins").symlink_to(profile_b / "plugins", target_is_directory=True)
            write_marker(profile_b / "plugins" / installer.LEGACY_PLUGIN_ID, "profile-b")

            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", ["install.py", "--no-enable"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile_a),
                patch("builtins.print"),
            ):
                result = installer.main()

            self.assertEqual(result, 2)
            self.assertFalse((profile_b / "plugins" / installer.PLUGIN_ID).exists())
            self.assertEqual(
                (profile_b / "plugins" / installer.LEGACY_PLUGIN_ID / "marker.txt").read_text(),
                "profile-b",
            )

    def test_commit_failure_restores_canonical_and_every_legacy_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile = root / "profile"
            target = profile / "plugins" / installer.PLUGIN_ID
            legacy = [
                profile / "plugins" / installer.LEGACY_PLUGIN_ID,
                profile / "desktop-plugins" / installer.LEGACY_PLUGIN_ID,
                profile / "plugin-data" / installer.LEGACY_PLUGIN_ID,
            ]
            write_marker(source, "new")
            write_marker(target, "canonical-old")
            for index, path in enumerate(legacy):
                write_marker(path, f"legacy-{index}")
            observed = {"quarantined": False}
            real_rename = installer.Path.rename

            def fail_commit(path, destination):
                if path.name == "payload" and Path(destination) == target:
                    observed["quarantined"] = all(not legacy_path.exists() for legacy_path in legacy)
                    raise OSError("commit failed")
                return real_rename(path, destination)

            with patch.object(installer.Path, "rename", fail_commit):
                with self.assertRaisesRegex(OSError, "commit failed"):
                    installer.install_bundle(
                        [(source, target)],
                        link=False,
                        force=True,
                        obsolete_targets=legacy,
                    )

            self.assertTrue(observed["quarantined"])
            self.assertEqual((target / "marker.txt").read_text(), "canonical-old")
            for index, path in enumerate(legacy):
                self.assertEqual((path / "marker.txt").read_text(), f"legacy-{index}")

    def test_quarantine_failure_restores_already_moved_legacy_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "profile" / "plugins" / installer.PLUGIN_ID
            legacy = [root / "legacy-a", root / "legacy-b"]
            write_marker(source, "new")
            write_marker(legacy[0], "old-a")
            write_marker(legacy[1], "old-b")
            real_rename = installer.Path.rename

            def fail_second_quarantine(path, destination):
                if path == legacy[1]:
                    raise OSError("quarantine failed")
                return real_rename(path, destination)

            with patch.object(installer.Path, "rename", fail_second_quarantine):
                with self.assertRaisesRegex(OSError, "quarantine failed"):
                    installer.install_bundle(
                        [(source, target)],
                        link=False,
                        force=False,
                        obsolete_targets=legacy,
                    )
            self.assertEqual((legacy[0] / "marker.txt").read_text(), "old-a")
            self.assertEqual((legacy[1] / "marker.txt").read_text(), "old-b")
            self.assertFalse(target.exists())

    def test_main_installs_canonical_payload_and_all_legacy_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile = root / "profile"
            source.mkdir()
            captured = {}

            def capture(targets, **options):
                captured["targets"] = targets
                captured["options"] = options

            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", ["install.py", "--no-enable"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile),
                patch.object(installer, "install_bundle", side_effect=capture),
                patch("builtins.print"),
            ):
                result = installer.main()

            self.assertEqual(result, 0)
            self.assertEqual(
                captured["targets"],
                [(source, profile / "plugins" / "lyrics-for-hermes")],
            )
            self.assertEqual(
                captured["options"]["obsolete_targets"],
                [
                    profile / "plugins" / "apple-music-lyrics",
                    profile / "desktop-plugins" / "apple-music-lyrics",
                    profile / "plugin-data" / "apple-music-lyrics",
                ],
            )

    def test_activation_updates_enabled_ids_without_plugin_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile"
            calls = []
            responses = iter(
                [
                    (0, '["other","apple-music-lyrics"]'),
                    (0, ""),
                    (0, '["other","lyrics-for-hermes"]'),
                ]
            )

            class Completed:
                def __init__(self, returncode, stdout):
                    self.returncode = returncode
                    self.stdout = stdout

            def run(command, **options):
                calls.append((command, options))
                returncode, stdout = next(responses)
                return Completed(returncode, stdout)

            with (
                patch.object(installer, "_profile_home_from_cli", return_value=profile.resolve()),
                patch.object(installer.subprocess, "run", side_effect=run),
            ):
                result = installer._activate_plugin("/usr/local/bin/hermes", profile)

            self.assertEqual(result, 0)
            self.assertEqual(
                [call[0][1:] for call in calls],
                [
                    ["config", "get", "--json", "plugins.enabled"],
                    ["config", "set", "plugins.enabled", '["other","lyrics-for-hermes"]'],
                    ["config", "get", "--json", "plugins.enabled"],
                ],
            )
            self.assertTrue(all(call[1]["env"]["HERMES_HOME"] == str(profile) for call in calls))

    def test_activation_rejects_a_different_active_profile_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile"
            other = Path(temp) / "other"
            with (
                patch.object(installer, "_profile_home_from_cli", return_value=other.resolve()),
                patch.object(installer.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "active profile"):
                    installer._activate_plugin("hermes", profile)
            run.assert_not_called()

    def test_activation_failure_restores_existing_canonical_and_all_legacy_paths_for_copy_and_link(self):
        for link in (False, True):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source"
                profile = root / "profile"
                target = profile / "plugins" / installer.PLUGIN_ID
                legacy = [
                    profile / "plugins" / installer.LEGACY_PLUGIN_ID,
                    profile / "desktop-plugins" / installer.LEGACY_PLUGIN_ID,
                    profile / "plugin-data" / installer.LEGACY_PLUGIN_ID,
                ]
                write_marker(source, "new")
                write_marker(target, "canonical-old")
                for index, path in enumerate(legacy):
                    write_marker(path, f"legacy-{index}")

                with self.assertRaisesRegex(RuntimeError, "enable failed"):
                    installer.install_bundle(
                        [(source, target)],
                        link=link,
                        force=True,
                        obsolete_targets=legacy,
                        activate=lambda: (_ for _ in ()).throw(RuntimeError("enable failed")),
                    )

                self.assertEqual((target / "marker.txt").read_text(), "canonical-old")
                for index, path in enumerate(legacy):
                    self.assertEqual((path / "marker.txt").read_text(), f"legacy-{index}")

    def test_activation_set_failure_restores_prior_enabled_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile"
            calls = []
            responses = iter(
                [
                    (0, '["apple-music-lyrics","lyrics-for-hermes"]'),
                    (7, ""),
                    (0, ""),
                ]
            )

            class Completed:
                def __init__(self, returncode, stdout):
                    self.returncode = returncode
                    self.stdout = stdout

            def run(command, **options):
                calls.append((command, options))
                returncode, stdout = next(responses)
                return Completed(returncode, stdout)

            with (
                patch.object(installer, "_profile_home_from_cli", return_value=profile.resolve()),
                patch.object(installer.subprocess, "run", side_effect=run),
            ):
                with self.assertRaisesRegex(RuntimeError, "enabled-plugin update failed"):
                    installer._activate_plugin("/usr/local/bin/hermes", profile)

            self.assertEqual(
                [call[0][1:] for call in calls],
                [
                    ["config", "get", "--json", "plugins.enabled"],
                    ["config", "set", "plugins.enabled", '["lyrics-for-hermes"]'],
                    [
                        "config",
                        "set",
                        "plugins.enabled",
                        '["apple-music-lyrics","lyrics-for-hermes"]',
                    ],
                ],
            )

    def test_activation_verification_failure_restores_prior_enabled_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile"
            calls = []
            responses = iter(
                [
                    (0, '["apple-music-lyrics"]'),
                    (0, ""),
                    (0, '["apple-music-lyrics"]'),
                    (0, ""),
                ]
            )

            class Completed:
                def __init__(self, returncode, stdout):
                    self.returncode = returncode
                    self.stdout = stdout

            def run(command, **options):
                calls.append((command, options))
                returncode, stdout = next(responses)
                return Completed(returncode, stdout)

            with (
                patch.object(installer, "_profile_home_from_cli", return_value=profile.resolve()),
                patch.object(installer.subprocess, "run", side_effect=run),
            ):
                with self.assertRaisesRegex(RuntimeError, "verification failed"):
                    installer._activate_plugin("/usr/local/bin/hermes", profile)

            self.assertEqual(
                [call[0][1:] for call in calls],
                [
                    ["config", "get", "--json", "plugins.enabled"],
                    ["config", "set", "plugins.enabled", '["lyrics-for-hermes"]'],
                    ["config", "get", "--json", "plugins.enabled"],
                    ["config", "set", "plugins.enabled", '["apple-music-lyrics"]'],
                ],
            )

    def test_main_activates_only_after_filesystem_success(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile"
            events = []

            def install(*_args, **options):
                events.append("filesystem")
                options["activate"]()

            def activate(*_args, **_kwargs):
                events.append("activate")
                return 0

            with (
                patch.object(installer.sys, "argv", ["install.py"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile),
                patch.object(installer, "install_bundle", side_effect=install),
                patch.object(installer.shutil, "which", return_value="hermes"),
                patch.object(installer, "_profile_home_from_cli", return_value=profile.resolve()),
                patch.object(installer, "_activate_plugin", side_effect=activate),
                patch("builtins.print"),
            ):
                result = installer.main()
            self.assertEqual(result, 0)
            self.assertEqual(events, ["filesystem", "activate"])

    def test_main_does_not_activate_after_filesystem_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "profile"
            with (
                patch.object(installer.sys, "argv", ["install.py"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile),
                patch.object(installer, "install_bundle", side_effect=OSError("failed")),
                patch.object(installer, "_activate_plugin") as activate,
                patch("builtins.print"),
            ):
                result = installer.main()
            self.assertEqual(result, 2)
            activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
