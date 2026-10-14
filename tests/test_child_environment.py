"""Subprocesses (java running user-supplied patch bundles) get no secrets."""

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import patcher, secrets_store

TOKEN = "fake-token-for-tests-0201"
KEY = "json://fake-urls-for-tests-0202"
KEPT = {
    "PATH": "/usr/bin:/bin",
    "JAVA_HOME": "/opt/fake-java",
    "HOME": "/home/fake",
    "TMPDIR": "/tmp/fake-tmp",
    "LANG": "de_DE.UTF-8",
    "LC_ALL": "de_DE.UTF-8",
    "JAVA_TOOL_OPTIONS": "-Xmx64m",
}
SRC = Path(__file__).resolve().parent.parent / "src"
PRINT_ENV = "import json, os; print(json.dumps(dict(os.environ)))"


@pytest.fixture
def full_environment(monkeypatch):
    """A process environment holding the secrets and the usual variables."""
    monkeypatch.setenv(secrets_store.GITHUB_TOKEN, TOKEN)
    monkeypatch.setenv(secrets_store.APPRISE_URLS, KEY)
    for name, value in KEPT.items():
        monkeypatch.setenv(name, value)


def test_child_environment_drops_the_secrets_and_keeps_the_rest(full_environment):
    env = secrets_store.child_environment()

    assert secrets_store.GITHUB_TOKEN not in env
    assert secrets_store.APPRISE_URLS not in env
    assert {name: env[name] for name in KEPT} == KEPT


def test_child_environment_is_a_copy_and_leaves_the_process_alone(full_environment):
    env = secrets_store.child_environment()
    env["EXTRA"] = "1"

    assert "EXTRA" not in secrets_store.os.environ
    assert secrets_store.os.environ[secrets_store.GITHUB_TOKEN] == TOKEN


def test_child_environment_matches_names_case_insensitively(monkeypatch):
    monkeypatch.setenv("github_token", TOKEN)

    assert "github_token" not in secrets_store.child_environment()


def test_run_child_gives_a_real_process_no_secrets(full_environment):
    result = patcher.run_child(
        [sys.executable, "-c", PRINT_ENV], capture_output=True, text=True, check=True
    )

    seen = json.loads(result.stdout)
    assert secrets_store.GITHUB_TOKEN not in seen
    assert secrets_store.APPRISE_URLS not in seen
    assert TOKEN not in result.stdout and KEY not in result.stdout
    assert {name: seen[name] for name in KEPT} == KEPT


def test_start_child_gives_a_real_process_no_secrets(full_environment):
    process = patcher.start_child(
        [sys.executable, "-c", PRINT_ENV], stdout=subprocess.PIPE, text=True
    )
    output, _ = process.communicate()

    seen = json.loads(output)
    assert secrets_store.GITHUB_TOKEN not in seen
    assert seen["JAVA_HOME"] == KEPT["JAVA_HOME"]


def test_the_environment_is_not_the_callers_choice():
    with pytest.raises(TypeError):
        patcher.run_child([sys.executable, "-c", "pass"], env={"A": "1"})
    with pytest.raises(TypeError):
        patcher.start_child([sys.executable, "-c", "pass"], env={"A": "1"})


@pytest.mark.skipif(shutil.which("java") is None, reason="java is not installed")
def test_java_still_starts_with_the_filtered_environment(monkeypatch):
    # The real PATH is needed to find java, so the fake environment is not used;
    # a context keeps the suite-wide isolation fixtures in place.
    with monkeypatch.context() as local:
        local.setenv(secrets_store.GITHUB_TOKEN, TOKEN)

        results = patcher.check_dependencies({"settings": {"java_path": "java"}})

    assert results["java"] is True


# Every call site goes through the helpers: the env seen by the fake is filtered.


@pytest.fixture
def captured_env(monkeypatch, full_environment):
    """Fake subprocess.run/Popen that record the `env` they were given."""
    seen: list[dict[str, str] | None] = []

    def fake_run(command, **kwargs):
        seen.append(kwargs.get("env"))
        return SimpleNamespace(stdout='package="com.x"\n', returncode=0)

    class FakePopen:
        def __init__(self, command, **kwargs):
            seen.append(kwargs.get("env"))
            self.stdout = SimpleNamespace(readline=lambda: "")
            self.returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(patcher.subprocess, "run", fake_run)
    monkeypatch.setattr(patcher.subprocess, "Popen", FakePopen)
    return seen


def assert_filtered(seen: list[dict[str, str] | None]) -> None:
    assert seen, "the call site started no subprocess"
    for env in seen:
        assert env is not None
        assert secrets_store.GITHUB_TOKEN not in env
        assert secrets_store.APPRISE_URLS not in env
        assert env["PATH"] == KEPT["PATH"]


def test_check_dependencies_uses_the_filtered_environment(captured_env):
    patcher.check_dependencies({})

    assert_filtered(captured_env)


def test_reading_a_package_id_uses_the_filtered_environment(captured_env):
    patcher._read_package_id("java", "in.apk", "fallback")

    assert_filtered(captured_env)


def test_reading_app_metadata_uses_the_filtered_environment(
    captured_env, data_dir, monkeypatch
):
    (data_dir / "bin").mkdir(exist_ok=True)
    (data_dir / "bin" / "APKEditor.jar").write_bytes(b"")
    (data_dir / "apks" / "icons").mkdir(parents=True, exist_ok=True)
    (data_dir / "apks" / "icons" / "com.x.png").write_bytes(b"")

    patcher.extract_app_metadata(data_dir / "in.apk", "com.x")

    assert_filtered(captured_env)


def test_the_patch_run_uses_the_filtered_environment(captured_env, data_dir):
    paths = patcher.get_source_paths(patcher.BASE_PATCHES_REPO)
    paths[".mpp"].parent.mkdir(parents=True, exist_ok=True)
    paths[".mpp"].write_bytes(b"")
    paths[".json"].write_text(json.dumps({"patches": []}))
    app = {"id": "com.x", "name": "X", "include_patches": [], "exclude_patches": []}
    config = {"sources": []}

    patcher.patch_app(app, {"cli_jar": "bin/cli.jar"}, "in.apk", "out.apk", config)

    assert_filtered(captured_env)


WATCHED_MODULES = {"subprocess", "os", "asyncio", "multiprocessing", "pty"}
SUBPROCESS_NAMES = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
}
OS_NAMES = {"system", "popen", "fork", "forkpty", "startfile"}
OS_PREFIXES = ("exec", "spawn", "posix_spawn")


def _starts_a_process(module: str, name: str) -> bool:
    """Tell whether `module.name` runs another program or forks."""
    if module == "subprocess":
        return name in SUBPROCESS_NAMES
    if module == "os":
        return name in OS_NAMES or name.startswith(OS_PREFIXES)
    if module == "asyncio":
        return name.startswith("create_subprocess")
    return module in ("multiprocessing", "pty")


def _direct_process_calls(path: Path) -> list[str]:
    """List the ways a source file can start a process without the helpers.

    Follows `import subprocess as sp`, `from subprocess import run`, os.spawn*,
    os.exec*, os.posix_spawn*, os.fork*, subprocess.getoutput, multiprocessing
    and pty (any import of the last two counts).
    """
    found = []
    aliases: dict[str, str] = {}
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                root = imported.name.split(".")[0]
                if root in WATCHED_MODULES:
                    aliases[imported.asname or root] = root
                if root in ("multiprocessing", "pty"):
                    found.append(f"{path.name}:{node.lineno} import {root}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            for imported in node.names:
                if root in WATCHED_MODULES and _starts_a_process(root, imported.name):
                    found.append(f"{path.name}:{node.lineno} from {root} import")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        value = node.func.value
        if isinstance(value, ast.Name) and value.id in aliases:
            module = aliases[value.id]
            if module in ("subprocess", "os", "asyncio") and _starts_a_process(
                module, node.func.attr
            ):
                found.append(f"{path.name}:{node.lineno} {module}.{node.func.attr}")
    return found


def test_no_source_file_starts_a_process_except_through_the_helpers():
    found = [
        call
        for path in sorted(SRC.glob("*.py"))
        for call in _direct_process_calls(path)
    ]

    # The two helpers in patcher.py are the only allowed direct calls.
    expected = [
        f"patcher.py:{line} subprocess.{name}" for line, name in _helper_lines()
    ]
    assert sorted(found) == sorted(expected)


def _helper_lines() -> list[tuple[int, str]]:
    """Line numbers of the one `subprocess.run` and `subprocess.Popen` in the helpers."""
    tree = ast.parse((SRC / "patcher.py").read_text())
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "run_child",
            "start_child",
        ):
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in ("run", "Popen")
                ):
                    lines.append((call.lineno, call.func.attr))
    return lines


GUARD_CASES = [
    "import subprocess\nsubprocess.run(['x'])",
    "from subprocess import run",
    "from subprocess import Popen as P",
    "import subprocess as sp\nsp.Popen(['x'])",
    "import subprocess\nsubprocess.getoutput('x')",
    "import os\nos.system('x')",
    "import os\nos.spawnv(0, 'x', [])",
    "import os\nos.posix_spawn('x', [], {})",
    "import os\nos.execv('x', [])",
    "import os\nos.fork()",
    "from os import system",
    "import multiprocessing",
    "from multiprocessing import Process",
    "import pty\npty.spawn(['x'])",
    "import asyncio\nasyncio.create_subprocess_exec('x')",
]


@pytest.mark.parametrize("code", GUARD_CASES)
def test_the_guard_notices_a_way_to_start_a_process(tmp_path, code):
    source = tmp_path / "sneaky.py"
    source.write_text(code + "\n")

    assert _direct_process_calls(source) != []


def test_the_guard_ignores_harmless_code(tmp_path):
    source = tmp_path / "fine.py"
    source.write_text("import os\nimport subprocess\nos.environ.get('x')\n")

    assert _direct_process_calls(source) == []
