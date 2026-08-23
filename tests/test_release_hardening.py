import json
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
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
            workflow,
        )
        self.assertIn(
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5",
            workflow,
        )
        self.assertIn(
            "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4",
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
        self.assertGreaterEqual(lock.count("--hash=sha256:"), 10)

    def test_release_candidate_is_version_0_2_0(self):
        project_version = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        manifest_version = json.loads(
            (ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")
        )["version"]
        plugin_version = next(
            line.split(":", 1)[1].strip()
            for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines()
            if line.startswith("version:")
        )

        self.assertEqual({project_version, manifest_version, plugin_version}, {"0.2.0"})
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [0.2.0]", changelog)
        self.assertIn("Nearby", changelog)


if __name__ == "__main__":
    unittest.main()
