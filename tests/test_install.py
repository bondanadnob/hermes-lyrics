import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install.py"
SPEC = importlib.util.spec_from_file_location("apple_music_lyrics_installer", INSTALLER)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
