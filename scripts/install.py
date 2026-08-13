#!/usr/bin/env python3
"""Install both halves of the plugin into the active local Hermes home."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_ID = "apple-music-lyrics"
ROOT = Path(__file__).resolve().parents[1]


def remove_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def resolve_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured is None or not configured.strip():
        return Path.home() / ".hermes"
    return Path(configured).expanduser()


class _PreparedTarget:
    __slots__ = (
        "source",
        "target",
        "staging_root",
        "staged",
        "backup",
        "target_existed",
        "backed_up",
        "committed",
    )

    def __init__(
        self,
        *,
        source: Path,
        target: Path,
        staging_root: Path,
        staged: Path,
        backup: Path,
        target_existed: bool,
    ) -> None:
        self.source = source
        self.target = target
        self.staging_root = staging_root
        self.staged = staged
        self.backup = backup
        self.target_existed = target_existed
        self.backed_up = False
        self.committed = False


def _validate_target(source: Path, target: Path, force: bool) -> bool:
    if not source.is_dir():
        raise RuntimeError(f"source directory does not exist: {source}")
    if target.is_symlink() and target.resolve() == source.resolve():
        print(f"Already linked: {target}")
        return False
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
    target_exists = target.exists() or target.is_symlink()
    if target_exists and not force:
        raise RuntimeError(f"{target} already exists; rerun with --force to replace it")
    return True


def install_targets(
    targets: list[tuple[Path, Path]], link: bool, force: bool
) -> None:
    installable = [
        (source, target)
        for source, target in targets
        if _validate_target(source, target, force)
    ]
    prepared: list[_PreparedTarget] = []

    try:
        # Stage every payload before touching any existing installation.
        for source, target in installable:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging_root = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.install-", dir=target.parent)
            )
            plan = _PreparedTarget(
                source=source,
                target=target,
                staging_root=staging_root,
                staged=staging_root / "payload",
                backup=staging_root / "previous",
                target_existed=target.exists() or target.is_symlink(),
            )
            prepared.append(plan)
            if link:
                plan.staged.symlink_to(source.resolve(), target_is_directory=True)
            else:
                shutil.copytree(
                    source,
                    plan.staged,
                    ignore=shutil.ignore_patterns(
                        ".git", ".venv", "__pycache__", ".DS_Store", "*.egg-info"
                    ),
                )

        try:
            for plan in prepared:
                if plan.target_existed:
                    plan.target.rename(plan.backup)
                    plan.backed_up = True
                plan.staged.rename(plan.target)
                plan.committed = True
        except Exception as commit_error:
            rollback_error: Exception | None = None
            for plan in reversed(prepared):
                try:
                    if plan.committed and (plan.target.exists() or plan.target.is_symlink()):
                        remove_target(plan.target)
                        plan.committed = False
                    if plan.backed_up and (plan.backup.exists() or plan.backup.is_symlink()):
                        plan.backup.rename(plan.target)
                        plan.backed_up = False
                except Exception as exc:  # preserve backups for manual recovery
                    rollback_error = rollback_error or exc
            if rollback_error is not None:
                raise RuntimeError(
                    f"installation failed and rollback was incomplete: {rollback_error}"
                ) from commit_error
            raise

        for plan in prepared:
            if plan.backup.exists() or plan.backup.is_symlink():
                try:
                    remove_target(plan.backup)
                    plan.backed_up = False
                except OSError as exc:
                    print(f"warning: could not remove installer backup: {exc}", file=sys.stderr)
    finally:
        for plan in prepared:
            if not (plan.backup.exists() or plan.backup.is_symlink()):
                shutil.rmtree(plan.staging_root, ignore_errors=True)

    for plan in prepared:
        if link:
            print(f"Linked {plan.target} -> {plan.source}")
        else:
            print(f"Copied {plan.source} -> {plan.target}")


def install_target(source: Path, target: Path, link: bool, force: bool) -> None:
    install_targets([(source, target)], link=link, force=force)


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

    hermes_home = resolve_hermes_home()
    backend_target = hermes_home / "plugins" / PLUGIN_ID
    desktop_target = hermes_home / "desktop-plugins" / PLUGIN_ID

    try:
        targets = [(ROOT / "desktop", desktop_target)]
        if not args.desktop_only:
            targets.insert(0, (ROOT, backend_target))
        install_targets(targets, args.link, args.force)
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
