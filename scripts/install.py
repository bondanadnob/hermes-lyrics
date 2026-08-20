#!/usr/bin/env python3
"""Install the unified plugin and recognition runtime into the local Hermes home."""

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
RECOGNITION_LOCK = ROOT / "requirements-recognition.lock"


def remove_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def resolve_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured is not None and configured.strip():
        return Path(configured).expanduser()

    hermes = shutil.which("hermes")
    if hermes:
        try:
            completed = subprocess.run(
                [hermes, "config", "path"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
                check=False,
            )
            config_path = Path(completed.stdout.strip()).expanduser()
            if (
                completed.returncode == 0
                and config_path.is_absolute()
                and config_path.name == "config.yaml"
            ):
                return config_path.parent
        except (OSError, subprocess.SubprocessError):
            pass

    return Path.home() / ".hermes"


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


class _PreparedRemoval:
    __slots__ = ("target", "staging_root", "backup", "backed_up")

    def __init__(self, *, target: Path, staging_root: Path) -> None:
        self.target = target
        self.staging_root = staging_root
        self.backup = staging_root / "previous"
        self.backed_up = False


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
            "choose a HERMES_HOME outside the checkout"
        )
    target_exists = target.exists() or target.is_symlink()
    if target_exists and not force:
        raise RuntimeError(f"{target} already exists; rerun with --force to replace it")
    return True


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    return (
        first_resolved == second_resolved
        or first_resolved.is_relative_to(second_resolved)
        or second_resolved.is_relative_to(first_resolved)
    )


def _validate_recognition_target(
    recognition_target: Path,
    targets: list[tuple[Path, Path]],
) -> None:
    for source, plugin_target in targets:
        if _paths_overlap(recognition_target, source) or _paths_overlap(
            recognition_target, plugin_target
        ):
            raise RuntimeError(
                "recognition runtime target overlaps a plugin source or target: "
                f"{recognition_target}"
            )


def _cleanup_prepared(prepared: list[_PreparedTarget]) -> None:
    for plan in prepared:
        if not (plan.backup.exists() or plan.backup.is_symlink()):
            shutil.rmtree(plan.staging_root, ignore_errors=True)


def _cleanup_removals(removals: list[_PreparedRemoval]) -> None:
    for plan in removals:
        if not (plan.backup.exists() or plan.backup.is_symlink()):
            shutil.rmtree(plan.staging_root, ignore_errors=True)


def _restore_removals(removals: list[_PreparedRemoval]) -> Exception | None:
    rollback_error: Exception | None = None
    for plan in reversed(removals):
        if not plan.backed_up:
            continue
        try:
            if plan.target.exists() or plan.target.is_symlink():
                raise RuntimeError(
                    f"cannot restore obsolete plugin path because it reappeared: {plan.target}"
                )
            plan.backup.rename(plan.target)
            plan.backed_up = False
        except Exception as exc:
            rollback_error = rollback_error or exc
    return rollback_error


def _quarantine_obsolete_targets(targets: list[Path]) -> list[_PreparedRemoval]:
    removals: list[_PreparedRemoval] = []
    try:
        for target in targets:
            if not (target.exists() or target.is_symlink()):
                continue
            staging_root = Path(
                tempfile.mkdtemp(prefix=f".{target.name}.remove-", dir=target.parent)
            )
            plan = _PreparedRemoval(target=target, staging_root=staging_root)
            removals.append(plan)
            target.rename(plan.backup)
            plan.backed_up = True
    except Exception as quarantine_error:
        rollback_error = _restore_removals(removals)
        _cleanup_removals(removals)
        if rollback_error is not None:
            raise RuntimeError(
                f"could not quarantine obsolete plugin paths and rollback was incomplete: "
                f"{rollback_error}"
            ) from quarantine_error
        raise
    return removals


def _prepare_targets(
    targets: list[tuple[Path, Path]],
    *,
    link: bool,
    force: bool,
    validated: bool = False,
) -> list[_PreparedTarget]:
    installable = targets
    if not validated:
        installable = [
            (source, target)
            for source, target in targets
            if _validate_target(source, target, force)
        ]
    prepared: list[_PreparedTarget] = []
    try:
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
    except Exception:
        _cleanup_prepared(prepared)
        raise
    return prepared


def _commit_prepared(prepared: list[_PreparedTarget]) -> None:
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


def install_targets(
    targets: list[tuple[Path, Path]], link: bool, force: bool
) -> None:
    prepared: list[_PreparedTarget] = []
    try:
        prepared = _prepare_targets(targets, link=link, force=force)
        _commit_prepared(prepared)
    finally:
        _cleanup_prepared(prepared)

    for plan in prepared:
        if link:
            print(f"Linked {plan.target} -> {plan.source}")
        else:
            print(f"Copied {plan.source} -> {plan.target}")


def install_target(source: Path, target: Path, link: bool, force: bool) -> None:
    install_targets([(source, target)], link=link, force=force)


def _runtime_subprocess_env(staging_root: Path) -> dict[str, str]:
    home = staging_root / "home"
    temp = staging_root / "tmp"
    home.mkdir()
    temp.mkdir()
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(temp),
    }


def _require_recognition_lock() -> None:
    if RECOGNITION_LOCK.is_symlink() or not RECOGNITION_LOCK.is_file():
        raise RuntimeError(f"recognition dependency lock is missing: {RECOGNITION_LOCK}")


def install_recognition_runtime(
    target: Path,
    *,
    python_executable: Path = Path(sys.executable),
    runner=subprocess.run,
) -> None:
    _require_recognition_lock()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.install-", dir=target.parent)
    )
    staged = staging_root / "venv"
    backup = staging_root / "previous"
    environment = _runtime_subprocess_env(staging_root)
    backed_up = False
    try:
        commands = [
            [str(python_executable), "-m", "venv", str(staged)],
            [
                str(staged / "bin" / "python"),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-input",
                "--index-url",
                "https://pypi.org/simple",
                "--require-hashes",
                "--only-binary=:all:",
                "-r",
                str(RECOGNITION_LOCK),
            ],
            [
                str(staged / "bin" / "python"),
                "-I",
                "-c",
                (
                    "from importlib import metadata, util; "
                    "assert metadata.version('shazamio') == '0.8.1'; "
                    "assert metadata.version('shazamio-core') == '1.1.2'; "
                    "assert util.find_spec('shazamio') is not None; "
                    "assert util.find_spec('shazamio_core') is not None; "
                    "print('shazamio')"
                ),
            ],
            [
                str(staged / "bin" / "python"),
                "-I",
                "-m",
                "pip",
                "check",
            ],
        ]
        for command in commands:
            completed = runner(
                command,
                check=False,
                env=environment,
                stdin=subprocess.DEVNULL,
            )
            if completed.returncode != 0:
                raise RuntimeError("could not install the ambient recognition runtime")
        if target.exists() or target.is_symlink():
            target.rename(backup)
            backed_up = True
        try:
            staged.rename(target)
        except Exception:
            if backed_up and (backup.exists() or backup.is_symlink()):
                backup.rename(target)
                backed_up = False
            raise
        if backup.exists() or backup.is_symlink():
            remove_target(backup)
            backed_up = False
    finally:
        if not backed_up:
            shutil.rmtree(staging_root, ignore_errors=True)


def install_bundle(
    targets: list[tuple[Path, Path]],
    recognition_target: Path,
    *,
    link: bool,
    force: bool,
    obsolete_targets: list[Path] | None = None,
) -> None:
    _require_recognition_lock()
    _validate_recognition_target(recognition_target, targets)
    installable = [
        (source, target)
        for source, target in targets
        if _validate_target(source, target, force)
    ]
    runtime_existed = recognition_target.exists() or recognition_target.is_symlink()
    if runtime_existed and not force:
        raise RuntimeError(
            f"{recognition_target} already exists; rerun with --force to replace it"
        )

    prepared: list[_PreparedTarget] = []
    removals: list[_PreparedRemoval] = []
    try:
        prepared = _prepare_targets(
            installable,
            link=link,
            force=force,
            validated=True,
        )
        recognition_target.parent.mkdir(parents=True, exist_ok=True)
        runtime_staging_root = Path(
            tempfile.mkdtemp(
                prefix=f".{recognition_target.name}.install-",
                dir=recognition_target.parent,
            )
        )
        runtime_plan = _PreparedTarget(
            source=RECOGNITION_LOCK,
            target=recognition_target,
            staging_root=runtime_staging_root,
            staged=runtime_staging_root / "payload",
            backup=runtime_staging_root / "previous",
            target_existed=runtime_existed,
        )
        prepared.append(runtime_plan)
        install_recognition_runtime(runtime_plan.staged)
        removals = _quarantine_obsolete_targets(obsolete_targets or [])
        try:
            _commit_prepared(prepared)
        except Exception as commit_error:
            rollback_error = _restore_removals(removals)
            if rollback_error is not None:
                raise RuntimeError(
                    "installation failed and obsolete-plugin rollback was incomplete: "
                    f"{rollback_error}"
                ) from commit_error
            raise
        for plan in removals:
            if plan.backup.exists() or plan.backup.is_symlink():
                try:
                    remove_target(plan.backup)
                    plan.backed_up = False
                except OSError as exc:
                    print(
                        f"warning: could not remove obsolete plugin backup: {exc}",
                        file=sys.stderr,
                    )
    finally:
        _cleanup_prepared(prepared)
        _cleanup_removals(removals)

    for plan in prepared[:-1]:
        if link:
            print(f"Linked {plan.target} -> {plan.source}")
        else:
            print(f"Copied {plan.source} -> {plan.target}")
    print(f"Installed recognition runtime -> {recognition_target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", action="store_true", help="symlink for local development")
    parser.add_argument("--force", action="store_true", help="replace an existing installation")
    parser.add_argument("--no-enable", action="store_true", help="do not enable the backend")
    args = parser.parse_args()

    hermes_home = resolve_hermes_home()
    backend_target = hermes_home / "plugins" / PLUGIN_ID
    desktop_target = hermes_home / "desktop-plugins" / PLUGIN_ID
    recognition_target = (
        hermes_home / "plugin-data" / PLUGIN_ID / "recognition-venv"
    )

    try:
        targets = [(ROOT, backend_target)]
        install_bundle(
            targets,
            recognition_target,
            link=args.link,
            force=args.force,
            obsolete_targets=[desktop_target],
        )
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
