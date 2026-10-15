from click.testing import CliRunner

from telegrief.cli import main


def test_help_lists_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for name in ("login", "run-now", "serve"):
        assert name in result.output


def test_run_now_help_shows_options() -> None:
    result = CliRunner().invoke(main, ["run-now", "--help"])
    assert result.exit_code == 0
    assert "--folder" in result.output
    assert "--from-h" in result.output
    assert "--to-h" in result.output
