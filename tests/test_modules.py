import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hostexec import detect, modules
from hostexec.modules import dotnet, make, npm, python


def fake_which(tool):
    return f"/usr/bin/{tool}"


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        patcher = mock.patch("hostexec.modules.shutil.which", fake_which)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, content):
        (self.repo / name).write_text(content, encoding="utf-8")


class NpmTest(FixtureCase):
    def test_detect_needs_scripts(self):
        self.assertFalse(npm.detect(self.repo))
        self.write("package.json", json.dumps({"name": "x"}))
        self.assertFalse(npm.detect(self.repo))
        self.write("package.json", json.dumps({"scripts": {"test": "jest"}}))
        self.assertTrue(npm.detect(self.repo))

    def test_translate_and_describe(self):
        self.write("package.json", json.dumps({"scripts": {"build": "tsc", "test": "jest"}}))
        self.assertEqual(npm.translate(self.repo, "build", []), ["/usr/bin/npm", "run", "build"])
        self.assertEqual(npm.describe(self.repo), {"build": "npm run build", "test": "npm run test"})

    def test_manager_from_lockfile(self):
        self.write("package.json", json.dumps({"scripts": {"build": "tsc"}}))
        self.write("pnpm-lock.yaml", "")
        self.assertEqual(npm.translate(self.repo, "build", [])[0], "/usr/bin/pnpm")
        (self.repo / "pnpm-lock.yaml").unlink()
        self.write("yarn.lock", "")
        self.assertEqual(npm.describe(self.repo), {"build": "yarn run build"})

    def test_refusals(self):
        self.write("package.json", json.dumps({"scripts": {"build": "tsc"}}))
        with self.assertRaisesRegex(ValueError, "no 'test' script"):
            npm.translate(self.repo, "test", [])
        with self.assertRaisesRegex(ValueError, "no arguments"):
            npm.translate(self.repo, "build", ["--watch"])
        with self.assertRaisesRegex(ValueError, "unknown verb"):
            npm.translate(self.repo, "deploy", [])
        self.write("package.json", "{broken")
        self.assertFalse(npm.detect(self.repo))
        with self.assertRaisesRegex(ValueError, "cannot read"):
            npm.translate(self.repo, "build", [])

    def test_missing_tool(self):
        self.write("package.json", json.dumps({"scripts": {"build": "tsc"}}))
        with mock.patch("hostexec.modules.shutil.which", lambda tool: None):
            with self.assertRaisesRegex(ValueError, "not installed"):
                npm.translate(self.repo, "build", [])


class DotnetTest(FixtureCase):
    def test_single_solution_wins_over_projects(self):
        self.write("app.sln", "")
        self.write("a.csproj", "")
        self.write("b.csproj", "")
        self.assertTrue(dotnet.detect(self.repo))
        self.assertEqual(dotnet.translate(self.repo, "build", []), ["/usr/bin/dotnet", "build", "app.sln"])
        self.assertEqual(dotnet.describe(self.repo), {"build": "dotnet build app.sln", "test": "dotnet test app.sln"})

    def test_single_project(self):
        self.write("lib.csproj", "")
        self.assertEqual(dotnet.translate(self.repo, "test", []), ["/usr/bin/dotnet", "test", "lib.csproj"])

    def test_ambiguous(self):
        self.write("a.sln", "")
        self.write("b.sln", "")
        self.assertFalse(dotnet.detect(self.repo))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            dotnet.translate(self.repo, "build", [])

    def test_configuration(self):
        self.write("app.sln", "")
        for args in (["-c", "Release"], ["--configuration", "Release"], ["--configuration=Release"]):
            self.assertEqual(dotnet.translate(self.repo, "build", args)[-2:], ["--configuration", "Release"])
        for args in (["-c", "Prod"], ["-c", "Release; rm -rf /"], ["--no-restore"], ["-c"]):
            with self.assertRaises(ValueError):
                dotnet.translate(self.repo, "build", args)


class MakeTest(FixtureCase):
    def test_targets(self):
        self.assertFalse(make.detect(self.repo))
        self.write("Makefile", "all: build\n\nbuild:\n\t$(CC) main.c\n\ntest :\n\t./run\n\nbuilder:\n\ttrue\n")
        self.assertTrue(make.detect(self.repo))
        self.assertEqual(make.describe(self.repo), {"build": "make build", "test": "make test"})
        self.assertEqual(make.translate(self.repo, "test", []), ["/usr/bin/make", "test"])

    def test_missing_target(self):
        self.write("Makefile", "build:\n\ttrue\n")
        with self.assertRaisesRegex(ValueError, "no 'test' target"):
            make.translate(self.repo, "test", [])
        self.write("Makefile", "clean:\n\trm -f *.o\n")
        self.assertFalse(make.detect(self.repo))


class PythonTest(FixtureCase):
    def test_detect(self):
        self.assertFalse(python.detect(self.repo))
        self.write("pytest.ini", "[pytest]\n")
        self.assertTrue(python.detect(self.repo))

    def test_translate(self):
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        with mock.patch("hostexec.modules.python._available", lambda package: True):
            self.assertEqual(python.translate(self.repo, "test", []), [sys.executable, "-m", "pytest"])
            self.assertEqual(python.translate(self.repo, "build", []), [sys.executable, "-m", "build"])
            self.assertEqual(python.describe(self.repo), {"build": "python -m build", "test": "python -m pytest"})
        with mock.patch("hostexec.modules.python._available", lambda package: package == "pytest"):
            self.assertEqual(python.describe(self.repo), {"test": "python -m pytest"})
            with self.assertRaisesRegex(ValueError, "build package is not installed"):
                python.translate(self.repo, "build", [])

    def test_build_needs_pyproject(self):
        self.write("pytest.ini", "[pytest]\n")
        with mock.patch("hostexec.modules.python._available", lambda package: True):
            self.assertEqual(python.describe(self.repo), {"test": "python -m pytest"})
            with self.assertRaisesRegex(ValueError, "pyproject.toml"):
                python.translate(self.repo, "build", [])


class DetectTest(FixtureCase):
    def test_no_match(self):
        self.assertIsNone(detect.pick(self.repo, forced=""))

    def test_priority(self):
        self.write("Makefile", "build:\n\ttrue\n")
        self.assertIs(detect.pick(self.repo, forced=""), make)
        self.write("app.sln", "")
        self.assertIs(detect.pick(self.repo, forced=""), dotnet)
        self.write("package.json", json.dumps({"scripts": {"build": "tsc"}}))
        self.assertIs(detect.pick(self.repo, forced=""), npm)

    def test_forced(self):
        self.write("package.json", json.dumps({"scripts": {"build": "tsc"}}))
        self.assertIs(detect.pick(self.repo, forced="make"), make)
        with mock.patch.dict("os.environ", {"CLAUDE_HOST_MODULE": "python"}):
            self.assertIs(detect.pick(self.repo), python)
        with self.assertRaises(LookupError):
            detect.pick(self.repo, forced="gradle")

    def test_registry(self):
        self.assertEqual([module.NAME for module in modules.all_modules()], list(modules.NAMES))
        self.assertIsNone(modules.find("nope"))


if __name__ == "__main__":
    unittest.main()
