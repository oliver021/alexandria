from __future__ import annotations

import io
import re
import shlex
from pathlib import Path

import pytest
import yaml

from app.cli import _ParserExit, build_parser
from app.config.settings import LimitsSettings
from app.services.errors import PACKAGE_REASONS
from app.services.package_inspector import PackageLimits, inspect_package
from tests.helpers.packages import make_zip

ROOT = Path(__file__).resolve().parents[2]
AUTHORING = ROOT / "docs" / "AUTHORING.md"
EXAMPLES_README = ROOT / "examples" / "README.md"
DOCUMENTS = [AUTHORING, EXAMPLES_README]

COMMANDS = ["validate", "pack", "publish", "search", "download", "pull"]
MCP_TOOLS = [
    "search_skills",
    "get_skill",
    "get_entrypoint",
    "list_files",
    "read_file",
    "get_download_info",
    "list_tags",
    "list_categories",
    "upload_package",
    "publish_version",
    "deprecate_version",
]
REST_PATHS = [
    "/api/v1/auth/login",
    "/api/v1/tokens",
    "/api/v1/packages",
    "/api/v1/skills",
    "/api/v1/info",
    "/api/v1/tags",
    "/api/v1/categories",
    "/restore",
    "/publish",
    "/deprecate",
    "/download",
    "/entrypoint",
    "/files",
    "/raw/",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fenced_blocks(text: str, *languages: str) -> list[str]:
    pattern = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)
    return [
        body for language, body in pattern.findall(text) if not languages or language in languages
    ]


def alx_invocations(text: str) -> list[list[str]]:
    invocations = []
    for block in fenced_blocks(text, "sh"):
        for line in block.replace("\\\n", " ").splitlines():
            stripped = line.strip().removeprefix("$ ")
            if stripped.startswith("alx "):
                command = re.split(r"\s[|>]\s|\s2>", stripped)[0]
                invocations.append(shlex.split(command, comments=True)[1:])
    return invocations


class TestAuthoringGuide:
    def test_the_guide_covers_the_whole_lifecycle(self):
        text = read(AUTHORING)

        for heading in (
            "## 1. Qué es un paquete",
            "## 2. Estructura",
            "## 3. El frontmatter",
            "## 4. Skill o grounding",
            "## 5. Versionado y ciclo de vida",
            "## 6. Límites y errores frecuentes",
            "## 7. Flujo de trabajo con `alx`",
            "## 8. Credenciales y consumo",
            "## 9. Seguridad y buenas prácticas",
            "## 10. Lista de comprobación antes de publicar",
        ):
            assert heading in text

    @pytest.mark.parametrize("reason", PACKAGE_REASONS)
    def test_every_rejection_reason_is_explained(self, reason: str):
        assert f"`{reason}`" in read(AUTHORING)

    @pytest.mark.parametrize("command", COMMANDS)
    def test_every_command_is_documented(self, command: str):
        text = read(AUTHORING)

        assert f"alx {command}" in text

    @pytest.mark.parametrize("tool", MCP_TOOLS)
    def test_every_mcp_tool_is_listed(self, tool: str):
        assert f"`{tool}`" in read(AUTHORING)

    @pytest.mark.parametrize("fragment", REST_PATHS)
    def test_the_rest_endpoints_are_mentioned(self, fragment: str):
        assert fragment in read(AUTHORING)

    @pytest.mark.parametrize(
        "field",
        ["name", "description", "version", "display_name", "tags", "category", "kind"],
    )
    def test_every_frontmatter_field_has_its_row(self, field: str):
        assert f"| `{field}` |" in read(AUTHORING)

    def test_the_documented_limits_are_the_real_defaults(self):
        text = read(AUTHORING)
        limits = PackageLimits()
        settings_limits = LimitsSettings()

        assert f"| Tamaño del zip subido | {limits.max_package_bytes // 1024**2} MiB |" in text
        assert (
            f"| Suma real de los archivos descomprimidos | {limits.max_uncompressed_bytes // 1024**2} MiB |"
            in text
        )
        assert f"| Un archivo descomprimido | {limits.max_file_bytes // 1024**2} MiB |" in text
        assert f"| Número de archivos | {limits.max_files} |" in text
        assert f"| Longitud de una ruta | {limits.max_path_length} caracteres |" in text
        assert (
            f"| Lectura en línea de un archivo de texto | {settings_limits.max_inline_read_bytes // 1024} KiB |"
            in text
        )
        assert "| Bloque de frontmatter | 16 KiB |" in text
        assert "| Cuerpo indexado para la búsqueda | 20 000 caracteres |" in text

    def test_the_environment_variables_are_documented(self):
        text = read(AUTHORING)

        for variable in ("ALX_URL", "ALX_TOKEN", "ALX_DEBUG"):
            assert variable in text

    def test_the_example_frontmatter_of_the_guide_is_a_valid_package(self):
        blocks = [
            block
            for block in fenced_blocks(read(AUTHORING), "markdown")
            if block.startswith("---\n")
        ]

        assert blocks
        for block in blocks:
            parsed = inspect_package(make_zip({"SKILL.md": block}))
            assert parsed.name == "changelog-from-commits"
            assert parsed.version == "1.0.0"
            assert parsed.tags == ["changelog", "git"]
            assert parsed.category == "release-management"
            assert parsed.display_name == "Changelog from Commits"

    def test_the_sample_error_lines_use_real_reasons(self):
        text = read(AUTHORING)

        for reason in re.findall(r"INVALID_PACKAGE \((\w+)\)", text):
            assert reason in PACKAGE_REASONS

    def test_the_tree_diagrams_match_the_example_packages(self):
        text = read(AUTHORING)

        for directory in ("scripts/", "references/", "tables/", "db/"):
            assert directory in text
        assert (ROOT / "examples" / "packages" / "changelog-from-commits" / "scripts").is_dir()
        assert (ROOT / "examples" / "packages" / "http-reference" / "tables").is_dir()


class TestCommandExamples:
    @pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
    def test_the_documents_contain_command_examples(self, document: Path):
        assert len(alx_invocations(read(document))) >= 5

    @pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
    def test_every_command_in_the_documents_is_accepted_by_the_parser(self, document: Path):
        for arguments in alx_invocations(read(document)):
            try:
                build_parser().parse_args(arguments)
            except _ParserExit as exit_request:
                assert exit_request.status == 0, arguments

    def test_the_examples_of_the_readme_name_real_directories(self):
        text = read(EXAMPLES_README)

        for path in re.findall(r"examples/packages/[\w-]+", text):
            assert (ROOT / path).is_dir(), path

    def test_the_readme_commands_run_against_the_real_examples(self):
        for arguments in alx_invocations(read(EXAMPLES_README)):
            if arguments[0] == "validate":
                from tests.test_cli.support import run

                result = run(
                    *[
                        str(ROOT / item) if item.startswith("examples/") else item
                        for item in arguments
                    ]
                )
                assert result.code == 0, (arguments, result.err)


class TestSampleOutputs:
    def test_the_sample_outputs_have_the_real_layout(self, tmp_path: Path):
        from tests.test_cli.support import parse_fields, run, skill_tree

        source = skill_tree(
            tmp_path / "mi-skill", "mi-skill", "1.0.0", tags=["demo"], category="demos"
        )
        validated = run("validate", source)
        text = read(AUTHORING)
        sample = fenced_blocks(text, "sh")[
            next(
                index
                for index, block in enumerate(fenced_blocks(text, "sh"))
                if block.startswith("$ alx validate mi-skill")
            )
        ]

        documented_labels = [line.split("  ")[1].strip() for line in sample.splitlines()[2:]]
        actual_labels = list(parse_fields(validated.out.splitlines()[1:]))
        assert documented_labels == actual_labels

    def test_the_documented_yaml_is_loadable(self):
        for block in fenced_blocks(read(AUTHORING), "markdown"):
            frontmatter = block.split("---\n")[1]
            assert yaml.safe_load(io.StringIO(frontmatter))["name"] == "changelog-from-commits"
