#!/usr/bin/env python3
"""Generate deterministic CycloneDX evidence from checked-in lockfile components."""

from __future__ import annotations

import argparse
import base64
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PYTHON_REQUIREMENT = re.compile(r"(?m)^([A-Za-z0-9_.-]+)==([^ \\\n]+)")
PYTHON_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
PYTHON_LICENSES = {
    "annotated-doc": "MIT",
    "annotated-types": "MIT",
    "anyio": "MIT",
    "fastapi": "MIT",
    "idna": "BSD-3-Clause",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "pip": "MIT",
    "pydantic": "MIT",
    "pydantic-core": "MIT",
    "ruff": "MIT",
    "setuptools": "MIT",
    "starlette": "BSD-3-Clause",
    "typing-extensions": "PSF-2.0",
    "typing-inspection": "MIT",
    "wheel": "MIT",
}


def _license_evidence(value: str) -> list[dict]:
    if any(operator in value for operator in (" OR ", " AND ", " WITH ")):
        return [{"expression": value}]
    return [{"license": {"id": value}}]


def _python_components(root: Path) -> list[dict]:
    text = (root / "requirements-ci.lock").read_text(encoding="utf-8")
    matches = list(PYTHON_REQUIREMENT.finditer(text))
    components = []
    for index, match in enumerate(matches):
        name = match.group(1).lower().replace("_", "-")
        version = match.group(2)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        hashes = sorted(set(PYTHON_HASH.findall(text[match.end() : end])))
        component = {
            "bom-ref": f"pkg:pypi/{quote(name)}@{quote(version)}",
            "hashes": [{"alg": "SHA-256", "content": value} for value in hashes],
            "licenses": _license_evidence(PYTHON_LICENSES[name]),
            "name": name,
            "properties": [{"name": "hermes:source-lock", "value": "requirements-ci.lock"}],
            "purl": f"pkg:pypi/{quote(name)}@{quote(version)}",
            "type": "library",
            "version": version,
        }
        components.append(component)
    return components


def _npm_integrity_hash(integrity: object) -> list[dict]:
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        return []
    try:
        content = base64.b64decode(integrity.removeprefix("sha512-"), validate=True).hex()
    except (ValueError, TypeError):
        return []
    return [{"alg": "SHA-512", "content": content}]


def _npm_components(root: Path) -> list[dict]:
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    components = []
    for package_path, package in lock.get("packages", {}).items():
        if not isinstance(package, dict):
            continue
        name = (
            package_path.rsplit("node_modules/", 1)[-1]
            if package_path
            else package.get("name")
        )
        version = package.get("version")
        if not name or not isinstance(version, str):
            continue
        encoded_name = quote(name, safe="/")
        purl = f"pkg:npm/{encoded_name}@{quote(version)}"
        component = {
            "bom-ref": purl,
            "licenses": _license_evidence(str(package.get("license") or "MIT")),
            "name": name,
            "properties": [{"name": "hermes:source-lock", "value": "package-lock.json"}],
            "purl": purl,
            "type": "library",
            "version": version,
        }
        hashes = _npm_integrity_hash(package.get("integrity"))
        if hashes:
            component["hashes"] = hashes
        components.append(component)
    return components


def build_bom(root: Path = ROOT) -> dict:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    project_component = {
        "bom-ref": f"pkg:pypi/{project['name']}@{project['version']}",
        "externalReferences": [
            {
                "type": "vcs",
                "url": "https://github.com/bondanadnob/hermes-lyrics",
            }
        ],
        "licenses": [{"license": {"id": "MIT"}}],
        "name": project["name"],
        "purl": f"pkg:pypi/{project['name']}@{project['version']}",
        "type": "application",
        "version": project["version"],
    }
    components = [project_component, *_python_components(root), *_npm_components(root)]
    components.sort(key=lambda component: component["bom-ref"])
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "components": components,
        "metadata": {"component": project_component},
        "specVersion": "1.5",
        "version": 1,
    }


def render_bom(root: Path = ROOT) -> str:
    return json.dumps(build_bom(root), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def check_bom(root: Path = ROOT) -> bool:
    target = root / "SBOM.json"
    return target.is_file() and target.read_text(encoding="utf-8") == render_bom(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if SBOM.json has drifted")
    args = parser.parse_args()
    target = ROOT / "SBOM.json"
    if args.check:
        return 0 if check_bom(ROOT) else 1
    target.write_text(render_bom(ROOT), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
