import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
RECOGNITION_LOCK = Path(__file__).resolve().parents[1] / "requirements-recognition.lock"
SPEC = importlib.util.spec_from_file_location("apple_music_lyrics_installer", INSTALLER)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_readme_documents_the_unified_plugin_install(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("--desktop-only", readme)
        self.assertIn("plugins/apple-music-lyrics/desktop/plugin.js", readme)
        self.assertIn("two-target", readme)

    def test_notices_record_the_shazamio_core_artifact_license(self):
        notices = (
            Path(__file__).resolve().parents[1] / "THIRD_PARTY_NOTICES.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`shazamio-core==1.1.2`", notices)
        self.assertIn("wheel and source distribution both include an MIT license", notices)
        self.assertIn("Copyright © 2024 dotX12", notices)
        self.assertIn("PyPI metadata license field is blank", notices)

    def test_recognition_lock_exactly_pins_and_hashes_every_package(self):
        text = RECOGNITION_LOCK.read_text(encoding="utf-8")
        blocks = []
        current = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if not raw_line[:1].isspace() and not line.startswith("--hash="):
                if current:
                    blocks.append(current)
                current = [line]
            else:
                current.append(line.rstrip("\\").strip())
        if current:
            blocks.append(current)

        self.assertGreaterEqual(len(blocks), 10)
        names = set()
        for block in blocks:
            requirement = block[0].rstrip("\\").strip()
            self.assertRegex(requirement, r"^[A-Za-z0-9_.-]+==[^\s;]+(?:\s*;.*)?$")
            self.assertNotIn("@", requirement)
            self.assertTrue(
                any(line.startswith("--hash=sha256:") for line in block[1:]),
                requirement,
            )
            names.add(requirement.split("==", 1)[0].lower().replace("_", "-"))

        self.assertIn("shazamio", names)
        self.assertIn("shazamio-core", names)
        self.assertIn("pip", names)
        self.assertIn("setuptools", names)

    def test_empty_hermes_home_defaults_to_the_user_home(self):
        with patch.dict(os.environ, {"HERMES_HOME": ""}):
            with (
                patch.object(installer.Path, "home", return_value=Path("/safe-home")),
                patch.object(installer.shutil, "which", return_value=None),
            ):
                self.assertEqual(
                    installer.resolve_hermes_home(),
                    Path("/safe-home/.hermes"),
                )

    def test_unset_hermes_home_can_resolve_the_active_cli_profile(self):
        class Result:
            returncode = 0
            stdout = "/Users/example/.hermes/profiles/work/config.yaml\n"

        with (
            patch.dict(os.environ, {"HERMES_HOME": ""}),
            patch.object(installer.shutil, "which", return_value="/usr/local/bin/hermes"),
            patch.object(installer.subprocess, "run", return_value=Result()),
        ):
            home = installer.resolve_hermes_home()

        self.assertEqual(home, Path("/Users/example/.hermes/profiles/work"))

    def test_rejects_target_inside_source_before_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "checkout"
            source.mkdir()
            target = source / ".hermes" / "plugins" / "apple-music-lyrics"
            with patch.object(installer.shutil, "copytree") as copytree:
                with self.assertRaises(RuntimeError):
                    installer.install_target(source, target, link=False, force=False)
            copytree.assert_not_called()

    def test_rejects_source_inside_target_before_force_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plugin-target"
            source = target / "checkout"
            source.mkdir(parents=True)
            with patch.object(installer, "remove_target") as remove_target:
                with self.assertRaises(RuntimeError):
                    installer.install_target(source, target, link=False, force=True)
            remove_target.assert_not_called()

    def test_force_never_deletes_the_source_when_already_in_target(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "plugin"
            source.mkdir()
            sentinel = source / "keep.txt"
            sentinel.write_text("safe", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "overlap"):
                installer.install_target(source, source, link=False, force=True)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")

    def test_failed_forced_copy_preserves_the_existing_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "new.txt").write_text("new", encoding="utf-8")
            sentinel = target / "healthy.txt"
            sentinel.write_text("healthy", encoding="utf-8")

            with patch.object(
                installer.shutil,
                "copytree",
                side_effect=OSError("injected copy failure"),
            ):
                with self.assertRaises(OSError):
                    installer.install_target(source, target, link=False, force=True)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "healthy")

    def test_missing_link_source_preserves_the_existing_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "missing-source"
            target = root / "target"
            target.mkdir()
            sentinel = target / "healthy.txt"
            sentinel.write_text("healthy", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "source directory"):
                installer.install_target(source, target, link=True, force=True)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "healthy")

    def test_pair_staging_failure_preserves_both_existing_installations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend_source = root / "backend-source"
            desktop_source = root / "desktop-source"
            backend_target = root / "backend-target"
            desktop_target = root / "desktop-target"
            for path in (backend_source, desktop_source, backend_target, desktop_target):
                path.mkdir()
            (backend_source / "new.txt").write_text("new backend", encoding="utf-8")
            (desktop_source / "new.txt").write_text("new desktop", encoding="utf-8")
            (backend_target / "old.txt").write_text("old backend", encoding="utf-8")
            (desktop_target / "old.txt").write_text("old desktop", encoding="utf-8")
            real_copytree = installer.shutil.copytree

            def fail_desktop_copy(source, *args, **kwargs):
                if Path(source) == desktop_source:
                    raise OSError("injected desktop staging failure")
                return real_copytree(source, *args, **kwargs)

            with patch.object(installer.shutil, "copytree", side_effect=fail_desktop_copy):
                with self.assertRaises(OSError):
                    installer.install_targets(
                        [
                            (backend_source, backend_target),
                            (desktop_source, desktop_target),
                        ],
                        link=False,
                        force=True,
                    )

            self.assertEqual((backend_target / "old.txt").read_text(), "old backend")
            self.assertEqual((desktop_target / "old.txt").read_text(), "old desktop")
            self.assertFalse((backend_target / "new.txt").exists())

    def test_pair_commit_failure_rolls_back_both_installations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend_source = root / "backend-source"
            desktop_source = root / "desktop-source"
            backend_target = root / "backend-target"
            desktop_target = root / "desktop-target"
            for path in (backend_source, desktop_source, backend_target, desktop_target):
                path.mkdir()
            (backend_source / "new.txt").write_text("new backend", encoding="utf-8")
            (desktop_source / "new.txt").write_text("new desktop", encoding="utf-8")
            (backend_target / "old.txt").write_text("old backend", encoding="utf-8")
            (desktop_target / "old.txt").write_text("old desktop", encoding="utf-8")
            real_rename = installer.Path.rename

            def fail_desktop_commit(path, destination):
                if path.name == "payload" and Path(destination) == desktop_target:
                    raise OSError("injected desktop commit failure")
                return real_rename(path, destination)

            with patch.object(installer.Path, "rename", fail_desktop_commit):
                with self.assertRaises(OSError):
                    installer.install_targets(
                        [
                            (backend_source, backend_target),
                            (desktop_source, desktop_target),
                        ],
                        link=False,
                        force=True,
                    )

            self.assertEqual((backend_target / "old.txt").read_text(), "old backend")
            self.assertEqual((desktop_target / "old.txt").read_text(), "old desktop")
            self.assertFalse((backend_target / "new.txt").exists())
            self.assertFalse((desktop_target / "new.txt").exists())

    def test_recognition_runtime_rejects_a_missing_lock_before_running_commands(self):
        calls = []

        class Result:
            returncode = 0

        def runner(command, **options):
            calls.append((command, options))
            if command[1:3] == ["-m", "venv"]:
                staged = Path(command[-1])
                (staged / "bin").mkdir(parents=True)
                (staged / "bin" / "python").write_text("python", encoding="utf-8")
            return Result()

        with tempfile.TemporaryDirectory() as temp:
            missing_lock = Path(temp) / "missing.lock"
            with patch.object(installer, "RECOGNITION_LOCK", missing_lock):
                with self.assertRaisesRegex(RuntimeError, "dependency lock"):
                    installer.install_recognition_runtime(
                        Path(temp) / "recognition-venv",
                        python_executable=Path("/usr/local/bin/python3.11"),
                        runner=runner,
                    )

        self.assertEqual(calls, [])

    def test_bundle_rejects_a_missing_lock_before_staging_plugin_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend_source = root / "backend-source"
            desktop_source = root / "desktop-source"
            backend_source.mkdir()
            desktop_source.mkdir()
            missing_lock = root / "missing.lock"
            real_copytree = installer.shutil.copytree
            with (
                patch.object(installer, "RECOGNITION_LOCK", missing_lock),
                patch.object(
                    installer.shutil,
                    "copytree",
                    wraps=real_copytree,
                ) as copytree,
            ):
                with self.assertRaisesRegex(RuntimeError, "dependency lock"):
                    installer.install_bundle(
                        [
                            (backend_source, root / "backend-target"),
                            (desktop_source, root / "desktop-target"),
                        ],
                        root / "recognition-target",
                        link=False,
                        force=False,
                    )

            copytree.assert_not_called()

    def test_bundle_rejects_a_recognition_target_overlapping_the_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            plugin_target = root / "plugins" / "apple-music-lyrics"
            recognition_target = source / "recognition-venv"

            real_copytree = installer.shutil.copytree

            def install_runtime(target, **_options):
                (target / "bin").mkdir(parents=True)
                (target / "bin" / "python").write_text("python", encoding="utf-8")

            with (
                patch.object(
                    installer.shutil,
                    "copytree",
                    wraps=real_copytree,
                ) as copytree,
                patch.object(
                    installer,
                    "install_recognition_runtime",
                    side_effect=install_runtime,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "recognition runtime.*overlap"):
                    installer.install_bundle(
                        [(source, plugin_target)],
                        recognition_target,
                        link=False,
                        force=False,
                    )

            copytree.assert_not_called()
            self.assertFalse(recognition_target.exists())

    def test_recognition_runtime_installs_a_pinned_package_without_a_shell(self):
        runtime_installer = getattr(installer, "install_recognition_runtime", None)
        if not callable(runtime_installer):
            self.fail("install_recognition_runtime is required")

        calls = []

        class Completed:
            returncode = 0

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1:3] == ["-m", "venv"]:
                staged = Path(command[-1])
                (staged / "bin").mkdir(parents=True)
                (staged / "bin" / "python").write_text("", encoding="utf-8")
            return Completed()

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "plugin-data" / "recognition-venv"
            runtime_installer(
                target,
                python_executable=Path("/usr/local/bin/python3.11"),
                runner=runner,
            )

            self.assertTrue((target / "bin" / "python").is_file())

        self.assertEqual(calls[0][0][1:3], ["-m", "venv"])
        self.assertTrue(
            any(str(RECOGNITION_LOCK) in command for command, _ in calls)
        )
        self.assertFalse(any(kwargs.get("shell", False) for _, kwargs in calls))

    def test_recognition_runtime_installs_only_from_the_hash_lock(self):
        calls = []

        class Result:
            returncode = 0

        def runner(command, **options):
            calls.append((command, options))
            if command[1:3] == ["-m", "venv"]:
                runtime = Path(command[-1])
                python = runtime / "bin" / "python"
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text("python", encoding="utf-8")
            return Result()

        with tempfile.TemporaryDirectory() as temp:
            installer.install_recognition_runtime(
                Path(temp) / "recognition-venv",
                python_executable=Path("/usr/local/bin/python3.11"),
                runner=runner,
            )

        pip_calls = [
            command
            for command, _options in calls
            if command[1:6] == ["-I", "-m", "pip", "--isolated", "install"]
        ]
        self.assertEqual(len(pip_calls), 1)
        command = pip_calls[0]
        self.assertEqual(command[4:6], ["--isolated", "install"])
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--no-cache-dir", command)
        self.assertEqual(
            command[command.index("--index-url") + 1],
            "https://pypi.org/simple",
        )
        self.assertEqual(command[command.index("-r") + 1], str(RECOGNITION_LOCK))
        self.assertFalse(any(value.startswith("shazamio==") for value in command))

    def test_recognition_runtime_pip_ignores_parent_python_paths(self):
        calls = []

        class Result:
            returncode = 0

        def runner(command, **options):
            calls.append((command, options))
            if command[1:3] == ["-m", "venv"]:
                runtime = Path(command[-1])
                python = runtime / "bin" / "python"
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text("python", encoding="utf-8")
            return Result()

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "recognition-venv"
            installer.install_recognition_runtime(
                target,
                python_executable=Path("/usr/local/bin/python3.11"),
                runner=runner,
            )

        self.assertEqual(calls[1][0][1:4], ["-I", "-m", "pip"])

    def test_recognition_runtime_subprocesses_receive_a_minimal_environment(self):
        calls = []

        class Result:
            returncode = 0

        def runner(command, **options):
            calls.append((command, options))
            if command[1:3] == ["-m", "venv"]:
                runtime = Path(command[-1])
                python = runtime / "bin" / "python"
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text("python", encoding="utf-8")
            return Result()

        poisoned = {
            "PYTHONPATH": "/poison/python",
            "PYTHONHOME": "/poison/home",
            "PIP_INDEX_URL": "https://poison.invalid/simple",
            "PIP_CONFIG_FILE": "/poison/pip.conf",
            "HTTPS_PROXY": "http://poison.invalid",
            "VIRTUAL_ENV": "/poison/venv",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, poisoned, clear=False
        ):
            installer.install_recognition_runtime(
                Path(temp) / "recognition-venv",
                python_executable=Path("/usr/local/bin/python3.11"),
                runner=runner,
            )

        allowed = {
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTHONNOUSERSITE",
            "TMPDIR",
        }
        for _command, options in calls:
            self.assertIn("env", options)
            environment = options["env"]
            self.assertLessEqual(set(environment), allowed)
            self.assertTrue(set(poisoned).isdisjoint(environment))
            self.assertEqual(environment["PYTHONNOUSERSITE"], "1")

    def test_recognition_runtime_verifies_distribution_without_import_side_effects(self):
        calls = []

        class Result:
            returncode = 0

        def runner(command, **options):
            calls.append((command, options))
            if command[1:3] == ["-m", "venv"]:
                runtime = Path(command[-1])
                python = runtime / "bin" / "python"
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text("python", encoding="utf-8")
            return Result()

        with tempfile.TemporaryDirectory() as temp:
            installer.install_recognition_runtime(
                Path(temp) / "recognition-venv",
                python_executable=Path("/usr/local/bin/python3.11"),
                runner=runner,
            )

        verification = next(command for command, _options in calls if "-c" in command)
        code = verification[verification.index("-c") + 1]
        self.assertNotIn("import shazamio;", code)
        self.assertIn("metadata.version", code)
        self.assertIn("find_spec", code)

    def test_recognition_runtime_verifies_the_pinned_core_distribution(self):
        calls = []

        class Result:
            returncode = 0

        def runner(command, **_options):
            calls.append(command)
            if command[1:3] == ["-m", "venv"]:
                runtime = Path(command[-1])
                python = runtime / "bin" / "python"
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text("python", encoding="utf-8")
            return Result()

        with tempfile.TemporaryDirectory() as temp:
            installer.install_recognition_runtime(
                Path(temp) / "recognition-venv",
                python_executable=Path("/usr/local/bin/python3.11"),
                runner=runner,
            )

        verification = next(command for command in calls if "-c" in command)
        code = verification[verification.index("-c") + 1]
        self.assertIn("metadata.version('shazamio-core') == '1.1.2'", code)
        self.assertIn("find_spec('shazamio_core')", code)

    def test_recognition_runtime_runs_pip_check_before_commit(self):
        calls = []

        class Result:
            returncode = 0

        def runner(command, **_options):
            calls.append(command)
            if command[1:3] == ["-m", "venv"]:
                runtime = Path(command[-1])
                python = runtime / "bin" / "python"
                python.parent.mkdir(parents=True, exist_ok=True)
                python.write_text("python", encoding="utf-8")
            return Result()

        with tempfile.TemporaryDirectory() as temp:
            installer.install_recognition_runtime(
                Path(temp) / "recognition-venv",
                python_executable=Path("/usr/local/bin/python3.11"),
                runner=runner,
            )

        self.assertTrue(
            any(command[1:] == ["-I", "-m", "pip", "check"] for command in calls)
        )

    def test_recognition_runtime_swap_failure_restores_the_previous_runtime(self):
        class Completed:
            returncode = 0

        def runner(command, **kwargs):
            if command[1:3] == ["-m", "venv"]:
                staged = Path(command[-1])
                (staged / "bin").mkdir(parents=True)
                (staged / "bin" / "python").write_text("", encoding="utf-8")
            return Completed()

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "plugin-data" / "recognition-venv"
            target.mkdir(parents=True)
            sentinel = target / "healthy.txt"
            sentinel.write_text("healthy", encoding="utf-8")
            real_rename = installer.Path.rename

            def fail_new_runtime_commit(path, destination):
                if path.name == "venv" and Path(destination) == target:
                    raise OSError("injected runtime commit failure")
                return real_rename(path, destination)

            with patch.object(installer.Path, "rename", fail_new_runtime_commit):
                with self.assertRaises(OSError):
                    installer.install_recognition_runtime(
                        target,
                        python_executable=Path("/usr/local/bin/python3.11"),
                        runner=runner,
                    )

            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "healthy")

    def test_bundle_runtime_commit_failure_restores_all_three_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backend_source = root / "backend-source"
            desktop_source = root / "desktop-source"
            backend_target = root / "backend-target"
            desktop_target = root / "desktop-target"
            runtime_target = root / "runtime-target"
            for path in (
                backend_source,
                desktop_source,
                backend_target,
                desktop_target,
                runtime_target,
            ):
                path.mkdir()
            (backend_source / "new.txt").write_text("new backend", encoding="utf-8")
            (desktop_source / "new.txt").write_text("new desktop", encoding="utf-8")
            (backend_target / "old.txt").write_text("old backend", encoding="utf-8")
            (desktop_target / "old.txt").write_text("old desktop", encoding="utf-8")
            (runtime_target / "old.txt").write_text("old runtime", encoding="utf-8")

            def install_runtime(target, **_options):
                target.mkdir(parents=True)
                (target / "new.txt").write_text("new runtime", encoding="utf-8")

            real_rename = installer.Path.rename

            def fail_runtime_commit(path, destination):
                if path.name == "payload" and Path(destination) == runtime_target:
                    raise OSError("injected runtime commit failure")
                return real_rename(path, destination)

            with (
                patch.object(
                    installer,
                    "install_recognition_runtime",
                    side_effect=install_runtime,
                ),
                patch.object(installer.Path, "rename", fail_runtime_commit),
            ):
                with self.assertRaisesRegex(OSError, "runtime commit failure"):
                    installer.install_bundle(
                        [
                            (backend_source, backend_target),
                            (desktop_source, desktop_target),
                        ],
                        runtime_target,
                        link=False,
                        force=True,
                    )

            self.assertEqual(
                (backend_target / "old.txt").read_text(encoding="utf-8"),
                "old backend",
            )
            self.assertEqual(
                (desktop_target / "old.txt").read_text(encoding="utf-8"),
                "old desktop",
            )
            self.assertEqual(
                (runtime_target / "old.txt").read_text(encoding="utf-8"),
                "old runtime",
            )
            self.assertFalse((backend_target / "new.txt").exists())
            self.assertFalse((desktop_target / "new.txt").exists())
            self.assertFalse((runtime_target / "new.txt").exists())

    def test_main_installs_one_unified_plugin_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            hermes_home = root / "hermes"
            source.mkdir()
            captured = {}

            def capture_bundle(targets, recognition_target, **options):
                captured["targets"] = targets
                captured["recognition_target"] = recognition_target
                captured["options"] = options

            argv = ["install.py", "--no-enable"]
            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", argv),
                patch.object(installer, "resolve_hermes_home", return_value=hermes_home),
                patch.object(installer, "install_bundle", side_effect=capture_bundle),
                patch("builtins.print"),
            ):
                exit_code = installer.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                captured["targets"],
                [
                    (
                        source,
                        hermes_home / "plugins" / "apple-music-lyrics",
                    )
                ],
            )

    def test_main_marks_the_standalone_desktop_copy_obsolete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            hermes_home = root / "hermes"
            source.mkdir()
            captured = {}

            def capture_bundle(_targets, _recognition_target, **options):
                captured.update(options)

            argv = ["install.py", "--no-enable"]
            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", argv),
                patch.object(installer, "resolve_hermes_home", return_value=hermes_home),
                patch.object(installer, "install_bundle", side_effect=capture_bundle),
                patch("builtins.print"),
            ):
                exit_code = installer.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                captured.get("obsolete_targets"),
                [hermes_home / "desktop-plugins" / "apple-music-lyrics"],
            )

    def test_main_rejects_the_obsolete_desktop_only_mode_before_installing(self):
        argv = ["install.py", "--desktop-only", "--no-enable"]
        with (
            patch.object(installer.sys, "argv", argv),
            patch.object(installer.sys, "stderr", io.StringIO()),
            patch.object(installer, "install_bundle") as install_bundle,
        ):
            with self.assertRaises(SystemExit) as raised:
                installer.main()

        self.assertEqual(raised.exception.code, 2)
        install_bundle.assert_not_called()

    def test_bundle_removes_the_legacy_desktop_copy_after_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            unified_target = root / "plugins" / "apple-music-lyrics"
            standalone_target = root / "desktop-plugins" / "apple-music-lyrics"
            runtime_target = root / "plugin-data" / "recognition-venv"
            (source / "desktop").mkdir(parents=True)
            (source / "desktop" / "plugin.js").write_text(
                "export default {}",
                encoding="utf-8",
            )
            standalone_target.mkdir(parents=True)
            (standalone_target / "plugin.js").write_text(
                "legacy",
                encoding="utf-8",
            )

            def install_runtime(target, **_options):
                (target / "bin").mkdir(parents=True)
                (target / "bin" / "python").write_text("python", encoding="utf-8")

            with patch.object(
                installer,
                "install_recognition_runtime",
                side_effect=install_runtime,
            ):
                installer.install_bundle(
                    [(source, unified_target)],
                    runtime_target,
                    link=False,
                    force=False,
                    obsolete_targets=[standalone_target],
                )

            discovered = [
                path
                for path in (
                    unified_target / "desktop" / "plugin.js",
                    standalone_target / "plugin.js",
                )
                if path.is_file()
            ]
            self.assertEqual(discovered, [unified_target / "desktop" / "plugin.js"])
            self.assertTrue((runtime_target / "bin" / "python").is_file())

    def test_bundle_restores_a_quarantined_legacy_copy_after_commit_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            unified_target = root / "plugins" / "apple-music-lyrics"
            standalone_target = root / "desktop-plugins" / "apple-music-lyrics"
            runtime_target = root / "plugin-data" / "recognition-venv"
            (source / "desktop").mkdir(parents=True)
            (source / "desktop" / "plugin.js").write_text("new", encoding="utf-8")
            standalone_target.mkdir(parents=True)
            legacy = standalone_target / "plugin.js"
            legacy.write_text("legacy", encoding="utf-8")
            runtime_target.mkdir(parents=True)
            (runtime_target / "old.txt").write_text("old runtime", encoding="utf-8")
            observed = {"legacy_quarantined": False}

            def install_runtime(target, **_options):
                (target / "bin").mkdir(parents=True)
                (target / "bin" / "python").write_text("python", encoding="utf-8")

            real_rename = installer.Path.rename

            def fail_runtime_commit(path, destination):
                if path.name == "payload" and Path(destination) == runtime_target:
                    observed["legacy_quarantined"] = not standalone_target.exists()
                    raise OSError("injected runtime commit failure")
                return real_rename(path, destination)

            with (
                patch.object(
                    installer,
                    "install_recognition_runtime",
                    side_effect=install_runtime,
                ),
                patch.object(installer.Path, "rename", fail_runtime_commit),
            ):
                with self.assertRaisesRegex(OSError, "runtime commit failure"):
                    installer.install_bundle(
                        [(source, unified_target)],
                        runtime_target,
                        link=False,
                        force=True,
                        obsolete_targets=[standalone_target],
                    )

            self.assertTrue(observed["legacy_quarantined"])
            self.assertEqual(legacy.read_text(encoding="utf-8"), "legacy")
            self.assertFalse(unified_target.exists())
            self.assertEqual(
                (runtime_target / "old.txt").read_text(encoding="utf-8"),
                "old runtime",
            )

    def test_main_does_not_touch_runtime_when_plugin_staging_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            desktop_source = source / "desktop"
            hermes_home = root / "hermes"
            backend_target = hermes_home / "plugins" / "apple-music-lyrics"
            desktop_target = (
                hermes_home / "desktop-plugins" / "apple-music-lyrics"
            )
            runtime_target = (
                hermes_home
                / "plugin-data"
                / "apple-music-lyrics"
                / "recognition-venv"
            )
            desktop_source.mkdir(parents=True)
            backend_target.mkdir(parents=True)
            desktop_target.mkdir(parents=True)
            runtime_target.mkdir(parents=True)
            (source / "new-backend.txt").write_text("new", encoding="utf-8")
            (desktop_source / "new-desktop.txt").write_text("new", encoding="utf-8")
            (backend_target / "old.txt").write_text("old backend", encoding="utf-8")
            (desktop_target / "old.txt").write_text("old desktop", encoding="utf-8")
            (runtime_target / "old.txt").write_text("old runtime", encoding="utf-8")

            runtime_calls = []

            def install_runtime(target, **_options):
                runtime_calls.append(target)
                installer.remove_target(target)
                target.mkdir(parents=True)
                (target / "new.txt").write_text("new runtime", encoding="utf-8")

            real_copytree = installer.shutil.copytree

            def fail_desktop_copy(copy_source, *args, **kwargs):
                if Path(copy_source) == desktop_source:
                    raise OSError("injected desktop staging failure")
                return real_copytree(copy_source, *args, **kwargs)

            argv = ["install.py", "--force", "--no-enable"]
            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", argv),
                patch.object(installer, "resolve_hermes_home", return_value=hermes_home),
                patch.object(
                    installer,
                    "install_recognition_runtime",
                    side_effect=install_runtime,
                ),
                patch.object(
                    installer.shutil,
                    "copytree",
                    side_effect=fail_desktop_copy,
                ),
                patch("builtins.print"),
            ):
                exit_code = installer.main()

            self.assertEqual(exit_code, 2)
            self.assertEqual(runtime_calls, [])
            self.assertTrue((runtime_target / "old.txt").is_file())
            self.assertEqual(
                (runtime_target / "old.txt").read_text(encoding="utf-8"),
                "old runtime",
            )
            self.assertFalse((runtime_target / "new.txt").exists())
            self.assertEqual(
                (backend_target / "old.txt").read_text(encoding="utf-8"),
                "old backend",
            )
            self.assertEqual(
                (desktop_target / "old.txt").read_text(encoding="utf-8"),
                "old desktop",
            )

    def test_main_builds_runtime_offline_before_committing_unified_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            desktop_source = source / "desktop"
            hermes_home = root / "hermes"
            backend_target = hermes_home / "plugins" / "apple-music-lyrics"
            desktop_target = hermes_home / "desktop-plugins" / "apple-music-lyrics"
            runtime_target = (
                hermes_home
                / "plugin-data"
                / "apple-music-lyrics"
                / "recognition-venv"
            )
            desktop_source.mkdir(parents=True)
            (source / "backend.txt").write_text("backend", encoding="utf-8")
            (desktop_source / "desktop.txt").write_text("desktop", encoding="utf-8")
            runtime_calls = []

            def install_runtime(target, **kwargs):
                runtime_calls.append(
                    (
                        target,
                        kwargs,
                        backend_target.exists(),
                        desktop_target.exists(),
                        runtime_target.exists(),
                    )
                )
                (target / "bin").mkdir(parents=True)
                (target / "bin" / "python").write_text("python", encoding="utf-8")

            argv = ["install.py", "--no-enable"]
            with (
                patch.object(installer, "ROOT", source),
                patch.object(installer.sys, "argv", argv),
                patch.object(installer, "resolve_hermes_home", return_value=hermes_home),
                patch.object(
                    installer,
                    "install_recognition_runtime",
                    side_effect=install_runtime,
                ),
                patch("builtins.print"),
            ):
                exit_code = installer.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(runtime_calls), 1)
            staged_runtime, _kwargs, backend_live, desktop_live, runtime_live = (
                runtime_calls[0]
            )
            self.assertNotEqual(staged_runtime, runtime_target)
            self.assertFalse(backend_live)
            self.assertFalse(desktop_live)
            self.assertFalse(runtime_live)
            self.assertEqual(
                (backend_target / "backend.txt").read_text(encoding="utf-8"),
                "backend",
            )
            self.assertEqual(
                (backend_target / "desktop" / "desktop.txt").read_text(
                    encoding="utf-8"
                ),
                "desktop",
            )
            self.assertFalse(desktop_target.exists())
            self.assertTrue((runtime_target / "bin" / "python").is_file())


if __name__ == "__main__":
    unittest.main()
