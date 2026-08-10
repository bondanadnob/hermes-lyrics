#!/usr/bin/env python3
"""Install both halves of the plugin into the active local Hermes home."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = "apple-music-lyrics"
ROOT = Path(__file__).resolve().parents[1]


def remove_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def install_target(source: Path, target: Path, link: bool, force: bool) -> None:
    if target.is_symlink() and target.resolve() == source.resolve():
        print(f"Already linked: {target}")
        return
    source_resolved = source.resolve()
    target_resolved = target.resolve()
    if (
        source_resolved == target_resolved
        or target_resolved.is_relative_to(source_resolved)
        or source_resolved.is_relative_to(target_resolved)
    ):
        raise RuntimeError(
            f"source and target directories overlap: {source} -> {target}; "
            "choose a HERMES_HOME outside the checkout or use --desktop-only"
        )
    if target.exists() or target.is_symlink():
        if not force:
            raise RuntimeError(f"{target} already exists; rerun with --force to replace it")
        remove_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if link:
        target.symlink_to(source, target_is_directory=True)
        print(f"Linked {target} -> {source}")
    else:
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "__pycache__", ".DS_Store", "*.egg-info"
            ),
        )
        print(f"Copied {source} -> {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", action="store_true", help="symlink for local development")
    parser.add_argument("--force", action="store_true", help="replace an existing installation")
    parser.add_argument(
        "--desktop-only",
        action="store_true",
        help="install only desktop/plugin.js after `hermes plugins install`",
    )
    parser.add_argument("--no-enable", action="store_true", help="do not enable the backend")
    args = parser.parse_args()

    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    backend_target = hermes_home / "plugins" / PLUGIN_ID
    desktop_target = hermes_home / "desktop-plugins" / PLUGIN_ID

    try:
        if not args.desktop_only:
            install_target(ROOT, backend_target, args.link, args.force)
        install_target(ROOT / "desktop", desktop_target, args.link, args.force)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.no_enable:
        hermes = shutil.which("hermes")
        if hermes:
            completed = subprocess.run(
                [
                    hermes,
                    "plugins",
                    "enable",
                    "--no-allow-tool-override",
                    PLUGIN_ID,
                ],
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                print("warning: plugin files installed, but backend enable failed", file=sys.stderr)
                return completed.returncode
        else:
            print("warning: `hermes` not found; enable the backend manually", file=sys.stderr)

    print("Installed Apple Music Lyrics. Restart Hermes Desktop and its local server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
