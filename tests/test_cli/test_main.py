from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tokenize
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from app import cli
from app.cli import main
from tests.test_cli.support import FakeRegistry, remote_env, run, skill_tree

ROOT = Path(__file__).resolve().parents[2]
CLI_SOURCE = ROOT / "app" / "cli.py"
COMMANDS = ["validate", "pack", "publish", "search", "download", "pull"]


def version_text() -> str:
    return f"alx {metadata.version('alexandria')}"


class TestHelpAndVersion:
    def test_no_arguments_is_a_usage_error(self):
        result = run()

        assert result.code == 2
        assert result.out == ""
        assert "usage: alx" in result.err
        assert "COMMAND" in result.err

    def test_help_lists_every_command_and_the_exit_statuses(self):
        result = run("--help")

        assert result.code == 0
        assert result.err == ""
        for command in COMMANDS:
            assert command in result.out
        assert "exit status" in result.out

    @pytest.mark.parametrize("flag", ["--version", "-V"])
    def test_the_version_is_printed_on_stdout(self, flag: str):
        result = run(flag)

        assert result.code == 0
        assert result.out == version_text() + "\n"
        assert result.err == ""

    @pytest.mark.parametrize(
        ("command", "options"),
        [
            ("validate", ["--version", "--files", "--json"]),
            ("pack", ["--output", "--version", "--json"]),
            ("publish", ["--url", "--token", "--version", "--publish", "--json"]),
            ("search", ["--kind", "--tag", "--limit", "--offset", "--url", "--token", "--json"]),
            ("download", ["--output", "--url", "--token", "--json"]),
            ("pull", ["--dir", "--force", "--url", "--token", "--json"]),
        ],
    )
    def test_each_command_documents_its_options(self, command: str, options: list[str]):
        result = run(command, "--help")

        assert result.code == 0
        assert result.err == ""
        assert f"alx {command}" in result.out
        for option in options:
            assert option in result.out

    def test_the_help_explains_the_environment_variables(self):
        text = run("publish", "--help").out

        assert "ALX_URL" in text and "ALX_TOKEN" in text


class TestUsageErrors:
    @pytest.mark.parametrize(
        "argv",
        [
            ["frobnicate"],
            ["validate", "--nonsense", "x"],
            ["pack"],
            ["pack", "a", "b"],
            ["publish"],
            ["search", "--limit"],
            ["download"],
            ["pull"],
            ["pull", "x", "--dir"],
            ["--json", "validate", "x"],
            ["validate", "x", "--jso"],
            ["search", "--lim", "3"],
            ["publish", "x", "--tok", "t"],
        ],
    )
    def test_malformed_command_lines_exit_with_two(self, argv: list[str]):
        result = run(*argv)

        assert result.code == 2
        assert result.out == ""
        assert "error:" in result.err
        assert "Traceback" not in result.err

    def test_usage_errors_name_the_command(self, tmp_path: Path):
        result = run("validate", tmp_path / "missing")

        assert result.err.startswith("alx validate: error: ")


class TestSafetyNet:
    def test_no_traceback_is_ever_shown_for_unexpected_failures(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(cli, "load_package", explode)

        result = run("validate", tmp_path)

        assert result.code == 1
        assert result.out == ""
        assert result.err == "error: INTERNAL_ERROR: unexpected failure (RuntimeError): boom\n"

    def test_unexpected_failures_are_json_in_json_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setattr(cli, "load_package", lambda *a, **k: 1 / 0)

        result = run("validate", tmp_path, "--json")

        assert result.code == 1
        error = json.loads(result.err)["error"]
        assert error["code"] == "INTERNAL_ERROR"
        assert "ZeroDivisionError" in error["message"]

    def test_the_debug_variable_lets_unexpected_failures_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setattr(cli, "load_package", lambda *a, **k: 1 / 0)

        with pytest.raises(ZeroDivisionError):
            run("validate", tmp_path, env={"ALX_DEBUG": "1"})

    def test_a_keyboard_interrupt_exits_with_130(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "load_package", interrupt)

        result = run("validate", tmp_path)

        assert result.code == 130
        assert result.err == "interrupted\n"

    def test_a_closed_pipe_is_silent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        def closed(*args, **kwargs):
            raise BrokenPipeError

        monkeypatch.setattr(cli, "load_package", closed)

        result = run("validate", tmp_path)

        assert result.code == 1
        assert result.out == "" and result.err == ""

    def test_operating_system_errors_are_summarised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        def fail(*args, **kwargs):
            raise PermissionError(13, "Permission denied", "/some/where\x1b")

        monkeypatch.setattr(cli, "load_package", fail)

        result = run("validate", tmp_path)

        assert result.code == 1
        assert result.err == "error: Permission denied: '/some/where?'\n"

    def test_operating_system_errors_without_a_file_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        def fail(*args, **kwargs):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(cli, "load_package", fail)

        assert run("validate", tmp_path).err == "error: Input/output error\n"

    def test_the_package_version_has_a_fallback(self, monkeypatch: pytest.MonkeyPatch):
        def missing(name: str) -> str:
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setattr(cli.metadata, "version", missing)

        assert run("--version").out == f"alx {cli.FALLBACK_VERSION}\n"

    def test_streams_that_cannot_be_reconfigured_are_tolerated(self, tmp_path: Path):
        class Stubborn(io.StringIO):
            def reconfigure(self, **kwargs):
                raise ValueError("no")

        out, err = Stubborn(), Stubborn()

        code = main(["validate", str(skill_tree(tmp_path / "pkg"))], env={}, stdout=out, stderr=err)

        assert code == 0
        assert out.getvalue().startswith("OK: ")


class TestEntryPoints:
    def test_the_default_arguments_and_streams_come_from_the_process(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        monkeypatch.setattr(sys, "argv", ["alx", "--version"])

        code = main()

        captured = capsys.readouterr()
        assert code == 0
        assert captured.out == version_text() + "\n"
        assert captured.err == ""

    def test_the_default_environment_is_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: FakeRegistry
    ):
        monkeypatch.setenv("ALX_URL", "http://localhost:5555")
        monkeypatch.setenv("ALX_TOKEN", "alx_write")
        source = skill_tree(tmp_path / "pkg")
        out, err = io.StringIO(), io.StringIO()

        code = main(["publish", str(source)], transport=registry.transport, stdout=out, stderr=err)

        assert code == 0
        assert registry.last_request().url.port == 5555
        assert registry.last_request().headers["Authorization"] == "Bearer alx_write"

    def test_the_environment_of_the_caller_does_not_leak_into_isolated_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: FakeRegistry
    ):
        monkeypatch.setenv("ALX_TOKEN", "alx_write")
        source = skill_tree(tmp_path / "pkg")

        result = run("publish", source, transport=registry.transport, env={})

        assert result.code == 2
        assert registry.requests == []

    def test_the_module_can_be_executed(self):
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "app.cli", "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0
        assert completed.stdout == version_text() + "\n"
        assert completed.stderr == ""

    def test_the_installed_script_runs(self, tmp_path: Path):
        script = Path(sys.executable).with_name("alx")
        if not script.exists():
            pytest.skip("the alx script is not installed in this environment")
        source = skill_tree(tmp_path / "pkg")

        completed = subprocess.run(
            [str(script), "validate", str(source), "--json"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

        assert completed.returncode == 0
        assert json.loads(completed.stdout)["name"] == "demo-skill"

    def test_the_exit_code_of_the_process_follows_the_result(self, tmp_path: Path):
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "app.cli", "validate", str(tmp_path / "missing")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 2
        assert "no such file or directory" in completed.stderr

    def test_the_console_script_is_declared(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())

        assert project["project"]["scripts"]["alx"] == "app.cli:main"
        assert callable(cli.main)

    def test_importing_the_cli_does_not_load_the_server(self):
        code = (
            "import sys, app.cli\n"
            "loaded = sorted(name for name in sys.modules "
            "if name.split('.')[0] in {'app', 'fastapi', 'starlette', 'sqlalchemy', 'mcp'})\n"
            "print('\\n'.join(loaded))\n"
        )

        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        assert completed.stdout.split() == [
            "app",
            "app.cli",
            "app.services",
            "app.services.errors",
            "app.services.package_inspector",
            "app.services.semver",
        ]

    def test_the_cli_does_not_read_files_from_the_project_on_import(self, tmp_path: Path):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", "import app.cli"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )

        assert completed.returncode == 0
        assert list(tmp_path.iterdir()) == []


class TestSourceRules:
    def test_the_module_has_no_comments(self):
        source = CLI_SOURCE.read_text(encoding="utf-8")

        comments = [
            (token.start[0], token.string)
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        ]

        assert comments == []

    def test_the_module_has_no_docstrings(self):
        tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))

        documented = [
            getattr(node, "name", "<module>")
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and ast.get_docstring(node, clean=False) is not None
        ]

        assert documented == []

    def test_the_module_depends_on_neither_the_server_nor_the_database(self):
        tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
        imported = {
            name.split(".")[0] if not name.startswith("app") else name
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
        }

        assert imported.isdisjoint({"fastapi", "starlette", "sqlalchemy", "uvicorn", "mcp"})
        assert {name for name in imported if name.startswith("app")} == {
            "app.services",
            "app.services.errors",
            "app.services.package_inspector",
        }

    def test_the_only_asynchronous_code_is_the_bridge_for_asynchronous_transports(self):
        tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))

        asynchronous = [
            (owner.name, function.name)
            for owner in ast.walk(tree)
            if isinstance(owner, ast.ClassDef)
            for function in owner.body
            if isinstance(function, ast.AsyncFunctionDef)
        ]

        assert asynchronous == [("_AsyncBridge", "_exchange")]
        assert len([n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]) == 1

    def test_the_remote_commands_never_print_the_token(self):
        source = CLI_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        printed_names = {
            sub.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
            for argument in node.args
            for sub in ast.walk(argument)
            if isinstance(sub, ast.Name)
        }

        assert "token" not in printed_names
        assert "remote" not in printed_names


class TestEnvironmentHandling:
    def test_empty_environment_values_fall_back_to_defaults(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        source = skill_tree(tmp_path / "pkg")

        result = run(
            "publish",
            source,
            transport=registry.transport,
            env={"ALX_URL": "", "ALX_TOKEN": "alx_write"},
        )

        assert result.code == 0
        assert str(registry.last_request().url) == "http://localhost:8000/api/v1/packages"

    def test_a_flag_url_overrides_an_invalid_environment_url(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        source = skill_tree(tmp_path / "pkg")

        result = run(
            "publish",
            source,
            "--url",
            "http://localhost:8001",
            transport=registry.transport,
            env=remote_env(url="not a url"),
        )

        assert result.code == 0

    def test_an_invalid_environment_url_is_a_usage_error(
        self, registry: FakeRegistry, tmp_path: Path
    ):
        source = skill_tree(tmp_path / "pkg")

        result = run("publish", source, transport=registry.transport, env=remote_env(url="nope"))

        assert result.code == 2
        assert "invalid registry URL" in result.err
