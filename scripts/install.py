#!/usr/bin/env python3
"""Transactionally install Lyrics for Hermes into the selected Hermes profile."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

PLUGIN_ID = "lyrics-for-hermes"
LEGACY_PLUGIN_ID = "apple-music-lyrics"
ROOT = Path(__file__).resolve().parents[1]


def remove_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _profile_home_from_cli(hermes: str, *, environment: dict[str, str] | None = None) -> Path | None:
    try:
        completed = subprocess.run(
            [hermes, "config", "path"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    config_path = Path(value).expanduser()
    if not config_path.is_absolute() or config_path.name != "config.yaml":
        return None
    return config_path.parent.resolve()


def resolve_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured is not None and configured.strip():
        return Path(configured).expanduser().resolve()

    hermes = shutil.which("hermes")
    if hermes:
        active_home = _profile_home_from_cli(hermes)
        if active_home is not None:
            return active_home

    return (Path.home() / ".hermes").resolve()


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
        target_existed: bool,
    ) -> None:
        self.source = source
        self.target = target
        self.staging_root = staging_root
        self.staged = staging_root / "payload"
        self.backup = staging_root / "previous"
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


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve()
    second_resolved = second.resolve()
    return (
        first_resolved == second_resolved
        or first_resolved.is_relative_to(second_resolved)
        or second_resolved.is_relative_to(first_resolved)
    )


def _lexical_paths_overlap(first: Path, second: Path) -> bool:
    first_absolute = Path(os.path.abspath(first))
    second_absolute = Path(os.path.abspath(second))
    return (
        first_absolute == second_absolute
        or first_absolute.is_relative_to(second_absolute)
        or second_absolute.is_relative_to(first_absolute)
    )


def _validate_target(source: Path, target: Path, force: bool) -> bool:
    if not source.is_dir():
        raise RuntimeError(f"source directory does not exist: {source}")
    if target.is_symlink() and target.resolve() == source.resolve():
        print(f"Already linked: {target}")
        return False
    if _paths_overlap(source, target):
        raise RuntimeError(
            f"source and target directories overlap: {source} -> {target}; "
            "choose a HERMES_HOME outside the checkout"
        )
    if (target.exists() or target.is_symlink()) and not force:
        raise RuntimeError(f"{target} already exists; rerun with --force to replace it")
    return True


def _validate_obsolete_targets(
    obsolete_targets: list[Path], targets: list[tuple[Path, Path]]
) -> None:
    seen: set[Path] = set()
    for obsolete in obsolete_targets:
        absolute = Path(os.path.abspath(obsolete))
        if absolute in seen:
            raise RuntimeError(f"duplicate legacy target: {obsolete}")
        seen.add(absolute)
        for source, target in targets:
            if _lexical_paths_overlap(obsolete, source) or _lexical_paths_overlap(
                obsolete, target
            ):
                raise RuntimeError(
                    f"legacy target overlaps a plugin source or target: {obsolete}"
                )


def _validate_profile_target_ancestors(
    hermes_home: Path,
    targets: list[Path],
) -> None:
    """Reject target parents that escape the selected profile through symlinks."""
    profile_absolute = Path(os.path.abspath(hermes_home))
    profile_root = hermes_home.resolve()
    for target in targets:
        absolute = Path(os.path.abspath(target))
        try:
            relative = absolute.relative_to(profile_absolute)
        except ValueError as exc:
            raise RuntimeError(
                f"plugin target escapes the selected Hermes profile: {target}"
            ) from exc
        current = profile_root
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                raise RuntimeError(
                    f"plugin target has a symlinked profile ancestor: {current}"
                )
        try:
            absolute.parent.resolve().relative_to(profile_root)
        except ValueError as exc:
            raise RuntimeError(
                f"plugin target parent escapes the selected Hermes profile: {target}"
            ) from exc


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
                    f"cannot restore legacy plugin path because it reappeared: {plan.target}"
                )
            plan.backup.rename(plan.target)
            plan.backed_up = False
        except Exception as exc:  # preserve recovery evidence when rollback itself fails
            rollback_error = rollback_error or exc
    return rollback_error


def _quarantine_obsolete_targets(targets: list[Path]) -> list[_PreparedRemoval]:
    removals: list[_PreparedRemoval] = []
    try:
        for target in targets:
            if not (target.exists() or target.is_symlink()):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
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
                "could not quarantine legacy plugin paths and rollback was incomplete: "
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
                        ".git",
                        ".venv",
                        ".ruff_cache",
                        "build",
                        "node_modules",
                        "__pycache__",
                        ".DS_Store",
                        "*.egg-info",
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
            except Exception as exc:  # preserve recovery evidence when rollback itself fails
                rollback_error = rollback_error or exc
        if rollback_error is not None:
            raise RuntimeError(
                f"installation failed and rollback was incomplete: {rollback_error}"
            ) from commit_error
        raise


def _rollback_bundle(
    prepared: list[_PreparedTarget], removals: list[_PreparedRemoval]
) -> Exception | None:
    rollback_error: Exception | None = None
    for plan in reversed(prepared):
        try:
            if plan.committed and (plan.target.exists() or plan.target.is_symlink()):
                remove_target(plan.target)
                plan.committed = False
            if plan.backed_up and (plan.backup.exists() or plan.backup.is_symlink()):
                plan.backup.rename(plan.target)
                plan.backed_up = False
        except Exception as exc:  # preserve recovery evidence when rollback itself fails
            rollback_error = rollback_error or exc
    removal_error = _restore_removals(removals)
    return rollback_error or removal_error


def _discard_backups(prepared: list[_PreparedTarget], removals: list[_PreparedRemoval]) -> None:
    for plan in [*prepared, *removals]:
        if plan.backup.exists() or plan.backup.is_symlink():
            try:
                remove_target(plan.backup)
                plan.backed_up = False
            except OSError as exc:
                print(f"warning: could not remove installer backup: {exc}", file=sys.stderr)


def install_bundle(
    targets: list[tuple[Path, Path]],
    *,
    link: bool,
    force: bool,
    obsolete_targets: list[Path] | None = None,
    activate: Callable[[], object] | None = None,
) -> None:
    obsolete = obsolete_targets or []
    _validate_obsolete_targets(obsolete, targets)
    installable = [
        (source, target)
        for source, target in targets
        if _validate_target(source, target, force)
    ]

    prepared: list[_PreparedTarget] = []
    removals: list[_PreparedRemoval] = []
    try:
        prepared = _prepare_targets(
            installable,
            link=link,
            force=force,
            validated=True,
        )
        removals = _quarantine_obsolete_targets(obsolete)
        try:
            _commit_prepared(prepared)
        except Exception as commit_error:
            rollback_error = _restore_removals(removals)
            if rollback_error is not None:
                raise RuntimeError(
                    "installation failed and legacy-path rollback was incomplete: "
                    f"{rollback_error}"
                ) from commit_error
            raise
        if activate is not None:
            try:
                activate()
            except Exception as activation_error:
                rollback_error = _rollback_bundle(prepared, removals)
                if rollback_error is not None:
                    raise RuntimeError(
                        "activation failed and filesystem rollback was incomplete: "
                        f"{rollback_error}"
                    ) from activation_error
                raise
        _discard_backups(prepared, removals)
    finally:
        _cleanup_prepared(prepared)
        _cleanup_removals(removals)

    for plan in prepared:
        if link:
            print(f"Linked {plan.target} -> {plan.source}")
        else:
            print(f"Copied {plan.source} -> {plan.target}")


def install_target(source: Path, target: Path, link: bool, force: bool) -> None:
    install_bundle([(source, target)], link=link, force=force)


def _enabled_plugins(hermes: str, environment: dict[str, str]) -> list[str]:
    completed = subprocess.run(
        [hermes, "config", "get", "--json", "plugins.enabled"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("could not read enabled-plugin configuration")
    try:
        value = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("enabled-plugin configuration is invalid") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("enabled-plugin configuration is invalid")
    return value


def _set_enabled_plugins(
    hermes: str,
    environment: dict[str, str],
    enabled: list[str],
) -> bool:
    payload = json.dumps(enabled, separators=(",", ":"))
    completed = subprocess.run(
        [hermes, "config", "set", "plugins.enabled", payload],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.returncode == 0


def _activate_plugin(hermes: str, hermes_home: Path) -> int:
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(hermes_home)
    active_home = _profile_home_from_cli(hermes, environment=environment)
    if active_home is None or active_home != hermes_home.resolve():
        raise RuntimeError(
            "Hermes active profile does not match the installed filesystem target"
        )

    previous = _enabled_plugins(hermes, environment)
    updated = [item for item in previous if item != LEGACY_PLUGIN_ID]
    if PLUGIN_ID not in updated:
        updated.append(PLUGIN_ID)
    if updated == previous:
        return 0

    if not _set_enabled_plugins(hermes, environment, updated):
        if not _set_enabled_plugins(hermes, environment, previous):
            raise RuntimeError("enabled-plugin update failed and rollback was incomplete")
        raise RuntimeError("enabled-plugin update failed")
    try:
        verified = _enabled_plugins(hermes, environment)
    except RuntimeError as exc:
        if not _set_enabled_plugins(hermes, environment, previous):
            raise RuntimeError(
                "enabled-plugin verification failed and rollback was incomplete"
            ) from exc
        raise RuntimeError("enabled-plugin verification failed") from exc
    if verified != updated:
        if not _set_enabled_plugins(hermes, environment, previous):
            raise RuntimeError("enabled-plugin verification failed and rollback was incomplete")
        raise RuntimeError("enabled-plugin verification failed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--link", action="store_true", help="symlink for local development")
    parser.add_argument("--force", action="store_true", help="replace an existing installation")
    parser.add_argument("--no-enable", action="store_true", help="do not change backend enablement")
    args = parser.parse_args()

    hermes_home = resolve_hermes_home()
    plugin_target = hermes_home / "plugins" / PLUGIN_ID
    legacy_targets = [
        hermes_home / "plugins" / LEGACY_PLUGIN_ID,
        hermes_home / "desktop-plugins" / LEGACY_PLUGIN_ID,
        hermes_home / "plugin-data" / LEGACY_PLUGIN_ID,
    ]

    try:
        _validate_profile_target_ancestors(
            hermes_home,
            [plugin_target, *legacy_targets],
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    hermes = None if args.no_enable else shutil.which("hermes")
    if hermes:
        active_home = _profile_home_from_cli(hermes, environment={**os.environ, "HERMES_HOME": str(hermes_home)})
        if active_home is None or active_home != hermes_home.resolve():
            print(
                "error: Hermes active profile does not match the installed filesystem target",
                file=sys.stderr,
            )
            return 2

    try:
        install_bundle(
            [(ROOT, plugin_target)],
            link=args.link,
            force=args.force,
            obsolete_targets=legacy_targets,
            activate=(lambda: _activate_plugin(hermes, hermes_home)) if hermes else None,
        )
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.no_enable and not hermes:
        print("warning: `hermes` not found; enable the backend manually", file=sys.stderr)

    print("Installed Lyrics for Hermes. Restart Hermes Desktop and its local server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
