import importlib.util
import os
import shutil
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
            with patch.object(installer, "_copy_source_tree", side_effect=OSError("copy failed")):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    installer.install_target(source, target, link=False, force=True)
            self.assertEqual((target / "marker.txt").read_text(encoding="utf-8"), "old")

    def test_copy_install_excludes_development_artifacts_but_keeps_dashboard_dist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            write_marker(source, "payload")
            excluded = (
                ".git",
                ".venv",
                ".audit-venv",
                ".hermes",
                ".mypy_cache",
                ".nox",
                ".pytest_cache",
                ".ruff_cache",
                ".tox",
                "build",
                "node_modules",
                "sample.egg-info",
                "venv-ci",
            )
            for name in excluded:
                write_marker(source / name, name)
            write_marker(source / "custom-environment", "virtualenv")
            (source / "custom-environment" / "pyvenv.cfg").write_text(
                "home = /usr/bin", encoding="utf-8"
            )
            write_marker(source / "dashboard" / "dist", "desktop bundle")

            installer.install_target(source, target, link=False, force=False)

            for name in (*excluded, "custom-environment"):
                self.assertFalse((target / name).exists(), name)
            self.assertEqual((target / "dashboard" / "dist" / "marker.txt").read_text(), "desktop bundle")

    def test_copy_install_ignores_symlinks_inside_node_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target, external = root / "source", root / "target", root / "external"
            write_marker(source, "payload")
            write_marker(external, "outside")
            (source / "node_modules").mkdir()
            (source / "node_modules" / "external").symlink_to(external, target_is_directory=True)

            installer.install_target(source, target, link=False, force=False)

            self.assertEqual((target / "marker.txt").read_text(), "payload")
            self.assertFalse((target / "node_modules").exists())

    def test_copy_install_excludes_dot_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "source", Path(temp) / "target"
            write_marker(source, "payload")
            write_marker(source / ".cache", "must-not-ship")

            installer.install_target(source, target, link=False, force=False)

            self.assertFalse((target / ".cache").exists())

    def test_copy_install_refuses_file_swapped_to_external_symlink_during_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target, external = root / "source", root / "target", root / "external.txt"
            write_marker(source, "new")
            (source / "payload.txt").write_text("reviewed", encoding="utf-8")
            write_marker(target, "old")
            external.write_text("external bytes", encoding="utf-8")
            real_open = installer.os.open
            swapped = False

            def swap_before_open(name, flags, *args, **kwargs):
                nonlocal swapped
                if name == "payload.txt" and not swapped:
                    swapped = True
                    (source / "payload.txt").unlink()
                    (source / "payload.txt").symlink_to(external)
                return real_open(name, flags, *args, **kwargs)

            with patch.object(installer.os, "open", side_effect=swap_before_open):
                with self.assertRaisesRegex(RuntimeError, "unsafe source entry"):
                    installer.install_target(source, target, link=False, force=True)

            self.assertEqual((target / "marker.txt").read_text(), "old")
            self.assertFalse((target / "payload.txt").exists())

    def test_main_never_mutates_profile_b_when_plugins_is_swapped_after_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, profile_a, profile_b = root / "source", root / "profiles" / "a", root / "profiles" / "b"
            write_marker(source, "new")
            write_marker(profile_a / "plugins" / installer.LEGACY_PLUGIN_ID, "a")
            write_marker(profile_b / "plugins" / installer.LEGACY_PLUGIN_ID, "b")
            real_validate = installer._validate_profile_target_ancestors

            def validate_then_swap(*args, **kwargs):
                real_validate(*args, **kwargs)
                shutil.rmtree(profile_a / "plugins")
                (profile_a / "plugins").symlink_to(profile_b / "plugins", target_is_directory=True)

            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", ["install.py", "--no-enable", "--force"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile_a),
                patch.object(installer, "_validate_profile_target_ancestors", side_effect=validate_then_swap),
                patch("builtins.print"),
            ):
                result = installer.main()

            self.assertEqual(result, 2)
            self.assertEqual((profile_b / "plugins" / installer.LEGACY_PLUGIN_ID / "marker.txt").read_text(), "b")
            self.assertFalse((profile_b / "plugins" / installer.PLUGIN_ID).exists())

    def test_profile_directory_swap_after_final_validation_cannot_mutate_profile_b(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile_a = root / "profiles" / "a"
            profile_b = root / "profiles" / "b"
            write_marker(source, "new")
            write_marker(profile_a / "plugins" / installer.LEGACY_PLUGIN_ID, "a")
            write_marker(profile_b / "plugins" / installer.LEGACY_PLUGIN_ID, "b")
            real_validate = installer._validate_profile_target_ancestors
            validations = 0

            def swap_after_final_validation(*args, **kwargs):
                nonlocal validations
                real_validate(*args, **kwargs)
                validations += 1
                if validations == 2:
                    shutil.rmtree(profile_a / "plugins")
                    (profile_a / "plugins").symlink_to(
                        profile_b / "plugins", target_is_directory=True
                    )

            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", ["install.py", "--no-enable", "--force"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile_a),
                patch.object(
                    installer,
                    "_validate_profile_target_ancestors",
                    side_effect=swap_after_final_validation,
                ),
                patch("builtins.print"),
            ):
                result = installer.main()

            self.assertEqual(result, 2)
            self.assertEqual(
                (profile_b / "plugins" / installer.LEGACY_PLUGIN_ID / "marker.txt").read_text(),
                "b",
            )
            self.assertFalse((profile_b / "plugins" / installer.PLUGIN_ID).exists())

    def test_anchored_profile_copy_migrates_canonical_and_all_legacy_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile = root / "profile"
            write_marker(source, "new")
            write_marker(profile / "plugins" / installer.PLUGIN_ID, "old")
            legacy = [
                profile / "plugins" / installer.LEGACY_PLUGIN_ID,
                profile / "desktop-plugins" / installer.LEGACY_PLUGIN_ID,
                profile / "plugin-data" / installer.LEGACY_PLUGIN_ID,
            ]
            for index, path in enumerate(legacy):
                write_marker(path, f"legacy-{index}")

            installer.install_profile_bundle(
                source,
                profile,
                link=False,
                force=True,
            )

            self.assertEqual(
                (profile / "plugins" / installer.PLUGIN_ID / "marker.txt").read_text(),
                "new",
            )
            self.assertTrue(all(not path.exists() for path in legacy))

    def test_anchored_profile_activation_failure_restores_all_paths(self):
        for link in (False, True):
            with self.subTest(link=link), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source"
                profile = root / "profile"
                write_marker(source, "new")
                canonical = profile / "plugins" / installer.PLUGIN_ID
                write_marker(canonical, "old")
                legacy = [
                    profile / "plugins" / installer.LEGACY_PLUGIN_ID,
                    profile / "desktop-plugins" / installer.LEGACY_PLUGIN_ID,
                    profile / "plugin-data" / installer.LEGACY_PLUGIN_ID,
                ]
                for index, path in enumerate(legacy):
                    write_marker(path, f"legacy-{index}")

                with self.assertRaisesRegex(RuntimeError, "enable failed"):
                    installer.install_profile_bundle(
                        source,
                        profile,
                        link=link,
                        force=True,
                        activate=lambda: (_ for _ in ()).throw(RuntimeError("enable failed")),
                    )

                self.assertEqual((canonical / "marker.txt").read_text(), "old")
                for index, path in enumerate(legacy):
                    self.assertEqual((path / "marker.txt").read_text(), f"legacy-{index}")

    def test_profile_swap_after_descriptors_open_never_mutates_profile_b(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile_a = root / "profiles" / "a"
            profile_b = root / "profiles" / "b"
            write_marker(source, "new")
            write_marker(profile_a / "plugins" / installer.LEGACY_PLUGIN_ID, "a")
            write_marker(profile_b / "plugins" / installer.LEGACY_PLUGIN_ID, "b")
            real_assert = installer._assert_profile_anchor
            assertions = 0

            def swap_after_open(home_fd, name, directory_fd):
                nonlocal assertions
                assertions += 1
                if assertions == 4:
                    shutil.rmtree(profile_a / "plugins")
                    (profile_a / "plugins").symlink_to(
                        profile_b / "plugins", target_is_directory=True
                    )
                return real_assert(home_fd, name, directory_fd)

            with patch.object(
                installer,
                "_assert_profile_anchor",
                side_effect=swap_after_open,
            ):
                with self.assertRaisesRegex(RuntimeError, "profile directory changed"):
                    installer.install_profile_bundle(
                        source,
                        profile_a,
                        link=False,
                        force=True,
                    )

            self.assertEqual(
                (profile_b / "plugins" / installer.LEGACY_PLUGIN_ID / "marker.txt").read_text(),
                "b",
            )
            self.assertFalse((profile_b / "plugins" / installer.PLUGIN_ID).exists())

    def test_profile_swap_during_activation_rolls_back_enablement_and_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            profile_a = root / "profiles" / "a"
            profile_b = root / "profiles" / "b"
            detached_plugins = profile_a / "plugins-detached"
            write_marker(source, "new")
            write_marker(profile_a / "plugins" / installer.PLUGIN_ID, "old")
            write_marker(profile_b / "plugins" / installer.LEGACY_PLUGIN_ID, "b")
            activation_events = []

            def activate_then_swap():
                activation_events.append("enabled")
                (profile_a / "plugins").rename(detached_plugins)
                (profile_a / "plugins").symlink_to(
                    profile_b / "plugins", target_is_directory=True
                )

                def rollback_activation():
                    activation_events.append("restored")

                return rollback_activation

            with self.assertRaisesRegex(RuntimeError, "profile directory changed"):
                installer.install_profile_bundle(
                    source,
                    profile_a,
                    link=False,
                    force=True,
                    activate=activate_then_swap,
                )

            self.assertEqual(activation_events, ["enabled", "restored"])
            self.assertEqual(
                (detached_plugins / installer.PLUGIN_ID / "marker.txt").read_text(),
                "old",
            )
            self.assertEqual(
                (profile_b / "plugins" / installer.LEGACY_PLUGIN_ID / "marker.txt").read_text(),
                "b",
            )
            self.assertFalse((profile_b / "plugins" / installer.PLUGIN_ID).exists())

    def test_copy_install_rejects_source_symlinks_before_replacing_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            external = root / "external"
            write_marker(source, "new")
            write_marker(target, "old")
            write_marker(external, "outside")
            (source / "dashboard").mkdir()
            (source / "dashboard" / "external-link").symlink_to(
                external, target_is_directory=True
            )

            with self.assertRaisesRegex(RuntimeError, "unsafe source entry"):
                installer.install_target(source, target, link=False, force=True)

            self.assertEqual((target / "marker.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((external / "marker.txt").read_text(encoding="utf-8"), "outside")

    def test_copy_install_rejects_symlink_inserted_during_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            external = root / "external"
            write_marker(source, "new")
            write_marker(target, "old")
            write_marker(external, "outside")
            (source / "dashboard").mkdir()
            real_open = installer.os.open
            injected = False

            def inject_symlink_during_copy(name, flags, *args, **kwargs):
                nonlocal injected
                if not injected and name == "late-link":
                    injected = True
                    (source / "dashboard" / "late-link").unlink()
                    (source / "dashboard" / "late-link").symlink_to(
                        external, target_is_directory=True
                    )
                return real_open(name, flags, *args, **kwargs)

            (source / "dashboard" / "late-link").write_text("reviewed", encoding="utf-8")
            with patch.object(installer.os, "open", side_effect=inject_symlink_during_copy):
                with self.assertRaisesRegex(RuntimeError, "unsafe source entry"):
                    installer.install_target(source, target, link=False, force=True)

            self.assertEqual((target / "marker.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((external / "marker.txt").read_text(encoding="utf-8"), "outside")

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
            real_copy = installer._copy_source_tree

            def fail_second(source, *args, **kwargs):
                if source == second_source:
                    raise OSError("second staging failed")
                return real_copy(source, *args, **kwargs)

            with patch.object(installer, "_copy_source_tree", side_effect=fail_second):
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

            def capture(source_path, profile_path, **options):
                captured["source"] = source_path
                captured["profile"] = profile_path
                captured["options"] = options

            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", ["install.py", "--no-enable"]),
                patch.object(installer, "resolve_hermes_home", return_value=profile),
                patch.object(installer, "install_profile_bundle", side_effect=capture),
                patch("builtins.print"),
            ):
                result = installer.main()

            self.assertEqual(result, 0)
            self.assertEqual(captured["source"], source)
            self.assertEqual(captured["profile"], profile)
            self.assertFalse(captured["options"]["link"])
            self.assertFalse(captured["options"]["force"])

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

            self.assertTrue(callable(result))
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
                patch.object(installer, "install_profile_bundle", side_effect=install),
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
                patch.object(
                    installer,
                    "install_profile_bundle",
                    side_effect=OSError("failed"),
                ),
                patch.object(installer, "_activate_plugin") as activate,
                patch("builtins.print"),
            ):
                result = installer.main()
            self.assertEqual(result, 2)
            activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
