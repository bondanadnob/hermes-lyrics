#!/usr/bin/env python3
"""Transactionally install Lyrics for Hermes into the selected Hermes profile."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

PLUGIN_ID = "lyrics-for-hermes"
LEGACY_PLUGIN_ID = "apple-music-lyrics"
ROOT = Path(__file__).resolve().parents[1]

_DEVELOPMENT_ARTIFACT_NAMES = {
    ".DS_Store",
    ".cache",
    ".git",
    ".hermes",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "build",
    "node_modules",
    "wheelhouse",
}

# The published plugin has a deliberately explicit payload boundary. Fixtures
# using the public install_target API retain the generic secure copier below.
_RUNTIME_TOP_LEVEL = {
    "CHANGELOG.md", "LICENSE", "README.md", "SBOM.json", "THIRD_PARTY_NOTICES.md",
    "__init__.py", "dashboard", "desktop", "package-lock.json", "package.json",
    "plugin.yaml", "pyproject.toml", "requirements-ci.in", "requirements-ci.lock", "scripts",
}


def _reject_source_symlinks(source: Path) -> None:
    """Fail closed instead of copying data outside the reviewed source tree."""
    if source.is_symlink():
        raise RuntimeError(f"source tree contains a symlink: {source}")
    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        parent = Path(directory)
        for name in (*directory_names, *file_names):
            candidate = parent / name
            if candidate.is_symlink():
                raise RuntimeError(f"source tree contains a symlink: {candidate}")


def _unsafe_source_entry(path: str) -> RuntimeError:
    return RuntimeError(f"unsafe source entry: {path}")


def _directory_has_pyvenv(directory_fd: int) -> bool:
    try:
        return stat.S_ISREG(os.stat("pyvenv.cfg", dir_fd=directory_fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _copy_source_tree_to_fd(source: Path, destination_fd: int) -> None:
    """Copy source via no-follow descriptors into an already-open directory."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(source, directory_flags)
    except OSError as exc:
        raise _unsafe_source_entry(str(source)) from exc
    restrict_to_runtime = source.resolve() == ROOT.resolve()

    def copy_directory(source_fd: int, output_fd: int, relative: tuple[str, ...]) -> None:
        for name in os.listdir(source_fd):
            if (
                (not relative and restrict_to_runtime and name not in _RUNTIME_TOP_LEVEL)
                or _is_development_artifact(Path(name))
            ):
                continue
            child_relative = (*relative, name)
            display = "/".join(child_relative)
            try:
                entry = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            except OSError as exc:
                raise _unsafe_source_entry(display) from exc
            if stat.S_ISDIR(entry.st_mode):
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=source_fd)
                except OSError as exc:
                    raise _unsafe_source_entry(display) from exc
                try:
                    opened_directory = os.fstat(child_fd)
                    if (
                        opened_directory.st_dev != entry.st_dev
                        or opened_directory.st_ino != entry.st_ino
                    ):
                        raise _unsafe_source_entry(display)
                    if _is_development_artifact(Path(name)) or _directory_has_pyvenv(child_fd):
                        continue
                    os.mkdir(name, stat.S_IMODE(entry.st_mode) & 0o755, dir_fd=output_fd)
                    output_child_fd = os.open(name, directory_flags, dir_fd=output_fd)
                    try:
                        copy_directory(child_fd, output_child_fd, child_relative)
                    finally:
                        os.close(output_child_fd)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(entry.st_mode):
                try:
                    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
                except OSError as exc:
                    raise _unsafe_source_entry(display) from exc
                try:
                    opened_file = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(opened_file.st_mode)
                        or opened_file.st_dev != entry.st_dev
                        or opened_file.st_ino != entry.st_ino
                    ):
                        raise _unsafe_source_entry(display)
                    output_file_fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        stat.S_IMODE(entry.st_mode) & 0o755,
                        dir_fd=output_fd,
                    )
                    try:
                        with (
                            os.fdopen(file_fd, "rb", closefd=False) as input_file,
                            os.fdopen(output_file_fd, "wb", closefd=False) as output_file,
                        ):
                            shutil.copyfileobj(input_file, output_file)
                        completed_file = os.fstat(file_fd)
                        if (
                            completed_file.st_size != opened_file.st_size
                            or completed_file.st_mtime_ns != opened_file.st_mtime_ns
                            or completed_file.st_ctime_ns != opened_file.st_ctime_ns
                        ):
                            raise _unsafe_source_entry(display)
                    finally:
                        os.close(output_file_fd)
                finally:
                    os.close(file_fd)
            else:
                raise _unsafe_source_entry(display)

    try:
        copy_directory(root_fd, destination_fd, ())
    finally:
        os.close(root_fd)


def _copy_source_tree(source: Path, destination: Path) -> None:
    """Copy source into a new directory through descriptor-anchored I/O."""
    destination.mkdir()
    destination_fd = os.open(
        destination,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        _copy_source_tree_to_fd(source, destination_fd)
    finally:
        os.close(destination_fd)


def _is_development_artifact(path: Path) -> bool:
    name = path.name.casefold()
    normalized = name.lstrip(".")
    virtualenv_name = (
        normalized == "venv"
        or normalized.startswith("venv-")
        or normalized.startswith("venv_")
        or normalized.endswith("-venv")
        or normalized.endswith("_venv")
    )
    return (
        name in _DEVELOPMENT_ARTIFACT_NAMES
        or name.endswith((".egg-info", ".dist-info"))
        or virtualenv_name
        or (path.is_dir() and (path / "pyvenv.cfg").is_file())
    )


def _ignore_development_artifacts(directory: str, names: list[str]) -> set[str]:
    parent = Path(directory)
    return {name for name in names if _is_development_artifact(parent / name)}


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
                _copy_source_tree(source, plan.staged)
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


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_entry_at(parent_fd: int, name: str) -> None:
    entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISDIR(entry.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("platform cannot safely remove an anchored directory tree")
        shutil.rmtree(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _unused_entry_name(parent_fd: int, prefix: str) -> str:
    for _ in range(32):
        name = f".{prefix}-{secrets.token_hex(8)}"
        if not _entry_exists(parent_fd, name):
            return name
    raise RuntimeError("could not allocate a private installer entry")


def _open_profile_subdirectory(home_fd: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=home_fd)
    except FileNotFoundError:
        os.mkdir(name, 0o700, dir_fd=home_fd)
        return os.open(name, flags, dir_fd=home_fd)
    except OSError as exc:
        raise RuntimeError(f"profile directory is not a safe local directory: {name}") from exc


def _assert_profile_anchor(home_fd: int, name: str, directory_fd: int) -> None:
    try:
        current = os.stat(name, dir_fd=home_fd, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(f"profile directory changed during installation: {name}") from exc
    anchored = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != anchored.st_dev
        or current.st_ino != anchored.st_ino
    ):
        raise RuntimeError(f"profile directory changed during installation: {name}")


def install_profile_bundle(
    source: Path,
    hermes_home: Path,
    *,
    link: bool,
    force: bool,
    activate: Callable[[], object] | None = None,
) -> None:
    """Install through stable profile directory descriptors with transactional rollback."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        home_fd = os.open(hermes_home, directory_flags)
    except OSError as exc:
        raise RuntimeError("selected Hermes profile is not a safe local directory") from exc

    directory_fds: dict[str, int] = {}
    stage_name: str | None = None
    canonical_backup: str | None = None
    canonical_committed = False
    legacy_backups: list[tuple[int, str, str]] = []
    activation_rollback: Callable[[], object] | None = None
    plugins_fd = -1
    target_existed = False
    already_linked = False
    try:
        for name in ("plugins", "desktop-plugins", "plugin-data"):
            directory_fds[name] = _open_profile_subdirectory(home_fd, name)
        plugins_fd = directory_fds["plugins"]
        for name, descriptor in directory_fds.items():
            _assert_profile_anchor(home_fd, name, descriptor)

        target_existed = _entry_exists(plugins_fd, PLUGIN_ID)
        if target_existed and link:
            try:
                already_linked = Path(os.readlink(PLUGIN_ID, dir_fd=plugins_fd)).resolve() == source.resolve()
            except OSError:
                already_linked = False
        if target_existed and not force and not already_linked:
            raise RuntimeError(
                f"{hermes_home / 'plugins' / PLUGIN_ID} already exists; "
                "rerun with --force to replace it"
            )

        if not already_linked:
            stage_name = _unused_entry_name(plugins_fd, f"{PLUGIN_ID}.install")
            if link:
                os.symlink(source.resolve(), stage_name, target_is_directory=True, dir_fd=plugins_fd)
            else:
                os.mkdir(stage_name, 0o700, dir_fd=plugins_fd)
                staged_fd = os.open(stage_name, directory_flags, dir_fd=plugins_fd)
                try:
                    _copy_source_tree_to_fd(source, staged_fd)
                finally:
                    os.close(staged_fd)

        for name, descriptor in directory_fds.items():
            _assert_profile_anchor(home_fd, name, descriptor)

        for directory_name, descriptor in (
            ("plugins", directory_fds["plugins"]),
            ("desktop-plugins", directory_fds["desktop-plugins"]),
            ("plugin-data", directory_fds["plugin-data"]),
        ):
            if not _entry_exists(descriptor, LEGACY_PLUGIN_ID):
                continue
            backup = _unused_entry_name(descriptor, f"{LEGACY_PLUGIN_ID}.previous")
            os.rename(
                LEGACY_PLUGIN_ID,
                backup,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            legacy_backups.append((descriptor, LEGACY_PLUGIN_ID, backup))

        if not already_linked:
            if target_existed:
                canonical_backup = _unused_entry_name(plugins_fd, f"{PLUGIN_ID}.previous")
                os.rename(
                    PLUGIN_ID,
                    canonical_backup,
                    src_dir_fd=plugins_fd,
                    dst_dir_fd=plugins_fd,
                )
            assert stage_name is not None
            os.rename(stage_name, PLUGIN_ID, src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd)
            stage_name = None
            canonical_committed = True

        for name, descriptor in directory_fds.items():
            _assert_profile_anchor(home_fd, name, descriptor)
        if activate is not None:
            activation_result = activate()
            if callable(activation_result):
                activation_rollback = activation_result
        for name, descriptor in directory_fds.items():
            _assert_profile_anchor(home_fd, name, descriptor)

    except Exception as operation_error:
        rollback_error: Exception | None = None
        activation_rollback_error: Exception | None = None
        if activation_rollback is not None:
            try:
                activation_rollback()
                activation_rollback = None
            except Exception as exc:
                activation_rollback_error = exc
        try:
            if canonical_committed and _entry_exists(plugins_fd, PLUGIN_ID):
                _remove_entry_at(plugins_fd, PLUGIN_ID)
            if canonical_backup is not None and _entry_exists(plugins_fd, canonical_backup):
                os.rename(
                    canonical_backup,
                    PLUGIN_ID,
                    src_dir_fd=plugins_fd,
                    dst_dir_fd=plugins_fd,
                )
                canonical_backup = None
            if stage_name is not None and _entry_exists(plugins_fd, stage_name):
                _remove_entry_at(plugins_fd, stage_name)
                stage_name = None
            for descriptor, original, backup in reversed(legacy_backups):
                if _entry_exists(descriptor, original):
                    raise RuntimeError(f"legacy plugin path reappeared during rollback: {original}")
                if _entry_exists(descriptor, backup):
                    os.rename(backup, original, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        except Exception as exc:
            rollback_error = exc
        if rollback_error is not None or activation_rollback_error is not None:
            raise RuntimeError(
                "profile installation failed and rollback was incomplete: "
                f"activation={activation_rollback_error}; filesystem={rollback_error}"
            ) from operation_error
        raise
    else:
        if canonical_backup is not None and _entry_exists(plugins_fd, canonical_backup):
            _remove_entry_at(plugins_fd, canonical_backup)
        for descriptor, _original, backup in legacy_backups:
            if _entry_exists(descriptor, backup):
                _remove_entry_at(descriptor, backup)
    finally:
        for descriptor in directory_fds.values():
            os.close(descriptor)
        os.close(home_fd)

    target = hermes_home / "plugins" / PLUGIN_ID
    if already_linked:
        print(f"Already linked: {target}")
    elif link:
        print(f"Linked {target} -> {source.resolve()}")
    else:
        print(f"Copied {source} -> {target}")


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


def _activate_plugin(hermes: str, hermes_home: Path) -> Callable[[], object]:
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
        return lambda: None

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

    def rollback_activation() -> None:
        if not _set_enabled_plugins(hermes, environment, previous):
            raise RuntimeError("enabled-plugin rollback failed")
        restored = _enabled_plugins(hermes, environment)
        if restored != previous:
            raise RuntimeError("enabled-plugin rollback verification failed")

    return rollback_activation


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
        # Re-open all profile ancestors immediately before the transaction. This
        # closes the validation/use gap if another process changed a component.
        _validate_profile_target_ancestors(
            hermes_home,
            [plugin_target, *legacy_targets],
        )
        install_profile_bundle(
            ROOT,
            hermes_home,
            link=args.link,
            force=args.force,
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
