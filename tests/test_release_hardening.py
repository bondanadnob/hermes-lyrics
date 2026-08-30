import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseHardeningContractTests(unittest.TestCase):
    def test_desktop_runtime_dependencies_are_pinned_and_locked(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        dependencies = package["devDependencies"]

        self.assertEqual(
            set(dependencies),
            {"@tanstack/react-query", "jsdom", "react", "react-dom"},
        )
        for name, version in dependencies.items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+$", name)

        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(lock["lockfileVersion"], 3)
        self.assertEqual(package["name"], "hermes-lyrics-tests")
        self.assertEqual(lock["name"], "hermes-lyrics-tests")
        self.assertEqual(lock["packages"][""]["name"], "hermes-lyrics-tests")
        self.assertEqual(lock["packages"][""]["devDependencies"], dependencies)

    def test_npm_test_runs_every_desktop_runtime_gate(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        scripts = package["scripts"]

        self.assertIn("desktop/plugin.js", scripts["test:syntax"])
        self.assertIn("dashboard/dist/index.js", scripts["test:syntax"])
        self.assertIn("desktop_artwork_runtime.mjs", scripts["test:artwork"])
        self.assertIn("desktop_state_failure_runtime.mjs", scripts["test:state"])
        self.assertIn("--import", scripts["test:state"])
        self.assertNotIn("--experimental-loader", scripts["test:state"])
        self.assertEqual(
            scripts["test"],
            "npm run test:syntax && npm run test:artwork && npm run test:state",
        )

    def test_github_actions_installs_and_runs_desktop_runtime_suite(self):
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(
            encoding="utf-8"
        )

        for action in ("actions/checkout", "actions/setup-python", "actions/setup-node"):
            self.assertNotRegex(workflow, rf"{action}@v\d")
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            workflow,
        )
        self.assertIn(
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0",
            workflow,
        )
        self.assertIn(
            "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
            workflow,
        )
        self.assertIn('python-version: "3.11.9"', workflow)
        self.assertIn('node-version: "22.23.2"', workflow)
        self.assertIn(
            "python -m pip install --require-hashes --only-binary=:all: -r requirements-ci.lock",
            workflow,
        )
        self.assertIn(
            "python -m pip install --no-deps --no-build-isolation -e .",
            workflow,
        )
        self.assertIn("npm ci --ignore-scripts", workflow)
        self.assertIn("npm audit --omit=optional --audit-level=high", workflow)
        self.assertIn("python scripts/generate_sbom.py --check", workflow)
        self.assertIn("npm test", workflow)

    def test_python_ci_dependencies_are_exactly_pinned_and_hash_locked(self):
        inputs = {
            line.split("=", 1)[0].strip()
            for line in (ROOT / "requirements-ci.in").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        self.assertEqual(inputs, {"fastapi", "pip", "ruff", "setuptools", "wheel"})

        lock = (ROOT / "requirements-ci.lock").read_text(encoding="utf-8")
        for package in ("fastapi", "pip", "ruff", "setuptools", "wheel"):
            self.assertRegex(lock, rf"(?m)^{package}==\d")
        self.assertIn("fastapi==0.141.1", lock)
        self.assertIn("setuptools==83.0.0", lock)
        self.assertGreaterEqual(lock.count("--hash=sha256:"), 10)

    def test_release_candidate_metadata_is_canonical(self):
        pyproject = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = pyproject["project"]
        manifest = json.loads(
            (ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")
        )
        plugin_version = next(
            line.split(":", 1)[1].strip()
            for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
            if line.startswith("version:")
        )

        self.assertEqual(
            {project["version"], manifest["version"], plugin_version}, {"0.3.1"}
        )
        self.assertEqual(project["name"], "hermes-lyrics")
        self.assertEqual(project["dependencies"], ["fastapi==0.141.1"])
        self.assertEqual(pyproject["build-system"]["requires"], ["setuptools==83.0.0"])
        self.assertEqual(
            project["urls"],
            {"Repository": "https://github.com/bondanadnob/hermes-lyrics"},
        )
        self.assertEqual(manifest["name"], "lyrics-for-hermes")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [0.3.1]", changelog)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("No PyPI/wheel release is published or supported", readme)
        self.assertIn("GitHub source archives", readme)

    def test_release_payload_does_not_ship_profile_environment(self):
        self.assertFalse((ROOT / ".hermes" / "environment.json").exists())

    def test_sbom_contains_project_and_every_locked_component_without_ffmpeg(self):
        sbom = json.loads((ROOT / "SBOM.json").read_text(encoding="utf-8"))
        self.assertEqual(sbom["bomFormat"], "CycloneDX")
        self.assertEqual(sbom["specVersion"], "1.5")
        self.assertEqual(sbom["metadata"]["component"]["name"], "hermes-lyrics")
        self.assertEqual(
            sbom["metadata"]["component"]["licenses"],
            [{"license": {"id": "MIT"}}],
        )

        requirements = (ROOT / "requirements-ci.lock").read_text(encoding="utf-8")
        python_components = {
            (name.lower().replace("_", "-"), version)
            for name, version in re.findall(
                r"(?m)^([A-Za-z0-9_.-]+)==([^ \\\n]+)", requirements
            )
        }
        package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        npm_components = {
            (
                path.rsplit("node_modules/", 1)[-1] if path else package["name"],
                package["version"],
            )
            for path, package in package_lock["packages"].items()
            if "version" in package and (path or "name" in package)
        }
        actual = {(component["name"], component["version"]) for component in sbom["components"]}
        self.assertIn(("hermes-lyrics", "0.3.1"), actual)
        self.assertTrue(python_components <= actual)
        self.assertTrue(npm_components <= actual)
        self.assertFalse(any("ffmpeg" in name.casefold() for name, _version in actual))
        dependencies = [
            component
            for component in sbom["components"]
            if component["name"] != "hermes-lyrics"
        ]
        self.assertGreaterEqual(len(dependencies), 59)
        self.assertTrue(
            all(component.get("licenses") for component in dependencies),
            "every locked dependency must carry reviewed license evidence",
        )

    def test_checked_in_sbom_is_deterministic_and_check_rejects_drift(self):
        command = [sys.executable, str(ROOT / "scripts" / "generate_sbom.py"), "--check"]
        completed = subprocess.run(command, cwd=ROOT, check=False)
        self.assertEqual(completed.returncode, 0)

        spec = importlib.util.spec_from_file_location(
            "sbom_generator", ROOT / "scripts" / "generate_sbom.py"
        )
        assert spec is not None and spec.loader is not None
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        self.assertEqual(generator.render_bom(ROOT), generator.render_bom(ROOT))

        with tempfile.TemporaryDirectory() as temporary:
            drift_root = Path(temporary)
            for name in (
                "requirements-ci.lock",
                "package-lock.json",
                "pyproject.toml",
                "SBOM.json",
            ):
                shutil.copy2(ROOT / name, drift_root / name)
            self.assertTrue(generator.check_bom(drift_root))
            (drift_root / "SBOM.json").write_text("{}\n", encoding="utf-8")
            self.assertFalse(generator.check_bom(drift_root))


if __name__ == "__main__":
    unittest.main()
