"""The full graph loads local configuration before parsing model options."""

import os

import pytest

import main as entrypoint
from main.scripts import run_graph


class _ParsedOptions(Exception):
    """Stop the CLI before it creates clients or calls external services."""


@pytest.mark.parametrize(
    ("file_model", "exported_key", "exported_model", "expected_key", "expected_model"),
    [
        ("openai:file-model", None, None, "file-key", "openai:file-model"),
        ("openai:file-model", "shell-key", "openai:shell-model", "shell-key", "openai:shell-model"),
        ("", None, None, "file-key", None),
    ],
)
def test_main_loads_root_env_from_any_working_directory(
    tmp_path,
    monkeypatch,
    file_model,
    exported_key,
    exported_model,
    expected_key,
    expected_model,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text(
        f"OPENAI_API_KEY=file-key\nSTART_AGENT_MODEL={file_model}\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(run_graph, "PROJECT_ROOT", project_root, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("START_AGENT_MODEL", raising=False)
    if exported_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", exported_key)
    if exported_model is not None:
        monkeypatch.setenv("START_AGENT_MODEL", exported_model)

    options = {}

    def capture_options(parser):
        options["start_model"] = next(
            action.default for action in parser._actions if action.dest == "start_model"
        )
        raise _ParsedOptions

    monkeypatch.setattr(run_graph.argparse.ArgumentParser, "parse_args", capture_options)

    with pytest.raises(_ParsedOptions):
        entrypoint.main()

    assert os.environ["OPENAI_API_KEY"] == expected_key
    assert options["start_model"] == expected_model
