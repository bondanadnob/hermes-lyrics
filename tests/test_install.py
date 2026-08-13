import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
SPEC = importlib.util.spec_from_file_location("apple_music_lyrics_installer", INSTALLER)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_empty_hermes_home_defaults_to_the_user_home(self):
        with patch.dict(os.environ, {"HERMES_HOME": ""}):
            with patch.object(installer.Path, "home", return_value=Path("/safe-home")):
                self.assertEqual(
                    installer.resolve_hermes_home(),
                    Path("/safe-home/.hermes"),
                )

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


if __name__ == "__main__":
    unittest.main()
