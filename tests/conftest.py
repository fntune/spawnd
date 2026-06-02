"""Shared pytest fixtures and typing for the unit test suite."""
from pathlib import Path
import pytest
from click.testing import CliRunner

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()

@pytest.fixture
def temp_spawnd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runs_dir = tmp_path / '.spawnd' / 'runs'
    _ = runs_dir.mkdir(parents=True)
    _ = monkeypatch.chdir(tmp_path)
    return tmp_path
